"""Unit tests for ``app.domain.distance`` — Haversine x 1.2 (TASKS.md G2/G3).

CLAUDE.md invariant 7 fixes the method: offline, no routing API, great-circle
miles multiplied by a flat road factor of 1.2. Every mile figure the product
reports for a lane, and every deadhead distance, traces to this module.

Expected values below are literals with the arithmetic shown in a comment, so a
reviewer can check them without running anything. See DECISIONS.md **D10** for
why Dallas -> Houston is 271 mi here and deliberately not the real-world 239.

No database, no network, no file I/O.
"""

from __future__ import annotations

import math

import pytest

from app.domain.distance import (
    EARTH_RADIUS_MILES,
    ROAD_FACTOR,
    haversine_between,
    haversine_miles,
    road_miles,
    road_miles_between,
)
from app.domain.geo import (
    ALL_PLACES,
    METRO_AUS,
    METRO_DFW,
    METRO_HOU,
    METRO_SAT,
    METRO_TX_OTHER,
    METROS,
    Place,
    lookup_zip,
    places_in_metro,
)

# ---------------------------------------------------------------------------
# Anchors: the first-listed (central) zip of each metro group, i.e. what
# lookup_city returns for the metro's namesake city.
# ---------------------------------------------------------------------------
DALLAS = lookup_zip("75201")
FORT_WORTH = lookup_zip("76102")
GRAND_PRAIRIE = lookup_zip("75050")
HOUSTON = lookup_zip("77002")
KATY = lookup_zip("77449")
SAN_ANTONIO = lookup_zip("78205")
AUSTIN = lookup_zip("78701")
WACO = lookup_zip("76701")

METRO_ANCHORS: dict[str, Place] = {
    METRO_DFW: DALLAS,
    METRO_HOU: HOUSTON,
    METRO_SAT: SAN_ANTONIO,
    METRO_AUS: AUSTIN,
    METRO_TX_OTHER: WACO,
}

# Tolerance is the half-width of rounding to one decimal: the expected literals
# below are the module's own output rounded to 0.1 mi, so 0.05 is exact-to-print,
# not a slack allowance.
TOL = 0.05

# (label, a, b, expected haversine mi, expected road mi = haversine x 1.2)
KNOWN_PAIRS: tuple[tuple[str, Place, Place, float, float], ...] = (
    # 225.7956 x 1.2 = 270.9547 -> see D10: the real I-45 drive is ~239 mi.
    ("Dallas 75201 -> Houston 77002", DALLAS, HOUSTON, 225.8, 271.0),
    # 252.4130 x 1.2 = 302.8955
    ("Dallas 75201 -> San Antonio 78205", DALLAS, SAN_ANTONIO, 252.4, 302.9),
    # 188.9232 x 1.2 = 226.7079
    ("Houston 77002 -> San Antonio 78205", HOUSTON, SAN_ANTONIO, 188.9, 226.7),
    # 216.1590 x 1.2 = 259.3908 -- the suburb-scatter lane (DFW -> HOU metro)
    ("Grand Prairie 75050 -> Katy 77449", GRAND_PRAIRIE, KATY, 216.2, 259.4),
    # 167.6786 x 1.2 = 201.2143
    ("San Antonio 78205 -> Waco 76701", SAN_ANTONIO, WACO, 167.7, 201.2),
    # 30.4720 x 1.2 = 36.5664 -- intra-DFW, the short end of the range
    ("Dallas 75201 -> Fort Worth 76102", DALLAS, FORT_WORTH, 30.5, 36.6),
)


class TestConstants:
    def test_road_factor_is_the_invariant_1_2(self) -> None:
        # CLAUDE.md invariant 7 / PRD section 3. D10 rejected 1.06.
        assert ROAD_FACTOR == 1.2

    def test_earth_radius_is_in_statute_miles(self) -> None:
        assert EARTH_RADIUS_MILES == 3958.7613

    def test_one_degree_of_latitude_is_about_69_miles(self) -> None:
        # Unit check on the radius: along a meridian, d = R * 1 degree in radians
        # = 3958.7613 x pi/180 = 69.0934 mi. Km would give 111.
        one_degree = haversine_miles(0.0, 0.0, 1.0, 0.0)
        assert one_degree == pytest.approx(EARTH_RADIUS_MILES * math.pi / 180)
        assert one_degree == pytest.approx(69.09, abs=0.01)


