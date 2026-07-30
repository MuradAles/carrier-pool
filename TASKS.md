# TASKS

Execution plan for `PRD.md`. Ordered by dependency — each phase needs the one above it.

**Legend:** `[ ]` todo · `[~]` in progress · `[x]` done · `[!]` blocked
**Agents:** `orchestrator` (runs a whole phase) · `data-gen` · `builder` · `unit-tester` ·
`integration-tester` · `breaker` · `reviewer` · `stack-docs`

Phases are handed to `orchestrator` one at a time; the per-task `Agent` column below is who it
dispatches.

Status: **Phases 0–7 complete.** 338 tests (285 unit + 22 data-integrity + 31 integration).
Committed through `074d7cd` on `phase-1-geography-and-fixtures`.

- Tenant isolation is enforced by Postgres RLS under a non-privileged role, not by convention —
  an unscoped query *raises* rather than returning rows, and a cross-broker request 404s.
- A late correction produces derived state **identical** to having ingested the corrected value
  from the start, proven for all three correction flavors across every percentile (I10).
- Production scoring and the generator's independent reference scorer agree on **all 192**
  ranking rows and 16/16 expected top carriers, having shared a rounding *rule* and never code.

**In flight:** P6 (API integration tests) · U3–U6 (load detail, price panel, ranked carriers,
sync history).

**Not started:** Phase 9 — *nothing adversarial has run yet*, which `CLAUDE.md` says is what
"done" requires · Phase 10 (README run section, clean-checkout rehearsal, walkthrough notes) ·
Phase 11 (pool build; the design ships as D17 regardless).

**Carried defects:** the integration suite truncates a shared database, so concurrent pytest
runs deadlock — matters for X4 · unreachable provenance fallback in `pricing.py`, logged against
H4.

---

## Phase 0 — Decisions and foundation ✅

Cheap now, expensive later. Nothing below should start until D1–D4 are settled.

| # | Task | Agent | Done when |
|---|---|---|---|
| [x] F1 | Scaffold: compose, Dockerfiles, deps, health endpoint, placeholder UI | — | `docker compose config` validates |
| [!] F2 | Verify the stack actually boots | builder | **Blocked** — Docker daemon not running. Deferred to start of Phase 1 |
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
| [ ] P6 | API integration tests incl. cross-broker access attempt | integration-tester | P5 | 404, not 500; no leak |

---

## Phase 8 — UI

Correctness and clarity only. README:101 — visual polish counts for nothing.

| # | Task | Agent | Depends | Done when |
|---|---|---|---|---|
| [x] U1 | API client + types | builder | P3 | Built ahead of the API behind one documented seam (`provisional.tsx`). Nullable in the API is nullable in the type — an `ACTIVE` load must not render `$0.00` |
| [x] U2 | Load list: broker dropdown, status filter, `ACTIVE` first-class | builder | U1 | No router, no state library. **The browser computes nothing it displays** — verified by grep for division, ×100, `toFixed` and `reduce` outside the formatter |
| [ ] U3 | Load detail: facts, stops, dates, rates | builder | U1 | — |
| [ ] U4 | Price estimate panel with provenance line | builder | U3 | Shows tier and load count |
| [ ] U5 | Ranked carriers with bulleted reasons | builder | U3 | Reasons legible to a non-technical rep |
| [ ] U6 | Sync history panel — a correction is visibly a correction | builder | P5 | — |

---

## Phase 9 — Hardening

| # | Task | Agent | Depends | Done when |
|---|---|---|---|---|
| [ ] H1 | **End-to-end check**: fresh DB → ingest 132 files → named day-11 load returns expected top carrier | integration-tester | U6, DG8 | One command; prints *why* it passed |
| [ ] H2 | Adversarial pass: tenant leaks, correction chains, hostile inputs, statistical nonsense | breaker | H1 | Findings reproduced, or attacks documented as failed |
| [ ] H3 | Fix what `breaker` found | builder | H2 | Each with a regression test |
| [ ] H4 | Full review against the invariants | reviewer | H3 | Report, ranked by severity. **Carry-in:** `pricing.py` `_provenance` has an unreachable fallback (`'nothing — lane ends not on the map'`) — `REGION`/`REGION_ANY` key on a fixed constant rather than the load's geography, so `tried` can never be empty and even a geo-null load yields `(tried REGION 0, REGION_ANY 0)`. Dead code, not a wrong answer, but it reads as handling a case it cannot reach |
| [ ] H5 | Fix what `reviewer` found | builder | H4 | — |

---

## Phase 10 — Delivery

| # | Task | Agent | Depends | Done when |
|---|---|---|---|---|
| [ ] X1 | README run section — exact command sequence, from clean checkout | builder | H1 | A stranger can reproduce results |
| [ ] X2 | `DECISIONS.md`: judgment calls, rejected alternatives, honest limits | builder | H5 | D1–D4 plus what `breaker` couldn't break |
| [ ] X3 | Shared-pool section — what crosses the broker boundary, what never does, how it's enforced | builder | D4 | Boundary + threat model + enforcement |
| [ ] X4 | Clean-checkout rehearsal: `git clone` → `docker compose up` → verify | integration-tester | X1 | Works on a machine with nothing cached |
| [ ] X5 | Walkthrough notes for the review call — trace one day-11 answer end to end by hand | — | X4 | Defensible without the code |

---

## Phase 11 — Shared carrier pool (optional, off the critical path)

Per D4. Only starts once tenant isolation is proven by passing tests. If it doesn't get built,
it ships as the written design in `DECISIONS.md` and nothing else changes.

| # | Task | Agent | Depends | Done when |
|---|---|---|---|---|
| [ ] S1 | Opt-in flag per broker | builder | X2 | Off by default; no behavior change when off |
| [ ] S2 | Carrier identity resolution across brokers on MC/DOT | builder | S1 | Same carrier recognized across TMSs |
| [ ] S3 | Pool projection that **cannot select rate columns** — separate read path, not a filter | builder | S2 | Structurally incapable of returning money |
| [ ] S4 | Pool carriers merged into rankings, visibly labeled, reasons from shareable fields only | builder | S3 | A leak would be visible in output |
| [ ] S5 | Leak tests: every shareable field present, every forbidden field absent | integration-tester | S4 | Asserts field-by-field |
| [ ] S6 | `breaker` attacks the boundary specifically | breaker | S5 | Attempts documented, successful or not |

---

## Critical path

`D1/D3 → G1 → DG1-9 → M1-3 → A1-5 → I1-10 → L1-4 → R1-5 → P1-5 → U1-6 → H1-5 → X1-5`

Parallelizable once Phase 2 lands: UI against stubbed responses, geo table, `DECISIONS.md` drafting.

## Biggest risks

1. **DG5–DG9** — if the fixtures don't demonstrate the behaviors, nothing downstream can prove anything. Most valuable hours on the project.
2. **I7 + I10** — the rebuild-on-correction story is the assignment's core question. Weak here means weak everywhere.
3. **R4** — reasons diverging from scores is a product-level bug, not a code-level one.
