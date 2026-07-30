"""The canonical model — the one shape all three TMSs are normalized into.

Adapters (Phase 3) turn a TMS's native JSON into these objects; the repository
stores and returns them; scoring and pricing read them. Nothing here parses,
converts units, or touches a database: it is the vocabulary the rest of the
system speaks, plus the guards that keep a wrong value from being expressible.

What is deliberately **not** expressible:

* **A defaulted equipment type.** ``Equipment`` has an explicit ``UNKNOWN``
  member and ``Load.equipment`` has no default, so an adapter must say which of
  the four it means. There is no code path where a missing value becomes
  ``DRY_VAN`` (CLAUDE.md invariant 5).
* **A silently unresolved location.** ``StopLocation.place`` has no default
  either. An adapter must pass the result of ``geo.resolve_place`` — a ``Place``
  or an explicit ``None`` (geo-null). The raw ``city``/``state``/``zip`` are kept
  alongside it, so a geo-null stop is still displayable while being excluded
  from lane statistics (normalization table, Location row).
* **A naive datetime.** Every ``datetime`` field is validated on construction
  and converted to UTC. A naive value raises rather than being assumed to be
  anything.
* **A rate that is "zero because we don't know yet".** Money fields are
  ``float | None`` with no default: an ``ACTIVE`` load has no carrier rate, and
  that is a different fact from a carrier rate of $0. Conflating them corrupts
  every average downstream.

``broker_id`` is deliberately **absent** from these objects. The tenant is
supplied by the repository binding (``repository.BrokerRepository``), which
stamps it on write, so a call site cannot construct a ``Load`` "belonging to"
one broker and store it under another. Adapters stay tenant-agnostic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import StrEnum

from .geo import Place

__all__ = [
    "Equipment",
    "LoadStatus",
    "RateSide",
    "EntityType",
    "Broker",
    "StopLocation",
    "Stop",
    "CargoItem",
    "Load",
    "Carrier",
    "LastDelivery",
    "Customer",
    "RateLine",
    "SyncEvent",
    "LaneStats",
    "CarrierStats",
    "ANY_EQUIPMENT",
    "as_utc",
]

# The equipment marker for the fourth rung of the tier walk (DECISIONS.md D6),
# whose pool deliberately spans every type. Not an ``Equipment`` member: it
# describes a *query*, not a trailer, and keeping it out of the enum is what
# stops it being written into a load.
ANY_EQUIPMENT = "ANY"


class Equipment(StrEnum):
    """The four canonical trailer types. ``UNKNOWN`` is a value, not a gap."""

    DRY_VAN = "DRY_VAN"
    REEFER = "REEFER"
    FLATBED = "FLATBED"
    UNKNOWN = "UNKNOWN"


class LoadStatus(StrEnum):
    """PRD section 5's status ladder, in lifecycle order."""

    PLANNED = "PLANNED"
    ACTIVE = "ACTIVE"
    COVERED = "COVERED"
    IN_TRANSIT = "IN_TRANSIT"
    DELIVERED = "DELIVERED"
    COMPLETED = "COMPLETED"


class RateSide(StrEnum):
    """Which side of the load a money line item lands on.

    TMS B's ``pay`` is the carrier side, ``bill`` the customer side (PRD section
    5, Money row). The canonical names say which, so no downstream reader has to
    remember the mapping.
    """

    CARRIER = "CARRIER"
    CUSTOMER = "CUSTOMER"


class EntityType(StrEnum):
    """``sync_events.entity_type`` (DECISIONS.md D3).

    ``RATE_LINE`` exists so a TMS B sync that appends money to a load whose
    ``loads`` row did not change is representable as an event.
    """

    LOAD = "LOAD"
    CARRIER = "CARRIER"
    CUSTOMER = "CUSTOMER"
    RATE_LINE = "RATE_LINE"