class TestHaversineProperties:
    def test_same_point_is_exactly_zero(self) -> None:
        assert haversine_miles(32.7831, -96.8067, 32.7831, -96.8067) == 0.0
        assert haversine_between(DALLAS, DALLAS) == 0.0
        assert road_miles_between(HOUSTON, HOUSTON) == 0.0

    @pytest.mark.parametrize(
        ("a", "b"),
        [
            (DALLAS, HOUSTON),
            (HOUSTON, SAN_ANTONIO),
            (GRAND_PRAIRIE, KATY),
            (DALLAS, FORT_WORTH),
            (WACO, AUSTIN),
        ],
    )
    def test_symmetry(self, a: Place, b: Place) -> None:
        assert haversine_between(a, b) == haversine_between(b, a)
        assert road_miles_between(a, b) == road_miles_between(b, a)

    @pytest.mark.parametrize(
        ("a", "via", "b"),
        [
            (DALLAS, WACO, HOUSTON),  # 104.5 + 195.3 = 299.8 >= 271.0
            (GRAND_PRAIRIE, DALLAS, KATY),  # 14.0 + 256.1 = 270.1 >= 259.4
            (SAN_ANTONIO, AUSTIN, HOUSTON),  # 88.2 + 176.0 = 264.2 >= 226.7
            (FORT_WORTH, DALLAS, SAN_ANTONIO),
        ],
    )
    def test_triangle_inequality(self, a: Place, via: Place, b: Place) -> None:
        direct = road_miles_between(a, b)
        detour = road_miles_between(a, via) + road_miles_between(via, b)
        assert detour >= direct
        # A flat scale factor preserves the inequality, so it holds on the
        # great-circle numbers too.
        assert haversine_between(a, via) + haversine_between(via, b) >= haversine_between(a, b)


class TestRoadFactorApplication:
    @pytest.mark.parametrize(
        ("a", "b"),
        [
            (DALLAS, HOUSTON),
            (DALLAS, SAN_ANTONIO),
            (HOUSTON, SAN_ANTONIO),
            (GRAND_PRAIRIE, KATY),
            (SAN_ANTONIO, WACO),
            (DALLAS, FORT_WORTH),
        ],
    )
    def test_road_miles_is_exactly_haversine_times_1_2(self, a: Place, b: Place) -> None:
        # Bit-exact, not approximate: road_miles is haversine x ROAD_FACTOR and
        # nothing else -- no rounding, no per-pair table (D10 rejected that).
        assert road_miles_between(a, b) == haversine_between(a, b) * 1.2
        assert road_miles(a.lat, a.lon, b.lat, b.lon) == haversine_miles(
            a.lat, a.lon, b.lat, b.lon
        ) * ROAD_FACTOR

    def test_place_helpers_agree_with_the_raw_coordinate_functions(self) -> None:
        assert haversine_between(DALLAS, HOUSTON) == haversine_miles(
            DALLAS.lat, DALLAS.lon, HOUSTON.lat, HOUSTON.lon
        )
        assert road_miles_between(DALLAS, HOUSTON) == road_miles(
            DALLAS.lat, DALLAS.lon, HOUSTON.lat, HOUSTON.lon
        )


