"""Small, offline controller core for observation and bounded-action replay.

This module deliberately stops at an injected adapter boundary.  It does not
know about the game, Bridge, command queues, saves, Mods, or live admission.
The adapter used by tests is an offline fixture; a caller still has to provide
its own separately authorized integration if one is ever needed.
"""

from __future__ import annotations

import datetime as dt
import enum
import hashlib
import json
import math
import os
import pathlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Callable, Optional, Protocol, Sequence, Tuple

from agent.field_contracts import (
    DUPLICANT_CHORE_PRIORITY_CONTRACT_ID,
    GAME_PAUSED_CONTRACT_ID,
    condition_contract_blockers,
    condition_declaration_blockers,
    read_contract_field,
)
from agent.advanced_actions import build_duplicant_priority_hint
from agent.state_quality import (
    canonical_state_hash,
    parse_snapshot_timestamp,
    quality_contract_blockers,
    snapshot_trust_blockers,
)


LIVE_ADMISSION = False
CONTROLLER_SCHEMA = "oni.controller-core.v1"
ACTION_PROPOSAL_SCHEMA = "oni.controller-action-proposal.v1"
RECOVERY_SCHEMA = "oni.controller-recovery.v1"

# Probe emits the complete state envelope.  Archived full snapshots measured
# 605–650 KiB, so the offline gate keeps a finite 1 MiB bound with room for
# normal telemetry growth while still rejecting unbounded payloads.  Evidence
# is never trimmed to make a snapshot fit this limit.
MAX_STATE_BYTES = 1024 * 1024
MAX_INTENT_RECEIPT_BYTES = 2 * 1024 * 1024
MAX_ACTION_ARGS_BYTES = 8 * 1024
# Derived from the advanced-action catalog, not chosen for convenience.  The
# largest catalogued leaf is a 64-cell Wire path: each placement target carries
# 10 preconditions and 15 expected effects on top of a few fixed environment
# and catalog conditions (~1,000 in total).  A dig batch is 32 cells.  Keep the
# bound finite with headroom; test_controller_loop checks that every builder's
# maximal hint still fits.
MAX_CONDITIONS = 2048
MAX_ACTION_ATTEMPTS = 3
# Retained VPS controller admission bound, not a claim about the game's limit.
MAX_CYCLE_VALUE = 1_000_000_000
_ACTION_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_PATH_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
_MISSING = object()


class Freshness(str, enum.Enum):
    """Freshness declared by the observation producer."""

    FRESH = "fresh"
    STALE = "stale"
    UNKNOWN = "unknown"


class ObservationQuality(str, enum.Enum):
    """Safety quality of an observation's decision surface."""

    SAFE = "safe"
    PARTIAL = "partial"
    UNSAFE = "unsafe"
    UNKNOWN = "unknown"


class EffectStatus(str, enum.Enum):
    """Status reported by the injected adapter's effect observation."""

    APPLIED = "applied"
    PENDING = "pending"
    MISMATCH = "mismatch"
    TIMEOUT = "timeout"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class ReconciliationStatus(str, enum.Enum):
    CONFIRMED = "confirmed"
    RETRYABLE = "retryable"
    BLOCKED = "blocked"


# Short aliases make the public records pleasant to use without duplicating
# status vocabularies.
Quality = ObservationQuality
OutcomeStatus = ReconciliationStatus


def _freeze(value: Any) -> Any:
    """Recursively freeze JSON-like data held by a frozen record."""

    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(item) for item in value)
    return value


