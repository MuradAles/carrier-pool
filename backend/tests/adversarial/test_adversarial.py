"""Phase 9 adversarial pass — attacks that broke something, and attacks that didn't.

Written by `breaker`. No production code was changed to make any of these run.

**Reading the markers.** A test marked ``xfail(strict=True)`` is a *finding*: it
asserts the behaviour the documents promise, and it fails today. Strict means
that when someone fixes the underlying bug the test starts XPASSing and the run
goes red, forcing the marker to be removed rather than left to rot. Everything
unmarked is an attack that **failed to break anything** — those are the evidence
of robustness and belong in DECISIONS.md's honest-limitations section.

Every test names, in its docstring, the invariant or decision it is attacking.
"""

from __future__ import annotations

import json
import math
import os
from datetime import date, datetime, timezone

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import router
from app.domain.geo import resolve_place
from app.domain.lanes import TIER_METRO, LaneKey
from app.domain.model import (
    Carrier,
    CarrierStats,
    Equipment,
    Load,
    LoadStatus,
    Stop,
    StopLocation,
)
from app.domain.pricing import estimate_price, walk_tiers
from app.domain.scoring import (
    SHRINK_K,
    SIGNAL_ON_TIME,
    WEIGHTS,
    deadhead_credit,
    experience_credit,
    rank_carriers,
    score_carrier,
    shrunk_on_time,
)
from app.ingestion import ingest_all
from app.repository import BrokerRepository, db
from app.repository.broker_repository import broker_session

from ..integration.sync_fixtures import (
    TMS_A_DIR,
    TMS_B_DIR,
    TMS_C_DIR,
    tms_a_envelope,
    tms_a_load,
    tms_b_carrier,
    tms_b_envelope,
    tms_b_load,
    tms_b_rate,
    tms_c_account_ref,
    tms_c_envelope,
    tms_c_location_ref,
    tms_c_record,
    write_file,
)
from .conftest import (
    _DEFAULTS_AT_IMPORT,
    APP_URL,
    BREAKER_DB,
    DB_PREFIX,
    OWNS_DATABASE,
)

CAR_A = (700001, "Alamo Freight", "111111", "2222222", "+18005550100")
CAR_A2 = (700002, "Ghost Trucking", "333333", "4444444", "+18005550200")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def day11(shipment_id: int = 770001, **kwargs) -> dict:
    """A day-11 ACTIVE TMS A load: the thing the system answers for."""
    return tms_a_load(
        shipment_id,
        status="Booking",
        total_buy=None,
        carrier=None,
        pickup_date="2026-07-16",
        delivery_date="2026-07-17",
        **kwargs,
    )


def _pure_stop(sequence: int, *, pickup: bool, city: str, zip_code: str) -> Stop:
    """A placeable stop, for the two tests here that need no database."""
    place = resolve_place(city, "TX", zip_code)
    assert place is not None, f"{city} {zip_code} must resolve"
    return Stop(
        sequence=sequence,
        is_pickup=pickup,
        is_drop=not pickup,
        location=StopLocation(city=city, state="TX", zip=zip_code, place=place),
        scheduled_date=None,
    )


def _pure_load() -> Load:
    """Dallas -> Houston, dry van. Scoring reads its equipment and its pickup."""
    return Load(
        source_load_id="DAY11",
        load_number="DAY11",
        status=LoadStatus.ACTIVE,
        equipment=Equipment.DRY_VAN,
        stops=(
            _pure_stop(1, pickup=True, city="Dallas", zip_code="75201"),
            _pure_stop(2, pickup=False, city="Houston", zip_code="77002"),
        ),
        weight_lbs=None,
        distance_miles=271.0,
        customer_rate=None,
        carrier_rate=None,
        source_carrier_id=None,
        source_customer_id=None,
        created_at=None,
        last_modified_at=None,
    )


def _pure_carrier(carrier_id: str) -> Carrier:
    return Carrier(
        source_carrier_id=carrier_id,
        name=carrier_id,
        mc_number=None,
        dot_number=None,
        phone=None,
        home_city=None,
        home_state=None,
    )


def _pure_stats(
    carrier_id: str, key: LaneKey, load_count: int, on_time: int, eligible: int
) -> CarrierStats:
    """One carrier's lane record. The dates are equal for both carriers here, so
    recency cannot be what separates them."""
    return CarrierStats(
        source_carrier_id=carrier_id,
        tier=key.tier,
        lane_key=key.lane_key,
        equipment=key.equipment,
        load_count=load_count,
        on_time_count=on_time,
        on_time_eligible_count=eligible,
        avg_rate_per_mile=2.0,
        first_load_at=datetime(2026, 7, 6, 12, tzinfo=timezone.utc),
        last_load_at=datetime(2026, 7, 15, 12, tzinfo=timezone.utc),
    )


def answers(conn, broker: str, load_id: str):
    """``(price_estimate, carrier_ranking)`` for one load, through the real path."""
    repo = BrokerRepository(conn, broker)
    load = repo.get_load(load_id)
    assert load is not None, load_id
    walk = walk_tiers(load, repo.lane_stats_for)
    key = None if walk.accepted is None else walk.accepted.key
    return (
        estimate_price(
            load, lane_stats=repo.lane_stats_for, equipment_mix=repo.equipment_mix_for
        ),
        rank_carriers(load, walk, repo.ranking_inputs(key)),
    )


def derived_snapshot(conn, broker: str) -> dict:
    """Every derived number for one broker, for exact before/after comparison."""
    with broker_session(conn, broker) as cur:
        cur.execute(
            "SELECT tier, origin_key, dest_key, equipment, load_count,"
            " rate_per_mile_p25, rate_per_mile_p50, rate_per_mile_p75,"
            " first_load_at, last_load_at FROM lane_stats ORDER BY 1,2,3,4"
        )
        lanes = [tuple(r.values()) for r in cur.fetchall()]
        cur.execute(
            "SELECT source_carrier_id, tier, lane_key, equipment, load_count,"
            " on_time_count, on_time_eligible_count, avg_rate_per_mile,"
            " first_load_at, last_load_at FROM carrier_stats ORDER BY 1,2,3,4"
        )
        carrier_rows = [tuple(r.values()) for r in cur.fetchall()]
        cur.execute(
            "SELECT source_load_id, carrier_rate, customer_rate, rate_per_mile,"
            " status, equipment, delivered_on_time FROM loads ORDER BY 1"
        )
        loads = [tuple(r.values()) for r in cur.fetchall()]
        cur.execute(
            "SELECT source_carrier_id, last_delivery_lat, last_delivery_lon,"
            " last_delivery_at FROM carriers ORDER BY 1"
        )
        positions = [tuple(r.values()) for r in cur.fetchall()]
    return {
        "lane_stats": lanes,
        "carrier_stats": carrier_rows,
        "loads": loads,
        "positions": positions,
    }


@pytest.fixture
def client(app_conn):
    """A TestClient whose every request runs on the breaker database.

    The dependency is overridden rather than the module default rebound, so
    nothing here can redirect another suite's connections.
    """
    from app.api.deps import db_connection

    application = FastAPI()
    application.include_router(router)

    def _conn():
        conn = db.connect(APP_URL)
        try:
            yield conn
        finally:
            conn.close()

    application.dependency_overrides[db_connection] = _conn
    return TestClient(application, raise_server_exceptions=False)


# ===========================================================================
# HARNESS HYGIENE — this suite must not change any other suite's behaviour
# ===========================================================================


def test_no_conftest_rebinds_the_connection_defaults():
    """Importing this suite must not redirect anybody else's connections.

    Pytest imports every ``conftest.py`` under a collected path during
    collection. An earlier revision of ``tests/adversarial/conftest.py``
    rebound ``db.DEFAULT_DATABASE_URL`` at module scope, which pointed
    ``tests/integration/conftest.py`` — whose ``admin_conn`` calls
    ``db.connect_admin()`` with **no URL** — at the breaker database, so two
    suites truncated and rebuilt the same tables in one process. The symptom
    was a ``lane_stats`` unique violation that depended on collection order and
    on whichever rows happened to be sitting in the database, which is exactly
    the kind of defect that passes in review and fails once in front of someone.

    This asserts the property directly rather than trusting it to stay true.
    """
    assert (db.DEFAULT_DATABASE_URL, db.DEFAULT_ADMIN_DATABASE_URL) == (
        _DEFAULTS_AT_IMPORT
    ), (
        "a conftest rebound app.repository.db's module defaults; scope the "
        "redirection to your own fixtures instead"
    )
    # And they still describe whatever the environment asked for, not the
    # breaker database — belt and braces, in case the snapshot itself was taken
    # after a rebind further up the collection order.
    assert BREAKER_DB not in db.DEFAULT_DATABASE_URL
    assert BREAKER_DB not in db.DEFAULT_ADMIN_DATABASE_URL


def test_this_sessions_database_is_private_to_this_process(app_conn):
    """Two people running this suite at once must both get a correct answer.

    ``clean_db`` truncates before and after every test, so a shared database
    means a concurrent run silently deletes your rows mid-test. That produced
    ``2 passed, 66 errors`` against ``68 passed`` when I ran two
    ``pytest tests/adversarial`` at the same time — errors in *setup*, and a
    failure set that moved between runs.

    The database name therefore carries this process's pid, and nothing else
    may be reading it.
    """
    catalog = app_conn.execute("SELECT current_catalog").fetchone()[0]
    assert catalog == BREAKER_DB
    if not OWNS_DATABASE:
        pytest.skip("BREAKER_DB was pinned by the caller; isolation is theirs")
    assert catalog.startswith(DB_PREFIX)
    assert catalog[len(DB_PREFIX) :] == str(os.getpid()), (
        "this session's database is not named after this process, so a "
        "concurrent run would share and truncate it"
    )
    # And no other backend is connected to it.
    others = app_conn.execute(
        "SELECT count(*) FROM pg_stat_activity"
        " WHERE datname = current_catalog AND pid <> pg_backend_pid()"
        " AND application_name NOT LIKE 'pytest%'"
    ).fetchone()[0]
    assert others <= 2, (
        f"{others} other backends are on {catalog}: it is not private"
    )


