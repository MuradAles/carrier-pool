"""The pool read path. Separate from :mod:`.broker_repository`, on purpose.

CLAUDE.md invariant 1 says one broker's data must never influence another's
answers. The shared carrier pool is the single, opt-in exception the README
allows, and DECISIONS.md D17 spends its length on making that exception small.
This module is the whole of it: every statement that reads across brokers is
here, and there are four of them.

**Why a second class rather than more methods on
:class:`~app.repository.broker_repository.BrokerRepository`.** That class is
the tenant-confined one, and its property is that a query it issues *cannot*
see another broker. Adding a cross-broker method to it would make the property
"most of its methods are confined", which is not a property. Here the reach is
the whole point, so it is named, isolated and small enough to read in one
sitting.

**Three things bound the reach.**

1. **Only ``pool_carrier_lane``.** No statement here names ``loads``,
   ``lane_stats``, ``customers``, ``sync_files`` or ``sync_events``, and no
   statement reads ``carrier_stats`` directly. The view is a projection over
   ``carriers`` and ``carrier_stats`` with an explicit column list and no money
   in it, owned by ``carrier_pool_reader``, which was never granted SELECT on
   ``carrier_stats.avg_rate_per_mile`` — so widening the view to leak a rate
   does not leak a rate, it stops working. See ``schema.sql``.

   The view also carries three conditions on the *reader*, so none of them is
   something this module could forget: it raises out of ``current_broker()``
   when no broker is bound, it returns nothing at all unless the reader is
   itself opted in, and it never returns the reader's own contribution. The
   Python below restates the last of those, in the same spirit as
   ``broker_repository``'s redundant ``broker_id = %s``: the code should be
   correct read on its own, with the database as the backstop rather than the
   only stop.

2. **The requester is bound at construction**, exactly as
   ``BrokerRepository`` binds one. No method takes a broker parameter, so no
   future method can forget to exclude the requester's own contribution or to
   check its opt-in.

3. **Everything still runs inside :func:`~.broker_repository.broker_session`.**
   The tenant tables this module does touch — ``pool_opt_in``, ``pool_audit``,
   ``carriers`` for the requester's own MC numbers — are RLS-filtered to the
   bound broker like every other read in the system. Only the view escapes
   that, because only the view is supposed to.

**MC/DOT never becomes a join path between two brokers' load data.** The one
place identity is resolved is :meth:`pool_carriers`, and it resolves it inside
the view (which matches a carrier to *its own broker's* stats and nothing else)
and against a list of the requester's own MC numbers read separately and passed
as a parameter. No statement in this module joins the view to a tenant table,
and ``broker_repository.py`` still contains ``mc_number`` in no ``WHERE`` and no
``JOIN`` at all.
"""

from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Iterator, Sequence

import psycopg

from ..domain.lanes import LaneKey
from ..domain.pool import POOL_TIERS, PoolCarrier, load_band, on_time_band
from .broker_repository import UnknownBroker, _unstorable, broker_session
from .db import get_broker

__all__ = ["PoolRepository"]


