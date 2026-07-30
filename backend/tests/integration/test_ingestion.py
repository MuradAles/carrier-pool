"""I9 -- ingestion mechanics against a real Postgres (TASKS.md I9).

CLAUDE.md invariants 3 and 4, plus the "Known traps" section, are the
specification. Every test starts from a known DB state (``clean_db``,
inherited from ``conftest.py``) and asserts numerically -- row counts, summed
totals, exact event ordering -- never "looks right".

Sections:

1. Filename order is genuinely cross-directory, not per-TMS-directory.
2. A later sync overwrites current truth, and the earlier version stays
   retrievable from ``sync_events``.
3. Idempotency: row counts *and* summed rate totals survive a re-ingest, ten
   repeats, and an interleaved-broker run.
4. Processing order is load-bearing: bypassing the sort produces a different,
   wrong answer.
5. Partial failure mid-file leaves no half-written state.
6. ``sync_events`` is genuinely append-only -- by grant, not by convention.
7. An ``ACTIVE`` load's null rate is excluded from rate stats, not counted as
   a zero.
8. The whole 132-file corpus: the numbers `team-lead` verified by hand, turned
   into a real, running assertion (TASKS.md I9's "reconciles exactly with
   TRACEABILITY.md" requirement, and the corpus-scale idempotency check).
"""

from __future__ import annotations

import json
from pathlib import Path

import psycopg
import pytest

from app.domain.model import EntityType, LoadStatus
from app.ingestion import discover_sync_files, ingest_all, ingest_sync_file
from app.repository import BrokerRepository, list_brokers
from app.repository.broker_repository import broker_session

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

# The repo root's data/ directory -- .../carrier-pool/data -- independent of
# the working directory a test runner happens to be invoked from.
_DATA_ROOT = Path(__file__).resolve().parents[3] / "data"


# ---------------------------------------------------------------------------
# 1. Cross-directory chronological order (I1)
# ---------------------------------------------------------------------------


def test_files_ordered_by_filename_timestamp_across_all_three_tms_directories(tmp_path, app_conn):
    """The merged order interleaves brokers by filename clock -- it is not
    each directory's own files concatenated together. A per-directory
    implementation would emit broker_a's two files, then broker_b's, then
    broker_c's; the true cross-directory sort interleaves them by time."""
    brokers = list_brokers(app_conn)

    write_file(tmp_path, TMS_A_DIR, "2026-07-06T00-00_sync.json", {"syncedAt": "2026-07-06T00:00:00-05:00", "loads": []})
    write_file(tmp_path, TMS_A_DIR, "2026-07-06T12-00_sync.json", {"syncedAt": "2026-07-06T12:00:00-05:00", "loads": []})
    write_file(
        tmp_path, TMS_B_DIR, "2026-07-06T06-00_sync.json",
        {"synced_at": "2026-07-06 06:00:00", "loads": [], "carriers": [], "rates": []},
    )
    write_file(
        tmp_path, TMS_B_DIR, "2026-07-06T18-00_sync.json",
        {"synced_at": "2026-07-06 18:00:00", "loads": [], "carriers": [], "rates": []},
    )
    write_file(
        tmp_path, TMS_C_DIR, "2026-07-06T03-00_sync.json",
        {"synced_at": "2026-07-06T08:00:00.000+0000", "records": [], "referenced_records": {}},
    )
    write_file(
        tmp_path, TMS_C_DIR, "2026-07-06T09-00_sync.json",
        {"synced_at": "2026-07-06T14:00:00.000+0000", "records": [], "referenced_records": {}},
    )

    discovered = discover_sync_files(tmp_path, brokers)
    order = [(d.sync_file, d.broker.id) for d in discovered]

    assert order == [
        ("2026-07-06T00-00_sync.json", "broker_a"),
        ("2026-07-06T03-00_sync.json", "broker_c"),
        ("2026-07-06T06-00_sync.json", "broker_b"),
        ("2026-07-06T09-00_sync.json", "broker_c"),
        ("2026-07-06T12-00_sync.json", "broker_a"),
        ("2026-07-06T18-00_sync.json", "broker_b"),
    ]

    # The wrong shape a per-directory implementation would produce: each
    # broker's own two files adjacent, in whatever order brokers were visited.
    per_directory_concat = [
        f for broker in brokers for f, b in order if b == broker.id
    ]
    assert [f for f, _ in order] != per_directory_concat


