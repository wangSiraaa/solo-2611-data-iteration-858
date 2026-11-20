"""Public artefact rendering and verification.

* ``public_semantic_product`` — immutable canonical observation product.
* ``public_delivery`` — standalone delivery document with enough information
  for an independent reader to re-check the actual decimal representation
  without consulting the current observation state.

Both builders re-verify the stored content digests before rendering.
"""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path

from .delivery import DeliveryService
from .physical import PhysicalService
from .service import SemanticsService
from .thermal import ThermalService


def public_thermal_product(thermal: ThermalService, product_id: str) -> dict:
    """Standalone thermal product document (closures + trigger originals)."""
    product = thermal.get_product(product_id)
    return {
        "kind": "thermal_product",
        "product_id": product["product_id"],
        "catalog_version": product["catalog_version"],
        "catalog_digest": product["catalog_digest"],
        "levels": product["levels"],
        "segments": product["segments"],
        "readings": product["readings"],
        "definition_digest": product["definition_digest"],
        "closure_digest": product["closure_digest"],
        "content_digest": product["content_digest"],
        "closures": product["closures"],
    }


def verify_public_thermal_product(document: dict) -> list[str]:
    """Re-derive closure digests from a standalone thermal document."""
    from .canonical import digest

    problems: list[str] = []
    definition = {
        "kind": "thermal_product",
        "catalog": {
            "version": document["catalog_version"],
            "digest": document["catalog_digest"],
        },
        "readings": document["readings"],
        "segments": document["segments"],
    }
    if digest(definition) != document["definition_digest"]:
        problems.append("definition digest mismatch (readings/segments/catalog)")
    closure_body = [
        {key: value for key, value in closure.items() if key != "ordinal"}
        for closure in document["closures"]
    ]
    if digest(closure_body) != document["closure_digest"]:
        problems.append("closure stream digest mismatch")
    if digest({**definition, "closures": closure_body}) != document["content_digest"]:
        problems.append("content digest mismatch (definition + closures)")
    for closure in document["closures"]:
        if isinstance(closure.get("warm_bits"), str) and len(closure["warm_bits"]) != 8:
            problems.append(f"closure {closure.get('ordinal')}: warm_bits malformed")
        if isinstance(closure.get("cool_bits"), str) and len(closure["cool_bits"]) != 8:
            problems.append(f"closure {closure.get('ordinal')}: cool_bits malformed")
    return problems


def public_reconciliation(physical: PhysicalService, reconciliation_id: str) -> dict:
    """Standalone physical reconciliation document for an independent reader."""
    view = physical.get_reconciliation(reconciliation_id)
    return {
        "kind": "physical_reconciliation",
        "reconciliation_id": view["reconciliation_id"],
        "product": {
            "product_id": view["product_id"],
            "content_digest": view["product_digest"],
        },
        "station": {
            "version": view["station_version"],
            "content_digest": view["station_digest"],
        },
        "carousel": {
            "config_id": view["carousel_config"],
            "content_digest": view["carousel_digest"],
        },
        "content_digest": view["content_digest"],
        "ops": view["ops"],
        "ticket": view["ticket"],
        "loads": [
            {
                "ordinal": load["ordinal"],
                "tick": load["tick"],
                "at": load["at"],
                "level": load["level"],
                "close_ordinal": load["close_ordinal"],
                "box_serial": load["box_serial"],
                "box_slot": load["box_slot"],
            }
            for load in view["loads"]
        ],
        "skips": [
            {
                "ordinal": skip["ordinal"],
                "tick": skip["tick"],
                "level": skip["level"],
                "close_ordinal": skip["close_ordinal"],
                "cooling_since": skip["cooling_since"],
                "cooling_until": skip["cooling_until"],
                "reason": skip["reason"],
            }
            for skip in view["skips"]
        ],
        "removals": [
            {
                "ordinal": removal["ordinal"],
                "tick": removal["tick"],
                "position": removal["position"],
                "box_serial": removal["box_serial"],
            }
            for removal in view["removals"]
        ],
        "final_slots": view["final_slots"],
        "ticket_verdict": view["ticket_verdict"],
    }


