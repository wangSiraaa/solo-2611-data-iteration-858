"""Thermal closure products: freeze stress-cycle closures for physical replay.

A thermal product binds one exact device catalog (thresholds per stress level),
one frozen reading series (readings + segment boundary markers), and the
derived closure stream.  Publishing is fail-closed:

* every reading must be finite (NaN / +/-Inf -> the whole product is rejected
  and any product already frozen under the same id is left untouched);
* duplicate ticks are rejected (the bench guarantees a strict total order);
* re-publishing identical content replays (``created=False``); a differing
  payload under the same id raises ``ThermalProductConflict``.

Once frozen, the product reads back bit-for-bit even if later corrections
change the *current* bench state: the frozen definition (bits included) and
the closure stream are stored as immutable rows and their digests are
re-checked on every read.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from . import semantics
from .canonical import canonical_json, digest, parse_fraction
from .errors import (
    IntegrityError,
    ThermalCatalogConflict,
    ThermalCatalogNotFound,
    ThermalProductConflict,
    ThermalProductNotFound,
    ThermalSegmentRejected,
    ValidationError,
)
from .storage import Store, utcnow


def _bad(message: str) -> ValidationError:
    return ValidationError(message)


def _tick_key(tick: Any) -> tuple[int, Any]:
    if isinstance(tick, bool):
        raise _bad(f"tick may not be a boolean: {tick!r}")
    if isinstance(tick, int):
        return (0, tick)
    if isinstance(tick, str):
        return (1, tick)
    raise _bad(f"tick must be an integer or string, got {tick!r}")


class ThermalService:
    def __init__(self, store: Store):
        self.store = store

    # -- catalogs ------------------------------------------------------------
    def publish_catalog(self, version: str, levels: Sequence[Mapping[str, Any]]) -> dict:
        if not isinstance(version, str) or not version:
            raise _bad("catalog version must be a non-empty string")
        try:
            parsed = semantics.thermal_catalog(levels)
        except ValueError as exc:
            raise _bad(str(exc)) from exc
        content_digest = semantics.thermal_catalog_digest(parsed)
        existing = self.store.conn.execute(
            "SELECT * FROM thermal_catalogs WHERE version=?", (version,)
        ).fetchone()
        if existing is not None:
            if existing["content_digest"] != content_digest:
                raise ThermalCatalogConflict(
                    f"thermal catalog {version!r} exists with different content"
                )
            return self._catalog_view(existing)
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO thermal_catalogs(version, levels_json, content_digest, created_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    version,
                    canonical_json([level.canonical() for level in parsed]),
                    content_digest,
                    utcnow(),
                ),
            )
        return self.catalog(version)

    def _catalog_levels(self, version: str) -> tuple[semantics.ThermalLevel, ...]:
        row = self.store.conn.execute(
            "SELECT * FROM thermal_catalogs WHERE version=?", (version,)
        ).fetchone()
        if row is None:
            raise ThermalCatalogNotFound(version)
        return tuple(
            semantics.ThermalLevel(
                name=item["name"],
                low=parse_fraction(item["low"], field="low"),
                high=parse_fraction(item["high"], field="high"),
            )
            for item in json.loads(row["levels_json"])
        )

    def catalog(self, version: str) -> dict:
        row = self.store.conn.execute(
            "SELECT * FROM thermal_catalogs WHERE version=?", (version,)
        ).fetchone()
        if row is None:
            raise ThermalCatalogNotFound(version)
        return self._catalog_view(row)

    @staticmethod
    def _catalog_view(row) -> dict:
        return {
            "version": row["version"],
            "levels": json.loads(row["levels_json"]),
            "content_digest": row["content_digest"],
        }

    # -- segments ------------------------------------------------------------
    # Segments are not a separately versioned object: a boundary marker *is*
    # part of the reading series, keyed by the tick at which it applies.  Model
    # them only through the product payload so a corrected series can never
    # inherit a stale marker from a different series.

    def _segments_for(
        self, ticks: Sequence[Any], explicit: Sequence[Mapping] | None
    ) -> list[semantics.ThermalSegment]:
        """Resolve boundary markers that apply to this reading series.

        Declarations come from the payload (``{"tick", "marker"}``).  A
        declaration whose tick is not part of the series is an error rather
        than a silent no-op, so a mistyped tick cannot quietly change the
        closure stream.
        """
        if not explicit:
            return []
        tick_set = set(ticks)
        resolved: dict[Any, str] = {}
        for entry in explicit:
            if not isinstance(entry, Mapping):
                raise _bad(f"segment declaration is not an object: {entry!r}")
            if "tick" not in entry:
                raise _bad("segment declaration requires a tick")
            tick = entry["tick"]
            marker = entry.get("marker")
            if marker not in semantics._VALID_MARKERS:
                raise _bad(f"bad segment marker {marker!r}")
            if tick not in tick_set:
                raise _bad(
                    f"segment tick {tick!r} does not correspond to a reading in this series"
                )
            if tick in resolved and resolved[tick] != marker:
                raise _bad(f"segment tick {tick!r} declared with conflicting markers")
            resolved[tick] = marker
        return [
            semantics.ThermalSegment(tick, marker)
            for tick, marker in sorted(resolved.items(), key=lambda kv: _tick_key(kv[0]))
        ]

    # -- products ------------------------------------------------------------
    def publish_product(
        self,
        product_id: str,
        catalog_version: str,
        readings: Sequence[Mapping[str, Any]],
        *,
        identity: Any = None,
        segments: Sequence[Mapping[str, Any]] | None = None,
    ) -> dict:
        if not isinstance(product_id, str) or not product_id:
            raise _bad("product_id must be a non-empty string")
        levels = self._catalog_levels(catalog_version)
        try:
            parsed = semantics.thermal_readings(readings)
        except ValueError as exc:
            raise _bad(str(exc)) from exc
        segs = self._segments_for([r.tick for r in parsed], segments)

        # Reading identity binds each tick/at and the original bit pattern, so a
        # correction to any reading (or to a boundary marker) changes the
        # definition digest and therefore the frozen product's content.
        definition = {
            "kind": "thermal_product",
            "catalog": {
                "version": catalog_version,
                "digest": semantics.thermal_catalog_digest(levels),
            },
            "readings": [reading.identity() for reading in parsed],
            "segments": [s.canonical() for s in segs],
        }
        definition_digest = digest(definition)

        # Fail-closed on any non-finite reading: reject before touching stored
        # state so any product already frozen under this id stays intact.
        nonfinite = [
            {"tick": r.tick, "bits": r.bits}
            for r in parsed
            if r.nonfinite_kind is not None
        ]
        if nonfinite:
            reasons = [{"reason": "nonfinite", "readings": nonfinite}]
            attempt_id = self._record_attempt(product_id, definition_digest, "rejected", reasons)
            raise ThermalSegmentRejected(product_id, reasons, attempt_id=attempt_id)

        closures = semantics.compute_closures(levels, parsed, segs)
        closure_body = [
            {key: value for key, value in closure.items() if key != "ordinal"}
            for closure in closures
        ]
        closure_digest = digest(closure_body)
        content = {**definition, "closures": closure_body}
        content_digest = digest(content)

        existing = self.store.conn.execute(
            "SELECT * FROM thermal_products WHERE product_id=?", (product_id,)
        ).fetchone()
        if existing is not None:
            if existing["content_digest"] != content_digest:
                raise ThermalProductConflict(
                    f"thermal product {product_id!r} exists with different content"
                )
            return self._product_view(existing, created=False)

        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO thermal_products "
                "(product_id, catalog_version, segment_ticks_json, definition_json, "
                " lineage_json, definition_digest, closure_digest, content_digest, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    product_id,
                    catalog_version,
                    canonical_json([s.tick for s in segs]),
                    canonical_json(definition),
                    canonical_json({"identity": identity}),
                    definition_digest,
                    closure_digest,
                    content_digest,
                    utcnow(),
                ),
            )
            for closure in closures:
                conn.execute(
                    "INSERT INTO thermal_product_closure "
                    "(product_id, ordinal, level, close_tick, close_at, open_tick, open_at, "
                    " warm_bits, warm_at, warm_tick, cool_bits, cool_at, cool_tick, "
                    " wave_json, canonical_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        product_id,
                        closure["ordinal"],
                        closure["level"],
                        canonical_json(closure["close_tick"]),
                        canonical_json(closure["close_at"]),
                        canonical_json(closure["open_tick"]),
                        canonical_json(closure["open_at"]),
                        closure["warm_bits"],
                        canonical_json(closure["warm_at"]),
                        canonical_json(closure["warm_tick"]),
                        closure["cool_bits"],
                        canonical_json(closure["cool_at"]),
                        canonical_json(closure["cool_tick"]),
                        canonical_json({"level": closure["level"]}),
                        canonical_json(closure),
                    ),
                )
            conn.execute(
                "INSERT INTO thermal_attempts "
                "(product_id, definition_digest, status, reasons_json, created_at) "
                "VALUES (?, ?, 'published', ?, ?)",
                (product_id, definition_digest, canonical_json([]), utcnow()),
            )
        row = self.store.conn.execute(
            "SELECT * FROM thermal_products WHERE product_id=?", (product_id,)
        ).fetchone()
        return self._product_view(row, created=True)

    def _record_attempt(
        self, product_id: str, definition_digest: str, status: str, reasons: list
    ) -> int:
        with self.store.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO thermal_attempts "
                "(product_id, definition_digest, status, reasons_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (product_id, definition_digest, status, canonical_json(reasons), utcnow()),
            )
            return cur.lastrowid

    def _load_closures(self, conn, product_id: str) -> list[dict]:
        rows = conn.execute(
            "SELECT canonical_json FROM thermal_product_closure "
            "WHERE product_id=? ORDER BY ordinal",
            (product_id,),
        ).fetchall()
        return [json.loads(row["canonical_json"]) for row in rows]

    def get_product(self, product_id: str) -> dict:
        row = self.store.conn.execute(
            "SELECT * FROM thermal_products WHERE product_id=?", (product_id,)
        ).fetchone()
        if row is None:
            raise ThermalProductNotFound(product_id)
        return self._product_view(row, created=False)

    def _product_view(self, row, created: bool) -> dict:
        definition = json.loads(row["definition_json"])
        closures = self._load_closures(self.store.conn, row["product_id"])
        closure_body = [
            {key: value for key, value in closure.items() if key != "ordinal"}
            for closure in closures
        ]
        # Independent read-back: re-derive both digests from stored content.
        if digest(definition) != row["definition_digest"]:
            raise IntegrityError(
                f"thermal product {row['product_id']!r}: definition digest mismatch"
            )
        if digest(closure_body) != row["closure_digest"]:
            raise IntegrityError(
                f"thermal product {row['product_id']!r}: closure stream digest mismatch"
            )
        if digest({**definition, "closures": closure_body}) != row["content_digest"]:
            raise IntegrityError(
                f"thermal product {row['product_id']!r}: content digest mismatch"
            )
        catalog = self.store.conn.execute(
            "SELECT levels_json FROM thermal_catalogs WHERE version=?",
            (row["catalog_version"],),
        ).fetchone()
        return {
            "product_id": row["product_id"],
            "catalog_version": row["catalog_version"],
            "catalog_digest": definition["catalog"]["digest"],
            "levels": json.loads(catalog["levels_json"]),
            "segments": definition["segments"],
            "readings": definition["readings"],
            "lineage": json.loads(row["lineage_json"]),
            "definition_digest": row["definition_digest"],
            "closure_digest": row["closure_digest"],
            "content_digest": row["content_digest"],
            "closures": closures,
            "created": created,
        }

    def attempts(self, product_id: str) -> list[dict]:
        rows = self.store.conn.execute(
            "SELECT attempt_id, status, reasons_json, created_at FROM thermal_attempts "
            "WHERE product_id=? ORDER BY attempt_id",
            (product_id,),
        ).fetchall()
        return [
            {
                "attempt_id": row["attempt_id"],
                "status": row["status"],
                "reasons": json.loads(row["reasons_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    # -- physical matching helpers ------------------------------------------
    def locate_trigger(self, product_id: str, closure_index: int, bits: str) -> str | None:
        """Which trigger (``heat_up`` / ``cool_down`` / ``both``) a bit pattern is."""
        closure = self.get_product(product_id)["closures"][closure_index]
        if closure["warm_bits"] == bits and closure["cool_bits"] == bits:
            return "both"
        if closure["warm_bits"] == bits:
            return "heat_up"
        if closure["cool_bits"] == bits:
            return "cool_down"
        return None
