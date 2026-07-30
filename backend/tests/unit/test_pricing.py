"""Unit tests for ``app.domain.pricing`` and ``app.domain.lanes`` -- the tier
walk (PRD section 7) and the price estimate (PRD section 9).

CLAUDE.md invariant 6: every lane/price answer reports which tier it used and
how many loads backed it, and low confidence is labeled, never hidden.
DECISIONS.md D6 (equipment filters at every tier, plus a fourth ``REGION_ANY``
rung), D15 (a heterogeneous accepted pool caps confidence at medium) and D18
(percentiles are already 4dp-quantized by the repository, so a point estimate
is reproducible by hand from its own provenance line) are the three rules this
file exists to pin down.

``lane_stats`` is a plain callable ``LaneKey -> LaneStats | None`` throughout,
so every test is a dict literal with no database. Every dollar assertion
carries its multiplication in a comment.

No database, no network, no file I/O.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.domain.geo import resolve_place
from app.domain.lanes import (
    TIER_METRO,
    TIER_REGION,
    TIER_REGION_ANY,
    TIER_ZIP3,
    LaneKey,
)
from app.domain.model import ANY_EQUIPMENT, Equipment, Load, LaneStats, LoadStatus, Stop, StopLocation
from app.domain.pricing import (
    HIGH_CONFIDENCE_LOADS,
    MIN_SAMPLE,
    Confidence,
    PriceEstimate,
    TierAttempt,
    TierWalk,
    estimate_price,
    price_estimate,
    walk_tiers,
)

UTC = timezone.utc


# ---------------------------------------------------------------------------
# Helpers -- inline stub Loads and LaneStats, no fixture files.
# ---------------------------------------------------------------------------


def _stop(seq: int, pickup: bool, drop: bool, city: str, state: str, zip_code: str) -> Stop:
    place = resolve_place(city, state, zip_code)
    assert place is not None, f"{city}, {state} {zip_code} must resolve for this test"
    loc = StopLocation(city=city, state=state, zip=zip_code, place=place)
    return Stop(sequence=seq, is_pickup=pickup, is_drop=drop, location=loc, scheduled_date=None)


def _geo_null_stop(seq: int, pickup: bool, drop: bool) -> Stop:
    loc = StopLocation(city=None, state=None, zip=None, place=None)
    return Stop(sequence=seq, is_pickup=pickup, is_drop=drop, location=loc, scheduled_date=None)


def _load(
    *,
    equipment: Equipment = Equipment.DRY_VAN,
    distance_miles: float | None = 200.0,
    stops: tuple[Stop, ...],
) -> Load:
    """The minimum Load a pricing test needs; every field pricing ignores is None."""
    return Load(
        source_load_id="TEST-1",
        load_number="TEST-1",
        status=LoadStatus.DELIVERED,
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


def _stats(
    tier: str,
    origin_key: str,
    dest_key: str,
    equipment: str,
    count: int,
    *,
    p25: float | None = None,
    p50: float | None = None,
    p75: float | None = None,
    first: datetime | None = None,
    last: datetime | None = None,
) -> LaneStats:
    return LaneStats(
        tier=tier,
        origin_key=origin_key,
        dest_key=dest_key,
        equipment=equipment,
        load_count=count,
        rate_per_mile_p25=p25,
        rate_per_mile_p50=p50,
        rate_per_mile_p75=p75,
        first_load_at=first,
        last_load_at=last,
    )


def _lookup(entries: dict[LaneKey, LaneStats]):
    """A dict-backed ``LaneStatsLookup``: a plain callable, no database."""
    return entries.get


# Dallas 75201 (zip3 752, metro DFW) -> Houston 77002 (zip3 770, metro HOU).
# Used for the generic tier-walk / confidence tests.
DALLAS_HOUSTON_STOPS = (
    _stop(1, True, False, "Dallas", "TX", "75201"),
    _stop(2, False, True, "Houston", "TX", "77002"),
)

# Irving 75061 (zip3 750, metro DFW) -> Pearland 77584 (zip3 775, metro HOU),
# 293.4 mi -- the exact day-11 load (SHP6701577) and lane DECISIONS.md D15
# documents, reused here so the cap and the dollar figures can be checked
# against the numbers already published in that decision.
IRVING_PEARLAND_STOPS = (
    _stop(1, True, False, "Irving", "TX", "75061"),
    _stop(2, False, True, "Pearland", "TX", "77584"),
)


class TestConstants:
    def test_min_sample_is_5(self) -> None:
        # CLAUDE.md "Known traps" / PRD section 7: 5 loads to accept a tier.
        assert MIN_SAMPLE == 5

    def test_high_confidence_threshold_is_15(self) -> None:
        # PRD section 9: high >= 15, medium 5-14.
        assert HIGH_CONFIDENCE_LOADS == 15


# ---------------------------------------------------------------------------
# Tier boundaries -- the 4-vs-5 case
# ---------------------------------------------------------------------------


class TestTierBoundaries:
    def test_five_loads_accepts_the_rung(self) -> None:
        load = _load(equipment=Equipment.DRY_VAN, distance_miles=200.0, stops=DALLAS_HOUSTON_STOPS)
        key = LaneKey(TIER_ZIP3, "752", "770", "DRY_VAN")
        stats = _stats(TIER_ZIP3, "752", "770", "DRY_VAN", 5, p25=2.0, p50=2.2, p75=2.4)
        walk = walk_tiers(load, _lookup({key: stats}))
        # 5 >= MIN_SAMPLE(5): accepted right at ZIP3, the narrowest rung.
        assert walk.tier == TIER_ZIP3
        assert walk.load_count == 5
        assert walk.accepted is not None
        assert walk.accepted.stats is stats
        assert len(walk.rungs) == 1  # nothing wider was ever asked

    def test_four_loads_rejects_and_falls_outward_to_metro(self) -> None:
        load = _load(equipment=Equipment.DRY_VAN, distance_miles=200.0, stops=DALLAS_HOUSTON_STOPS)
        zip3_key = LaneKey(TIER_ZIP3, "752", "770", "DRY_VAN")
        zip3_stats = _stats(TIER_ZIP3, "752", "770", "DRY_VAN", 4, p25=2.0, p50=2.2, p75=2.4)
        metro_key = LaneKey(TIER_METRO, "DFW", "HOU", "DRY_VAN")
        metro_stats = _stats(TIER_METRO, "DFW", "HOU", "DRY_VAN", 5, p25=2.1, p50=2.3, p75=2.5)
        walk = walk_tiers(load, _lookup({zip3_key: zip3_stats, metro_key: metro_stats}))
        # 4 < MIN_SAMPLE(5): ZIP3 is rejected on the identical geography that
        # accepted at 5 above, and the walk reports METRO -- not ZIP3 -- as
        # the tier it actually used.
        assert walk.tier == TIER_METRO
        assert walk.load_count == 5
        assert walk.rungs[0].tier == TIER_ZIP3
        assert walk.rungs[0].load_count == 4
        assert walk.rungs[0].accepted is False
        assert walk.rungs[1].tier == TIER_METRO
        assert walk.rungs[1].load_count == 5
        assert walk.rungs[1].accepted is True


# ---------------------------------------------------------------------------
# The trace is a first-class result: rungs before the winner keep their real
# counts, rungs after it are simply absent.
# ---------------------------------------------------------------------------


class TestTierWalkTrace:
    def test_rungs_before_the_winner_are_present_and_rungs_after_are_absent(self) -> None:
        load = _load(equipment=Equipment.DRY_VAN, distance_miles=200.0, stops=DALLAS_HOUSTON_STOPS)
        zip3_key = LaneKey(TIER_ZIP3, "752", "770", "DRY_VAN")
        zip3_stats = _stats(TIER_ZIP3, "752", "770", "DRY_VAN", 3)
        metro_key = LaneKey(TIER_METRO, "DFW", "HOU", "DRY_VAN")
        metro_stats = _stats(TIER_METRO, "DFW", "HOU", "DRY_VAN", 20, p25=2.1, p50=2.3, p75=2.5)
        walk = walk_tiers(load, _lookup({zip3_key: zip3_stats, metro_key: metro_stats}))
        # Won at METRO (rung 2 of 4). REGION and REGION_ANY were never asked:
        # the trace has exactly 2 rungs, not 4 with the last two zeroed out.
        assert walk.tier == TIER_METRO
        assert len(walk.rungs) == 2
        assert [r.tier for r in walk.rungs] == [TIER_ZIP3, TIER_METRO]
        # The rejected rung keeps its real (nonzero) count -- it was asked and
        # answered, just not enough.
        assert walk.rungs[0].load_count == 3
        assert walk.rungs[0].accepted is False
        assert walk.rungs[1].load_count == 20
        assert walk.rungs[1].accepted is True


# ---------------------------------------------------------------------------
# All four rungs, each forced to be the winner in turn.
# ---------------------------------------------------------------------------


class TestAllFourRungs:
    def test_zip3_wins(self) -> None:
        load = _load(equipment=Equipment.DRY_VAN, distance_miles=200.0, stops=DALLAS_HOUSTON_STOPS)
        key = LaneKey(TIER_ZIP3, "752", "770", "DRY_VAN")
        stats = _stats(TIER_ZIP3, "752", "770", "DRY_VAN", 10, p25=2.0, p50=2.2, p75=2.4)
        walk = walk_tiers(load, _lookup({key: stats}))
        assert walk.tier == TIER_ZIP3
        assert walk.equipment_filter == "DRY_VAN"
        assert walk.equipment_filtered is True

    def test_metro_wins(self) -> None:
        load = _load(equipment=Equipment.DRY_VAN, distance_miles=200.0, stops=DALLAS_HOUSTON_STOPS)
        zip3_key = LaneKey(TIER_ZIP3, "752", "770", "DRY_VAN")
        metro_key = LaneKey(TIER_METRO, "DFW", "HOU", "DRY_VAN")
        entries = {
            zip3_key: _stats(TIER_ZIP3, "752", "770", "DRY_VAN", 2),
            metro_key: _stats(TIER_METRO, "DFW", "HOU", "DRY_VAN", 10, p25=2.0, p50=2.2, p75=2.4),
        }
        walk = walk_tiers(load, _lookup(entries))
        assert walk.tier == TIER_METRO
        assert walk.load_count == 10

    def test_region_wins(self) -> None:
        load = _load(equipment=Equipment.DRY_VAN, distance_miles=200.0, stops=DALLAS_HOUSTON_STOPS)
        # ZIP3/METRO absent entirely (lookup returns None -> count 0, rejected).
        region_key = LaneKey(TIER_REGION, "TX_TRIANGLE", "TX_TRIANGLE", "DRY_VAN")
        region_stats = _stats(TIER_REGION, "TX_TRIANGLE", "TX_TRIANGLE", "DRY_VAN", 10, p25=2.0, p50=2.2, p75=2.4)
        walk = walk_tiers(load, _lookup({region_key: region_stats}))
        assert walk.tier == TIER_REGION
        assert walk.rungs[0].load_count == 0  # ZIP3: asked, found nothing
        assert walk.rungs[1].load_count == 0  # METRO: same
        assert walk.equipment_filter == "DRY_VAN"  # still filtered -- rung 3 keeps the filter

    def test_region_any_wins(self) -> None:
        load = _load(equipment=Equipment.DRY_VAN, distance_miles=200.0, stops=DALLAS_HOUSTON_STOPS)
        region_any_key = LaneKey(TIER_REGION_ANY, "TX_TRIANGLE", "TX_TRIANGLE", ANY_EQUIPMENT)
        region_any_stats = _stats(
            TIER_REGION_ANY, "TX_TRIANGLE", "TX_TRIANGLE", ANY_EQUIPMENT, 12, p25=2.0, p50=2.2, p75=2.4
        )
        walk = walk_tiers(load, _lookup({region_any_key: region_any_stats}))
        assert walk.tier == TIER_REGION_ANY
        assert walk.load_count == 12
        # Rung 4 drops the filter by definition, even for a DRY_VAN load whose
        # own equipment was never UNKNOWN.
        assert walk.equipment_filter == ANY_EQUIPMENT
        assert walk.equipment_filtered is False


# ---------------------------------------------------------------------------
# D6 -- equipment filters at every tier; UNKNOWN skips the filter.
# ---------------------------------------------------------------------------


class TestEquipmentFilterD6:
    def test_unknown_equipment_reads_the_any_pool_and_says_so(self) -> None:
        load = _load(equipment=Equipment.UNKNOWN, distance_miles=200.0, stops=DALLAS_HOUSTON_STOPS)
        key = LaneKey(TIER_ZIP3, "752", "770", ANY_EQUIPMENT)
        stats = _stats(TIER_ZIP3, "752", "770", ANY_EQUIPMENT, 7, p25=2.0, p50=2.2, p75=2.4)
        walk = walk_tiers(load, _lookup({key: stats}))
        assert walk.tier == TIER_ZIP3
        assert walk.equipment == ANY_EQUIPMENT  # the pool asked for, not "DRY_VAN"
        assert walk.equipment_filter == ANY_EQUIPMENT
        assert walk.equipment_filtered is False

        estimate = price_estimate(load, walk)
        # D6: provenance names the pool honestly rather than silently naming
        # dry van (invariant 5 -- UNKNOWN must never be read as a default).
        assert "all equipment types" in estimate.provenance
        assert "dry van" not in estimate.provenance

    def test_dry_van_load_cannot_see_the_any_pool_data_a_zip3_over(self) -> None:
        """The mirror of the test above: a real equipment filter must actually filter."""
        # Only an ANY-keyed ZIP3 entry exists, with plenty of loads -- but a
        # DRY_VAN load queries the DRY_VAN-keyed bucket at every real tier, and
        # the ANY-keyed bucket only exists at ZIP3 (not at the region level),
        # so its own equipment filter must keep it from ever reaching this data.
        any_key = LaneKey(TIER_ZIP3, "752", "770", ANY_EQUIPMENT)
        any_stats = _stats(TIER_ZIP3, "752", "770", ANY_EQUIPMENT, 50, p25=2.0, p50=2.2, p75=2.4)
        lookup = _lookup({any_key: any_stats})

        dry_van_load = _load(equipment=Equipment.DRY_VAN, distance_miles=200.0, stops=DALLAS_HOUSTON_STOPS)
        dry_van_walk = walk_tiers(dry_van_load, lookup)
        # The 50-load ANY pool sits right there in the dict, but a DRY_VAN
        # query never sees it: DRY_VAN-keyed lookups all miss, and rung 4's
        # region-level ANY key is a different LaneKey than this ZIP3 one.
        assert dry_van_walk.accepted is None
        assert dry_van_walk.rungs[0].load_count == 0

        # Same geography, UNKNOWN equipment: now the same ANY-keyed data answers.
        unknown_load = _load(equipment=Equipment.UNKNOWN, distance_miles=200.0, stops=DALLAS_HOUSTON_STOPS)
        unknown_walk = walk_tiers(unknown_load, lookup)
        assert unknown_walk.tier == TIER_ZIP3
        assert unknown_walk.load_count == 50


# ---------------------------------------------------------------------------
# Confidence boundaries (PRD section 9).
# ---------------------------------------------------------------------------


def _walk_with(tier: str, key: LaneKey, count: int, **stat_kwargs) -> TierWalk:
    """Build a one-rung, already-accepted TierWalk directly -- isolates
    ``_confidence`` from the lookup/dict machinery the walk tests above use."""
    stats = _stats(tier, key.origin_key, key.dest_key, key.equipment, count, **stat_kwargs)
    attempt = TierAttempt(tier=tier, key=key, load_count=count, accepted=True, stats=stats)
    return TierWalk(rungs=(attempt,), accepted=attempt, equipment=key.equipment)


class TestConfidence:
    LOAD = _load(equipment=Equipment.DRY_VAN, distance_miles=200.0, stops=DALLAS_HOUSTON_STOPS)
    ZIP3_KEY = LaneKey(TIER_ZIP3, "752", "770", "DRY_VAN")

    def test_14_loads_is_medium(self) -> None:
        walk = _walk_with(TIER_ZIP3, self.ZIP3_KEY, 14, p25=2.0, p50=2.2, p75=2.4)
        estimate = price_estimate(self.LOAD, walk)
        assert estimate.confidence == Confidence.MEDIUM  # 14 < HIGH_CONFIDENCE_LOADS(15)

    def test_15_loads_is_high(self) -> None:
        walk = _walk_with(TIER_ZIP3, self.ZIP3_KEY, 15, p25=2.0, p50=2.2, p75=2.4)
        estimate = price_estimate(self.LOAD, walk)
        assert estimate.confidence == Confidence.HIGH  # 15 >= HIGH_CONFIDENCE_LOADS(15)

    def test_5_loads_is_medium_the_floor_of_the_band(self) -> None:
        walk = _walk_with(TIER_ZIP3, self.ZIP3_KEY, 5, p25=2.0, p50=2.2, p75=2.4)
        estimate = price_estimate(self.LOAD, walk)
        assert estimate.confidence == Confidence.MEDIUM  # PRD section 9: medium is 5-14

    def test_no_accepted_rung_is_low(self) -> None:
        walk = walk_tiers(self.LOAD, _lookup({}))
        assert walk.accepted is None
        estimate = price_estimate(self.LOAD, walk)
        assert estimate.confidence == Confidence.LOW

    def test_region_rung_with_40_loads_is_still_low(self) -> None:
        # PRD section 9: low confidence "or the REGION/REGION_ANY tiers" --
        # the rule fires on the *rung*, not on the count, however large.
        region_key = LaneKey(TIER_REGION, "TX_TRIANGLE", "TX_TRIANGLE", "DRY_VAN")
        walk = _walk_with(TIER_REGION, region_key, 40, p25=2.0, p50=2.2, p75=2.4)
        estimate = price_estimate(self.LOAD, walk)
        assert estimate.confidence == Confidence.LOW  # not high, despite 40 >= 15

    def test_region_any_rung_with_100_loads_is_still_low(self) -> None:
        region_any_key = LaneKey(TIER_REGION_ANY, "TX_TRIANGLE", "TX_TRIANGLE", ANY_EQUIPMENT)
        walk = _walk_with(TIER_REGION_ANY, region_any_key, 100, p25=2.0, p50=2.2, p75=2.4)
        estimate = price_estimate(self.LOAD, walk)
        assert estimate.confidence == Confidence.LOW  # not high, despite 100 >= 15

    def test_confidence_has_its_own_min_sample_guard(self) -> None:
        # Defense in depth: even a (contrived, hand-built) "accepted" rung with
        # fewer than MIN_SAMPLE loads must not be reported as anything above
        # low. walk_tiers itself never produces this, but _confidence does not
        # trust the caller's accepted flag alone.
        attempt = TierAttempt(
            tier=TIER_ZIP3,
            key=self.ZIP3_KEY,
            load_count=4,
            accepted=True,
            stats=_stats(TIER_ZIP3, "752", "770", "DRY_VAN", 4, p25=2.0, p50=2.2, p75=2.4),
        )
        walk = TierWalk(rungs=(attempt,), accepted=attempt, equipment="DRY_VAN")
        estimate = price_estimate(self.LOAD, walk)
        assert estimate.confidence == Confidence.LOW  # 4 < MIN_SAMPLE(5)


# ---------------------------------------------------------------------------
# D15 -- a heterogeneous accepted pool caps confidence at medium.
# ---------------------------------------------------------------------------


class TestHeterogeneousPoolD15:
    # DECISIONS.md D15's own example: day-11 load SHP6701577, Irving 75061 ->
    # Pearland 77584, 293.4 mi, METRO DFW->HOU, 31 loads (23 dry van + 8
    # reefer), p25 2.4800 / median 2.5300 / p75 2.6700.
    LOAD = _load(equipment=Equipment.UNKNOWN, distance_miles=293.4, stops=IRVING_PEARLAND_STOPS)
    METRO_ANY_KEY = LaneKey(TIER_METRO, "DFW", "HOU", ANY_EQUIPMENT)

    def _walk(self) -> TierWalk:
        stats = _stats(
            TIER_METRO, "DFW", "HOU", ANY_EQUIPMENT, 31, p25=2.48, p50=2.53, p75=2.67
        )
        return walk_tiers(self.LOAD, _lookup({self.METRO_ANY_KEY: stats}))

    def test_mixed_pool_caps_at_medium_and_names_the_mix(self) -> None:
        walk = self._walk()
        assert walk.tier == TIER_METRO
        assert walk.load_count == 31  # would be HIGH (>=15) without the D15 cap
        mix = {"DRY_VAN": 23, "REEFER": 8}
        estimate = price_estimate(self.LOAD, walk, equipment_mix=mix)

        assert estimate.is_heterogeneous is True
        assert estimate.equipment_mix == (("DRY_VAN", 23), ("REEFER", 8))  # sorted by -count
        assert estimate.confidence == Confidence.MEDIUM  # capped, not HIGH
        assert "23 dry van, 8 reefer" in estimate.provenance
        assert "capped at medium" in estimate.provenance

        # D18 + D15 together: the dollar figures reproduce D15's own doc
        # exactly, by hand, from the rate and mileage printed alongside them.
        assert estimate.point_usd == 742.3   # 2.5300 x 293.4 = 742.302 -> $742.30
        assert estimate.low_usd == 727.63    # 2.4800 x 293.4 = 727.632 -> $727.63
        assert estimate.high_usd == 783.38   # 2.6700 x 293.4 = 783.378 -> $783.38 (D15's own number)

    def test_homogeneous_pool_is_not_capped_even_though_the_filter_was_off(self) -> None:
        """The rule is about the *mix*, not about the filter having been skipped."""
        walk = self._walk()
        mix = {"DRY_VAN": 31}  # same accepted pool, but every load happens to be one type
        estimate = price_estimate(self.LOAD, walk, equipment_mix=mix)

        assert estimate.is_heterogeneous is False
        assert estimate.equipment_mix == (("DRY_VAN", 31),)
        assert estimate.confidence == Confidence.HIGH  # 31 >= 15, not capped
        assert "capped" not in estimate.provenance

    def test_filtered_pool_ignores_an_equipment_mix_argument(self) -> None:
        """D15 only ever applies to the unfiltered ANY pool (per its own docstring)."""
        load = _load(equipment=Equipment.DRY_VAN, distance_miles=293.4, stops=IRVING_PEARLAND_STOPS)
        key = LaneKey(TIER_ZIP3, "750", "775", "DRY_VAN")
        stats = _stats(TIER_ZIP3, "750", "775", "DRY_VAN", 20, p25=2.4, p50=2.5, p75=2.6)
        walk = walk_tiers(load, _lookup({key: stats}))
        assert walk.tier == TIER_ZIP3

        # Even handed a (wrong, hypothetical) heterogeneous mix, a filtered
        # pool is one type by construction and must not be capped by it.
        estimate = price_estimate(load, walk, equipment_mix={"DRY_VAN": 10, "REEFER": 10})
        assert estimate.equipment_mix == ()
        assert estimate.confidence == Confidence.HIGH  # 20 >= 15, untouched by the cap

    def test_rung_four_names_the_mix_without_claiming_a_cap_that_never_bound(
        self,
    ) -> None:
        """D25: on REGION_ANY the cap sentence would contradict its own label.

        ``_confidence`` returns LOW for REGION_ANY *before* the D15 cap is
        reached, so nothing was ever capped at medium -- but ``mix`` is computed
        for any accepted key whose equipment is ANY, which rung 4 always is. The
        old wording therefore shipped ``confidence: low`` beside a sentence
        reading "so confidence is capped at medium", live on broker_b's day-11
        load ``HD-2026-005077``. That is invariant 2 at the pricing layer.

        The mix itself is true at every rung and stays; the cap claim is gated
        on the outcome. Reproduces the shipped pool: 93 loads on TX_TRIANGLE,
        71 dry van / 18 reefer / 4 flatbed, for a FLATBED load.
        """
        load = _load(
            equipment=Equipment.FLATBED, distance_miles=200.0, stops=DALLAS_HOUSTON_STOPS
        )
        key = LaneKey(TIER_REGION_ANY, "TX_TRIANGLE", "TX_TRIANGLE", ANY_EQUIPMENT)
        stats = _stats(
            TIER_REGION_ANY, "TX_TRIANGLE", "TX_TRIANGLE", ANY_EQUIPMENT, 93,
            p25=2.07, p50=2.33, p75=2.58,
        )
        walk = walk_tiers(load, _lookup({key: stats}))
        assert walk.tier == TIER_REGION_ANY

        mix = {"DRY_VAN": 71, "REEFER": 18, "FLATBED": 4}
        estimate = price_estimate(load, walk, equipment_mix=mix)

        assert estimate.confidence == Confidence.LOW  # rung 4 is low by rule (D6)
        assert estimate.is_heterogeneous is True
        assert "71 dry van, 18 reefer, 4 flatbed" in estimate.provenance
        assert "low confidence" in estimate.provenance
        assert "capped" not in estimate.provenance, (
            f"the provenance claims a cap the confidence field contradicts: "
            f"{estimate.provenance!r}"
        )

    def test_rung_four_names_the_loads_equipment_as_the_loads(self) -> None:
        """D25, the other half: 4 of those 93 loads are flatbed, and the phrase
        sitting where every other rung prints the *pool's* type must not read as
        a claim about the pool."""
        load = _load(
            equipment=Equipment.FLATBED, distance_miles=200.0, stops=DALLAS_HOUSTON_STOPS
        )
        key = LaneKey(TIER_REGION_ANY, "TX_TRIANGLE", "TX_TRIANGLE", ANY_EQUIPMENT)
        stats = _stats(
            TIER_REGION_ANY, "TX_TRIANGLE", "TX_TRIANGLE", ANY_EQUIPMENT, 93,
            p25=2.07, p50=2.33, p75=2.58,
        )
        walk = walk_tiers(load, _lookup({key: stats}))

        # No mix supplied: the sentence has to stand on its own, because
        # price_estimate can be called without one.
        estimate = price_estimate(load, walk)
        assert "(any equipment)" in estimate.provenance
        assert "for a flatbed load" in estimate.provenance
        assert ", flatbed" not in estimate.provenance


# ---------------------------------------------------------------------------
# D18 -- an estimate is reproducible by hand from its own provenance line.
# ---------------------------------------------------------------------------


class TestHandReproducibleDollarsD18:
    def test_point_estimate_is_the_stored_4dp_rate_times_miles(self) -> None:
        # DECISIONS.md D18's own worked example: 2.0200 $/mi x 187.2 mi =
        # 378.144, which rounds to $378.14 -- not the $378.15 the doc found
        # and then corrected. The stored rate is already 4dp, so this is the
        # exact multiplication a reviewer would do by hand.
        load = _load(equipment=Equipment.DRY_VAN, distance_miles=187.2, stops=DALLAS_HOUSTON_STOPS)
        key = LaneKey(TIER_ZIP3, "752", "770", "DRY_VAN")
        stats = _stats(TIER_ZIP3, "752", "770", "DRY_VAN", 10, p25=2.0, p50=2.02, p75=2.05)
        walk = walk_tiers(load, _lookup({key: stats}))
        estimate = price_estimate(load, walk)
        assert estimate.rate_per_mile_p50 == 2.02
        assert estimate.point_usd == 378.14  # 2.0200 x 187.2 = 378.144 -> $378.14


# ---------------------------------------------------------------------------
# Degenerate inputs.
# ---------------------------------------------------------------------------


class TestDegenerateInputs:
    def test_identical_rates_give_equal_p25_p50_p75_and_that_is_correct(self) -> None:
        load = _load(equipment=Equipment.DRY_VAN, distance_miles=200.0, stops=DALLAS_HOUSTON_STOPS)
        key = LaneKey(TIER_ZIP3, "752", "770", "DRY_VAN")
        stats = _stats(TIER_ZIP3, "752", "770", "DRY_VAN", 5, p25=2.5, p50=2.5, p75=2.5)
        walk = walk_tiers(load, _lookup({key: stats}))
        estimate = price_estimate(load, walk)
        # Every load on this lane pays exactly $2.50/mi: a flat range is the
        # honest answer, not a sign of a broken percentile calculation.
        assert estimate.rate_per_mile_p25 == estimate.rate_per_mile_p50 == estimate.rate_per_mile_p75 == 2.5
        assert estimate.low_usd == estimate.point_usd == estimate.high_usd == 500.0  # 2.50 x 200 = 500.00

    def test_zero_mile_load_gives_a_rate_but_no_dollars(self) -> None:
        load = _load(equipment=Equipment.DRY_VAN, distance_miles=0.0, stops=DALLAS_HOUSTON_STOPS)
        key = LaneKey(TIER_ZIP3, "752", "770", "DRY_VAN")
        stats = _stats(TIER_ZIP3, "752", "770", "DRY_VAN", 10, p25=2.0, p50=2.5, p75=3.0)
        walk = walk_tiers(load, _lookup({key: stats}))
        estimate = price_estimate(load, walk)
        assert estimate.rate_per_mile_p50 == 2.5  # the rate is still reported
        assert estimate.point_usd is None  # but there is no distance to turn it into dollars
        assert estimate.low_usd is None
        assert estimate.high_usd is None
        assert "no distance, so no dollar estimate" in estimate.provenance
        assert "2.5000 $/mi" in estimate.provenance

    def test_null_mile_load_gives_a_rate_but_no_dollars_and_does_not_raise(self) -> None:
        load = _load(equipment=Equipment.DRY_VAN, distance_miles=None, stops=DALLAS_HOUSTON_STOPS)
        key = LaneKey(TIER_ZIP3, "752", "770", "DRY_VAN")
        stats = _stats(TIER_ZIP3, "752", "770", "DRY_VAN", 10, p25=2.0, p50=2.5, p75=3.0)
        walk = walk_tiers(load, _lookup({key: stats}))
        estimate = price_estimate(load, walk)  # must not raise ZeroDivisionError or produce inf/nan
        assert estimate.distance_miles is None
        assert estimate.point_usd is None
        assert estimate.low_usd is None
        assert estimate.high_usd is None

    def test_empty_lane_at_every_rung_for_a_resolvable_load_is_honest_about_finding_nothing(self) -> None:
        load = _load(equipment=Equipment.DRY_VAN, distance_miles=200.0, stops=DALLAS_HOUSTON_STOPS)
        walk = walk_tiers(load, _lookup({}))
        assert walk.accepted is None
        assert walk.tier is None
        assert walk.load_count == 0
        # A resolvable load can form all four keys, so all four rungs were
        # actually asked (none skipped) -- they just all came back empty.
        assert len(walk.rungs) == 4
        assert [r.skipped for r in walk.rungs] == [False, False, False, False]
        assert [r.load_count for r in walk.rungs] == [0, 0, 0, 0]

        estimate = price_estimate(load, walk)
        assert estimate.confidence == Confidence.LOW
        assert estimate.point_usd is None
        assert estimate.low_usd is None
        assert estimate.high_usd is None
        assert estimate.provenance == (
            "no estimate: no tier reached the 5-load minimum for dry van "
            "(tried ZIP3 0, METRO 0, REGION 0, REGION_ANY 0), so no dollar estimate"
        )

    def test_empty_lane_for_a_geo_null_load_only_tries_the_region_rungs(self) -> None:
        load = _load(
            equipment=Equipment.DRY_VAN,
            distance_miles=None,
            stops=(_geo_null_stop(1, True, False), _geo_null_stop(2, False, True)),
        )
        walk = walk_tiers(load, _lookup({}))
        # ZIP3/METRO are unreachable (the load's own geography can't form
        # them); REGION/REGION_ANY are always formable, keyed on the region
        # constant rather than this load's geography.
        assert walk.rungs[0].skipped is True
        assert walk.rungs[1].skipped is True
        assert walk.rungs[2].skipped is False
        assert walk.rungs[3].skipped is False
        assert walk.accepted is None

        estimate = price_estimate(load, walk)
        assert estimate.provenance == (
            "no estimate: no tier reached the 5-load minimum for dry van "
            "(tried REGION 0, REGION_ANY 0), so no dollar estimate"
        )


# ---------------------------------------------------------------------------
# estimate_price -- the whole of PRD section 9, wired end to end.
# ---------------------------------------------------------------------------


class TestEstimatePriceWrapper:
    def test_fetches_the_equipment_mix_only_for_an_unfiltered_accepted_pool(self) -> None:
        load = _load(equipment=Equipment.UNKNOWN, distance_miles=293.4, stops=IRVING_PEARLAND_STOPS)
        metro_any_key = LaneKey(TIER_METRO, "DFW", "HOU", ANY_EQUIPMENT)
        stats = _stats(TIER_METRO, "DFW", "HOU", ANY_EQUIPMENT, 31, p25=2.48, p50=2.53, p75=2.67)
        lane_stats = _lookup({metro_any_key: stats})

        calls: list[LaneKey] = []

        def mix_lookup(key: LaneKey) -> dict[str, int]:
            calls.append(key)
            return {"DRY_VAN": 23, "REEFER": 8}

        result = estimate_price(load, lane_stats=lane_stats, equipment_mix=mix_lookup)
        assert calls == [metro_any_key]  # queried exactly once, for the accepted ANY key
        assert result.confidence == Confidence.MEDIUM  # D15 cap fired
        assert result.equipment_mix == (("DRY_VAN", 23), ("REEFER", 8))

    def test_skips_the_equipment_mix_lookup_for_a_filtered_accepted_pool(self) -> None:
        load = _load(equipment=Equipment.DRY_VAN, distance_miles=293.4, stops=IRVING_PEARLAND_STOPS)
        key = LaneKey(TIER_ZIP3, "750", "775", "DRY_VAN")
        stats = _stats(TIER_ZIP3, "750", "775", "DRY_VAN", 20, p25=2.4, p50=2.5, p75=2.6)
        lane_stats = _lookup({key: stats})

        def mix_lookup(_key: LaneKey) -> dict[str, int]:
            raise AssertionError("equipment_mix must not be queried for a filtered pool")

        result = estimate_price(load, lane_stats=lane_stats, equipment_mix=mix_lookup)
        assert result.equipment_mix == ()
        assert result.confidence == Confidence.HIGH  # 20 >= 15, untouched

    def test_no_accepted_rung_never_calls_the_mix_lookup_either(self) -> None:
        load = _load(equipment=Equipment.DRY_VAN, distance_miles=200.0, stops=DALLAS_HOUSTON_STOPS)

        def mix_lookup(_key: LaneKey) -> dict[str, int]:
            raise AssertionError("no accepted rung means no equipment_mix key to look up")

        result = estimate_price(load, lane_stats=_lookup({}), equipment_mix=mix_lookup)
        assert result.confidence == Confidence.LOW
        assert result.equipment_mix == ()


# ---------------------------------------------------------------------------
# TierAttempt.verdict -- the vocabulary data/TRACEABILITY.md reads against.
# ---------------------------------------------------------------------------


class TestTierAttemptVerdict:
    def test_accepted_rung_reads_accepted(self) -> None:
        attempt = TierAttempt(
            tier=TIER_ZIP3,
            key=LaneKey(TIER_ZIP3, "752", "770", "DRY_VAN"),
            load_count=5,
            accepted=True,
        )
        assert attempt.verdict == "ACCEPTED"
        assert attempt.skipped is False

    def test_rejected_rung_names_the_shortfall(self) -> None:
        attempt = TierAttempt(
            tier=TIER_ZIP3,
            key=LaneKey(TIER_ZIP3, "752", "770", "DRY_VAN"),
            load_count=4,
            accepted=False,
        )
        assert attempt.verdict == "rejected, 4 < 5"  # 4 < MIN_SAMPLE(5)

    def test_skipped_rung_names_the_reason(self) -> None:
        attempt = TierAttempt(tier=TIER_ZIP3, key=None, load_count=0, accepted=False)
        assert attempt.skipped is True
        assert attempt.verdict == "skipped, lane end not on the map"
