"""Unit tests for the three TMS adapters and the normalization functions they
share (TASKS.md A5).

CLAUDE.md's normalization table is the specification; PRD section 5 has the
full status mapping; DECISIONS.md D7/D16 define on-time. No database, no
network, no reading the 132-file dataset -- every payload here is a dict built
inline, trimmed to whatever one test needs.

Every arithmetic assertion carries the computation in a comment, so a reviewer
can check it by hand without running anything.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.adapters import (
    AdapterError,
    TmsAFreightFlowAdapter,
    TmsBHaulDeskAdapter,
    TmsCBrokerOsAdapter,
)
from app.adapters.normalize import (
    equipment_from_code,
    equipment_from_free_text,
    equipment_from_picklist,
    kg_to_lbs,
    km_to_miles,
    parse_central_naive,
    status_from_a,
    status_from_b,
    status_from_c,
    weight_to_lbs,
)
from app.domain.localtime import delivered_on_time
from app.domain.model import (
    Equipment,
    EntityType,
    Load,
    LoadStatus,
    RateSide,
    Stop,
    StopLocation,
)

UTC = timezone.utc


def _geo_null() -> StopLocation:
    """A location with nothing resolved -- fine wherever a test doesn't care."""
    return StopLocation(city=None, state=None, zip=None, place=None)


def _stub_load(stops: tuple[Stop, ...] = ()) -> Load:
    """The minimum Load a domain-logic test needs, every optional field None."""
    return Load(
        source_load_id="TEST-1",
        load_number="TEST-1",
        status=LoadStatus.IN_TRANSIT,
        equipment=Equipment.DRY_VAN,
        stops=stops,
        weight_lbs=None,
        distance_miles=None,
        customer_rate=None,
        carrier_rate=None,
        source_carrier_id=None,
        source_customer_id=None,
        created_at=None,
        last_modified_at=None,
    )


# ===========================================================================
# TMS A -- FreightFlow
# ===========================================================================


class TestTmsAFullFixture:
    """``data/tms_a_freightflow/example_sync.jsonc`` -> canonical load, field by field."""

    PAYLOAD = {
        "syncedAt": "2026-07-06T06:00:00-05:00",
        "loads": [
            {
                "shipmentId": 127472397,
                "status": "Booking",
                "mileage": 242.1,
                "totalSell": 1450.0,
                "totalBuy": None,
                "customer": {"customerId": 889264, "name": "Lone Star Beverages"},
                "carrier": None,
                "equipment": "53 ft Van | Dry",
                "weightTotal": 24000.0,
                "stops": [
                    {
                        "stopType": "First Pickup",
                        "city": "GRAND PRAIRIE",
                        "state": "TX",
                        "zipCode": "75050",
                        "estimatedReadyDateTime": "2026-07-07T08:00:00-05:00",
                        "estimatedCloseDateTime": "2026-07-07T16:00:00-05:00",
                        "actualDepartureDateTime": None,
                    },
                    {
                        "stopType": "Last Drop",
                        "city": "KATY",
                        "state": "TX",
                        "zipCode": "77449",
                        "estimatedReadyDateTime": "2026-07-08T08:00:00-05:00",
                        "estimatedCloseDateTime": "2026-07-08T16:00:00-05:00",
                        "actualDepartureDateTime": None,
                    },
                ],
                "createdDate": "2026-07-06T04:12:44-05:00",
                "lastModifiedDate": "2026-07-06T04:12:44-05:00",
            }
        ],
    }

    def test_adapts_the_sample_field_by_field(self) -> None:
        adapted = TmsAFreightFlowAdapter().adapt("2026-07-06T06-00_sync.json", self.PAYLOAD)

        # Envelope: "2026-07-06T06:00:00-05:00" + 5h -> UTC.
        assert adapted.synced_at == datetime(2026, 7, 6, 11, 0, 0, tzinfo=UTC)

        assert len(adapted.loads) == 1
        load = adapted.loads[0]
        # shipmentId is numeric in the source; source_id stringifies it once.
        assert load.source_load_id == "127472397"
        assert load.load_number == "127472397"  # no separate load number in A
        assert load.status is LoadStatus.ACTIVE  # "Booking" -> ACTIVE
        assert load.equipment is Equipment.DRY_VAN  # "53 ft Van | Dry" -> DRY_VAN
        assert load.weight_lbs == 24000.0  # already lbs, untouched
        assert load.distance_miles == 242.1  # already miles, untouched
        assert load.customer_rate == 1450.0  # totalSell, stated as given
        assert load.carrier_rate is None  # totalBuy null: no carrier booked yet
        assert load.source_customer_id == "889264"
        assert load.source_carrier_id is None  # carrier block is null
        # "2026-07-06T04:12:44-05:00" + 5h -> UTC.
        assert load.created_at == datetime(2026, 7, 6, 9, 12, 44, tzinfo=UTC)
        assert load.last_modified_at == datetime(2026, 7, 6, 9, 12, 44, tzinfo=UTC)
        assert load.cargo == ()  # FreightFlow states no cargo detail

        assert len(load.stops) == 2
        origin, dest = load.stops
        assert origin.sequence == 1
        assert (origin.is_pickup, origin.is_drop) == (True, False)
        assert origin.location.city == "GRAND PRAIRIE"  # raw casing preserved
        assert origin.location.state == "TX"
        assert origin.location.zip == "75050"
        assert not origin.location.is_geo_null  # Grand Prairie resolves
        # window opens "2026-07-07T08:00:00-05:00": the -05:00 offset already
        # *is* Central in July, so central_date does not shift the day.
        assert origin.scheduled_date == date(2026, 7, 7)
        assert origin.window_start == datetime(2026, 7, 7, 13, 0, 0, tzinfo=UTC)
        assert origin.window_end == datetime(2026, 7, 7, 21, 0, 0, tzinfo=UTC)
        assert origin.actual_departure is None  # actualDepartureDateTime null

        assert dest.sequence == 2
        assert (dest.is_pickup, dest.is_drop) == (False, True)
        assert dest.location.city == "KATY"
        assert dest.location.zip == "77449"
        assert dest.scheduled_date == date(2026, 7, 8)
        assert dest.actual_arrival is None  # FreightFlow never reports arrival

        assert load.origin is origin
        assert load.destination is dest

        # customer block produces a Customer record; carrier block does not.
        assert len(adapted.customers) == 1
        assert adapted.customers[0].source_customer_id == "889264"
        assert adapted.customers[0].name == "Lone Star Beverages"
        assert adapted.carriers == ()


