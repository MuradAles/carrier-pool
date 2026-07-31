# TASKS

Execution plan for `PRD.md`. Ordered by dependency — each phase needs the one above it.

**Legend:** `[ ]` todo · `[~]` in progress · `[x]` done · `[!]` blocked
**Agents:** `orchestrator` (runs a whole phase) · `data-gen` · `builder` · `unit-tester` ·
`integration-tester` · `breaker` · `reviewer` · `stack-docs`

Phases are handed to `orchestrator` one at a time; the per-task `Agent` column below is who it
dispatches.

Status: **Phases 0–9 complete.** 454 tests (287 unit + 22 data-integrity + 57 integration +
88 adversarial). Committed through `9811742`. `docker compose up` verified end to end: a
day-11 answer reaches the browser through the frontend proxy from a cold start.

72 of 83 rows done. What the project can defend:

- An unscoped query **raises** rather than returning rows; a cross-broker request 404s. Proven
  with all three brokers loaded simultaneously on one lane key, each seeing its own median.
- A late correction produces derived state **identical** to on-time arrival — all three
  flavours, every percentile (I10).
- Two independently written scorers agree on **all 192** ranking rows, having shared a rounding
  *rule* and never code. `data/TRACEABILITY.md` was computed before the code existed and has
  been the authoritative artifact three times (D18, D19, H5).
- `breaker` found **14 defects and failed 70 attacks**; `reviewer` found 3 more. All fixed.

**Not started:** X1 README run section · X2 `DECISIONS.md` final pass (incl. the 70 failed
attacks) · X4 clean-checkout rehearsal · X5 walkthrough notes. Phase 11 ships as design only
(D17), per D4.

**Known, unfixed:** `pricing.py:481`'s unreachable `_provenance` fallback — dead code, no wrong
answer. The integration suite truncates a shared database, so parallel pytest invocations
against one database conflict; the three suites are run as separate invocations.

---

## Phase 0 — Decisions and foundation ✅

Cheap now, expensive later. Nothing below should start until D1–D4 are settled.

| # | Task | Agent | Done when |
|---|---|---|---|
| [x] F1 | Scaffold: compose, Dockerfiles, deps, health endpoint, placeholder UI | — | `docker compose config` validates |
| [x] F2 | Verify the stack actually boots | builder | Verified end to end from an **empty volume** (`docker compose down -v` → `up`), not just a warm restart: Postgres `initdb`, schema bootstrap, role creation, all 132 files ingested, and a day-11 answer reaching the browser through the Vite proxy. Closed during X4 |
| [x] D1 | **Load budget** | — | Mostly `COMPLETED` backfill; arithmetic in `DECISIONS.md` |
| [x] D2 | **Cross-TMS carrier identity** | — | TMS C extended with MC/DOT, disclosed as an extension |
| [x] D3 | **`sync_events` grain** | — | Split into `sync_files` + per-entity `sync_events`; PRD §5 updated |
| [x] D4 | **Scope of the bonus pool** | — | Build it, last, off the critical path. Boundary + threat model written |
| [x] F3 | Create `DECISIONS.md`, logging calls as they're made | — | D1–D4 recorded |
| [x] D5 | **Cold-start formula per signal** | — | Experience saturates, on-time shrinks; PRD §8 amended |
| [x] D6 | **Equipment filter and the tier walk** | — | Filters at every tier + `REGION_ANY` rung; PRD §7 amended |
| [x] D7 | **On-time definition** | — | Day-granular, the only cross-format honest one |
| [x] D8 | **Ingestion trigger** | — | FastAPI lifespan, synchronous, before serving |
| [x] D9 | **Fixture depth across brokers** | — | All three equally rich; budget arithmetic recorded |
| [x] F4 | Add `orchestrator`; give `builder` a frontend brief | — | `.claude/agents/` updated, `CLAUDE.md` table current |

---

## Phase 1 — Geography and fixtures

Everything downstream reads this data. Get it right before writing logic against it.

