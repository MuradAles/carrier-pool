"""Chronological, one-file-at-a-time ingestion (TASKS.md I2-I8).

The shape of one file's work, in order:

1. **Claim the file.** ``sync_files`` gets one row holding the raw bytes. The
   ``UNIQUE (broker_id, sync_file)`` means a second attempt inserts nothing, and
   ingestion stops right there — so re-ingesting is a no-op enforced by the
   database rather than by a check somebody could forget (invariant 4). Nothing
   below this line runs twice for the same file, which is what stops TMS B's
   rate line items from being counted twice.
2. **Append the events.** One ``sync_events`` row per changed entity, in the
   adapter's order, its index as ``event_seq`` (D3). Append-only is a privilege,
   not a convention: the app role holds no UPDATE or DELETE on this table.
3. **Overwrite current truth.** Later syncs win, wholesale, for loads, carriers
   and customers.
4. **Re-sum the money.** For every load this file mentions *in any form*, the
   carrier and customer totals are recomputed from every rate-line event ever
   recorded for it — see :func:`_rebuild_money`.
5. **Rebuild what changed.** The keys the file dirtied are recomputed from
   scratch; the carriers it touched get their last known delivery position
   recomputed from scratch.

**The one design point worth arguing about.** Invariant 3 says derived stats are
rebuilt from ``sync_events``, never patched. The chain here is
``sync_events -> loads -> lane_stats/carrier_stats``, and every link is a full
recompute of what it derives: ``loads`` is overwritten wholesale from the newest
event and its TMS B money re-summed from *all* rate-line events, then the stats
are aggregated from ``loads``. No step adds a delta to a number it did not
derive, so a correction arriving late produces the same figures as one arriving
on time — which is the property the invariant exists for. Re-deriving every load
on a lane from the log at rebuild time would be the literal reading, and it is
the alternative D3 rejects: it re-parses every file that ever mentioned any of
those loads, and the dirty-key optimization stops meaning anything.

**The trap this is built around.** A TMS B sync can append a rate line for a load
whose ``loads`` array does not mention it (CLAUDE.md, Known traps —
``HD-2026-004733`` receives −120 in ``2026-07-12T06-00_sync.json``, whose
``loads`` array holds only 004817/004821/004832). There is no load object to
hang the change on, so every step above works from *load ids mentioned by any
record*, not from the file's loads: the rate line is an event, the load's money
is re-summed, its lane keys go dirty, and the medians move. Nothing here special
cases it, because a special case is a thing that can be forgotten.

**Failure policy.** One file is one transaction. A file that raises leaves no
trace — no ``sync_files`` row, so it is retried on the next run — and the
exception propagates rather than being swallowed: a half-ingested history
produces wrong answers, which is worse than no answers.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import psycopg

from ..adapters import AdaptedSync, TmsAdapter, adapter_for
from ..domain.lanes import LaneKey, keys_for_load
from ..domain.model import EntityType, Load, RateSide
from ..repository import BrokerRepository, list_brokers
from .discovery import DiscoveredSync, discover_sync_files

__all__ = [
    "DEFAULT_DATA_ROOT",
    "FileOutcome",
    "IngestionReport",
    "ingest_all",
    "ingest_sync_file",
]

log = logging.getLogger(__name__)

#: Where the three TMS directories live. Overridable for tests and for the
#: container, whose working directory differs from a developer's.
DEFAULT_DATA_ROOT = Path(os.environ.get("DATA_DIR", "../data"))


@dataclass(frozen=True, slots=True)
class FileOutcome:
    """What one file did. ``ingested=False`` means it was already in the log."""

    broker_id: str
    sync_file: str
    ingested: bool
    events_written: int = 0
    loads_touched: int = 0
    lane_keys_rebuilt: int = 0
    carriers_repositioned: int = 0


@dataclass(slots=True)
class IngestionReport:
    """Totals for a whole run. Printed at startup so a container says what it did."""

    files_discovered: int = 0
    files_ingested: int = 0
    files_skipped: int = 0
    events_written: int = 0
    loads_touched: int = 0
    lane_keys_rebuilt: int = 0
    carriers_repositioned: int = 0
    outcomes: list[FileOutcome] = field(default_factory=list)

    def record(self, outcome: FileOutcome) -> None:
        self.files_discovered += 1
        if outcome.ingested:
            self.files_ingested += 1
        else:
            self.files_skipped += 1
        self.events_written += outcome.events_written
        self.loads_touched += outcome.loads_touched
        self.lane_keys_rebuilt += outcome.lane_keys_rebuilt
        self.carriers_repositioned += outcome.carriers_repositioned
        self.outcomes.append(outcome)

    def summary(self) -> str:
        return (
            f"{self.files_ingested} files ingested, {self.files_skipped} already "
            f"present; {self.events_written} events, {self.loads_touched} load "
            f"touches, {self.lane_keys_rebuilt} lane-key rebuilds, "
            f"{self.carriers_repositioned} carrier position rebuilds"
        )


def ingest_all(
    conn: psycopg.Connection, data_root: Path | str = DEFAULT_DATA_ROOT
) -> IngestionReport:
    """Ingest every sync file, in filename order, one at a time.

    Idempotent by construction: files already in ``sync_files`` are skipped, so a
    second run over the same data changes no row and no total. That is what makes
    it safe to run on every container start (D8) and what makes the manual replay
    endpoint harmless.
    """
    brokers = list_brokers(conn)
    report = IngestionReport()
    for discovered in discover_sync_files(Path(data_root), brokers):
        report.record(ingest_sync_file(conn, discovered))
    log.info("ingestion complete: %s", report.summary())
    return report


def ingest_sync_file(conn: psycopg.Connection, discovered: DiscoveredSync) -> FileOutcome:
    """Ingest exactly one file, in one transaction. See the module docstring."""
    adapter = adapter_for(discovered.broker.tms_type)
    payload = json.loads(discovered.path.read_text(encoding="utf-8"))
    adapted = adapter.adapt(discovered.sync_file, payload)
    repo = BrokerRepository(conn, discovered.broker.id)

    with conn.transaction():
        sync_file_id = repo.record_sync_file(
            discovered.sync_file, adapted.synced_at, payload
        )
        if sync_file_id is None:
            # Already ingested. Not an error, and deliberately not a "verify it
            # matches" path either: the file's bytes are already in sync_files,
            # and re-applying them is what would double-count TMS B's money.
            log.debug("%s %s already ingested", discovered.broker.id, discovered.sync_file)
            return FileOutcome(discovered.broker.id, discovered.sync_file, ingested=False)

        touched = _touched_load_ids(adapted)
        # Read before writing: a correction can move a load to another lane or
        # change its equipment, and the lane it *left* is just as stale as the
        # one it joined. Dirtying only the new keys would leave the old bucket
        # quietly counting a load that is no longer in it.
        before = {load_id: repo.get_load(load_id) for load_id in touched}

        events = _append_events(repo, sync_file_id, adapted)
        _upsert_entities(repo, adapted)
        for load_id in touched:
            _rebuild_money(repo, adapter, load_id)

        after = {load_id: repo.get_load(load_id) for load_id in touched}
        dirty = _dirty_keys(before, after)
        carriers = _affected_carriers(adapted, before, after)

        for key in sorted(
            dirty, key=lambda k: (k.tier, k.origin_key, k.dest_key, k.equipment)
        ):
            _rebuild_lane_key(repo, key)
        for carrier_id in sorted(carriers):
            _rebuild_last_delivery(repo, carrier_id)

    return FileOutcome(
        broker_id=discovered.broker.id,
        sync_file=discovered.sync_file,
        ingested=True,
        events_written=events,
        loads_touched=len(touched),
        lane_keys_rebuilt=len(dirty),
        carriers_repositioned=len(carriers),
    )


# ---------------------------------------------------------------------------
# The steps
# ---------------------------------------------------------------------------


def _touched_load_ids(adapted: AdaptedSync) -> tuple[str, ...]:
    """Every load this file says anything about, in first-mention order.

    Reads ``source_load_id`` off the records rather than the file's loads, so a
    load that appears only as a rate line is touched exactly like one that
    arrived as a load object. That single line is what makes the rate-only
    correction work.
    """
    seen: dict[str, None] = {}
    for record in adapted.records:
        if record.source_load_id is not None:
            seen.setdefault(record.source_load_id, None)
    return tuple(seen)


def _append_events(
    repo: BrokerRepository, sync_file_id: int, adapted: AdaptedSync
) -> int:
    """Append one event per changed entity. ``event_seq`` is the record's index.

    The adapter already ordered them — carriers and customers, then loads, then
    rate lines — so position in ``records`` is the intra-file order, and
    ``(synced_at, event_seq)`` is a total order over the whole log.
    """
    for event_seq, record in enumerate(adapted.records):
        repo.append_event(
            sync_file_id=sync_file_id,
            synced_at=adapted.synced_at,
            entity_type=record.entity_type,
            source_entity_id=record.source_entity_id,
            source_load_id=record.source_load_id,
            raw_json=record.raw,
            event_seq=event_seq,
        )
    return len(adapted.records)


def _upsert_entities(repo: BrokerRepository, adapted: AdaptedSync) -> None:
    """Overwrite current truth with this file's version of it (I3).

    Rate lines write nothing here: they are not current truth about anything,
    they are contributions to a total that :func:`_rebuild_money` re-sums from
    the log. ``last_seen_sync_at`` is the file's own envelope timestamp, so a
    load records when it was last *described*, not when we happened to ingest it.
    """
    for record in adapted.records:
        entity = record.entity
        if record.entity_type is EntityType.CARRIER:
            repo.upsert_carrier(entity)
        elif record.entity_type is EntityType.CUSTOMER:
            repo.upsert_customer(entity)
        elif record.entity_type is EntityType.LOAD:
            repo.upsert_load(entity, last_seen_sync_at=adapted.synced_at)


def _rebuild_money(repo: BrokerRepository, adapter: TmsAdapter, load_id: str) -> None:
    """Re-sum one load's money from every rate-line event ever recorded for it (I4/I7).

    TMS B states no totals: a load's carrier rate is the sum of every ``pay``
    line item ever appended, the customer rate the same over ``bill``, negatives
    included — a negative ``ADJUSTMENT`` row is exactly how a correction arrives.
    So the total is re-derived here from the log, by re-running the same adapter
    over the same stored bytes, rather than by adding this file's delta to the
    column. For a pure sum the two agree arithmetically; they stop agreeing the
    moment anything else is wrong, and only one of them can be checked against a
    replay.

    A no-op for TMS A and C, which restate totals on the load itself:
    ``rate_contributions()`` is empty for them, so the column
    :meth:`~app.repository.BrokerRepository.upsert_load` just wrote stands.
    That is also why an empty result must not be written as zero.

    Rate lines that arrive before their load produce no row to update; the sum is
    redone when the load itself turns up, because that file touches the same id.
    """
    totals: dict[RateSide, float] = {}
    for sync_file, raw_json in repo.raw_syncs_for_rate_lines(load_id):
        contributions = adapter.adapt(sync_file, raw_json).rate_contributions()
        for (contributed_to, side), amount in contributions.items():
            if contributed_to == load_id:
                totals[side] = totals.get(side, 0.0) + amount
    if not totals:
        return
    repo.set_load_money(
        load_id,
        carrier_rate=totals.get(RateSide.CARRIER),
        customer_rate=totals.get(RateSide.CUSTOMER),
    )


def _dirty_keys(
    before: dict[str, Load | None], after: dict[str, Load | None]
) -> set[LaneKey]:
    """Every lane bucket a touched load belonged to or now belongs to (I6).

    ``keys_for_load`` returns all four tiers and both equipment pools at each, so
    a change dirties the narrow key *and every rung above it* — a stale REGION
    row is as wrong as a stale ZIP3 one, and it is the rung a thin lane actually
    lands on.
    """
    dirty: set[LaneKey] = set()
    for state in (before, after):
        for load in state.values():
            if load is not None:
                dirty.update(keys_for_load(load))
    return dirty


def _affected_carriers(
    adapted: AdaptedSync,
    before: dict[str, Load | None],
    after: dict[str, Load | None],
) -> set[str]:
    """Carriers whose last known position may have moved.

    Both sides of every touched load, because a reassignment strips the position
    from the carrier that lost the load as surely as it gives one to the carrier
    that gained it. Plus the file's own carrier records: a carrier can be
    referenced by ``carrier_ref`` before its row exists, and the write that
    recorded the delivery found no row to update — this is where it is picked up.
    """
    carriers = {
        record.entity.source_carrier_id
        for record in adapted.records
        if record.entity_type is EntityType.CARRIER
    }
    for state in (before, after):
        for load in state.values():
            if load is not None and load.source_carrier_id is not None:
                carriers.add(load.source_carrier_id)
    return carriers


def _rebuild_lane_key(repo: BrokerRepository, key: LaneKey) -> None:
    """Delete and recompute one bucket's lane and carrier rows (I7).

    Delete first, unconditionally: a key whose last load just moved away has to
    end up with no row, and an upsert would leave the old numbers standing.
    """
    lane_key_args = {
        "tier": key.tier,
        "origin_key": key.origin_key,
        "dest_key": key.dest_key,
        "equipment": key.equipment,
    }
    repo.delete_lane_stats(**lane_key_args)
    lane_stats = repo.compute_lane_stats(**lane_key_args)
    if lane_stats is not None:
        repo.insert_lane_stats(lane_stats)

    repo.delete_carrier_stats(
        tier=key.tier, lane_key=key.lane_key, equipment=key.equipment
    )
    for carrier_stats in repo.compute_carrier_stats(**lane_key_args):
        repo.insert_carrier_stats(carrier_stats)


def _rebuild_last_delivery(repo: BrokerRepository, source_carrier_id: str) -> None:
    """Recompute a carrier's last known delivery position (I8).

    Recomputed, not stamped on each ``DELIVERED`` event: a late-arriving load, or
    a correction that moves a delivery time, has to be able to move this
    backwards as well as forwards. Writing ``None`` when nothing qualifies is
    part of that — a stale position is a wrong deadhead number wearing a
    plausible label.
    """
    position = repo.latest_delivery_position(source_carrier_id)
    lat, lon, at = position if position is not None else (None, None, None)
    repo.set_carrier_last_delivery(source_carrier_id, lat=lat, lon=lon, at=at)
