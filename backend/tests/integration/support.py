"""Fixture-data factories for the tenant-isolation tests.

Kept separate from test bodies so a test reads as "what is asserted", not "how
a Load gets constructed". Every factory takes only what a given test cares
about and fills the rest with fixed, unremarkable defaults -- a DFW -> HOU dry
van load, unless a test says otherwise.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from app.domain.geo import resolve_place
from app.domain.model import (
    Carrier,
    Customer,
    Equipment,
    Load,
    LoadStatus,
    Stop,
    StopLocation,
)

__all__ = ["utc", "make_load", "make_carrier", "make_customer"]


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


def _stop(
    sequence: int,
    *,
    is_pickup: bool,
    is_drop: bool,
    city: str,
    state: str,
    zip_code: str,
    scheduled: date,
    actual: datetime | None,
) -> Stop:
    return Stop(
        sequence=sequence,
        is_pickup=is_pickup,
        is_drop=is_drop,
        location=StopLocation(
            city=city, state=state, zip=zip_code, place=resolve_place(city, state, zip_code)
        ),
        scheduled_date=scheduled,
        actual_arrival=actual if is_drop else None,
        actual_departure=actual if is_pickup else None,
    )


def make_load(
    source_load_id: str,
    *,
    source_carrier_id: str | None = None,
    source_customer_id: str | None = None,
    customer_rate: float | None = 2200.0,
    carrier_rate: float | None = 1800.0,
    distance_miles: float | None = 240.0,
    equipment: Equipment = Equipment.DRY_VAN,
    status: LoadStatus = LoadStatus.COMPLETED,
    origin: tuple[str, str, str] = ("Dallas", "TX", "75201"),
    dest: tuple[str, str, str] = ("Houston", "TX", "77002"),
    pickup_date: date = date(2026, 7, 6),
    delivery_date: date = date(2026, 7, 7),
) -> Load:
    """A minimal, otherwise-unremarkable DFW -> HOU dry van load."""
    o_city, o_state, o_zip = origin
    d_city, d_state, d_zip = dest
    pickup = _stop(
        1,
        is_pickup=True,
        is_drop=False,
        city=o_city,
        state=o_state,
        zip_code=o_zip,
        scheduled=pickup_date,
        actual=utc(pickup_date.year, pickup_date.month, pickup_date.day, 8),
    )
    drop = _stop(
        2,
        is_pickup=False,
        is_drop=True,
        city=d_city,
        state=d_state,
        zip_code=d_zip,
        scheduled=delivery_date,
        actual=utc(delivery_date.year, delivery_date.month, delivery_date.day, 14),
    )
    return Load(
        source_load_id=source_load_id,
        load_number=None,
        status=status,
        equipment=equipment,
        stops=(pickup, drop),
        weight_lbs=40_000.0,
        distance_miles=distance_miles,
        customer_rate=customer_rate,
        carrier_rate=carrier_rate,
        source_carrier_id=source_carrier_id,
        source_customer_id=source_customer_id,
        created_at=utc(pickup_date.year, pickup_date.month, pickup_date.day),
        last_modified_at=utc(delivery_date.year, delivery_date.month, delivery_date.day),
    )


def make_carrier(
    source_carrier_id: str,
    *,
    name: str,
    mc_number: str | None = None,
    dot_number: str | None = None,
    home_city: str | None = "Dallas",
    home_state: str | None = "TX",
) -> Carrier:
    return Carrier(
        source_carrier_id=source_carrier_id,
        name=name,
        mc_number=mc_number,
        dot_number=dot_number,
        phone=None,
        home_city=home_city,
        home_state=home_state,
    )


def make_customer(source_customer_id: str, *, name: str) -> Customer:
    return Customer(source_customer_id=source_customer_id, name=name)
