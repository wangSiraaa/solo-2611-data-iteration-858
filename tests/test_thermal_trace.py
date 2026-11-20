"""Thermal closure + physical traceability tests.

Covers the task examples verbatim, wiring through the normal reader entry
points, correction/version behaviour, fail-closed non-finite handling, carousel
wrap-around, same-instant exchange, multi-level closure, replay warnings and
first-physical-mismatch reporting, plus database reopen.
"""

from __future__ import annotations

import pytest

from obsdelivery.errors import (
    PhysicalTraceConflict,
    ReconciliationConflict,
    StationLogConflict,
    ThermalCatalogConflict,
    ThermalProductConflict,
    ThermalSegmentRejected,
    ValidationError,
)
from obsdelivery.physical import PhysicalService
from obsdelivery.public import (
    public_reconciliation,
    public_thermal_product,
    verify_public_reconciliation,
    verify_public_thermal_product,
)
from obsdelivery.storage import Store
from obsdelivery.thermal import ThermalService

# binary32 originals
B0 = "00000000"  # 0
B1 = "3f800000"  # 1
B2 = "40000000"  # 2
B3 = "40400000"  # 3
B4 = "40800000"  # 4
B5 = "40a00000"  # 5
NAN = "7fc00001"


@pytest.fixture()
def env():
    store = Store(":memory:")
    thermal = ThermalService(store)
    physical = PhysicalService(store, thermal)
    yield store, thermal, physical
    store.close()


def l1_catalog(thermal):
    thermal.publish_catalog("c1", [{"name": "L1", "low": "1", "high": "3"}])


def base_readings():
    return [
        {"tick": 0, "at": "t0", "bits": B0},
        {"tick": 1, "at": "t1", "bits": B4},
        {"tick": 2, "at": "t2", "bits": B1},
        {"tick": 3, "at": "t3", "bits": B4},
        {"tick": 4, "at": "t4", "bits": B0},
    ]


# ---------------------------------------------------------------------------
# closure detection
# ---------------------------------------------------------------------------

def test_basic_closures_at_two_and_four(env):
    _, thermal, _ = env
    l1_catalog(thermal)
    product = thermal.publish_product("P", "c1", base_readings())
    closes = [(c["level"], c["close_tick"]) for c in product["closures"]]
    assert closes == [("L1", 2), ("L1", 4)]
    # Heat-up original is the high crossing (t=1); cool-down original is the
    # closing low crossing (t=2 and t=4 respectively).
    first, second = product["closures"]
    assert first["warm_tick"] == 1 and first["warm_bits"] == B4
    assert first["cool_tick"] == 2 and first["cool_bits"] == B1
    assert second["warm_tick"] == 3 and second["warm_bits"] == B4
    assert second["cool_tick"] == 4 and second["cool_bits"] == B0


def test_between_thresholds_keeps_state(env):
    _, thermal, _ = env
    l1_catalog(thermal)
    # high=3, low=1: reading 2 between them must neither open nor close.
    readings = [
        {"tick": 0, "at": "0", "bits": B0},
        {"tick": 1, "at": "1", "bits": B4},  # hot
        {"tick": 2, "at": "2", "bits": B2},  # stays hot
        {"tick": 3, "at": "3", "bits": B4},  # stays hot
        {"tick": 4, "at": "4", "bits": B1},  # closes
    ]
    product = thermal.publish_product("P", "c1", readings)
    assert [(c["level"], c["close_tick"]) for c in product["closures"]] == [("L1", 4)]
    assert product["closures"][0]["warm_tick"] == 1


def test_continuation_carries_open_state_across_segment(env):
    _, thermal, _ = env
    l1_catalog(thermal)
    readings = [
        {"tick": 0, "at": "0", "bits": B4},  # hot, never closed before break
        {"tick": 1, "at": "1", "bits": B1},  # would close only if carried
    ]
    product = thermal.publish_product(
        "P", "c1", readings, segments=[{"tick": 1, "marker": "continuation"}]
    )
    assert [(c["close_tick"]) for c in product["closures"]] == [1]


def test_reload_reset_discards_open_state(env):
    _, thermal, _ = env
    l1_catalog(thermal)
    readings = [
        {"tick": 0, "at": "0", "bits": B4},
        {"tick": 1, "at": "1", "bits": B1},
    ]
    product = thermal.publish_product(
        "P", "c1", readings, segments=[{"tick": 1, "marker": "reload_reset"}]
    )
    assert product["closures"] == []