def test_each_suite_talks_to_its_own_database(app_conn):
    """This suite's connections land on the breaker database; a default
    ``connect()`` still lands wherever ``DATABASE_URL`` points."""
    assert app_conn.execute("SELECT current_catalog").fetchone()[0] == BREAKER_DB
    default = db.connect()
    try:
        landed = default.execute("SELECT current_catalog").fetchone()[0]
    finally:
        default.close()
    assert landed != BREAKER_DB, (
        f"a bare connect() reached {landed}: the default binding has been "
        "redirected at this suite's database"
    )


# ===========================================================================
# FINDINGS
# ===========================================================================


def test_tms_b_rate_line_resent_in_a_later_file_is_not_double_counted(
    app_conn, data_root
):
    """CLAUDE.md Money row + invariant 3; DECISIONS.md D3's dedupe-key claim.

    HaulDesk's ``rates`` array is append-only at the source, so an overlapping
    sync window — or an operator re-pulling a day — restates a line item that
    was already delivered. The money must not move.
    """
    write_file(
        data_root,
        TMS_B_DIR,
        "2026-07-06T00-00_sync.json",
        tms_b_envelope(
            "2026-07-06 00:00:00",
            carriers=[tms_b_carrier(800001, name="Alamo", mc_no="1", dot_no="2")],
            loads=[tms_b_load("HD-1")],
            rates=[tms_b_rate(9001, "HD-1", "pay", "LINEHAUL", 700.0)],
        ),
    )
    # The next sync's window overlaps and restates rate 9001 verbatim.
    write_file(
        data_root,
        TMS_B_DIR,
        "2026-07-06T06-00_sync.json",
        tms_b_envelope(
            "2026-07-06 06:00:00",
            rates=[tms_b_rate(9001, "HD-1", "pay", "LINEHAUL", 700.0)],
        ),
    )
    ingest_all(app_conn, data_root)

    load = BrokerRepository(app_conn, "broker_b").get_load("HD-1")
    assert load.carrier_rate == pytest.approx(700.0), (
        f"rate 9001 was appended once for $700 and restated once; the load's "
        f"carrier rate is ${load.carrier_rate}"
    )


def test_a_restated_rate_line_beside_a_new_one_counts_each_exactly_once(
    app_conn, data_root
):
    """FINDING 1, in the shape the event-level key alone cannot catch (D22).

    The second file restates rate 9001 **and** appends a genuine correction as a
    new rate id. So it does contribute a ``RATE_LINE`` event, its bytes are
    re-summed by ``_rebuild_money``, and the restated line is sitting in them.
    The duplicate must count once and the correction must still apply:
    ``700 - 120 = 580``.
    """
    write_file(
        data_root,
        TMS_B_DIR,
        "2026-07-06T00-00_sync.json",
        tms_b_envelope(
            "2026-07-06 00:00:00",
            carriers=[tms_b_carrier(800001, name="Alamo", mc_no="1", dot_no="2")],
            loads=[tms_b_load("HD-3")],
            rates=[tms_b_rate(9003, "HD-3", "pay", "LINEHAUL", 700.0)],
        ),
    )
    write_file(
        data_root,
        TMS_B_DIR,
        "2026-07-07T00-00_sync.json",
        tms_b_envelope(
            "2026-07-07 00:00:00",
            rates=[
                tms_b_rate(9003, "HD-3", "pay", "LINEHAUL", 700.0),  # restated
                tms_b_rate(9004, "HD-3", "pay", "ADJUSTMENT", -120.0),  # new
            ],
        ),
    )
    ingest_all(app_conn, data_root)

    load = BrokerRepository(app_conn, "broker_b").get_load("HD-3")
    assert load.carrier_rate == pytest.approx(580.0), (
        f"$700 stated once and restated once, less a $120 correction, is $580; "
        f"the load's carrier rate is ${load.carrier_rate}"
    )


def test_two_loads_in_one_file_may_share_a_rate_id(app_conn, data_root):
    """D25: D22's dedupe key was narrower than the grain it dedupes at.

    ``sync_events_rate_line_identity_idx`` was ``UNIQUE (broker_id,
    source_entity_id)`` while ``_rebuild_money`` counts each ``rate_id`` once
    **per load**. A TMS that numbers rate ids per load rather than globally --
    an ordinary convention -- then loses every load after the first: the second
    load's ``rate_id: 1`` is refused as a duplicate of the first load's, no
    ``RATE_LINE`` event is written, and its carrier rate stays NULL with nothing
    anywhere saying why.

    The key is now ``(broker_id, source_load_id, source_entity_id)``, which
    still refuses D22's duplicate -- a restated line item is the same load's by
    definition, which the test above asserts.
    """
    write_file(
        data_root,
        TMS_B_DIR,
        "2026-07-06T00-00_sync.json",
        tms_b_envelope(
            "2026-07-06 00:00:00",
            carriers=[tms_b_carrier(800001, name="Alamo", mc_no="1", dot_no="2")],
            loads=[tms_b_load("HD-5"), tms_b_load("HD-6")],
            rates=[
                tms_b_rate(1, "HD-5", "pay", "LINEHAUL", 700.0),
                tms_b_rate(1, "HD-6", "pay", "LINEHAUL", 815.0),
            ],
        ),
    )
    ingest_all(app_conn, data_root)

    repo = BrokerRepository(app_conn, "broker_b")
    rates = {load_id: repo.get_load(load_id).carrier_rate for load_id in ("HD-5", "HD-6")}
    assert rates["HD-5"] == pytest.approx(700.0), rates
    assert rates["HD-6"] == pytest.approx(815.0), (
        f"HD-6's rate line was refused as a duplicate of HD-5's: {rates}"
    )

    with broker_session(app_conn, "broker_b") as cur:
        cur.execute(
            "SELECT count(*) AS n FROM sync_events WHERE entity_type = 'RATE_LINE'"
        )
        assert cur.fetchone()["n"] == 2, "one of the two rate lines was never logged"


def test_identical_tms_b_payload_under_two_filenames_is_not_double_counted(
    app_conn, data_root
):
    """Invariant 4's idempotency, pushed one step past ``sync_file``."""
    payload = tms_b_envelope(
        "2026-07-06 00:00:00",
        carriers=[tms_b_carrier(800001, name="Alamo", mc_no="1", dot_no="2")],
        loads=[tms_b_load("HD-2")],
        rates=[tms_b_rate(9002, "HD-2", "pay", "LINEHAUL", 700.0)],
    )
    write_file(data_root, TMS_B_DIR, "2026-07-06T00-00_sync.json", payload)
    write_file(data_root, TMS_B_DIR, "2026-07-06T01-00_sync.json", payload)
    ingest_all(app_conn, data_root)

    assert BrokerRepository(app_conn, "broker_b").get_load(
        "HD-2"
    ).carrier_rate == pytest.approx(700.0)


#: FINDING 2's threshold. ``(2 + 5L)/7 > (164 + 5L)/205`` reduces to
#: ``990L > 738``, so a perfect 2-load carrier out-shrinks a 164/200 veteran on
#: the on-time signal for every lane average above this. Written as the fraction
#: rather than the decimal so the algebra stays checkable.
ON_TIME_CROSSOVER = 738 / 990


@pytest.mark.parametrize("lane_average", [164 / 200, 0.9565, 0.9032, 1.0])
def test_two_for_two_out_shrinks_164_for_200_above_the_crossover(lane_average):
    """FINDING 2, and it is not a defect — the *sentence* was wrong (D21).

    CLAUDE.md's Known traps, PRD section 8 and DECISIONS.md D5 all used to say
    "2-for-2 must not beat 164-for-200" without saying *on which signal*. On the
    shrunk on-time rate it is false, and correctly so: shrinkage toward the lane
    mean says "with little evidence, assume average", so on a lane averaging 92%
    a veteran demonstrating 82% is genuinely below average and a 2-for-2 carrier
    estimated near the mean *should* read better on that one signal.

    ``164/200`` is the average of a lane the veteran dominates; ``0.9565`` and
    ``0.9032`` are the real ``DFW->HOU`` METRO on-time rates in the fixture, so
    the condition is live on the shipped data rather than hypothetical. This
    asserts the arithmetic in both directions instead of the old claim.
    """
    assert lane_average > ON_TIME_CROSSOVER
    rookie = shrunk_on_time(on_time_count=2, eligible_count=2, lane_rate=lane_average)
    veteran = shrunk_on_time(
        on_time_count=164, eligible_count=200, lane_rate=lane_average
    )
    assert rookie > veteran, (
        f"2-for-2 shrinks to {rookie:.4f}, 164-for-200 to {veteran:.4f} "
        f"(k={SHRINK_K}, lane average {lane_average:.2f})"
    )
    # And below the crossover the ordering flips, which is what makes this a
    # property of the formula rather than an accident of these four numbers.
    below = ON_TIME_CROSSOVER - 0.01
    assert shrunk_on_time(on_time_count=2, eligible_count=2, lane_rate=below) < (
        shrunk_on_time(on_time_count=164, eligible_count=200, lane_rate=below)
    )


def test_two_for_two_does_not_beat_164_for_200_on_experience():
    """The other half of the same trap — this reading of it does hold (D5)."""
    assert experience_credit(2) < experience_credit(200)


