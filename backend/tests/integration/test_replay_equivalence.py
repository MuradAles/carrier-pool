"""I10 -- replay-equivalence, the headline correction property (TASKS.md I10).

CLAUDE.md invariant 3: derived stats are *rebuilt* from ``sync_events`` for
touched keys, never incrementally patched, so **a late correction produces the
same numbers as if it had arrived on time.** This file is the check on that
claim, not a restatement of it.

Each correction flavor below runs the same shape twice, against two
independently clean databases:

* **Run A** ingests a sequence where a load's rate arrives wrong and is
  corrected by a later file.
* **Run B** ingests the same lane with the corrected value present from the
  start, and the correcting file removed entirely.

If the rebuild design is right, ``lane_stats`` and ``carrier_stats`` after Run
A must be byte-for-byte identical to Run B -- every percentile, not merely the
corrected load's own ``carrier_rate``. A delta-patching implementation could
easily get the load's own rate right while its lane's percentiles drift, which
is exactly the failure mode a field-by-field comparison catches and a
single-field comparison would miss.

All three correction flavors exercise different code paths and are covered
separately: TMS A restates ``totalBuy`` wholesale, TMS B appends a negative
``ADJUSTMENT`` line -- including the rate-only case where the corrected load is
absent from that file's ``loads`` array -- and TMS C silently restates
``bos__Carrier_Rate__c`` with no marker that it changed.

A fourth test proves the other half of invariant 3's blast radius: a
correction has to dirty not just the narrow key but *every* tier above it
(ZIP3 -> METRO -> REGION -> REGION_ANY), plus the carrier. A missed upper tier
serves a stale answer forever and nothing else in the system would notice.
"""

from __future__ import annotations

import pytest

from app.domain.model import CarrierStats, EntityType, LaneStats
from app.ingestion import ingest_all
from app.repository import BrokerRepository