def test_catalog_requires_low_strictly_below_high(env):
    _, thermal, _ = env
    with pytest.raises(ValidationError):
        thermal.publish_catalog("bad", [{"name": "L", "low": "3", "high": "3"}])
    with pytest.raises(ValidationError):
        thermal.publish_catalog("bad", [{"name": "L", "low": "4", "high": "3"}])
    with pytest.raises(ValidationError):
        thermal.publish_catalog("bad", [{"name": "L", "low": "1", "high": "3"}, {"name": "L", "low": "0", "high": "9"}])


def test_catalog_conflict(env):
    _, thermal, _ = env
    l1_catalog(thermal)
    # identical replay is fine
    thermal.publish_catalog("c1", [{"name": "L1", "low": "1", "high": "3"}])
    with pytest.raises(ThermalCatalogConflict):
        thermal.publish_catalog("c1", [{"name": "L1", "low": "1", "high": "4"}])


def test_duplicate_tick_rejected(env):
    _, thermal, _ = env
    l1_catalog(thermal)
    with pytest.raises(ValidationError):
        thermal.publish_product(
            "P",
            "c1",
            [{"tick": 0, "bits": B0}, {"tick": 0, "bits": B4}],
        )


# ---------------------------------------------------------------------------
# fail-closed non-finite & freezing/correction
# ---------------------------------------------------------------------------

def test_nonfinite_rejects_and_preserves_prior_product(env):
    _, thermal, _ = env
    l1_catalog(thermal)
    good = thermal.publish_product("P", "c1", base_readings())
    with pytest.raises(ThermalSegmentRejected) as exc:
        thermal.publish_product(
            "P",
            "c1",
            [{"tick": 0, "at": "0", "bits": B0}, {"tick": 1, "at": "1", "bits": NAN}],
        )
    assert exc.value.reasons[0]["reason"] == "nonfinite"
    # Prior product untouched and reads back byte-for-byte.
    again = thermal.get_product("P")
    assert again["content_digest"] == good["content_digest"]
    assert [(c["close_tick"]) for c in again["closures"]] == [2, 4]
    # Audit shows a rejected attempt then the published one.
    statuses = [a["status"] for a in thermal.attempts("P")]
    assert "rejected" in statuses


def test_corrected_product_conflicts_under_same_id(env):
    _, thermal, _ = env
    l1_catalog(thermal)
    thermal.publish_product("P", "c1", base_readings())
    corrected = [
        {"tick": 0, "at": "t0", "bits": B0},
        {"tick": 1, "at": "t1", "bits": B4},
        {"tick": 2, "at": "t2", "bits": B2},  # was B1
        {"tick": 3, "at": "t3", "bits": B4},
        {"tick": 4, "at": "t4", "bits": B0},
    ]
    with pytest.raises(ThermalProductConflict):
        thermal.publish_product("P", "c1", corrected)


def test_frozen_product_reads_back_after_new_correction(env):
    _, thermal, _ = env
    l1_catalog(thermal)
    old = thermal.publish_product("P-old", "c1", base_readings())
    # A later, separately-identified corrected product does not disturb the old.
    thermal.publish_product(
        "P-new",
        "c1",
        [
            {"tick": 0, "at": "t0", "bits": B0},
            {"tick": 1, "at": "t1", "bits": B4},
            {"tick": 2, "at": "t2", "bits": B2},
            {"tick": 3, "at": "t3", "bits": B4},
            {"tick": 4, "at": "t4", "bits": B0},
        ],
    )
    reread = thermal.get_product("P-old")
    assert reread["content_digest"] == old["content_digest"]
    assert reread["closures"][0]["cool_bits"] == B1  # original bit pattern


def test_public_thermal_product_self_verifies_and_detects_tamper(env):
    _, thermal, _ = env
    l1_catalog(thermal)
    thermal.publish_product("P", "c1", base_readings())
    doc = public_thermal_product(thermal, "P")
    assert verify_public_thermal_product(doc) == []
    tampered = {**doc, "closures": [dict(doc["closures"][0], close_tick=99), *doc["closures"][1:]]}
    assert verify_public_thermal_product(tampered)


# ---------------------------------------------------------------------------
# physical replay: the three task scenarios
# ---------------------------------------------------------------------------

def _trace_env(thermal, physical):
    l1_catalog(thermal)
    thermal.publish_product("P-base", "c1", base_readings())
    physical.publish_station_log("cool1", [{"position": 0, "levels": ["L1"], "cooling": 1}])
    physical.publish_carousel_config("xy", ["X", "Y"])


