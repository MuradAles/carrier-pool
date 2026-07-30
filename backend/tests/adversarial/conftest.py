"""Adversarial suite fixtures — pinned to a private database.

This suite writes deliberately hostile data. It must never touch
``carrier_pool_phase5`` (the shared full-ingest database) or ``carrier_pool``,
so both URLs are **explicit constants here** and are passed to every
``connect``/``connect_admin`` call.

**This module must not change any other suite's behaviour by being imported.**
Pytest imports every ``conftest.py`` under a collected path during collection,
even for a directory whose tests are all deselected, so rebinding
``db.DEFAULT_DATABASE_URL`` here would silently redirect the integration
suite — including its truncating fixture — at this database, for the rest of
the process. That is order- and data-dependent, which is worse than a clean
failure: it passes locally and bites once, in front of someone.

An earlier revision of this file did exactly that. It is now enforced rather
than merely avoided: :func:`_DEFAULTS_AT_IMPORT` snapshots the two module
globals before anything else runs, and
``test_no_conftest_rebinds_the_connection_defaults`` fails if they ever move.

The database is created on demand (:func:`_ensure_database`) so that a clean
checkout running a bare ``pytest tests`` gets a real run rather than 80 silent
skips — Phase 10's X4 rehearsal is exactly that command on a fresh machine.

**The database name carries the process id, and that is load-bearing.** An
earlier revision used one fixed name. Because :func:`clean_db` truncates before
and after every test, two pytest processes running this suite at the same time
destroyed each other's rows: the loser reported dozens of *setup* errors and a
handful of assertion failures whose identity moved between runs. Reproduced
deterministically — two concurrent ``pytest tests/adversarial`` gave
``68 passed`` and ``2 passed, 66 errors``.

That is a harness defect of exactly the kind this suite exists to find, and the
"known issue, not a finding" note about concurrent runs on a shared database is
not a licence to keep it: a teammate running the suite while someone else does
must get a correct answer, not a corrupted one. So each session gets its own
database, dropped at the end, plus a sweep of any database abandoned by a
process that no longer exists. Set ``BREAKER_DB`` to pin a fixed name; a
caller-supplied name is never created-and-dropped, because it is not ours.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from app.repository import db

#: The values ``app.repository.db`` computed from the environment at import
#: time. Captured before this module defines anything else, so the guard test
#: compares against what the *environment* asked for, not against a constant
#: that a rebind could also be edited to match.
_DEFAULTS_AT_IMPORT = (db.DEFAULT_DATABASE_URL, db.DEFAULT_ADMIN_DATABASE_URL)

#: Databases this suite creates are named ``carrier_pool_breaker_p<pid>``. The
#: prefix is what the sweep matches on; the pid is what makes it safe to drop.
DB_PREFIX = "carrier_pool_breaker_p"

_PINNED_DB = os.environ.get("BREAKER_DB")
#: True when we created the database and may therefore drop it.
OWNS_DATABASE = _PINNED_DB is None
BREAKER_DB = _PINNED_DB or f"{DB_PREFIX}{os.getpid()}"

_HOST = "localhost:5432"
ADMIN_URL = f"postgresql://carrier:carrier@{_HOST}/{BREAKER_DB}"
APP_URL = f"postgresql://carrier_pool_app:carrier_pool_app@{_HOST}/{BREAKER_DB}"
_MAINTENANCE_URL = f"postgresql://carrier:carrier@{_HOST}/postgres"

TENANT_TABLES = (
    "sync_events",
    "sync_files",
    "loads",
    "carriers",
    "customers",
    "lane_stats",
    "carrier_stats",
)


def _maintenance() -> psycopg.Connection:
    """Autocommit connection to ``postgres``; ``CREATE/DROP DATABASE`` need one."""
    try:
        return psycopg.connect(_MAINTENANCE_URL, autocommit=True)
    except psycopg.OperationalError as exc:  # pragma: no cover - no server
        pytest.skip(f"no Postgres at {_HOST}: {exc}")


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - exists, owned by someone else
        return True
    return True


def _sweep_abandoned(maintenance: psycopg.Connection) -> None:
    """Drop breaker databases left behind by processes that are gone.

    Only touches names this suite minted, and only when the encoded pid is not a
    live process — so a concurrent run's database is never dropped out from
    under it. A hard-killed session is the only way one is left, and this is
    what stops them accumulating.
    """
    rows = maintenance.execute(
        "SELECT datname FROM pg_database WHERE datname LIKE %s", (f"{DB_PREFIX}%",)
    ).fetchall()
    for (name,) in rows:
        suffix = name[len(DB_PREFIX) :].split("_", 1)[0]
        if not suffix.isdigit() or _process_alive(int(suffix)):
            continue
        try:
            maintenance.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        except psycopg.Error:  # pragma: no cover - raced, or in use after all
            pass


def _ensure_database(name: str = BREAKER_DB) -> None:
    """Create ``name`` if it does not exist, sweeping abandoned ones first."""
    maintenance = _maintenance()
    try:
        _sweep_abandoned(maintenance)
        exists = maintenance.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (name,)
        ).fetchone()
        if exists is None:
            try:
                maintenance.execute(f'CREATE DATABASE "{name}"')
            except psycopg.errors.DuplicateDatabase:  # pragma: no cover - race
                pass
    finally:
        maintenance.close()


#: Arbitrary but fixed key for the advisory lock that serialises bootstrap.
_BOOTSTRAP_LOCK = 0x0CA881E0


def bootstrap_serialised(conn: psycopg.Connection) -> None:
    """Apply ``schema.sql`` under a cluster-wide advisory lock.

    ``schema.sql`` is not safe to run concurrently and a private database does
    not help, because the parts that clash are **cluster-wide**::

        ALTER ROLE carrier_pool_app WITH LOGIN NOSUPERUSER ... PASSWORD ...
        GRANT carrier_pool_app TO CURRENT_USER

    ``pg_authid`` and ``pg_auth_members`` are shared catalogs, so two sessions
    bootstrapping at the same moment both update the same role tuple and one
    dies with ``InternalError: tuple concurrently updated``. That is what broke
    three concurrent runs of this suite even after each got its own database.

    It is also a real property of the product — see FINDING 15 and
    ``test_concurrent_bootstrap_collides_on_the_shared_role_catalog``. Here we
    simply take the lock; the lock lives in the ``postgres`` database so every
    session, whatever its own database, contends on the same one.
    """
    guard = _maintenance()
    try:
        guard.execute("SELECT pg_advisory_lock(%s)", (_BOOTSTRAP_LOCK,))
        db.bootstrap(conn)
    finally:
        guard.close()  # closing the session releases the advisory lock


def _drop_database(name: str) -> None:
    """Drop a database this session created. Never called for a pinned name."""
    if not OWNS_DATABASE:
        return
    maintenance = _maintenance()
    try:
        maintenance.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    except psycopg.Error:  # pragma: no cover - best effort; the sweep catches it
        pass
    finally:
        maintenance.close()


@pytest.fixture(scope="session")
def admin_conn() -> Iterator[psycopg.Connection]:
    """Owner connection on this session's own breaker database.

    Created here and dropped on the way out, so two concurrent runs of this
    suite cannot see — let alone truncate — each other's rows.
    """
    _ensure_database()
    conn = db.connect_admin(ADMIN_URL)
    bootstrap_serialised(conn)
    yield conn
    conn.close()
    _drop_database(BREAKER_DB)


def truncate(conn: psycopg.Connection) -> None:
    conn.execute(f"TRUNCATE {', '.join(TENANT_TABLES)} RESTART IDENTITY CASCADE")


@pytest.fixture(autouse=True)
def clean_db(admin_conn: psycopg.Connection) -> Iterator[None]:
    truncate(admin_conn)
    yield
    truncate(admin_conn)


@pytest.fixture
def app_conn() -> Iterator[psycopg.Connection]:
    """An unprivileged connection, explicitly on the breaker database."""
    conn = db.connect(APP_URL)
    yield conn
    conn.close()


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    """An empty per-test directory to write hostile sync files into."""
    root = tmp_path / "data"
    root.mkdir()
    return root
