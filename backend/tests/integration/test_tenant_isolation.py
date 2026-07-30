"""M4 -- repository tenant-isolation tests (TASKS.md M4, CLAUDE.md invariant 1).

Proves no path through :class:`BrokerRepository` -- or through the raw
connection it sits on -- reaches another broker's rows. Every assertion here
is numeric (row counts, field values, exact dataclass equality) rather than
"looks isolated by inspection".

Sections:

1. Raw-connection probes (barrier 3: RLS itself), reproducing what was
   verified by hand before this suite existed.
2. The headline property: broker A's answers are byte-for-byte unaffected by
   loading a broker B whose data is *deliberately* built to collide with A's
   on every natural key that isn't ``broker_id``.
3. Isolation on paths that aren't simple selects: aggregates, a join that
   omits its own ``broker_id`` equality, upserts, ``sync_events`` ordering,
   deletes.
4. Fail-closed behavior for an unknown broker, documented rather than assumed.
5. The ambient-credential / admin-escape-hatch split. This was found RED
   against `db.py` as it stood at the start of this suite (``DATABASE_URL``
   named the superuser); `builder`'s fix landed concurrently while this file
   was being written, so it is now green -- see the report for the timeline
   and what would make it red again.
"""

from __future__ import annotations

import psycopg
import pytest

from app.repository import db
from app.repository.broker_repository import BrokerRepository, UnknownBroker, broker_session
from app.domain.model import CarrierStats, EntityType, LaneStats

from .support import make_carrier, make_customer, make_load, utc

pytestmark = pytest.mark.usefixtures("clean_db")


# ---------------------------------------------------------------------------
# 1. Raw-connection probes -- reproducing the hand-verified behavior
# ---------------------------------------------------------------------------


def test_raw_unscoped_select_raises_insufficient_privilege(app_conn):
    """A fresh app-role connection has no broker bound; any tenant-table read
    must fail closed rather than return every broker's rows."""
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_conn.execute("SELECT broker_id, name FROM carriers")


def test_forged_broker_guc_does_not_survive_to_the_next_statement(app_conn):
    """Setting ``app.broker_id`` directly, without going through
    :func:`broker_session` (i.e. outside its transaction), does not bind
    anything durable: on an autocommit connection each statement is its own
    implicit transaction, so a ``set_config(..., true)`` (transaction-local)
    call is gone by the time the next statement runs."""
    app_conn.execute("SELECT set_config('app.broker_id', 'broker_b', true)")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_conn.execute("SELECT broker_id, name FROM carriers")


def test_forged_guc_then_cross_broker_update_raises(app_conn):
    app_conn.execute("SELECT set_config('app.broker_id', 'broker_b', true)")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_conn.execute("UPDATE carriers SET name = 'PWNED' WHERE broker_id = 'broker_b'")


def test_two_brokers_same_mc_number_stay_isolated(app_conn):
    """Two brokers, each holding a carrier under the same real-world MC
    number, must never see each other's row -- the cross-TMS-carrier scenario
    (PRD scenario 6) is exactly the case the tenant boundary must survive."""
    repo_a = BrokerRepository.for_broker(app_conn, "broker_a")
    repo_b = BrokerRepository.for_broker(app_conn, "broker_b")

    repo_a.upsert_carrier(make_carrier("CARR-A1", name="Lone Star Freight", mc_number="1346382"))
    repo_b.upsert_carrier(make_carrier("CARR-B1", name="Alamo Transport", mc_number="1346382"))

    carriers_a = repo_a.list_carriers()
    carriers_b = repo_b.list_carriers()

    assert [c.source_carrier_id for c in carriers_a] == ["CARR-A1"]
    assert [c.source_carrier_id for c in carriers_b] == ["CARR-B1"]
    assert carriers_a[0].name == "Lone Star Freight"
    assert carriers_b[0].name == "Alamo Transport"
    assert carriers_a[0].mc_number == carriers_b[0].mc_number == "1346382"


