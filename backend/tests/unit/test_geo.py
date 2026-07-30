"""Unit tests for ``app.domain.geo`` — the offline city/zip table (TASKS.md G1/G3).

No database, no network, no file I/O: the table is the whole data source
(CLAUDE.md invariant 7). What these tests defend:

* the Location row of the normalization table — city/state/zip -> lat, lon,
  metro, zip3, and **unmatched -> geo-null (``None``), never an exception**;
* DECISIONS.md **D11** — a recognized city with an unrecognized zip keeps the
  *load's own* zip, so the ZIP3 tier key stays honest (invariant 6);
* the metro clustering PRD section 7 rests on, including the negative cases
  (Waco and Temple are not DFW);
* table-wide consistency, because a single hand-typed ``zip3`` or a transposed
  coordinate in 180 rows would corrupt a tier or a lane distance silently.
"""

from __future__ import annotations

import dataclasses

import pytest

from app.domain.geo import (
    ALL_PLACES,
    METRO_AUS,
    METRO_DFW,
    METRO_HOU,
    METRO_SAT,
    METRO_TX_OTHER,
    METROS,
    REGION,
    Place,
    all_cities,
    lookup_city,
    lookup_zip,
    places_in_metro,
    resolve_place,
)


class TestLookupZip:
    """Zip is the authoritative key; it is also the ZIP3 tier key."""

    def test_exact_zip_hit(self) -> None:
        p = lookup_zip("77002")
        assert p is not None
        assert (p.city, p.state, p.zip) == ("Houston", "TX", "77002")
        assert p.metro == METRO_HOU
        assert p.zip3 == "770"  # "77002"[:3]
        assert (p.lat, p.lon) == (29.7563, -95.3648)  # downtown Houston centroid

    def test_surrounding_whitespace_tolerated(self) -> None:
        # TMS payloads are hand-shaped strings; " 77449 " is the Katy row.
        assert lookup_zip(" 77449 ") is lookup_zip("77449")

    def test_zip_plus_four_tolerated(self) -> None:
        # "77449-1234" -> "77449": zip+4 must not geo-null a known stop.
        assert lookup_zip("77449-1234") is lookup_zip("77449")
        assert lookup_zip("77449-1234").city == "Katy"

    @pytest.mark.parametrize(
        "bad",
        [
            None,
            "",
            "   ",
            "99999",  # not in the table
            "7744",  # four digits
            "774490",  # six digits
            "ABCDE",
            "774A9",
            "79901",  # El Paso: real zip, deliberately outside the Triangle
        ],
    )
    def test_unmatched_zip_is_none_not_an_exception(self, bad: str | None) -> None:
        assert lookup_zip(bad) is None


class TestLookupCity:
    """TMS A emits cities UPPERCASE, TMS B and C mixed — all must resolve."""

    def test_case_insensitive_both_ways(self) -> None:
        upper = lookup_city("GRAND PRAIRIE", "TX")
        lower = lookup_city("grand prairie", "tx")
        title = lookup_city("Grand Prairie", "TX")
        assert upper is not None
        # Identical row, not merely an equal copy.
        assert upper is lower is title
        assert (upper.city, upper.zip, upper.metro) == ("Grand Prairie", "75050", METRO_DFW)

    def test_whitespace_and_mixed_case_state(self) -> None:
        assert lookup_city(" dallas ", " Tx ") is lookup_zip("75201")

    @pytest.mark.parametrize(
        ("city", "expected_zip"),
        [
            ("Dallas", "75201"),
            ("Houston", "77002"),
            ("Fort Worth", "76102"),
            ("San Antonio", "78205"),
            ("Austin", "78701"),
            ("Waco", "76701"),
        ],
    )
    def test_multi_zip_city_returns_the_first_listed_central_zip(
        self, city: str, expected_zip: str
    ) -> None:
        p = lookup_city(city, "TX")
        assert p is not None
        assert p.zip == expected_zip

    @pytest.mark.parametrize(
        ("city", "state"),
        [
            (None, "TX"),
            ("Houston", None),
            (None, None),
            ("", "TX"),
            ("Houston", ""),
            ("", ""),
            ("Nowheresville", "TX"),
            ("El Paso", "TX"),  # real Texas city, outside the Triangle table
            ("Houston", "MO"),  # right city name, wrong state
        ],
    )
    def test_unmatched_city_is_none_not_an_exception(
        self, city: str | None, state: str | None
    ) -> None:
        assert lookup_city(city, state) is None

    def test_all_cities_are_all_resolvable_and_distinct(self) -> None:
        cities = all_cities()
        assert len(cities) == len(set(cities))  # no duplicate (city, state)
        for city, state in cities:
            assert lookup_city(city, state) is not None, f"{city}, {state} does not resolve"
        # Canonical case round-trips: the generator picks from all_cities().
        for city, state in cities:
            assert lookup_city(city, state).city == city