def test_example_base_claims_y(env):
    _, thermal, physical = env
    _trace_env(thermal, physical)
    rec = physical.reconcile(
        "R",
        "P-base",
        "cool1",
        "xy",
        ops=[{"tick": 5, "position": 1, "take": "Y", "replace": "Z"}],
        ticket={"box_serial": "Y", "claims": [{"tick": 4}]},
    )
    assert [(l["tick"], l["box_serial"]) for l in rec["loads"]] == [(2, "X"), (4, "Y")]
    assert [(r["tick"], r["position"], r["box_serial"]) for r in rec["removals"]] == [(5, 1, "Y")]
    assert rec["ticket_verdict"]["overall"] == "MATCH"
    assert rec["ticket_verdict"]["claims"][0]["status"] == "matched"


def test_example_corrected_observation_enters_x(env):
    _, thermal, physical = env
    l1_catalog(thermal)
    corrected = [
        {"tick": 0, "at": "t0", "bits": B0},
        {"tick": 1, "at": "t1", "bits": B4},
        {"tick": 2, "at": "t2", "bits": B2},  # corrected to 2
        {"tick": 3, "at": "t3", "bits": B4},
        {"tick": 4, "at": "t4", "bits": B0},
    ]
    thermal.publish_product("P-corr", "c1", corrected)
    physical.publish_station_log("cool1", [{"position": 0, "levels": ["L1"], "cooling": 1}])
    physical.publish_carousel_config("xy", ["X", "Y"])
    rec = physical.reconcile(
        "R",
        "P-corr",
        "cool1",
        "xy",
        ops=[{"tick": 5, "position": 1, "take": "Y", "replace": "Z"}],
        ticket={"box_serial": "Y", "claims": [{"tick": 4}]},
    )
    assert [(l["tick"], l["box_serial"]) for l in rec["loads"]] == [(4, "X")]
    claim = rec["ticket_verdict"]["claims"][0]
    assert claim["status"] == "wrong_box" and claim["actual_box"] == "X"


def test_example_cooling_drop_is_distinct_reason(env):
    _, thermal, physical = env
    l1_catalog(thermal)
    thermal.publish_product("P-base", "c1", base_readings())
    physical.publish_station_log("cool3", [{"position": 0, "levels": ["L1"], "cooling": 3}])
    physical.publish_carousel_config("xy", ["X", "Y"])
    rec = physical.reconcile(
        "R",
        "P-base",
        "cool3",
        "xy",
        ops=[{"tick": 5, "position": 1, "take": "Y", "replace": "Z"}],
        ticket={"box_serial": "Y", "claims": [{"tick": 4}]},
    )
    assert [(l["tick"], l["box_serial"]) for l in rec["loads"]] == [(2, "X")]
    assert [(s["tick"], s["level"]) for s in rec["skips"]] == [(4, "L1")]
    assert rec["skips"][0]["cooling_until"] == 5
    claim = rec["ticket_verdict"]["claims"][0]
    assert claim["status"] == "skipped_due_to_cooling"
    # Crucially, the reason differs from the "wrong_box" correction case.
    assert claim["status"] != "wrong_box"


def test_cooling_deadline_allows_sampling(env):
    _, thermal, physical = env
    l1_catalog(thermal)
    # closures at t=2 and t=4; cooling duration 2 -> window [2,4) so t=4 is
    # exactly at the deadline and may sample again.
    thermal.publish_product("P", "c1", base_readings())
    physical.publish_station_log("cool2", [{"position": 0, "levels": ["L1"], "cooling": 2}])
    physical.publish_carousel_config("xy", ["X", "Y"])
    rec = physical.reconcile("R", "P", "cool2", "xy")
    assert [(l["tick"], l["box_serial"]) for l in rec["loads"]] == [(2, "X"), (4, "Y")]
    assert rec["skips"] == []


# ---------------------------------------------------------------------------
# combination: wrap-around, same-tick exchange, co-closing levels
# ---------------------------------------------------------------------------

def combo_product(thermal):
    thermal.publish_catalog(
        "cab", [{"name": "A", "low": "1", "high": "2"}, {"name": "B", "low": "1", "high": "4"}]
    )
    readings = [
        {"tick": 0, "at": "c0", "bits": B0},
        {"tick": 1, "at": "c1", "bits": B5},  # A and B hot
        {"tick": 2, "at": "c2", "bits": B1},  # A and B close together
        {"tick": 3, "at": "c3", "bits": B4},  # A hot (B: 4>=4 hot)
        {"tick": 4, "at": "c4", "bits": B1},  # A closes
        {"tick": 5, "at": "c5", "bits": B4},  # A hot
        {"tick": 6, "at": "c6", "bits": B1},  # A closes
    ]
    return thermal.publish_product("P-combo", "cab", readings)


