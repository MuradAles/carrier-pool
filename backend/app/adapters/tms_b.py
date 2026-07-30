"""TMS B — HaulDesk. Flat table dump, metric units, naive Central timestamps.

Three of this project's sharpest traps live in this one file format.

* **Metric.** ``weight_kg`` and ``dist_km`` are the only non-US units in the
  system, and both conversions go through :mod:`.normalize` so the constants
  exist once.
* **Naive Central, DST-aware.** ``"2026-07-06 03:45:33"`` has no offset and is US
  Central wall-clock time, so July is UTC-5 and January is UTC-6. A hardcoded
  ``-6`` is the documented bug; so is a hardcoded ``-5``. The zone comes from
  ``zoneinfo`` (:mod:`app.domain.localtime`), never from a constant.
* **Money is a running sum this file cannot finish.** A load's carrier rate is
  the sum of *every* ``pay`` line item ever appended, negatives included; the
  customer rate is the same over ``bill``. One file holds one instalment, so this
  adapter leaves ``Load.carrier_rate`` and ``Load.customer_rate`` as ``None`` and
  returns the file's ``rates`` rows as :class:`~app.domain.model.RateLine`
  records. ``AdaptedSync.rate_contributions()`` states this file's delta per load
  and side. Filtering to ``LINEHAUL`` or dropping negatives would silently
  unwind exactly the correction the fixtures exist to prove.

And the trap that follows from the last one: **a sync can carry rate rows for a
load whose ``loads`` row did not change** (CLAUDE.md, Known traps). The rate line
is emitted with its ``load_num`` regardless of whether that load appears in the
file, which is what makes a rate-only correction representable as an event
(DECISIONS.md D3). One such file exists in the fixtures.
"""

from __future__ import annotations

from ..domain.model import Carrier, Customer, Load, RateLine, RateSide, Stop
from .base import AdapterError, AdaptedSync, SyncBuilder, TmsAdapter
from .normalize import (
    equipment_from_code,
    kg_to_lbs,
    km_to_miles,
    money,
    parse_central_naive,
    parse_local_date,
    source_id,
    status_from_b,
    stop_location,
    text,
)

__all__ = ["TmsBHaulDeskAdapter"]

_SIDES = {"pay": RateSide.CARRIER, "bill": RateSide.CUSTOMER}


