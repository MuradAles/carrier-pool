"""Does `validate_data.py` actually bite?

The bug this suite exists to prevent is not a bad fixture. It is a *validator
that cannot fail*. `validate_data.py` used to rebuild the plan from the seed and
then assert the plan against itself, so eight of ten hand-injected corruptions
passed validation -- including a null `bos__Equipment_Type__c` flipped to
"Dry Van" (invariant 5's only fixture) and the rate-only `ADJUSTMENT` row
(CLAUDE.md's named TMS B trap) deleted outright. The reconciliation loop in
`Validator.check_shapes` fixed that by comparing the emitted bytes to the plan
appearance by appearance.

But reconciliation code that is never shown to fail is unfalsifiable in exactly
the same way. So: each test below corrupts one thing in a throwaway copy of
`data/` and asserts the validator rejects it *for the stated reason*. A nonzero
exit is not enough -- a validator broken for an unrelated cause would satisfy
that. `test_pristine_copy_passes` is the positive control: without it, a
validator that failed on everything would look perfectly healthy here.

One test per class of divergence, so a failure names what stopped being caught.

What the reconciliation checks
------------------------------
Per (load id, slot) appearance, in both directions -- the emitted set of pairs
must equal the plan's, which is what catches a deleted load or record:

  miles, carrier rate, customer rate, weight, equipment string, status,
  createdDate / lastModifiedDate, customer id + name, carrier id + MC number +
  name, the ordered stop zip sequence, and the actual pickup/delivery
  timestamps.

Per-TMS normalization is applied before comparing: TMS B `dist_km x 0.621371`
and `weight_kg x 2.20462`, TMS B carrier rate as the running sum of `pay` line
items across all files up to and including that slot (negatives included), and
TMS C per-line-item weight units summed. TMS A and C money is taken as given.

What is NOT value-reconciled
----------------------------
Shape-checked but not compared to the plan. Corrupting any of these will pass
validation, deliberately, and that is worth knowing before trusting a green run:

  - TMS A `estimatedReadyDateTime` / `estimatedCloseDateTime` (appointment
    windows, derived from the actuals rather than carried in the plan)
  - TMS C `bos__Arrival_Time__c` -- left uncoupled on purpose. Whether it is
    Central or UTC is an open normalization question with the user, and a check
    written against today's emission would have to be rewritten alongside the
    answer.
  - TMS C `bos__Pallet_Count__c`
  - `referenced_records` **Location** `Name` strings -- only the postal code is
    reconciled. The Customer and Carrier account `Name`s are reconciled.
  - TMS C stop flags beyond first-is-pickup / last-is-dropoff, i.e.
    `bos__Number__c` ordering is checked but individual
    `bos__Is_Pickup__c` / `bos__Is_Dropoff__c` values are not reconciled
  - TMS B `rate_id` sequencing
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from .conftest import gd

TMS_A, TMS_B, TMS_C = "tms_a_freightflow", "tms_b_hauldesk", "tms_c_brokeros"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _sync_files(tree: Path, tms: str) -> list[Path]:
    return sorted((tree / tms).glob("*_sync.json"))


def _load(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def _save(p: Path, doc: dict) -> None:
    p.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _validate(plans: list, tree: Path) -> tuple[bool, list[str]]:
    """Run the real validator in-process against ``tree``."""
    v = gd.Validator(tree, plans)
    return v.run(), list(v.errors)


def _assert_caught(plans: list, tree: Path, *needles: str) -> None:
    """The validator must reject the tree, and for the reason we injected."""
    ok, errors = _validate(plans, tree)
    assert not ok and errors, (
        "the validator PASSED a corrupted tree: this class of divergence is no "
        "longer detected"
    )
    blob = "\n".join(errors)
    missing = [n for n in needles if n not in blob]
    assert not missing, (
        f"the validator failed, but not for the injected reason -- missing "
        f"{missing}. It may be failing for an unrelated cause, which would make "
        f"this test pass by accident. Errors were:\n"
        + "\n".join(f"  - {e}" for e in errors)
    )


def _first_a_load(tree: Path, pred=lambda ld: True) -> tuple[Path, dict, dict]:
    for p in _sync_files(tree, TMS_A):
        doc = _load(p)
        for ld in doc["loads"]:
            if pred(ld):
                return p, doc, ld
    pytest.fail("no TMS A load matched the predicate")


def _first_b_load(tree: Path, pred=lambda ld: True) -> tuple[Path, dict, dict]:
    for p in _sync_files(tree, TMS_B):
        doc = _load(p)
        for ld in doc["loads"]:
            if pred(ld):
                return p, doc, ld
    pytest.fail("no TMS B load matched the predicate")


def _first_c_record(tree: Path, pred=lambda r: True) -> tuple[Path, dict, dict]:
    for p in _sync_files(tree, TMS_C):
        doc = _load(p)
        for rec in doc["records"]:
            if pred(rec):
                return p, doc, rec
    pytest.fail("no TMS C record matched the predicate")


# ---------------------------------------------------------------------------
# positive control
# ---------------------------------------------------------------------------


def test_pristine_copy_passes(plans, tree):
    """Control. If this fails, every test below passes for the wrong reason."""
    ok, errors = _validate(plans, tree)
    assert ok, "an untouched copy of data/ does not validate:\n" + "\n".join(
        f"  - {e}" for e in errors
    )


# ---------------------------------------------------------------------------
# TMS B -- unit normalization
# ---------------------------------------------------------------------------


def test_b_dist_km_unit_inverted(plans, tree):
    """`dist_km` multiplied by 0.621371, i.e. km recorded as if it were miles."""
    p, doc, ld = _first_b_load(tree)
    ld["dist_km"] = round(ld["dist_km"] * 0.621371, 1)
    _save(p, doc)
    _assert_caught(plans, tree, "dist_km x 0.621371 -> miles", ld["load_num"])


def test_b_weight_kg_unit_inverted(plans, tree):
    """`weight_kg` multiplied by 2.20462, i.e. lbs written into the kg column."""
    p, doc, ld = _first_b_load(tree)
    ld["weight_kg"] = round(ld["weight_kg"] * 2.20462, 1)
    _save(p, doc)
    _assert_caught(plans, tree, "weight_kg x 2.20462 -> lbs", ld["load_num"])


# ---------------------------------------------------------------------------
# TMS B -- append-only money
# ---------------------------------------------------------------------------


def test_b_rate_amount_restated(plans, tree):
    """One `pay` line item's amount changed. The running sum must not match."""
    for p in _sync_files(tree, TMS_B):
        doc = _load(p)
        if doc["rates"]:
            doc["rates"][0]["amount_usd"] = 9999.0
            _save(p, doc)
            break
    else:
        pytest.fail("no TMS B rate row found")
    _assert_caught(plans, tree, "running sum of `pay` rows through this file")