def test_multi_level_same_instant_single_trigger(env):
    _, thermal, physical = env
    product = combo_product(thermal)
    closes = [(c["level"], c["close_tick"]) for c in product["closures"]]
    assert closes == [
        ("A", 2), ("B", 2),
        ("A", 4), ("B", 4),
        ("A", 6), ("B", 6),
    ]
    physical.publish_station_log("st", [{"position": 0, "levels": ["A", "B"], "cooling": 0}])
    physical.publish_carousel_config("xyz", ["X", "Y", "Z"])
    rec = physical.reconcile("R", "P-combo", "st", "xyz")
    # Three loads (one per trigger instant), not six (one per closure).
    assert len(rec["loads"]) == 3
    assert [l["levels"] for l in rec["loads"]] == [["A", "B"], ["A", "B"], ["A", "B"]]


def test_wrap_around_with_same_tick_exchange(env):
    _, thermal, physical = env
    combo_product(thermal)
    physical.publish_station_log("st", [{"position": 0, "levels": ["A", "B"], "cooling": 0}])
    # Two slots, three loads: t2->X, t4->Y, t6 wraps to slot 0.
    physical.publish_carousel_config("xy", ["X", "Y"])
    rec = physical.reconcile(
        "R",
        "P-combo",
        "st",
        "xy",
        ops=[{"tick": 6, "position": 0, "take": "X", "replace": "W"}],
        ticket={"box_serial": "W", "claims": [{"tick": 6, "level": "A"}]},
    )
    assert [(l["tick"], l["box_serial"], l["box_slot"]) for l in rec["loads"]] == [
        (2, "X", 0),
        (4, "Y", 1),
        (6, "W", 0),  # wrapped; exchange at t=6 happens before the t=6 trigger
    ]
    assert [(r["tick"], r["position"], r["box_serial"]) for r in rec["removals"]] == [(6, 0, "X")]
    assert rec["ticket_verdict"]["overall"] == "MATCH"


# ---------------------------------------------------------------------------
# replay safety: no double load, first mismatch, preserve prior delivery
# ---------------------------------------------------------------------------

def test_box_cannot_be_loaded_twice_without_exchange(env):
    _, thermal, physical = env
    combo_product(thermal)
    physical.publish_station_log("st", [{"position": 0, "levels": ["A", "B"], "cooling": 0}])
    physical.publish_carousel_config("xy", ["X", "Y"])
    with pytest.raises(PhysicalTraceConflict) as exc:
        physical.reconcile("R", "P-combo", "st", "xy")
    assert exc.value.mismatch["reason"] == "box_loaded_twice"
    assert exc.value.mismatch["tick"] == 6
    assert exc.value.mismatch["box_serial"] == "X"


def test_take_wrong_serial_flags_first_mismatch_and_keeps_prior(env):
    _, thermal, physical = env
    _trace_env(thermal, physical)
    good = physical.reconcile("R-good", "P-base", "cool1", "xy")
    with pytest.raises(PhysicalTraceConflict) as exc:
        physical.reconcile(
            "R-bad",
            "P-base",
            "cool1",
            "xy",
            ops=[{"tick": 5, "position": 1, "take": "X", "replace": "Z"}],
        )
    assert exc.value.mismatch["reason"] == "take_serial_mismatch"
    assert exc.value.mismatch["claimed_take"] == "X"
    assert exc.value.mismatch["actual_serial"] == "Y"
    # Prior delivery stays readable and unchanged.
    assert physical.get_reconciliation("R-good")["content_digest"] == good["content_digest"]
    # The conflicting attempt is audited but consumes no delivery identity.
    assert [a["status"] for a in physical.attempts("R-bad")] == ["conflict"]


def test_replacement_serial_must_be_unused(env):
    _, thermal, physical = env
    _trace_env(thermal, physical)
    with pytest.raises(PhysicalTraceConflict) as exc:
        physical.reconcile(
            "R",
            "P-base",
            "cool1",
            "xy",
            ops=[{"tick": 5, "position": 1, "take": "Y", "replace": "X"}],  # X already used
        )
    assert exc.value.mismatch["reason"] == "replacement_serial_already_used"


def test_reconciliation_replay_and_conflict(env):
    _, thermal, physical = env
    _trace_env(thermal, physical)
    first = physical.reconcile("R", "P-base", "cool1", "xy")
    again = physical.reconcile("R", "P-base", "cool1", "xy")
    assert again["created"] is False
    assert again["content_digest"] == first["content_digest"]
    with pytest.raises(ReconciliationConflict):
        physical.reconcile("R", "P-base", "cool1", "xy", ticket={"box_serial": "Y", "claims": [{"tick": 4}]})


