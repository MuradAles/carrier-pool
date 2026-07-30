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

## Honest limitations

*To be filled as they're found — including what `breaker` attacked and could not break.*
