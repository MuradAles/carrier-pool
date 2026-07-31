# DECISIONS — appendix

The working behind `DECISIONS.md`. Same 26 decisions, same numbering, with the arithmetic that
is the argument: the reproductions, the measured effects, the field-by-field pool boundary, the
attack logs. Written as decisions were made, not reconstructed afterwards.

`DECISIONS.md` is the deliverable and stands on its own. This is here for a reader who wants to
check a claim rather than accept it.

---

## D1 — Load budget: history arrives mostly as completed backfill

**The constraint.** Each TMS syncs 4×/day for 11 days = 44 files per broker, each holding 1–3
loads. That caps a broker near ~132 load-*appearances*. A load walking the full lifecycle
(PLANNED → ACTIVE → COVERED → IN_TRANSIT → DELIVERED → COMPLETED) consumes 4–6 of them. So if
every load were traced end to end, a broker would end up with roughly 30 distinct loads — not
enough to fund a rich lane (25+ loads), a thin lane, *and* a spread of carrier experience
levels at the same time.

**Decision.** Most historical loads appear **once, already `COMPLETED`**. The multi-appearance
budget is spent deliberately on the loads that exist to demonstrate lifecycle progression and
corrections.

**Why this is honest, not a shortcut.** A sync returns everything changed since the last one.
A load created and closed between two syncs legitimately appears once, in its final state — and
brokers routinely backfill history when connecting a new system. The data stays realistic while
the fixture budget goes where it demonstrates something.

**Rejected:** tracing every load end to end. More lifecycle coverage, but it starves lane and
carrier statistics, and lane depth is what the recommendations are actually built on.

---

## D2 — TMS C schema extended with carrier MC/DOT numbers

**The problem.** Cross-TMS carrier identity relies on MC/DOT numbers — the federal authority
numbers that identify a trucking company regardless of what any given system calls it. TMS A
has `mcNumber`/`dotNumber` and TMS B has `mc_no`/`dot_no`, but the provided TMS C schema
exposes carriers only as `Account` records with a name.

**Decision.** Add `bos__MC_Number__c` and `bos__DOT_Number__c` to Carrier Accounts in the TMS C
fixtures, and disclose it here as a **deliberate extension of provided material**.

**Why.** Real CRM-based TMSs carry these numbers; their absence is an omission in an
illustrative example, not a meaningful constraint. Without them, cross-broker carrier identity
has no reliable key, which undercuts both the shared-pool design and the demonstration that
nothing leaks between brokers.

**Rejected:** matching carriers by name + home city. No schema change, but "DELTA PRIME LLC"
vs "Delta Prime, L.L.C." makes fuzzy matching its own bug source — and a *wrong* identity match
across brokers is precisely the tenant-isolation failure this project must not have.

---

## D3 — The event log is two tables, and events are per-entity

**The problem.** The obvious design is one event row per load object per file. TMS B breaks it
twice. A sync can append a rate line item for a load whose `loads` row did not change — so
there is no load object to hang the event on, yet the load's money must change. And TMS B's
carrier rate is the sum of *every* line item ever appended, so "the newest event for this load"
never holds the full picture the way it does for TMS A and C.

**Decision.** Split provenance from change events:

```
sync_files    id, broker_id, sync_file, synced_at, raw_json, ingested_at
              UNIQUE (broker_id, sync_file)

sync_events   id, sync_file_id, broker_id, entity_type, source_entity_id,
              source_load_id, raw_json, event_seq
              entity_type: LOAD | CARRIER | CUSTOMER | RATE_LINE
```

`sync_files` answers *"was this file ingested?"* — one row per file, holding the raw bytes. The
unique constraint is the idempotency guarantee: re-ingesting is a no-op at the database level,
not by convention.

`sync_events` answers *"what changed, and where did it come from?"* — one row per changed
entity. `source_load_id` is set on both `LOAD` and `RATE_LINE` rows, so recomputing any load
means selecting every event that mentions it, ordered by `(synced_at, event_seq)`, regardless
of which shape it arrived in. `source_entity_id` (a TMS B `rate_id`, a carrier ID) gives a
second dedupe key beneath the file-level one.

**What this buys.** A TMS B rate-only sync produces `RATE_LINE` events and no `LOAD` event —
representable, and the rebuild finds them. Ordering is total: filename across files, `event_seq`
within one. And "rebuild the touched keys from the raw log" stays literally true, because the
log holds every contribution rather than the latest snapshot.

**Rejected:** one event per file, with affected loads discovered by re-parsing at rebuild time.
Simpler to write, but every rebuild re-parses every file that ever mentioned the load, and the
dirty-key optimization stops meaning anything.

**Rejected:** storing only the current load state and treating corrections as overwrites. Fast,
but it destroys the audit trail — and "show every version of this load as it arrived" is a
required UI feature precisely because a correction has to be *visible*.

---

## D4 — Shared carrier pool: built, but last and optional

**Decision.** Build it, as the final phase, deliberately off the critical path. If it doesn't
get built, it ships as the written design below and nothing else is affected.

**Why build it.** It is the assignment's stated appetite test, and it's the only part that
demonstrates the tenant isolation everything else merely asserts. A boundary you enforce in
code and prove with a test is a stronger claim than a boundary you describe.

**Why last.** It depends on tenant isolation already being correct and tested. Building it
before the core is proven would mean designing a sharing boundary around code whose
non-sharing behavior isn't yet verified — backwards.

**The boundary** (holds whether built or written):

| Crosses the boundary, opt-in only | Never crosses |
|---|---|
| Carrier identity: MC/DOT, name, phone | What another broker pays them |
| Lanes they run and how often | Another broker's customers |
| Equipment they operate | Another broker's margins or customer rates |
| On-time performance | Which specific loads they hauled for whom |
| Coarse recency ("active in the last 30 days") | Exact load dates, IDs, or history |

**The threat model.** Brokers are competitors. Leaking what Broker B pays a carrier hands
Broker A B's cost structure — enough to undercut B's bid or poach the carrier with a slightly
better rate. That is the commercial harm the boundary exists to prevent; everything else is
secondary.

**Enforcement.** Pool data is served from a separate read path with its own projection that
physically cannot select rate columns — not a filter applied to the full record, which is one
forgotten `SELECT *` away from a leak. Pool-sourced carriers are visibly labeled in the UI and
carry reasons built only from shareable fields, so a leak would be visible in the output rather
than silent.

---

## D5 — Experience is a saturating count, not a shrunk rate

**The problem.** PRD §8 applied one formula — `adjusted = (observed × n + lane_avg × k)/(n + k)`
— to both lane experience and on-time. That formula shrinks a **rate** toward a prior. On-time
is a rate and fits perfectly. Lane experience is a **count**, and shrinking a count toward "the
lane average count" means a carrier with zero loads on the lane scores like an average one.
Backwards: the signal would reward absence of evidence.

**Decision.** Split them.

- **Lane experience** = `n / (n + k)`, `k = 5`. 1 load → 0.17, 5 → 0.50, 20 → 0.80, 50 → 0.91.
  Monotonic, saturating, zero at zero.
- **On-time** = the shrinkage formula, toward the lane's average on-time rate, `k = 5`.

**Why this keeps the invariant — corrected by D21.** This entry originally read: *"`CLAUDE.md`'s
'2-for-2 must not beat 164-for-200' is an on-time framing, and on-time still uses shrinkage.
Under the saturating curve, 2 loads (0.29) also cannot outscore 200 loads (0.98) on experience.
Both readings hold."* **The on-time half of that is false**, and `breaker` proved it: shrinkage
toward the lane mean puts 2-for-2 *above* 164-for-200 for every lane average above
`738/990 ≈ 0.7455`. The experience half holds — `n/(n+5)` is monotone in the count — and so does
the composite, by 22.4 points or more. The formulas here are unchanged and correct; it was the
claim about them that was wrong. See D21 for the algebra and why the formula stays.

---

## D6 — Equipment filters at every tier; the walk has a fourth rung

**Decision.** Equipment is a hard filter at all three tiers, so the walk is
`ZIP3+equip → METRO+equip → REGION+equip → REGION, any equipment`. The last rung exists so a
rare equipment type still gets an answer instead of nothing; it is always reported as such and
is automatically **low** confidence.

A load whose own equipment is `UNKNOWN` skips the filter entirely and its provenance line reads
"all equipment types" — consistent with invariant 5: `UNKNOWN` neither rewards nor punishes, so
it must not silently become a dry-van query.

**Rejected:** equipment as a scoring penalty rather than a filter. It lets a flatbed lane's
prices contaminate a reefer estimate, and the contamination is invisible in the output.

---

## D7 — On-time is a day-granular comparison

**The constraint.** The three schemas do not agree on precision. TMS B gives `del_date` (date
only) against `del_arrived_at` (timestamp). TMS A gives an estimated window on the last drop
plus `actualDepartureDateTime`. TMS C gives `bos__Scheduled_Date__c` (date) plus
`bos__Arrival_Time__c`.

**Decision.** On-time means **delivered on or before the scheduled delivery date**. The only
definition all three formats can support honestly.

**Rejected:** hour-level on-time using A's and C's timestamps. It would make on-time mean
something different per broker, which is worse than a coarse metric that means one thing.

---

## D8 — Ingestion runs in the FastAPI lifespan, before serving

**Decision.** On startup the backend bootstraps the schema and runs the full chronological
ingest **synchronously**, before accepting requests. `POST /api/admin/ingest` remains as the
manual replay path.

**Why.** `docker compose up` is the graded entry point, and the UI must never be briefly and
inexplicably empty. 132 small files is a few seconds, and ingestion is idempotent on
`broker_id + sync_file`, so a container restart re-runs it for free.

**Rejected:** a separate one-shot `ingest` compose service. Cleaner separation, but it adds a
service and an ordering dependency to the one command a reviewer is guaranteed to run.

**Rejected:** background-task ingestion at startup. The API would answer requests against a
half-loaded database — the failure mode is wrong answers, not slow ones.

---

## D9 — All three brokers get equal fixture depth

