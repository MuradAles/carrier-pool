"""Small, hand-built sync files for the ingestion suite (TASKS.md I9/I10).

`test_tenant_isolation.py` builds canonical objects and hands them straight to
:class:`BrokerRepository`; the ingestion suite needs something one layer further
out -- real bytes on disk, in each TMS's native shape, so :func:`ingest_all` and
:func:`ingest_sync_file` can be exercised end to end. These builders return
plain dicts shaped like the real fixtures under ``data/`` (cross-checked against
them directly), with sensible defaults so a test only has to override what it
cares about.

Every load defaults onto the same lane -- Dallas, TX 75201 (DFW / zip3 752) to
Houston, TX 77002 (HOU / zip3 770), dry van -- so a test that wants a shared
lane gets one for free, and a test that wants a different lane only overrides
``origin``/``dest``.
"""

from __future__ import annotations

import json
from pathlib import Path

__all__ = [
    "TMS_A_DIR",
    "TMS_B_DIR",
    "TMS_C_DIR",
    "write_file",
    "tms_a_envelope",
    "tms_a_load",
    "tms_b_envelope",
    "tms_b_load",
    "tms_b_carrier",
    "tms_b_rate",
    "tms_c_envelope",
    "tms_c_record",
    "tms_c_location_ref",
    "tms_c_account_ref",
]

TMS_A_DIR = "tms_a_freightflow"
TMS_B_DIR = "tms_b_hauldesk"
TMS_C_DIR = "tms_c_brokeros"

_DEFAULT_ORIGIN = ("Dallas", "TX", "75201")
_DEFAULT_DEST = ("Houston", "TX", "77002")


def write_file(data_root: Path, tms_dir: str, filename: str, payload: dict) -> Path:
    """Write one payload as ``{data_root}/{tms_dir}/{filename}``, creating dirs."""
    directory = data_root / tms_dir
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# TMS A -- FreightFlow
# ---------------------------------------------------------------------------


def tms_a_load(
    shipment_id: int,
    *,
    status: str = "Completed",
    mileage: float = 271.0,
    total_sell: float | None = 950.0,
    total_buy: float | None = 700.0,
    customer: tuple[int, str] | None = (880001, "Lone Star Beverages"),
    carrier: tuple[int, str, str, str, str] | None = (
        700001,
        "Bluebonnet Freight Systems",
        "111111",
        "2222222",
        "+18005550100",
    ),
    equipment: str = "53 ft Van | Dry",
    weight: float = 40000.0,
    origin: tuple[str, str, str] = _DEFAULT_ORIGIN,
    dest: tuple[str, str, str] = _DEFAULT_DEST,
    pickup_date: str = "2026-07-06",
    delivery_date: str = "2026-07-07",
    created: str = "2026-07-04T08:00:00-05:00",
    modified: str = "2026-07-07T15:00:00-05:00",
) -> dict:
    """One FreightFlow ``loads[]`` entry. Defaults to a COMPLETED, on-time load."""
    o_city, o_state, o_zip = origin
    d_city, d_state, d_zip = dest
    return {
        "shipmentId": shipment_id,
        "status": status,
        "mileage": mileage,
        "totalSell": total_sell,
        "totalBuy": total_buy,
        "customer": None if customer is None else {"customerId": customer[0], "name": customer[1]},
        "carrier": None
        if carrier is None
        else {
            "carrierMasterId": carrier[0],
            "name": carrier[1],
            "mcNumber": carrier[2],
            "dotNumber": carrier[3],
            "phoneNumber": carrier[4],
        },
        "equipment": equipment,
        "weightTotal": weight,
        "stops": [
            {
                "stopType": "First Pickup",
                "city": o_city,
                "state": o_state,
                "zipCode": o_zip,
                "estimatedReadyDateTime": f"{pickup_date}T07:00:00-05:00",
                "estimatedCloseDateTime": f"{pickup_date}T16:00:00-05:00",
                "actualDepartureDateTime": f"{pickup_date}T09:00:00-05:00",
            },
            {
                "stopType": "Last Drop",
                "city": d_city,
                "state": d_state,
                "zipCode": d_zip,
                "estimatedReadyDateTime": f"{delivery_date}T07:00:00-05:00",
                "estimatedCloseDateTime": f"{delivery_date}T16:00:00-05:00",
                "actualDepartureDateTime": f"{delivery_date}T14:00:00-05:00",
            },
        ],
        "createdDate": created,
        "lastModifiedDate": modified,
    }


def tms_a_envelope(synced_at: str, loads: list[dict]) -> dict:
    return {"syncedAt": synced_at, "loads": loads}


# ---------------------------------------------------------------------------
# TMS B -- HaulDesk
# ---------------------------------------------------------------------------


def tms_b_carrier(
    carrier_id: int,
    *,
    name: str,
    mc_no: str,
    dot_no: str,
    home_city: str = "Houston",
    home_state: str = "TX",
    phone: str = "(713) 555-0100",
) -> dict:
    return {
        "carrier_id": carrier_id,
        "carrier_name": name,
        "mc_no": mc_no,
        "dot_no": dot_no,
        "home_city": home_city,
        "home_state": home_state,
        "phone": phone,
    }