class TestTmsAStatus:
    """PRD section 5: TMS A's eight statuses, three collapsing to IN_TRANSIT."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Quoting", LoadStatus.PLANNED),
            ("Booking", LoadStatus.ACTIVE),
            ("Dispatched", LoadStatus.COVERED),
            ("At Shipper", LoadStatus.IN_TRANSIT),
            ("En Route", LoadStatus.IN_TRANSIT),
            ("At Receiver", LoadStatus.IN_TRANSIT),
            ("Delivered", LoadStatus.DELIVERED),
            ("Completed", LoadStatus.COMPLETED),
        ],
    )
    def test_every_documented_status(self, raw: str, expected: LoadStatus) -> None:
        assert status_from_a(raw) is expected

    def test_three_statuses_collapse_to_in_transit(self) -> None:
        collapsed = {status_from_a(s) for s in ("At Shipper", "En Route", "At Receiver")}
        assert collapsed == {LoadStatus.IN_TRANSIT}

    def test_unrecognized_status_raises(self) -> None:
        # No safe default: guessing wrong moves a load into/out of the pool
        # that backs every lane statistic (normalize.py docstring).
        with pytest.raises(AdapterError):
            status_from_a("Cancelled")


class TestTmsAEquipment:
    """Free-text keyword match; blank and unrecognized both land on UNKNOWN."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("53 ft Van | Dry", Equipment.DRY_VAN),
            ("53 ft Van | Reefer", Equipment.REEFER),  # "reefer" outranks "van"
            ("48 ft Flatbed", Equipment.FLATBED),
            ("", Equipment.UNKNOWN),  # blank is a legitimate value, not a gap
            (None, Equipment.UNKNOWN),
            ("   ", Equipment.UNKNOWN),
            ("Conestoga Wagon", Equipment.UNKNOWN),  # unrecognized free text
        ],
    )
    def test_every_case(self, raw: str | None, expected: Equipment) -> None:
        assert equipment_from_free_text(raw) is expected

    def test_null_does_not_become_dry_van(self) -> None:
        assert equipment_from_free_text(None) is not Equipment.DRY_VAN
        assert equipment_from_free_text(None) is Equipment.UNKNOWN


# ===========================================================================
# TMS B -- HaulDesk
# ===========================================================================