def test_a_non_json_neighbour_is_skipped_but_a_misnamed_json_file_raises(
    tmp_path, app_conn
):
    """D25: the module docstring claimed *every* non-matching name raises, and
    the code skips anything not ending ``.json``.

    Both halves are deliberate and both are asserted here, because the doc used
    to describe only one of them. The extension is the "is this data" test --
    all three shipped TMS directories hold the assignment's annotated
    ``example_sync.jsonc`` beside the real syncs, so a rule that raised on those
    would fail every clean-checkout run. The filename pattern is the "is this
    data well-formed" test, and there raising is right: a ``.json`` file with no
    place in the chronological order is data we would be silently dropping
    (invariant 4).
    """
    brokers = list_brokers(app_conn)
    envelope = {"syncedAt": "2026-07-06T00:00:00-05:00", "loads": []}
    write_file(tmp_path, TMS_A_DIR, "2026-07-06T00-00_sync.json", envelope)
    write_file(tmp_path, TMS_A_DIR, "example_sync.jsonc", envelope)
    (tmp_path / TMS_A_DIR / "notes.txt").write_text("scratch", encoding="utf-8")

    discovered = discover_sync_files(tmp_path, brokers)
    assert [d.sync_file for d in discovered] == ["2026-07-06T00-00_sync.json"]

    write_file(tmp_path, TMS_A_DIR, "monday.json", envelope)
    with pytest.raises(ValueError, match="does not match"):
        discover_sync_files(tmp_path, brokers)


# ---------------------------------------------------------------------------
# 2. Overwrite + audit trail
# ---------------------------------------------------------------------------


def test_later_sync_overwrites_earlier_truth_and_earlier_version_stays_in_sync_events(tmp_path, app_conn):
    write_file(
        tmp_path, TMS_A_DIR, "2026-07-06T00-00_sync.json",
        tms_a_envelope("2026-07-06T00:00:00-05:00", [tms_a_load(920001, total_buy=500.0)]),
    )
    write_file(
        tmp_path, TMS_A_DIR, "2026-07-08T00-00_sync.json",
        tms_a_envelope("2026-07-08T00:00:00-05:00", [tms_a_load(920001, total_buy=650.0)]),
    )

    ingest_all(app_conn, tmp_path)

    repo = BrokerRepository(app_conn, "broker_a")
    load = repo.get_load("920001")
    assert load.carrier_rate == 650.0  # later sync is current truth

    events = repo.events_for_load("920001")
    assert [e.sync_file for e in events] == [
        "2026-07-06T00-00_sync.json",
        "2026-07-08T00-00_sync.json",
    ]
    # Both versions genuinely retrievable -- the earlier truth was superseded
    # in `loads`, never overwritten or deleted in `sync_events`.
    assert [e.raw_json["totalBuy"] for e in events] == [500.0, 650.0]


# ---------------------------------------------------------------------------
# 3. Idempotency
# ---------------------------------------------------------------------------


def test_reingest_changes_no_row_and_no_summed_rate_total(tmp_path, app_conn):
    """The likely failure is a double-counted TMS B line item, which would not
    show up in a row-count check alone -- hence asserting the summed total
    too."""
    write_file(
        tmp_path, TMS_B_DIR, "2026-07-06T00-00_sync.json",
        tms_b_envelope(
            "2026-07-06 00:00:00",
            loads=[tms_b_load("HD-IDEMP-1"), tms_b_load("HD-IDEMP-2")],
            carriers=[tms_b_carrier(800002, name="Idempotent Carriers LLC", mc_no="9999991", dot_no="9999992")],
            rates=[
                tms_b_rate(930001, "HD-IDEMP-1", "pay", "LINEHAUL", 500.0),
                tms_b_rate(930002, "HD-IDEMP-1", "pay", "FUEL", 100.0),
                tms_b_rate(930003, "HD-IDEMP-1", "bill", "LINEHAUL", 800.0),
                tms_b_rate(930004, "HD-IDEMP-2", "pay", "LINEHAUL", 450.0),
                tms_b_rate(930005, "HD-IDEMP-2", "bill", "LINEHAUL", 700.0),
            ],
        ),
    )

    first = ingest_all(app_conn, tmp_path)
    assert (first.files_ingested, first.files_skipped) == (1, 0)

    repo = BrokerRepository(app_conn, "broker_b")
    rates_before = {l.source_load_id: l.carrier_rate for l in repo.list_loads()}
    assert rates_before == {"HD-IDEMP-1": 600.0, "HD-IDEMP-2": 450.0}

    second = ingest_all(app_conn, tmp_path)
    assert (second.files_ingested, second.files_skipped) == (0, 1)

    rates_after = {l.source_load_id: l.carrier_rate for l in repo.list_loads()}
    assert rates_after == rates_before  # not 1200.0 / 900.0 -- would be, if
    # the rate lines were re-applied on top of the running sum.
    assert sum(rates_after.values()) == pytest.approx(1050.0)