# ---------------------------------------------------------------------------
# 2. The headline property: numeric equivalence under a colliding broker B
# ---------------------------------------------------------------------------

_COLLIDING_LOAD_ID = "SHP-COLLIDE-0001"
_COLLIDING_CARRIER_ID = "CARR-COLLIDE"
_COLLIDING_CUSTOMER_ID = "CUST-COLLIDE"
_LANE_KEY = dict(tier="METRO", origin_key="DFW", dest_key="HOU", equipment="DRY_VAN")
_CARRIER_STAT_KEY = dict(tier="METRO", lane_key="DFW->HOU", equipment="DRY_VAN")


def _seed_colliding_broker(
    repo: BrokerRepository,
    *,
    carrier_rate: float,
    customer_rate: float,
    carrier_name: str,
    customer_name: str,
    lane_load_count: int,
    lane_p50: float,
) -> None:
    """Write one broker's version of data that collides with another
    broker's on every natural key *except* ``broker_id``: identical
    ``source_load_id``, identical ``source_carrier_id`` (and MC/DOT), the
    same lane key and carrier-stat lane key, and colliding customer id --
    but each broker's own money and counts, so a leak would visibly move
    the numbers rather than just the row's existence."""
    sync_file_id = repo.record_sync_file(
        "2026-07-06T00-00_sync.json", utc(2026, 7, 6), {"broker": repo.broker_id}
    )
    load = make_load(
        _COLLIDING_LOAD_ID,
        source_carrier_id=_COLLIDING_CARRIER_ID,
        source_customer_id=_COLLIDING_CUSTOMER_ID,
        carrier_rate=carrier_rate,
        customer_rate=customer_rate,
    )
    repo.upsert_load(load, last_seen_sync_at=utc(2026, 7, 6))
    repo.append_event(
        sync_file_id=sync_file_id,
        synced_at=utc(2026, 7, 6),
        entity_type=EntityType.LOAD,
        source_entity_id=_COLLIDING_LOAD_ID,
        source_load_id=_COLLIDING_LOAD_ID,
        raw_json={"carrier_rate": carrier_rate, "broker": repo.broker_id},
        event_seq=1,
    )
    repo.upsert_carrier(
        make_carrier(
            _COLLIDING_CARRIER_ID,
            name=carrier_name,
            mc_number="1346382",
            dot_number="998877",
        )
    )
    repo.upsert_customer(make_customer(_COLLIDING_CUSTOMER_ID, name=customer_name))
    repo.insert_lane_stats(
        LaneStats(
            **_LANE_KEY,
            load_count=lane_load_count,
            rate_per_mile_p25=lane_p50 - 0.20,
            rate_per_mile_p50=lane_p50,
            rate_per_mile_p75=lane_p50 + 0.20,
            first_load_at=utc(2026, 7, 6),
            last_load_at=utc(2026, 7, 6),
        )
    )
    repo.insert_carrier_stats(
        CarrierStats(
            source_carrier_id=_COLLIDING_CARRIER_ID,
            **_CARRIER_STAT_KEY,
            load_count=lane_load_count,
            on_time_count=lane_load_count - 1,
            on_time_eligible_count=lane_load_count,
            avg_rate_per_mile=lane_p50,
            first_load_at=utc(2026, 7, 6),
            last_load_at=utc(2026, 7, 6),
        )
    )


