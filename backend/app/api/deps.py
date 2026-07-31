"""Request-scoped plumbing: a connection, a bound repository, and 404 policy.

**Every load-scoped route depends on :func:`broker_repo`.** There is no other
way for a route to reach the database, so a route cannot be written that reads
tenant data without naming a broker — the same property the repository enforces
one layer down, restated at the edge so the two agree.

**Fail closed, twice.**

* A ``broker_id`` that is not a tenant raises
  :class:`~app.repository.UnknownBroker` at construction, and that becomes a 404.
  Only that one exception is caught. A broad ``except`` here would swallow the
  ``unrecognized configuration parameter`` that the database raises when a query
  escapes its binding, converting the loudest tenant-isolation failure we have
  into a tidy 404 — so the narrow catch is load-bearing, not fastidiousness.
* A load id belonging to *another* broker reads as ``None`` from
  :meth:`~app.repository.BrokerRepository.get_load`, which :func:`require_load`
  turns into the same 404 as a load id that does not exist anywhere. The two
  cases are indistinguishable on purpose: whether an id exists is itself another
  tenant's data, and a 403 would leak it.

**Connections are per-request, not pooled.** ``connect()`` on each request,
closed when it ends. At this scale a local connect is a millisecond against a
few seconds of ingestion, and a pool is a stateful thing to get wrong for no
measurable gain (PRD section 3: no cache layer, no queue). The important
property is that each request runs on ``DATABASE_URL`` — the unprivileged role,
NOSUPERUSER and NOBYPASSRLS — never on ``ADMIN_DATABASE_URL``, which exists only
for the schema bootstrap in the lifespan. That split is the isolation barrier;
convenience is not a reason to collapse it.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

import psycopg
from fastapi import Depends, HTTPException, Query, status

from ..domain.model import Load
from ..repository import BrokerRepository, PoolRepository, UnknownBroker, connect

__all__ = [
    "BrokerRepo",
    "DbConnection",
    "PoolRepo",
    "broker_repo",
    "db_connection",
    "pool_repo",
    "require_load",
]


def db_connection() -> Iterator[psycopg.Connection]:
    """One unprivileged connection for the life of one request."""
    with connect() as conn:
        yield conn


DbConnection = Annotated[psycopg.Connection, Depends(db_connection)]


def broker_repo(
    conn: DbConnection,
    broker_id: Annotated[
        str,
        Query(
            description=(
                "The tenant. Required on every load route: source_load_id is "
                "unique only within a broker."
            )
        ),
    ],
) -> BrokerRepository:
    """A repository bound to ``broker_id``, or 404 if there is no such tenant.

    ``UnknownBroker`` is 404 rather than 400 because "this tenant does not
    exist" is a missing resource, and because the alternative — letting a
    typo'd broker id build a repository that reads empty — would render as
    "this broker has no loads", which is a wrong answer wearing a confident face.
    """
    try:
        return BrokerRepository(conn, broker_id)
    except UnknownBroker:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no such broker: {broker_id}",
        ) from None


BrokerRepo = Annotated[BrokerRepository, Depends(broker_repo)]


def pool_repo(
    conn: DbConnection,
    broker_id: Annotated[
        str,
        Query(
            description=(
                "The tenant asking. The pool is read on behalf of exactly one "
                "broker: it excludes that broker's own contribution and "
                "answers only if it has opted in."
            )
        ),
    ],
) -> PoolRepository:
    """A pool reader bound to ``broker_id``, or 404 if there is no such tenant.

    Deliberately a *separate* dependency from :func:`broker_repo` rather than a
    method reached through it. The pool is the one read path in the system that
    crosses a tenant boundary, and a route has to name it to get it — a route
    that asks for ``BrokerRepo`` cannot reach the pool by accident, and this
    one is greppable.
    """
    try:
        return PoolRepository(conn, broker_id)
    except UnknownBroker:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no such broker: {broker_id}",
        ) from None


PoolRepo = Annotated[PoolRepository, Depends(pool_repo)]


def require_load(repo: BrokerRepository, load_id: str) -> Load:
    """This broker's load, or 404 — including when it is someone else's.

    The message names the broker deliberately: it tells the caller *which*
    tenant was searched without disclosing whether the id exists elsewhere.
    """
    load = repo.get_load(load_id)
    if load is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no load {load_id} for broker {repo.broker_id}",
        )
    return load