def test_reingesting_ten_times_while_interleaving_two_brokers_stays_idempotent(tmp_path, app_conn):
    write_file(
        tmp_path, TMS_A_DIR, "2026-07-06T00-00_sync.json",
        tms_a_envelope("2026-07-06T00:00:00-05:00", [tms_a_load(930001, total_buy=800.0)]),
    )
    write_file(
        tmp_path, TMS_C_DIR, "2026-07-06T06-00_sync.json",
        tms_c_envelope(
            "2026-07-06T11:00:00.000+0000",
            [tms_c_record("rec-idemp-1", "SHP-IDEMP-1", carrier_rate=555.0)],
            {
                "cust-1": tms_c_account_ref("Idempotent Shipper", role="Customer"),
                "carr-1": tms_c_account_ref("Idempotent Carrier", role="Carrier", mc_number="1", dot_number="2"),
                "loc-origin": tms_c_location_ref("Dallas", "TX", "75201"),
                "loc-dest": tms_c_location_ref("Houston", "TX", "77002"),
            },
        ),
    )

    for i in range(10):
        report = ingest_all(app_conn, tmp_path)
        expected = (2, 0) if i == 0 else (0, 2)
        assert (report.files_ingested, report.files_skipped) == expected

    repo_a = BrokerRepository(app_conn, "broker_a")
    repo_c = BrokerRepository(app_conn, "broker_c")
    assert len(repo_a.list_loads()) == 1
    assert len(repo_c.list_loads()) == 1
    assert repo_a.get_load("930001").carrier_rate == 800.0
    assert repo_c.get_load("rec-idemp-1").carrier_rate == 555.0


# ---------------------------------------------------------------------------
# 4. Processing order is load-bearing
# ---------------------------------------------------------------------------


def test_out_of_order_processing_gives_a_different_wrong_answer_than_the_real_pipeline(
    tmp_path, app_conn, admin_conn
):
    """`ingest_all`/`discover_sync_files` sort by filename regardless of listing
    order (I1); this proves that sort is load-bearing rather than cosmetic by
    bypassing it -- feeding the identical three files to `ingest_sync_file`
    directly, in a shuffled order -- and showing the final answer changes."""
    write_file(
        tmp_path, TMS_A_DIR, "2026-07-06T00-00_sync.json",
        tms_a_envelope("2026-07-06T00:00:00-05:00", [tms_a_load(940001, total_buy=500.0)]),
    )
    write_file(
        tmp_path, TMS_A_DIR, "2026-07-08T00-00_sync.json",
        tms_a_envelope("2026-07-08T00:00:00-05:00", [tms_a_load(940002, total_buy=999.0)]),
    )
    write_file(
        tmp_path, TMS_A_DIR, "2026-07-10T00-00_sync.json",
        tms_a_envelope("2026-07-10T00:00:00-05:00", [tms_a_load(940001, total_buy=760.0)]),
    )

    # The real pipeline: always chronological, whatever order the files were
    # discovered or created in.
    ingest_all(app_conn, tmp_path)
    repo = BrokerRepository(app_conn, "broker_a")
    assert repo.get_load("940001").carrier_rate == 760.0  # the correction wins

    _truncate(admin_conn)

    brokers = list_brokers(app_conn)
    discovered = {d.sync_file: d for d in discover_sync_files(tmp_path, brokers)}
    shuffled = [
        "2026-07-10T00-00_sync.json",  # the correction, applied first
        "2026-07-06T00-00_sync.json",  # the original wrong value, applied last
        "2026-07-08T00-00_sync.json",
    ]
    for name in shuffled:
        ingest_sync_file(app_conn, discovered[name])

    # Wrong: the stale value overwrote the correction, because it was applied
    # last in this (deliberately incorrect) processing order.
    assert repo.get_load("940001").carrier_rate == 500.0
    assert repo.get_load("940001").carrier_rate != 760.0


# ---------------------------------------------------------------------------
# 5. Partial failure mid-file leaves no half-written state
# ---------------------------------------------------------------------------


