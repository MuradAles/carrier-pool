"""S5 -- shared carrier pool leak tests (TASKS.md S5, DECISIONS.md D17).

Repository/domain level, against real Postgres, with synthetic data under
per-test truncation -- the mirror of ``test_tenant_isolation.py`` for the one
place CLAUDE.md invariant 1 deliberately bends. ``test_pool_corpus.py`` is the
HTTP-level companion, run against the real 132-file corpus, and is where the
two genuine cross-broker carriers (D17: MC 1346382, MC 884201) and the
opt-out-is-byte-identical property are checked against real numbers.

Every assertion here is field-by-field and numeric: dataclass-field equality,
DB catalog column lists, exact band values, exact row counts. "No leak
observed" by inspection is not a result the team lead will accept, and it
should not be one we produce either.

Fixture strategy: ``conftest.py``'s autouse ``clean_db`` truncates the seven
original tenant tables before/after every test; :func:`clean_pool_tables`
below extends that to the two Phase 11 tables, which ``clean_db`` predates and
does not know about.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date

import psycopg
import pytest

from app.api.schemas import PoolSectionOut
from app.domain.lanes import TIER_METRO, LaneKey
from app.domain.model import CarrierStats, LoadStatus
from app.domain.pool import (
    LOAD_BAND_FLOOR,
    ON_TIME_BAND_FLOOR,
    POOL_CARRIER_FIELDS,
    PoolCarrier,
    build_pool_section,
)
from app.repository import BrokerRepository, PoolRepository, UnknownBroker
from app.repository.broker_repository import broker_session

from .support import make_carrier, make_load, utc

pytestmark = pytest.mark.usefixtures("clean_db")

# One fixed lane every synthetic test seeds onto, unless it says otherwise.
_TIER = TIER_METRO
_LANE_KEY = "DFW->HOU"
_EQUIPMENT = "DRY_VAN"
_KEY = LaneKey(tier=_TIER, origin_key="DFW", dest_key="HOU", equipment=_EQUIPMENT)

# A distinctive, never-real rate. If this string turns up anywhere in a pool
# JSON payload, a rate crossed the boundary.
_SENTINEL_RATE = 9.9999


@pytest.fixture(autouse=True)
def clean_pool_tables(admin_conn: psycopg.Connection) -> Iterator[None]:
    """Truncate the two Phase 11 tables ``conftest.py`` doesn't know about."""
    admin_conn.execute("TRUNCATE pool_opt_in, pool_audit RESTART IDENTITY CASCADE")
    yield
    admin_conn.execute("TRUNCATE pool_opt_in, pool_audit RESTART IDENTITY CASCADE")


def _seed(
    repo: BrokerRepository,
    *,
    source_carrier_id: str,
    mc_number: str,
    name: str,
    load_count: int,
    on_time_count: int,
    on_time_eligible_count: int,
    last_load_at,
    tier: str = _TIER,
    lane_key: str = _LANE_KEY,
    equipment: str = _EQUIPMENT,
    dot_number: str | None = "998877",
    home_city: str | None = "Dallas",
    home_state: str | None = "TX",
    avg_rate_per_mile: float = _SENTINEL_RATE,
) -> None:
    """Write one carrier + one carrier_stats row -- one pool row's worth."""
    repo.upsert_carrier(
        make_carrier(
            source_carrier_id,
            name=name,
            mc_number=mc_number,
            dot_number=dot_number,
            home_city=home_city,
            home_state=home_state,
        )
    )
    repo.insert_carrier_stats(
        CarrierStats(
            source_carrier_id=source_carrier_id,
            tier=tier,
            lane_key=lane_key,
            equipment=equipment,
            load_count=load_count,
            on_time_count=on_time_count,
            on_time_eligible_count=on_time_eligible_count,
            avg_rate_per_mile=avg_rate_per_mile,
            first_load_at=utc(2026, 7, 6),
            last_load_at=last_load_at,
        )
    )


def _opt_in(conn: psycopg.Connection, *brokers: str) -> None:
    for broker_id in brokers:
        PoolRepository.for_broker(conn, broker_id).set_opted_in(True)


# ---------------------------------------------------------------------------
# 1. The boundary itself -- dataclass equality and the DB catalog
# ---------------------------------------------------------------------------