def test_b_adjustment_row_deleted(plans, tree):
    """A negative `ADJUSTMENT` row removed: the correction silently un-happens."""
    for p in _sync_files(tree, TMS_B):
        doc = _load(p)
        keep = [r for r in doc["rates"] if r["code"] != "ADJUSTMENT"]
        if len(keep) != len(doc["rates"]):
            doc["rates"] = keep
            _save(p, doc)
            break
    else:
        pytest.fail("no ADJUSTMENT row found anywhere in TMS B")
    _assert_caught(plans, tree, "running sum of `pay` rows through this file")


def test_b_rate_only_adjustment_row_deleted(plans, tree):
    """CLAUDE.md's named TMS B trap, deleted outright.

    The rate-only correction is the `ADJUSTMENT` row whose load is absent from
    the same file's `loads` array. Losing it is worse than losing the other one:
    it removes the only fixture proving the ingester must recompute money for a
    load the file never mentions, while TRACEABILITY.md keeps asserting the
    corrected total. Two independent checks must fire -- the running sum, and
    the assertion that such a row exists at all.
    """
    for p in _sync_files(tree, TMS_B):
        doc = _load(p)
        listed = {ld["load_num"] for ld in doc["loads"]}
        gone = [r for r in doc["rates"]
                if r["code"] == "ADJUSTMENT" and r["load_num"] not in listed]
        if gone:
            doc["rates"] = [r for r in doc["rates"] if r not in gone]
            _save(p, doc)
            break
    else:
        pytest.fail("no rate-only ADJUSTMENT row found: the trap is already gone")
    _assert_caught(
        plans, tree,
        "running sum of `pay` rows through this file",
        "the TMS B rate-only correction trap is not present in the data",
    )


def test_b_loads_array_row_deleted(plans, tree):
    """The row removed while its `rates` rows stay.

    `_shape_b` already objects here ("rate references unknown load"), so the
    assertion has to name the reconciliation's own message -- otherwise this
    test would pass on the pre-fix validator and prove nothing.
    """
    p, doc, ld = _first_b_load(tree)
    doc["loads"].remove(ld)
    _save(p, doc)
    _assert_caught(
        plans, tree,
        f"plan expects load_num {ld['load_num']} here and the file does not contain it",
    )


