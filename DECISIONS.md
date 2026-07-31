# DECISIONS

Judgment calls, the alternatives I rejected, and what is still wrong with this. Written as the decisions were made, not reconstructed afterwards.

Each entry gives the call and the alternative it beat. Where a decision turned on arithmetic, the figure that settled it is quoted inline.

The README asks five questions. Corrections and derived stats are D3. What counts as a lane is D6 and D11. Fairness to a carrier with little history is D5 and D21. Pricing a thin lane is D6, D15 and D18. The pool boundary is D17 and D26.

---

## The calls that shaped the system

**D3. The event log is two tables, and events are per entity.** TMS B breaks the obvious one-event-per-load design twice. A sync can append a rate line for a load whose `loads` row did not change, and its carrier rate is the sum of every line item ever appended. So `sync_files` (one row per file, `UNIQUE (broker_id, sync_file)`, so idempotency is enforced by the database) is split from `sync_events` (one row per changed entity, carrying `source_load_id` on rate lines too). That grain is what makes a rate-only sync representable at all. Rejected: one event per file with loads rediscovered by re-parsing, which kills the dirty-key optimization. Rejected: storing current state only, which destroys the audit trail the UI has to show.

**D5. Experience is a saturating count, not a shrunk rate.** PRD §8 applied one shrinkage formula to both lane experience and on-time. That formula shrinks a rate. Applied to a count it lets a carrier with zero loads on the lane score like an average one, which rewards the absence of evidence. Split them: experience is `n/(n+5)`, on-time shrinks toward the lane average with `k=5`. D21 corrects this entry's original claim about what the split preserves, which was false on the on-time half. The formulas were right; my sentence about them was not.

**D6. Equipment filters at every tier, and the walk has a fourth rung.** `ZIP3+equip`, then `METRO+equip`, then `REGION+equip`, then region with no filter at all. The last rung exists so rare equipment gets an answer instead of nothing, and it is always low confidence. A load whose own equipment is `UNKNOWN` skips the filter and says so. Rejected: equipment as a scoring penalty rather than a filter, which lets flatbed prices contaminate a reefer estimate invisibly.

**D13. Each broker gets a distinct rate band, so a tenant leak moves the money.** The first generation gave two brokers an identical $1.8200/mi median on the rich lane, and the pooled median was also $1.8200. A repository that forgot `broker_id` would have returned the right headline price, betrayed only by a load count. I separated the bands and added a validator asserting 0.15 $/mi between every broker's median and the pooled one. The assertion matters more than the regeneration: it stops a future re-seed colliding them again.

**D15. A mixed equipment pool caps confidence at medium.** D6's filter skip draws an estimate from every equipment type at once. One day-11 load matched 31 loads and came out labelled high, but those 31 are two disjoint clusters, and the published p75 landed in the empty 0.27 $/mi gap between them. That is a rate nobody has ever been paid. Capped at medium, with the mix named in the provenance. Rejected: capping every unfiltered estimate, which punishes the tier rather than the defect. Rejected: dropping the range, since the range was honest and the label was what lied. Rejected: splitting per equipment type, which invents a distinction the broker has not made.

**D16. TMS C on-time compares in Central, not UTC.** Its scheduled date is a bare local date while its arrival time is UTC, and nothing in the schema says so. Comparing the UTC date marks every delivery after 19:00 Central a day late. 14 of 96 arrivals flip, seven of twelve broker_c carriers get a different count, and one swings 25 points. Rejected: treating the bare date as UTC midnight, which records a load delivered at 8pm Central on its scheduled day as late. That is wrong in the domain, not merely inconvenient.

**D17. The pool boundary, field by field.** Crossing: MC/DOT, name, phone, home city, lane at METRO and REGION only, equipment, load count in buckets, on-time as a band, recency as a boolean. Never crossing: any rate or margin column, customers, load ids, stops, cargo, ZIP3 keys, truck positions, raw JSON. Enforced as a projection whose owning role holds no grant on the money columns, rather than a filter over the full record, which is one forgotten `SELECT *` away from a leak. Rejected: a `shared` boolean, where the control enforcing the boundary becomes the control relaxing it. Rejected: a physical pool table, a second source of truth that drifts on rebuild. Rejected: differential privacy, since at n=22 the noise makes counts useless. Rejected: per-carrier opt-in, because the set you decline to share is itself the signal.

**D18. An estimate has to be reproducible by hand from its own provenance line.** Six day-11 loads disagreed with the traceability table by a cent, because percentiles are stored `NUMERIC(10,4)` and the document computed at full float. I kept the stored precision and fixed the document, which could be shown wrong on its own terms: it printed a multiplication and then a different answer. An estimate whose own evidence does not reproduce it is worse than one a cent from a hypothetical. Rejected: widening the column, since nobody verifies `2.020017478 × 187.2` on a phone call. Rejected: a tolerance band in the e2e check, which is where a real regression later hides.

