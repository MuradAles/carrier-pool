"""Carrier ranking: five signals, one score, and the reasons that explain it.

PRD section 8. For a load looking for a truck, score **every** carrier the
broker has used — 0-100, a weighted sum of five signals each in 0-1 — and hand
back the arithmetic alongside the answer.

The lane a carrier's experience is measured on is **the rung the tier walk
accepted**, not a lane this module picks for itself. Pricing and ranking
therefore answer from the same evidence: an estimate built on ``METRO`` cannot
sit next to a ranking built on ``ZIP3``, because both read
:class:`~app.domain.pricing.TierWalk`.

**Invariant 2 is the whole point of this module.** Every reason is a string
formatted from the same locals that produced the value it describes, inside the
function that produced it — see :func:`_experience`, :func:`_recency`,
:func:`_equipment`, :func:`_deadhead`, :func:`_on_time`. Each returns a
:class:`Signal` carrying the raw observation, the adjusted value the score used,
the weight, and the sentence. :meth:`CarrierScore.reasons` reads that tuple and
nothing else; there is no path in which a reason is computed from a second
lookup, and none in which a displayed number could be recomputed differently
from the scored one.

**Raw versus adjusted, stated once.** Reasons quote the **raw observation** —
"8 loads", "5 of 6 on-time" — because that is the fact a rep can check against
their own memory. The score uses the **adjusted** value. Where the two differ
(on-time, which shrinks) the sentence names both, so the reason can never be
read as claiming the adjustment was the observation. Both come from the same
:class:`Signal`.

**Two cold-start formulas, deliberately different** (DECISIONS.md D5)::

    experience = n / (n + k)                                k = 5   # a count
    on_time    = (on_time_count + lane_avg * k) / (n + k)   k = 5   # a rate

Shrinking the *count* toward a lane average would let a carrier with no loads on
the lane score like an average one — rewarding the absence of evidence. The
saturating form is zero at zero and monotone, so 2-for-2 (0.29) cannot beat
164-for-200 (0.98) on either signal (CLAUDE.md, Known traps).

The two shapes PRD section 8 left open are pinned by DECISIONS.md D12 and by
``data/TRACEABILITY.md``, which was computed before this module existed::

    recency  = exp(-days_since_last_lane_load / 30)
    deadhead = clamp((250 - miles_from_last_delivery) / 200, 0, 1)

so the table is an independent check on this code rather than a restatement of
it. D10's caveat applies to deadhead and is deliberately **not** corrected here:
our miles run ~13% high against real road miles, so the absolute 50/250 mi
thresholds bite slightly early. That is a documented, one-directional
pessimism, not a bug to tune away.

**Nothing here touches the database.** :func:`rank_carriers` is a pure function
of a load, a walk and a :class:`RankingInputs` — which the repository builds in
one bound session (:meth:`~app.repository.BrokerRepository.ranking_inputs`).
Unit tests construct one by hand.

**Nobody is dropped.** A carrier with no history on this lane, no known truck
position and the wrong trailer still comes back — last, with a reason that says
which of those is true (TASKS.md R5). A silently short list is indistinguishable
from a bug, and "we have nothing on them" is an answer.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from .distance import road_miles
from .lanes import TIER_REGION_ANY, LaneKey
from .localtime import central_date
from .model import Carrier, CarrierStats, Equipment, LastDelivery, Load
# Imported rather than restated: the words a lane and an equipment pool are
# called in a sentence have to be the same ones the price provenance uses, or a
# rep reads two names for one bucket on a single screen.
from .pricing import MIN_SAMPLE, _EQUIPMENT_WORDS, TierWalk, _lane_label

__all__ = [
    "SHRINK_K",
    "RECENCY_DECAY_DAYS",
    "DEADHEAD_FULL_CREDIT_MILES",
    "DEADHEAD_NO_CREDIT_MILES",
    "NEUTRAL",
    "SIGNAL_EXPERIENCE",
    "SIGNAL_RECENCY",
    "SIGNAL_EQUIPMENT",
    "SIGNAL_DEADHEAD",
    "SIGNAL_ON_TIME",
    "SIGNALS",
    "SCORE_DECIMALS",
    "WEIGHTS",
    "round_half_up",
    "Signal",
    "CarrierScore",
    "CarrierRanking",
    "RankingInputs",
    "experience_credit",
    "recency_credit",
    "deadhead_credit",
    "shrunk_on_time",
    "score_carrier",
    "rank_carriers",
]

#: Cold-start constant for both formulas (PRD section 8, DECISIONS.md D5).
SHRINK_K = 5

#: Recency e-folding time in days (DECISIONS.md D12).
RECENCY_DECAY_DAYS = 30.0

#: Deadhead: full credit at or under this, zero at or over the other (PRD
#: section 8), linear between — so the curve is monotonically non-increasing.
DEADHEAD_FULL_CREDIT_MILES = 50.0
DEADHEAD_NO_CREDIT_MILES = 250.0

#: Neither reward nor punishment. Used wherever the missing datum belongs to
#: the *load* or to the *lane* rather than to one carrier, so that every carrier
#: is affected identically and the value cannot reorder the list: equipment when
#: the load's own equipment is ``UNKNOWN`` (invariant 5 as a number — 1 rewards
#: a gap, 0 punishes a carrier for it), deadhead when the load's pickup has no
#: coordinates, and on-time when the accepted lane has produced no delivery
#: verdicts at all. A gap in one *carrier's* record is the opposite case and
#: earns nothing; see :func:`deadhead_credit`.
NEUTRAL = 0.5

#: Decimal places the published score carries (PRD section 8, DECISIONS.md D19).
SCORE_DECIMALS = 1

SIGNAL_EXPERIENCE = "lane_experience"
SIGNAL_RECENCY = "recency"
SIGNAL_EQUIPMENT = "equipment"
SIGNAL_DEADHEAD = "deadhead"
SIGNAL_ON_TIME = "on_time"

#: PRD section 8's weights. The order is the order of the table there, which is
#: also the order reasons are rendered in.
WEIGHTS: dict[str, float] = {
    SIGNAL_EXPERIENCE: 0.35,
    SIGNAL_RECENCY: 0.20,
    SIGNAL_EQUIPMENT: 0.15,
    SIGNAL_DEADHEAD: 0.20,
    SIGNAL_ON_TIME: 0.10,
}
SIGNALS: tuple[str, ...] = tuple(WEIGHTS)
assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9, "PRD section 8 weights must sum to 1"

#: How each signal reads as a column heading.
_LABELS: dict[str, str] = {
    SIGNAL_EXPERIENCE: "Lane experience",
    SIGNAL_RECENCY: "Recency",
    SIGNAL_EQUIPMENT: "Equipment match",
    SIGNAL_DEADHEAD: "Deadhead",
    SIGNAL_ON_TIME: "On-time",
}


def round_half_up(value: float, places: int = SCORE_DECIMALS) -> float:
    """Round half **away from zero** — 22.05 becomes 22.1, never 22.0.

    PRD section 8's presentation contract (DECISIONS.md D19). Python's built-in
    :func:`round` is worse than half-to-even: it rounds the *binary* value, so
    its direction varies by number rather than by rule. ``69.55`` is stored as
    ``69.5499…`` and rounds **down** to 69.5, while ``22.05`` is stored as
    ``22.0500…071`` and rounds **up** to 22.1 — the same nominal ``.X5``, two
    different answers, with nothing in the source to say which you get. On this
    fixture that is not hypothetical: six of the 192 day-11 ranking rows sit
    exactly on a boundary.

    Half-up rather than half-even because it is what PostgreSQL's ``NUMERIC``
    rounding does and what a rep redoing the arithmetic on a calculator does,
    and because ``data/TRACEABILITY.md`` computes at display precision the same
    way (DECISIONS.md D18). The two scorers stay independent (D12) and agree on
    the printed digit by sharing the *rule*, never the code.

    ``Decimal(str(value))`` so the input is the float's shortest repr rather
    than its binary tail: ``22.05`` is really ``22.0499999...`` in binary, which
    would round down under any rule applied to the raw bits.
    """
    quantum = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


# ---------------------------------------------------------------------------
# The five curves, each a pure function of one observation
# ---------------------------------------------------------------------------


def experience_credit(lane_loads: int) -> float:
    """``n / (n + k)`` — saturating, zero at zero (DECISIONS.md D5)."""
    return lane_loads / (lane_loads + SHRINK_K)


def recency_credit(days_since: float | None) -> float:
    """``exp(-days / 30)``. ``None`` — never ran the lane — earns nothing.

    Not shrunk toward anything for the same reason experience is not: a carrier
    with no load on this lane has no recency to be uncertain about.
    """
    if days_since is None:
        return 0.0
    return math.exp(-max(days_since, 0.0) / RECENCY_DECAY_DAYS)


def deadhead_credit(miles: float | None) -> float:
    """Full credit at 50 mi, none at 250, linear between (PRD section 8).

    ``None`` here means **this carrier** has no placeable delivery behind them,
    and earns **zero** — the same answer the reference model in
    ``backend/scripts/generate_data.py`` gives.

    That is deliberate and it is *not* the same case as ``UNKNOWN`` equipment,
    which scores :data:`NEUTRAL`. The difference is whose data is missing.
    ``UNKNOWN`` equipment is a gap in the **load**, identical for every carrier,
    so a neutral value shifts all scores equally and cannot reorder anyone.
    An unknown truck position is a gap in **one carrier's** record, so neutral
    credit would rank a carrier we know nothing about above every carrier we
    know to be more than 150 mi out — rewarding the absence of evidence, which
    is the failure DECISIONS.md D5 exists to prevent. The reason string names
    the gap instead of letting a 0 read as "300 miles away".

    The load-side version of this gap — a pickup with no coordinates, so nobody
    can be measured — is handled by :func:`_deadhead`, which scores it
    :data:`NEUTRAL` for exactly the ``UNKNOWN``-equipment reason.
    """
    if miles is None:
        return 0.0
    span = DEADHEAD_NO_CREDIT_MILES - DEADHEAD_FULL_CREDIT_MILES
    return max(0.0, min(1.0, (DEADHEAD_NO_CREDIT_MILES - miles) / span))


def shrunk_on_time(
    *, on_time_count: int, eligible_count: int, lane_rate: float | None
) -> float:
    """``(on_time + lane_avg × k) / (n + k)`` — a rate shrunk toward its lane.

    ``eligible_count`` is **loads with a verdict**, not loads: a load still
    rolling has no arrival, so ``delivered_on_time`` is ``None`` for it and
    dividing by the carrier's whole load count would score every truck in motion
    as a miss — understating exactly the busiest carriers (TASKS.md R1). The two
    counts are stored separately by ingestion precisely so this ratio cannot be
    formed the wrong way round.

    ``lane_rate is None`` means the accepted lane has produced no verdicts at
    all, so neither the observation nor the prior exists; the result is
    :data:`NEUTRAL`. That case is rank-neutral by construction — carrier stats
    and lane stats are computed from the same population, so if the lane has no
    verdicts then no carrier on it has one either.
    """
    if lane_rate is None:
        return NEUTRAL
    return (on_time_count + lane_rate * SHRINK_K) / (eligible_count + SHRINK_K)


# ---------------------------------------------------------------------------
# The breakdown
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Signal:
    """One signal's arithmetic and the sentence generated from it.

    ``observed`` is the raw number the reason quotes (loads on the lane, days
    since, deadhead miles, the observed on-time rate) and is ``None`` when there
    was nothing to observe. ``value`` is what the score actually used. Both, the
    weight and the resulting points live on one object, so a caller that renders
    ``reason`` is quoting the same computation that produced ``contribution``.
    """

    name: str
    label: str
    weight: float
    observed: float | None
    value: float
    reason: str

    @property
    def contribution(self) -> float:
        """Points out of 100 this signal contributed. Scores are their sum."""
        return 100.0 * self.weight * self.value


@dataclass(frozen=True, slots=True)
class CarrierScore:
    """One carrier's answer: the score, every signal behind it, and the facts.

    The observation fields are not a second derivation — they are the same
    values passed into the signal builders, kept so the API can render a table
    without re-parsing sentences. ``rank`` is 1-based and assigned by
    :func:`rank_carriers`.

    **The score is not stored.** ``score_exact`` is the sum of the signals'
    contributions and ``score`` is that value rounded once for publication, so
    the only place a score can come from is the breakdown that explains it.
    There is no field that could be set to a number the signals do not add up
    to (invariant 2).
    """

    carrier: Carrier
    rank: int
    signals: tuple[Signal, ...]
    #: Loads this carrier ran on the accepted lane — the experience numerator.
    lane_loads: int
    #: Central date of their most recent delivery on that lane, and the gap to
    #: the as-of date, which is what recency decays over.
    last_lane_load_date: date | None
    days_since_lane_load: int | None
    #: Loads of the *load's* equipment type this carrier has hauled anywhere for
    #: this broker. Zero when the equipment signal scored 0.
    equipment_loads: int
    #: Where their truck last ended up, and how far that is from this pickup.
    last_delivery: LastDelivery | None
    deadhead_miles: float | None
    #: On-time numerator and its answerable denominator, never ``load_count``.
    on_time_count: int
    on_time_eligible_count: int
    observed_on_time_rate: float | None
    #: Their average $/mi on the accepted lane. Not a scored signal — reported
    #: because PRD section 8's reasoning example does, and read from the same
    #: ``carrier_stats`` row as ``lane_loads``.
    avg_rate_per_mile: float | None
    #: The non-scoring rate note, kept separate so ``reasons`` can order it last.
    rate_note: str | None

    @property
    def score_exact(self) -> float:
        """The weighted sum, 0-100, before display rounding.

        What :func:`rank_carriers` orders by. Exposed because a caller checking
        two adjacent carriers needs to see the difference the published score
        rounds away, not because anything should display it.
        """
        return sum(signal.contribution for signal in self.signals)

    @property
    def score(self) -> float:
        """The published 0-100 score: :attr:`score_exact` rounded half-up to
        :data:`SCORE_DECIMALS` (PRD section 8, DECISIONS.md D19).

        Rounded here rather than at the edge so that every consumer — the API,
        the UI, a test — shows the same digits, and so the rule is one named
        function instead of whatever each formatter happens to do at ``.X5``.
        """
        return round_half_up(self.score_exact)

    @property
    def reasons(self) -> tuple[str, ...]:
        """The plain-language explanation, in PRD section 8's signal order.

        Every element is a signal's own ``reason`` — this property reads the
        breakdown and nothing else (invariant 2). The rate note is appended
        last and comes from the same ``carrier_stats`` row the experience signal
        was built from.
        """
        lines = tuple(signal.reason for signal in self.signals)
        return lines if self.rate_note is None else lines + (self.rate_note,)

    def signal(self, name: str) -> Signal:
        """One signal by name. Raises rather than inventing a missing one."""
        for candidate in self.signals:
            if candidate.name == name:
                return candidate
        raise KeyError(name)


@dataclass(frozen=True, slots=True)
class CarrierRanking:
    """Every carrier the broker has used, best first, with the shared basis.

    ``tier``/``lane_key`` are ``None`` when no rung cleared the minimum: there is
    then no lane to have experience on, and the ranking falls back to equipment
    and truck position — which ``basis`` says out loud rather than presenting a
    thinner answer as if it were the usual one (invariant 6).
    """

    as_of: date
    tier: str | None
    lane_key: str | None
    equipment_pool: str
    lane_load_count: int
    lane_on_time_count: int
    lane_on_time_eligible_count: int
    lane_on_time_rate: float | None
    carriers: tuple[CarrierScore, ...]
    basis: str
    walk: TierWalk


@dataclass(frozen=True, slots=True)
class RankingInputs:
    """Everything :func:`rank_carriers` reads, gathered by the repository.

    Mappings are keyed by ``source_carrier_id``. A carrier absent from any of
    them is not an error — it is a carrier with no loads on this lane, no
    history of this equipment, or no placeable delivery, each of which is a
    scoreable fact with its own reason.
    """

    #: The day the question is asked, which recency counts back from. The
    #: repository derives it from the newest sync file rather than the wall
    #: clock, so an answer is reproducible from the fixture forever.
    as_of: date
    carriers: Sequence[Carrier]
    carrier_stats: Mapping[str, CarrierStats]
    equipment_loads: Mapping[str, Mapping[str, int]]
    last_deliveries: Mapping[str, LastDelivery]
    #: The accepted lane's own on-time record — the prior on-time shrinks
    #: toward. Same population as the carrier rows above, so the prior and the
    #: observations describe the same loads.
    lane_on_time_count: int = 0
    lane_on_time_eligible_count: int = 0

    @property
    def lane_on_time_rate(self) -> float | None:
        """The lane average, or ``None`` when the lane has no verdicts yet."""
        if self.lane_on_time_eligible_count == 0:
            return None
        return self.lane_on_time_count / self.lane_on_time_eligible_count


# ---------------------------------------------------------------------------
# The signals — value and sentence built together, from the same locals
# ---------------------------------------------------------------------------


def _plural(count: int, word: str = "load") -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _ago(days: int) -> str:
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    return f"{days} days ago"


def _signal(name: str, *, observed: float | None, value: float, reason: str) -> Signal:
    return Signal(
        name=name,
        label=_LABELS[name],
        weight=WEIGHTS[name],
        observed=observed,
        value=value,
        reason=reason,
    )


def _experience(*, lane_loads: int, lane_phrase: str | None, tier: str | None) -> Signal:
    value = experience_credit(lane_loads)
    if lane_phrase is None:
        reason = (
            f"No lane history to score against — no tier reached the "
            f"{MIN_SAMPLE}-load minimum for this load"
        )
    elif lane_loads == 0:
        reason = f"Has never run {lane_phrase} at the {tier} tier"
    else:
        reason = f"Ran {_plural(lane_loads)} on {lane_phrase} at the {tier} tier"
    return _signal(
        SIGNAL_EXPERIENCE,
        observed=float(lane_loads),
        value=value,
        reason=reason,
    )


def _recency(
    *, days_since: int | None, last_date: date | None, lane_loads: int
) -> Signal:
    value = recency_credit(None if days_since is None else float(days_since))
    if days_since is not None and last_date is not None:
        reason = f"Last load on this lane {_ago(days_since)} ({last_date})"
    elif lane_loads > 0:
        # On the lane, but nothing delivered: the loads are still rolling, so
        # there is no delivery date to decay from. Said explicitly, because
        # "0.00 recency" and "never ran it" are different facts.
        reason = (
            f"Ran this lane {_plural(lane_loads)} but none has delivered yet, "
            "so there is no recency credit"
        )
    else:
        reason = "No load on this lane, so no recency credit"
    return _signal(
        SIGNAL_RECENCY,
        observed=None if days_since is None else float(days_since),
        value=value,
        reason=reason,
    )


def _equipment(*, load_equipment: Equipment, equipment_loads: int) -> Signal:
    word = _EQUIPMENT_WORDS.get(str(load_equipment), str(load_equipment).lower())
    if load_equipment is Equipment.UNKNOWN:
        # Invariant 5: nobody recorded this load's equipment, so the signal must
        # neither reward nor punish. Not a default to DRY_VAN, and not a zero.
        return _signal(
            SIGNAL_EQUIPMENT,
            observed=None,
            value=NEUTRAL,
            reason=(
                "This load's equipment is unknown, so equipment neither helps "
                "nor hurts"
            ),
        )
    hauled = equipment_loads > 0
    value = 1.0 if hauled else 0.0
    reason = (
        f"Has hauled {word} for you ({_plural(equipment_loads)})"
        if hauled
        else f"Has never hauled {word} for you"
    )
    return _signal(
        SIGNAL_EQUIPMENT, observed=float(equipment_loads), value=value, reason=reason
    )


def _deadhead(
    *,
    miles: float | None,
    last_delivery: LastDelivery | None,
    as_of: date,
    pickup_known: bool,
) -> Signal:
    """Proximity credit, and which of three things the number means.

    Three cases, deliberately scored differently (see :func:`deadhead_credit`):

    * **Measured** — the curve.
    * **This carrier has no known delivery** — 0.0. Their own gap; neutral
      credit here would outrank carriers we know are far away.
    * **This load's pickup has no coordinates** — :data:`NEUTRAL`. The load's
      gap, so it applies to every carrier identically and cannot reorder the
      list; scoring it 0.0 would instead drag every carrier down 20 points for
      a fact about the load, making a first-rate carrier read as mediocre.
      This is the ``UNKNOWN``-equipment rule applied to the other signal that
      can be unmeasurable.
    """
    # The load's gap is tested first, and both the value and the sentence branch
    # on the same condition in the same order: a carrier with no position on a
    # load with no pickup must not be told "no proximity credit" while scoring
    # the neutral 0.5 everyone else got.
    if not pickup_known:
        value = NEUTRAL
        reason = (
            "This load's pickup is not on the map, so proximity could not be "
            "measured for anyone — scored neutral"
        )
    elif last_delivery is None:
        value = deadhead_credit(None)
        reason = "No known recent delivery for this carrier, so no proximity credit"
    else:
        value = deadhead_credit(miles)
        assert miles is not None
        when = (
            ""
            if last_delivery.at is None
            else f" {_ago((as_of - central_date(last_delivery.at)).days)}"
        )
        reason = (
            f"Delivered in {last_delivery.location}{when}, "
            f"{miles:.1f} mi from your pickup"
        )
        if value == 0.0:
            reason += (
                f" — past the {DEADHEAD_NO_CREDIT_MILES:.0f} mi cutoff, so no "
                "proximity credit"
            )
    return _signal(SIGNAL_DEADHEAD, observed=miles, value=value, reason=reason)


def _on_time(
    *, on_time_count: int, eligible_count: int, lane_rate: float | None
) -> Signal:
    observed = None if eligible_count == 0 else on_time_count / eligible_count
    value = shrunk_on_time(
        on_time_count=on_time_count,
        eligible_count=eligible_count,
        lane_rate=lane_rate,
    )
    if observed is not None and lane_rate is not None:
        reason = (
            f"{observed:.0%} on-time on this lane ({on_time_count} of "
            f"{eligible_count} delivered loads), shrunk to {value:.0%} "
            f"toward the lane's {lane_rate:.0%}"
        )
    elif lane_rate is not None:
        reason = (
            "No completed delivery on this lane yet, so on-time sits at the "
            f"lane average of {lane_rate:.0%}"
        )
    else:
        reason = (
            "Nothing on this lane has delivered yet, so on-time is scored "
            "neutral for everyone"
        )
    return _signal(SIGNAL_ON_TIME, observed=observed, value=value, reason=reason)


# ---------------------------------------------------------------------------
# One carrier, then all of them
# ---------------------------------------------------------------------------


def _rate_note(stats: CarrierStats | None, lane_phrase: str | None) -> str | None:
    """The ``$/mi`` line. ``None`` when they have no rated load on the lane.

    Read from the same ``carrier_stats`` row as ``load_count``, so the rate and
    the count in the reasons always describe one population. It scores nothing —
    PRD section 8 lists it among the reasons, not among the signals.
    """
    if stats is None or stats.avg_rate_per_mile is None or lane_phrase is None:
        return None
    return f"Averages ${stats.avg_rate_per_mile:.2f}/mi on {lane_phrase}"


def score_carrier(
    carrier: Carrier,
    *,
    load: Load,
    as_of: date,
    key: LaneKey | None,
    stats: CarrierStats | None,
    equipment_loads: int,
    last_delivery: LastDelivery | None,
    lane_on_time_rate: float | None,
) -> CarrierScore:
    """Score one carrier and generate its reasons from that same arithmetic.

    Every observation is read from its source exactly once here and handed to
    the signal that uses it; each signal formats its own sentence from the value
    it just computed. Nothing downstream re-derives a number for display, which
    is what makes a reason unable to disagree with its score (invariant 2).

    ``rank`` is 0 on the way out — :func:`rank_carriers` assigns it after
    sorting, since a rank is a property of the field, not of the carrier. The
    score is not passed in either: it is :attr:`CarrierScore.score_exact`,
    computed from the signals below.
    """
    lane_phrase = None if key is None else _lane_phrase(key)

    lane_loads = 0 if stats is None else stats.load_count
    last_lane_load_date = (
        None
        if stats is None or stats.last_load_at is None
        else central_date(stats.last_load_at)
    )
    days_since = (
        None if last_lane_load_date is None else (as_of - last_lane_load_date).days
    )

    pickup = load.origin.location.place if load.origin is not None else None
    deadhead_miles = (
        None
        if pickup is None or last_delivery is None
        else round(
            road_miles(last_delivery.lat, last_delivery.lon, pickup.lat, pickup.lon), 1
        )
    )

    on_time_count = 0 if stats is None else stats.on_time_count
    eligible_count = 0 if stats is None else stats.on_time_eligible_count

    signals = (
        _experience(
            lane_loads=lane_loads,
            lane_phrase=lane_phrase,
            tier=None if key is None else key.tier,
        ),
        _recency(
            days_since=days_since,
            last_date=last_lane_load_date,
            lane_loads=lane_loads,
        ),
        _equipment(load_equipment=load.equipment, equipment_loads=equipment_loads),
        _deadhead(
            miles=deadhead_miles,
            last_delivery=last_delivery,
            as_of=as_of,
            pickup_known=pickup is not None,
        ),
        _on_time(
            on_time_count=on_time_count,
            eligible_count=eligible_count,
            lane_rate=lane_on_time_rate,
        ),
    )

    return CarrierScore(
        carrier=carrier,
        rank=0,
        signals=signals,
        lane_loads=lane_loads,
        last_lane_load_date=last_lane_load_date,
        days_since_lane_load=days_since,
        equipment_loads=equipment_loads,
        last_delivery=last_delivery,
        deadhead_miles=deadhead_miles,
        on_time_count=on_time_count,
        on_time_eligible_count=eligible_count,
        observed_on_time_rate=(
            None if eligible_count == 0 else on_time_count / eligible_count
        ),
        avg_rate_per_mile=None if stats is None else stats.avg_rate_per_mile,
        rate_note=_rate_note(stats, lane_phrase),
    )


def _lane_phrase(key: LaneKey) -> str:
    """``"750→774, dry van"`` — the lane and pool the ranking is measured on.

    Built from :func:`~app.domain.pricing._lane_label` and the same equipment
    vocabulary the price provenance uses, so one screen never names one bucket
    two ways.
    """
    word = _EQUIPMENT_WORDS.get(key.equipment, key.equipment.lower())
    label = _lane_label(key)
    # Rung 4's label already says "(any equipment)"; repeating the pool there
    # would read "TX_TRIANGLE (any equipment), all equipment types".
    return label if key.tier == TIER_REGION_ANY else f"{label}, {word}"


def rank_carriers(
    load: Load, walk: TierWalk, inputs: RankingInputs
) -> CarrierRanking:
    """Score every carrier for this load, best first. Nobody is dropped.

    The accepted rung of ``walk`` chooses the lane experience is measured on, so
    ranking and pricing always answer from the same evidence. When no rung was
    accepted there is no lane: experience and recency are zero for everyone,
    on-time falls back to neutral, and equipment and truck position decide —
    which ``basis`` states rather than implying a lane that was never found.

    Ordering is by :attr:`CarrierScore.score_exact` — the **unrounded** sum —
    with carrier id as the tie-break, so equal scores are stable across runs
    rather than dependent on dict order. Rounding to the published
    :attr:`~CarrierScore.score` is monotone, so a rendered list can never show a
    higher number above a lower one; two carriers separated in the third decimal
    can render identically and still be ordered, which is correct.

    ``data/TRACEABILITY.md`` computes from signals already rounded to their
    printed precision (DECISIONS.md D18/D19), which can move a score by ~0.1 and
    swap such a pair. Both artifacts are internally consistent and agree on
    every signal value; only the presentation differs, and the shared half-up
    rule (:func:`round_half_up`) is what keeps the printed digit from depending
    on which language construct rounded it.
    """
    accepted = walk.accepted
    key = None if accepted is None else accepted.key
    lane_rate = inputs.lane_on_time_rate

    scored = [
        score_carrier(
            carrier,
            load=load,
            as_of=inputs.as_of,
            key=key,
            stats=inputs.carrier_stats.get(carrier.source_carrier_id),
            equipment_loads=inputs.equipment_loads.get(
                carrier.source_carrier_id, {}
            ).get(str(load.equipment), 0),
            last_delivery=inputs.last_deliveries.get(carrier.source_carrier_id),
            lane_on_time_rate=lane_rate,
        )
        for carrier in inputs.carriers
    ]
    scored.sort(key=lambda s: (-s.score_exact, s.carrier.source_carrier_id))
    ranked = tuple(
        replace(score, rank=position) for position, score in enumerate(scored, start=1)
    )

    return CarrierRanking(
        as_of=inputs.as_of,
        tier=None if key is None else key.tier,
        lane_key=None if key is None else key.lane_key,
        equipment_pool=walk.equipment_filter,
        lane_load_count=walk.load_count,
        lane_on_time_count=inputs.lane_on_time_count,
        lane_on_time_eligible_count=inputs.lane_on_time_eligible_count,
        lane_on_time_rate=lane_rate,
        carriers=ranked,
        basis=_basis(
            key=key,
            load_count=walk.load_count,
            carrier_count=len(ranked),
            lane_rate=lane_rate,
            as_of=inputs.as_of,
        ),
        walk=walk,
    )


def _basis(
    *,
    key: LaneKey | None,
    load_count: int,
    carrier_count: int,
    lane_rate: float | None,
    as_of: date,
) -> str:
    """The one line saying what the whole ranking was measured against.

    Formatted from the values the scoring just used, for the same reason each
    signal formats its own: a header that could name a different lane or a
    different count than the scores beneath it is invariant 2 one level up.
    """
    who = _plural(carrier_count, "carrier")
    if key is None:
        return (
            f"{who} scored as of {as_of} with no lane history — no tier reached "
            f"the {MIN_SAMPLE}-load minimum, so only equipment and truck "
            "position separate them"
        )
    lane_on_time = (
        "" if lane_rate is None else f", {lane_rate:.0%} on-time overall"
    )
    return (
        f"{who} scored as of {as_of} against {_plural(load_count)} on "
        f"{_lane_phrase(key)} ({key.tier} tier{lane_on_time})"
    )
