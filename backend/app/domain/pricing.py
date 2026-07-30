"""The tier walk and the price estimate (PRD sections 7 and 9).

Two questions, in order. **Which evidence answers this load?** — walk
``ZIP3 → METRO → REGION → REGION_ANY`` and stop at the first rung backed by at
least :data:`MIN_SAMPLE` loads. **What does that evidence say?** — median $/mi ×
the load's miles for the point, p25/p75 for the range, plus the label saying how
far the walk had to go and how much it found.

Three things here are load-bearing rather than decorative:

* **The trace is a return value, not a log line.** :class:`TierWalk` carries
  every rung tried and the count at each, including the ones that fell short and
  the ones a geo-null load could not even form. ``data/TRACEABILITY.md``
  documents that trace for all 16 day-11 loads and Phase 9 asserts against it, so
  "which rungs did you try" has to be answerable by the caller, not just by
  whoever is reading the logs.

* **The provenance sentence is built from the same locals as the numbers.**
  Invariant 2 is about carrier reasons, but a price provenance line that could
  disagree with its own estimate is the same bug. :func:`price_estimate` derives
  every field once and formats the sentence from those values, so there is no
  second path that could name a different tier or a different count.

* **Nothing here touches the database.** The walk takes a lookup callable, which
  the repository supplies (:meth:`~app.repository.BrokerRepository.lane_stats_for`).
  That keeps this module a pure function of its inputs and testable with a dict.

**Confidence** is PRD section 9 — high ≥15 loads, medium 5-14, low below that or
on either region rung — with one cap on top of it, DECISIONS.md D15: an accepted
pool that is *heterogeneous in equipment* cannot be high. That happens only when
the equipment filter was skipped, which per D6 is only when the load's own
equipment is ``UNKNOWN``. On the real fixture that pool's p75 of $2.67/mi sits in
the empty gap between the highest dry van (2.54) and the lowest reefer (2.81) — a
rate nobody has ever been paid, which was being labelled "high" on count alone.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from .lanes import (
    TIER_REGION,
    TIER_REGION_ANY,
    TIERS,
    LaneKey,
    query_equipment,
    query_keys_for_load,
)
from .localtime import central_date
from .model import ANY_EQUIPMENT, LaneStats, Load

__all__ = [
    "MIN_SAMPLE",
    "HIGH_CONFIDENCE_LOADS",
    "Confidence",
    "TierAttempt",
    "TierWalk",
    "PriceEstimate",
    "LaneStatsLookup",
    "EquipmentMixLookup",
    "walk_tiers",
    "price_estimate",
    "estimate_price",
]

#: Loads needed to accept a rung (PRD section 7, CLAUDE.md "Known traps").
MIN_SAMPLE = 5

#: Loads needed for high confidence (PRD section 9). Medium spans 5-14.
HIGH_CONFIDENCE_LOADS = 15


class Confidence(StrEnum):
    """How much to trust an estimate. Lower-cased because it is a UI label."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


#: Weakest to strongest, so a cap is a ``min`` over this order rather than a
#: chain of ifs that could disagree with itself.
_CONFIDENCE_ORDER: tuple[Confidence, ...] = (
    Confidence.LOW,
    Confidence.MEDIUM,
    Confidence.HIGH,
)

#: How each equipment pool reads in a sentence a freight rep would say out loud.
_EQUIPMENT_WORDS: dict[str, str] = {
    "DRY_VAN": "dry van",
    "REEFER": "reefer",
    "FLATBED": "flatbed",
    "UNKNOWN": "unknown equipment",
    ANY_EQUIPMENT: "all equipment types",
}


def _cap(confidence: Confidence, ceiling: Confidence) -> Confidence:
    """The weaker of two confidences."""
    return min(confidence, ceiling, key=_CONFIDENCE_ORDER.index)


# ---------------------------------------------------------------------------
# The walk
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TierAttempt:
    """One rung of the walk: what was asked, what was found, what was decided.

    ``key`` is ``None`` for a rung a geo-null load cannot form — the walk did not
    reject it on evidence, it could not ask the question. That distinction stays
    visible rather than being flattened into "0 loads found".
    """

    tier: str
    key: LaneKey | None
    load_count: int
    accepted: bool
    stats: LaneStats | None = None

    @property
    def skipped(self) -> bool:
        """True when this rung was unreachable, not merely thin."""
        return self.key is None

    @property
    def verdict(self) -> str:
        """One line, in the vocabulary ``data/TRACEABILITY.md`` uses."""
        if self.skipped:
            return "skipped, lane end not on the map"
        if self.accepted:
            return "ACCEPTED"
        return f"rejected, {self.load_count} < {MIN_SAMPLE}"