| # | Task | Agent | Depends | Done when |
|---|---|---|---|---|
| [x] G1 | Geo table: ~150 Texas Triangle cities/zips → lat, lon, metro, zip3 | builder | D1 | `backend/app/domain/geo.py` — 180 places, 101 cities, DFW 56 / HOU 47 / SAT 29 / AUS 25 / TX_OTHER 23. Every generated stop resolves (0 unresolved, verified from disk) |
| [x] G2 | Haversine × 1.2 road factor | builder | G1 | `backend/app/domain/distance.py`. **Acceptance text amended by D10** — ×1.2 on real coords gives Dallas→Houston 271 mi, not 240; the invariant wins over the illustrative figure |
| [x] G3 | Unit tests for G1/G2 | unit-tester | G2 | 130 tests passing in 0.04s. One bound corrected per D14. `reviewer` confirmed they are substantive, not tautological |
| [x] DG1 | Generator skeleton: seeded, re-runnable, emits the 132-slot filename grid | data-gen | D1, G1 | `backend/scripts/generate_data.py`. 3 consecutive runs byte-identical over 133 files |
| [x] DG2 | TMS A writer — nested camelCase, US units, ISO+offset | data-gen | DG1 | 44 files; `syncedAt` carries `-05:00` (CDT) |
| [x] DG3 | TMS B writer — flat tables, kg/km, naive Central, append-only rate rows | data-gen | DG1 | 44 files; naive `synced_at` matches the Central filename; no duplicate `rate_id` |
| [x] DG4 | TMS C writer — CRM records, referenced_records, UTC | data-gen | DG1 | 44 files; 06:00 Central → `11:00:00.000+0000` = UTC-5; every referenced id resolves |
| [x] DG5 | Plant scenarios 1–4: lifecycle, corrections (all 3 flavors), lane contrast, carrier contrast | data-gen | DG2-4 | All 4 confirmed present **in the JSON** by `reviewer`, not just in the script: lifecycle `127402240` across 6 files, all 3 correction flavors, per-broker rate bands per D13 |
| [x] DG6 | Plant scenarios 5–8: suburb scatter, cross-TMS carrier, deadhead setup, messy edges | data-gen | DG5, D2 | All 4 confirmed in the JSON. **Rate-only `ADJUSTMENT` verified**: `HD-2026-004733` gets −120 in a file whose `loads` array holds only three other loads |
| [x] DG7 | Day-11 loads — one per behavior demonstrated | data-gen | DG6 | 16 loads (5/5/6), all uncovered. All four tier rungs have a fixture: ZIP3 6, METRO 4, REGION 5, REGION_ANY 1 |
| [x] DG8 | **Traceability table** — per day-11 load: behavior proven, supporting history, expected top carrier, arithmetic | data-gen | DG7 | `data/TRACEABILITY.md`, 1,501 lines. `reviewer` reproduced **all 16 rows** from the JSON with an independent parser — miles, every tier rung, percentiles to 4dp. Brokers A and B exact; C differs only on on-time (the open timezone item) |
| [x] DG9 | Generate all 132 files and validate | data-gen | DG8 | 132 files, grid exact, byte-identical across runs (`8fcc1679…`). Validator now **reconciles emitted bytes against the plan** — was 8/10 injected corruptions passing, now 0/21 |
| [x] DG10 | **Corruption harness** — prove the validator actually bites | data-gen | DG9 | `backend/tests/data_integrity/`, 21 named corruption tests + positive control. Verified by mutation: with reconciliation disabled, 21 fail and only the control passes |

---

## Phase 2 — Canonical model and storage