**The budget.** Per broker: 44 files, less 4 reserved for day 11, is 40 history files at ≤3
loads = 120 load-appearances. One full-lifecycle load costs 6, two correction loads cost 3
each, and ~5 deliberately empty syncs forgo ~15. That leaves **≈93 distinct history loads per
broker**.

**Decision.** All three brokers get a rich lane (25+ loads), a thin lane (1–2), a veteran
carrier (20+), and cold-start carriers — rather than making one broker a showcase and thinning
the other two.

**Why it fits.** The requirements overlap instead of stacking: a veteran's 20 loads *are* the
rich lane's loads, and the suburb-scatter scenario is expressed *within* the rich lane rather
than beside it. 93 loads funds all of it with slack.

**What stays broker-specific.** Correction flavors are format-bound and cannot be duplicated:
A restates `totalBuy`, B appends a negative `ADJUSTMENT` (including the rate-only case where
the `loads` array does not mention the load), C silently restates `bos__Carrier_Rate__c`.
Likewise the messy edges live where their schema allows them — null equipment and the `kg`
line item are TMS C, the 3-stop load is A or C.

**Why symmetry is worth the tightness.** Tenant isolation is the headline claim. Proving it
with three comparably-rich brokers — where A's numbers must not move when B and C load — is a
stronger demonstration than proving it against two thin ones.

---

## D10 — The 1.2 road factor wins over the "Dallas→Houston ≈ 240 mi" acceptance number

**The conflict.** `CLAUDE.md` invariant 7 and PRD §3 fix distance as *Haversine × 1.2*.
`TASKS.md` G2 states the acceptance criterion as *Dallas→Houston ≈ 240 mi*. With real
coordinates these cannot both hold: the great-circle distance between downtown Dallas and
downtown Houston is **225.8 mi**, so ×1.2 yields **271.0 mi**. The actual I-45 drive is ~239 mi
— a real-world road factor of ~1.06 for that unusually straight pair. Interstate 45 is close to
a straight line; 1.2 is calibrated for typical, less direct pairs.

**Decision.** Keep `ROAD_FACTOR = 1.2` and real coordinates. Treat G2's "≈ 240" as the number
that gives, and record the ~13% overstatement on straight corridor pairs as a known limitation.

**Why.** The factor is a hard invariant; the mileage figure is an illustrative check written
before the coordinates existed. Bending the invariant to hit one pair would make every other
pair worse, and it would be a silent, undocumented change to a rule the reviewer can read.

**Why it is safe downstream — with one exception.** Nothing in the product compares our miles
to a real odometer. Rate-per-mile is computed against the same miles the fixtures were
generated from, so $/mi stays inside the plausible $1.50–$3.50 band, and lane and carrier
comparisons stay honest because the scale factor cancels in every ratio.

**The exception is deadhead, and it does not cancel.** PRD §8 sets *absolute* thresholds — full
credit ≤50 mi, zero credit ≥250 mi. Their counterparty is not another output of `road_miles`;
it is a pair of constants. A uniform scale factor cancels against a ratio, never against a
constant. So the effective policy is stricter than the stated one:

| Stated threshold | Fires at haversine | ≈ true road miles (r≈1.06) |
|---|---|---|
| 250 mi (zero credit) | 208.3 | **220.8** |
| 50 mi (full credit) | 41.7 | **44.2** |

A truck genuinely 221 road miles out gets zero credit under a rule that says 250; one genuinely
48 road miles away loses full credit under a rule that says 50. The bias is one-directional —
deadhead is always scored **more pessimistically** than the stated policy. Measured across all
192 (carrier, day-11 load) pairs in the fixture, 85 (44%) would change credit by more than 0.02
at r=1.06, and 45 cross a zero- or full-credit boundary.

No day-11 answer changes: scenario 7's `FAR` carrier sits at 264.8 mi (233.9 at r=1.06 → credit
0.080, worth 1.6 points) against a 15.8-point margin, so `NEAR` still wins. This is recorded as
a documentation limit rather than fixed, because changing `ROAD_FACTOR` would break a stated
invariant for the benefit of one term in one signal.

**Corrected claim.** The earlier wording here — "every number that matters is a ratio or a
comparison in which it cancels" — was too strong and listed deadhead among the safe consumers.
It isn't one.

**Rejected:** setting `ROAD_FACTOR = 1.06`. Hits 239.4 for Dallas→Houston and breaks the stated
invariant, while under-estimating genuinely indirect pairs — trading a documented uniform bias
for an undocumented non-uniform one.

**Rejected:** nudging coordinates until ×1.2 lands on 240. Fabricated geography that would then
corrupt every other distance and every deadhead calculation involving Dallas or Houston.

**Rejected:** a hardcoded real-mileage matrix for known city pairs. Accurate for the pairs in
it, but it is a second source of truth for distance sitting next to `road_miles`, and the two
would disagree the moment a load used a city pair the matrix missed.

---

## D11 — An unrecognized zip on a recognized city keeps its own `zip3`

**The problem.** `resolve_place(city, state, zip)` matches on zip first and falls back to
city/state. The fallback originally returned the city's *canonical* table row, so a Houston load
with an unlisted zip resolved to Houston 77002 and took `zip3 = 770` — the table's zip, not the
load's. The metro tier would still be right, but the **ZIP3 tier key would be silently wrong**,
and invariant 6 requires every answer to name the tier it used. A wrong key at the narrowest
tier is a wrong answer wearing a correct-looking label.

**Decision.** On the city/state fallback, keep the city's `lat`/`lon`/`metro` but carry the
**load's own zip**, so `zip3` derives from what the load actually said.

**Why.** The coordinates are an approximation either way — city centroid is the best we have
offline. The zip is not an approximation; it arrived in the data. Discarding known-good input in
favor of a table default is the kind of quiet substitution that makes a tier report untrue.

**Rejected:** geo-nulling the whole load when the zip is unknown. It throws away a usable metro
match, and PRD §7's whole point is that the metro tier catches what the zip3 tier misses.

---

## D12 — The reference scorer pins two shapes PRD §8 left open

**The gap.** PRD §8 fixes the five signal weights (0.35 / 0.20 / 0.15 / 0.20 / 0.10) and D5 fixes
both cold-start formulas, but it never says *how* recency decays or *how* deadhead interpolates
between "full credit ≤50 mi" and "zero ≥250 mi". `data/TRACEABILITY.md` cannot state an expected
top carrier without committing to something, because a different decay shape can reorder two
close carriers.

**Decision.** The traceability table pins these and discloses them at its top; Phase 6 implements
**these exact shapes**, so the table stays the independent check on the code rather than a
restatement of it:

```
recency   = exp(-days_since_last_lane_load / 30)
deadhead  = clamp((250 - miles_from_last_delivery) / 200, 0, 1)
equipment = 1 if the carrier has hauled it, else 0;  0.5 if the LOAD's equipment is UNKNOWN
```

The `0.5` for an `UNKNOWN` load is invariant 5 expressed as a number: `UNKNOWN` must neither
reward nor punish, and both 1 and 0 would do one or the other.

**Why it matters that this is written down.** The table is generated *before* the scorer exists,
so it is a real prediction. If Phase 6 silently picks a different curve, every expected score in
a 1,479-line document becomes wrong and the end-to-end check starts asserting fiction. Every
scenario is built with a ≥5-point margin, so rank, tier, counts and price survive a different
curve — but the absolute scores do not, and H1 should assert the former.

**Rejected:** leaving the shapes to Phase 6 and regenerating the table afterwards from whatever
the code does. That is the failure `CLAUDE.md` invariant 2 warns about one level up: a check
derived from the thing it checks cannot fail.

---

## D13 — Each broker gets a distinct rate band, so a tenant leak moves the money

**What the first generation produced.** On the rich lane `750→774` dry van, broker_a and broker_c
had an *identical* median of $1.8200/mi, and the three-broker pooled median was also $1.8200. A
repository layer that forgot `broker_id` would have returned broker_a the same price estimate it
should have returned anyway. The leak would have been silent in the headline number, betrayed
only by the load count (36 instead of 12).

**Decision.** Separate the three brokers' rate bands so every lane's median, p25 and p75 differ
across brokers by a wide margin, and add a **validator assertion** that each broker's
accepted-tier median differs from every other broker's and from the pooled median by ≥0.15 $/mi.

**Why.** D4 names rate leakage as *the* commercial harm the tenant boundary exists to prevent.
Fixtures in which that leak is arithmetically invisible cannot demonstrate the boundary holds —
they can only fail to contradict it. The assertion matters more than the regeneration, because it
stops a future re-seed from quietly re-colliding the bands.

**On realism:** separated bands are *more* realistic, not less. Brokers genuinely pay different
rates on the same lane — different contract terms, volumes and carrier relationships.

---

## D14 — `TASKS.md`'s intra-metro sanity bound was specified against the wrong unit

**What happened.** `unit-tester` was asked to assert that any two places in the same metro are
under ~75 mi apart, as a cheap way to catch a transposed coordinate in 180 hand-entered rows. The
test failed on five pairs, worst being McKinney 75071 ↔ Cleburne 76031 at 88.0 road mi.

**It was the bound that was wrong.** The 75 figure was calibrated on straight-line distance and
then asserted against `road_miles`, which is the same number × 1.2. Straight-line maxima are DFW
73.3 / HOU 65.2 / SAT 57.3 / AUS 57.2 — all under 75. The road-mile image of that bound is 90.
McKinney and Cleburne are genuinely both DFW (Collin and Johnson counties), at opposite corners.

**Decision.** Correct the bound to 90.0 road miles, with the reasoning in the test. Keep the
tighter typo-catcher that actually does the work: every row is nearest to its own metro anchor
(passes for all 157 big-metro rows) and sits within 60 road mi of it (actual maxima DFW 54.3,
HOU 49.1, SAT 39.5, AUS 35.1).

**Recorded because the distinction is easy to abuse.** A loosened tolerance and a corrected
specification look identical in a diff. This one is a correction: the evidence was gathered
before the number moved, the failing pairs were verified as real geography rather than typos, and
a strictly tighter independent check was added in the same change. `unit-tester` left the test
red and escalated rather than widening it, which is why the distinction is checkable at all.