@dataclass(frozen=True, slots=True)
class TierWalk:
    """Every rung tried, narrow to wide, and the one that won.

    ``accepted is None`` means no rung cleared the minimum — a broker with almost
    no history. The walk still reports what it tried; "we have no evidence" is an
    answer, and a silent empty response is not.
    """

    rungs: tuple[TierAttempt, ...]
    accepted: TierAttempt | None
    #: The pool rungs 1-3 asked for — the load's equipment, or ``ANY`` when D6
    #: skipped the filter. Kept even when nothing was accepted, because "we found
    #: no dry-van history" and "we found no history of any kind" are different
    #: statements and a walk that accepted nothing still has to make one of them.
    equipment: str

    @property
    def tier(self) -> str | None:
        return None if self.accepted is None else self.accepted.tier

    @property
    def load_count(self) -> int:
        return 0 if self.accepted is None else self.accepted.load_count

    @property
    def equipment_filter(self) -> str:
        """The pool the answer came from: an equipment type, or ``ANY``."""
        if self.accepted is None or self.accepted.key is None:
            return self.equipment
        return self.accepted.key.equipment

    @property
    def equipment_filtered(self) -> bool:
        """False when the accepted pool spans every equipment type.

        Either because the load's own equipment is ``UNKNOWN`` and D6 skipped the
        filter, or because the walk reached rung 4, which has no filter by
        definition. Both mean the same thing to a reader of the estimate: these
        rates are not like-for-like.
        """
        return self.equipment_filter != ANY_EQUIPMENT


#: Reads one bucket's derived row. The repository supplies it; the walk never
#: learns what a database is.
LaneStatsLookup = Callable[[LaneKey], LaneStats | None]

#: Counts the accepted pool by equipment type, for D15. Only ever called for an
#: unfiltered pool, where the answer can be more than one entry.
EquipmentMixLookup = Callable[[LaneKey], Mapping[str, int]]


def walk_tiers(load: Load, lane_stats: LaneStatsLookup) -> TierWalk:
    """Walk outward until a rung has :data:`MIN_SAMPLE` loads. Report all of it.

    Stops at the first rung that clears the minimum, so the rungs after it are
    absent from the trace rather than present with counts — they were never
    asked, and claiming a count for them would be inventing evidence.
    """
    equipment = query_equipment(load)
    rungs: list[TierAttempt] = []
    for tier, key in zip(TIERS, query_keys_for_load(load), strict=True):
        if key is None:
            rungs.append(TierAttempt(tier=tier, key=None, load_count=0, accepted=False))
            continue
        stats = lane_stats(key)
        count = 0 if stats is None else stats.load_count
        accepted = count >= MIN_SAMPLE
        attempt = TierAttempt(
            tier=tier, key=key, load_count=count, accepted=accepted, stats=stats
        )
        rungs.append(attempt)
        if accepted:
            return TierWalk(
                rungs=tuple(rungs), accepted=attempt, equipment=equipment
            )
    return TierWalk(rungs=tuple(rungs), accepted=None, equipment=equipment)


# ---------------------------------------------------------------------------
# The estimate
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PriceEstimate:
    """What this load should cost to cover, and exactly what backs that number.

    Every money field is nullable, and each ``None`` means something different
    from zero: no rung accepted (nothing to go on), a lane with no rate per mile
    at all, or a load whose distance is missing, zero or negative (a rate that
    cannot be turned into dollars). Rendering any of them as ``$0`` would be a
    wrong number wearing a confident label, which is the failure invariant 6
    exists to prevent — hence ``provenance``, which is populated in every one of
    those cases, says which one it is, and always ends in the words "no dollar
    estimate" when the dollars are absent (D23).

    ``equipment_mix`` is empty when the equipment filter was on, and otherwise
    counts the accepted pool by type (DECISIONS.md D15).
    """

    tier: str | None
    load_count: int
    confidence: Confidence
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
    equipment_mix: tuple[tuple[str, int], ...]
    provenance: str
    walk: TierWalk

    @property
    def is_heterogeneous(self) -> bool:
        """True when the accepted pool mixes equipment types (D15)."""
        return len(self.equipment_mix) > 1


def _usable_miles(miles: float | None) -> float | None:
    """The load's distance if it can multiply a rate into dollars, else ``None``.

    Zero, ``None`` and **negative** all fail. A TMS that reports −271 mi has told
    us something impossible, and multiplying by it produces MINUS $700 wearing a
    medium-confidence label; a negative dollar figure means nothing at all, where
    a missing one means "we could not say" (D23). The distance itself is still
    reported on the estimate and still displayed — refusing to price it is not
    the same as pretending it was never stated.
    """
    if miles is None or miles <= 0:
        return None
    return miles