**D21. "2-for-2 must not beat 164-for-200" was false as written, and the formula is right.** Three documents stated it without naming a signal. On the shrunk on-time rate the rookie does win, whenever the lane average exceeds `738/990`, which is every substantial lane here. That is shrinkage working: a veteran at 82% on a lane averaging 92% is genuinely below average. I changed the claim rather than the formula. The trap still holds where it matters, since on-time carries 0.10 and can hand the rookie 1.8 points while experience carries 0.35 and hands the veteran 24.1. Rejected: a saturating curve for on-time, which re-answers D5 wrongly. Rejected: raising `k`, as no constant makes the claim true.

**D23. An impossible input is not a measurement, and the sentence beside it has to say so.** The pattern behind five findings. A `NaN`, a negative distance, a future-dated delivery, an unknown weight unit, a $0 percentile: each was carried through the arithmetic with a confident sentence printed beside it. One rule now, applied at the boundary each value crosses. Refuse, and ship the wording that names the refusal. Refusal beats a default because every plausible default was worse, and reading `tons` as pounds understates a load 2000-fold. The sentence is half of it: a silent refusal keeps the arithmetic honest while breaking the explanation, and for this product the explanation is the deliverable.

**D25. A refusal has to be refused everywhere it is read, and a caveat may only claim what happened.** Five review findings, three of them the same mistake at different layers. The sharpest one: the provenance said "confidence is capped at medium" beside a confidence field reading low, and it reached the screen, after the frontend had deliberately refused to name a confidence level for exactly that reason. Also a negative distance, refused as a subject but accepted as evidence, which published "Averages $-1.85/mi" inside a carrier's reasons. I rejected one of the five findings, and a rejected finding belongs in this file as much as an accepted one.

**D26. The pool boundary, attacked.** Five attacks failed. Money is unreadable through the pool role and a bare `SELECT *` fails closed, the view still requires a bound broker, every published field is banded, self-exclusion holds, and a banded value cannot be pinned. One attack worked: watching a broker opt out and back in attributes its rows exactly, 45 to 24 to 45. It yields which broker runs a carrier, never how often or at what price. Not fixed. D17's claim that source anonymity is worth "about one bit" is corrected to certainty.

---

## The smaller calls

Same numbering, one line each.

| | Decided | Instead of |
|---|---|---|
| D1 | Most history arrives once, already `COMPLETED`; the multi-appearance budget goes to lifecycle and corrections | Tracing every load end to end, which yields ~30 loads per broker and starves the lane statistics |
| D2 | Added MC/DOT to TMS C carriers, disclosed as an extension of provided material | Fuzzy name matching, where a wrong cross-broker match is the exact isolation failure this must not have |
| D4 | Build the pool last, off the critical path | Building a sharing boundary before the non-sharing behaviour is proven |
| D7 | On-time is delivered on or before the scheduled date, the only precision all three schemas support | Hour-level on-time, which makes "on time" mean something different per broker |
| D8 | Ingest synchronously in the FastAPI lifespan, before serving | Background ingest, where the failure mode is wrong answers rather than slow ones |
| D9 | All three brokers get ~93 history loads and equal depth | One showcase broker, which weakens the isolation demonstration |
| D10 | Keep `ROAD_FACTOR = 1.2` and record the 13% overstatement | `1.06`, trading a documented uniform bias for an undocumented non-uniform one |
| D11 | On a city fallback, keep the city's coordinates but the load's own zip | Substituting the table's zip, which makes the ZIP3 tier key silently wrong |
| D12 | Pin the recency and deadhead curves in the traceability table before writing the scorer | Regenerating the table afterwards, since a check derived from the thing it checks cannot fail |
| D14 | Correct a sanity bound specified in the wrong unit, and add a tighter check in the same change | Widening it quietly, which looks identical in a diff |
| D19 | Two scorers share a rounding rule, never code | Making the generator import `scoring.py`, which removes the oracle |
| D20 | A gap in the load scores neutral; a gap in one carrier's record scores zero | One rule for both, which promotes a carrier for the absence of evidence |
| D22 | Dedupe rate lines on `rate_id`, not just on the file | The file key alone, which silently doubled a carrier rate on an overlapping sync |
| D24 | Rank carriers known only from a dangling load reference | Inventing a stub `carriers` row, which is a different claim from what we know |

---

## Honest limitations

There is no authentication. `broker_id` is a query parameter, so anything that can reach the API can name any broker. Everything above enforces isolation against a query that forgot its tenant, not against a caller who lies about theirs. This is the largest gap between this and something you could run.