| # | Task | Agent | Depends | Done when |
|---|---|---|---|---|
| [x] M1 | Canonical dataclasses: Load, Stop, Carrier, Customer | builder | D3 | `backend/app/domain/model.py`, 421 lines. `Equipment` enum with no dry-van default; geo-null distinct from absent; money nullable so "unknown" never becomes 0 |
| [x] M2 | DB schema (PRD §5) + migration/bootstrap SQL | builder | M1, D3 | `backend/app/repository/schema.sql`, 299 lines. Applies clean to a fresh Postgres, idempotent. 8 tables; RLS enabled **and forced** on 7 (`brokers` excluded — the broker list is not tenant data) |
| [x] M3 | Repository layer — **`broker_id` enforced structurally**, not by convention | builder | M2 | `broker_repository.py`, 774 lines. Raw unscoped SELECT, a forged `app.broker_id` GUC, and a cross-broker UPDATE all raise `InsufficientPrivilege: no broker bound`. Credentials split: `DATABASE_URL` → `carrier_pool_app` (no superuser, no `bypassrls`), `ADMIN_DATABASE_URL` → owner, bootstrap only. **A plain `psycopg.connect(DATABASE_URL)` now fails closed** — verified after dropping the schema *and* the app role, so a clean `docker compose up` works |
| [x] M4 | Repository tenant-isolation tests | integration-tester | M3 | `backend/tests/integration/`, 17 tests. Broker A's reads are **exactly equal** (frozen-dataclass `==`) before and after broker B writes with identical `source_load_id`, identical `source_carrier_id` + MC/DOT, and colliding customer id. Covers aggregates, a join with **no** `broker_id` in its condition, upsert collisions, `sync_events` ordering, and deletes. Verified non-vacuous: mutating the RLS predicate to `USING (true)` fails 6 of 17 |

---

## Phase 3 — Adapters

| # | Task | Agent | Depends | Done when |
|---|---|---|---|---|
| [x] A1 | Adapter interface + shared normalization helpers (units, time, equipment) | builder | M1 | `backend/app/adapters/{base,normalize}.py`. One function per rule; `AdaptedSync.rate_contributions()` makes TMS B's per-file money delta explicit. D16 lands in `backend/app/domain/localtime.py` |
| [x] A2 | TMS A adapter | builder | A1 | `example_sync.jsonc` → ACTIVE / DRY_VAN / 24000 lb / 242.1 mi / 750→774; blank free text → `UNKNOWN` |
| [x] A3 | TMS B adapter — kg/km, **DST-aware** Central, rate-row summing | builder | A1 | 10886.2 kg → 23999.93 lb, 389.6 km → 242.09 mi; 06:00 naive → 11:00 UTC (CDT) via zoneinfo; negatives kept, rate-only sync emits a `RATE_LINE` for a load absent from `loads` |
| [x] A4 | TMS C adapter — referenced_records, per-line-item weight units, null equipment | builder | A1 | 3 null-equipment loads → `UNKNOWN`, 0 → `DRY_VAN`; SHP6701343 = 9300 lb + 6800 kg = 24291.42 lb; D16 reproduces all seven broker_c on-time counts |
| [x] A5 | Adapter unit tests, all three | unit-tester | A2-4 | `tests/unit/test_adapters.py`, 74 tests. Both DST seasons asserted from the same wall-clock string, so a hardcoded offset of either sign fails one. Verified non-vacuous by mutation: `2.20462`→`2.2` fails 3, the equipment gate→`DRY_VAN` fails 17, `US_CENTRAL`→fixed UTC-6 fails 2 |

---

## Phase 4 — Ingestion

The correction-handling story lives here. This is the heart of the assignment.

