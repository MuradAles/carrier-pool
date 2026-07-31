"""The route surface of PRD section 10. Validate, delegate, serialize.

Nothing here decides anything. The tier walk, the score, the confidence label,
every reason and every provenance sentence are produced by
:mod:`app.domain.pricing` and :mod:`app.domain.scoring` and are copied onto the
wire by :mod:`app.api.schemas`. No route contains an arithmetic operator, and
none contains SQL — the two rules that keep this layer thin enough to be
obviously correct.

**Ranking and pricing answer from the same walk.** Both endpoints call
:func:`~app.domain.pricing.walk_tiers` on the same load through the same
repository lookup, so an estimate built on ``METRO`` cannot sit next to a
ranking built on ``ZIP3``. They are separate requests, so each does its own
walk; the walk is a pure function of the load and the stored lane rows, so two
calls a millisecond apart give the same rung.
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel

from ..domain.model import LoadStatus
from ..domain.pool import build_pool_section, pool_eligible
from ..domain.pricing import estimate_price, walk_tiers
from ..domain.scoring import rank_carriers
from ..ingestion import ingest_all
from ..repository import list_brokers as db_list_brokers
from .deps import BrokerRepo, DbConnection, PoolRepo, require_load
from .schemas import (
    BrokerOut,
    IngestReportOut,
    LoadDetailOut,
    LoadOut,
    PoolOptInOut,
    PoolSectionOut,
    PriceEstimateOut,
    RecommendationsOut,
)

__all__ = ["router"]

router = APIRouter(prefix="/api", tags=["carrier-pool"])


@router.get("/brokers")
def list_brokers(conn: DbConnection) -> list[BrokerOut]:
    """The tenant directory — the one route with no broker binding.

    ``brokers`` holds no load, carrier, customer or money data, only the names
    the UI's dropdown is built from, which is why it carries no RLS policy.
    """
    return [BrokerOut.of(broker) for broker in db_list_brokers(conn)]


@router.get("/loads")
def list_loads(
    repo: BrokerRepo,
    status: LoadStatus | None = Query(
        default=None, description="Canonical status filter; omit for all."
    ),
) -> list[LoadOut]:
    """This broker's loads, optionally filtered by canonical status.

    ``status`` is typed as the enum, so an unknown value is a 422 from FastAPI
    rather than a stray string reaching the repository.
    """
    return [LoadOut.of(load) for load in repo.list_loads(status=status)]


@router.get("/loads/{load_id}")
def get_load(repo: BrokerRepo, load_id: str) -> LoadDetailOut:
    """One load, its counterparties, and its full sync history (P5).

    The history is every event that ever mentioned this load, in arrival order
    — including ``RATE_LINE`` events from a TMS B sync whose ``loads`` array
    never named it. That is what makes a correction visible as a correction.
    """
    load = require_load(repo, load_id)
    carrier = (
        None
        if load.source_carrier_id is None
        else repo.get_carrier(load.source_carrier_id)
    )
    customer = (
        None
        if load.source_customer_id is None
        else repo.get_customer(load.source_customer_id)
    )
    return LoadDetailOut.build(
        load,
        carrier=carrier,
        customer=customer,
        history=repo.events_for_load(load_id),
    )


@router.get("/loads/{load_id}/recommendations")
def get_recommendations(repo: BrokerRepo, load_id: str) -> RecommendationsOut:
    """Every carrier this broker has used, ranked for this load, with reasons.

    The accepted rung of the walk chooses the lane experience is measured on;
    ``ranking_inputs(None)`` is the honest input when nothing cleared the
    minimum, rather than a lane picked to have something to say.
    """
    load = require_load(repo, load_id)
    walk = walk_tiers(load, repo.lane_stats_for)
    key = None if walk.accepted is None else walk.accepted.key
    ranking = rank_carriers(load, walk, repo.ranking_inputs(key))
    return RecommendationsOut.of(load_id, ranking)


@router.get("/loads/{load_id}/price-estimate")
def get_price_estimate(repo: BrokerRepo, load_id: str) -> PriceEstimateOut:
    """What this load should cost to cover, with the whole walk behind it."""
    load = require_load(repo, load_id)
    estimate = estimate_price(
        load,
        lane_stats=repo.lane_stats_for,
        equipment_mix=repo.equipment_mix_for,
    )
    return PriceEstimateOut.of(load_id, estimate)


class PoolOptInIn(BaseModel):
    """The one thing a broker can say about the pool: in, or out."""

    opted_in: bool


@router.get("/pool/opt-in")
def get_pool_opt_in(pool: PoolRepo) -> PoolOptInOut:
    """Is this broker in the shared carrier pool? Off by default (S1).

    A broker can only ask about itself: the read is RLS-confined to its own
    row, so the roster of who else has joined is not available here. What the
    pool discloses about other brokers is exactly what a pool answer contains.
    """
    return PoolOptInOut(broker_id=pool.broker_id, opted_in=pool.is_opted_in())


@router.put("/pool/opt-in")
def set_pool_opt_in(pool: PoolRepo, body: PoolOptInIn) -> PoolOptInOut:
    """Join or leave the pool. Idempotent in both directions.

    Leaving takes effect on the next read for everybody, because there is no
    copy of this broker's rows anywhere — the projection reads ``carrier_stats``
    live and joins ``pool_opt_in`` (DECISIONS.md D17 rejects a physical pool
    table for exactly this reason).
    """
    return PoolOptInOut(
        broker_id=pool.broker_id, opted_in=pool.set_opted_in(body.opted_in)
    )


@router.get("/loads/{load_id}/pool-carriers")
def get_pool_carriers(
    repo: BrokerRepo, pool: PoolRepo, load_id: str
) -> PoolSectionOut:
    """The labeled second section: carriers this broker has never used (S4).

    A **separate route** from ``/recommendations``, not a field on it. The
    ranking response has no field a pool carrier could be assigned to, so with
    every broker opted out this endpoint is the only thing that changes and the
    ranking is byte-identical to what it was before Phase 11 existed.

    ``require_load`` first, so the load is one of *this* broker's and anything
    else is the usual 404 — the pool never answers for a load id the requester
    does not own, which is what bounds the query space to its own freight. The
    audit row is written before the read and only when the read is going to
    happen, so what is recorded is what was answered.
    """
    load = require_load(repo, load_id)
    opted_in = pool.is_opted_in()
    if pool_eligible(load, opted_in=opted_in):
        pool.record_pool_read(load_id)
    return PoolSectionOut.of(
        build_pool_section(
            load,
            as_of=repo.as_of_date(),
            opted_in=opted_in,
            carriers_for=pool.pool_carriers,
        )
    )


@router.post("/admin/ingest")
def ingest(conn: DbConnection) -> IngestReportOut:
    """Replay every sync file, chronologically, one at a time (P4).

    The same call the lifespan makes (D8), exposed as the manual path. It is
    idempotent on ``broker_id + sync_file``, so on a warm database this reports
    every file skipped and changes no row. Synchronous: an ingest that answers
    before it has finished would be reporting on work that might still fail.

    Runs on the unprivileged connection like every other route, so a bug here
    cannot cross a tenant boundary either.
    """
    return IngestReportOut.of(ingest_all(conn))