class TestKnownCityPairs:
    """TASKS.md G3: known city pairs assert to expected miles."""

    @pytest.mark.parametrize(
        ("label", "a", "b", "expected_hav", "expected_road"),
        KNOWN_PAIRS,
        ids=[p[0] for p in KNOWN_PAIRS],
    )
    def test_known_pair_miles(
        self, label: str, a: Place, b: Place, expected_hav: float, expected_road: float
    ) -> None:
        hav = haversine_between(a, b)
        road = road_miles_between(a, b)
        assert hav == pytest.approx(expected_hav, abs=TOL), label
        assert road == pytest.approx(expected_road, abs=TOL), label
        # The road number is the haversine number x 1.2, visibly:
        assert road == pytest.approx(expected_hav * 1.2, abs=TOL * 1.2), label

    def test_dallas_to_houston_is_271_not_240(self) -> None:
        """DECISIONS.md **D10** — deliberate. Do not "fix" this to 240.

        Downtown-to-downtown great circle is 225.7956 mi; x 1.2 = 270.9547.
        TASKS.md G2 wrote the acceptance number as "Dallas->Houston ~= 240 mi"
        (the real I-45 drive, whose road factor is ~1.06 because the corridor is
        nearly straight). D10 kept ROAD_FACTOR = 1.2 -- a hard invariant -- and
        recorded the ~13% overstatement on straight corridors as a known
        limitation, because bending the factor to hit one pair makes every
        indirect pair worse and silently changes a rule the reviewer can read.
        Everything downstream (fixture distance_miles, $/mi, deadhead) is
        computed from this same function, so the bias cancels.
        """
        hav = haversine_between(DALLAS, HOUSTON)
        road = road_miles_between(DALLAS, HOUSTON)
        assert hav == pytest.approx(225.8, abs=TOL)
        assert road == pytest.approx(271.0, abs=TOL)  # 225.7956 x 1.2
        assert road == hav * ROAD_FACTOR  # the invariant is what produced 271

    def test_intra_metro_pair_is_much_shorter_than_an_inter_metro_pair(self) -> None:
        # Sanity on direction of magnitude: 36.6 mi Dallas->Fort Worth vs
        # 271.0 mi Dallas->Houston. A km/mi mix-up would collapse this ratio.
        assert road_miles_between(DALLAS, FORT_WORTH) < 50.0
        assert road_miles_between(DALLAS, HOUSTON) > 250.0


class TestMetroSeparation:
    @pytest.mark.parametrize(
        ("m1", "m2"),
        [(a, b) for i, a in enumerate(METROS) for b in METROS[i + 1 :]],
    )
    def test_distinct_metro_centers_are_more_than_30_miles_apart(self, m1: str, m2: str) -> None:
        # No two metro clusters are adjacent, so the METRO tier can never be
        # ambiguous between two anchors. Closest pair is SAT <-> AUS:
        # 73.5 haversine x 1.2 = 88.2 road mi.
        d = road_miles_between(METRO_ANCHORS[m1], METRO_ANCHORS[m2])
        assert d > 30.0, f"{m1} <-> {m2} is only {d:.1f} road mi apart"

    def test_every_place_is_nearest_to_its_own_metro_anchor(self) -> None:
        # The clustering invariant PRD section 7 depends on, and a tight
        # typo-catcher: a transposed coordinate almost certainly lands a row
        # nearer some other metro's anchor. TX_OTHER is excluded because it is
        # by construction the "belongs to no big metro" bucket (geo.py), so its
        # members have no anchor to be nearest to.
        big = {m: METRO_ANCHORS[m] for m in (METRO_DFW, METRO_HOU, METRO_SAT, METRO_AUS)}
        wrong = []
        for metro, anchor in big.items():
            for p in places_in_metro(metro):
                nearest = min(big, key=lambda m: road_miles_between(p, big[m]))
                if nearest != metro:
                    wrong.append((str(p), metro, nearest))
        assert wrong == []

    @pytest.mark.parametrize(
        ("metro", "limit"),
        [
            (METRO_DFW, 60.0),  # actual max: Cleburne 76031, 54.3 road mi from Dallas
            (METRO_HOU, 60.0),  # actual max: Conroe 77304, 49.1 road mi from Houston
            (METRO_SAT, 60.0),  # actual max: Seguin 78155, 39.5 road mi from San Antonio
            (METRO_AUS, 60.0),  # actual max: San Marcos 78666, 35.1 road mi from Austin
        ],
    )
    def test_metro_members_are_within_60_road_miles_of_their_anchor(
        self, metro: str, limit: float
    ) -> None:
        anchor = METRO_ANCHORS[metro]
        for p in places_in_metro(metro):
            d = road_miles_between(p, anchor)
            assert d <= limit, f"{p} is {d:.1f} road mi from {anchor} ({metro})"

    def test_tx_other_is_deliberately_not_a_compact_cluster(self) -> None:
        # geo.py: "Triangle corridor towns that belong to no big metro (Waco,
        # Temple, Huntsville...)". It is a residual bucket, so it has no
        # compactness invariant -- Corsicana to Gonzales is 226.0 road mi. This
        # test exists so the exclusion above is visible rather than assumed, and
        # so that turning TX_OTHER into a real cluster fails loudly here.
        towns = places_in_metro(METRO_TX_OTHER)
        diameter = max(
            road_miles_between(a, b) for i, a in enumerate(towns) for b in towns[i + 1 :]
        )
        assert diameter > 200.0
        # Even so, no corridor town sits inside a real metro's radius: the
        # closest is Luling 78648 at 49.5 road mi from San Antonio, against
        # SAT's own radius of 39.5.
        radii = {
            m: max(road_miles_between(p, METRO_ANCHORS[m]) for p in places_in_metro(m))
            for m in (METRO_DFW, METRO_HOU, METRO_SAT, METRO_AUS)
        }
        for town in towns:
            for m, radius in radii.items():
                d = road_miles_between(town, METRO_ANCHORS[m])
                assert d > radius, f"{town} is {d:.1f} mi from {m} anchor (radius {radius:.1f})"