from .conftest import _truncate
from .sync_fixtures import (
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

pytestmark = pytest.mark.usefixtures("clean_db")

# Dallas 75201 (DFW / zip3 752) -> Houston 77002 (HOU / zip3 770), the same
# lane every fixture builder defaults onto.
_TIER_KEYS: dict[str, dict[str, str]] = {
    "ZIP3": {"origin_key": "752", "dest_key": "770"},
    "METRO": {"origin_key": "DFW", "dest_key": "HOU"},
    "REGION": {"origin_key": "TX_TRIANGLE", "dest_key": "TX_TRIANGLE"},
    "REGION_ANY": {"origin_key": "TX_TRIANGLE", "dest_key": "TX_TRIANGLE"},
}


def _lane_snapshot(repo: BrokerRepository, equipment: str = "DRY_VAN") -> dict[str, LaneStats | None]:
    """Every rung this lane backs, narrowest to widest."""
    snapshot = {
        tier: repo.get_lane_stats(tier=tier, equipment=equipment, **_TIER_KEYS[tier])
        for tier in ("ZIP3", "METRO", "REGION")
    }
    snapshot["REGION_ANY"] = repo.get_lane_stats(tier="REGION_ANY", equipment="ANY", **_TIER_KEYS["REGION_ANY"])
    return snapshot


def _carrier_snapshot(
    repo: BrokerRepository, source_carrier_id: str, *, tier: str = "METRO", equipment: str = "DRY_VAN"
) -> CarrierStats | None:
    keys = _TIER_KEYS[tier]
    lane_key = f"{keys['origin_key']}->{keys['dest_key']}"
    rows = repo.list_carrier_stats(tier=tier, lane_key=lane_key, equipment=equipment)
    return next((r for r in rows if r.source_carrier_id == source_carrier_id), None)


def _assert_populated(lanes: dict[str, LaneStats | None], carrier: CarrierStats | None) -> None:
    for tier, stats in lanes.items():
        assert stats is not None, f"expected a populated {tier} lane_stats row"
    assert carrier is not None, "expected a populated carrier_stats row"


# ---------------------------------------------------------------------------
# Flavor 1: TMS A restates `totalBuy` wholesale
# ---------------------------------------------------------------------------

_CARRIER_A = (700001, "Bluebonnet Freight Systems", "111111", "2222222", "+18005550100")


def test_replay_equivalence_tms_a_restated_total_buy(app_conn, admin_conn, tmp_path_factory):
    root_a = tmp_path_factory.mktemp("replay_a_run_a")
    write_file(
        root_a, TMS_A_DIR, "2026-07-06T00-00_sync.json",
        tms_a_envelope(
            "2026-07-06T00:00:00-05:00",
            [
                tms_a_load(811001, total_buy=600.0, carrier=_CARRIER_A, delivery_date="2026-07-07"),
                tms_a_load(811002, total_buy=700.0, carrier=_CARRIER_A, delivery_date="2026-07-08"),
                tms_a_load(811003, total_buy=650.0, carrier=_CARRIER_A, delivery_date="2026-07-09"),
            ],
        ),
    )
    write_file(
        root_a, TMS_A_DIR, "2026-07-11T00-00_sync.json",
        tms_a_envelope(
            "2026-07-11T00:00:00-05:00",
            # The whole load object reappears, restating `totalBuy` -- PRD
            # section 4 scenario 2.
            [tms_a_load(811001, total_buy=760.0, carrier=_CARRIER_A, delivery_date="2026-07-07")],
        ),
    )
    ingest_all(app_conn, root_a)
    repo = BrokerRepository(app_conn, "broker_a")
    assert repo.get_load("811001").carrier_rate == 760.0  # the correction landed

    run_a_lanes = _lane_snapshot(repo)
    run_a_carrier = _carrier_snapshot(repo, "700001")
    _assert_populated(run_a_lanes, run_a_carrier)

    _truncate(admin_conn)

    root_b = tmp_path_factory.mktemp("replay_a_run_b")
    write_file(
        root_b, TMS_A_DIR, "2026-07-06T00-00_sync.json",
        tms_a_envelope(
            "2026-07-06T00:00:00-05:00",
            [
                # Corrected from the start; the correcting file is gone.
                tms_a_load(811001, total_buy=760.0, carrier=_CARRIER_A, delivery_date="2026-07-07"),
                tms_a_load(811002, total_buy=700.0, carrier=_CARRIER_A, delivery_date="2026-07-08"),
                tms_a_load(811003, total_buy=650.0, carrier=_CARRIER_A, delivery_date="2026-07-09"),
            ],
        ),
    )
    ingest_all(app_conn, root_b)
    run_b_lanes = _lane_snapshot(repo)
    run_b_carrier = _carrier_snapshot(repo, "700001")

    # The headline assertion: correct-then-rebuild equals ingest-corrected-
    # from-start, field by field, including every percentile.
    assert run_a_lanes == run_b_lanes
    assert run_a_carrier == run_b_carrier


# ---------------------------------------------------------------------------
# Flavor 2: TMS B negative ADJUSTMENT, rate-only (load absent from `loads`)
# ---------------------------------------------------------------------------

_CARRIER_B = dict(name="Bayou City Transport LLC", mc_no="1111111", dot_no="2222222")


def test_replay_equivalence_tms_b_rate_only_adjustment(app_conn, admin_conn, tmp_path_factory):
    root_a = tmp_path_factory.mktemp("replay_b_run_a")
    write_file(
        root_a, TMS_B_DIR, "2026-07-06T00-00_sync.json",
        tms_b_envelope(
            "2026-07-06 00:00:00",
            loads=[
                tms_b_load("HD-REPLAY-001", carrier_ref=820001, delivery_date="2026-07-07", del_arrived_at="2026-07-07 14:00:00"),
                tms_b_load("HD-REPLAY-002", carrier_ref=820001, delivery_date="2026-07-08", del_arrived_at="2026-07-08 14:00:00"),
                tms_b_load("HD-REPLAY-003", carrier_ref=820001, delivery_date="2026-07-09", del_arrived_at="2026-07-09 14:00:00"),
            ],
            carriers=[tms_b_carrier(820001, **_CARRIER_B)],
            rates=[
                tms_b_rate(940001, "HD-REPLAY-001", "pay", "LINEHAUL", 550.0),
                tms_b_rate(940002, "HD-REPLAY-001", "pay", "FUEL", 120.0),
                tms_b_rate(940003, "HD-REPLAY-001", "bill", "LINEHAUL", 900.0),
                tms_b_rate(940004, "HD-REPLAY-002", "pay", "LINEHAUL", 600.0),
                tms_b_rate(940005, "HD-REPLAY-002", "bill", "LINEHAUL", 950.0),
                tms_b_rate(940006, "HD-REPLAY-003", "pay", "LINEHAUL", 580.0),
                tms_b_rate(940007, "HD-REPLAY-003", "bill", "LINEHAUL", 930.0),
            ],
        ),
    )
    write_file(
        root_a, TMS_B_DIR, "2026-07-12T00-00_sync.json",
        tms_b_envelope(
            "2026-07-12 00:00:00",
            # The trap CLAUDE.md documents for HD-2026-004733: `loads` holds
            # only the two OTHER loads, never the one being corrected.
            loads=[
                tms_b_load("HD-REPLAY-002", carrier_ref=820001, delivery_date="2026-07-08", del_arrived_at="2026-07-08 14:00:00"),
                tms_b_load("HD-REPLAY-003", carrier_ref=820001, delivery_date="2026-07-09", del_arrived_at="2026-07-09 14:00:00"),
            ],
            rates=[tms_b_rate(940008, "HD-REPLAY-001", "pay", "ADJUSTMENT", -70.0)],
        ),
    )
    ingest_all(app_conn, root_a)
    repo = BrokerRepository(app_conn, "broker_b")
    assert repo.get_load("HD-REPLAY-001").carrier_rate == pytest.approx(600.0)  # 550 + 120 - 70

    # The trap really is representable: an event was recorded, and the raw
    # file genuinely never mentions this load in its `loads` array.
    events = repo.events_for_load("HD-REPLAY-001")
    adjustment = [
        e for e in events
        if e.entity_type is EntityType.RATE_LINE and e.raw_json.get("code") == "ADJUSTMENT"
    ]
    assert len(adjustment) == 1
    assert adjustment[0].sync_file == "2026-07-12T00-00_sync.json"

    run_a_lanes = _lane_snapshot(repo)
    run_a_carrier = _carrier_snapshot(repo, "820001")
    _assert_populated(run_a_lanes, run_a_carrier)

    _truncate(admin_conn)

    root_b = tmp_path_factory.mktemp("replay_b_run_b")
    write_file(
        root_b, TMS_B_DIR, "2026-07-06T00-00_sync.json",
        tms_b_envelope(
            "2026-07-06 00:00:00",
            loads=[
                tms_b_load("HD-REPLAY-001", carrier_ref=820001, delivery_date="2026-07-07", del_arrived_at="2026-07-07 14:00:00"),
                tms_b_load("HD-REPLAY-002", carrier_ref=820001, delivery_date="2026-07-08", del_arrived_at="2026-07-08 14:00:00"),
                tms_b_load("HD-REPLAY-003", carrier_ref=820001, delivery_date="2026-07-09", del_arrived_at="2026-07-09 14:00:00"),
            ],
            carriers=[tms_b_carrier(820001, **_CARRIER_B)],
            rates=[
                tms_b_rate(940001, "HD-REPLAY-001", "pay", "LINEHAUL", 550.0),
                tms_b_rate(940002, "HD-REPLAY-001", "pay", "FUEL", 120.0),
                # The corrected value present from the start; the correcting
                # file is gone entirely.
                tms_b_rate(940008, "HD-REPLAY-001", "pay", "ADJUSTMENT", -70.0),
                tms_b_rate(940003, "HD-REPLAY-001", "bill", "LINEHAUL", 900.0),
                tms_b_rate(940004, "HD-REPLAY-002", "pay", "LINEHAUL", 600.0),
                tms_b_rate(940005, "HD-REPLAY-002", "bill", "LINEHAUL", 950.0),
                tms_b_rate(940006, "HD-REPLAY-003", "pay", "LINEHAUL", 580.0),
                tms_b_rate(940007, "HD-REPLAY-003", "bill", "LINEHAUL", 930.0),
            ],
        ),
    )
    ingest_all(app_conn, root_b)
    run_b_lanes = _lane_snapshot(repo)
    run_b_carrier = _carrier_snapshot(repo, "820001")

    assert run_a_lanes == run_b_lanes
    assert run_a_carrier == run_b_carrier


# ---------------------------------------------------------------------------
# Flavor 3: TMS C silently restates `bos__Carrier_Rate__c`, no marker
# ---------------------------------------------------------------------------


def _tms_c_refs() -> dict:
    return {
        "cust-replay": tms_c_account_ref("Replay Shipper", role="Customer"),
        "carr-replay": tms_c_account_ref("Replay Carrier", role="Carrier", mc_number="333333", dot_number="4444444"),
        "loc-origin": tms_c_location_ref("Dallas", "TX", "75201"),
        "loc-dest": tms_c_location_ref("Houston", "TX", "77002"),
    }


def test_replay_equivalence_tms_c_silent_carrier_rate_restatement(app_conn, admin_conn, tmp_path_factory):
    root_a = tmp_path_factory.mktemp("replay_c_run_a")
    write_file(
        root_a, TMS_C_DIR, "2026-07-06T00-00_sync.json",
        tms_c_envelope(
            "2026-07-06T05:00:00.000+0000",
            [
                tms_c_record(
                    "rec-replay-1", "SHP-REPLAY-1", customer_ref="cust-replay", carrier_ref="carr-replay",
                    carrier_rate=500.0, delivery_date="2026-07-07", delivery_arrival="2026-07-07T19:00:00.000+0000",
                ),
                tms_c_record(
                    "rec-replay-2", "SHP-REPLAY-2", customer_ref="cust-replay", carrier_ref="carr-replay",
                    carrier_rate=700.0, delivery_date="2026-07-08", delivery_arrival="2026-07-08T19:00:00.000+0000",
                ),
                tms_c_record(
                    "rec-replay-3", "SHP-REPLAY-3", customer_ref="cust-replay", carrier_ref="carr-replay",
                    carrier_rate=650.0, delivery_date="2026-07-09", delivery_arrival="2026-07-09T19:00:00.000+0000",
                ),
            ],
            _tms_c_refs(),
        ),
    )
    write_file(
        root_a, TMS_C_DIR, "2026-07-11T05-00_sync.json",
        tms_c_envelope(
            "2026-07-11T05:00:00.000+0000",
            # Silent restatement: same record id, a different
            # `bos__Carrier_Rate__c`, no field anywhere marking it as changed.
            [
                tms_c_record(
                    "rec-replay-1", "SHP-REPLAY-1", customer_ref="cust-replay", carrier_ref="carr-replay",
                    carrier_rate=580.0, delivery_date="2026-07-07", delivery_arrival="2026-07-07T19:00:00.000+0000",
                )
            ],
            _tms_c_refs(),
        ),
    )
    ingest_all(app_conn, root_a)
    repo = BrokerRepository(app_conn, "broker_c")
    assert repo.get_load("rec-replay-1").carrier_rate == 580.0

    run_a_lanes = _lane_snapshot(repo)
    run_a_carrier = _carrier_snapshot(repo, "carr-replay")
    _assert_populated(run_a_lanes, run_a_carrier)

    _truncate(admin_conn)

    root_b = tmp_path_factory.mktemp("replay_c_run_b")
    write_file(
        root_b, TMS_C_DIR, "2026-07-06T00-00_sync.json",
        tms_c_envelope(
            "2026-07-06T05:00:00.000+0000",
            [
                tms_c_record(
                    "rec-replay-1", "SHP-REPLAY-1", customer_ref="cust-replay", carrier_ref="carr-replay",
                    # Correct from the start; the restating file is gone.
                    carrier_rate=580.0, delivery_date="2026-07-07", delivery_arrival="2026-07-07T19:00:00.000+0000",
                ),
                tms_c_record(
                    "rec-replay-2", "SHP-REPLAY-2", customer_ref="cust-replay", carrier_ref="carr-replay",
                    carrier_rate=700.0, delivery_date="2026-07-08", delivery_arrival="2026-07-08T19:00:00.000+0000",
                ),
                tms_c_record(
                    "rec-replay-3", "SHP-REPLAY-3", customer_ref="cust-replay", carrier_ref="carr-replay",
                    carrier_rate=650.0, delivery_date="2026-07-09", delivery_arrival="2026-07-09T19:00:00.000+0000",
                ),
            ],
            _tms_c_refs(),
        ),
    )
    ingest_all(app_conn, root_b)
    run_b_lanes = _lane_snapshot(repo)
    run_b_carrier = _carrier_snapshot(repo, "carr-replay")

    assert run_a_lanes == run_b_lanes
    assert run_a_carrier == run_b_carrier


# ---------------------------------------------------------------------------
# A correction dirties the carrier, the lane, and every tier above it
# ---------------------------------------------------------------------------


def test_correction_dirties_the_carrier_the_lane_and_every_tier_above_it(app_conn, tmp_path):
    """The rate-only TMS B trap again, but the assertion this time is about
    *blast radius* rather than equivalence: every one of ZIP3, METRO, REGION,
    REGION_ANY, and the carrier's own row has to change. A rebuild that
    stopped at the narrow key would leave a stale METRO or REGION answer that
    nothing else in the system would ever notice was wrong."""
    write_file(
        tmp_path, TMS_B_DIR, "2026-07-06T00-00_sync.json",
        tms_b_envelope(
            "2026-07-06 00:00:00",
            loads=[
                tms_b_load("HD-DIRTY-001", carrier_ref=830001, delivery_date="2026-07-07", del_arrived_at="2026-07-07 14:00:00"),
                tms_b_load("HD-DIRTY-002", carrier_ref=830001, delivery_date="2026-07-08", del_arrived_at="2026-07-08 14:00:00"),
            ],
            carriers=[tms_b_carrier(830001, name="Dirty Tier Transport", mc_no="5555555", dot_no="6666666")],
            rates=[
                tms_b_rate(950001, "HD-DIRTY-001", "pay", "LINEHAUL", 500.0),
                tms_b_rate(950002, "HD-DIRTY-001", "bill", "LINEHAUL", 800.0),
                tms_b_rate(950003, "HD-DIRTY-002", "pay", "LINEHAUL", 600.0),
                tms_b_rate(950004, "HD-DIRTY-002", "bill", "LINEHAUL", 900.0),
            ],
        ),
    )
    ingest_all(app_conn, tmp_path)
    repo = BrokerRepository(app_conn, "broker_b")
    assert repo.get_load("HD-DIRTY-001").carrier_rate == 500.0

    before_lanes = _lane_snapshot(repo)
    before_carrier = _carrier_snapshot(repo, "830001")
    _assert_populated(before_lanes, before_carrier)

    write_file(
        tmp_path, TMS_B_DIR, "2026-07-10T00-00_sync.json",
        tms_b_envelope(
            "2026-07-10 00:00:00",
            # HD-DIRTY-001 is absent from `loads` -- a pure rate-line correction.
            loads=[tms_b_load("HD-DIRTY-002", carrier_ref=830001, delivery_date="2026-07-08", del_arrived_at="2026-07-08 14:00:00")],
            rates=[tms_b_rate(950005, "HD-DIRTY-001", "pay", "ADJUSTMENT", -80.0)],
        ),
    )
    ingest_all(app_conn, tmp_path)
    assert repo.get_load("HD-DIRTY-001").carrier_rate == 420.0

    after_lanes = _lane_snapshot(repo)
    after_carrier = _carrier_snapshot(repo, "830001")

    for tier in ("ZIP3", "METRO", "REGION", "REGION_ANY"):
        assert after_lanes[tier] != before_lanes[tier], f"{tier} lane_stats did not change"
    assert after_carrier != before_carrier