def as_utc(value: datetime | None) -> datetime | None:
    """Validate a datetime is timezone-aware and return it in UTC.

    ``None`` passes through — an unknown time is a legitimate fact. A *naive*
    datetime is not: the three TMSs disagree about what a bare timestamp means
    (TMS B's are US Central, TMS C's are UTC), so accepting one here would let
    that ambiguity into the canonical model. Adapters attach the right zone.
    """
    if value is None:
        return None
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(
            f"naive datetime {value!r} in the canonical model; attach the "
            "source TMS's timezone in the adapter (CLAUDE.md, Time row)"
        )
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class Broker:
    """A tenant. The only entity that is not itself tenant-scoped."""

    id: str
    name: str
    tms_type: str


@dataclass(frozen=True, slots=True)
class StopLocation:
    """Where a stop is, as the TMS said it, plus what geo made of it.

    ``place is None`` means **geo-null**: the city/state/zip did not resolve
    against the offline table. It never means "no location was given" — the raw
    fields are always carried, so the stop still renders in the UI. The two
    cases stay distinguishable because resolution is a separate field from the
    inputs, and because ``place`` has no default: an adapter cannot leave a stop
    accidentally unresolved and have it look identical to a genuine geo-null.
    """

    city: str | None
    state: str | None
    zip: str | None
    place: Place | None
    name: str | None = None

    @property
    def is_geo_null(self) -> bool:
        """True when this location contributes no lane key and no coordinates."""
        return self.place is None

    @property
    def zip3(self) -> str | None:
        return self.place.zip3 if self.place else None

    @property
    def metro(self) -> str | None:
        return self.place.metro if self.place else None

    @property
    def lat(self) -> float | None:
        return self.place.lat if self.place else None

    @property
    def lon(self) -> float | None:
        return self.place.lon if self.place else None

    def __str__(self) -> str:
        town = ", ".join(part for part in (self.city, self.state) if part)
        return f"{town} {self.zip}".strip() if self.zip else town


@dataclass(frozen=True, slots=True)
class Stop:
    """One stop on a load, in the order the TMS gave them.

    ``sequence`` is 1-based. ``scheduled_date`` is a **local (US Central)
    calendar date**, which is what all three formats agree on and what on-time
    is measured against (DECISIONS.md D7/D16); the actual arrival/departure
    timestamps are UTC. TMS C reports arrival, TMS A departure, TMS B both —
    hence both fields, each optional.
    """

    sequence: int
    is_pickup: bool
    is_drop: bool
    location: StopLocation
    scheduled_date: date | None
    window_start: datetime | None = None
    window_end: datetime | None = None
    actual_arrival: datetime | None = None
    actual_departure: datetime | None = None

    def __post_init__(self) -> None:
        for name in ("window_start", "window_end", "actual_arrival", "actual_departure"):
            object.__setattr__(self, name, as_utc(getattr(self, name)))

    @property
    def actual_at(self) -> datetime | None:
        """When the truck was demonstrably at this stop, whichever we were told.

        Arrival first: it is the moment on-time is about. TMS A only reports
        departure, so that is the fallback rather than a second signal.
        """
        return self.actual_arrival or self.actual_departure


@dataclass(frozen=True, slots=True)
class CargoItem:
    """A cargo line item. Only TMS C splits cargo out; A and B state one weight.

    ``weight_lbs`` is already normalized — TMS C's per-item
    ``bos__Weight_Units__c`` is applied by the adapter, not stored here, so no
    downstream reader can forget to convert.
    """

    commodity: str | None
    weight_lbs: float | None
    pallet_count: float | None