def _dollars(rate: float | None, miles: float | None) -> float | None:
    """``rate × miles``, rounded to cents. ``None`` when either is unusable.

    Guards the unusable distance explicitly: a load with no distance, a distance
    of zero or a negative one has no dollar figure to give — the *rate* is still
    reported, and the provenance says why the dollars are missing.
    """
    usable = _usable_miles(miles)
    if rate is None or usable is None:
        return None
    return round(rate * usable, 2)


def _confidence(
    *,
    tier: str | None,
    load_count: int,
    heterogeneous: bool,
    non_positive_rate: bool,
) -> Confidence:
    """PRD section 9, then the D15 cap, then the D23 one.

    The D15 cap can only bind on rungs 1-3 with the filter off, i.e. the D6
    filter-skip case: rung 4 is unfiltered too, but it is already low by rule.

    ``non_positive_rate`` says a published percentile is at or below zero, which
    happens when booked rates of $0 sit in the pool — two of them in a five-load
    lane put p25 exactly on zero. The percentile is arithmetically right and the
    dollar figure it produces ($0.00) is not a price anyone would quote, so the
    estimate goes out at **low** confidence with the provenance naming it, which
    is the one thing the earlier version did not do (D23).
    """
    if tier is None or load_count < MIN_SAMPLE:
        return Confidence.LOW
    if tier in (TIER_REGION, TIER_REGION_ANY):
        return Confidence.LOW
    if non_positive_rate:
        return Confidence.LOW
    level = (
        Confidence.HIGH if load_count >= HIGH_CONFIDENCE_LOADS else Confidence.MEDIUM
    )
    return _cap(level, Confidence.MEDIUM) if heterogeneous else level


def _lane_label(key: LaneKey) -> str:
    """How the accepted bucket reads in a sentence.

    The two region rungs are one place, so naming both ends would read
    ``TX_TRIANGLE→TX_TRIANGLE``; rung 4 additionally announces that its pool has
    no equipment filter, because that is a property of the *lane* there rather
    than of the load.
    """
    if key.tier == TIER_REGION_ANY:
        return f"{key.origin_key} (any equipment)"
    if key.tier == TIER_REGION:
        return key.origin_key
    return key.lane_key


def _mix_phrase(mix: tuple[tuple[str, int], ...]) -> str:
    """``"23 dry van, 8 reefer"`` — D15's naming of the pool."""
    return ", ".join(
        f"{count} {_EQUIPMENT_WORDS.get(equipment, equipment.lower())}"
        for equipment, count in mix
    )


def price_estimate(
    load: Load,
    walk: TierWalk,
    equipment_mix: Mapping[str, int] | None = None,
) -> PriceEstimate:
    """Turn an accepted rung into dollars, a confidence and a provenance line.

    Every value below is computed once and the sentence is formatted from those
    same values, so the label cannot describe a different estimate than the one
    it is attached to.
    """
    accepted = walk.accepted
    stats = None if accepted is None else accepted.stats
    key = None if accepted is None else accepted.key
    miles = load.distance_miles
    load_equipment = str(load.equipment)

    # Only meaningful for an unfiltered pool; a filtered one is one type by
    # construction and saying so adds noise, not provenance.
    mix: tuple[tuple[str, int], ...] = ()
    if key is not None and key.equipment == ANY_EQUIPMENT and equipment_mix:
        mix = tuple(sorted(equipment_mix.items(), key=lambda kv: (-kv[1], kv[0])))

    p25 = None if stats is None else stats.rate_per_mile_p25
    p50 = None if stats is None else stats.rate_per_mile_p50
    p75 = None if stats is None else stats.rate_per_mile_p75
    point = _dollars(p50, miles)
    low = _dollars(p25, miles)
    high = _dollars(p75, miles)
    first_date = (
        None
        if stats is None or stats.first_load_at is None
        else central_date(stats.first_load_at)
    )
    last_date = (
        None
        if stats is None or stats.last_load_at is None
        else central_date(stats.last_load_at)
    )
    load_count = 0 if accepted is None else accepted.load_count
    # Computed once, here, and handed to both the label and the sentence, so
    # neither can describe a pool the other did not see.
    non_positive = _non_positive_rates(p25, p50, p75)
    confidence = _confidence(
        tier=walk.tier,
        load_count=load_count,
        heterogeneous=len(mix) > 1,
        non_positive_rate=bool(non_positive),
    )

    return PriceEstimate(
        tier=walk.tier,
        load_count=load_count,
        confidence=confidence,
        rate_per_mile_p25=p25,
        rate_per_mile_p50=p50,
        rate_per_mile_p75=p75,
        point_usd=point,
        low_usd=low,
        high_usd=high,
        distance_miles=miles,
        first_load_date=first_date,
        last_load_date=last_date,
        equipment_filter=walk.equipment_filter,
        load_equipment=load_equipment,
        equipment_mix=mix,
        provenance=_provenance(
            key=key,
            load_equipment=load_equipment,
            load_count=load_count,
            p50=p50,
            miles=miles,
            first_date=first_date,
            last_date=last_date,
            mix=mix,
            confidence=confidence,
            non_positive=non_positive,
            walk=walk,
        ),
        walk=walk,
    )


