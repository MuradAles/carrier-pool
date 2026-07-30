"""FINDING 6 — the dirty-key rebuild is not concurrency-safe.

``_rebuild_lane_key`` (``app/ingestion/pipeline.py``) is DELETE-then-INSERT with
no lock and no upsert. Two transactions rebuilding the same lane key both find
the row gone and both insert, so one loses with ``UniqueViolation``; two
transactions rebuilding *overlapping sets* of keys in different orders can take
each other's row locks and one loses with ``DeadlockDetected``. Reachable
whenever two ingests overlap: two app replicas on one database, or
``POST /api/admin/ingest`` fired while D8's synchronous startup ingest is still
running.

**Why this is its own module, on its own database.**

The obvious test — run N ingests at once and assert no exception — is exactly
the kind of test that must not be written here. Whether the race opens depends
on how loaded the Postgres server is at that instant, so it passes on an idle
machine and fails when a colleague's suite happens to be running. I measured
that directly: 8 of 12 consecutive runs failed while another suite was hammering
the same container, then 12 of 12 passed once it was idle, then 12 rounds of
8-way concurrent ingest produced zero errors. A test with that behaviour is a
worse defect than the bug it documents, and it would poison the single
``pytest tests`` that Phase 10's X4 rehearses.

So the assertions here are the two properties that hold **regardless of
timing**:

* :func:`test_concurrent_ingest_crashes_but_never_corrupts` — however many
  ingests collide and however many raise, the surviving data is always exactly
  right. One file is one transaction, so a loser rolls its whole file back and
  leaves no trace; the file is simply retried on the next run.
* :func:`test_the_delete_then_insert_rebuild_loses_a_deliberate_race` — the
  mechanism itself, driven deterministically. The second transaction is
  confirmed *actually blocked* on the first's row lock, by reading
  ``pg_stat_activity``, before the first commits. No sleeps, no guessing.

A dedicated database, and a no-op override of the package's autouse
``clean_db``, so a deadlock here can never cascade into another test's fixture
teardown.
"""

from __future__ import annotations

import threading
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from app.ingestion import ingest_all
from app.repository import BrokerRepository, db
from app.repository.broker_repository import broker_session

from ..integration.sync_fixtures import (
    TMS_A_DIR,
    TMS_B_DIR,
    tms_a_envelope,
    tms_a_load,
    tms_b_carrier,
    tms_b_envelope,
    tms_b_load,
    tms_b_rate,
    write_file,
)
from .conftest import (
    BREAKER_DB,
    TENANT_TABLES,
    _drop_database,
    _ensure_database,
    bootstrap_serialised,
)

#: Suffixed off this session's own breaker database, so it inherits the
#: per-process isolation and the abandoned-database sweep.
CONCURRENCY_DB = f"{BREAKER_DB}_conc"
_HOST = "localhost:5432"
CONC_ADMIN_URL = f"postgresql://carrier:carrier@{_HOST}/{CONCURRENCY_DB}"
CONC_APP_URL = (
    f"postgresql://carrier_pool_app:carrier_pool_app@{_HOST}/{CONCURRENCY_DB}"
)

CAR_A = (700001, "Alamo Freight", "111111", "2222222", "+18005550100")
HISTORY_FILES = 24


@pytest.fixture(autouse=True)
def clean_db() -> Iterator[None]:
    """Shadow the package's autouse truncate.

    Defined at module scope so pytest resolves it in preference to the one in
    ``conftest.py``. These tests deliberately provoke lock contention; sharing
    the package's session-scoped admin connection would let a deadlock here
    block another test's ``TRUNCATE`` teardown.
    """
    yield


@pytest.fixture(scope="module")
def conc_admin() -> Iterator[psycopg.Connection]:
    _ensure_database(CONCURRENCY_DB)
    conn = db.connect_admin(CONC_ADMIN_URL)
    bootstrap_serialised(conn)
    yield conn
    conn.close()
    _drop_database(CONCURRENCY_DB)


def _reset(conn: psycopg.Connection) -> None:
    conn.execute(f"TRUNCATE {', '.join(TENANT_TABLES)} RESTART IDENTITY CASCADE")


def _write_contended_corpus(root: Path) -> None:
    """Files that all land on one lane key, so every ingest fights for one row."""
    for hour in range(HISTORY_FILES):
        write_file(
            root,
            TMS_A_DIR,
            f"2026-07-06T{hour:02d}-00_sync.json",
            tms_a_envelope(
                "2026-07-06T00:00:00-05:00",
                [tms_a_load(700000 + hour, carrier=CAR_A)],
            ),
        )
    # One TMS B load too: its money is a running sum, so it is the sharpest
    # available check that nothing was applied twice.
    write_file(
        root,
        TMS_B_DIR,
        "2026-07-06T00-00_sync.json",
        tms_b_envelope(
            "2026-07-06 00:00:00",
            carriers=[tms_b_carrier(800001, name="HD", mc_no="9", dot_no="9")],
            loads=[tms_b_load("HD-1")],
            rates=[tms_b_rate(1, "HD-1", "pay", "LINEHAUL", 5000.0)],
        ),
    )