@dataclass(frozen=True, slots=True)
class Load:
    """A load in canonical form: the whole of what any TMS told us about it.

    Every field is required, including the ones that are frequently ``None``.
    An adapter that has no value for something has to say so.
    """

    source_load_id: str
    load_number: str | None
    status: LoadStatus
    equipment: Equipment
    stops: tuple[Stop, ...]
    weight_lbs: float | None
    distance_miles: float | None
    customer_rate: float | None
    carrier_rate: float | None
    source_carrier_id: str | None
    source_customer_id: str | None
    created_at: datetime | None
    last_modified_at: datetime | None
    cargo: tuple[CargoItem, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "created_at", as_utc(self.created_at))
        object.__setattr__(self, "last_modified_at", as_utc(self.last_modified_at))
        object.__setattr__(self, "stops", tuple(self.stops))
        object.__setattr__(self, "cargo", tuple(self.cargo))
        expected = list(range(1, len(self.stops) + 1))
        if [s.sequence for s in self.stops] != expected:
            raise ValueError(
                f"load {self.source_load_id}: stops must be ordered 1..n, got "
                f"{[s.sequence for s in self.stops]}"
            )

    @property
    def origin(self) -> Stop | None:
        """The first pickup — one half of the lane (PRD section 5, Stops row)."""
        return next((s for s in self.stops if s.is_pickup), None)

    @property
    def destination(self) -> Stop | None:
        """The last drop. Middle stops are kept but are not lane-forming."""
        return next((s for s in reversed(self.stops) if s.is_drop), None)

    @property
    def intermediate_stops(self) -> tuple[Stop, ...]:
        first, last = self.origin, self.destination
        return tuple(s for s in self.stops if s is not first and s is not last)

    @property
    def is_lane_resolvable(self) -> bool:
        """Both lane ends resolved, so this load can back a lane statistic."""
        return (
            self.origin is not None
            and self.destination is not None
            and not self.origin.location.is_geo_null
            and not self.destination.location.is_geo_null
        )

    @property
    def rate_per_mile(self) -> float | None:
        """Carrier rate per mile — the single definition of $/mi in the system.

        ``loads.rate_per_mile`` is a generated column carrying the same
        expression, so the SQL that computes lane percentiles and the Python
        that formats a reason cannot drift apart.
        """
        if self.carrier_rate is None or not self.distance_miles:
            return None
        return self.carrier_rate / self.distance_miles


@dataclass(frozen=True, slots=True)
class Carrier:
    """A carrier as one broker's TMS knows it.

    MC/DOT are the cross-system identity (DECISIONS.md D2) but never a
    cross-*broker* one: two brokers' rows for the same real company are two
    distinct carriers here, and stay that way unless the opt-in pool of Phase 11
    is built.
    """

    source_carrier_id: str
    name: str | None
    mc_number: str | None
    dot_number: str | None
    phone: str | None
    home_city: str | None
    home_state: str | None
    last_delivery_lat: float | None = None
    last_delivery_lon: float | None = None
    last_delivery_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "last_delivery_at", as_utc(self.last_delivery_at))


@dataclass(frozen=True, slots=True)
class LastDelivery:
    """Where a carrier's truck last ended up, and which load put it there.

    The deadhead signal needs coordinates; the reason a rep reads wants a town
    name ("delivered in Baytown yesterday, 38 mi from your pickup"). Both come
    off this one object, derived from one delivered load, so the miles that were
    scored and the place that was named can never describe different events.

    ``lat``/``lon`` are the load's own delivery coordinates — the same columns
    ingestion writes to ``carriers.last_delivery_*`` — and ``location`` carries
    the raw city/state/zip for display. Distance is always measured from the
    coordinates, never re-derived from the label.

    **The coordinates are nullable, and that is a third state, not a missing
    one.** A carrier that has never delivered anything has no ``LastDelivery``
    at all; a carrier whose only delivery went to a town the geo table has never
    heard of has one with ``place=None`` and no coordinates. Both score zero
    proximity credit, but they are different facts and the deadhead reason says
    which — "no known recent delivery" told to a rep about a truck that unloaded
    yesterday ten miles from the pickup is the opposite of what they would do
    with it (D23).
    """

    source_carrier_id: str
    source_load_id: str
    lat: float | None
    lon: float | None
    at: datetime | None
    location: StopLocation

    @property
    def is_placeable(self) -> bool:
        """True when this delivery can be measured from. Distance needs both."""
        return self.lat is not None and self.lon is not None

    def __post_init__(self) -> None:
        object.__setattr__(self, "at", as_utc(self.at))