class TestTmsBFullFixture:
    """``data/tms_b_hauldesk/example_sync.jsonc`` -> canonical load, field by field."""

    PAYLOAD = {
        "synced_at": "2026-07-06 06:00:00",
        "loads": [
            {
                "load_num": "HD-2026-004417",
                "status_code": 30,
                "customer_code": "C-0031",
                "customer_name": "Alamo Building Supply",
                "carrier_ref": 66861,
                "equip": "V",
                "weight_kg": 10886.2,
                "dist_km": 389.6,
                "pu_city": "New Braunfels",
                "pu_state": "TX",
                "pu_zip": "78130",
                "pu_date": "2026-07-07",
                "pu_departed_at": None,
                "del_city": "Pasadena",
                "del_state": "TX",
                "del_zip": "77502",
                "del_date": "2026-07-08",
                "del_arrived_at": None,
                "entered_at": "2026-07-05 14:22:10",
                "updated_at": "2026-07-06 03:45:33",
            }
        ],
        "carriers": [
            {
                "carrier_id": 66861,
                "carrier_name": "DELTA PRIME LLC",
                "mc_no": "884201",
                "dot_no": "2551377",
                "home_city": "Seguin",
                "home_state": "TX",
                "phone": "(830) 555-0144",
            }
        ],
        "rates": [
            {
                "rate_id": 910233,
                "load_num": "HD-2026-004417",
                "side": "pay",
                "code": "LINEHAUL",
                "amount_usd": 1035.00,
                "created_at": "2026-07-06 03:45:33",
            },
            {
                "rate_id": 910234,
                "load_num": "HD-2026-004417",
                "side": "bill",
                "code": "LINEHAUL",
                "amount_usd": 1310.00,
                "created_at": "2026-07-06 03:45:33",
            },
        ],
    }

    def test_adapts_the_sample_field_by_field(self) -> None:
        adapted = TmsBHaulDeskAdapter().adapt("2026-07-06T06-00_sync.json", self.PAYLOAD)

        # "2026-07-06 06:00:00" is July Central (CDT, UTC-5): +5h -> UTC.
        assert adapted.synced_at == datetime(2026, 7, 6, 11, 0, 0, tzinfo=UTC)

        assert len(adapted.loads) == 1
        load = adapted.loads[0]
        assert load.source_load_id == "HD-2026-004417"
        assert load.load_number == "HD-2026-004417"
        assert load.status is LoadStatus.COVERED  # status_code 30
        assert load.equipment is Equipment.DRY_VAN  # "V"
        # 10886.2 kg x 2.20462 = 23999.934444 -> rounded to one decimal.
        assert load.weight_lbs == 23999.9
        # 389.6 km x 0.621371 = 242.0861416 -> rounded to one decimal.
        assert load.distance_miles == 242.1
        # Money is deliberately absent on the Load: TMS B's total is a running
        # sum ingestion builds from RATE_LINE events, not a fact one file states.
        assert load.customer_rate is None
        assert load.carrier_rate is None
        assert load.source_carrier_id == "66861"
        assert load.source_customer_id == "C-0031"
        # "2026-07-05 14:22:10" Central July: +5h -> UTC.
        assert load.created_at == datetime(2026, 7, 5, 19, 22, 10, tzinfo=UTC)
        # "2026-07-06 03:45:33" Central July: +5h -> UTC.
        assert load.last_modified_at == datetime(2026, 7, 6, 8, 45, 33, tzinfo=UTC)
        assert load.cargo == ()

        assert len(load.stops) == 2
        pickup, drop = load.stops
        assert (pickup.sequence, pickup.is_pickup, pickup.is_drop) == (1, True, False)
        assert pickup.location.city == "New Braunfels"
        assert pickup.location.zip == "78130"
        assert not pickup.location.is_geo_null
        assert pickup.scheduled_date == date(2026, 7, 7)  # pure date, no tz math
        assert pickup.actual_departure is None

        assert (drop.sequence, drop.is_pickup, drop.is_drop) == (2, False, True)
        assert drop.location.city == "Pasadena"
        assert drop.location.zip == "77502"
        assert drop.scheduled_date == date(2026, 7, 8)
        assert drop.actual_arrival is None

        assert len(adapted.carriers) == 1
        carrier = adapted.carriers[0]
        assert carrier.source_carrier_id == "66861"
        assert carrier.name == "DELTA PRIME LLC"
        assert carrier.mc_number == "884201"
        assert carrier.dot_number == "2551377"
        assert carrier.home_city == "Seguin"
        assert carrier.home_state == "TX"
        assert carrier.phone == "(830) 555-0144"

        assert len(adapted.customers) == 1
        assert adapted.customers[0].source_customer_id == "C-0031"
        assert adapted.customers[0].name == "Alamo Building Supply"

        assert len(adapted.rate_lines) == 2
        pay, bill = adapted.rate_lines
        assert pay.source_load_id == "HD-2026-004417"
        assert pay.side is RateSide.CARRIER  # "pay" -> carrier side
        assert pay.amount_usd == 1035.00
        assert pay.created_at == datetime(2026, 7, 6, 8, 45, 33, tzinfo=UTC)
        assert bill.side is RateSide.CUSTOMER  # "bill" -> customer side
        assert bill.amount_usd == 1310.00

        assert adapted.rate_contributions() == {
            ("HD-2026-004417", RateSide.CARRIER): 1035.00,
            ("HD-2026-004417", RateSide.CUSTOMER): 1310.00,
        }

    def test_record_order_is_carrier_customer_load_then_rate_lines(self) -> None:
        # base.py: carriers and customers precede the loads that reference
        # them; rate lines come last, since a rate line is a fact about a load.
        adapted = TmsBHaulDeskAdapter().adapt("2026-07-06T06-00_sync.json", self.PAYLOAD)
        assert [r.entity_type for r in adapted.records] == [
            EntityType.CARRIER,
            EntityType.CUSTOMER,
            EntityType.LOAD,
            EntityType.RATE_LINE,
            EntityType.RATE_LINE,
        ]


class TestTmsBStatus:
    """PRD section 5: TMS B's six numeric status codes."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (10, LoadStatus.PLANNED),
            (20, LoadStatus.ACTIVE),
            (30, LoadStatus.COVERED),
            (40, LoadStatus.IN_TRANSIT),
            (50, LoadStatus.DELIVERED),
            (90, LoadStatus.COMPLETED),
        ],
    )
    def test_every_documented_code(self, raw: int, expected: LoadStatus) -> None:
        assert status_from_b(raw) is expected

    def test_stringified_code_is_accepted(self) -> None:
        assert status_from_b("30") is LoadStatus.COVERED

    def test_unrecognized_code_raises(self) -> None:
        with pytest.raises(AdapterError):
            status_from_b(99)


class TestTmsBEquipment:
    """A closed V/R/F code column; anything else is a data error -> UNKNOWN."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("V", Equipment.DRY_VAN),
            ("R", Equipment.REEFER),
            ("F", Equipment.FLATBED),
            ("v", Equipment.DRY_VAN),  # case-tolerant
            (None, Equipment.UNKNOWN),
            ("", Equipment.UNKNOWN),
            ("   ", Equipment.UNKNOWN),
            ("X", Equipment.UNKNOWN),  # not a documented code
        ],
    )
    def test_every_case(self, raw: str | None, expected: Equipment) -> None:
        assert equipment_from_code(raw) is expected

    def test_null_does_not_become_dry_van(self) -> None:
        assert equipment_from_code(None) is not Equipment.DRY_VAN
        assert equipment_from_code(None) is Equipment.UNKNOWN