def test_partial_failure_mid_file_leaves_no_trace_and_is_retried_cleanly(tmp_path, app_conn, monkeypatch):
    write_file(
        tmp_path, TMS_A_DIR, "2026-07-06T00-00_sync.json",
        tms_a_envelope(
            "2026-07-06T00:00:00-05:00",
            [tms_a_load(950001, total_buy=500.0), tms_a_load(950002, total_buy=600.0)],
        ),
    )

    brokers = list_brokers(app_conn)
    [discovered] = discover_sync_files(tmp_path, brokers)  # only broker_a's dir exists

    import app.ingestion.pipeline as pipeline_module

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated failure mid-file")

    monkeypatch.setattr(pipeline_module, "_rebuild_lane_key", _boom)

    with pytest.raises(RuntimeError, match="simulated failure mid-file"):
        ingest_sync_file(app_conn, discovered)

    repo = BrokerRepository(app_conn, "broker_a")
    # No trace: not claimed, no events, no upserted loads.
    assert repo.sync_file_id("2026-07-06T00-00_sync.json") is None
    assert repo.get_load("950001") is None
    assert repo.get_load("950002") is None
    assert repo.events_for_load("950001") == []
    assert repo.events_for_load("950002") == []

    monkeypatch.undo()  # restore the real _rebuild_lane_key

    # Retried on the next run, cleanly -- nothing was recorded, so this is an
    # ordinary first ingest, not a skip.
    outcome = ingest_sync_file(app_conn, discovered)
    assert outcome.ingested is True
    assert repo.get_load("950001").carrier_rate == 500.0
    assert repo.get_load("950002").carrier_rate == 600.0


# ---------------------------------------------------------------------------
# 6. sync_events is genuinely append-only
# ---------------------------------------------------------------------------


def test_sync_events_grants_permit_only_select_and_insert(admin_conn):
    """The privilege set itself, not just observed behavior -- so a future
    grant change shows up here even before anything tries to exploit it."""
    rows = admin_conn.execute(
        "SELECT privilege_type FROM information_schema.role_table_grants"
        " WHERE table_name = 'sync_events' AND grantee = 'carrier_pool_app'"
    ).fetchall()
    assert {r[0] for r in rows} == {"SELECT", "INSERT"}


def test_sync_events_update_and_delete_raise_even_with_a_broker_bound(app_conn):
    """With a broker legitimately bound (so RLS itself is satisfied), an
    UPDATE or DELETE against sync_events still has no privilege to succeed on
    -- append-only is a grant, not a check ingestion happens to pass."""
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="permission denied"):
        with broker_session(app_conn, "broker_a") as cur:
            cur.execute("UPDATE sync_events SET event_seq = 0")
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="permission denied"):
        with broker_session(app_conn, "broker_a") as cur:
            cur.execute("DELETE FROM sync_events")


# ---------------------------------------------------------------------------
# 7. ACTIVE loads: a null rate is not a zero rate
# ---------------------------------------------------------------------------


def test_active_loads_null_rate_excluded_from_rate_stats_not_counted_as_zero(tmp_path, app_conn):
    write_file(
        tmp_path, TMS_A_DIR, "2026-07-06T00-00_sync.json",
        tms_a_envelope(
            "2026-07-06T00:00:00-05:00",
            [
                tms_a_load(960001, status="Completed", total_buy=800.0),
                tms_a_load(960002, status="Completed", total_buy=1000.0),
                tms_a_load(960003, status="Booking", total_buy=None, carrier=None),
            ],
        ),
    )
    ingest_all(app_conn, tmp_path)

    repo = BrokerRepository(app_conn, "broker_a")
    active_load = repo.get_load("960003")
    assert active_load.status == LoadStatus.ACTIVE
    assert active_load.carrier_rate is None  # not 0.0

    lane = repo.get_lane_stats(tier="ZIP3", origin_key="752", dest_key="770", equipment="DRY_VAN")
    assert lane is not None
    assert lane.load_count == 2  # the ACTIVE load does not vote

    rated = [800.0 / 271.0, 1000.0 / 271.0]
    expected_median = sum(rated) / 2  # a straight average of the two rated loads
    # lane_stats.rate_per_mile_p50 is NUMERIC(10, 4); match its own precision
    # rather than comparing against an unrounded Python float.
    assert lane.rate_per_mile_p50 == pytest.approx(expected_median, abs=1e-4)
    # If the ACTIVE load's missing rate had been folded in as a zero, the
    # median would collapse toward it instead of sitting between the two
    # real rates.
    assert lane.rate_per_mile_p50 > min(rated)