The integration suite shares a mutable database. Its fixture truncates the tenant tables around every test on one fixed database, so a second pytest invocation, or a running backend, corrupts it. Measured: 2 concurrent processes give 454 passed, 8 give 9 failed, 410 passed and 38 errors, and the collision can kill the live backend mid-boot. The bad part is that the failures include wrong-value assertions (`assert 9 == 12`, an unexpected top carrier) that look like product regressions rather than infrastructure. The adversarial suite solved this for itself with a database per pid and this one did not. It is a harness defect of the kind this project treats as real when it appears in product code. Product code is unaffected, and the unit and adversarial suites are deterministic.

Four defects are characterised and unfixed. A nested `broker_session` keeps its binding after the savepoint releases, so the third isolation barrier's real strength is "nothing in this codebase does that". `/recommendations` and `/price-estimate` each run their own tier walk, so an ingest landing between them could put a ZIP3 ranking beside a METRO estimate. An unplaceable pickup lifts every carrier on that load by exactly 10.00 points, which is rank-neutral within one load and not comparable across loads. `bootstrap()` races on Postgres's shared role catalog, so two API instances against one cluster kill one of them.

Deadhead scores stricter than the PRD states. That is D10's consequence: 85 of 192 carrier-load pairs would change credit at a true road factor, and 45 cross a threshold. No day-11 answer changes, but the policy the code applies is not the policy the PRD describes, and it is always the harsher one.

The pool has no k-anonymity. At three brokers it is arithmetically unavailable, since every pooled statistic about a shared carrier is one other broker's data minus your own, and only 2 of 34 carriers are shared at all. Bucketing is obfuscation without a proof. `pool_audit` records every read and nothing reads it.

There is dead code. `_provenance`'s no-rung fallback cannot fire, because the region keys do not depend on either lane end resolving. It produces no wrong answer, which is a reason not to hurry rather than a reason it is fine.

This runs on one machine. One Postgres, one process, one connection, and ingestion is a `for` loop over 132 files.

### What breaks at millions of loads

The rebuild, and the bottleneck is the region rungs. One touched load dirties 8 lane keys, three of them `TX_TRIANGLE→TX_TRIANGLE`, a bucket that is the broker's entire placeable history. So every file re-runs three `percentile_cont` sorts over every load the broker has ever had, inside the ingest transaction, holding the write lock on `lane_stats`. Cost per file is O(the broker's total loads) rather than O(loads changed), so ingestion serialises against itself and tail latency grows with history. That is the wrong way round.

The fix is not delta-patching. Rebuilding is what buys "a late correction produces the same numbers as if it had arrived on time", which is the assignment's central question. In order: move the rebuild into an idempotent job keyed on the dirty key, so ingestion only records dirt. The tier walk then reads a possibly stale row and has to say so, which changes the provenance line rather than merely a scheduler. Then shard that queue by broker, which chronological ordering permits. Then replace exact percentiles on the region rungs with a mergeable sketch. Only the last one changes a published number, and only at the two rungs already fixed at low confidence.

### Where the verification stops

`data/TRACEABILITY.md` was computed by a reference scorer written from the PRD before any production code existed, sharing only the geo table. It agreed with the implementation on all 192 ranking rows and every signal value, and it was the authoritative artifact three times.

It cannot catch a misreading held once and implemented twice. That is D21 exactly: three documents asserted the same false sentence, the table was generated from that sentence, and the one test that came near it picked a lane average below the crossover, said so in its docstring, and passed for four phases. Two further limits. The oracle checks numbers rather than sentences, and three of D25's five findings were sentences. And its authority stops at the corpus, since "effect on the shipped fixture: none" appears three times, which is the weakest evidence here and is labelled as such wherever it appears.

---

## What I would do next, in this order

1. Authentication, with `broker_id` coming from the session rather than the URL. Everything else on this list improves an answer. This one is the difference between a boundary that holds against a bug and one that holds against a person. It is also the cheapest, since the binding point exists and is already enforced and only its input is untrusted.
2. A private database per pytest session. An hour of work on the harness that certifies everything else. I left it undone deliberately at the end of a long session, because that is how a green suite starts certifying nothing.
3. Move the derived-stats rebuild off the ingest transaction. It is the first thing that fails at scale and it is structural, so it wants doing before more code assumes the rebuild is synchronous.
4. A property-based layer over the three adapters. The failed attacks are shapes someone thought of, and both rate-line dedupe defects were found by reading schemas rather than running data.
5. Split the D15 estimate per equipment type. It removes the one estimate whose p75 sits in an interval no carrier has ever been paid in.
6. One tier walk behind both endpoints. Narrow, and currently masked by D8, but a product-level contradiction on screen is the class of bug this project treats as worst.