class TestTmsBTimeDstAware:
    """CLAUDE.md Time row: naive Central, and it must know about DST."""

    def test_july_is_cdt_utc_minus_5(self) -> None:
        # 03:45:33 CDT + 5h -> 08:45:33 UTC.
        assert parse_central_naive("2026-07-06 03:45:33") == datetime(
            2026, 7, 6, 8, 45, 33, tzinfo=UTC
        )

    def test_january_is_cst_utc_minus_6(self) -> None:
        # Same wall-clock string, six months later: 03:45:33 CST + 6h -> 09:45:33 UTC.
        # A hardcoded offset of either sign passes only one of these two tests.
        assert parse_central_naive("2026-01-06 03:45:33") == datetime(
            2026, 1, 6, 9, 45, 33, tzinfo=UTC
        )


class TestTmsBMoney:
    """Money row: sum of every rates line item per side, negatives included."""

    def test_sum_includes_adjustment_and_fuel(self) -> None:
        payload = {
            "synced_at": "2026-07-08 00:00:00",
            "loads": [],
            "carriers": [],
            "rates": [
                {
                    "rate_id": 1,
                    "load_num": "HD-2026-009900",
                    "side": "pay",
                    "code": "LINEHAUL",
                    "amount_usd": 1035.00,
                    "created_at": "2026-07-08 00:00:00",
                },
                {
                    "rate_id": 2,
                    "load_num": "HD-2026-009900",
                    "side": "pay",
                    "code": "FUEL",
                    "amount_usd": 150.25,
                    "created_at": "2026-07-08 00:00:00",
                },
                {
                    "rate_id": 3,
                    "load_num": "HD-2026-009900",
                    "side": "pay",
                    "code": "ADJUSTMENT",
                    "amount_usd": -75.00,
                    "created_at": "2026-07-08 00:00:00",
                },
                {
                    "rate_id": 4,
                    "load_num": "HD-2026-009900",
                    "side": "bill",
                    "code": "LINEHAUL",
                    "amount_usd": 1310.00,
                    "created_at": "2026-07-08 00:00:00",
                },
            ],
        }
        adapted = TmsBHaulDeskAdapter().adapt("2026-07-08T00-00_sync.json", payload)

        # carrier (pay) side: 1035.00 + 150.25 - 75.00 = 1110.25
        # customer (bill) side: 1310.00
        assert adapted.rate_contributions() == {
            ("HD-2026-009900", RateSide.CARRIER): 1110.25,
            ("HD-2026-009900", RateSide.CUSTOMER): 1310.00,
        }

    def test_contribution_is_unrounded_and_can_carry_float_dust(self) -> None:
        """Money is deliberately *not* rounded -- unlike weight/distance.

        1200.33 - 479.19 is 721.14 on paper, but summed as raw ``float`` it
        lands on 721.1399999999999 (verified: ``repr(1200.33 + -479.19)``).
        That residue is expected and correct at this layer -- ``loads`` is a
        ``NUMERIC(12,2)`` column, so Postgres rounds at the storage boundary,
        not the adapter. Asserting a rounded contribution here would hide a
        regression where someone "cleans up" this sum and it silently stops
        matching what ingestion actually adds up.
        """
        payload = {
            "synced_at": "2026-07-10 00:00:00",
            "loads": [],
            "carriers": [],
            "rates": [
                {
                    "rate_id": 1,
                    "load_num": "HD-2026-009901",
                    "side": "pay",
                    "code": "LINEHAUL",
                    "amount_usd": 1200.33,
                    "created_at": "2026-07-10 00:00:00",
                },
                {
                    "rate_id": 2,
                    "load_num": "HD-2026-009901",
                    "side": "pay",
                    "code": "ADJUSTMENT",
                    "amount_usd": -479.19,
                    "created_at": "2026-07-10 00:00:00",
                },
            ],
        }
        adapted = TmsBHaulDeskAdapter().adapt("2026-07-10T00-00_sync.json", payload)
        contribution = adapted.rate_contributions()[("HD-2026-009901", RateSide.CARRIER)]
        assert contribution == 721.1399999999999  # not the clean 721.14
        assert contribution != round(contribution, 2)  # the dust is real, not a typo

    def test_rate_row_for_a_load_absent_from_this_files_loads_array(self) -> None:
        """CLAUDE.md's named trap: a rate-only correction with no `loads` row.

        The event must still be recorded and the money still recomputed, even
        though this file's ``loads`` array says nothing about the load.
        """
        payload = {
            "synced_at": "2026-07-09 00:00:00",
            "loads": [],  # the load's own row did not change in this file
            "carriers": [],
            "rates": [
                {
                    "rate_id": 910299,
                    "load_num": "HD-2026-004417",  # not in `loads` above
                    "side": "pay",
                    "code": "ADJUSTMENT",
                    "amount_usd": -150.00,
                    "created_at": "2026-07-09 00:00:00",
                }
            ],
        }
        adapted = TmsBHaulDeskAdapter().adapt("2026-07-09T00-00_sync.json", payload)

        assert adapted.loads == ()  # no LOAD event this file
        assert len(adapted.rate_lines) == 1  # but the RATE_LINE event is still here
        line = adapted.rate_lines[0]
        assert line.source_load_id == "HD-2026-004417"
        assert line.side is RateSide.CARRIER
        assert line.amount_usd == -150.00
        assert adapted.rate_contributions() == {
            ("HD-2026-004417", RateSide.CARRIER): -150.00
        }