def test_two_for_two_does_not_beat_164_for_200_on_the_composite_score():
    """FINDING 2's other half: the ranking a rep actually sees is unaffected.

    The on-time signal carries 0.10 and can hand the rookie at most
    ``100 x 0.10 x (1.0 - 0.8244) = 1.76`` points — the lane average that
    maximises the gap is a lane where *everyone* is perfect. Lane experience
    carries 0.35 with ``n/(n+5)``, which is monotone in the count and hands the
    veteran ``100 x 0.35 x (0.9756 - 0.2857) = 24.15``. So the veteran leads by
    at least 22.4 points however the lane behaves, and the two carriers are
    scored here through the *production* scorer to prove it rather than by
    re-deriving the weights.

    Everything except lane history is held equal — same equipment record, same
    absent truck position, same last-load date — so the only differences are the
    two signals under discussion.
    """
    load = _pure_load()
    key = LaneKey(TIER_METRO, "DFW", "HOU", "DRY_VAN")
    scores = {}
    for name, lane_loads, on_time, eligible in (
        ("rookie", 2, 2, 2),
        ("veteran", 200, 164, 200),
    ):
        scores[name] = score_carrier(
            _pure_carrier(name),
            load=load,
            as_of=date(2026, 7, 16),
            key=key,
            stats=_pure_stats(name, key, lane_loads, on_time, eligible),
            equipment_loads=lane_loads,
            last_delivery=None,
            lane_on_time_rate=164 / 200,
        )
    rookie, veteran = scores["rookie"], scores["veteran"]
    assert rookie.signal("on_time").value > veteran.signal("on_time").value
    assert veteran.score_exact - rookie.score_exact > 22.0, (
        f"rookie {rookie.score} vs veteran {veteran.score}: "
        f"{[(s.name, round(s.contribution, 3)) for s in rookie.signals]} / "
        f"{[(s.name, round(s.contribution, 3)) for s in veteran.signals]}"
    )
    # The whole reversal budget the on-time signal can ever fund, at the lane
    # average that maximises it (every load on the lane delivered on time).
    perfect_lane = 100 * WEIGHTS[SIGNAL_ON_TIME] * (
        shrunk_on_time(on_time_count=2, eligible_count=2, lane_rate=1.0)
        - shrunk_on_time(on_time_count=164, eligible_count=200, lane_rate=1.0)
    )
    assert perfect_lane < 2.0


def test_a_delivery_after_the_newest_sync_does_not_earn_full_recency(
    app_conn, data_root
):
    """CLAUDE.md invariant 2, and PRD section 8's recency signal."""
    history = [
        tms_a_load(
            700000 + i,
            carrier=CAR_A,
            pickup_date="2026-07-20",
            delivery_date="2026-07-25",  # 19 days after the file that reports it
        )
        for i in range(1, 6)
    ]
    write_file(
        data_root,
        TMS_A_DIR,
        "2026-07-06T00-00_sync.json",
        tms_a_envelope("2026-07-06T00:00:00-05:00", history),
    )
    write_file(
        data_root,
        TMS_A_DIR,
        "2026-07-06T06-00_sync.json",
        tms_a_envelope("2026-07-06T06:00:00-05:00", [day11()]),
    )
    ingest_all(app_conn, data_root)

    _, ranking = answers(app_conn, "broker_a", "770001")
    recency = ranking.carriers[0].signal("recency")

    assert recency.observed is None or recency.observed >= 0, (
        f"days-since is {recency.observed}: the reason reads {recency.reason!r} "
        f"while the ranking is as of {ranking.as_of}"
    )
    assert recency.value < 1.0, (
        f"a load that has not been delivered as of {ranking.as_of} earned the "
        f"maximum recency credit ({recency.value})"
    )


def test_a_zero_dollar_booked_rate_does_not_become_the_published_floor(
    app_conn, data_root
):
    """PRD section 9, and PriceEstimate's 'no wrong number wearing a confident
    label' contract."""
    rates = [0.0, 0.0, 700.0, 700.0, 700.0]
    write_file(
        data_root,
        TMS_A_DIR,
        "2026-07-06T00-00_sync.json",
        tms_a_envelope(
            "2026-07-06T00:00:00-05:00",
            [
                tms_a_load(700000 + i, carrier=CAR_A, total_buy=r)
                for i, r in enumerate(rates, start=1)
            ],
        ),
    )
    write_file(
        data_root,
        TMS_A_DIR,
        "2026-07-16T00-00_sync.json",
        tms_a_envelope("2026-07-16T00:00:00-05:00", [day11()]),
    )
    ingest_all(app_conn, data_root)

    estimate, _ = answers(app_conn, "broker_a", "770001")
    assert not (estimate.low_usd == 0.0 and estimate.confidence != "low"), (
        f"range floor ${estimate.low_usd} at {estimate.confidence} confidence; "
        f"provenance: {estimate.provenance}"
    )


def test_a_negative_distance_does_not_produce_a_negative_price(app_conn, data_root):
    """PriceEstimate: every ``None`` money field means something; a negative
    dollar figure means nothing."""
    write_file(
        data_root,
        TMS_A_DIR,
        "2026-07-06T00-00_sync.json",
        tms_a_envelope(
            "2026-07-06T00:00:00-05:00",
            [tms_a_load(700000 + i, carrier=CAR_A) for i in range(1, 6)],
        ),
    )
    write_file(
        data_root,
        TMS_A_DIR,
        "2026-07-16T00-00_sync.json",
        tms_a_envelope("2026-07-16T00:00:00-05:00", [day11(mileage=-271.0)]),
    )
    ingest_all(app_conn, data_root)

    estimate, _ = answers(app_conn, "broker_a", "770001")
    assert estimate.point_usd is None or estimate.point_usd >= 0, (
        f"point estimate {estimate.point_usd} on {estimate.distance_miles} mi; "
        f"provenance: {estimate.provenance}"
    )


def test_a_negative_distance_does_not_vote_in_anyone_elses_statistics(
    app_conn, data_root
):
    """D25: D23 refused a negative distance for the load being *priced*; the
    same load as **evidence** was unfiltered.

    ``_population`` filtered on ``rate_per_mile IS NOT NULL`` and nothing about
    sign, and ``schema.sql``'s generated column only nulls a distance of exactly
    zero. So one −250.30 mi load with a $463.06 carrier rate published a
    −1.8500 $/mi into the pool: it dragged all three lane percentiles without
    pushing any of them to zero (so the D23 non-positive guard never fired, and
    the label stayed medium), and it made its carrier's ``avg_rate_per_mile``
    negative, which ``scoring._rate_note`` prints as "Averages $-1.85/mi" inside
    that carrier's reasons.

    Five honest loads over 271 mi at $650/$675/$700/$725/$750 -- deliberately
    spread, so a sixth entrant moves every percentile rather than hiding in a
    flat pool -- plus the poisoned sixth.
    """
    write_file(
        data_root,
        TMS_A_DIR,
        "2026-07-06T00-00_sync.json",
        tms_a_envelope(
            "2026-07-06T00:00:00-05:00",
            [
                tms_a_load(700000 + i, carrier=CAR_A, total_buy=buy)
                for i, buy in enumerate([650.0, 675.0, 700.0, 725.0, 750.0], start=1)
            ]
            + [
                tms_a_load(
                    700006, carrier=CAR_A2, mileage=-250.30, total_buy=463.06
                )
            ],
        ),
    )
    write_file(
        data_root,
        TMS_A_DIR,
        "2026-07-16T00-00_sync.json",
        tms_a_envelope("2026-07-16T00:00:00-05:00", [day11()]),
    )
    ingest_all(app_conn, data_root)

    estimate, ranking = answers(app_conn, "broker_a", "770001")

    assert estimate.load_count == 5, (
        f"{estimate.load_count} loads back this estimate: the −250.30 mi load "
        f"is being counted as evidence. provenance: {estimate.provenance}"
    )
    # The five honest loads alone: 675/271, 700/271, 725/271 at 4dp.
    for label, rate, expected in (
        ("p25", estimate.rate_per_mile_p25, 2.4908),
        ("median", estimate.rate_per_mile_p50, 2.5830),
        ("p75", estimate.rate_per_mile_p75, 2.6753),
    ):
        assert rate == pytest.approx(expected, abs=1e-4), (
            f"{label} is {rate} $/mi, not {expected}: the impossible load voted"
        )

    ghost = next(
        c for c in ranking.carriers if c.carrier.source_carrier_id == str(CAR_A2[0])
    )
    assert ghost.rate_note is None or "$-" not in ghost.rate_note, (
        f"a carrier's reasons quote a negative rate: {ghost.rate_note!r}"
    )
    for score in ranking.carriers:
        for reason in score.reasons:
            assert "$-" not in reason, (
                f"{score.carrier.source_carrier_id} reads {reason!r}"
            )


def test_a_nul_byte_in_a_load_id_is_a_404(client):
    """PRD section 10's error contract."""
    assert client.get("/api/loads/a%00b?broker_id=broker_a").status_code == 404


def test_tms_c_tons_are_not_silently_read_as_pounds(app_conn, data_root):
    """CLAUDE.md Weight row: TMS C units are per line item."""
    record = tms_c_record("a0B1", "CX-1")
    record["bos__Line_Items__r"] = [
        {
            "bos__Commodity__c": "steel coil",
            "bos__Weight__c": 20.0,
            "bos__Weight_Units__c": "tons",
            "bos__Pallet_Count__c": 1.0,
        }
    ]
    write_file(
        data_root,
        TMS_C_DIR,
        "2026-07-06T00-00_sync.json",
        tms_c_envelope(
            "2026-07-06T05:00:00.000+0000",
            [record],
            {
                "loc-origin": tms_c_location_ref("Dallas", "TX", "75201"),
                "loc-dest": tms_c_location_ref("Houston", "TX", "77002"),
                "cust-1": tms_c_account_ref("Cust", role="Customer"),
                "carr-1": tms_c_account_ref(
                    "Carr", role="Carrier", mc_number="1", dot_number="2"
                ),
            },
        ),
    )
    ingest_all(app_conn, data_root)

    weight = BrokerRepository(app_conn, "broker_c").get_load("a0B1").weight_lbs
    assert weight != pytest.approx(20.0), (
        "a 20-ton line item was recorded as 20 lb"
    )