def _non_positive_rates(
    p25: float | None, p50: float | None, p75: float | None
) -> tuple[tuple[str, float], ...]:
    """The published percentiles that are at or below zero, named (D23).

    Zero is what a lane looks like when booked rates of $0 are in it — "we were
    not told what this cost" recorded as a number. The percentile is honest; the
    dollars it produces are not a price, so the estimate must say so.
    """
    named = (("p25", p25), ("median", p50), ("p75", p75))
    return tuple((label, value) for label, value in named if value is not None and value <= 0)


def _provenance(
    *,
    key: LaneKey | None,
    load_equipment: str,
    load_count: int,
    p50: float | None,
    miles: float | None,
    first_date: date | None,
    last_date: date | None,
    mix: tuple[tuple[str, int], ...],
    confidence: Confidence,
    non_positive: tuple[tuple[str, float], ...],
    walk: TierWalk,
) -> str:
    """The one-line explanation, formatted from the estimate's own values.

    Takes the numbers rather than re-reading them from anywhere, so there is no
    arrangement of the code in which the sentence and the fields come from two
    different places. Every branch says which tier and how many loads, because a
    walk that found nothing still has to report what it tried (invariant 6).

    **Every absent dollar figure ends in the words "no dollar estimate".** That
    is the contract :class:`PriceEstimate` states, and it has to hold in the
    branch where no rung was accepted too, not only where a rate exists and the
    distance is missing — a reader (or a UI) checking one phrase must not have to
    know which of four ways the dollars went missing (D23).
    """
    if key is None:
        tried = ", ".join(
            f"{rung.tier} {rung.load_count}" for rung in walk.rungs if not rung.skipped
        )
        pool = _EQUIPMENT_WORDS.get(walk.equipment, walk.equipment)
        return (
            f"no estimate: no tier reached the {MIN_SAMPLE}-load minimum for {pool} "
            f"(tried {tried or 'nothing — lane ends not on the map'}), so no "
            "dollar estimate"
        )

    # The equipment phrase names the *load* on rung 4 (the lane label already
    # said the pool is unfiltered there) and the *pool* everywhere else, which is
    # where "all equipment types" means the D6 filter skip.
    if key.tier == TIER_REGION_ANY:
        equipment_phrase = _EQUIPMENT_WORDS.get(load_equipment, load_equipment)
    else:
        equipment_phrase = _EQUIPMENT_WORDS.get(key.equipment, key.equipment)

    span = (
        f", {first_date} to {last_date}" if first_date and last_date else ""
    )
    line = (
        f"median of {load_count} loads on {_lane_label(key)}, "
        f"{equipment_phrase}{span} — {confidence} confidence"
    )
    if len(mix) > 1:
        line += (
            f"; mixed pool ({load_count} loads: {_mix_phrase(mix)}), "
            "so confidence is capped at medium"
        )
    if non_positive:
        stated = ", ".join(f"{label} {value:.4f}" for label, value in non_positive)
        line += (
            f"; {stated} $/mi — loads with no positive booked rate are in this "
            "pool, so confidence is held at low"
        )
    if p50 is None:
        line += "; no rate per mile on this lane, so no dollar estimate"
    elif miles is None or miles == 0:
        line += (
            f"; {p50:.4f} $/mi, but this load has no distance, so no dollar estimate"
        )
    elif miles < 0:
        line += (
            f"; {p50:.4f} $/mi, but this load's distance is {miles:.1f} mi, which "
            "cannot be priced, so no dollar estimate"
        )
    return line


def estimate_price(
    load: Load,
    *,
    lane_stats: LaneStatsLookup,
    equipment_mix: EquipmentMixLookup,
) -> PriceEstimate:
    """Walk the tiers and price the winner. The whole of PRD section 9.

    The equipment mix is only fetched for an unfiltered accepted pool — a
    filtered one is one type by construction, so the extra query would be asking
    a question whose answer is already known.
    """
    walk = walk_tiers(load, lane_stats)
    accepted = walk.accepted
    mix: Mapping[str, int] | None = None
    if accepted is not None and accepted.key is not None:
        if accepted.key.equipment == ANY_EQUIPMENT:
            mix = equipment_mix(accepted.key)
    return price_estimate(load, walk, mix)