| # | Task | Agent | Depends | Done when |
|---|---|---|---|---|
| [x] I1 | File discovery + chronological sort by filename timestamp | builder | A5 | `ingestion/discovery.py`. A true merge sort across all three directories, not per-directory concatenation — tested. The shared local Central clock is what makes cross-directory ordering valid, and that assumption is stated where the sort happens |
| [x] I2 | One-file-at-a-time ingestion loop, append-only `sync_events` | builder | I1, M3 | `ingestion/pipeline.py`. 132 files, 1,156 events. Append-only verified two ways: the actual `GRANT` set, and a behavioural UPDATE/DELETE attempt with a broker bound |
| [x] I3 | Upsert loads/carriers/customers from newest event | builder | I2 | Later sync overwrites; **earlier versions stay retrievable from `sync_events`** — both halves tested, since the audit trail is what makes a correction visible in the UI |
| [x] I4 | **TMS B rate-only sync** — load absent from `loads` array still updates money | builder | I3, D3 | `HD-2026-004733` = 674.70 + 148.10 − 120.00 = **702.80**, where the −120 arrives in a file whose `loads` array holds only `004817/004821/004832`. Verified against the raw file on disk, not just the DB |
| [x] I5 | Idempotency on `broker_id + sync_file` | builder | I3 | Re-ingest: 0 ingested, 132 skipped. Asserted on **summed money** (`150716.43` / `186840.92`) as well as row counts — double-counted TMS B line items would show there and nowhere else. Also 10× re-ingest with two brokers interleaved |
| [x] I6 | Dirty-key marking — carrier, lane, and every tier above | builder | I3 | 1,556 lane keys rebuilt over the corpus. A rate-only correction dirties ZIP3, METRO, REGION, REGION_ANY **and** the carrier — all five asserted |
| [x] I7 | Rebuild `lane_stats` / `carrier_stats` from raw events for dirty keys | builder | I6 | Rebuild, never delta-patch. Cross-checked against `TRACEABILITY.md`, computed before this code existed: ZIP3 `750→774` DRY_VAN is 12 loads in all three brokers, medians `1.7800 / 2.1600 / 2.5100` — exact |
| [x] I8 | Carrier last-known delivery position (for deadhead) | builder | I7 | 261 carrier repositions over the corpus; feeds the deadhead signal in R1 |
| [x] I9 | Ingestion integration tests: order, idempotency, overwrite, partial failure | integration-tester | I8 | 10 tests, real Postgres. Order-sensitivity proven by *bypassing* the sort and showing the answer becomes wrong; partial failure via an exception inside the transaction leaving no trace |
| [x] I10 | **Replay-equivalence test** — correct-then-rebuild equals ingest-corrected-from-start | integration-tester | I9 | 4 tests, all three flavors, two independent runs per flavor compared with exact dataclass equality on `lane_stats` at all four tiers **plus** `carrier_stats` — every percentile, not just the corrected load's own rate. **Verified non-vacuous by mutation:** dirty-key marking limited to ZIP3 fails 4 tests; changing rate accumulation from `+=` to latest-wins — the exact snapshot mistake D3 exists to prevent — fails 3 |

---

## Phase 5 — Lanes and pricing

| # | Task | Agent | Depends | Done when |
|---|---|---|---|---|
| [x] L1 | Lane keys at all four tiers (ZIP3, METRO, REGION, REGION_ANY) | builder | I7, G1 | `backend/app/domain/lanes.py`. Built during I6 for dirty-key marking, extended here with the read-side mirror rather than duplicated |
| [x] L2 | Tier walk with 5-load minimum, **reporting the tier used** | builder | L1 | `walk_tiers()` returns the whole trace — every rung tried with its count — and stops at the first acceptance, so later rungs are *absent* rather than claimed with a count they were never asked for. All 16 day-11 walks match `TRACEABILITY.md` rung for rung |
| [x] L3 | Price estimate: median × miles, p25–p75 range | builder | L2 | `percentile_cont` in SQL. Per **D18** the rate quantizes to 4dp *before* multiplying, so the dollar figure is reproducible by hand from the rate in its own provenance line |
| [x] L4 | Confidence + provenance (tier, load count, date range, equipment) | builder | L3 | **D15 fires**: the `UNKNOWN`-equipment load matches 31 loads (23 dry van, 8 reefer) and caps to medium instead of the high its count alone earns, naming the mix in the provenance |
| [x] L5 | Lane/pricing unit tests | unit-tester | L4 | `tests/unit/test_pricing.py`, 33 tests. Verified non-vacuous by mutation: `MIN_SAMPLE` 5→4 fails 6, and implementing D15 as "cap whenever the filter was skipped" rather than "cap when the pool is mixed" fails 4 |

---

## Phase 6 — Carrier ranking