**Also noted for Phase 3:** `resolve_place(city, None, zip)` returns `None`, because the
city/state fallback requires both. All three TMS schemas carry state (`pu_state`, `state`,
`bos__State__c`), so no adapter hits it — but an adapter that dropped state would silently
geo-null every load rather than failing loudly.

---

## D15 — A heterogeneous equipment pool caps confidence at medium

**The problem.** D6 drops the equipment filter for a load whose own equipment is `UNKNOWN`, so
the estimate is drawn from every equipment type at once. PRD §9 sets confidence from tier and
load count alone, so day-11 load `SHP6701577` (Irving 75061 → Pearland 77584, 293.4 mi) matches
31 loads at `METRO DFW→HOU` and is labelled **high**.

Those 31 loads are two disjoint clusters with an empty gap between them:

```
mixed   n=31   p25 2.4800   median 2.5300   p75 2.6700     spread 0.19
dry van n=23   p25 2.4800   median 2.5100   p75 2.5400     spread 0.06
reefer  n= 8   p25 2.8100   median 2.8450   p75 2.8900     spread 0.08
```

The reported p75 of **2.6700 → $783.38 is a rate no carrier has ever been paid on this lane** —
it falls in the 0.27 $/mi gap between the highest dry van (2.54) and the lowest reefer (2.81).
A high-confidence range whose upper bound sits in an empty interval is exactly the failure
invariant 6 exists to prevent: a wrong number wearing a correct-looking label.

**Decision.** Cap confidence at **medium** when the accepted pool is heterogeneous in
equipment — which is precisely and only the D6 filter-skip case. The provenance line names the
mix ("31 loads: 23 dry van, 8 reefer").

**Rejected:** capping *every* unfiltered estimate at medium. Slightly simpler, but it punishes
the tier rather than the actual defect. A pool that happens to be all one equipment type
despite the filter being off is not less trustworthy than a filtered one.

**Rejected:** returning the estimate without a range. Hides the problem instead of labelling
it, and the range is the honest part — it is the *label* that was lying.

**Rejected:** splitting the estimate per equipment type and returning both. Better information,
but the load's equipment is genuinely unknown, so the system would be inventing a distinction
the broker has not made. Deferred as a Phase 5 follow-up if the UI can present it clearly.

---

## D16 — TMS C on-time compares in Central, not UTC

**The problem.** D7 defines on-time as *"delivered on or before the scheduled delivery date"* — a
date comparison. For TMS C the two sides of that comparison live in different calendars:
`bos__Scheduled_Date__c` is a bare local **Central** date, while `bos__Arrival_Time__c` is
**UTC**. Nothing in the provided schema says so; `example_sync.jsonc` annotates the arrival as
"ISO datetime" and leaves the scheduled date unqualified. Worse, `CLAUDE.md`'s Time row said
"C is already UTC", which reads as licence to compare the UTC date directly.

TMS A and B don't have this problem: A carries `-05:00`, so its date component *is* the Central
date, and B is naive Central on both sides.

**Decision.** Convert `bos__Arrival_Time__c` back to Central before comparing dates. Recorded as
a dedicated On-time row in `CLAUDE.md`'s normalization table, so the adapter can't guess.

**What was at stake.** 14 of 96 broker_c last-stop arrivals flip verdict between the two
readings — any delivery at or after 19:00 Central rolls into the next UTC day. `SHP6700394`
arrives `2026-07-07T04:22:00+0000` against a scheduled `2026-07-06`: late under UTC, on-time
under Central. Seven of twelve broker_c carriers get a different count, one swinging 25 points:

```
Metroplex Ridge (V1)   UTC 18/22   Central 21/22
Espinoza Bros   (V3)   UTC  6/16   Central 10/16
Delta Prime     (M1)   UTC  8/11   Central 10/11
Guadalupe Vly  (FAR)   UTC  5/8    Central  7/8
Trinity Bay    (M2)    UTC  8/9    Central  9/9
Montgomery Cty (NEAR)  UTC  2/6    Central  3/6
Frio Line      (C3)    UTC  1/2    Central  2/2
```

**Why Central.** It is what the generator means, it matches the numbers already in
`TRACEABILITY.md`, and it is the only reading under which all three TMSs agree on what "on time"
means — which is D7's whole argument for a coarse metric in the first place. A definition that
silently differs per broker is worse than a coarse one.

**Why this was escalated rather than decided in-flight.** No day-11 winner changes under either
reading and every margin stays above 5 points, so nothing was broken — but the on-time column
and every absolute score in broker_c's rankings depend on it. An adapter picking silently is
exactly how a wrong number acquires a correct-looking label.

**Rejected:** treating the bare date as UTC midnight. Consistent with the old `CLAUDE.md`
wording and needs no new rule, but it means a load delivered at 8pm Central on its scheduled day
is recorded late. That is wrong in the domain, not just inconvenient.

---

## D17 — The shared carrier pool: the boundary, field by field

D4 decided to build this last and ship it as a written design if it didn't get built. PRD §2 cut
the build. So this is the deliverable, and it is written to be checked against
`backend/app/repository/schema.sql` rather than agreed with.

**The shape.** A broker opts in as a whole (`pool_opt_in (broker_id PK REFERENCES brokers,
opted_in_at)`; no row means not in). For one of *its own* `ACTIVE` loads, an opted-in broker's
carrier ranking gains a labeled second section: carriers it has never used, known to other
opted-in brokers, matched across brokers on `carriers.mc_number` (D2). **The price estimate is
untouched** — it is computed from `lane_stats` percentiles, and no rate column crosses, so the
number a broker is quoted stays derived entirely from its own loads. Invariant 1 bends for the
ranking only, which is the one place the README's opt-in exception buys anything.

### What crosses

| Column | Why it is safe to share |
|---|---|
| `carriers.mc_number`, `carriers.dot_number` | Federal authority numbers, public in FMCSA licensing records. Identity is not a relationship. |
| `carriers.name`, `carriers.phone` | The carrier's own published contact details. It is what makes the answer actionable — "call them" needs a number. |
| `carriers.home_city`, `carriers.home_state` | The carrier's own fact, and coarse. Where they are based, not where their truck is now. |
| `carrier_stats.lane_key` at `tier = 'METRO'` or `'REGION'` only | A metro pair ("DFW→HOU") is a market. It does not name a facility. |
| `carrier_stats.equipment` | What the truck is. A property of the carrier's fleet, not of anyone's freight. |
| `carrier_stats.load_count`, **bucketed** — suppressed below 5, then `5–9 / 10–19 / 20–49 / 50+` | Depth of the relationship at a resolution too coarse to be a shipment count. The floor of 5 is `CLAUDE.md`'s minimum sample, reused. |
| On-time as a **band** (`≥90% / 75–89% / <75%`), derived from `carrier_stats.on_time_count / on_time_eligible_count` | Reliability, without the raw pair. D16's table is the argument for banding: `18/22` is a fingerprint that identifies one carrier under one broker. |
| Recency as a **boolean**, `carrier_stats.last_load_at > now() - 30 days` | "Still running" without a date. |

### What never crosses

| Column | What a competitor does with it |
|---|---|
| `loads.carrier_rate` | The harm D4 names. What Broker B pays this carrier; undercut B's bid, or poach the carrier at $25 more. |
| `loads.rate_per_mile` | The same number per mile, and a **generated column** — so `SELECT *` on `loads` carries it even for someone who remembered to exclude `carrier_rate`. |
| `carrier_stats.avg_rate_per_mile`, `lane_stats.rate_per_mile_p25/p50/p75` | The same number, aggregated, which is not laundering. Per D13 the three brokers' medians differ by ≥0.15 $/mi, so one leaked percentile identifies both the cost *and* whose it is. |
| `loads.customer_rate` | The other broker's sell price. With the carrier rate it is their margin; alone it is what a shipper will pay — enough to bid against them on the customer side. |
| `customers.name`, `loads.source_customer_id` | The book of business. The single most poachable asset a broker has. |
| `loads.source_load_id`, `load_number`, `stops`, `cargo`, `weight_lbs`, `distance_miles` | An individual shipment. Stops plus a date plus "frozen poultry" identifies the shipper without ever naming it. |
| `loads.pickup_zip3`, `loads.delivery_zip3` | A ZIP3 pair is roughly a facility. This is why only the METRO and REGION tiers cross. |
| `carriers.last_delivery_lat/lon/at`, `loads.delivery_actual_at`, `loads.pickup_scheduled_date` | Where a competitor's capacity physically is, this morning. It is our own deadhead signal pointed at someone else's fleet. |
| `sync_files.raw_json`, `sync_events.raw_json` | Every row above, in original form. The pool read path must not reference these two tables at all. |

### The threat model: an adversarial broker who has opted in

The outside attacker is not the interesting one. The interesting one is a broker who joined,
queries honestly-shaped questions repeatedly, and does arithmetic.

**Small numbers, and the honest answer.** Our own fixtures are the worst case. Exactly **2 of 34
carriers** appear under more than one broker, and each appears under exactly two:

```
MC 1346382  IBRAHIM TRANSPORT INC / "Ibrahim Transport, Inc."   broker_a 22 loads, broker_c  2
MC 884201   DELTA PRIME LLC       / "Delta Prime, L.L.C."       broker_b 12 loads, broker_c 11
```

If the pool published a true count of 24 for Ibrahim, broker_c subtracts its own 2 and knows
broker_a's 22 exactly. With three brokers there is no k-anonymity to have: every pooled
statistic about a shared carrier *is* one other broker's data minus your own. A "≥3 contributing
brokers" threshold would suppress 100% of this pool.

So the design does not pretend to hide the contributor. It makes the disclosure survivable
instead: **every crossing field is chosen so that perfect subtraction yields exactly what the
opt-in offered** — that the other broker runs this carrier, roughly this often, roughly this
reliably, recently. Rates, customers and shipments are not in the set, so no amount of
arithmetic reaches them. What is genuinely lost is anonymity of the source, and with three
brokers that is about one bit. Say so rather than claim a threshold that doesn't exist.