def test_numeric_equivalence_broker_a_exactly_unaffected_by_colliding_broker_b(app_conn):
    """Compute everything broker A can read; load a broker B built to collide
    on every key; recompute; assert exact, field-by-field equality.

    This is the property TASKS.md M4 actually asks for: "no path reaches
    cross-broker rows". A wrong-row bug here would show up as A's carrier
    rate, customer name, lane median, or carrier stats quietly taking on B's
    values -- not as an error.
    """
    repo_a = BrokerRepository.for_broker(app_conn, "broker_a")
    repo_b = BrokerRepository.for_broker(app_conn, "broker_b")

    _seed_colliding_broker(
        repo_a,
        carrier_rate=1800.0,
        customer_rate=2200.0,
        carrier_name="FreightFlow Preferred Carrier",
        customer_name="Broker A's Big Shipper",
        lane_load_count=12,
        lane_p50=1.80,
    )

    before_load = repo_a.get_load(_COLLIDING_LOAD_ID)
    before_list_loads = repo_a.list_loads()
    before_carrier = repo_a.get_carrier(_COLLIDING_CARRIER_ID)
    before_list_carriers = repo_a.list_carriers()
    before_customer = repo_a.get_customer(_COLLIDING_CUSTOMER_ID)
    before_lane = repo_a.get_lane_stats(**_LANE_KEY)
    before_carrier_stats = repo_a.list_carrier_stats(**_CARRIER_STAT_KEY)
    before_events = repo_a.events_for_load(_COLLIDING_LOAD_ID)

    # Sanity: the seed actually wrote what we think it did, before B ever loads.
    assert before_load is not None and before_load.carrier_rate == 1800.0
    assert before_lane is not None and before_lane.load_count == 12

    # Broker B: same load id, same carrier id (and MC/DOT), same customer id,
    # same lane key and carrier-stat key -- different money and counts.
    _seed_colliding_broker(
        repo_b,
        carrier_rate=999.0,
        customer_rate=1111.0,
        carrier_name="HaulDesk Rival Carrier",
        customer_name="Broker B's Big Shipper",
        lane_load_count=40,
        lane_p50=2.90,
    )

    after_load = repo_a.get_load(_COLLIDING_LOAD_ID)
    after_list_loads = repo_a.list_loads()
    after_carrier = repo_a.get_carrier(_COLLIDING_CARRIER_ID)
    after_list_carriers = repo_a.list_carriers()
    after_customer = repo_a.get_customer(_COLLIDING_CUSTOMER_ID)
    after_lane = repo_a.get_lane_stats(**_LANE_KEY)
    after_carrier_stats = repo_a.list_carrier_stats(**_CARRIER_STAT_KEY)
    after_events = repo_a.events_for_load(_COLLIDING_LOAD_ID)

    # Exact dataclass equality -- field by field, not "looks the same".
    assert after_load == before_load
    assert after_list_loads == before_list_loads
    assert after_carrier == before_carrier
    assert after_list_carriers == before_list_carriers
    assert after_customer == before_customer
    assert after_lane == before_lane
    assert after_carrier_stats == before_carrier_stats
    assert after_events == before_events

    # And B's own numbers are genuinely its own -- not a shared/merged row.
    b_load = repo_b.get_load(_COLLIDING_LOAD_ID)
    b_carrier = repo_b.get_carrier(_COLLIDING_CARRIER_ID)
    b_lane = repo_b.get_lane_stats(**_LANE_KEY)
    assert b_load.carrier_rate == 999.0
    assert b_carrier.name == "HaulDesk Rival Carrier"
    assert b_lane.load_count == 40
    assert repo_a.get_load(_COLLIDING_LOAD_ID).carrier_rate == 1800.0
    assert repo_a.get_carrier(_COLLIDING_CARRIER_ID).name == "FreightFlow Preferred Carrier"
    assert repo_a.get_lane_stats(**_LANE_KEY).load_count == 12


# ---------------------------------------------------------------------------
# 3. Paths that aren't simple selects
# ---------------------------------------------------------------------------


def test_aggregate_count_over_whole_table_is_scoped_to_bound_broker(app_conn):
    repo_a = BrokerRepository.for_broker(app_conn, "broker_a")
    repo_b = BrokerRepository.for_broker(app_conn, "broker_b")

    for i in range(3):
        repo_a.upsert_load(make_load(f"A-{i}"), last_seen_sync_at=utc(2026, 7, 6))
    for i in range(7):
        repo_b.upsert_load(make_load(f"B-{i}"), last_seen_sync_at=utc(2026, 7, 6))

    with broker_session(app_conn, "broker_a") as cur:
        cur.execute("SELECT count(*) AS n, sum(carrier_rate) AS total FROM loads")
        row = cur.fetchone()
    assert row["n"] == 3
    assert float(row["total"]) == pytest.approx(3 * 1800.0)

    with broker_session(app_conn, "broker_b") as cur:
        cur.execute("SELECT count(*) AS n, sum(carrier_rate) AS total FROM loads")
        row = cur.fetchone()
    assert row["n"] == 7
    assert float(row["total"]) == pytest.approx(7 * 1800.0)


