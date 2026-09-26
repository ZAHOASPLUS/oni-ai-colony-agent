"""Cross-language, versioned hashing for security-sensitive JSON-shaped trees.

The byte contract is intentionally independent of JSON text rendering.  Python
and the in-game C# verifier both implement ``oni.typed-canonical-sha256.v1``.
See ``docs/canonical-hash-contract-v1.md`` for the complete grammar.
"""

from __future__ import annotations

import hashlib
import json
import math
import struct
from collections.abc import Mapping
from typing import Any


CANONICAL_HASH_CONTRACT = "oni.typed-canonical-sha256.v1"
_HASH_DOMAIN = CANONICAL_HASH_CONTRACT.encode("ascii") + b"\x00"
_MIN_INTEGER = -(1 << 63)
_MAX_INTEGER = (1 << 63) - 1


class CanonicalHashError(ValueError):
    """Raised when a value is outside the canonical tree contract."""


def _ascii_decimal(value: int) -> bytes:
    return str(value).encode("ascii")


def _utf8(value: str, *, path: str) -> bytes:
    try:
        return value.encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        raise CanonicalHashError(f"{path} contains an unpaired Unicode surrogate") from exc


def _append_string(output: bytearray, encoded: bytes) -> None:
    output.extend(b"s")
    output.extend(_ascii_decimal(len(encoded)))
    output.extend(b":")
    output.extend(encoded)


def _append_value(output: bytearray, value: Any, *, path: str) -> None:
    if value is None:
        output.extend(b"n")
        return

    if isinstance(value, bool):
        output.extend(b"b1" if value else b"b0")
        return

    if isinstance(value, int):
        if value < _MIN_INTEGER or value > _MAX_INTEGER:
            raise CanonicalHashError(f"{path} integer is outside signed 64-bit range")
        output.extend(b"i")
        output.extend(_ascii_decimal(value))
        output.extend(b";")
        return

    if isinstance(value, float):
        if not math.isfinite(value):
            raise CanonicalHashError(f"{path} contains a non-finite float")
        output.extend(b"f")
        output.extend(struct.pack(">d", value).hex().encode("ascii"))
        output.extend(b";")
        return

    if isinstance(value, str):
        _append_string(output, _utf8(value, path=path))
        return

    if isinstance(value, list):
        output.extend(b"a")
        output.extend(_ascii_decimal(len(value)))
        output.extend(b":")
        for index, item in enumerate(value):
            _append_value(output, item, path=f"{path}[{index}]")
        return

    if isinstance(value, Mapping):
        properties: list[tuple[bytes, str, Any]] = []
        seen: set[str] = set()
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalHashError(f"{path} contains a non-string object key")
            if key in seen:
                raise CanonicalHashError(f"{path} contains duplicate object key {key!r}")
            seen.add(key)
            properties.append((_utf8(key, path=f"{path} object key"), key, item))

        properties.sort(key=lambda property_: property_[0])
        output.extend(b"o")
        output.extend(_ascii_decimal(len(properties)))
        output.extend(b":")
        for key_bytes, key, item in properties:
            _append_string(output, key_bytes)
            _append_value(output, item, path=f"{path}.{key}")
        return

    raise CanonicalHashError(
        f"{path} has unsupported canonical type {type(value).__name__}"
    )


def canonical_bytes(value: Any) -> bytes:
    """Return the exact versioned byte stream covered by the SHA-256 digest."""

    output = bytearray(_HASH_DOMAIN)
    _append_value(output, value, path="$")
    return bytes(output)


def canonical_hash(value: Any) -> str:
    """Hash one canonical tree while preserving the existing sha256: envelope."""

    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def _reject_constant(value: str) -> Any:
    raise CanonicalHashError(f"JSON contains forbidden non-finite constant {value}")


def _object_from_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise CanonicalHashError(f"JSON contains duplicate object key {key!r}")
        value[key] = item
    return value


def loads_strict(source: str) -> Any:
    """Parse JSON without collapsing duplicate names or accepting NaN/Infinity."""

    try:
        value = json.loads(
            source,
            object_pairs_hook=_object_from_pairs,
            parse_constant=_reject_constant,
        )
    except CanonicalHashError:
        raise
    except (TypeError, ValueError, OverflowError, json.JSONDecodeError) as exc:
        raise CanonicalHashError("input is not strict canonical-tree JSON") from exc

    # Parsing can produce infinity from a finite-looking exponent such as 1e9999,
    # or preserve an unpaired surrogate.  The encoder is the final type gate.
    canonical_bytes(value)
    return value


__all__ = [
    "CANONICAL_HASH_CONTRACT",
    "CanonicalHashError",
    "canonical_bytes",
    "canonical_hash",
    "loads_strict",
]