| # | Task | Agent | Depends | Done when |
|---|---|---|---|---|
| [x] R1 | Five signals: lane experience, recency, equipment, deadhead, on-time | builder | L2, I8 | `backend/app/domain/scoring.py`. On-time divides by **loads with a verdict** — `delivered_on_time` returns `None` for a rolling truck, and `on_time_count / load_count` would have counted every one as a miss |
| [x] R2 | Shrinkage toward lane average, `k=5` | builder | R1 | Two formulas per **D5**: experience saturates `n/(n+5)` (zero at zero, so absence of evidence is never rewarded); on-time shrinks toward the lane rate. `unit-tester` found the 2-for-2 case is **prior-dependent**, not universal, and documented that rather than asserting something untrue |
| [x] R3 | Weighted score 0–100 | builder | R2 | Weights match PRD §8 and sum to 1. **D19** pins the presentation contract: full precision throughout, rounded once at the end, half-up — Python's builtin rounds the *binary* value, so `69.55` goes down and `22.05` goes up with nothing in the source to say which |
| [x] R4 | **Reasons generated from the same values that produced the score** | builder | R3 | `_signal(name, observed=, value=, reason=)` builds all three in one call. `score` is now a **derived property**, not a stored field, so no field can hold a number the breakdown doesn't add up to. Tripwire test asserts `score == round_half_up(sum(contributions))` — 0 violations across all 192 rows |
| [x] R5 | Weak carriers still returned, ranked last, with an accurate reason | builder | R4 | All 12 returned, weakest last: *"Has never run 750→774, dry van at the ZIP3 tier"*, *"…304.8 mi from your pickup — past the 250 mi cutoff, so no proximity credit"*. Survives serialization to the API |
| [x] R6 | Scoring unit tests incl. deadhead curve and `UNKNOWN` equipment neutrality | unit-tester | R5 | `tests/unit/test_scoring.py`, 48 tests. Caught a real docstring error: `round(22.05,1)` is 22.1, not 22.0 — the conclusion was right, the example backwards |

---

## Phase 7 — API

| # | Task | Agent | Depends | Done when |
|---|---|---|---|---|
| [x] P1 | `/api/brokers`, `/api/loads`, `/api/loads/{id}` | builder | M3 | `app/api/{routes,schemas,deps}.py`. Hand-verified live: 3 brokers, 5 `ACTIVE` loads for broker_a with `carrier_rate: null` |
| [x] P2 | `/api/loads/{id}/recommendations` | builder | R5 | 12 carriers, 84.3 down to 9.2, every one with reasons. Nothing filtered or truncated |
| [x] P3 | `/api/loads/{id}/price-estimate` | builder | L4 | Returns the **whole walk** — every rung tried with its count, `min_sample`, and whether the equipment filter applied — not just the accepted rung. Invariant 6 |
| [x] P4 | `/api/admin/ingest` — replay all files chronologically | builder | I5 | Idempotent. Per **D8** a full ingest also runs in the FastAPI lifespan, synchronously, before serving |
| [x] P5 | Load detail includes sync history, so corrections are visible | builder | P1 | `HD-2026-004733`: 6 entries, four rate lines on 07-11 then the −120 `ADJUSTMENT` on 07-12, settling at 702.80. Each carries `raw_json` — the entity exactly as its TMS stated it |
| [x] P6 | API integration tests incl. cross-broker access attempt | integration-tester | P5 | `tests/integration/test_api.py`, 23 tests. 404 not 500 on every load-scoped route, disjointness across brokers rather than counts, a SQL-injection-shaped id, 422 on a missing `broker_id`. **Isolation proven with all three brokers loaded simultaneously** on the identical lane key — each sees its own median `{1.78, 2.16, 2.51}`, not the pooled ~2.16 |

---

## Phase 8 — UI

Correctness and clarity only. README:101 — visual polish counts for nothing.