class TestTmsBDefensiveParsing:
    def test_missing_optional_fields_degrade_rather_than_raise(self) -> None:
        payload = {
            "synced_at": "2026-07-06 06:00:00",
            "loads": [{"load_num": "HD-MINIMAL-1", "status_code": 20}],
            "carriers": [],
            "rates": [],
        }
        adapted = TmsBHaulDeskAdapter().adapt("2026-07-06T06-00_sync.json", payload)

        load = adapted.loads[0]
        assert load.equipment is Equipment.UNKNOWN  # missing "equip"
        assert load.weight_lbs is None
        assert load.distance_miles is None
        assert load.source_carrier_id is None
        assert load.source_customer_id is None
        assert load.created_at is None
        assert load.last_modified_at is None
        assert adapted.carriers == ()
        assert adapted.customers == ()

        pickup, drop = load.stops
        assert pickup.location.city is None
        assert pickup.location.is_geo_null
        assert pickup.scheduled_date is None
        assert drop.location.city is None
        assert drop.scheduled_date is None


# ===========================================================================
# TMS C -- BrokerOS
# ===========================================================================


class TestTmsCFullFixture:
    """``data/tms_c_brokeros/example_sync.jsonc`` -> canonical load, field by field."""

    PAYLOAD = {
        "synced_at": "2026-07-06T11:00:00.000+0000",
        "records": [
            {
                "Id": "a0jO900000YgsYJIAZ",
                "Name": "SHP6743062",
                "bos__Load_Status__c": "Ready to Book",
                "bos__Distance_Miles__c": 197.4,
                "bos__Customer__c": "0011I00000NMUrPQAX",
                "bos__Carrier__c": None,
                "bos__Equipment_Type__c": "Reefer",
                "bos__Customer_Rate__c": 1720.00,
                "bos__Carrier_Rate__c": None,
                "bos__Stops__r": [
                    {
                        "bos__Number__c": 1.0,
                        "bos__Is_Pickup__c": True,
                        "bos__Is_Dropoff__c": False,
                        "bos__Location__c": "0011I00000HAeJnQAL",
                        "bos__Scheduled_Date__c": "2026-07-07",
                        "bos__Arrival_Time__c": None,
                    },
                    {
                        "bos__Number__c": 2.0,
                        "bos__Is_Pickup__c": False,
                        "bos__Is_Dropoff__c": True,
                        "bos__Location__c": "0011I00000NMha6QAD",
                        "bos__Scheduled_Date__c": "2026-07-08",
                        "bos__Arrival_Time__c": None,
                    },
                ],
                "bos__Line_Items__r": [
                    {
                        "bos__Commodity__c": "Packaged foods",
                        "bos__Weight__c": 14440.0,
                        "bos__Weight_Units__c": "lbs",
                        "bos__Pallet_Count__c": 18.0,
                    }
                ],
                "CreatedDate": "2026-07-06T09:40:02.000+0000",
                "LastModifiedDate": "2026-07-06T09:40:02.000+0000",
            }
        ],
        "referenced_records": {
            "0011I00000HAeJnQAL": {
                "type": "Location",
                "Name": "Sugar Land Cold Storage",
                "bos__City__c": "Sugar Land",
                "bos__State__c": "TX",
                "bos__Postal_Code__c": "77478",
            },
            "0011I00000NMha6QAD": {
                "type": "Location",
                "Name": "Schertz Distribution Ctr",
                "bos__City__c": "Schertz",
                "bos__State__c": "TX",
                "bos__Postal_Code__c": "78154",
            },
            "0011I00000NMUrPQAX": {
                "type": "Account",
                "record_type": "Customer",
                "Name": "Gulf Coast Foods",
            },
        },
    }

    def test_adapts_the_sample_field_by_field(self) -> None:
        adapted = TmsCBrokerOsAdapter().adapt("2026-07-06T11-00_sync.json", self.PAYLOAD)

        # Already UTC: "2026-07-06T11:00:00.000+0000" parses as-is.
        assert adapted.synced_at == datetime(2026, 7, 6, 11, 0, 0, tzinfo=UTC)

        assert len(adapted.loads) == 1
        load = adapted.loads[0]
        assert load.source_load_id == "a0jO900000YgsYJIAZ"
        assert load.load_number == "SHP6743062"  # Id and Name genuinely differ
        assert load.status is LoadStatus.ACTIVE  # "Ready to Book" -> ACTIVE
        assert load.equipment is Equipment.REEFER
        assert load.distance_miles == 197.4  # already miles, untouched
        assert load.customer_rate == 1720.00
        assert load.carrier_rate is None  # not yet booked
        assert load.source_customer_id == "0011I00000NMUrPQAX"
        assert load.source_carrier_id is None  # bos__Carrier__c null
        assert load.created_at == datetime(2026, 7, 6, 9, 40, 2, tzinfo=UTC)
        assert load.last_modified_at == datetime(2026, 7, 6, 9, 40, 2, tzinfo=UTC)

        # One line item, already lbs: weight_to_lbs rounds to one decimal.
        assert len(load.cargo) == 1
        item = load.cargo[0]
        assert item.commodity == "Packaged foods"
        assert item.weight_lbs == 14440.0
        assert item.pallet_count == 18.0
        assert load.weight_lbs == 14440.0  # sum of the (one) line item

        assert len(load.stops) == 2
        origin, dest = load.stops
        assert (origin.sequence, origin.is_pickup, origin.is_drop) == (1, True, False)
        assert origin.location.city == "Sugar Land"
        assert origin.location.zip == "77478"
        assert origin.location.name == "Sugar Land Cold Storage"
        assert not origin.location.is_geo_null
        assert origin.scheduled_date == date(2026, 7, 7)
        assert origin.actual_arrival is None
        assert origin.actual_departure is None  # BrokerOS never reports departure

        assert (dest.sequence, dest.is_pickup, dest.is_drop) == (2, False, True)
        assert dest.location.city == "Schertz"
        assert dest.location.zip == "78154"
        assert dest.scheduled_date == date(2026, 7, 8)

        assert load.origin is origin
        assert load.destination is dest

        assert len(adapted.customers) == 1
        assert adapted.customers[0].source_customer_id == "0011I00000NMUrPQAX"
        assert adapted.customers[0].name == "Gulf Coast Foods"
        assert adapted.carriers == ()  # carrier null: no Account to resolve