class TmsBHaulDeskAdapter(TmsAdapter):
    """Adapts one HaulDesk sync file."""

    tms_type = "B"
    data_dir = "tms_b_hauldesk"

    def adapt(self, sync_file: str, payload: dict) -> AdaptedSync:
        synced_at = parse_central_naive(payload.get("synced_at"))
        if synced_at is None:
            raise AdapterError(f"{sync_file}: no usable 'synced_at' in the envelope")

        builder = SyncBuilder()
        for raw_carrier in payload.get("carriers") or ():
            self._add_carrier(raw_carrier, builder)
        for raw_load in payload.get("loads") or ():
            self._add_load(sync_file, raw_load, builder)
        for raw_rate in payload.get("rates") or ():
            self._add_rate_line(sync_file, raw_rate, builder)
        return builder.build(sync_file, synced_at)

    # -- one load ----------------------------------------------------------

    def _add_load(self, sync_file: str, raw: dict, builder: SyncBuilder) -> None:
        load_id = text(raw.get("load_num"))
        if load_id is None:
            raise AdapterError(f"{sync_file}: a load row has no 'load_num'")

        customer_id = self._add_customer(raw, builder)

        builder.add_load(
            Load(
                source_load_id=load_id,
                # ``load_num`` is both the key and the human-readable number.
                load_number=load_id,
                status=status_from_b(raw.get("status_code")),
                equipment=equipment_from_code(raw.get("equip")),
                stops=self._stops(raw),
                weight_lbs=kg_to_lbs(raw.get("weight_kg")),
                distance_miles=km_to_miles(raw.get("dist_km")),
                # Money deliberately absent: see the module docstring. Ingestion
                # sums the RATE_LINE events. A number here would be a second,
                # always-stale source of truth for the same dollars.
                customer_rate=None,
                carrier_rate=None,
                # ``carrier_ref`` is kept even when the ``carriers`` array does
                # not describe it: it is the TMS's own id, and a later sync may
                # yet introduce the carrier. What we do not do is invent a
                # Carrier record for it.
                source_carrier_id=source_id(raw.get("carrier_ref")),
                source_customer_id=customer_id,
                created_at=parse_central_naive(raw.get("entered_at")),
                last_modified_at=parse_central_naive(raw.get("updated_at")),
                cargo=(),
            ),
            raw,
        )

    @staticmethod
    def _stops(raw: dict) -> tuple[Stop, ...]:
        """Exactly two stops, flattened onto the load row by this schema.

        HaulDesk cannot express a third stop, so the pickup/drop pair is built
        positionally rather than from any direction flag.
        """
        pickup = Stop(
            sequence=1,
            is_pickup=True,
            is_drop=False,
            location=stop_location(raw.get("pu_city"), raw.get("pu_state"), raw.get("pu_zip")),
            scheduled_date=parse_local_date(raw.get("pu_date")),
            actual_departure=parse_central_naive(raw.get("pu_departed_at")),
        )
        drop = Stop(
            sequence=2,
            is_pickup=False,
            is_drop=True,
            location=stop_location(raw.get("del_city"), raw.get("del_state"), raw.get("del_zip")),
            scheduled_date=parse_local_date(raw.get("del_date")),
            actual_arrival=parse_central_naive(raw.get("del_arrived_at")),
        )
        return (pickup, drop)

    # -- related records ---------------------------------------------------

    def _add_customer(self, raw: dict, builder: SyncBuilder) -> str | None:
        """HaulDesk has no customer table: code and name ride the load row."""
        customer_id = text(raw.get("customer_code"))
        if customer_id is None:
            return None
        builder.add_customer(
            Customer(source_customer_id=customer_id, name=text(raw.get("customer_name"))),
            # The load row is this customer's whole provenance in this file.
            raw,
        )
        return customer_id

    def _add_carrier(self, raw: dict, builder: SyncBuilder) -> None:
        carrier_id = source_id(raw.get("carrier_id"))
        if carrier_id is None:
            return
        builder.add_carrier(
            Carrier(
                source_carrier_id=carrier_id,
                name=text(raw.get("carrier_name")),
                mc_number=text(raw.get("mc_no")),
                dot_number=text(raw.get("dot_no")),
                phone=text(raw.get("phone")),
                home_city=text(raw.get("home_city")),
                home_state=text(raw.get("home_state")),
            ),
            raw,
        )

    def _add_rate_line(self, sync_file: str, raw: dict, builder: SyncBuilder) -> None:
        rate_id = source_id(raw.get("rate_id"))
        load_id = text(raw.get("load_num"))
        if rate_id is None or load_id is None:
            raise AdapterError(
                f"{sync_file}: a rates row is missing 'rate_id' or 'load_num' "
                f"({raw!r}); money that cannot be attributed to a load cannot be "
                "dropped silently"
            )
        side = _SIDES.get((text(raw.get("side")) or "").lower())
        if side is None:
            raise AdapterError(
                f"{sync_file}: rate {rate_id} has side {raw.get('side')!r}, which is "
                "neither 'pay' nor 'bill'; guessing would move money between the "
                "carrier and the customer side of the load"
            )
        amount = money(raw.get("amount_usd"))
        if amount is None:
            raise AdapterError(
                f"{sync_file}: rate {rate_id} has no usable 'amount_usd' "
                f"({raw.get('amount_usd')!r})"
            )
        builder.add_rate_line(
            RateLine(
                source_rate_id=rate_id,
                source_load_id=load_id,
                side=side,
                code=text(raw.get("code")),
                amount_usd=amount,
                created_at=parse_central_naive(raw.get("created_at")),
            ),
            raw,
        )