| # | Task | Agent | Depends | Done when |
|---|---|---|---|---|
| [x] U1 | API client + types | builder | P3 | Built ahead of the API behind one documented seam (`provisional.tsx`). Nullable in the API is nullable in the type — an `ACTIVE` load must not render `$0.00` |
| [x] U2 | Load list: broker dropdown, status filter, `ACTIVE` first-class | builder | U1 | No router, no state library. **The browser computes nothing it displays** — verified by grep for division, ×100, `toFixed` and `reduce` outside the formatter |
| [x] U3 | Load detail: facts, stops, dates, rates | builder | U1 | Stops in order; a geo-null stop still renders its raw city/state/zip — excluded from lane stats is not hidden from the user |
| [x] U4 | Price estimate panel with provenance line | builder | U3 | `PriceEstimatePanel.tsx` + `TierWalk.tsx` — renders **every rung tried** with its count and verdict, not just the winner. Its mixed-pool caveat deliberately refuses to name a confidence level, since the backend's label is the only thing entitled to say which |
| [x] U5 | Ranked carriers with bulleted reasons | builder | U3 | `RankedCarriersPanel.tsx`. Does not re-sort, truncate, or filter — the zero-score carrier appears last with its reasons, so R5 survives to the screen |
| [x] U6 | Sync history panel — a correction is visibly a correction | builder | P5 | `SyncHistoryPanel.tsx`. `HD-2026-004733` shows four rate lines on 07-11 then the −120 `ADJUSTMENT` on 07-12, each with `raw_json` as its TMS stated it |

---

## Phase 9 — Hardening

| # | Task | Agent | Depends | Done when |
|---|---|---|---|---|
| [x] H1 | **End-to-end check**: fresh DB → ingest 132 files → named day-11 load returns expected top carrier | integration-tester | U6, DG8 | `backend/scripts/e2e_check.py` (also runnable as `pytest tests/integration/test_end_to_end.py`). Creates and drops its own database (`carrier_pool_e2e_check`), never `carrier_pool`/`carrier_pool_phase5`. 132 files, 0 skipped; 295 loads, `sum(carrier_rate)=$150,716.43`, `sum(customer_rate)=$186,840.92`; idempotent re-ingest (0/132); one named day-11 load per tier rung (ZIP3/METRO/REGION/REGION_ANY) matches `data/TRACEABILITY.md` on tier, load count, top carrier, score, price point/range and confidence — except the UNKNOWN-equipment load's confidence, sourced from DECISIONS.md D15 (medium), which supersedes the traceability table's uncapped "high" that D15 itself flagged as an open question. ~11s |
| [x] H2 | Adversarial pass: tenant leaks, correction chains, hostile inputs, statistical nonsense | breaker | H1 | `tests/adversarial/`, **14 defects found and 70 attacks that failed**. Each finding written as an `xfail` carrying its own reproduction and arithmetic, so the fix flips a test that already existed |
| [x] H3 | Fix what `breaker` found | builder | H2 | All 14 fixed, 431→447 tests, 0 xfailed. **FINDING 2**: three documents asserted something false about our own data — the composite was verified (veteran ahead by 22.39) *before* the claim was rewritten. D21–D24 record the calls |
| [x] H4 | Full review against the invariants | reviewer | H3 | First look at the whole codebase. 3 defects + 2 doc drifts, each with a reproduction it had actually run. **One finding rejected**: it reported `CLAUDE.md:51` as carrying the false cold-start claim, having quoted one line and stopped at the wrap — the correction runs 51–58. Cleared, having run rather than pattern-matched: **zero score/reason divergences across all 192 rows**, `mc_number` in no `WHERE`/`JOIN`, no `UPDATE`/`DELETE` on the event tables. Carry-in confirmed still present: the unreachable `_provenance` fallback (`pricing.py:481`), dead code, not a wrong answer |
| [x] H5 | Fix what `reviewer` found | builder | H4 | 447→453. A live invariant-2 violation (`low` confidence beside "capped at medium"), negative distance poisoning lane percentiles, and a dedupe key narrower than the rebuild's. **`TRACEABILITY.md` needed no regeneration** — the oracle was right and the code was wrong, for the third time today |

---

## Phase 10 — Delivery