def test_absent_dollars_are_always_explained_by_the_provenance(app_conn, data_root):
    """PRD section 9 / invariant 6: a low-confidence or missing answer is
    labelled, never blank."""
    history = [tms_a_load(700000 + i, carrier=CAR_A) for i in range(1, 6)]
    for load in history[:4]:
        load["mileage"] = "NaN"
    write_file(
        data_root,
        TMS_A_DIR,
        "2026-07-06T00-00_sync.json",
        tms_a_envelope("2026-07-06T00:00:00-05:00", history),
    )
    write_file(
        data_root,
        TMS_A_DIR,
        "2026-07-16T00-00_sync.json",
        tms_a_envelope("2026-07-16T00:00:00-05:00", [day11()]),
    )
    ingest_all(app_conn, data_root)

    estimate, _ = answers(app_conn, "broker_a", "770001")
    reportable = estimate.point_usd is not None and math.isfinite(estimate.point_usd)
    assert reportable or "no dollar estimate" in estimate.provenance, (
        f"point_usd={estimate.point_usd!r} (serialises to null) but the "
        f"provenance still reads: {estimate.provenance}"
    )


def test_a_dangling_carrier_ref_does_not_vanish_from_the_ranking(app_conn, data_root):
    """PRD section 8: 'score every carrier the broker has used'."""
    write_file(
        data_root,
        TMS_B_DIR,
        "2026-07-06T00-00_sync.json",
        tms_b_envelope(
            "2026-07-06 00:00:00",
            carriers=[],  # the carriers array never describes 800001
            loads=[tms_b_load(f"HD-{i}", carrier_ref=800001) for i in range(1, 6)],
            rates=[
                tms_b_rate(100 + i, f"HD-{i}", "pay", "LINEHAUL", 700.0)
                for i in range(1, 6)
            ],
        ),
    )
    write_file(
        data_root,
        TMS_B_DIR,
        "2026-07-16T00-00_sync.json",
        tms_b_envelope(
            "2026-07-16 00:00:00",
            loads=[tms_b_load("HD-DAY11", status_code=20, carrier_ref=None)],
        ),
    )
    ingest_all(app_conn, data_root)

    _, ranking = answers(app_conn, "broker_b", "HD-DAY11")
    with broker_session(app_conn, "broker_b") as cur:
        cur.execute("SELECT count(*) AS n FROM carrier_stats")
        stat_rows = cur.fetchone()["n"]

    assert not (stat_rows > 0 and not ranking.carriers), (
        f"{stat_rows} carrier_stats rows exist, {ranking.lane_load_count} loads "
        f"back the lane, and the ranking returned {len(ranking.carriers)} "
        f"carriers. Basis: {ranking.basis}"
    )


def test_the_broker_binding_survives_a_nested_broker_session(app_conn):
    """FINDING 11 (hardening note, not a leak).

    ``broker_session`` sets the role and ``app.broker_id`` transaction-locally.
    Nested inside an outer transaction — the shape ingestion uses — the block
    becomes a savepoint, and releasing a savepoint *keeps* its ``SET LOCAL``
    values. So after an inner block exits, raw SQL issued on the connection
    outside any binding silently runs as the last-bound broker instead of
    failing closed, which is the third barrier the module docstring relies on.

    Not exploitable in current code: a repository is per-file and per-broker, so
    the surviving binding is always the one that was just used.
    """
    with app_conn.transaction():
        with broker_session(app_conn, "broker_a"):
            pass
        bound = app_conn.execute(
            "SELECT current_setting('app.broker_id', true) AS b, current_user AS u"
        ).fetchone()

    assert bound == ("broker_a", "carrier_pool_app"), (
        "if this now fails closed, the finding is fixed"
    )


# ===========================================================================
# ATTACKS THAT FAILED TO BREAK ANYTHING
# ===========================================================================

# -- tenant isolation -------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT count(*) FROM loads",
        "SELECT count(*) FROM sync_events",
        "SELECT count(*) FROM lane_stats",
        "SET ROLE carrier",
        "ALTER ROLE carrier_pool_app BYPASSRLS",
        "DROP POLICY broker_isolation ON loads",
        "ALTER TABLE loads DISABLE ROW LEVEL SECURITY",
        "CREATE OR REPLACE FUNCTION current_broker() RETURNS TEXT"
        " LANGUAGE sql AS $$SELECT 'broker_b'$$",
        "DELETE FROM sync_events",
        "UPDATE sync_events SET raw_json = '{}'::jsonb",
        "SELECT rolpassword FROM pg_authid LIMIT 1",
    ],
)
def test_raw_escape_attempts_are_all_refused(app_conn, sql):
    """Invariant 1, and the append-only half of invariant 3.

    Every one of these is refused as ``InsufficientPrivilege`` — either because
    ``current_broker()`` raises with no binding, or because ``carrier_pool_app``
    owns nothing and holds no DDL or DELETE grant.
    """
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_conn.execute(sql)


def test_a_second_brokers_data_never_moves_the_first_brokers_answers(
    app_conn, data_root
):
    """Invariant 1, the headline claim, asserted numerically.

    Broker A and broker B share a carrier's MC/DOT, share a lane, share literal
    ``source_load_id`` strings and share a customer name — and B pays 13x what A
    pays. A's estimate, its percentiles and every carrier's exact score must be
    bit-identical before and after B exists.
    """
    mc, dot = "884201", "3113017"
    shared_carrier = (700001, "DELTA PRIME LLC", mc, dot, "+18005550100")
    write_file(
        data_root,
        TMS_A_DIR,
        "2026-07-06T00-00_sync.json",
        tms_a_envelope(
            "2026-07-06T00:00:00-05:00",
            [
                tms_a_load(
                    700000 + i,
                    total_buy=700.0,
                    carrier=shared_carrier,
                    customer=(880001, "Lone Star Beverages"),
                )
                for i in range(1, 6)
            ],
        ),
    )
    write_file(
        data_root,
        TMS_A_DIR,
        "2026-07-16T00-00_sync.json",
        tms_a_envelope("2026-07-16T00:00:00-05:00", [day11()]),
    )
    ingest_all(app_conn, data_root)
    before_estimate, before_ranking = answers(app_conn, "broker_a", "770001")
    before_scores = [
        (c.carrier.source_carrier_id, c.score_exact) for c in before_ranking.carriers
    ]

    # Broker B: same MC/DOT, same lane, the same load ids including 770001,
    # the same customer name, and $9,000 a load instead of $700.
    write_file(
        data_root,
        TMS_B_DIR,
        "2026-07-06T00-00_sync.json",
        tms_b_envelope(
            "2026-07-06 00:00:00",
            carriers=[
                tms_b_carrier(800001, name="Delta Prime, L.L.C.", mc_no=mc, dot_no=dot)
            ],
            loads=[
                tms_b_load(f"70000{i}", customer_name="Lone Star Beverages")
                for i in range(1, 6)
            ]
            + [tms_b_load("770001", customer_name="Lone Star Beverages")],
            rates=[
                tms_b_rate(9000 + i, f"70000{i}", "pay", "LINEHAUL", 9000.0)
                for i in range(1, 6)
            ]
            + [tms_b_rate(9099, "770001", "pay", "LINEHAUL", 9000.0)],
        ),
    )
    ingest_all(app_conn, data_root)
    after_estimate, after_ranking = answers(app_conn, "broker_a", "770001")

    assert after_estimate.load_count == before_estimate.load_count
    assert after_estimate.rate_per_mile_p25 == before_estimate.rate_per_mile_p25
    assert after_estimate.rate_per_mile_p50 == before_estimate.rate_per_mile_p50
    assert after_estimate.rate_per_mile_p75 == before_estimate.rate_per_mile_p75
    assert after_estimate.point_usd == before_estimate.point_usd
    assert [
        (c.carrier.source_carrier_id, c.score_exact) for c in after_ranking.carriers
    ] == before_scores

    # The colliding id resolves to a different load under each tenant.
    assert BrokerRepository(app_conn, "broker_a").get_load("770001").carrier_rate is None
    assert BrokerRepository(app_conn, "broker_b").get_load(
        "770001"
    ).carrier_rate == pytest.approx(9000.0)


def test_each_binding_sees_only_its_own_rows_through_every_aggregate(
    app_conn, data_root, admin_conn
):
    """Invariant 1 on the aggregate paths, not just the row reads."""
    write_file(
        data_root,
        TMS_A_DIR,
        "2026-07-06T00-00_sync.json",
        tms_a_envelope(
            "2026-07-06T00:00:00-05:00",
            [tms_a_load(700000 + i, carrier=CAR_A) for i in range(1, 4)],
        ),
    )
    write_file(
        data_root,
        TMS_B_DIR,
        "2026-07-06T00-00_sync.json",
        tms_b_envelope(
            "2026-07-06 00:00:00",
            carriers=[tms_b_carrier(800001, name="HD", mc_no="9", dot_no="9")],
            loads=[tms_b_load(f"HD-{i}") for i in range(1, 8)],
            rates=[
                tms_b_rate(i, f"HD-{i}", "pay", "LINEHAUL", 5000.0)
                for i in range(1, 8)
            ],
        ),
    )
    ingest_all(app_conn, data_root)

    counts = {}
    for broker in ("broker_a", "broker_b", "broker_c"):
        with broker_session(app_conn, broker) as cur:
            cur.execute(
                "SELECT (SELECT count(*) FROM loads) AS loads,"
                " (SELECT count(*) FROM sync_events) AS events,"
                " (SELECT sum(carrier_rate) FROM loads) AS money"
            )
            counts[broker] = dict(cur.fetchone())

    assert counts["broker_a"]["loads"] == 3
    assert counts["broker_b"]["loads"] == 7
    assert counts["broker_c"]["loads"] == 0
    assert counts["broker_c"]["money"] is None
    total = admin_conn.execute("SELECT count(*) FROM loads").fetchone()[0]
    assert total == 10, "the owner sees both tenants; neither binding did"


def test_an_unbound_connection_fails_closed_not_open(app_conn):
    """Invariant 1's third barrier: no binding must not mean 'everything'."""
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_conn.execute("SELECT * FROM loads")


# -- correction and replay --------------------------------------------------