class TestTmsCStatus:
    """PRD section 5: TMS C's seven statuses, two collapsing to DELIVERED."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Quotes Requested", LoadStatus.PLANNED),
            ("Ready to Book", LoadStatus.ACTIVE),
            ("Booked", LoadStatus.COVERED),
            ("In Transit", LoadStatus.IN_TRANSIT),
            ("Delivered", LoadStatus.DELIVERED),
            ("Invoiced", LoadStatus.DELIVERED),
            ("Paid", LoadStatus.COMPLETED),
        ],
    )
    def test_every_documented_status(self, raw: str, expected: LoadStatus) -> None:
        assert status_from_c(raw) is expected

    def test_delivered_and_invoiced_collapse_together(self) -> None:
        assert status_from_c("Delivered") is status_from_c("Invoiced") is LoadStatus.DELIVERED

    def test_unrecognized_status_raises(self) -> None:
        with pytest.raises(AdapterError):
            status_from_c("Cancelled")


class TestTmsCEquipment:
    """A nullable picklist; null means unknown, not Dry Van (invariant 5)."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Dry Van", Equipment.DRY_VAN),
            ("Reefer", Equipment.REEFER),
            ("Flatbed", Equipment.FLATBED),
            (None, Equipment.UNKNOWN),
            ("", Equipment.UNKNOWN),
            ("   ", Equipment.UNKNOWN),
            ("Conestoga", Equipment.UNKNOWN),  # unrecognized picklist value
        ],
    )
    def test_every_case(self, raw: str | None, expected: Equipment) -> None:
        assert equipment_from_picklist(raw) is expected

    def test_null_does_not_become_dry_van(self) -> None:
        assert equipment_from_picklist(None) is not Equipment.DRY_VAN
        assert equipment_from_picklist(None) is Equipment.UNKNOWN


class TestTmsCPerLineItemWeight:
    """A kg item mixed among lbs items: units applied per line, then summed."""

    def test_weight_to_lbs_direct(self) -> None:
        assert weight_to_lbs(14440.0, "lbs") == 14440.0
        # 6800 kg x 2.20462 = 14991.416 -> rounded to one decimal.
        assert weight_to_lbs(6800.0, "kg") == 14991.4
        assert weight_to_lbs(9300.0, "lbs") == 9300.0

    def test_load_total_sums_the_mixed_units_correctly(self) -> None:
        payload = {
            "synced_at": "2026-07-06T11:00:00.000+0000",
            "records": [
                {
                    "Id": "MIXED-WEIGHT-1",
                    "bos__Load_Status__c": "In Transit",
                    "bos__Line_Items__r": [
                        {"bos__Commodity__c": "A", "bos__Weight__c": 14440.0, "bos__Weight_Units__c": "lbs"},
                        {"bos__Commodity__c": "B", "bos__Weight__c": 6800.0, "bos__Weight_Units__c": "kg"},
                        {"bos__Commodity__c": "C", "bos__Weight__c": 9300.0, "bos__Weight_Units__c": "lbs"},
                    ],
                }
            ],
            "referenced_records": {},
        }
        adapted = TmsCBrokerOsAdapter().adapt("2026-07-06T11-00_sync.json", payload)
        load = adapted.loads[0]

        weights = [item.weight_lbs for item in load.cargo]
        assert weights == [14440.0, 14991.4, 9300.0]
        # 14440.0 lbs + 14991.4 lbs (6800 kg x 2.20462, rounded) + 9300.0 lbs
        assert load.weight_lbs == 38731.4
        # Both halves: the total is exactly the sum of the (already-rounded)
        # parts the UI lists beside it -- rounding the total instead would let
        # a line-item list visibly fail to add up.
        assert load.weight_lbs == sum(weights)


