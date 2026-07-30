"""One function per normalization rule, shared by all three adapters.

CLAUDE.md's normalization table is the specification for this module, and the
reason it is a module: three copies of ``kg x 2.20462`` is three chances to fix
only two of them. If an adapter needs a conversion, it calls something here.

The other half of the job is **degrading without inventing**. The fixtures carry
deliberate messy edges — a blank equipment string, a null rate, a zip the geo
table has never heard of — and none of them should stop ingestion or quietly
acquire a plausible-looking default. So:

* Equipment is ``UNKNOWN`` unless the source said something recognizable
  (invariant 5). There is no code path from "missing" to ``DRY_VAN``.
* A location that does not resolve is **geo-null**: ``place=None``, raw
  city/state/zip preserved so the UI can still show it.
* A missing number is ``None``, which is a different fact from ``0``.
* A status outside the TMS's own documented vocabulary raises. It is the one
  field with no honest fallback — every canonical status asserts something about
  where the load is, and guessing wrong moves a load into or out of the pool that
  backs every lane statistic.
"""

from __future__ import annotations

from datetime import date, datetime

from ..domain.geo import resolve_place
from ..domain.localtime import central_naive_to_utc
from ..domain.model import Equipment, LoadStatus, StopLocation
from .base import AdapterError

__all__ = [
    "KG_TO_LBS",
    "KM_TO_MILES",
    "UNIT_DECIMALS",
    "text",
    "source_id",
    "optional_float",
    "money",
    "kg_to_lbs",
    "km_to_miles",
    "weight_to_lbs",
    "equipment_from_free_text",
    "equipment_from_code",
    "equipment_from_picklist",
    "status_from_a",
    "status_from_b",
    "status_from_c",
    "parse_offset_datetime",
    "parse_central_naive",
    "parse_local_date",
    "stop_location",
]

# CLAUDE.md, Weight and Distance rows. Both convert *into* the canonical unit,
# so both are multiplications and neither is ever inverted at a call site.
KG_TO_LBS = 2.20462
KM_TO_MILES = 0.621371

# A converted value is rounded to one decimal place, **once, here**.
#
# TMS A and TMS C state pounds and miles directly, at one decimal ("277.4 mi",
# "21900.0 lb"). Only TMS B converts, and an unrounded conversion carries the
# whole float residue — 283.71799860000004 mi for a load whose two siblings read
# 283.7. That is arithmetically the more precise number and the wrong one to
# keep: it renders to a broker as-is, and TRACEABILITY.md's hand-checkable
# arithmetic is written to one decimal. Rounding at conversion makes all three
# adapters produce the same shape of number, and makes TMS B's kg/km loads agree
# with the US-unit loads they sit next to in the same lane.
#
# Rounding happens **at conversion and nowhere else**. Rounding again downstream
# would let a stored value and a recomputed one drift apart, which is the class
# of bug invariant 2 is about one level up.
#
# Money is deliberately excluded. All three sources state currency at two
# decimals already, and a TMS B carrier rate is a running sum over every line
# item ever appended (see :meth:`AdaptedSync.rate_contributions`) — the one place
# a rounding step would compound rather than cancel.
UNIT_DECIMALS = 1


# ---------------------------------------------------------------------------
# Scalars
# ---------------------------------------------------------------------------


def text(value: object) -> str | None:
    """A trimmed string, or ``None`` for missing/blank.

    Blank collapses to ``None`` because ``""`` and absent mean the same thing in
    every field this is used on (a name, a phone number). Equipment is the
    exception and does **not** go through here: TMS A's ``""`` is a meaningful
    free-text value that maps to ``UNKNOWN``.
    """
    if value is None:
        return None
    stripped = str(value).strip()
    return stripped or None


def source_id(value: object) -> str | None:
    """A source system's own id as a string.

    TMS A numbers its loads and carriers, TMS B its carriers, TMS C uses opaque
    18-character strings. Canonical ids are text, so the numeric ones are
    stringified once, here, rather than at eight call sites that could disagree
    about whether ``127472397`` and ``"127472397"`` are the same load.
    """
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip() or None