**Inference from a moving aggregate.** A count that goes 4 → 5 tells you a specific load
happened. Bucketing turns most increments invisible: a change is observable only at a band edge,
so a carrier's first 50 loads produce 4 observable transitions (at 5, 10, 20, 50) rather than 50.
It does not reduce it to zero, and the transition that *is* visible is dated by when you asked.

**Enumeration and timing.** The pool is not a directory. It answers only for a load in the
requester's own `loads` table with `status = 'ACTIVE'` — so the query space is bounded by
freight the broker actually has, not by the carrier universe. You cannot walk MC numbers looking
for a competitor's roster. Every pool read is written to an audit row keyed by
`(broker_id, source_load_id, asked_at)`, which makes repetition visible after the fact.

**Collusion.** Two opted-in brokers comparing their own pool views isolate the third's
contribution exactly. Nothing in this design prevents that, and nothing technical can.

### Enforcement — the part that is checkable

Built on the three barriers already in `broker_repository.py`, not beside them.

1. **One cross-broker reader, and it is a projection.** A view `pool_carrier_lane` over
   `carriers` and `carrier_stats`, with an explicit column list and `security_invoker = false`
   (the default), so the underlying policies are checked against the *view owner*.
   `carrier_pool_app` is granted `SELECT` on the view and nothing else. Not a filter over the
   full record: a filter is one forgotten `SELECT *` away from a leak; a projection has no rate
   column to forget.

   The owner has to be a **new** role holding `BYPASSRLS`, not the schema owner: `schema.sql`
   declares `FORCE ROW LEVEL SECURITY` on every tenant table, so even the table owner is subject
   to `broker_isolation` and an unbound read raises out of `current_broker()`. That is the single
   riskiest line of the whole feature — it mints the only credential in the system that sees
   across brokers — and it is why the projection, not the grant, is the control.
2. **RLS stays on underneath.** `loads` is never referenced by the view, so the view cannot
   reach a rate even transitively, and a caller who joins the view back to `loads` inside a
   `broker_session` gets its own rows — the policy filters the join, not the projection.
3. **A `PoolCarrier` dataclass with no money attribute.** The pool reasons are built by a scorer
   that takes `PoolCarrier`; there is no field from which a rate reason could be formatted, so
   invariant 2 holds by construction rather than by review.

What proves it:

- `set(PoolCarrier.__dataclass_fields__) == {…}` — **equality**, not a subset check, so adding a
  field to the dataclass fails the test until someone justifies it in this table.
- A catalog test: read every column the view depends on out of `pg_depend` joined to
  `pg_attribute` (`information_schema.view_column_usage` is the readable form, but it only
  reports tables the querying role owns) and assert the set is disjoint from the forbidden column
  list above. This covers columns the view *reads* and does not return — which a test against the
  JSON response would miss.
- Opt-out: broker_c leaves; broker_a's pool section loses exactly the carriers whose only other
  contributor was c, and the remaining bands recompute.
- D13's separated rate bands make a leak numerically detectable: assert no number in a pool
  payload falls inside another broker's $/mi band.
- `breaker` should attack the view's `SELECT` grant directly, the ACTIVE-load precondition (can a
  COMPLETED or another broker's load id get through?), and whether a band edge plus an own-count
  reconstructs an exact competitor count.

**Rejected:** a `shared` boolean on the existing rows, filtered at query time. It makes the RLS
policy read `broker_id = current_broker() OR shared` — the one control that enforces the
boundary becomes the control that relaxes it, and every existing query silently gains pool rows.

**Rejected:** a physical `pool_carriers` table populated at ingest with the shareable fields.
Tempting because no rate is physically present, but it is a second source of truth that drifts
from `carrier_stats` the moment a correction rebuilds a dirty key (invariant 3) — and a copy job
is exactly where "while we're here, copy avg_rate too" gets written.

**Rejected:** differential privacy on the counts. The right tool at scale and the wrong one at
n=22: noise large enough to hide a single load makes the count ±5, which is useless for deciding
who to call, and invariant 2 would then have reasons quoting a number that is not real. Bucketing
is the honest small-n version — coarse, and admittedly not a privacy guarantee.

**Rejected:** per-carrier opt-in instead of per-broker. Finer control, but the *set* a broker
chooses is itself the signal — declining to share exactly your three best carriers tells everyone
which three they are.

### What this does not do

- No k-anonymity guarantee. At three brokers it is arithmetically unavailable (above).
- Bucketing is obfuscation, not a privacy mechanism with a proof. A determined observer with
  enough queries over enough days recovers more than the bands intend to give.
- Nothing here is rate-limited or contractual. A real deployment needs the opt-in terms to
  enumerate this table, and needs the audit log to be read by someone.
- ~~**It is unbuilt.**~~ **Superseded.** This section was written as a design under D4's
  assumption that the build might not happen. It was built, in Phase 11 (S1–S6): the view, the
  `BYPASSRLS` owner, the column grants, the `PoolCarrier` field-set equality and the catalog
  assertion all exist, with 43 tests behind them, and D26 records the boundary being attacked
  live. The table above is kept as written because it is what the implementation was checked
  against, field by field.

---

## D18 — An estimate must be reproducible by hand from its own provenance line

**The problem.** Six of the sixteen day-11 loads disagreed with `data/TRACEABILITY.md` by exactly
one cent. `lane_stats.rate_per_mile_p*` is `NUMERIC(10,4)`, while the generator computed the
traceability figures at full float precision:

```
true p75   2.020017478 × 187.2 = 378.1473 → $378.15   (the doc)
stored p75 2.0200      × 187.2 = 378.1440 → $378.14   (the system)
```

**The doc is wrong, and it can be shown wrong without reference to our code.**
`TRACEABILITY.md:296` reads:

```
p75 = 2.0200 $/mi → 2.0200 × 187.2 = **$378.15**
```

`2.0200 × 187.2` is `378.144`. The document states a multiplication and then prints a different
answer — it displayed a rounded rate while computing with precision it never shows. That is an
internal contradiction, not a disagreement with the implementation.

**Decision.** Keep `NUMERIC(10,4)`. Regenerate the traceability figures from the rate the
document actually displays.

**Why the stored precision is the right one.** Invariant 6 exists so a broker can check the
answer: every estimate reports its tier, its load count, and its rate. If we printed $378.15
beside a stated rate of $2.0200/mi, a rep multiplying those two numbers would get $378.14 and
conclude our arithmetic is broken. **An estimate whose own evidence does not reproduce it is
worse than one that is a cent away from a hypothetical.** Nothing in this product compares our
dollars to an external source of truth; everything compares them to the numbers printed next
to them.

**Note on method.** Throughout this project the rule has been that when generated expectations
disagree with the implementation, the expectation wins and the code is fixed — otherwise tests
merely assert current behavior. This is the one case where the reverse applies, and it qualifies
only because the document contradicts *itself*: its stated arithmetic does not produce its
stated result. That test — can the discrepancy be demonstrated using only the artifact in
question — is what separates a genuine correction from bending expectations toward the code.

**Rejected:** widening the column to `NUMERIC(10,9)`. It would match the doc, but it makes every
estimate un-hand-checkable — nobody verifies `2.020017478 × 187.2` on a phone call — and it
would not repair the already-stored rows without a full rebuild.

**Rejected:** tolerating a cent in the Phase 9 end-to-end assertion. It hides the question
rather than answering it, and a tolerance band is exactly where a real regression later goes
unnoticed.

---

## D19 — Two scorers may disagree on rounding, never on a signal

**What happened.** `data/TRACEABILITY.md`'s ranking tables come from a *reference scorer* inside
the generator (D12); `app/domain/scoring.py` is the production one. They are deliberately
independent — that is the entire reason the document can serve as an oracle. Comparing all 192
ranking rows across the 16 day-11 loads:

```
176  agree exactly
 10  the doc computes the weighted sum from its 3dp-displayed signals; the code uses full precision
  6  differ by 0.1 at a rounding boundary — e.g. 0.15 + 0.0205 + 0.05 = 22.050000 exactly
  0  disagree on any signal value
```

**The finding is the last line.** Every `n/(n+5)`, every recency decay, every deadhead credit,
every shrunk on-time rate matched between two independently written implementations. The
divergence is entirely in how the final number is presented.

**Decision.** Fix the document, not the scorer, and pin the presentation contract:

1. The traceability table displays signal values at the precision it computes with, so its
   arithmetic reproduces — the D18 rule applied to ranking instead of pricing.
2. Both scorers round the final 0–100 score by one documented rule, stated in `PRD.md` §8.
   A score landing on exactly `.X5` must not depend on which language construct rounded it.

**What is deliberately *not* done: making the generator import `scoring.py`.** That would make
the two agree by construction and destroy the oracle. The reference scorer's value is that it
was written from the PRD independently, so a genuine formula error in either one shows up as a
disagreement. Sharing a rounding *rule* is a spec; sharing the *code* is collusion.

**Why this isn't the D18 situation.** D18 corrected the document because it contradicted itself
— its printed factors did not produce its printed result. Here both artifacts are internally
consistent; they simply made different presentation choices. So the fix is to align the
contract, not to declare one side wrong.

**Honest note.** These differences are ≤0.1 on a 0–100 scale and change no ranking order and no
top carrier. They matter because Phase 9's end-to-end check asserts against this document, and
an assertion that needs a tolerance band is an assertion that will hide a real regression later.

---

## D20 — Whose datum is missing decides whether a gap scores 0.0 or neutral

**The question.** `deadhead_credit` had no obvious answer for a missing input, and "be consistent
with `UNKNOWN` equipment, which is neutral" turned out to be the wrong generalization.

**Decision.** The rule is *whose* datum is missing.

| Gap | Credit | Why |
|---|---|---|
| The **load's** pickup can't be placed | `0.5` neutral | Nobody can be measured, so it applies to all carriers identically |
| **One carrier** has no known last delivery | `0.0` | Not rank-neutral — it is a gap in that carrier's own record |

**The arithmetic that settles it.** A gap in the *load* shifts every carrier by exactly the same
amount, so a neutral 0.5 cannot reorder anyone — while 0.0 would deflate all twelve scores by 20
points for a fact about the load, making a first-rate carrier read as mediocre. That is pure
downside, so neutral wins.

