"""S5 -- shared carrier pool leak tests, HTTP level, against the real corpus.

Companion to ``test_pool_leak.py`` (synthetic, repository-level). Everything
here runs through the real FastAPI app, against the real 132-file sync corpus
(DECISIONS.md's fixture), so the two genuine cross-broker carriers D17 names
by MC number are exercised with real data rather than a stand-in.

**A correction to the task brief, verified against the real fixture before
writing a single assertion.** D17 states *"MC 1346382 ... broker_a 22 loads,
broker_c 2"* and *"MC 884201 ... broker_b 12, broker_c 11"* -- both shared
carriers already have broker_c on one side. ``PoolRepository.pool_carriers``
excludes every MC number the *requester* already has anywhere in its own
``carriers`` table (D17: "carriers it has never used"), not just the
requester's own contribution to that one lane. Since broker_c already runs
*both* Ibrahim (its own ``C2``, 2 loads) and Delta Prime (its own ``M1``, 11
loads), broker_c's own pool view excludes both of them entirely -- confirmed
below in ``test_a_broker_already_holding_both_shared_mc_numbers_sees_neither``.
The genuine, checkable cross-broker exposures are:

* **broker_b**, who has never run MC 1346382, sees broker_a's Ibrahim.
* **broker_a**, who has never run MC 884201, sees broker_b's Delta Prime.

That is the same pair of real carriers the brief asks for, read from the
correct pair of (requester, contributor) brokers rather than the pair named in
the brief, which the fixture cannot actually produce. Everything below is
reproducible by running ``backend/scripts`` against the shipped corpus.

**Fixture strategy**, identical to ``test_api.py``'s: ``clean_db`` is
shadowed to a no-op for this module, and :func:`client` truncates once, boots
the real app (bootstrap + full chronological ingest, DECISIONS.md D8), and
truncates again after every test in this file has run. Additive writes
(opt-in rows, audit rows) are cleaned up per test.
"""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.repository import PoolRepository

from .conftest import _truncate


@pytest.fixture(autouse=True)
def clean_db() -> Iterator[None]:
    """Shadow ``conftest.py``'s per-test truncate (see module docstring)."""
    yield


@pytest.fixture(scope="module")
def client(admin_conn: psycopg.Connection) -> Iterator[TestClient]:
    """One full-corpus-backed app for every test in this file (D8's lifespan:
    bootstrap, then the synchronous 132-file ingest)."""
    _truncate(admin_conn)
    admin_conn.execute("TRUNCATE pool_opt_in, pool_audit RESTART IDENTITY CASCADE")
    with TestClient(app) as test_client:
        yield test_client
    admin_conn.execute("TRUNCATE pool_opt_in, pool_audit RESTART IDENTITY CASCADE")
    _truncate(admin_conn)


@pytest.fixture(autouse=True)
def clean_pool_audit(admin_conn: psycopg.Connection) -> Iterator[None]:
    """Truncate ``pool_audit`` around every test in this module.

    ``client`` is module-scoped (the full ingest is too costly to redo per
    test), so without this a read recorded by one test would still be sitting
    there when a later test counts audit rows for the same broker/load pair.
    ``pool_opt_in`` is left to the ``opt_in`` fixture below, which always
    opts its brokers back out -- but truncating it too costs nothing and
    guards against a test that forgets.
    """
    admin_conn.execute("TRUNCATE pool_audit RESTART IDENTITY CASCADE")
    yield
    admin_conn.execute("TRUNCATE pool_audit RESTART IDENTITY CASCADE")


@pytest.fixture
def opt_in(app_conn: psycopg.Connection):
    """Opt brokers in for one test, and always opt them back out afterwards
    -- an additive write cleaned up like ``test_api.py``'s cross-broker
    collision test, never a truncate that would disturb a sibling test."""
    joined: list[str] = []

    def _join(*broker_ids: str) -> None:
        for broker_id in broker_ids:
            PoolRepository.for_broker(app_conn, broker_id).set_opted_in(True)
            joined.append(broker_id)

    yield _join
    for broker_id in joined:
        PoolRepository.for_broker(app_conn, broker_id).set_opted_in(False)