class TestSameMetroCompactness:
    def test_same_metro_pairs_are_under_90_road_miles(self) -> None:
        """Coarse whole-table catch: any two places in one metro within ~90 mi.

        **Why 90 and not 75.** The compactness bound is a *straight-line* figure
        of ~75 mi (see ``test_same_metro_pairs_are_under_75_great_circle_miles``,
        which asserts exactly that). Road miles are that same number x
        ``ROAD_FACTOR``, so the road-mile image of a 75 mi straight-line bound is
        75 x 1.2 = **90**. 90 is a corrected specification, not a tolerance
        loosened to get green: asserting 75 against ``road_miles`` compared a
        straight-line budget to a road-mile measurement, which no correct table
        could satisfy. It failed on the true corners of two real metros --
        McKinney 75071 <-> Cleburne 76031 at 88.0 road mi (Collin and Johnson
        counties, both genuinely DFW) and Conroe 77304 <-> Alvin 77511 at 78.2 --
        with every coordinate involved verified correct.

        **This is the loose net, not the tight one.** A transposed digit is
        caught by ``test_metro_members_are_within_60_road_miles_of_their_anchor``
        and ``test_every_place_is_nearest_to_its_own_metro_anchor``; those are the
        real typo-catchers. This test only rules out a member landing a whole
        metro away.

        TX_OTHER is excluded by construction, not to make this pass: geo.py
        defines it as the bucket for "Triangle corridor towns that belong to no
        big metro", and ``test_tx_other_is_deliberately_not_a_compact_cluster``
        asserts that sprawl positively.
        """
        offenders: list[str] = []
        for metro in (METRO_DFW, METRO_HOU, METRO_SAT, METRO_AUS):
            places = places_in_metro(metro)
            for i, a in enumerate(places):
                for b in places[i + 1 :]:
                    d = road_miles_between(a, b)
                    if d >= 90.0:  # 75 mi straight line x 1.2 road factor
                        offenders.append(f"{metro}: {a} <-> {b} = {d:.1f} road mi")
        assert offenders == [], "\n".join(offenders)

    def test_same_metro_pairs_are_under_75_great_circle_miles(self) -> None:
        # The straight-line bound the 90 mi road bound above is derived from
        # (75 x 1.2 = 90); this is the form the figure was calibrated in.
        # Actual maxima:
        # DFW 73.3 (McKinney <-> Cleburne), HOU 65.2 (Conroe <-> Alvin),
        # SAT 57.3 (Boerne <-> Floresville), AUS 57.2 (San Marcos <-> Taylor).
        offenders: list[str] = []
        for metro in (METRO_DFW, METRO_HOU, METRO_SAT, METRO_AUS):
            places = places_in_metro(metro)
            for i, a in enumerate(places):
                for b in places[i + 1 :]:
                    d = haversine_between(a, b)
                    if d >= 75.0:
                        offenders.append(f"{metro}: {a} <-> {b} = {d:.1f} mi")
        assert offenders == [], "\n".join(offenders)

    def test_no_two_distinct_places_share_coordinates(self) -> None:
        # Two rows at the same lat/lon would make a real lane look like zero
        # miles. Copy-paste catch over all 180 rows.
        coords = [(p.lat, p.lon) for p in ALL_PLACES]
        duplicates = {c for c in coords if coords.count(c) > 1}
        assert duplicates == set()

    def test_every_distinct_pair_has_a_positive_distance(self) -> None:
        assert min(
            road_miles_between(a, b)
            for i, a in enumerate(ALL_PLACES)
            for b in ALL_PLACES[i + 1 :]
        ) > 0.0
