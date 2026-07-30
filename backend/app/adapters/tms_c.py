"""TMS C — BrokerOS. CRM records, opaque ids, ``referenced_records`` lookups.

US units and UTC timestamps, so the conversions are easy; the hazards are all
about *shape* and about one timezone subtlety the schema does not mention.

* **Weight units are per line item.** ``bos__Weight_Units__c`` is checked on
  every ``bos__Line_Items__r`` row and applied *before* summing. Exactly one
  fixture load carries a ``kg`` item among ``lbs`` items — converting after the
  sum, or reading the first item's unit and applying it to all, both produce a
  wrong total that looks entirely plausible.
* **Equipment is a nullable picklist.** The schema comment says it outright:
  ``null`` means unknown, not dry van (invariant 5).
* **Stops order by ``bos__Number__c``**, not by array position, and a load can
  have more than two. The middle stop of the fixtures' three-stop load is kept
  but is not lane-forming — the lane is first pickup to last drop.
* **The carrier rate is restated silently.** ``bos__Carrier_Rate__c`` simply
  holds a different number in a later sync with no marker that it changed
  (CLAUDE.md, Known traps). Nothing to detect here: report the value, let
  ingestion rebuild.
* **On-time compares in Central (DECISIONS.md D16).**
  ``bos__Scheduled_Date__c`` is a bare *local Central* date while
  ``bos__Arrival_Time__c`` is UTC. The adapter keeps them as what they are — a
  ``date`` and a UTC ``datetime`` — and
  :func:`app.domain.localtime.delivered_on_time` converts the arrival back to
  Central before comparing. Comparing the UTC date directly marks every delivery
  after 19:00 Central a day late, which flips 14 of broker_c's 96 arrivals and
  swings one carrier's on-time rate by 25 points.

Every ``Id`` used on a record resolves in ``referenced_records`` in the same
file. When one does not, the id is kept on the load and no entity record is
emitted for it — a dangling reference is a fact about the export, and inventing
a nameless carrier to hang it on would put a phantom in the carrier list.
"""

from __future__ import annotations

from ..domain.model import CargoItem, Carrier, Customer, Load, Stop, StopLocation
from .base import AdapterError, AdaptedSync, SyncBuilder, TmsAdapter
from .normalize import (
    UNIT_DECIMALS,
    equipment_from_picklist,
    money,
    optional_float,
    parse_local_date,
    parse_offset_datetime,
    status_from_c,
    stop_location,
    text,
    weight_to_lbs,
)

__all__ = ["TmsCBrokerOsAdapter"]


