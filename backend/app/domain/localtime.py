"""US Central local time, and the one comparison that depends on it.

Three TMSs, three timezone conventions, one shared local clock. CLAUDE.md's Time
row: TMS A carries an offset, TMS B emits naive strings that are US Central, TMS
C is already UTC. Sync **filenames** are local Central for all three, which is
the only reason sorting by filename orders the three streams against each other.

Everything Central lives here so the rule is written once:

* **Parsing.** ``central_naive_to_utc`` attaches ``America/Chicago`` and converts
  to UTC. It is DST-aware by construction — July is CDT (UTC-5), January is CST
  (UTC-6). A hardcoded offset either way is a bug, and hardcoding is what this
  function exists to prevent.
* **On-time.** ``delivered_on_time`` is DECISIONS.md D7 (on-time means delivered
  on or before the scheduled delivery *date*) plus D16 (the comparison happens in
  Central, because ``Stop.scheduled_date`` is a local Central calendar date while
  ``Stop.actual_arrival`` is UTC). Comparing a UTC timestamp's date against a
  Central date marks every delivery after 19:00 Central a day late — worth 25
  points on one broker_c carrier's on-time rate.

Both directions of the conversion are needed and both are here, so no caller has
to know which way ``ZoneInfo`` composes.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from .model import Load

__all__ = [
    "US_CENTRAL",
    "central_naive_to_utc",
    "to_central",
    "central_date",
    "delivered_on_time",
]

# The single local clock all three TMSs and every sync filename share.
US_CENTRAL = ZoneInfo("America/Chicago")


def central_naive_to_utc(value: datetime) -> datetime:
    """Read a naive datetime as US Central wall-clock time and return it in UTC.

    Used for TMS B, whose timestamps carry no offset, and for sync filenames.
    ``fold`` is left at its default, so the ambiguous hour of the autumn
    transition resolves to the first (daylight) reading — outside our July
    fixtures, and a choice we have no data to make better.
    """
    if value.tzinfo is not None:
        raise ValueError(
            f"central_naive_to_utc expects a naive datetime, got {value!r}; an "
            "already-aware value has said what it means"
        )
    return value.replace(tzinfo=US_CENTRAL).astimezone(ZoneInfo("UTC"))


def to_central(value: datetime) -> datetime:
    """Express an aware datetime on the Central clock. Naive input raises."""
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(
            f"to_central expects an aware datetime, got naive {value!r}; use "
            "central_naive_to_utc if this value is Central wall-clock time"
        )
    return value.astimezone(US_CENTRAL)


def central_date(value: datetime) -> date:
    """The Central calendar date an aware instant falls on (DECISIONS.md D16)."""
    return to_central(value).date()


def delivered_on_time(load: Load) -> bool | None:
    """Was this load delivered on or before its scheduled delivery date?

    ``None`` when the question cannot be answered — no last drop, no scheduled
    date, or no actual arrival yet. That is a third outcome, not a ``False``:
    a load still in transit has not been late, and folding it into a denominator
    would understate every carrier that has open freight.

    The comparison is day-granular (D7) because that is the only precision all
    three schemas support honestly, and it happens in Central (D16) because
    ``scheduled_date`` is a local date while ``actual_at`` is UTC.
    """
    drop = load.destination
    if drop is None or drop.scheduled_date is None:
        return None
    actual = drop.actual_at
    if actual is None:
        return None
    return central_date(actual) <= drop.scheduled_date
