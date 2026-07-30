"""P6 -- API integration tests (TASKS.md Phase 7 P6).

Exercises the route surface in ``app.api.routes`` end to end: real Postgres,
the real 132-file sync corpus, a real ``TestClient`` -- no mocks. CLAUDE.md
invariants 1 (tenant isolation), 2 (reasons come from the score) and 6 (every
answer reports its tier and count) all surface at this layer, so most of what
is asserted here is numeric, against ``data/TRACEABILITY.md``'s hand-verified
figures -- never "the shape looks plausible".

**Fixture strategy.** ``conftest.py``'s ``clean_db`` truncates before and
after every test, which is right for tests that write their own rows but
wrong for this module: every read here depends on the *whole* corpus being
ingested, and re-ingesting it per test would cost ~5s x N for no benefit,
since nothing in this file mutates lane/carrier stats. So:

* :func:`clean_db` is overridden, module-locally, to a no-op -- it shadows
  ``conftest.py``'s autouse fixture for every test collected from this file
  only (standard pytest fixture resolution), and does not affect any other
  test module.
* :func:`client` truncates once, boots the real FastAPI app through
  ``TestClient`` -- which runs the exact lifespan ``docker compose up`` runs
  (D8: bootstrap, then a synchronous full ingest) -- and truncates again once
  every test in this module has run. One ingest backs the whole file.

Tests that need to *write* (the cross-broker collision, the admin-ingest
idempotency check) do so additively, through ``BrokerRepository`` or the
``/api/admin/ingest`` route itself, never by truncating -- so they cannot
step on a sibling test's fixture data.
"""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.repository import BrokerRepository

from .conftest import _truncate
from .support import make_load, utc

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_db() -> Iterator[None]:
    """Shadow ``conftest.py``'s per-test truncate for this module (see above).

    ``client`` below does its own truncate-ingest-truncate around the whole
    file instead.
    """
    yield


@pytest.fixture(scope="module")
def client(admin_conn: psycopg.Connection) -> Iterator[TestClient]:
    """One full-corpus-backed app for every test in this file.

    ``TestClient(app)`` used as a context manager runs ``app.main.lifespan``
    -- the same bootstrap-then-synchronous-ingest ``docker compose up``
    triggers (DECISIONS.md D8) -- against whatever ``DATABASE_URL`` /
    ``ADMIN_DATABASE_URL`` name (the isolated database this suite is run
    against, per the task brief). Idempotent ingestion means a second
    ``with TestClient(app)`` block anywhere else in the session is harmless,
    but this file only opens one.
    """
    _truncate(admin_conn)
    with TestClient(app) as test_client:
        yield test_client
    _truncate(admin_conn)


# ---------------------------------------------------------------------------
# Real day-11 loads this file addresses by id (data/TRACEABILITY.md)
# ---------------------------------------------------------------------------

# DAY11-RICH, one per broker: same lane key (ZIP3 750->774, DRY_VAN), same
# accepted tier, 12 loads each -- deliberately colliding on everything except
# broker_id (DECISIONS.md D9's "cross-broker rate separation" tripwire).
_RICH_A = "127412794"  # broker_a / FreightFlow (TMS A)
_RICH_B = "HD-2026-005053"  # broker_b / HaulDesk (TMS B)
_RICH_C = "a0jO900000Pe4PRTiG"  # broker_c / BrokerOS (TMS C)

# DAY11-THIN, broker_a: REGION tier, 70 loads, confidence forced low.
_THIN_A = "127413097"

# DAY11-SCATTER, broker_a: ZIP3 rejected (3 < 5), METRO accepted -- a rung
# tried and rejected, still reported (invariant 6).
_SCATTER_A = "127412960"

# DAY11-UNKNOWN-EQUIP, broker_c: null equipment, D6 skips the filter, D15
# caps the resulting heterogeneous pool at medium.
_UNKNOWN_EQUIP_C = "a0jO900000RE5kFMEU"

