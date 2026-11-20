"""Physical sample traceability: carousel replay against a frozen product.

This module replays a *physical* carousel driven by frozen thermal closures.
It answers: which physical box received which closed stress cycle, which
triggers were dropped because the mechanism was still cooling, when a box was
taken off, and whether a lab slip ("this box holds the sample from tick t")
actually agrees with the replay.

Carousel model
--------------
* ``N`` ordered slots, index ``0 .. N-1``, each initially holding one box with
  a unique serial.  A single *current* pointer selects the slot that receives
  the next valid sample; after every load the pointer advances by one and wraps
  (``(p + 1) % N``).
* Station logs are versioned.  A station subscribes to a set of stress levels
  and has its own mechanical cooling duration.  A trigger fires for a station
  at reading tick ``t`` when a *subscribed* level closes at ``t``; several
  subscribed levels closing at the same instant still produce exactly one
  trigger for that station.
* A trigger inside the cooling window is *dropped*: it neither queues nor
  advances the carousel.  A trigger exactly at the cooling deadline may sample
  again (the window is half-open ``[load, load+duration)``).
* Each successful trigger loads the sample into the current box and advances.
* Only the worker may remove a box: taking the box at a given position and
  replacing it with a fresh (never-used) serial.  The recorded ``take`` serial
  must equal the box actually at that position -- a replay may not substitute
  another box.
* All physical operations at one tick happen *before* any trigger at that tick
  (box exchange first, then trigger).

Replay is pure and deterministic: running it twice yields the same loads; the
frozen product and station log are never mutated.  Any contradiction (a take
of the wrong serial, a reused replacement serial, a duplicate initial serial)
is reported as ``PhysicalTraceConflict`` naming the *first* physical mismatch,
and no reconciliation is published, so the most recent reconcilable delivery
stays readable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .canonical import canonical_json, digest
from .errors import (
    CarouselConfigConflict,
    CarouselConfigNotFound,
    IntegrityError,
    PhysicalTraceConflict,
    ReconciliationConflict,
    ReconciliationNotFound,
    StationLogConflict,
    StationLogNotFound,
    ValidationError,
)
from .storage import Store, utcnow
from .thermal import ThermalService


def _bad(message: str) -> ValidationError:
    return ValidationError(message)


def _tick_sort_key(tick: Any) -> tuple[int, Any]:
    if isinstance(tick, bool):
        raise _bad(f"tick may not be a boolean: {tick!r}")
    if isinstance(tick, int):
        return (0, tick)
    if isinstance(tick, str):
        return (1, tick)
    raise _bad(f"tick must be an integer or string, got {tick!r}")


def _sorted_ticks(ticks) -> list:
    return sorted(ticks, key=_tick_sort_key)


@dataclass
class _ReplayConflict(Exception):
    """Internal: a pure replay contradiction; never persists on its own."""

    mismatch: dict

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"physical replay conflict: {self.mismatch!r}"


class PhysicalService:
    def __init__(self, store: Store, thermal: ThermalService | None = None):
        self.store = store
        self.thermal = thermal or ThermalService(store)

    # -- station logs --------------------------------------------------------
    def publish_station_log(self, version: str, stations: Sequence[Mapping[str, Any]]) -> dict:
        if not isinstance(version, str) or not version:
            raise _bad("station log version must be a non-empty string")
        parsed = self._normalise_stations(stations)
        content_digest = digest({"version": version, "stations": parsed})
        existing = self.store.conn.execute(
            "SELECT * FROM station_logs WHERE version=?", (version,)
        ).fetchone()
        if existing is not None:
            if existing["content_digest"] != content_digest:
                raise StationLogConflict(
                    f"station log {version!r} exists with different content"
                )
            return self.station_log(version)
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO station_logs(version, content_digest, created_at) VALUES (?, ?, ?)",
                (version, content_digest, utcnow()),
            )
            for station in parsed:
                conn.execute(
                    "INSERT INTO station_cooling(version, position, duration) VALUES (?, ?, ?)",
                    (version, station["position"], station["cooling"]),
                )
                for level in station["levels"]:
                    conn.execute(
                        "INSERT INTO station_log_subscription(version, position, level) "
                        "VALUES (?, ?, ?)",
                        (version, station["position"], level),
                    )
        return self.station_log(version)

    @staticmethod
    def _normalise_stations(stations: Sequence[Mapping[str, Any]]) -> list[dict]:
        if isinstance(stations, (str, bytes)) or not isinstance(stations, Sequence) or not stations:
            raise _bad("stations must be a non-empty sequence")
        parsed: list[dict] = []
        seen_positions: set[int] = set()
        for station in stations:
            if not isinstance(station, Mapping):
                raise _bad(f"station is not an object: {station!r}")
            position = station.get("position")
            if isinstance(position, bool) or not isinstance(position, int) or position < 0:
                raise _bad(f"station position must be a non-negative integer, got {position!r}")
            if position in seen_positions:
                raise _bad(f"duplicate station position {position}")
            seen_positions.add(position)
            levels = station.get("levels")
            if isinstance(levels, (str, bytes)) or not isinstance(levels, Sequence) or not levels:
                raise _bad(f"station {position} must subscribe to at least one level")
            level_list = []
            for level in levels:
                if not isinstance(level, str) or not level:
                    raise _bad(f"station {position}: level subscription must be a string")
                level_list.append(level)
            if len(set(level_list)) != len(level_list):
                raise _bad(f"station {position} subscribes to a level twice")
            cooling = station.get("cooling", 0)
            if isinstance(cooling, bool) or not isinstance(cooling, int) or cooling < 0:
                raise _bad(f"station {position}: cooling must be a non-negative integer")
            parsed.append(
                {
                    "position": position,
                    "levels": sorted(level_list),
                    "cooling": cooling,
                }
            )
        return sorted(parsed, key=lambda item: item["position"])

    def station_log(self, version: str) -> dict:
        row = self.store.conn.execute(
            "SELECT * FROM station_logs WHERE version=?", (version,)
        ).fetchone()
        if row is None:
            raise StationLogNotFound(version)
        subs = self.store.conn.execute(
            "SELECT position, level FROM station_log_subscription WHERE version=? "
            "ORDER BY position, level",
            (version,),
        ).fetchall()
        cooling = self.store.conn.execute(
            "SELECT position, duration FROM station_cooling WHERE version=? ORDER BY position",
            (version,),
        ).fetchall()
        level_by_position: dict[int, list[str]] = {}
        for item in subs:
            level_by_position.setdefault(item["position"], []).append(item["level"])
        stations = [
            {
                "position": item["position"],
                "levels": level_by_position.get(item["position"], []),
                "cooling": item["duration"],
            }
            for item in cooling
        ]
        if digest({"version": version, "stations": stations}) != row["content_digest"]:
            raise IntegrityError(f"station log {version!r}: content digest mismatch")
        return {"version": version, "stations": stations, "content_digest": row["content_digest"]}

    # -- carousel configuration ---------------------------------------------
    def publish_carousel_config(self, config_id: str, slots: Sequence[str]) -> dict:
        if not isinstance(config_id, str) or not config_id:
            raise _bad("config_id must be a non-empty string")
        if isinstance(slots, (str, bytes)) or not isinstance(slots, Sequence) or not slots:
            raise _bad("slots must be a non-empty sequence of box serials")
        serials = []
        for slot in slots:
            if not isinstance(slot, str) or not slot:
                raise _bad(f"box serial must be a non-empty string, got {slot!r}")
            serials.append(slot)
        if len(set(serials)) != len(serials):
            raise _bad("carousel slots must hold uniquely serialised boxes")
        content_digest = digest({"config_id": config_id, "slots": serials})
        existing = self.store.conn.execute(
            "SELECT * FROM carousel_configs WHERE config_id=?", (config_id,)
        ).fetchone()
        if existing is not None:
            if existing["content_digest"] != content_digest:
                raise CarouselConfigConflict(
                    f"carousel config {config_id!r} exists with different content"
                )
            return self.carousel_config(config_id)
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO carousel_configs(config_id, slots_json, content_digest, created_at) "
                "VALUES (?, ?, ?, ?)",
                (config_id, canonical_json(serials), content_digest, utcnow()),
            )
        return self.carousel_config(config_id)

    def carousel_config(self, config_id: str) -> dict:
        row = self.store.conn.execute(
            "SELECT * FROM carousel_configs WHERE config_id=?", (config_id,)
        ).fetchone()
        if row is None:
            raise CarouselConfigNotFound(config_id)
        return {
            "config_id": config_id,
            "slots": json.loads(row["slots_json"]),
            "content_digest": row["content_digest"],
        }

    # -- replay --------------------------------------------------------------
    @staticmethod
    def _fires_by_tick(product: dict, stations: list[dict]) -> dict[Any, list[dict]]:
        """Map tick -> station fires (one per station even if several levels close)."""
        closures_by_tick: dict[Any, list[dict]] = {}
        for closure in product["closures"]:
            closures_by_tick.setdefault(closure["close_tick"], []).append(closure)
        fires: dict[Any, list[dict]] = {}
        for tick, closures in closures_by_tick.items():
            levels_here = {closure["level"] for closure in closures}
            tick_fires = []
            for station in stations:
                matched = sorted(levels_here & set(station["levels"]))
                if not matched:
                    continue
                # One trigger per station even when several subscribed levels
                # close at the same instant; record every driving closure, but
                # a *single* trigger means a *single* carousel advance.
                driving = [c for c in closures if c["level"] in matched]
                tick_fires.append(
                    {
                        "station": station,
                        "levels": [c["level"] for c in driving],
                        "close_ordinals": [c["ordinal"] for c in driving],
                    }
                )
            if tick_fires:
                fires[tick] = sorted(tick_fires, key=lambda f: f["station"]["position"])
        return fires

    def reconcile(
        self,
        reconciliation_id: str,
        product_id: str,
        station_version: str,
        carousel_config_id: str,
        ops: Sequence[Mapping[str, Any]] | None = None,
        ticket: Mapping[str, Any] | None = None,
    ) -> dict:
        if not isinstance(reconciliation_id, str) or not reconciliation_id:
            raise _bad("reconciliation_id must be a non-empty string")
        product = self.thermal.get_product(product_id)
        station = self.station_log(station_version)
        config = self.carousel_config(carousel_config_id)
        ops_list = list(ops or [])
        ticket_doc = self._normalise_ticket(ticket)
        request_body = {
            "product_id": product_id,
            "product_digest": product["content_digest"],
            "station_version": station_version,
            "station_digest": station["content_digest"],
            "carousel_config": carousel_config_id,
            "carousel_digest": config["content_digest"],
            "ops": ops_list,
            "ticket": ticket_doc,
        }
        request_digest = digest(request_body)

        existing = self.store.conn.execute(
            "SELECT * FROM reconciliations WHERE reconciliation_id=?", (reconciliation_id,)
        ).fetchone()
        if existing is not None:
            if existing["request_digest"] != request_digest:
                raise ReconciliationConflict(
                    f"reconciliation {reconciliation_id!r} exists for a different request"
                )
            return self._view(existing, created=False)

        try:
            replay = self._replay(product, station, config, ops_list, ticket_doc)
        except _ReplayConflict as exc:
            attempt_id = self._record_attempt(
                reconciliation_id, request_digest, "conflict", exc.mismatch
            )
            raise PhysicalTraceConflict(
                reconciliation_id, exc.mismatch, attempt_id=attempt_id
            ) from None

        content_digest = digest({**request_body, **replay})

        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO reconciliations "
                "(reconciliation_id, product_id, station_version, carousel_config, "
                " ops_json, ticket_json, request_digest, content_digest, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    reconciliation_id,
                    product_id,
                    station_version,
                    carousel_config_id,
                    canonical_json(ops_list),
                    canonical_json(ticket_doc),
                    request_digest,
                    content_digest,
                    utcnow(),
                ),
            )
            for load in replay["loads"]:
                conn.execute(
                    "INSERT INTO reconciliation_loads "
                    "(reconciliation_id, ordinal, tick, timestamp, product_id, close_ordinal, "
                    " level, box_serial, box_slot) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        reconciliation_id,
                        load["ordinal"],
                        canonical_json(load["tick"]),
                        canonical_json(load["at"]),
                        product_id,
                        load["close_ordinal"],
                        load["level"],
                        load["box_serial"],
                        load["box_slot"],
                    ),
                )
            for skip in replay["skips"]:
                conn.execute(
                    "INSERT INTO reconciliation_skips "
                    "(reconciliation_id, ordinal, tick, timestamp, product_id, close_ordinal, "
                    " level, cooling_since, cooling_until, reason) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        reconciliation_id,
                        skip["ordinal"],
                        canonical_json(skip["tick"]),
                        canonical_json(skip["at"]),
                        product_id,
                        skip["close_ordinal"],
                        skip["level"],
                        None if skip["cooling_since"] is None else canonical_json(skip["cooling_since"]),
                        None if skip["cooling_until"] is None else canonical_json(skip["cooling_until"]),
                        skip["reason"],
                    ),
                )
            for removal in replay["removals"]:
                conn.execute(
                    "INSERT INTO reconciliation_removals "
                    "(reconciliation_id, ordinal, tick, timestamp, position, box_serial, box_slot) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        reconciliation_id,
                        removal["ordinal"],
                        canonical_json(removal["tick"]),
                        canonical_json(removal["at"]),
                        removal["position"],
                        removal["box_serial"],
                        removal["box_slot"],
                    ),
                )
            conn.execute(
                "INSERT INTO reconciliation_attempts "
                "(reconciliation_id, request_digest, status, reasons_json, mismatch_json, created_at) "
                "VALUES (?, ?, 'published', ?, NULL, ?)",
                (reconciliation_id, request_digest, canonical_json([]), utcnow()),
            )
        row = self.store.conn.execute(
            "SELECT * FROM reconciliations WHERE reconciliation_id=?", (reconciliation_id,)
        ).fetchone()
        return self._view(row, created=True)

    def _record_attempt(self, reconciliation_id: str, request_digest: str, status: str, mismatch: dict) -> int:
        with self.store.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO reconciliation_attempts "
                "(reconciliation_id, request_digest, status, reasons_json, mismatch_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    reconciliation_id,
                    request_digest,
                    status,
                    canonical_json([mismatch]),
                    canonical_json(mismatch),
                    utcnow(),
                ),
            )
            return cur.lastrowid

    @staticmethod
    def _normalise_ticket(ticket: Mapping[str, Any] | None) -> dict:
        if ticket is None:
            return {"box_serial": None, "claims": []}
        if not isinstance(ticket, Mapping):
            raise _bad("ticket must be an object")
        box = ticket.get("box_serial")
        if box is not None and (not isinstance(box, str) or not box):
            raise _bad("ticket.box_serial must be a non-empty string when present")
        claims = ticket.get("claims", [])
        if isinstance(claims, (str, bytes)) or not isinstance(claims, Sequence):
            raise _bad("ticket.claims must be a sequence")
        normalised = []
        for claim in claims:
            if not isinstance(claim, Mapping):
                raise _bad(f"ticket claim is not an object: {claim!r}")
            if "tick" not in claim:
                raise _bad("ticket claim requires a tick")
            level = claim.get("level")
            if level is not None and not isinstance(level, str):
                raise _bad("ticket claim level must be a string when present")
            normalised.append({"tick": claim["tick"], "level": level})
        return {"box_serial": box, "claims": normalised}

    @staticmethod
    def _tick_timestamps(product) -> dict[Any, Any]:
        """tick -> reported timestamp from the frozen reading identities."""
        return {
            reading["tick"]: reading.get("at", reading["tick"])
            for reading in product.get("readings", [])
        }

    def _replay(self, product, station, config, ops, ticket) -> dict:
        slots: list[str | None] = list(config["slots"])
        n = len(slots)
        used_serials = set(slots)
        pointer = 0
        cooling_until: dict[int, Any] = {}
        cooling_since: dict[int, Any] = {}
        fires = self._fires_by_tick(product, station["stations"])
        tick_at = self._tick_timestamps(product)

        # Cooling is expressed in whole ticks, so a numeric (integer) timeline
        # is required whenever any station actually cools.  Guard up front so a
        # string-tick series cannot silently fail mid-replay.
        if any(st["cooling"] > 0 for st in station["stations"]):
            for tick in set(fires):
                if isinstance(tick, bool) or not isinstance(tick, int):
                    raise _bad(
                        f"station cooling needs integer ticks, got {tick!r}"
                    )

        removals_by_tick: dict[Any, list[dict]] = {}
        for index, op in enumerate(ops):
            if not isinstance(op, Mapping):
                raise _bad(f"physical op is not an object: {op!r}")
            if op.get("op", "remove_and_replace") != "remove_and_replace":
                raise _bad(f"unknown physical op {op.get('op')!r}")
            if "tick" not in op:
                raise _bad("physical op requires a tick")
            if isinstance(op["tick"], bool) or not isinstance(op["tick"], (int, str)):
                raise _bad(f"physical op tick is not usable: {op['tick']!r}")
            removals_by_tick.setdefault(op["tick"], []).append({"index": index, **op})

        all_ticks = _sorted_ticks(set(fires) | set(removals_by_tick))
        loads: list[dict] = []
        skips: list[dict] = []
        removals: list[dict] = []
        loaded_serials: set[str] = set()

        for tick in all_ticks:
            # 1) Physical box exchange happens before any trigger at this tick.
            for op in removals_by_tick.get(tick, []):
                position = op.get("position")
                if isinstance(position, bool) or not isinstance(position, int) or not 0 <= position < n:
                    raise _ReplayConflict(
                        {
                            "reason": "position_out_of_range",
                            "tick": tick,
                            "position": position,
                            "slots_count": n,
                        }
                    )
                take = op.get("take")
                actual = slots[position]
                if take != actual:
                    # First physical mismatch: the recorded take does not match
                    # the box actually present -- never substitute another box.
                    raise _ReplayConflict(
                        {
                            "reason": "take_serial_mismatch",
                            "tick": tick,
                            "position": position,
                            "claimed_take": take,
                            "actual_serial": actual,
                        }
                    )
                replacement = op.get("replace")
                if not isinstance(replacement, str) or not replacement:
                    raise _ReplayConflict(
                        {
                            "reason": "missing_replacement_serial",
                            "tick": tick,
                            "position": position,
                        }
                    )
                if replacement in used_serials:
                    raise _ReplayConflict(
                        {
                            "reason": "replacement_serial_already_used",
                            "tick": tick,
                            "position": position,
                            "serial": replacement,
                        }
                    )
                used_serials.add(replacement)
                slots[position] = replacement
                removals.append(
                    {
                        "ordinal": len(removals),
                        "tick": tick,
                        "at": tick_at.get(tick, tick),
                        "position": position,
                        "box_serial": actual,
                        "box_slot": position,
                    }
                )

            # 2) Then triggers for this tick, in station order.
            for fire in fires.get(tick, []):
                st = fire["station"]
                pos = st["position"]
                duration = st["cooling"]
                until = cooling_until.get(pos)
                if until is not None and duration > 0 and tick < until:
                    for level, close_ordinal in zip(fire["levels"], fire["close_ordinals"]):
                        skips.append(
                            {
                                "ordinal": len(skips),
                                "tick": tick,
                                "at": tick_at.get(tick, tick),
                                "level": level,
                                "close_ordinal": close_ordinal,
                                "station": pos,
                                "cooling_since": cooling_since.get(pos),
                                "cooling_until": until,
                                "reason": "cooling",
                            }
                        )
                    continue
                box = slots[pointer]
                if box is None:  # defensive; slots are always serialised strings
                    raise _ReplayConflict(
                        {"reason": "load_into_empty_slot", "tick": tick, "position": pointer}
                    )
                if box in loaded_serials:
                    # A frozen product must never load the same physical box
                    # twice: a box holds one sample once, until it is taken and
                    # replaced.  Surface the first such contradiction.
                    raise _ReplayConflict(
                        {
                            "reason": "box_loaded_twice",
                            "tick": tick,
                            "box_serial": box,
                            "box_slot": pointer,
                        }
                    )
                loaded_serials.add(box)
                loads.append(
                    {
                        "ordinal": len(loads),
                        "tick": tick,
                        "at": tick_at.get(tick, tick),
                        "level": fire["levels"][0],
                        "levels": list(fire["levels"]),
                        "close_ordinal": fire["close_ordinals"][0],
                        "close_ordinals": list(fire["close_ordinals"]),
                        "station": pos,
                        "box_serial": box,
                        "box_slot": pointer,
                    }
                )
                if duration > 0:
                    cooling_since[pos] = tick
                    cooling_until[pos] = tick + duration
                else:
                    cooling_since.pop(pos, None)
                    cooling_until.pop(pos, None)
                pointer = (pointer + 1) % n

        verdict = self._match_ticket(ticket, loads, skips)
        return {
            "loads": loads,
            "skips": skips,
            "removals": removals,
            "final_slots": [{"position": i, "box_serial": s} for i, s in enumerate(slots)],
            "ticket_verdict": verdict,
        }

    @staticmethod
    def _match_ticket(ticket: dict, loads: list[dict], skips: list[dict]) -> dict:
        box = ticket["box_serial"]
        loads_by_tick = {load["tick"]: load for load in loads}
        skips_by_tick = {skip["tick"]: skip for skip in skips}
        claims = []
        all_ok = True
        for claim in ticket["claims"]:
            tick = claim["tick"]
            level = claim["level"]
            load = loads_by_tick.get(tick)
            if load is None:
                skip = skips_by_tick.get(tick)
                if skip is not None:
                    status = "skipped_due_to_cooling"
                    reason = (
                        f"tick {tick} trigger was dropped: mechanism still cooling "
                        f"until {skip['cooling_until']}"
                    )
                else:
                    status = "no_event_at_tick"
                    reason = f"no closed stress cycle triggers a load at tick {tick}"
                claims.append(
                    {"tick": tick, "level": level, "status": status,
                     "actual_box": None, "reason": reason}
                )
                all_ok = False
                continue
            if level is not None and level != load["level"]:
                claims.append(
                    {
                        "tick": tick,
                        "level": level,
                        "status": "level_mismatch",
                        "actual_box": load["box_serial"],
                        "reason": f"tick {tick} closed level {load['level']}, not {level}",
                    }
                )
                all_ok = False
                continue
            if box is not None and box != load["box_serial"]:
                claims.append(
                    {
                        "tick": tick,
                        "level": level,
                        "status": "wrong_box",
                        "actual_box": load["box_serial"],
                        "reason": (
                            f"tick {tick} sample went to box {load['box_serial']!r}, "
                            f"not the claimed box {box!r}"
                        ),
                    }
                )
                all_ok = False
                continue
            claims.append(
                {
                    "tick": tick,
                    "level": level,
                    "status": "matched",
                    "actual_box": load["box_serial"],
                    "reason": f"tick {tick} sample is in box {load['box_serial']!r}",
                }
            )
        overall = "NO_CLAIMS" if not ticket["claims"] else ("MATCH" if all_ok else "MISMATCH")
        return {"box_serial": box, "claims": claims, "overall": overall}

    # -- read-back -----------------------------------------------------------
    def get_reconciliation(self, reconciliation_id: str) -> dict:
        row = self.store.conn.execute(
            "SELECT * FROM reconciliations WHERE reconciliation_id=?", (reconciliation_id,)
        ).fetchone()
        if row is None:
            raise ReconciliationNotFound(reconciliation_id)
        return self._view(row, created=False)

    def _view(self, row, created: bool) -> dict:
        product = self.thermal.get_product(row["product_id"])
        station = self.station_log(row["station_version"])
        config = self.carousel_config(row["carousel_config"])
        ops = json.loads(row["ops_json"])
        ticket = json.loads(row["ticket_json"])
        request_body = {
            "product_id": row["product_id"],
            "product_digest": product["content_digest"],
            "station_version": row["station_version"],
            "station_digest": station["content_digest"],
            "carousel_config": row["carousel_config"],
            "carousel_digest": config["content_digest"],
            "ops": ops,
            "ticket": ticket,
        }

        # Independent re-derivation from the frozen inputs.  A stored
        # reconciliation was valid when published, so replay must succeed and
        # reproduce the stored projections exactly.  Tampered inputs that would
        # now contradict are surfaced as an integrity failure, not a new
        # "conflict" on read.
        try:
            replay = self._replay(product, station, config, ops, ticket)
        except _ReplayConflict as exc:
            raise IntegrityError(
                f"reconciliation {row['reconciliation_id']!r}: "
                f"stored inputs no longer replay cleanly: {exc.mismatch!r}"
            ) from None
        recomputed = digest({**request_body, **replay})
        if recomputed != row["content_digest"]:
            raise IntegrityError(
                f"reconciliation {row['reconciliation_id']!r}: content digest mismatch"
            )

        stored_loads = [
            {
                "ordinal": r["ordinal"],
                "tick": json.loads(r["tick"]),
                "at": json.loads(r["timestamp"]),
                "level": r["level"],
                "close_ordinal": r["close_ordinal"],
                "box_serial": r["box_serial"],
                "box_slot": r["box_slot"],
            }
            for r in self.store.conn.execute(
                "SELECT * FROM reconciliation_loads WHERE reconciliation_id=? ORDER BY ordinal",
                (row["reconciliation_id"],),
            ).fetchall()
        ]
        projected_loads = [
            {
                "ordinal": l["ordinal"],
                "tick": l["tick"],
                "at": l["at"],
                "level": l["level"],
                "close_ordinal": l["close_ordinal"],
                "box_serial": l["box_serial"],
                "box_slot": l["box_slot"],
            }
            for l in replay["loads"]
        ]
        if canonical_json(stored_loads) != canonical_json(projected_loads):
            raise IntegrityError(
                f"reconciliation {row['reconciliation_id']!r}: stored loads differ from replay"
            )

        return {
            "reconciliation_id": row["reconciliation_id"],
            "product_id": row["product_id"],
            "product_digest": product["content_digest"],
            "station_version": row["station_version"],
            "station_digest": station["content_digest"],
            "carousel_config": row["carousel_config"],
            "carousel_digest": config["content_digest"],
            "ops": ops,
            "ticket": ticket,
            "loads": replay["loads"],
            "skips": replay["skips"],
            "removals": replay["removals"],
            "final_slots": replay["final_slots"],
            "ticket_verdict": replay["ticket_verdict"],
            "content_digest": row["content_digest"],
            "created": created,
        }

    def attempts(self, reconciliation_id: str) -> list[dict]:
        rows = self.store.conn.execute(
            "SELECT attempt_id, status, reasons_json, mismatch_json, created_at "
            "FROM reconciliation_attempts WHERE reconciliation_id=? ORDER BY attempt_id",
            (reconciliation_id,),
        ).fetchall()
        return [
            {
                "attempt_id": row["attempt_id"],
                "status": row["status"],
                "reasons": json.loads(row["reasons_json"]),
                "mismatch": None if row["mismatch_json"] is None else json.loads(row["mismatch_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]