def _plain(value: Any) -> Any:
    """Return JSON-shaped mutable data for hashing and evidence rendering."""

    if isinstance(value, Mapping):
        return {_plain(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_plain(item) for item in value), key=repr)
    if isinstance(value, (Freshness, ObservationQuality, EffectStatus, ReconciliationStatus)):
        return value.value
    return value


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        _plain(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _reject_json_constant(value: str) -> Any:
    """Reject non-finite JSON constants when reading durable receipts."""
    raise ValueError(f"non-finite JSON constant: {value}")


def _finite_tree(value: Any) -> bool:
    if isinstance(value, bool) or value is None or isinstance(value, (str, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, Mapping):
        return all(isinstance(key, str) and _finite_tree(item) for key, item in value.items())
    if isinstance(value, (list, tuple, set, frozenset)):
        return all(_finite_tree(item) for item in value)
    return False


def _strict_equal(actual: Any, expected: Any) -> bool:
    """Compare JSON values without Python's nested boolean/number coercion."""
    if isinstance(actual, bool) or isinstance(expected, bool):
        return type(actual) is type(expected) and actual == expected
    if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
        return actual == expected
    if isinstance(actual, Mapping) and isinstance(expected, Mapping):
        return actual.keys() == expected.keys() and all(
            _strict_equal(actual[key], expected[key]) for key in actual
        )
    if isinstance(actual, list) and isinstance(expected, list):
        return len(actual) == len(expected) and all(
            _strict_equal(left, right) for left, right in zip(actual, expected)
        )
    return type(actual) is type(expected) and actual == expected


def _strict_contains(container: Any, item: Any) -> bool:
    if isinstance(container, list):
        return any(_strict_equal(value, item) for value in container)
    # Preserve string/key membership while refusing implicit numeric coercion.
    if isinstance(container, (str, Mapping)):
        if isinstance(item, str):
            return item in container
        raise TypeError("string/key membership requires a string")
    raise TypeError("membership requires an array, string, or object")


def _valid_cycle_scalar(value: Any, *, continuous: bool) -> bool:
    if type(value) not in (int, float):
        return False
    if not continuous and type(value) is not int:
        return False
    return (
        (not isinstance(value, float) or math.isfinite(value))
        and (0 if continuous else 1) <= value <= MAX_CYCLE_VALUE
    )


def _cycle_metadata_blockers(state: Mapping[str, Any]) -> list[str]:
    """Validate clock fields when a generic effect carries clock evidence."""
    plain = _plain(state)
    blockers: list[str] = []
    values: dict[str, int | float] = {}
    for name, continuous in (("cycle", False), ("game_time_cycles", True)):
        if name in plain:
            if not _valid_cycle_scalar(plain[name], continuous=continuous):
                blockers.append(name + "_invalid")
            else:
                values[name] = plain[name]
    if len(values) == 2 and not values["cycle"] <= values["game_time_cycles"] < values["cycle"] + 1:
        blockers.append("cycle_metadata_mismatch")
    quality = plain.get("quality")
    if "quality" in plain and not isinstance(quality, dict):
        return blockers + ["quality_metadata_invalid"]
    for name in values.keys() | ({"cycle", "game_time_cycles"} if isinstance(quality, dict) else set()):
        required = name == "game_time_cycles" or plain.get("schema") == "oni.state.v2"
        if not isinstance(quality, dict):
            if required:
                blockers.append(name + "_quality:missing")
            continue
        if name == "cycle":
            errors = quality_contract_blockers(
                quality.get(name), semantics=("display_1_based",),
                sources=("GameClock.GetCycle",), completeness=None, captures=None,
            )
        else:
            errors = quality_contract_blockers(
                quality.get(name), semantics=("display_1_based_continuous",),
                units=("cycle",), scopes=("save",), completeness=("complete",),
                sources=("GameClock.GetTimeInCycles",), captures=("snapshot.ts",),
            )
        blockers.extend(name + "_quality:" + item for item in errors)
    return sorted(set(blockers))


def _enum_value(enum_type: type[enum.Enum], value: Any, fallback: enum.Enum) -> enum.Enum:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except (TypeError, ValueError):
        return fallback


def _unique_strings(values: Sequence[Any]) -> Tuple[str, ...]:
    result = []
    for value in values:
        text = str(value)
        if text not in result:
            result.append(text)
    return tuple(result)


@dataclass(frozen=True, slots=True)
class Observation:
    """One producer observation with explicit freshness and quality."""

    observation_id: str
    freshness: Freshness
    quality: ObservationQuality
    payload: Mapping[str, Any] = field(default_factory=dict)
    source: str = "offline-fixture"
    observed_at: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "freshness",
            _enum_value(Freshness, self.freshness, Freshness.UNKNOWN),
        )
        object.__setattr__(
            self,
            "quality",
            _enum_value(ObservationQuality, self.quality, ObservationQuality.UNKNOWN),
        )
        object.__setattr__(
            self,
            "observation_id",
            self.observation_id if isinstance(self.observation_id, str) else "",
        )
        object.__setattr__(
            self,
            "payload",
            _freeze(self.payload) if isinstance(self.payload, Mapping) else MappingProxyType({}),
        )
        object.__setattr__(
            self,
            "source",
            self.source if isinstance(self.source, str) and self.source.strip() else "unknown",
        )
        if self.observed_at is not None and not isinstance(self.observed_at, str):
            object.__setattr__(self, "observed_at", None)

    @property
    def state(self) -> Mapping[str, Any]:
        """Compatibility name for callers that call the payload a state."""

        return self.payload

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "Observation":
        payload = value.get("payload", value.get("state", {}))
        return cls(
            observation_id=value.get("observation_id", value.get("id", "")),
            freshness=value.get("freshness", Freshness.UNKNOWN),
            quality=value.get("quality", ObservationQuality.UNKNOWN),
            payload=payload,
            source=value.get("source", "offline-fixture"),
            observed_at=value.get("observed_at"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": CONTROLLER_SCHEMA,
            "observation_id": self.observation_id,
            "freshness": self.freshness.value,
            "quality": self.quality.value,
            "payload": _plain(self.payload),
            "source": self.source,
            "observed_at": self.observed_at,
        }


@dataclass(frozen=True, slots=True)
class NormalizedState:
    """Immutable state used by the action decision gate."""

    observation_id: str
    state_hash: str
    state: Mapping[str, Any]
    freshness: Freshness
    quality: ObservationQuality
    blockers: Tuple[str, ...] = ()
    live_admission: bool = LIVE_ADMISSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", _freeze(self.state) if isinstance(self.state, Mapping) else MappingProxyType({}))
        object.__setattr__(self, "freshness", _enum_value(Freshness, self.freshness, Freshness.UNKNOWN))
        object.__setattr__(self, "quality", _enum_value(ObservationQuality, self.quality, ObservationQuality.UNKNOWN))
        object.__setattr__(self, "blockers", _unique_strings(self.blockers))
        object.__setattr__(self, "live_admission", False)

    @property
    def eligible(self) -> bool:
        return not self.blockers and self.freshness is Freshness.FRESH and self.quality is ObservationQuality.SAFE

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": CONTROLLER_SCHEMA,
            "observation_id": self.observation_id,
            "state_hash": self.state_hash,
            "state": _plain(self.state),
            "freshness": self.freshness.value,
            "quality": self.quality.value,
            "blockers": list(self.blockers),
            "live_admission": False,
        }


@dataclass(frozen=True, slots=True)
class Condition:
    """A small equality or comparison condition over a normalized state."""

    path: str
    op: str = "eq"
    value: Any = None
    quality: Tuple[str, ...] = ("observed",)
    quality_contract_id: Optional[str] = None
    has_value: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", self.path if isinstance(self.path, str) else "")
        op = self.op if isinstance(self.op, str) else ""
        # "ne" is the project-wide spelling (field contracts, Colony Plan,
        # command queue, and the C# executor).  Accept the older controller
        # spelling as an input alias only, so emitted conditions always use
        # the vocabulary every other layer validates.
        object.__setattr__(self, "op", {"==": "eq", "!=": "ne", "neq": "ne"}.get(op, op))
        qualities = self.quality if isinstance(self.quality, (list, tuple)) else ()
        object.__setattr__(self, "quality", _unique_strings(qualities))
        object.__setattr__(self, "quality_contract_id", self.quality_contract_id if isinstance(self.quality_contract_id, str) and self.quality_contract_id else None)
        object.__setattr__(self, "has_value", bool(self.has_value))
        object.__setattr__(self, "value", _freeze(self.value))

    @classmethod
    def from_value(cls, value: Any) -> "Condition":
        if isinstance(value, Condition):
            return value
        if not isinstance(value, Mapping):
            return cls("", "", None, (), None, False)
        qualities = value.get("quality", ("observed",))
        if isinstance(qualities, str):
            qualities = (qualities,)
        return cls(
            path=value.get("path", ""),
            op=value.get("op", "eq"),
            value=value.get("value"),
            quality=qualities,
            quality_contract_id=value.get("quality_contract_id"),
            has_value="value" in value,
        )

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "path": self.path,
            "op": self.op,
        }
        if self.has_value:
            result["value"] = _plain(self.value)
        if self.quality_contract_id is not None:
            result["quality"] = list(self.quality)
            result["quality_contract_id"] = self.quality_contract_id
        return result

    def declaration_blockers(self, *, action: Optional[str] = None) -> list[str]:
        blockers: list[str] = []
        if not _PATH_RE.fullmatch(self.path):
            blockers.append("condition_path_invalid")
        if self.op not in {"eq", "ne", "lt", "lte", "gt", "gte", "contains", "in"}:
            blockers.append("condition_operator_invalid")
        if not self.has_value:
            blockers.append("condition_value_missing")
        elif not _finite_tree(self.value):
            blockers.append("condition_value_non_finite_or_non_json")
        else:
            try:
                _json_bytes(self.value)
            except (TypeError, ValueError, OverflowError):
                blockers.append("condition_value_not_canonicalizable")
        if self.quality_contract_id is not None:
            blockers.extend(condition_declaration_blockers(self.as_dict(), action=action))
        return sorted(set(blockers))

    def blockers_for(self, state: Mapping[str, Any], *, action: Optional[str] = None) -> list[str]:
        blockers = self.declaration_blockers(action=action)
        if blockers:
            return blockers
        plain_state = _plain(state)
        if not isinstance(plain_state, Mapping):
            return ["state_missing"]
        if self.quality_contract_id is not None:
            blockers.extend(condition_contract_blockers(plain_state, self.as_dict(), action=action))
            if blockers:
                return sorted(set(blockers))
        actual = _get_path(plain_state, self.path)
        if actual is _MISSING:
            return [f"field_missing:{self.path}"]
        expected = _plain(self.value)
        # Evaluate only the selected operator.  Building a dictionary of all
        # comparisons eagerly evaluates membership and ordering operations even
        # for a boolean equality, turning a valid ``actual == expected`` into
        # a spurious type mismatch.
        try:
            if self.op == "eq":
                matches = _strict_equal(actual, expected)
            elif self.op == "ne":
                matches = not _strict_equal(actual, expected)
            elif self.op == "lt":
                matches = actual < expected
            elif self.op == "lte":
                matches = actual <= expected
            elif self.op == "gt":
                matches = actual > expected
            elif self.op == "gte":
                matches = actual >= expected
            elif self.op == "contains":
                matches = _strict_contains(actual, expected)
            elif self.op == "in":
                matches = _strict_contains(expected, actual)
            else:  # declaration_blockers normally catches this first
                return ["condition_operator_invalid"]
        except (TypeError, ValueError):
            return [f"condition_value_mismatch:{self.path}"]
        return [] if matches else [f"condition_not_met:{self.path}"]


Precondition = Condition
ExpectedEffect = Condition