def tms_b_load(
    load_num: str,
    *,
    status_code: int = 90,
    customer_code: str = "C-0001",
    customer_name: str = "Brazos Steel Works",
    carrier_ref: int | None = 800001,
    equip: str = "V",
    weight_kg: float = 18000.0,
    dist_km: float = 436.0,
    origin: tuple[str, str, str] = _DEFAULT_ORIGIN,
    dest: tuple[str, str, str] = _DEFAULT_DEST,
    pickup_date: str = "2026-07-06",
    delivery_date: str = "2026-07-07",
    pu_departed_at: str | None = "2026-07-06 09:00:00",
    del_arrived_at: str | None = "2026-07-07 14:00:00",
    entered_at: str = "2026-07-04 08:00:00",
    updated_at: str = "2026-07-07 15:00:00",
) -> dict:
    """One HaulDesk ``loads[]`` row. Defaults to a COMPLETED, on-time load."""
    o_city, o_state, o_zip = origin
    d_city, d_state, d_zip = dest
    return {
        "load_num": load_num,
        "status_code": status_code,
        "customer_code": customer_code,
        "customer_name": customer_name,
        "carrier_ref": carrier_ref,
        "equip": equip,
        "weight_kg": weight_kg,
        "dist_km": dist_km,
        "pu_city": o_city,
        "pu_state": o_state,
        "pu_zip": o_zip,
        "pu_date": pickup_date,
        "pu_departed_at": pu_departed_at,
        "del_city": d_city,
        "del_state": d_state,
        "del_zip": d_zip,
        "del_date": delivery_date,
        "del_arrived_at": del_arrived_at,
        "entered_at": entered_at,
        "updated_at": updated_at,
    }


def tms_b_rate(
    rate_id: int,
    load_num: str,
    side: str,
    code: str,
    amount_usd: float,
    *,
    created_at: str = "2026-07-06 00:00:00",
) -> dict:
    return {
        "rate_id": rate_id,
        "load_num": load_num,
        "side": side,
        "code": code,
        "amount_usd": amount_usd,
        "created_at": created_at,
    }


def tms_b_envelope(
    synced_at: str, *, loads: list[dict] = (), carriers: list[dict] = (), rates: list[dict] = ()
) -> dict:
    return {
        "synced_at": synced_at,
        "loads": list(loads),
        "carriers": list(carriers),
        "rates": list(rates),
    }


# ---------------------------------------------------------------------------
# TMS C -- BrokerOS
# ---------------------------------------------------------------------------


def tms_c_location_ref(city: str, state: str, zip_code: str, name: str = "Facility") -> dict:
    return {
        "type": "Location",
        "Name": name,
        "bos__City__c": city,
        "bos__State__c": state,
        "bos__Postal_Code__c": zip_code,
    }


def tms_c_account_ref(
    name: str, *, role: str, mc_number: str | None = None, dot_number: str | None = None
) -> dict:
    """A referenced ``Account`` -- ``role`` is ``"Customer"`` or ``"Carrier"``."""
    record: dict = {"type": "Account", "record_type": role, "Name": name}
    if role == "Carrier":
        record["bos__MC_Number__c"] = mc_number
        record["bos__DOT_Number__c"] = dot_number
    return record


def tms_c_record(
    record_id: str,
    name: str,
    *,
    status: str = "Paid",
    distance_miles: float = 271.0,
    customer_ref: str | None = "cust-1",
    carrier_ref: str | None = "carr-1",
    equipment: str = "Dry Van",
    customer_rate: float | None = 950.0,
    carrier_rate: float | None = 700.0,
    origin_loc_ref: str = "loc-origin",
    dest_loc_ref: str = "loc-dest",
    pickup_date: str = "2026-07-06",
    delivery_date: str = "2026-07-07",
    delivery_arrival: str | None = "2026-07-07T19:00:00.000+0000",
    weight_lbs: float = 40000.0,
    commodity: str = "General freight",
    created: str = "2026-07-04T13:00:00.000+0000",
    modified: str = "2026-07-07T20:00:00.000+0000",
) -> dict:
    """One BrokerOS ``records[]`` entry. ``delivery_arrival`` at 19:00 UTC is
    14:00 Central on the same calendar day as ``delivery_date`` -- on time
    under D16 without the caller having to do the zone arithmetic."""
    return {
        "Id": record_id,
        "Name": name,
        "bos__Load_Status__c": status,
        "bos__Distance_Miles__c": distance_miles,
        "bos__Customer__c": customer_ref,
        "bos__Carrier__c": carrier_ref,
        "bos__Equipment_Type__c": equipment,
        "bos__Customer_Rate__c": customer_rate,
        "bos__Carrier_Rate__c": carrier_rate,
        "bos__Stops__r": [
            {
                "bos__Number__c": 1.0,
                "bos__Is_Pickup__c": True,
                "bos__Is_Dropoff__c": False,
                "bos__Location__c": origin_loc_ref,
                "bos__Scheduled_Date__c": pickup_date,
                "bos__Arrival_Time__c": None,
            },
            {
                "bos__Number__c": 2.0,
                "bos__Is_Pickup__c": False,
                "bos__Is_Dropoff__c": True,
                "bos__Location__c": dest_loc_ref,
                "bos__Scheduled_Date__c": delivery_date,
                "bos__Arrival_Time__c": delivery_arrival,
            },
        ],
        "bos__Line_Items__r": [
            {
                "bos__Commodity__c": commodity,
                "bos__Weight__c": weight_lbs,
                "bos__Weight_Units__c": "lbs",
                "bos__Pallet_Count__c": 10.0,
            }
        ],
        "CreatedDate": created,
        "LastModifiedDate": modified,
    }


def tms_c_envelope(synced_at: str, records: list[dict], referenced_records: dict) -> dict:
    return {"synced_at": synced_at, "records": records, "referenced_records": referenced_records}
