"""Unit tests for ``app.domain.scoring`` -- carrier ranking (PRD section 8).

CLAUDE.md invariant 2 is what this file exists to protect: *"Reasons come from
the score... A reason that can disagree with its score is the worst possible
bug here."* ``CarrierScore.score`` is a derived property (the rounded sum of
``Signal.contribution`` values), so the invariant is structural -- but it is
tested explicitly here so that anyone who later reintroduces a stored score
field breaks a named assertion instead of silently reopening the bug.

DECISIONS.md **D5** (experience saturates at ``n/(n+5)``; on-time shrinks a
*rate* toward the lane average at the same ``k``), **D19** (round half-up to
one decimal, once, at the end) and **D20** (a gap in the *load* scores
neutral; a gap in *one carrier's* record scores zero) are the three rules
this file pins down numerically.

``RankingInputs``/``TierWalk`` are built by hand throughout, so every test is
a dict/dataclass literal with no database and no fixture files. Every
assertion carries its arithmetic in a comment.

No database, no network, no file I/O.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.domain.geo import resolve_place
from app.domain.lanes import TIER_METRO, LaneKey
from app.domain.model import (
    Carrier,
    CarrierStats,
    Equipment,
    LastDelivery,
    Load,
    LoadStatus,
    Stop,
    StopLocation,
)
from app.domain.pricing import MIN_SAMPLE, TierAttempt, TierWalk
from app.domain.scoring import (
    DEADHEAD_FULL_CREDIT_MILES,
    DEADHEAD_NO_CREDIT_MILES,
    NEUTRAL,
    RECENCY_DECAY_DAYS,
    SCORE_DECIMALS,
    SHRINK_K,
    SIGNAL_DEADHEAD,
    SIGNAL_EQUIPMENT,
    SIGNAL_EXPERIENCE,
    SIGNAL_ON_TIME,
    SIGNAL_RECENCY,
    SIGNALS,
    WEIGHTS,
    CarrierRanking,
    RankingInputs,
    Signal,
    deadhead_credit,
    experience_credit,
    rank_carriers,
    recency_credit,
    round_half_up,
    score_carrier,
    shrunk_on_time,
)

UTC = timezone.utc

# Day 11 (CLAUDE.md "Dates"): the day the question is asked for every test
# below that needs an as-of date.
DAY11 = date(2026, 7, 16)

# DFW -> HOU, dry van, at the METRO tier -- the lane every ranking test in
# this file is measured against unless a test says otherwise.
KEY = LaneKey(TIER_METRO, "DFW", "HOU", "DRY_VAN")


# ---------------------------------------------------------------------------
# Helpers -- inline stub Loads, Carriers, CarrierStats, LastDeliveries and
# TierWalks. No fixture files, no database.
# ---------------------------------------------------------------------------


def _stop(seq: int, pickup: bool, drop: bool, city: str, state: str, zip_code: str) -> Stop:
    place = resolve_place(city, state, zip_code)
    assert place is not None, f"{city}, {state} {zip_code} must resolve for this test"
    loc = StopLocation(city=city, state=state, zip=zip_code, place=place)
    return Stop(sequence=seq, is_pickup=pickup, is_drop=drop, location=loc, scheduled_date=None)


def _geo_null_stop(seq: int, pickup: bool, drop: bool) -> Stop:
    loc = StopLocation(city=None, state=None, zip=None, place=None)
    return Stop(sequence=seq, is_pickup=pickup, is_drop=drop, location=loc, scheduled_date=None)


# Dallas 75201 (metro DFW) -> Houston 77002 (metro HOU) -- the same pair
# test_pricing.py uses, so a reviewer cross-checking geography sees one lane.
DALLAS_HOUSTON_STOPS = (
    _stop(1, True, False, "Dallas", "TX", "75201"),
    _stop(2, False, True, "Houston", "TX", "77002"),
)


def _load(
    *,
    equipment: Equipment = Equipment.DRY_VAN,
    distance_miles: float | None = 200.0,
    stops: tuple[Stop, ...] = DALLAS_HOUSTON_STOPS,
) -> Load:
    """The minimum Load a scoring test needs. Scoring never reads
    ``distance_miles`` or ``carrier_rate`` at all (see
    ``TestScoreIgnoresPriceAndDistance``), so both are deliberately unused by
    most tests below."""
    return Load(
        source_load_id="TEST-1",
        load_number="TEST-1",
        status=LoadStatus.ACTIVE,
        equipment=equipment,
        stops=stops,
        weight_lbs=None,
        distance_miles=distance_miles,
        customer_rate=None,
        carrier_rate=None,
        source_carrier_id=None,
        source_customer_id=None,
        created_at=None,
        last_modified_at=None,
    )


def _carrier(carrier_id: str) -> Carrier:
    """A carrier with no field scoring reads directly -- the repository-built
    facts scoring actually uses (lane stats, equipment history, last
    delivery) all arrive separately via ``RankingInputs``."""
    return Carrier(
        source_carrier_id=carrier_id,
        name=None,
        mc_number=None,
        dot_number=None,
        phone=None,
        home_city=None,
        home_state=None,
    )


def _stats(
    carrier_id: str,
    key: LaneKey,
    load_count: int,
    *,
    on_time_count: int = 0,
    on_time_eligible_count: int = 0,
    avg_rate_per_mile: float | None = None,
    last_load_at: datetime | None = None,
    first_load_at: datetime | None = None,
) -> CarrierStats:
    return CarrierStats(
        source_carrier_id=carrier_id,
        tier=key.tier,
        lane_key=key.lane_key,
        equipment=key.equipment,
        load_count=load_count,
        on_time_count=on_time_count,
        on_time_eligible_count=on_time_eligible_count,
        avg_rate_per_mile=avg_rate_per_mile,
        first_load_at=first_load_at,
        last_load_at=last_load_at,
    )


def _last_delivery(
    carrier_id: str, load_id: str, city: str, state: str, zip_code: str, *, at: datetime | None = None
) -> LastDelivery:
    place = resolve_place(city, state, zip_code)
    assert place is not None, f"{city}, {state} {zip_code} must resolve for this test"
    loc = StopLocation(city=city, state=state, zip=zip_code, place=place)
    return LastDelivery(
        source_carrier_id=carrier_id, source_load_id=load_id, lat=place.lat, lon=place.lon, at=at, location=loc
    )


def _walk_accepted(key: LaneKey, load_count: int) -> TierWalk:
    """A one-rung, already-accepted ``TierWalk`` -- isolates scoring from the
    tier-walk machinery ``test_pricing.py`` already covers."""
    attempt = TierAttempt(tier=key.tier, key=key, load_count=load_count, accepted=True)
    return TierWalk(rungs=(attempt,), accepted=attempt, equipment=key.equipment)


def _walk_no_rung(equipment: str = "DRY_VAN") -> TierWalk:
    """A walk where no rung cleared :data:`MIN_SAMPLE` -- ``accepted`` is
    ``None``, so ``rank_carriers`` has no lane to measure experience on."""
    return TierWalk(rungs=(), accepted=None, equipment=equipment)


def _inputs(
    *,
    as_of: date,
    carriers: tuple[Carrier, ...],
    carrier_stats: dict[str, CarrierStats] | None = None,
    equipment_loads: dict[str, dict[str, int]] | None = None,
    last_deliveries: dict[str, LastDelivery] | None = None,
    lane_on_time_count: int = 0,
    lane_on_time_eligible_count: int = 0,
) -> RankingInputs:
    return RankingInputs(
        as_of=as_of,
        carriers=carriers,
        carrier_stats=carrier_stats or {},
        equipment_loads=equipment_loads or {},
        last_deliveries=last_deliveries or {},
        lane_on_time_count=lane_on_time_count,
        lane_on_time_eligible_count=lane_on_time_eligible_count,
    )


def _good_weak_setup() -> tuple[Load, TierWalk, RankingInputs]:
    """One accepted METRO/DRY_VAN lane, two carriers: a proven one
    (``CARRIER-GOOD``, 14 lane loads, 13-of-14 on-time, nearby) and one with
    zero history of any kind (``CARRIER-WEAK``). Reused by every test in
    ``TestGoodAndWeakCarrierRanking`` and the invariant-2 tripwire below so
    the hand-computed numbers only have to be derived once.
    """
    load = _load(equipment=Equipment.DRY_VAN, distance_miles=271.0, stops=DALLAS_HOUSTON_STOPS)
    walk = _walk_accepted(KEY, load_count=20)
    good_stats = _stats(
        "CARRIER-GOOD",
        KEY,
        14,
        on_time_count=13,
        on_time_eligible_count=14,
        avg_rate_per_mile=1.12,
        last_load_at=datetime(2026, 7, 10, 12, 0, tzinfo=UTC),  # 6 days before DAY11
    )
    good_delivery = _last_delivery("CARRIER-GOOD", "LOAD-PRIOR", "Fort Worth", "TX", "76102")
    inputs = _inputs(
        as_of=DAY11,
        carriers=(_carrier("CARRIER-GOOD"), _carrier("CARRIER-WEAK")),
        carrier_stats={"CARRIER-GOOD": good_stats},
        equipment_loads={"CARRIER-GOOD": {"DRY_VAN": 20}},
        last_deliveries={"CARRIER-GOOD": good_delivery},
        lane_on_time_count=17,
        lane_on_time_eligible_count=20,  # 17/20 = 0.85 lane average
    )
    return load, walk, inputs


# ---------------------------------------------------------------------------
# Constants -- PRD section 8's numbers, verbatim.
# ---------------------------------------------------------------------------


class TestConstants:
    def test_shrink_k_is_5(self) -> None:
        # CLAUDE.md "Known traps" / DECISIONS.md D5: cold-start constant for
        # both formulas.
        assert SHRINK_K == 5

    def test_weights_match_prd_section_8_and_sum_to_1(self) -> None:
        assert WEIGHTS[SIGNAL_EXPERIENCE] == 0.35
        assert WEIGHTS[SIGNAL_RECENCY] == 0.20
        assert WEIGHTS[SIGNAL_EQUIPMENT] == 0.15
        assert WEIGHTS[SIGNAL_DEADHEAD] == 0.20
        assert WEIGHTS[SIGNAL_ON_TIME] == 0.10
        # 0.35 + 0.20 + 0.15 + 0.20 + 0.10 = 1.00
        assert sum(WEIGHTS.values()) == pytest.approx(1.0)

    def test_signal_order_matches_prd_table_order(self) -> None:
        assert SIGNALS == (
            SIGNAL_EXPERIENCE,
            SIGNAL_RECENCY,
            SIGNAL_EQUIPMENT,
            SIGNAL_DEADHEAD,
            SIGNAL_ON_TIME,
        )

    def test_neutral_is_one_half(self) -> None:
        assert NEUTRAL == 0.5

    def test_score_is_published_to_one_decimal(self) -> None:
        # DECISIONS.md D19.
        assert SCORE_DECIMALS == 1

    def test_deadhead_thresholds_are_50_and_250(self) -> None:
        # PRD section 8: full credit <=50 mi, zero >=250 mi.
        assert DEADHEAD_FULL_CREDIT_MILES == 50.0
        assert DEADHEAD_NO_CREDIT_MILES == 250.0

    def test_recency_decay_is_30_days(self) -> None:
        assert RECENCY_DECAY_DAYS == 30.0


# ---------------------------------------------------------------------------
# D5 -- experience is a saturating count, zero at zero.
# ---------------------------------------------------------------------------


class TestExperienceCreditD5:
    def test_zero_at_zero_loads(self) -> None:
        # 0 / (0 + 5) = 0.0 -- the property that stops absence of evidence
        # from being rewarded (that is the whole point of D5).
        assert experience_credit(0) == 0.0

    def test_saturating_values_at_1_5_20_200(self) -> None:
        assert experience_credit(1) == pytest.approx(1 / 6)  # 1/(1+5) = 0.16667
        assert experience_credit(5) == 0.5  # 5/(5+5) = 0.5
        assert experience_credit(20) == 0.8  # 20/(20+5) = 0.8
        assert experience_credit(200) == pytest.approx(200 / 205)  # 0.97561

    def test_monotonically_increasing_with_more_loads(self) -> None:
        counts = [0, 1, 5, 20, 50, 200]
        values = [experience_credit(n) for n in counts]
        assert values == sorted(values)
        assert values[0] == 0.0
        assert values[-1] < 1.0  # saturates, never reaches 1.0


# ---------------------------------------------------------------------------
# D5 -- on-time shrinks a *rate*, a different formula from experience's count.
# ---------------------------------------------------------------------------


class TestShrunkOnTimeD5:
    def test_no_verdicts_at_all_is_neutral(self) -> None:
        # Neither an observation nor a prior exists.
        assert shrunk_on_time(on_time_count=0, eligible_count=0, lane_rate=None) == NEUTRAL

    def test_zero_eligible_with_a_lane_rate_present_equals_the_lane_rate_exactly(self) -> None:
        # (0 + 0.85*5) / (0+5) = 4.25/5 = 0.85 -- with no evidence of its own,
        # a carrier's on-time signal sits exactly at the prior, not at 0 and
        # not at NEUTRAL.
        assert shrunk_on_time(on_time_count=0, eligible_count=0, lane_rate=0.85) == pytest.approx(0.85)

    def test_2_for_2_does_not_outrank_164_for_200_at_a_realistic_lane_rate(self) -> None:
        """CLAUDE.md "Known traps", verbatim, applied to the on-time signal
        (DECISIONS.md D5 calls this "an on-time framing").

        Whether the tiny, perfect sample (2-for-2) outranks the huge, strong
        sample (164-for-200) depends on the lane prior it shrinks toward: at
        n=200 the shrunk value sits almost exactly on the observed 82% no
        matter what the prior is (5 pseudo-observations out of 205 barely
        move it), while at n=2 the prior dominates (5 pseudo-observations out
        of 7). ``lane_rate=0.70`` is a realistic, moderate lane average --
        below both carriers' raw rates -- under which the large sample's
        proven 82% correctly beats the small sample's unproven 100%.
        """
        lane_rate = 0.70
        small = shrunk_on_time(on_time_count=2, eligible_count=2, lane_rate=lane_rate)
        large = shrunk_on_time(on_time_count=164, eligible_count=200, lane_rate=lane_rate)
        # small = (2 + 0.70*5) / (2+5)   = (2 + 3.5) / 7   = 5.5/7   = 0.785714
        # large = (164 + 0.70*5) / (200+5) = (164 + 3.5) / 205 = 167.5/205 = 0.817073
        assert small == pytest.approx(5.5 / 7)
        assert large == pytest.approx(167.5 / 205)
        assert small < large  # the trap: 2-for-2 does not beat 164-for-200

    def test_shrinkage_barely_moves_a_large_sample_but_dominates_a_tiny_one(self) -> None:
        # Changing the prior from 0.0 to 1.0 swings the n=2 sample by nearly
        # 5/7 of the range, but the n=200 sample by only ~5/205.
        small_low_prior = shrunk_on_time(on_time_count=2, eligible_count=2, lane_rate=0.0)
        small_high_prior = shrunk_on_time(on_time_count=2, eligible_count=2, lane_rate=1.0)
        large_low_prior = shrunk_on_time(on_time_count=164, eligible_count=200, lane_rate=0.0)
        large_high_prior = shrunk_on_time(on_time_count=164, eligible_count=200, lane_rate=1.0)
        assert (small_high_prior - small_low_prior) == pytest.approx(5 / 7)
        assert (large_high_prior - large_low_prior) == pytest.approx(5 / 205)
        assert (small_high_prior - small_low_prior) > (large_high_prior - large_low_prior)


# ---------------------------------------------------------------------------
# D19 -- round half-up to one decimal, never Python's half-to-even builtin.
# ---------------------------------------------------------------------------


class TestRoundHalfUpD19:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (22.05, 22.1),
            (69.55, 69.6),
            (58.65, 58.7),
            (29.85, 29.9),
        ],
    )
    def test_half_up_at_the_x5_boundary(self, value: float, expected: float) -> None:
        assert round_half_up(value) == expected

    def test_the_builtin_disagrees_on_some_of_the_same_inputs(self) -> None:
        """The reason ``round_half_up`` exists at all: Python's ``round()`` is
        half-to-**even** on the *binary* value of the float, so its direction
        varies by number rather than following one rule. Two of the four D19
        boundary cases above (69.55, 58.65) are where the builtin actually
        disagrees with our half-up rule; the other two happen to agree
        because those particular binary values land slightly on the far side
        of the decimal boundary. Either way, ``round_half_up`` gives the
        contractual answer regardless of which side of the boundary the float
        happens to sit on.
        """
        assert round(69.55, 1) != round_half_up(69.55)  # builtin: 69.5, ours: 69.6
        assert round(58.65, 1) != round_half_up(58.65)  # builtin: 58.6, ours: 58.7
        assert round_half_up(69.55) == 69.6
        assert round_half_up(58.65) == 58.7

    def test_values_not_on_a_boundary_are_unaffected(self) -> None:
        assert round_half_up(78.24) == 78.2
        assert round_half_up(78.26) == 78.3
        assert round_half_up(0.0) == 0.0
        assert round_half_up(100.0) == 100.0


# ---------------------------------------------------------------------------
# D20 -- the deadhead curve itself (pure function, no carrier or load).
# ---------------------------------------------------------------------------


class TestDeadheadCreditCurveD20:
    def test_full_credit_at_and_under_50_miles(self) -> None:
        assert deadhead_credit(0.0) == 1.0
        assert deadhead_credit(50.0) == 1.0  # (250-50)/200 = 1.0 exactly at the edge
        assert deadhead_credit(10.0) == 1.0  # would be 1.2 uncapped -- clamped to 1.0

    def test_zero_credit_at_and_over_250_miles(self) -> None:
        assert deadhead_credit(250.0) == 0.0
        assert deadhead_credit(300.0) == 0.0  # would be -0.25 uncapped -- clamped to 0.0

    def test_something_strictly_between_at_150_miles(self) -> None:
        # (250 - 150) / 200 = 100/200 = 0.5
        assert deadhead_credit(150.0) == 0.5

    def test_curve_is_monotonically_non_increasing(self) -> None:
        miles = [0, 25, 50, 75, 100, 125, 150, 175, 200, 225, 250, 300]
        values = [deadhead_credit(m) for m in miles]
        assert values == sorted(values, reverse=True)
        assert values[0] == 1.0
        assert values[-1] == 0.0

    def test_none_the_pure_carrier_side_gap_scores_zero(self) -> None:
        # deadhead_credit's own contract for a missing carrier position
        # (DECISIONS.md D20) -- distinct from the load-side NEUTRAL case,
        # which _deadhead (tested below via score_carrier) handles, not this
        # function.
        assert deadhead_credit(None) == 0.0


# ---------------------------------------------------------------------------
# D20 -- the deadhead *signal* in context: which of three things a gap means,
# and that the reason names the gap rather than implying a distance.
# ---------------------------------------------------------------------------


class TestDeadheadSignalInContextD20:
    STATS = _stats("C1", KEY, 10)

    def _score(self, *, last_delivery: LastDelivery | None, stops: tuple[Stop, ...]):
        return score_carrier(
            _carrier("C1"),
            load=_load(stops=stops),
            as_of=DAY11,
            key=KEY,
            stats=self.STATS,
            equipment_loads=0,
            last_delivery=last_delivery,
            lane_on_time_rate=None,
        )

    def test_measured_full_credit_names_the_town_and_the_miles(self) -> None:
        delivery = _last_delivery("C1", "L1", "Fort Worth", "TX", "76102")
        score = self._score(last_delivery=delivery, stops=DALLAS_HOUSTON_STOPS)
        deadhead = score.signal(SIGNAL_DEADHEAD)
        # round(road_miles(Fort Worth 76102, Dallas 75201), 1) = 36.6
        # (test_distance.py's own Dallas<->Fort Worth pair: 30.5 haversine x 1.2 = 36.6)
        assert deadhead.observed == 36.6
        assert deadhead.value == 1.0  # (250-36.6)/200 = 1.067, clamped to 1.0
        assert deadhead.reason == "Delivered in Fort Worth, TX 76102, 36.6 mi from your pickup"

    def test_measured_strictly_between_reports_the_real_partial_credit(self) -> None:
        delivery = _last_delivery("C1", "L1", "Austin", "TX", "78701")
        score = self._score(last_delivery=delivery, stops=DALLAS_HOUSTON_STOPS)
        deadhead = score.signal(SIGNAL_DEADHEAD)
        # round(road_miles(Austin 78701, Dallas 75201), 1) = 218.5
        assert deadhead.observed == 218.5
        assert deadhead.value == 0.1575  # (250-218.5)/200 = 31.5/200
        assert deadhead.reason == "Delivered in Austin, TX 78701, 218.5 mi from your pickup"

    def test_measured_zero_credit_names_the_cutoff_not_just_a_zero(self) -> None:
        delivery = _last_delivery("C1", "L1", "San Antonio", "TX", "78205")
        score = self._score(last_delivery=delivery, stops=DALLAS_HOUSTON_STOPS)
        deadhead = score.signal(SIGNAL_DEADHEAD)
        # round(road_miles(San Antonio 78205, Dallas 75201), 1) = 302.9
        assert deadhead.observed == 302.9
        assert deadhead.value == 0.0
        assert deadhead.reason == (
            "Delivered in San Antonio, TX 78205, 302.9 mi from your pickup "
            "— past the 250 mi cutoff, so no proximity credit"
        )

    def test_carrier_side_gap_scores_zero_and_names_the_carriers_own_gap(self) -> None:
        # DECISIONS.md D20: a gap in *this carrier's* record, not the load's.
        score = self._score(last_delivery=None, stops=DALLAS_HOUSTON_STOPS)
        deadhead = score.signal(SIGNAL_DEADHEAD)
        assert deadhead.value == 0.0
        assert deadhead.observed is None
        assert deadhead.reason == "No known recent delivery for this carrier, so no proximity credit"
        # The reason must not imply a distance for a gap that has none.
        assert "mi from" not in deadhead.reason
        assert not any(ch.isdigit() for ch in deadhead.reason)

    def test_load_side_gap_scores_neutral_and_names_the_loads_own_gap(self) -> None:
        # DECISIONS.md D20: a gap in the *load*'s pickup, identical for every
        # carrier -- NEUTRAL, not the carrier-side 0.0, and the reason says so.
        geo_null_stops = (_geo_null_stop(1, True, False), _geo_null_stop(2, False, True))
        delivery = _last_delivery("C1", "L1", "Fort Worth", "TX", "76102")
        score = self._score(last_delivery=delivery, stops=geo_null_stops)
        deadhead = score.signal(SIGNAL_DEADHEAD)
        assert deadhead.value == NEUTRAL == 0.5
        assert deadhead.observed is None
        assert deadhead.reason == (
            "This load's pickup is not on the map, so proximity could not be "
            "measured for anyone — scored neutral"
        )
        assert "mi from" not in deadhead.reason


# ---------------------------------------------------------------------------
# Invariant 5 -- equipment UNKNOWN neither rewards nor punishes.
# ---------------------------------------------------------------------------


class TestEquipmentSignalInvariant5:
    def _score(self, *, equipment: Equipment, equipment_loads: int):
        return score_carrier(
            _carrier("C1"),
            load=_load(equipment=equipment, stops=DALLAS_HOUSTON_STOPS),
            as_of=DAY11,
            key=KEY,
            stats=None,
            equipment_loads=equipment_loads,
            last_delivery=None,
            lane_on_time_rate=None,
        )

    def test_unknown_equipment_is_neutral_regardless_of_history(self) -> None:
        no_history = self._score(equipment=Equipment.UNKNOWN, equipment_loads=0)
        lots_of_history = self._score(equipment=Equipment.UNKNOWN, equipment_loads=100)
        assert no_history.signal(SIGNAL_EQUIPMENT).value == NEUTRAL == 0.5
        assert lots_of_history.signal(SIGNAL_EQUIPMENT).value == NEUTRAL == 0.5
        # A pile of *other*-equipment loads must not leak in as a reward.
        assert no_history.signal(SIGNAL_EQUIPMENT).value == lots_of_history.signal(SIGNAL_EQUIPMENT).value
        assert no_history.signal(SIGNAL_EQUIPMENT).observed is None
        reason = no_history.signal(SIGNAL_EQUIPMENT).reason
        assert reason == "This load's equipment is unknown, so equipment neither helps nor hurts"

    def test_known_equipment_rewards_history_and_the_absence_of_it_costs(self) -> None:
        hauled = self._score(equipment=Equipment.DRY_VAN, equipment_loads=4)
        never = self._score(equipment=Equipment.DRY_VAN, equipment_loads=0)
        assert hauled.signal(SIGNAL_EQUIPMENT).value == 1.0
        assert never.signal(SIGNAL_EQUIPMENT).value == 0.0
        assert hauled.signal(SIGNAL_EQUIPMENT).reason == "Has hauled dry van for you (4 loads)"
        assert never.signal(SIGNAL_EQUIPMENT).reason == "Has never hauled dry van for you"


# ---------------------------------------------------------------------------
# A fully hand-controlled signal vector -- the weighted-sum arithmetic, and
# nothing else, so a reviewer can multiply it out without touching pytest.
# ---------------------------------------------------------------------------


class TestWeightedTotalKnownVector:
    def test_hand_computed_signal_vector_sums_to_the_expected_score(self) -> None:
        # experience = 15/(15+5)          = 0.75
        # recency    = exp(-0/30)         = 1.0     (delivered "today")
        # equipment  = 1.0                           (has hauled dry van before)
        # deadhead   = NEUTRAL = 0.5                 (this load's pickup is
        #                                             geo-null, so nobody can
        #                                             be measured -- D20)
        # on_time    = (8 + 0.5*5)/(10+5) = 10.5/15 = 0.7
        stats = _stats(
            "C1",
            KEY,
            15,
            on_time_count=8,
            on_time_eligible_count=10,
            last_load_at=datetime(2026, 7, 16, 12, 0, tzinfo=UTC),  # same day as as_of
        )
        geo_null_load = _load(stops=(_geo_null_stop(1, True, False), _geo_null_stop(2, False, True)))
        score = score_carrier(
            _carrier("C1"),
            load=geo_null_load,
            as_of=date(2026, 7, 16),
            key=KEY,
            stats=stats,
            equipment_loads=3,
            last_delivery=None,
            lane_on_time_rate=0.5,
        )
        assert score.signal(SIGNAL_EXPERIENCE).value == 0.75
        assert score.signal(SIGNAL_RECENCY).value == 1.0
        assert score.signal(SIGNAL_EQUIPMENT).value == 1.0
        assert score.signal(SIGNAL_DEADHEAD).value == NEUTRAL == 0.5
        assert score.signal(SIGNAL_ON_TIME).value == 0.7

        # 100 * (0.35*0.75 + 0.20*1.0 + 0.15*1.0 + 0.20*0.5 + 0.10*0.7)
        #   = 26.25 + 20.0 + 15.0 + 10.0 + 7.0
        #   = 78.25 -> half-up (D19) -> 78.3
        assert score.score_exact == 78.25
        assert score.score == 78.3
        # Confirmed independently against the running interpreter using the
        # exact same formulas, before this literal was written into the test.


# ---------------------------------------------------------------------------
# Invariant 2 -- the tripwire. A rounded score can only ever be the rounded
# sum of the signals that explain it.
# ---------------------------------------------------------------------------


class TestInvariant2Tripwire:
    def test_every_carriers_score_equals_the_rounded_sum_of_its_own_signals(self) -> None:
        load, walk, inputs = _good_weak_setup()
        ranking = rank_carriers(load, walk, inputs)
        assert len(ranking.carriers) == 2
        for carrier_score in ranking.carriers:
            hand_summed = sum(signal.contribution for signal in carrier_score.signals)
            # This is the tripwire itself: were CarrierScore.score ever
            # changed back into an independently-set field, this equality
            # would be the thing that breaks, by name, instead of the bug
            # reopening silently.
            assert carrier_score.score == round_half_up(hand_summed, 1)
            assert carrier_score.score_exact == hand_summed


# ---------------------------------------------------------------------------
# Reasons cannot contradict the score -- every quoted number traces to the
# signal that produced it.
# ---------------------------------------------------------------------------


class TestReasonsQuoteTheSameNumbersAsTheScore:
    def test_experience_reason_names_the_same_count_the_signal_scored(self) -> None:
        stats = _stats("C-8", KEY, 8, last_load_at=datetime(2026, 7, 1, 12, 0, tzinfo=UTC))
        score = score_carrier(
            _carrier("C-8"),
            load=_load(stops=DALLAS_HOUSTON_STOPS),
            as_of=DAY11,
            key=KEY,
            stats=stats,
            equipment_loads=0,
            last_delivery=None,
            lane_on_time_rate=None,
        )
        experience = score.signal(SIGNAL_EXPERIENCE)
        assert experience.observed == 8.0
        assert "8 loads" in experience.reason
        assert experience.reason == "Ran 8 loads on DFW->HOU, dry van at the METRO tier"
        # The same 8 is what the value used: experience_credit(8) = 8/13.
        assert experience.value == experience_credit(8) == pytest.approx(8 / 13)

    def test_good_carriers_reasons_match_the_hand_computed_strings_exactly(self) -> None:
        load, walk, inputs = _good_weak_setup()
        ranking = rank_carriers(load, walk, inputs)
        good = next(c for c in ranking.carriers if c.carrier.source_carrier_id == "CARRIER-GOOD")
        assert good.reasons == (
            "Ran 14 loads on DFW->HOU, dry van at the METRO tier",
            "Last load on this lane 6 days ago (2026-07-10)",
            "Has hauled dry van for you (20 loads)",
            "Delivered in Fort Worth, TX 76102, 36.6 mi from your pickup",
            "93% on-time on this lane (13 of 14 delivered loads), shrunk to 91% toward the lane's 85%",
            "Averages $1.12/mi on DFW->HOU, dry van",
        )

    def test_carrier_with_zero_lane_history_gets_truthful_not_boilerplate_reasons(self) -> None:
        """R5: weak carriers are returned, ranked last, with an accurate
        reason -- not silence and not a copy-pasted "no data" line."""
        load, walk, inputs = _good_weak_setup()
        ranking = rank_carriers(load, walk, inputs)
        weak = next(c for c in ranking.carriers if c.carrier.source_carrier_id == "CARRIER-WEAK")
        assert weak.rank == 2  # ranked last of the two
        assert weak.reasons == (
            "Has never run DFW->HOU, dry van at the METRO tier",
            "No load on this lane, so no recency credit",
            "Has never hauled dry van for you",
            "No known recent delivery for this carrier, so no proximity credit",
            "No completed delivery on this lane yet, so on-time sits at the lane average of 85%",
        )
        # Every one of the five is distinct and non-empty: each names which
        # of the five facts is actually missing for this specific carrier,
        # not a single generic "no data" sentence repeated five times.
        assert len(set(weak.reasons)) == 5
        assert all(reason for reason in weak.reasons)
        assert weak.rate_note is None  # no carrier_stats row to report a rate from


# ---------------------------------------------------------------------------
# Ranking order and the ranking-level basis sentence.
# ---------------------------------------------------------------------------


class TestRankOrderingAndBasis:
    def test_good_outranks_weak_and_basis_names_the_lane(self) -> None:
        load, walk, inputs = _good_weak_setup()
        ranking = rank_carriers(load, walk, inputs)
        assert [c.carrier.source_carrier_id for c in ranking.carriers] == ["CARRIER-GOOD", "CARRIER-WEAK"]
        assert ranking.carriers[0].rank == 1
        assert ranking.carriers[1].rank == 2
        assert ranking.tier == "METRO"
        assert ranking.lane_key == "DFW->HOU"
        assert ranking.equipment_pool == "DRY_VAN"
        assert ranking.lane_load_count == 20
        assert ranking.lane_on_time_rate == pytest.approx(0.85)  # 17/20
        assert ranking.basis == (
            "2 carriers scored as of 2026-07-16 against 20 loads on "
            "DFW->HOU, dry van (METRO tier, 85% on-time overall)"
        )

    def test_good_and_weak_carriers_scores(self) -> None:
        load, walk, inputs = _good_weak_setup()
        ranking = rank_carriers(load, walk, inputs)
        good = ranking.carriers[0]
        weak = ranking.carriers[1]
        # GOOD -- experience 14/19=0.736842, recency exp(-6/30)=0.818731,
        # equipment 1.0, deadhead min(1,(250-36.6)/200)=1.0,
        # on_time (13+0.85*5)/19=0.907895.
        # 100*(0.35*0.736842 + 0.20*0.818731 + 0.15*1.0 + 0.20*1.0 + 0.10*0.907895)
        #   = 25.7895 + 16.3746 + 15.0 + 20.0 + 9.0789 = 86.2430 -> 86.2
        assert good.score_exact == pytest.approx(86.243, abs=0.001)
        assert good.score == 86.2
        # WEAK -- only on-time contributes, and at n=0 it sits exactly at the
        # lane average: 100*0.10*0.85 = 8.5.
        assert weak.score_exact == 8.5
        assert weak.score == 8.5
        assert good.score > weak.score

    def test_signal_lookup_by_name_raises_for_an_unknown_name(self) -> None:
        load, walk, inputs = _good_weak_setup()
        ranking = rank_carriers(load, walk, inputs)
        with pytest.raises(KeyError):
            ranking.carriers[0].signal("not_a_real_signal")


# ---------------------------------------------------------------------------
# Determinism and ties.
# ---------------------------------------------------------------------------


class TestDeterminismAndTieBreak:
    def test_two_identical_calls_produce_identical_order_and_scores(self) -> None:
        load, walk, inputs = _good_weak_setup()
        first = rank_carriers(load, walk, inputs)
        second = rank_carriers(load, walk, inputs)
        assert [c.carrier.source_carrier_id for c in first.carriers] == [
            c.carrier.source_carrier_id for c in second.carriers
        ]
        assert [c.score for c in first.carriers] == [c.score for c in second.carriers]

    def test_equal_scores_break_ties_by_ascending_carrier_id(self) -> None:
        # Two carriers with identical (empty) lane history score identically
        # -- both fall to the on-time-at-the-prior value only. The tie-break
        # is carrier id, ascending, documented in rank_carriers's own
        # docstring -- Phase 9's end-to-end check depends on this order not
        # reshuffling.
        load = _load(stops=DALLAS_HOUSTON_STOPS)
        walk = _walk_accepted(KEY, load_count=10)
        inputs = _inputs(
            as_of=DAY11,
            carriers=(_carrier("Z-CARRIER"), _carrier("A-CARRIER")),
            lane_on_time_count=8,
            lane_on_time_eligible_count=10,  # 0.8
        )
        ranking = rank_carriers(load, walk, inputs)
        assert ranking.carriers[0].score_exact == ranking.carriers[1].score_exact  # a genuine tie
        assert [c.carrier.source_carrier_id for c in ranking.carriers] == ["A-CARRIER", "Z-CARRIER"]


# ---------------------------------------------------------------------------
# No rung cleared the minimum -- there is no lane, so equipment and truck
# position are the only signals that can separate carriers (invariant 6).
# ---------------------------------------------------------------------------


class TestNoAcceptedRungFallback:
    def test_no_lane_scores_only_equipment_and_position_and_says_so(self) -> None:
        load = _load(stops=DALLAS_HOUSTON_STOPS)
        walk = _walk_no_rung("DRY_VAN")
        inputs = _inputs(as_of=DAY11, carriers=(_carrier("C1"),))
        ranking = rank_carriers(load, walk, inputs)
        assert ranking.tier is None
        assert ranking.lane_key is None
        assert ranking.equipment_pool == "DRY_VAN"

        c1 = ranking.carriers[0]
        experience = c1.signal(SIGNAL_EXPERIENCE)
        assert experience.value == 0.0
        assert experience.reason == (
            f"No lane history to score against — no tier reached the "
            f"{MIN_SAMPLE}-load minimum for this load"
        )
        on_time = c1.signal(SIGNAL_ON_TIME)
        assert on_time.value == NEUTRAL
        assert on_time.reason == "Nothing on this lane has delivered yet, so on-time is scored neutral for everyone"

        # Only on-time contributes anything (NEUTRAL, no lane, no equipment,
        # no known position): 100 * 0.10 * 0.5 = 5.0.
        assert c1.score_exact == 5.0

        assert ranking.basis == (
            "1 carrier scored as of 2026-07-16 with no lane history — no tier "
            f"reached the {MIN_SAMPLE}-load minimum, so only equipment and "
            "truck position separate them"
        )


# ---------------------------------------------------------------------------
# Scoring reads lane/equipment/position evidence only -- never money and
# never the load's own trip distance.
# ---------------------------------------------------------------------------


class TestScoreIgnoresPriceAndDistance:
    def test_zero_mile_load_scores_identically_to_a_long_haul_load(self) -> None:
        # Deadhead is the carrier's last-delivery-to-pickup distance, never
        # load.distance_miles -- scoring.py never reads that field at all.
        carrier = _carrier("C1")
        stats = _stats("C1", KEY, 10, last_load_at=datetime(2026, 7, 1, 12, 0, tzinfo=UTC))
        delivery = _last_delivery("C1", "L1", "Fort Worth", "TX", "76102")
        short = _load(distance_miles=0.0, stops=DALLAS_HOUSTON_STOPS)
        long = _load(distance_miles=5000.0, stops=DALLAS_HOUSTON_STOPS)
        short_score = score_carrier(
            carrier, load=short, as_of=DAY11, key=KEY, stats=stats, equipment_loads=0,
            last_delivery=delivery, lane_on_time_rate=0.8,
        )
        long_score = score_carrier(
            carrier, load=long, as_of=DAY11, key=KEY, stats=stats, equipment_loads=0,
            last_delivery=delivery, lane_on_time_rate=0.8,
        )
        assert short_score.score_exact == long_score.score_exact

    def test_a_zero_dollar_rate_neither_helps_nor_hurts_the_score(self) -> None:
        # avg_rate_per_mile feeds only the non-scoring rate_note, never a
        # Signal -- so a $0/mi average must not read as an attractive signal.
        carrier = _carrier("C1")
        load = _load(stops=DALLAS_HOUSTON_STOPS)
        cheap = _stats("C1", KEY, 10, avg_rate_per_mile=0.0)
        normal = _stats("C1", KEY, 10, avg_rate_per_mile=2.50)
        cheap_score = score_carrier(
            carrier, load=load, as_of=DAY11, key=KEY, stats=cheap, equipment_loads=0,
            last_delivery=None, lane_on_time_rate=0.8,
        )
        normal_score = score_carrier(
            carrier, load=load, as_of=DAY11, key=KEY, stats=normal, equipment_loads=0,
            last_delivery=None, lane_on_time_rate=0.8,
        )
        assert cheap_score.score_exact == normal_score.score_exact
        assert cheap_score.rate_note == "Averages $0.00/mi on DFW->HOU, dry van"

    def test_a_carrier_whose_only_load_paid_zero_does_not_top_the_ranking(self) -> None:
        load = _load(stops=DALLAS_HOUSTON_STOPS)
        walk = _walk_accepted(KEY, load_count=25)
        cheap_stats = _stats("CHEAP", KEY, 1, avg_rate_per_mile=0.0)  # $0/mi, one thin load
        strong_stats = _stats(
            "STRONG", KEY, 20, avg_rate_per_mile=2.50, on_time_count=18, on_time_eligible_count=20,
            last_load_at=datetime(2026, 7, 15, 12, 0, tzinfo=UTC),  # 1 day before DAY11
        )
        strong_delivery = _last_delivery("STRONG", "L1", "Fort Worth", "TX", "76102")
        inputs = _inputs(
            as_of=DAY11,
            carriers=(_carrier("CHEAP"), _carrier("STRONG")),
            carrier_stats={"CHEAP": cheap_stats, "STRONG": strong_stats},
            equipment_loads={"STRONG": {"DRY_VAN": 5}},
            last_deliveries={"STRONG": strong_delivery},
            lane_on_time_count=17,
            lane_on_time_eligible_count=20,  # 0.85 -- an on-time lane
        )
        ranking = rank_carriers(load, walk, inputs)
        # CHEAP: experience 1/6=0.1667, everything else 0 (no equipment
        # history, no known position, no on-time verdict) except on-time
        # sitting at the 0.85 prior. STRONG: a real (non-zero) track record on
        # every signal. STRONG must win despite CHEAP's "attractive" $0 rate,
        # because the rate never entered the score.
        assert [c.carrier.source_carrier_id for c in ranking.carriers] == ["STRONG", "CHEAP"]
        cheap = next(c for c in ranking.carriers if c.carrier.source_carrier_id == "CHEAP")
        assert cheap.rank == 2
        assert cheap.avg_rate_per_mile == 0.0
        assert cheap.rate_note == "Averages $0.00/mi on DFW->HOU, dry van"


# ---------------------------------------------------------------------------
# Edge cases: empty carrier list.
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_carrier_list_returns_an_empty_ranking_without_raising(self) -> None:
        load = _load(stops=DALLAS_HOUSTON_STOPS)
        walk = _walk_no_rung("DRY_VAN")
        inputs = _inputs(as_of=DAY11, carriers=())
        ranking = rank_carriers(load, walk, inputs)
        assert ranking.carriers == ()
        assert ranking.basis.startswith("0 carriers scored")

    def test_single_carrier_with_no_loads_at_all_is_still_returned_and_ranked_first(self) -> None:
        # "Nobody is dropped" (TASKS.md R5): a lone, entirely unproven carrier
        # is still rank 1 of 1 -- there is no one to lose to, but it must not
        # crash or vanish.
        load = _load(stops=DALLAS_HOUSTON_STOPS)
        walk = _walk_accepted(KEY, load_count=12)
        inputs = _inputs(
            as_of=DAY11,
            carriers=(_carrier("LONE"),),
            lane_on_time_count=9,
            lane_on_time_eligible_count=12,  # 0.75
        )
        ranking = rank_carriers(load, walk, inputs)
        assert len(ranking.carriers) == 1
        assert ranking.carriers[0].rank == 1
        assert ranking.carriers[0].lane_loads == 0