def _coerce_conditions(values: Any) -> Tuple[Condition, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        return (Condition.from_value(values),)
    return tuple(Condition.from_value(item) for item in values)


@dataclass(frozen=True, slots=True)
class ActionPolicy:
    """Static bounded action shape, optionally created from an existing hint."""

    action: str
    args: Mapping[str, Any] = field(default_factory=dict)
    preconditions: Tuple[Condition, ...] = ()
    expected_effects: Tuple[Condition, ...] = ()
    max_attempts: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "args", _freeze(self.args))
        object.__setattr__(self, "preconditions", _coerce_conditions(self.preconditions))
        object.__setattr__(self, "expected_effects", _coerce_conditions(self.expected_effects))

    @classmethod
    def from_hint(cls, hint: Mapping[str, Any], *, max_attempts: int = 1) -> "ActionPolicy":
        return cls(
            action=hint.get("action", "") if isinstance(hint, Mapping) else "",
            args=hint.get("args", {}) if isinstance(hint, Mapping) else {},
            preconditions=hint.get("preconditions", ()) if isinstance(hint, Mapping) else (),
            expected_effects=hint.get("expected_effects", ()) if isinstance(hint, Mapping) else (),
            max_attempts=max_attempts,
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ActionPolicy":
        if not isinstance(value, Mapping):
            return cls("", {}, (), (), 0)
        return cls(
            action=value.get("action", ""),
            args=value.get("args", {}),
            preconditions=value.get("preconditions", ()),
            expected_effects=value.get("expected_effects", ()),
            max_attempts=value.get("max_attempts", 1),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "args": _plain(self.args),
            "preconditions": [item.as_dict() for item in self.preconditions],
            "expected_effects": [item.as_dict() for item in self.expected_effects],
            "max_attempts": self.max_attempts,
        }


@dataclass(frozen=True, slots=True)
class ActionProposal:
    """One immutable, bounded proposal with a stable idempotency key."""

    action: str
    args: Mapping[str, Any]
    preconditions: Tuple[Condition, ...]
    expected_effects: Tuple[Condition, ...]
    idempotency_key: str
    source_observation_id: str
    state_hash: str
    attempt: int = 1
    max_attempts: int = 1
    source_snapshot: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "args", _freeze(self.args))
        object.__setattr__(self, "preconditions", _coerce_conditions(self.preconditions))
        object.__setattr__(self, "expected_effects", _coerce_conditions(self.expected_effects))
        object.__setattr__(self, "source_snapshot", _freeze(self.source_snapshot))

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": ACTION_PROPOSAL_SCHEMA,
            "action": self.action,
            "args": _plain(self.args),
            "preconditions": [item.as_dict() for item in self.preconditions],
            "expected_effects": [item.as_dict() for item in self.expected_effects],
            "idempotency_key": self.idempotency_key,
            "source_observation_id": self.source_observation_id,
            "state_hash": self.state_hash,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "source_snapshot": _plain(self.source_snapshot),
            "live_admission": False,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ActionProposal":
        """Rebuild one previously persisted proposal without dispatching it."""
        if not isinstance(value, Mapping):
            raise ValueError("proposal_mapping_required")
        return cls(
            action=value.get("action", ""),
            args=value.get("args", {}),
            preconditions=value.get("preconditions", ()),
            expected_effects=value.get("expected_effects", ()),
            idempotency_key=value.get("idempotency_key", ""),
            source_observation_id=value.get("source_observation_id", ""),
            state_hash=value.get("state_hash", ""),
            attempt=value.get("attempt", 1),
            max_attempts=value.get("max_attempts", 1),
            source_snapshot=value.get("source_snapshot", {}),
        )


@dataclass(frozen=True, slots=True)
class ActionDecision:
    proposal: Optional[ActionProposal]
    reason: str
    blockers: Tuple[str, ...] = ()

    @property
    def action_count(self) -> int:
        return 1 if self.proposal is not None else 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "proposal": self.proposal.as_dict() if self.proposal is not None else None,
            "reason": self.reason,
            "blockers": list(self.blockers),
            "action_count": self.action_count,
        }


@dataclass(frozen=True, slots=True)
class EffectObservation:
    """Effect telemetry returned by the injected adapter."""

    idempotency_key: str
    status: EffectStatus
    freshness: Freshness
    quality: ObservationQuality
    payload: Mapping[str, Any] = field(default_factory=dict)
    effect_id: str = ""
    source: str = "offline-fixture"

    def __post_init__(self) -> None:
        object.__setattr__(self, "idempotency_key", self.idempotency_key if isinstance(self.idempotency_key, str) else "")
        object.__setattr__(self, "status", _enum_value(EffectStatus, self.status, EffectStatus.UNKNOWN))
        object.__setattr__(self, "freshness", _enum_value(Freshness, self.freshness, Freshness.UNKNOWN))
        object.__setattr__(self, "quality", _enum_value(ObservationQuality, self.quality, ObservationQuality.UNKNOWN))
        object.__setattr__(self, "payload", _freeze(self.payload))
        object.__setattr__(self, "effect_id", self.effect_id if isinstance(self.effect_id, str) else "")
        object.__setattr__(self, "source", self.source if isinstance(self.source, str) and self.source.strip() else "unknown")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "EffectObservation":
        payload = value.get("payload", value.get("state", {}))
        return cls(
            idempotency_key=value.get("idempotency_key", value.get("action_key", "")),
            status=value.get("status", EffectStatus.UNKNOWN),
            freshness=value.get("freshness", Freshness.UNKNOWN),
            quality=value.get("quality", ObservationQuality.UNKNOWN),
            payload=payload,
            effect_id=value.get("effect_id", ""),
            source=value.get("source", "offline-fixture"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": CONTROLLER_SCHEMA,
            "idempotency_key": self.idempotency_key,
            "status": self.status.value,
            "freshness": self.freshness.value,
            "quality": self.quality.value,
            "payload": _plain(self.payload),
            "effect_id": self.effect_id,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class RecoveryPlan:
    reason: str
    attempt: int
    max_attempts: int
    next_step: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": RECOVERY_SCHEMA,
            "reason": self.reason,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "next_step": self.next_step,
        }


@dataclass(frozen=True, slots=True)
class Reconciliation:
    status: ReconciliationStatus
    recovery_reason: str
    attempt: int
    max_attempts: int
    recovery: Optional[RecoveryPlan] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", _enum_value(ReconciliationStatus, self.status, ReconciliationStatus.BLOCKED))
        object.__setattr__(self, "recovery_reason", self.recovery_reason if isinstance(self.recovery_reason, str) else "unknown")

    @property
    def confirmed(self) -> bool:
        return self.status is ReconciliationStatus.CONFIRMED

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "recovery_reason": self.recovery_reason,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "recovery": self.recovery.as_dict() if self.recovery is not None else None,
        }


@dataclass(frozen=True, slots=True)
class ControllerCycle:
    observation: Observation
    normalized_state: NormalizedState
    decision: ActionDecision
    effect: Optional[EffectObservation] = None
    reconciliation: Optional[Reconciliation] = None
    live_admission: bool = LIVE_ADMISSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "live_admission", False)

    @property
    def action_count(self) -> int:
        return self.decision.action_count

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": CONTROLLER_SCHEMA,
            "observation": self.observation.as_dict(),
            "normalized_state": self.normalized_state.as_dict(),
            "decision": self.decision.as_dict(),
            "effect": self.effect.as_dict() if self.effect is not None else None,
            "reconciliation": self.reconciliation.as_dict() if self.reconciliation is not None else None,
            "live_admission": False,
        }


class ControllerAdapter(Protocol):
    """Offline adapter boundary used by :class:`ControllerCore`."""

    def observe(self) -> Observation: ...

    def apply(self, proposal: ActionProposal) -> Any: ...

    def observe_effect(self, proposal: ActionProposal) -> EffectObservation: ...


OfflineAdapter = ControllerAdapter


def _get_path(state: Mapping[str, Any], path: str) -> Any:
    current: Any = state
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return _MISSING
        current = current[part]
    return current


def _coerce_observation(value: Any) -> Observation:
    if isinstance(value, Observation):
        return value
    if isinstance(value, Mapping):
        return Observation.from_mapping(value)
    return Observation("", Freshness.UNKNOWN, ObservationQuality.UNKNOWN, {})


def _coerce_effect(value: Any) -> EffectObservation:
    if isinstance(value, EffectObservation):
        return value
    if isinstance(value, Mapping):
        return EffectObservation.from_mapping(value)
    return EffectObservation("", EffectStatus.UNKNOWN, Freshness.UNKNOWN, ObservationQuality.UNKNOWN, {})


def _coerce_policy(value: Any) -> ActionPolicy:
    if isinstance(value, ActionPolicy):
        return value
    if isinstance(value, Mapping):
        return ActionPolicy.from_mapping(value)
    return ActionPolicy("", {}, (), (), 0)


