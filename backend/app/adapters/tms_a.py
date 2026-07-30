"""TMS A — FreightFlow. Nested camelCase REST, US units, offsets on every time.

The easy one: pounds and miles are already canonical, ``totalSell``/``totalBuy``
are stated totals, and every timestamp carries ``-05:00`` so nothing has to be
guessed. Three things still need care.

* **Equipment is free text.** ``"53 ft Van | Dry"``, ``"53 ft Van | Reefer"``,
  ``"48 ft Flatbed"`` — and ``""`` when nobody filled the field in. Blank is a
  legitimate value that means ``UNKNOWN``, never ``DRY_VAN`` (invariant 5).
* **A correction is a restatement.** ``totalBuy`` simply holds a different number
  in a later sync (PRD section 4, scenario 2). The adapter reports what the file
  says; noticing that it changed is ingestion's job.
* **``lastModifiedDate`` can go backwards.** Deliberately, in the fixtures. It is
  parsed and passed through untouched — file order comes from the *filename*
  (invariant 4), so an out-of-order field is data to record, not to correct.
"""

from __future__ import annotations

from ..domain.localtime import central_date
from ..domain.model import Carrier, Customer, Load, Stop
from .base import AdapterError, AdaptedSync, SyncBuilder, TmsAdapter
from .normalize import (
    equipment_from_free_text,
    money,
    optional_float,
    parse_offset_datetime,
    source_id,
    status_from_a,
    stop_location,
    text,
)

__all__ = ["TmsAFreightFlowAdapter"]


class TmsAFreightFlowAdapter(TmsAdapter):
    """Adapts one FreightFlow sync file."""

    tms_type = "A"
    data_dir = "tms_a_freightflow"

    def adapt(self, sync_file: str, payload: dict) -> AdaptedSync:
        synced_at = parse_offset_datetime(payload.get("syncedAt"))
        if synced_at is None:
            raise AdapterError(f"{sync_file}: no usable 'syncedAt' in the envelope")

        builder = SyncBuilder()
        for raw_load in payload.get("loads") or ():
            self._add_load(sync_file, raw_load, builder)
        return builder.build(sync_file, synced_at)

    # -- one load ----------------------------------------------------------

    def _add_load(self, sync_file: str, raw: dict, builder: SyncBuilder) -> None:
        load_id = source_id(raw.get("shipmentId"))
        if load_id is None:
            raise AdapterError(f"{sync_file}: a load has no 'shipmentId'")

        customer_id = self._add_customer(raw.get("customer"), builder)
        carrier_id = self._add_carrier(raw.get("carrier"), builder)

        stops = tuple(
            self._stop(index, raw_stop)
            for index, raw_stop in enumerate(raw.get("stops") or ())
        )

        builder.add_load(
            Load(
                source_load_id=load_id,
                # FreightFlow has no separate load number: ``shipmentId`` is both
                # the stable key and what a rep reads out loud.
                load_number=load_id,
                status=status_from_a(raw.get("status")),
                equipment=equipment_from_free_text(raw.get("equipment")),
                stops=stops,
                weight_lbs=optional_float(raw.get("weightTotal")),
                distance_miles=optional_float(raw.get("mileage")),
                customer_rate=money(raw.get("totalSell")),
                carrier_rate=money(raw.get("totalBuy")),
                source_carrier_id=carrier_id,
                source_customer_id=customer_id,
                created_at=parse_offset_datetime(raw.get("createdDate")),
                last_modified_at=parse_offset_datetime(raw.get("lastModifiedDate")),
                # Cargo detail is not broken out in this schema; ``weightTotal``
                # is the whole of what FreightFlow says about the freight.
                cargo=(),
            ),
            raw,
        )

    def _stop(self, index: int, raw: dict) -> Stop:
        window_start = parse_offset_datetime(raw.get("estimatedReadyDateTime"))
        is_pickup, is_drop = self._direction(raw.get("stopType"), index)
        return Stop(
            sequence=index + 1,
            is_pickup=is_pickup,
            is_drop=is_drop,
            location=stop_location(raw.get("city"), raw.get("state"), raw.get("zipCode")),
            # The scheduled date is the *Central* date the appointment window
            # opens on. The window carries -05:00, so its own date component is
            # already the local one, but it is taken through ``central_date``
            # anyway so the rule is stated rather than relied upon (D16).
            scheduled_date=None if window_start is None else central_date(window_start),
            window_start=window_start,
            window_end=parse_offset_datetime(raw.get("estimatedCloseDateTime")),
            # FreightFlow records departure only, never arrival.
            actual_arrival=None,
            actual_departure=parse_offset_datetime(raw.get("actualDepartureDateTime")),
        )

    @staticmethod
    def _direction(stop_type: object, index: int) -> tuple[bool, bool]:
        """Read ``stopType`` — "First Pickup" / "Drop" / "Last Drop".

        An unrecognized label falls back to position: the first stop is a pickup,
        anything after it is a drop. That is true of every load in all three
        schemas, and the alternative — a stop that is neither — would silently
        remove the load from every lane statistic by making ``origin`` or
        ``destination`` ``None``.
        """
        label = (text(stop_type) or "").lower()
        is_pickup = "pickup" in label or "pick up" in label
        is_drop = "drop" in label or "deliver" in label or "receiver" in label
        if not (is_pickup or is_drop):
            return index == 0, index != 0
        return is_pickup, is_drop

    # -- related records ---------------------------------------------------

    def _add_customer(self, raw: object, builder: SyncBuilder) -> str | None:
        if not isinstance(raw, dict):
            return None
        customer_id = source_id(raw.get("customerId"))
        if customer_id is None:
            return None
        builder.add_customer(
            Customer(source_customer_id=customer_id, name=text(raw.get("name"))), raw
        )
        return customer_id

    def _add_carrier(self, raw: object, builder: SyncBuilder) -> str | None:
        # ``carrier`` is null until a truck is booked. That is the normal state of
        # a Quoting/Booking load, not a defect.
        if not isinstance(raw, dict):
            return None
        carrier_id = source_id(raw.get("carrierMasterId"))
        if carrier_id is None:
            return None
        builder.add_carrier(
            Carrier(
                source_carrier_id=carrier_id,
                name=text(raw.get("name")),
                mc_number=text(raw.get("mcNumber")),
                dot_number=text(raw.get("dotNumber")),
                phone=text(raw.get("phoneNumber")),
                # FreightFlow states no carrier domicile.
                home_city=None,
                home_state=None,
            ),
            raw,
        )
        return carrier_id
