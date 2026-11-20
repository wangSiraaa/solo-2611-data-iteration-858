"""Guided demo for thermal closure + physical sample traceability.

Run::

    python3 -m obsdelivery.thermal_demo [--db /tmp/trace.sqlite] [--json]

It walks the exact examples from the task statement:

1. base: readings 0,4,1,4,0 close at t=2 and t=4; boxes X then Y; cooling=1;
   take Y at t=5 -- the slip's "t=4 sample" is genuinely in Y.
2. correct t=2's reading to 2 -> only t=4 closes, the sample enters X, so the
   claimed Y is empty (``wrong_box``).
3. instead freeze the original observation and change station cooling to 3 ->
   t=4 is dropped while cooling, so Y is empty again but for a *different*
   reason (``skipped_due_to_cooling``).
4. combination: rotate the carousel past its end, exchange a box in the same
   instant a trigger fires, and let two levels close together; then reopen the
   database and re-read everything through the normal reader entry points.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile

from .physical import PhysicalService
from .public import (
    public_reconciliation,
    public_thermal_product,
    verify_public_reconciliation,
    verify_public_thermal_product,
)
from .storage import Store
from .thermal import ThermalService

LEVEL_L1 = {"name": "L1", "low": "1", "high": "3"}
# binary32 originals: 0, 4, 1, 4, 0
BASE_READINGS = [
    {"tick": 0, "at": "00:00", "bits": "00000000"},
    {"tick": 1, "at": "00:01", "bits": "40800000"},  # 4
    {"tick": 2, "at": "00:02", "bits": "3f800000"},  # 1
    {"tick": 3, "at": "00:03", "bits": "40800000"},  # 4
    {"tick": 4, "at": "00:04", "bits": "00000000"},  # 0
]
CORRECTED_READINGS = [
    {"tick": 0, "at": "00:00", "bits": "00000000"},
    {"tick": 1, "at": "00:01", "bits": "40800000"},  # 4
    {"tick": 2, "at": "00:02", "bits": "40000000"},  # 2 (below high) -- corrected
    {"tick": 3, "at": "00:03", "bits": "40800000"},  # 4
    {"tick": 4, "at": "00:04", "bits": "00000000"},  # 0
]


def _closure_summary(product: dict) -> list[dict]:
    return [
        {
            "ordinal": closure["ordinal"],
            "level": closure["level"],
            "close_tick": closure["close_tick"],
            "warm_bits": closure["warm_bits"],
            "warm_tick": closure["warm_tick"],
            "cool_bits": closure["cool_bits"],
            "cool_tick": closure["cool_tick"],
        }
        for closure in product["closures"]
    ]


def _recon_summary(rec: dict) -> dict:
    return {
        "reconciliation_id": rec["reconciliation_id"],
        "loads": [
            {"tick": l["tick"], "box": l["box_serial"], "levels": l["levels"]}
            for l in rec["loads"]
        ],
        "skips": [
            {
                "tick": s["tick"],
                "level": s["level"],
                "cooling_since": s["cooling_since"],
                "cooling_until": s["cooling_until"],
            }
            for s in rec["skips"]
        ],
        "removals": [
            {"tick": r["tick"], "position": r["position"], "box": r["box_serial"]}
            for r in rec["removals"]
        ],
        "ticket_verdict": rec["ticket_verdict"],
        "content_digest": rec["content_digest"],
    }


def run(db_path: str | None = None) -> dict:
    path = db_path or tempfile.mktemp(suffix=".sqlite")
    log: dict = {"db": path, "steps": []}

    store = Store(path)
    thermal = ThermalService(store)
    physical = PhysicalService(store, thermal)
    thermal.publish_catalog("cycle-L1", [LEVEL_L1])

    # -- base scenario ------------------------------------------------------
    base = thermal.publish_product("P-base", "cycle-L1", BASE_READINGS)
    log["steps"].append({"op": "freeze_product", "product": "P-base", "closures": _closure_summary(base)})
    physical.publish_station_log("cool-1", [{"position": 0, "levels": ["L1"], "cooling": 1}])
    physical.publish_carousel_config("carousel-XY", ["X", "Y"])
    base_rec = physical.reconcile(
        "R-base",
        "P-base",
        "cool-1",
        "carousel-XY",
        ops=[{"tick": 5, "position": 1, "take": "Y", "replace": "Z"}],
        ticket={"box_serial": "Y", "claims": [{"tick": 4}]},
    )
    log["steps"].append({"op": "reconcile", "scenario": "base", **_recon_summary(base_rec)})

    # -- corrected observation: only t=4 closes -> X, claimed Y empty -------
    corrected = thermal.publish_product("P-corrected", "cycle-L1", CORRECTED_READINGS)
    log["steps"].append(
        {"op": "freeze_product", "product": "P-corrected", "closures": _closure_summary(corrected)}
    )
    corr_rec = physical.reconcile(
        "R-corrected",
        "P-corrected",
        "cool-1",
        "carousel-XY",
        ops=[{"tick": 5, "position": 1, "take": "Y", "replace": "Z"}],
        ticket={"box_serial": "Y", "claims": [{"tick": 4}]},
    )
    log["steps"].append(
        {"op": "reconcile", "scenario": "corrected_observation", **_recon_summary(corr_rec)}
    )

    # -- original observation, cooling=3 -> t=4 skipped, distinct reason ----
    physical.publish_station_log("cool-3", [{"position": 0, "levels": ["L1"], "cooling": 3}])
    cool_rec = physical.reconcile(
        "R-cool3",
        "P-base",
        "cool-3",
        "carousel-XY",
        ops=[{"tick": 5, "position": 1, "take": "Y", "replace": "Z"}],
        ticket={"box_serial": "Y", "claims": [{"tick": 4}]},
    )
    log["steps"].append(
        {"op": "reconcile", "scenario": "cooling_drop", **_recon_summary(cool_rec)}
    )

    # Old products and reconciliations stay readable after corrections.
    old_base = thermal.get_product("P-base")
    log["steps"].append(
        {
            "op": "read_back_old",
            "product": "P-base",
            "same_digest": old_base["content_digest"] == base["content_digest"],
            "closures": _closure_summary(old_base),
        }
    )

    # -- combination: wrap-around, same-tick exchange, two levels together --
    thermal.publish_catalog(
        "cycle-AB",
        [{"name": "A", "low": "1", "high": "2"}, {"name": "B", "low": "1", "high": "4"}],
    )
    combo_readings = [
        {"tick": 0, "at": "c0", "bits": "00000000"},
        {"tick": 1, "at": "c1", "bits": "40a00000"},  # 5 -> A & B both hot
        {"tick": 2, "at": "c2", "bits": "3f800000"},  # 1 -> A & B close together
        {"tick": 3, "at": "c3", "bits": "40800000"},  # 4 -> A hot
        {"tick": 4, "at": "c4", "bits": "3f800000"},  # 1 -> A closes
        {"tick": 5, "at": "c5", "bits": "40800000"},  # 4 -> A hot
        {"tick": 6, "at": "c6", "bits": "3f800000"},  # 1 -> A closes
    ]
    combo = thermal.publish_product("P-combo", "cycle-AB", combo_readings)
    log["steps"].append(
        {"op": "freeze_product", "product": "P-combo", "closures": _closure_summary(combo)}
    )
    physical.publish_station_log(
        "combo-station", [{"position": 0, "levels": ["A", "B"], "cooling": 0}]
    )
    # Two-slot carousel: X, Y.  Loads at t2 -> X, t4 -> Y, then t6 wraps back to
    # slot 0; exchange slot 0 (X->W) in the *same* tick as the t6 trigger, so the
    # wrapped load enters the freshly exchanged box W.
    physical.publish_carousel_config("carousel-XYW", ["X", "Y"])
    combo_rec = physical.reconcile(
        "R-combo",
        "P-combo",
        "combo-station",
        "carousel-XYW",
        ops=[{"tick": 6, "position": 0, "take": "X", "replace": "W"}],
        ticket={"box_serial": "W", "claims": [{"tick": 6, "level": "A"}]},
    )
    log["steps"].append(
        {"op": "reconcile", "scenario": "combination", **_recon_summary(combo_rec)}
    )
    log["steps"].append(
        {
            "op": "verify_public",
            "thermal_problems": verify_public_thermal_product(public_thermal_product(thermal, "P-combo")),
            "reconciliation_problems": verify_public_reconciliation(
                public_reconciliation(physical, "R-combo")
            ),
        }
    )

    store.close()

    # -- reopen the database and re-read through normal reader entry points --
    store2 = Store(path)
    thermal2 = ThermalService(store2)
    physical2 = PhysicalService(store2, thermal2)
    reread = {
        "product_same": thermal2.get_product("P-combo")["content_digest"] == combo["content_digest"],
        "base_same": thermal2.get_product("P-base")["content_digest"] == base["content_digest"],
        "base_closures": _closure_summary(thermal2.get_product("P-base")),
        "recon_same": physical2.get_reconciliation("R-combo")["content_digest"]
        == combo_rec["content_digest"],
        "base_verdict": physical2.get_reconciliation("R-base")["ticket_verdict"]["overall"],
        "corrected_verdict": physical2.get_reconciliation("R-corrected")["ticket_verdict"]["overall"],
        "cool3_verdict": physical2.get_reconciliation("R-cool3")["ticket_verdict"]["overall"],
        "combo_loads": [
            (l["tick"], l["box_serial"]) for l in physical2.get_reconciliation("R-combo")["loads"]
        ],
    }
    log["steps"].append({"op": "reopen_recheck", **reread})
    store2.close()
    if db_path is None:
        try:
            os.unlink(path)
        except OSError:
            pass
    return log


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", help="SQLite path (default: temporary)")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(argv)
    result = run(args.db)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return 0
    _print_text(result)
    return 0


def _print_text(log: dict) -> None:
    print(f"store: {log['db']}")
    for step in log["steps"]:
        op = step["op"]
        if op == "freeze_product":
            print(f"\n[freeze {step['product']}] closures:")
            for closure in step["closures"]:
                print(
                    f"  {closure['level']} close={closure['close_tick']} "
                    f"warm={closure['warm_bits']}@{closure['warm_tick']} "
                    f"cool={closure['cool_bits']}@{closure['cool_tick']}"
                )
        elif op == "reconcile":
            print(f"\n[reconcile {step['reconciliation_id']}] ({step['scenario']})")
            for load in step["loads"]:
                print(f"  load t={load['tick']} -> box {load['box']} levels={load['levels']}")
            for skip in step["skips"]:
                print(
                    f"  skipped t={skip['tick']} level={skip['level']} "
                    f"(cooling {skip['cooling_since']}..{skip['cooling_until']})"
                )
            for removal in step["removals"]:
                print(f"  take t={removal['tick']} slot {removal['position']} -> {removal['box']}")
            verdict = step["ticket_verdict"]
            print(f"  ticket: {verdict['overall']}")
            for claim in verdict["claims"]:
                print(
                    f"    tick {claim['tick']}: {claim['status']} "
                    f"(actual={claim['actual_box']}) {claim['reason']}"
                )
        elif op == "read_back_old":
            print(f"\n[read back {step['product']}] same_digest={step['same_digest']}")
        elif op == "verify_public":
            print(
                f"\n[public verify] thermal={step['thermal_problems'] or 'none'} "
                f"reconciliation={step['reconciliation_problems'] or 'none'}"
            )
        elif op == "reopen_recheck":
            print("\n[reopen database]")
            print(f"  P-combo digest stable: {step['product_same']}")
            print(f"  P-base  digest stable: {step['base_same']}")
            print(f"  R-combo digest stable: {step['recon_same']}")
            print(f"  verdicts: base={step['base_verdict']} corrected={step['corrected_verdict']} "
                  f"cool3={step['cool3_verdict']}")
            print(f"  combo loads: {step['combo_loads']}")
        else:
            print(f"\n[{op}] {step}")


if __name__ == "__main__":
    raise SystemExit(main())