def test_station_log_conflict_and_public_verify(env):
    _, thermal, physical = env
    _trace_env(thermal, physical)
    physical.publish_station_log("cool1", [{"position": 0, "levels": ["L1"], "cooling": 1}])
    with pytest.raises(StationLogConflict):
        physical.publish_station_log("cool1", [{"position": 0, "levels": ["L1"], "cooling": 2}])
    physical.reconcile(
        "R", "P-base", "cool1", "xy", ticket={"box_serial": "Y", "claims": [{"tick": 4}]}
    )
    doc = public_reconciliation(physical, "R")
    assert verify_public_reconciliation(doc) == []
    # Tampering with the load a claim points at is caught independently: the
    # verdict's matched box no longer agrees with the load table.
    tampered = {**doc, "loads": [doc["loads"][0], dict(doc["loads"][1], box_serial="Q")]}
    assert verify_public_reconciliation(tampered)


# ---------------------------------------------------------------------------
# database reopen: normal reader entry points
# ---------------------------------------------------------------------------

def test_correction_creates_new_version_old_still_queryable(env):
    """A correction + station-log change yields a new reconciliation under a new
    id; the old slip's original conclusion stays readable by its old id."""
    _, thermal, physical = env
    _trace_env(thermal, physical)
    old = physical.reconcile(
        "R-old",
        "P-base",
        "cool1",
        "xy",
        ops=[{"tick": 5, "position": 1, "take": "Y", "replace": "Z"}],
        ticket={"box_serial": "Y", "claims": [{"tick": 4}]},
    )
    assert old["ticket_verdict"]["overall"] == "MATCH"

    # Later the station cooling is revised to 3; the original product is frozen,
    # so a fresh reconciliation under a new id gives a different conclusion.
    physical.publish_station_log("cool3", [{"position": 0, "levels": ["L1"], "cooling": 3}])
    new = physical.reconcile(
        "R-new",
        "P-base",
        "cool3",
        "xy",
        ops=[{"tick": 5, "position": 1, "take": "Y", "replace": "Z"}],
        ticket={"box_serial": "Y", "claims": [{"tick": 4}]},
    )
    assert new["ticket_verdict"]["claims"][0]["status"] == "skipped_due_to_cooling"
    assert new["content_digest"] != old["content_digest"]

    # The old slip's conclusion is still available under its original id.
    reread = physical.get_reconciliation("R-old")
    assert reread["ticket_verdict"]["overall"] == "MATCH"
    assert reread["ticket_verdict"]["claims"][0]["status"] == "matched"
    assert reread["content_digest"] == old["content_digest"]


def test_reopen_database_reads_through_normal_entry_points(tmp_path):
    path = str(tmp_path / "trace.sqlite")
    store = Store(path)
    thermal = ThermalService(store)
    physical = PhysicalService(store, thermal)
    _trace_env(thermal, physical)
    base_rec = physical.reconcile(
        "R-base",
        "P-base",
        "cool1",
        "xy",
        ops=[{"tick": 5, "position": 1, "take": "Y", "replace": "Z"}],
        ticket={"box_serial": "Y", "claims": [{"tick": 4}]},
    )
    product = thermal.get_product("P-base")
    store.close()

    store2 = Store(path)
    thermal2 = ThermalService(store2)
    physical2 = PhysicalService(store2, thermal2)
    # Products and reconciliations read back identically and verify their own
    # digests via the normal readers.
    assert thermal2.get_product("P-base")["content_digest"] == product["content_digest"]
    reread = physical2.get_reconciliation("R-base")
    assert reread["content_digest"] == base_rec["content_digest"]
    assert reread["ticket_verdict"]["overall"] == "MATCH"
    assert [(l["tick"], l["box_serial"]) for l in reread["loads"]] == [(2, "X"), (4, "Y")]
    assert verify_public_thermal_product(public_thermal_product(thermal2, "P-base")) == []
    assert verify_public_reconciliation(public_reconciliation(physical2, "R-base")) == []
    store2.close()


def test_tampered_stored_load_is_detected_on_readback(env):
    store, thermal, physical = env
    _trace_env(thermal, physical)
    physical.reconcile("R", "P-base", "cool1", "xy")
    store.conn.execute(
        "UPDATE reconciliation_loads SET box_serial='Q' WHERE reconciliation_id='R' AND ordinal=0"
    )
    from obsdelivery.errors import IntegrityError

    with pytest.raises(IntegrityError):
        physical.get_reconciliation("R")
