# PRD — Carrier Recommendation for Freight Brokers

## 1. The point

A freight broker has a load that needs a truck. Today a coverage rep guesses who to call and
what to pay. Their TMS already holds the answer — years of history showing who runs that lane,
how often, and at what price — but nobody queries it.

This platform turns that dead history into two live answers for any load still looking for a
carrier:

1. **Which of my carriers should I call first, and why?**
2. **What should I expect to pay?**

Both come only from that broker's own data. Both must be explainable — a rep ignores
`score: 0.87`, but acts on *"ran this lane 14 times, last one 6 days ago at $1.12/mi, and their
truck delivered 40 mi from your pickup yesterday."* **The reasoning is the product.**

---

## 2. Scope

### In scope
- Synthetic sync data for 3 fictional TMSs, 11 days
- Chronological, one-file-at-a-time ingestion into a canonical model
- Derived history (lane stats, carrier stats, carrier last-known position)
- Carrier ranking with human-readable reasons
- Price estimate with source and confidence
- Minimal 2-screen web UI
- `DECISIONS.md` + run instructions + one end-to-end check

### Out of scope (deliberate)
| Cut | Why |
|---|---|
| **Shared carrier pool (bonus)** | Biggest complexity in the assignment, explicitly optional. Delivered as a written design in `DECISIONS.md` instead — boundary, threat model, and enforcement — without the build cost. |
| Auth / user accounts | Broker is chosen from a dropdown. Adds nothing to what's graded. |
| Real road routing | No API key, no network dependency. Straight-line × 1.2. |
| Visual design | README: "visual polish counts for nothing." |
| Live TMS API clients | README: assume data is already downloaded. |

---

## 3. Fixed decisions

| Topic | Decision |
|---|---|
| Brokers | 3, one per TMS. `broker_a` → FreightFlow, `broker_b` → HaulDesk, `broker_c` → BrokerOS |
| Dates | Days 1–10 = **2026-07-06 → 2026-07-15** (history). Day 11 = **2026-07-16** (loads to answer for) |
| Stack | Python 3.12 + FastAPI + Postgres 16 + React/Vite (TypeScript), one `docker compose up` |
| Geography | Hardcoded lookup of ~150 Texas Triangle cities/zips → lat, lon, metro. Offline and deterministic |
| Distance | Haversine × 1.2 road factor |
| Persistence | Plain SQL. No ORM, no queue, no cache layer |

---

## 4. Data to generate

**132 sync files** — 3 TMSs × 4 syncs/day (00:00, 06:00, 12:00, 18:00) × 11 days.
Each file holds **1–3 loads** created or changed since the previous sync, in that TMS's own
native shape, named `{YYYY-MM-DD}T{HH-MM}_sync.json`.

Geography is the **Texas Triangle** — DFW, Houston, San Antonio — with stops spread across
suburbs and nearby towns (Grand Prairie, Katy, Schertz, New Braunfels, Pasadena, Sugar Land,
Conroe, Waco…), not just the three city centers.

### Required scenarios
The data is written as **test fixtures for our own system**, not noise. Every behavior we claim
must have data proving it.

| # | Scenario | Purpose |
|---|---|---|
| 1 | **Full lifecycle across syncs** — a load moves PLANNED → ACTIVE → COVERED → IN_TRANSIT → DELIVERED → COMPLETED across separate files, with carrier rate appearing at booking and final amounts at completion | Proves incremental ingestion and late-arriving money |
| 2 | **Corrections** — a rate already recorded changes to a new value in a later sync (all three flavors: TMS A restates `totalBuy`, TMS B appends a negative `ADJUSTMENT` row, TMS C silently restates `bos__Carrier_Rate__c`) | Proves derived stats stay correct after the fact |
| 3 | **Lane contrast** — DFW→Houston rich (25+ loads); San Antonio→Waco thin (1–2 loads) | Proves the pricing tier walk and its confidence signal |
| 4 | **Carrier contrast** — veterans with 20+ loads next to carriers with 1–2 | Proves cold-start shrinkage |
| 5 | **Suburb scatter** — same real lane expressed as Grand Prairie→Katy, Fort Worth→Houston, Irving→Sugar Land | Proves metro clustering beats city-pair matching |
| 6 | **Cross-TMS carrier** — the same MC/DOT number appears under different names/IDs in two TMSs | Supports the pool design doc; must **not** leak between brokers |
| 7 | **Deadhead setup** — a carrier delivers near a day-11 pickup on day 10 | Proves the proximity signal |
| 8 | **Messy edges** — null equipment (TMS C), a `kg` weight unit on a line item, a 3-stop load, an out-of-order `lastModifiedDate` | Proves defensive parsing |