class TestTmsCStops:
    """3+ stops, ordered by bos__Number__c; first pickup/last drop form the lane."""

    def test_three_stops_ordered_by_number_not_array_position(self) -> None:
        # The raw array is deliberately scrambled (middle, last, first) to prove
        # sorting reads bos__Number__c and does not trust input order.
        payload = {
            "synced_at": "2026-07-06T11:00:00.000+0000",
            "records": [
                {
                    "Id": "THREE-STOP-1",
                    "bos__Load_Status__c": "In Transit",
                    "bos__Stops__r": [
                        {  # a genuine middle stop: a partial drop, not the final one
                            "bos__Number__c": 2.0,
                            "bos__Is_Pickup__c": False,
                            "bos__Is_Dropoff__c": True,
                            "bos__Location__c": "LOC_KATY",
                            "bos__Scheduled_Date__c": "2026-07-09",
                            "bos__Arrival_Time__c": None,
                        },
                        {
                            "bos__Number__c": 3.0,
                            "bos__Is_Pickup__c": False,
                            "bos__Is_Dropoff__c": True,
                            "bos__Location__c": "LOC_SCHERTZ",
                            "bos__Scheduled_Date__c": "2026-07-10",
                            "bos__Arrival_Time__c": None,
                        },
                        {
                            "bos__Number__c": 1.0,
                            "bos__Is_Pickup__c": True,
                            "bos__Is_Dropoff__c": False,
                            "bos__Location__c": "LOC_SUGARLAND",
                            "bos__Scheduled_Date__c": "2026-07-08",
                            "bos__Arrival_Time__c": None,
                        },
                    ],
                }
            ],
            "referenced_records": {
                "LOC_SUGARLAND": {
                    "type": "Location",
                    "Name": "Sugar Land Cold Storage",
                    "bos__City__c": "Sugar Land",
                    "bos__State__c": "TX",
                    "bos__Postal_Code__c": "77478",
                },
                "LOC_KATY": {
                    "type": "Location",
                    "Name": "Katy Waypoint",
                    "bos__City__c": "Katy",
                    "bos__State__c": "TX",
                    "bos__Postal_Code__c": "77449",
                },
                "LOC_SCHERTZ": {
                    "type": "Location",
                    "Name": "Schertz Distribution Ctr",
                    "bos__City__c": "Schertz",
                    "bos__State__c": "TX",
                    "bos__Postal_Code__c": "78154",
                },
            },
        }
        adapted = TmsCBrokerOsAdapter().adapt("2026-07-06T11-00_sync.json", payload)
        load = adapted.loads[0]

        assert len(load.stops) == 3
        assert [s.sequence for s in load.stops] == [1, 2, 3]  # renumbered 1..n
        first, middle, last = load.stops
        assert first.location.city == "Sugar Land"
        assert middle.location.city == "Katy"
        assert last.location.city == "Schertz"

        # First pickup / last drop form the lane...
        assert load.origin is first
        assert load.destination is last
        # ...the middle stop is retained, but is neither end of it.
        assert middle in load.stops
        assert middle in load.intermediate_stops
        assert middle is not load.origin
        assert middle is not load.destination


class TestTmsCDefensiveParsing:
    def test_missing_optional_fields_degrade_rather_than_raise(self) -> None:
        payload = {
            "synced_at": "2026-07-06T11:00:00.000+0000",
            "records": [{"Id": "MINIMAL-1", "bos__Load_Status__c": "Quotes Requested"}],
            "referenced_records": {},
        }
        adapted = TmsCBrokerOsAdapter().adapt("2026-07-06T11-00_sync.json", payload)

        load = adapted.loads[0]
        assert load.load_number is None  # no "Name"
        assert load.equipment is Equipment.UNKNOWN  # no "bos__Equipment_Type__c"
        assert load.distance_miles is None
        assert load.customer_rate is None
        assert load.carrier_rate is None
        assert load.source_carrier_id is None
        assert load.source_customer_id is None
        assert load.created_at is None
        assert load.last_modified_at is None
        assert load.stops == ()
        assert load.cargo == ()
        assert load.weight_lbs is None  # no weighed line items: None, not 0.0
        assert adapted.carriers == ()
        assert adapted.customers == ()

    def test_geo_unmatched_location_keeps_its_raw_fields(self) -> None:
        """A resolved address the geo table has never heard of: raw fields kept."""
        payload = {
            "synced_at": "2026-07-06T11:00:00.000+0000",
            "records": [
                {
                    "Id": "GEO-NULL-1",
                    "bos__Load_Status__c": "In Transit",
                    "bos__Stops__r": [
                        {
                            "bos__Number__c": 1.0,
                            "bos__Is_Pickup__c": True,
                            "bos__Is_Dropoff__c": False,
                            "bos__Location__c": "LOC_UNKNOWN_CITY",
                            "bos__Scheduled_Date__c": "2026-07-07",
                        }
                    ],
                }
            ],
            "referenced_records": {
                "LOC_UNKNOWN_CITY": {
                    "type": "Location",
                    "Name": "Nowheresville Yard",
                    "bos__City__c": "Nowheresville",
                    "bos__State__c": "TX",
                    "bos__Postal_Code__c": "99999",  # not in the geo table
                }
            },
        }
        adapted = TmsCBrokerOsAdapter().adapt("2026-07-06T11-00_sync.json", payload)

        stop = adapted.loads[0].stops[0]
        assert stop.location.place is None  # geo-null
        assert stop.location.is_geo_null
        # ...but the raw address is preserved so the stop still renders.
        assert stop.location.city == "Nowheresville"
        assert stop.location.state == "TX"
        assert stop.location.zip == "99999"
        assert adapted.loads != ()  # the load itself is not dropped

    def test_dangling_location_reference_is_blank_not_preserved(self) -> None:
        """Distinct from the above: an *unresolvable reference* has nothing to
        preserve, since the export never told us a city/state/zip at all."""
        payload = {
            "synced_at": "2026-07-06T11:00:00.000+0000",
            "records": [
                {
                    "Id": "DANGLING-REF-1",
                    "bos__Load_Status__c": "In Transit",
                    "bos__Stops__r": [
                        {
                            "bos__Number__c": 1.0,
                            "bos__Is_Pickup__c": True,
                            "bos__Is_Dropoff__c": False,
                            "bos__Location__c": "DOES_NOT_EXIST",
                            "bos__Scheduled_Date__c": "2026-07-07",
                        }
                    ],
                }
            ],
            "referenced_records": {},  # the id resolves to nothing
        }
        adapted = TmsCBrokerOsAdapter().adapt("2026-07-06T11-00_sync.json", payload)

        stop = adapted.loads[0].stops[0]
        assert stop.location.place is None
        assert stop.location.city is None
        assert stop.location.state is None
        assert stop.location.zip is None


