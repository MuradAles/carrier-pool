# Walkthrough — one day-11 answer, raw file to screen

For the review call. One load, followed all the way through, with the number at every step
and where it came from.

**The test this document is trying to pass:** a reader with the JSON files under `data/`
and a calculator can check every figure below without running the system. If any of them
can only be obtained by executing our code, the explanation is not an explanation.

The load is **`127412794`**, broker_a (FreightFlow / TMS A). It is the `DAY11-RICH`
scenario. I picked it because it is the only one where the narrowest tier fires directly —
so nothing is hidden behind a fallback — and because its top carrier wins on three
different signals at once, which makes the ranking argument checkable rather than a
one-term coincidence. `data/TRACEABILITY.md:141` holds the same numbers in table form;
this is the narrative between them.

Run alongside: `cd backend && ./.venv/bin/python -m scripts.e2e_check`, which asserts every
figure in §4 and §5.

---

## 1. The question

`data/tms_a_freightflow/2026-07-16T00-00_sync.json`, verbatim:

```json
{ "syncedAt": "2026-07-16T00:00:00-05:00",
  "loads": [ {
    "shipmentId": 127412794,
    "status": "Booking",
    "mileage": 296.0,
    "totalSell": 628.7,
    "totalBuy": null,
    "customer": { "customerId": 880048, "name": "Lone Star Beverages" },
    "carrier": null,
    "equipment": "53 ft Van | Dry",
    "weightTotal": 31300.0,
    "stops": [
      { "stopType": "First Pickup", "city": "COPPELL",    "state": "TX", "zipCode": "75019", ... },
      { "stopType": "Last Drop",    "city": "SUGAR LAND", "state": "TX", "zipCode": "77479", ... } ],
    "createdDate": "2026-07-15T14:03:00-05:00",
    "lastModifiedDate": "2026-07-15T14:03:00-05:00" } ] }
```

`"carrier": null` and `"totalBuy": null` are the whole point. The customer will pay
$628.70. Who do we call, and what do we pay them? Neither number exists anywhere in the
file.

## 2. Normalization — TMS A's shape to ours

| Field | Raw | Canonical | Rule |
|---|---|---|---|
| Status | `"Booking"` | `ACTIVE` | PRD §5 status map |
| Equipment | `"53 ft Van \| Dry"` | `DRY_VAN` | free-text match. A blank string would be `UNKNOWN`, never dry van (invariant 5) |
| Weight | `31300.0` | `31,300 lbs` | TMS A is already lbs (TMS B is kg, TMS C is per-line-item) |
| Distance | `296.0` | `296.0 mi` | already miles (TMS B is km) |
| Times | `-05:00` offsets | UTC | TMS A carries an offset. `2026-07-16T00:00-05:00` = `05:00Z` |
| Lane | first pickup, last drop | `75019 → 77479` | middle stops are kept but do not form the lane |

The two lane endpoints go through the hardcoded geo table (`app/domain/geo.py`, 180
places), which is the only source of coordinates anywhere in the project:

```
Coppell    TX 75019 -> 32.9546, -96.9900  metro DFW  zip3 750
Sugar Land TX 77479 -> 29.5766, -95.6187  metro HOU  zip3 774
```

Sanity check on the mileage the file states: Haversine between those two points is
246.664 mi, × 1.2 = **296.0 mi**. The fixtures and the platform share this one module
deliberately, so "how far apart is Coppell from Sugar Land" has a single answer.

## 3. Ingestion — how the history got there

132 files across the three TMS directories are sorted **by the timestamp in the filename**
and processed one at a time, never in bulk (invariant 4). Filenames are local Central for
all three TMSs, which is the only reason a single cross-directory sort is meaningful; the
payload timezones differ per TMS and are normalized separately.

Each file writes one `sync_files` row and one `sync_events` row per entity it mentions.
Both tables are append-only — the app role holds no UPDATE or DELETE grant on either.
`loads`, `carriers` and `customers` are then upserted from the newest event, and the lane
and carrier keys the file touched are marked dirty. After the file, `lane_stats` and
`carrier_stats` are **rebuilt for those keys from the raw events**, never patched with a
delta (invariant 3).

Two of the twelve loads that end up backing this answer show why that matters.

**`127402240` arrived six times**, one status at a time:

| file | status | `totalBuy` |
|---|---|---|
| `2026-07-06T00-00` | Quoting | `null` |
| `2026-07-06T06-00` | Booking | `null` |
| `2026-07-06T18-00` | Dispatched | 456.58 |
| `2026-07-07T00-00` | En Route | 456.58 |
| `2026-07-07T12-00` | Delivered | 456.58 |
| `2026-07-08T00-00` | Completed | **483.15** |

The money appears when a carrier is booked and is restated at settlement. Note the
`lastModifiedDate` on the 07-07T12-00 row is `2026-07-06T16:00`, *earlier* than the row
before it — deliberately out of order, to make sure ordering comes from the filename and
not from a field the TMS controls.

**`127403721` is a correction.** `2026-07-09T00-00` states `totalBuy: 565.2`;
`2026-07-10T06-00` restates the same load as `480.2`. Both events are in the log. The
lane's percentiles were computed once with 565.20 and then recomputed, from raw events,
with 480.20. Nothing subtracts 85.00 from a running average anywhere.

That is the property under test in `tests/integration` (I10): correcting a load and
rebuilding produces derived state **exactly equal** — dataclass equality on `lane_stats` at
all four tiers plus `carrier_stats`, every percentile — to ingesting the corrected value
from the start. It was verified non-vacuous by mutation: change rate accumulation from
`+=` to latest-wins and three of the four tests fail.

## 4. The tier walk

Lane keys narrow to wide, stopping at the first rung with at least 5 loads. Equipment
filters at every rung (`DECISIONS.md` D6).

| rung | key | loads | verdict |
|---|---|---|---|
| 1 · ZIP3 | `750 → 774` + `DRY_VAN` | **12** | ACCEPTED (12 ≥ 5) |

Rung 1 clears, so the walk stops and rungs 2–4 are never asked. The API returns the walk
it actually performed rather than claiming counts for rungs it did not query.

Those 12 loads, straight out of the sync files — every one is broker_a's, has both lane
endpoints in zip3 750 and 774, is dry van, and has a carrier rate. `$/mi` is
`carrier_rate ÷ distance_miles`, the load's latest values:

| load | carrier | miles | carrier rate | $/mi | on time? | file supplying the final value |
|---|---|---|---|---|---|---|
| `127402240` | IBRAHIM | 280.9 | 483.15 | 1.720007 | yes | `07-08T00-00` (6th of 6) |
| `127410591` | IBRAHIM | 292.0 | 502.24 | 1.720000 | yes | `07-14T00-00` |
| `127403721` | LONE STAR | 274.4 | 480.20 | 1.750000 | yes | `07-10T06-00` (corrected) |
| `127402425` | LONE STAR | 306.8 | 536.90 | 1.750000 | yes | `07-08T00-00` |
| `127407181` | IBRAHIM | 286.7 | 501.72 | 1.749983 | yes | `07-11T06-00` |
| `127406350` | SILVERADO | 251.7 | 448.03 | 1.779993 | **no** | `07-10T18-00` |
| `127409532` | IBRAHIM | 293.9 | 523.14 | 1.780016 | yes | `07-13T00-00` |
| `127403026` | IBRAHIM | 255.6 | 460.08 | 1.800000 | yes | `07-08T12-00` |
| `127407077` | IBRAHIM | 292.8 | 527.04 | 1.800000 | yes | `07-11T06-00` |
| `127408149` | IBRAHIM | 264.7 | 481.75 | 1.819985 | yes | `07-12T06-00` |
| `127400898` | COMANCHE PEAK | 250.3 | 463.06 | 1.850019 | yes | `07-07T00-00` |
| `127401932` | IBRAHIM | 267.1 | 494.14 | 1.850020 | yes | `07-07T18-00` |

*(on-time is a date comparison — delivered on or before the scheduled delivery date, the
only precision all three TMS formats support, `DECISIONS.md` D7. `127406350` was scheduled
to close 2026-07-08 and departed the drop 2026-07-09T20:25, so: late. 11 of 12.)*

## 5. The price

`percentile_cont` on those 12 values — linear interpolation, index `q × (n−1)`:

```
p25: 0.25 × 11 = 2.75   between v[2]=1.749983 and v[3]=1.750000   -> 1.74999564
p50: 0.50 × 11 = 5.50   between v[5]=1.779993 and v[6]=1.780016   -> 1.78000454
p75: 0.75 × 11 = 8.25   v[8]=1.800000 + 0.25 × (1.819985 − 1.800000) -> 1.80499622
```