A gap in *one carrier's* record is different. On `broker_a`'s `DAY11-RICH`, six carriers score
24.17 with a deadhead credit of 0.000 and **known** positions 257–305 mi out. A carrier with no
delivery history at all would score 34.17 under a neutral 0.5 and outrank all six — **promoted
for the absence of evidence.** That is exactly the failure D5 identified in the experience
signal, reappearing inside the deadhead term. So it scores 0.0.

**Reason strings name the gap rather than implying a distance.** "No known recent delivery for
this carrier, so no proximity credit", and "This load's pickup is not on the map, so proximity
could not be measured for anyone — scored neutral". A reason that said "0 mi" or implied
remoteness would be a reason disagreeing with its own basis, which invariant 2 forbids.

**Why `UNKNOWN` equipment is not a counterexample.** It is a gap in the *load*, identical for
every carrier, so by the rule above it is neutral — which is what PRD §8 already said. The two
rules agree; the earlier framing just generalized from the wrong half of the table.

**Honest note.** No day-11 load has a geo-null pickup and all 36 carriers have a placeable last
delivery, so neither branch fires on the current fixture. Both are reachable on real data, which
is precisely why they need a stated rule rather than whatever the first `None` check happened to
return.

---

## D21 — "2-for-2 must not beat 164-for-200" was false as written; the formula is right

**What was claimed.** `CLAUDE.md`'s Known traps, PRD §8 and D5 above all asserted, without
naming a signal, that a 2-for-2 carrier must not out-rank a 164-for-200 veteran. D5 went further
and said the claim "is an on-time framing", i.e. that it holds *there specifically*.

**What is true.** With `k = 5` and a lane average `L`, the shrunk on-time rates are

```
rookie  = (2   + 5L) / (2   + 5) = (2 + 5L)/7
veteran = (164 + 5L) / (200 + 5) = (164 + 5L)/205

(2 + 5L)/7 > (164 + 5L)/205
  205(2 + 5L) > 7(164 + 5L)
  410 + 1025L > 1148 + 35L
       990L   > 738
          L   > 738/990 = 0.74545...
```

Every substantial lane in the shipped fixture sits between 0.80 and 1.00 — the real `DFW→HOU`
METRO on-time rates are 0.9565 and 0.9032 — so the condition is not hypothetical. At `L = 0.82`
the rookie shrinks to 0.8714 and the veteran to 0.8200.

**Decision: change the claim, not the formula.** Shrinkage toward the lane mean says *with
little evidence, assume average*. If a lane averages 92% and a veteran has demonstrated 82% over
200 loads, that veteran **is** below average, and a 2-load carrier whose estimate sits near the
mean reading better *on that one signal* is correct statistics. Rewriting the formula to force
the old sentence would mean punishing a carrier for having enough evidence to be measured.

**Why the trap it was written to prevent is still prevented.** Verified through the production
scorer, not by re-deriving the weights (`tests/adversarial/test_adversarial.py::
test_two_for_two_does_not_beat_164_for_200_on_the_composite_score`), holding equipment history,
truck position and last-load date equal:

```
                 experience   recency   equipment   deadhead   on-time    total
rookie  2/2        10.000      19.344     15.000      0.000      8.714     53.1
veteran 164/200    34.146      19.344     15.000      0.000      8.200     76.7
```

On-time carries 0.10, so the largest advantage it can ever hand the rookie is at `L = 1.0`:
`100 × 0.10 × (1.0000 − 0.8244) = 1.76` points. Lane experience carries 0.35 with `n/(n+5)`,
which is monotone in the count, and hands the veteran `100 × 0.35 × (0.9756 − 0.2857) = 24.15`.
**The veteran leads by at least 22.4 points on every lane average**, so no ranking a rep sees
inverts on this pair.

**Recorded rather than reworded.** The original sentence was in three documents and one module
docstring, and a decisions document that quietly edits a false claim into a true one is worth
less than one that says it was wrong. `unit-tester` had in fact already found the
prior-dependence (`TASKS.md` R2, and
`test_2_for_2_does_not_outrank_164_for_200_at_a_realistic_lane_rate`, which picks `L = 0.70` —
below the crossover — and says so in its docstring). The finding is that the *documents* were
never updated to match what the tests already knew.

**Rejected:** shrinking on-time by a count-saturating curve like experience. It makes the
sentence true, but it re-answers a question D5 already settled correctly: on-time is a rate, and
a rate with no evidence behind it must fall back to a prior, not to zero.

**Rejected:** raising `k` until the crossover exceeds 1.0. `(2 + kL)/(2 + k) > (164 + kL)/(200 + k)`
holds for *some* `L < 1` at every `k > 0`, so no constant makes the claim true; it would only
move the threshold while degrading every real cold-start estimate.

---

## D22 — A rate line is deduped on its own id, not just on its file

**The problem.** `breaker` FINDING 1. Invariant 4's idempotency key is `(broker_id, sync_file)`,
which stops a file being ingested twice — but not the same *rate line* arriving in two different
files. HaulDesk's `rates` array is append-only at the source, so an overlapping sync window, or
an operator re-pulling a day under a new filename, restates a line item already delivered.
`_rebuild_money` re-sums whole files, so the load's carrier rate silently doubled. D3 had already
claimed `source_entity_id` *"gives a second dedupe key beneath the file-level one"*; nothing
implemented it.

**Decision.** Implement the claim, in two places, because one is not enough:

1. `sync_events_rate_line_identity_idx` — `UNIQUE (broker_id, source_entity_id) WHERE
   entity_type = 'RATE_LINE'`, with `ON CONFLICT ... DO NOTHING` in `append_event`. A repeated
   `rate_id` writes no second event, enforced by the database rather than by a check a call site
   could forget. Only `RATE_LINE` is covered: `LOAD` and `CARRIER` events are restatements of
   current truth and are *supposed* to arrive many times.
2. `_rebuild_money` counts each `rate_id` once. The rebuild re-parses whole *files*, so a file
   that mixes one already-recorded line with one new line would otherwise re-add the old one
   even though its event was refused. First occurrence wins, and files are processed in filename
   order, so the surviving contribution is the one the log recorded.

**What is deliberately not deduped.** A **new** `rate_id` carrying a negative amount is TMS B's
correction mechanism (CLAUDE.md, Known traps) and must still apply — it conflicts with nothing.
A repeated `rate_id` carrying a *different* amount is treated as a duplicate, not a silent
restatement: TMS B states corrections by appending, and the raw bytes of both files remain in
`sync_files` for anyone who needs to see the discrepancy.

**Effect on the shipped fixture: none.** All 380 rate rows across the 132 files have distinct
`rate_id`s, so no stored number, no traceability figure and no ranking moves. The defect was
reachable only from data the fixture does not contain, which is exactly why it survived to
Phase 9.

**Rejected:** verifying that a re-sent file matches what was stored and raising on a mismatch.
It answers a different question (did the source change its mind?) at the cost of failing an
ingest over a duplicate that is harmless once counted once.

---

## D23 — An impossible input is not a measurement, and the sentence beside it must say so

**The pattern behind five of `breaker`'s findings.** Each was a field that arrived holding
something no measurement can hold — a delivery dated after the day we are answering for, a `NaN`
mileage, a distance of −271 mi, a booked rate of $0, a unit label we do not know — and in every
case the code carried it through the arithmetic and then printed a confident sentence beside the
result. The score was wrong in some and right in others; the *label* was wrong in all of them,
which is the failure invariant 2 and invariant 6 both exist to prevent.

**Decision. One rule, applied at the boundary each value crosses:**

| Input | Refused where | Becomes | And the sentence says |
|---|---|---|---|
| `NaN`/`Infinity` in any numeric field (FINDING 9) | `normalize.optional_float`, plus `_f` on the way back out of NUMERIC | `None` | the lane has no rate per mile, so no dollar estimate |
| Delivery dated after the ranking's as-of date (FINDING 3) | `scoring.score_carrier` | `days_since = None`, recency credit 0.0 | "…is dated 2026-07-25, after the 2026-07-06 as-of date — not a usable recency measurement" |
| Distance ≤ 0 (FINDING 5) | `pricing._usable_miles` | no dollar figure; the rate is still published | "…$/mi, but this load's distance is −271.0 mi, which cannot be priced, so no dollar estimate" |
| A published percentile ≤ 0 (FINDING 4) | `pricing._confidence` | confidence **low** | "p25 0.0000 $/mi — loads with no positive booked rate are in this pool, so confidence is held at low" |
| An unrecognized weight unit (FINDING 8) | `normalize.weight_to_lbs` | `None` for that line item | the item renders with no weight, beside the ones that have one |
| A NUL byte in an id (FINDING 7) | `broker_repository._unstorable` | `None` / `UnknownBroker` → 404 | the same 404 every other malformed id gets |
| A last delivery we cannot place (FINDING 14) | `scoring._deadhead`, fed by `unplaceable_deliveries()` | 0.0, unchanged | "Last delivered to Nowheresville, ZZ yesterday, which is not on the map…" |

**Why refusal rather than a default.** Every one of these had a plausible-looking default sitting
next to it, and each default was worse than the gap: full recency credit means a garbage future
date buys a carrier the maximum 20 points permanently; pounds for `"tons"` understates a load
2000-fold, where dropping the item understates it by one line the UI shows as blank; `not miles`
already refused zero, and a negative distance is *more* impossible than zero, not less.

**Why the sentence is half the decision.** `PriceEstimate` promises that every `None` money field
is explained by the provenance line, and `CarrierScore` promises that every reason is generated
from the value it describes. A refusal that is silent keeps the arithmetic honest and breaks the
explanation, which for this product is the deliverable. So each refusal above ships with the
wording that names it, and `_provenance`'s no-rung branch gained the words "no dollar estimate"
so that one phrase covers all four ways the dollars can be absent.