def test_pool_carrier_dataclass_fields_equal_d17_boundary_exactly() -> None:
    """Equality, not containment: a new field must be justified in D17's
    table first, or this fails at import (``app/domain/pool.py`` asserts it
    too -- this is the belt-and-braces copy so a future edit to either one
    that removes the assertion still gets caught by the test suite)."""
    assert set(PoolCarrier.__dataclass_fields__) == POOL_CARRIER_FIELDS


def test_pool_carrier_lane_view_column_list_matches_the_boundary_exactly(
    admin_conn: psycopg.Connection,
) -> None:
    """The view's *output* columns -- what a caller could possibly select.

    Not a filter over the full record: a filter is one forgotten ``SELECT *``
    away from a leak. This asserts the projection has no rate, customer,
    shipment or exact-count column to forget in the first place.
    """
    rows = admin_conn.execute(
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_name = 'pool_carrier_lane'"
    ).fetchall()
    columns = {r[0] for r in rows}

    forbidden = {
        "carrier_rate", "rate_per_mile", "avg_rate_per_mile", "customer_rate",
        "customer_name", "source_customer_id", "source_load_id", "load_number",
        "stops", "cargo", "weight_lbs", "distance_miles", "pickup_zip3",
        "delivery_zip3", "last_delivery_lat", "last_delivery_lon",
        "last_delivery_at", "load_count", "on_time_count",
        "on_time_eligible_count", "raw_json",
    }
    assert columns.isdisjoint(forbidden)

    # And the columns present are exactly D17's crossing set, plus the one
    # internal column (contributor_broker_id) the read path uses to
    # self-exclude and never copies into PoolCarrier.
    assert columns == {
        "contributor_broker_id", "mc_number", "dot_number", "name", "phone",
        "home_city", "home_state", "tier", "lane_key", "equipment",
        "load_band", "load_band_rank", "on_time_band", "on_time_band_rank",
        "active_recently",
    }


def test_pool_carrier_lane_view_depends_on_no_forbidden_column(
    admin_conn: psycopg.Connection,
) -> None:
    """``pg_depend``/``pg_attribute``: every column the view's *query* reads,
    whether or not it is ever projected out.

    This is the check D17 asks for that a test against the JSON response
    would miss: a column consulted in a ``WHERE`` or a ``CASE`` and then
    discarded never shows up in ``information_schema.columns`` for the view,
    but it is still a place a rate or an exact count could have leaked in
    before being (correctly) thrown away -- or could start leaking if someone
    "simplified" the ``CASE`` into a raw passthrough.
    """
    rows = admin_conn.execute(
        """
        SELECT DISTINCT dep_table.relname AS table_name,
               dep_col.attname AS column_name
        FROM pg_depend d
        JOIN pg_rewrite r ON r.oid = d.objid
        JOIN pg_class view ON view.oid = r.ev_class
        JOIN pg_class dep_table ON dep_table.oid = d.refobjid
        JOIN pg_attribute dep_col
          ON dep_col.attrelid = d.refobjid AND dep_col.attnum = d.refobjsubid
        WHERE view.relname = 'pool_carrier_lane'
          AND d.deptype = 'n'
          AND dep_col.attnum > 0
        """
    ).fetchall()
    used = {(table, column) for table, column in rows}

    tables_touched = {table for table, _ in used}
    assert tables_touched <= {"carriers", "carrier_stats", "pool_opt_in"}, (
        "pool_carrier_lane must depend on nothing but carriers, carrier_stats "
        f"and pool_opt_in -- it also touches {tables_touched - {'carriers', 'carrier_stats', 'pool_opt_in'}}"
    )

    forbidden = {
        ("loads", "carrier_rate"), ("loads", "rate_per_mile"),
        ("loads", "customer_rate"), ("loads", "source_customer_id"),
        ("loads", "source_load_id"), ("loads", "pickup_zip3"),
        ("loads", "delivery_zip3"),
        ("carrier_stats", "avg_rate_per_mile"),
        ("carriers", "last_delivery_lat"), ("carriers", "last_delivery_lon"),
        ("carriers", "last_delivery_at"),
    }
    assert used.isdisjoint(forbidden), used & forbidden


