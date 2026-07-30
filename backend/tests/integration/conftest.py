"""Fixtures for the real-Postgres tenant-isolation suite (TASKS.md M4).

Every test starts from a known DB state: the tenant tables are truncated
before *and* after each test function, so a failing test's rows can never
leak into the next one. ``brokers`` itself is never truncated -- it holds the
fixed three-broker seed from ``schema.sql`` (DECISIONS.md D9) and carries no
RLS policy; it is the tenant directory, not tenant data.
"""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest

from app.repository import db

# Every RLS-governed table. Order doesn't matter for TRUNCATE ... CASCADE in
# one statement -- Postgres resolves FK dependencies (sync_events ->
# sync_files) itself.
_TENANT_TABLES = (
    "sync_events",
    "sync_files",
    "loads",
    "carriers",
    "customers",
    "lane_stats",
    "carrier_stats",
)


@pytest.fixture(scope="session")
def admin_conn() -> Iterator[psycopg.Connection]:
    """Schema-owner connection: bootstraps once, and truncates between tests.

    Session-scoped deliberately -- bootstrap is idempotent (D8) but there is
    no reason to re-apply schema.sql per test.
    """
    conn = db.connect_admin()
    db.bootstrap(conn)
    yield conn
    conn.close()


def _truncate(conn: psycopg.Connection) -> None:
    tables = ", ".join(_TENANT_TABLES)
    conn.execute(f"TRUNCATE {tables} RESTART IDENTITY CASCADE")


@pytest.fixture(autouse=True)
def clean_db(admin_conn: psycopg.Connection) -> Iterator[None]:
    """Guarantee an empty tenant dataset at the start and end of every test."""
    _truncate(admin_conn)
    yield
    _truncate(admin_conn)


@pytest.fixture
def app_conn() -> Iterator[psycopg.Connection]:
    """A fresh runtime connection per test -- exactly what the app gets from
    :func:`app.repository.db.connect`: session-switched to
    ``carrier_pool_app`` before any broker is bound."""
    conn = db.connect()
    yield conn
    conn.close()