# ---------------------------------------------------------------------------
# 8. The whole 132-file corpus: hand-verified numbers, made into real tests
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _DATA_ROOT.is_dir(), reason=f"fixture corpus not found at {_DATA_ROOT}")
def test_full_corpus_ingest_matches_hand_verified_totals_and_stays_idempotent(app_conn, admin_conn):
    """Every number here was independently verified by hand before this test
    existed (see the task message this suite was written from) -- this makes
    that verification a real, re-runnable assertion instead of a claim."""
    report = ingest_all(app_conn, _DATA_ROOT)
    assert (report.files_discovered, report.files_ingested, report.files_skipped) == (132, 132, 0)
    assert report.events_written == 1156
    assert report.loads_touched == 316
    assert report.lane_keys_rebuilt == 1556
    assert report.carriers_repositioned == 261

    def _snapshot():
        return admin_conn.execute(
            "SELECT"
            " (SELECT count(*) FROM loads),"
            " (SELECT count(*) FROM carriers),"
            " (SELECT count(*) FROM customers),"
            " (SELECT count(*) FROM lane_stats),"
            " (SELECT count(*) FROM carrier_stats),"
            " (SELECT sum(carrier_rate) FROM loads),"
            " (SELECT sum(customer_rate) FROM loads)"
        ).fetchone()

    loads, carriers, customers, lane_stats, carrier_stats, carrier_total, customer_total = _snapshot()
    assert (loads, carriers, customers, lane_stats, carrier_stats) == (295, 36, 18, 445, 884)
    # 295 = 93 history loads per broker x 3 brokers + 16 day-11 loads (D1, DG7).
    assert loads == 93 * 3 + 16
    assert float(carrier_total) == pytest.approx(150716.43, abs=0.01)
    assert float(customer_total) == pytest.approx(186840.92, abs=0.01)

    # Re-ingest: a genuine no-op, at corpus scale -- row counts *and* summed
    # totals, since a double-counted rate line would not show up in the former.
    second = ingest_all(app_conn, _DATA_ROOT)
    assert (second.files_ingested, second.files_skipped) == (0, 132)
    assert _snapshot() == (loads, carriers, customers, lane_stats, carrier_stats, carrier_total, customer_total)

    # The rate-only ADJUSTMENT trap: HD-2026-004733 ends at 702.80 (674.70
    # LINEHAUL + 148.10 FUEL - 120.00 ADJUSTMENT), where the -120 arrives in a
    # file whose `loads` array never mentions this load.
    repo_b = BrokerRepository(app_conn, "broker_b")
    corrected = repo_b.get_load("HD-2026-004733")
    assert corrected.carrier_rate == pytest.approx(702.80)

    correcting_file = json.loads(
        (_DATA_ROOT / "tms_b_hauldesk" / "2026-07-12T06-00_sync.json").read_text(encoding="utf-8")
    )
    mentioned = {row["load_num"] for row in correcting_file["loads"]}
    assert "HD-2026-004733" not in mentioned
    assert mentioned == {"HD-2026-004817", "HD-2026-004821", "HD-2026-004832"}

    events = repo_b.events_for_load("HD-2026-004733")
    adjustment_events = [
        e for e in events
        if e.entity_type is EntityType.RATE_LINE and e.raw_json.get("code") == "ADJUSTMENT"
    ]
    assert len(adjustment_events) == 1
    assert adjustment_events[0].sync_file == "2026-07-12T06-00_sync.json"
    assert adjustment_events[0].raw_json["amount_usd"] == -120.0

    # Reconciles with data/TRACEABILITY.md's independently-computed medians
    # for the rich ZIP3 lane 750->774, DRY_VAN -- 12 loads in every broker.
    expected = {
        "broker_a": (1.750, 1.780, 1.805),
        "broker_b": (2.0925, 2.160, 2.1775),
        "broker_c": (2.480, 2.510, 2.520),
    }
    for broker_id, (p25, p50, p75) in expected.items():
        lane = BrokerRepository(app_conn, broker_id).get_lane_stats(
            tier="ZIP3", origin_key="750", dest_key="774", equipment="DRY_VAN"
        )
        assert lane is not None
        assert lane.load_count == 12
        assert (lane.rate_per_mile_p25, lane.rate_per_mile_p50, lane.rate_per_mile_p75) == pytest.approx(
            (p25, p50, p75), abs=1e-6
        )