def test_carrier_pool_reader_column_grants_exclude_every_money_column(
    admin_conn: psycopg.Connection,
) -> None:
    """The grant is the binding constraint, not the view text (D17): even if
    someone edited the view to select a rate, ``carrier_pool_reader`` was
    never granted that column, so the view would fail to (re)create rather
    than leak.

    Checked directly against ``information_schema.column_privileges`` rather
    than trusting the hand-verified probes below alone."""
    rows = admin_conn.execute(
        "SELECT table_name, column_name FROM information_schema.column_privileges"
        " WHERE grantee = 'carrier_pool_reader' AND privilege_type = 'SELECT'"
    ).fetchall()
    granted = {(t, c) for t, c in rows}
    assert ("carrier_stats", "avg_rate_per_mile") not in granted
    assert ("carriers", "last_delivery_lat") not in granted
    assert ("carriers", "last_delivery_lon") not in granted
    assert ("carriers", "last_delivery_at") not in granted
    # And it holds nothing at all on loads, lane_stats, customers, the sync
    # tables, or pool_audit. ``pool_carrier_lane`` itself is expected --
    # carrier_pool_reader owns that view, so it naturally carries every
    # privilege on it.
    tables_granted = {t for t, _ in granted}
    assert tables_granted <= {"carriers", "carrier_stats", "pool_opt_in", "pool_carrier_lane"}


# ---------------------------------------------------------------------------
# 2. Field-by-field on a real, fully-scored pool carrier
# ---------------------------------------------------------------------------


def test_serialized_pool_carrier_has_every_shareable_field_and_no_forbidden_one(
    app_conn: psycopg.Connection,
) -> None:
    """The headline S5 assertion, built through the real path -- seed via
    ``BrokerRepository`` -> read via ``PoolRepository.pool_carriers`` -> score
    via ``domain.pool`` -> serialize via ``api.schemas`` -- and inspect the
    actual dict FastAPI would emit, not the type.
    """
    repo_a = BrokerRepository.for_broker(app_conn, "broker_a")
    _opt_in(app_conn, "broker_a", "broker_c")
    _seed(
        repo_a,
        source_carrier_id="CARR-POOL-A",
        mc_number="1346382",
        name="Ibrahim-Like Carrier",
        dot_number="3771394",
        load_count=22,
        on_time_count=20,
        on_time_eligible_count=22,
        last_load_at=utc(2026, 7, 15),
    )

    load = make_load("C-ACTIVE-1", status=LoadStatus.ACTIVE)
    pool_c = PoolRepository.for_broker(app_conn, "broker_c")
    section = build_pool_section(
        load, as_of=date(2026, 7, 16), opted_in=True, carriers_for=pool_c.pool_carriers
    )
    assert len(section.carriers) == 1
    payload = PoolSectionOut.of(section).model_dump()
    carrier_payload = payload["carriers"][0]["carrier"]

    expected_present = {
        "mc_number", "dot_number", "name", "phone", "home_city", "home_state",
        "tier", "lane_key", "equipment", "load_band", "on_time_band",
        "active_recently", "equipment_operated", "contributor_count",
    }
    assert set(carrier_payload) == expected_present

    assert carrier_payload["mc_number"] == "1346382"
    assert carrier_payload["load_band"] == "20-49"
    assert carrier_payload["on_time_band"] == "90+"
    assert carrier_payload["contributor_count"] == 1
    assert carrier_payload["equipment_operated"] == ["DRY_VAN"]

    # Every string in the whole section, scanned for anything that must never
    # have crossed: the sentinel rate, the broker's internal ids, the exact
    # counts and the exact on-time ratio.
    forbidden_probe = repr(payload)
    for leak in (
        str(_SENTINEL_RATE), "CARR-POOL-A",
        "20/22", "0.909", "22 loads", "20 loads",
    ):
        assert leak not in forbidden_probe, f"{leak!r} leaked into the pool payload"


@pytest.mark.parametrize(
    "load_count, expected_band",
    [
        (4, None),
        (5, "5-9"),
        (9, "5-9"),
        (10, "10-19"),
        (19, "10-19"),
        (20, "20-49"),
        (49, "20-49"),
        (50, "50+"),
        (200, "50+"),
    ],
)
def test_load_band_suppression_and_every_boundary(
    app_conn: psycopg.Connection, load_count: int, expected_band: str | None
) -> None:
    """A carrier below 5 loads has **no row at all** -- not a suppressed
    count, an absent one -- and every boundary above it lands in the band
    CLAUDE.md's minimum sample and D17's floor both name."""
    repo_a = BrokerRepository.for_broker(app_conn, "broker_a")
    _opt_in(app_conn, "broker_a", "broker_b")
    _seed(
        repo_a,
        source_carrier_id="CARR-BAND",
        mc_number=f"5{load_count:06d}",
        name="Band Test Carrier",
        load_count=load_count,
        on_time_count=load_count,
        on_time_eligible_count=load_count,
        last_load_at=utc(2026, 7, 15),
    )
    rows = PoolRepository.for_broker(app_conn, "broker_b").pool_carriers(_KEY)
    if expected_band is None:
        assert rows == [], (
            f"a {load_count}-load carrier must have no row in the pool at "
            f"all -- there is nothing to un-bucket -- got {rows}"
        )
    else:
        assert len(rows) == 1
        assert rows[0].load_band == expected_band
        assert LOAD_BAND_FLOOR[expected_band] <= load_count