def normalize_observation(
    observation: Observation | Mapping[str, Any] | Any,
    *,
    now: Optional[dt.datetime] = None,
) -> NormalizedState:
    """Normalize one observation and return an explicit fail-closed state."""

    current = _coerce_observation(observation)
    state = _plain(current.payload)
    blockers: list[str] = []
    if not current.observation_id.strip():
        blockers.append("observation_id_missing")
    if current.freshness is not Freshness.FRESH:
        blockers.append(f"freshness_{current.freshness.value}")
    if current.quality is not ObservationQuality.SAFE:
        blockers.append(f"quality_{current.quality.value}")
    if not isinstance(state, Mapping):
        state = {}
        blockers.append("observation_payload_invalid")
    if not _finite_tree(state):
        blockers.append("state_non_finite_or_non_json")
    else:
        try:
            if len(_json_bytes(state)) > MAX_STATE_BYTES:
                blockers.append("state_too_large")
        except (TypeError, ValueError, OverflowError):
            blockers.append("state_not_canonicalizable")

    state_hash = ""
    if not blockers or not {"state_non_finite_or_non_json", "state_not_canonicalizable"}.intersection(blockers):
        try:
            state_hash = canonical_state_hash(dict(state))
        except (TypeError, ValueError, OverflowError):
            blockers.append("state_not_canonicalizable")

    # Wrapper labels are claims, never a substitute for the complete Probe
    # payload. Missing/legacy/projected payloads fail this same trust gate.
    blockers.extend(f"snapshot:{item}" for item in _full_snapshot_blockers(state, now=now))

    return NormalizedState(
        observation_id=current.observation_id,
        state_hash=state_hash,
        state=state,
        freshness=current.freshness,
        quality=current.quality,
        blockers=tuple(sorted(set(blockers))),
    )


def _policy_blockers(policy: ActionPolicy) -> list[str]:
    blockers: list[str] = []
    if not isinstance(policy.action, str) or _ACTION_NAME_RE.fullmatch(policy.action) is None:
        blockers.append("action_name_invalid")
    if not isinstance(policy.args, Mapping):
        blockers.append("action_args_invalid")
    elif not _finite_tree(policy.args):
        blockers.append("action_args_non_finite_or_non_json")
    else:
        try:
            if len(_json_bytes(policy.args)) > MAX_ACTION_ARGS_BYTES:
                blockers.append("action_args_too_large")
        except (TypeError, ValueError, OverflowError):
            blockers.append("action_args_not_canonicalizable")
    if isinstance(policy.max_attempts, bool) or not isinstance(policy.max_attempts, int):
        blockers.append("max_attempts_invalid")
    elif not 1 <= policy.max_attempts <= MAX_ACTION_ATTEMPTS:
        blockers.append("max_attempts_out_of_bounds")
    for name, conditions in (
        ("preconditions", policy.preconditions),
        ("expected_effects", policy.expected_effects),
    ):
        if not conditions or len(conditions) > MAX_CONDITIONS:
            blockers.append(f"{name}_out_of_bounds")
        for condition in conditions:
            blockers.extend(f"{name}:{item}" for item in condition.declaration_blockers(action=policy.action))
    return sorted(set(blockers))


def _proposal_key(normalized: NormalizedState, policy: ActionPolicy) -> str:
    intent = {
        "schema": "oni.controller-idempotency.v1",
        "action": policy.action,
        "args": _plain(policy.args),
        "preconditions": [item.as_dict() for item in policy.preconditions],
        "expected_effects": [item.as_dict() for item in policy.expected_effects],
        "state_hash": normalized.state_hash,
    }
    return "ctrl-" + hashlib.sha256(_json_bytes(intent)).hexdigest()


def _intent_key(policy: ActionPolicy) -> str:
    intent = {
        "schema": "oni.controller-intent.v1",
        "action": policy.action,
        "args": _plain(policy.args),
        "preconditions": [item.as_dict() for item in policy.preconditions],
        "expected_effects": [item.as_dict() for item in policy.expected_effects],
    }
    try:
        return hashlib.sha256(_json_bytes(intent)).hexdigest()
    except (TypeError, ValueError, OverflowError):
        return "invalid-intent"


def _no_action(reason: str, blockers: Sequence[Any] = ()) -> ActionDecision:
    return ActionDecision(None, reason, tuple(sorted(set(str(item) for item in blockers))))


def decide_bounded_action(
    normalized: NormalizedState,
    policy: ActionPolicy | Mapping[str, Any],
    *,
    attempt: int = 1,
    now: Optional[dt.datetime] = None,
) -> ActionDecision:
    """Build at most one action proposal from an eligible normalized state."""

    if not isinstance(normalized, NormalizedState):
        return _no_action("normalized_state_invalid", ("normalized_state_invalid",))
    resolved = _coerce_policy(policy)
    if normalized.blockers or not normalized.eligible:
        return _no_action("observation_not_actionable", normalized.blockers or ("observation_not_actionable",))
    policy_blockers = _policy_blockers(resolved)
    if policy_blockers:
        return _no_action("action_policy_invalid", policy_blockers)
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        return _no_action("attempt_invalid", ("attempt_invalid",))
    if attempt > resolved.max_attempts:
        return _no_action("recovery_attempt_limit_exhausted", ("recovery_attempt_limit_exhausted",))

    if resolved.action == _PRIORITY_ACTION:
        blockers = _priority_policy_blockers(resolved)
        if blockers:
            return _no_action("priority_policy_invalid", blockers)
        blockers = _full_snapshot_blockers(normalized.state, now=now)
        blockers.extend(_priority_state_blockers(normalized.state, resolved.args))
        if blockers:
            return _no_action("priority_snapshot_not_actionable", blockers)
        if canonical_state_hash(_plain(normalized.state)) != normalized.state_hash:
            return _no_action("normalized_state_hash_mismatch", ("normalized_state_hash_mismatch",))
        path = f"duplicants.by_instance_id.id_{resolved.args['duplicant_instance_id']}.chore_priorities.Build"
        if _get_path(normalized.state, path) == 4:
            return _no_action("target_already_at_priority")

    precondition_blockers: list[str] = []
    for condition in resolved.preconditions:
        precondition_blockers.extend(condition.blockers_for(normalized.state, action=resolved.action))
    if precondition_blockers:
        return _no_action("preconditions_not_met", precondition_blockers)

    if all(
        not condition.blockers_for(normalized.state, action=resolved.action)
        for condition in resolved.expected_effects
    ):
        return _no_action("expected_effects_already_satisfied")

    proposal = ActionProposal(
        action=resolved.action,
        args=resolved.args,
        preconditions=resolved.preconditions,
        expected_effects=resolved.expected_effects,
        idempotency_key=_proposal_key(normalized, resolved),
        source_observation_id=normalized.observation_id,
        state_hash=normalized.state_hash,
        attempt=attempt,
        max_attempts=resolved.max_attempts,
        source_snapshot=normalized.state,
    )
    return ActionDecision(proposal, "bounded_action_ready", ())


def _recovery_for(proposal: ActionProposal, reason: str) -> Reconciliation:
    attempt = proposal.attempt if isinstance(proposal.attempt, int) and not isinstance(proposal.attempt, bool) else 0
    maximum = proposal.max_attempts if isinstance(proposal.max_attempts, int) and not isinstance(proposal.max_attempts, bool) else 0
    if attempt < 1 or maximum < 1 or attempt > maximum:
        return Reconciliation(
            ReconciliationStatus.BLOCKED,
            "recovery_attempt_limit_exhausted",
            attempt,
            maximum,
            RecoveryPlan("recovery_attempt_limit_exhausted", attempt, maximum, "stop"),
        )
    if attempt < maximum:
        return Reconciliation(
            ReconciliationStatus.RETRYABLE,
            reason,
            attempt,
            maximum,
            RecoveryPlan(reason, attempt, maximum, "reobserve"),
        )
    exhausted = f"recovery_attempt_limit_exhausted:{reason}"
    return Reconciliation(
        ReconciliationStatus.BLOCKED,
        exhausted,
        attempt,
        maximum,
        RecoveryPlan(exhausted, attempt, maximum, "stop"),
    )


def _blocked(reason: str, proposal: ActionProposal) -> Reconciliation:
    return Reconciliation(
        ReconciliationStatus.BLOCKED,
        reason,
        proposal.attempt,
        proposal.max_attempts,
        RecoveryPlan(reason, proposal.attempt, proposal.max_attempts, "stop"),
    )


