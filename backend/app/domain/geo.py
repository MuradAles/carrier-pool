"""Offline geography for the Texas Triangle (CLAUDE.md invariant 7).

A hardcoded city/zip table mapping a stop's ``city``/``state``/``zip`` to
latitude, longitude, metro cluster, and zip3. No network, no file reads, no
import-time I/O — the table below is the whole data source.

Two things depend on getting this right:

* **Lane tiers** (PRD section 7). The metro on each row is what collapses
  Grand Prairie -> Katy and Fort Worth -> Houston into the same ``DFW -> HOU``
  metro lane, and what keeps Waco out of ``DFW``. Metro is a property of the
  zip, not of the city name.
* **Distance**. Coordinates feed ``distance.road_miles``, so they are real
  centroids, accurate to roughly 0.01-0.05 degrees.

An unmatched location is **geo-null**: the lookups return ``None`` rather than
raising or guessing. Callers keep displaying the raw city/state/zip and exclude
the load from lane stats (normalization table, Location row).
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "Place",
    "REGION",
    "METRO_DFW",
    "METRO_HOU",
    "METRO_SAT",
    "METRO_AUS",
    "METRO_TX_OTHER",
    "METROS",
    "ALL_PLACES",
    "lookup_zip",
    "lookup_city",
    "resolve_place",
    "places_in_metro",
    "all_cities",
]

# Tier-3 key: the whole table is one region (PRD section 7).
REGION = "TX_TRIANGLE"

METRO_DFW = "DFW"
METRO_HOU = "HOU"
METRO_SAT = "SAT"
METRO_AUS = "AUS"
# Triangle corridor towns that belong to no big metro (Waco, Temple, Huntsville...).
METRO_TX_OTHER = "TX_OTHER"

METROS: tuple[str, ...] = (METRO_DFW, METRO_HOU, METRO_SAT, METRO_AUS, METRO_TX_OTHER)


@dataclass(frozen=True, slots=True)
class Place:
    """One row of the geo table. Immutable and hashable.

    ``zip3`` is always derived from ``zip`` — never passed in, so it cannot be
    typed inconsistently.
    """

    city: str
    state: str
    zip: str
    lat: float
    lon: float
    metro: str
    zip3: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "zip3", self.zip[:3])

    @property
    def region(self) -> str:
        return REGION

    def __str__(self) -> str:
        return f"{self.city}, {self.state} {self.zip}"


# --------------------------------------------------------------------------
# The table. Grouped by metro so a row's cluster comes from its group, not from
# a per-row string somebody can typo. Within a city, the first zip listed is
# the one the (city, state) fallback returns, so list the central zip first.
# --------------------------------------------------------------------------

# (city, state, zip, lat, lon)
_Row = tuple[str, str, str, float, float]

_DFW: tuple[_Row, ...] = (
    ("Dallas", "TX", "75201", 32.7831, -96.8067),
    ("Dallas", "TX", "75207", 32.7860, -96.8280),
    ("Dallas", "TX", "75212", 32.7757, -96.8686),
    ("Dallas", "TX", "75217", 32.7146, -96.6800),
    ("Dallas", "TX", "75220", 32.8617, -96.8686),
    ("Dallas", "TX", "75228", 32.8221, -96.6822),
    ("Dallas", "TX", "75235", 32.8283, -96.8394),
    ("Dallas", "TX", "75241", 32.6704, -96.7767),
    ("Dallas", "TX", "75243", 32.9124, -96.7300),
    ("Fort Worth", "TX", "76102", 32.7534, -97.3300),
    ("Fort Worth", "TX", "76106", 32.8072, -97.3520),
    ("Fort Worth", "TX", "76137", 32.8600, -97.2900),
    ("Fort Worth", "TX", "76140", 32.6300, -97.2800),
    ("Fort Worth", "TX", "76155", 32.8267, -97.0450),
    ("Fort Worth", "TX", "76177", 32.9500, -97.3200),
    ("Arlington", "TX", "76010", 32.7290, -97.0836),
    ("Arlington", "TX", "76011", 32.7583, -97.0803),
    ("Arlington", "TX", "76018", 32.6660, -97.0867),
    ("Grand Prairie", "TX", "75050", 32.7663, -97.0072),
    ("Grand Prairie", "TX", "75051", 32.7154, -97.0117),
    ("Irving", "TX", "75061", 32.8177, -96.9557),
    ("Irving", "TX", "75038", 32.8757, -96.9525),
    ("Irving", "TX", "75063", 32.9068, -96.9700),
    ("Plano", "TX", "75074", 33.0210, -96.6699),
    ("Plano", "TX", "75075", 33.0140, -96.7350),
    ("Plano", "TX", "75024", 33.0762, -96.7994),
    ("Garland", "TX", "75040", 32.9315, -96.6156),
    ("Garland", "TX", "75041", 32.8829, -96.6503),
    ("Mesquite", "TX", "75149", 32.7668, -96.5992),
    ("Mesquite", "TX", "75150", 32.8148, -96.6300),
    ("Denton", "TX", "76201", 33.2148, -97.1331),
    ("Denton", "TX", "76205", 33.1900, -97.1200),
    ("Denton", "TX", "76210", 33.1600, -97.0800),
    ("Frisco", "TX", "75034", 33.1507, -96.8236),
    ("McKinney", "TX", "75071", 33.2153, -96.6600),
    ("Richardson", "TX", "75080", 32.9718, -96.7455),
    ("Carrollton", "TX", "75006", 32.9537, -96.8903),
    ("Lewisville", "TX", "75067", 33.0362, -97.0122),
    ("Flower Mound", "TX", "75028", 33.0234, -97.0836),
    ("Grapevine", "TX", "76051", 32.9343, -97.0781),
    ("Euless", "TX", "76040", 32.8371, -97.0819),
    ("Hurst", "TX", "76053", 32.8235, -97.1706),
    ("North Richland Hills", "TX", "76180", 32.8607, -97.2189),
    ("Keller", "TX", "76248", 32.9346, -97.2289),
    ("Coppell", "TX", "75019", 32.9546, -96.9900),
    ("Farmers Branch", "TX", "75234", 32.9268, -96.8916),
    ("Rockwall", "TX", "75087", 32.9312, -96.4597),
    ("Cedar Hill", "TX", "75104", 32.5885, -96.9561),
    ("DeSoto", "TX", "75115", 32.5896, -96.8570),
    ("Duncanville", "TX", "75116", 32.6518, -96.9083),
    ("Lancaster", "TX", "75134", 32.5921, -96.7561),
    ("Waxahachie", "TX", "75165", 32.3865, -96.8484),
    ("Midlothian", "TX", "76065", 32.4823, -96.9944),
    ("Mansfield", "TX", "76063", 32.5632, -97.1417),
    ("Burleson", "TX", "76028", 32.5421, -97.3208),
    ("Cleburne", "TX", "76031", 32.3476, -97.3867),
)

_HOU: tuple[_Row, ...] = (
    ("Houston", "TX", "77002", 29.7563, -95.3648),
    ("Houston", "TX", "77008", 29.7996, -95.4139),
    ("Houston", "TX", "77015", 29.7826, -95.1817),
    ("Houston", "TX", "77020", 29.7757, -95.3153),
    ("Houston", "TX", "77029", 29.7712, -95.2413),
    ("Houston", "TX", "77032", 29.9455, -95.3352),
    ("Houston", "TX", "77041", 29.8676, -95.5651),
    ("Houston", "TX", "77049", 29.8221, -95.1524),
    ("Houston", "TX", "77060", 29.9358, -95.4142),
    ("Houston", "TX", "77064", 29.9210, -95.5486),
    ("Houston", "TX", "77084", 29.8306, -95.6564),
    ("Houston", "TX", "77094", 29.7690, -95.6900),
    ("Houston", "TX", "77099", 29.6674, -95.5813),
    ("Katy", "TX", "77449", 29.8320, -95.7370),
    ("Katy", "TX", "77450", 29.7513, -95.7419),
    ("Katy", "TX", "77494", 29.7420, -95.8330),
    ("Pasadena", "TX", "77502", 29.6899, -95.1978),
    ("Pasadena", "TX", "77503", 29.7071, -95.1697),
    ("Pasadena", "TX", "77505", 29.6470, -95.1520),
    ("Sugar Land", "TX", "77478", 29.6236, -95.6180),
    ("Sugar Land", "TX", "77479", 29.5766, -95.6383),
    ("Baytown", "TX", "77520", 29.7457, -94.9660),
    ("Baytown", "TX", "77521", 29.7960, -94.9660),
    ("Conroe", "TX", "77301", 30.3050, -95.4416),
    ("Conroe", "TX", "77304", 30.3350, -95.5100),
    ("The Woodlands", "TX", "77380", 30.1420, -95.4600),
    ("The Woodlands", "TX", "77381", 30.1740, -95.5060),
    ("Spring", "TX", "77373", 30.0530, -95.3820),
    ("Tomball", "TX", "77375", 30.0900, -95.6180),
    ("Cypress", "TX", "77429", 29.9880, -95.6690),
    ("Cypress", "TX", "77433", 29.9200, -95.7350),
    ("Humble", "TX", "77338", 29.9970, -95.2620),
    ("Kingwood", "TX", "77339", 30.0530, -95.1810),
    ("Pearland", "TX", "77581", 29.5580, -95.2860),
    ("Pearland", "TX", "77584", 29.5470, -95.3760),
    ("Friendswood", "TX", "77546", 29.5090, -95.1900),
    ("League City", "TX", "77573", 29.4900, -95.0930),
    ("La Porte", "TX", "77571", 29.6650, -95.0200),
    ("Deer Park", "TX", "77536", 29.6900, -95.1230),
    ("Channelview", "TX", "77530", 29.7770, -95.1150),
    ("Stafford", "TX", "77477", 29.6230, -95.5680),
    ("Missouri City", "TX", "77459", 29.5350, -95.5400),
    ("Rosenberg", "TX", "77471", 29.5560, -95.8080),
    ("Richmond", "TX", "77469", 29.5650, -95.7300),
    ("Alvin", "TX", "77511", 29.4200, -95.2450),
    ("Brookshire", "TX", "77423", 29.7900, -95.9500),
    ("Magnolia", "TX", "77354", 30.2100, -95.6900),
)

_SAT: tuple[_Row, ...] = (
    ("San Antonio", "TX", "78205", 29.4246, -98.4861),
    ("San Antonio", "TX", "78201", 29.4670, -98.5330),
    ("San Antonio", "TX", "78207", 29.4230, -98.5290),
    ("San Antonio", "TX", "78210", 29.3990, -98.4640),
    ("San Antonio", "TX", "78216", 29.5290, -98.4880),
    ("San Antonio", "TX", "78218", 29.4930, -98.3960),
    ("San Antonio", "TX", "78219", 29.4400, -98.3760),
    ("San Antonio", "TX", "78221", 29.3290, -98.4930),
    ("San Antonio", "TX", "78223", 29.3480, -98.4180),
    ("San Antonio", "TX", "78227", 29.4030, -98.6290),
    ("San Antonio", "TX", "78229", 29.5030, -98.5720),
    ("San Antonio", "TX", "78233", 29.5580, -98.3620),
    ("San Antonio", "TX", "78240", 29.5200, -98.5990),
    ("San Antonio", "TX", "78245", 29.4180, -98.7110),
    ("San Antonio", "TX", "78247", 29.5820, -98.4110),
    ("San Antonio", "TX", "78249", 29.5580, -98.6180),
    ("San Antonio", "TX", "78251", 29.4680, -98.6800),
    ("San Antonio", "TX", "78258", 29.6300, -98.4890),
    ("Schertz", "TX", "78154", 29.5522, -98.2697),
    ("Cibolo", "TX", "78108", 29.5600, -98.2270),
    ("Universal City", "TX", "78148", 29.5480, -98.2900),
    ("Converse", "TX", "78109", 29.5180, -98.3160),
    ("New Braunfels", "TX", "78130", 29.7030, -98.1245),
    ("New Braunfels", "TX", "78132", 29.7440, -98.1830),
    ("Seguin", "TX", "78155", 29.5688, -97.9647),
    ("Boerne", "TX", "78006", 29.7947, -98.7320),
    ("Helotes", "TX", "78023", 29.5780, -98.6900),
    ("Floresville", "TX", "78114", 29.1336, -98.1561),
    ("Bulverde", "TX", "78163", 29.7440, -98.4550),
)

_AUS: tuple[_Row, ...] = (
    ("Austin", "TX", "78701", 30.2711, -97.7437),
    ("Austin", "TX", "78702", 30.2620, -97.7150),
    ("Austin", "TX", "78704", 30.2440, -97.7650),
    ("Austin", "TX", "78721", 30.2700, -97.6870),
    ("Austin", "TX", "78723", 30.3040, -97.6810),
    ("Austin", "TX", "78724", 30.2890, -97.6180),
    ("Austin", "TX", "78741", 30.2290, -97.7130),
    ("Austin", "TX", "78744", 30.1820, -97.7400),
    ("Austin", "TX", "78745", 30.2080, -97.7930),
    ("Austin", "TX", "78753", 30.3820, -97.6730),
    ("Austin", "TX", "78758", 30.3760, -97.7130),
    ("Round Rock", "TX", "78664", 30.5060, -97.6630),
    ("Round Rock", "TX", "78681", 30.5230, -97.7180),
    ("Georgetown", "TX", "78626", 30.6330, -97.6640),
    ("Pflugerville", "TX", "78660", 30.4400, -97.6200),
    ("Cedar Park", "TX", "78613", 30.5090, -97.8200),
    ("Leander", "TX", "78641", 30.5730, -97.8580),
    ("Kyle", "TX", "78640", 29.9890, -97.8770),
    ("Buda", "TX", "78610", 30.0850, -97.8410),
    ("San Marcos", "TX", "78666", 29.8833, -97.9414),
    ("Bastrop", "TX", "78602", 30.1100, -97.3150),
    ("Elgin", "TX", "78621", 30.3500, -97.3700),
    ("Lockhart", "TX", "78644", 29.8830, -97.6700),
    ("Taylor", "TX", "76574", 30.5710, -97.4090),
    ("Hutto", "TX", "78634", 30.5430, -97.5460),
)

_TX_OTHER: tuple[_Row, ...] = (
    ("Waco", "TX", "76701", 31.5540, -97.1350),
    ("Waco", "TX", "76705", 31.6280, -97.0790),
    ("Waco", "TX", "76710", 31.5300, -97.1900),
    ("Waco", "TX", "76712", 31.5090, -97.2400),
    ("Temple", "TX", "76501", 31.0982, -97.3428),
    ("Temple", "TX", "76504", 31.1120, -97.3720),
    ("Belton", "TX", "76513", 31.0560, -97.4640),
    ("Killeen", "TX", "76541", 31.1170, -97.7280),
    ("Corsicana", "TX", "75110", 32.0954, -96.4688),
    ("Hillsboro", "TX", "76645", 32.0110, -97.1300),
    ("West", "TX", "76691", 31.8030, -97.0910),
    ("Huntsville", "TX", "77340", 30.7235, -95.5508),
    ("Bryan", "TX", "77803", 30.6700, -96.3760),
    ("College Station", "TX", "77840", 30.6100, -96.3400),
    ("Navasota", "TX", "77868", 30.3870, -96.0870),
    ("Brenham", "TX", "77833", 30.1669, -96.3977),
    ("Giddings", "TX", "78942", 30.1830, -96.9360),
    ("La Grange", "TX", "78945", 29.9050, -96.8760),
    ("Columbus", "TX", "78934", 29.7060, -96.5400),
    ("Schulenburg", "TX", "78956", 29.6820, -96.9050),
    ("Flatonia", "TX", "78941", 29.6870, -97.1090),
    ("Luling", "TX", "78648", 29.6800, -97.6470),
    ("Gonzales", "TX", "78629", 29.5030, -97.4500),
)

_GROUPS: tuple[tuple[str, tuple[_Row, ...]], ...] = (
    (METRO_DFW, _DFW),
    (METRO_HOU, _HOU),
    (METRO_SAT, _SAT),
    (METRO_AUS, _AUS),
    (METRO_TX_OTHER, _TX_OTHER),
)


def _build() -> tuple[Place, ...]:
    places: list[Place] = []
    seen: dict[str, str] = {}
    for metro, rows in _GROUPS:
        for city, state, zip_code, lat, lon in rows:
            if zip_code in seen:
                # A duplicate zip would silently give one city two metros.
                raise ValueError(f"duplicate zip {zip_code}: {seen[zip_code]} and {city}")
            seen[zip_code] = city
            places.append(Place(city=city, state=state, zip=zip_code, lat=lat, lon=lon, metro=metro))
    return tuple(places)


ALL_PLACES: tuple[Place, ...] = _build()

_BY_ZIP: dict[str, Place] = {p.zip: p for p in ALL_PLACES}

# (CITY, STATE) -> the first-listed zip for that city, used by the fallback.
_BY_CITY: dict[tuple[str, str], Place] = {}
for _p in ALL_PLACES:
    _BY_CITY.setdefault((_p.city.upper(), _p.state.upper()), _p)


def _clean_zip(zip_code: str | None) -> str | None:
    """``'77478-1234'`` / ``' 77478 '`` -> ``'77478'``. Anything else -> ``None``."""
    if not zip_code:
        return None
    head = str(zip_code).strip().split("-", 1)[0]
    return head if len(head) == 5 and head.isdigit() else None


def lookup_zip(zip_code: str | None) -> Place | None:
    """Exact 5-digit zip lookup. ``None`` when the zip is not in the table."""
    cleaned = _clean_zip(zip_code)
    return _BY_ZIP.get(cleaned) if cleaned else None


def lookup_city(city: str | None, state: str | None) -> Place | None:
    """Case-insensitive ``(city, state)`` lookup.

    TMS A emits cities UPPERCASE, TMS B and C mixed case; all resolve here. For
    a multi-zip city this returns the first zip listed in the table (the central
    one), so a caller who had no zip gets that city's representative zip3.
    """
    if not city or not state:
        return None
    return _BY_CITY.get((city.strip().upper(), state.strip().upper()))


def resolve_place(
    city: str | None = None,
    state: str | None = None,
    zip_code: str | None = None,
) -> Place | None:
    """Resolve a stop to a ``Place``. Zip is authoritative; city/state is the fallback.

    A known city whose zip is *not* in the table keeps the load's own zip
    (DECISIONS.md D11): the returned ``Place`` carries the city's canonical
    ``city``/``state``/``lat``/``lon``/``metro`` but the supplied zip, so ``zip3``
    derives from what the load actually said rather than from the table's
    representative zip. Coordinates are an approximation either way; the zip is
    not, and a substituted ZIP3 tier key would make the tier report untrue
    (invariant 6). The supplied zip is normalized exactly as ``lookup_zip`` does;
    if it is not a usable 5-digit zip the canonical row is returned unchanged, as
    it also is when no zip was supplied. Returns ``None`` (geo-null) when neither
    zip nor city matches — never raises, never guesses a nearby city.

    The synthesized ``Place`` is a return value only: ``ALL_PLACES`` stays the
    canonical table.
    """
    exact = lookup_zip(zip_code)
    if exact is not None:
        return exact

    canonical = lookup_city(city, state)
    if canonical is None:
        return None

    cleaned = _clean_zip(zip_code)
    if cleaned is None:
        return canonical

    return Place(
        city=canonical.city,
        state=canonical.state,
        zip=cleaned,
        lat=canonical.lat,
        lon=canonical.lon,
        metro=canonical.metro,
    )


def places_in_metro(metro: str) -> tuple[Place, ...]:
    """Every place in a metro cluster, in table order. Empty tuple if unknown."""
    return tuple(p for p in ALL_PLACES if p.metro == metro)


def all_cities() -> tuple[tuple[str, str], ...]:
    """Distinct ``(city, state)`` pairs in canonical case, table order.

    For generators that pick cities: everything here resolves via ``lookup_city``.
    """
    return tuple(dict.fromkeys((p.city, p.state) for p in ALL_PLACES))