def test_a_correction_chain_that_returns_to_its_original_value_restores_everything(
    app_conn, data_root
):
    """Invariant 3: a late correction produces the same numbers as an on-time one.

    700 -> 900 -> 1100 -> 700, as TMS A restatements of ``totalBuy``. Every
    derived row — lane percentiles, carrier averages, on-time counts, the
    carrier's last known position — must come back bit-identical.
    """
    write_file(
        data_root,
        TMS_A_DIR,
        "2026-07-06T00-00_sync.json",
        tms_a_envelope(
            "2026-07-06T00:00:00-05:00",
            [tms_a_load(700000 + i, total_buy=700.0, carrier=CAR_A) for i in range(1, 6)],
        ),
    )
    ingest_all(app_conn, data_root)
    original = derived_snapshot(app_conn, "broker_a")

    for day, buy in (("07", 900.0), ("08", 1100.0), ("09", 700.0)):
        write_file(
            data_root,
            TMS_A_DIR,
            f"2026-07-{day}T00-00_sync.json",
            tms_a_envelope(
                f"2026-07-{day}T00:00:00-05:00",
                [
                    tms_a_load(
                        700001,
                        total_buy=buy,
                        carrier=CAR_A,
                        modified=f"2026-07-{day}T15:00:00-05:00",
                    )
                ],
            ),
        )
        ingest_all(app_conn, data_root)

    assert derived_snapshot(app_conn, "broker_a") == original


def test_a_tms_b_adjustment_chain_that_nets_to_zero_restores_everything(
    app_conn, data_root
):
    """Invariant 3 through the append-only money path, including the rate-only
    correction whose ``loads`` array never names the load (CLAUDE.md, Known traps)."""
    write_file(
        data_root,
        TMS_B_DIR,
        "2026-07-06T00-00_sync.json",
        tms_b_envelope(
            "2026-07-06 00:00:00",
            carriers=[tms_b_carrier(800001, name="Alamo", mc_no="1", dot_no="2")],
            loads=[tms_b_load(f"HD-{i}") for i in range(1, 6)],
            rates=[
                tms_b_rate(100 + i, f"HD-{i}", "pay", "LINEHAUL", 700.0)
                for i in range(1, 6)
            ],
        ),
    )
    ingest_all(app_conn, data_root)
    original = derived_snapshot(app_conn, "broker_b")

    for day, amount, rate_id in (("07", -200.0, 201), ("08", 200.0, 202)):
        write_file(
            data_root,
            TMS_B_DIR,
            f"2026-07-{day}T00-00_sync.json",
            tms_b_envelope(
                f"2026-07-{day} 00:00:00",
                rates=[tms_b_rate(rate_id, "HD-1", "pay", "ADJUSTMENT", amount)],
            ),
        )
        ingest_all(app_conn, data_root)

    assert derived_snapshot(app_conn, "broker_b") == original


def test_a_correction_that_empties_a_lane_falls_back_instead_of_serving_it_stale(
    app_conn, data_root
):
    """Invariant 6 and PRD section 7: the walk must re-reject a rung whose
    evidence a correction removed."""
    history = [tms_a_load(700000 + i, total_buy=700.0, carrier=CAR_A) for i in range(1, 6)]
    history += [
        tms_a_load(
            710000 + i,
            total_buy=1400.0,
            carrier=CAR_A,
            origin=("Irving", "TX", "75061"),
            dest=("Pasadena", "TX", "77502"),
        )
        for i in range(1, 6)
    ]
    write_file(
        data_root,
        TMS_A_DIR,
        "2026-07-06T00-00_sync.json",
        tms_a_envelope("2026-07-06T00:00:00-05:00", history[:5]),
    )
    write_file(
        data_root,
        TMS_A_DIR,
        "2026-07-06T06-00_sync.json",
        tms_a_envelope("2026-07-06T06:00:00-05:00", history[5:]),
    )
    write_file(
        data_root,
        TMS_A_DIR,
        "2026-07-16T00-00_sync.json",
        tms_a_envelope("2026-07-16T00:00:00-05:00", [day11()]),
    )
    ingest_all(app_conn, data_root)
    before, _ = answers(app_conn, "broker_a", "770001")
    assert before.tier == "ZIP3" and before.load_count == 5

    # One of the five ZIP3 loads is restated as a quote, leaving RATED_STATUSES.
    write_file(
        data_root,
        TMS_A_DIR,
        "2026-07-17T00-00_sync.json",
        tms_a_envelope(
            "2026-07-17T00:00:00-05:00",
            [tms_a_load(700001, status="Quoting", total_buy=700.0, carrier=CAR_A)],
        ),
    )
    ingest_all(app_conn, data_root)

    after, ranking = answers(app_conn, "broker_a", "770001")
    assert after.tier == "METRO", "the walk kept serving a rung with 4 loads"
    assert after.walk.rungs[0].load_count == 4
    assert after.walk.rungs[0].accepted is False
    # And the ranking's basis names the rung the estimate actually used.
    assert "METRO tier" in ranking.basis
    assert f"{after.load_count} loads" in ranking.basis


def test_a_correction_that_moves_a_load_off_a_lane_leaves_no_stale_row(
    app_conn, data_root
):
    """Invariant 3: the lane a load *left* is as stale as the one it joined."""
    write_file(
        data_root,
        TMS_A_DIR,
        "2026-07-06T00-00_sync.json",
        tms_a_envelope(
            "2026-07-06T00:00:00-05:00",
            [tms_a_load(700000 + i, carrier=CAR_A) for i in range(1, 7)],
        ),
    )
    ingest_all(app_conn, data_root)
    write_file(
        data_root,
        TMS_A_DIR,
        "2026-07-07T00-00_sync.json",
        tms_a_envelope(
            "2026-07-07T00:00:00-05:00",
            [
                tms_a_load(
                    700001,
                    carrier=CAR_A,
                    origin=("San Antonio", "TX", "78205"),
                    dest=("Waco", "TX", "76701"),
                )
            ],
        ),
    )
    ingest_all(app_conn, data_root)

    repo = BrokerRepository(app_conn, "broker_a")
    old = repo.get_lane_stats(
        tier="ZIP3", origin_key="752", dest_key="770", equipment="DRY_VAN"
    )
    new = repo.get_lane_stats(
        tier="ZIP3", origin_key="782", dest_key="767", equipment="DRY_VAN"
    )
    assert old.load_count == 5
    assert new.load_count == 1


def test_re_ingesting_ten_times_and_interleaving_brokers_changes_nothing(
    app_conn, data_root
):
    """Invariant 4. The TMS B totals are the sharp end: they are a running sum."""
    write_file(
        data_root,
        TMS_A_DIR,
        "2026-07-06T00-00_sync.json",
        tms_a_envelope(
            "2026-07-06T00:00:00-05:00",
            [tms_a_load(700000 + i, carrier=CAR_A) for i in range(1, 4)],
        ),
    )
    write_file(
        data_root,
        TMS_B_DIR,
        "2026-07-06T03-00_sync.json",
        tms_b_envelope(
            "2026-07-06 03:00:00",
            carriers=[tms_b_carrier(800001, name="HD", mc_no="9", dot_no="9")],
            loads=[tms_b_load(f"HD-{i}") for i in range(1, 4)],
            rates=[
                tms_b_rate(i, f"HD-{i}", "pay", "LINEHAUL", 700.0) for i in range(1, 4)
            ],
        ),
    )
    write_file(
        data_root,
        TMS_C_DIR,
        "2026-07-06T06-00_sync.json",
        tms_c_envelope(
            "2026-07-06T11:00:00.000+0000",
            [tms_c_record(f"a0B{i}", f"CX-{i}") for i in range(1, 4)],
            {
                "loc-origin": tms_c_location_ref("Dallas", "TX", "75201"),
                "loc-dest": tms_c_location_ref("Houston", "TX", "77002"),
                "cust-1": tms_c_account_ref("Cust", role="Customer"),
                "carr-1": tms_c_account_ref(
                    "Carr", role="Carrier", mc_number="1", dot_number="2"
                ),
            },
        ),
    )
    ingest_all(app_conn, data_root)
    baseline = {b: derived_snapshot(app_conn, b) for b in ("broker_a", "broker_b", "broker_c")}

    for _ in range(10):
        ingest_all(app_conn, data_root)

    assert {
        b: derived_snapshot(app_conn, b) for b in ("broker_a", "broker_b", "broker_c")
    } == baseline


# -- adversarial data -------------------------------------------------------


def test_hostile_payloads_degrade_without_inventing_data(app_conn, data_root):
    """normalize.py's stated contract, attacked field by field.

    Unicode, empty strings and nulls in every string field; a blank carrier id;
    an unresolvable address; a rates row with a blank ``code``.
    """
    write_file(
        data_root,
        TMS_B_DIR,
        "2026-07-06T00-00_sync.json",
        tms_b_envelope(
            "2026-07-06 00:00:00",
            carriers=[
                {
                    "carrier_id": "   ",  # blank id: no carrier can be made of it
                    "carrier_name": "",
                    "mc_no": None,
                    "dot_no": "",
                    "home_city": "\U0001f69a\U0001f69a",
                    "home_state": None,
                    "phone": "",
                },
                {
                    "carrier_id": 800001,
                    "carrier_name": "Ｄｅｌｔａ　Ｐｒｉｍｅ",
                    "mc_no": "١٢٣٤٥٦",
                    "dot_no": "",
                    "home_city": "",
                    "home_state": "",
                    "phone": None,
                },
            ],
            loads=[
                tms_b_load(
                    "HD-1",
                    customer_code="",
                    customer_name="",
                    origin=("", "", ""),
                    dest=(None, None, None),
                    carrier_ref=800001,
                )
            ],
            rates=[tms_b_rate(1, "HD-1", "pay", "", 700.0)],
        ),
    )
    ingest_all(app_conn, data_root)

    repo = BrokerRepository(app_conn, "broker_b")
    load = repo.get_load("HD-1")
    assert load is not None
    assert load.source_customer_id is None, "an empty customer code became an id"
    assert load.origin.location.place is None, "an empty address resolved to a place"
    assert load.destination.location.place is None
    assert repo.get_carrier("   ") is None, "a blank carrier id produced a carrier"
    assert repo.get_carrier("800001").dot_number is None, "'' became a DOT number"
    # A geo-null load backs no lane statistic but is still stored and readable.
    assert (
        repo.get_lane_stats(
            tier="REGION",
            origin_key="TX_TRIANGLE",
            dest_key="TX_TRIANGLE",
            equipment="DRY_VAN",
        )
        is None
    )