| # | Task | Agent | Depends | Done when |
|---|---|---|---|---|
| [x] X1 | Run section — exact command sequence, from clean checkout | builder | H1 | `RUNNING.md`. Every command executed, not assumed. Per-phase cold-start breakdown with the machine named, after two measurements disagreed 20× and agreed only on the ~4s that is our code |
| [x] X2 | `DECISIONS.md`: judgment calls, rejected alternatives, honest limits | builder | H5 | 25 decisions + limitations. Every claim paired with what it does **not** reach — *"tenant isolation holds against a query, not against a caller"*. Answers the README's millions-of-loads question with the actual bottleneck |
| [x] X3 | Shared-pool section — what crosses the broker boundary, what never does, how it's enforced | builder | D4 | **D17**, field by field against the real schema. Establishes that k-anonymity is arithmetically unavailable at three brokers and says so rather than claiming a threshold that doesn't exist |
| [x] X4 | Clean-checkout rehearsal: `docker compose down -v` → `up` → verify | integration-tester | X1 | Ran from an empty volume. **Nothing needed a step the document didn't already have.** RLS re-verified forced on all 7 tenant tables, app role back at `rolsuper=f, rolbypassrls=f` |
| [x] X5 | Walkthrough notes for the review call — trace one day-11 answer end to end by hand | — | X4 | `WALKTHROUGH.md`. ZIP3 pool rebuilt with a throwaway parser **independent of production code**, reproducing $518.00/$526.88/$534.28 — every number checkable with the JSON and a calculator |

---

## Phase 11 — Shared carrier pool (optional, off the critical path)

Per D4. Only starts once tenant isolation is proven by passing tests. If it doesn't get built,
it ships as the written design in `DECISIONS.md` and nothing else changes.

| # | Task | Agent | Depends | Done when |
|---|---|---|---|---|
| [x] S1 | Opt-in flag per broker | builder | X2 | `pool_opt_in`; no row is the default. Opted-out behaviour asserted **byte-identical** by exact dict equality on real rankings and estimates |
| [x] S2 | Carrier identity resolution across brokers on MC/DOT | builder | S1 | Matched in the projection, trimmed and upper-cased once. Self-exclusion is **global by MC**, not lane-scoped: own-MC ∩ pool-MC is empty for all three brokers |
| [x] S3 | Pool projection that **cannot select rate columns** — separate read path, not a filter | builder | S2 | `carrier_pool_reader` has column grants excluding every money column and no grant on `loads` at all. `SELECT carrier_rate FROM loads`, `avg_rate_per_mile`, and a bare `SELECT *` all return **permission denied**. A `pg_depend` test proves the view never even *reads* a money column |
| [x] S4 | Pool carriers merged into rankings, visibly labeled, reasons from shareable fields only | builder | S3 | On-time crosses as a **band**, not a ratio — "18 of 22" is a fingerprint identifying one carrier under one broker. `pool_audit` records every read, append-only |
| [x] S5 | Leak tests: every shareable field present, every forbidden field absent | integration-tester | S4 | 43 tests, 454→497. Field-by-field both directions, bucketing boundaries, and a join-path attack (`pool_carrier_lane → carriers → loads`) proving RLS still confines it — the one way MC/DOT could have become a tenant leak |
| [x] S6 | Attack the boundary specifically | breaker → lead | S5 | `breaker` was cut short by a spend limit; run by the lead instead. **D26**: five attacks failed, one finding — opting out is disclosive to an observer, resolving D17's "about one bit" of source anonymity to certainty. Money never moves; the bands hold |

---

## Critical path

`D1/D3 → G1 → DG1-9 → M1-3 → A1-5 → I1-10 → L1-4 → R1-5 → P1-5 → U1-6 → H1-5 → X1-5`

Parallelizable once Phase 2 lands: UI against stubbed responses, geo table, `DECISIONS.md` drafting.

## Biggest risks

1. **DG5–DG9** — if the fixtures don't demonstrate the behaviors, nothing downstream can prove anything. Most valuable hours on the project.
2. **I7 + I10** — the rebuild-on-correction story is the assignment's core question. Weak here means weak everywhere.
3. **R4** — reasons diverging from scores is a product-level bug, not a code-level one.
