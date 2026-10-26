from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import math
import re
from functools import lru_cache
from typing import Any, Callable, Dict, List, Optional, Tuple


USABLE_STATUSES = {"observed", "estimated"}
BLOCKED_STATUSES = {"unsupported", "error", "stale"}
_STATE_HASH_EXCLUDED_TOP_LEVEL_FIELDS = frozenset({"bridge", "writer", "read_metrics_ms"})
_STATE_HASH_EXCLUDED_SAMPLE_FIELDS = frozenset({"duration_ms"})
MIN_TRUSTED_SAMPLE_INTERVAL_SECONDS = 0.1
MAX_TRUSTED_SAMPLE_INTERVAL_SECONDS = 60.0
MAX_FUTURE_CLOCK_SKEW_SECONDS = 5.0
# Publishing-cadence margin for the live Bridge.  A snapshot's ``ts`` is fixed
# when the Probe samples it, but the Bridge republishes only the last record it
# processed, so a reader's observed age also carries the Bridge's own
# read-and-publish cadence.  Every consumer of the same live Bridge shares this
# margin, so the Bridge's own health and an independent re-check agree; without
# it a paused colony self-locked (snapshots published 19-22 s old failed a 15 s
# limit, every autonomous authorization was refused for ``snapshot_stale``, the
# game stayed paused, and the snapshots stayed old — 2026-10-25 rounds 1-3).
# Genuine silence from the Probe still fails: the rule stays Probe-relative.
BRIDGE_PUBLISH_MARGIN_SECONDS = 30.0
_SAVE_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$", re.IGNORECASE)


@lru_cache(maxsize=256)
def _split_dotted_path(path: str) -> Tuple[str, ...]:
    return tuple(path.split("."))


# Canonical alert coverage (review G3/M8).  The Probe reads alerts for every
# world with live duplicants; the older active-world form is still recognised
# so recordings replay, but only covers a colony living on the camera world.
ALERTS_ALL_WORLDS_SCOPE = "all_worlds"
ALERTS_ACTIVE_WORLD_SCOPE = "active_world"
ALERTS_ALL_WORLDS_SOURCE = (
    "Components.LiveMinionIdentities.Items+GetMyWorldId+typed_monitor_predicates;"
    "catalog=oni.alert.catalog.all_worlds.v1"
)
ALERTS_ACTIVE_WORLD_SOURCE = (
    "Components.LiveMinionIdentities.GetWorldItems(activeWorldId,false)+typed_monitor_predicates;"
    "catalog=oni.alert.catalog.active_world.v1"
)


def safe_get(mapping: Dict[str, Any], path: str, default: Any = None) -> Any:
    if not isinstance(mapping, dict):
        return default
    if "." not in path:
        return mapping.get(path, default)
    current: Any = mapping
    for part in _split_dotted_path(path):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


def field_quality(state: Optional[Dict[str, Any]], path: str) -> Dict[str, Any]:
    if not isinstance(state, dict):
        return {"status": "unsupported", "source": "missing_state"}

    quality = state.get("quality")
    if isinstance(quality, dict):
        entry = quality.get(path)
        if isinstance(entry, dict):
            return entry

    schema = str(state.get("schema") or "")
    if schema == "oni.state.v2":
        return {"status": "unsupported", "source": "missing_quality_entry"}
    if "." in path:
        value = safe_get(state, path)
    else:
        value = state.get(path)
    return {
        "status": "unsupported" if value is None else "observed",
        "source": "legacy_inference",
    }


def field_status(state: Optional[Dict[str, Any]], path: str) -> str:
    return str(field_quality(state, path).get("status") or "unsupported")