def _assert_consistent(conn: psycopg.Connection) -> None:
    """Whatever landed, it landed exactly once and the derived rows agree.

    True of every intermediate state, not just the finished one — a file that
    lost the race is absent entirely rather than half-applied.
    """
    with broker_session(conn, "broker_a") as cur:
        cur.execute("SELECT count(*) AS n FROM sync_files")
        files = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM loads")
        loads = cur.fetchone()["n"]
        cur.execute(
            "SELECT tier, equipment, load_count FROM lane_stats"
            " WHERE equipment IN ('DRY_VAN', 'ANY')"
        )
        lanes = cur.fetchall()
        cur.execute(
            "SELECT count(*) AS n FROM ("
            "  SELECT source_load_id FROM sync_events WHERE entity_type = 'LOAD'"
            "  GROUP BY source_load_id, sync_file_id HAVING count(*) > 1) d"
        )
        duplicated = cur.fetchone()["n"]

    assert files == loads, (
        f"{files} files ingested but {loads} loads exist: a file was applied "
        "partially"
    )
    assert duplicated == 0, "a load event was written twice for one file"
    for lane in lanes:
        assert lane["load_count"] == loads, (
            f"{lane['tier']}/{lane['equipment']} counts {lane['load_count']} of "
            f"{loads} loads: a rebuild was skipped or ran on stale rows"
        )

    money = BrokerRepository(conn, "broker_b").get_load("HD-1")
    if money is not None:
        assert money.carrier_rate == pytest.approx(5000.0), (
            f"TMS B carrier rate is {money.carrier_rate}, not 5000.00: a rate "
            "line was applied more than once"
        )


def test_concurrent_ingest_crashes_but_never_corrupts(conc_admin, tmp_path):
    """The timing-independent half of FINDING 6.

    Eight simultaneous full ingests over a corpus engineered so every file
    dirties the same lane key, six times over. Whether any of them raises
    depends on machine load and is deliberately **not** asserted — what is
    asserted is that the database is consistent after every round, and that any
    exception which does escape is one of the two known race outcomes rather
    than something unaccounted for.

    Observed error classes are reported through ``record_property`` so a run
    that does hit the race says so without failing.
    """
    root = tmp_path / "data"
    root.mkdir()
    _write_contended_corpus(root)

    seen: Counter[str] = Counter()
    for _ in range(6):
        _reset(conc_admin)
        errors: list[BaseException] = []

        def ingest() -> None:
            conn = db.connect(CONC_APP_URL)
            try:
                ingest_all(conn, root)
            except Exception as exc:  # noqa: BLE001 - classifying is the point
                errors.append(exc)
            finally:
                conn.close()

        threads = [threading.Thread(target=ingest) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        for error in errors:
            seen[type(error).__name__] += 1
            assert isinstance(
                error,
                (
                    psycopg.errors.UniqueViolation,
                    psycopg.errors.DeadlockDetected,
                    psycopg.errors.InFailedSqlTransaction,
                ),
            ), f"unaccounted-for failure under concurrency: {error!r}"

        # Consistent whether or not anybody lost the race.
        check = db.connect(CONC_APP_URL)
        try:
            _assert_consistent(check)
        finally:
            check.close()

    # A serial re-run must complete the corpus: every rolled-back file is
    # simply retried, which is why the crash is survivable.
    _reset(conc_admin)
    conn = db.connect(CONC_APP_URL)
    try:
        ingest_all(conn, root)
        _assert_consistent(conn)
        lane = BrokerRepository(conn, "broker_a").get_lane_stats(
            tier="ZIP3", origin_key="752", dest_key="770", equipment="DRY_VAN"
        )
        assert lane.load_count == HISTORY_FILES
    finally:
        conn.close()

    print(f"\nrace outcomes observed over 6 x 8-way ingest: {dict(seen) or 'none'}")


def test_concurrent_bootstrap_collides_on_the_shared_role_catalog(conc_admin):
    """FINDING 15 (crash at startup). ``bootstrap()`` is not concurrency-safe,
    and a separate database per instance does not help.

    D8 runs ``bootstrap()`` in the FastAPI lifespan on **every** start, and
    ``schema.sql`` ends with cluster-wide role DDL::

        ALTER ROLE carrier_pool_app WITH LOGIN NOSUPERUSER ... PASSWORD ...
        GRANT carrier_pool_app TO CURRENT_USER

    ``pg_authid`` and ``pg_auth_members`` are **shared catalogs**, not
    per-database ones. Two instances starting against the same Postgres cluster
    therefore update the same role tuple concurrently and one dies with
    ``InternalError: tuple concurrently updated``. Because the lifespan fails
    loudly by design, that container does not start.

    Reachable by ``docker compose up --scale api=2``, by a rolling restart, or
    by any two developers pointing at one cluster. I hit it running three copies
    of this suite at once, each on its own database — which is what proves the
    per-database isolation is not the mitigation.

    Driven deliberately here: N threads bootstrap at the same instant, without
    the advisory lock that :func:`bootstrap_serialised` normally holds.
    """
    barrier = threading.Barrier(4)
    failures: list[BaseException] = []

    def bootstrap_now() -> None:
        conn = db.connect_admin(CONC_ADMIN_URL)
        try:
            barrier.wait(timeout=30)
            db.bootstrap(conn)  # deliberately NOT bootstrap_serialised
        except Exception as exc:  # noqa: BLE001 - classifying is the point
            failures.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=bootstrap_now) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    # The collision is timing-dependent, so a clean round is not a failure —
    # what must never happen is a failure of some *other* kind, and the schema
    # must be intact either way.
    for failure in failures:
        assert isinstance(
            failure, (psycopg.errors.InternalError_, psycopg.errors.DeadlockDetected)
        ), f"unaccounted-for bootstrap failure: {failure!r}"

    bootstrap_serialised(conn := db.connect_admin(CONC_ADMIN_URL))
    try:
        intact = conn.execute(
            "SELECT count(*) FROM information_schema.tables"
            " WHERE table_schema = 'public' AND table_name = ANY(%s)",
            (list(TENANT_TABLES),),
        ).fetchone()[0]
    finally:
        conn.close()
    assert intact == len(TENANT_TABLES), "a failed concurrent bootstrap left a gap"
    print(
        f"\nconcurrent bootstrap failures this round: "
        f"{[type(f).__name__ for f in failures] or 'none'}"
    )