@dataclass(frozen=True, slots=True)
class Customer:
    """A customer. TMS B carries no customer table — code and name ride the load."""

    source_customer_id: str
    name: str | None


@dataclass(frozen=True, slots=True)
class RateLine:
    """One money line item (TMS B ``rates``).

    TMS A and C state totals instead, so they produce no rate lines; their
    corrections are restatements of the total on the load itself. A TMS B total
    is the sum of every line ever appended for a side, negatives included.
    """

    source_rate_id: str
    source_load_id: str
    side: RateSide
    code: str | None
    amount_usd: float
    created_at: datetime | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "created_at", as_utc(self.created_at))


@dataclass(frozen=True, slots=True)
class SyncEvent:
    """One row of the append-only event log, as read back (DECISIONS.md D3)."""

    id: int
    sync_file: str
    synced_at: datetime
    entity_type: EntityType
    source_entity_id: str
    source_load_id: str | None
    event_seq: int
    raw_json: dict

    def __post_init__(self) -> None:
        object.__setattr__(self, "synced_at", as_utc(self.synced_at))


@dataclass(frozen=True, slots=True)
class LaneStats:
    """A derived lane row. Rebuilt for dirty keys, never patched (invariant 3).

    ``equipment`` is one of the four canonical types, or ``ANY_EQUIPMENT`` for
    the fourth rung of the tier walk (DECISIONS.md D6), which pools every type.
    """

    tier: str
    origin_key: str
    dest_key: str
    equipment: str
    load_count: int
    rate_per_mile_p25: float | None
    rate_per_mile_p50: float | None
    rate_per_mile_p75: float | None
    first_load_at: datetime | None
    last_load_at: datetime | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "first_load_at", as_utc(self.first_load_at))
        object.__setattr__(self, "last_load_at", as_utc(self.last_load_at))


@dataclass(frozen=True, slots=True)
class CarrierStats:
    """A derived carrier-on-a-lane row. Rebuilt for dirty keys, never patched.

    The carrier's last known delivery position lives on ``Carrier``, not here:
    it is one fact per carrier, and a copy on every lane row could disagree with
    itself after a partial rebuild.

    **Three counts, not two.** ``on_time_count`` is a numerator whose denominator
    is ``on_time_eligible_count`` — the loads that *have a verdict*
    (:func:`~app.domain.localtime.delivered_on_time` returns ``None`` for a load
    with no arrival yet) — and not ``load_count``. Dividing by ``load_count``
    counts every rolling truck as a miss, which understates precisely the
    carriers with freight in motion (TASKS.md R1). They are stored separately so
    scoring cannot reconstruct the wrong ratio from the right numbers.
    """

    source_carrier_id: str
    tier: str
    lane_key: str
    equipment: str
    load_count: int
    on_time_count: int
    on_time_eligible_count: int
    avg_rate_per_mile: float | None
    first_load_at: datetime | None
    last_load_at: datetime | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "first_load_at", as_utc(self.first_load_at))
        object.__setattr__(self, "last_load_at", as_utc(self.last_load_at))
        # Loads with a verdict are a subset of the carrier's loads on the lane,
        # and the on-time ones are a subset of those. A rebuild that violates
        # this has counted two different populations, which would silently
        # produce an on-time rate above 1.0 or below the truth.
        if not 0 <= self.on_time_count <= self.on_time_eligible_count <= self.load_count:
            raise ValueError(
                f"carrier {self.source_carrier_id} on {self.lane_key}: expected "
                f"0 <= on_time_count <= on_time_eligible_count <= load_count, got "
                f"{self.on_time_count} <= {self.on_time_eligible_count} <= "
                f"{self.load_count}"
            )

    @property
    def on_time_rate(self) -> float | None:
        """Observed on-time rate, or ``None`` when nothing is answerable yet.

        The one place the ratio is formed, so no caller picks its own
        denominator. Scoring shrinks this toward the lane average (D5); it does
        not re-divide.
        """
        if self.on_time_eligible_count == 0:
            return None
        return self.on_time_count / self.on_time_eligible_count