def optional_float(value: object) -> float | None:
    """A number, or ``None`` when absent or unparseable.

    An unparseable numeric field degrades to ``None`` rather than raising: the
    resulting load displays with a blank weight instead of failing the whole
    file, and ``None`` already means "we were not told" everywhere downstream. It
    cannot be mistaken for a real value, which is the property that matters.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


#: Money is just a number here; the alias exists so a reader of an adapter can
#: see which fields are dollars (CLAUDE.md, Money row) without checking types.
money = optional_float


def kg_to_lbs(kg: object) -> float | None:
    """Kilograms to pounds (TMS B ``weight_kg``), to one decimal."""
    value = optional_float(kg)
    return None if value is None else round(value * KG_TO_LBS, UNIT_DECIMALS)


def km_to_miles(km: object) -> float | None:
    """Kilometres to miles (TMS B ``dist_km``), to one decimal."""
    value = optional_float(km)
    return None if value is None else round(value * KM_TO_MILES, UNIT_DECIMALS)


def weight_to_lbs(weight: object, units: object) -> float | None:
    """One weight with its own unit label, in pounds.

    TMS C states ``bos__Weight_Units__c`` **per line item**, so a ``kg`` item can
    sit among ``lbs`` items on the same load and the units must be applied before
    summing, never after. An unrecognized unit label is treated as pounds, which
    is what the schema comment says the field usually is; the alternative —
    dropping the line item — would silently understate the load's weight, and
    weight is never used to make a decision, only displayed.

    Each item is rounded here, so the load's total is the exact sum of the parts
    the UI displays beside it. Rounding the total instead would let a line-item
    list visibly fail to add up.
    """
    value = optional_float(weight)
    if value is None:
        return None
    label = (text(units) or "").lower()
    if label in ("kg", "kgs", "kilogram", "kilograms"):
        return round(value * KG_TO_LBS, UNIT_DECIMALS)
    return round(value, UNIT_DECIMALS)


# ---------------------------------------------------------------------------
# Equipment — invariant 5 lives in one place
# ---------------------------------------------------------------------------


def _equipment_or_unknown(matched: Equipment | None) -> Equipment:
    """The single gate every equipment value passes through.

    Null, empty, and unrecognized all land on ``UNKNOWN``. There is exactly one
    ``return Equipment.UNKNOWN`` behind the three parsers precisely so no future
    edit can turn it into ``DRY_VAN`` for one TMS and not the others.
    """
    return matched if matched is not None else Equipment.UNKNOWN


#: TMS A is free text ("53 ft Van | Dry", "53 ft Van | Reefer", "48 ft Flatbed",
#: and "" when nobody filled it in). Order matters: a reefer description also
#: contains "van", so the most specific keyword has to win.
_A_KEYWORDS: tuple[tuple[tuple[str, ...], Equipment], ...] = (
    (("reefer", "refrigerated", "temp control", "temp-control"), Equipment.REEFER),
    (("flatbed", "flat bed", "step deck", "stepdeck"), Equipment.FLATBED),
    (("dry van", "van", "dry"), Equipment.DRY_VAN),
)

_B_CODES = {"V": Equipment.DRY_VAN, "R": Equipment.REEFER, "F": Equipment.FLATBED}

_C_PICKLIST = {
    "dry van": Equipment.DRY_VAN,
    "reefer": Equipment.REEFER,
    "flatbed": Equipment.FLATBED,
}


def equipment_from_free_text(value: object) -> Equipment:
    """TMS A. Keyword match over a free-text trailer description."""
    if value is None:
        return _equipment_or_unknown(None)
    description = str(value).strip().lower()
    for keywords, equipment in _A_KEYWORDS:
        if any(word in description for word in keywords):
            return _equipment_or_unknown(equipment)
    return _equipment_or_unknown(None)


def equipment_from_code(value: object) -> Equipment:
    """TMS B. A closed ``V``/``R``/``F`` code column.

    The column has no code for "unknown", so anything else is a data error — and
    a data error resolves to ``UNKNOWN``, not to the most common value.
    """
    code = (text(value) or "").upper()
    return _equipment_or_unknown(_B_CODES.get(code))


def equipment_from_picklist(value: object) -> Equipment:
    """TMS C. A nullable picklist; the schema comment warns about the null."""
    label = (text(value) or "").lower()
    return _equipment_or_unknown(_C_PICKLIST.get(label))


# ---------------------------------------------------------------------------
# Status — PRD section 5's mapping table, one dict per source vocabulary
# ---------------------------------------------------------------------------

_A_STATUS = {
    "Quoting": LoadStatus.PLANNED,
    "Booking": LoadStatus.ACTIVE,
    "Dispatched": LoadStatus.COVERED,
    "At Shipper": LoadStatus.IN_TRANSIT,
    "En Route": LoadStatus.IN_TRANSIT,
    "At Receiver": LoadStatus.IN_TRANSIT,
    "Delivered": LoadStatus.DELIVERED,
    "Completed": LoadStatus.COMPLETED,
}

_B_STATUS = {
    10: LoadStatus.PLANNED,
    20: LoadStatus.ACTIVE,
    30: LoadStatus.COVERED,
    40: LoadStatus.IN_TRANSIT,
    50: LoadStatus.DELIVERED,
    90: LoadStatus.COMPLETED,
}

# "Delivered" and "Invoiced" both map to DELIVERED: PRD section 5 puts them on
# the same rung, because invoicing is a back-office step that says nothing new
# about the freight.
_C_STATUS = {
    "Quotes Requested": LoadStatus.PLANNED,
    "Ready to Book": LoadStatus.ACTIVE,
    "Booked": LoadStatus.COVERED,
    "In Transit": LoadStatus.IN_TRANSIT,
    "Delivered": LoadStatus.DELIVERED,
    "Invoiced": LoadStatus.DELIVERED,
    "Paid": LoadStatus.COMPLETED,
}


def _status_or_raise(matched: LoadStatus | None, raw: object, tms: str) -> LoadStatus:
    if matched is None:
        raise AdapterError(
            f"TMS {tms}: status {raw!r} is not in the documented vocabulary. "
            "Every canonical status asserts where the load is, so there is no "
            "safe default to fall back to."
        )
    return matched


def status_from_a(value: object) -> LoadStatus:
    """TMS A's eight-value status string."""
    return _status_or_raise(_A_STATUS.get(text(value) or ""), value, "A")