### Day 11
Fresh loads in `ACTIVE` (or each TMS's equivalent), never covered. These are what the system
answers for. Each one is planted to demonstrate a specific behavior — rich lane, thin lane,
deadhead win, cold-start carrier surfacing — and must be traceable back to days 1–10 by hand.

---

## 5. Canonical model

### Statuses
| Canonical | TMS A (FreightFlow) | TMS B (HaulDesk) | TMS C (BrokerOS) |
|---|---|---|---|
| `PLANNED` | Quoting | 10 Entered | Quotes Requested |
| `ACTIVE` | Booking | 20 Open | Ready to Book |
| `COVERED` | Dispatched | 30 Truck Assigned | Booked |
| `IN_TRANSIT` | At Shipper, En Route, At Receiver | 40 Rolling | In Transit |
| `DELIVERED` | Delivered | 50 Unloaded | Delivered, Invoiced |
| `COMPLETED` | Completed | 90 Closed | Paid |

### Normalization rules
| Field | Rule |
|---|---|
| Weight | → **lbs**. TMS B `weight_kg × 2.20462`. TMS C: check `bos__Weight_Units__c` per line item; sum line items |
| Distance | → **miles**. TMS B `dist_km × 0.621371` |
| Equipment | → `DRY_VAN` \| `REEFER` \| `FLATBED` \| `UNKNOWN`. TMS A free-text match, TMS B `V/R/F`, TMS C picklist. **Null stays UNKNOWN, never defaults to dry van** |
| Money | TMS A/C: totals as given. TMS B: **sum** of `rates` line items per side (`pay` → carrier rate, `bill` → customer rate), including negatives |
| Time | → **UTC**. TMS B naive strings are US Central; TMS A carries an offset; TMS C is already UTC |
| Stops | → ordered list. First pickup / last drop drive the lane; middle stops kept but not lane-forming |
| Location | city/state/zip → lat, lon, metro, zip3 via the hardcoded table. Unmatched → geo-null, excluded from lane stats but still shown |

### Tables
```
brokers            id, name, tms_type

sync_files         APPEND-ONLY. id, broker_id, sync_file, synced_at,
                   raw_json, ingested_at
                   UNIQUE (broker_id, sync_file)   -- the idempotency key
                   -- one row per ingested file; the immutable provenance record

sync_events        APPEND-ONLY. id, sync_file_id, broker_id, entity_type,
                   source_entity_id, source_load_id, raw_json, event_seq
                   -- entity_type: LOAD | CARRIER | CUSTOMER | RATE_LINE
                   -- one row per entity changed within a file; every derived
                   -- number traces back here. RATE_LINE carries source_load_id
                   -- so a TMS B rate-only change is representable (see D3)

loads              broker_id, source_load_id, status, equipment, weight_lbs,
                   distance_miles, customer_rate, carrier_rate, carrier_id,
                   customer_id, pickup_*, delivery_*, scheduled/actual dates,
                   last_seen_sync_at
                   -- current truth, rewritten from the newest event

carriers           broker_id, source_carrier_id, name, mc_number, dot_number,
                   phone, home_city, home_state

customers          broker_id, source_customer_id, name

lane_stats         DERIVED. broker_id, tier, origin_key, dest_key, equipment,
                   load_count, rate_per_mile_p25/p50/p75, last_load_at

carrier_stats      DERIVED. broker_id, carrier_id, lane_key, tier, load_count,
                   on_time_count, avg_rate_per_mile, last_load_at,
                   last_delivery_lat/lon/at
```

**Multi-tenancy:** `broker_id` on every row, applied in the repository layer so no query path
can omit it. One broker's data never reaches another's answers.

---

## 6. Ingestion

1. List all sync files across all TMS directories, sort by the timestamp **in the filename**
2. Process **one file at a time**, in order — never bulk-load
3. For each file: parse → adapt to canonical → write raw to `sync_events` → upsert
   `loads`/`carriers`/`customers` → mark affected lane+carrier keys dirty
4. After the file, rebuild `lane_stats` / `carrier_stats` **for dirty keys only**, from
   `sync_events`
5. Re-running an already-ingested file is a no-op (idempotent on `broker_id + sync_file`)

**Why rebuild instead of patch:** a correction that changes a rate we already folded into an
average makes that average wrong in a way you cannot subtract your way out of without keeping
the old value anyway. Recomputing the touched keys from the raw log is correct by construction
and cheap at this scale. `DECISIONS.md` covers what changes at millions of loads.

---

## 7. Lane definition

Three tiers, narrow to wide. Queries walk outward until they hit a minimum sample size, and
**every answer reports which tier it used**.

| Tier | Key | Example |
|---|---|---|
| 1 `ZIP3` | 3-digit zip pair + equipment | `750 → 774`, dry van |
| 2 `METRO` | metro cluster pair + equipment | `DFW → HOU`, dry van |
| 3 `REGION` | triangle-wide + equipment | `TX_TRIANGLE`, dry van |
| 4 `REGION_ANY` | triangle-wide, no equipment filter | `TX_TRIANGLE`, all equipment |

Equipment filters at **every** tier; the fourth rung exists so a rare equipment type still gets
an answer, and it is always low confidence. A load whose own equipment is `UNKNOWN` skips the
filter and says so in its provenance (`DECISIONS.md` D6).

Metro clusters are assigned by the geo table — every zip in the DFW area maps to `DFW`
regardless of city name or (in principle) state. This is what makes Grand Prairie→Katy and
Fort Worth→Houston the same lane, and what keeps El Paso→Houston out of it.

Minimum sample size: **5 loads** to accept a tier.

---

## 8. Carrier ranking

For an `ACTIVE` load, score every carrier the broker has used. Score is 0–100, a weighted sum of
five signals, each 0–1.

| Signal | Weight | Basis |
|---|---|---|
| Lane experience | 0.35 | Loads on this lane, shrunk (below) |
| Recency | 0.20 | Days since their last load on this lane, decayed |
| Equipment match | 0.15 | Has hauled this equipment; `UNKNOWN` neither rewards nor punishes |
| Deadhead | 0.20 | Miles from their last known delivery to this pickup; full credit ≤50 mi, zero ≥250 mi |
| On-time | 0.10 | Actual vs. scheduled delivery, shrunk |

### Cold start
Raw rates on tiny samples are noise — 2-for-2 is not better than 164-for-200. Two different
corrections, because the two signals are different kinds of number (see `DECISIONS.md` D5):

```
experience = n / (n + k)                                    k = 5   # a count, saturating
on_time    = (observed × n + lane_average × k) / (n + k)    k = 5   # a rate, shrunk
```

A carrier with 2 loads sits at 0.29 on experience and near the lane average on on-time, then
climbs as evidence accumulates. A good new carrier still surfaces; it just cannot leapfrog a
proven one on 2 data points.

### Score precision (the presentation contract)

Signals are computed and kept at full precision. The weighted sum is rounded **once**, at the
end, to **one decimal, half-up** — `22.05` → `22.1`, never `22.0`. Half-up because it is what
PostgreSQL `NUMERIC` rounding and a rep with a calculator both do, and because
`data/TRACEABILITY.md` computes at display precision the same way (`DECISIONS.md` D18).

This is a rule both scorers honor, not code they share: the reference scorer in
`backend/scripts/generate_data.py` is independent by design (D12), which is the only reason the
document can catch a formula error. Sharing the rule removes a disagreement that was never about
the model; sharing the code would remove the oracle (D19).

Two consequences worth stating:

- **Rank by the unrounded sum**, tie-broken by carrier id so an order is stable across runs.
  Rounding is monotone, so a rendered list never shows a higher number above a lower one.
- **Round in the API, never in the browser.** A percentage the frontend recomputes is a second
  source of truth for a number that already exists.

### Reasoning
Every ranked carrier returns plain-language reasons, generated from the same numbers that drove
the score — never written independently of it:

> - Ran DFW → Houston **14 times** in the last 90 days (metro-level match)
> - Last load on this lane **6 days ago**
> - Delivered in Baytown **yesterday, 38 mi** from your pickup
> - Averages **$1.12/mi** on this lane
> - **92% on-time** over 14 loads

Carriers scoring near zero are still returned, ranked last, with the reason they're weak.

---

## 9. Price estimate

Same tier walk. From completed/covered loads on the matched tier, take carrier rate per mile:

- **Point estimate** = median × load miles
- **Range** = p25 → p75 × load miles
- **Confidence** = high (≥15 loads) / medium (5–14) / low (<5, or the `REGION`/`REGION_ANY` tiers)

Always returned with: which tier, how many loads backed it, the date range they span, and the
equipment filter. A low-confidence estimate is labeled as such rather than hidden — the broker
decides whether to trust it.

---

## 10. API

```
GET  /api/brokers
GET  /api/loads?broker_id=&status=            list, filterable
GET  /api/loads/{id}                          detail incl. sync history
GET  /api/loads/{id}/recommendations          ranked carriers + reasons
GET  /api/loads/{id}/price-estimate           estimate + range + provenance
POST /api/admin/ingest                        replay all sync files chronologically
GET  /api/health
```

---

## 11. UI

Two screens. No design work.

**Load list** — broker dropdown, status filter, table of loads (id, lane, equipment, dates,
status, customer rate). `ACTIVE` loads visually first-class since they're the ones that need
answering.

**Load detail** —
- Load facts (stops, equipment, weight, miles, dates, rates)
- **Price estimate** — point, range, confidence, and the provenance line ("median of 22 loads
  on DFW → Houston, dry van, last 90 days")
- **Ranked carriers** — score, name, contact, and the bulleted reasons
- **Sync history** — every version of this load as it arrived, so a correction is visible

---

## 12. Deliverables

- [ ] 132 sync files across the 3 TMS directories
- [ ] Data generator (scripted and re-runnable, not hand-written JSON)
- [ ] 3 TMS adapters + canonical model
- [ ] Chronological ingestion with dirty-key rebuild
- [ ] Lane tiering, ranking, pricing
- [ ] FastAPI backend
- [ ] React frontend, 2 screens
- [ ] `docker compose up` brings up everything
- [ ] End-to-end check: fresh DB → ingest → assert a known day-11 load returns the expected top carrier
- [ ] `README` run section
- [ ] `DECISIONS.md` — judgment calls, rejected alternatives, the pool design, honest limits

## 13. Done means

From a clean checkout: `docker compose up`, ingestion runs over all 132 files in order, the UI
lists day-11 `ACTIVE` loads, and each one shows a price estimate and ranked carriers whose
reasons can be verified by hand against the sync files.