@pytest.mark.parametrize(
    "equipment", ["Conestoga", None, "", "  ", "dry-van", "DRYVAN", "\U0001f69a"]
)
def test_no_unrecognised_equipment_ever_becomes_dry_van(app_conn, data_root, equipment):
    """Invariant 5, attacked with everything the picklist is not."""
    record = tms_c_record("a0B1", "CX-1")
    record["bos__Equipment_Type__c"] = equipment
    write_file(
        data_root,
        TMS_C_DIR,
        "2026-07-06T00-00_sync.json",
        tms_c_envelope(
            "2026-07-06T05:00:00.000+0000",
            [record],
            {
                "loc-origin": tms_c_location_ref("Dallas", "TX", "75201"),
                "loc-dest": tms_c_location_ref("Houston", "TX", "77002"),
                "cust-1": tms_c_account_ref("Cust", role="Customer"),
                "carr-1": tms_c_account_ref(
                    "Carr", role="Carrier", mc_number="1", dot_number="2"
                ),
            },
        ),
    )
    ingest_all(app_conn, data_root)
    assert str(BrokerRepository(app_conn, "broker_c").get_load("a0B1").equipment) == (
        "UNKNOWN"
    )


def test_one_stop_and_twelve_stop_loads_are_stored_and_lane_keyed_correctly(
    app_conn, data_root
):
    """PRD section 5, Stops: first pickup / last drop form the lane."""
    single = tms_c_record("a0B1", "CX-1")
    single["bos__Stops__r"] = [
        {
            "bos__Number__c": 1.0,
            "bos__Is_Pickup__c": True,
            "bos__Is_Dropoff__c": False,
            "bos__Location__c": "loc-origin",
            "bos__Scheduled_Date__c": "2026-07-06",
            "bos__Arrival_Time__c": None,
        }
    ]
    many = tms_c_record("a0B2", "CX-2")
    many["bos__Stops__r"] = [
        {
            "bos__Number__c": float(n),
            "bos__Is_Pickup__c": n == 1,
            "bos__Is_Dropoff__c": n == 12,
            "bos__Location__c": "loc-origin" if n == 1 else "loc-dest",
            "bos__Scheduled_Date__c": "2026-07-06" if n == 1 else "2026-07-07",
            "bos__Arrival_Time__c": (
                "2026-07-07T19:00:00.000+0000" if n == 12 else None
            ),
        }
        for n in range(1, 13)
    ]
    write_file(
        data_root,
        TMS_C_DIR,
        "2026-07-06T00-00_sync.json",
        tms_c_envelope(
            "2026-07-06T05:00:00.000+0000",
            [single, many],
            {
                "loc-origin": tms_c_location_ref("Dallas", "TX", "75201"),
                "loc-dest": tms_c_location_ref("Houston", "TX", "77002"),
                "cust-1": tms_c_account_ref("Cust", role="Customer"),
                "carr-1": tms_c_account_ref(
                    "Carr", role="Carrier", mc_number="1", dot_number="2"
                ),
            },
        ),
    )
    ingest_all(app_conn, data_root)

    repo = BrokerRepository(app_conn, "broker_c")
    one = repo.get_load("a0B1")
    twelve = repo.get_load("a0B2")
    assert len(one.stops) == 1 and one.destination is None
    assert not one.is_lane_resolvable, "a pickup-only load formed a lane"
    assert len(twelve.stops) == 12
    assert twelve.origin.location.zip3 == "752"
    assert twelve.destination.location.zip3 == "770"
    assert len(twelve.intermediate_stops) == 10


def test_a_dangling_tms_c_reference_geo_nulls_instead_of_guessing(app_conn, data_root):
    """base.py's failure policy: a dangling reference keeps the id and invents
    nothing."""
    write_file(
        data_root,
        TMS_C_DIR,
        "2026-07-06T00-00_sync.json",
        tms_c_envelope(
            "2026-07-06T05:00:00.000+0000", [tms_c_record("a0B1", "CX-1")], {}
        ),
    )
    ingest_all(app_conn, data_root)

    repo = BrokerRepository(app_conn, "broker_c")
    load = repo.get_load("a0B1")
    assert load.origin.location.place is None
    assert load.destination.location.place is None
    assert load.source_carrier_id == "carr-1", "the raw reference was dropped"
    assert repo.get_carrier("carr-1") is None, "a carrier was invented from a ref"


def test_a_city_outside_the_geo_table_is_excluded_from_stats_but_still_stored(
    app_conn, data_root
):
    """CLAUDE.md Location row: unmatched -> geo-null, still displayed."""
    write_file(
        data_root,
        TMS_A_DIR,
        "2026-07-06T00-00_sync.json",
        tms_a_envelope(
            "2026-07-06T00:00:00-05:00",
            [tms_a_load(1, origin=("Nowheresville", "ZZ", "00000"), carrier=CAR_A)],
        ),
    )
    ingest_all(app_conn, data_root)

    repo = BrokerRepository(app_conn, "broker_a")
    load = repo.get_load("1")
    assert load.origin.location.city == "Nowheresville"
    assert load.origin.location.place is None
    assert not load.is_lane_resolvable
    assert (
        repo.get_lane_stats(
            tier="REGION",
            origin_key="TX_TRIANGLE",
            dest_key="TX_TRIANGLE",
            equipment="DRY_VAN",
        )
        is None
    ), "a load we could not place still voted on the region"


def test_a_zero_mile_load_reports_a_rate_but_no_dollars(app_conn, data_root):
    """PriceEstimate: rate present, dollars absent, and the provenance says so."""
    write_file(
        data_root,
        TMS_A_DIR,
        "2026-07-06T00-00_sync.json",
        tms_a_envelope(
            "2026-07-06T00:00:00-05:00",
            [tms_a_load(700000 + i, carrier=CAR_A) for i in range(1, 6)],
        ),
    )
    write_file(
        data_root,
        TMS_A_DIR,
        "2026-07-16T00-00_sync.json",
        tms_a_envelope("2026-07-16T00:00:00-05:00", [day11(mileage=0.0)]),
    )
    ingest_all(app_conn, data_root)

    estimate, _ = answers(app_conn, "broker_a", "770001")
    assert estimate.rate_per_mile_p50 is not None
    assert estimate.point_usd is None
    assert "no distance" in estimate.provenance


def test_a_zero_mile_history_load_does_not_vote_on_the_median(app_conn, data_root):
    """``rate_per_mile`` is NULL for a zero distance, so it cannot be a zero
    that drags the percentile."""
    history = [tms_a_load(700000 + i, carrier=CAR_A) for i in range(1, 6)]
    history[0]["mileage"] = 0.0
    write_file(
        data_root,
        TMS_A_DIR,
        "2026-07-06T00-00_sync.json",
        tms_a_envelope("2026-07-06T00:00:00-05:00", history),
    )
    ingest_all(app_conn, data_root)

    lane = BrokerRepository(app_conn, "broker_a").get_lane_stats(
        tier="ZIP3", origin_key="752", dest_key="770", equipment="DRY_VAN"
    )
    assert lane.load_count == 4
    assert lane.rate_per_mile_p25 == pytest.approx(2.583, abs=1e-3)


# -- statistical nonsense ---------------------------------------------------


def test_one_absurd_outlier_does_not_wreck_the_median(app_conn, data_root):
    """A $50,000 / 400 mi load ($125/mi) among five normal ones."""
    history = [
        tms_a_load(700000 + i, carrier=CAR_A, total_buy=rate)
        for i, rate in enumerate([480.0, 490.0, 500.0, 510.0, 520.0], start=1)
    ]
    history.append(
        tms_a_load(700099, carrier=CAR_A, total_buy=50_000.0, mileage=400.0)
    )
    write_file(
        data_root,
        TMS_A_DIR,
        "2026-07-06T00-00_sync.json",
        tms_a_envelope("2026-07-06T00:00:00-05:00", history),
    )
    write_file(
        data_root,
        TMS_A_DIR,
        "2026-07-16T00-00_sync.json",
        tms_a_envelope("2026-07-16T00:00:00-05:00", [day11()]),
    )
    ingest_all(app_conn, data_root)

    estimate, _ = answers(app_conn, "broker_a", "770001")
    # The outlier is $125/mi; the honest band is well under $2/mi.
    assert estimate.rate_per_mile_p50 < 2.0
    assert estimate.rate_per_mile_p75 < 2.0


def test_a_lane_of_identical_rates_reports_an_honest_zero_width_range(
    app_conn, data_root
):
    """Twelve loads at exactly the same rate. p25 == p50 == p75 is the truth,
    and PRD section 9's count-based confidence is 'medium' at n=12."""
    write_file(
        data_root,
        TMS_A_DIR,
        "2026-07-06T00-00_sync.json",
        tms_a_envelope(
            "2026-07-06T00:00:00-05:00",
            [tms_a_load(700000 + i, carrier=CAR_A, total_buy=700.0) for i in range(12)],
        ),
    )
    write_file(
        data_root,
        TMS_A_DIR,
        "2026-07-16T00-00_sync.json",
        tms_a_envelope("2026-07-16T00:00:00-05:00", [day11()]),
    )
    ingest_all(app_conn, data_root)

    estimate, _ = answers(app_conn, "broker_a", "770001")
    assert estimate.load_count == 12
    assert estimate.rate_per_mile_p25 == estimate.rate_per_mile_p75
    assert estimate.low_usd == estimate.high_usd == estimate.point_usd
    assert estimate.confidence == "medium"