def status_from_b(value: object) -> LoadStatus:
    """TMS B's numeric ``status_code``. Accepts ``30`` and ``"30"`` alike."""
    code = optional_float(value)
    key = int(code) if code is not None and float(code).is_integer() else None
    return _status_or_raise(_B_STATUS.get(key) if key is not None else None, value, "B")


def status_from_c(value: object) -> LoadStatus:
    """TMS C's ``bos__Load_Status__c`` picklist."""
    return _status_or_raise(_C_STATUS.get(text(value) or ""), value, "C")


# ---------------------------------------------------------------------------
# Time — CLAUDE.md's Time row, plus DECISIONS.md D16
# ---------------------------------------------------------------------------


def parse_offset_datetime(value: object) -> datetime | None:
    """Parse a timestamp that carries its own offset (TMS A and TMS C).

    TMS A writes ``2026-07-06T04:12:44-05:00``, TMS C writes
    ``2026-07-06T09:40:02.000+0000``; ``fromisoformat`` handles both on 3.11+.
    A value without an offset is rejected rather than assumed — the whole point
    of the split between this and :func:`parse_central_naive` is that the caller
    has to say which convention the field follows.
    """
    raw = text(value)
    if raw is None:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        raise AdapterError(
            f"timestamp {raw!r} was expected to carry an offset but is naive; "
            "use parse_central_naive if this field is TMS B wall-clock time"
        )
    return parsed


def parse_central_naive(value: object) -> datetime | None:
    """Parse a TMS B naive timestamp as US Central and return it in UTC.

    ``"2026-07-06 03:45:33"`` in July is CDT, so UTC-5 — and in January the same
    string would be CST, UTC-6. The conversion is delegated to
    :func:`app.domain.localtime.central_naive_to_utc` so the DST rule is the
    zoneinfo database's job and not a constant anyone can typo.
    """
    raw = text(value)
    if raw is None:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        # TMS B is documented as offset-free; if one ever arrives with an offset
        # it is already unambiguous, so honour it rather than overriding it.
        return parsed
    return central_naive_to_utc(parsed)


def parse_local_date(value: object) -> date | None:
    """A bare ``YYYY-MM-DD`` as a local Central calendar date.

    TMS B's ``pu_date``/``del_date`` and TMS C's ``bos__Scheduled_Date__c``.
    Deliberately *not* turned into a datetime: it is a date on the local
    calendar, and on-time compares it against a Central date (D7/D16). Giving it
    a fictional midnight would invite a timezone conversion that shifts the day.
    """
    raw = text(value)
    if raw is None:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Location
# ---------------------------------------------------------------------------


def stop_location(
    city: object,
    state: object,
    zip_code: object,
    name: object = None,
) -> StopLocation:
    """Resolve a stop's address against the offline geo table.

    Always returns a :class:`StopLocation`: an address that does not resolve is
    **geo-null** (``place=None``) with its raw city/state/zip intact, so the stop
    still renders while contributing nothing to a lane statistic (CLAUDE.md,
    Location row). Never raises, never substitutes a nearby city.
    """
    city_text = text(city)
    state_text = text(state)
    zip_text = text(zip_code)
    return StopLocation(
        city=city_text,
        state=state_text,
        zip=zip_text,
        place=resolve_place(city_text, state_text, zip_text),
        name=text(name),
    )