# ===========================================================================
# D7 / D16 -- on-time, day-granular, compared in Central
# ===========================================================================


class TestDeliveredOnTime:
    def test_utc_arrival_after_19_00_central_reads_on_time_not_late(self) -> None:
        """DECISIONS.md D16's marquee case (mirrors real load SHP6700394):
        arrival 2026-07-07T04:22:00Z against a scheduled 2026-07-06.

        04:22 UTC - 5h (CDT) = 2026-07-06 23:22 Central -> same day as the
        schedule -> on time. A naive UTC-date comparison would read the arrival
        date as 07-07, a day *after* the 07-06 schedule, and call it late.
        """
        drop = Stop(
            sequence=2,
            is_pickup=False,
            is_drop=True,
            location=_geo_null(),
            scheduled_date=date(2026, 7, 6),
            actual_arrival=datetime(2026, 7, 7, 4, 22, 0, tzinfo=UTC),
        )
        load = _stub_load((Stop(1, True, False, _geo_null(), date(2026, 7, 5)), drop))
        assert delivered_on_time(load) is True

    def test_arrival_a_full_central_day_late_is_false(self) -> None:
        # 15:00 UTC - 5h = 10:00 Central on 07-11, a day after the 07-10 schedule.
        drop = Stop(
            sequence=2,
            is_pickup=False,
            is_drop=True,
            location=_geo_null(),
            scheduled_date=date(2026, 7, 10),
            actual_arrival=datetime(2026, 7, 11, 15, 0, 0, tzinfo=UTC),
        )
        load = _stub_load((Stop(1, True, False, _geo_null(), date(2026, 7, 9)), drop))
        assert delivered_on_time(load) is False

    def test_arrival_same_central_day_as_scheduled_is_true(self) -> None:
        # 15:00 UTC - 5h = 10:00 Central on 07-10, same day as the schedule.
        drop = Stop(
            sequence=2,
            is_pickup=False,
            is_drop=True,
            location=_geo_null(),
            scheduled_date=date(2026, 7, 10),
            actual_arrival=datetime(2026, 7, 10, 15, 0, 0, tzinfo=UTC),
        )
        load = _stub_load((Stop(1, True, False, _geo_null(), date(2026, 7, 9)), drop))
        assert delivered_on_time(load) is True

    def test_none_when_there_is_no_last_drop(self) -> None:
        # A pickup-only stop list: destination is None, so the question has no
        # answer -- not False. A load in transit has not been "late".
        load = _stub_load((Stop(1, True, False, _geo_null(), date(2026, 7, 9)),))
        assert delivered_on_time(load) is None

    def test_none_when_there_is_no_scheduled_date(self) -> None:
        drop = Stop(
            sequence=2,
            is_pickup=False,
            is_drop=True,
            location=_geo_null(),
            scheduled_date=None,
            actual_arrival=datetime(2026, 7, 10, 15, 0, 0, tzinfo=UTC),
        )
        load = _stub_load((Stop(1, True, False, _geo_null(), date(2026, 7, 9)), drop))
        assert delivered_on_time(load) is None

    def test_none_when_there_is_no_actual_arrival(self) -> None:
        # Still in transit: not delivered, so not "late" -- None, never False.
        drop = Stop(
            sequence=2,
            is_pickup=False,
            is_drop=True,
            location=_geo_null(),
            scheduled_date=date(2026, 7, 10),
            actual_arrival=None,
            actual_departure=None,
        )
        load = _stub_load((Stop(1, True, False, _geo_null(), date(2026, 7, 9)), drop))
        assert delivered_on_time(load) is None