def test_a_carrier_whose_only_rate_is_zero_does_not_top_the_ranking(
    app_conn, data_root
):
    """A $0 rate is not a scored signal, so it cannot buy rank — and the rate
    note quotes the same zero the average was built from."""
    history = [tms_a_load(700000 + i, carrier=CAR_A, total_buy=700.0) for i in range(1, 6)]
    history.append(tms_a_load(710001, carrier=CAR_A2, total_buy=0.0))
    write_file(
        data_root,
        TMS_A_DIR,
        "2026-07-06T00-00_sync.json",
        tms_a_envelope("2026-07-06T00:00:00-05:00", history),
    )
    write_file(
        data_root,
        TMS_A_DIR,
        "2026-07-16T00-00_sync.json",
        tms_a_envelope("2026-07-16T00:00:00-05:00", [day11()]),
    )
    ingest_all(app_conn, data_root)

    _, ranking = answers(app_conn, "broker_a", "770001")
    assert ranking.carriers[0].carrier.source_carrier_id == "700001"
    zero_rate = next(
        c for c in ranking.carriers if c.carrier.source_carrier_id == "700002"
    )
    assert zero_rate.avg_rate_per_mile == pytest.approx(0.0)
    assert "$0.00/mi" in zero_rate.rate_note


def test_shrinkage_cannot_be_gamed_on_experience_alone(app_conn, data_root):
    """A 1-load carrier can only out-rank a veteran by *earning* the other four
    signals — never on lane experience, which is monotone in the count."""
    assert experience_credit(1) < experience_credit(50)
    # And the composite gap experience alone opens is 26.0 of the 35 points it
    # can award, so a reversal always has to be paid for elsewhere. Stated as an
    # assertion so a weight change surfaces here.
    gap = 100 * 0.35 * (experience_credit(50) - experience_credit(1))
    assert gap == pytest.approx(26.0, abs=0.1)


@pytest.mark.parametrize(
    "miles,expected",
    [(0.0, 1.0), (50.0, 1.0), (150.0, 0.5), (250.0, 0.0), (1e9, 0.0), (None, 0.0)],
)
def test_deadhead_credit_is_bounded_and_monotone(miles, expected):
    """PRD section 8's curve, at and past both ends. ``None`` is a gap in the
    carrier's own record and earns nothing (D20)."""
    assert deadhead_credit(miles) == pytest.approx(expected)


def test_an_empty_lane_and_an_all_null_lane_produce_no_estimate_not_a_zero(
    app_conn, data_root
):
    """Divide-by-zero and empty-population paths: 'we have nothing' is an answer,
    ``$0`` is a wrong one."""
    # Five ACTIVE loads: real loads, no booked rate, so nothing votes.
    write_file(
        data_root,
        TMS_A_DIR,
        "2026-07-06T00-00_sync.json",
        tms_a_envelope(
            "2026-07-06T00:00:00-05:00",
            [day11(700000 + i) for i in range(1, 6)],
        ),
    )
    write_file(
        data_root,
        TMS_A_DIR,
        "2026-07-16T00-00_sync.json",
        tms_a_envelope("2026-07-16T00:00:00-05:00", [day11()]),
    )
    ingest_all(app_conn, data_root)

    estimate, ranking = answers(app_conn, "broker_a", "770001")
    assert estimate.tier is None
    assert estimate.point_usd is None
    assert estimate.confidence == "low"
    assert "no estimate" in estimate.provenance
    assert "no tier reached the 5-load minimum" in ranking.basis
    assert ranking.carriers == (), "there is no carrier to rank and none was invented"


# -- reasons and scores -----------------------------------------------------


def test_every_reason_is_generated_from_the_signal_it_accompanies(
    app_conn, data_root
):
    """Invariant 2, checked structurally: the published score is exactly the sum
    of the signal contributions, and each reason is that signal's own sentence."""
    history = [tms_a_load(700000 + i, carrier=CAR_A) for i in range(1, 6)]
    history.append(tms_a_load(710001, carrier=CAR_A2, status="Dispatched"))
    write_file(
        data_root,
        TMS_A_DIR,
        "2026-07-06T00-00_sync.json",
        tms_a_envelope("2026-07-06T00:00:00-05:00", history),
    )
    write_file(
        data_root,
        TMS_A_DIR,
        "2026-07-16T00-00_sync.json",
        tms_a_envelope("2026-07-16T00:00:00-05:00", [day11()]),
    )
    ingest_all(app_conn, data_root)

    _, ranking = answers(app_conn, "broker_a", "770001")
    assert ranking.carriers, "nobody was scored"
    for scored in ranking.carriers:
        assert scored.score_exact == pytest.approx(
            sum(s.contribution for s in scored.signals)
        )
        assert scored.score == pytest.approx(round(scored.score_exact, 1), abs=0.05)
        for signal in scored.signals:
            assert signal.reason in scored.reasons
        # The experience sentence must quote the count the signal scored.
        experience = scored.signal("lane_experience")
        assert f"{scored.lane_loads} load" in experience.reason
        assert experience.value == pytest.approx(
            experience_credit(scored.lane_loads)
        )
        # And the on-time sentence must quote its own numerator and denominator.
        on_time = scored.signal("on_time")
        if scored.on_time_eligible_count:
            assert (
                f"{scored.on_time_count} of {scored.on_time_eligible_count}"
                in on_time.reason
            )


def test_a_carrier_with_no_lane_history_gets_an_accurate_reason_not_boilerplate(
    app_conn, data_root
):
    """PRD section 8: carriers scoring near zero are still returned, ranked last,
    with the reason they are weak."""
    history = [tms_a_load(700000 + i, carrier=CAR_A) for i in range(1, 6)]
    # A carrier known only from a flatbed load on a different lane.
    history.append(
        tms_a_load(
            710001,
            carrier=CAR_A2,
            equipment="48 ft Flatbed",
            origin=("San Antonio", "TX", "78205"),
            dest=("Waco", "TX", "76701"),
        )
    )
    write_file(
        data_root,
        TMS_A_DIR,
        "2026-07-06T00-00_sync.json",
        tms_a_envelope("2026-07-06T00:00:00-05:00", history),
    )
    write_file(
        data_root,
        TMS_A_DIR,
        "2026-07-16T00-00_sync.json",
        tms_a_envelope("2026-07-16T00:00:00-05:00", [day11()]),
    )
    ingest_all(app_conn, data_root)

    _, ranking = answers(app_conn, "broker_a", "770001")
    stranger = next(
        c for c in ranking.carriers if c.carrier.source_carrier_id == "700002"
    )
    assert stranger.rank == len(ranking.carriers)
    assert stranger.lane_loads == 0
    assert "never run" in stranger.signal("lane_experience").reason
    assert stranger.signal("lane_experience").value == 0.0
    assert "No load on this lane" in stranger.signal("recency").reason
    assert stranger.signal("recency").value == 0.0
    assert "never hauled dry van" in stranger.signal("equipment").reason
    assert stranger.signal("equipment").value == 0.0
    assert stranger.rate_note is None, "a carrier with no lane loads quoted a $/mi"


def _d20_field(data_root) -> None:
    """Three carriers whose deadhead inputs are missing in three different ways.

    Far Fleet has a known truck 271 mi out (past the cutoff, credit 0.0). Ghost
    Trucking has never delivered anything (no position at all). Lost Signal
    delivered *yesterday, ten miles from the pickup* — but to a town the geo
    table has never heard of, so its position is unplaceable too.
    """
    far = (700001, "Far Fleet", "111", "222", "+1")
    ghost = (700002, "Ghost Trucking", "333", "444", "+2")
    lost = (700003, "Lost Signal Ltd", "555", "666", "+3")

    history = [tms_a_load(700000 + i, carrier=far) for i in range(1, 6)]
    rolling = tms_a_load(710001, status="Dispatched", carrier=ghost)
    rolling["stops"][1]["actualDepartureDateTime"] = None
    history.append(rolling)
    history.append(
        tms_a_load(
            710002,
            carrier=lost,
            dest=("Nowheresville", "ZZ", "00000"),
            pickup_date="2026-07-14",
            delivery_date="2026-07-15",
        )
    )
    write_file(
        data_root,
        TMS_A_DIR,
        "2026-07-06T00-00_sync.json",
        tms_a_envelope("2026-07-06T00:00:00-05:00", history[:4]),
    )
    write_file(
        data_root,
        TMS_A_DIR,
        "2026-07-06T06-00_sync.json",
        tms_a_envelope("2026-07-06T06:00:00-05:00", history[4:]),
    )
    # Two day-11 loads differing only in whether the pickup resolves.
    write_file(
        data_root,
        TMS_A_DIR,
        "2026-07-16T00-00_sync.json",
        tms_a_envelope(
            "2026-07-16T00:00:00-05:00",
            [
                day11(770001),
                day11(770002, origin=("Nowheresville", "ZZ", "00000")),
            ],
        ),
    )


def test_d20s_two_gap_rules_cannot_reorder_a_ranking(app_conn, data_root):
    """D20's central arithmetic claim, attacked directly — and it holds.

    A gap in the *load* (unplaceable pickup) is neutral for everyone; a gap in
    *one carrier's* record earns nothing. The worry is that mixing them produces
    a visibly absurd order. It cannot: ``pickup_known`` is constant across one
    ranking, so the neutral branch is all-or-nothing and shifts every carrier by
    the same amount. The order is identical on both loads.
    """
    _d20_field(data_root)
    ingest_all(app_conn, data_root)

    _, placeable = answers(app_conn, "broker_a", "770001")
    _, unplaceable = answers(app_conn, "broker_a", "770002")

    assert [c.carrier.source_carrier_id for c in placeable.carriers] == [
        c.carrier.source_carrier_id for c in unplaceable.carriers
    ]
    # And the carrier with no position never outranks one we know to be far.
    far = next(c for c in placeable.carriers if c.carrier.name == "Far Fleet")
    ghost = next(c for c in placeable.carriers if c.carrier.name == "Ghost Trucking")
    assert far.rank < ghost.rank
    assert far.signal("deadhead").value == ghost.signal("deadhead").value == 0.0


