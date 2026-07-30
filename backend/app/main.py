"""FastAPI entrypoint: startup, health, and the mounted route surface.

The schema is applied on startup, in the lifespan, before anything is served
(DECISIONS.md D8), and the full chronological ingest runs immediately after it,
synchronously. Both are idempotent, so a container restart re-applies the schema
and re-runs the ingest for free — the already-ingested files are skipped at the
database level. Both fail loudly: serving requests against a database whose shape
or contents are unknown produces wrong answers rather than slow ones, and D8
rejects background ingestion for exactly that reason.

Everything else lives in :mod:`app.api`. ``/api/health`` stays here because it is
the one route that must answer when the database does not — it reports the
failure instead of depending on it.
"""

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import psycopg
from fastapi import FastAPI

from .api import router
from .api.schemas import HealthOut
from .ingestion import ingest_all
from .repository import DEFAULT_DATABASE_URL, bootstrap, connect, connect_admin

DATA_DIR = Path(os.environ.get("DATA_DIR", "../data"))

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # The one place the admin credential is used: applying the schema. Every
    # request afterwards runs on DATABASE_URL, which cannot bypass RLS.
    with connect_admin() as conn:
        bootstrap(conn)
    # Ingestion runs as the unprivileged app role like everything else, so a bug
    # here cannot cross a tenant boundary either.
    with connect() as conn:
        report = ingest_all(conn, DATA_DIR)
    log.info("startup ingest: %s", report.summary())
    yield


app = FastAPI(title="Carrier Pool", version="0.1.0", lifespan=lifespan)


@app.get("/api/health", tags=["carrier-pool"])
def health() -> HealthOut:
    """Liveness plus the two things that make answers possible.

    Deliberately does not use the request-scoped connection dependency: a health
    check that 500s when the database is down has reported nothing a caller
    could act on. It catches broadly *because* its whole job is to describe the
    failure — the one place in this codebase where that is the right shape.
    """
    try:
        with psycopg.connect(DEFAULT_DATABASE_URL, connect_timeout=3) as conn:
            conn.execute("SELECT 1")
        database = "ok"
    except Exception as exc:  # surface the reason instead of a bare 500
        database = f"unavailable: {type(exc).__name__}"

    sync_files = sorted(DATA_DIR.glob("*/*_sync.json")) if DATA_DIR.is_dir() else []
    return HealthOut(
        status="ok",
        database=database,
        data_dir=f"{DATA_DIR} ({len(sync_files)} sync files)",
    )


app.include_router(router)