def reconcile_effect(
    proposal: ActionProposal,
    effect: EffectObservation | Mapping[str, Any] | Any,
    *,
    now: Optional[dt.datetime] = None,
) -> Reconciliation:
    """Reconcile trusted effect telemetry and expose bounded recovery only."""

    if not isinstance(proposal, ActionProposal):
        return Reconciliation(
            ReconciliationStatus.BLOCKED,
            "proposal_invalid",
            0,
            0,
            RecoveryPlan("proposal_invalid", 0, 0, "stop"),
        )
    if (type(proposal.attempt) is not int or type(proposal.max_attempts) is not int
            or not 1 <= proposal.attempt <= proposal.max_attempts <= MAX_ACTION_ATTEMPTS):
        return _blocked("recovery_attempt_limit_exhausted", proposal)
    observed = _coerce_effect(effect)
    if observed.idempotency_key != proposal.idempotency_key:
        return _blocked("effect_idempotency_key_mismatch", proposal)
    if observed.status is EffectStatus.TIMEOUT:
        return _recovery_for(proposal, "effect_timeout")
    if observed.status is EffectStatus.PENDING:
        return _recovery_for(proposal, "effect_pending")
    if observed.status not in (EffectStatus.APPLIED, EffectStatus.MISMATCH):
        return _blocked(f"effect_status_{observed.status.value}", proposal)
    if observed.freshness is not Freshness.FRESH:
        return _blocked(f"effect_freshness_{observed.freshness.value}", proposal)
    if observed.quality is not ObservationQuality.SAFE:
        return _blocked(f"effect_quality_{observed.quality.value}", proposal)
    if not isinstance(observed.payload, Mapping):
        return _blocked("effect_payload_shape_invalid", proposal)
    if not _finite_tree(observed.payload):
        return _blocked("effect_payload_non_finite_or_non_json", proposal)
    try:
        if len(_json_bytes(observed.payload)) > MAX_STATE_BYTES:
            return _blocked("effect_payload_too_large", proposal)
    except (TypeError, ValueError, OverflowError):
        return _blocked("effect_payload_not_canonicalizable", proposal)
    cycle_blockers = _cycle_metadata_blockers(observed.payload)
    if cycle_blockers:
        return _blocked(cycle_blockers[0], proposal)

    policy = ActionPolicy(
        proposal.action, proposal.args, proposal.preconditions,
        proposal.expected_effects, proposal.max_attempts,
    )
    invalid = _policy_blockers(policy)
    if not invalid and proposal.action == _PRIORITY_ACTION:
        invalid.extend(_priority_policy_blockers(policy))
    if invalid:
        return _blocked("effect_proposal_invalid:" + invalid[0], proposal)
    if proposal.action == _PRIORITY_ACTION:
        pair_blockers = _priority_snapshot_pair_blockers(proposal, observed.payload, now=now)
        if pair_blockers:
            return _blocked("effect_snapshot_untrusted:" + pair_blockers[0], proposal)

    mismatch: list[str] = []
    for condition in proposal.expected_effects:
        blockers = condition.blockers_for(observed.payload, action=proposal.action)
        for blocker in blockers:
            if blocker != f"condition_not_met:{condition.path}":
                return _blocked("effect_evidence_invalid:" + blocker, proposal)
        mismatch.extend(blockers)
    if observed.status is EffectStatus.MISMATCH:
        return _recovery_for(proposal, "effect_reported_mismatch")
    if mismatch:
        return _recovery_for(proposal, "expected_effect_mismatch")
    return Reconciliation(
        ReconciliationStatus.CONFIRMED,
        "expected_effects_confirmed",
        proposal.attempt,
        proposal.max_attempts,
        None,
    )


def _synthetic_effect(key: str, status: EffectStatus) -> EffectObservation:
    return EffectObservation(key, status, Freshness.UNKNOWN, ObservationQuality.UNKNOWN, {})


_PRIORITY_ACTION = "set_duplicant_chore_priority"
_DUPLICANT_SOURCE = "Components.LiveMinionIdentities+ChoreConsumer"
_CYCLE_SOURCE = "GameClock.GetCycle"
_GAME_TIME_CYCLES_SOURCE = "GameClock.GetTimeInCycles"
# ProbeStateReader emits these surfaces even when a particular field is
# unsupported. Requiring their presence rejects an action-only projection
# disguised as a full state_snapshot. Unrelated unsupported fields need not
# be observed for this one action; the selected identity/paused/priority do.
_FULL_PROBE_FIELDS = frozenset({
    "game", "cycle", "game_time_cycles", "duplicants", "resources",
    "report_counters", "observation_scope", "materials", "production",
    "consumption", "power", "oxygen", "water", "temperature", "rockets",
    "space", "resilience", "colony", "alerts", "research", "buildings",
    "functional_buildings", "designations", "blueprints", "cells",
    "quality", "read_metrics_ms",
})


def _scalar_snapshot_blockers(state: Mapping[str, Any]) -> list[str]:
    """Validate the two GameClock scalars emitted by the real Probe.

    These values have producer-defined metadata but no registered field
    contract.  Keep the gate local to the full Probe envelope and require the
    exact emission semantics instead of coercing strings, booleans, nulls, or
    non-finite numbers into an actionable clock reading.
    """

    blockers: list[str] = []
    cycle = state.get("cycle", _MISSING)
    cycle_valid = _valid_cycle_scalar(cycle, continuous=False)
    if not cycle_valid:
        blockers.append("snapshot_cycle_invalid")

    game_time_cycles = state.get("game_time_cycles", _MISSING)
    game_time_valid = False
    if _valid_cycle_scalar(game_time_cycles, continuous=True):
        try:
            game_time_numeric = float(game_time_cycles)
        except (OverflowError, ValueError, TypeError):
            game_time_numeric = math.nan
        game_time_valid = math.isfinite(game_time_numeric)
        if game_time_valid and cycle_valid:
            try:
                in_display_cycle = cycle <= game_time_numeric < cycle + 1.0
            except (OverflowError, TypeError):
                in_display_cycle = False
            if not in_display_cycle:
                blockers.append("snapshot_game_time_cycles_out_of_range")
    if not game_time_valid:
        blockers.append("snapshot_game_time_cycles_invalid")

    quality = state.get("quality")
    cycle_quality = quality.get("cycle") if isinstance(quality, dict) else None
    blockers.extend(
        "cycle:" + item
        for item in quality_contract_blockers(
            cycle_quality,
            allowed_statuses=("observed",),
            semantics=("display_1_based",),
            sources=(_CYCLE_SOURCE,),
            completeness=None,
            captures=None,
        )
    )
    game_time_quality = quality.get("game_time_cycles") if isinstance(quality, dict) else None
    blockers.extend(
        "game_time_cycles:" + item
        for item in quality_contract_blockers(
            game_time_quality,
            allowed_statuses=("observed",),
            semantics=("display_1_based_continuous",),
            units=("cycle",),
            scopes=("save",),
            completeness=("complete",),
            sources=(_GAME_TIME_CYCLES_SOURCE,),
            captures=("snapshot.ts",),
        )
    )
    return blockers


def _full_snapshot_blockers(
    state: Mapping[str, Any], *, now: Optional[dt.datetime] = None,
) -> list[str]:
    plain = _plain(state)
    blockers: list[str] = []
    if not isinstance(plain, dict):
        return ["snapshot_missing"]
    if not _finite_tree(plain):
        return ["snapshot_non_finite_or_non_json"]
    try:
        if len(_json_bytes(plain)) > MAX_STATE_BYTES:
            blockers.append("state_too_large")
    except (TypeError, ValueError, OverflowError):
        return ["snapshot_not_canonicalizable"]
    blockers.extend(snapshot_trust_blockers(plain, now=now))
    missing = _FULL_PROBE_FIELDS.difference(plain)
    if missing:
        blockers.append("snapshot_fields_missing:" + ",".join(sorted(missing)))
    blockers.extend(_scalar_snapshot_blockers(plain))
    for key in _FULL_PROBE_FIELDS - {"cycle", "game_time_cycles", "alerts"}:
        if key in plain and not isinstance(plain[key], dict):
            blockers.append("snapshot_section_malformed:" + key)
    if "alerts" in plain and plain["alerts"] is not None and not isinstance(plain["alerts"], list):
        blockers.append("snapshot_section_malformed:alerts")
    return sorted(set(blockers))