def verify_public_reconciliation(document: dict) -> list[str]:
    """Standalone physical-trace plausibility checks (no database needed).

    Confirms each load refers to a uniquely consumed box, each skip carries a
    cooling window, the removal's serial matches the box that was loaded into
    that slot (when both are visible), and the ticket verdict agrees with the
    load table.
    """
    problems: list[str] = []
    by_tick = {load["tick"]: load for load in document["loads"]}
    for load in document["loads"]:
        if load["box_serial"] is None:
            problems.append(f"load {load['ordinal']}: missing box serial")
    consumed = [load["box_serial"] for load in document["loads"]]
    if len(set(consumed)) != len(consumed):
        problems.append("a physical box was loaded more than once")
    for skip in document["skips"]:
        if skip["reason"] != "cooling" or skip["cooling_until"] is None:
            problems.append(
                f"skip {skip['ordinal']}: dropped without a cooling window"
            )
    # The box removed at a slot must be the box most recently loaded there.
    last_load_at_slot: dict[int, dict] = {}
    events = sorted(
        [(load["tick"], "load", load) for load in document["loads"]]
        + [(removal["tick"], "remove", removal) for removal in document["removals"]],
        key=lambda item: (item[0], 0 if item[1] == "remove" else 1),
    )
    for tick, kind, payload in events:
        if kind == "load":
            last_load_at_slot[payload["box_slot"]] = payload
        else:
            prior = last_load_at_slot.get(payload["position"])
            if prior is not None and prior["box_serial"] != payload["box_serial"]:
                problems.append(
                    f"removal at tick {tick}: took {payload['box_serial']!r} but slot "
                    f"{payload['position']} held {prior['box_serial']!r}"
                )
    verdict = document["ticket_verdict"]
    for claim in verdict["claims"]:
        load = by_tick.get(claim["tick"])
        if claim["status"] == "matched":
            if load is None:
                problems.append(f"claim tick {claim['tick']}: matched without a load")
            elif claim["actual_box"] != load["box_serial"]:
                problems.append(
                    f"claim tick {claim['tick']}: matched box disagrees with load table"
                )
        elif claim["status"] == "wrong_box":
            if load is None:
                problems.append(f"claim tick {claim['tick']}: wrong_box without a load")
            elif claim["actual_box"] == load["box_serial"]:
                problems.append(
                    f"claim tick {claim['tick']}: wrong_box but box equals the load"
                )
    return problems


def public_semantic_product(semantics: SemanticsService, product_id: str) -> dict:
    product = semantics.get_semantic_product(product_id)
    return {
        "kind": "semantic_product",
        "product_id": product["id"],
        "reference_version": product["reference_version"],
        "reference_digest": product["reference_digest"],
        "content_digest": product["content_digest"],
        "sample_ids": product["sample_ids"],
        "records": [
            {
                "sample_id": item["sample_id"],
                "received_revision": item["received_revision"],
                "original_bits": item["bits"],
                "kind": item["kind"],
                "sign": item["sign"],
                "payload": item["payload"],
                "exact_value": item["value"],
                "negated_value": item["negated_value"],
                "expected": item["expected"],
                "tolerance": item["tolerance"],
                "status": item["status"],
                "difference": item["difference"],
                "abs_difference": item["abs_difference"],
            }
            for item in product["items"]
        ],
    }


def public_delivery(delivery: DeliveryService, delivery_id: str) -> dict:
    view = delivery.get_delivery(delivery_id)
    return {
        "kind": "delivery",
        "delivery_id": view["delivery_id"],
        "canonical_product": {
            "product_id": view["product_id"],
            "content_digest": view["product_digest"],
        },
        "reference": {
            "version": view["reference_version"],
            "content_digest": view["reference_digest"],
        },
        "reader_contract": view["contract"],
        "contract_id": view["contract_id"],
        "content_digest": view["content_digest"],
        "lines": [
            {
                "sample_id": line["sample_id"],
                "original_bits": line["original_bits"],
                "kind": line["kind"],
                "sign": line["sign"],
                "original_exact_value": line["exact_value"],
                "original_negated_value": line["negated_value"],
                "semantic_status": line["status"],
                "delivered_status": line["delivered_status"],
                "coefficient": line["coefficient"],
                "scale": line["scale"],
                "reader_value": line["reader_value"],
                "exact_error": line["error"],
                "decoder": "coefficient / 10**scale",
                "evidence": line["evidence"],
            }
            for line in view["lines"]
        ],
    }


def verify_public_delivery(document: dict) -> list[str]:
    """Re-derive all checks from a standalone public document.

    Returns a list of textual problems (empty means the document is
    internally consistent).  It does not need a database: it recomputes the
    coefficient bound, error budget, scale legality and reader value.
    """
    problems: list[str] = []
    contract = document["reader_contract"]
    allowed = set(contract["allowed_scales"])
    bound = contract["max_abs_coefficient"]
    budget = Fraction(contract["max_abs_error"])
    required = {
        "sample_id", "original_bits", "kind", "sign", "original_exact_value",
        "semantic_status", "delivered_status", "coefficient", "scale",
        "reader_value", "exact_error",
    }
    for line in document["lines"]:
        missing = required - line.keys()
        if missing:
            problems.append(f"{line.get('sample_id')}: missing {sorted(missing)}")
            continue
        sample = line["sample_id"]
        scale = line["scale"]
        coefficient = line["coefficient"]
        if scale not in allowed:
            problems.append(f"{sample}: scale {scale} not allowed")
        if abs(coefficient) > bound:
            problems.append(f"{sample}: coefficient {coefficient} exceeds {bound}")
        if line["semantic_status"] != line["delivered_status"]:
            problems.append(f"{sample}: semantic status changed on delivery")
        value = Fraction(line["original_exact_value"])
        delivered = Fraction(coefficient, 10 ** scale)
        error = delivered - value
        if str(Fraction(line["exact_error"])) != str(error):
            problems.append(f"{sample}: stored error {line['exact_error']} != {error}")
        if abs(error) > budget:
            problems.append(f"{sample}: error {error} exceeds budget {budget}")
        if str(Fraction(line["reader_value"])) != str(delivered):
            problems.append(f"{sample}: reader value mismatch")
    return problems


def write_json(path: str | Path, document: dict) -> Path:
    target = Path(path)
    target.write_text(json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8")
    return target
