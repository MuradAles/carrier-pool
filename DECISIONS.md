# DECISIONS

Judgment calls, the alternatives rejected, and honest limits. Written as decisions are made,
not reconstructed afterwards.

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

**Why this keeps the invariant.** `CLAUDE.md`'s "2-for-2 must not beat 164-for-200" is an
on-time framing, and on-time still uses shrinkage. Under the saturating curve, 2 loads (0.29)
also cannot outscore 200 loads (0.98) on experience. Both readings hold.

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
- **It is unbuilt.** The enforcement above is a design, not a passing test. The three barriers it
  builds on are real and tested; the view, the dataclass and the catalog assertion are not.

---

## Honest limitations

*To be filled as they're found — including what `breaker` attacked and could not break.*