def _priority_policy_blockers(policy: ActionPolicy) -> list[str]:
    """Require exactly the unchanged helper's Build 3-to-4 contract."""
    args = _plain(policy.args)
    if not isinstance(args, dict) or set(args) != {
        "duplicant_instance_id", "expected_name", "chore_group",
        "from_priority", "priority",
    }:
        return ["priority_args_shape_invalid"]
    instance_id = args["duplicant_instance_id"]
    name = args["expected_name"]
    if type(instance_id) is not int or not 0 < instance_id <= 2**31 - 1:
        return ["priority_instance_id_invalid"]
    if not isinstance(name, str) or not name.strip() or len(name) > 128:
        return ["priority_expected_name_invalid"]
    if (args["chore_group"] != "Build"
            or type(args["from_priority"]) is not int or args["from_priority"] != 3
            or type(args["priority"]) is not int or args["priority"] != 4):
        return ["priority_transition_invalid"]
    path = f"duplicants.by_instance_id.id_{instance_id}.chore_priorities.Build"
    expected_preconditions = (
        Condition("game.paused", "eq", True, ("observed",), GAME_PAUSED_CONTRACT_ID),
        Condition(path, "eq", 3, ("observed",), DUPLICANT_CHORE_PRIORITY_CONTRACT_ID),
    )
    expected_effects = (
        Condition(path, "eq", 4, ("observed",), DUPLICANT_CHORE_PRIORITY_CONTRACT_ID),
    )
    # Canonical JSON equality also distinguishes integers from floats/bools.
    expected = [c.as_dict() for c in expected_preconditions + expected_effects]
    actual = [c.as_dict() for c in policy.preconditions + policy.expected_effects]
    if (len(policy.preconditions) != 2 or len(policy.expected_effects) != 1
            or _json_bytes(actual) != _json_bytes(expected)):
        return ["priority_helper_contract_mismatch"]
    return []


def _priority_state_blockers(state: Mapping[str, Any], args: Mapping[str, Any]) -> list[str]:
    """Read one selected identity and the helper's observed field contracts."""
    plain = _plain(state)
    instance_id = args["duplicant_instance_id"]
    prefix = f"duplicants.by_instance_id.id_{instance_id}"
    target = _get_path(plain, prefix)
    if not isinstance(target, dict):
        return ["duplicant_identity_missing"]
    blockers: list[str] = []
    if type(target.get("instance_id")) is not int or target.get("instance_id") != instance_id:
        blockers.append("duplicant_instance_id_mismatch")
    if target.get("name") != args["expected_name"]:
        blockers.append("duplicant_name_mismatch")
    if type(target.get("world_id")) is not int or target.get("world_id") != plain.get("world_id"):
        blockers.append("duplicant_world_mismatch")
    by_instance = _get_path(plain, "duplicants.by_instance_id")
    if isinstance(by_instance, dict):
        for key, row in by_instance.items():
            if key != f"id_{instance_id}" and isinstance(row, dict) and row.get("instance_id") == instance_id:
                blockers.append("duplicant_identity_duplicate")
    quality = plain.get("quality", {})
    for path in ("duplicants.by_instance_id", prefix + ".instance_id", prefix + ".name", prefix + ".world_id"):
        entry = quality.get(path) if isinstance(quality, dict) else None
        if (not isinstance(entry, dict) or entry.get("status") != "observed"
                or entry.get("source") != _DUPLICANT_SOURCE):
            blockers.append("duplicant_identity_quality_invalid:" + path)
    for path, contract in (
        ("game.paused", GAME_PAUSED_CONTRACT_ID),
        (prefix + ".chore_priorities.Build", DUPLICANT_CHORE_PRIORITY_CONTRACT_ID),
    ):
        read = read_contract_field(plain, path, contract, allowed_statuses=("observed",))
        blockers.extend(path + ":" + item for item in read.blockers)
    if _get_path(plain, "game.paused") is not True:
        blockers.append("game_not_paused")
    return sorted(set(blockers))


def build_duplicant_priority_policy(
    state: Mapping[str, Any], *, instance_id: int, max_attempts: int = 2,
) -> ActionPolicy:
    """Use the unchanged helper for a selected Build 3-to-4 intent.

    ``max_attempts`` retains the core API name but bounds effect observations,
    never sends. ControllerCore reports a trusted target already at 4 as a
    no-op when evaluating this fixed policy against its current observation.
    """
    hint = build_duplicant_priority_hint(
        _plain(state), instance_id=instance_id, chore_group="Build", priority=4,
    )
    result = ActionPolicy.from_hint(hint, max_attempts=max_attempts)
    blockers = _priority_policy_blockers(result)
    if blockers:
        raise ValueError(",".join(blockers))
    return result


def _priority_snapshot_pair_blockers(
    proposal: ActionProposal, later_state: Mapping[str, Any],
    *, now: Optional[dt.datetime] = None,
) -> list[str]:
    source = _plain(proposal.source_snapshot)
    later = _plain(later_state)
    source_ts = parse_snapshot_timestamp(source.get("ts")) if isinstance(source, dict) else None
    blockers = ["source:" + b for b in _full_snapshot_blockers(source, now=source_ts)]
    blockers.extend("later:" + b for b in _full_snapshot_blockers(later, now=now))
    if blockers:
        return sorted(set(blockers))
    if canonical_state_hash(source) != proposal.state_hash:
        blockers.append("source_state_hash_mismatch")
    for key in ("session_id", "save_id_hash", "world_id", "game_build", "mod_version"):
        if source[key] != later[key]:
            blockers.append("snapshot_" + key + "_mismatch")
    # Both sequence counters must advance, and snapshot increments cannot
    # exceed total record increments (other records may occur in between).
    seq_delta = later["seq"] - source["seq"]
    snapshot_delta = later["snapshot_seq"] - source["snapshot_seq"]
    if seq_delta <= 0 or snapshot_delta <= 0 or snapshot_delta > seq_delta:
        blockers.append("snapshot_sequence_not_later_or_inconsistent")
    later_ts = parse_snapshot_timestamp(later["ts"])
    if source_ts is None or later_ts is None or later_ts <= source_ts:
        blockers.append("snapshot_timestamp_not_later")
    blockers.extend("source:" + b for b in _priority_state_blockers(source, proposal.args))
    blockers.extend("later:" + b for b in _priority_state_blockers(later, proposal.args))
    # Validate the source's observed from value as well as its stored hash.
    for condition in proposal.preconditions:
        blockers.extend("source:" + b for b in condition.blockers_for(source, action=proposal.action))
    return sorted(set(blockers))