class TestPlaceInvariants:
    def test_zip3_is_zip_prefix_for_every_row(self) -> None:
        # Asserted over the whole table, not a sample: zip3 is the ZIP3 tier key,
        # so one hand-typed inconsistency would corrupt a tier silently.
        mismatched = [(p.zip, p.zip3) for p in ALL_PLACES if p.zip3 != p.zip[:3]]
        assert mismatched == []
        assert all(len(p.zip) == 5 and p.zip.isdigit() for p in ALL_PLACES)

    def test_no_duplicate_zip_in_the_table(self) -> None:
        # A duplicate zip would give one zip two metros, silently.
        zips = [p.zip for p in ALL_PLACES]
        assert len(zips) == len(set(zips))

    def test_place_is_immutable(self) -> None:
        p = lookup_zip("75201")
        with pytest.raises(dataclasses.FrozenInstanceError):
            p.city = "Houston"  # type: ignore[misc]
        with pytest.raises(dataclasses.FrozenInstanceError):
            p.zip = "77002"  # type: ignore[misc]
        with pytest.raises(dataclasses.FrozenInstanceError):
            p.zip3 = "999"  # type: ignore[misc]
        assert (p.city, p.zip, p.zip3) == ("Dallas", "75201", "752")

    def test_place_derives_zip3_on_construction(self) -> None:
        p = Place(city="Nowhere", state="TX", zip="12345", lat=1.0, lon=2.0, metro=METRO_DFW)
        assert p.zip3 == "123"  # "12345"[:3], never passed in

    def test_place_is_hashable(self) -> None:
        assert len(set(ALL_PLACES)) == len(ALL_PLACES)

    def test_str_is_the_displayable_raw_location(self) -> None:
        # Geo-null loads still display their location, so __str__ matters.
        assert str(lookup_zip("77002")) == "Houston, TX 77002"


class TestResolvePlaceD11:
    """DECISIONS.md D11 — an unrecognized zip on a recognized city keeps its own zip3."""

    def test_exact_row_when_the_zip_is_in_the_table(self) -> None:
        p = resolve_place("Houston", "TX", "77099")
        assert p is lookup_zip("77099")
        assert (p.city, p.zip, p.zip3, p.metro) == ("Houston", "77099", "770", METRO_HOU)

    def test_unknown_zip_on_known_city_keeps_the_loads_own_zip(self) -> None:
        canonical = lookup_zip("77002")  # Houston's first-listed row
        p = resolve_place("Houston", "TX", "99999")
        assert p is not None
        # D11: coordinates and metro come from the city (best offline approximation)...
        assert (p.city, p.state) == ("Houston", "TX")
        assert (p.lat, p.lon) == (canonical.lat, canonical.lon)
        assert p.metro == METRO_HOU
        # ...but the zip is not an approximation — it arrived in the data.
        assert p.zip == "99999"
        assert p.zip3 == "999"  # NOT "770": the ZIP3 tier key must not be substituted
        assert p.zip3 != canonical.zip3

    def test_unknown_zip_plus_four_is_normalized_before_being_carried(self) -> None:
        p = resolve_place("Katy", "TX", " 99988-4321 ")
        assert p is not None
        assert (p.zip, p.zip3, p.metro) == ("99988", "999", METRO_HOU)

    def test_synthesized_place_never_enters_the_table_or_any_index(self) -> None:
        before = len(ALL_PLACES)
        synth = resolve_place("Houston", "TX", "99999")
        assert synth not in ALL_PLACES
        assert len(ALL_PLACES) == before  # the table is a constant, not a cache
        assert lookup_zip("99999") is None  # the zip index was not polluted
        assert lookup_city("Houston", "TX") is lookup_zip("77002")  # nor the city index
        assert places_in_metro(METRO_HOU) == tuple(p for p in ALL_PLACES if p.metro == METRO_HOU)

    @pytest.mark.parametrize("zip_code", [None, "", "   ", "abc", "1234", "7700A"])
    def test_no_or_unparsable_zip_returns_the_canonical_row(self, zip_code: str | None) -> None:
        # Nothing usable arrived, so there is no honest zip to carry: use the table's.
        assert resolve_place("Houston", "TX", zip_code) is lookup_zip("77002")

    def test_zip_wins_over_a_disagreeing_city(self) -> None:
        # Zip is authoritative; a mismatched city name does not override it.
        p = resolve_place("Dallas", "TX", "77002")
        assert p is lookup_zip("77002")
        assert p.metro == METRO_HOU

    def test_zip_only_resolves(self) -> None:
        assert resolve_place(zip_code="75050") is lookup_zip("75050")

    def test_city_only_resolves(self) -> None:
        assert resolve_place(city="Katy", state="TX") is lookup_zip("77449")

    @pytest.mark.parametrize(
        ("city", "state", "zip_code"),
        [
            (None, None, None),
            ("", "", ""),
            ("El Paso", "TX", "79901"),  # nothing in the Triangle table matches
            ("Nowheresville", "TX", "99999"),
            (None, None, "99999"),  # unknown zip, no city to fall back to
            ("Houston", None, "99999"),  # no state, so no city fallback
        ],
    )
    def test_unmatched_is_geo_null(
        self, city: str | None, state: str | None, zip_code: str | None
    ) -> None:
        # Geo-null: excluded from lane stats, still displayed. Never raises,
        # never guesses a nearby city.
        assert resolve_place(city, state, zip_code) is None