class TmsCBrokerOsAdapter(TmsAdapter):
    """Adapts one BrokerOS sync file."""

    tms_type = "C"
    data_dir = "tms_c_brokeros"

    def adapt(self, sync_file: str, payload: dict) -> AdaptedSync:
        synced_at = parse_offset_datetime(payload.get("synced_at"))
        if synced_at is None:
            raise AdapterError(f"{sync_file}: no usable 'synced_at' in the envelope")

        refs = payload.get("referenced_records")
        refs = refs if isinstance(refs, dict) else {}

        builder = SyncBuilder()
        for record in payload.get("records") or ():
            self._add_load(sync_file, record, refs, builder)
        return builder.build(sync_file, synced_at)

    # -- one load ----------------------------------------------------------

    def _add_load(
        self, sync_file: str, raw: dict, refs: dict, builder: SyncBuilder
    ) -> None:
        load_id = text(raw.get("Id"))
        if load_id is None:
            raise AdapterError(f"{sync_file}: a record has no 'Id'")

        customer_id = self._add_customer(raw.get("bos__Customer__c"), refs, builder)
        carrier_id = self._add_carrier(raw.get("bos__Carrier__c"), refs, builder)
        cargo = self._cargo(raw.get("bos__Line_Items__r") or ())

        builder.add_load(
            Load(
                source_load_id=load_id,
                # ``Id`` is the opaque key; ``Name`` (SHP6743062) is what a human
                # reads. Unlike A and B these are genuinely two different values.
                load_number=text(raw.get("Name")),
                status=status_from_c(raw.get("bos__Load_Status__c")),
                equipment=equipment_from_picklist(raw.get("bos__Equipment_Type__c")),
                stops=self._stops(raw.get("bos__Stops__r") or (), refs),
                weight_lbs=self._total_weight(cargo),
                distance_miles=optional_float(raw.get("bos__Distance_Miles__c")),
                customer_rate=money(raw.get("bos__Customer_Rate__c")),
                carrier_rate=money(raw.get("bos__Carrier_Rate__c")),
                source_carrier_id=carrier_id,
                source_customer_id=customer_id,
                created_at=parse_offset_datetime(raw.get("CreatedDate")),
                last_modified_at=parse_offset_datetime(raw.get("LastModifiedDate")),
                cargo=cargo,
            ),
            raw,
        )

    @staticmethod
    def _cargo(raw_items) -> tuple[CargoItem, ...]:
        """Line items, each converted to pounds by its **own** unit label."""
        return tuple(
            CargoItem(
                commodity=text(item.get("bos__Commodity__c")),
                weight_lbs=weight_to_lbs(
                    item.get("bos__Weight__c"), item.get("bos__Weight_Units__c")
                ),
                pallet_count=optional_float(item.get("bos__Pallet_Count__c")),
            )
            for item in raw_items
            if isinstance(item, dict)
        )

    @staticmethod
    def _total_weight(cargo: tuple[CargoItem, ...]) -> float | None:
        """Sum of the already-normalized line items, or ``None`` if none said.

        ``None`` rather than ``0.0`` for a load with no weighed line items: a
        zero-pound load is a claim, and we were not told one.

        The items are already rounded to one decimal, so the sum is re-rounded
        only to shed binary representation error — the operation is idempotent on
        one-decimal inputs and cannot move the total away from the parts shown
        beside it. It is not a second rounding of a conversion.
        """
        weights = [item.weight_lbs for item in cargo if item.weight_lbs is not None]
        return round(sum(weights), UNIT_DECIMALS) if weights else None

    # -- stops -------------------------------------------------------------

    def _stops(self, raw_stops, refs: dict) -> tuple[Stop, ...]:
        """Child stop records, ordered by ``bos__Number__c``.

        A stop with no usable number sorts last, keeping its position among its
        equally-unnumbered peers — a stable sort, so an export that omits the
        field degrades to array order rather than scrambling. Sequences are then
        renumbered 1..n, which is what the canonical model requires and what
        makes "first pickup, last drop" mean the same thing for every TMS.
        """
        numbered = [stop for stop in raw_stops if isinstance(stop, dict)]
        order = sorted(
            range(len(numbered)),
            key=lambda i: (
                optional_float(numbered[i].get("bos__Number__c")) is None,
                optional_float(numbered[i].get("bos__Number__c")) or 0.0,
                i,
            ),
        )
        return tuple(
            self._stop(sequence, numbered[i], refs, sequence == 1)
            for sequence, i in enumerate(order, start=1)
        )

    def _stop(self, sequence: int, raw: dict, refs: dict, is_first: bool) -> Stop:
        is_pickup = bool(raw.get("bos__Is_Pickup__c"))
        is_drop = bool(raw.get("bos__Is_Dropoff__c"))
        if not (is_pickup or is_drop):
            # Both flags false. Fall back to position rather than leaving a stop
            # that is neither end of anything, which would strip the load out of
            # every lane statistic by making ``origin`` or ``destination`` None.
            is_pickup, is_drop = is_first, not is_first
        return Stop(
            sequence=sequence,
            is_pickup=is_pickup,
            is_drop=is_drop,
            location=self._location(raw.get("bos__Location__c"), refs),
            # A bare local Central date (D16) — deliberately not a datetime.
            scheduled_date=parse_local_date(raw.get("bos__Scheduled_Date__c")),
            # BrokerOS records arrival only, in UTC.
            actual_arrival=parse_offset_datetime(raw.get("bos__Arrival_Time__c")),
            actual_departure=None,
        )

    @staticmethod
    def _location(location_id: object, refs: dict) -> StopLocation:
        record = refs.get(text(location_id) or "")
        if not isinstance(record, dict) or record.get("type") != "Location":
            # Unresolvable reference: nothing is known about this address, so the
            # stop is geo-null with nothing to display. Distinct from a resolved
            # address the geo table has never heard of, which keeps its raw
            # city/state/zip.
            return StopLocation(city=None, state=None, zip=None, place=None)
        return stop_location(
            record.get("bos__City__c"),
            record.get("bos__State__c"),
            record.get("bos__Postal_Code__c"),
            record.get("Name"),
        )

    # -- accounts ----------------------------------------------------------

    def _add_customer(self, account_id: object, refs: dict, builder: SyncBuilder) -> str | None:
        customer_id = text(account_id)
        if customer_id is None:
            return None
        record = self._account(customer_id, refs)
        if record is not None:
            builder.add_customer(
                Customer(source_customer_id=customer_id, name=text(record.get("Name"))),
                record,
            )
        return customer_id

    def _add_carrier(self, account_id: object, refs: dict, builder: SyncBuilder) -> str | None:
        # Null until the load is "Booked" — the normal state of a quote.
        carrier_id = text(account_id)
        if carrier_id is None:
            return None
        record = self._account(carrier_id, refs)
        if record is not None:
            builder.add_carrier(
                Carrier(
                    source_carrier_id=carrier_id,
                    name=text(record.get("Name")),
                    # DECISIONS.md D2: MC/DOT are a disclosed extension of the
                    # provided TMS C schema, which shipped carriers as bare
                    # Accounts with a name.
                    mc_number=text(record.get("bos__MC_Number__c")),
                    dot_number=text(record.get("bos__DOT_Number__c")),
                    # BrokerOS Accounts carry no phone or domicile.
                    phone=None,
                    home_city=None,
                    home_state=None,
                ),
                record,
            )
        return carrier_id

    @staticmethod
    def _account(account_id: str, refs: dict) -> dict | None:
        """The ``Account`` record behind an id, or ``None`` if it dangles.

        ``record_type`` is not checked: the *load field* the id came from already
        said whether it is playing the carrier or the customer role, and trusting
        the reference over the referent keeps a mislabelled export from dropping
        a real relationship.
        """
        record = refs.get(account_id)
        if isinstance(record, dict) and record.get("type") == "Account":
            return record
        return None
