"""The shared carrier pool: what a banded fact is worth, and how it reads.

DECISIONS.md D17, tasks S2-S4. For one of a broker's own ``ACTIVE`` loads, an
**opted-in** broker gets a second, labeled section of carriers it has never
used, known to other opted-in brokers and matched across them on MC number
(D2). It is a section beside the ranking, not rows merged into it, because the
two are measured from different evidence and interleaving them would imply a
comparability that does not exist.

**Nothing here can quote a rate, because nothing here has one.**
:class:`PoolCarrier` is the only input type, and it has no money attribute —
the field set is D17's "what crosses" table, and it is asserted as an
*equality* below rather than left to review. The pool's half of invariant 2 is
the same as the ranking's: each signal formats its own sentence from the value
it just computed (:func:`_pool_experience` and friends), and
:attr:`PoolCarrierScore.reasons` reads that tuple and nothing else.

**One rule decides every value: score the bound that cannot overstate.**
A band is a range, and the honest reading of a range is its weakest end. So

* ``20-49`` loads scores ``experience_credit(20)`` — the band's floor;
* ``75-89%`` on-time scores ``0.75``;
* "active in the last 30 days" scores ``recency_credit(30)``, the credit at the
  far edge of the window rather than at its near one;
* truck position, which never crosses, scores **zero** and says so.

The consequence is deliberate and worth stating: a pool carrier's score is a
**lower bound** on what the same carrier would score with full data, and it
tops out well below 100 (deadhead's 20 points are unreachable). That is the
opt-in's honest price — you are told less about a carrier you have never used —
and the reasons say which points were unavailable rather than letting a low
score read as a bad carrier.

**Several contributors are not merged into a composite.** When two brokers run
the same carrier, the depth and on-time bands published are **one
contributor's** — the deepest relationship on that lane — not a function of
both. Two reasons, and the second is the load-bearing one:

* the two bands then describe a single real relationship. A depth taken from
  one broker beside a reliability taken from another is a carrier profile that
  nobody has ever had;
* it discloses one contributor's bands instead of every contributor's. Summing
  or extremising across brokers publishes strictly more.

Only the two genuinely existential facts are taken over everybody: whether
*anyone* has run them lately, and how many other brokers do at all. Equal depth
is broken toward the *weaker* on-time band, so even the choice of representative
follows the rule above.

D17 is explicit that no k-anonymity is available at three brokers — publish
anything about a carrier two brokers share and the third subtracts. Bands are
why depth crosses as a range rather than as a number, and
``contributor_count`` is disclosed rather than hidden because a rep deciding
whether to call needs it and because pretending otherwise would be the claim
D17 refuses to make.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import date

from .lanes import TIER_METRO, TIER_REGION, LaneKey, query_keys_for_load
from .model import ANY_EQUIPMENT, Equipment, Load, LoadStatus
from .pricing import MIN_SAMPLE, _EQUIPMENT_WORDS, _lane_label
from .scoring import (
    NEUTRAL,
    SIGNAL_DEADHEAD,
    SIGNAL_EQUIPMENT,
    SIGNAL_EXPERIENCE,
    SIGNAL_ON_TIME,
    SIGNAL_RECENCY,
    Signal,
    _plural,
    _signal,
    experience_credit,
    recency_credit,
    round_half_up,
)

__all__ = [
    "LOAD_BANDS",
    "LOAD_BAND_FLOOR",
    "ON_TIME_BANDS",
    "ON_TIME_BAND_FLOOR",
    "POOL_CARRIER_FIELDS",
    "POOL_LOAD_STATUS",
    "POOL_RECENCY_DAYS",
    "POOL_TIERS",
    "PoolCarrier",
    "PoolCarrierScore",
    "PoolSection",
    "build_pool_section",
    "load_band",
    "on_time_band",
    "pool_eligible",
    "pool_lane_keys",
    "rank_pool_carriers",
    "score_pool_carrier",
]

#: The only status the pool answers for (D17). The pool is not a directory: the
#: query space is bounded by freight the requester actually has on the board, so
#: nobody can walk MC numbers looking for a competitor's roster, and a load whose
#: carrier is already chosen buys a questioner nothing but another look.
POOL_LOAD_STATUS = LoadStatus.ACTIVE

#: Depth bands, weakest first. The ordinal a row carries is the 1-based index
#: into this tuple, so ``schema.sql``'s ``load_band_rank`` and these labels are
#: two spellings of one vocabulary — :func:`load_band` raises if they drift.
LOAD_BANDS: tuple[str, ...] = ("5-9", "10-19", "20-49", "50+")

#: The count each band guarantees. ``5`` is CLAUDE.md's minimum sample, reused
#: as D17's suppression floor: below it a carrier has no row in the view at all.
LOAD_BAND_FLOOR: dict[str, int] = {"5-9": 5, "10-19": 10, "20-49": 20, "50+": 50}
assert set(LOAD_BANDS) == set(LOAD_BAND_FLOOR)
assert LOAD_BAND_FLOOR[LOAD_BANDS[0]] == MIN_SAMPLE

#: On-time bands, **strongest first** — the ordinal orders by reliability, so
#: merging several contributors takes the largest rank (the weakest band).
ON_TIME_BANDS: tuple[str, ...] = ("90+", "75-89", "<75")

#: The rate each band guarantees. ``<75`` guarantees nothing, which is the
#: honest floor: all we were told is that it is under three quarters.
ON_TIME_BAND_FLOOR: dict[str, float] = {"90+": 0.90, "75-89": 0.75, "<75": 0.0}
assert set(ON_TIME_BANDS) == set(ON_TIME_BAND_FLOOR)

#: The recency window that crosses as a boolean. Keep in step with the
#: ``INTERVAL '30 days'`` in ``pool_carrier_lane`` (``schema.sql``).
POOL_RECENCY_DAYS = 30.0

#: The tiers the pool answers at, narrow to wide. ``ZIP3`` is absent by design:
#: a ZIP3 pair is roughly a facility, and naming the facility a competitor's
#: carrier runs into is naming their shipper (D17).
POOL_TIERS: tuple[str, ...] = (TIER_METRO, TIER_REGION)

#: How each band reads in a sentence.
_ON_TIME_WORDS: dict[str, str] = {
    "90+": "90% or better",
    "75-89": "75-89%",
    "<75": "under 75%",
}
assert set(_ON_TIME_WORDS) == set(ON_TIME_BANDS)


def load_band(rank: int, label: str) -> str:
    """The depth band named by a view row, checked against its own ordinal.

    The view emits the label *and* its rank; this is where the two are made to
    agree. A mismatch means ``schema.sql``'s ``CASE`` and :data:`LOAD_BANDS`
    have drifted, which would silently score a carrier at another band's floor
    — so it raises rather than trusting either one.
    """
    if not 1 <= rank <= len(LOAD_BANDS) or LOAD_BANDS[rank - 1] != label:
        raise ValueError(
            f"load band {label!r} does not match rank {rank} "
            f"(expected {LOAD_BANDS}); schema.sql and app.domain.pool disagree"
        )
    return label


def on_time_band(rank: int | None, label: str | None) -> str | None:
    """The on-time band named by a view row, or ``None`` for no verdicts yet.

    ``None`` is a third state and not ``<75``: nothing this carrier ran on the
    lane has delivered, so the pool has no reliability to report.
    """
    if rank is None or label is None:
        if rank is not None or label is not None:
            raise ValueError(f"half an on-time band: rank={rank!r} label={label!r}")
        return None
    if not 1 <= rank <= len(ON_TIME_BANDS) or ON_TIME_BANDS[rank - 1] != label:
        raise ValueError(
            f"on-time band {label!r} does not match rank {rank} "
            f"(expected {ON_TIME_BANDS}); schema.sql and app.domain.pool disagree"
        )
    return label


def pool_lane_keys(load: Load) -> tuple[LaneKey, ...]:
    """The pool lanes to try for this load, narrow to wide.

    Built from :func:`~app.domain.lanes.query_keys_for_load` so the pool reads
    the same buckets the tier walk does — including D6's rule that a load whose
    own equipment is ``UNKNOWN`` reads the mixed ``ANY`` pool rather than
    quietly becoming a dry-van question. Only the ``METRO`` and ``REGION`` rungs
    survive: ``ZIP3`` does not cross, and ``REGION_ANY`` is the region again
    with the filter dropped, which ``REGION`` already reaches for an
    ``UNKNOWN`` load.

    A geo-null pickup or delivery drops the ``METRO`` rung, exactly as it does
    for pricing; the region rung is always formable.
    """
    return tuple(
        key
        for key in query_keys_for_load(load)
        if key is not None and key.tier in POOL_TIERS
    )


# ---------------------------------------------------------------------------
# What crosses the boundary
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PoolCarrier:
    """One carrier as the pool knows them. **The field list is the boundary.**

    Every field here is a row of D17's "what crosses" table, and there is
    deliberately no field for a rate, a customer, a shipment, a stop, a date or
    a truck position. That is not a convention to be reviewed: the pool scorer
    takes this type and only this type, so there is no attribute from which a
    rate reason could be formatted even by a caller trying to.

    ``S5`` should assert ``set(PoolCarrier.__dataclass_fields__)`` **equals**
    the set below — equality, not containment, so adding a field fails the test
    until someone has justified it against D17's table.

    ``contributor_count`` is the number of *other* opted-in brokers that run
    this carrier. It is disclosed rather than hidden: at three brokers it can
    identify the contributor by elimination, which D17 already records as the
    one bit the opt-in genuinely costs.
    """

    #: Federal authority numbers — public in FMCSA licensing records. Identity
    #: is not a relationship.
    mc_number: str
    dot_number: str | None
    #: The carrier's own published contact details. "Call them" needs a number.
    name: str | None
    phone: str | None
    #: Where the carrier is based — their fact, and coarse. Not where a truck is.
    home_city: str | None
    home_state: str | None
    #: The pool lane this row answers on: METRO or REGION, never ZIP3.
    tier: str
    lane_key: str
    equipment: str
    #: Depth, on-time and recency — all three banded (see the module docstring).
    load_band: str
    on_time_band: str | None
    active_recently: bool
    #: Trailer types this carrier is known to operate anywhere in the pool.
    #: A property of their fleet, not of anybody's freight.
    equipment_operated: tuple[str, ...]
    contributor_count: int


#: The exact boundary, restated as data so it can be asserted rather than read.
#: Changing :class:`PoolCarrier` without changing this fails at import.
POOL_CARRIER_FIELDS: frozenset[str] = frozenset(
    {
        "mc_number",
        "dot_number",
        "name",
        "phone",
        "home_city",
        "home_state",
        "tier",
        "lane_key",
        "equipment",
        "load_band",
        "on_time_band",
        "active_recently",
        "equipment_operated",
        "contributor_count",
    }
)
assert set(PoolCarrier.__dataclass_fields__) == POOL_CARRIER_FIELDS, (
    "PoolCarrier's fields are DECISIONS.md D17's boundary; a new one needs a "
    "row in that table first"
)


# ---------------------------------------------------------------------------
# The signals — each a band, scored at the end that cannot overstate it
# ---------------------------------------------------------------------------


def _pool_lane_phrase(carrier: PoolCarrier) -> str:
    """``"DFW->HOU, dry van"`` — the pool lane, named as pricing names lanes."""
    word = _EQUIPMENT_WORDS.get(carrier.equipment, carrier.equipment.lower())
    label = _lane_label(
        LaneKey(
            tier=carrier.tier,
            origin_key=carrier.lane_key.split("->")[0],
            dest_key=carrier.lane_key.split("->")[-1],
            equipment=carrier.equipment,
        )
    )
    if carrier.equipment == ANY_EQUIPMENT:
        return f"{label} (all equipment types)"
    return f"{label}, {word}"


def _brokers(count: int) -> str:
    return "1 other broker" if count == 1 else f"{count} other brokers"


def _pool_experience(*, carrier: PoolCarrier, lane_phrase: str) -> Signal:
    floor = LOAD_BAND_FLOOR[carrier.load_band]
    value = experience_credit(floor)
    who = (
        f"{_brokers(carrier.contributor_count)} in the pool run them; the "
        "deepest has"
        if carrier.contributor_count > 1
        else f"{_brokers(carrier.contributor_count)} in the pool has run them"
    )
    reason = (
        f"{who} {carrier.load_band} loads on {lane_phrase} at the "
        f"{carrier.tier} tier — counts cross as bands, so this is scored at "
        f"the band's floor of {floor}"
    )
    return _signal(
        SIGNAL_EXPERIENCE, observed=float(floor), value=value, reason=reason
    )


def _pool_recency(*, carrier: PoolCarrier) -> Signal:
    if not carrier.active_recently:
        return _signal(
            SIGNAL_RECENCY,
            observed=None,
            value=recency_credit(None),
            reason=(
                f"No pool load in the last {POOL_RECENCY_DAYS:.0f} days — exact "
                "dates do not cross, so there is no recency credit"
            ),
        )
    value = recency_credit(POOL_RECENCY_DAYS)
    reason = (
        f"Active in the pool within the last {POOL_RECENCY_DAYS:.0f} days — "
        "recency crosses as a boolean, so this is scored at the far edge of "
        "that window"
    )
    return _signal(
        SIGNAL_RECENCY, observed=POOL_RECENCY_DAYS, value=value, reason=reason
    )


def _pool_equipment(*, load_equipment: Equipment, carrier: PoolCarrier) -> Signal:
    word = _EQUIPMENT_WORDS.get(str(load_equipment), str(load_equipment).lower())
    if load_equipment is Equipment.UNKNOWN:
        # Invariant 5, exactly as the ranking treats it: the gap is the load's,
        # identical for every carrier, so it neither rewards nor punishes.
        return _signal(
            SIGNAL_EQUIPMENT,
            observed=None,
            value=NEUTRAL,
            reason=(
                "This load's equipment is unknown, so equipment neither helps "
                "nor hurts"
            ),
        )
    operates = str(load_equipment) in carrier.equipment_operated
    value = 1.0 if operates else 0.0
    if operates:
        reason = f"Runs {word} in the pool"
    elif carrier.equipment_operated:
        known = ", ".join(
            _EQUIPMENT_WORDS.get(e, e.lower()) for e in carrier.equipment_operated
        )
        reason = f"No {word} in the pool — known to run {known}"
    else:
        reason = (
            f"No {word} in the pool — no trailer type of theirs clears the "
            f"{MIN_SAMPLE}-load floor, so none crosses"
        )
    return _signal(
        SIGNAL_EQUIPMENT,
        observed=float(len(carrier.equipment_operated)),
        value=value,
        reason=reason,
    )


def _pool_deadhead() -> Signal:
    """Always zero, always the same sentence, and never a measurement.

    ``carriers.last_delivery_*`` is on D17's "never crosses" list — it is our
    own deadhead signal pointed at somebody else's fleet, and it says where a
    competitor's capacity physically is this morning. So the pool cannot
    measure proximity for anybody.

    Zero rather than :data:`~app.domain.scoring.NEUTRAL`, for the same reason
    :func:`~app.domain.scoring.deadhead_credit` gives an unknown truck position
    zero: neutral credit would let "we were told nothing" outrank a carrier we
    know to be 200 miles out. It costs every pool carrier the same 20 points,
    which is part of why a pool score is a lower bound and not a rival to the
    ranking beside it.
    """
    return _signal(
        SIGNAL_DEADHEAD,
        observed=None,
        value=0.0,
        reason=(
            "Truck position does not cross the pool boundary, so proximity "
            "could not be measured — no proximity credit for any pool carrier"
        ),
    )


def _pool_on_time(*, carrier: PoolCarrier) -> Signal:
    band = carrier.on_time_band
    if band is None:
        # Nothing they ran on this lane has delivered, so the pool has no
        # reliability to report. Neutral for the same reason the ranking is
        # neutral when a lane has produced no verdicts: there is no observation
        # and no prior, and inventing either would be worse than saying so.
        return _signal(
            SIGNAL_ON_TIME,
            observed=None,
            value=NEUTRAL,
            reason=(
                "Nothing this carrier ran on this lane in the pool has "
                "delivered yet, so on-time is scored neutral"
            ),
        )
    value = ON_TIME_BAND_FLOOR[band]
    whose = (
        " for that same broker" if carrier.contributor_count > 1 else ""
    )
    reason = (
        f"On-time {_ON_TIME_WORDS[band]} on this lane in the pool{whose} — the "
        f"raw count does not cross, so this is scored at the band's floor of "
        f"{value:.0%}"
    )
    return _signal(SIGNAL_ON_TIME, observed=value, value=value, reason=reason)


# ---------------------------------------------------------------------------
# One pool carrier, then the section
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PoolCarrierScore:
    """One pool carrier's answer: the score, the signals, and the bands.

    Mirrors :class:`~app.domain.scoring.CarrierScore` deliberately — same
    weights, same 0-100 scale, same "the score is not stored, it is the sum of
    the signals" property. What differs is only what the signals were computed
    from, which the reasons state on every line.
    """

    carrier: PoolCarrier
    rank: int
    signals: tuple[Signal, ...]

    @property
    def score_exact(self) -> float:
        return sum(signal.contribution for signal in self.signals)

    @property
    def score(self) -> float:
        return round_half_up(self.score_exact)

    @property
    def reasons(self) -> tuple[str, ...]:
        """The explanation, in the ranking's signal order. No rate note — a
        pool carrier has no ``$/mi`` and could not be given one."""
        return tuple(signal.reason for signal in self.signals)


@dataclass(frozen=True, slots=True)
class PoolSection:
    """The labeled second section, and why it holds what it holds.

    ``opted_in`` and ``eligible`` are separate because they fail for different
    reasons and a rep needs to know which: a broker that has not joined sees
    nothing at all, while a joined broker asking about a ``COMPLETED`` load is
    being refused by the precondition that keeps the pool from being a
    directory.
    """

    load_id: str
    as_of: date
    opted_in: bool
    eligible: bool
    tier: str | None
    lane_key: str | None
    equipment_pool: str | None
    carriers: tuple[PoolCarrierScore, ...]
    basis: str


def score_pool_carrier(carrier: PoolCarrier, *, load: Load) -> PoolCarrierScore:
    """Score one pool carrier from its bands, generating each reason beside the
    value that produced it.

    ``rank`` is 0 on the way out; :func:`rank_pool_carriers` assigns it after
    sorting, exactly as the ranking does.
    """
    lane_phrase = _pool_lane_phrase(carrier)
    signals = (
        _pool_experience(carrier=carrier, lane_phrase=lane_phrase),
        _pool_recency(carrier=carrier),
        _pool_equipment(load_equipment=load.equipment, carrier=carrier),
        _pool_deadhead(),
        _pool_on_time(carrier=carrier),
    )
    return PoolCarrierScore(carrier=carrier, rank=0, signals=signals)


def rank_pool_carriers(
    load: Load,
    carriers: Sequence[PoolCarrier],
    *,
    as_of: date,
    key: LaneKey | None,
    opted_in: bool,
    eligible: bool,
    reason: str | None = None,
) -> PoolSection:
    """Score every pool carrier for this load, best first. Nobody is dropped.

    Ordering is by the unrounded sum with MC number as the tie-break, so equal
    scores are stable across runs — the same rule
    :func:`~app.domain.scoring.rank_carriers` uses, with the stable identity
    the pool actually has.

    ``reason`` overrides the basis sentence for the cases where there is
    nothing to rank and *why* is the whole answer: not opted in, not an
    ``ACTIVE`` load, no lane.
    """
    scored = [score_pool_carrier(carrier, load=load) for carrier in carriers]
    scored.sort(key=lambda s: (-s.score_exact, s.carrier.mc_number))
    ranked = tuple(
        replace(score, rank=position) for position, score in enumerate(scored, start=1)
    )
    return PoolSection(
        load_id=load.source_load_id,
        as_of=as_of,
        opted_in=opted_in,
        eligible=eligible,
        tier=None if key is None else key.tier,
        lane_key=None if key is None else key.lane_key,
        equipment_pool=None if key is None else key.equipment,
        carriers=ranked,
        basis=reason if reason is not None else _pool_basis(ranked, key, as_of),
    )


def pool_eligible(load: Load, *, opted_in: bool) -> bool:
    """Will the pool answer for this load at all?

    Two preconditions, both D17's, and both are refusals rather than empty
    answers: the broker has to be in the pool, and the load has to be one of
    its own with :data:`POOL_LOAD_STATUS`. The route asks this before writing
    the audit row, and :func:`build_pool_section` asks it again to decide what
    to say — one function, so the row that records a pool read and the answer
    that constitutes one cannot come apart.
    """
    return opted_in and load.status is POOL_LOAD_STATUS


def build_pool_section(
    load: Load,
    *,
    as_of: date,
    opted_in: bool,
    carriers_for: Callable[[LaneKey], Sequence[PoolCarrier]],
) -> PoolSection:
    """The whole pool answer for one load, refusals included.

    ``carriers_for`` is the repository's one cross-broker read, passed as a
    callable for the same reason :func:`~app.domain.pricing.walk_tiers` takes
    ``lane_stats``: this stays a function of a load and a lookup, with no idea
    a database exists, and a unit test hands it a dict.

    The rungs are :func:`pool_lane_keys` — ``METRO`` then ``REGION`` — and the
    first one that returns anybody wins. There is no minimum-sample test here
    because the projection already applied it: a carrier below five loads has
    no row in the view to return.
    """
    if not opted_in:
        return rank_pool_carriers(
            load,
            (),
            as_of=as_of,
            key=None,
            opted_in=False,
            eligible=False,
            reason=(
                "This broker is not in the shared carrier pool, so no other "
                "broker's carriers are shown and none of its own data is "
                "shared. Opting in is per broker and off by default"
            ),
        )
    if not pool_eligible(load, opted_in=opted_in):
        return rank_pool_carriers(
            load,
            (),
            as_of=as_of,
            key=None,
            opted_in=True,
            eligible=False,
            reason=(
                f"The pool answers only for a load that is still "
                f"{POOL_LOAD_STATUS} — this one is {load.status}. That "
                "precondition is what keeps the pool from being a directory of "
                "other brokers' carriers"
            ),
        )

    key: LaneKey | None = None
    carriers: Sequence[PoolCarrier] = ()
    for candidate in pool_lane_keys(load):
        found = carriers_for(candidate)
        if found:
            key, carriers = candidate, found
            break
    return rank_pool_carriers(
        load, carriers, as_of=as_of, key=key, opted_in=True, eligible=True
    )


def _pool_basis(
    carriers: Sequence[PoolCarrierScore], key: LaneKey | None, as_of: date
) -> str:
    """The one line saying what the whole section was measured against.

    Formatted from the values just scored, for the same reason
    :func:`~app.domain.scoring._basis` is: a header that could name a different
    lane or a different count than the cards beneath it is invariant 2 one
    level up.
    """
    if key is None:
        return (
            "No pool lane for this load: no other opted-in broker has "
            f"{MIN_SAMPLE} or more loads on any lane it could be matched on"
        )
    if not carriers:
        return (
            f"No pool carriers on {_lane_label(key)} at the {key.tier} tier as "
            f"of {as_of} — every carrier the pool knows there is one you "
            "already use"
        )
    contributors = max(c.carrier.contributor_count for c in carriers)
    lane = _pool_lane_phrase(carriers[0].carrier)
    return (
        f"{_plural(len(carriers), 'pool carrier')} you have never used, from up "
        f"to {_brokers(contributors)}, scored as of {as_of} on {lane} at the "
        f"{key.tier} tier. Every figure is a band and no rate, customer or "
        "shipment crosses, so these scores are lower bounds and are not "
        "comparable with the ranking above"
    )
