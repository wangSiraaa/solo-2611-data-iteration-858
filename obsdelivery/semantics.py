"""Binary32 interpretation and reference judging (pure, independent of storage).

Bit layout (the task's 32-bit format)::

    bit 31        sign
    bits 30..23   exponent (8 bits)
    bits 22..0    fraction (23 bits)

    exponent == 0 : fraction == 0 -> signed zero (value 0, sign preserved)
                    otherwise    -> (-1)^sign * fraction * 2**-149
    1 <= exponent <= 254        -> (-1)^sign * (2**23 + fraction) * 2**(exponent-150)
    exponent == 255 : fraction == 0 -> signed infinity
                      otherwise    -> NaN (23-bit payload = fraction bits)

No host floating point is involved: the exact value is a ``Fraction``.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Iterable, Mapping, Sequence

from .canonical import digest as _digest, frac_str as _frac_str

FINITE = "FINITE"
ZERO = "ZERO"
INFINITY = "INFINITY"
NAN = "NAN"
NONFINITE_KINDS = (INFINITY, NAN)

PASS = "PASS"
OUTSIDE = "OUTSIDE"
NONFINITE = "NONFINITE"
MISSING = "MISSING"

_BITS_RE = frozenset("0123456789abcdefABCDEF")


def parse_bits(value: Any) -> int:
    """Validate an 8-hex-digit unsigned 32-bit original and return it."""
    if not isinstance(value, str):
        raise ValueError(f"bits must be an 8-hex-digit string, got {value!r}")
    if len(value) != 8 or not set(value) <= _BITS_RE:
        raise ValueError(f"bits must be exactly 8 hex digits, got {value!r}")
    return int(value, 16)


def format_bits(raw: int) -> str:
    return f"{raw:08x}"


@dataclass(frozen=True)
class Decoded:
    raw: int
    bits: str
    raw_sign: int
    exponent: int
    fraction: int
    kind: str  # FINITE | ZERO | INFINITY | NAN
    sign: int
    payload: int | None  # NaN fraction payload, else None
    value: Fraction | None  # exact value, None iff non-finite

    @property
    def finite(self) -> bool:
        return self.value is not None

    def canonical(self) -> dict[str, Any]:
        """Immutable canonical observation fields (sign-inclusive)."""
        # ~x is the exact value with the sign bit flipped, including signed
        # zero: ~+0 is -0 (which is what makes negative zero representable).
        negated = None if self.value is None else -self.value
        if self.kind == NAN:
            return {
                "kind": NAN,
                "bits": self.bits,
                "sign": self.sign,
                "payload": self.payload,
                "bits_negated": False,
                "value": None,
                "negated_value": None,
            }
        return {
            "kind": self.kind,
            "bits": self.bits,
            "sign": self.sign,
            "payload": None,
            "bits_negated": self.sign == 1,
            "value": self.value,
            "negated_value": negated,
        }


def decode(raw: int) -> Decoded:
    sign = (raw >> 31) & 1
    exponent = (raw >> 23) & 0xFF
    fraction = raw & 0x7FFFFF
    bits = format_bits(raw)
    if exponent == 0:
        if fraction == 0:
            return Decoded(raw, bits, sign, exponent, fraction, ZERO, sign, None, Fraction(0))
        value = Fraction(fraction * (-1 if sign else 1), 1 << 149)
        return Decoded(raw, bits, sign, exponent, fraction, FINITE, sign, None, value)
    if exponent == 255:
        if fraction == 0:
            return Decoded(raw, bits, sign, exponent, fraction, INFINITY, sign, None, None)
        return Decoded(raw, bits, sign, exponent, fraction, NAN, sign, fraction, None)
    mantissa = (1 << 23) | fraction
    scale = exponent - 150
    magnitude = Fraction(mantissa * (1 << scale), 1) if scale >= 0 else Fraction(mantissa, 1 << -scale)
    value = -magnitude if sign else magnitude
    return Decoded(raw, bits, sign, exponent, fraction, FINITE, sign, None, value)


def interpret(bits: Any) -> dict[str, Any]:
    """Public semantic interpretation of an original bit pattern."""
    decoded = decode(parse_bits(bits))
    result: dict[str, Any] = {
        "bits": decoded.bits,
        "kind": decoded.kind,
        "finite": decoded.finite,
        "sign": decoded.sign,
        "exponent": decoded.exponent,
        "fraction": decoded.fraction,
        "payload": decoded.payload,
    }
    if decoded.finite:
        result["value"] = decoded.value
        result["value_text"] = _frac_text(decoded.value)
        result["zero_sign"] = "negative" if (decoded.kind == ZERO and decoded.sign) else (
            "positive" if decoded.kind == ZERO else None
        )
    else:
        result["value"] = None
        result["value_text"] = None
        result["zero_sign"] = None
    return result


def _frac_text(value: Fraction) -> str:
    if value.denominator == 1:
        return str(value.numerator)
    return f"{value.numerator}/{value.denominator}"


def judge(decoded: Decoded, expected: Fraction, tolerance: Fraction) -> tuple[str, Fraction | None]:
    """Reference judgement on the ORIGINAL exact value.

    Returns ``(status, exact_abs_diff_or_None)``.  Non-finite values never
    compare numerically, so a NaN/Inf with an arbitrary reference is
    NONFINITE; a missing observation is MISSING and is handled by the caller.
    """
    if tolerance < 0:
        raise ValueError("tolerance must be non-negative")
    if not decoded.finite:
        return NONFINITE, None
    delta = decoded.value - expected
    distance = abs(delta)
    return (PASS if distance <= tolerance else OUTSIDE), distance


# ===========================================================================
# Thermal stress-cycle closure
# ===========================================================================
#
# A thermal bench reads one sample per ordered tick.  Device catalogs declare a
# (low, high) threshold pair per stress level, low < high.  Every level starts
# cold, becomes hot once the reading reaches the HIGH threshold, and then
# closes one cycle on the *first* reading that reaches the LOW threshold,
# returning to cold.  Readings strictly between the two thresholds leave the
# level's state unchanged.  The closing reading is part of the cycle.
#
# Numeric decisions use the exact ``Fraction`` value of the original bit
# pattern -- never a rounded delivery value.  A non-finite reading (NaN or +/-Inf)
# makes the whole reading unusable: the caller must reject and keep any prior
# product.  ``None`` readings (no sample this tick) are simply skipped.
#
# Segments carry a boundary marker.  ``continuation`` carries the unclosed hot
# state of every level across a washout boundary; ``reload_reset`` starts every
# level cold.  Closure detection therefore happens per level over the whole
# sequence, not per independent run of readings.

THERMAL_LOW = "low"
THERMAL_HIGH = "high"
CONTINUATION = "continuation"
RELOAD_RESET = "reload_reset"
_VALID_MARKERS = (CONTINUATION, RELOAD_RESET)


@dataclass(frozen=True)
class ThermalLevel:
    name: str
    low: Fraction
    high: Fraction

    def canonical(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "low": _frac_str(self.low),
            "high": _frac_str(self.high),
        }


@dataclass(frozen=True)
class ThermalReading:
    tick: Any
    at: Any
    bits: str | None
    value: Fraction | None  # None iff ``bits is None`` (no sample)
    nonfinite_kind: str | None  # NONFINITE if bits present but not finite

    @property
    def present(self) -> bool:
        return self.bits is not None

    @property
    def usable(self) -> bool:
        return self.bits is not None and self.nonfinite_kind is None

    def canonical(self) -> dict[str, Any]:
        return {
            "tick": self.tick,
            "at": self.at,
            "bits": self.bits,
            "value": None if self.value is None else _frac_str(self.value),
            "nonfinite": self.nonfinite_kind is not None,
        }

    def identity(self) -> dict[str, Any]:
        # Bits-inclusive identity, so a correction changes the definition digest.
        return {"tick": self.tick, "at": self.at, "bits": self.bits}


@dataclass(frozen=True)
class ThermalSegment:
    tick: Any
    marker: str

    def canonical(self) -> dict[str, Any]:
        return {"tick": self.tick, "marker": self.marker}


def _as_threshold(text: Any, field: str) -> Fraction:
    from .canonical import parse_fraction

    return parse_fraction(text, field=field)


def thermal_catalog(levels: Iterable[Mapping[str, Any]]) -> tuple[ThermalLevel, ...]:
    parsed: list[ThermalLevel] = []
    seen: set[str] = set()
    for entry in levels:
        if not isinstance(entry, Mapping):
            raise ValueError(f"level is not an object: {entry!r}")
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"level name must be a non-empty string, got {name!r}")
        if name in seen:
            raise ValueError(f"duplicate level {name!r}")
        seen.add(name)
        low = _as_threshold(entry.get("low"), f"{name}.low")
        high = _as_threshold(entry.get("high"), f"{name}.high")
        if not low < high:
            raise ValueError(f"level {name!r} requires low < high, got {low} / {high}")
        parsed.append(ThermalLevel(name, low, high))
    if not parsed:
        raise ValueError("thermal catalog must declare at least one level")
    return tuple(sorted(parsed, key=lambda level: level.name))


def _thermal_identity(levels: Sequence[ThermalLevel]) -> dict[str, Any]:
    return {"kind": "thermal_catalog", "levels": [level.canonical() for level in levels]}


def thermal_catalog_digest(levels: Sequence[ThermalLevel]) -> str:
    return _digest(_thermal_identity(levels))


def thermal_readings(
    records: Iterable[Mapping[str, Any]],
) -> list[ThermalReading]:
    """Validate and order raw readings by strict ``tick``.

    Duplicate ticks are rejected: the bench guarantees a strict total order.
    ``bits`` may be ``None`` (no sample this tick); otherwise the eight hex
    digits are validated and interpreted exactly.
    """
    parsed: list[ThermalReading] = []
    seen: set[Any] = set()
    for record in records:
        if not isinstance(record, Mapping):
            raise ValueError(f"reading is not an object: {record!r}")
        tick = record.get("tick")
        if tick is None:
            raise ValueError("reading.tick is required")
        if tick in seen:
            raise ValueError(f"duplicate tick {tick!r}")
        seen.add(tick)
        at = record.get("at", tick)
        bits = record.get("bits")
        if bits is None:
            parsed.append(ThermalReading(tick, at, None, None, None))
            continue
        raw = parse_bits(bits)
        decoded = decode(raw)
        parsed.append(
            ThermalReading(
                tick,
                at,
                f"{raw:08x}",
                decoded.value,
                None if decoded.finite else NONFINITE,
            )
        )
    try:
        parsed.sort(key=lambda reading: reading.tick)
    except TypeError as exc:  # pragma: no cover - mixed tick types
        raise ValueError(f"ticks are not mutually orderable: {exc}") from exc
    return parsed


@dataclass
class _LevelState:
    phase: str = "cold"  # cold | hot
    open_tick: Any = None
    open_at: Any = None
    open_bits: str | None = None


def compute_closures(
    levels: Sequence[ThermalLevel],
    readings: Sequence[ThermalReading],
    segments: Sequence[ThermalSegment],
) -> list[dict[str, Any]]:
    """Detect every closed stress cycle, in strict reading order.

    A level warms up on the first reading at/above its high threshold (that
    reading is the *heat-up original*) and closes on the first subsequent
    reading at/below its low threshold (that reading is the *cool-down
    original*).  Multiple levels may close on the same reading; closures are
    emitted ordered by ``(close tick position, level name)``, which keeps the
    result deterministic for downstream physical replay.
    """
    markers = {segment.tick: segment.marker for segment in segments}
    pending_boundary: str | None = markers.get(readings[0].tick) if readings else None
    states = {level.name: _LevelState() for level in levels}
    closures: list[dict[str, Any]] = []

    for reading in readings:
        boundary = markers.get(reading.tick)
        if boundary is not None and boundary != pending_boundary:
            pending_boundary = boundary
        if pending_boundary is not None:
            if pending_boundary == RELOAD_RESET:
                states = {level.name: _LevelState() for level in levels}
            # CONTINUATION: unclosed hot state (with its warm-up original) is
            # carried across the boundary unchanged.
            pending_boundary = None

        if not reading.usable:
            continue
        value = reading.value
        for level in levels:  # sorted by name -> deterministic same-tick order
            state = states[level.name]
            if state.phase == "cold":
                if value >= level.high:
                    state.phase = "hot"
                    state.open_tick = reading.tick
                    state.open_at = reading.at
                    state.open_bits = reading.bits
                continue
            if value <= level.low:
                closures.append(
                    {
                        "ordinal": len(closures),
                        "level": level.name,
                        "close_tick": reading.tick,
                        "close_at": reading.at,
                        "close_bits": reading.bits,
                        "open_tick": state.open_tick,
                        "open_at": state.open_at,
                        "open_bits": state.open_bits,
                        # warm-up original == the heat-up trigger
                        "warm_tick": state.open_tick,
                        "warm_at": state.open_at,
                        "warm_bits": state.open_bits,
                        # cool-down original == the closing trigger
                        "cool_tick": reading.tick,
                        "cool_at": reading.at,
                        "cool_bits": reading.bits,
                    }
                )
                state.phase = "cold"
                state.open_tick = None
                state.open_at = None
                state.open_bits = None
    return closures