class TestMetroClustering:
    """PRD section 7's metro tier is exactly this mapping (plus its negatives)."""

    @pytest.mark.parametrize(
        "city",
        ["Grand Prairie", "Fort Worth", "Irving", "Dallas", "Arlington", "Plano", "Denton"],
    )
    def test_dfw_members(self, city: str) -> None:
        assert lookup_city(city, "TX").metro == METRO_DFW

    @pytest.mark.parametrize(
        "city", ["Katy", "Houston", "Sugar Land", "Pasadena", "Baytown", "Conroe"]
    )
    def test_hou_members(self, city: str) -> None:
        assert lookup_city(city, "TX").metro == METRO_HOU

    @pytest.mark.parametrize("city", ["New Braunfels", "Schertz", "Seguin", "San Antonio"])
    def test_sat_members(self, city: str) -> None:
        assert lookup_city(city, "TX").metro == METRO_SAT

    @pytest.mark.parametrize(("city", "zip_code"), [("Waco", "76701"), ("Temple", "76501")])
    def test_waco_and_temple_are_not_dfw(self, city: str, zip_code: str) -> None:
        # The negative case is what keeps a ~200-mile lane out of a metro cluster:
        # Waco -> Dallas is 104.5 road mi, so collapsing it into DFW would make a
        # long-haul lane look like a local one.
        p = lookup_zip(zip_code)
        assert p.city == city
        assert p.metro != METRO_DFW
        assert p.metro == METRO_TX_OTHER
        assert lookup_city(city, "TX").metro == METRO_TX_OTHER

    def test_every_waco_and_temple_zip_is_tx_other(self) -> None:
        for p in ALL_PLACES:
            if p.city in {"Waco", "Temple"}:
                assert p.metro == METRO_TX_OTHER, p

    def test_places_in_metro_matches_the_metro_field(self) -> None:
        for metro in METROS:
            assert places_in_metro(metro) == tuple(p for p in ALL_PLACES if p.metro == metro)
            assert all(p.metro == metro for p in places_in_metro(metro))
            assert places_in_metro(metro), f"{metro} is empty"

    def test_metro_totals_sum_to_the_whole_table(self) -> None:
        # 56 DFW + 47 HOU + 29 SAT + 25 AUS + 23 TX_OTHER = 180 rows, no row
        # orphaned into a metro that is not in METROS.
        counts = {m: len(places_in_metro(m)) for m in METROS}
        assert sum(counts.values()) == len(ALL_PLACES) == 180
        assert counts == {
            METRO_DFW: 56,
            METRO_HOU: 47,
            METRO_SAT: 29,
            METRO_AUS: 25,
            METRO_TX_OTHER: 23,
        }

    def test_places_in_metro_of_an_unknown_metro_is_empty(self) -> None:
        assert places_in_metro("CHICAGOLAND") == ()
        assert places_in_metro("") == ()

    def test_every_metro_value_is_declared(self) -> None:
        assert {p.metro for p in ALL_PLACES} == set(METROS)

    def test_every_place_is_in_the_one_region(self) -> None:
        # Tier 3 is a single region, so REGION-tier stats cover the whole table.
        assert REGION == "TX_TRIANGLE"
        assert {p.region for p in ALL_PLACES} == {"TX_TRIANGLE"}


class TestTableCoordinateSanity:
    def test_every_place_is_inside_a_texas_bounding_box(self) -> None:
        # Catches a transposed digit or a dropped minus sign: lat 28.0-34.0,
        # lon -100.0 to -93.5. Actual extent: lat 29.1336 (Floresville) to
        # 33.2153 (McKinney), lon -98.732 (Boerne) to -94.966 (Baytown).
        outside = [
            p
            for p in ALL_PLACES
            if not (28.0 <= p.lat <= 34.0 and -100.0 <= p.lon <= -93.5)
        ]
        assert outside == []

    def test_longitude_is_negative_everywhere(self) -> None:
        # A dropped minus sign puts a Texas city in China without failing a
        # loose bounding box on latitude alone.
        assert all(p.lon < 0 for p in ALL_PLACES)