# The real day-11 ACTIVE loads the two genuine cross-broker carriers surface
# on, hand-verified against the ingested corpus (see the module docstring).
_LOAD_SURFACING_IBRAHIM_FOR_BROKER_B = "HD-2026-005053"  # broker_b, DRY_VAN
_LOAD_SURFACING_DELTA_PRIME_FOR_BROKER_A = "127412960"  # broker_a, REEFER

# DAY11-RICH, reused from test_api.py -- untouched-by-pool control loads.
_RICH_A = "127412794"
_RICH_B = "HD-2026-005053"
_RICH_C = "a0jO900000Pe4PRTiG"


# ---------------------------------------------------------------------------
# 1. The two real cross-broker carriers
# ---------------------------------------------------------------------------


def test_broker_b_sees_broker_as_ibrahim_as_a_pool_carrier(
    client: TestClient, opt_in
) -> None:
    """MC 1346382, IBRAHIM TRANSPORT INC -- broker_a's real 20-22-load
    relationship, surfaced to broker_b (who has never run this carrier) at
    the METRO DFW->HOU / dry van bucket, field by field."""
    opt_in("broker_a", "broker_b")
    resp = client.get(
        f"/api/loads/{_LOAD_SURFACING_IBRAHIM_FOR_BROKER_B}/pool-carriers",
        params={"broker_id": "broker_b"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["opted_in"] is True
    assert body["eligible"] is True
    assert body["tier"] == "METRO"
    assert body["lane_key"] == "DFW->HOU"
    assert len(body["carriers"]) == 1

    entry = body["carriers"][0]
    assert entry["source"] == "shared_pool"
    carrier = entry["carrier"]
    assert carrier["mc_number"] == "1346382"
    assert carrier["dot_number"] == "3771394"
    assert carrier["name"] == "IBRAHIM TRANSPORT INC"
    assert carrier["tier"] == "METRO"
    assert carrier["lane_key"] == "DFW->HOU"
    assert carrier["equipment"] == "DRY_VAN"
    assert carrier["load_band"] == "10-19"  # the METRO-tier, dry-van-only relationship
    assert carrier["on_time_band"] == "90+"
    assert carrier["contributor_count"] == 1
    assert carrier["active_recently"] is True
    assert carrier["equipment_operated"] == ["DRY_VAN"]

    # Every shareable field is present and no forbidden one is -- enumerated,
    # not counted.
    assert set(carrier) == {
        "mc_number", "dot_number", "name", "phone", "home_city", "home_state",
        "tier", "lane_key", "equipment", "load_band", "on_time_band",
        "active_recently", "equipment_operated", "contributor_count",
    }

    # broker_a's real total is 22 (D17); broker_c's is 2. Neither exact figure,
    # nor any rate, appears anywhere in the response -- checked as whole
    # phrases, not bare digits (which collide with unrelated floats like the
    # score) so this actually tests for the count, not for the digit 2.
    whole_response = resp.text
    for leak in (
        "20/22", "22 loads", "20 loads", "0.909", "$",
        "carrier_rate", "customer_rate", "rate_per_mile",
    ):
        assert leak not in whole_response, f"{leak!r} leaked into the response body"


def test_broker_a_sees_broker_bs_delta_prime_as_a_pool_carrier(
    client: TestClient, opt_in
) -> None:
    """MC 884201, DELTA PRIME LLC -- broker_b's real relationship, surfaced to
    broker_a (who has never run this carrier) at METRO DFW->HOU / reefer."""
    opt_in("broker_a", "broker_b")
    resp = client.get(
        f"/api/loads/{_LOAD_SURFACING_DELTA_PRIME_FOR_BROKER_A}/pool-carriers",
        params={"broker_id": "broker_a"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["opted_in"] is True
    assert body["eligible"] is True
    assert body["tier"] == "METRO"
    assert body["lane_key"] == "DFW->HOU"
    assert body["equipment_pool"] == "REEFER"

    delta = next(c for c in body["carriers"] if c["carrier"]["mc_number"] == "884201")
    carrier = delta["carrier"]
    assert carrier["dot_number"] == "2551377"
    assert carrier["name"] == "DELTA PRIME LLC"
    assert carrier["home_city"] == "Seguin"
    assert carrier["home_state"] == "TX"
    assert carrier["tier"] == "METRO"
    assert carrier["lane_key"] == "DFW->HOU"
    assert carrier["equipment"] == "REEFER"
    assert carrier["load_band"] == "5-9"
    assert carrier["on_time_band"] == "<75"
    assert carrier["contributor_count"] == 1
    assert set(carrier) == {
        "mc_number", "dot_number", "name", "phone", "home_city", "home_state",
        "tier", "lane_key", "equipment", "load_band", "on_time_band",
        "active_recently", "equipment_operated", "contributor_count",
    }

    whole_response = resp.text
    for leak in (
        "10/12", "10/11", "12 loads", "11 loads",
        "carrier_rate", "customer_rate", "rate_per_mile",
    ):
        assert leak not in whole_response, f"{leak!r} leaked into the response body"


def test_a_broker_already_holding_both_shared_mc_numbers_sees_neither(
    client: TestClient, opt_in
) -> None:
    """broker_c already runs both Ibrahim (its own C2, 2 loads) and Delta
    Prime (its own M1, 11 loads) -- D17's "carriers it has never used"
    excludes both from broker_c's *own* pool section, everywhere, regardless
    of lane. This is the self-exclusion property applied to real fixture
    carriers rather than synthetic ones."""
    opt_in("broker_a", "broker_b", "broker_c")
    for status_load in (
        client.get("/api/loads", params={"broker_id": "broker_c", "status": "ACTIVE"})
        .json()
    ):
        resp = client.get(
            f"/api/loads/{status_load['source_load_id']}/pool-carriers",
            params={"broker_id": "broker_c"},
        )
        body = resp.json()
        mc_numbers = {c["carrier"]["mc_number"] for c in body["carriers"]}
        assert "1346382" not in mc_numbers
        assert "884201" not in mc_numbers


# ---------------------------------------------------------------------------
# 2. Opt-out (and even opt-in) is byte-identical for the ranking and price
# ---------------------------------------------------------------------------


def test_recommendations_and_price_estimate_are_untouched_by_pool_opt_in(
    client: TestClient, opt_in
) -> None:
    """The strongest single guarantee this feature can offer: the ranking and
    price-estimate routes have no field a pool carrier could be assigned to,
    so their output for the real DAY11-RICH loads is *exactly*
    ``test_api.py``'s hand-verified figures whether the broker is opted out
    (the default) or opted in. Reused verbatim from
    ``test_recommendations_and_price_estimate_are_exact_and_unaffected_by_the_other_two_brokers``
    so a regression in either module shows up as a mismatch between them."""
    expectations = {
        "broker_a": dict(load_id=_RICH_A, top_carrier="IBRAHIM TRANSPORT INC", top_score=84.3, p50=1.78),
        "broker_b": dict(load_id=_RICH_B, top_carrier="NORTH TEXAS LINE HAUL INC", top_score=84.6, p50=2.16),
        "broker_c": dict(load_id=_RICH_C, top_carrier="Metroplex Ridge Logistics, Inc.", top_score=83.1, p50=2.51),
    }

    def _snapshot(broker_id: str, load_id: str) -> tuple:
        ranking = client.get(
            f"/api/loads/{load_id}/recommendations", params={"broker_id": broker_id}
        ).json()
        estimate = client.get(
            f"/api/loads/{load_id}/price-estimate", params={"broker_id": broker_id}
        ).json()
        return ranking, estimate

    before = {b: _snapshot(b, e["load_id"]) for b, e in expectations.items()}
    for broker_id, expected in expectations.items():
        ranking, estimate = before[broker_id]
        assert ranking["carriers"][0]["carrier"]["name"] == expected["top_carrier"]
        assert ranking["carriers"][0]["score"] == pytest.approx(expected["top_score"], abs=0.05)
        assert estimate["rate_per_mile_p50"] == pytest.approx(expected["p50"], abs=1e-6)

    # Opt every broker in -- the maximal case for "could pool data leak into
    # the primary ranking" -- and confirm byte-for-byte identity.
    opt_in("broker_a", "broker_b", "broker_c")
    after = {b: _snapshot(b, e["load_id"]) for b, e in expectations.items()}
    assert after == before, "opting every broker into the pool moved the primary ranking/price output"


# ---------------------------------------------------------------------------
# 3. Preconditions and 404s, at the real route
# ---------------------------------------------------------------------------


def test_pool_carriers_route_404s_on_another_brokers_load_id_before_any_pool_logic(
    client: TestClient, opt_in
) -> None:
    opt_in("broker_a", "broker_b")
    resp = client.get(
        f"/api/loads/{_LOAD_SURFACING_IBRAHIM_FOR_BROKER_B}/pool-carriers",
        params={"broker_id": "broker_a"},
    )
    assert resp.status_code == 404
    assert resp.json() == {
        "detail": f"no load {_LOAD_SURFACING_IBRAHIM_FOR_BROKER_B} for broker broker_a"
    }


def test_pool_carriers_route_reports_not_opted_in_without_a_404(client: TestClient) -> None:
    resp = client.get(
        f"/api/loads/{_LOAD_SURFACING_IBRAHIM_FOR_BROKER_B}/pool-carriers",
        params={"broker_id": "broker_b"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["opted_in"] is False
    assert body["eligible"] is False
    assert body["carriers"] == []
    assert "not in the shared carrier pool" in body["basis"]


def test_pool_carriers_route_refuses_a_completed_load_even_when_opted_in(
    client: TestClient, opt_in
) -> None:
    opt_in("broker_b")
    completed = client.get(
        "/api/loads", params={"broker_id": "broker_b", "status": "COMPLETED"}
    ).json()
    assert completed, "fixture must contain at least one COMPLETED broker_b load"
    load_id = completed[0]["source_load_id"]

    resp = client.get(f"/api/loads/{load_id}/pool-carriers", params={"broker_id": "broker_b"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["opted_in"] is True
    assert body["eligible"] is False
    assert body["carriers"] == []
    assert "ACTIVE" in body["basis"]
    assert "COMPLETED" in body["basis"]


# ---------------------------------------------------------------------------
# 4. The audit trail, written by the real route
# ---------------------------------------------------------------------------


def test_pool_carriers_route_writes_exactly_one_audit_row_per_eligible_read(
    client: TestClient, opt_in, admin_conn: psycopg.Connection
) -> None:
    opt_in("broker_a", "broker_b")
    load_id = _LOAD_SURFACING_IBRAHIM_FOR_BROKER_B

    def _audit_count() -> int:
        return admin_conn.execute(
            "SELECT count(*) FROM pool_audit WHERE broker_id = %s AND source_load_id = %s",
            ("broker_b", load_id),
        ).fetchone()[0]

    assert _audit_count() == 0
    client.get(f"/api/loads/{load_id}/pool-carriers", params={"broker_id": "broker_b"})
    assert _audit_count() == 1
    client.get(f"/api/loads/{load_id}/pool-carriers", params={"broker_id": "broker_b"})
    assert _audit_count() == 2  # append, not overwrite

    # And a refused read (not opted in) writes nothing for a different broker.
    before = admin_conn.execute(
        "SELECT count(*) FROM pool_audit WHERE broker_id = 'broker_c'"
    ).fetchone()[0]
    client.get(f"/api/loads/{_RICH_C}/pool-carriers", params={"broker_id": "broker_c"})
    after = admin_conn.execute(
        "SELECT count(*) FROM pool_audit WHERE broker_id = 'broker_c'"
    ).fetchone()[0]
    assert after == before, "an opted-out broker's refused pool read must not be audited"
