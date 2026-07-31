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
import time
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


def _log_through_uvicorn() -> None:
    """Give the ``app`` logger uvicorn's handler, once, at startup.

    uvicorn configures its own loggers and leaves the root logger alone, so an
    ``app.*`` record has nowhere to go and the lifespan runs silent — on a clean
    volume that is minutes of nothing between "Waiting for application startup."
    and "Application startup complete.", which reads exactly like a hang.
    Borrowing the handler rather than calling ``basicConfig`` keeps these lines
    in uvicorn's format and on its stream, so they interleave with its own
    output in order. Under pytest there is no uvicorn and no handler to borrow,
    which is why this is a no-op there rather than noise on every test.
    """
    # ``uvicorn``, not ``uvicorn.error``: the handler lives on the parent and the
    # error logger reaches it by propagation, so borrowing from the child copies
    # an empty list and prints nothing.
    source = logging.getLogger("uvicorn")
    target = logging.getLogger(__package__)
    if source.handlers and not target.handlers:
        target.handlers = source.handlers
        target.setLevel(source.getEffectiveLevel())
        target.propagate = False


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    _log_through_uvicorn()
    started = time.monotonic()
    # The one place the admin credential is used: applying the schema. Every
    # request afterwards runs on DATABASE_URL, which cannot bypass RLS.
    log.info("startup: applying database schema")
    with connect_admin() as conn:
        bootstrap(conn)
    log.info("startup: schema ready in %.1fs", time.monotonic() - started)
    # Ingestion runs as the unprivileged app role like everything else, so a bug
    # here cannot cross a tenant boundary either. It reports its own progress:
    # it is the part of startup long enough to be mistaken for a stall.
    with connect() as conn:
        ingest_all(conn, DATA_DIR)
    log.info("startup complete in %.1fs", time.monotonic() - started)
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
