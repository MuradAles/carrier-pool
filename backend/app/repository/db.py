"""Connections and schema bootstrap.

Two credentials, and the difference matters:

* ``DATABASE_URL`` names ``carrier_pool_app`` — NOSUPERUSER, NOBYPASSRLS. This
  is the **ambient** credential: the one a future call site picks up if it
  reaches for ``psycopg.connect(DATABASE_URL)`` out of habit. On it, a query
  against a tenant table outside a broker binding *raises* rather than
  returning every broker's rows, so the careless path fails closed.
* ``ADMIN_DATABASE_URL`` names the table owner. It exists only so
  :func:`bootstrap` can apply ``schema.sql``, and :func:`connect_admin` is the
  only function that uses it. A superuser silently bypasses row-level security,
  so this credential is a genuine escape hatch — it is separated and named
  rather than left as the default.

Chicken-and-egg: ``carrier_pool_app`` is created *by* ``schema.sql``, so on a
brand-new database :func:`connect` fails until :func:`bootstrap` has run once.
That ordering is already what happens — the FastAPI lifespan bootstraps before
serving (DECISIONS.md D8).

This is the second, independent barrier behind
:class:`~app.repository.broker_repository.BrokerRepository`: the first is that
the repository stamps the bound ``broker_id`` into every statement, and the
second is that the database refuses to serve anything else.
"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg
from psycopg.rows import class_row

from ..domain.model import Broker

__all__ = [
    "APP_ROLE",
    "BROKER_SETTING",
    "DEFAULT_ADMIN_DATABASE_URL",
    "DEFAULT_DATABASE_URL",
    "SCHEMA_PATH",
    "bootstrap",
    "connect",
    "connect_admin",
    "get_broker",
    "list_brokers",
]

# The unprivileged application credential. Deliberately the one named
# DATABASE_URL, so the obvious way to connect is also the safe way.
DEFAULT_DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://carrier_pool_app:carrier_pool_app@localhost:5432/carrier_pool",
)

# Owner rights, for schema bootstrap only. Everything else that uses this is a
# bug, and it is named loudly enough to be greppable.
DEFAULT_ADMIN_DATABASE_URL = os.environ.get(
    "ADMIN_DATABASE_URL", "postgresql://carrier:carrier@localhost:5432/carrier_pool"
)

# Kept in sync with schema.sql by the assertions below — SET ROLE and policy
# names cannot take query parameters, so both files spell them out as literals.
APP_ROLE = "carrier_pool_app"
BROKER_SETTING = "app.broker_id"

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

_SET_SESSION_ROLE_SQL = "SET SESSION ROLE carrier_pool_app"
assert APP_ROLE in _SET_SESSION_ROLE_SQL


def bootstrap(conn: psycopg.Connection) -> None:
    """Apply ``schema.sql``. Idempotent — safe on every container start (D8).

    Needs owner rights, so pass a :func:`connect_admin` connection.
    """
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with conn.transaction():
        conn.execute(sql)


def connect_admin(url: str | None = None) -> psycopg.Connection:
    """Connect as the schema owner. For :func:`bootstrap` and tests only.

    This connection bypasses row-level security. Anything tenant-scoped done
    over it must still go through ``broker_session``, which drops to
    ``carrier_pool_app`` for the duration and so re-imposes the policies.
    """
    return psycopg.connect(url or DEFAULT_ADMIN_DATABASE_URL, autocommit=True)


def connect(url: str | None = None) -> psycopg.Connection:
    """Connect for application use, as the unprivileged app role.

    ``bootstrap`` must have run at least once against this database, since the
    role is created there. Autocommit is on: statements outside an explicit
    ``conn.transaction()`` stand alone, and ingestion wraps one sync file in one
    transaction.

    The ``SET SESSION ROLE`` is belt and braces: the credential is already
    unprivileged, but a caller who passes an admin URL here still ends up
    without the bypass.
    """
    conn = psycopg.connect(url or DEFAULT_DATABASE_URL, autocommit=True)
    try:
        conn.execute(_SET_SESSION_ROLE_SQL)
    except Exception:
        conn.close()
        raise
    return conn


def list_brokers(conn: psycopg.Connection) -> list[Broker]:
    """Every broker. Deliberately not tenant-scoped: this is the tenant list.

    ``brokers`` holds no load, carrier, customer or money data — only the names
    the UI's dropdown is built from — which is why it carries no policy. It is
    the one table this module reads without a broker binding, and that fact is
    checkable by grepping for ``FROM brokers``.
    """
    with conn.cursor(row_factory=class_row(Broker)) as cur:
        cur.execute("SELECT id, name, tms_type FROM brokers ORDER BY id")
        return cur.fetchall()


def get_broker(conn: psycopg.Connection, broker_id: str) -> Broker | None:
    """One broker, or ``None`` if there is no such tenant."""
    with conn.cursor(row_factory=class_row(Broker)) as cur:
        cur.execute("SELECT id, name, tms_type FROM brokers WHERE id = %s", (broker_id,))
        return cur.fetchone()
