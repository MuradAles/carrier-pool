# TASKS

Execution plan for `PRD.md`. Ordered by dependency — each phase needs the one above it.

**Legend:** `[ ]` todo · `[~]` in progress · `[x]` done · `[!]` blocked
**Agents:** `orchestrator` (runs a whole phase) · `data-gen` · `builder` · `unit-tester` ·
`integration-tester` · `breaker` · `reviewer` · `stack-docs`

Phases are handed to `orchestrator` one at a time; the per-task `Agent` column below is who it
dispatches.

Status: **Phases 0 and 1 complete.** F2 cleared — Docker is up, Postgres 16.14 healthy.
150 tests passing (130 unit + 22 data-integrity, minus overlap). Phase 2 ready to start.

One item deferred to the user: the TMS C on-time timezone rule (see Phase 1 follow-ups).

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
| [ ] M1 | Canonical dataclasses: Load, Stop, Carrier, Customer | builder | D3 | Covers every field all three TMSs produce |
| [ ] M2 | DB schema (PRD §5) + migration/bootstrap SQL | builder | M1, D3 | Applies to a fresh Postgres cleanly |
| [ ] M3 | Repository layer — **`broker_id` enforced structurally**, not by convention | builder | M2 | A query cannot be written without a broker |
| [ ] M4 | Repository tenant-isolation tests | integration-tester | M3 | Proves no path reaches cross-broker rows |

---

## Phase 3 — Adapters

| # | Task | Agent | Depends | Done when |
|---|---|---|---|---|
| [ ] A1 | Adapter interface + shared normalization helpers (units, time, equipment) | builder | M1 | One place per rule |
| [ ] A2 | TMS A adapter | builder | A1 | `example_sync.jsonc` → expected canonical load |
| [ ] A3 | TMS B adapter — kg/km, **DST-aware** Central, rate-row summing | builder | A1 | July = UTC-5; negatives included |
| [ ] A4 | TMS C adapter — referenced_records, per-line-item weight units, null equipment | builder | A1 | Null equipment → `UNKNOWN`, never `DRY_VAN` |
| [ ] A5 | Adapter unit tests, all three | unit-tester | A2-4 | Arithmetic shown in every assertion |

---

## Phase 4 — Ingestion

The correction-handling story lives here. This is the heart of the assignment.

| # | Task | Agent | Depends | Done when |
|---|---|---|---|---|
| [ ] I1 | File discovery + chronological sort by filename timestamp | builder | A5 | Strict order across all three directories |
| [ ] I2 | One-file-at-a-time ingestion loop, append-only `sync_events` | builder | I1, M3 | Never bulk-loads |
| [ ] I3 | Upsert loads/carriers/customers from newest event | builder | I2 | Later sync overwrites earlier truth |
| [ ] I4 | **TMS B rate-only sync** — load absent from `loads` array still updates money | builder | I3, D3 | The known trap is handled |
| [ ] I5 | Idempotency on `broker_id + sync_file` | builder | I3 | Re-ingest changes no row and no summed total |
| [ ] I6 | Dirty-key marking — carrier, lane, and every tier above | builder | I3 | All affected keys dirtied |
| [ ] I7 | Rebuild `lane_stats` / `carrier_stats` from raw events for dirty keys | builder | I6 | Rebuild, never delta-patch |
| [ ] I8 | Carrier last-known delivery position (for deadhead) | builder | I7 | Updated on DELIVERED |
| [ ] I9 | Ingestion integration tests: order, idempotency, overwrite, partial failure | integration-tester | I8 | Real Postgres |
| [ ] I10 | **Replay-equivalence test** — correct-then-rebuild equals ingest-corrected-from-start | integration-tester | I9 | All three correction flavors |

---

## Phase 5 — Lanes and pricing

| # | Task | Agent | Depends | Done when |
|---|---|---|---|---|
| [ ] L1 | Lane keys at all three tiers (ZIP3, METRO, REGION) | builder | I7, G1 | Suburb scatter collapses to one metro lane |
| [ ] L2 | Tier walk with 5-load minimum, **reporting the tier used** | builder | L1 | Falls outward correctly |
| [ ] L3 | Price estimate: median × miles, p25–p75 range | builder | L2 | Percentiles in SQL |
| [ ] L4 | Confidence + provenance (tier, load count, date range, equipment) | builder | L3 | Low confidence labeled, not hidden |
| [ ] L5 | Lane/pricing unit tests | unit-tester | L4 | Tier boundaries at 4 vs 5 loads |

---

## Phase 6 — Carrier ranking

| # | Task | Agent | Depends | Done when |
|---|---|---|---|---|
| [ ] R1 | Five signals: lane experience, recency, equipment, deadhead, on-time | builder | L2, I8 | Each 0–1, independently testable |
| [ ] R2 | Shrinkage toward lane average, `k=5` | builder | R1 | 2-for-2 doesn't beat 164-for-200 |
| [ ] R3 | Weighted score 0–100 | builder | R2 | Weights match PRD §8 |
| [ ] R4 | **Reasons generated from the same values that produced the score** | builder | R3 | Single computation feeds both |
| [ ] R5 | Weak carriers still returned, ranked last, with an accurate reason | builder | R4 | Never silently dropped |
| [ ] R6 | Scoring unit tests incl. deadhead curve and `UNKNOWN` equipment neutrality | unit-tester | R5 | Monotonic where it should be |

---

## Phase 7 — API

| # | Task | Agent | Depends | Done when |
|---|---|---|---|---|
| [ ] P1 | `/api/brokers`, `/api/loads`, `/api/loads/{id}` | builder | M3 | Replaces the 501 stubs |
| [ ] P2 | `/api/loads/{id}/recommendations` | builder | R5 | Score + carrier + reasons |
| [ ] P3 | `/api/loads/{id}/price-estimate` | builder | L4 | Estimate + range + provenance |
| [ ] P4 | `/api/admin/ingest` — replay all files chronologically | builder | I5 | Idempotent |
| [ ] P5 | Load detail includes sync history, so corrections are visible | builder | P1 | Every version retrievable |
| [ ] P6 | API integration tests incl. cross-broker access attempt | integration-tester | P5 | 404, not 500; no leak |

---

## Phase 8 — UI

Correctness and clarity only. README:101 — visual polish counts for nothing.

| # | Task | Agent | Depends | Done when |
|---|---|---|---|---|
| [ ] U1 | API client + types | builder | P3 | Typed against real responses |
| [ ] U2 | Load list: broker dropdown, status filter, `ACTIVE` first-class | builder | U1 | Lists day-11 loads |
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
| [ ] H4 | Full review against the invariants | reviewer | H3 | Report, ranked by severity |
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