`lane_stats.rate_per_mile_p*` is `NUMERIC(10,4)`, so the rate is quantized to four decimals
**before** it is multiplied by the miles. That ordering is the whole of `DECISIONS.md` D18,
and it exists so the rate a broker is shown is the rate the system used:

```
p25    1.7500 × 296.0 = $518.00
median 1.7800 × 296.0 = $526.88   <- the estimate
p75    1.8050 × 296.0 = $534.28
```

Multiply the printed rate by the printed miles on a phone and you get the printed dollars.
Had we kept full precision, the median would have been `$526.8813…` → `$526.88` — the same
here, but six of the sixteen day-11 loads came out a cent apart, and an estimate whose own
stated evidence does not reproduce it is worse than one a cent from a hypothetical.

**Confidence: medium.** 12 loads is the 5–14 band. Not high, and it does not get to
be high by rounding up: 12 is 12. Two additional caps could have applied and did not —
the pool is 12/12 dry van, so D15's heterogeneity cap does not bind, and no published
percentile is at or below $0.00/mi, so D23's guard does not fire.

Provenance, exactly as the API returns it:

> *median of 12 loads on 750->774, dry van, 2026-07-06 to 2026-07-13 — medium confidence*

Tier, count, equipment filter, and the date span the evidence covers. The customer is
paying $628.70, so the expected buy of $526.88 implies about 16.2% gross.

## 6. The ranking

Five signals, each 0–1, weighted 0.35 / 0.20 / 0.15 / 0.20 / 0.10, summed, ×100, and
rounded **once at the end**, half-up. Signals are never rounded on the way in.

The lane pool from §4 supplies the counts. IBRAHIM has 8 of those 12 loads; LONE STAR has
2; SILVERADO and COMANCHE PEAK have 1 each.

**IBRAHIM TRANSPORT INC**, term by term, in points out of 100:

| signal | value | where it comes from | points |
|---|---|---|---|
| Lane experience | `8/(8+5)` = 0.615385 | 8 of the 12 loads above | 35 × 0.615385 = **21.538** |
| Recency | `e^(−3/30)` = 0.904837 | last lane load `127410591` delivered 07-13; today is 07-16 | 20 × 0.904837 = **18.097** |
| Equipment | 1.0 | has hauled dry van 20 times across the whole book | 15 × 1.0 = **15.000** |
| Deadhead | 1.0 | last delivery Irving 75061, 11.6 mi from Coppell — inside the 50 mi full-credit band | 20 × 1.0 = **20.000** |
| On-time | `(8 + (11/12)×5)/(8+5)` = 0.967949 | 8-for-8 on this lane, shrunk toward the lane's 11/12 | 10 × 0.967949 = **9.679** |
| | | | **84.315 → 84.3** |

The deadhead figure is checkable: `127412069` (file `2026-07-15T12-00_sync.json`) is
IBRAHIM's last drop of the history, `IRVING TX 75061`, departed `2026-07-15T07:08-05:00`.
Irving 75061 is (32.8177, −96.9557) in the geo table; great-circle to Coppell 75019 is
9.666 mi, × 1.2 = **11.6 mi**.

**LONE STAR COLD LINES LLC**, second at **69.7** (`10.000 + 15.319 + 15.000 + 20.000 +
9.405 = 69.723`). The gap decomposes cleanly:

| | IBRAHIM | LONE STAR | gap |
|---|---|---|---|
| Lane experience | 21.538 | 10.000 | **+11.54** |
| Recency | 18.097 | 15.319 | **+2.78** |
| Equipment | 15.000 | 15.000 | 0 |
| Deadhead | 20.000 | 20.000 | 0 |
| On-time | 9.679 | 9.405 | +0.27 |
| | 84.3 | 69.7 | 14.6 |

Both trucks are sitting in the DFW metro, so proximity separates nobody, and both haul dry
van. The answer is decided by who has actually run this zip3 pair and how recently — which
is what a coverage rep would say out loud.

**The reasons are the same numbers.** Each signal is constructed by one call that produces
the value, the contribution and the sentence together; `score` is a derived property of the
contributions, not a stored field, so there is no field that can hold a number the
breakdown does not add up to. A tripwire test asserts `score == round_half_up(sum(
contributions))` across all 192 (carrier, day-11 load) rows — 0 violations. That is
invariant 2, and it is the one bug `CLAUDE.md` calls the worst possible.

What the rep sees for the winner:

> - Ran 8 loads on 750->774, dry van at the ZIP3 tier
> - Last load on this lane 3 days ago (2026-07-13)
> - Has hauled dry van for you (20 loads)
> - Delivered in IRVING, TX 75061 yesterday, 11.6 mi from your pickup
> - 100% on-time on this lane (8 of 8 delivered loads), shrunk to 97% toward the lane's 92%
> - Averages $1.78/mi on 750->774, dry van

All twelve of broker_a's carriers come back, weakest last, with the reason they are weak —
ALAMO CHILL at 9.2 with *"Delivered in PEARLAND, TX 77584 9 days ago, 304.8 mi from your
pickup — past the 250 mi cutoff, so no proximity credit."* Nothing is filtered, truncated,
or re-sorted between the scorer and the screen.

---

## 7. The correction a reviewer will ask about

`CLAUDE.md` names one trap by name: a TMS B sync can append a rate row for a load whose
`loads` array entry is not in the file at all. `HD-2026-004733` (broker_b) is that case.

`data/tms_b_hauldesk/2026-07-11T00-00_sync.json` carries the load and four rate lines:

```json
{"rate_id": 910191, "load_num": "HD-2026-004733", "side": "pay",  "code": "LINEHAUL",   "amount_usd": 674.70}
{"rate_id": 910192, "load_num": "HD-2026-004733", "side": "pay",  "code": "FUEL",       "amount_usd": 148.10}
{"rate_id": 910193, "load_num": "HD-2026-004733", "side": "bill", "code": "LINEHAUL",   "amount_usd": 669.07}
{"rate_id": 910194, "load_num": "HD-2026-004733", "side": "bill", "code": "FUEL",       "amount_usd": 118.07}
```

`data/tms_b_hauldesk/2026-07-12T06-00_sync.json` carries one more:

```json
{"rate_id": 910239, "load_num": "HD-2026-004733", "side": "pay", "code": "ADJUSTMENT", "amount_usd": -120.00}
```

and its `loads` array is `["HD-2026-004817", "HD-2026-004821", "HD-2026-004832"]`. Our load
is not in it. A system that keys change detection off the `loads` array — the obvious
design — records nothing here and keeps quoting a carrier rate that is $120 too high, with
no error anywhere.

TMS B money is the **sum of every `rates` line ever appended** per side, negatives
included, which is why `sync_events` carries `RATE_LINE` as an entity type with its own
`source_load_id`. So:

```
carrier (pay):  674.70 + 148.10 − 120.00 = 702.80
customer (bill): 669.07 + 118.07         = 787.14
distance:        450.6 km × 0.621371     = 280.0 mi
weight:       11566.6 kg × 2.20462       = 25,500 lbs
```

The load detail screen shows all six events in arrival order, each with the raw JSON its
TMS stated, so the correction is visible as a correction rather than as a number that
quietly changed. And because the affected lane and carrier keys were marked dirty and
**rebuilt from the event log**, the reefer percentiles on `761→770` are exactly what they
would have been had the −120 arrived on time.

The other two TMSs correct differently, and all three flavours are in the fixture.
`127403721` in §3 is TMS A's: a restated `totalBuy`, 565.20 → 480.20. TMS C's is the
nastiest, because nothing marks it — broker_c's `a0jO9000006Ag2iVHT` states
`bos__Carrier_Rate__c: 790.33` on `2026-07-09T00-00` and `705.33` on `2026-07-10T06-00`,
same field, no flag, no status change, `bos__Customer_Rate__c` untouched at 818.18. The
only thing that catches it is treating every arrival as a new event and rebuilding.

Dedupe is on the rate line's own id, not on the file it arrived in: re-sending
`rate_id: 910239` in a later file adds nothing. That was tightened during hardening — the
original key was narrower than the grain the rebuild works at (`DECISIONS.md` D25 §4).

## 8. Why these numbers are trustworthy

The argument is not "the tests pass". It is that the expected answers were written down
**before** the code that produces them existed, by a different implementation.

`data/TRACEABILITY.md` is computed by a reference model inside
`backend/scripts/generate_data.py`. That model was written from the PRD directly. It does
not import `app.domain.pricing` or `app.domain.scoring` — the only thing it shares with the
platform is the geography table, deliberately, so both agree on where Coppell is. It
therefore functions as an oracle: a genuine formula error on either side shows up as a
disagreement, rather than as two copies of the same mistake agreeing with each other.

Comparing all 192 ranking rows once both existed:

```
176  agree exactly
 10  the doc computed its weighted sum from displayed 3dp signals; the code used full precision
  6  differ by 0.1 at a rounding boundary
  0  disagree on any signal value
```

Zero disagreements on any `n/(n+5)`, any `e^(−d/30)`, any deadhead credit, any shrunk
on-time rate. The entire divergence was presentation, and it was closed by pinning a
rounding **rule** in the PRD that both sides honour — deliberately not by making the
generator import `scoring.py`, which would make them agree by construction and destroy the
oracle. Sharing a rule is a spec; sharing the code is collusion.

The document has since been the authoritative artifact **three separate times**, and each
time it was right and the code was wrong:

| | what the doc caught | outcome |
|---|---|---|
| D18 | six estimates a cent off | the pricing arithmetic was reordered: quantize the rate, then multiply |
| D19 | six scores 0.1 apart at a `.X5` tie | half-up rounding pinned in `PRD.md` §8, applied once at the end |
| H5 | a `low` confidence printed beside "capped at medium" | a live invariant-2 violation that had reached the screen; the clause is now gated on the outcome |

`TRACEABILITY.md` needed no regeneration for any of them.

It has been overruled exactly once, in D18, and only because it contradicted *itself*:
it printed `2.0200 × 187.2 = $378.15` when `2.0200 × 187.2` is `378.144`. The test we
applied — can the discrepancy be demonstrated using only the artifact in question — is what
separates a real correction from bending an expectation toward the code.

Alongside that: `breaker` found 14 defects and failed 70 attacks, each finding written as
an `xfail` carrying its own reproduction so the fix flips a test that already existed;
`reviewer` found 3 more. All fixed. The adversarial and isolation suites were checked for
vacuousness by mutation — set the RLS predicate to `USING (true)` and 6 of 17 isolation
tests fail; drop `MIN_SAMPLE` from 5 to 4 and 6 pricing tests fail.

## 9. What is wrong with it, before you ask

**Dallas→Houston reads 271 mi. The real drive is about 239.** Distance is Haversine × 1.2
(a stated invariant), and I-45 is unusually straight — its true road factor is about 1.06.
`TASKS.md` G2 had written the acceptance criterion as "≈ 240 mi", and with real coordinates
the two cannot both hold. We kept the invariant and recorded the overstatement
(`DECISIONS.md` D10). Bending the factor to hit one pair would have made every indirect
pair worse, silently.

That is safe for anything that is a ratio — $/mi is computed against the same miles
throughout, so the factor cancels and rates stay in a plausible $1.50–$3.50 band. **It does
not cancel for deadhead**, whose counterparty is a pair of constants, not another distance:

| stated threshold | fires at haversine | ≈ true road miles |
|---|---|---|
| 250 mi (zero credit) | 208.3 | ~221 |
| 50 mi (full credit) | 41.7 | ~44 |

So deadhead is scored about 13% **more pessimistically** than the stated policy, uniformly
and in one direction. Across all 192 (carrier, load) pairs, 85 would change credit by more
than 0.02 at r = 1.06 and 45 cross a boundary. No day-11 answer changes — the deadhead
scenario's margin is 15.8 points against a worst case of 1.6 — but a reviewer counting
miles on a map will notice, and the honest answer is that we chose a documented uniform
bias over an undocumented non-uniform one.

Two others worth naming before they are found:

- **The shared carrier pool is designed, not built** (`DECISIONS.md` D17, D4). Field by
  field against the real schema, including the finding that k-anonymity is arithmetically
  unavailable at three brokers — stated rather than papered over with a threshold that
  does not exist.
- **`pricing.py:481` has an unreachable `_provenance` fallback.** Dead code found during
  review, deliberately left, producing no wrong answer.

---

## The five-minute version

Coppell 75019 → Sugar Land 77479, dry van, 296 mi, no carrier, no price.
`750 → 774` + dry van has 12 completed loads in broker_a's own history, so the walk stops
at the narrowest tier. Their median rate is $1.78/mi → **$526.88**, range $518.00–$534.28,
**medium** confidence because 12 is in the 5–14 band, over evidence spanning 2026-07-06 to
07-13. **IBRAHIM TRANSPORT INC** scores **84.3**: it ran 8 of those 12 loads, the most
recent three days ago, it hauls dry van, and its last drop was in Irving — 11.6 miles from
the pickup. Second place is 14.6 behind, all of it on experience and recency, because both
trucks are equally close and equally equipped.

Every number in that paragraph is in `data/tms_a_freightflow/*.json`.