# ---------------------------------------------------------------------------
# TMS B -- carrier identity
# ---------------------------------------------------------------------------


def test_b_carrier_mc_number_swapped(plans, tree):
    """The `carriers` table's `mc_no` changed.

    This is the case that proves the MC check reads the emitted table rather
    than feeding the plan's own value back in as the observation. A tautological
    version of that check would pass this test.
    """
    for p in _sync_files(tree, TMS_B):
        doc = _load(p)
        if doc["carriers"]:
            doc["carriers"][0]["mc_no"] = "999999"
            _save(p, doc)
            break
    else:
        pytest.fail("no TMS B carrier row found")
    _assert_caught(plans, tree, "carrier MC number on disk is '999999'")


# ---------------------------------------------------------------------------
# TMS A
# ---------------------------------------------------------------------------


def test_a_total_buy_restated(plans, tree):
    """A's correction flavor, applied where no correction was planned."""
    p, doc, ld = _first_a_load(tree, lambda ld: ld["totalBuy"] is not None)
    ld["totalBuy"] = 12345.0
    _save(p, doc)
    _assert_caught(plans, tree, "totalBuy on disk is 12345.0", str(ld["shipmentId"]))


def test_a_mileage_restated(plans, tree):
    """Mileage no longer matches the city pair. Two checks must fire: the plan
    comparison, and the plan-independent one against the emitted stops."""
    p, doc, ld = _first_a_load(tree)
    ld["mileage"] = round(ld["mileage"] * 0.4, 1)
    _save(p, doc)
    _assert_caught(
        plans, tree,
        "mileage on disk is",
        "does not match the emitted stops",
    )


def test_a_load_deleted(plans, tree):
    """A whole load removed from a file: caught by set equality, not by any
    field comparison."""
    p, doc, ld = _first_a_load(tree)
    doc["loads"].remove(ld)
    _save(p, doc)
    _assert_caught(
        plans, tree,
        f"plan expects shipmentId {ld['shipmentId']} here and the file does not contain it",
    )


def test_a_status_moved_backwards(plans, tree):
    p, doc, ld = _first_a_load(tree, lambda ld: ld["status"] == "Completed")
    ld["status"] = "Dispatched"
    _save(p, doc)
    _assert_caught(plans, tree, "status on disk is 'Dispatched'", "'Completed'")


def test_a_carrier_object_swapped(plans, tree):
    """The load is credited to a different carrier -- which would move the
    carrier stats every recommendation is built from."""
    found = []
    for p in _sync_files(tree, TMS_A):
        doc = _load(p)
        for ld in doc["loads"]:
            if ld["carrier"] is not None:
                found.append((p, doc, ld))
    assert len(found) >= 2, "need two carrier-bearing TMS A loads"
    (p1, d1, l1), (_p2, _d2, l2) = found[0], found[-1]
    assert l1["carrier"]["carrierMasterId"] != l2["carrier"]["carrierMasterId"], (
        "picked the same carrier twice; the swap would be a no-op"
    )
    l1["carrier"] = dict(l2["carrier"])
    _save(p1, d1)
    _assert_caught(plans, tree, "carrier id on disk is", "carrier MC number on disk is")


# ---------------------------------------------------------------------------
# TMS C
# ---------------------------------------------------------------------------


def test_c_carrier_rate_silently_restated(plans, tree):
    """C's correction flavor: `bos__Carrier_Rate__c` changes with no marker."""
    p, doc, rec = _first_c_record(tree, lambda r: r["bos__Carrier_Rate__c"] is not None)
    rec["bos__Carrier_Rate__c"] = 99.0
    _save(p, doc)
    _assert_caught(plans, tree, "bos__Carrier_Rate__c on disk is 99.0", rec["Name"])


def test_c_carrier_rate_nudged_ten_percent(plans, tree):
    """The same field moved only 10%.

    A plausible drift rather than an obviously wrong number -- still inside
    every sanity band, so nothing but the reconciliation can see it.
    """
    p, doc, rec = _first_c_record(tree, lambda r: r["bos__Carrier_Rate__c"] is not None)
    before = rec["bos__Carrier_Rate__c"]
    rec["bos__Carrier_Rate__c"] = round(before * 1.10, 2)
    _save(p, doc)
    _assert_caught(plans, tree, "bos__Carrier_Rate__c on disk is", str(before), rec["Name"])