def test_an_unplaceable_pickup_inflates_every_score_by_ten_points(
    app_conn, data_root
):
    """FINDING 13 (comparability, not ordering). D20's rank-neutrality argument
    is about one ranking; it says nothing across rankings.

    The same roster, the same lane, the same day: every carrier scores **exactly
    10.00 points higher** on the load whose pickup could not be placed, because
    the neutral 0.5 replaces a 0.0 that each of them had earned. A rep comparing
    two loads' carrier lists sees the worse-documented load's carriers look
    uniformly better, and nothing on the screen normalises for it.

    Characterising, not asserting a fix — the alternative (0.0 for everyone)
    is the one D20 rejects for good reason.
    """
    _d20_field(data_root)
    ingest_all(app_conn, data_root)

    _, placeable = answers(app_conn, "broker_a", "770001")
    _, unplaceable = answers(app_conn, "broker_a", "770002")
    by_name = {c.carrier.name: c for c in placeable.carriers}

    for scored in unplaceable.carriers:
        baseline = by_name[scored.carrier.name]
        assert scored.score_exact - baseline.score_exact == pytest.approx(10.0), (
            f"{scored.carrier.name}: {baseline.score} -> {scored.score}"
        )
        assert baseline.signal("deadhead").value == 0.0
        assert scored.signal("deadhead").value == 0.5


def test_an_unplaceable_delivery_reads_differently_from_no_delivery(
    app_conn, data_root
):
    """D20: 'Reason strings name the gap rather than implying a distance.'"""
    _d20_field(data_root)
    ingest_all(app_conn, data_root)

    _, ranking = answers(app_conn, "broker_a", "770001")
    never = next(c for c in ranking.carriers if c.carrier.name == "Ghost Trucking")
    unplaceable = next(
        c for c in ranking.carriers if c.carrier.name == "Lost Signal Ltd"
    )
    assert (
        never.signal("deadhead").reason != unplaceable.signal("deadhead").reason
    ), (
        "a carrier that has never delivered and one whose last delivery is off "
        f"the map share one sentence: {never.signal('deadhead').reason!r}"
    )


def test_the_basis_line_names_the_rung_the_estimate_actually_used(
    app_conn, data_root
):
    """Invariant 6 across both endpoints: ranking and pricing answer from one walk."""
    write_file(
        data_root,
        TMS_A_DIR,
        "2026-07-06T00-00_sync.json",
        tms_a_envelope(
            "2026-07-06T00:00:00-05:00",
            [
                tms_a_load(
                    700000 + i,
                    carrier=CAR_A,
                    origin=("Irving", "TX", "75061"),
                    dest=("Pasadena", "TX", "77502"),
                )
                for i in range(1, 7)
            ],
        ),
    )
    write_file(
        data_root,
        TMS_A_DIR,
        "2026-07-16T00-00_sync.json",
        tms_a_envelope("2026-07-16T00:00:00-05:00", [day11()]),
    )
    ingest_all(app_conn, data_root)

    estimate, ranking = answers(app_conn, "broker_a", "770001")
    assert estimate.tier == "METRO" == ranking.tier
    assert estimate.load_count == ranking.lane_load_count == 6
    assert "METRO tier" in ranking.basis
    assert "6 loads" in ranking.basis and "6 loads" in estimate.provenance
    assert "DFW->HOU" in ranking.basis and "DFW->HOU" in estimate.provenance


def test_a_correction_between_the_two_endpoints_splits_the_screen(
    client, app_conn, data_root
):
    """FINDING 12 (product-level, narrow). The load-detail screen shows a price
    and a ranking side by side, but ``/recommendations`` and ``/price-estimate``
    are separate requests that each do their own tier walk. An ingest landing
    between them — ``POST /api/admin/ingest`` is a live endpoint — leaves the
    two halves quoting different rungs of different evidence.

    ``routes.py`` claims "an estimate built on ``METRO`` cannot sit next to a
    ranking built on ``ZIP3``" and then concedes the two are separate requests.
    This shows the concession is the operative half.

    Mitigated in practice by D8: ingestion normally finishes before serving.
    """
    history = [tms_a_load(700000 + i, total_buy=700.0, carrier=CAR_A) for i in range(1, 6)]
    history += [
        tms_a_load(
            710000 + i,
            total_buy=1400.0,
            carrier=CAR_A,
            origin=("Irving", "TX", "75061"),
            dest=("Pasadena", "TX", "77502"),
        )
        for i in range(1, 6)
    ]
    write_file(
        data_root,
        TMS_A_DIR,
        "2026-07-06T00-00_sync.json",
        tms_a_envelope("2026-07-06T00:00:00-05:00", history[:5]),
    )
    write_file(
        data_root,
        TMS_A_DIR,
        "2026-07-06T06-00_sync.json",
        tms_a_envelope("2026-07-06T06:00:00-05:00", history[5:]),
    )
    write_file(
        data_root,
        TMS_A_DIR,
        "2026-07-16T00-00_sync.json",
        tms_a_envelope("2026-07-16T00:00:00-05:00", [day11()]),
    )
    ingest_all(app_conn, data_root)

    ranking = client.get(
        "/api/loads/770001/recommendations?broker_id=broker_a"
    ).json()
    assert ranking["tier"] == "ZIP3"

    # The correction the rep never sees: one ZIP3 load leaves RATED_STATUSES.
    write_file(
        data_root,
        TMS_A_DIR,
        "2026-07-17T00-00_sync.json",
        tms_a_envelope(
            "2026-07-17T00:00:00-05:00",
            [tms_a_load(700001, status="Quoting", total_buy=700.0, carrier=CAR_A)],
        ),
    )
    ingest_all(app_conn, data_root)

    price = client.get("/api/loads/770001/price-estimate?broker_id=broker_a").json()
    assert price["tier"] == "METRO"
    assert price["tier"] != ranking["tier"], (
        "if the two endpoints now share a walk, this finding is fixed"
    )
    # The screen would read: "5 loads on 752->770 (ZIP3)" beside
    # "median of 9 loads on DFW->HOU (METRO)" — two tiers, one load.
    assert "752->770" in ranking["basis"]
    assert "DFW->HOU" in price["provenance"]


def test_an_unknown_equipment_load_is_answered_from_a_mixed_pool_and_says_so(
    app_conn, data_root
):
    """D6 and D15 together: the filter is skipped, the mix is named, and
    confidence is capped at medium however many loads back it."""
    history = [
        tms_a_load(700000 + i, carrier=CAR_A, total_buy=700.0) for i in range(1, 17)
    ]
    for load in history[:4]:
        load["equipment"] = "53 ft Van | Reefer"
        load["totalBuy"] = 1400.0
    write_file(
        data_root,
        TMS_A_DIR,
        "2026-07-06T00-00_sync.json",
        tms_a_envelope("2026-07-06T00:00:00-05:00", history),
    )
    write_file(
        data_root,
        TMS_A_DIR,
        "2026-07-16T00-00_sync.json",
        tms_a_envelope("2026-07-16T00:00:00-05:00", [day11(equipment="")]),
    )
    ingest_all(app_conn, data_root)

    estimate, ranking = answers(app_conn, "broker_a", "770001")
    assert estimate.load_equipment == "UNKNOWN"
    assert estimate.equipment_filter == "ANY", "an UNKNOWN load became a dry-van query"
    assert estimate.load_count == 16
    assert estimate.is_heterogeneous
    assert estimate.confidence == "medium", "16 loads would be 'high' without D15's cap"
    assert "mixed pool" in estimate.provenance
    assert "12 dry van, 4 reefer" in estimate.provenance
    # And the equipment signal neither rewards nor punishes (invariant 5).
    assert ranking.carriers[0].signal("equipment").value == 0.5


# -- the API surface --------------------------------------------------------


def test_another_brokers_load_id_is_a_404_that_leaks_nothing(client, app_conn, data_root):
    """deps.require_load: existence is itself another tenant's data."""
    write_file(
        data_root,
        TMS_B_DIR,
        "2026-07-06T00-00_sync.json",
        tms_b_envelope(
            "2026-07-06 00:00:00",
            carriers=[tms_b_carrier(800001, name="HD", mc_no="9", dot_no="9")],
            loads=[tms_b_load("HD-SECRET")],
            rates=[tms_b_rate(1, "HD-SECRET", "pay", "LINEHAUL", 5000.0)],
        ),
    )
    ingest_all(app_conn, data_root)

    for suffix in ("", "/recommendations", "/price-estimate"):
        response = client.get(f"/api/loads/HD-SECRET{suffix}?broker_id=broker_a")
        assert response.status_code == 404
        body = json.dumps(response.json())
        assert "5000" not in body and "HaulDesk" not in body and "800001" not in body
    # Identical answer for an id that exists nowhere: the two are indistinguishable.
    assert client.get("/api/loads/HD-SECRET?broker_id=broker_a").json()["detail"] == (
        client.get("/api/loads/NOT-A-LOAD?broker_id=broker_a")
        .json()["detail"]
        .replace("NOT-A-LOAD", "HD-SECRET")
    )


@pytest.mark.parametrize(
    "url,expected",
    [
        ("/api/loads", 422),
        ("/api/loads?broker_id=broker_zzz", 404),
        ("/api/loads?broker_id=broker_a' OR '1'='1", 404),
        ("/api/loads?broker_id=broker_a&status=DELIVERD", 422),
        ("/api/loads?broker_id=broker_a&status=active", 422),
        ("/api/loads?broker_id=broker_a&status=' OR 1=1--", 422),
        ("/api/loads/x'; DROP TABLE loads;--?broker_id=broker_a", 404),
        ("/api/loads/nope/recommendations?broker_id=broker_a", 404),
        ("/api/loads/nope/price-estimate?broker_id=nope", 404),
        ("/api/loads/" + "A" * 4000 + "?broker_id=broker_a", 404),
    ],
)
def test_malformed_requests_are_refused_cleanly(client, url, expected):
    """PRD section 10. Nothing here reaches SQL as anything but a parameter."""
    assert client.get(url).status_code == expected


def test_the_loads_table_survives_the_injection_attempts(client, app_conn, data_root):
    """The table named in the payload above is still there afterwards."""
    write_file(
        data_root,
        TMS_A_DIR,
        "2026-07-06T00-00_sync.json",
        tms_a_envelope("2026-07-06T00:00:00-05:00", [tms_a_load(1, carrier=CAR_A)]),
    )
    ingest_all(app_conn, data_root)
    client.get("/api/loads/x'; DROP TABLE loads;--?broker_id=broker_a")
    assert BrokerRepository(app_conn, "broker_a").get_load("1") is not None