def test_join_between_tenant_tables_without_its_own_broker_id_still_scoped(app_conn):
    """A join that forgets to equate ``broker_id`` on *both* sides is the
    easiest way to lose the boundary -- RLS must still confine each side to
    its own broker even though the join condition alone would happily match
    across brokers on a colliding ``source_carrier_id``."""
    repo_a = BrokerRepository.for_broker(app_conn, "broker_a")
    repo_b = BrokerRepository.for_broker(app_conn, "broker_b")

    shared_carrier_id = "CARR-SHARED"
    repo_a.upsert_carrier(make_carrier(shared_carrier_id, name="Broker A's Carrier", mc_number="111"))
    repo_b.upsert_carrier(make_carrier(shared_carrier_id, name="Broker B's Carrier", mc_number="222"))
    repo_a.upsert_load(
        make_load("SHARED-LOAD", source_carrier_id=shared_carrier_id),
        last_seen_sync_at=utc(2026, 7, 6),
    )

    with broker_session(app_conn, "broker_a") as cur:
        # Deliberately no `c.broker_id = l.broker_id` in the join condition --
        # only RLS on `carriers` keeps this from matching broker B's row.
        cur.execute(
            "SELECT l.source_load_id, c.name AS carrier_name, c.broker_id AS carrier_broker"
            " FROM loads l"
            " JOIN carriers c ON c.source_carrier_id = l.source_carrier_id"
            " WHERE l.broker_id = %s AND l.source_load_id = %s",
            ("broker_a", "SHARED-LOAD"),
        )
        rows = cur.fetchall()

    assert len(rows) == 1
    assert rows[0]["carrier_name"] == "Broker A's Carrier"
    assert rows[0]["carrier_broker"] == "broker_a"


def test_upsert_cannot_overwrite_another_brokers_row_on_a_colliding_natural_key(app_conn):
    """``upsert_carrier``'s ``ON CONFLICT (broker_id, source_carrier_id)``
    means a colliding ``source_carrier_id`` alone can never conflict across
    brokers -- confirm that holds for both the insert and the update half of
    the upsert."""
    repo_a = BrokerRepository.for_broker(app_conn, "broker_a")
    repo_b = BrokerRepository.for_broker(app_conn, "broker_b")

    shared_id = "CARR-UPSERT-COLLIDE"
    repo_a.upsert_carrier(make_carrier(shared_id, name="A original", mc_number="A-MC"))
    # B's first write of the same natural key: an INSERT from B's perspective,
    # which must not appear as an UPDATE of A's row.
    repo_b.upsert_carrier(make_carrier(shared_id, name="B original", mc_number="B-MC"))
    assert repo_a.get_carrier(shared_id).name == "A original"
    assert repo_b.get_carrier(shared_id).name == "B original"

    # B updates its own row on the same key -- must not touch A's.
    repo_b.upsert_carrier(make_carrier(shared_id, name="B updated", mc_number="B-MC-2"))
    assert repo_a.get_carrier(shared_id).name == "A original"
    assert repo_a.get_carrier(shared_id).mc_number == "A-MC"
    assert repo_b.get_carrier(shared_id).name == "B updated"


