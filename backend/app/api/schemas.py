"""The wire shapes, and the one-way mapping from domain objects into them.

Every model here is a **projection** of something the domain or the repository
already built. The constructors below copy fields and nothing else: no rounding,
no ratios, no string building, no re-derivation of a number that already exists
on the object being copied. That is the API's half of invariant 2 — the reasons,
the provenance line, the ``basis`` sentence and the published score all arrive
finished from :mod:`app.domain.scoring` and :mod:`app.domain.pricing`, and this
layer's only job is to serialize them without introducing a second source of
truth. If a field here needed a formula, the formula would belong upstream.

Three consequences worth stating, because they look like omissions otherwise:

* **``score`` is copied, not computed.** It is
  :attr:`~app.domain.scoring.CarrierScore.score` — the weighted sum of the
  signals, rounded half-up once (DECISIONS.md D19). ``score_exact`` rides along
  because it is what the ordering used and two adjacent carriers can round to
  the same digit; it is diagnostic, and the number to *display* is ``score``.
* **The whole tier walk is returned, not just the accepted rung.** Invariant 6
  says every answer reports which tier it used and how many loads backed it;
  :class:`TierWalkOut` reports every rung that was tried, including the ones
  that fell short and the ones a geo-null load could not form at all.
* **Nullable stays nullable.** An ``ACTIVE`` load has no carrier rate and a
  broker with no history has no estimate. Both serialize as ``null``. Rendering
  either as ``0`` would be the conflation the canonical model went out of its
  way to prevent (``app.domain.model``).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel

from ..domain.geo import Place
from ..domain.model import (
    Broker,
    CargoItem,
    Carrier,
    Customer,
    LastDelivery,
    Load,
    Stop,
    StopLocation,
    SyncEvent,
)
from ..domain.pricing import MIN_SAMPLE, PriceEstimate, TierAttempt, TierWalk
from ..domain.scoring import CarrierRanking, CarrierScore, Signal
from ..ingestion import IngestionReport

__all__ = [
    "BrokerOut",
    "CargoItemOut",
    "CarrierOut",
    "CarrierScoreOut",
    "CustomerOut",
    "EquipmentCountOut",
    "HealthOut",
    "IngestReportOut",
    "LastDeliveryOut",
    "LoadDetailOut",
    "LoadOut",
    "PlaceOut",
    "PriceEstimateOut",
    "RecommendationsOut",
    "SignalOut",
    "StopLocationOut",
    "StopOut",
    "SyncEventOut",
    "TierAttemptOut",
    "TierWalkOut",
]


# ---------------------------------------------------------------------------
# Health and tenants
# ---------------------------------------------------------------------------


class HealthOut(BaseModel):
    """``/api/health``. Three lines of free text meant for a human."""

    status: str
    database: str
    data_dir: str


class BrokerOut(BaseModel):
    """A tenant. ``/api/brokers`` is the one route with no broker binding."""

    id: str
    name: str
    tms_type: str

    @classmethod
    def of(cls, broker: Broker) -> BrokerOut:
        return cls(id=broker.id, name=broker.name, tms_type=broker.tms_type)


# ---------------------------------------------------------------------------
# Loads
# ---------------------------------------------------------------------------


class PlaceOut(BaseModel):
    """A resolved row of the offline geo table. Absent from a geo-null stop."""

    city: str
    state: str
    zip: str
    lat: float
    lon: float
    metro: str

    @classmethod
    def of(cls, place: Place | None) -> PlaceOut | None:
        if place is None:
            return None
        return cls(
            city=place.city,
            state=place.state,
            zip=place.zip,
            lat=place.lat,
            lon=place.lon,
            metro=place.metro,
        )


class StopLocationOut(BaseModel):
    """Where a stop is, as the TMS said it, plus what geo made of it.

    ``place is None`` is **geo-null**: the city/state/zip did not resolve. That
    excludes the load from lane statistics; it does not hide the stop, so the
    raw fields are always sent (PRD section 5, Location row).
    """

    city: str | None
    state: str | None
    zip: str | None
    name: str | None
    place: PlaceOut | None

    @classmethod
    def of(cls, location: StopLocation) -> StopLocationOut:
        return cls(
            city=location.city,
            state=location.state,
            zip=location.zip,
            name=location.name,
            place=PlaceOut.of(location.place),
        )


class StopOut(BaseModel):
    """One stop, in the order the TMS gave it. ``sequence`` is 1-based.

    ``scheduled_date`` is a local (US Central) calendar date; the ``window_*``
    and ``actual_*`` fields are UTC instants (DECISIONS.md D7/D16).
    """

    sequence: int
    is_pickup: bool
    is_drop: bool
    location: StopLocationOut
    scheduled_date: date | None
    window_start: datetime | None
    window_end: datetime | None
    actual_arrival: datetime | None
    actual_departure: datetime | None

    @classmethod
    def of(cls, stop: Stop) -> StopOut:
        return cls(
            sequence=stop.sequence,
            is_pickup=stop.is_pickup,
            is_drop=stop.is_drop,
            location=StopLocationOut.of(stop.location),
            scheduled_date=stop.scheduled_date,
            window_start=stop.window_start,
            window_end=stop.window_end,
            actual_arrival=stop.actual_arrival,
            actual_departure=stop.actual_departure,
        )


class CargoItemOut(BaseModel):
    """Only TMS C splits cargo out. ``weight_lbs`` is already normalized."""

    commodity: str | None
    weight_lbs: float | None
    pallet_count: float | None

    @classmethod
    def of(cls, item: CargoItem) -> CargoItemOut:
        return cls(
            commodity=item.commodity,
            weight_lbs=item.weight_lbs,
            pallet_count=item.pallet_count,
        )


class LoadOut(BaseModel):
    """A load in canonical form — the list row and the base of the detail.

    ``source_load_id`` is what every load route addresses: it is the natural key
    *inside* a broker (``UNIQUE (broker_id, source_load_id)``), which is why
    those routes all carry a ``broker_id``.
    """

    source_load_id: str
    load_number: str | None
    status: str
    equipment: str
    stops: list[StopOut]
    cargo: list[CargoItemOut]
    weight_lbs: float | None
    distance_miles: float | None
    customer_rate: float | None
    carrier_rate: float | None
    source_carrier_id: str | None
    source_customer_id: str | None
    created_at: datetime | None
    last_modified_at: datetime | None

    @classmethod
    def of(cls, load: Load) -> LoadOut:
        return cls(**_load_fields(load))


def _load_fields(load: Load) -> dict[str, Any]:
    """The common projection, so the list row and the detail cannot drift."""
    return {
        "source_load_id": load.source_load_id,
        "load_number": load.load_number,
        "status": str(load.status),
        "equipment": str(load.equipment),
        "stops": [StopOut.of(stop) for stop in load.stops],
        "cargo": [CargoItemOut.of(item) for item in load.cargo],
        "weight_lbs": load.weight_lbs,
        "distance_miles": load.distance_miles,
        "customer_rate": load.customer_rate,
        "carrier_rate": load.carrier_rate,
        "source_carrier_id": load.source_carrier_id,
        "source_customer_id": load.source_customer_id,
        "created_at": load.created_at,
        "last_modified_at": load.last_modified_at,
    }


class CarrierOut(BaseModel):
    """A carrier as one broker's TMS knows it, plus its last known position.

    MC/DOT are a cross-*system* identity, never a cross-*broker* one
    (DECISIONS.md D2): two brokers' rows for the same real company are two
    carriers here and stay that way.
    """

    source_carrier_id: str
    name: str | None
    mc_number: str | None
    dot_number: str | None
    phone: str | None
    home_city: str | None
    home_state: str | None
    last_delivery_lat: float | None
    last_delivery_lon: float | None
    last_delivery_at: datetime | None

    @classmethod
    def of(cls, carrier: Carrier | None) -> CarrierOut | None:
        if carrier is None:
            return None
        return cls(
            source_carrier_id=carrier.source_carrier_id,
            name=carrier.name,
            mc_number=carrier.mc_number,
            dot_number=carrier.dot_number,
            phone=carrier.phone,
            home_city=carrier.home_city,
            home_state=carrier.home_state,
            last_delivery_lat=carrier.last_delivery_lat,
            last_delivery_lon=carrier.last_delivery_lon,
            last_delivery_at=carrier.last_delivery_at,
        )


class CustomerOut(BaseModel):
    source_customer_id: str
    name: str | None

    @classmethod
    def of(cls, customer: Customer | None) -> CustomerOut | None:
        if customer is None:
            return None
        return cls(
            source_customer_id=customer.source_customer_id, name=customer.name
        )


class SyncEventOut(BaseModel):
    """One row of the append-only log, as it arrived (P5, invariant 3).

    ``raw_json`` is the entity exactly as its TMS stated it in that file — not a
    canonical projection — because the point of the history panel is to make a
    correction *visible as a correction*: two versions of the same load, in the
    source's own vocabulary, with the file that carried each. A normalized
    rendering would hide the very difference the panel exists to show.

    ``entity_type`` includes ``RATE_LINE``, which is how a TMS B sync that
    appends money to a load its ``loads`` array never mentions still shows up in
    that load's history (CLAUDE.md, Known traps).
    """

    id: int
    sync_file: str
    synced_at: datetime
    entity_type: str
    source_entity_id: str
    source_load_id: str | None
    event_seq: int
    raw_json: dict[str, Any]

    @classmethod
    def of(cls, event: SyncEvent) -> SyncEventOut:
        return cls(
            id=event.id,
            sync_file=event.sync_file,
            synced_at=event.synced_at,
            entity_type=str(event.entity_type),
            source_entity_id=event.source_entity_id,
            source_load_id=event.source_load_id,
            event_seq=event.event_seq,
            raw_json=event.raw_json,
        )


class LoadDetailOut(LoadOut):
    """One load, its counterparties, and every version of it that ever arrived.

    ``sync_history`` is P5 and is a required feature, not a debugging aid: a
    rate that changed after we recorded it has to be visibly a *change*, in
    arrival order, or the correction machinery is invisible to the person it is
    meant to reassure.
    """

    carrier: CarrierOut | None
    customer: CustomerOut | None
    sync_history: list[SyncEventOut]

    @classmethod
    def build(
        cls,
        load: Load,
        *,
        carrier: Carrier | None,
        customer: Customer | None,
        history: list[SyncEvent],
    ) -> LoadDetailOut:
        return cls(
            **_load_fields(load),
            carrier=CarrierOut.of(carrier),
            customer=CustomerOut.of(customer),
            sync_history=[SyncEventOut.of(event) for event in history],
        )


# ---------------------------------------------------------------------------
# The tier walk — shared by both answers (invariant 6)
# ---------------------------------------------------------------------------


class TierAttemptOut(BaseModel):
    """One rung: what was asked, what was found, what was decided.

    ``skipped`` distinguishes a rung a geo-null load could not *form* from one
    that was asked and came back thin. Both are reported; flattening them into
    "0 loads" would claim we looked when we could not.
    """

    tier: str
    origin_key: str | None
    dest_key: str | None
    lane_key: str | None
    equipment: str | None
    load_count: int
    accepted: bool
    skipped: bool
    verdict: str

    @classmethod
    def of(cls, attempt: TierAttempt) -> TierAttemptOut:
        key = attempt.key
        return cls(
            tier=attempt.tier,
            origin_key=None if key is None else key.origin_key,
            dest_key=None if key is None else key.dest_key,
            lane_key=None if key is None else key.lane_key,
            equipment=None if key is None else key.equipment,
            load_count=attempt.load_count,
            accepted=attempt.accepted,
            skipped=attempt.skipped,
            verdict=attempt.verdict,
        )


class TierWalkOut(BaseModel):
    """Every rung tried, narrow to wide, and the one that won.

    ``tier is None`` means nothing cleared the five-load minimum. The rungs are
    still listed: "we have no evidence" is an answer, and an empty response is
    not (PRD section 7).
    """

    tier: str | None
    load_count: int
    equipment: str
    equipment_filter: str
    equipment_filtered: bool
    min_sample: int
    rungs: list[TierAttemptOut]

    @classmethod
    def of(cls, walk: TierWalk) -> TierWalkOut:
        return cls(
            tier=walk.tier,
            load_count=walk.load_count,
            equipment=walk.equipment,
            equipment_filter=walk.equipment_filter,
            equipment_filtered=walk.equipment_filtered,
            min_sample=MIN_SAMPLE,
            rungs=[TierAttemptOut.of(rung) for rung in walk.rungs],
        )


# ---------------------------------------------------------------------------
# Price estimate (PRD section 9)
# ---------------------------------------------------------------------------


class EquipmentCountOut(BaseModel):
    """One line of the accepted pool's equipment breakdown (DECISIONS.md D15).

    Sent as objects rather than as the domain's ``(type, count)`` pairs because
    a JSON consumer indexing ``[0]``/``[1]`` is a positional contract nobody
    writes down. The order is the domain's — commonest first — and is preserved.
    """

    equipment: str
    load_count: int


class PriceEstimateOut(BaseModel):
    """What this load should cost, and exactly what backs the number.

    Every money field is nullable and each ``null`` means something different
    from zero: no rung accepted, or a load with no usable distance. The rate per
    mile is still reported in the second case, and ``provenance`` — built
    upstream from these same values — says which case it is.

    ``confidence`` is the domain's label (high/medium/low), never re-derived
    here from ``load_count``: DECISIONS.md D15 caps a heterogeneous pool at
    medium, and a caller recomputing from the count alone would disagree.
    """

    load_id: str
    tier: str | None
    load_count: int
    confidence: str
    rate_per_mile_p25: float | None
    rate_per_mile_p50: float | None
    rate_per_mile_p75: float | None
    point_usd: float | None
    low_usd: float | None
    high_usd: float | None
    distance_miles: float | None
    first_load_date: date | None
    last_load_date: date | None
    equipment_filter: str
    load_equipment: str
    equipment_mix: list[EquipmentCountOut]
    is_heterogeneous: bool
    provenance: str
    walk: TierWalkOut

    @classmethod
    def of(cls, load_id: str, estimate: PriceEstimate) -> PriceEstimateOut:
        return cls(
            load_id=load_id,
            tier=estimate.tier,
            load_count=estimate.load_count,
            confidence=str(estimate.confidence),
            rate_per_mile_p25=estimate.rate_per_mile_p25,
            rate_per_mile_p50=estimate.rate_per_mile_p50,
            rate_per_mile_p75=estimate.rate_per_mile_p75,
            point_usd=estimate.point_usd,
            low_usd=estimate.low_usd,
            high_usd=estimate.high_usd,
            distance_miles=estimate.distance_miles,
            first_load_date=estimate.first_load_date,
            last_load_date=estimate.last_load_date,
            equipment_filter=estimate.equipment_filter,
            load_equipment=estimate.load_equipment,
            equipment_mix=[
                EquipmentCountOut(equipment=equipment, load_count=count)
                for equipment, count in estimate.equipment_mix
            ],
            is_heterogeneous=estimate.is_heterogeneous,
            provenance=estimate.provenance,
            walk=TierWalkOut.of(estimate.walk),
        )


# ---------------------------------------------------------------------------
# Recommendations (PRD section 8)
# ---------------------------------------------------------------------------


class SignalOut(BaseModel):
    """One signal's arithmetic and the sentence generated from it.

    ``observed`` is the raw fact the reason quotes; ``value`` is what the score
    used; ``contribution`` is the points out of 100. All three come off the same
    :class:`~app.domain.scoring.Signal`, so the sentence in ``reasons`` and the
    numbers here can never describe different computations.
    """

    name: str
    label: str
    weight: float
    observed: float | None
    value: float
    contribution: float
    reason: str

    @classmethod
    def of(cls, signal: Signal) -> SignalOut:
        return cls(
            name=signal.name,
            label=signal.label,
            weight=signal.weight,
            observed=signal.observed,
            value=signal.value,
            contribution=signal.contribution,
            reason=signal.reason,
        )


class LastDeliveryOut(BaseModel):
    """Where this carrier's truck last ended up, and which load put it there.

    The deadhead miles were measured from ``lat``/``lon``; ``location`` is the
    town the reason names. One object, one event — so the distance scored and
    the place named cannot come apart.

    ``lat``/``lon`` are ``null`` when the delivery went somewhere the offline geo
    table does not know: we have the load and the town, and no coordinates to
    measure from, so ``deadhead_miles`` is ``null`` too and the reason says which
    gap it is (D23). That is a different response from ``last_delivery: null``,
    which means the carrier has never delivered anything for this broker.
    """

    source_load_id: str
    lat: float | None
    lon: float | None
    at: datetime | None
    location: StopLocationOut

    @classmethod
    def of(cls, delivery: LastDelivery | None) -> LastDeliveryOut | None:
        if delivery is None:
            return None
        return cls(
            source_load_id=delivery.source_load_id,
            lat=delivery.lat,
            lon=delivery.lon,
            at=delivery.at,
            location=StopLocationOut.of(delivery.location),
        )


class CarrierScoreOut(BaseModel):
    """One ranked carrier: the published score, the reasons, and the evidence.

    ``score`` is the number to display — the weighted sum rounded once, half-up
    (PRD section 8's presentation contract). ``score_exact`` is what the sort
    used; two carriers can print the same digit and still be correctly ordered.
    Never re-round or re-add anything on the client: the sum of ``contribution``
    over ``signals`` *is* ``score_exact``, and computing it again is a second
    source of truth for a number that already exists.

    ``reasons`` is ordered by PRD section 8's signal order with the non-scoring
    rate note last. Render it as given: no re-sorting, no truncating to three.
    """

    rank: int
    carrier: CarrierOut
    score: float
    score_exact: float
    reasons: list[str]
    signals: list[SignalOut]
    lane_loads: int
    last_lane_load_date: date | None
    days_since_lane_load: int | None
    equipment_loads: int
    deadhead_miles: float | None
    last_delivery: LastDeliveryOut | None
    on_time_count: int
    on_time_eligible_count: int
    observed_on_time_rate: float | None
    avg_rate_per_mile: float | None

    @classmethod
    def of(cls, score: CarrierScore) -> CarrierScoreOut:
        carrier = CarrierOut.of(score.carrier)
        assert carrier is not None  # a scored carrier always exists
        return cls(
            rank=score.rank,
            carrier=carrier,
            score=score.score,
            score_exact=score.score_exact,
            reasons=list(score.reasons),
            signals=[SignalOut.of(signal) for signal in score.signals],
            lane_loads=score.lane_loads,
            last_lane_load_date=score.last_lane_load_date,
            days_since_lane_load=score.days_since_lane_load,
            equipment_loads=score.equipment_loads,
            deadhead_miles=score.deadhead_miles,
            last_delivery=LastDeliveryOut.of(score.last_delivery),
            on_time_count=score.on_time_count,
            on_time_eligible_count=score.on_time_eligible_count,
            observed_on_time_rate=score.observed_on_time_rate,
            avg_rate_per_mile=score.avg_rate_per_mile,
        )


class RecommendationsOut(BaseModel):
    """Every carrier this broker has used, best first. Nobody is dropped.

    A carrier with no lane history, no known truck position and the wrong
    trailer still appears — last, with the reasons saying which of those is
    true. A silently short list is indistinguishable from a bug.

    ``tier``/``lane_key`` are ``null`` when no rung cleared the minimum; ``basis``
    then says so in words rather than presenting a thinner answer as the usual
    one. ``as_of`` is the Central date of the newest ingested sync file, not the
    wall clock, which is what makes an answer reproducible from the fixture.
    """

    load_id: str
    as_of: date
    tier: str | None
    lane_key: str | None
    equipment_pool: str
    lane_load_count: int
    lane_on_time_count: int
    lane_on_time_eligible_count: int
    lane_on_time_rate: float | None
    basis: str
    walk: TierWalkOut
    carriers: list[CarrierScoreOut]

    @classmethod
    def of(cls, load_id: str, ranking: CarrierRanking) -> RecommendationsOut:
        return cls(
            load_id=load_id,
            as_of=ranking.as_of,
            tier=ranking.tier,
            lane_key=ranking.lane_key,
            equipment_pool=ranking.equipment_pool,
            lane_load_count=ranking.lane_load_count,
            lane_on_time_count=ranking.lane_on_time_count,
            lane_on_time_eligible_count=ranking.lane_on_time_eligible_count,
            lane_on_time_rate=ranking.lane_on_time_rate,
            basis=ranking.basis,
            walk=TierWalkOut.of(ranking.walk),
            carriers=[CarrierScoreOut.of(score) for score in ranking.carriers],
        )


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------


class IngestReportOut(BaseModel):
    """What a replay did. ``files_skipped`` is the idempotency showing its work.

    The per-file outcomes are deliberately not sent: 132 rows of "already
    present" is noise, and the totals plus ``summary`` are what the endpoint is
    for. The authoritative per-file record is ``sync_files`` itself.
    """

    files_discovered: int
    files_ingested: int
    files_skipped: int
    events_written: int
    loads_touched: int
    lane_keys_rebuilt: int
    carriers_repositioned: int
    summary: str

    @classmethod
    def of(cls, report: IngestionReport) -> IngestReportOut:
        return cls(
            files_discovered=report.files_discovered,
            files_ingested=report.files_ingested,
            files_skipped=report.files_skipped,
            events_written=report.events_written,
            loads_touched=report.loads_touched,
            lane_keys_rebuilt=report.lane_keys_rebuilt,
            carriers_repositioned=report.carriers_repositioned,
            summary=report.summary(),
        )