def test_the_delete_then_insert_rebuild_loses_a_deliberate_race(conc_admin):
    """The mechanism of FINDING 6, driven deterministically.

    No sleeps: the second transaction is confirmed to be *waiting on a lock* via
    ``pg_stat_activity`` before the first commits, so the interleaving that
    exposes the bug is forced rather than hoped for.
    """
    _reset(conc_admin)
    where = (
        "WHERE broker_id='broker_a' AND tier='ZIP3' AND origin_key='752'"
        " AND dest_key='770' AND equipment='DRY_VAN'"
    )
    insert = (
        "INSERT INTO lane_stats (broker_id, tier, origin_key, dest_key,"
        " equipment, load_count) VALUES ('broker_a','ZIP3','752','770',"
        "'DRY_VAN', %s)"
    )
    conc_admin.execute(insert, (1,))

    first = psycopg.connect(CONC_ADMIN_URL)
    second = psycopg.connect(CONC_ADMIN_URL)
    outcome: list[str] = []
    try:
        # Transaction one rebuilds the key and holds it uncommitted.
        first.execute(f"DELETE FROM lane_stats {where}")
        first.execute(insert, (2,))

        def rebuild_too() -> None:
            try:
                second.execute(f"DELETE FROM lane_stats {where}")
                second.execute(insert, (3,))
                second.commit()
                outcome.append("committed")
            except psycopg.errors.UniqueViolation:
                outcome.append("UniqueViolation")
                second.rollback()
            except psycopg.errors.DeadlockDetected:  # pragma: no cover
                outcome.append("DeadlockDetected")
                second.rollback()

        thread = threading.Thread(target=rebuild_too)
        thread.start()

        # Wait for transaction two to be genuinely blocked on transaction one's
        # row lock. This is the determinism: we do not commit until the race is
        # actually set up.
        backend = second.info.backend_pid
        blocked = False
        for _ in range(200):
            row = conc_admin.execute(
                "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s",
                (backend,),
            ).fetchone()
            if row is not None and row[0] == "Lock":
                blocked = True
                break
            threading.Event().wait(0.05)
        assert blocked, "transaction two never blocked; the race was not set up"

        first.commit()
        thread.join(timeout=30)
    finally:
        first.close()
        second.close()

    assert outcome == ["UniqueViolation"], (
        "delete-then-insert is expected to lose this race. If it now survives, "
        f"FINDING 6 is fixed and this test should be deleted. Got {outcome}"
    )