**What deliberately did not change.** The population `lane_stats` is computed from still includes
$0 loads. Excluding them there would be a second, quieter judgment — the carrier who was paid $0
would lose their `carrier_stats` row and their `$0.00/mi` rate note, and a rep would see a
ranking that no longer mentions the zero at all. The percentile is arithmetically correct; it is
the *label* on it that was lying, so the label is what moved.

**Honest limit.** None of these inputs occurs in the shipped fixture — no NaN, no non-positive
booked rate, no unknown unit, no future delivery, no unplaceable delivery. Every fix here is
therefore unexercised by the 132 files and exercised only by the adversarial suite, and no stored
number, traceability figure or ranking moved.

---

## D24 — A carrier known only from a load id is still a carrier the broker used

**The problem.** `breaker` FINDING 10. A TMS can reference a `carrier_ref` whose `carriers`
record never arrives. Adapters deliberately invent nothing from a dangling reference (D3's
failure policy, and `test_a_dangling_tms_c_reference_geo_nulls_instead_of_guessing` asserts it),
so no `carriers` row exists — but the *loads* do. They count toward `lane_stats.load_count` and
they produce `carrier_stats` rows, so the ranking was reporting "5 loads back this lane" above a
list that omitted the only carrier that ran them.

**Decision.** `list_carriers()` — the ranking's candidate pool — is the union of the `carriers`
table and the distinct `source_carrier_id`s on this broker's loads. A carrier known only by
reference comes back with its id and every other field NULL, which is exactly what "we have only
ever seen a reference" looks like; its reasons then describe the loads without claiming a name we
do not have.

**Why not create a stub `carriers` row at ingest instead.** That is inventing an entity from a
reference — the thing the adapters refuse to do, and the thing `get_carrier()` must keep
answering `None` to, because "this carrier exists in our records" is a different claim from "some
load names this id". The union keeps the two answers separate: the ranking scores them, the
carrier lookup still says we know nothing about them.

**Effect on the shipped fixture: none.** Every `carrier_ref` in all 132 files resolves to a
carrier record, so the candidate pool is unchanged and the 192 day-11 ranking rows are unchanged.

---

## D25 — A refusal has to be refused everywhere it is read, and a caveat may only claim what happened

Five findings from H4, the full review. Four are one shape each; they are recorded together
because three of them are the same mistake at three layers — a rule stated in one place and not
carried to the second place that reads the same value.

### 1. The provenance claimed a cap that never bound

`_confidence` returns **low** for `REGION`/`REGION_ANY` *before* the D15 heterogeneity cap is
evaluated, so on rung 4 the cap cannot bind. But `mix` is computed for any accepted key whose
equipment is `ANY`, which rung 4 always is — so the clause fired anyway. Live on broker_b's
day-11 load `HD-2026-005077`:

```
confidence field : low
provenance says  : ...so confidence is capped at medium
```

That is invariant 2 at the pricing layer, and it reached the screen. `PriceEstimatePanel.tsx`
deliberately refuses to name a confidence level in its own mixed-pool caveat *for exactly this
reason* — "the effective confidence can still be lower … `confidence` above is the only thing
entitled to say which" — and then renders the backend's provenance verbatim. The frontend was
careful and the backend undid it.

**Decision.** Gate the cap sentence on the **outcome**, not on the mix: append
`", so confidence is capped at medium"` only when the confidence actually is medium. Name the
mix unconditionally — that half is true at every rung, and it is the part a reader needs.

**Rejected:** dropping the whole clause on rung 4. The mix is the most important fact about a
rung-4 pool; suppressing it to avoid a wrong half-sentence trades a contradiction for a silence,
and D23 already settled that a silent refusal breaks the explanation, which is the deliverable.

**Rejected:** evaluating the D15 cap before the region rule so the sentence becomes true.
It would make rung 4 read *medium*, contradicting D6, which fixes rung 4 at low **by rule** —
repairing a caveat by corrupting the label it describes.

### 2. Rung 4's equipment word read as a claim about the pool

The same sentence named the *load's* equipment — "median of 93 loads on `TX_TRIANGLE`
(any equipment), **flatbed**" — in the position where every other rung prints the *pool's* type.
Only 4 of those 93 loads are flatbed. The mix clause repaired the impression, but only
incidentally, and `price_estimate` can be called without a mix.

**Decision.** Rung 4 says **"for a flatbed load"**, and "for a load of unknown equipment" when
the load's own equipment is `UNKNOWN`. Same value, same source; the grammar now carries which
side of the comparison it belongs to. This is deliberately consistent with fix 1: the clause that
states the pool composition stays, so neither sentence depends on the other to be true.

### 3. A negative distance was refused as a subject and accepted as evidence

D23 refuses `distance ≤ 0` for the load being *priced*, because a negative mileage is an
impossible measurement rather than a small one. The same load as **evidence** was unfiltered:
`_population` filtered on `rate_per_mile IS NOT NULL` and nothing about sign, and `schema.sql`'s
generated column nulls a distance of exactly zero (`NULLIF`) but not a negative one. Flipping
broker_a's `127400898` to −250.30 mi:

```
p25/p50/p75                       1.7500/1.7800/1.8050  ->  1.7425/1.7650/1.8000
carrier 834323 avg_rate_per_mile  1.8500                ->  -1.8500
```

All three percentiles stay positive, so D23's `_non_positive_rates` guard never fires and the
label stays **medium**. And `scoring._rate_note` prints **"Averages $-1.85/mi"** inside that
carrier's reasons — a reason built correctly from a number that should never have existed.

**Decision.** `_population` gains `AND distance_miles > 0 AND rate_per_mile >= 0`.