@pytest.mark.parametrize(
    "on_time_count, on_time_eligible_count, expected_band",
    [
        (0, 0, None),  # nothing delivered yet -- a real third state, not "<75"
        (18, 20, "90+"),  # exactly the 0.90 boundary
        (17, 20, "75-89"),  # 0.85
        (15, 20, "75-89"),  # exactly the 0.75 boundary
        (14, 20, "<75"),  # 0.70
        (0, 20, "<75"),
    ],
)
def test_on_time_band_boundaries_and_the_no_verdict_third_state(
    app_conn: psycopg.Connection,
    on_time_count: int,
    on_time_eligible_count: int,
    expected_band: str | None,
) -> None:
    repo_a = BrokerRepository.for_broker(app_conn, "broker_a")
    _opt_in(app_conn, "broker_a", "broker_b")
    _seed(
        repo_a,
        source_carrier_id="CARR-ONTIME",
        mc_number=f"6{on_time_count:03d}{on_time_eligible_count:03d}",
        name="On-Time Test Carrier",
        load_count=20,
        on_time_count=on_time_count,
        on_time_eligible_count=on_time_eligible_count,
        last_load_at=utc(2026, 7, 15),
    )
    rows = PoolRepository.for_broker(app_conn, "broker_b").pool_carriers(_KEY)
    assert len(rows) == 1
    assert rows[0].on_time_band == expected_band
    if expected_band is not None:
        ratio = on_time_count / on_time_eligible_count
        assert ratio >= ON_TIME_BAND_FLOOR[expected_band]
    # However precise the true ratio, only the band ever crosses.
    payload_probe = repr(rows[0])
    if on_time_eligible_count:
        exact_fraction = f"{on_time_count}/{on_time_eligible_count}"
        assert exact_fraction not in payload_probe


# ---------------------------------------------------------------------------
# 3. Several contributors: the deepest relationship, never a merge
# ---------------------------------------------------------------------------


def test_two_contributors_publish_the_deepest_relationship_not_a_merge(
    app_conn: psycopg.Connection,
) -> None:
    repo_a = BrokerRepository.for_broker(app_conn, "broker_a")
    repo_b = BrokerRepository.for_broker(app_conn, "broker_b")
    _opt_in(app_conn, "broker_a", "broker_b", "broker_c")
    _seed(
        repo_a,
        source_carrier_id="CARR-A",
        mc_number="8123456",
        name="Broker A Spelling LLC",
        load_count=22,
        on_time_count=20,
        on_time_eligible_count=22,
        last_load_at=utc(2026, 6, 1),  # older
    )
    _seed(
        repo_b,
        source_carrier_id="CARR-B",
        mc_number="8123456",
        name="Broker B Spelling, L.L.C.",
        load_count=6,  # clears the 5-load floor, but far shallower than A's 22
        on_time_count=6,
        on_time_eligible_count=6,
        last_load_at=utc(2026, 7, 15),  # recent
    )

    rows = PoolRepository.for_broker(app_conn, "broker_c").pool_carriers(_KEY)
    assert len(rows) == 1
    carrier = rows[0]
    assert carrier.load_band == "20-49"  # A's 22, the deepest -- never merged to 28
    assert carrier.on_time_band == "90+"  # A's ratio, not B's 100%
    assert carrier.contributor_count == 2  # existential: both contribute
    assert carrier.active_recently is True  # existential: *someone* ran recently (B)