# DAY11-COLDSTART, broker_b: only 4 flatbed loads in the whole history, so
# ZIP3/METRO/REGION all fall short with the filter on and the walk lands on
# rung 4 -- the one rung where the D15 cap can never bind (D25).
_COLDSTART_B = "HD-2026-005077"

# The rate-only ADJUSTMENT correction (CLAUDE.md "Known traps"), broker_b.
_RATE_ONLY_CORRECTION_B = "HD-2026-004733"


# ---------------------------------------------------------------------------
# 1. Smoke: brokers and health
# ---------------------------------------------------------------------------


def test_list_brokers_returns_the_three_seeded_tenants(client: TestClient) -> None:
    resp = client.get("/api/brokers")
    assert resp.status_code == 200
    brokers = {b["id"]: b for b in resp.json()}
    assert set(brokers) == {"broker_a", "broker_b", "broker_c"}
    assert brokers["broker_a"]["tms_type"] == "A"
    assert brokers["broker_b"]["tms_type"] == "B"
    assert brokers["broker_c"]["tms_type"] == "C"


def test_health_reports_ok_against_the_real_database(client: TestClient) -> None:
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"


# ---------------------------------------------------------------------------
# 2. Tenant isolation -- every load-scoped route, another broker's id
# ---------------------------------------------------------------------------


def test_load_detail_cross_broker_id_is_404_not_500_and_leaks_nothing(
    client: TestClient,
) -> None:
    """Hand-verified: broker_b asking for broker_a's load 404s; broker_a
    asking for its own succeeds. The 404 body is exactly the documented
    message and nothing else -- no load fields, no stray keys."""
    wrong = client.get(f"/api/loads/{_RICH_A}", params={"broker_id": "broker_b"})
    assert wrong.status_code == 404
    assert wrong.json() == {
        "detail": f"no load {_RICH_A} for broker broker_b"
    }

    right = client.get(f"/api/loads/{_RICH_A}", params={"broker_id": "broker_a"})
    assert right.status_code == 200
    assert right.json()["source_load_id"] == _RICH_A