**`>= 0` and not `> 0`, deliberately.** D23 decided to keep $0 booked loads in the population and
move the *label* instead ("the percentile is arithmetically correct; it is the label on it that
was lying"). Tightening to `> 0` here would quietly re-decide that in the opposite direction, and
the carrier paid $0 would vanish from the ranking rather than being reported.

**Two predicates, not one.** `rate_per_mile` is `carrier_rate / NULLIF(distance_miles, 0)`, and
two negatives divide to a positive — a −$463 rate over −250 mi publishes a perfectly plausible
$1.85/mi. A non-negative rate does not imply a usable distance, so both are stated.

**What this changes for the refused load.** It leaves the population entirely, so the lane's
count drops (12 → 11 in the reproduction above) and the percentiles move with it. That is the
intended reading: a load with an impossible distance has no $/mi to contribute, exactly as a load
with no carrier rate has none.

**Effect on the shipped fixture: none.** No load in the 132 files has a non-positive distance;
this is reachable only from data the fixture does not contain.

### 4. The rate-line dedupe key was narrower than the grain it dedupes at

D22 wrote `UNIQUE (broker_id, source_entity_id) WHERE entity_type = 'RATE_LINE'`, while
`pipeline._rebuild_money` counts each `rate_id` once **per load**. A TMS that numbers rate ids per
load rather than globally — an ordinary convention — then loses every load after the first in a
file:

```
HD-1 carrier_rate 700.00 | HD-2 carrier_rate NULL
RATE_LINE events written: 1
```

Fragility rather than a wrong shipped answer: all 380 rate ids in the fixture are distinct.

**Decision.** `sync_events_rate_line_load_identity_idx` —
`UNIQUE (broker_id, source_load_id, source_entity_id) WHERE entity_type = 'RATE_LINE'`, with the
`ON CONFLICT` target matched, and the superseded index dropped by `schema.sql` so a database
created before this is brought into step on the next start (D8) rather than silently keeping the
stricter key.

**It does not eat D22's case.** D22 refuses a line item *restated in a later file*, which is the
same load's line item by definition — same `source_load_id`, same `rate_id`, still refused. Both
D22 regression tests pass unchanged, including the hard one where a file mixes a restated line
with a genuine negative `ADJUSTMENT` and the answer must be `700 − 120 = 580`. All three
correction flavours were re-verified against the replay-equivalence suite after the change.

### 5. `discovery.py` documented one rule and implemented two

The docstring said a filename that does not match the pattern **raises**; the code skips anything
not ending `.json` and raises only past that gate.

**Decision.** Correct the docstring; the behaviour is right. All three shipped TMS directories
hold the assignment's annotated `example_sync.jsonc` beside the real syncs, so a rule that raised
on every non-matching name would fail `docker compose up` from a clean checkout. The extension is
the "is this data" test; the filename pattern is the "is this data well-formed" test, and there
raising is correct — a `.json` file with no place in the chronological order is data we would be
silently dropping (invariant 4). Both halves are now asserted, because the doc previously
described only one of them.

### Also in this pass, and one thing deliberately not done

`PRD.md` §5's table sketch had drifted from `schema.sql`: it listed `last_delivery_lat/lon/at`
under `carrier_stats`, where D17 and the schema comment both explain they must **not** be — one
copy per lane row could disagree with itself after a partial rebuild — and it omitted
`on_time_eligible_count`, `equipment` and `first_load_at` from `carrier_stats`, `first_load_at`
from `lane_stats`, and the generated `rate_per_mile` and stored `delivered_on_time` from `loads`.
Brought in line, with the two reasons that are load-bearing stated inline.

**The review's first documentation finding was rejected.** It reported that `CLAUDE.md`'s
cold-start trap still carried the pre-D21 one-line claim. It does not: the qualification runs to
the end of that bullet and closes with "The original one-line version of this trap was **false as
written** — see `DECISIONS.md` D21." `CLAUDE.md` is correct, and `scoring.py`'s past-tense
reference to it is therefore correct too. Both left alone. Recorded because "the reviewer said so"
is not a reason to edit a document that is already right, and a rejected finding is worth as much
in this file as an accepted one.

---

## D26 — The pool boundary, attacked (S6)

`breaker` was cut short by a spend limit, so I ran S6 myself. Six attacks against a live
three-broker pool with everyone opted in.

**Failed to break anything:**

- **Reading money through the pool role.** Holding `carrier_pool_reader` directly:
  `SELECT carrier_rate FROM loads` → *permission denied for table loads*;
  `SELECT avg_rate_per_mile FROM carrier_stats` → *permission denied*;
  `SELECT *` from `carrier_stats` → *permission denied*, so the forgotten-star case fails closed
  rather than widening. `loads` is not granted to that role at all.
- **Reaching the view with the pool role alone.** `SELECT … FROM pool_carrier_lane` still raises
  *"no broker bound"*. The pool sits **on top of** invariant 1, not beside it: the pool role and
  a bound broker are both required, and neither suffices.
- **Finding an exact number in the payload.** Every field is banded or categorical —
  `load_band`, `on_time_band`, `active_recently`, `contributor_count`, `equipment_operated`. No
  count, no ratio, no date, no load id.
- **Self-exclusion.** Own-MC ∩ pool-MC is empty for all three brokers (12/13, 12/13, 12/12).
- **Pinning a banded value.** MC 812445 on `METRO DFW→HOU DRY_VAN` is truly `broker_b, 12 loads,
  12/12 on-time`. Published: `10-19` and `90+`. Ten candidates for the count, and a perfect
  record indistinguishable from 90%.

**One finding, and it is about source anonymity rather than money.** D17 says a contributor's
identity is worth "about one bit" at three brokers. It can be resolved to certainty by
observation. Counting `broker_a`'s pool rows while `broker_b`'s opt-in changes:

```
all three opted in           45 rows
broker_b opted out           24 rows
broker_b opted back in       45 rows
```

So 21 rows are attributable to `broker_b` exactly. A broker cannot toggle another's flag — only
`broker_b` controls `broker_b` — so this is passive observation across a change the contributor
made themselves, not an active attack. What it yields is *which* broker runs a carrier, never
*how often* or *at what price*: the bands do not move. Still, "about one bit" understates it,
and the honest statement is that **opting out is itself disclosive to anyone watching.**

**Not fixed.** Suppressing it would mean withholding pool data for a period after any opt-in
change, which trades a real feature against an attacker who must already be an opted-in
competitor watching a specific carrier across a specific window. Recorded instead, and D17's
"about one bit" is corrected by this entry rather than left to stand.

---

## Honest limitations

Everything above records a decision. This records what those decisions cost, what is broken and
still broken, and where the evidence for the rest of this file runs out.

### What `breaker` attacked and could not break

`backend/tests/adversarial/` holds 88 tests. **63** sit under the file's own heading *attacks
that failed to break anything*; 19 are the regression tests the 14 defects left behind, four of
which characterise a finding rather than assert a fix; 3 guard the harness against redirecting
another suite's connections; 3 are the concurrency file. The 85 in `test_adversarial.py` pass
in 8.4 s. Grouped by the claim each set supports, and by what that claim does not reach:

**Tenant isolation holds against a query, not against a caller.** Eleven raw statements —
`SELECT count(*) FROM loads`, `SET ROLE carrier`, `ALTER ROLE carrier_pool_app BYPASSRLS`,
`DROP POLICY broker_isolation ON loads`, redefining `current_broker()` to return `'broker_b'`,
`DELETE FROM sync_events` — each raises `InsufficientPrivilege`, and an unbound connection
fails closed rather than open. With broker B loaded on the same lane key, sharing A's carrier
MC/DOT, A's literal `source_load_id` strings and A's customer name while paying 13× a load,
every one of A's percentiles and every carrier's `score_exact` is identical before and after B
exists; the colliding id resolves to a different load under each tenant; the disjointness holds
through `count` and `sum`, where the owner connection sees all ten loads and neither binding
saw more than its own. Another broker's load id is a 404 whose body is byte-comparable with a
nonexistent id's.

*What it does not cover.* All of this is enforcement against a query that forgot its broker or
a role that tried to widen itself. `broker_id` arrives as an unauthenticated query parameter,
so nothing here establishes *who is asking* (below). The pool read path is covered separately,
by S5's 43 leak tests and D26's attacks, not by this suite.

**A correction lands the same wherever it lands.** `700 → 900 → 1100 → 700` as TMS A
restatements restores every derived row bit-identically — percentiles, carrier averages,
on-time counts, truck positions. So does a TMS B `−200 / +200` adjustment chain, including the
rate-only file whose `loads` array never names the load. A correction that drops a rung below
the 5-load minimum makes the walk re-reject it instead of serving it stale; a correction that
moves a load to another lane leaves no stale row on the lane it left; ten re-ingests with three
brokers interleaved change nothing.

*What it does not cover.* All of it replays the file *shapes the fixture contains*. A TMS
restating history in a shape none of the three uses is untested by construction — D22 and D25.4
were both exactly that, found by reasoning about the schemas rather than by running the corpus.

**Impossible inputs degrade to a null and a sentence.** Unicode, empty strings and nulls in
every string field; a blank carrier id; an unresolvable address; a rates row with a blank
`code`; `NaN` mileage; −271 mi; a $0 booked rate; a 20-**ton** line item; a NUL byte in an id;
a 1-stop and a 12-stop load; a dangling carrier reference and a dangling location reference;
seven equipment strings that must not become `DRY_VAN`; a 4,000-character load id; and
injection-shaped load ids, broker ids and status values (404 or 422, never a 500, never SQL).
Nothing is invented from any of them, and D23's rule means each refusal ships with the wording
that names it.

*What it does not cover.* Hand-picked shapes. Nothing here is generated, so the coverage is
"what we thought of" — there is no property-based layer over the adapters.

**Statistical nonsense does not move an answer.** A $125/mi outlier among five normal loads
leaves the median under $2/mi; twelve identical rates report a zero-width range honestly at
medium; a carrier whose only rate is $0 does not top a ranking, and its reason quotes the same
$0 the average was built from; an empty lane answers "no estimate", never `$0`; the deadhead
curve is bounded and monotone at 0 / 50 / 150 / 250 / 1e9 / `None`; 2-for-2 loses to
164-for-200 on the composite by more than 22 points at every lane average (D21).

*What it does not cover.* No attack here changed a **rank order** in the fixture — every day-11
scenario was built with a ≥5-point margin (D12), which is a property of the data we generated
as much as of the scorer.

**Reasons cannot drift from scores.** Checked structurally rather than by string matching: the
published score equals the sum of the signal contributions, every reason is its own signal's
sentence, the experience sentence quotes the count that signal scored, and the on-time sentence
quotes its own numerator and denominator. Zero divergences across all 192 day-11 rows.

*What it does not cover.* It proves a sentence was generated from the number beside it. It does
not prove the sentence is a fair *description* of that number — both H4 findings in D25.1 and
D25.2 were grammatical (a caveat claiming a cap that had not bound; an equipment word on the
wrong side of a comparison), and no structural check catches those.

**A survived attack is evidence, not proof.** The same method that produced those 63 produced
14 defects, three of which reached a screen or would have. The honest reading is that this
suite has stopped finding things, not that there is nothing left to find.

### Known-broken, and unfixed

**Dead code in `_provenance` (`pricing.py:494`).** The no-rung branch ends
`tried or "nothing — lane ends not on the map"`. That fallback cannot fire: the `REGION` and
`REGION_ANY` keys are the constant `TX_TRIANGLE→TX_TRIANGLE` and do not depend on either lane
end resolving, so a walk always reports at least two non-skipped rungs and `tried` is never
empty. Verified by running a fully geo-null load through `estimate_price`: it reads
`tried REGION 0, REGION_ANY 0`. Confirmed still present after H5. It produces no wrong answer,
which is a reason not to hurry, not a reason it is fine.

**The integration suite truncates a shared database.** Its `clean_db` fixture truncates all
seven tenant tables before and after every test on one fixed database, so two pytest
invocations against that database delete each other's rows mid-test. The adversarial suite fixed
this for itself — a database per pid, swept on exit — after the failure produced `2 passed,
66 errors` against `68 passed`; the integration suite did not. The three suites are therefore
run as three separate invocations. It is a harness defect of exactly the kind this project
treats as real when it appears in product code.

**And it is worse than "two invocations collide" — there are two mechanisms, measured
separately.**

*Concurrency amplifies it.* Sampling `pgrep -f "m pytest"` during three back-to-back runs:

| peak concurrent pytest processes | result |
|---|---|
| 2 | **454 passed** |
| 7 | 14 failed, 409 passed, 34 errors |
| 8 | 9 failed, 410 passed, 38 errors |

The lock graph confirms it — `Process 2483 waits for AccessExclusiveLock on relation 16394;
blocked by process 2606. Process 2606 waits for RowExclusiveLock on relation 16411; blocked by
2483` — a `TRUNCATE` against another run's in-flight ingest, every party connecting as
`carrier_pool_app` from the host. The same suite against a *private* database passes 454
deterministically.

**A third symptom, and one observation I could not fully explain.** The same collision also
takes down the *application*: at `00:08:07`, with the integration suite running against
`carrier_pool` while the backend booted, the lifespan ingest died on
`psycopg.errors.UniqueViolation: duplicate key value violates unique constraint
"lane_stats_broker_id_tier_origin_key_dest_key_equipment_key"` and uvicorn reported
`Application startup failed. Exiting.` So a reviewer who runs the test suite while the stack is
up can lose the backend, not merely the data.

Separately, and left unresolved: one boot logged `ingest complete: 132 files … 132 files
ingested` and `startup complete`, and ~70 seconds later `loads`, `sync_files` and `lane_stats`
were all `0`, with no pytest process running and no connection to that database. A plain restart
produced a correct, populated database (295 / 132 / 445), stable across two further restarts. It
is recorded here without a diagnosis because I could not reproduce it deliberately, and a
guessed cause in this file would be worth less than an honest gap.

*But concurrency is not the whole cause.* Three consecutive invocations of the identical
command, verified beforehand at **zero** running pytest processes and with the backend container
stopped:

```
run 1:  3 failed, 447 passed, 4 errors
run 2:  454 passed
run 3:  397 passed, 57 errors      <- the entire integration suite failed at setup
```

Back-to-back sequential runs still diverge, so state or connections outliving a run affect the
next one. An earlier measurement of the integration suite alone gave `33 passed, 24 errors` then
`57 passed` twice.

The failures are not all errors. Alongside `DeadlockDetected` and a `ForeignKeyViolation` on
`sync_events_sync_file_id_fkey`, two runs produced **wrong-value assertions** — `assert 9 == 12`
on a lane's load count, and a top carrier of `IRON HORSE FLATBED CO` where `ALAMO CHILL
TRANSPORT` was expected. That is the dangerous shape: an error is obviously infrastructure, a
wrong number looks like a product regression. Anyone bisecting a real defect against this suite
can be sent somewhere false by a run that had nothing to do with their change.

**A running backend breaks it too**, which matters because it is the reviewer's default path:
`docker compose up`, then run the tests, gives `13 failed, 412 passed, 29 errors` with
`DeadlockDetected` — the suite truncates `carrier_pool` while the live container reads it.
`docker compose stop backend` clears it, and `RUNNING.md` §6 says so before the invocation
rather than after.

**Why it is documented rather than fixed.** The fix is the one the adversarial suite already
demonstrates — a database per pytest session, created and swept by the fixture, so no suite
shares mutable state with any other or with a running app. That is maybe an hour, and it is the
first thing to do next. Doing it at the end of a long session, on the harness that certifies
everything else, is how a green suite starts certifying nothing. The product code is unaffected:
every defect above is in the test fixtures, and the 88-test adversarial suite and the 287 unit
tests are deterministic across repeated runs.

**Four findings characterised and left unmarked.**

- **FINDING 11.** `broker_session` nested inside an outer transaction becomes a savepoint, and
  releasing a savepoint *keeps* its `SET LOCAL` values. So raw SQL issued on that connection
  after the block, outside any binding, runs as the last-bound broker instead of failing closed
  — the third barrier the repository docstring relies on. Not reachable in current code: a
  repository is per-file and per-broker, so the surviving binding is always the one just used.
  The barrier's real strength is therefore "nothing in this codebase does that", not "it cannot
  be done".
- **FINDING 12.** `/recommendations` and `/price-estimate` are separate requests that each run
  their own tier walk. An ingest landing between them — `POST /api/admin/ingest` is live —
  leaves the load-detail screen quoting a ZIP3 ranking beside a METRO estimate. Mitigated by D8
  (ingestion finishes before serving) and by nothing else.
- **FINDING 13.** An unplaceable pickup shifts every carrier on that load by exactly **+10.00
  points**, because D20's neutral `0.5` replaces a `0.0` each of them had earned. Rank-neutral
  within one ranking, which is what D20 argues and all D20 claims; not comparable *across*
  loads, and nothing on the screen normalises for it. A rep comparing two loads sees the
  worse-documented one's carriers look uniformly better.
- **FINDING 15.** `bootstrap()` is not concurrency-safe. `schema.sql` ends in cluster-wide role
  DDL, and `pg_authid` / `pg_auth_members` are **shared** catalogs, so two API instances
  starting against one Postgres cluster update the same role tuple and one dies with
  `tuple concurrently updated`. Because D8 makes the lifespan fail loudly, that container does
  not start. Reachable by `docker compose up --scale api=2` or a rolling restart; a separate
  database per instance is not the mitigation.

**D10's mileage, and its one-directional deadhead bias.** Haversine × 1.2 overstates straight
corridors by ~13% — Dallas→Houston 271.0 mi against a real ~239. Every ratio cancels the factor;
the deadhead thresholds are *constants* and do not. Across all 192 (carrier, day-11 load) pairs,
85 (44%) would change credit by more than 0.02 at a true road factor of 1.06, and 45 cross a
zero- or full-credit boundary. No day-11 answer changes, but the policy the code applies is
still not the policy PRD §8 states, and it is always the stricter one.

**D17's k-anonymity.** Arithmetically unavailable at three brokers: every pooled statistic about
a shared carrier is one other broker's data minus your own, and only 2 of 34 carriers are shared
at all. Bucketing is obfuscation without a proof. The design says that rather than claiming a
threshold it cannot enforce.

### What the design does not do

- **No authentication.** `broker_id` is a query parameter. Anything that can reach the API can
  name any broker, and the isolation above is enforcement against a query that forgot its
  tenant, not against a caller who lies about theirs. In a deployment the value passed to
  `broker_session` comes from an authenticated session; here it comes from the URL. This is the
  largest gap between this and something you could run.
- **The shared pool discloses its contributors.** It was built (S1–S6), and money provably does
  not cross — but D26 shows that watching a broker opt out attributes its rows exactly, and at
  three brokers there is no k-anonymity to be had. What crosses is bounded and audited, not
  anonymous.
- **No road routing.** Invariant 7 forbids network at runtime; distance is Haversine × 1.2 over
  a 180-row hardcoded table. D10 is the bill for that.
- **One machine.** One Postgres, one process, one connection, and ingestion is a `for` loop over
  132 files.

**What breaks at millions of loads.** The README asks directly. The answer is the rebuild, and
the bottleneck is the region rungs:

- One touched load dirties **8 lane keys** — 4 tiers × {its own equipment, `ANY`}
  (`keys_for_load`) — and `_rebuild_lane_key` handles each with `DELETE` + recompute from raw
  `loads`, **inside the ingest transaction**.
- Three of those 8 are `REGION`/`REGION_ANY`, whose key is the constant
  `TX_TRIANGLE→TX_TRIANGLE`. That bucket is *the broker's entire placeable history*. So every
  file ingested re-runs three `percentile_cont` sorts over every load the broker has ever had,
  and `compute_carrier_stats` re-derives one row per carrier on those keys.
- Cost per file is therefore **O(the broker's total loads), not O(loads changed)**. At 132 files
  and ~93 loads a broker that is a few seconds inside `docker compose up` (D8). At 10⁶ loads it
  is a full-history sort per sync, in a transaction that also holds the write lock on
  `lane_stats` — ingestion serialises against itself and tail latency grows with history size,
  which is exactly backwards.
- **The fix is not delta-patching.** Invariant 3 is what buys "a late correction produces the
  same numbers as if it had arrived on time", proved exactly by I10, and that is the
  assignment's central question. The order this wants doing: (1) move the rebuild out of the
  ingest transaction into an idempotent job keyed on the dirty key, so ingestion only *records*
  dirt — the tier walk then reads a possibly-stale row and has to say so, which is a change to
  invariant 6's provenance line and not merely to a scheduler; (2) shard that queue by broker,
  which invariant 4 permits because chronological order only has to hold within one broker;
  (3) replace exact `percentile_cont` on the region rungs with a mergeable sketch. Only (3)
  changes a published number, and it changes it at the two rungs D6 already fixes at **low**
  confidence.

### What I'd do next, in this order

1. **Authentication, with `broker_id` derived from the session rather than the URL.** Everything
   else on this list improves an answer; this one is the difference between a boundary that
   holds against a bug and one that holds against a person. It is also the cheapest — the
   binding point already exists and is already enforced, only its input is untrusted.
2. **Move the derived-stats rebuild off the ingest transaction.** Named above. It is the first
   thing that fails at scale, and it is structural, so it wants doing before more code is
   written that assumes the rebuild is synchronous.
3. **Read the `pool_audit` log.** It records every pool read append-only, and D17 is explicit
   that a question asked two hundred times about one load is the shape of an inference attack.
   Nothing reads it. Recovering the pattern after the fact is worth less than noticing it, and
   the rows are already there.
4. **A property-based layer over the three adapters.** The 63 failed attacks are shapes someone
   thought of; the two rate-line dedupe defects (D22, D25.4) were found by reading schemas, not
   by running data. Fourth because what it is most likely to surface are defects that never
   change a shipped answer — which is also why it is worth doing before the corpus grows.
5. **Split the D15 estimate per equipment type.** D15 deferred it pending a UI that can present
   the distinction without inventing one the broker has not made. It removes the one estimate in
   the fixture whose p75 sits in an interval no carrier has ever been paid in.
6. **One walk behind both endpoints (FINDING 12).** Narrow, currently masked by D8, and the
   cheapest of the four unfixed findings — but it is a product-level contradiction on the
   screen, which is the class of bug this project treats as worst.

### The verification method, and where it stops

This project leaned on an independent oracle. `data/TRACEABILITY.md` was computed by a reference
scorer inside the generator, written from `PRD.md` **before any production code existed**;
`backend/scripts/generate_data.py` imports `app.domain.distance` and `app.domain.geo` and has
never imported `app/domain/scoring.py`. D19 states why: sharing a rounding *rule* is a spec,
sharing the *code* is collusion, and two scorers that agree by construction cannot disagree when
one is wrong.

It earned that. The two scorers agreed on all 192 ranking rows and on **every** signal value —
`n/(n+5)`, each recency decay, each deadhead credit, each shrunk on-time rate — differing only
in presentation. And the document was the authoritative artifact three separate times: D19's
rounding and H5's confidence clause, where the doc was right and the code moved; and D18's
pricing arithmetic, where the *document* was wrong and lost, but only because its own printed
factors did not produce its own printed result. That last one is the important one: the oracle
is not simply believed. The test that separates a correction from bending expectations toward
the code is whether the discrepancy can be shown using only the artifact in question.

**What it does not buy.** An oracle catches a formula written two ways. It cannot catch a
misreading held once and implemented twice — two implementations of the same wrong reading of
the PRD agree with each other and are both wrong. That is precisely FINDING 2. `CLAUDE.md`'s
Known traps, `PRD.md` §8 and D5 all asserted "2-for-2 must not beat 164-for-200" without naming
a signal; it is false on the shrunk on-time rate for every lane average above `738/990`, which
is where every substantial lane in the fixture sits; and the traceability table could never have
caught it, because the table was generated from the same sentence. The one test that came near
it — `test_2_for_2_does_not_outrank_164_for_200_at_a_realistic_lane_rate` — picked `L = 0.70`,
below the crossover, said so in its docstring, and passed from Phase 6 to Phase 9 while three
documents stayed wrong.

Two further limits on all of the above. The oracle checks **numbers, not sentences**: every
prose claim in this system is checked only where someone thought to write a test for it, and
three of D25's five findings were sentences rather than arithmetic. And its authority stops at
the corpus — "effect
on the shipped fixture: none" appears three times in this file, and D23's honest limit says the
same of seven refusals at once. Those fixes are exercised only by the adversarial suite, on data
we invented for the purpose, which is the weakest evidence in this document and is labelled as
such where it appears.