def test_equal_depth_band_breaks_toward_the_weaker_on_time_band(
    app_conn: psycopg.Connection,
) -> None:
    """Same band (``10-19``), different exact counts (15 vs 12) -- the module
    breaks the tie on rank, not count, and toward the weaker on-time band."""
    repo_a = BrokerRepository.for_broker(app_conn, "broker_a")
    repo_b = BrokerRepository.for_broker(app_conn, "broker_b")
    _opt_in(app_conn, "broker_a", "broker_b", "broker_c")
    _seed(
        repo_a,
        source_carrier_id="CARR-TIE-A",
        mc_number="8200000",
        name="Tie Carrier A",
        load_count=15,
        on_time_count=15,
        on_time_eligible_count=15,  # 100%
        last_load_at=utc(2026, 7, 15),
    )
    _seed(
        repo_b,
        source_carrier_id="CARR-TIE-B",
        mc_number="8200000",
        name="Tie Carrier B",
        load_count=12,
        on_time_count=6,
        on_time_eligible_count=12,  # 50%, weaker
        last_load_at=utc(2026, 7, 15),
    )
    rows = PoolRepository.for_broker(app_conn, "broker_c").pool_carriers(_KEY)
    assert len(rows) == 1
    assert rows[0].load_band == "10-19"
    assert rows[0].on_time_band == "<75"  # the weaker contributor's band wins the tie


# ---------------------------------------------------------------------------
# 4. Self-exclusion
# ---------------------------------------------------------------------------


def test_broker_never_sees_its_own_contribution_as_pool_data(
    app_conn: psycopg.Connection,
) -> None:
    repo_a = BrokerRepository.for_broker(app_conn, "broker_a")
    _opt_in(app_conn, "broker_a")
    _seed(
        repo_a,
        source_carrier_id="CARR-SELF",
        mc_number="7000001",
        name="Self Carrier",
        load_count=25,
        on_time_count=25,
        on_time_eligible_count=25,
        last_load_at=utc(2026, 7, 15),
    )
    assert PoolRepository.for_broker(app_conn, "broker_a").pool_carriers(_KEY) == []


def test_a_carrier_the_requester_already_uses_is_excluded_even_from_a_different_lane(
    app_conn: psycopg.Connection,
) -> None:
    """A broker's own carrier never comes back as a *pool* fact, even on a
    lane that broker has never itself run it on -- the exclusion is on the
    MC number globally (``_own_mc_numbers``), matching D17's "carriers it has
    never used"."""
    repo_a = BrokerRepository.for_broker(app_conn, "broker_a")
    repo_b = BrokerRepository.for_broker(app_conn, "broker_b")
    _opt_in(app_conn, "broker_a", "broker_b")

    # broker_a already knows this carrier, but only on a different lane.
    repo_a.upsert_carrier(
        make_carrier("CARR-OWN-ELSEWHERE", name="Own Carrier", mc_number="7100001")
    )
    # broker_b runs the same MC number, richly, on the lane under test.
    _seed(
        repo_b,
        source_carrier_id="CARR-OWN-ELSEWHERE-B",
        mc_number="7100001",
        name="Same Carrier, Broker B's Relationship",
        load_count=30,
        on_time_count=30,
        on_time_eligible_count=30,
        last_load_at=utc(2026, 7, 15),
    )
    assert PoolRepository.for_broker(app_conn, "broker_a").pool_carriers(_KEY) == []


# ---------------------------------------------------------------------------
# 5. Reciprocity, fail-closed behavior, and the reader role's own limits
# ---------------------------------------------------------------------------


def test_broker_not_opted_in_sees_nothing_even_via_hand_written_sql(
    app_conn: psycopg.Connection,
) -> None:
    repo_a = BrokerRepository.for_broker(app_conn, "broker_a")
    _opt_in(app_conn, "broker_a")  # broker_c deliberately never opts in
    _seed(
        repo_a,
        source_carrier_id="CARR-RECIP",
        mc_number="9000001",
        name="Reciprocity Carrier",
        load_count=25,
        on_time_count=25,
        on_time_eligible_count=25,
        last_load_at=utc(2026, 7, 15),
    )
    with broker_session(app_conn, "broker_c") as cur:
        cur.execute("SELECT * FROM pool_carrier_lane")
        assert cur.fetchall() == []
    assert PoolRepository.for_broker(app_conn, "broker_c").pool_carriers(_KEY) == []


def test_pool_view_raises_no_broker_bound_outside_any_session(
    app_conn: psycopg.Connection,
) -> None:
    """``current_broker()`` fails closed for the view exactly like it does
    for every other tenant table -- invariant 1 still applies to the one
    cross-broker reader."""
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="no broker bound"):
        app_conn.execute("SELECT * FROM pool_carrier_lane")


