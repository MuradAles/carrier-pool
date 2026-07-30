#!/usr/bin/env python3
"""Deterministic generator for the 132 TMS sync fixtures (TASKS.md DG1-DG9).

Run
---
    python3 backend/scripts/generate_data.py            # generate + validate
    python3 backend/scripts/generate_data.py --validate-only
    python3 backend/scripts/generate_data.py --out /tmp/x  # write elsewhere

The data is **test fixtures for our own system**, not noise. Every behavior the
platform claims has data proving it, and every day-11 answer is hand-traceable
back to the loads that caused it via ``data/TRACEABILITY.md``, which this script
computes from the emitted fixtures (never hand-written).

Determinism
-----------
One seeded ``random.Random`` per broker, derived from ``SEED``. No module-level
``random``, no wall clock, no iteration over unordered containers, fixed JSON
``indent`` and insertion-ordered keys. Same seed -> byte-identical output.

=============================================================================
BUDGET ARITHMETIC (DECISIONS.md D1 + D9) -- resolved before generating
=============================================================================
Per broker: 4 syncs/day x 11 days = 44 files.
  44 files - 4 reserved for day 11          = 40 history files
  40 history files x <=3 loads              = 120 load-APPEARANCES ceiling

Spend:
  1 full-lifecycle load  x 6 appearances    =   6   (scenario 1)
  2 correction loads     x 2 appearances    =   4   (scenario 2; TMS B's
                                                     rate-only correction costs
                                                     only 1 loads-array slot,
                                                     so B spends 3)
  5 deliberately empty syncs x 3 forgone    = -15
  --------------------------------------------------
  appearances left for single-appearance backfill:
      120 - 15 (empty) - 6 (lifecycle) - 4 (corrections) = 95 ceiling

We spend 90 of that 95, leaving deliberate slack so several files carry 1-2
loads instead of always 3:

      90 single-appearance COMPLETED backfill loads x 1 =  90
    +  1 lifecycle load                             x 6 =   6
    +  2 correction loads                           x 2 =   4
      --------------------------------------------------------
      93 distinct history loads / 100 loads-array entries
      (into the 35 non-empty history files, whose ceiling is 105)

    TMS B emits 99 entries, not 100: its second correction is rate-only, so
    the load is NOT in that file's `loads` array -- only in `rates`.

So: **93 distinct history loads per broker**, matching D9's ~93. Of those, 3
carry the multi-appearance budget (1 lifecycle + 2 corrections) and 90 arrive
once, already COMPLETED -- the realistic backfill D1 authorizes.

Day 11: 5 loads per broker across 4 slots (one slot deliberately empty), each
planted to demonstrate exactly one behavior.
=============================================================================
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.domain.distance import road_miles_between  # noqa: E402
from app.domain.geo import METRO_TX_OTHER, REGION, Place, lookup_zip, resolve_place  # noqa: E402

# ---------------------------------------------------------------------------
# Grid constants
# ---------------------------------------------------------------------------

SEED = 20260716

DAY_ONE = date(2026, 7, 6)
N_DAYS = 11                       # 2026-07-06 .. 2026-07-16
SYNC_HOURS = (0, 6, 12, 18)
HISTORY_SLOTS = 40                # slots 0..39  = days 1..10
DAY11_SLOTS = (40, 41, 42, 43)    # 2026-07-16
TOTAL_SLOTS = HISTORY_SLOTS + len(DAY11_SLOTS)   # 44 files per broker

# July in Texas is CDT = UTC-5 (never -6). The trap CLAUDE.md calls out.
CENTRAL_OFFSET_HOURS = 5
ISO_OFFSET = "-05:00"

DRY_VAN, REEFER, FLATBED, UNKNOWN = "DRY_VAN", "REEFER", "FLATBED", "UNKNOWN"

PLANNED, ACTIVE, COVERED, IN_TRANSIT, DELIVERED, COMPLETED = (
    "PLANNED", "ACTIVE", "COVERED", "IN_TRANSIT", "DELIVERED", "COMPLETED",
)
STATUS_ORDER = {PLANNED: 0, ACTIVE: 1, COVERED: 2, IN_TRANSIT: 3, DELIVERED: 4, COMPLETED: 5}

# 5 empty history syncs per broker. Not laziness: a sync that saw no change is
# a valid, realistic envelope, and the ingester must cope with it.
EMPTY_HISTORY_SLOTS = (2, 15, 23, 29, 35)
EMPTY_DAY11_SLOT = 43

MIN_TIER_LOADS = 5                # PRD section 7
SHRINK_K = 5                      # PRD section 8 / DECISIONS D5

TIER_ZIP3, TIER_METRO, TIER_REGION, TIER_REGION_ANY = "ZIP3", "METRO", "REGION", "REGION_ANY"


def slot_local(slot: int) -> datetime:
    """Slot index -> naive US Central datetime. Filenames are local Central."""
    return datetime.combine(
        DAY_ONE + timedelta(days=slot // 4), datetime.min.time()
    ) + timedelta(hours=SYNC_HOURS[slot % 4])


def slot_filename(slot: int) -> str:
    dt = slot_local(slot)
    return f"{dt:%Y-%m-%d}T{dt:%H-%M}_sync.json"


def iso_a(dt: datetime) -> str:
    """TMS A: ISO-8601 with the Central offset carried explicitly."""
    return f"{dt:%Y-%m-%dT%H:%M:%S}{ISO_OFFSET}"


def naive_b(dt: datetime) -> str:
    """TMS B: naive local Central, no offset. The adapter must supply CDT."""
    return f"{dt:%Y-%m-%d %H:%M:%S}"


def utc_c(dt: datetime) -> str:
    """TMS C: already UTC. 06:00 Central -> 11:00:00.000+0000."""
    return f"{dt + timedelta(hours=CENTRAL_OFFSET_HOURS):%Y-%m-%dT%H:%M:%S}.000+0000"


# ---------------------------------------------------------------------------
# Zip pools, grouped by zip3 so lane-tier structure is explicit, not accidental
# ---------------------------------------------------------------------------

Z750 = ("75050", "75051", "75061", "75038", "75063", "75006", "75019", "75067",
        "75074", "75080", "75040", "75034", "75071", "75087", "75028")
Z751 = ("75149", "75150", "75104", "75115", "75116", "75134", "75165")
Z752 = ("75201", "75207", "75212", "75217", "75220", "75228", "75235", "75241",
        "75243", "75234")
Z760 = ("76010", "76011", "76018", "76051", "76040", "76053", "76063", "76065",
        "76028", "76031")
Z761 = ("76102", "76106", "76137", "76140", "76155", "76177", "76180")
Z762 = ("76201", "76205", "76210", "76248")
Z770 = ("77002", "77008", "77015", "77020", "77029", "77032", "77041", "77049",
        "77060", "77064", "77084", "77094", "77099")
Z773 = ("77301", "77304", "77380", "77381", "77373", "77375", "77338", "77339", "77354")
Z774 = ("77449", "77450", "77494", "77478", "77479", "77477", "77459", "77471",
        "77469", "77429", "77433", "77423")
Z775 = ("77502", "77503", "77505", "77520", "77521", "77581", "77584", "77546",
        "77573", "77571", "77536", "77530", "77511")
Z780 = ("78006", "78023")
Z781 = ("78154", "78108", "78148", "78109", "78130", "78132", "78155", "78114", "78163")
Z782 = ("78205", "78201", "78207", "78210", "78216", "78218", "78219", "78221",
        "78223", "78227", "78229", "78233", "78240", "78245", "78247", "78249",
        "78251", "78258")
Z786 = ("78664", "78681", "78626", "78660", "78613", "78641", "78640", "78610",
        "78666", "78602", "78621", "78644", "78634")
Z787 = ("78701", "78702", "78704", "78721", "78723", "78724", "78741", "78744",
        "78745", "78753", "78758")
Z765 = ("76501", "76504", "76513", "76541")
Z766 = ("76645", "76691")
Z767 = ("76701", "76705", "76710", "76712")
Z778 = ("77803", "77840", "77868", "77833")
Z789 = ("78942", "78945", "78934", "78956", "78941")

ZIP_POOLS: dict[str, tuple[str, ...]] = {
    "750": Z750, "751": Z751, "752": Z752, "760": Z760, "761": Z761, "762": Z762,
    "770": Z770, "773": Z773, "774": Z774, "775": Z775, "780": Z780, "781": Z781,
    "782": Z782, "786": Z786, "787": Z787, "765": Z765, "766": Z766, "767": Z767,
    "778": Z778, "789": Z789,
}


def place(zip_code: str) -> Place:
    p = lookup_zip(zip_code)
    if p is None:                      # fail loudly at generation time
        raise AssertionError(f"zip {zip_code} is not in the geo table")
    return p


# ---------------------------------------------------------------------------
# Commodity / weight / rate models -- kept inside sane Texas Triangle bands
# ---------------------------------------------------------------------------

COMMODITIES = {
    DRY_VAN: ("Packaged foods", "Paper goods", "Retail apparel", "Auto parts",
              "Bottled water", "Household goods", "Plastic resin totes"),
    REEFER: ("Frozen poultry", "Fresh produce", "Dairy products",
             "Ice cream novelties", "Chilled juice", "Frozen dough"),
    FLATBED: ("Steel coils", "Lumber bundles", "Concrete pipe",
              "Structural steel", "Roofing shingles", "Rebar bundles"),
    UNKNOWN: ("General freight", "Palletized dry goods"),
}

WEIGHT_BANDS = {
    DRY_VAN: (18000, 43000),
    REEFER: (20000, 42000),
    FLATBED: (24000, 44000),
    UNKNOWN: (16000, 38000),
}

# =========================================================================
# RATE MODEL -- three NON-OVERLAPPING per-broker bands, so a tenant leak is
# loud in the money and not just in the load count.
# =========================================================================
# Each broker gets a flat $/mi bias (BrokerCfg.rpm_bias). Same lane, same
# equipment, three clearly separated price levels:
#
#     broker_a  -0.05   the cheap book    (rich-lane dry van median ~1.80)
#     broker_b  +0.30   mid market        (                        ~2.15)
#     broker_c  +0.68   the premium book  (                        ~2.53)
#
# This is realistic, not a fudge: brokers pay materially different rates for
# the same lane depending on contract terms, volume and carrier relationships.
# It is also what makes DECISIONS.md D4's threat model testable -- if the
# repository layer ever forgets broker_id, the pooled median/p25/p75 move by
# far more than noise instead of landing back on the right answer.
#
# Budget check so every emitted rate stays inside the mandated 1.50-3.50 band:
#   lowest  = 1.85 (long dry van) - 0.08 (jitter) - 0.05 (broker_a) = 1.72
#             ... and 1.72 x 0.945 = 1.63 for a lifecycle load's BOOKING rate,
#             which is also emitted and also has to stay above 1.50
#   highest = 2.44 (short haul) + 0.28 (reefer) + 0.08 + 0.68       = 3.48
#
# LIMIT OF THIS TECHNIQUE, stated up front. A single broker's own rates already
# span ~0.9 $/mi across haul lengths and equipment types (short reefer vs long
# dry van). The inter-broker offset is 0.38. So the bands separate cleanly on a
# NARROW lane key -- one zip3 pair or metro pair, one equipment type, where haul
# length barely varies -- but at REGION and REGION_ANY the three brokers'
# distributions overlap heavily and the middle broker's pooled median barely
# moves. Widening the offsets enough to fix that would require broker_c to pay
# over $3/mi for long-haul dry van, which breaks the plausibility rule that
# matters more. The validator therefore asserts the strong form only where it is
# arithmetically achievable, and says so instead of pretending otherwise.
RPM_JITTER = (-0.08, -0.05, -0.02, 0.0, 0.02, 0.05, 0.08)
MARGINS = (1.12, 1.14, 1.16, 1.18, 1.20, 1.22)

# Fraction of the carrier rate that arrives late, at settlement (scenario 1).
LIFECYCLE_HOLDBACK = 0.055

# Minimum $/mi separation the validator demands between any two brokers on the
# same lane key, and between a broker and the illegally-pooled figure.
BAND_SEPARATION = 0.15


def base_rpm(equipment: str, miles: float, bias: float = 0.0) -> float:
    if miles < 100:
        base = 2.44
    elif miles < 175:
        base = 2.28
    elif miles < 250:
        base = 2.02
    else:
        base = 1.85
    if equipment == REEFER:
        base += 0.28
    elif equipment == FLATBED:
        base += 0.17
    return base + bias


# ---------------------------------------------------------------------------
# Broker + carrier + customer rosters
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CarrierSpec:
    role: str          # V1 / V2 / V3 / FAR / NEAR / M1 / M2 / C1..C5
    name: str
    mc: str
    dot: str
    home_zip: str
    phone: str
    on_time_p: float
    target_loads: int


@dataclass(frozen=True)
class BrokerCfg:
    key: str
    tms: str
    dirname: str
    tms_name: str
    seed_offset: int
    carriers: tuple[CarrierSpec, ...]
    customers: tuple[str, ...]
    rpm_bias: float          # this broker's price level -- see RATE MODEL above
    price_label: str


# Roles are identical across brokers (D9: all three equally rich); only the
# names, ids, home bases and MC/DOT differ -- except where a carrier is
# deliberately the SAME real-world company in two systems (scenario 6).
_ROLE_TARGETS = {
    "V1": 22, "V2": 12, "V3": 16, "FAR": 8, "NEAR": 6, "M1": 11, "M2": 9,
    "C1": 1, "C2": 2, "C3": 2, "C4": 1, "C5": 3,
}
assert sum(_ROLE_TARGETS.values()) == 93

CARRIERS_A = (
    # scenario 6: same MC/DOT as broker_c's "Ibrahim Transport, Inc." (role C2)
    CarrierSpec("V1", "IBRAHIM TRANSPORT INC", "1346382", "3771394", "75050", "+15714906959", 0.92, 22),
    CarrierSpec("V2", "LONE STAR COLD LINES LLC", "902114", "2338907", "76011", "+18175550118", 0.88, 12),
    CarrierSpec("V3", "RIO GRANDE HAULING CO", "771450", "1998233", "78219", "+12105550143", 0.85, 16),
    CarrierSpec("FAR", "BLUEBONNET FREIGHT SYSTEMS", "655318", "1740092", "78006", "+18305550172", 0.80, 8),
    CarrierSpec("NEAR", "PINEY WOODS CARTAGE LLC", "1120674", "3102885", "77301", "+19365550196", 0.83, 6),
    CarrierSpec("M1", "TRINITY RIVER LOGISTICS INC", "843276", "2410558", "76102", "+18175550231", 0.86, 11),
    CarrierSpec("M2", "GULF COAST DRAYAGE LLC", "1005991", "2887431", "77502", "+17135550264", 0.78, 9),
    CarrierSpec("C1", "COMANCHE PEAK TRUCKING", "1288402", "3560117", "76031", "+18175550307", 1.00, 1),
    CarrierSpec("C2", "SILVERADO EXPRESS LLC", "1391775", "3812260", "75149", "+19725550348", 0.50, 2),
    CarrierSpec("C3", "ALAMO CHILL TRANSPORT", "1402883", "3844519", "78154", "+12105550381", 1.00, 2),
    CarrierSpec("C4", "BRAZOS VALLEY CARRIERS", "964120", "2705884", "77803", "+19795550412", 1.00, 1),
    CarrierSpec("C5", "IRON HORSE FLATBED CO", "1177336", "3288740", "75165", "+19725550455", 0.67, 3),
)

CARRIERS_B = (
    CarrierSpec("V1", "NORTH TEXAS LINE HAUL INC", "812445", "2260118", "76201", "+19405550117", 0.92, 22),
    # scenario 6: same MC/DOT as broker_c's "Delta Prime, L.L.C." (role M1)
    CarrierSpec("V2", "DELTA PRIME LLC", "884201", "2551377", "78155", "+18305550144", 0.88, 12),
    CarrierSpec("V3", "MISSION VALLEY TRUCKING LLC", "1043902", "2961540", "78221", "+12105550159", 0.85, 16),
    CarrierSpec("FAR", "HILL COUNTRY EXPRESS CO", "738214", "1893006", "78023", "+18305550183", 0.80, 8),
    CarrierSpec("NEAR", "WOODLANDS REGIONAL CARRIERS", "1256009", "3487215", "77373", "+12815550204", 0.83, 6),
    CarrierSpec("M1", "CROSSROADS FREIGHTWAYS INC", "926733", "2664190", "76501", "+12545550248", 0.86, 11),
    CarrierSpec("M2", "BAYOU CITY TRANSPORT LLC", "1098447", "3055872", "77020", "+17135550279", 0.78, 9),
    CarrierSpec("C1", "PECAN CREEK HAULING", "1345880", "3742601", "76701", "+12545550315", 1.00, 1),
    CarrierSpec("C2", "TWIN OAKS TRUCKING LLC", "1408112", "3860447", "77449", "+12815550356", 0.50, 2),
    CarrierSpec("C3", "FROSTLINE CARRIERS INC", "1362504", "3778120", "75040", "+19725550390", 1.00, 2),
    CarrierSpec("C4", "SAN MARCOS SHIPPING CO", "995631", "2803466", "78666", "+15125550421", 1.00, 1),
    CarrierSpec("C5", "STEEL PLAINS FLATBED LLC", "1189260", "3311058", "76065", "+19725550467", 0.67, 3),
)

CARRIERS_C = (
    CarrierSpec("V1", "Metroplex Ridge Logistics, Inc.", "856920", "2489013", "75207", "+12145550129", 0.92, 22),
    CarrierSpec("V2", "Bay Area Cold Carriers, LLC", "1067712", "2974885", "77546", "+12815550163", 0.88, 12),
    CarrierSpec("V3", "Espinoza Brothers Trucking Co.", "789003", "2044117", "78223", "+12105550188", 0.85, 16),
    CarrierSpec("FAR", "Guadalupe Valley Freight, LLC", "701558", "1802996", "78155", "+18305550212", 0.80, 8),
    CarrierSpec("NEAR", "Montgomery County Haulers Inc", "1233470", "3428907", "77304", "+19365550237", 0.83, 6),
    # scenario 6: same MC/DOT as broker_b's "DELTA PRIME LLC" (role V2)
    CarrierSpec("M1", "Delta Prime, L.L.C.", "884201", "2551377", "78155", "+18305550144", 0.86, 11),
    CarrierSpec("M2", "Trinity Bay Transport Co.", "1015228", "2901744", "77520", "+12815550288", 0.78, 9),
    CarrierSpec("C1", "Hillsboro Freight Partners LLC", "1310669", "3618402", "76645", "+12545550324", 1.00, 1),
    # scenario 6: same MC/DOT as broker_a's "IBRAHIM TRANSPORT INC" (role V1)
    CarrierSpec("C2", "Ibrahim Transport, Inc.", "1346382", "3771394", "75051", "+19725550361", 0.50, 2),
    CarrierSpec("C3", "Frio Line Refrigerated LLC", "1379942", "3805663", "78109", "+12105550399", 1.00, 2),
    CarrierSpec("C4", "Brazos Bend Carriers, Inc.", "973318", "2718550", "77469", "+12815550433", 1.00, 1),
    CarrierSpec("C5", "Ironclad Flatbed Services LLC", "1201884", "3350219", "76031", "+18175550478", 0.67, 3),
)

BROKERS: tuple[BrokerCfg, ...] = (
    BrokerCfg("broker_a", "A", "tms_a_freightflow", "FreightFlow", 101, CARRIERS_A,
              ("Lone Star Beverages", "Trinity Paper Mills", "Cowtown Auto Supply",
               "Bayou Provision Co", "Alamo Building Supply", "Panther Retail Group"),
              rpm_bias=-0.05, price_label="cheap book"),
    BrokerCfg("broker_b", "B", "tms_b_hauldesk", "HaulDesk", 202, CARRIERS_B,
              ("Alamo Building Supply", "Gulf Harbor Chemicals", "Red River Grocers",
               "Capitol Office Products", "Brazos Steel Works", "Hill Country Dairy"),
              rpm_bias=0.30, price_label="mid market"),
    BrokerCfg("broker_c", "C", "tms_c_brokeros", "BrokerOS", 303, CARRIERS_C,
              ("Gulf Coast Foods", "Silverleaf Distributors", "Texan Home Goods",
               "Pecos Industrial Supply", "Bluebonnet Grocery Co", "Ridgeline Materials"),
              rpm_bias=0.68, price_label="premium book"),
)


# ---------------------------------------------------------------------------
# Load plan model
# ---------------------------------------------------------------------------


@dataclass
class Stop:
    place: Place
    is_pickup: bool
    is_drop: bool
    sched_date: date
    actual: datetime | None = None      # naive Central


@dataclass
class LineItem:
    commodity: str
    weight: float
    units: str          # "lbs" | "kg"  (TMS C only; per line item -- the trap)
    pallets: float


@dataclass
class Appearance:
    slot: int
    status: str
    carrier_rate: float | None
    show_actuals: bool
    last_modified: datetime            # naive Central
    note: str = ""
    rate_only: bool = False            # TMS B: rates rows only, no `loads` row
    adjustment_pay: float | None = None  # TMS B: negative ADJUSTMENT amount
    out_of_order_lm: bool = False      # deliberately regressed lastModifiedDate


@dataclass
class LoadPlan:
    key: str
    group: str
    scenario: str
    equipment: str
    carrier_role: str | None
    customer: str
    stops: list[Stop]
    miles: float
    weight_lbs: float
    line_items: list[LineItem]
    carrier_rate: float | None
    customer_rate: float
    on_time: bool
    appearances: list[Appearance] = field(default_factory=list)
    # packing constraints
    pinned_slot: int | None = None
    max_slot: int = HISTORY_SLOTS - 1
    deliver_hours_before: int | None = None
    # filled in by the renderers
    public_id: str = ""
    # day-11 only
    is_day11: bool = False
    behavior: str = ""
    expected_winner: str = ""

    @property
    def origin(self) -> Place:
        return self.stops[0].place

    @property
    def dest(self) -> Place:
        return self.stops[-1].place

    @property
    def delivered_at(self) -> datetime | None:
        return self.stops[-1].actual

    @property
    def sched_delivery(self) -> date:
        return self.stops[-1].sched_date

    @property
    def rpm(self) -> float:
        assert self.carrier_rate is not None
        return self.carrier_rate / self.miles


@dataclass
class BrokerPlan:
    cfg: BrokerCfg
    rng: random.Random
    loads: list[LoadPlan] = field(default_factory=list)
    day11: list[LoadPlan] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # Public id maps, filled in by render(). Kept so the validator can
    # reconcile emitted ids against the plan instead of trusting them.
    ids: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Load construction helpers
# ---------------------------------------------------------------------------


def _pick(rng: random.Random, pool: tuple[str, ...]) -> str:
    return pool[rng.randrange(len(pool))]


def _stops_from_zips(zips: list[str]) -> list[Stop]:
    out: list[Stop] = []
    for i, z in enumerate(zips):
        out.append(
            Stop(
                place=place(z),
                is_pickup=(i == 0),
                is_drop=(i == len(zips) - 1),
                sched_date=DAY_ONE,     # replaced by schedule_load()
            )
        )
    return out


def _leg_miles(stops: list[Stop]) -> float:
    total = 0.0
    for a, b in zip(stops, stops[1:]):
        total += road_miles_between(a.place, b.place)
    return round(total, 1)


def make_load(
    plan: BrokerPlan,
    key: str,
    group: str,
    scenario: str,
    zips: list[str],
    equipment: str,
    carrier_role: str | None,
    *,
    pinned_slot: int | None = None,
    max_slot: int = HISTORY_SLOTS - 1,
    deliver_hours_before: int | None = None,
    kg_line_item: bool = False,
    force_on_time: bool | None = None,
) -> LoadPlan:
    """Build one COMPLETED history load. Rates and weights come from the models
    above so every emitted number stays inside the sanity bands."""
    rng = plan.rng
    stops = _stops_from_zips(zips)
    for s in stops:
        assert resolve_place(s.place.city, s.place.state, s.place.zip) is not None, s.place
    miles = _leg_miles(stops)

    lo, hi = WEIGHT_BANDS[equipment]
    weight = float(rng.randrange(lo // 100, hi // 100 + 1) * 100)

    commodity = _pick(rng, COMMODITIES[equipment])
    if kg_line_item:
        # multi line item, ONE of them in kg -- exercises the per-line-item rule
        lbs_part = float(rng.randrange(90, 160) * 100)
        kg_part = float(rng.randrange(35, 70) * 100)
        weight = round(lbs_part + kg_part * 2.20462, 1)
        second = COMMODITIES[equipment][(COMMODITIES[equipment].index(commodity) + 1)
                                        % len(COMMODITIES[equipment])]
        line_items = [
            LineItem(commodity, lbs_part, "lbs", float(round(lbs_part / 1300))),
            LineItem(second, kg_part, "kg", float(round(kg_part * 2.20462 / 1300))),
        ]
    else:
        line_items = [LineItem(commodity, weight, "lbs", float(round(weight / 1300)))]
    assert weight < 45000, (key, weight)

    rpm = round(base_rpm(equipment, miles, plan.cfg.rpm_bias)
                + _pick_num(rng, RPM_JITTER), 2)
    carrier_rate = round(miles * rpm, 2)
    customer_rate = round(carrier_rate * _pick_num(rng, MARGINS), 2)

    if force_on_time is None:
        role_p = 0.85 if carrier_role is None else _carrier(plan.cfg, carrier_role).on_time_p
        on_time = rng.random() < role_p
    else:
        on_time = force_on_time

    load = LoadPlan(
        key=key, group=group, scenario=scenario, equipment=equipment,
        carrier_role=carrier_role, customer=_pick(rng, plan.cfg.customers),
        stops=stops, miles=miles, weight_lbs=weight, line_items=line_items,
        carrier_rate=carrier_rate, customer_rate=customer_rate, on_time=on_time,
        pinned_slot=pinned_slot, max_slot=max_slot,
        deliver_hours_before=deliver_hours_before,
    )
    plan.loads.append(load)
    return load


def _pick_num(rng: random.Random, pool: tuple[float, ...]) -> float:
    return pool[rng.randrange(len(pool))]


def _carrier(cfg: BrokerCfg, role: str) -> CarrierSpec:
    for c in cfg.carriers:
        if c.role == role:
            return c
    raise KeyError(role)


def schedule_load(plan: BrokerPlan, load: LoadPlan, final_slot: int) -> None:
    """Derive every timestamp from the slot the load lands in.

    Guarantees created < departed < delivered <= last_modified <= sync time, so
    chronology holds by construction rather than by hope.
    """
    rng = plan.rng
    sync = slot_local(final_slot)
    hours_back = load.deliver_hours_before
    if hours_back is None:
        hours_back = rng.randrange(3, 23)
    delivered = sync - timedelta(hours=hours_back, minutes=rng.randrange(0, 60))
    transit_h = max(5, int(load.miles / 45) + rng.randrange(2, 9))
    if not load.on_time:
        # A late load has to be expressible: its scheduled delivery date is the
        # day BEFORE it actually arrived, so the trip must cross a midnight or
        # the schedule would put the drop before the pickup. Extending dwell at
        # the shipper is also why it ran late.
        transit_h = max(transit_h, delivered.hour + 3)
    departed = delivered - timedelta(hours=transit_h)

    sched_del = delivered.date() if load.on_time else delivered.date() - timedelta(days=1)
    sched_pu = departed.date()
    assert sched_pu <= sched_del, (load.key, sched_pu, sched_del)

    load.stops[0].sched_date = sched_pu
    load.stops[0].actual = departed
    for i, s in enumerate(load.stops[1:-1], start=1):
        frac = i / (len(load.stops) - 1)
        s.actual = departed + timedelta(hours=transit_h * frac)
        s.sched_date = s.actual.date()
    load.stops[-1].sched_date = sched_del
    load.stops[-1].actual = delivered

    last_mod = delivered + timedelta(minutes=rng.randrange(20, 150))
    if last_mod > sync - timedelta(minutes=5):
        last_mod = sync - timedelta(minutes=5)
    created = departed - timedelta(days=rng.randrange(1, 4), hours=rng.randrange(0, 13))

    if not load.appearances:
        load.appearances.append(
            Appearance(final_slot, COMPLETED, load.carrier_rate, True, last_mod,
                       note="single-appearance completed backfill")
        )
    load.created_at = created                                # type: ignore[attr-defined]
    load.final_last_modified = last_mod                      # type: ignore[attr-defined]


# ===========================================================================
# SCENARIO BLOCKS -- a reader should be able to open this file and see
# "this is the deadhead setup".
# ===========================================================================


def scenario_1_full_lifecycle(plan: BrokerPlan) -> LoadPlan:
    """SCENARIO 1 -- FULL LIFECYCLE.

    One load walks PLANNED -> ACTIVE -> COVERED -> IN_TRANSIT -> DELIVERED ->
    COMPLETED across six separate files (slots 0,1,3,4,6,8 -- slot 2 is a
    deliberately empty sync in the middle of the walk). The carrier and the
    carrier rate appear at COVERED (booking); the final amounts land at
    COMPLETED. It sits on the rich ZIP3 lane so it also feeds lane stats.
    """
    rng = plan.rng
    load = make_load(
        plan, key="LC01", group="R", scenario="lifecycle",
        zips=[_pick(rng, Z750), _pick(rng, Z774)],
        equipment=DRY_VAN, carrier_role="V1",
        pinned_slot=8, deliver_hours_before=9, force_on_time=True,
    )
    # 5.5% of the linehaul is held back until settlement, so the booking rate is
    # below the final amount and the delta arrives at COMPLETED as "late-arriving
    # money" -- NOT as a correction. Proportional rather than a flat $40 so the
    # cheapest broker's long hauls cannot dip under the $1.50/mi sanity floor.
    booking_rate = round(load.carrier_rate * (1.0 - LIFECYCLE_HOLDBACK), 2)

    schedule_load(plan, load, 8)
    load.appearances.clear()
    created = load.created_at                                # type: ignore[attr-defined]

    steps = [
        (0, PLANNED, None, False, "quote requested"),
        (1, ACTIVE, None, False, "searching for a truck"),
        (3, COVERED, booking_rate, False, "carrier booked; carrier rate appears"),
        (4, IN_TRANSIT, booking_rate, False, "rolling"),
        (6, DELIVERED, booking_rate, True, "delivered; actuals revealed"),
        (8, COMPLETED, load.carrier_rate, True, "settled; final amounts"),
    ]
    for i, (slot, status, rate, actuals, note) in enumerate(steps):
        lm = slot_local(slot) - timedelta(hours=1, minutes=17 + i * 3)
        if i == 0:
            lm = created + timedelta(minutes=8)
        out_of_order = False
        if plan.cfg.tms == "A" and status == DELIVERED:
            # SCENARIO 8 (messy edge): lastModifiedDate goes BACKWARDS relative
            # to the previous file. File order is still the truth.
            lm = slot_local(3) - timedelta(hours=2)
            out_of_order = True
        load.appearances.append(
            Appearance(slot, status, rate, actuals, lm, note=note,
                       out_of_order_lm=out_of_order)
        )
    plan.notes.append(
        f"lifecycle {load.key}: booking rate {booking_rate} -> final {load.carrier_rate}"
    )
    return load


def scenario_2_corrections(plan: BrokerPlan) -> tuple[LoadPlan, LoadPlan]:
    """SCENARIO 2 -- CORRECTIONS, in each TMS's own flavor.

    Correction #1 lives on the rich ZIP3 lane (slots 12 -> 17) and correction #2
    on the reefer metro-scatter lane (slots 20 -> 25), so both day-11 price
    answers depend on a corrected number being handled properly.

      TMS A: the whole load object is restated with a new ``totalBuy``.
      TMS B: a negative ``ADJUSTMENT`` pay row is APPENDED. #1 also restates the
             ``loads`` row; #2 is the rate-only case -- the file's ``loads``
             array does not mention the load at all (CLAUDE.md's known trap).
      TMS C: ``bos__Carrier_Rate__c`` is silently restated, no marker at all.
             #2 additionally carries an out-of-order ``LastModifiedDate``.
    """
    rng = plan.rng
    tms = plan.cfg.tms

    c1 = make_load(plan, key="CORR1", group="R", scenario="correction",
                   zips=[_pick(rng, Z750), _pick(rng, Z774)],
                   equipment=DRY_VAN, carrier_role="V2",
                   pinned_slot=12, deliver_hours_before=7)
    c2 = make_load(plan, key="CORR2", group="S", scenario="correction",
                   zips=[_pick(rng, Z761), _pick(rng, Z770)],
                   equipment=REEFER, carrier_role="V2",
                   pinned_slot=20, deliver_hours_before=6)

    for load, first_slot, second_slot, delta in ((c1, 12, 17, -85.0), (c2, 20, 25, -120.0)):
        original = round(load.carrier_rate - delta, 2)   # what arrived first
        corrected = load.carrier_rate                    # what is true afterwards
        schedule_load(plan, load, first_slot)
        load.appearances.clear()
        lm1 = load.final_last_modified                   # type: ignore[attr-defined]
        lm2 = slot_local(second_slot) - timedelta(hours=2, minutes=11)
        out_of_order = False
        if tms == "C" and load.key == "CORR2":
            # SCENARIO 8 (messy edge): the restatement carries an EARLIER
            # LastModifiedDate than the record it replaces.
            lm2 = lm1 - timedelta(hours=5)
            out_of_order = True
        load.appearances.append(
            Appearance(first_slot, COMPLETED, original, True, lm1,
                       note=f"first truth: carrier rate {original}")
        )
        rate_only = (tms == "B" and load.key == "CORR2")
        load.appearances.append(
            Appearance(second_slot, COMPLETED, corrected, True, lm2,
                       note=f"corrected to {corrected} (delta {delta})",
                       rate_only=rate_only,
                       adjustment_pay=(delta if tms == "B" else None),
                       out_of_order_lm=out_of_order)
        )
        plan.notes.append(
            f"correction {load.key}: {original} -> {corrected} "
            f"(slot {first_slot} -> {second_slot}, rate_only={rate_only})"
        )
    return c1, c2


def scenario_3a_rich_zip3_lane(plan: BrokerPlan) -> None:
    """SCENARIO 3 (rich half) -- the ZIP3 tier must actually fire.

    12 completed DRY_VAN loads on the single triple (750, 774, DRY_VAN):
    DFW inner-ring suburbs -> Katy/Sugar Land/Cypress. The day-11 rich-lane
    load sits on exactly this triple, so PRD section 7's top rung is exercised.
    Well clear of the 5-load minimum, with headroom so a small edit cannot
    silently drop it under.
    """
    rng = plan.rng
    # The lane carries 12 loads: V1 x8, V2 x2, C1 x1, C2 x1. LC01 (V1) and
    # CORR1 (V2) are already on it from scenarios 1 and 2, so this block only
    # has to make up the difference.
    alloc = [("V1", 7), ("V2", 1), ("C1", 1), ("C2", 1)]
    n = 0
    for role, count in alloc:
        for _ in range(count):
            n += 1
            make_load(plan, key=f"R{n:02d}", group="R", scenario="rich_zip3_lane",
                      zips=[_pick(rng, Z750), _pick(rng, Z774)],
                      equipment=DRY_VAN, carrier_role=role,
                      max_slot=_role_max_slot(role))


def scenario_5_suburb_scatter(plan: BrokerPlan) -> None:
    """SCENARIO 5 -- SUBURB SCATTER, so METRO demonstrably beats city pairs.

    8 REEFER loads inside the DFW -> HOU metro pair, deliberately spread over
    four different zip3 pairs so that NO zip3 pair reaches the 5-load minimum
    while the metro pair does:

        761 -> 770  x3   (Fort Worth  -> Houston)
        760 -> 775  x2   (Arlington   -> Pasadena/Baytown)
        750 -> 775  x2   (Irving      -> Deer Park/La Porte)
        752 -> 774  x1   (Dallas      -> Sugar Land)
        --------------------------------  METRO DFW->HOU reefer = 8 >= 5

    The day-11 scatter load is REEFER on 761 -> 770: ZIP3 finds 3 (< 5) and the
    walk falls to METRO, which finds 8. Equipment separates this cleanly from
    the dry-van rich lane in the same metro pair (DECISIONS D6).
    """
    rng = plan.rng
    pairs = [("761", "770")] * 3 + [("760", "775")] * 2 + [("750", "775")] * 2 + [("752", "774")]
    # CORR2 already occupies one of the 761->770 slots.
    pairs = pairs[1:]
    roles = ["V2"] * 4 + ["V1"] + ["C3"] * 2
    assert len(pairs) == len(roles) == 7
    for i, ((oz3, dz3), role) in enumerate(zip(pairs, roles), start=1):
        make_load(plan, key=f"S{i:02d}", group="S", scenario="suburb_scatter",
                  zips=[_pick(rng, ZIP_POOLS[oz3]), _pick(rng, ZIP_POOLS[dz3])],
                  equipment=REEFER, carrier_role=role,
                  max_slot=_role_max_slot(role))


def scenario_3b_rich_lane_depth(plan: BrokerPlan) -> None:
    """SCENARIO 3 (depth) -- 10 more DRY_VAN loads in DFW -> HOU on zip3 pairs
    that each stay under 5, taking the metro pair past PRD section 4's "25+".

        761 -> 770 x4,  760 -> 775 x3,  750 -> 770 x3
    """
    rng = plan.rng
    pairs = [("761", "770")] * 4 + [("760", "775")] * 3 + [("750", "770")] * 3
    roles = ["V1"] * 4 + ["M1"] * 3 + ["M2"] * 2 + ["C4"]
    assert len(pairs) == len(roles) == 10
    for i, ((oz3, dz3), role) in enumerate(zip(pairs, roles), start=1):
        make_load(plan, key=f"D{i:02d}", group="D", scenario="rich_lane_depth",
                  zips=[_pick(rng, ZIP_POOLS[oz3]), _pick(rng, ZIP_POOLS[dz3])],
                  equipment=DRY_VAN, carrier_role=role,
                  max_slot=_role_max_slot(role))


def scenario_3c_thin_lane(plan: BrokerPlan) -> None:
    """SCENARIO 3 (thin half) -- San Antonio -> Waco, exactly 2 loads.

    Nothing else in the fixture runs SAT -> TX_OTHER dry van, so the day-11
    thin-lane load fails ZIP3 (2 < 5), fails METRO (2 < 5) and lands on REGION
    with a "low confidence" label. That fall-through is the whole point.
    """
    rng = plan.rng
    for i, role in enumerate(("V3", "C2"), start=1):
        make_load(plan, key=f"T{i:02d}", group="T", scenario="thin_lane",
                  zips=[_pick(rng, Z782), _pick(rng, Z767)],
                  equipment=DRY_VAN, carrier_role=role,
                  max_slot=_role_max_slot(role))


def scenario_7_deadhead_setup(plan: BrokerPlan) -> None:
    """SCENARIO 7 -- DEADHEAD, engineered so proximity is the DECIDING signal.

    Lane: Conroe (773) -> Austin (787), dry van, 6 loads => the day-11 deadhead
    load resolves at the ZIP3 tier.

      FAR   4 of the 6 loads, most recent lane load 2026-07-13, and its LAST
            delivery of any kind is Denton on 2026-07-14 -- 264.8 mi from the
            day-11 pickup, i.e. past the 250 mi cutoff, so deadhead credit is 0.
      NEAR  2 of the 6 loads, most recent lane load 2026-07-11, but it drops a
            different load in The Woodlands on 2026-07-15 -- ~16 mi out, full
            deadhead credit.

    FAR is ahead on BOTH lane experience and recency. NEAR wins only because of
    the deadhead term, which is exactly what the scenario has to prove.
    """
    rng = plan.rng
    far_slots = [5, 13, 22, 30]
    for i, s in enumerate(far_slots, start=1):
        make_load(plan, key=f"H{i:02d}", group="H", scenario="deadhead_lane",
                  zips=[_pick(rng, Z773), _pick(rng, Z787)],
                  equipment=DRY_VAN, carrier_role="FAR",
                  pinned_slot=s, deliver_hours_before=6)
    for i, s in enumerate((12, 21), start=5):
        make_load(plan, key=f"H{i:02d}", group="H", scenario="deadhead_lane",
                  zips=[_pick(rng, Z773), _pick(rng, Z787)],
                  equipment=DRY_VAN, carrier_role="NEAR",
                  pinned_slot=s, deliver_hours_before=5)

    # FAR's last delivery of ANY kind -- 264.8 mi from Conroe, past the cutoff.
    make_load(plan, key="DH_FAR_LAST", group="P", scenario="deadhead_far_anchor",
              zips=[_pick(rng, Z770), "76201"],
              equipment=DRY_VAN, carrier_role="FAR",
              pinned_slot=34, deliver_hours_before=4)
    # NEAR's last delivery -- 16 mi from the day-11 pickup, on day 10.
    make_load(plan, key="DH_NEAR_LAST", group="P", scenario="deadhead_near_anchor",
              zips=[_pick(rng, Z787), "77380"],
              equipment=DRY_VAN, carrier_role="NEAR",
              pinned_slot=37, deliver_hours_before=3)


def scenario_4_cold_start_flatbed(plan: BrokerPlan) -> None:
    """SCENARIO 4 -- CARRIER CONTRAST / cold start, and the REGION_ANY rung.

    brokers A and C -- 7 flatbed loads region-wide (3 of them DFW -> AUS).
    V3 has 4, C5 has 2, M2 has 1. Their day-11 cold-start load is flatbed
    DFW -> AUS: ZIP3 and METRO both fall short, so the walk lands on rung 3,
    REGION + FLATBED, with 7 loads. There the saturating experience curve (D5)
    does its job: C5's 2 loads give 2/(2+5) = 0.286 against V3's 4/(4+5) =
    0.444. C5 still surfaces with an honest reason; it just cannot leapfrog on
    two data points.

    broker B -- deliberately only **4** flatbed loads region-wide. That is
    below the 5-load minimum at every tier including REGION, so broker B's
    day-11 flatbed load is the fixture that drives the walk to **rung 4,
    REGION_ANY** (DECISIONS D6: "so a rare equipment type still gets an answer
    instead of nothing", automatically low confidence). Without this the fourth
    rung would be exactly as undemonstrated as the ZIP3 rung would have been if
    750->774 had never cleared 5.

    The three loads B gives up on flatbed are re-issued on other equipment, so
    all three brokers still hold 93 distinct history loads and every carrier
    keeps its target load count (D9's equal-depth claim is untouched).
    """
    rng = plan.rng
    thin_flatbed = plan.cfg.tms == "B"
    specs = [
        # key,  zips,                                     role,  equipment for B
        ("F01", ["75207", _pick(rng, Z787)], "V3", FLATBED),
        ("F02", [_pick(rng, Z752), _pick(rng, Z787)], "V3", DRY_VAN),
        ("F03", [_pick(rng, Z750), _pick(rng, Z786)], "C5", FLATBED),
        ("F04", [_pick(rng, Z770), _pick(rng, Z760)], "V3", DRY_VAN),
        ("F05", [_pick(rng, Z782), _pick(rng, Z770)], "V3", REEFER),
        ("F06", [_pick(rng, Z752), _pick(rng, Z782)], "C5", FLATBED),
        ("F07", [_pick(rng, Z787), _pick(rng, Z770)], "M2", FLATBED),
    ]
    for key, zips, role, b_equip in specs:
        equip = b_equip if thin_flatbed else FLATBED
        scen = "cold_start_flatbed" if equip == FLATBED else "flatbed_kept_scarce"
        make_load(plan, key=key, group="F", scenario=scen,
                  zips=zips, equipment=equip, carrier_role=role,
                  max_slot=_role_max_slot(role))


def scenario_backhaul(plan: BrokerPlan) -> None:
    """HOU -> DFW backhaul, 10 dry-van loads.

    Also the anchor for the rich lane's deadhead term: V1's last delivery of the
    whole history is an Irving drop on 2026-07-15, ~6 mi from the day-11
    rich-lane pickup, so the lane veteran is also the nearest truck.
    """
    rng = plan.rng
    make_load(plan, key="B01", group="B", scenario="v1_home_anchor",
              zips=[_pick(rng, Z770), "75061"],
              equipment=DRY_VAN, carrier_role="V1",
              pinned_slot=38, deliver_hours_before=4)
    roles = ["V1"] * 4 + ["M1"] * 3 + ["NEAR"] + ["M2"]
    for i, role in enumerate(roles, start=2):
        make_load(plan, key=f"B{i:02d}", group="B", scenario="backhaul",
                  zips=[_pick(rng, Z770 + Z774 + Z775), _pick(rng, Z750 + Z752 + Z760)],
                  equipment=DRY_VAN, carrier_role=role,
                  max_slot=_role_max_slot(role))


# Lanes used for breadth. Deliberately excludes DFW->HOU (protected by the rich
# lane and the scatter), SAT->TX_OTHER (protected by the thin lane), flatbed
# (owned by the cold-start block) and 773->787 dry van (the deadhead lane).
_BREADTH_LANES: tuple[tuple[tuple[str, ...], tuple[str, ...], str], ...] = (
    (Z770 + Z774, Z782 + Z781, DRY_VAN),
    (Z782 + Z781, Z770 + Z775, DRY_VAN),
    (Z787 + Z786, Z750 + Z752, DRY_VAN),
    (Z752 + Z750, Z787 + Z786, DRY_VAN),
    (Z770, Z778, DRY_VAN),
    (Z752 + Z761, Z767 + Z765, DRY_VAN),
    (Z782, Z787, REEFER),
    (Z787, Z782 + Z781, DRY_VAN),
    (Z770, Z787 + Z786, DRY_VAN),
    (Z787, Z770 + Z775, REEFER),
    (Z782 + Z781, Z750 + Z760, DRY_VAN),
    (Z752, Z782 + Z781, REEFER),
    (Z778 + Z789, Z752 + Z750, DRY_VAN),
    (Z767 + Z766, Z770 + Z774, DRY_VAN),
    (Z781, Z787 + Z786, DRY_VAN),
    (Z774, Z765 + Z767, REEFER),
)


def scenario_breadth_and_messy_edges(plan: BrokerPlan) -> None:
    """REGION-tier depth, carrier load counts, and the remaining messy edges.

    38 loads. Named anchors first (the last-delivery positions the deadhead and
    lane answers depend on, plus the schema-specific messy edges), then a
    deterministic rotation over lanes that cannot disturb any protected count.
    """
    rng = plan.rng
    tms = plan.cfg.tms
    remaining = {"V1": 4, "V2": 5, "V3": 11, "FAR": 3, "NEAR": 2,
                 "M1": 5, "M2": 5, "C5": 1}
    # DH_FAR_LAST / DH_NEAR_LAST were already charged to FAR / NEAR above.

    def take(role: str) -> str:
        remaining[role] -= 1
        assert remaining[role] >= 0, role
        return role

    idx = 0

    def nxt(key_prefix: str = "P") -> str:
        nonlocal idx
        idx += 1
        return f"{key_prefix}{idx:02d}"

    # --- last-delivery anchors -------------------------------------------------
    # Where each carrier's truck ENDS the history is a first-class signal
    # (deadhead, PRD section 8 / TASKS I8), so it is placed deliberately rather
    # than left to the shuffle. Every carrier's final drop is pinned to a day-10
    # slot; the anchors below are the ones a day-11 answer turns on.
    #
    # V2 (reefer veteran) ends its history 19 mi from the day-11 scatter pickup.
    make_load(plan, key=nxt(), group="P", scenario="v2_home_anchor",
              zips=[_pick(rng, Z782), "76011"], equipment=REEFER,
              carrier_role=take("V2"), pinned_slot=39, deliver_hours_before=5)
    # V3 (south Texas veteran) ends 10 mi from the day-11 thin-lane pickup.
    make_load(plan, key=nxt(), group="P", scenario="v3_home_anchor",
              zips=[_pick(rng, Z770), "78154"], equipment=DRY_VAN,
              carrier_role=take("V3"), pinned_slot=39, deliver_hours_before=6)
    # M1 / M2 end in the Houston area: >200 mi from the San Antonio thin-lane
    # pickup, so the thin lane is decided by V3's proximity and not muddied by
    # two mid-tier carriers that happen to be parked nearby.
    make_load(plan, key=nxt(), group="P", scenario="m1_home_anchor",
              zips=[_pick(rng, Z752), "77002"], equipment=DRY_VAN,
              carrier_role=take("M1"), pinned_slot=38, deliver_hours_before=5)
    make_load(plan, key=nxt(), group="P", scenario="m2_home_anchor",
              zips=[_pick(rng, Z787), "77502"], equipment=DRY_VAN,
              carrier_role=take("M2"), pinned_slot=38, deliver_hours_before=7)

    # --- messy edges (scenario 8) ---------------------------------------------
    if tms in ("A", "C"):
        # 3-stop load. Middle stop is kept but is NOT lane-forming: the lane is
        # first pickup -> last drop. Mileage is the sum of the legs.
        mid = "78741" if tms == "A" else "78934"
        legs = ([_pick(rng, Z752), mid, _pick(rng, Z782)] if tms == "A"
                else [_pick(rng, Z770), mid, _pick(rng, Z782)])
        make_load(plan, key=nxt(), group="P", scenario="messy_three_stop",
                  zips=legs, equipment=DRY_VAN, carrier_role=take("M1"),
                  max_slot=_role_max_slot("M1"))
    if tms == "C":
        # Null bos__Equipment_Type__c -> UNKNOWN, never DRY_VAN (invariant 5).
        for _ in range(2):
            make_load(plan, key=nxt(), group="P", scenario="messy_null_equipment",
                      zips=[_pick(rng, Z770), _pick(rng, Z787)],
                      equipment=UNKNOWN, carrier_role=take("M2"),
                      max_slot=_role_max_slot("M2"))
        # Two line items, ONE of them in kg.
        make_load(plan, key=nxt(), group="P", scenario="messy_kg_line_item",
                  zips=[_pick(rng, Z782), _pick(rng, Z752)],
                  equipment=DRY_VAN, carrier_role=take("V3"),
                  max_slot=_role_max_slot("V3"), kg_line_item=True)

    # --- deterministic breadth rotation ---------------------------------------
    order = [r for r in ("V1", "V2", "V3", "FAR", "NEAR", "M1", "M2", "C5")]
    lane_i = 0
    while any(remaining[r] > 0 for r in order):
        for role in order:
            if remaining[role] <= 0:
                continue
            o_pool, d_pool, equip = _BREADTH_LANES[lane_i % len(_BREADTH_LANES)]
            lane_i += 1
            o = _pick(rng, o_pool)
            d = _pick(rng, d_pool)
            # never disturb the deadhead lane's protected zip3 triple
            guard = 0
            while place(o).zip3 == "773" and place(d).zip3 == "787" and equip == DRY_VAN:
                o = _pick(rng, o_pool)
                guard += 1
                assert guard < 50
            make_load(plan, key=nxt(), group="P", scenario="breadth",
                      zips=[o, d], equipment=equip, carrier_role=take(role),
                      max_slot=_role_max_slot(role))


def _role_max_slot(role: str) -> int:
    """Keep each anchored carrier's LAST delivery where the scenario needs it."""
    return {
        "V1": 36,     # last delivery must be the Irving drop pinned at slot 38
        "V2": 37,     # ... the Arlington drop at slot 39
        "V3": 37,     # ... the Schertz drop at slot 39
        "FAR": 32,    # ... the Denton drop at slot 34
        "NEAR": 35,   # ... The Woodlands drop at slot 37
        "M1": 36,     # ... the Houston drop at slot 38
        "M2": 36,     # ... the Pasadena drop at slot 38
    }.get(role, HISTORY_SLOTS - 1)


def scenario_day11(plan: BrokerPlan) -> None:
    """DAY 11 -- fresh loads, each proving exactly one behavior.

    All are in the TMS's "looking for a truck" status with NO carrier, and none
    of them is ever covered in a later file.

    Five loads in every broker; broker C gets a sixth, in the otherwise-empty
    18:00 slot, carrying **null equipment**. That is the only schema that can
    express it (D9: messy edges live where their schema allows them) and it is
    the fixture for D6's other half -- a load whose OWN equipment is UNKNOWN
    skips the equipment filter at every rung and says so in its provenance.

    Load 5 means different things per broker, on purpose:
      A, C -- flatbed with 7 region-wide flatbed loads -> rung 3, REGION+FLATBED
      B    -- flatbed with only 4 region-wide          -> rung 4, REGION_ANY
    """
    rng = plan.rng
    tms = plan.cfg.tms
    if tms == "B":
        cold = ("DAY11-COLDSTART", 42, [_pick(rng, Z752), _pick(rng, Z787)], FLATBED,
                "region_any_rare_equipment", "V1")
    else:
        cold = ("DAY11-COLDSTART", 42, [_pick(rng, Z752), _pick(rng, Z787)], FLATBED,
                "cold_start_surfacing", "V3")
    specs = [
        # key, slot, zips, equipment, behavior, expected winner role
        ("DAY11-RICH", 40, [_pick(rng, Z750), _pick(rng, Z774)], DRY_VAN,
         "rich_lane_zip3", "V1"),
        ("DAY11-SCATTER", 40, [_pick(rng, Z761), _pick(rng, Z770)], REEFER,
         "metro_scatter", "V2"),
        ("DAY11-THIN", 41, [_pick(rng, Z782), _pick(rng, Z767)], DRY_VAN,
         "thin_lane_region", "V3"),
        ("DAY11-DEADHEAD", 42, ["77304", _pick(rng, Z787)], DRY_VAN,
         "deadhead_win", "NEAR"),
        cold,
    ]
    if tms == "C":
        # Irving -> Baytown. ZIP3 750->775 unfiltered holds only 2 loads, so the
        # walk falls to METRO DFW->HOU with the equipment filter OFF, where the
        # pool is a genuine mix of dry van and reefer.
        specs.append(
            ("DAY11-UNKNOWN-EQUIP", 43, [_pick(rng, Z750), _pick(rng, Z775)], UNKNOWN,
             "unknown_equipment_no_filter", "V1")
        )
    for key, slot, zips, equip, behavior, winner in specs:
        stops = _stops_from_zips(zips)
        for s in stops:
            assert resolve_place(s.place.city, s.place.state, s.place.zip) is not None
        miles = _leg_miles(stops)
        lo, hi = WEIGHT_BANDS[equip]
        weight = float(rng.randrange(lo // 100, hi // 100 + 1) * 100)
        commodity = _pick(rng, COMMODITIES[equip])
        # No carrier rate yet -- that is the question the system answers. The
        # customer rate is a normal quote at the equipment's typical margin.
        customer_rate = round(miles * base_rpm(equip, miles, plan.cfg.rpm_bias) * 1.18, 2)
        sync = slot_local(slot)
        created = sync - timedelta(hours=rng.randrange(3, 11), minutes=rng.randrange(0, 60))
        pu_date = date(2026, 7, 17)
        del_date = pu_date if miles < 120 else pu_date + timedelta(days=1)
        stops[0].sched_date = pu_date
        stops[-1].sched_date = del_date

        load = LoadPlan(
            key=key, group="DAY11", scenario=behavior, equipment=equip,
            carrier_role=None, customer=_pick(rng, plan.cfg.customers),
            stops=stops, miles=miles, weight_lbs=weight,
            line_items=[LineItem(commodity, weight, "lbs", float(round(weight / 1300)))],
            carrier_rate=None, customer_rate=customer_rate, on_time=True,
            pinned_slot=slot, is_day11=True, behavior=behavior,
            expected_winner=winner,
        )
        load.created_at = created                            # type: ignore[attr-defined]
        load.final_last_modified = created                   # type: ignore[attr-defined]
        load.appearances.append(
            Appearance(slot, ACTIVE, None, False, created, note="fresh, uncovered")
        )
        plan.day11.append(load)


# ---------------------------------------------------------------------------
# Slot packing
# ---------------------------------------------------------------------------


def pack_slots(plan: BrokerPlan) -> None:
    """Assign every non-pinned backfill load a history slot.

    Deterministic: the free-slot list is shuffled with the broker's seeded RNG
    and consumed in order, skipping slots that violate a load's ``max_slot``.
    """
    rng = plan.rng
    used: dict[int, int] = {s: 0 for s in range(HISTORY_SLOTS)}

    pinned = [ld for ld in plan.loads if ld.pinned_slot is not None]
    free_loads = [ld for ld in plan.loads if ld.pinned_slot is None]

    for ld in pinned:
        if ld.appearances:
            for ap in ld.appearances:
                if not ap.rate_only:
                    used[ap.slot] += 1
        else:
            used[ld.pinned_slot] += 1   # type: ignore[index]

    # capacity vector
    capacity: dict[int, int] = {}
    for s in range(HISTORY_SLOTS):
        capacity[s] = 0 if s in EMPTY_HISTORY_SLOTS else max(0, 3 - used[s])

    total_cap = sum(capacity.values())
    need = len(free_loads)
    assert total_cap >= need, f"{plan.cfg.key}: capacity {total_cap} < need {need}"

    # Trim surplus capacity so some files carry 1-2 loads, not always 3.
    surplus = total_cap - need
    trim_order = sorted((s for s in capacity if capacity[s] > 0), key=lambda s: (s * 7919) % 40)
    ti = 0
    while surplus > 0:
        s = trim_order[ti % len(trim_order)]
        ti += 1
        if capacity[s] > 0:
            capacity[s] -= 1
            surplus -= 1

    slots_pool: list[int] = []
    for s in range(HISTORY_SLOTS):
        slots_pool.extend([s] * capacity[s])
    rng.shuffle(slots_pool)

    for ld in free_loads:
        chosen = None
        for i, s in enumerate(slots_pool):
            if s <= ld.max_slot:
                chosen = slots_pool.pop(i)
                break
        assert chosen is not None, f"{plan.cfg.key}: no slot <= {ld.max_slot} for {ld.key}"
        ld.pinned_slot = chosen
        schedule_load(plan, ld, chosen)

    for ld in pinned:
        if not ld.appearances:
            schedule_load(plan, ld, ld.pinned_slot)   # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Public id assignment (per TMS)
# ---------------------------------------------------------------------------

_SF_ALPHA = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"


def sf_id(rng: random.Random, prefix: str) -> str:
    return prefix + "".join(_SF_ALPHA[rng.randrange(len(_SF_ALPHA))]
                            for _ in range(18 - len(prefix)))


def assign_public_ids(plan: BrokerPlan) -> dict:
    """Stable, TMS-shaped public identifiers, assigned in slot order."""
    rng = plan.rng
    tms = plan.cfg.tms
    ordered = sorted(plan.loads + plan.day11, key=lambda ld: (ld.pinned_slot or 0, ld.key))

    ids: dict = {"loads": {}, "carriers": {}, "customers": {}, "locations": {}}

    for i, ld in enumerate(ordered):
        if tms == "A":
            ld.public_id = str(127400000 + i * 137 + rng.randrange(3, 97))
        elif tms == "B":
            ld.public_id = f"HD-2026-{4400 + i * 7 + rng.randrange(1, 6):06d}"
        else:
            ld.public_id = sf_id(rng, "a0jO900000")
        ids["loads"][ld.key] = ld.public_id
    assert len({ld.public_id for ld in ordered}) == len(ordered), "duplicate load id"

    for i, c in enumerate(plan.cfg.carriers):
        if tms == "A":
            ids["carriers"][c.role] = 830000 + i * 611 + rng.randrange(7, 199)
        elif tms == "B":
            ids["carriers"][c.role] = 66800 + i * 43 + rng.randrange(1, 17)
        else:
            ids["carriers"][c.role] = sf_id(rng, "0011I00000")

    for i, name in enumerate(plan.cfg.customers):
        if tms == "A":
            ids["customers"][name] = 880000 + i * 907 + rng.randrange(5, 301)
        elif tms == "B":
            ids["customers"][name] = f"C-{31 + i * 7:04d}"
        else:
            ids["customers"][name] = sf_id(rng, "0011I00000")

    if tms == "C":
        zips_used = sorted({s.place.zip for ld in ordered for s in ld.stops})
        for z in zips_used:
            ids["locations"][z] = sf_id(rng, "0011I00000")
    return ids


LOCATION_SUFFIXES = ("Distribution Ctr", "Cold Storage", "Cross Dock", "Logistics Park",
                     "Warehouse", "Freight Terminal", "Yard", "Fulfillment Ctr")


def location_name(p: Place) -> str:
    return f"{p.city} {LOCATION_SUFFIXES[int(p.zip) % len(LOCATION_SUFFIXES)]}"


# ---------------------------------------------------------------------------
# TMS writers
# ---------------------------------------------------------------------------

# INVARIANT 5: unknown equipment must never be emitted as dry van. These maps
# therefore have NO ``UNKNOWN`` entry -- a lookup for it is a KeyError, not a
# silent dry-van fixture. Go through ``equip_a`` / ``equip_b`` / ``equip_c``.
EQUIP_A = {DRY_VAN: "53 ft Van | Dry", REEFER: "53 ft Van | Reefer",
           FLATBED: "48 ft Flatbed"}
EQUIP_B = {DRY_VAN: "V", REEFER: "R", FLATBED: "F"}
EQUIP_C = {DRY_VAN: "Dry Van", REEFER: "Reefer", FLATBED: "Flatbed", UNKNOWN: None}

# The one legitimate way for TMS A to say "we don't know": a free-text field
# that is simply blank. The adapter must map it to UNKNOWN, never to DRY_VAN.
EQUIP_A_UNKNOWN = ""


def equip_a(equipment: str) -> str:
    """TMS A -- free text. UNKNOWN emits blank, which is what a real free-text
    equipment column looks like when nobody filled it in."""
    if equipment == UNKNOWN:
        return EQUIP_A_UNKNOWN
    return EQUIP_A[equipment]


def equip_b(equipment: str) -> str:
    """TMS B -- a closed ``V``/``R``/``F`` code column with no null and no
    'unknown' code. There is no honest way to express UNKNOWN here, so an
    UNKNOWN load must not be routed to broker B in the first place."""
    if equipment == UNKNOWN:
        raise ValueError(
            "TMS B has no representation for UNKNOWN equipment; its `equip` "
            "column is V/R/F only. Emitting 'V' would violate invariant 5 "
            "(null equipment must never become DRY_VAN). Give this load real "
            "equipment or move the UNKNOWN fixture to TMS C, whose "
            "bos__Equipment_Type__c is nullable."
        )
    return EQUIP_B[equipment]


def equip_c(equipment: str) -> str | None:
    """TMS C -- nullable picklist. This is where the UNKNOWN fixture lives."""
    return EQUIP_C[equipment]

STATUS_A = {PLANNED: "Quoting", ACTIVE: "Booking", COVERED: "Dispatched",
            IN_TRANSIT: "En Route", DELIVERED: "Delivered", COMPLETED: "Completed"}
STATUS_B = {PLANNED: 10, ACTIVE: 20, COVERED: 30, IN_TRANSIT: 40,
            DELIVERED: 50, COMPLETED: 90}
STATUS_C = {PLANNED: "Quotes Requested", ACTIVE: "Ready to Book", COVERED: "Booked",
            IN_TRANSIT: "In Transit", DELIVERED: "Delivered", COMPLETED: "Paid"}


def _appearances_by_slot(plan: BrokerPlan) -> dict[int, list[tuple[LoadPlan, Appearance]]]:
    by_slot: dict[int, list[tuple[LoadPlan, Appearance]]] = {s: [] for s in range(TOTAL_SLOTS)}
    for ld in plan.loads + plan.day11:
        for ap in ld.appearances:
            by_slot[ap.slot].append((ld, ap))
    for s in by_slot:
        by_slot[s].sort(key=lambda pair: pair[0].key)
    return by_slot


def write_tms_a(plan: BrokerPlan, ids: dict) -> dict[str, dict]:
    """TMS A -- FreightFlow: nested camelCase REST, US units, ISO + offset."""
    files: dict[str, dict] = {}
    by_slot = _appearances_by_slot(plan)
    for slot in range(TOTAL_SLOTS):
        sync = slot_local(slot)
        loads_json = []
        for ld, ap in by_slot[slot]:
            carrier = None
            if ap.status in (COVERED, IN_TRANSIT, DELIVERED, COMPLETED) and ld.carrier_role:
                c = _carrier(plan.cfg, ld.carrier_role)
                carrier = {
                    "carrierMasterId": ids["carriers"][c.role],
                    "name": c.name,
                    "mcNumber": c.mc,
                    "dotNumber": c.dot,
                    "phoneNumber": c.phone,
                }
            stops_json = []
            for i, st in enumerate(ld.stops):
                if i == 0:
                    kind = "First Pickup"
                elif i == len(ld.stops) - 1:
                    kind = "Last Drop"
                else:
                    kind = "Drop"
                # Appointment window. When we know the actual and it fell on the
                # scheduled date, anchor the window so the actual sits inside
                # it; a LATE load's actual is outside on purpose (D7).
                start_h = 8
                if st.actual is not None and st.actual.date() == st.sched_date:
                    start_h = max(5, min(14, st.actual.hour - 2))
                ready = (datetime.combine(st.sched_date, datetime.min.time())
                         + timedelta(hours=start_h))
                close = ready + timedelta(hours=9)
                stops_json.append({
                    "stopType": kind,
                    "city": st.place.city.upper(),
                    "state": st.place.state,
                    "zipCode": st.place.zip,
                    "estimatedReadyDateTime": iso_a(ready),
                    "estimatedCloseDateTime": iso_a(close),
                    "actualDepartureDateTime": (
                        iso_a(st.actual) if (ap.show_actuals and st.actual) else None
                    ),
                })
            loads_json.append({
                "shipmentId": int(ld.public_id),
                "status": STATUS_A[ap.status],
                "mileage": ld.miles,
                "totalSell": ld.customer_rate,
                "totalBuy": ap.carrier_rate,
                "customer": {
                    "customerId": ids["customers"][ld.customer],
                    "name": ld.customer,
                },
                "carrier": carrier,
                "equipment": equip_a(ld.equipment),
                "weightTotal": ld.weight_lbs,
                "stops": stops_json,
                "createdDate": iso_a(ld.created_at),          # type: ignore[attr-defined]
                "lastModifiedDate": iso_a(ap.last_modified),
            })
        files[slot_filename(slot)] = {"syncedAt": iso_a(sync), "loads": loads_json}
    return files


def write_tms_b(plan: BrokerPlan, ids: dict) -> dict[str, dict]:
    """TMS B -- HaulDesk: flat table dump, kg/km, naive Central, append-only rates."""
    files: dict[str, dict] = {}
    by_slot = _appearances_by_slot(plan)
    seen_carriers: set[str] = set()
    rate_seq = 910000

    for slot in range(TOTAL_SLOTS):
        sync = slot_local(slot)
        loads_json: list[dict] = []
        carriers_json: list[dict] = []
        rates_json: list[dict] = []

        for ld, ap in by_slot[slot]:
            pu, dl = ld.stops[0], ld.stops[-1]
            carrier_id = None
            if ap.status in (COVERED, IN_TRANSIT, DELIVERED, COMPLETED) and ld.carrier_role:
                c = _carrier(plan.cfg, ld.carrier_role)
                carrier_id = ids["carriers"][c.role]
                if c.role not in seen_carriers:
                    seen_carriers.add(c.role)
                    home = place(c.home_zip)
                    carriers_json.append({
                        "carrier_id": carrier_id,
                        "carrier_name": c.name,
                        "mc_no": c.mc,
                        "dot_no": c.dot,
                        "home_city": home.city,
                        "home_state": home.state,
                        "phone": f"({c.phone[2:5]}) {c.phone[5:8]}-{c.phone[8:]}",
                    })

            if not ap.rate_only:
                loads_json.append({
                    "load_num": ld.public_id,
                    "status_code": STATUS_B[ap.status],
                    "customer_code": ids["customers"][ld.customer],
                    "customer_name": ld.customer,
                    "carrier_ref": carrier_id,
                    "equip": equip_b(ld.equipment),
                    "weight_kg": round(ld.weight_lbs / 2.20462, 1),
                    "dist_km": round(ld.miles / 0.621371, 1),
                    "pu_city": pu.place.city,
                    "pu_state": pu.place.state,
                    "pu_zip": pu.place.zip,
                    "pu_date": pu.sched_date.isoformat(),
                    "pu_departed_at": (naive_b(pu.actual)
                                       if (ap.show_actuals and pu.actual) else None),
                    "del_city": dl.place.city,
                    "del_state": dl.place.state,
                    "del_zip": dl.place.zip,
                    "del_date": dl.sched_date.isoformat(),
                    "del_arrived_at": (naive_b(dl.actual)
                                       if (ap.show_actuals and dl.actual) else None),
                    "entered_at": naive_b(ld.created_at),     # type: ignore[attr-defined]
                    "updated_at": naive_b(ap.last_modified),
                })

            # ---- money: append-only line items, summed by the adapter --------
            new_lines: list[tuple[str, str, float]] = []
            if ap.adjustment_pay is not None:
                new_lines.append(("pay", "ADJUSTMENT", ap.adjustment_pay))
            elif ld.is_day11:
                new_lines.append(("bill", "LINEHAUL", ld.customer_rate))
            elif ap.carrier_rate is not None:
                prior = _prior_pay_total(ld, ap)
                delta_pay = round(ap.carrier_rate - prior, 2)
                if abs(delta_pay) > 0.001:
                    if prior == 0.0:
                        fuel = round(delta_pay * 0.18, 2)
                        new_lines.append(("pay", "LINEHAUL", round(delta_pay - fuel, 2)))
                        new_lines.append(("pay", "FUEL", fuel))
                    else:
                        new_lines.append(("pay", "ACCESSORIAL", delta_pay))
                if not _billed_before(ld, ap):
                    bill_fuel = round(ld.customer_rate * 0.15, 2)
                    new_lines.append(("bill", "LINEHAUL",
                                      round(ld.customer_rate - bill_fuel, 2)))
                    new_lines.append(("bill", "FUEL", bill_fuel))
            for side, code, amount in new_lines:
                rate_seq += 1
                rates_json.append({
                    "rate_id": rate_seq,
                    "load_num": ld.public_id,
                    "side": side,
                    "code": code,
                    "amount_usd": amount,
                    "created_at": naive_b(ap.last_modified),
                })

        files[slot_filename(slot)] = {
            "synced_at": naive_b(sync),
            "loads": loads_json,
            "carriers": carriers_json,
            "rates": rates_json,
        }
    return files


def _prior_pay_total(ld: LoadPlan, ap: Appearance) -> float:
    total = 0.0
    for prev in ld.appearances:
        if prev is ap:
            break
        if prev.adjustment_pay is not None:
            total += prev.adjustment_pay
        elif prev.carrier_rate is not None:
            total = prev.carrier_rate
    return round(total, 2)


def _billed_before(ld: LoadPlan, ap: Appearance) -> bool:
    for prev in ld.appearances:
        if prev is ap:
            return False
        if prev.status in (COVERED, IN_TRANSIT, DELIVERED, COMPLETED) or prev.carrier_rate:
            return True
    return False


def write_tms_c(plan: BrokerPlan, ids: dict) -> dict[str, dict]:
    """TMS C -- BrokerOS: CRM records + referenced_records, UTC timestamps.

    DECISIONS.md D2: Carrier Accounts carry ``bos__MC_Number__c`` and
    ``bos__DOT_Number__c``, a deliberate, disclosed extension of the provided
    schema so cross-TMS carrier identity has a real key.
    """
    files: dict[str, dict] = {}
    by_slot = _appearances_by_slot(plan)
    for slot in range(TOTAL_SLOTS):
        sync = slot_local(slot)
        records: list[dict] = []
        refs: dict[str, dict] = {}

        for ld, ap in by_slot[slot]:
            cust_id = ids["customers"][ld.customer]
            refs[cust_id] = {"type": "Account", "record_type": "Customer", "Name": ld.customer}

            carrier_id = None
            if ap.status in (COVERED, IN_TRANSIT, DELIVERED, COMPLETED) and ld.carrier_role:
                c = _carrier(plan.cfg, ld.carrier_role)
                carrier_id = ids["carriers"][c.role]
                refs[carrier_id] = {
                    "type": "Account",
                    "record_type": "Carrier",
                    "Name": c.name,
                    "bos__MC_Number__c": c.mc,
                    "bos__DOT_Number__c": c.dot,
                }

            stops_json = []
            for i, st in enumerate(ld.stops):
                loc_id = ids["locations"][st.place.zip]
                refs[loc_id] = {
                    "type": "Location",
                    "Name": location_name(st.place),
                    "bos__City__c": st.place.city,
                    "bos__State__c": st.place.state,
                    "bos__Postal_Code__c": st.place.zip,
                }
                stops_json.append({
                    "bos__Number__c": float(i + 1),
                    "bos__Is_Pickup__c": i == 0,
                    "bos__Is_Dropoff__c": i > 0,
                    "bos__Location__c": loc_id,
                    "bos__Scheduled_Date__c": st.sched_date.isoformat(),
                    # st.actual is the DEPARTURE from the stop; the truck
                    # arrived roughly an hour earlier at the shipper.
                    "bos__Arrival_Time__c": (
                        utc_c(st.actual - timedelta(hours=1) if i == 0 else st.actual)
                        if (ap.show_actuals and st.actual) else None
                    ),
                })

            records.append({
                "Id": ld.public_id,
                "Name": ld.name_c,                        # type: ignore[attr-defined]
                "bos__Load_Status__c": STATUS_C[ap.status],
                "bos__Distance_Miles__c": ld.miles,
                "bos__Customer__c": cust_id,
                "bos__Carrier__c": carrier_id,
                "bos__Equipment_Type__c": equip_c(ld.equipment),
                "bos__Customer_Rate__c": ld.customer_rate,
                "bos__Carrier_Rate__c": ap.carrier_rate,
                "bos__Stops__r": stops_json,
                "bos__Line_Items__r": [
                    {
                        "bos__Commodity__c": li.commodity,
                        "bos__Weight__c": li.weight,
                        "bos__Weight_Units__c": li.units,
                        "bos__Pallet_Count__c": li.pallets,
                    }
                    for li in ld.line_items
                ],
                "CreatedDate": utc_c(ld.created_at),          # type: ignore[attr-defined]
                "LastModifiedDate": utc_c(ap.last_modified),
            })

        files[slot_filename(slot)] = {
            "synced_at": utc_c(sync),
            "records": records,
            "referenced_records": refs,
        }
    return files


# ---------------------------------------------------------------------------
# Reference model -- the numbers the traceability table asserts
# ---------------------------------------------------------------------------
#
# This is a *reference* implementation of PRD sections 7-9 plus DECISIONS D5/D6/D7,
# written here so TRACEABILITY.md is computed from the fixtures rather than from
# whatever the production code later emits. Two spots where the PRD leaves a
# free parameter are pinned explicitly and stated in TRACEABILITY.md:
#   * recency decay   = exp(-days_since_last_lane_load / 30)
#   * deadhead credit = clamp((250 - miles) / 200, 0, 1)   [full <=50, zero >=250]


def shown(value: float, places: int) -> Decimal:
    """The number TRACEABILITY.md prints, as the number it also computes with.

    DECISIONS D18: every figure in that document has to be reproducible from the
    figures printed beside it. Displaying a rounded factor while multiplying the
    unrounded one is how ``2.0200 x 187.2 = $378.15`` got written down. So a
    factor is quantized to its displayed precision *first* and the product is
    taken from the quantized value, never the other way round.

    Half-up, because that is what Postgres does storing a ``NUMERIC(10,4)`` and
    what a person does with a calculator; ``Decimal(str(...))`` so the input is
    the float's own shortest representation rather than its binary tail.
    """
    return Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def usd(rate: Decimal, miles: float) -> Decimal:
    """``rate x miles`` in dollars, from the rate exactly as displayed.

    Mirrors ``app.domain.pricing._dollars`` (``round(rate * miles, 2)`` over a
    rate read back out of ``lane_stats.rate_per_mile_p* NUMERIC(10,4)``) — same
    4-dp rate in, same cents out — but in exact decimal arithmetic, so the
    hand-check on a calculator lands on the printed cent too.
    """
    return (rate * Decimal(str(miles))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def pct_cont(values: list[float], p: float) -> float:
    """PostgreSQL ``percentile_cont`` semantics: linear interpolation."""
    xs = sorted(values)
    if not xs:
        return 0.0
    if len(xs) == 1:
        return xs[0]
    idx = p * (len(xs) - 1)
    lo = math.floor(idx)
    hi = math.ceil(idx)
    if lo == hi:
        return xs[lo]
    return xs[lo] + (idx - lo) * (xs[hi] - xs[lo])


def _tier_key(load: LoadPlan, tier: str) -> tuple:
    if tier == TIER_ZIP3:
        return (load.origin.zip3, load.dest.zip3)
    if tier == TIER_METRO:
        return (load.origin.metro, load.dest.metro)
    return (REGION,)


def tier_walk(history: list[LoadPlan], target: LoadPlan) -> tuple[str, list[LoadPlan], list[tuple[str, int]]]:
    """Walk ZIP3 -> METRO -> REGION -> REGION_ANY, 5-load minimum. Returns the
    accepted tier, its backing loads, and the count seen at every rung tried."""
    trace: list[tuple[str, int]] = []
    for tier in (TIER_ZIP3, TIER_METRO, TIER_REGION, TIER_REGION_ANY):
        if tier == TIER_REGION_ANY:
            matched = list(history)
        else:
            key = _tier_key(target, tier)
            matched = [
                h for h in history
                if _tier_key(h, tier) == key
                and (target.equipment == UNKNOWN or h.equipment == target.equipment)
            ]
        trace.append((tier, len(matched)))
        if len(matched) >= MIN_TIER_LOADS:
            return tier, matched, trace
    return TIER_REGION_ANY, list(history), trace


def confidence(tier: str, n: int) -> str:
    if tier in (TIER_REGION, TIER_REGION_ANY):
        return "low"
    if n >= 15:
        return "high"
    if n >= MIN_TIER_LOADS:
        return "medium"
    return "low"


DAY11_REF = date(2026, 7, 16)


def deadhead_credit(miles: float) -> Decimal:
    """``clamp((250 - miles) / 200, 0, 1)`` in exact decimal, from the printed miles.

    Decimal rather than float because the fixture has deadheads like 219.9 mi,
    where the answer is exactly 0.1505 — a 3-dp tie. In binary that lands at
    0.150499999999999967 and rounds *down*, so the table would print 0.150 beside
    a distance that a reader's calculator turns into 0.151 (D18).
    """
    return max(Decimal(0), min(Decimal(1), (250 - Decimal(str(miles))) / 200))


def recency_credit(days: float | None) -> float:
    if days is None:
        return 0.0
    return math.exp(-days / 30.0)


#: The five PRD section 8 weights, as decimals so the term-by-term arithmetic
#: printed in TRACEABILITY.md is the arithmetic that produced the score.
W_EXPERIENCE = Decimal("0.35")
W_RECENCY = Decimal("0.20")
W_EQUIPMENT = Decimal("0.15")
W_DEADHEAD = Decimal("0.20")
W_ON_TIME = Decimal("0.10")

#: Decimal places each signal is *printed* to, and therefore computed at (D18).
DP_SIGNAL = 3
DP_EQUIPMENT = 2
DP_TERM = 4
DP_SCORE = 1


@dataclass
class CarrierScore:
    """One carrier's five signals and its score, at the precision they are shown.

    Every field the document prints is a :class:`~decimal.Decimal` already
    rounded to its display precision, and ``score`` is built by weighting those
    rounded values rather than the floats behind them (D18). The cost is at most
    0.05 of a score point against an infinite-precision model; the benefit is
    that the ranking table is arithmetic a reader can redo in the margin.
    """

    role: str
    name: str
    lane_loads: int
    experience: Decimal
    days_since_lane: float | None
    recency: Decimal
    equipment_ok: bool
    equipment: Decimal
    last_delivery_place: Place | None
    last_delivery_at: datetime | None
    deadhead_miles: float | None
    deadhead: Decimal
    on_time_obs: int
    on_time_n: int
    on_time: Decimal
    lane_avg_on_time: Decimal
    avg_rpm: float | None
    terms: tuple[Decimal, ...]
    score: Decimal


def score_carriers(plan: BrokerPlan, target: LoadPlan, tier: str,
                   backing: list[LoadPlan]) -> list[CarrierScore]:
    history = plan.loads
    lane_ontime = [1 if h.on_time else 0 for h in backing]
    # Rounded here, because the shrinkage line in the document states this
    # average and then divides by it; the reader's arithmetic has to close.
    lane_avg = shown(Decimal(sum(lane_ontime)) / len(lane_ontime) if lane_ontime
                     else Decimal("0.85"), DP_SIGNAL)

    out: list[CarrierScore] = []
    for c in plan.cfg.carriers:
        mine = [h for h in backing if h.carrier_role == c.role]
        n = len(mine)
        experience = shown(Decimal(n) / (n + SHRINK_K), DP_SIGNAL)

        if mine:
            last_lane = max(h.delivered_at for h in mine if h.delivered_at)
            days = (DAY11_REF - last_lane.date()).days
        else:
            days = None
        rec = shown(recency_credit(days), DP_SIGNAL)

        all_mine = [h for h in history if h.carrier_role == c.role]
        equip_ok = any(h.equipment == target.equipment for h in all_mine)
        if target.equipment == UNKNOWN:
            equip = 0.5
        else:
            equip = 1.0 if equip_ok else 0.0
        equip = shown(equip, DP_EQUIPMENT)

        delivered = [h for h in all_mine if h.delivered_at]
        if delivered:
            last = max(delivered, key=lambda h: h.delivered_at)
            dh_miles = round(road_miles_between(last.dest, target.origin), 1)
            dh = shown(deadhead_credit(dh_miles), DP_SIGNAL)
            last_place, last_at = last.dest, last.delivered_at
        else:
            dh_miles, dh, last_place, last_at = None, shown(0.0, DP_SIGNAL), None, None

        obs = sum(1 for h in mine if h.on_time)
        on_time = shown((obs + lane_avg * SHRINK_K) / (n + SHRINK_K), DP_SIGNAL)

        rpms = [h.rpm for h in mine if h.carrier_rate]
        avg_rpm = (sum(rpms) / len(rpms)) if rpms else None

        terms = tuple(
            shown(w * v, DP_TERM)
            for w, v in ((W_EXPERIENCE, experience), (W_RECENCY, rec),
                         (W_EQUIPMENT, equip), (W_DEADHEAD, dh), (W_ON_TIME, on_time))
        )
        score = shown(100 * sum(terms), DP_SCORE)
        out.append(CarrierScore(
            role=c.role, name=c.name, lane_loads=n, experience=experience,
            days_since_lane=days, recency=rec, equipment_ok=equip_ok, equipment=equip,
            last_delivery_place=last_place, last_delivery_at=last_at,
            deadhead_miles=dh_miles, deadhead=dh,
            on_time_obs=obs, on_time_n=n, on_time=on_time, lane_avg_on_time=lane_avg,
            avg_rpm=avg_rpm, terms=terms, score=score,
        ))
    out.sort(key=lambda s: (-s.score, s.role))
    return out


# ---------------------------------------------------------------------------
# Traceability table
# ---------------------------------------------------------------------------


def build_traceability(plans: list[BrokerPlan]) -> str:
    lines: list[str] = []
    w = lines.append

    w("# Day-11 traceability")
    w("")
    w("Generated by `backend/scripts/generate_data.py`. **Provenance, stated exactly:** "
      "every number below is computed by that script from the same seeded load plan it "
      "writes the 132 JSON files from — not written by hand, and not read back out of the "
      "JSON. What ties the two together is `backend/scripts/validate_data.py`, which "
      "reconciles the emitted bytes against that plan appearance by appearance (miles, "
      "carrier rate, weight, equipment, status, stops, carrier identity, and TMS B's running "
      "`pay` sum), and fails if they diverge. So: these numbers are true of the fixtures "
      "because the reconciliation passes, not because the table was derived from them.")
    w("")
    w("The one thing it shares with the production code is geography: `app.domain.geo`'s "
      "hardcoded table and `app.domain.distance`'s Haversine x 1.2, deliberately, so the "
      "fixtures and the system agree on where places are. Everything the platform is actually "
      "judged on — the adapters, ingestion, the tier walk, scoring and pricing — is not "
      "imported here and did not exist when this table was computed. That is the point: it is "
      "an independent expectation to measure the system against, not an echo of it. "
      "Re-running the generator regenerates this file.")
    w("")
    w("This is the source of the end-to-end assertions (TASKS.md H1) and the demo script.")
    w("")
    w("## How to read a row, and what is asserted")
    w("")
    w("For each day-11 load: the behavior it proves, the tier walk with the count at every")
    w("rung, the exact historical loads backing the accepted tier, the price arithmetic, and")
    w("the carrier ranking with each signal shown separately. A human with a calculator and")
    w("the sync files can reproduce all of it.")
    w("")
    w("**Hard assertions** (these must hold exactly):")
    w("")
    w("- the accepted tier and the load count backing it")
    w("- the identity of the top-ranked carrier")
    w("- median / p25 / p75 $/mi and the resulting price point and range")
    w("- lane-experience `n/(n+5)` and the deadhead distance for the top two carriers")
    w("")
    w("**Reference-model parameters.** PRD section 8 fixes the five weights (0.35 / 0.20 /")
    w("0.15 / 0.20 / 0.10) and both cold-start formulas (DECISIONS D5), but leaves the")
    w("recency decay shape and the deadhead interpolation open. This table pins them:")
    w("")
    w("```")
    w("experience = n / (n + 5)                                    # D5, saturating count")
    w("recency    = exp(-days_since_last_lane_load / 30)")
    w("equipment  = 1 if the carrier has ever hauled it, else 0    # 0.5 if the LOAD is UNKNOWN")
    w("deadhead   = clamp((250 - miles_from_last_delivery) / 200, 0, 1)   # full <=50, zero >=250")
    w("on_time    = (observed + lane_avg * 5) / (n + 5)            # D5, shrunk rate; D7 day-granular")
    w("score      = 100 * (0.35*exp + 0.20*rec + 0.15*equip + 0.20*deadhead + 0.10*on_time)")
    w("percentiles = PostgreSQL percentile_cont (linear interpolation)")
    w("```")
    w("")
    w("**Precision, and why it is part of the model** (DECISIONS D18). Every number below")
    w("is rounded to the precision it is *printed* at before anything is multiplied by it:")
    w("signals and the shrinkage lane average to 3 dp, equipment to 2 dp, each weighted")
    w("term to 4 dp, the score to 1 dp, and $/mi percentiles to 4 dp — which is also what")
    w("`lane_stats.rate_per_mile_p*` stores (`NUMERIC(10,4)`). So `2.0200 × 187.2` is")
    w("written as `$378.14`, the cent a calculator returns, not the `$378.15` an")
    w("unrounded rate would give. Rounding is half-up throughout. The cost is at most")
    w("0.05 of a score point against an infinite-precision model; the benefit is that")
    w("every line here can be re-derived from the line above it.")
    w("")
    w("That rounding is this document's convention, not a demand on the implementation:")
    w("the **price** figures are exact — the system stores the same 4-dp rate and reaches")
    w("the same cent — while a **score** computed from unrounded signals may sit up to")
    w("0.05 away from the one printed here. Ranks are unaffected (no margin below is")
    w("under 4 points), which is why the assertions are on rank and not on score.")
    w("")
    w("If the implementation picks a different recency shape, the **ranking** assertions")
    w("still hold — every scenario below is built with a margin — but the absolute scores")
    w("will move. Assert on rank, tier, counts and price; treat scores as indicative.")
    w("")

    for plan in plans:
        cfg = plan.cfg
        history = plan.loads
        w("---")
        w("")
        w(f"# {cfg.key} — {cfg.tms_name} (TMS {cfg.tms})")
        w("")
        w(f"History: **{len(history)} distinct completed loads** across "
          f"{len({ld.pinned_slot for ld in history})} sync files. "
          f"Day 11: **{len(plan.day11)} uncovered loads**.")
        w("")
        w("### Carrier roster")
        w("")
        w("| role | name | MC | DOT | history loads | on-time | last delivery |")
        w("|---|---|---|---|---|---|---|")
        for c in cfg.carriers:
            mine = [h for h in history if h.carrier_role == c.role]
            ot = sum(1 for h in mine if h.on_time)
            last = max((h for h in mine if h.delivered_at), key=lambda h: h.delivered_at, default=None)
            last_s = (f"{last.dest.city} {last.dest.zip} on {last.delivered_at:%Y-%m-%d}"
                      if last else "—")
            w(f"| `{c.role}` | {c.name} | {c.mc} | {c.dot} | {len(mine)} | "
              f"{ot}/{len(mine)} | {last_s} |")
        w("")
        w("### Scenario index — where to look in the files")
        w("")
        w("| scenario | load(s) | files |")
        w("|---|---|---|")
        for label, keys in _SCENARIO_INDEX:
            hits = [ld for ld in history if ld.scenario == keys]
            if not hits:
                continue
            if len(hits) <= 3:
                for h in hits:
                    w(f"| {label} | `{h.public_id}` | "
                      + ", ".join(f"`{slot_filename(a.slot)}`" for a in h.appearances) + " |")
            else:
                w(f"| {label} | {len(hits)} loads: "
                  + ", ".join(f"`{h.public_id}`" for h in hits[:4]) + ", … | "
                  + f"{len({a.slot for h in hits for a in h.appearances})} files |")
        empty_slots = [s for s in range(TOTAL_SLOTS)
                       if not any(a.slot == s for ld in history + plan.day11
                                  for a in ld.appearances)]
        w(f"| empty syncs (valid envelope, nothing changed) | — | "
          + ", ".join(f"`{slot_filename(s)}`" for s in empty_slots) + " |")
        w("")

        for target in plan.day11:
            tier, backing, trace = tier_walk(history, target)
            scores = score_carriers(plan, target, tier, backing)
            rpms = [h.rpm for h in backing]
            # Quantized to the 4 dp the document prints and the system stores
            # (lane_stats.rate_per_mile_p* NUMERIC(10,4)) *before* the dollars
            # are taken from them — DECISIONS D18.
            p25, p50, p75 = (shown(pct_cont(rpms, q), 4) for q in (0.25, 0.50, 0.75))
            low_usd, point_usd, high_usd = (usd(p, target.miles) for p in (p25, p50, p75))
            conf = confidence(tier, len(backing))
            dates = sorted(h.delivered_at.date() for h in backing if h.delivered_at)

            w(f"## {target.key} — `{target.public_id}` — {BEHAVIOR_TITLE[target.behavior]}")
            w("")
            w(f"- **Lane:** {target.origin.city} {target.origin.state} {target.origin.zip} "
              f"({target.origin.metro}/zip3 {target.origin.zip3}) → "
              f"{target.dest.city} {target.dest.state} {target.dest.zip} "
              f"({target.dest.metro}/zip3 {target.dest.zip3})")
            unfiltered = target.equipment == UNKNOWN
            equip_note = (" — **null in the source**, so the tier walk runs with **no "
                          "equipment filter** at any rung (DECISIONS D6) and must never "
                          "be defaulted to DRY_VAN (invariant 5)") if unfiltered else ""
            w(f"- **Equipment:** {target.equipment}{equip_note} · **Miles:** {target.miles} "
              f"(haversine × 1.2, DECISIONS D10) · **Weight:** {target.weight_lbs:,.0f} lbs")
            w(f"- **Customer rate:** ${target.customer_rate:,.2f} · **Carrier rate:** none — "
              f"this is the question")
            w(f"- **Status:** {_status_label(cfg.tms)} · **carrier:** null · never covered "
              f"in any later file")
            w(f"- **Proves:** {BEHAVIOR_DESC[target.behavior]}")
            w("")
            w("### Tier walk (PRD section 7, minimum 5 loads)")
            w("")
            w("| rung | key | loads found | verdict |")
            w("|---|---|---|---|")
            filt = "all equipment types" if unfiltered else target.equipment
            for t, n in trace:
                key = _tier_key(target, t)
                if t == TIER_REGION_ANY:
                    keystr = f"{REGION} (no equipment filter)"
                elif t == TIER_REGION:
                    keystr = f"{REGION} + {filt}"
                else:
                    keystr = f"{'→'.join(key)} + {filt}"
                verdict = "**ACCEPTED**" if t == tier else f"rejected, {n} < {MIN_TIER_LOADS}"
                w(f"| {t} | `{keystr}` | {n} | {verdict} |")
            w("")
            w(f"**Accepted tier: `{tier}` with {len(backing)} loads** "
              f"({dates[0]} … {dates[-1]}) → confidence **{conf}**.")
            w("")
            mix: dict[str, int] = {}
            for h in backing:
                mix[h.equipment] = mix.get(h.equipment, 0) + 1
            if len(mix) > 1:
                w("Because the equipment filter is off, the pool is **heterogeneous**: "
                  + ", ".join(f"{k} {v}" for k, v in sorted(mix.items()))
                  + ". Every one of those rates goes into the median below, which is exactly "
                    "why this answer must be labeled as unfiltered rather than presented as a "
                    "like-for-like comparison.")
                w("")
            w("### Supporting history")
            w("")
            multi: list[LoadPlan] = []
            for h in backing:
                if len(h.appearances) > 1:
                    multi.append(h)
            if len(backing) <= 25:
                w("| load | delivered | carrier | miles | carrier rate | $/mi | on-time | "
                  "provenance |")
                w("|---|---|---|---|---|---|---|---|")
                for h in sorted(backing, key=lambda x: (x.delivered_at or datetime.min)):
                    cn = _carrier(cfg, h.carrier_role).name if h.carrier_role else "—"
                    if len(h.appearances) > 1:
                        prov = (f"**{h.scenario}**, {len(h.appearances)} files "
                                f"({', '.join(slot_filename(a.slot) for a in h.appearances)})")
                    else:
                        prov = f"one file ({slot_filename(h.appearances[0].slot)})"
                    w(f"| `{h.public_id}` | {h.delivered_at:%Y-%m-%d} | {cn} "
                      f"(`{h.carrier_role}`) | {h.miles} | ${h.carrier_rate:,.2f} | "
                      f"{h.rpm:.3f} | {'yes' if h.on_time else 'no'} | {prov} |")
            else:
                w(f"{len(backing)} loads back this tier — too many to list one per row without "
                  f"burying the arithmetic, so they are aggregated by carrier here. The complete "
                  f"sorted $/mi vector is printed under *Price estimate arithmetic* below, which "
                  f"is what you need to reproduce the percentiles by hand; to recover the "
                  f"individual loads, filter the sync files on "
                  f"`{_provenance_key(target, tier)}` + "
                  f"{'all equipment types' if unfiltered else target.equipment}.")
                w("")
                w("| carrier | loads | median $/mi | min–max $/mi | on-time | date range |")
                w("|---|---|---|---|---|---|")
                by_car: dict[str, list[LoadPlan]] = {}
                for h in backing:
                    by_car.setdefault(h.carrier_role or "—", []).append(h)
                for role in [c.role for c in cfg.carriers if c.role in by_car]:
                    hs = by_car[role]
                    rs = sorted(h.rpm for h in hs)
                    ds = sorted(h.delivered_at.date() for h in hs if h.delivered_at)
                    w(f"| {_carrier(cfg, role).name} (`{role}`) | {len(hs)} | "
                      f"{pct_cont(rs, 0.50):.3f} | {rs[0]:.3f}–{rs[-1]:.3f} | "
                      f"{sum(1 for h in hs if h.on_time)}/{len(hs)} | {ds[0]} … {ds[-1]} |")
            w("")
            if multi:
                w("**Loads in this set that arrived more than once** — the numbers above use the "
                  "LATEST value, which is what makes the correction/rebuild story checkable:")
                w("")
                for h in multi:
                    for a in h.appearances:
                        rate = "null" if a.carrier_rate is None else f"${a.carrier_rate:,.2f}"
                        extra = " *(rate-only: the `loads` array does NOT mention this load)*" \
                            if a.rate_only else ""
                        extra += " *(lastModifiedDate deliberately out of order)*" \
                            if a.out_of_order_lm else ""
                        w(f"- `{h.public_id}` @ `{slot_filename(a.slot)}` → status "
                          f"{a.status}, carrier rate {rate} — {a.note}{extra}")
                w("")
            w("### Price estimate arithmetic")
            w("")
            srt = sorted(rpms)
            w(f"Sorted $/mi ({len(srt)} values): "
              + ", ".join(f"{v:.3f}" for v in srt))
            w("")
            w(f"- p25 = {p25:.4f} $/mi → {p25:.4f} × {target.miles} = "
              f"**${low_usd:,.2f}**")
            w(f"- **median = {p50:.4f} $/mi → {p50:.4f} × {target.miles} = "
              f"${point_usd:,.2f}**  ← point estimate")
            w(f"- p75 = {p75:.4f} $/mi → {p75:.4f} × {target.miles} = "
              f"**${high_usd:,.2f}**")
            w(f"- Provenance line: *median of {len(backing)} loads on "
              f"`{_provenance_key(target, tier)}`, "
              f"{'all equipment types' if unfiltered else target.equipment}, "
              f"{dates[0]} to {dates[-1]}* — confidence **{conf}**")
            if unfiltered and conf != "low":
                w(f"- **Open question for Phase 5, surfaced by this fixture:** PRD section 9 sets")
                w(f"  confidence purely from tier and count, so this answer comes back")
                w(f"  **{conf}** on {len(backing)} loads even though the pool mixes "
                  + " and ".join(sorted(mix)) + " rates.")
                w(f"  That may well be too confident. This row is deliberately planted to force")
                w(f"  the decision rather than to pre-empt it: either PRD section 9 stands and the")
                w(f"  provenance line carries the warning, or unfiltered estimates get capped at")
                w(f"  medium. Whichever is chosen, it should be a recorded decision, not an")
                w(f"  accident of the code.")
            w(f"- Margin check: customer quote ${target.customer_rate:,.2f} vs expected buy "
              f"${point_usd:,.2f} → "
              f"{shown(100 * (Decimal(str(target.customer_rate)) - point_usd) / Decimal(str(target.customer_rate)), 1):.1f}"
              f"% gross")
            w("")
            w("### Carrier ranking")
            w("")
            w("| # | carrier | lane loads n | exp n/(n+5) | days since | recency | equip | "
              "last delivery | deadhead mi | deadhead | on-time | **score** |")
            w("|---|---|---|---|---|---|---|---|---|---|---|---|")
            for i, s in enumerate(scores, start=1):
                last_s = (f"{s.last_delivery_place.city} {s.last_delivery_at:%m-%d}"
                          if s.last_delivery_place else "—")
                w(f"| {i} | {s.name} (`{s.role}`) | {s.lane_loads} | {s.experience:.3f} | "
                  f"{'—' if s.days_since_lane is None else s.days_since_lane} | "
                  f"{s.recency:.3f} | {s.equipment:.2f} | {last_s} | "
                  f"{'—' if s.deadhead_miles is None else f'{s.deadhead_miles:.1f}'} | "
                  f"{s.deadhead:.3f} | {s.on_time:.3f} | **{s.score:.1f}** |")
            w("")
            top, second = scores[0], scores[1]
            w(f"**Expected top carrier: {top.name} (`{top.role}`), score {top.score:.1f}, "
              f"{top.score - second.score:.1f} ahead of {second.name} (`{second.role}`).**")
            w("")
            w("Arithmetic for the top two, term by term:")
            w("")
            for s in (top, second):
                t = s.terms
                w(f"- **{s.name}** — "
                  f"0.35×{s.experience:.3f} = {t[0]:.4f}; "
                  f"0.20×{s.recency:.3f} = {t[1]:.4f}; "
                  f"0.15×{s.equipment:.2f} = {t[2]:.4f}; "
                  f"0.20×{s.deadhead:.3f} = {t[3]:.4f}; "
                  f"0.10×{s.on_time:.3f} = {t[4]:.4f}; "
                  f"sum = {sum(t):.4f} → ×100 = **{s.score:.1f}**")
            w("")
            w(f"  (`{top.role}` lane experience {top.lane_loads}/({top.lane_loads}+5) = "
              f"{top.experience:.3f}; on-time shrunk toward the lane average "
              f"{top.lane_avg_on_time:.3f} with k=5: "
              f"({top.on_time_obs} + {top.lane_avg_on_time:.3f}×5)/({top.on_time_n}+5) = "
              f"{top.on_time:.3f})")
            w("")
            w(f"**Why this is the right answer:** {WHY[target.behavior]}")
            w("")

    lines.extend(_band_separation_section(plans))

    w("---")
    w("")
    w("## Fixture-wide notes")
    w("")
    w("- Miles come from `road_miles_between` (haversine × 1.2). Under DECISIONS D10 that")
    w("  makes Dallas→Houston 271 mi rather than the real-world 239. Every $/mi in this")
    w("  table is computed against those same miles, so the ratios are self-consistent.")
    w("- Every history load has `customer rate > carrier rate`. There are **no deliberate")
    w("  loss-making loads** in the fixture.")
    w("- All three brokers are structurally identical (DECISIONS D9) so tenant isolation is")
    w("  provable: broker A's answers must not move when B and C are ingested — and thanks")
    w("  to the separated rate bands above, if it *does* move, the number moves visibly.")
    w("- Rung coverage across the fixture: ZIP3 fires for the rich-lane and deadhead loads,")
    w("  METRO for the suburb-scatter and unknown-equipment loads, REGION for the thin-lane")
    w("  and (in brokers A and C) the flatbed cold-start load, and REGION_ANY for broker B's")
    w("  flatbed load. All four rungs of PRD section 7 have a fixture.")
    return "\n".join(lines) + "\n"


def _band_separation_section(plans: list[BrokerPlan]) -> list[str]:
    """Cross-broker $/mi comparison — the fixture's tenant-leak tripwire.

    For every day-11 load, compute the accepted-tier median/p25/p75 in the load's
    own broker, in each of the other two brokers on the same lane key, and in the
    illegally-pooled three-broker set.
    """
    lines: list[str] = []
    w = lines.append
    w("---")
    w("")
    w("## Cross-broker rate separation — the tenant-leak tripwire")
    w("")
    w("Each broker prices to its own book "
      + ", ".join(f"`{p.cfg.key}` {p.cfg.price_label} ({p.cfg.rpm_bias:+.2f} $/mi)"
                  for p in plans) + ".")
    w("")
    w("So for every day-11 load below, the same lane key gives a **different** median in")
    w("each broker, and a different one again if the three are illegally pooled. If the")
    w("repository layer ever drops `broker_id`, the reported $/mi and the p25–p75 range move")
    w("by far more than sampling noise — the leak is loud in the money, which is the harm")
    w("DECISIONS D4's threat model is actually about.")
    w("")
    w("| day-11 load | tier / key | own n | own p25 / **median** / p75 | other brokers' medians | "
      "pooled n | pooled p25 / median / p75 |")
    w("|---|---|---|---|---|---|---|")
    for plan in plans:
        for target in plan.day11:
            tier, backing, _ = tier_walk(plan.loads, target)
            own = [h.rpm for h in backing]
            others: list[str] = []
            pooled = list(own)
            for other in plans:
                if other is plan:
                    continue
                o_backing = _same_key_loads(other.loads, target, tier)
                pooled.extend(h.rpm for h in o_backing)
                med = pct_cont([h.rpm for h in o_backing], 0.50) if o_backing else 0.0
                others.append(f"`{other.cfg.key}` {med:.3f} (n={len(o_backing)})")
            w(f"| `{plan.cfg.key}` / {target.key} | {tier} `{_provenance_key(target, tier)}` | "
              f"{len(own)} | {pct_cont(own, .25):.3f} / **{pct_cont(own, .50):.3f}** / "
              f"{pct_cont(own, .75):.3f} | " + " · ".join(others) + f" | {len(pooled)} | "
              f"{pct_cont(pooled, .25):.3f} / {pct_cont(pooled, .50):.3f} / "
              f"{pct_cont(pooled, .75):.3f} |")
    w("")
    w("### What the generator asserts, and at what strength")
    w("")
    w(f"1. **Always** — every own-vs-other-broker median gap on the same lane key is at least")
    w(f"   **{BAND_SEPARATION:.2f} $/mi**. Substituting another broker's history for yours")
    w(f"   changes the headline number, visibly. A regeneration that let two bands re-collide")
    w(f"   fails validation.")
    w("2. **Always** — pooling strictly increases the load count, which is why the provenance")
    w("   line has to report how many loads backed the answer.")
    w(f"3. **At `ZIP3` / `METRO`** — pooling moves at least **two of three** of")
    w(f"   (p25, median, p75) by >= {BAND_SEPARATION:.2f} $/mi.")
    w(f"4. **At `REGION`** — pooling moves the median by >= {BAND_SEPARATION:.2f} $/mi *or*")
    w(f"   widens the reported p25–p75 range by >= 1.5x.")
    w("5. **At `REGION_ANY`** — no money-level assertion, deliberately. See below.")
    w("")
    w("### Two honest limits, both arithmetic rather than fixture weaknesses")
    w("")
    w("**The middle band cannot separate on the median.** With three equally-sized blocks and")
    w("`broker_b` priced between the other two, the pooled *median* of the 3n loads necessarily")
    w("lands inside `broker_b`'s block — the 18th and 19th of 36 sorted values *are*")
    w("`broker_b`'s 6th and 7th. No arrangement of separated bands avoids that while keeping")
    w("the three medians apart. For `broker_b` the tell is the count and the range; for")
    w("`broker_a` and `broker_c` it is all three statistics.")
    w("")
    w("**The wider the tier, the weaker the money signal.** One broker's own rates already span")
    w("~0.9 $/mi across haul lengths and equipment (short reefer vs long dry van), against an")
    w("inter-broker offset of 0.38. On a narrow key haul length barely varies and the bands are")
    w("disjoint; at `REGION` they overlap; at `REGION_ANY` — which *is* \"the whole book, any")
    w("equipment\" — three whole books look much like one, so the distribution test carries no")
    w("information and asserting it would be theatre. Closing that gap would need `broker_c` to")
    w("pay over $3/mi for long-haul dry van, which breaks the plausibility rule that matters")
    w("more. At `REGION_ANY` the leak is caught by the load count and the mandatory")
    w("low-confidence label, not by the price.")
    w("")
    return lines


def _same_key_loads(history: list[LoadPlan], target: LoadPlan, tier: str) -> list[LoadPlan]:
    """Loads in another broker matching the SAME tier key the target resolved to."""
    if tier == TIER_REGION_ANY:
        return list(history)
    key = _tier_key(target, tier)
    return [h for h in history
            if _tier_key(h, tier) == key
            and (target.equipment == UNKNOWN or h.equipment == target.equipment)]


_SCENARIO_INDEX: tuple[tuple[str, str], ...] = (
    ("1 · full lifecycle (6 files, PLANNED→COMPLETED)", "lifecycle"),
    ("2 · correction", "correction"),
    ("3 · rich ZIP3 lane 750→774 dry van", "rich_zip3_lane"),
    ("3 · rich lane depth (other DFW→HOU zip3 pairs)", "rich_lane_depth"),
    ("3 · thin lane SAT→Waco", "thin_lane"),
    ("5 · suburb scatter (DFW→HOU reefer)", "suburb_scatter"),
    ("4 · cold-start flatbed pool", "cold_start_flatbed"),
    ("4/D6 · flatbed kept under 5 region-wide (re-issued on other equipment)",
     "flatbed_kept_scarce"),
    ("7 · deadhead lane Conroe→Austin", "deadhead_lane"),
    ("7 · FAR last delivery (Denton, 264.8 mi from the day-11 pickup)", "deadhead_far_anchor"),
    ("7 · NEAR last delivery (The Woodlands, 16.4 mi away, day 10)", "deadhead_near_anchor"),
    ("anchor · V1 last delivery (Irving)", "v1_home_anchor"),
    ("anchor · V2 last delivery (Arlington)", "v2_home_anchor"),
    ("anchor · V3 last delivery (Schertz)", "v3_home_anchor"),
    ("anchor · M1 last delivery (Houston)", "m1_home_anchor"),
    ("anchor · M2 last delivery (Pasadena)", "m2_home_anchor"),
    ("8 · messy: 3-stop load", "messy_three_stop"),
    ("8 · messy: null equipment → UNKNOWN", "messy_null_equipment"),
    ("8 · messy: kg on one line item of two", "messy_kg_line_item"),
)

BEHAVIOR_TITLE = {
    "rich_lane_zip3": "rich lane, ZIP3 tier fires",
    "metro_scatter": "suburb scatter, METRO tier beats city pairs",
    "thin_lane_region": "thin lane, walk falls through to REGION",
    "deadhead_win": "deadhead proximity decides the winner",
    "cold_start_surfacing": "cold-start shrinkage on a sparse lane",
    "region_any_rare_equipment": "rare equipment drives the walk to REGION_ANY (rung 4)",
    "unknown_equipment_no_filter": "UNKNOWN equipment skips the equipment filter",
}

BEHAVIOR_DESC = {
    "rich_lane_zip3": "the narrowest tier (`ZIP3 + equipment`) clears the 5-load minimum, "
                      "so the walk stops at rung 1 and the estimate is high/medium confidence",
    "metro_scatter": "the zip3 pair is too thin, but metro clustering collects the same real "
                     "lane expressed through different suburbs, so METRO fires where city-pair "
                     "matching would have found nothing",
    "thin_lane_region": "both narrow tiers fall short, the walk reaches REGION, and the answer "
                        "is labeled low confidence rather than hidden",
    "deadhead_win": "a carrier that is behind on lane experience AND recency still wins, purely "
                    "on the proximity of its last delivery",
    "cold_start_surfacing": "a 2-load carrier surfaces with an honest reason but cannot leapfrog "
                            "a carrier with 4, because experience saturates as n/(n+5)",
    "region_any_rare_equipment": "this broker has only 4 flatbed loads in its whole history, so "
                                 "ZIP3, METRO and REGION all fall short with the equipment "
                                 "filter on. Rung 4 drops the filter so the load still gets an "
                                 "answer instead of nothing, and that answer is low confidence "
                                 "by rule, not by count (DECISIONS D6)",
    "unknown_equipment_no_filter": "the load's own equipment is null, so per D6 the filter is "
                                   "skipped at every rung and the provenance must read \"all "
                                   "equipment types\". Invariant 5 forbids inferring DRY_VAN",
}

WHY = {
    "rich_lane_zip3": "the veteran has by far the most loads on this exact zip3 pair AND its "
                      "last drop of the history is a few miles from the pickup, so it leads on "
                      "experience, recency and deadhead simultaneously.",
    "metro_scatter": "the reefer specialist owns the metro-level lane. Its zip3 pairs differ "
                     "from the day-11 load's, which is exactly why ZIP3 had to fail first.",
    "thin_lane_region": "at REGION the veteran with the deepest dry-van history leads on "
                        "experience, and the deadhead term separates the ones whose trucks are "
                        "actually near San Antonio from the ones sitting in DFW.",
    "deadhead_win": "the lane leader's last drop was 264.8 mi away — past the 250 mi cutoff, so it "
                    "earns ZERO deadhead credit; "
                    "the runner-up on experience delivered ~16 mi from the pickup on day 10 and "
                    "takes full credit. Remove the deadhead term and the ranking flips — which "
                    "is what makes this a real test of the signal.",
    "cold_start_surfacing": "with only 7 flatbed loads region-wide, nobody is a specialist. "
                            "n/(n+5) keeps the 2-load carrier visible (0.286) without letting it "
                            "pass the 4-load carrier (0.444).",
    "region_any_rare_equipment": "at rung 4 the equipment filter is gone, so lane experience is "
                                 "measured over the carrier's ENTIRE regional history rather than "
                                 "its flatbed history. That is why the general-freight veteran "
                                 "leads even though it scores 0.00 on the equipment signal and "
                                 "has never pulled a flatbed — the reason line has to say so out "
                                 "loud. Note this is the one answer in the fixture where a rep "
                                 "would reasonably override the ranking; the estimate is labeled "
                                 "low confidence precisely because the evidence is thin.",
    "unknown_equipment_no_filter": "the narrow zip3 pair holds too few loads, so the walk falls to "
                                   "the metro pair — and because the load's equipment is unknown, "
                                   "that metro pool keeps its dry van AND reefer loads. The lane "
                                   "veteran leads on both experience and truck position.",
}


def _status_label(tms: str) -> str:
    return {"A": '`"Booking"`', "B": "`status_code` 20 (Open)", "C": '`"Ready to Book"`'}[tms]


def _provenance_key(target: LoadPlan, tier: str) -> str:
    if tier == TIER_ZIP3:
        return f"{target.origin.zip3}→{target.dest.zip3}"
    if tier == TIER_METRO:
        return f"{target.origin.metro}→{target.dest.metro}"
    if tier == TIER_REGION:
        return REGION
    return f"{REGION} (any equipment)"


# ---------------------------------------------------------------------------
# Build + write
# ---------------------------------------------------------------------------


def build_plan(cfg: BrokerCfg) -> BrokerPlan:
    plan = BrokerPlan(cfg=cfg, rng=random.Random(SEED + cfg.seed_offset))

    scenario_1_full_lifecycle(plan)
    scenario_2_corrections(plan)
    scenario_3a_rich_zip3_lane(plan)
    scenario_5_suburb_scatter(plan)
    scenario_3b_rich_lane_depth(plan)
    scenario_3c_thin_lane(plan)
    scenario_7_deadhead_setup(plan)
    scenario_4_cold_start_flatbed(plan)
    scenario_backhaul(plan)
    scenario_breadth_and_messy_edges(plan)

    assert len(plan.loads) == 93, f"{cfg.key}: {len(plan.loads)} history loads, expected 93"
    for role, target in _ROLE_TARGETS.items():
        got = sum(1 for ld in plan.loads if ld.carrier_role == role)
        assert got == target, f"{cfg.key}: carrier {role} has {got} loads, expected {target}"

    pack_slots(plan)
    scenario_day11(plan)
    return plan


def render(plan: BrokerPlan) -> dict[str, dict]:
    ids = plan.ids = assign_public_ids(plan)
    if plan.cfg.tms == "C":
        # human-readable load numbers alongside the opaque CRM Ids
        for i, ld in enumerate(sorted(plan.loads + plan.day11,
                                      key=lambda x: (x.pinned_slot or 0, x.key))):
            ld.name_c = f"SHP{6700000 + i * 13 + plan.cfg.seed_offset:07d}"  # type: ignore[attr-defined]
    writer = {"A": write_tms_a, "B": write_tms_b, "C": write_tms_c}[plan.cfg.tms]
    return writer(plan, ids)


def write_all(out_root: Path, plans: list[BrokerPlan]) -> int:
    count = 0
    for plan in plans:
        d = out_root / plan.cfg.dirname
        d.mkdir(parents=True, exist_ok=True)
        for fname, payload in render(plan).items():
            (d / fname).write_text(
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            count += 1
    return count


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------

A_LOAD_KEYS = {"shipmentId", "status", "mileage", "totalSell", "totalBuy", "customer",
               "carrier", "equipment", "weightTotal", "stops", "createdDate",
               "lastModifiedDate"}
A_STOP_KEYS = {"stopType", "city", "state", "zipCode", "estimatedReadyDateTime",
               "estimatedCloseDateTime", "actualDepartureDateTime"}
A_STATUSES = {"Quoting", "Booking", "Dispatched", "At Shipper", "En Route", "At Receiver",
              "Delivered", "Completed"}
# Free text, so the set is ours -- but it must not silently grow a fourth value.
A_EQUIPMENT = set(EQUIP_A.values()) | {EQUIP_A_UNKNOWN}
B_LOAD_KEYS = {"load_num", "status_code", "customer_code", "customer_name", "carrier_ref",
               "equip", "weight_kg", "dist_km", "pu_city", "pu_state", "pu_zip", "pu_date",
               "pu_departed_at", "del_city", "del_state", "del_zip", "del_date",
               "del_arrived_at", "entered_at", "updated_at"}
B_CARRIER_KEYS = {"carrier_id", "carrier_name", "mc_no", "dot_no", "home_city",
                  "home_state", "phone"}
B_RATE_KEYS = {"rate_id", "load_num", "side", "code", "amount_usd", "created_at"}
C_RECORD_KEYS = {"Id", "Name", "bos__Load_Status__c", "bos__Distance_Miles__c",
                 "bos__Customer__c", "bos__Carrier__c", "bos__Equipment_Type__c",
                 "bos__Customer_Rate__c", "bos__Carrier_Rate__c", "bos__Stops__r",
                 "bos__Line_Items__r", "CreatedDate", "LastModifiedDate"}
C_STOP_KEYS = {"bos__Number__c", "bos__Is_Pickup__c", "bos__Is_Dropoff__c",
               "bos__Location__c", "bos__Scheduled_Date__c", "bos__Arrival_Time__c"}
C_ITEM_KEYS = {"bos__Commodity__c", "bos__Weight__c", "bos__Weight_Units__c",
               "bos__Pallet_Count__c"}
C_STATUSES = {"Quotes Requested", "Ready to Book", "Booked", "In Transit", "Delivered",
              "Invoiced", "Paid"}

REEFER_COMMODITIES = set(COMMODITIES[REEFER])
FLATBED_COMMODITIES = set(COMMODITIES[FLATBED])
VAN_COMMODITIES = set(COMMODITIES[DRY_VAN]) | set(COMMODITIES[UNKNOWN])


class Validator:
    def __init__(self, root: Path, plans: list[BrokerPlan]) -> None:
        self.root = root
        self.plans = plans
        self.errors: list[str] = []
        self.report: list[str] = []

    def fail(self, msg: str) -> None:
        self.errors.append(msg)

    def note(self, msg: str) -> None:
        self.report.append(msg)

    # -- 1 & 2 --------------------------------------------------------------
    def check_grid(self) -> dict[str, dict[str, dict]]:
        expected = [slot_filename(s) for s in range(TOTAL_SLOTS)]
        assert len(expected) == 44
        loaded: dict[str, dict[str, dict]] = {}
        total = 0
        for plan in self.plans:
            d = self.root / plan.cfg.dirname
            actual = sorted(p.name for p in d.glob("*_sync.json"))
            if actual != sorted(expected):
                extra = set(actual) - set(expected)
                missing = set(expected) - set(actual)
                self.fail(f"{plan.cfg.dirname}: grid mismatch. extra={sorted(extra)} "
                          f"missing={sorted(missing)}")
            files: dict[str, dict] = {}
            for name in expected:
                p = d / name
                if not p.exists():
                    continue
                try:
                    files[name] = json.loads(p.read_text(encoding="utf-8"))
                except Exception as exc:  # noqa: BLE001
                    self.fail(f"{plan.cfg.dirname}/{name}: not valid JSON: {exc}")
                total += 1
            loaded[plan.cfg.key] = files
            self.note(f"{plan.cfg.dirname}: {len(actual)} files, grid matches "
                      f"({'OK' if actual == sorted(expected) else 'MISMATCH'})")
        self.note(f"total files parsed as JSON: {total} (expected {3 * TOTAL_SLOTS})")
        if total != 3 * TOTAL_SLOTS:
            self.fail(f"expected {3 * TOTAL_SLOTS} files, parsed {total}")
        return loaded

    # -- 3, 4, 5 ------------------------------------------------------------
    def check_shapes(self, loaded: dict[str, dict[str, dict]]) -> None:
        for plan in self.plans:
            files = loaded[plan.cfg.key]
            getattr(self, f"_shape_{plan.cfg.tms.lower()}")(plan, files)
            # Structure is not enough: the remaining checks read `self.plans`,
            # so on their own they would only prove the generator agrees with
            # itself. Reconcile the BYTES on disk against the plan that wrote
            # them before anything else believes the plan.
            getattr(self, f"_reconcile_{plan.cfg.tms.lower()}")(plan, files)

    def _check_place(self, tag: str, city: str, state: str, zip_code: str) -> Place | None:
        p = resolve_place(city, state, zip_code)
        if p is None:
            self.fail(f"{tag}: stop {city}/{state}/{zip_code} does not resolve")
        return p

    def _shape_a(self, plan: BrokerPlan, files: dict[str, dict]) -> None:
        for name, doc in sorted(files.items()):
            tag = f"{plan.cfg.dirname}/{name}"
            if set(doc) != {"syncedAt", "loads"}:
                self.fail(f"{tag}: envelope keys {sorted(doc)}")
            slot = self._slot_of(name)
            if doc["syncedAt"] != iso_a(slot_local(slot)):
                self.fail(f"{tag}: syncedAt {doc['syncedAt']} != {iso_a(slot_local(slot))}")
            if not 0 <= len(doc["loads"]) <= 3:
                self.fail(f"{tag}: {len(doc['loads'])} loads, expected 0-3")
            for ld in doc["loads"]:
                if set(ld) != A_LOAD_KEYS:
                    self.fail(f"{tag}: load keys {sorted(set(ld) ^ A_LOAD_KEYS)}")
                if ld["status"] not in A_STATUSES:
                    self.fail(f"{tag}: bad status {ld['status']}")
                if ld["equipment"] not in A_EQUIPMENT:
                    self.fail(f"{tag}: unrecognized free-text equipment "
                              f"{ld['equipment']!r} on {ld['shipmentId']}")
                if not isinstance(ld["shipmentId"], int):
                    self.fail(f"{tag}: shipmentId not int")
                if ld["carrier"] is not None:
                    if set(ld["carrier"]) != {"carrierMasterId", "name", "mcNumber",
                                              "dotNumber", "phoneNumber"}:
                        self.fail(f"{tag}: carrier keys")
                if set(ld["customer"]) != {"customerId", "name"}:
                    self.fail(f"{tag}: customer keys")
                if ld["weightTotal"] >= 45000:
                    self.fail(f"{tag}: weight {ld['weightTotal']}")
                for st in ld["stops"]:
                    if set(st) != A_STOP_KEYS:
                        self.fail(f"{tag}: stop keys")
                    if st["city"] != st["city"].upper():
                        self.fail(f"{tag}: FreightFlow cities must be UPPERCASE")
                    self._check_place(tag, st["city"], st["state"], st["zipCode"])
                if ld["stops"][0]["stopType"] != "First Pickup":
                    self.fail(f"{tag}: first stop is not 'First Pickup'")
                if ld["stops"][-1]["stopType"] != "Last Drop":
                    self.fail(f"{tag}: last stop is not 'Last Drop'")
                if ld["createdDate"] > ld["lastModifiedDate"] and \
                        int(ld["shipmentId"]) not in self._ooo_ids(plan):
                    self.fail(f"{tag}: createdDate after lastModifiedDate on {ld['shipmentId']}")

    def _shape_b(self, plan: BrokerPlan, files: dict[str, dict]) -> None:
        known_carriers: set[int] = set()
        known_loads: set[str] = set()
        for name, doc in sorted(files.items()):
            tag = f"{plan.cfg.dirname}/{name}"
            if set(doc) != {"synced_at", "loads", "carriers", "rates"}:
                self.fail(f"{tag}: envelope keys {sorted(doc)}")
            slot = self._slot_of(name)
            if doc["synced_at"] != naive_b(slot_local(slot)):
                self.fail(f"{tag}: synced_at {doc['synced_at']}")
            for c in doc["carriers"]:
                if set(c) != B_CARRIER_KEYS:
                    self.fail(f"{tag}: carrier keys")
                known_carriers.add(c["carrier_id"])
            for ld in doc["loads"]:
                if set(ld) != B_LOAD_KEYS:
                    self.fail(f"{tag}: load keys {sorted(set(ld) ^ B_LOAD_KEYS)}")
                known_loads.add(ld["load_num"])
                if ld["status_code"] not in (10, 20, 30, 40, 50, 90):
                    self.fail(f"{tag}: bad status_code {ld['status_code']}")
                if ld["equip"] not in ("V", "R", "F"):
                    self.fail(f"{tag}: bad equip {ld['equip']}")
                if ld["carrier_ref"] is not None and ld["carrier_ref"] not in known_carriers:
                    self.fail(f"{tag}: carrier_ref {ld['carrier_ref']} unseen")
                if ld["weight_kg"] * 2.20462 >= 45000:
                    self.fail(f"{tag}: weight {ld['weight_kg']} kg too heavy")
                self._check_place(tag, ld["pu_city"], ld["pu_state"], ld["pu_zip"])
                self._check_place(tag, ld["del_city"], ld["del_state"], ld["del_zip"])
                if ld["pu_date"] > ld["del_date"]:
                    self.fail(f"{tag}: pickup after delivery on {ld['load_num']}")
                if ld["entered_at"] > ld["updated_at"]:
                    self.fail(f"{tag}: entered_at after updated_at on {ld['load_num']}")
            for r in doc["rates"]:
                if set(r) != B_RATE_KEYS:
                    self.fail(f"{tag}: rate keys")
                if r["side"] not in ("pay", "bill"):
                    self.fail(f"{tag}: bad side {r['side']}")
                if r["code"] not in ("LINEHAUL", "FUEL", "ACCESSORIAL", "ADJUSTMENT"):
                    self.fail(f"{tag}: bad code {r['code']}")
                if r["load_num"] not in known_loads:
                    self.fail(f"{tag}: rate references unknown load {r['load_num']}")

    def _shape_c(self, plan: BrokerPlan, files: dict[str, dict]) -> None:
        for name, doc in sorted(files.items()):
            tag = f"{plan.cfg.dirname}/{name}"
            if set(doc) != {"synced_at", "records", "referenced_records"}:
                self.fail(f"{tag}: envelope keys {sorted(doc)}")
            slot = self._slot_of(name)
            if doc["synced_at"] != utc_c(slot_local(slot)):
                self.fail(f"{tag}: synced_at {doc['synced_at']} != {utc_c(slot_local(slot))}")
            refs = doc["referenced_records"]
            for rec in doc["records"]:
                if set(rec) != C_RECORD_KEYS:
                    self.fail(f"{tag}: record keys {sorted(set(rec) ^ C_RECORD_KEYS)}")
                if rec["bos__Load_Status__c"] not in C_STATUSES:
                    self.fail(f"{tag}: bad status {rec['bos__Load_Status__c']}")
                if rec["bos__Equipment_Type__c"] not in ("Dry Van", "Reefer", "Flatbed", None):
                    self.fail(f"{tag}: bad equipment {rec['bos__Equipment_Type__c']}")
                if len(rec["Id"]) != 18:
                    self.fail(f"{tag}: Id length {len(rec['Id'])}")
                for ref_field in ("bos__Customer__c", "bos__Carrier__c"):
                    v = rec[ref_field]
                    if v is not None and v not in refs:
                        self.fail(f"{tag}: {ref_field} {v} not in referenced_records")
                if rec["bos__Carrier__c"] is not None:
                    acct = refs[rec["bos__Carrier__c"]]
                    if acct.get("record_type") != "Carrier":
                        self.fail(f"{tag}: carrier ref is not a Carrier account")
                    # DECISIONS D2 extension must actually be present
                    if "bos__MC_Number__c" not in acct or "bos__DOT_Number__c" not in acct:
                        self.fail(f"{tag}: carrier account missing MC/DOT (D2 extension)")
                total_lbs = 0.0
                for li in rec["bos__Line_Items__r"]:
                    if set(li) != C_ITEM_KEYS:
                        self.fail(f"{tag}: line item keys")
                    if li["bos__Weight_Units__c"] not in ("lbs", "kg"):
                        self.fail(f"{tag}: bad weight units")
                    total_lbs += (li["bos__Weight__c"] * 2.20462
                                  if li["bos__Weight_Units__c"] == "kg" else li["bos__Weight__c"])
                    eq = rec["bos__Equipment_Type__c"]
                    com = li["bos__Commodity__c"]
                    if eq == "Reefer" and com not in REEFER_COMMODITIES:
                        self.fail(f"{tag}: reefer load carrying '{com}'")
                    if eq == "Flatbed" and com not in FLATBED_COMMODITIES:
                        self.fail(f"{tag}: flatbed load carrying '{com}'")
                    if eq == "Dry Van" and com in REEFER_COMMODITIES | FLATBED_COMMODITIES:
                        self.fail(f"{tag}: dry van carrying '{com}'")
                if total_lbs >= 45000:
                    self.fail(f"{tag}: total weight {total_lbs:.0f} lbs")
                nums = [s["bos__Number__c"] for s in rec["bos__Stops__r"]]
                if nums != sorted(nums):
                    self.fail(f"{tag}: stops out of order")
                for st in rec["bos__Stops__r"]:
                    if set(st) != C_STOP_KEYS:
                        self.fail(f"{tag}: stop keys")
                    if st["bos__Location__c"] not in refs:
                        self.fail(f"{tag}: stop Location {st['bos__Location__c']} unresolved")
                    else:
                        loc = refs[st["bos__Location__c"]]
                        self._check_place(tag, loc["bos__City__c"], loc["bos__State__c"],
                                          loc["bos__Postal_Code__c"])
                if not rec["bos__Stops__r"][0]["bos__Is_Pickup__c"]:
                    self.fail(f"{tag}: first stop is not a pickup")
                if not rec["bos__Stops__r"][-1]["bos__Is_Dropoff__c"]:
                    self.fail(f"{tag}: last stop is not a dropoff")

    # -- 3b: reconciliation, emitted bytes vs the plan ----------------------
    #
    # Everything above this point either reads the JSON structurally or reads
    # `self.plans`. This block is the only thing that ties a number in a file to
    # the intent that produced it, per TMS normalization:
    #
    #   TMS A, C : totals as given
    #   TMS B    : miles = dist_km x 0.621371, lbs = weight_kg x 2.20462, and
    #              carrier rate = running sum of `pay` line items across ALL
    #              files up to and including that slot, negatives included
    #
    # It also asserts the SET of emitted (load id, slot) pairs equals the plan's
    # in both directions, which is what catches a deleted load or a deleted
    # rate row rather than a changed one.

    @staticmethod
    def _plan_appearances(
        plan: BrokerPlan, include_rate_only: bool = True
    ) -> dict[tuple[str, int], tuple[LoadPlan, Appearance]]:
        """(public id, slot) -> the appearance that slot is supposed to carry."""
        out: dict[tuple[str, int], tuple[LoadPlan, Appearance]] = {}
        for ld in plan.loads + plan.day11:
            for ap in ld.appearances:
                if ap.rate_only and not include_rate_only:
                    continue
                out[(str(ld.public_id), ap.slot)] = (ld, ap)
        return out

    def _eq(self, tag: str, what: str, got, want, tol: float = 0.0) -> None:
        """One emitted value against one planned value."""
        numeric = (isinstance(got, (int, float)) and isinstance(want, (int, float))
                   and not isinstance(got, bool) and not isinstance(want, bool))
        if numeric:
            if abs(float(got) - float(want)) <= tol:
                return
        elif got == want:
            return
        self.fail(f"{tag}: {what} on disk is {got!r} but the plan says {want!r}"
                  + (f" (tolerance {tol})" if tol else ""))

    def _reconcile_keys(self, plan: BrokerPlan, emitted: set, expected: set, what: str) -> None:
        for lid, slot in sorted(expected - emitted):
            self.fail(f"{plan.cfg.dirname}/{slot_filename(slot)}: plan expects {what} {lid} "
                      f"here and the file does not contain it (deleted or moved?)")
        for lid, slot in sorted(emitted - expected):
            self.fail(f"{plan.cfg.dirname}/{slot_filename(slot)}: carries {what} {lid}, "
                      f"which the plan does not put in this file")

    def _reconcile_stops(self, tag: str, got_zips: list, want_zips: list) -> None:
        if got_zips != want_zips:
            self.fail(f"{tag}: stop zips on disk {got_zips} != plan {want_zips}")

    def _miles_vs_geography(self, tag: str, zips: list, miles) -> None:
        """Plan-independent check: do the emitted miles match the emitted stops?

        This is what catches a unit inversion even if the plan were also wrong.
        Callers skip it when the emitted stop list is not the whole trip (TMS B
        keeps only pickup and delivery, so a 3-stop load's mileage exceeds the
        endpoint distance by design).
        """
        if len(zips) < 2 or any(z is None for z in zips):
            return
        total = 0.0
        for a, b in zip(zips, zips[1:]):
            pa, pb = lookup_zip(a), lookup_zip(b)
            if pa is None or pb is None:
                return
            total += road_miles_between(pa, pb)
        if abs(total - float(miles)) > max(1.0, 0.02 * total):
            self.fail(f"{tag}: emitted mileage {miles} does not match the emitted stops "
                      f"{zips}, which are {total:.1f} road miles apart")

    def _reconcile_carrier(self, tag: str, plan: BrokerPlan, ld: LoadPlan, ap: Appearance,
                           got_id, got_mc, got_name=None) -> None:
        covered = (ap.status in (COVERED, IN_TRANSIT, DELIVERED, COMPLETED)
                   and ld.carrier_role is not None)
        if not covered:
            self._eq(tag, "carrier reference", got_id, None)
            return
        c = _carrier(plan.cfg, ld.carrier_role)
        self._eq(tag, "carrier id", got_id, plan.ids["carriers"][c.role])
        self._eq(tag, "carrier MC number", got_mc, c.mc)
        if got_name is not None:
            self._eq(tag, "carrier name", got_name, c.name)

    def _reconcile_a(self, plan: BrokerPlan, files: dict[str, dict]) -> None:
        want = self._plan_appearances(plan)
        seen: set[tuple[str, int]] = set()
        for name, doc in sorted(files.items()):
            slot = self._slot_of(name)
            for rec in doc["loads"]:
                key = (str(rec["shipmentId"]), slot)
                seen.add(key)
                if key not in want:
                    continue
                ld, ap = want[key]
                tag = f"{plan.cfg.dirname}/{name} shipment {rec['shipmentId']} ({ld.key})"
                self._eq(tag, "mileage", rec["mileage"], ld.miles)
                self._eq(tag, "totalBuy", rec["totalBuy"], ap.carrier_rate)
                self._eq(tag, "totalSell", rec["totalSell"], ld.customer_rate)
                self._eq(tag, "weightTotal", rec["weightTotal"], ld.weight_lbs)
                self._eq(tag, "equipment", rec["equipment"], equip_a(ld.equipment))
                self._eq(tag, "status", rec["status"], STATUS_A[ap.status])
                self._eq(tag, "createdDate", rec["createdDate"], iso_a(ld.created_at))
                self._eq(tag, "lastModifiedDate", rec["lastModifiedDate"],
                         iso_a(ap.last_modified))
                self._eq(tag, "customer id", rec["customer"]["customerId"],
                         plan.ids["customers"][ld.customer])
                self._eq(tag, "customer name", rec["customer"]["name"], ld.customer)
                car = rec["carrier"]
                self._reconcile_carrier(tag, plan, ld, ap,
                                        car["carrierMasterId"] if car else None,
                                        car["mcNumber"] if car else None,
                                        car["name"] if car else None)
                zips = [s["zipCode"] for s in rec["stops"]]
                self._reconcile_stops(tag, zips, [s.place.zip for s in ld.stops])
                self._miles_vs_geography(tag, zips, rec["mileage"])
                for st, want_st in zip(rec["stops"], ld.stops):
                    self._eq(tag, f"stop {want_st.place.zip} actualDepartureDateTime",
                             st["actualDepartureDateTime"],
                             iso_a(want_st.actual) if (ap.show_actuals and want_st.actual)
                             else None)
        self._reconcile_keys(plan, seen, set(want), "shipmentId")
        self.note(f"{plan.cfg.dirname}: reconciled {len(seen)} load appearances against the plan")

    def _reconcile_b(self, plan: BrokerPlan, files: dict[str, dict]) -> None:
        # A rate-only appearance is deliberately absent from `loads`, so the
        # loads-array key set must exclude it; the money check below is what
        # proves that appearance still landed.
        want_rows = self._plan_appearances(plan, include_rate_only=False)
        want_all = self._plan_appearances(plan)
        by_id = {str(ld.public_id): ld for ld in plan.loads + plan.day11}
        aps_at: dict[int, list[tuple[str, LoadPlan, Appearance]]] = {}
        for (lid, slot), (ld, ap) in want_all.items():
            aps_at.setdefault(slot, []).append((lid, ld, ap))

        seen: set[tuple[str, int]] = set()
        pay: dict[str, float] = {}
        bill: dict[str, float] = {}
        rate_slots: dict[str, set[int]] = {}
        rate_only_rows = 0
        # carrier_id -> mc_no, as emitted in the `carriers` table. Read from the
        # files, not the plan, so the MC check below is not a tautology.
        emitted_mc: dict = {}
        emitted_carrier_name: dict = {}

        for name, doc in sorted(files.items()):
            slot = self._slot_of(name)
            listed = {r["load_num"] for r in doc["loads"]}
            for c in doc["carriers"]:
                emitted_mc[c["carrier_id"]] = c["mc_no"]
                emitted_carrier_name[c["carrier_id"]] = c["carrier_name"]
            for rec in doc["loads"]:
                key = (rec["load_num"], slot)
                seen.add(key)
                if key not in want_rows:
                    continue
                ld, ap = want_rows[key]
                tag = f"{plan.cfg.dirname}/{name} load {rec['load_num']} ({ld.key})"
                self._eq(tag, "dist_km x 0.621371 -> miles",
                         rec["dist_km"] * 0.621371, ld.miles, tol=0.1)
                self._eq(tag, "weight_kg x 2.20462 -> lbs",
                         rec["weight_kg"] * 2.20462, ld.weight_lbs, tol=0.5)
                self._eq(tag, "equip", rec["equip"], equip_b(ld.equipment))
                self._eq(tag, "status_code", rec["status_code"], STATUS_B[ap.status])
                self._eq(tag, "customer_code", rec["customer_code"],
                         plan.ids["customers"][ld.customer])
                self._eq(tag, "customer_name", rec["customer_name"], ld.customer)
                self._eq(tag, "entered_at", rec["entered_at"], naive_b(ld.created_at))
                self._eq(tag, "updated_at", rec["updated_at"], naive_b(ap.last_modified))
                self._reconcile_carrier(
                    tag, plan, ld, ap, rec["carrier_ref"],
                    emitted_mc.get(rec["carrier_ref"]) if rec["carrier_ref"] else None,
                    emitted_carrier_name.get(rec["carrier_ref"]) if rec["carrier_ref"] else None,
                )
                # TMS B's flat row carries only the lane-forming endpoints.
                pu, dl = ld.stops[0], ld.stops[-1]
                self._reconcile_stops(tag, [rec["pu_zip"], rec["del_zip"]],
                                      [pu.place.zip, dl.place.zip])
                self._eq(tag, "pu_date", rec["pu_date"], pu.sched_date.isoformat())
                self._eq(tag, "del_date", rec["del_date"], dl.sched_date.isoformat())
                if len(ld.stops) == 2:
                    self._miles_vs_geography(tag, [rec["pu_zip"], rec["del_zip"]],
                                             rec["dist_km"] * 0.621371)
                self._eq(tag, "pu_departed_at", rec["pu_departed_at"],
                         naive_b(pu.actual) if (ap.show_actuals and pu.actual) else None)
                self._eq(tag, "del_arrived_at", rec["del_arrived_at"],
                         naive_b(dl.actual) if (ap.show_actuals and dl.actual) else None)

            for r in doc["rates"]:
                lid = r["load_num"]
                if lid not in by_id:
                    continue                      # already failed in _shape_b
                side = pay if r["side"] == "pay" else bill
                side[lid] = round(side.get(lid, 0.0) + r["amount_usd"], 2)
                rate_slots.setdefault(lid, set()).add(slot)
                if lid not in listed:
                    rate_only_rows += 1

            # Running `pay` total must equal the carrier rate the plan says is
            # true as of this file. This is the check that a deleted or edited
            # ADJUSTMENT row cannot survive.
            for lid, ld, ap in sorted(aps_at.get(slot, [])):
                self._eq(f"{plan.cfg.dirname}/{name} load {lid} ({ld.key})",
                         "running sum of `pay` rows through this file",
                         pay.get(lid, 0.0),
                         ap.carrier_rate if ap.carrier_rate is not None else 0.0,
                         tol=0.011)

        for lid, ld in sorted(by_id.items()):
            final = max(ld.appearances, key=lambda a: a.slot)
            self._eq(f"{plan.cfg.dirname} load {lid} ({ld.key})",
                     "final sum of all `pay` rows", pay.get(lid, 0.0),
                     final.carrier_rate if final.carrier_rate is not None else 0.0, tol=0.011)
            self._eq(f"{plan.cfg.dirname} load {lid} ({ld.key})",
                     "final sum of all `bill` rows", bill.get(lid, 0.0),
                     ld.customer_rate, tol=0.011)
            stray = rate_slots.get(lid, set()) - {ap.slot for ap in ld.appearances}
            if stray:
                self.fail(f"{plan.cfg.dirname} load {lid} ({ld.key}): rate rows in slots "
                          f"{sorted(stray)} where the plan gives the load no appearance")

        self._reconcile_keys(plan, seen, set(want_rows), "load_num")
        # CLAUDE.md's named trap has to be visible in the bytes, not just in the
        # plan: at least one rate row for a load the same file's `loads` array
        # does not mention.
        self.note(f"{plan.cfg.dirname}: reconciled {len(seen)} loads-array rows; "
                  f"rate rows whose load is absent from the same file's `loads` array "
                  f"(the rate-only trap): {rate_only_rows}")
        if rate_only_rows == 0:
            self.fail(f"{plan.cfg.dirname}: no emitted rate row references a load missing from "
                      f"its own file's `loads` array — the TMS B rate-only correction trap is "
                      f"not present in the data, whatever the plan says")

    def _reconcile_c(self, plan: BrokerPlan, files: dict[str, dict]) -> None:
        want = self._plan_appearances(plan)
        seen: set[tuple[str, int]] = set()
        for name, doc in sorted(files.items()):
            slot = self._slot_of(name)
            refs = doc["referenced_records"]
            for rec in doc["records"]:
                key = (rec["Id"], slot)
                seen.add(key)
                if key not in want:
                    continue
                ld, ap = want[key]
                tag = f"{plan.cfg.dirname}/{name} {rec['Name']} ({ld.key})"
                self._eq(tag, "bos__Distance_Miles__c", rec["bos__Distance_Miles__c"], ld.miles)
                self._eq(tag, "bos__Carrier_Rate__c", rec["bos__Carrier_Rate__c"],
                         ap.carrier_rate)
                self._eq(tag, "bos__Customer_Rate__c", rec["bos__Customer_Rate__c"],
                         ld.customer_rate)
                # Invariant 5's only fixture lives here: UNKNOWN must stay null.
                self._eq(tag, "bos__Equipment_Type__c", rec["bos__Equipment_Type__c"],
                         equip_c(ld.equipment))
                self._eq(tag, "bos__Load_Status__c", rec["bos__Load_Status__c"],
                         STATUS_C[ap.status])
                self._eq(tag, "CreatedDate", rec["CreatedDate"], utc_c(ld.created_at))
                self._eq(tag, "LastModifiedDate", rec["LastModifiedDate"],
                         utc_c(ap.last_modified))
                self._eq(tag, "bos__Customer__c", rec["bos__Customer__c"],
                         plan.ids["customers"][ld.customer])
                cust = refs.get(rec["bos__Customer__c"], {})
                self._eq(tag, "customer account Name", cust.get("Name"), ld.customer)
                acct = refs.get(rec["bos__Carrier__c"]) if rec["bos__Carrier__c"] else None
                self._reconcile_carrier(tag, plan, ld, ap, rec["bos__Carrier__c"],
                                        acct.get("bos__MC_Number__c") if acct else None,
                                        acct.get("Name") if acct else None)
                items = rec["bos__Line_Items__r"]
                if len(items) != len(ld.line_items):
                    self.fail(f"{tag}: {len(items)} line items on disk, plan has "
                              f"{len(ld.line_items)}")
                else:
                    for got_li, want_li in zip(items, ld.line_items):
                        self._eq(tag, "bos__Commodity__c", got_li["bos__Commodity__c"],
                                 want_li.commodity)
                        self._eq(tag, "bos__Weight_Units__c", got_li["bos__Weight_Units__c"],
                                 want_li.units)
                        self._eq(tag, f"bos__Weight__c ({want_li.commodity})",
                                 got_li["bos__Weight__c"], want_li.weight)
                # per-line-item units, THEN summed -- the normalization rule
                total_lbs = sum(
                    li["bos__Weight__c"] * (2.20462
                                            if li["bos__Weight_Units__c"] == "kg" else 1.0)
                    for li in items
                )
                self._eq(tag, "sum of line items normalized to lbs", total_lbs,
                         ld.weight_lbs, tol=0.05)
                zips = [refs.get(s["bos__Location__c"], {}).get("bos__Postal_Code__c")
                        for s in rec["bos__Stops__r"]]
                self._reconcile_stops(tag, zips, [s.place.zip for s in ld.stops])
                self._miles_vs_geography(tag, zips, rec["bos__Distance_Miles__c"])
                for st, want_st in zip(rec["bos__Stops__r"], ld.stops):
                    self._eq(tag, f"stop {want_st.place.zip} bos__Scheduled_Date__c",
                             st["bos__Scheduled_Date__c"], want_st.sched_date.isoformat())
        self._reconcile_keys(plan, seen, set(want), "record Id")
        self.note(f"{plan.cfg.dirname}: reconciled {len(seen)} record appearances "
                  f"against the plan")

    @staticmethod
    def _slot_of(name: str) -> int:
        stamp = name.replace("_sync.json", "")
        d = date.fromisoformat(stamp[:10])
        hh = int(stamp[11:13])
        return (d - DAY_ONE).days * 4 + SYNC_HOURS.index(hh)

    @staticmethod
    def _ooo_ids(plan: BrokerPlan) -> set:
        """Loads whose lastModifiedDate is deliberately out of order."""
        out = set()
        for ld in plan.loads:
            if any(ap.out_of_order_lm for ap in ld.appearances):
                out.add(int(ld.public_id) if plan.cfg.tms == "A" else ld.public_id)
        return out

    # -- 4b: cross-file chronology / status monotonicity --------------------
    def check_cross_file(self) -> None:
        for plan in self.plans:
            for ld in plan.loads + plan.day11:
                seq = sorted(ld.appearances, key=lambda a: a.slot)
                for a, b in zip(seq, seq[1:]):
                    if STATUS_ORDER[b.status] < STATUS_ORDER[a.status]:
                        self.fail(f"{plan.cfg.key}/{ld.public_id}: status went backwards "
                                  f"{a.status} -> {b.status}")
            ooo = self._ooo_ids(plan)
            for ld in plan.loads:
                if ld.delivered_at and ld.stops[0].actual:
                    if ld.stops[0].actual >= ld.delivered_at:
                        self.fail(f"{plan.cfg.key}/{ld.public_id}: departed after delivered")
                seq = sorted(ld.appearances, key=lambda a: a.slot)
                key = int(ld.public_id) if plan.cfg.tms == "A" else ld.public_id
                if key not in ooo:
                    for a, b in zip(seq, seq[1:]):
                        if b.last_modified < a.last_modified:
                            self.fail(f"{plan.cfg.key}/{ld.public_id}: lastModified regressed "
                                      f"without being a declared out-of-order case")
            self.note(f"{plan.cfg.key}: out-of-order lastModified whitelist = "
                      f"{sorted(str(x) for x in ooo) or 'none'}")

    # -- sanity rules -------------------------------------------------------
    def check_sanity(self) -> None:
        for plan in self.plans:
            worst_lo, worst_hi = 99.0, 0.0
            for ld in plan.loads:
                rpm = ld.rpm
                worst_lo, worst_hi = min(worst_lo, rpm), max(worst_hi, rpm)
                if not 1.50 <= rpm <= 3.50:
                    self.fail(f"{plan.cfg.key}/{ld.public_id}: {rpm:.2f} $/mi out of band")
                # Intermediate values matter too: the lifecycle load's booking rate
                # and a correction's pre-correction rate are BOTH visible in files a
                # reviewer will open, so they have to be plausible as well.
                for ap in ld.appearances:
                    if ap.carrier_rate is None:
                        continue
                    ar = ap.carrier_rate / ld.miles
                    worst_lo, worst_hi = min(worst_lo, ar), max(worst_hi, ar)
                    if not 1.50 <= ar <= 3.50:
                        self.fail(f"{plan.cfg.key}/{ld.public_id}: intermediate rate in "
                                  f"{slot_filename(ap.slot)} is {ar:.3f} $/mi, out of band "
                                  f"({ap.note})")
                if ld.customer_rate <= ld.carrier_rate:
                    self.fail(f"{plan.cfg.key}/{ld.public_id}: margin not positive "
                              f"({ld.customer_rate} vs {ld.carrier_rate})")
                if ld.weight_lbs >= 45000:
                    self.fail(f"{plan.cfg.key}/{ld.public_id}: weight {ld.weight_lbs}")
                for st in ld.stops:
                    if resolve_place(st.place.city, st.place.state, st.place.zip) is None:
                        self.fail(f"{plan.cfg.key}/{ld.public_id}: unresolved stop {st.place}")
                if ld.stops[0].sched_date > ld.stops[-1].sched_date:
                    self.fail(f"{plan.cfg.key}/{ld.public_id}: pickup scheduled after delivery")
            self.note(f"{plan.cfg.key}: carrier $/mi range "
                      f"{worst_lo:.2f} - {worst_hi:.2f} (band 1.50-3.50)")

    # -- 6, 7, 8: scenario coverage counts ----------------------------------
    def check_scenarios(self) -> None:
        for plan in self.plans:
            cfg = plan.cfg
            history = plan.loads
            slots_used = {ld.pinned_slot for ld in history}
            per_slot: dict[int, int] = {}
            for ld in history:
                for ap in ld.appearances:
                    if not ap.rate_only:
                        per_slot[ap.slot] = per_slot.get(ap.slot, 0) + 1
            empty = [s for s in range(HISTORY_SLOTS) if per_slot.get(s, 0) == 0]
            appearances = sum(per_slot.values())
            self.note("")
            self.note(f"== {cfg.key} ({cfg.dirname}) ==")
            self.note(f"  distinct history loads : {len(history)}   (D9 target ~93)")
            self.note(f"  loads-array entries    : {appearances}  "
                      f"(ceiling {3 * (HISTORY_SLOTS - len(empty))})")
            self.note(f"  history files with load: {len(slots_used)} of {HISTORY_SLOTS}")
            self.note(f"  empty history syncs    : {len(empty)} -> slots {empty}")
            self.note(f"  day-11 loads           : {len(plan.day11)} "
                      f"in slots {sorted({ld.pinned_slot for ld in plan.day11})}")
            if max(per_slot.values()) > 3:
                self.fail(f"{cfg.key}: a file carries more than 3 loads")

            # ZIP3 triple counts
            zip3_counts: dict[tuple[str, str, str], int] = {}
            metro_counts: dict[tuple[str, str, str], int] = {}
            for ld in history:
                k = (ld.origin.zip3, ld.dest.zip3, ld.equipment)
                zip3_counts[k] = zip3_counts.get(k, 0) + 1
                m = (ld.origin.metro, ld.dest.metro, ld.equipment)
                metro_counts[m] = metro_counts.get(m, 0) + 1

            clearing = sorted((k for k, v in zip3_counts.items() if v >= MIN_TIER_LOADS),
                              key=lambda k: -zip3_counts[k])
            self.note(f"  ZIP3 triples clearing {MIN_TIER_LOADS}: "
                      + ", ".join(f"{a}->{b}/{e}={zip3_counts[(a, b, e)]}"
                                  for a, b, e in clearing))
            if not clearing:
                self.fail(f"{cfg.key}: NO zip3 triple reaches {MIN_TIER_LOADS} — "
                          f"the top tier can never fire")

            rich = plan.day11[0]
            rk = (rich.origin.zip3, rich.dest.zip3, rich.equipment)
            n_rich = zip3_counts.get(rk, 0)
            self.note(f"  rich day-11 load sits on {rk[0]}->{rk[1]}/{rk[2]} with {n_rich} loads")
            if n_rich < MIN_TIER_LOADS:
                self.fail(f"{cfg.key}: rich day-11 load's zip3 triple has only {n_rich}")
            if n_rich < 8:
                self.fail(f"{cfg.key}: rich zip3 triple {n_rich} < 8, too little headroom")

            # metro scatter
            scatter = plan.day11[1]
            mk = (scatter.origin.metro, scatter.dest.metro, scatter.equipment)
            n_metro = metro_counts.get(mk, 0)
            inner = {k: v for k, v in zip3_counts.items()
                     if v and _metro_of_zip3_pair(history, k) == mk}
            self.note(f"  scatter metro {mk[0]}->{mk[1]}/{mk[2]} = {n_metro} loads; "
                      f"inner zip3 pairs: "
                      + ", ".join(f"{a}->{b}={v}" for (a, b, _e), v in sorted(inner.items())))
            if n_metro < MIN_TIER_LOADS:
                self.fail(f"{cfg.key}: metro scatter lane has only {n_metro}")
            for k, v in inner.items():
                if v >= MIN_TIER_LOADS:
                    self.fail(f"{cfg.key}: scatter inner zip3 pair {k} has {v} >= "
                              f"{MIN_TIER_LOADS}; METRO would never be reached")
            sk = (scatter.origin.zip3, scatter.dest.zip3, scatter.equipment)
            if zip3_counts.get(sk, 0) >= MIN_TIER_LOADS:
                self.fail(f"{cfg.key}: scatter day-11 load's own zip3 triple clears the minimum")

            # thin lane
            thin = plan.day11[2]
            tk3 = (thin.origin.zip3, thin.dest.zip3, thin.equipment)
            tkm = (thin.origin.metro, thin.dest.metro, thin.equipment)
            self.note(f"  thin lane zip3 {tk3[0]}->{tk3[1]}={zip3_counts.get(tk3, 0)}, "
                      f"metro {tkm[0]}->{tkm[1]}={metro_counts.get(tkm, 0)} "
                      f"(both must stay < {MIN_TIER_LOADS})")
            if zip3_counts.get(tk3, 0) >= MIN_TIER_LOADS or metro_counts.get(tkm, 0) >= MIN_TIER_LOADS:
                self.fail(f"{cfg.key}: thin lane is not thin; REGION rung never demonstrated")

            # rich metro depth per PRD section 4 ("25+")
            dfw_hou = sum(v for (o, d, _e), v in metro_counts.items() if (o, d) == ("DFW", "HOU"))
            self.note(f"  DFW->HOU total (all equipment): {dfw_hou} loads (PRD wants 25+)")
            if dfw_hou < 25:
                self.fail(f"{cfg.key}: DFW->HOU only {dfw_hou} loads")

            # carrier contrast
            counts = sorted(((c.role, sum(1 for h in history if h.carrier_role == c.role))
                             for c in cfg.carriers), key=lambda t: -t[1])
            self.note("  carrier load counts    : "
                      + ", ".join(f"{r}={n}" for r, n in counts))
            if counts[0][1] < 20:
                self.fail(f"{cfg.key}: no carrier with 20+ loads")
            if not any(n <= 2 for _r, n in counts):
                self.fail(f"{cfg.key}: no cold-start carrier with 1-2 loads")

            # day-11 uncovered
            for ld in plan.day11:
                if len(ld.appearances) != 1:
                    self.fail(f"{cfg.key}/{ld.public_id}: day-11 load appears more than once")
                if ld.appearances[0].status != ACTIVE or ld.carrier_role is not None:
                    self.fail(f"{cfg.key}/{ld.public_id}: day-11 load is not uncovered ACTIVE")

            # expected winners hold with margin
            for ld in plan.day11:
                tier, backing, _ = tier_walk(history, ld)
                sc = score_carriers(plan, ld, tier, backing)
                if sc[0].role != ld.expected_winner:
                    self.fail(f"{cfg.key}/{ld.key}: expected {ld.expected_winner} to win, "
                              f"got {sc[0].role} ({sc[0].score:.1f} vs "
                              f"{[f'{s.role}:{s.score:.1f}' for s in sc[:3]]})")
                elif sc[0].score - sc[1].score < 4.0:
                    self.fail(f"{cfg.key}/{ld.key}: winner margin only "
                              f"{sc[0].score - sc[1].score:.1f} pts — too tight to assert")
                else:
                    self.note(f"  {ld.key:16s} tier={tier:10s} n={len(backing):3d} "
                              f"winner={sc[0].role} ({sc[0].score:.1f}) "
                              f"margin=+{sc[0].score - sc[1].score:.1f}")

            # equipment scarcity: broker B must keep FLATBED under the minimum so
            # rung 4 (REGION_ANY) has a fixture; A and C must keep it at/over 5 so
            # rung 3 keeps its own fixture. Both behaviors must exist.
            n_flat = sum(1 for h in history if h.equipment == FLATBED)
            self.note(f"  REGION FLATBED loads   : {n_flat} "
                      f"({'< 5 -> drives REGION_ANY' if n_flat < MIN_TIER_LOADS else '>= 5 -> stops at REGION'})")
            if cfg.tms == "B" and n_flat >= MIN_TIER_LOADS:
                self.fail(f"{cfg.key}: FLATBED={n_flat} >= {MIN_TIER_LOADS}; "
                          f"REGION_ANY (rung 4) would never fire anywhere")
            if cfg.tms in ("A", "C") and n_flat < MIN_TIER_LOADS:
                self.fail(f"{cfg.key}: FLATBED={n_flat} < {MIN_TIER_LOADS}; "
                          f"the REGION rung loses its cold-start fixture")

            # messy edges present
            scen = {ld.scenario for ld in history}
            expect = {"lifecycle", "correction", "rich_zip3_lane", "suburb_scatter",
                      "thin_lane", "deadhead_lane", "cold_start_flatbed"}
            missing = expect - scen
            if missing:
                self.fail(f"{cfg.key}: scenarios missing {sorted(missing)}")
            if cfg.tms in ("A", "C") and "messy_three_stop" not in scen:
                self.fail(f"{cfg.key}: 3-stop load missing")
            if cfg.tms == "C":
                if "messy_null_equipment" not in scen:
                    self.fail(f"{cfg.key}: null-equipment load missing")
                if "messy_kg_line_item" not in scen:
                    self.fail(f"{cfg.key}: kg line item missing")
            n_ooo = sum(1 for ld in history for ap in ld.appearances if ap.out_of_order_lm)
            self.note(f"  messy edges            : "
                      f"3-stop={'yes' if 'messy_three_stop' in scen else 'n/a'}, "
                      f"null-equip={sum(1 for ld in history if ld.equipment == UNKNOWN)}, "
                      f"kg-line-item={'yes' if 'messy_kg_line_item' in scen else 'n/a'}, "
                      f"out-of-order-lastModified={n_ooo}")

    def check_cross_tms_carrier(self) -> None:
        by_mc: dict[str, list[tuple[str, str]]] = {}
        for plan in self.plans:
            for c in plan.cfg.carriers:
                by_mc.setdefault(c.mc, []).append((plan.cfg.key, c.name))
        shared = {mc: v for mc, v in by_mc.items() if len({b for b, _n in v}) > 1}
        if not shared:
            self.fail("scenario 6: no MC/DOT appears in two TMSs")
        for mc, v in sorted(shared.items()):
            names = {n for _b, n in v}
            self.note(f"cross-TMS carrier MC {mc}: " + "; ".join(f"{b}='{n}'" for b, n in v))
            if len(names) < 2:
                self.fail(f"cross-TMS carrier MC {mc} uses the same name in both TMSs — "
                          f"name matching would accidentally work")

    def check_tier_rung_coverage(self) -> None:
        """All four rungs of PRD section 7 must have at least one day-11 fixture,
        and D6's UNKNOWN-equipment skip must have one too."""
        rungs: dict[str, list[str]] = {}
        unknown_day11: list[str] = []
        for plan in self.plans:
            for ld in plan.day11:
                tier, backing, _ = tier_walk(plan.loads, ld)
                rungs.setdefault(tier, []).append(f"{plan.cfg.key}/{ld.key}(n={len(backing)})")
                if ld.equipment == UNKNOWN:
                    unknown_day11.append(f"{plan.cfg.key}/{ld.key}")
        self.note("")
        self.note("== tier-rung coverage across all day-11 loads ==")
        for tier in (TIER_ZIP3, TIER_METRO, TIER_REGION, TIER_REGION_ANY):
            got = rungs.get(tier, [])
            self.note(f"  {tier:11s}: {len(got)} load(s)  {', '.join(got) or '— NONE —'}")
            if not got:
                self.fail(f"tier rung {tier} is never exercised by any day-11 load — "
                          f"PRD section 7's walk is undemonstrated at that rung")
        self.note(f"  day-11 loads with UNKNOWN equipment (D6 filter skip): "
                  f"{', '.join(unknown_day11) or '— NONE —'}")
        if not unknown_day11:
            self.fail("no day-11 load has UNKNOWN equipment; DECISIONS D6's "
                      "'skips the filter entirely' branch has no fixture")

    def check_rate_band_separation(self) -> None:
        """The tenant-leak tripwire.

        For every day-11 load: the accepted-tier median must be at least
        BAND_SEPARATION $/mi away from every OTHER broker's median on the same
        lane key, and pooling all three brokers must move at least two of
        (p25, median, p75) by that much AND change the load count.
        """
        self.note("")
        self.note("== cross-broker rate separation (own vs other brokers vs pooled) ==")
        for plan in self.plans:
            cfg = plan.cfg
            self.note(f"  {cfg.key} ({cfg.price_label}, bias {cfg.rpm_bias:+.2f} $/mi)")
            for ld in plan.day11:
                tier, backing, _ = tier_walk(plan.loads, ld)
                own = [h.rpm for h in backing]
                own_stats = (pct_cont(own, .25), pct_cont(own, .50), pct_cont(own, .75))
                pooled = list(own)
                other_bits = []
                for other in self.plans:
                    if other is plan:
                        continue
                    o = _same_key_loads(other.loads, ld, tier)
                    pooled.extend(h.rpm for h in o)
                    if not o:
                        self.fail(f"{cfg.key}/{ld.key}: {other.cfg.key} has no loads on the "
                                  f"same {tier} key, so the leak comparison is vacuous")
                        continue
                    o_med = pct_cont([h.rpm for h in o], .50)
                    gap = abs(o_med - own_stats[1])
                    other_bits.append(f"{other.cfg.key}={o_med:.3f}(Δ{gap:.3f})")
                    if gap < BAND_SEPARATION:
                        self.fail(f"{cfg.key}/{ld.key}: median {own_stats[1]:.3f} is only "
                                  f"{gap:.3f} $/mi from {other.cfg.key}'s {o_med:.3f} on the "
                                  f"same {tier} key — a cross-broker leak would be invisible "
                                  f"in the headline number (need >= {BAND_SEPARATION})")
                pool_stats = (pct_cont(pooled, .25), pct_cont(pooled, .50), pct_cont(pooled, .75))
                moved = sum(1 for a, b in zip(own_stats, pool_stats)
                            if abs(a - b) >= BAND_SEPARATION)
                own_range = own_stats[2] - own_stats[0]
                pool_range = pool_stats[2] - pool_stats[0]
                ratio = (pool_range / own_range) if own_range > 0 else float("inf")
                self.note(f"    {ld.key:22s} {tier:10s} own n={len(own):3d} "
                          f"p25/med/p75 {own_stats[0]:.3f}/{own_stats[1]:.3f}/{own_stats[2]:.3f}"
                          f" | others {' '.join(other_bits)}"
                          f" | pooled n={len(pooled):3d} "
                          f"{pool_stats[0]:.3f}/{pool_stats[1]:.3f}/{pool_stats[2]:.3f} "
                          f"({moved}/3 stats moved, range x{ratio:.2f})")

                # (1) The count always changes. This is the one tell that works
                #     at every tier, and it is why the provenance line must state
                #     how many loads backed the answer.
                if len(pooled) <= len(own):
                    self.fail(f"{cfg.key}/{ld.key}: pooling did not increase the load count")

                # (2) Money-level detection, asserted at the strength the tier
                #     can actually support (see RATE MODEL note).
                if tier in (TIER_ZIP3, TIER_METRO):
                    # Narrow key: haul length barely varies, so the bands are
                    # genuinely disjoint and the strong form must hold.
                    if moved < 2:
                        self.fail(f"{cfg.key}/{ld.key}: narrow {tier} key, but pooling moved "
                                  f"only {moved}/3 of (p25, median, p75) by >= "
                                  f"{BAND_SEPARATION} $/mi — a pooled answer would look too "
                                  f"much like the correct one")
                elif tier == TIER_REGION:
                    # Wide key: require the median to move OR the reported range
                    # to blow out by at least half again.
                    ok = (abs(own_stats[1] - pool_stats[1]) >= BAND_SEPARATION or ratio >= 1.5)
                    if not ok:
                        self.fail(f"{cfg.key}/{ld.key}: at {tier}, pooling moved the median by "
                                  f"{abs(own_stats[1] - pool_stats[1]):.3f} and widened the "
                                  f"range only x{ratio:.2f} — the leak would be invisible in "
                                  f"both the point estimate and the range")
                else:
                    # REGION_ANY is BY DEFINITION "the whole book, any equipment".
                    # Three whole books have a similar distribution to one, so no
                    # money-level test is meaningful here; the count is the tell.
                    self.note(f"      ^ REGION_ANY: money-level leak test waived by "
                              f"construction (whole book vs three whole books); detection "
                              f"rests on n={len(own)} vs n={len(pooled)} and the low-confidence "
                              f"label")

    def run(self) -> bool:
        loaded = self.check_grid()
        self.check_shapes(loaded)
        self.check_cross_file()
        self.check_sanity()
        self.check_scenarios()
        self.check_cross_tms_carrier()
        self.check_tier_rung_coverage()
        self.check_rate_band_separation()
        return not self.errors


def _metro_of_zip3_pair(history: list[LoadPlan], key: tuple[str, str, str]) -> tuple[str, str, str]:
    for ld in history:
        if (ld.origin.zip3, ld.dest.zip3, ld.equipment) == key:
            return (ld.origin.metro, ld.dest.metro, ld.equipment)
    return ("?", "?", key[2])


# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(REPO_ROOT / "data"))
    ap.add_argument("--validate-only", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    out_root = Path(args.out)
    plans = [build_plan(cfg) for cfg in BROKERS]
    rendered = {plan.cfg.key: render(plan) for plan in plans}

    if not args.validate_only:
        n = 0
        for plan in plans:
            d = out_root / plan.cfg.dirname
            d.mkdir(parents=True, exist_ok=True)
            for fname, payload in rendered[plan.cfg.key].items():
                (d / fname).write_text(
                    json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
                )
                n += 1
        (out_root / "TRACEABILITY.md").write_text(build_traceability(plans), encoding="utf-8")
        print(f"wrote {n} sync files + TRACEABILITY.md under {out_root}")

    v = Validator(out_root, plans)
    ok = v.run()
    if not args.quiet:
        print("\n".join(v.report))
    if v.errors:
        print("\nVALIDATION FAILURES:")
        for e in v.errors:
            print(f"  - {e}")
    print(f"\nvalidation: {'PASS' if ok else 'FAIL'} "
          f"({len(v.errors)} error{'' if len(v.errors) == 1 else 's'})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
