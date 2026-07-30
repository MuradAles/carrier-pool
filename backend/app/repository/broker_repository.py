"""All SQL that touches tenant data, and the binding that makes it safe.

CLAUDE.md invariant 1 says every query filters by ``broker_id``, *enforced in
the repository layer so no call site can omit it*. TASKS.md M3 sharpens that:
the property is not "every method written so far filters correctly", it is
"a method that does not filter cannot succeed". Three mechanisms, each
independently sufficient to stop a cross-broker read:

1. **The broker is bound at construction.** :class:`BrokerRepository` is built
   from a ``broker_id`` and keeps it private. No method takes a broker
   parameter, so no future method can forget one — there is nothing to forget.
   Writes stamp the bound value; a caller cannot supply a different one,
   because the canonical dataclasses do not carry ``broker_id`` at all.

2. **Every statement goes through :func:`broker_session`.** It is the only
   place in the package that hands out a cursor for a tenant table, and it sets
   ``SET LOCAL ROLE carrier_pool_app`` and ``app.broker_id`` before yielding.

3. **The database enforces it.** ``carrier_pool_app`` is NOSUPERUSER and
   NOBYPASSRLS, and every tenant table has a row-level-security policy keyed on
   ``app.broker_id``. A method whose SQL forgets ``WHERE broker_id = %s``
   returns only the bound broker's rows anyway, and one that runs with no
   binding at all raises (``unrecognized configuration parameter``) rather than
   returning everything. See ``schema.sql``.

The SQL below still spells out ``broker_id = %s`` everywhere. That is
deliberate redundancy: the code should be correct when read on its own, and RLS
should be the backstop rather than the only stop.

**The documented way around it.** RLS is silently ignored for superusers, so
the escape hatch is a superuser connection. It is confined to one credential:
``ADMIN_DATABASE_URL``, reachable only through
:func:`app.repository.db.connect_admin`, and used only to apply ``schema.sql``.
``DATABASE_URL`` — the ambient credential, the one a call site picks up if it
writes ``psycopg.connect(DATABASE_URL)`` from habit — names ``carrier_pool_app``
and has neither SUPERUSER nor BYPASSRLS, so the careless path fails closed
rather than open. Even on an admin connection, anything routed through
:func:`broker_session` is confined, because ``SET LOCAL ROLE`` drops the bypass
for the duration of the block.

What is left: code that reads ``ADMIN_DATABASE_URL`` (or the compose file) and
writes raw SQL over it. No database setting can stop a superuser; the mitigation
is that doing so now requires naming the admin credential explicitly, which is
greppable and reviewable.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ..domain.geo import Place
from ..domain.lanes import (
    RATED_STATUSES,
    TIER_METRO,
    TIER_REGION,
    TIER_REGION_ANY,
    TIER_ZIP3,
    LaneKey,
)
from ..domain.localtime import central_date, delivered_on_time
from ..domain.model import (
    ANY_EQUIPMENT,
    CargoItem,
    Carrier,
    CarrierStats,
    Customer,
    EntityType,
    Equipment,
    LaneStats,
    LastDelivery,
    Load,
    LoadStatus,
    Stop,
    StopLocation,
    SyncEvent,
)
from ..domain.scoring import RankingInputs
from .db import APP_ROLE, BROKER_SETTING, get_broker

__all__ = ["BrokerRepository", "UnknownBroker", "broker_session"]


class UnknownBroker(LookupError):
    """No such tenant. Raised by :meth:`BrokerRepository.for_broker`."""


# SET ROLE takes no query parameter, so this one is a literal rather than a
# parameterized statement; the asserts keep it honest against db.py.
_SET_ROLE_SQL = "SET LOCAL ROLE carrier_pool_app"
_BIND_BROKER_SQL = "SELECT set_config('app.broker_id', %s, true)"
assert APP_ROLE in _SET_ROLE_SQL
assert BROKER_SETTING in _BIND_BROKER_SQL

# One constant SQL fragment per tier, selected by name in
# ``BrokerRepository._population``. This is a lookup in a closed table, not
# string-building: the tier is a schema-checked enum-ish value and the fragments
# contain no data, while every value the query filters on is a parameter.
#
# REGION and REGION_ANY share a predicate because the region *is* the whole
# table (PRD section 7); what separates them is the equipment pool and the
# confidence the walk assigns them, not the geography. Both still require both
# ends placed, so a geo-null load backs no rung at all.
_TIER_PREDICATE: dict[str, str] = {
    TIER_ZIP3: "pickup_zip3 = %s AND delivery_zip3 = %s",
    TIER_METRO: "pickup_metro = %s AND delivery_metro = %s",
    TIER_REGION: "pickup_metro IS NOT NULL AND delivery_metro IS NOT NULL",
    TIER_REGION_ANY: "pickup_metro IS NOT NULL AND delivery_metro IS NOT NULL",
}
# ``_population`` writes the mixed-pool marker as a SQL literal, so it has to be
# the value the rest of the system means by it.
assert ANY_EQUIPMENT == "ANY"


@contextmanager
def broker_session(conn: psycopg.Connection, broker_id: str) -> Iterator[psycopg.Cursor]:
    """Yield a cursor confined to one broker for the duration of the block.

    Inside the block the connection runs as ``carrier_pool_app`` with
    ``app.broker_id`` set, both transaction-locally, so every tenant table is
    filtered by the database itself. Reads see one broker's rows; writes of a
    row carrying any other ``broker_id`` are rejected by the policy's
    ``WITH CHECK``.

    Nested use is fine: the block opens a transaction, which becomes a
    savepoint when one is already open, so ingestion can wrap a whole sync file
    in a transaction and still make many repository calls inside it.

    ``integration-tester``: this is the seam for proving barrier 3. Run
    deliberately unfiltered SQL — ``SELECT count(*) FROM loads`` — inside the
    block and observe that it counts only the bound broker's rows, and that the
    same SQL outside a block on a :func:`app.repository.db.connect` connection
    raises instead of returning everything.
    """
    with conn.transaction():
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(_SET_ROLE_SQL)
            cur.execute(_BIND_BROKER_SQL, (broker_id,))
            yield cur


# ---------------------------------------------------------------------------
# Row <-> canonical object mapping
# ---------------------------------------------------------------------------


def _f(value: Any) -> float | None:
    """NUMERIC comes back as ``Decimal``; the canonical model speaks floats."""
    return None if value is None else float(value)


def _iso(value: datetime | date | None) -> str | None:
    return None if value is None else value.isoformat()


def _dt(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def _d(value: str | None) -> date | None:
    return None if value is None else date.fromisoformat(value)


def _place_json(place: Place | None) -> dict | None:
    if place is None:
        return None
    return {
        "city": place.city,
        "state": place.state,
        "zip": place.zip,
        "lat": place.lat,
        "lon": place.lon,
        "metro": place.metro,
    }


def _place_from_json(raw: dict | None) -> Place | None:
    if raw is None:
        return None
    return Place(
        city=raw["city"],
        state=raw["state"],
        zip=raw["zip"],
        lat=raw["lat"],
        lon=raw["lon"],
        metro=raw["metro"],
    )


def _stops_json(stops: Sequence[Stop]) -> list[dict]:
    return [
        {
            "sequence": s.sequence,
            "is_pickup": s.is_pickup,
            "is_drop": s.is_drop,
            "location": {
                "city": s.location.city,
                "state": s.location.state,
                "zip": s.location.zip,
                "name": s.location.name,
                "place": _place_json(s.location.place),
            },
            "scheduled_date": _iso(s.scheduled_date),
            "window_start": _iso(s.window_start),
            "window_end": _iso(s.window_end),
            "actual_arrival": _iso(s.actual_arrival),
            "actual_departure": _iso(s.actual_departure),
        }
        for s in stops
    ]


def _stops_from_json(raw: Sequence[dict]) -> tuple[Stop, ...]:
    return tuple(
        Stop(
            sequence=s["sequence"],
            is_pickup=s["is_pickup"],
            is_drop=s["is_drop"],
            location=StopLocation(
                city=s["location"]["city"],
                state=s["location"]["state"],
                zip=s["location"]["zip"],
                place=_place_from_json(s["location"]["place"]),
                name=s["location"]["name"],
            ),
            scheduled_date=_d(s["scheduled_date"]),
            window_start=_dt(s["window_start"]),
            window_end=_dt(s["window_end"]),
            actual_arrival=_dt(s["actual_arrival"]),
            actual_departure=_dt(s["actual_departure"]),
        )
        for s in raw
    )


def _cargo_json(cargo: Sequence[CargoItem]) -> list[dict]:
    return [
        {
            "commodity": c.commodity,
            "weight_lbs": c.weight_lbs,
            "pallet_count": c.pallet_count,
        }
        for c in cargo
    ]


def _cargo_from_json(raw: Sequence[dict]) -> tuple[CargoItem, ...]:
    return tuple(
        CargoItem(
            commodity=c["commodity"],
            weight_lbs=c["weight_lbs"],
            pallet_count=c["pallet_count"],
        )
        for c in raw
    )


def _load_from_row(row: dict) -> Load:
    return Load(
        source_load_id=row["source_load_id"],
        load_number=row["load_number"],
        status=LoadStatus(row["status"]),
        equipment=Equipment(row["equipment"]),
        stops=_stops_from_json(row["stops"]),
        weight_lbs=_f(row["weight_lbs"]),
        distance_miles=_f(row["distance_miles"]),
        customer_rate=_f(row["customer_rate"]),
        carrier_rate=_f(row["carrier_rate"]),
        source_carrier_id=row["source_carrier_id"],
        source_customer_id=row["source_customer_id"],
        created_at=row["created_at"],
        last_modified_at=row["last_modified_at"],
        cargo=_cargo_from_json(row["cargo"]),
    )


def _carrier_from_row(row: dict) -> Carrier:
    return Carrier(
        source_carrier_id=row["source_carrier_id"],
        name=row["name"],
        mc_number=row["mc_number"],
        dot_number=row["dot_number"],
        phone=row["phone"],
        home_city=row["home_city"],
        home_state=row["home_state"],
        last_delivery_lat=row["last_delivery_lat"],
        last_delivery_lon=row["last_delivery_lon"],
        last_delivery_at=row["last_delivery_at"],
    )


def _lane_stats_from_row(row: dict) -> LaneStats:
    return LaneStats(
        tier=row["tier"],
        origin_key=row["origin_key"],
        dest_key=row["dest_key"],
        equipment=row["equipment"],
        load_count=row["load_count"],
        rate_per_mile_p25=_f(row["rate_per_mile_p25"]),
        rate_per_mile_p50=_f(row["rate_per_mile_p50"]),
        rate_per_mile_p75=_f(row["rate_per_mile_p75"]),
        first_load_at=row["first_load_at"],
        last_load_at=row["last_load_at"],
    )


def _carrier_stats_from_row(row: dict) -> CarrierStats:
    return CarrierStats(
        source_carrier_id=row["source_carrier_id"],
        tier=row["tier"],
        lane_key=row["lane_key"],
        equipment=row["equipment"],
        load_count=row["load_count"],
        on_time_count=row["on_time_count"],
        on_time_eligible_count=row["on_time_eligible_count"],
        avg_rate_per_mile=_f(row["avg_rate_per_mile"]),
        first_load_at=row["first_load_at"],
        last_load_at=row["last_load_at"],
    )


@dataclass(frozen=True, slots=True)
class _LaneKeys:
    """The lane columns of ``loads``, derived from a load's own stops.

    Built in one place so the indexed key columns and the ``stops`` JSON they
    summarise are always written from the same ``Load`` in the same statement.
    """

    pickup_zip3: str | None
    pickup_metro: str | None
    pickup_lat: float | None
    pickup_lon: float | None
    delivery_zip3: str | None
    delivery_metro: str | None
    delivery_lat: float | None
    delivery_lon: float | None
    pickup_scheduled_date: date | None
    delivery_scheduled_date: date | None
    pickup_actual_at: datetime | None
    delivery_actual_at: datetime | None

    @classmethod
    def of(cls, load: Load) -> _LaneKeys:
        origin, dest = load.origin, load.destination
        o_loc = origin.location if origin else None
        d_loc = dest.location if dest else None
        return cls(
            pickup_zip3=o_loc.zip3 if o_loc else None,
            pickup_metro=o_loc.metro if o_loc else None,
            pickup_lat=o_loc.lat if o_loc else None,
            pickup_lon=o_loc.lon if o_loc else None,
            delivery_zip3=d_loc.zip3 if d_loc else None,
            delivery_metro=d_loc.metro if d_loc else None,
            delivery_lat=d_loc.lat if d_loc else None,
            delivery_lon=d_loc.lon if d_loc else None,
            pickup_scheduled_date=origin.scheduled_date if origin else None,
            delivery_scheduled_date=dest.scheduled_date if dest else None,
            pickup_actual_at=origin.actual_at if origin else None,
            delivery_actual_at=dest.actual_at if dest else None,
        )


# ---------------------------------------------------------------------------
# The repository
# ---------------------------------------------------------------------------


class BrokerRepository:
    """Every tenant-scoped query, bound to one broker for its whole lifetime.

    **Unknown tenant versus empty tenant.** Constructing a repository for a
    ``broker_id`` that is not in ``brokers`` raises :class:`UnknownBroker`, on
    *every* construction path — there is no way to get a silently inert
    repository whose reads all come back empty. So:

    * ``UnknownBroker`` means the tenant does not exist. The API turns it into
      a 404; a typo'd broker id must not read as "this broker has no carriers".
    * ``None`` or ``[]`` from a method means this tenant has no such row —
      *including* the case where the row exists under a different broker.
      :meth:`get_load` deliberately cannot distinguish "another broker's load"
      from "no such load", because the existence of that id is itself another
      tenant's data.

    :func:`broker_session` is the exception and stays unvalidated: it is the raw
    mechanism, used to prove the database-level barrier, and a bogus broker
    there correctly sees nothing.
    """

    __slots__ = ("_conn", "_broker_id")

    def __init__(self, conn: psycopg.Connection, broker_id: str) -> None:
        """Bind to ``broker_id``, or raise :class:`UnknownBroker`.

        The check lives here rather than in :meth:`for_broker` alone so that the
        plain constructor cannot be the lenient way in.
        """
        if get_broker(conn, broker_id) is None:
            raise UnknownBroker(broker_id)
        self._conn = conn
        self._broker_id = broker_id

    @classmethod
    def for_broker(cls, conn: psycopg.Connection, broker_id: str) -> BrokerRepository:
        """Named constructor. Identical to ``BrokerRepository(conn, broker_id)``."""
        return cls(conn, broker_id)

    @property
    def broker_id(self) -> str:
        return self._broker_id

    @contextmanager
    def _cursor(self) -> Iterator[psycopg.Cursor]:
        with broker_session(self._conn, self._broker_id) as cur:
            yield cur

    # -- provenance: append-only (invariant 3, DECISIONS.md D3) --------------

    def record_sync_file(
        self, sync_file: str, synced_at: datetime, raw_json: dict
    ) -> int | None:
        """Record a file as ingested. ``None`` means it already was.

        The no-op on re-ingest is the database's ``UNIQUE (broker_id,
        sync_file)``, not a check here — so it holds even for a caller that
        never asked (invariant 4).
        """
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO sync_files (broker_id, sync_file, synced_at, raw_json)"
                " VALUES (%s, %s, %s, %s)"
                " ON CONFLICT (broker_id, sync_file) DO NOTHING"
                " RETURNING id",
                (self._broker_id, sync_file, synced_at, Jsonb(raw_json)),
            )
            row = cur.fetchone()
            return None if row is None else row["id"]

    def sync_file_id(self, sync_file: str) -> int | None:
        """The id of an already-ingested file, or ``None``."""
        with self._cursor() as cur:
            cur.execute(
                "SELECT id FROM sync_files WHERE broker_id = %s AND sync_file = %s",
                (self._broker_id, sync_file),
            )
            row = cur.fetchone()
            return None if row is None else row["id"]

    def append_event(
        self,
        *,
        sync_file_id: int,
        synced_at: datetime,
        entity_type: EntityType,
        source_entity_id: str,
        raw_json: dict,
        event_seq: int,
        source_load_id: str | None = None,
    ) -> int:
        """Append one entity-level event. There is no update or delete path.

        ``carrier_pool_app`` holds only SELECT and INSERT on this table, so
        append-only is a privilege, not a convention.
        """
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO sync_events (sync_file_id, broker_id, entity_type,"
                " source_entity_id, source_load_id, raw_json, event_seq, synced_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
                " RETURNING id",
                (
                    sync_file_id,
                    self._broker_id,
                    str(entity_type),
                    source_entity_id,
                    source_load_id,
                    Jsonb(raw_json),
                    event_seq,
                    synced_at,
                ),
            )
            return cur.fetchone()["id"]

    def events_for_load(self, source_load_id: str) -> list[SyncEvent]:
        """Every event mentioning this load, in arrival order.

        Ordered by ``(synced_at, event_seq)`` — the file's own clock, then
        position within the file — so a rebuild replays exactly the order the
        data arrived in. Includes ``RATE_LINE`` events for a load whose TMS B
        ``loads`` row never changed.
        """
        with self._cursor() as cur:
            cur.execute(
                "SELECT e.id, f.sync_file, e.synced_at, e.entity_type,"
                " e.source_entity_id, e.source_load_id, e.event_seq, e.raw_json"
                " FROM sync_events e"
                " JOIN sync_files f ON f.id = e.sync_file_id AND f.broker_id = e.broker_id"
                " WHERE e.broker_id = %s AND e.source_load_id = %s"
                " ORDER BY e.synced_at, e.event_seq",
                (self._broker_id, source_load_id),
            )
            return [
                SyncEvent(
                    id=row["id"],
                    sync_file=row["sync_file"],
                    synced_at=row["synced_at"],
                    entity_type=EntityType(row["entity_type"]),
                    source_entity_id=row["source_entity_id"],
                    source_load_id=row["source_load_id"],
                    event_seq=row["event_seq"],
                    raw_json=row["raw_json"],
                )
                for row in cur.fetchall()
            ]

    def raw_syncs_for_rate_lines(self, source_load_id: str) -> list[tuple[str, dict]]:
        """Every ingested file that contributed a ``RATE_LINE`` event to this load.

        Returns ``(sync_file, raw_json)`` in filename order — which within one
        broker is ingestion order, since files are processed sorted by the
        timestamp in the name (invariant 4).

        This is the raw material for rebuilding a TMS B load's money. Ingestion
        re-runs the *same adapter* over these bytes and re-sums the
        contributions, rather than adding this file's delta to whatever the
        column already held: a sum of appended line items and a patched running
        total agree only for as long as nothing else is ever wrong, and the
        rebuild is what makes a late correction produce the same number as an
        on-time one (invariant 3).

        Deliberately returns *files*, not events: the file is what the adapter
        takes, so no TMS-specific knowledge of a rate row's shape leaks into the
        repository or into ingestion.
        """
        with self._cursor() as cur:
            cur.execute(
                "SELECT f.sync_file, f.raw_json FROM sync_files f"
                " WHERE f.broker_id = %s AND EXISTS ("
                "   SELECT 1 FROM sync_events e"
                "   WHERE e.broker_id = f.broker_id AND e.sync_file_id = f.id"
                "     AND e.entity_type = 'RATE_LINE' AND e.source_load_id = %s)"
                " ORDER BY f.sync_file",
                (self._broker_id, source_load_id),
            )
            return [(row["sync_file"], row["raw_json"]) for row in cur.fetchall()]

    # -- current truth ------------------------------------------------------

    def upsert_load(self, load: Load, *, last_seen_sync_at: datetime) -> None:
        """Write the newest truth for a load.

        ``broker_id`` comes from the binding: :class:`~app.domain.model.Load`
        carries none, so there is no way to write one broker's load into
        another's rows even by accident.

        The on-time verdict is computed here, by calling the domain function,
        rather than taken from the caller: it is a pure function of the load, and
        deriving it on every write means no ingestion path can forget it and no
        SQL aggregate has to re-implement the Central-date comparison (D7/D16).
        """
        keys = _LaneKeys.of(load)
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO loads (broker_id, source_load_id, load_number, status,"
                " equipment, weight_lbs, distance_miles, customer_rate, carrier_rate,"
                " source_carrier_id, source_customer_id,"
                " pickup_zip3, pickup_metro, pickup_lat, pickup_lon,"
                " delivery_zip3, delivery_metro, delivery_lat, delivery_lon,"
                " pickup_scheduled_date, delivery_scheduled_date,"
                " pickup_actual_at, delivery_actual_at, delivered_on_time,"
                " stops, cargo, created_at, last_modified_at, last_seen_sync_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
                " %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (broker_id, source_load_id) DO UPDATE SET"
                " load_number = EXCLUDED.load_number,"
                " status = EXCLUDED.status,"
                " equipment = EXCLUDED.equipment,"
                " weight_lbs = EXCLUDED.weight_lbs,"
                " distance_miles = EXCLUDED.distance_miles,"
                " customer_rate = EXCLUDED.customer_rate,"
                " carrier_rate = EXCLUDED.carrier_rate,"
                " source_carrier_id = EXCLUDED.source_carrier_id,"
                " source_customer_id = EXCLUDED.source_customer_id,"
                " pickup_zip3 = EXCLUDED.pickup_zip3,"
                " pickup_metro = EXCLUDED.pickup_metro,"
                " pickup_lat = EXCLUDED.pickup_lat,"
                " pickup_lon = EXCLUDED.pickup_lon,"
                " delivery_zip3 = EXCLUDED.delivery_zip3,"
                " delivery_metro = EXCLUDED.delivery_metro,"
                " delivery_lat = EXCLUDED.delivery_lat,"
                " delivery_lon = EXCLUDED.delivery_lon,"
                " pickup_scheduled_date = EXCLUDED.pickup_scheduled_date,"
                " delivery_scheduled_date = EXCLUDED.delivery_scheduled_date,"
                " pickup_actual_at = EXCLUDED.pickup_actual_at,"
                " delivery_actual_at = EXCLUDED.delivery_actual_at,"
                " delivered_on_time = EXCLUDED.delivered_on_time,"
                " stops = EXCLUDED.stops,"
                " cargo = EXCLUDED.cargo,"
                " created_at = EXCLUDED.created_at,"
                " last_modified_at = EXCLUDED.last_modified_at,"
                " last_seen_sync_at = EXCLUDED.last_seen_sync_at",
                (
                    self._broker_id,
                    load.source_load_id,
                    load.load_number,
                    str(load.status),
                    str(load.equipment),
                    load.weight_lbs,
                    load.distance_miles,
                    load.customer_rate,
                    load.carrier_rate,
                    load.source_carrier_id,
                    load.source_customer_id,
                    keys.pickup_zip3,
                    keys.pickup_metro,
                    keys.pickup_lat,
                    keys.pickup_lon,
                    keys.delivery_zip3,
                    keys.delivery_metro,
                    keys.delivery_lat,
                    keys.delivery_lon,
                    keys.pickup_scheduled_date,
                    keys.delivery_scheduled_date,
                    keys.pickup_actual_at,
                    keys.delivery_actual_at,
                    delivered_on_time(load),
                    Jsonb(_stops_json(load.stops)),
                    Jsonb(_cargo_json(load.cargo)),
                    load.created_at,
                    load.last_modified_at,
                    last_seen_sync_at,
                ),
            )

    def set_load_money(
        self,
        source_load_id: str,
        *,
        carrier_rate: float | None,
        customer_rate: float | None,
    ) -> bool:
        """Write a load's money without touching anything else. ``False`` if no
        such load.

        Exists for TMS B, whose totals are not stated on the load at all but
        summed from every rate line ever appended — including a sync that
        appends a line item for a load its ``loads`` array does not mention
        (CLAUDE.md, Known traps). That correction has no load object to ride in
        on, so it cannot go through :meth:`upsert_load`.

        Both sides are written together and always from a full re-sum, so a side
        with no line items becomes ``NULL`` rather than ``0`` — "we have not been
        told what this costs" is a different fact from "it cost nothing", and
        conflating them drags every median that reads it. The return value is
        ``False`` when the rate lines arrived before the load itself; ingestion
        re-sums when the load turns up, so nothing is lost.
        """
        with self._cursor() as cur:
            cur.execute(
                "UPDATE loads SET carrier_rate = %s, customer_rate = %s"
                " WHERE broker_id = %s AND source_load_id = %s",
                (carrier_rate, customer_rate, self._broker_id, source_load_id),
            )
            return cur.rowcount > 0

    def get_load(self, source_load_id: str) -> Load | None:
        """One load, or ``None``.

        A load id belonging to another broker returns ``None`` — the same
        answer as an id that does not exist. The caller cannot tell the two
        apart, which is the point: existence is itself another tenant's data.
        """
        with self._cursor() as cur:
            cur.execute(
                "SELECT source_load_id, load_number, status, equipment, weight_lbs,"
                " distance_miles, customer_rate, carrier_rate, source_carrier_id,"
                " source_customer_id, stops, cargo, created_at, last_modified_at"
                " FROM loads WHERE broker_id = %s AND source_load_id = %s",
                (self._broker_id, source_load_id),
            )
            row = cur.fetchone()
            return None if row is None else _load_from_row(row)

    def list_loads(self, *, status: LoadStatus | None = None) -> list[Load]:
        """This broker's loads, optionally filtered by status."""
        with self._cursor() as cur:
            cur.execute(
                "SELECT source_load_id, load_number, status, equipment, weight_lbs,"
                " distance_miles, customer_rate, carrier_rate, source_carrier_id,"
                " source_customer_id, stops, cargo, created_at, last_modified_at"
                " FROM loads"
                " WHERE broker_id = %s AND (%s::text IS NULL OR status = %s::text)"
                " ORDER BY pickup_scheduled_date NULLS LAST, source_load_id",
                (
                    self._broker_id,
                    None if status is None else str(status),
                    None if status is None else str(status),
                ),
            )
            return [_load_from_row(row) for row in cur.fetchall()]

    def upsert_carrier(self, carrier: Carrier) -> None:
        """Write carrier identity. Leaves the last-delivery position alone —
        that is derived, and :meth:`set_carrier_last_delivery` owns it."""
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO carriers (broker_id, source_carrier_id, name, mc_number,"
                " dot_number, phone, home_city, home_state)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (broker_id, source_carrier_id) DO UPDATE SET"
                " name = EXCLUDED.name,"
                " mc_number = EXCLUDED.mc_number,"
                " dot_number = EXCLUDED.dot_number,"
                " phone = EXCLUDED.phone,"
                " home_city = EXCLUDED.home_city,"
                " home_state = EXCLUDED.home_state",
                (
                    self._broker_id,
                    carrier.source_carrier_id,
                    carrier.name,
                    carrier.mc_number,
                    carrier.dot_number,
                    carrier.phone,
                    carrier.home_city,
                    carrier.home_state,
                ),
            )

    def set_carrier_last_delivery(
        self,
        source_carrier_id: str,
        *,
        lat: float | None,
        lon: float | None,
        at: datetime | None,
    ) -> None:
        """Set the carrier's last known delivery position (deadhead input).

        Written as a whole, from one delivered load, so the three columns
        always describe the same event.
        """
        with self._cursor() as cur:
            cur.execute(
                "UPDATE carriers SET last_delivery_lat = %s, last_delivery_lon = %s,"
                " last_delivery_at = %s"
                " WHERE broker_id = %s AND source_carrier_id = %s",
                (lat, lon, at, self._broker_id, source_carrier_id),
            )

    def get_carrier(self, source_carrier_id: str) -> Carrier | None:
        with self._cursor() as cur:
            cur.execute(
                "SELECT source_carrier_id, name, mc_number, dot_number, phone,"
                " home_city, home_state, last_delivery_lat, last_delivery_lon,"
                " last_delivery_at"
                " FROM carriers WHERE broker_id = %s AND source_carrier_id = %s",
                (self._broker_id, source_carrier_id),
            )
            row = cur.fetchone()
            return None if row is None else _carrier_from_row(row)

    def list_carriers(self) -> list[Carrier]:
        """Every carrier this broker has used — the ranking's candidate pool."""
        with self._cursor() as cur:
            cur.execute(
                "SELECT source_carrier_id, name, mc_number, dot_number, phone,"
                " home_city, home_state, last_delivery_lat, last_delivery_lon,"
                " last_delivery_at"
                " FROM carriers WHERE broker_id = %s ORDER BY source_carrier_id",
                (self._broker_id,),
            )
            return [_carrier_from_row(row) for row in cur.fetchall()]

    def upsert_customer(self, customer: Customer) -> None:
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO customers (broker_id, source_customer_id, name)"
                " VALUES (%s, %s, %s)"
                " ON CONFLICT (broker_id, source_customer_id) DO UPDATE SET"
                " name = EXCLUDED.name",
                (self._broker_id, customer.source_customer_id, customer.name),
            )

    def get_customer(self, source_customer_id: str) -> Customer | None:
        with self._cursor() as cur:
            cur.execute(
                "SELECT source_customer_id, name FROM customers"
                " WHERE broker_id = %s AND source_customer_id = %s",
                (self._broker_id, source_customer_id),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return Customer(source_customer_id=row["source_customer_id"], name=row["name"])

    # -- derived statistics -------------------------------------------------
    #
    # Rebuild is delete-then-insert per dirty key (invariant 3): these two
    # halves exist separately because a key whose loads all disappeared must
    # still be deleted, and an incremental patch would be a different number
    # from a replay. Both halves run inside the binding like everything else.
    #
    # The compute_* halves are the "from raw events" of invariant 3, one step
    # removed: they aggregate `loads`, which is itself rewritten wholesale from
    # the newest event for the load and whose TMS B money is re-summed from every
    # rate-line event (see raw_syncs_for_rate_lines). So the chain events ->
    # loads -> stats contains no delta anywhere; each link is a full recompute of
    # what it derives. Re-deriving every load from the log again *here* would
    # re-parse every file that ever mentioned any load on the lane, which is the
    # alternative DECISIONS.md D3 rejects for exactly that reason.

    def _population(
        self, *, tier: str, origin_key: str, dest_key: str, equipment: str
    ) -> tuple[str, list[Any]]:
        """The WHERE clause selecting the loads one lane key is computed from.

        The tier chooses a **constant** SQL fragment from a fixed table; every
        value — broker, keys, equipment, statuses — is a parameter. Nothing
        derived from data is ever interpolated into the statement.

        Three filters, each load-bearing:

        * ``status = ANY(...)`` — only booked rates count (``RATED_STATUSES``),
          so a day-11 ``ACTIVE`` load looking for a truck does not vote.
        * ``rate_per_mile IS NOT NULL`` — a load with no carrier rate, or no
          distance, has no $/mi. Counting it as zero would drag the median down.
        * The tier predicate is written against the ``pickup_*``/``delivery_*``
          key columns, which are NULL for a geo-null stop — so a load we could
          not place is excluded from every tier including ``REGION``, while its
          row and its stops stay fully displayable.
        """
        try:
            predicate = _TIER_PREDICATE[tier]
        except KeyError:
            raise ValueError(
                f"unknown tier {tier!r}; expected one of {sorted(_TIER_PREDICATE)}"
            ) from None
        sql = (
            " WHERE broker_id = %s AND status = ANY(%s) AND rate_per_mile IS NOT NULL"
            f" AND (%s = '{ANY_EQUIPMENT}' OR equipment = %s) AND {predicate}"
        )
        params: list[Any] = [
            self._broker_id,
            [str(s) for s in RATED_STATUSES],
            equipment,
            equipment,
        ]
        if tier in (TIER_ZIP3, TIER_METRO):
            params += [origin_key, dest_key]
        return sql, params

    def compute_lane_stats(
        self, *, tier: str, origin_key: str, dest_key: str, equipment: str
    ) -> LaneStats | None:
        """Recompute one lane key from scratch. ``None`` when nothing backs it.

        Percentiles are PostgreSQL's ``percentile_cont`` — linear interpolation,
        which is what ``data/TRACEABILITY.md`` computed its expected medians
        with. The date range is the span of *deliveries*, since that is the
        evidence's age; a load still rolling contributes its rate but no date.
        """
        where, params = self._population(
            tier=tier, origin_key=origin_key, dest_key=dest_key, equipment=equipment
        )
        with self._cursor() as cur:
            cur.execute(
                "SELECT count(*) AS load_count,"
                " percentile_cont(0.25) WITHIN GROUP (ORDER BY rate_per_mile) AS p25,"
                " percentile_cont(0.50) WITHIN GROUP (ORDER BY rate_per_mile) AS p50,"
                " percentile_cont(0.75) WITHIN GROUP (ORDER BY rate_per_mile) AS p75,"
                " min(delivery_actual_at) AS first_load_at,"
                " max(delivery_actual_at) AS last_load_at"
                " FROM loads" + where,
                params,
            )
            row = cur.fetchone()
        if row["load_count"] == 0:
            return None
        return LaneStats(
            tier=tier,
            origin_key=origin_key,
            dest_key=dest_key,
            equipment=equipment,
            load_count=row["load_count"],
            rate_per_mile_p25=_f(row["p25"]),
            rate_per_mile_p50=_f(row["p50"]),
            rate_per_mile_p75=_f(row["p75"]),
            first_load_at=row["first_load_at"],
            last_load_at=row["last_load_at"],
        )

    def compute_carrier_stats(
        self, *, tier: str, origin_key: str, dest_key: str, equipment: str
    ) -> list[CarrierStats]:
        """Recompute every carrier's record on one lane key, from the same
        population :meth:`compute_lane_stats` uses.

        Same population deliberately: a carrier's lane experience has to be
        countable against the lane's own load count, or "ran 14 of the 22 loads
        on this lane" stops being true.

        The two on-time counts come out of one pass, so the numerator and its
        answerable denominator always describe the same rows. ``delivered_on_time``
        is a stored verdict written by :meth:`upsert_load` from the domain
        function — this query counts it, it does not re-derive it.
        """
        where, params = self._population(
            tier=tier, origin_key=origin_key, dest_key=dest_key, equipment=equipment
        )
        with self._cursor() as cur:
            cur.execute(
                "SELECT source_carrier_id, count(*) AS load_count,"
                " count(*) FILTER (WHERE delivered_on_time) AS on_time_count,"
                " count(*) FILTER (WHERE delivered_on_time IS NOT NULL)"
                "     AS on_time_eligible_count,"
                " avg(rate_per_mile) AS avg_rate_per_mile,"
                " min(delivery_actual_at) AS first_load_at,"
                " max(delivery_actual_at) AS last_load_at"
                " FROM loads" + where + " AND source_carrier_id IS NOT NULL"
                " GROUP BY source_carrier_id ORDER BY source_carrier_id",
                params,
            )
            rows = cur.fetchall()
        return [
            CarrierStats(
                source_carrier_id=row["source_carrier_id"],
                tier=tier,
                lane_key=f"{origin_key}->{dest_key}",
                equipment=equipment,
                load_count=row["load_count"],
                on_time_count=row["on_time_count"],
                on_time_eligible_count=row["on_time_eligible_count"],
                avg_rate_per_mile=_f(row["avg_rate_per_mile"]),
                first_load_at=row["first_load_at"],
                last_load_at=row["last_load_at"],
            )
            for row in rows
        ]

    def latest_delivery_position(
        self, source_carrier_id: str
    ) -> tuple[float, float, datetime] | None:
        """This carrier's most recent delivery that we can point at on a map.

        The deadhead signal needs coordinates, so a geo-null delivery cannot be
        the answer — the last *known* position is then the most recent one we
        could place, which is stale by a load rather than absent. All three
        columns come from that single load, so the reason a rep reads
        ("delivered in Baytown yesterday, 38 mi away") and the miles the score
        used always describe the same event.

        ``None`` when the carrier has delivered nothing placeable; ingestion
        writes that through as three NULLs rather than leaving a stale position
        standing after a correction.
        """
        with self._cursor() as cur:
            cur.execute(
                "SELECT delivery_lat, delivery_lon, delivery_actual_at FROM loads"
                " WHERE broker_id = %s AND source_carrier_id = %s"
                " AND status = ANY(%s) AND delivery_actual_at IS NOT NULL"
                " AND delivery_lat IS NOT NULL AND delivery_lon IS NOT NULL"
                " ORDER BY delivery_actual_at DESC, source_load_id DESC LIMIT 1",
                (
                    self._broker_id,
                    source_carrier_id,
                    [str(LoadStatus.DELIVERED), str(LoadStatus.COMPLETED)],
                ),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return (row["delivery_lat"], row["delivery_lon"], row["delivery_actual_at"])

    def delete_lane_stats(
        self, *, tier: str, origin_key: str, dest_key: str, equipment: str
    ) -> None:
        with self._cursor() as cur:
            cur.execute(
                "DELETE FROM lane_stats WHERE broker_id = %s AND tier = %s"
                " AND origin_key = %s AND dest_key = %s AND equipment = %s",
                (self._broker_id, tier, origin_key, dest_key, equipment),
            )

    def insert_lane_stats(self, stats: LaneStats) -> None:
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO lane_stats (broker_id, tier, origin_key, dest_key,"
                " equipment, load_count, rate_per_mile_p25, rate_per_mile_p50,"
                " rate_per_mile_p75, first_load_at, last_load_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    self._broker_id,
                    stats.tier,
                    stats.origin_key,
                    stats.dest_key,
                    stats.equipment,
                    stats.load_count,
                    stats.rate_per_mile_p25,
                    stats.rate_per_mile_p50,
                    stats.rate_per_mile_p75,
                    stats.first_load_at,
                    stats.last_load_at,
                ),
            )

    def get_lane_stats(
        self, *, tier: str, origin_key: str, dest_key: str, equipment: str
    ) -> LaneStats | None:
        with self._cursor() as cur:
            cur.execute(
                "SELECT tier, origin_key, dest_key, equipment, load_count,"
                " rate_per_mile_p25, rate_per_mile_p50, rate_per_mile_p75,"
                " first_load_at, last_load_at"
                " FROM lane_stats WHERE broker_id = %s AND tier = %s"
                " AND origin_key = %s AND dest_key = %s AND equipment = %s",
                (self._broker_id, tier, origin_key, dest_key, equipment),
            )
            row = cur.fetchone()
            return None if row is None else _lane_stats_from_row(row)

    def lane_stats_for(self, key: LaneKey) -> LaneStats | None:
        """:meth:`get_lane_stats` addressed by a :class:`LaneKey`.

        The tier walk (``app.domain.pricing``) holds keys, not four loose
        strings, and it reads through this as a plain callable — which is what
        keeps the walk itself a pure function with no idea a database exists.
        """
        return self.get_lane_stats(
            tier=key.tier,
            origin_key=key.origin_key,
            dest_key=key.dest_key,
            equipment=key.equipment,
        )

    def equipment_mix_for(self, key: LaneKey) -> dict[str, int]:
        """How one bucket's loads break down by equipment type (DECISIONS.md D15).

        Computed from :meth:`_population` — the same predicate
        :meth:`compute_lane_stats` counted with — so the mix always sums to the
        ``load_count`` the estimate reports. A different population here would
        let the provenance line name a pool that is not the one the median came
        from, which is the disagreement invariant 2 is about.

        Only interesting for a bucket whose ``equipment`` is ``ANY``: a filtered
        one returns a single entry by construction.
        """
        where, params = self._population(
            tier=key.tier,
            origin_key=key.origin_key,
            dest_key=key.dest_key,
            equipment=key.equipment,
        )
        with self._cursor() as cur:
            cur.execute(
                "SELECT equipment, count(*) AS load_count FROM loads" + where
                + " GROUP BY equipment ORDER BY count(*) DESC, equipment",
                params,
            )
            return {row["equipment"]: row["load_count"] for row in cur.fetchall()}

    def delete_carrier_stats(
        self, *, tier: str, lane_key: str, equipment: str
    ) -> None:
        """Drop every carrier's row for one dirty lane key, before rebuilding."""
        with self._cursor() as cur:
            cur.execute(
                "DELETE FROM carrier_stats WHERE broker_id = %s AND tier = %s"
                " AND lane_key = %s AND equipment = %s",
                (self._broker_id, tier, lane_key, equipment),
            )

    def insert_carrier_stats(self, stats: CarrierStats) -> None:
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO carrier_stats (broker_id, source_carrier_id, tier,"
                " lane_key, equipment, load_count, on_time_count,"
                " on_time_eligible_count, avg_rate_per_mile,"
                " first_load_at, last_load_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    self._broker_id,
                    stats.source_carrier_id,
                    stats.tier,
                    stats.lane_key,
                    stats.equipment,
                    stats.load_count,
                    stats.on_time_count,
                    stats.on_time_eligible_count,
                    stats.avg_rate_per_mile,
                    stats.first_load_at,
                    stats.last_load_at,
                ),
            )

    def list_carrier_stats(self, *, tier: str, lane_key: str, equipment: str) -> list[CarrierStats]:
        """Every carrier's record on one lane at one tier — the ranking input."""
        with self._cursor() as cur:
            cur.execute(
                "SELECT source_carrier_id, tier, lane_key, equipment, load_count,"
                " on_time_count, on_time_eligible_count, avg_rate_per_mile,"
                " first_load_at, last_load_at"
                " FROM carrier_stats WHERE broker_id = %s AND tier = %s"
                " AND lane_key = %s AND equipment = %s"
                " ORDER BY source_carrier_id",
                (self._broker_id, tier, lane_key, equipment),
            )
            return [_carrier_stats_from_row(row) for row in cur.fetchall()]

    # -- the ranking read side (Phase 6) -------------------------------------
    #
    # Read-only, and every one of them goes through the same binding as the
    # rest: a ranking is as tenant-confined as an ingest. The lane-scoped ones
    # take a LaneKey rather than four loose strings so a caller cannot ask for
    # a carrier's record on one bucket and the lane average on another.
    def lane_on_time_for(self, key: LaneKey) -> tuple[int, int]:
        """One lane bucket's on-time record: ``(on_time, answerable)``.

        The prior the ranking shrinks a carrier's on-time rate toward
        (DECISIONS.md D5). Computed from :meth:`_population` — the same
        predicate :meth:`compute_lane_stats` and :meth:`compute_carrier_stats`
        use — so the lane average and the carrier observations shrunk toward it
        always describe the same loads.

        The second element counts loads with a **verdict**, not loads: a rolling
        truck has no arrival and no verdict, and counting it in the denominator
        would drag the prior down by exactly the freight that is going fine
        (TASKS.md R1).
        """
        where, params = self._population(
            tier=key.tier,
            origin_key=key.origin_key,
            dest_key=key.dest_key,
            equipment=key.equipment,
        )
        with self._cursor() as cur:
            cur.execute(
                "SELECT count(*) FILTER (WHERE delivered_on_time) AS on_time_count,"
                " count(*) FILTER (WHERE delivered_on_time IS NOT NULL) AS eligible"
                " FROM loads" + where,
                params,
            )
            row = cur.fetchone()
        return (row["on_time_count"], row["eligible"])

    def carrier_equipment_loads(self) -> dict[str, dict[str, int]]:
        """How many loads of each equipment type every carrier has actually run.

        The equipment signal is broker-wide, not lane-scoped: "has this carrier
        ever hauled a reefer for me" is a fact about the carrier, and PRD
        section 8 scores it that way. ``RATED_STATUSES`` because a load that was
        only ever quoted is not a trailer anyone pulled.

        ``UNKNOWN`` appears as a key like any other type and is never merged
        into another one (invariant 5) — a load whose equipment nobody recorded
        is not evidence that the carrier owns a dry van. Scoring never looks it
        up, because a load whose *own* equipment is ``UNKNOWN`` is neutral by
        rule (DECISIONS.md D12).
        """
        with self._cursor() as cur:
            cur.execute(
                "SELECT source_carrier_id, equipment, count(*) AS load_count"
                " FROM loads WHERE broker_id = %s AND status = ANY(%s)"
                " AND source_carrier_id IS NOT NULL"
                " GROUP BY source_carrier_id, equipment",
                (self._broker_id, [str(s) for s in RATED_STATUSES]),
            )
            rows = cur.fetchall()
        counts: dict[str, dict[str, int]] = {}
        for row in rows:
            counts.setdefault(row["source_carrier_id"], {})[row["equipment"]] = row[
                "load_count"
            ]
        return counts

    def last_deliveries(self) -> dict[str, LastDelivery]:
        """Every carrier's most recent placeable delivery, keyed by carrier id.

        The bulk form of :meth:`latest_delivery_position` — same statuses, same
        "must have coordinates" filter, same ``(delivery_actual_at DESC,
        source_load_id DESC)`` tie-break — so the position the ranking scores
        from is by construction the one ingestion wrote to
        ``carriers.last_delivery_*``. It returns more: the delivering load's id
        and its last drop, so the deadhead reason can name the town without a
        second query that might land on a different load.

        A carrier with nothing placeable behind them is simply absent, which the
        deadhead signal renders as "no known last delivery" rather than as a
        distance of zero.
        """
        with self._cursor() as cur:
            cur.execute(
                "SELECT DISTINCT ON (source_carrier_id) source_carrier_id,"
                " source_load_id, delivery_lat, delivery_lon, delivery_actual_at,"
                " stops"
                " FROM loads"
                " WHERE broker_id = %s AND source_carrier_id IS NOT NULL"
                " AND status = ANY(%s) AND delivery_actual_at IS NOT NULL"
                " AND delivery_lat IS NOT NULL AND delivery_lon IS NOT NULL"
                " ORDER BY source_carrier_id, delivery_actual_at DESC,"
                " source_load_id DESC",
                (
                    self._broker_id,
                    [str(LoadStatus.DELIVERED), str(LoadStatus.COMPLETED)],
                ),
            )
            rows = cur.fetchall()
        deliveries: dict[str, LastDelivery] = {}
        for row in rows:
            stops = _stops_from_json(row["stops"])
            drop = next((s for s in reversed(stops) if s.is_drop), stops[-1])
            deliveries[row["source_carrier_id"]] = LastDelivery(
                source_carrier_id=row["source_carrier_id"],
                source_load_id=row["source_load_id"],
                lat=row["delivery_lat"],
                lon=row["delivery_lon"],
                at=row["delivery_actual_at"],
                location=drop.location,
            )
        return deliveries

    def latest_sync_at(self) -> datetime | None:
        """When this broker's newest ingested file was synced. ``None`` if none.

        The ranking's "as of" clock. Recency is measured against the freshest
        data we hold rather than against ``now()``, so an answer is a function
        of the fixture and stays reproducible — ``data/TRACEABILITY.md``'s
        expected scores are still the expected scores next month, and a demo
        does not silently decay as the wall clock moves away from day 11.
        """
        with self._cursor() as cur:
            cur.execute(
                "SELECT max(synced_at) AS latest FROM sync_files WHERE broker_id = %s",
                (self._broker_id,),
            )
            return cur.fetchone()["latest"]

    def ranking_inputs(
        self, key: LaneKey | None, *, as_of: date | None = None
    ) -> RankingInputs:
        """Everything :func:`~app.domain.scoring.rank_carriers` needs, in one go.

        ``key`` is the rung the tier walk accepted, or ``None`` when it accepted
        nothing — in which case there is no lane to have experience on and the
        lane-scoped reads are skipped rather than guessed at.

        ``as_of`` defaults to the Central date of the newest sync file (see
        :meth:`latest_sync_at`); a caller can override it, which is what makes
        "what would this have ranked on day 7" answerable without a clock. The
        fallback to today only fires for a broker with no ingested files, which
        also has no carriers to rank.
        """
        if as_of is None:
            latest = self.latest_sync_at()
            as_of = central_date(latest) if latest is not None else central_date(
                datetime.now(timezone.utc)
            )
        stats: dict[str, CarrierStats] = {}
        on_time: tuple[int, int] = (0, 0)
        if key is not None:
            stats = {
                row.source_carrier_id: row
                for row in self.list_carrier_stats(
                    tier=key.tier, lane_key=key.lane_key, equipment=key.equipment
                )
            }
            on_time = self.lane_on_time_for(key)
        return RankingInputs(
            as_of=as_of,
            carriers=tuple(self.list_carriers()),
            carrier_stats=stats,
            equipment_loads=self.carrier_equipment_loads(),
            last_deliveries=self.last_deliveries(),
            lane_on_time_count=on_time[0],
            lane_on_time_eligible_count=on_time[1],
        )