def test_sync_events_ordering_is_scoped_per_broker(app_conn):
    """``events_for_load`` orders by ``(synced_at, event_seq)`` -- the rebuild
    path Phase 4 will use. Two brokers appending events for the identical
    ``source_load_id``, with identical ``event_seq`` numbers and overlapping
    ``synced_at`` timestamps, must not merge or reorder across the boundary."""
    repo_a = BrokerRepository.for_broker(app_conn, "broker_a")
    repo_b = BrokerRepository.for_broker(app_conn, "broker_b")
    load_id = "SHP-ORDER-COLLIDE"

    file_a1 = repo_a.record_sync_file("2026-07-06T00-00_sync.json", utc(2026, 7, 6, 0), {"broker": "a"})
    file_a2 = repo_a.record_sync_file("2026-07-06T06-00_sync.json", utc(2026, 7, 6, 6), {"broker": "a"})
    file_b1 = repo_b.record_sync_file("2026-07-06T00-00_sync.json", utc(2026, 7, 6, 0), {"broker": "b"})
    file_b2 = repo_b.record_sync_file("2026-07-06T06-00_sync.json", utc(2026, 7, 6, 6), {"broker": "b"})

    repo_a.append_event(
        sync_file_id=file_a1, synced_at=utc(2026, 7, 6, 0), entity_type=EntityType.LOAD,
        source_entity_id=load_id, source_load_id=load_id, raw_json={"rev": "a1"}, event_seq=1,
    )
    repo_a.append_event(
        sync_file_id=file_a2, synced_at=utc(2026, 7, 6, 6), entity_type=EntityType.RATE_LINE,
        source_entity_id="RATE-A", source_load_id=load_id, raw_json={"rev": "a2"}, event_seq=1,
    )

    repo_b.append_event(
        sync_file_id=file_b1, synced_at=utc(2026, 7, 6, 0), entity_type=EntityType.LOAD,
        source_entity_id=load_id, source_load_id=load_id, raw_json={"rev": "b1"}, event_seq=1,
    )
    repo_b.append_event(
        sync_file_id=file_b2, synced_at=utc(2026, 7, 6, 6), entity_type=EntityType.RATE_LINE,
        source_entity_id="RATE-B", source_load_id=load_id, raw_json={"rev": "b2"}, event_seq=1,
    )
    repo_b.append_event(
        sync_file_id=file_b2, synced_at=utc(2026, 7, 6, 6), entity_type=EntityType.RATE_LINE,
        source_entity_id="RATE-B2", source_load_id=load_id, raw_json={"rev": "b3"}, event_seq=2,
    )

    events_a = repo_a.events_for_load(load_id)
    events_b = repo_b.events_for_load(load_id)

    assert [e.raw_json["rev"] for e in events_a] == ["a1", "a2"]
    assert [e.raw_json["rev"] for e in events_b] == ["b1", "b2", "b3"]
    assert len(events_a) == 2
    assert len(events_b) == 3


def test_delete_lane_stats_cannot_reach_another_brokers_row(app_conn):
    repo_a = BrokerRepository.for_broker(app_conn, "broker_a")
    repo_b = BrokerRepository.for_broker(app_conn, "broker_b")
    key = dict(tier="ZIP3", origin_key="750", dest_key="770", equipment="REEFER")

    repo_a.insert_lane_stats(
        LaneStats(**key, load_count=9, rate_per_mile_p25=1.0, rate_per_mile_p50=1.2,
                  rate_per_mile_p75=1.4, first_load_at=utc(2026, 7, 6), last_load_at=utc(2026, 7, 6))
    )
    repo_b.insert_lane_stats(
        LaneStats(**key, load_count=99, rate_per_mile_p25=9.0, rate_per_mile_p50=9.2,
                  rate_per_mile_p75=9.4, first_load_at=utc(2026, 7, 6), last_load_at=utc(2026, 7, 6))
    )

    repo_b.delete_lane_stats(**key)

    assert repo_b.get_lane_stats(**key) is None
    survivor = repo_a.get_lane_stats(**key)
    assert survivor is not None
    assert survivor.load_count == 9