def test_carrier_pool_reader_role_cannot_reach_money_even_directly(
    admin_conn: psycopg.Connection,
) -> None:
    """Reproduces, as assertions, what was verified by hand holding
    ``carrier_pool_reader`` directly: no grant on ``loads`` at all, no
    ``avg_rate_per_mile`` on ``carrier_stats``, no bare ``SELECT *`` on
    ``carrier_stats`` either (the forgotten-star case) -- and invariant 1
    still applies, so even this role gets "no broker bound" reading the view
    unbound."""
    admin_conn.execute("SET ROLE carrier_pool_reader")
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            admin_conn.execute("SELECT carrier_rate FROM loads")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            admin_conn.execute("SELECT avg_rate_per_mile FROM carrier_stats")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            admin_conn.execute("SELECT * FROM carrier_stats")
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="no broker bound"):
            admin_conn.execute("SELECT * FROM pool_carrier_lane")
    finally:
        admin_conn.execute("RESET ROLE")


def test_pool_repository_raises_unknown_broker_for_a_nonexistent_tenant(
    app_conn: psycopg.Connection,
) -> None:
    with pytest.raises(UnknownBroker):
        PoolRepository.for_broker(app_conn, "broker_zzz")


# ---------------------------------------------------------------------------
# 6. MC/DOT must not become a join path into another broker's loads
# ---------------------------------------------------------------------------


def test_mc_dot_is_not_a_join_path_into_another_brokers_loads(
    app_conn: psycopg.Connection,
) -> None:
    """The one place identity is resolved is the view itself, matching a
    carrier to *its own broker's* stats. Joining the view back to
    ``carriers``/``loads`` inside a broker's own session must still be
    filtered by RLS on those tables -- the shared MC number cannot be used
    to reach the other broker's rate.
    """
    repo_a = BrokerRepository.for_broker(app_conn, "broker_a")
    repo_c = BrokerRepository.for_broker(app_conn, "broker_c")
    _opt_in(app_conn, "broker_a", "broker_c")

    _seed(
        repo_a,
        source_carrier_id="CARR-JOIN-A",
        mc_number="1346382",
        name="Ibrahim-Like",
        load_count=22,
        on_time_count=20,
        on_time_eligible_count=22,
        last_load_at=utc(2026, 7, 15),
    )
    repo_a.upsert_load(
        make_load("A-SECRET-LOAD", source_carrier_id="CARR-JOIN-A", carrier_rate=9999.99),
        last_seen_sync_at=utc(2026, 7, 15),
    )

    repo_c.upsert_carrier(make_carrier("CARR-JOIN-C", name="Ibrahim, Inc.", mc_number="1346382"))
    repo_c.upsert_load(
        make_load("C-OWN-LOAD", source_carrier_id="CARR-JOIN-C", carrier_rate=111.11),
        last_seen_sync_at=utc(2026, 7, 15),
    )

    with broker_session(app_conn, "broker_c") as cur:
        cur.execute(
            "SELECT l.carrier_rate FROM pool_carrier_lane p"
            " JOIN carriers c ON upper(btrim(c.mc_number)) = p.mc_number"
            " JOIN loads l ON l.source_carrier_id = c.source_carrier_id"
            " WHERE p.mc_number = %s",
            ("1346382",),
        )
        rows = cur.fetchall()

    rates = {float(r["carrier_rate"]) for r in rows if r["carrier_rate"] is not None}
    assert rates == {111.11}, (
        "joining the pool view back to carriers/loads inside broker_c's own "
        f"session must yield only broker_c's own rate, got {rates}"
    )


def test_no_mc_number_appears_in_a_where_or_join_in_broker_repository() -> None:
    """A static backstop matching D17/D4's own claim: the tenant-confined
    repository never uses ``mc_number`` as a join or filter key -- identity
    resolution across brokers lives in exactly one place
    (``PoolRepository.pool_carriers``, which does its own matching *inside*
    the view, never against a tenant table directly)."""
    import inspect

    from app.repository import broker_repository

    for line in inspect.getsource(broker_repository).splitlines():
        upper = line.upper()
        if "MC_NUMBER" not in upper:
            continue
        assert "JOIN" not in upper, f"mc_number used in a JOIN: {line!r}"
        assert "WHERE" not in upper, f"mc_number used in a WHERE: {line!r}"