def quality_contract_blockers(
    entry: Any,
    *,
    allowed_statuses: Tuple[str, ...] = ("observed",),
    semantics: Optional[Tuple[str, ...]] = None,
    units: Optional[Tuple[str, ...]] = None,
    scopes: Optional[Tuple[str, ...]] = None,
    completeness: Optional[Tuple[str, ...]] = ("complete",),
    authorities: Optional[Tuple[str, ...]] = None,
    sources: Optional[Tuple[str, ...]] = None,
    contract_ids: Optional[Tuple[str, ...]] = None,
    captures: Optional[Tuple[str, ...]] = ("snapshot.ts",),
    require_proof_hashes: bool = False,
    proof_verifier: Optional[Callable[[Dict[str, Any]], bool]] = None,
) -> List[str]:
    """Return fail-closed metadata blockers for a certifying quality entry.

    ``status=observed`` only says that a producer emitted a value.  It does not
    prove that the value has the unit, scope, derivation, or policy authority a
    strategy gate expects.  This helper keeps those concerns explicit without
    changing the backwards-compatible ``oni.state.v2`` envelope.
    """

    if not isinstance(entry, dict):
        return ["quality_entry_missing"]

    blockers: List[str] = []
    if entry.get("status") not in allowed_statuses:
        blockers.append(
            "quality_not_observed"
            if allowed_statuses == ("observed",)
            else "quality_status_mismatch"
        )

    def require_member(name: str, allowed: Optional[Tuple[str, ...]]) -> None:
        if allowed is None:
            return
        value = entry.get(name)
        if not isinstance(value, str) or value not in allowed:
            blockers.append(f"quality_{name}_mismatch")

    require_member("semantic", semantics)
    require_member("unit", units)
    require_member("scope", scopes)
    require_member("completeness", completeness)
    require_member("authority", authorities)
    require_member("source", sources)
    require_member("contract_id", contract_ids)

    source = entry.get("source")
    if not isinstance(source, str) or not source.strip() or source == "none":
        blockers.append("quality_source_missing")
    if captures is not None and entry.get("captured_at") not in captures:
        blockers.append("quality_capture_binding_missing")

    if require_proof_hashes:
        for key in ("input_hash", "policy_hash"):
            value = entry.get(key)
            if not isinstance(value, str) or _SAVE_HASH_RE.fullmatch(value) is None:
                blockers.append(f"quality_{key}_invalid")
        policy_version = entry.get("policy_version")
        if not isinstance(policy_version, str) or not policy_version.strip():
            blockers.append("quality_policy_version_missing")
        # Self-declared hashes establish structure, not provenance.  A caller
        # must supply a trusted in-process verifier that recomputes the input
        # hash and checks the policy hash/version against an allowlist.  Until
        # such a producer exists, derived/certified fields remain unusable.
        if proof_verifier is None:
            blockers.append("quality_proof_unverified")
        else:
            try:
                if proof_verifier(entry) is not True:
                    blockers.append("quality_proof_unverified")
            except Exception:
                blockers.append("quality_proof_unverified")

    return sorted(set(blockers))


def quality_contract_matches(
    entry: Any,
    *,
    allowed_statuses: Tuple[str, ...] = ("observed",),
    semantics: Optional[Tuple[str, ...]] = None,
    units: Optional[Tuple[str, ...]] = None,
    scopes: Optional[Tuple[str, ...]] = None,
    completeness: Optional[Tuple[str, ...]] = ("complete",),
    authorities: Optional[Tuple[str, ...]] = None,
    sources: Optional[Tuple[str, ...]] = None,
    contract_ids: Optional[Tuple[str, ...]] = None,
    captures: Optional[Tuple[str, ...]] = ("snapshot.ts",),
    require_proof_hashes: bool = False,
    proof_verifier: Optional[Callable[[Dict[str, Any]], bool]] = None,
) -> bool:
    """Allocation-light predicate for hot-path gate reads."""

    if not isinstance(entry, dict) or entry.get("status") not in allowed_statuses:
        return False
    for name, allowed in (
        ("semantic", semantics),
        ("unit", units),
        ("scope", scopes),
        ("completeness", completeness),
        ("authority", authorities),
        ("source", sources),
        ("contract_id", contract_ids),
        ("captured_at", captures),
    ):
        if allowed is not None and entry.get(name) not in allowed:
            return False
    source = entry.get("source")
    if not isinstance(source, str) or not source.strip() or source == "none":
        return False
    if require_proof_hashes:
        input_hash = entry.get("input_hash")
        policy_hash = entry.get("policy_hash")
        policy_version = entry.get("policy_version")
        if (
            not isinstance(input_hash, str)
            or _SAVE_HASH_RE.fullmatch(input_hash) is None
            or not isinstance(policy_hash, str)
            or _SAVE_HASH_RE.fullmatch(policy_hash) is None
            or not isinstance(policy_version, str)
            or not policy_version.strip()
        ):
            return False
        if proof_verifier is None:
            return False
        try:
            if proof_verifier(entry) is not True:
                return False
        except Exception:
            return False
    return True


def is_usable(state: Optional[Dict[str, Any]], path: str, *, allow_estimated: bool = False) -> bool:
    status = field_status(state, path)
    return status == "observed" or (allow_estimated and status == "estimated")