@pytest.mark.parametrize(
    "route_suffix",
    ["", "/recommendations", "/price-estimate"],
)
def test_every_load_scoped_route_404s_on_cross_broker_id(
    client: TestClient, route_suffix: str
) -> None:
    """Not just the detail route -- recommendations and price-estimate sit
    behind the identical ``require_load`` dependency, so the same load id
    under the wrong broker must 404 on every one of them, never 500."""
    resp = client.get(
        f"/api/loads/{_RICH_A}{route_suffix}", params={"broker_id": "broker_c"}
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == f"no load {_RICH_A} for broker broker_c"


def test_list_loads_filters_by_broker_and_active_status_with_null_rate(
    client: TestClient,
) -> None:
    """Hand-verified: broker_a's ACTIVE loads are exactly 5, every one with a
    null (not zero) carrier rate -- and none of those ids show up for either
    other broker under the same filter, which is the leak check that
    matters, not just the count."""
    resp = client.get(
        "/api/loads", params={"broker_id": "broker_a", "status": "ACTIVE"}
    )
    assert resp.status_code == 200
    loads = resp.json()
    assert len(loads) == 5
    assert all(load["status"] == "ACTIVE" for load in loads)
    assert all(load["carrier_rate"] is None for load in loads)
    active_a_ids = {load["source_load_id"] for load in loads}

    for other in ("broker_b", "broker_c"):
        other_resp = client.get(
            "/api/loads", params={"broker_id": other, "status": "ACTIVE"}
        )
        assert other_resp.status_code == 200
        other_ids = {load["source_load_id"] for load in other_resp.json()}
        assert active_a_ids.isdisjoint(other_ids)


def test_unknown_broker_id_404s_on_every_route_without_revealing_anything(
    client: TestClient,
) -> None:
    for path, params in (
        ("/api/loads", {"broker_id": "broker_zzz"}),
        (f"/api/loads/{_RICH_A}", {"broker_id": "broker_zzz"}),
        (f"/api/loads/{_RICH_A}/recommendations", {"broker_id": "broker_zzz"}),
        (f"/api/loads/{_RICH_A}/price-estimate", {"broker_id": "broker_zzz"}),
    ):
        resp = client.get(path, params=params)
        assert resp.status_code == 404, path
        assert resp.json() == {"detail": "no such broker: broker_zzz"}


def test_malformed_broker_id_is_404_not_500(client: TestClient) -> None:
    """A SQL-injection-shaped id is just a string ``BrokerRepository`` never
    recognizes as a tenant -- the parameterized query underneath treats it as
    inert data, so this fails the same documented way an unknown id does,
    not a 500."""
    hostile = "broker_a'; DROP TABLE loads; --"
    resp = client.get("/api/loads", params={"broker_id": hostile})
    assert resp.status_code == 404
    assert resp.json() == {"detail": f"no such broker: {hostile}"}

    # And the table really is still there, for good measure.
    still_there = client.get(
        "/api/loads", params={"broker_id": "broker_a", "status": "ACTIVE"}
    )
    assert still_there.status_code == 200
    assert len(still_there.json()) == 5


def test_missing_broker_id_param_is_422_not_a_silent_empty_result(
    client: TestClient,
) -> None:
    for path in ("/api/loads", f"/api/loads/{_RICH_A}", f"/api/loads/{_RICH_A}/price-estimate"):
        resp = client.get(path)
        assert resp.status_code == 422, path


def test_unknown_status_filter_value_is_422_not_500(client: TestClient) -> None:
    resp = client.get(
        "/api/loads", params={"broker_id": "broker_a", "status": "SHIPPED_YESTERDAY"}
    )
    assert resp.status_code == 422


def test_unknown_load_id_for_a_real_broker_is_404_not_500(client: TestClient) -> None:
    resp = client.get(
        "/api/loads/NOPE-DOES-NOT-EXIST-ANYWHERE", params={"broker_id": "broker_a"}
    )
    assert resp.status_code == 404
    assert resp.json() == {
        "detail": "no load NOPE-DOES-NOT-EXIST-ANYWHERE for broker broker_a"
    }


def test_load_id_shared_by_two_brokers_stays_isolated_through_the_api(
    app_conn: psycopg.Connection,
    admin_conn: psycopg.Connection,
    client: TestClient,
) -> None:
    """Not a hypothetical: a load id, and the carrier MC/DOT behind it, that
    genuinely collide across two brokers -- seeded directly (real fixture
    ids never collide across TMS formats), then read back exclusively
    through the API to prove the isolation holds at this layer too, not just
    in the repository (which ``test_tenant_isolation.py`` already covers).

    Cleans up its own rows afterwards: this module's ``clean_db`` is a
    no-op (see the module docstring), so a test that writes has to remove
    what it wrote, or it would silently inflate the corpus totals the
    idempotency test below checks."""
    shared_id = "XBROKER-COLLIDE-0001"
    repo_a = BrokerRepository.for_broker(app_conn, "broker_a")
    repo_b = BrokerRepository.for_broker(app_conn, "broker_b")
    repo_a.upsert_load(
        make_load(shared_id, carrier_rate=1234.56, customer_rate=1500.0),
        last_seen_sync_at=utc(2026, 7, 6),
    )
    repo_b.upsert_load(
        make_load(shared_id, carrier_rate=999.99, customer_rate=1100.0),
        last_seen_sync_at=utc(2026, 7, 6),
    )
    try:
        resp_a = client.get(f"/api/loads/{shared_id}", params={"broker_id": "broker_a"})
        resp_b = client.get(f"/api/loads/{shared_id}", params={"broker_id": "broker_b"})
        resp_c = client.get(f"/api/loads/{shared_id}", params={"broker_id": "broker_c"})

        assert resp_a.status_code == resp_b.status_code == 200
        assert resp_a.json()["carrier_rate"] == pytest.approx(1234.56)
        assert resp_b.json()["carrier_rate"] == pytest.approx(999.99)
        assert resp_c.status_code == 404  # never seeded there
    finally:
        admin_conn.execute(
            "DELETE FROM loads WHERE source_load_id = %s", (shared_id,)
        )


def test_recommendations_and_price_estimate_are_exact_and_unaffected_by_the_other_two_brokers(
    client: TestClient,
) -> None:
    """The headline tenant-isolation property, through the API.

    All three brokers' DAY11-RICH loads sit on the *identical* lane key
    (ZIP3 ``750->774``, DRY_VAN), each with exactly 12 supporting loads, all
    three ingested and present in the database simultaneously (not
    sequentially) -- which DECISIONS.md D9 argues is the stronger proof: if
    the repository or the API ever dropped ``broker_id``, these three answers
    would collapse toward the 36-load pooled median (~2.16), and they don't.
    Every number below is data/TRACEABILITY.md's hand-verified figure, not a
    value re-derived from today's code.
    """
    expectations = {
        "broker_a": dict(
            load_id=_RICH_A,
            tier="ZIP3",
            load_count=12,
            p25=1.7500,
            p50=1.7800,
            p75=1.8050,
            point=526.88,
            low=518.0,
            high=534.28,
            distance=296.0,
            top_carrier="IBRAHIM TRANSPORT INC",
            top_score=84.3,
        ),
        "broker_b": dict(
            load_id=_RICH_B,
            tier="ZIP3",
            load_count=12,
            p25=2.0925,
            p50=2.1600,
            p75=2.1775,
            point=601.99,
            low=583.18,
            high=606.87,
            distance=278.7,
            top_carrier="NORTH TEXAS LINE HAUL INC",
            top_score=84.6,
        ),
        "broker_c": dict(
            load_id=_RICH_C,
            tier="ZIP3",
            load_count=12,
            p25=2.4800,
            p50=2.5100,
            p75=2.5200,
            point=786.38,
            low=776.98,
            high=789.52,
            distance=313.3,
            top_carrier="Metroplex Ridge Logistics, Inc.",
            top_score=83.1,
        ),
    }

    medians: set[float] = set()
    for broker_id, expected in expectations.items():
        estimate = client.get(
            f"/api/loads/{expected['load_id']}/price-estimate",
            params={"broker_id": broker_id},
        ).json()
        assert estimate["tier"] == expected["tier"]
        assert estimate["load_count"] == expected["load_count"]
        assert estimate["rate_per_mile_p25"] == pytest.approx(expected["p25"], abs=1e-6)
        assert estimate["rate_per_mile_p50"] == pytest.approx(expected["p50"], abs=1e-6)
        assert estimate["rate_per_mile_p75"] == pytest.approx(expected["p75"], abs=1e-6)
        assert estimate["point_usd"] == pytest.approx(expected["point"], abs=0.01)
        assert estimate["low_usd"] == pytest.approx(expected["low"], abs=0.01)
        assert estimate["high_usd"] == pytest.approx(expected["high"], abs=0.01)
        assert estimate["distance_miles"] == pytest.approx(expected["distance"], abs=0.05)
        assert estimate["confidence"] == "medium"
        medians.add(estimate["rate_per_mile_p50"])

        ranking = client.get(
            f"/api/loads/{expected['load_id']}/recommendations",
            params={"broker_id": broker_id},
        ).json()
        assert ranking["tier"] == expected["tier"]
        assert ranking["lane_load_count"] == expected["load_count"]
        assert len(ranking["carriers"]) == 12
        top = ranking["carriers"][0]
        assert top["rank"] == 1
        assert top["carrier"]["name"] == expected["top_carrier"]
        assert top["score"] == pytest.approx(expected["top_score"], abs=0.05)

    # The three medians are genuinely distinct -- not the pooled ~2.16 that a
    # dropped broker_id would produce (data/TRACEABILITY.md's "Cross-broker
    # rate separation" table).
    assert medians == {1.78, 2.16, 2.51}


# ---------------------------------------------------------------------------
# 3. Invariant 6 -- the whole tier walk survives serialization
# ---------------------------------------------------------------------------


def test_price_estimate_returns_the_whole_walk_not_just_the_accepted_rung(
    client: TestClient,
) -> None:
    """DAY11-SCATTER: ZIP3 is tried and rejected (3 < 5) before METRO is
    accepted (8 loads) -- both rungs must be in the response, with
    ``min_sample`` and the equipment filter alongside them."""
    resp = client.get(
        f"/api/loads/{_SCATTER_A}/price-estimate", params={"broker_id": "broker_a"}
    )
    assert resp.status_code == 200
    body = resp.json()
    walk = body["walk"]
    assert walk["min_sample"] == 5
    assert walk["equipment_filter"] == "REEFER"
    assert walk["equipment_filtered"] is True

    rungs_by_tier = {rung["tier"]: rung for rung in walk["rungs"]}
    assert rungs_by_tier["ZIP3"]["accepted"] is False
    assert rungs_by_tier["ZIP3"]["load_count"] == 3
    assert rungs_by_tier["ZIP3"]["verdict"] == "rejected, 3 < 5"
    assert rungs_by_tier["METRO"]["accepted"] is True
    assert rungs_by_tier["METRO"]["load_count"] == 8
    assert body["tier"] == "METRO"
    assert body["load_count"] == 8


def test_low_confidence_estimate_is_labeled_never_omitted(client: TestClient) -> None:
    """DAY11-THIN: REGION always caps at low confidence (PRD section 7), but
    the estimate itself -- 70 loads, a real point/low/high -- is still
    returned in full. Low confidence is a label, not a reason to hide data."""
    resp = client.get(
        f"/api/loads/{_THIN_A}/price-estimate", params={"broker_id": "broker_a"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["confidence"] == "low"
    assert body["tier"] == "REGION"
    assert body["load_count"] == 70
    assert body["rate_per_mile_p25"] == pytest.approx(1.7850, abs=1e-6)
    assert body["rate_per_mile_p50"] == pytest.approx(1.8800, abs=1e-6)
    assert body["rate_per_mile_p75"] == pytest.approx(2.0200, abs=1e-6)
    assert body["point_usd"] == pytest.approx(351.94, abs=0.01)
    assert body["low_usd"] == pytest.approx(334.15, abs=0.01)
    assert body["high_usd"] == pytest.approx(378.14, abs=0.01)  # DECISIONS.md D18


def test_unknown_equipment_load_caps_at_medium_with_the_mix_named(
    client: TestClient,
) -> None:
    """DAY11-UNKNOWN-EQUIP / SHP6701577 (broker_c): the load's own equipment
    is null, so invariant 5 forbids defaulting to DRY_VAN and D6 skips the
    equipment filter at every rung. The resulting 31-load pool is
    heterogeneous (23 dry van, 8 reefer), so D15 caps confidence at medium
    even though 31 loads alone would otherwise read as high -- and the
    provenance line names the mix rather than hiding the reason."""
    resp = client.get(
        f"/api/loads/{_UNKNOWN_EQUIP_C}/price-estimate", params={"broker_id": "broker_c"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["load_equipment"] == "UNKNOWN"
    assert body["equipment_filter"] == "ANY"
    assert body["tier"] == "METRO"
    assert body["load_count"] == 31
    assert body["confidence"] == "medium"
    assert body["is_heterogeneous"] is True
    assert body["equipment_mix"] == [
        {"equipment": "DRY_VAN", "load_count": 23},
        {"equipment": "REEFER", "load_count": 8},
    ]
    assert body["rate_per_mile_p25"] == pytest.approx(2.4800, abs=1e-6)
    assert body["rate_per_mile_p50"] == pytest.approx(2.5300, abs=1e-6)
    assert body["rate_per_mile_p75"] == pytest.approx(2.6700, abs=1e-6)
    assert body["point_usd"] == pytest.approx(742.30, abs=0.01)
    assert "mixed pool" in body["provenance"]
    assert "23 dry van, 8 reefer" in body["provenance"]
    assert "capped at medium" in body["provenance"]


def test_rung_four_provenance_does_not_contradict_its_own_confidence(
    client: TestClient,
) -> None:
    """DAY11-COLDSTART / HD-2026-005077 (broker_b), the shipped load D25 was
    found on.

    The mirror of the test above, at the one rung where the D15 cap *cannot*
    bind: REGION_ANY is low by rule before the cap is ever evaluated, yet the
    pool is unfiltered so the mix is computed anyway. The mix must still be
    named -- 4 of these 93 loads are flatbed and a reader has to know that --
    but the sentence must not claim a cap the ``confidence`` field beside it
    contradicts (invariant 2).
    """
    resp = client.get(
        f"/api/loads/{_COLDSTART_B}/price-estimate", params={"broker_id": "broker_b"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["tier"] == "REGION_ANY"
    assert body["load_count"] == 93
    assert body["load_equipment"] == "FLATBED"
    assert body["equipment_filter"] == "ANY"
    assert body["confidence"] == "low"
    assert body["is_heterogeneous"] is True
    assert body["equipment_mix"] == [
        {"equipment": "DRY_VAN", "load_count": 71},
        {"equipment": "REEFER", "load_count": 18},
        {"equipment": "FLATBED", "load_count": 4},
    ]
    provenance = body["provenance"]
    assert "71 dry van, 18 reefer, 4 flatbed" in provenance
    assert "for a flatbed load" in provenance  # the load's, not the pool's
    assert "capped" not in provenance, (
        f"confidence is {body['confidence']!r} but the provenance reads: {provenance!r}"
    )


# ---------------------------------------------------------------------------
# 4. Invariant 2 / R5 -- reasons and the zero-score carrier
# ---------------------------------------------------------------------------


def test_every_recommendation_has_nonempty_reasons(client: TestClient) -> None:
    resp = client.get(
        f"/api/loads/{_RICH_A}/recommendations", params={"broker_id": "broker_a"}
    )
    assert resp.status_code == 200
    carriers = resp.json()["carriers"]
    assert len(carriers) == 12
    for carrier in carriers:
        assert len(carrier["reasons"]) > 0, carrier["carrier"]["name"]


def test_scores_are_non_increasing_down_the_ranked_list(client: TestClient) -> None:
    resp = client.get(
        f"/api/loads/{_RICH_A}/recommendations", params={"broker_id": "broker_a"}
    )
    scores = [c["score_exact"] for c in resp.json()["carriers"]]
    assert scores == sorted(scores, reverse=True)
    ranks = [c["rank"] for c in resp.json()["carriers"]]
    assert ranks == list(range(1, len(ranks) + 1))


def test_zero_score_carrier_is_returned_last_with_truthful_reasons(
    client: TestClient,
) -> None:
    """Hand-verified: ALAMO CHILL TRANSPORT (broker_a, DAY11-RICH) scores
    9.2 -- lowest of 12 -- and is still present, ranked last, with reasons
    that name each of the three things actually making it weak: no lane
    history, never hauled dry van, and a last delivery 304.8 mi away (past
    the 250 mi cutoff, so no proximity credit either)."""
    resp = client.get(
        f"/api/loads/{_RICH_A}/recommendations", params={"broker_id": "broker_a"}
    )
    carriers = resp.json()["carriers"]
    weakest = carriers[-1]
    assert weakest["carrier"]["name"] == "ALAMO CHILL TRANSPORT"
    assert weakest["rank"] == 12
    assert weakest["score"] == pytest.approx(9.2, abs=0.05)
    reasons = " | ".join(weakest["reasons"])
    assert "never run" in reasons.lower()
    assert "never hauled" in reasons.lower()
    assert "cutoff" in reasons.lower() or "no proximity credit" in reasons.lower()

    top = carriers[0]
    assert top["carrier"]["name"] == "IBRAHIM TRANSPORT INC"
    assert top["score"] == pytest.approx(84.3, abs=0.05)
    assert top["score"] - weakest["score"] == pytest.approx(75.1, abs=0.1)


# ---------------------------------------------------------------------------
# 5. Shapes, status codes, sync history
# ---------------------------------------------------------------------------


def test_load_detail_shows_the_rate_only_correction_as_the_tms_stated_it(
    client: TestClient,
) -> None:
    """HD-2026-004733 (broker_b): a corrected load whose second sync file's
    ``loads`` array never mentions it at all -- the correction only shows up
    as an appended ``ADJUSTMENT`` rate line (CLAUDE.md's "Known traps"). The
    history must still carry it, and ``carrier_rate`` must reflect it: 674.70
    LINEHAUL + 148.10 FUEL - 120.00 ADJUSTMENT = 702.80."""
    resp = client.get(
        f"/api/loads/{_RATE_ONLY_CORRECTION_B}", params={"broker_id": "broker_b"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["carrier_rate"] == pytest.approx(702.80)

    history = body["sync_history"]
    assert len(history) == 6
    entity_types = [event["entity_type"] for event in history]
    assert entity_types.count("LOAD") == 1
    assert entity_types.count("RATE_LINE") == 5

    last_event = history[-1]
    assert last_event["entity_type"] == "RATE_LINE"
    assert last_event["sync_file"] == "2026-07-12T06-00_sync.json"
    # The raw entity, exactly as TMS B stated it -- not a normalized rate.
    assert last_event["raw_json"] == {
        "rate_id": 910239,
        "load_num": "HD-2026-004733",
        "side": "pay",
        "code": "ADJUSTMENT",
        "amount_usd": -120.0,
        "created_at": "2026-07-12 03:49:00",
    }


def test_active_loads_serialize_null_carrier_rate_not_zero(client: TestClient) -> None:
    resp = client.get(
        "/api/loads", params={"broker_id": "broker_a", "status": "ACTIVE"}
    )
    loads = resp.json()
    assert len(loads) > 0
    for load in loads:
        assert load["carrier_rate"] is None
        assert not isinstance(load["carrier_rate"], (int, float))


# ---------------------------------------------------------------------------
# 6. D8 -- admin ingest is idempotent through the API
# ---------------------------------------------------------------------------


def test_admin_ingest_endpoint_is_idempotent_in_row_counts_and_summed_money(
    client: TestClient, admin_conn: psycopg.Connection
) -> None:
    """The whole corpus is already ingested once by ``client``'s own startup
    lifespan (D8), so the *first* call here is already the idempotency
    check; a second back-to-back call confirms it again. Hand-verified
    totals from ``test_ingestion.py``'s full-corpus test double as the
    absolute figures: 295 loads, summed carrier/customer money unchanged."""

    def _snapshot() -> tuple:
        return admin_conn.execute(
            "SELECT count(*), sum(carrier_rate), sum(customer_rate) FROM loads"
        ).fetchone()

    first = client.post("/api/admin/ingest").json()
    assert first["files_discovered"] == 132
    assert first["files_ingested"] == 0  # already ingested at startup
    assert first["files_skipped"] == 132
    assert first["events_written"] == 0
    loads_1, carrier_total_1, customer_total_1 = _snapshot()
    assert loads_1 == 295
    assert float(carrier_total_1) == pytest.approx(150716.43, abs=0.01)
    assert float(customer_total_1) == pytest.approx(186840.92, abs=0.01)

    second = client.post("/api/admin/ingest").json()
    assert second["files_ingested"] == 0
    assert second["files_skipped"] == 132
    assert second["events_written"] == 0
    loads_2, carrier_total_2, customer_total_2 = _snapshot()
    assert (loads_2, float(carrier_total_2), float(customer_total_2)) == (
        loads_1,
        pytest.approx(float(carrier_total_1)),
        pytest.approx(float(customer_total_1)),
    )