def test_delete_carrier_stats_cannot_reach_another_brokers_row(app_conn):
    repo_a = BrokerRepository.for_broker(app_conn, "broker_a")
    repo_b = BrokerRepository.for_broker(app_conn, "broker_b")
    key = dict(tier="METRO", lane_key="DFW->HOU", equipment="DRY_VAN")

    repo_a.insert_carrier_stats(
        CarrierStats(source_carrier_id="CARR-X", **key, load_count=5, on_time_count=4,
                     on_time_eligible_count=5,
                     avg_rate_per_mile=1.5, first_load_at=utc(2026, 7, 6), last_load_at=utc(2026, 7, 6))
    )
    repo_b.insert_carrier_stats(
        CarrierStats(source_carrier_id="CARR-X", **key, load_count=50, on_time_count=45,
                     on_time_eligible_count=50,
                     avg_rate_per_mile=2.5, first_load_at=utc(2026, 7, 6), last_load_at=utc(2026, 7, 6))
    )

    repo_b.delete_carrier_stats(tier=key["tier"], lane_key=key["lane_key"], equipment=key["equipment"])

    assert repo_b.list_carrier_stats(**key) == []
    survivors = repo_a.list_carrier_stats(**key)
    assert len(survivors) == 1
    assert survivors[0].load_count == 5


# ---------------------------------------------------------------------------
# 4. Fail-closed behavior for an unknown broker
# ---------------------------------------------------------------------------
#
# NOTE: as of writing this test, ``BrokerRepository.__init__`` itself validates
# the broker (see its docstring: "there is no way to get a silently inert
# repository whose reads all come back empty"). That is *stricter* than what
# was true when this task was assigned -- `builder` closed the gap
# concurrently with this suite being written. Both construction paths below
# are asserted; the ask was to "assert whatever is true today so a future
# change to it is visible", which is what these do.


def test_for_broker_raises_unknown_broker_for_a_nonexistent_tenant(app_conn):
    """The documented entry point fails loudly."""
    with pytest.raises(UnknownBroker):
        BrokerRepository.for_broker(app_conn, "broker_zzz")


def test_direct_construction_also_raises_unknown_broker(app_conn):
    """The bare constructor validates too -- there is no lenient way in.
    (Historically the plan for this test was the opposite: that the bare
    constructor stayed silent. That is no longer true; see the module note
    above.)"""
    with pytest.raises(UnknownBroker):
        BrokerRepository(app_conn, "broker_zzz")


def test_broker_session_the_raw_mechanism_stays_unvalidated_and_reads_empty(app_conn):
    """:func:`broker_session` is documented as the one deliberate exception:
    the raw mechanism used to prove the database-level barrier, which a
    bogus broker id passes through to "sees nothing" rather than raising a
    Python-level :class:`UnknownBroker`. (The database itself does not
    object either -- ``app.broker_id`` is merely a filter value with no FK
    of its own; nothing here writes a row, which is the only place an
    unknown broker would be rejected, via the ``loads.broker_id`` FK.)"""
    with broker_session(app_conn, "broker_zzz") as cur:
        cur.execute("SELECT count(*) AS n FROM loads")
        assert cur.fetchone()["n"] == 0
        cur.execute("SELECT count(*) AS n FROM carriers")
        assert cur.fetchone()["n"] == 0


def test_write_for_an_unknown_broker_raises_fk_violation_when_it_reaches_the_db(app_conn):
    """A write for a broker that isn't in ``brokers`` cannot quietly succeed
    even at the raw-SQL layer: every tenant table's ``broker_id`` is
    ``REFERENCES brokers (id)``, so an insert fails loudly with a
    foreign-key violation rather than creating an orphaned row for a tenant
    that doesn't exist. (``BrokerRepository`` itself never reaches this --
    its constructor now rejects the unknown broker first -- so this is
    exercised through ``broker_session`` directly, the one path that still
    lets an unknown broker id through to the database.)"""
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with broker_session(app_conn, "broker_zzz") as cur:
            cur.execute(
                "INSERT INTO carriers (broker_id, source_carrier_id, name)"
                " VALUES (%s, %s, %s)",
                ("broker_zzz", "CARR-ORPHAN", "Nobody"),
            )