def test_c_null_equipment_becomes_dry_van(plans, tree):
    """Invariant 5's only fixture, destroyed.

    Every null `bos__Equipment_Type__c` set to "Dry Van". Before the
    reconciliation this passed: `check_scenarios`' `messy_null_equipment` test
    and `check_tier_rung_coverage`' UNKNOWN test both read the plan, so both
    stayed green while the data no longer contained a single null.
    """
    hits: list[str] = []
    for p in _sync_files(tree, TMS_C):
        doc = _load(p)
        touched = False
        for rec in doc["records"]:
            if rec["bos__Equipment_Type__c"] is None:
                rec["bos__Equipment_Type__c"] = "Dry Van"
                hits.append(rec["Name"])
                touched = True
        if touched:
            _save(p, doc)
    assert hits, "no null equipment in the fixtures: invariant 5 has no fixture at all"
    _assert_caught(
        plans, tree,
        "bos__Equipment_Type__c on disk is 'Dry Van' but the plan says None",
        *hits,
    )


def test_c_carrier_account_name_changed(plans, tree):
    """Scenario 6 rests on the same MC/DOT appearing under *different* names in
    two TMSs. If a name drifts, cross-TMS identity is silently testing something
    else, so the account `Name` is reconciled too."""
    for p in _sync_files(tree, TMS_C):
        doc = _load(p)
        hit = next((ref for ref in doc["referenced_records"].values()
                    if ref.get("record_type") == "Carrier"), None)
        if hit is not None:
            hit["Name"] = "Some Other Trucking LLC"
            _save(p, doc)
            break
    else:
        pytest.fail("no TMS C carrier account found")
    _assert_caught(plans, tree, "carrier name on disk is 'Some Other Trucking LLC'")


def test_b_customer_name_changed(plans, tree):
    """The customer id still resolves; only the name is wrong. Nothing
    structural objects."""
    p, doc, ld = _first_b_load(tree)
    ld["customer_name"] = "Nobody Industries"
    _save(p, doc)
    _assert_caught(plans, tree, "customer_name on disk is 'Nobody Industries'")


def test_c_record_deleted(plans, tree):
    p, doc, rec = _first_c_record(tree)
    doc["records"].remove(rec)
    _save(p, doc)
    _assert_caught(
        plans, tree,
        f"plan expects record Id {rec['Id']} here and the file does not contain it",
    )


def test_c_file_replaced_with_empty_envelope(plans, tree):
    """An empty sync is legitimate -- but not in a slot the plan fills. The
    envelope stays structurally valid, so only set equality can object."""
    for p in _sync_files(tree, TMS_C):
        doc = _load(p)
        if doc["records"]:
            ids = [r["Id"] for r in doc["records"]]
            doc["records"] = []
            doc["referenced_records"] = {}
            _save(p, doc)
            break
    else:
        pytest.fail("no non-empty TMS C file found")
    _assert_caught(plans, tree, *(f"plan expects record Id {i} here" for i in ids))


def test_c_kg_line_item_relabelled_lbs(plans, tree):
    """The one `kg` line item marked `lbs`.

    Both the per-item units and the summed weight must object; the units alone
    would let a same-magnitude edit slip through.
    """
    for p in _sync_files(tree, TMS_C):
        doc = _load(p)
        for rec in doc["records"]:
            hit = next((li for li in rec["bos__Line_Items__r"]
                        if li["bos__Weight_Units__c"] == "kg"), None)
            if hit is not None:
                hit["bos__Weight_Units__c"] = "lbs"
                _save(p, doc)
                _assert_caught(
                    plans, tree,
                    "bos__Weight_Units__c on disk is 'lbs' but the plan says 'kg'",
                    "sum of line items normalized to lbs",
                )
                return
    pytest.fail("no kg line item found: the per-line-item units trap is gone")


def test_c_stop_location_swapped(plans, tree):
    """The pickup pointed at the drop's location. The lane silently collapses to
    a zero-mile trip while every id still resolves."""
    p, doc, rec = _first_c_record(
        tree,
        lambda r: len({s["bos__Location__c"] for s in r["bos__Stops__r"]})
        == len(r["bos__Stops__r"]) >= 2,
    )
    rec["bos__Stops__r"][0]["bos__Location__c"] = rec["bos__Stops__r"][-1]["bos__Location__c"]
    _save(p, doc)
    _assert_caught(plans, tree, "stop zips on disk", "does not match the emitted stops")
