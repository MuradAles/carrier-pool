# Carrier Pool — project rules

Carrier recommendation + price estimation for freight brokers. Read `README.md` (the
assignment) and `PRD.md` (our plan) before non-trivial work.

## Stack

Python 3.12 + FastAPI + Postgres 16 (plain SQL, no ORM) + React 19/Vite/TypeScript.
`docker compose up` must bring up everything from a clean checkout.

## Hard invariants — violating any of these is a bug, not a style issue

1. **Multi-tenancy.** Every query filters by `broker_id`, enforced in the repository layer so
   no call site can omit it. One broker's data must never influence another's answers.
2. **Reasons come from the score.** Every human-readable reason is generated from the same
   numbers that produced the score — never written independently. A reason that can disagree
   with its score is the worst possible bug here: the reasoning is the product.
3. **`sync_events` is append-only.** Derived stats (`lane_stats`, `carrier_stats`) are
   *rebuilt* from it for touched keys, never incrementally patched. A late correction must
   produce the same numbers as if it had arrived on time.
4. **Ingestion is one file at a time, chronological.** Sort by the timestamp in the filename.
   Never bulk-load. Re-ingesting a file is a no-op (idempotent on `broker_id + sync_file`).
5. **Null equipment stays `UNKNOWN`.** Never defaults to `DRY_VAN`. The TMS C schema comment
   warns about this explicitly.
6. **Every lane/price answer reports which tier it used** (`ZIP3` → `METRO` → `REGION`) and how
   many loads backed it. Low confidence is labeled, never hidden.
7. **No network at runtime.** No API keys, no external calls. Geo and distance are offline:
   hardcoded city/zip table, Haversine × 1.2.

## Normalization rules (get these exactly right)

| Field | Rule |
|---|---|
| Weight | → lbs. TMS B `weight_kg × 2.20462`. TMS C: check `bos__Weight_Units__c` **per line item**, then sum |
| Distance | → miles. TMS B `dist_km × 0.621371` |
| Equipment | → `DRY_VAN` \| `REEFER` \| `FLATBED` \| `UNKNOWN`. A free-text, B `V`/`R`/`F`, C picklist |
| Money | A/C: totals as given. **B: sum of ALL `rates` line items ever appended** per side (`pay`→carrier, `bill`→customer), including negatives |
| Time | → UTC. **TMS B naive strings are US Central and must be DST-aware** (July = CDT = UTC-5, not -6). A carries an offset, C is already UTC |
| On-time | Delivered **on or before the scheduled delivery date** — a date comparison, the only precision all three formats support. **TMS C's bare `bos__Scheduled_Date__c` is a local Central date while `bos__Arrival_Time__c` is UTC** — convert the arrival back to Central before comparing, or every delivery after 19:00 Central reads a day late |
| Stops | Ordered. First pickup / last drop form the lane; middle stops kept but not lane-forming |
| Location | city/state/zip → lat, lon, metro, zip3 via the hardcoded table. Unmatched → geo-null: excluded from lane stats, still displayed |

## Known traps

- **TMS B rate-only changes.** A sync can append an `ADJUSTMENT` row for a load whose `loads`
  row did not change. The event must still be recorded and the load's money recomputed, even
  though the file's `loads` array does not mention it.
- **TMS C silent restatement.** `bos__Carrier_Rate__c` changes with no marker that it changed.
- **Filenames are local Central; payload timezones differ per TMS.** Sorting by filename only
  works because all three share the same local clock. Keep that assumption explicit.
- **Cold start.** Shrink toward the lane average, `k = 5`. 2-for-2 must not beat 164-for-200.
- **Minimum sample is 5 loads** to accept a tier.

## Dates

Days 1–10 = 2026-07-06 → 2026-07-15 (history). Day 11 = 2026-07-16 (loads to answer for).

## Specialist agents

| Agent | Use for |
|---|---|
| `orchestrator` | Running one whole phase of `TASKS.md` — dispatches the agents below, judges their output, re-dispatches on failure, keeps `DECISIONS.md` and `TASKS.md` current |
| `data-gen` | Generating or fixing TMS sync fixture files, and the day-11 traceability table |
| `builder` | Implementing features from `PRD.md` — adapters, ingestion, scoring, API, UI |
| `unit-tester` | Fast no-DB tests: adapters, normalization math, scoring functions |
| `integration-tester` | Real-Postgres tests: ingestion, corrections, tenant isolation, the e2e check |
| `breaker` | Adversarial — actively tries to produce a wrong answer or a tenant leak |
| `reviewer` | Reading any diff against the invariants above |
| `stack-docs` | FastAPI / psycopg3 / Vite / React API questions — reads real docs, never guesses |

Normal loop: `builder` → `unit-tester` → `integration-tester` → `breaker` → `reviewer`.
Nothing is "done" until `breaker` has genuinely attacked it.

Work is handed over **one phase at a time** to `orchestrator`, which runs that loop internally
and returns a single phase report.