# ---------------------------------------------------------------------------
# 7. pool_audit: append-only, and keyed by broker/load/time
# ---------------------------------------------------------------------------


def test_pool_audit_records_every_read_keyed_by_broker_load_and_time(
    app_conn: psycopg.Connection,
) -> None:
    pool_a = PoolRepository.for_broker(app_conn, "broker_a")
    pool_a.record_pool_read("LOAD-1")
    pool_a.record_pool_read("LOAD-1")  # a repeat is recorded again, not merged
    pool_a.record_pool_read("LOAD-2")

    with broker_session(app_conn, "broker_a") as cur:
        cur.execute(
            "SELECT broker_id, source_load_id, asked_at FROM pool_audit ORDER BY id"
        )
        rows = cur.fetchall()
    assert len(rows) == 3
    assert [r["source_load_id"] for r in rows] == ["LOAD-1", "LOAD-1", "LOAD-2"]
    assert all(r["broker_id"] == "broker_a" for r in rows)
    assert all(r["asked_at"] is not None for r in rows)

    # RLS still applies: broker_b cannot see broker_a's audit trail.
    with broker_session(app_conn, "broker_b") as cur:
        cur.execute("SELECT count(*) AS n FROM pool_audit")
        assert cur.fetchone()["n"] == 0


def test_pool_audit_is_append_only_for_the_application_role(
    app_conn: psycopg.Connection,
) -> None:
    pool_a = PoolRepository.for_broker(app_conn, "broker_a")
    pool_a.record_pool_read("SOME-LOAD")
    with broker_session(app_conn, "broker_a") as cur:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute("DELETE FROM pool_audit WHERE broker_id = 'broker_a'")
    with broker_session(app_conn, "broker_a") as cur:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute(
                "UPDATE pool_audit SET source_load_id = 'X' WHERE broker_id = 'broker_a'"
            )
    # The original row survived both attempts.
    with broker_session(app_conn, "broker_a") as cur:
        cur.execute("SELECT source_load_id FROM pool_audit")
        assert [r["source_load_id"] for r in cur.fetchall()] == ["SOME-LOAD"]


# ---------------------------------------------------------------------------
# 8. Opt-in/opt-out mechanics
# ---------------------------------------------------------------------------


def test_opt_in_is_idempotent_and_opt_out_is_a_delete_not_a_flag(
    app_conn: psycopg.Connection,
) -> None:
    pool_a = PoolRepository.for_broker(app_conn, "broker_a")
    assert pool_a.is_opted_in() is False

    assert pool_a.set_opted_in(True) is True
    assert pool_a.is_opted_in() is True
    assert pool_a.set_opted_in(True) is True  # idempotent
    with broker_session(app_conn, "broker_a") as cur:
        cur.execute("SELECT count(*) AS n FROM pool_opt_in WHERE broker_id = 'broker_a'")
        assert cur.fetchone()["n"] == 1

    assert pool_a.set_opted_in(False) is False
    assert pool_a.is_opted_in() is False
    with broker_session(app_conn, "broker_a") as cur:
        cur.execute("SELECT count(*) AS n FROM pool_opt_in")
        assert cur.fetchone()["n"] == 0  # a DELETE, not a flag flip -- no row left behind


# ---------------------------------------------------------------------------
# 9. The two refusal preconditions never even reach the read path
# ---------------------------------------------------------------------------


def test_pool_section_refuses_a_non_active_load_without_calling_carriers_for() -> None:
    load = make_load("SOME-LOAD", status=LoadStatus.COMPLETED)

    def _must_not_be_called(_key):
        raise AssertionError("carriers_for must not be called for an ineligible load")

    section = build_pool_section(
        load, as_of=date(2026, 7, 16), opted_in=True, carriers_for=_must_not_be_called
    )
    assert section.opted_in is True
    assert section.eligible is False
    assert section.carriers == ()
    assert "ACTIVE" in section.basis


def test_pool_section_refuses_a_broker_that_never_opted_in_without_calling_carriers_for() -> None:
    load = make_load("SOME-LOAD", status=LoadStatus.ACTIVE)

    def _must_not_be_called(_key):
        raise AssertionError("carriers_for must not be called for an opted-out broker")

    section = build_pool_section(
        load, as_of=date(2026, 7, 16), opted_in=False, carriers_for=_must_not_be_called
    )
    assert section.opted_in is False
    assert section.eligible is False
    assert section.carriers == ()
    assert "not in the shared carrier pool" in section.basis