def parse_timestamp(value: Any) -> Optional[dt.datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_positive_sequence(value: Any) -> Optional[int]:
    """Return an exact positive JSON integer, rejecting bool/string coercion."""

    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None


def is_state_snapshot(record: Dict[str, Any]) -> bool:
    return record.get("event") == "state_snapshot" and record.get("schema") == "oni.state.v2"


def is_session_started(record: Dict[str, Any]) -> bool:
    return record.get("event") == "session_started" and record.get("schema") == "oni.event.v2"


def parse_snapshot_timestamp(value: Any) -> Optional[dt.datetime]:
    """Parse an observation timestamp only when it has an explicit UTC offset."""

    parsed = parse_timestamp(value)
    if parsed is None or parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def trusted_sample_interval_seconds(state: Optional[Dict[str, Any]]) -> Optional[float]:
    interval = safe_get(state or {}, "sample.interval_seconds")
    if isinstance(interval, bool) or not isinstance(interval, (int, float)):
        return None
    try:
        numeric = float(interval)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(numeric):
        return None
    if not (MIN_TRUSTED_SAMPLE_INTERVAL_SECONDS <= numeric <= MAX_TRUSTED_SAMPLE_INTERVAL_SECONDS):
        return None
    return numeric


def _normalized_now(now: Optional[dt.datetime]) -> dt.datetime:
    current = now or dt.datetime.now(dt.timezone.utc).astimezone()
    if current.tzinfo is None or current.utcoffset() is None:
        return current.replace(tzinfo=dt.timezone.utc)
    return current


def snapshot_freshness(
    state: Optional[Dict[str, Any]],
    *,
    now: Optional[dt.datetime] = None,
    bridge_margin_seconds: float = 0.0,
) -> Tuple[Optional[float], Optional[float], List[str]]:
    """Return age, trusted stale threshold, and fail-closed freshness blockers.

    ``bridge_margin_seconds`` is the publishing Bridge's own iteration lag.  A
    snapshot's ``ts`` is fixed when the Probe samples it, but the Bridge only
    republishes the last record it processed, so the age a reader observes
    includes the Bridge's read-and-publish cadence on top of the Probe's sample
    interval.  With a pure ``interval * 3`` threshold a paused colony deadlocked:
    the Bridge published snapshots 19-22 s old against a 15 s limit, so every
    autonomous authorization was refused for ``snapshot_stale``, which kept the
    game paused and the snapshots old (2026-10-25 rounds 1-3).  Callers that
    know their own cadence pass it here; the Probe-relative rule is unchanged
    when the margin is zero.
    """

    blockers: List[str] = []
    observed_at = parse_snapshot_timestamp(state.get("ts") if isinstance(state, dict) else None)
    interval = trusted_sample_interval_seconds(state)
    current = _normalized_now(now)
    age: Optional[float] = None
    threshold: Optional[float] = None

    margin = 0.0
    if isinstance(bridge_margin_seconds, (int, float)) and not isinstance(bridge_margin_seconds, bool):
        candidate = float(bridge_margin_seconds)
        if math.isfinite(candidate) and candidate > 0:
            margin = candidate

    if observed_at is None:
        blockers.append("timestamp_invalid")
    else:
        delta = (current - observed_at).total_seconds()
        if delta < -MAX_FUTURE_CLOCK_SKEW_SECONDS:
            blockers.append("timestamp_in_future")
        else:
            age = max(0.0, delta)

    if interval is None:
        blockers.append("sample_interval_invalid")
    else:
        threshold = interval * 3.0 + margin
        if age is not None and age > threshold:
            blockers.append("snapshot_stale")

    return age, threshold, sorted(set(blockers))


def snapshot_age_seconds(state: Optional[Dict[str, Any]], now: Optional[dt.datetime] = None) -> Optional[float]:
    age, _, blockers = snapshot_freshness(state, now=now)
    if {"timestamp_invalid", "timestamp_in_future", "sample_interval_invalid"}.intersection(blockers):
        return None
    return age


def stale_after_seconds(state: Optional[Dict[str, Any]], multiplier: float = 3.0) -> float:
    interval = trusted_sample_interval_seconds(state)
    if interval is None or isinstance(multiplier, bool) or not isinstance(multiplier, (int, float)):
        return 0.0
    numeric_multiplier = float(multiplier)
    if not math.isfinite(numeric_multiplier) or numeric_multiplier <= 0:
        return 0.0
    return max(1.0, interval * numeric_multiplier)


def _is_nonnegative_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_finite_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def _exact_world_ids(value: Any) -> Optional[List[int]]:
    if not isinstance(value, list):
        return None
    if any(not _is_nonnegative_integer(item) for item in value):
        return None
    if len(value) != len(set(value)):
        return None
    return value


def alerts_cover_all_worlds(state: Dict[str, Any]) -> bool:
    """True when the snapshot carries a usable alert set spanning every world."""

    entry = field_quality(state, "alerts")
    if not (
        entry.get("status") == "observed"
        and entry.get("scope") == ALERTS_ALL_WORLDS_SCOPE
        and entry.get("source") == ALERTS_ALL_WORLDS_SOURCE
    ):
        return False
    # field_contracts imports this module; resolve it at call time.
    from agent.field_contracts import ALERTS_CANONICAL_CONTRACT_ID, read_contract_field

    return read_contract_field(state, "alerts", ALERTS_CANONICAL_CONTRACT_ID).usable


def observation_scope_coverage_blockers(
    state: Dict[str, Any],
    *,
    active_world_id: Any,
    populated_world_ids: Optional[List[int]],
    covered_world_ids: Optional[List[int]],
    complete: Any,
) -> List[str]:
    """Multi-world aware coverage check (review M8).

    The Probe's per-world readers cover the camera world; duplicants may also
    live in a rocket or on another asteroid, and the camera may look at a
    world without any.  Such a snapshot stays trusted when its survival
    alerts cover every world, because an unobserved world can then still
    raise a suffocation or starvation pause.  With active-world-only alerts
    the old rule applies: every populated world must be covered.
    """

    # The Probe covers exactly the camera world; a wider covered set or a
    # "complete" claim for any other world is a forged scope.
    if not _is_nonnegative_integer(active_world_id):
        return ["active_world_id_invalid"]
    blockers: List[str] = []
    expected_world_ids = [active_world_id]
    if covered_world_ids != expected_world_ids:
        blockers.append("covered_world_ids_not_active_world_only")
    if complete is True and populated_world_ids != expected_world_ids:
        blockers.append("complete_populated_world_ids_not_active_world_only")
    if complete is not True and not alerts_cover_all_worlds(state):
        blockers.append("populated_world_coverage_incomplete")
    return blockers


def snapshot_trust_blockers(
    state: Optional[Dict[str, Any]],
    *,
    now: Optional[dt.datetime] = None,
    bridge_margin_seconds: float = 0.0,
) -> List[str]:
    """Return stable fail-closed blocker codes for a live Probe snapshot.

    ``bridge_margin_seconds`` is forwarded to the freshness rule so a caller that
    knows its own publish cadence does not report ``snapshot_stale`` for lag it
    introduces itself.
    """

    if not isinstance(state, dict):
        return ["snapshot_missing"]

    blockers: List[str] = []
    if not is_state_snapshot(state):
        blockers.append("snapshot_envelope_invalid")

    session_id = state.get("session_id")
    sequence = parse_positive_sequence(state.get("seq"))
    snapshot_sequence = parse_positive_sequence(state.get("snapshot_seq"))
    if not isinstance(session_id, str) or not session_id.strip():
        blockers.append("session_id_invalid")
    if sequence is None:
        blockers.append("sequence_invalid")
    if snapshot_sequence is None or (sequence is not None and snapshot_sequence > sequence):
        blockers.append("snapshot_sequence_invalid")
    expected_snapshot_id = f"{session_id}:{sequence}" if isinstance(session_id, str) and sequence else None
    if not expected_snapshot_id or state.get("snapshot_id") != expected_snapshot_id:
        blockers.append("snapshot_id_invalid")

    save_hash = state.get("save_id_hash")
    if not isinstance(save_hash, str) or _SAVE_HASH_RE.fullmatch(save_hash) is None:
        blockers.append("save_id_hash_invalid")
    world_id = state.get("world_id")
    if not _is_nonnegative_integer(world_id):
        blockers.append("world_id_invalid")
    observation_scope = state.get("observation_scope")
    if not isinstance(observation_scope, dict):
        blockers.append("observation_scope_missing")
        active_world_id = None
    else:
        active_world_id = observation_scope.get("active_world_id")
        populated_world_ids = _exact_world_ids(
            observation_scope.get("populated_world_ids")
        )
        covered_world_ids = _exact_world_ids(
            observation_scope.get("covered_world_ids")
        )
        complete = observation_scope.get("complete_for_populated_worlds")
        blockers.extend(
            observation_scope_coverage_blockers(
                state,
                active_world_id=active_world_id,
                populated_world_ids=populated_world_ids,
                covered_world_ids=covered_world_ids,
                complete=complete,
            )
        )
    if (
        _is_nonnegative_integer(world_id)
        and _is_nonnegative_integer(active_world_id)
        and world_id != active_world_id
    ):
        blockers.append("active_world_identity_mismatch")
    for key in ("game_build", "mod_version"):
        if not isinstance(state.get(key), str) or not str(state.get(key)).strip():
            blockers.append(f"{key}_invalid")
    if not isinstance(state.get("save_dirty"), bool):
        blockers.append("save_dirty_invalid")
    if not isinstance(state.get("quality"), dict):
        blockers.append("quality_missing")

    if "cycle" in state and (
        not _is_finite_number(state.get("cycle"))
        or float(state["cycle"]) < 1.0
        or not float(state["cycle"]).is_integer()
    ):
        blockers.append("cycle_invalid")
    # Native GetTimeInCycles()+1 is continuous; fractional finite values are
    # legitimate. Validate its scalar shape without rounding it to a cycle.
    if "game_time_cycles" in state and (
        not _is_finite_number(state.get("game_time_cycles"))
        or float(state["game_time_cycles"]) < 0.0
    ):
        blockers.append("game_time_cycles_invalid")

    _, _, freshness_blockers = snapshot_freshness(
        state, now=now, bridge_margin_seconds=bridge_margin_seconds
    )
    blockers.extend(freshness_blockers)

    writer = state.get("writer")
    if not isinstance(writer, dict):
        blockers.append("writer_status_missing")
    else:
        dropped = writer.get("dropped_snapshot_count")
        if not _is_nonnegative_integer(dropped):
            blockers.append("writer_drop_count_invalid")
        elif dropped > 0:
            blockers.append("writer_dropped_snapshots")

        queue_depth = writer.get("queue_depth")
        if not _is_nonnegative_integer(queue_depth):
            blockers.append("writer_queue_depth_invalid")
        elif queue_depth > 0:
            blockers.append("writer_queue_not_empty")

        fault_count = writer.get("fault_count")
        if not _is_nonnegative_integer(fault_count):
            blockers.append("writer_fault_count_invalid")
        elif fault_count > 0:
            blockers.append("writer_faults_detected")

        non_finite_count = writer.get("non_finite_numeric_count")
        if not _is_nonnegative_integer(non_finite_count):
            blockers.append("writer_non_finite_count_invalid")
        elif non_finite_count > 0:
            blockers.append("writer_non_finite_numbers")

        writer_fault = writer.get("fault")
        if writer_fault is not None and writer_fault != "" and writer_fault is not False:
            blockers.append("writer_fault")

    return sorted(set(blockers))


def mark_stale(state: Dict[str, Any], age_seconds: float) -> Dict[str, Any]:
    result = copy.deepcopy(state)
    quality = result.get("quality")
    if isinstance(quality, dict):
        for entry in quality.values():
            if not isinstance(entry, dict):
                continue
            status = entry.get("status")
            if status in USABLE_STATUSES:
                entry["original_status"] = status
                entry["status"] = "stale"
    result["bridge"] = {
        "stale": True,
        "snapshot_age_seconds": round(age_seconds, 3),
    }
    return result


def canonical_state_hash(state: Optional[Dict[str, Any]]) -> str:
    """Hash the complete decision surface of a snapshot.

    This intentionally uses an exclusion list instead of a field allowlist so
    that new probe telemetry is audit-bound by default.  Only data injected by
    the Bridge, or measurements which describe capture/transport cost rather
    than colony state, are removed.  ``sample.interval_seconds`` remains in the
    hash because freshness decisions depend on it.
    """

    # Only these two containers are changed. JSON encoding reads the nested
    # telemetry without mutating it, so recursively copying every field adds
    # replay cost without providing additional isolation.
    payload = dict(state) if isinstance(state, dict) else {}
    for field in _STATE_HASH_EXCLUDED_TOP_LEVEL_FIELDS:
        payload.pop(field, None)

    sample = payload.get("sample")
    if isinstance(sample, dict):
        sample = dict(sample)
        payload["sample"] = sample
        for field in _STATE_HASH_EXCLUDED_SAMPLE_FIELDS:
            sample.pop(field, None)

    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def quality_summary(state: Optional[Dict[str, Any]]) -> Tuple[int, int, int]:
    quality = state.get("quality") if isinstance(state, dict) else None
    if not isinstance(quality, dict):
        return (0, 0, 0)
    observed = 0
    estimated = 0
    blocked = 0
    for entry in quality.values():
        if not isinstance(entry, dict):
            continue
        status = entry.get("status")
        if status == "observed":
            observed += 1
        elif status == "estimated":
            estimated += 1
        elif status in BLOCKED_STATUSES:
            blocked += 1
    return observed, estimated, blocked
