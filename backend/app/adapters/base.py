"""The adapter contract: one sync file in, canonical entities out.

An adapter is a pure function of ``(sync_file, payload)``. It parses one TMS's
native shape into the canonical objects of :mod:`app.domain.model` and nothing
else — no database, no business logic, no scoring, no lane keys. Everything it
returns is derived from the bytes of a single file, because that is all ingestion
hands it (PRD section 6: one file at a time, chronological).

**What an adapter deliberately cannot do: finish TMS B's money.** A TMS B load's
carrier rate is the sum of every ``rates`` line item *ever appended* across every
file (CLAUDE.md, Money row). One file holds one instalment. So the adapter leaves
``Load.carrier_rate``/``customer_rate`` as ``None`` for TMS B and returns the
file's line items as :class:`~app.domain.model.RateLine` records; ingestion
accumulates them. :meth:`AdaptedSync.rate_contributions` states this file's
contribution per load and side, so Phase 4 adds a number rather than re-deriving
it. Pretending a single file yields a total is how a correction gets lost.

**Ordering.** :attr:`AdaptedSync.records` is the file's entities in the order
they should be logged, and its index is ``sync_events.event_seq`` (DECISIONS.md
D3). Carriers and customers precede the loads that reference them; rate lines
come last, since a rate line is a fact about a load.

**Failure policy.** Optional fields degrade: a missing rate is ``None``, an
unrecognized equipment string is ``UNKNOWN``, an unresolvable location is
geo-null, a dangling carrier reference keeps the id and emits no carrier record.
Only a structurally impossible record raises :class:`AdapterError` — no load id,
or a status outside the TMS's documented vocabulary. Those cannot be degraded
without inventing data, and inventing a status is worse than a loud failure.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from ..domain.model import (
    Carrier,
    Customer,
    EntityType,
    Load,
    RateLine,
    RateSide,
    as_utc,
)

__all__ = [
    "AdapterError",
    "AdaptedRecord",
    "AdaptedSync",
    "SyncBuilder",
    "TmsAdapter",
]


class AdapterError(ValueError):
    """A record that cannot be parsed without inventing data.

    Carries the sync file and the offending record's id where they are known, so
    a failure names the file a human has to open.
    """


@dataclass(frozen=True, slots=True)
class AdaptedRecord:
    """One changed entity, paired with the raw JSON fragment it came from.

    The raw fragment travels with the entity because ``sync_events.raw_json``
    stores it (DECISIONS.md D3) and only the adapter knows which slice of the
    file produced which entity. Making ingestion re-find it would put per-TMS
    parsing knowledge in the ingestion layer, which is the boundary this package
    exists to hold.
    """

    entity_type: EntityType
    source_entity_id: str
    source_load_id: str | None
    entity: Load | Carrier | Customer | RateLine
    raw: dict

    def __post_init__(self) -> None:
        # ``sync_events`` enforces the same rule with a CHECK constraint; failing
        # here names the adapter that got it wrong instead of the INSERT.
        if self.entity_type in (EntityType.LOAD, EntityType.RATE_LINE):
            if self.source_load_id is None:
                raise AdapterError(
                    f"{self.entity_type} event {self.source_entity_id!r} has no "
                    "source_load_id; sync_events requires one"
                )


@dataclass(frozen=True, slots=True)
class AdaptedSync:
    """One sync file, adapted. The whole of an adapter's output.

    ``synced_at`` is the envelope timestamp the file states about itself, in UTC.
    It is not the filename timestamp: ingestion orders files by the **filename**
    (CLAUDE.md invariant 4), which is local Central for all three TMSs, while the
    envelope is in whatever zone that TMS uses. Both are kept because they are
    different facts.
    """

    sync_file: str
    synced_at: datetime
    records: tuple[AdaptedRecord, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "synced_at", as_utc(self.synced_at))
        object.__setattr__(self, "records", tuple(self.records))

    def _of(self, entity_type: EntityType) -> tuple:
        return tuple(r.entity for r in self.records if r.entity_type is entity_type)

    @property
    def loads(self) -> tuple[Load, ...]:
        return self._of(EntityType.LOAD)

    @property
    def carriers(self) -> tuple[Carrier, ...]:
        return self._of(EntityType.CARRIER)

    @property
    def customers(self) -> tuple[Customer, ...]:
        return self._of(EntityType.CUSTOMER)

    @property
    def rate_lines(self) -> tuple[RateLine, ...]:
        return self._of(EntityType.RATE_LINE)

    def rate_contributions(self) -> dict[tuple[str, RateSide], float]:
        """This file's money delta per ``(source_load_id, side)``.

        A **contribution**, never a total: TMS B appends line items forever, so
        the running sum lives in the event log, not in any one file. Negative
        values are included — a negative ``ADJUSTMENT`` row is exactly how TMS B
        states a correction (CLAUDE.md, Known traps), and dropping it would leave
        the corrected load permanently overpaid.

        Empty for TMS A and TMS C, which restate totals on the load instead.
        """
        totals: dict[tuple[str, RateSide], float] = {}
        for line in self.rate_lines:
            key = (line.source_load_id, line.side)
            totals[key] = totals.get(key, 0.0) + line.amount_usd
        return totals


class SyncBuilder:
    """Accumulates one file's entities, then emits them in log order.

    Two rules that all three adapters need and none of them should re-implement:

    * **Order.** Carriers and customers, then loads, then rate lines. The index
      of the result is ``event_seq``.
    * **Dedupe within the file.** The same carrier can be attached to two loads
      in one sync, and TMS B repeats a customer code on every load row. Those are
      one description arriving twice, not two events, so the first occurrence
      wins. Loads and rate lines are *not* deduped — a file legitimately carrying
      the same load twice is a data question for ingestion, not something an
      adapter should quietly collapse.
    """

    def __init__(self) -> None:
        self._carriers: dict[str, AdaptedRecord] = {}
        self._customers: dict[str, AdaptedRecord] = {}
        self._loads: list[AdaptedRecord] = []
        self._rate_lines: list[AdaptedRecord] = []

    def add_carrier(self, carrier: Carrier, raw: dict) -> None:
        self._carriers.setdefault(
            carrier.source_carrier_id,
            AdaptedRecord(
                entity_type=EntityType.CARRIER,
                source_entity_id=carrier.source_carrier_id,
                source_load_id=None,
                entity=carrier,
                raw=raw,
            ),
        )

    def add_customer(self, customer: Customer, raw: dict) -> None:
        self._customers.setdefault(
            customer.source_customer_id,
            AdaptedRecord(
                entity_type=EntityType.CUSTOMER,
                source_entity_id=customer.source_customer_id,
                source_load_id=None,
                entity=customer,
                raw=raw,
            ),
        )

    def add_load(self, load: Load, raw: dict) -> None:
        self._loads.append(
            AdaptedRecord(
                entity_type=EntityType.LOAD,
                source_entity_id=load.source_load_id,
                source_load_id=load.source_load_id,
                entity=load,
                raw=raw,
            )
        )

    def add_rate_line(self, line: RateLine, raw: dict) -> None:
        self._rate_lines.append(
            AdaptedRecord(
                entity_type=EntityType.RATE_LINE,
                source_entity_id=line.source_rate_id,
                source_load_id=line.source_load_id,
                entity=line,
                raw=raw,
            )
        )

    def build(self, sync_file: str, synced_at: datetime) -> AdaptedSync:
        return AdaptedSync(
            sync_file=sync_file,
            synced_at=synced_at,
            records=(
                *self._carriers.values(),
                *self._customers.values(),
                *self._loads,
                *self._rate_lines,
            ),
        )


class TmsAdapter(ABC):
    """Base class for the three TMS adapters.

    Stateless: one instance can adapt any number of files, and adapting a file
    twice returns equal output. Ingestion picks the implementation from
    ``brokers.tms_type`` via :func:`app.adapters.adapter_for`.
    """

    #: Matches ``brokers.tms_type`` — ``'A'``, ``'B'`` or ``'C'``.
    tms_type: str
    #: The directory under ``data/`` this TMS writes to.
    data_dir: str

    @abstractmethod
    def adapt(self, sync_file: str, payload: dict) -> AdaptedSync:
        """Adapt one parsed sync file. ``payload`` is the decoded JSON object."""