# ---------------------------------------------------------------------------
# 5. The ambient-credential / admin-escape-hatch split
# ---------------------------------------------------------------------------
#
# When this task was assigned, `db.py` had one credential: `DATABASE_URL`
# named the schema-owning superuser (`carrier`, NOSUPERUSER=False,
# NOBYPASSRLS=False), and `connect()` was the *only* thing that downgraded to
# `carrier_pool_app`. A plain `psycopg.connect(DATABASE_URL)` -- what any call
# site gets by reaching for the obvious env var instead of `db.connect()` --
# read across every broker with no binding at all. Verified RED against that
# code (see the report for the captured output).
#
# `builder` closed the gap concurrently with this suite being written:
# `DATABASE_URL` (`DEFAULT_DATABASE_URL`) now itself names `carrier_pool_app`
# (LOGIN, NOSUPERUSER, NOBYPASSRLS), and the superuser was renamed to a
# separately-named `ADMIN_DATABASE_URL` used only by `connect_admin()`. Both
# tests below are green against the current code.


def test_default_database_url_connection_cannot_read_unscoped_across_brokers():
    """Safe behavior for the app's default/ambient credential: a plain
    ``psycopg.connect(DATABASE_URL)`` -- no ``SET ROLE``, no
    ``db.connect()`` role-downgrade -- must still fail closed on an unscoped
    read, exactly like the hardened ``carrier_pool_app`` role does via
    ``db.connect()`` (see
    ``test_raw_unscoped_select_raises_insufficient_privilege`` above).

    This is the test the report calls "verified red, now green": at the
    start of this task, ``DEFAULT_DATABASE_URL`` resolved to the superuser
    and this assertion failed (the query returned rows instead of raising).
    It now resolves to ``carrier_pool_app`` directly, so the connection is
    unprivileged even without going through ``db.connect()``.
    """
    conn = psycopg.connect(db.DEFAULT_DATABASE_URL, autocommit=True)
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT broker_id FROM carriers")
    finally:
        conn.close()


def test_admin_database_url_is_the_one_named_escape_hatch_and_nothing_else_is(app_conn):
    """The superuser bypass still exists -- it has to, for
    :func:`~app.repository.db.bootstrap` to apply ``schema.sql`` -- but it
    must be confined to the one credential named for it
    (``ADMIN_DATABASE_URL`` / :func:`~app.repository.db.connect_admin`), and
    it must actually behave like a bypass there (documenting the limit
    rather than assuming it). Everything else -- the ambient
    ``DATABASE_URL`` credential and the ``carrier_pool_app`` role it names --
    must not carry it."""
    admin_conn_raw = psycopg.connect(db.DEFAULT_ADMIN_DATABASE_URL, autocommit=True)
    try:
        row = admin_conn_raw.execute(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
        ).fetchone()
        assert row == (True, True), (
            "connect_admin()'s credential is no longer a superuser/bypassrls "
            "role -- if this changed, bootstrap() may no longer be able to "
            "apply schema.sql, which is a different problem worth knowing about"
        )
        # And, being the escape hatch, it really does read across brokers
        # unscoped -- that's the limit this test exists to keep honest.
        repo_a = BrokerRepository.for_broker(app_conn, "broker_a")
        repo_b = BrokerRepository.for_broker(app_conn, "broker_b")
        repo_a.upsert_carrier(make_carrier("ADMIN-ESCAPE-A", name="A"))
        repo_b.upsert_carrier(make_carrier("ADMIN-ESCAPE-B", name="B"))
        seen = {
            r[0]
            for r in admin_conn_raw.execute(
                "SELECT broker_id FROM carriers WHERE source_carrier_id LIKE 'ADMIN-ESCAPE-%'"
            ).fetchall()
        }
        assert seen == {"broker_a", "broker_b"}
    finally:
        admin_conn_raw.close()

    row = psycopg.connect(db.DEFAULT_DATABASE_URL, autocommit=True)
    try:
        current_role = row.execute(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
        ).fetchone()
        assert current_role == (False, False), (
            "the ambient DATABASE_URL credential must never carry the bypass"
        )
    finally:
        row.close()