class DurableIntentStore:
    """Caller-owned create-only dispatch receipts; never a game command queue.

    The caller supplies an existing private directory. Each exact intent gets
    one exclusive receipt, fsynced before adapter.apply is reachable. Existing
    receipts (including empty/partial ones after a failure) block forever;
    restarting the core with the same store cannot replay an uncertain send.
    The core never deletes receipts or interprets absence as a rollback.
    """

    def __init__(self, directory: str | os.PathLike[str]) -> None:
        self.directory = pathlib.Path(directory).resolve(strict=True)
        if not self.directory.is_dir():
            raise ValueError("intent_store_directory_required")

    def consume(self, intent: str, proposal: ActionProposal) -> bool:
        if re.fullmatch(r"[0-9a-f]{64}", intent) is None:
            raise ValueError("intent_key_invalid")
        receipt = self.directory / (intent + ".json")
        data = _json_bytes({
            "schema": "oni.controller-consumed-intent.v1",
            "intent": intent,
            "idempotency_key": proposal.idempotency_key,
            "source_state_hash": proposal.state_hash,
            # Keep the complete bounded proposal so an explicit coordinator
            # can resume observation after a process restart.  This is an
            # immutable, credential-free snapshot; it never authorizes a new
            # apply call and remains subject to the adapter's live evidence
            # checks.
            "proposal": proposal.as_dict(),
            "consumed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "live_admission": False,
        })
        try:
            descriptor = os.open(receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return False
        # Any write/fsync/close failure propagates and blocks dispatch. A
        # possibly created receipt is deliberately retained, never retried.
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        return True

    def recover(self, intent: str) -> Optional[ActionProposal]:
        """Load one exact consumed proposal for explicit read-only recovery.

        Missing/legacy/invalid receipts return ``None`` so callers stop
        fail-closed instead of guessing from the latest state or command.
        """
        if re.fullmatch(r"[0-9a-f]{64}", intent) is None:
            raise ValueError("intent_key_invalid")
        receipt = self.directory / (intent + ".json")
        try:
            raw = receipt.read_bytes()
            if len(raw) > MAX_INTENT_RECEIPT_BYTES:
                return None
            value = json.loads(raw.decode("utf-8"), parse_constant=_reject_json_constant)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            return None
        if not isinstance(value, Mapping) or value.get("schema") != "oni.controller-consumed-intent.v1":
            return None
        if value.get("intent") != intent or value.get("live_admission") is not False:
            return None
        proposal_value = value.get("proposal")
        if (not isinstance(proposal_value, Mapping)
                or proposal_value.get("live_admission") is not False):
            return None
        try:
            proposal = ActionProposal.from_mapping(proposal_value)
        except (TypeError, ValueError, OverflowError):
            return None
        if (proposal.idempotency_key != value.get("idempotency_key")
                or proposal.state_hash != value.get("source_state_hash")
                or not proposal.idempotency_key or not proposal.source_observation_id
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", proposal.state_hash)):
            return None
        return proposal


class ControllerCore:
    """Offline at-most-once dispatch followed by bounded observation only.

    Priority actions require a DurableIntentStore in an explicitly supplied
    directory. A restarted core stops on an existing receipt; it never assumes
    that lack of process-local effect state means an earlier send did not run.
    """

    def __init__(
        self, adapter: ControllerAdapter, policy: ActionPolicy | Mapping[str, Any],
        *, intent_store: Optional[DurableIntentStore] = None,
        final_apply_guard: Optional[Callable[[ActionProposal], Any]] = None,
    ) -> None:
        self.adapter = adapter
        self.policy = _coerce_policy(policy)
        self.intent_store = intent_store
        if final_apply_guard is not None and not callable(final_apply_guard):
            raise TypeError("final_apply_guard must be callable")
        # The guard is deliberately caller supplied: the offline core cannot
        # infer process or lifecycle ownership.  When present it is the last
        # callback before adapter.apply/submit, so a caller can re-check its
        # mutable admission boundary; only an exact True result permits apply.
        self.final_apply_guard = final_apply_guard
        self._seen_idempotency_keys: set[str] = set()
        self._consumed_intents: set[str] = set()
        self._pending: Optional[ActionProposal] = None
        self._observations = 0
        self._terminal_reason: Optional[str] = None
        # Once an adapter exposes a confirmation reader, ordinary observation
        # can never replace it, including after the reader disappears/fails.
        self._effect_reader_required = (
            hasattr(adapter, "observe_effect") or hasattr(adapter, "read_effect")
        )

    @property
    def seen_idempotency_keys(self) -> Tuple[str, ...]:
        return tuple(sorted(self._seen_idempotency_keys))

    @property
    def consumed_intents(self) -> Tuple[str, ...]:
        return tuple(sorted(self._consumed_intents))

    def _observe(self) -> Observation:
        try:
            method = getattr(self.adapter, "observe", None) or getattr(self.adapter, "read_observation", None)
            return _coerce_observation(method()) if callable(method) else _coerce_observation(None)
        except Exception:
            return _coerce_observation(None)

    def _effect(self, proposal: ActionProposal) -> EffectObservation:
        try:
            apply_method = getattr(self.adapter, "apply", None) or getattr(self.adapter, "submit", None)
            if not callable(apply_method):
                return _synthetic_effect(proposal.idempotency_key, EffectStatus.UNKNOWN)
            if self.final_apply_guard is not None:
                try:
                    if self.final_apply_guard(proposal) is not True:
                        return _synthetic_effect(proposal.idempotency_key, EffectStatus.REJECTED)
                except Exception:
                    # Guard failures are an admission rejection.  Do not call
                    # the adapter after an unreadable process/lifecycle check.
                    return _synthetic_effect(proposal.idempotency_key, EffectStatus.REJECTED)
            returned = apply_method(proposal)
            if isinstance(returned, EffectObservation):
                return returned
            if isinstance(returned, Mapping) and "status" in returned:
                return _coerce_effect(returned)
            method = getattr(self.adapter, "observe_effect", None) or getattr(self.adapter, "read_effect", None)
            return _coerce_effect(method(proposal)) if callable(method) else _synthetic_effect(proposal.idempotency_key, EffectStatus.UNKNOWN)
        except TimeoutError:
            return _synthetic_effect(proposal.idempotency_key, EffectStatus.TIMEOUT)
        except Exception:
            # Unknown adapter effects stop. Exception text is never evidence.
            return _synthetic_effect(proposal.idempotency_key, EffectStatus.UNKNOWN)

    def _record_reconciliation(self, result: Reconciliation) -> None:
        if result.confirmed:
            self._terminal_reason = "intent_already_confirmed"
            self._pending = None
        elif result.status is ReconciliationStatus.BLOCKED:
            self._terminal_reason = "intent_blocked"
            self._pending = None

    def _pending_effect(self, proposal: ActionProposal, observation: Observation) -> EffectObservation:
        try:
            reader = getattr(self.adapter, "observe_effect", None) or getattr(self.adapter, "read_effect", None)
            if reader is not None:
                self._effect_reader_required = True
            if self._effect_reader_required:
                if not callable(reader):
                    return _synthetic_effect(proposal.idempotency_key, EffectStatus.UNKNOWN)
                return _coerce_effect(reader(proposal))
        except TimeoutError:
            return _synthetic_effect(proposal.idempotency_key, EffectStatus.TIMEOUT)
        except Exception:
            return _synthetic_effect(proposal.idempotency_key, EffectStatus.UNKNOWN)
        # Compatibility for state-only offline fixtures. Queue/game adapters
        # must implement observe_effect and bind actual command confirmation.
        return EffectObservation(
            proposal.idempotency_key, EffectStatus.APPLIED,
            observation.freshness, observation.quality, observation.payload,
            effect_id=observation.observation_id, source=observation.source,
        )

    def _commit_confirmed_effect(self, proposal: ActionProposal, effect: EffectObservation) -> EffectObservation:
        """Give an adapter a chance to persist its exact confirmed leaf."""
        if effect.status is not EffectStatus.APPLIED:
            return effect
        method = getattr(self.adapter, "commit_confirmed_effect", None)
        if not callable(method):
            return effect
        try:
            returned = method(proposal, effect)
            return _coerce_effect(returned)
        except TimeoutError:
            return _synthetic_effect(proposal.idempotency_key, EffectStatus.TIMEOUT)
        except Exception:
            return _synthetic_effect(proposal.idempotency_key, EffectStatus.UNKNOWN)

    def step(self, *, now: Optional[dt.datetime] = None) -> ControllerCycle:
        observation = self._observe()
        normalized = normalize_observation(observation, now=now)
        if self._terminal_reason:
            return ControllerCycle(observation, normalized, _no_action(self._terminal_reason))
        if self._pending is not None:
            # Later steps read state only. They cannot reach apply, even when
            # unrelated telemetry has changed the canonical state hash.
            self._observations += 1
            proposal = self._pending
            bounded = replace(proposal, attempt=self._observations)
            decision = _no_action("intent_pending_reobserve", ("intent_consumed", "reobserve_only"))
            effect = _synthetic_effect(proposal.idempotency_key, EffectStatus.UNKNOWN)
            if not normalized.eligible:
                result = _blocked("reobserve_not_actionable", bounded)
            elif self._observations > proposal.max_attempts:
                result = _blocked("reobserve_budget_exhausted", bounded)
            else:
                effect = self._pending_effect(bounded, observation)
                effect = self._commit_confirmed_effect(bounded, effect)
                result = reconcile_effect(bounded, effect, now=now)
            self._record_reconciliation(result)
            return ControllerCycle(observation, normalized, decision, effect, result)

        decision = decide_bounded_action(normalized, self.policy, now=now)
        proposal = decision.proposal
        if proposal is None:
            if decision.reason in {"expected_effects_already_satisfied", "target_already_at_priority"}:
                method = getattr(self.adapter, "commit_already_satisfied", None)
                if callable(method) and method(normalized.state) is not True:
                    return ControllerCycle(observation, normalized, _no_action("plan_reconciliation_failed"))
            if decision.reason in {"target_already_at_priority", "expected_effects_already_satisfied"}:
                self._terminal_reason = "intent_already_satisfied"
            return ControllerCycle(observation, normalized, decision)
        intent = _intent_key(self.policy)
        if intent in self._consumed_intents:
            self._terminal_reason = "intent_consumed"
            return ControllerCycle(observation, normalized, _no_action("intent_consumed"))
        if (self.policy.action == _PRIORITY_ACTION
                or getattr(self.adapter, "requires_durable_intent_store", False) is True) and self.intent_store is None:
            return ControllerCycle(observation, normalized, _no_action("durable_intent_store_required"))
        # Consume in memory first as well: a store exception on this instance
        # can never be retried, including a write that succeeded before error.
        self._consumed_intents.add(intent)
        try:
            claimed = self.intent_store.consume(intent, proposal) if self.intent_store is not None else True
        except Exception:
            self._terminal_reason = "intent_consumption_failed"
            return ControllerCycle(observation, normalized, _no_action(self._terminal_reason))
        if claimed is not True:
            self._terminal_reason = "intent_already_consumed"
            return ControllerCycle(observation, normalized, _no_action(self._terminal_reason))
        self._seen_idempotency_keys.add(proposal.idempotency_key)
        self._pending = proposal
        self._observations = 1
        effect = self._effect(proposal)
        result = reconcile_effect(proposal, effect, now=now)
        self._record_reconciliation(result)
        return ControllerCycle(observation, normalized, decision, effect, result)

    def resume_consumed_intent(self, *, now: Optional[dt.datetime] = None) -> ControllerCycle:
        """Explicitly resume observation for a durable consumed intent.

        This method is never called by ``step``.  It reconstructs the exact
        proposal stored before the original apply attempt, then enters the
        existing pending/effect reader path.  No intent is consumed and no
        adapter ``apply`` method is reachable from this entry point.
        """
        if self._pending is not None or self._terminal_reason is not None:
            return self.step(now=now)
        if self.intent_store is None:
            observation = self._observe()
            return ControllerCycle(
                observation,
                normalize_observation(observation, now=now),
                _no_action("intent_recovery_store_missing"),
            )
        proposal = self.intent_store.recover(_intent_key(self.policy))
        if proposal is None:
            observation = self._observe()
            return ControllerCycle(observation, normalize_observation(observation, now=now),
                _no_action("intent_recovery_unavailable"))
        expected = self.policy.as_dict()
        actual = {key: proposal.as_dict()[key] for key in
                  ("action", "args", "preconditions", "expected_effects")}
        if actual != {key: expected[key] for key in
                      ("action", "args", "preconditions", "expected_effects")}:
            observation = self._observe()
            return ControllerCycle(observation, normalize_observation(observation, now=now),
                _no_action("intent_recovery_policy_mismatch"))
        self._pending = proposal
        self._observations = max(1, int(proposal.attempt))
        self._consumed_intents.add(_intent_key(self.policy))
        self._seen_idempotency_keys.add(proposal.idempotency_key)
        return self.step(now=now)

    run_once = step


class ControllerSequence:
    """A finite, prevalidated sequence of at-most-once controller intents."""

    def __init__(
        self,
        adapter: ControllerAdapter,
        policies: Sequence[ActionPolicy | Mapping[str, Any]],
        *,
        max_steps: int,
        intent_store: Optional[DurableIntentStore] = None,
    ) -> None:
        if type(max_steps) is not int or max_steps < 1:
            raise ValueError("max_steps must be a positive integer")
        if isinstance(policies, (str, bytes)) or not isinstance(policies, Sequence) or not policies:
            raise ValueError("policies must be a non-empty sequence")
        resolved = tuple(_coerce_policy(item) for item in policies)
        for index, policy in enumerate(resolved):
            blockers = _policy_blockers(policy)
            if not blockers and policy.action == _PRIORITY_ACTION:
                blockers.extend(_priority_policy_blockers(policy))
            if blockers:
                raise ValueError(f"policy invalid at index {index}: {','.join(blockers)}")
        intents = tuple(_intent_key(item) for item in resolved)
        if len(set(intents)) != len(intents):
            raise ValueError("duplicate policy intents are not allowed")
        if intent_store is None and any(item.action == _PRIORITY_ACTION for item in resolved):
            raise ValueError("durable_intent_store_required")
        self._cores = tuple(
            ControllerCore(adapter, item, intent_store=intent_store) for item in resolved
        )
        self._max_steps = max_steps
        self._steps = 0
        self._index = 0
        self._stop_reason: Optional[str] = None
        self._stepping = False

    @classmethod
    def from_named_policies(
        cls,
        adapter: ControllerAdapter,
        policies: Mapping[str, ActionPolicy | Mapping[str, Any]],
        selected: Sequence[str],
        *,
        max_steps: int,
        intent_store: Optional[DurableIntentStore] = None,
    ) -> "ControllerSequence":
        """Freeze caller-selected objectives before any adapter access."""
        if not isinstance(policies, Mapping) or not policies:
            raise ValueError("policies must be a non-empty mapping")
        if isinstance(selected, (str, bytes)) or not isinstance(selected, Sequence) or not selected:
            raise ValueError("selected must be a non-empty sequence")
        names = tuple(selected)
        if any(type(name) is not str or not name.strip() for name in names):
            raise ValueError("selected names must be non-empty strings")
        if len(set(names)) != len(names):
            raise ValueError("selected names must be unique")
        resolved = []
        for name in names:
            if name not in policies:
                raise ValueError(f"selected policy not found: {name}")
            resolved.append(policies[name])
        return cls(adapter, resolved, max_steps=max_steps, intent_store=intent_store)

    @property
    def completed(self) -> bool:
        return self._index == len(self._cores)

    @property
    def policy_index(self) -> int:
        return self._index

    @property
    def stop_reason(self) -> Optional[str]:
        return self._stop_reason

    def cancel(self) -> None:
        """Stop between calls, preserving any existing terminal evidence."""
        if self._stop_reason is None:
            self._stop_reason = "sequence_cancelled"

    def step(self, *, now: Optional[dt.datetime] = None) -> ControllerCycle:
        if self._stop_reason is not None:
            raise StopIteration(self._stop_reason)
        if self._stepping:
            raise RuntimeError("sequence_step_in_progress")
        self._stepping = True
        try:
            self._steps += 1
            cycle = self._cores[self._index].step(now=now)
            if self._stop_reason is not None:
                return cycle
            reconciliation = cycle.reconciliation
            if reconciliation is not None and reconciliation.status is ReconciliationStatus.BLOCKED:
                self._stop_reason = reconciliation.recovery_reason
            elif cycle.decision.reason in {
                "intent_blocked", "intent_consumed", "intent_already_consumed",
                "intent_consumption_failed", "durable_intent_store_required",
                "intent_already_confirmed", "recovery_attempt_limit_exhausted",
            }:
                self._stop_reason = cycle.decision.reason
            elif (reconciliation is not None and reconciliation.confirmed) or cycle.decision.reason in {
                "expected_effects_already_satisfied", "target_already_at_priority",
                "intent_already_satisfied",
            }:
                self._index += 1
            if self.completed:
                self._stop_reason = "sequence_completed"
            elif self._stop_reason is None and self._steps >= self._max_steps:
                self._stop_reason = "sequence_step_budget_exhausted"
            return cycle
        finally:
            self._stepping = False


__all__ = [
    "ACTION_PROPOSAL_SCHEMA",
    "ActionDecision",
    "ActionPolicy",
    "ActionProposal",
    "Condition",
    "CONTROLLER_SCHEMA",
    "ControllerAdapter",
    "ControllerCore",
    "ControllerCycle",
    "ControllerSequence",
    "DurableIntentStore",
    "EffectObservation",
    "EffectStatus",
    "ExpectedEffect",
    "Freshness",
    "LIVE_ADMISSION",
    "MAX_CYCLE_VALUE",
    "NormalizedState",
    "Observation",
    "ObservationQuality",
    "OfflineAdapter",
    "OutcomeStatus",
    "Precondition",
    "Quality",
    "RecoveryPlan",
    "Reconciliation",
    "ReconciliationStatus",
    "build_duplicant_priority_policy",
    "decide_bounded_action",
    "normalize_observation",
    "reconcile_effect",
]