class PoolRepository:
    """The shared pool, read on behalf of exactly one broker.

    Construction validates the tenant and raises :class:`UnknownBroker`
    otherwise, on every path, for the same reason ``BrokerRepository`` does: a
    typo'd broker must not read as "this broker sees an empty pool".
    """

    __slots__ = ("_conn", "_broker_id")

    def __init__(self, conn: psycopg.Connection, broker_id: str) -> None:
        if _unstorable(broker_id) or get_broker(conn, broker_id) is None:
            raise UnknownBroker(broker_id)
        self._conn = conn
        self._broker_id = broker_id

    @classmethod
    def for_broker(cls, conn: psycopg.Connection, broker_id: str) -> PoolRepository:
        return cls(conn, broker_id)

    @property
    def broker_id(self) -> str:
        return self._broker_id

    @contextmanager
    def _cursor(self) -> Iterator[psycopg.Cursor]:
        with broker_session(self._conn, self._broker_id) as cur:
            yield cur

    # -- S1: the opt-in ------------------------------------------------------

    def is_opted_in(self) -> bool:
        """Is this broker in the pool? No row means no, which is the default.

        RLS confines the read to the bound broker's own row, so a broker cannot
        learn who else has joined by asking this — the pool answer itself
        reports how many *other* brokers contributed, in bands, which is the
        disclosure the opt-in actually makes.

        This is not the control. The reciprocity rule lives in the view's own
        ``WHERE`` clause, so a broker that has not joined reads an empty pool
        even through hand-written SQL; this method is what lets the answer
        *say* so rather than looking like a lane with nobody on it.
        """
        with self._cursor() as cur:
            cur.execute(
                "SELECT 1 FROM pool_opt_in WHERE broker_id = %s", (self._broker_id,)
            )
            return cur.fetchone() is not None

    def set_opted_in(self, opted_in: bool) -> bool:
        """Join or leave the pool. Returns the state afterwards.

        Leaving is a ``DELETE`` rather than a flag, so "not in the pool" has one
        representation and a broker that leaves stops contributing on the next
        read — there is no copy of its rows anywhere to expire (D17 rejects the
        physical ``pool_carriers`` table for exactly this reason).

        Idempotent in both directions: joining twice keeps the original
        ``opted_in_at``, so the opt-in date means when they joined.
        """
        with self._cursor() as cur:
            if opted_in:
                cur.execute(
                    "INSERT INTO pool_opt_in (broker_id) VALUES (%s)"
                    " ON CONFLICT (broker_id) DO NOTHING",
                    (self._broker_id,),
                )
            else:
                cur.execute(
                    "DELETE FROM pool_opt_in WHERE broker_id = %s", (self._broker_id,)
                )
        return opted_in

    # -- the audit row (D17's enumeration and timing mitigation) -------------

    def record_pool_read(self, source_load_id: str) -> None:
        """Append one row saying this broker asked the pool about this load.

        Append-only: ``carrier_pool_app`` holds SELECT and INSERT on
        ``pool_audit`` and nothing else, so there is no privilege with which to
        erase having asked. Nothing reads it automatically — D17 is explicit
        that a real deployment needs someone to read it — but a question asked
        two hundred times about the same load is the shape of an inference
        attack, and it has to be *recoverable* for that to ever be noticed.
        """
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO pool_audit (broker_id, source_load_id) VALUES (%s, %s)",
                (self._broker_id, source_load_id),
            )

    # -- S2/S3: the one cross-broker read ------------------------------------

    def _own_mc_numbers(self) -> list[str]:
        """The requester's own carriers' MC numbers, normalized as the view is.

        Read from ``carriers`` inside the binding, so this is the requester's
        own data and nothing else. It is used to *exclude*: the pool section is
        carriers a broker has never used, and a carrier it already has is not
        news — it is already in the ranking, scored from real numbers.

        Returned to Python and passed back as a parameter rather than joined,
        so no statement in this module joins the pool view to a tenant table.
        """
        with self._cursor() as cur:
            cur.execute(
                "SELECT DISTINCT upper(btrim(mc_number)) AS mc FROM carriers"
                " WHERE broker_id = %s AND mc_number IS NOT NULL"
                "   AND btrim(mc_number) <> ''",
                (self._broker_id,),
            )
            return [row["mc"] for row in cur.fetchall()]

    def pool_carriers(self, key: LaneKey) -> list[PoolCarrier]:
        """Every pool carrier on one lane that this broker has never used.

        One row per MC number. When several brokers run the same carrier, the
        bands published are **one contributor's** — the deepest relationship on
        this lane — rather than a function of all of them (see
        :mod:`app.domain.pool`). Depth and reliability then describe a single
        real relationship instead of two brokers' facts glued together, and one
        contributor's worth of information is disclosed instead of everybody's.

        The two fields that are existential by nature are the exceptions and
        are taken over every contributor: ``active_recently`` ("somebody in the
        pool has run them lately") and the count of contributing brokers.

        The band labels come back beside their ordinals and are checked against
        the domain's vocabulary by :func:`~app.domain.pool.load_band`, so a
        ``CASE`` in ``schema.sql`` that drifts from ``LOAD_BANDS`` raises
        instead of quietly scoring at the wrong floor.

        Nothing in the result set is a count, a rate, a date or a load id: the
        view has no such column, and :class:`~app.domain.pool.PoolCarrier` has
        no field one could be assigned to.
        """
        if key.tier not in POOL_TIERS:
            raise ValueError(
                f"tier {key.tier!r} does not cross the pool boundary; "
                f"expected one of {POOL_TIERS} (DECISIONS.md D17)"
            )
        own = self._own_mc_numbers()
        with self._cursor() as cur:
            cur.execute(
                # Columns are named rather than starred even here, where the
                # source is the projection and a star would be safe: the habit
                # is the control, and the one place it is allowed to lapse is
                # the place a future column gets picked up for free.
                "WITH lane AS ("
                "  SELECT contributor_broker_id, mc_number, dot_number, name,"
                "         phone, home_city, home_state, load_band,"
                "         load_band_rank, on_time_band, on_time_band_rank,"
                "         active_recently"
                "  FROM pool_carrier_lane"
                "  WHERE contributor_broker_id <> %s"
                "    AND tier = %s AND lane_key = %s AND equipment = %s"
                "    AND mc_number <> ALL(%s)"
                "), deepest AS ("
                # One row per carrier, taken whole: the deepest relationship on
                # this lane. Equal depth is broken by the *weakest* on-time
                # band, which keeps this module's one rule — never overstate a
                # carrier the requester has no evidence about — and then by
                # name, so two brokers spelling the same company differently
                # ("DELTA PRIME LLC" / "Delta Prime, L.L.C.") give a stable
                # answer rather than one that depends on scan order. A row with
                # no on-time band at all sorts last: "nothing has delivered" is
                # unknown rather than weak, and a real band says more.
                "  SELECT DISTINCT ON (mc_number) mc_number, dot_number, name,"
                "         phone, home_city, home_state, load_band,"
                "         load_band_rank, on_time_band, on_time_band_rank"
                "  FROM lane"
                "  ORDER BY mc_number, load_band_rank DESC,"
                "           on_time_band_rank DESC NULLS LAST,"
                "           name NULLS LAST, phone NULLS LAST"
                "), contributors AS ("
                "  SELECT mc_number,"
                "         count(DISTINCT contributor_broker_id) AS n,"
                "         bool_or(active_recently) AS active_recently"
                "  FROM lane GROUP BY mc_number"
                ")"
                " SELECT d.mc_number, d.dot_number, d.name, d.phone,"
                "        d.home_city, d.home_state, d.load_band,"
                "        d.load_band_rank, d.on_time_band, d.on_time_band_rank,"
                "        c.n AS contributor_count, c.active_recently"
                " FROM deepest d JOIN contributors c USING (mc_number)"
                " ORDER BY d.mc_number",
                (self._broker_id, key.tier, key.lane_key, key.equipment, own),
            )
            rows = cur.fetchall()
        if not rows:
            return []
        operated = self._equipment_operated([row["mc_number"] for row in rows])
        return [
            PoolCarrier(
                mc_number=row["mc_number"],
                dot_number=row["dot_number"],
                name=row["name"],
                phone=row["phone"],
                home_city=row["home_city"],
                home_state=row["home_state"],
                tier=key.tier,
                lane_key=key.lane_key,
                equipment=key.equipment,
                load_band=load_band(row["load_band_rank"], row["load_band"]),
                on_time_band=on_time_band(
                    row["on_time_band_rank"], row["on_time_band"]
                ),
                active_recently=row["active_recently"],
                equipment_operated=operated.get(row["mc_number"], ()),
                contributor_count=row["contributor_count"],
            )
            for row in rows
        ]

    def _equipment_operated(
        self, mc_numbers: Sequence[str]
    ) -> dict[str, tuple[str, ...]]:
        """Trailer types each of these carriers runs anywhere in the pool.

        Broker-wide rather than lane-scoped, like
        :meth:`~app.repository.BrokerRepository.carrier_equipment_loads`: "does
        this carrier own a reefer" is a fact about the carrier.

        Read from the same view, so it inherits the same suppression — a
        trailer type they have run fewer than five times on every lane does not
        appear. That understates a fleet and never overstates one, which is the
        direction this whole module rounds in.

        ``ANY`` is excluded: it is the mixed-pool marker (D6), not a trailer.
        ``UNKNOWN`` is kept as itself and never merged into ``DRY_VAN``
        (invariant 5) — it just never matches a load, since a load whose own
        equipment is ``UNKNOWN`` is scored neutral before this is consulted.
        """
        with self._cursor() as cur:
            cur.execute(
                "SELECT mc_number, array_agg(DISTINCT equipment) AS equipment"
                " FROM pool_carrier_lane"
                " WHERE contributor_broker_id <> %s AND mc_number = ANY(%s)"
                "   AND equipment <> 'ANY'"
                " GROUP BY mc_number",
                (self._broker_id, list(mc_numbers)),
            )
            return {
                row["mc_number"]: tuple(sorted(row["equipment"]))
                for row in cur.fetchall()
            }
