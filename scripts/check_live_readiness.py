#!/usr/bin/env python3
"""Read-only, fail-closed observation/capability gate for a live ONI Bridge bundle.

The Bridge publishes ``decision-frame.json`` last as the generation commit
marker.  This checker reads that marker before and after ``latest_state.json``
and ``health.json`` so a partially published generation is never accepted.
It writes nothing to the Bridge directory and never creates commands.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import pathlib
import stat
import sys
import time
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple


ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import state_quality  # noqa: E402
from agent.decision import DecisionFrameError, validate_decision_frame  # noqa: E402
from tools.game_verify import verify_power_telemetry_contract_v2 as power_v2  # noqa: E402
from tools.game_verify import verify_telemetry_contract as telemetry_v1  # noqa: E402


REPORT_SCHEMA = "oni.live-readiness.v1"
MAX_BUNDLE_FILE_BYTES = 32 * 1024 * 1024
MAX_TIMEOUT_SECONDS = 3600.0
MIN_POLL_SECONDS = 0.05
MAX_POLL_SECONDS = 60.0
DEFAULT_V1_CONTRACT = ROOT / "tools" / "game_verify" / "contracts" / "oni_telemetry.v1.json"
DEFAULT_POWER_V2_CONTRACT = (
    ROOT / "tools" / "game_verify" / "contracts" / "oni_power_telemetry.v2.json"
)


class BundleReadError(ValueError):
    """A safe diagnostic code for an unreadable or unstable output bundle."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def local_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).astimezone()


def default_bridge_dir() -> pathlib.Path:
    return (
        pathlib.Path.home()
        / "Documents"
        / "Klei"
        / "OxygenNotIncluded"
        / "ai-bridge"
    ).resolve()


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-standard numeric constant: {value}")


def _read_json_object(path: pathlib.Path, label: str) -> Tuple[Dict[str, Any], bytes]:
    descriptor = None
    try:
        before = path.lstat()
        if path.is_symlink() or bool(int(getattr(before, "st_file_attributes", 0)) & 0x400):
            raise BundleReadError(f"{label}_link_forbidden")
        if not stat.S_ISREG(before.st_mode):
            raise BundleReadError(f"{label}_regular_file_required")
        if int(before.st_nlink) != 1:
            raise BundleReadError(f"{label}_hardlink_forbidden")
        size = int(before.st_size)
        if size <= 0:
            raise BundleReadError(f"{label}_empty")
        if size > MAX_BUNDLE_FILE_BYTES:
            raise BundleReadError(f"{label}_too_large")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(str(path), flags)
        opened = os.fstat(descriptor)
        if (int(opened.st_dev), int(opened.st_ino)) != (int(before.st_dev), int(before.st_ino)):
            raise BundleReadError(f"{label}_identity_changed_before_read")
        chunks = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        named_after = path.lstat()
        if ((int(after.st_dev), int(after.st_ino)) != (int(before.st_dev), int(before.st_ino)) or
                (int(named_after.st_dev), int(named_after.st_ino)) != (int(before.st_dev), int(before.st_ino)) or
                int(after.st_size) != size or int(named_after.st_size) != size or len(raw) != size or
                int(getattr(after, "st_mtime_ns", 0)) != int(getattr(before, "st_mtime_ns", 0))):
            raise BundleReadError(f"{label}_changed_during_read")
    except FileNotFoundError as error:
        raise BundleReadError(f"{label}_missing") from error
    except BundleReadError:
        raise
    except OSError as error:
        raise BundleReadError(f"{label}_unreadable") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)

    if len(raw) > MAX_BUNDLE_FILE_BYTES:
        raise BundleReadError(f"{label}_too_large")
    try:
        value = json.loads(
            raw.decode("utf-8-sig"),
            parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise BundleReadError(f"{label}_json_invalid") from error
    if not isinstance(value, dict):
        raise BundleReadError(f"{label}_object_required")
    return value, raw


def read_stable_bundle(root: pathlib.Path) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    """Read one committed Bridge generation without locking or mutation."""

    frame_path = root / "decision-frame.json"
    state_path = root / "latest_state.json"
    health_path = root / "health.json"
    frame_before, frame_before_raw = _read_json_object(frame_path, "decision_frame")
    state, _ = _read_json_object(state_path, "latest_state")
    health, _ = _read_json_object(health_path, "health")
    frame_after, frame_after_raw = _read_json_object(frame_path, "decision_frame")
    if frame_before_raw != frame_after_raw:
        raise BundleReadError("bundle_generation_changed")
    try:
        validate_decision_frame(frame_after)
    except (DecisionFrameError, TypeError, ValueError) as error:
        raise BundleReadError("decision_frame_invalid") from error
    return state, health, frame_after


def _is_exact_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_finite_number(value: Any) -> bool:
    return telemetry_v1.exact_number(value)


def _safe_get(value: Mapping[str, Any], path: str) -> Any:
    current: Any = value
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def _source_identity(state: Mapping[str, Any]) -> Dict[str, Any]:
    try:
        state_hash: Optional[str] = state_quality.canonical_state_hash(dict(state))
    except (TypeError, ValueError, OverflowError):
        state_hash = None
    return {
        "session_id": state.get("session_id"),
        "save_id_hash": state.get("save_id_hash"),
        "world_id": state.get("world_id"),
        "snapshot_id": state.get("snapshot_id"),
        "seq": state.get("seq"),
        "snapshot_seq": state.get("snapshot_seq"),
        "ts": state.get("ts"),
        "state_hash": state_hash,
    }


def binding_blockers(
    state: Mapping[str, Any],
    health: Mapping[str, Any],
    frame: Mapping[str, Any],
    *,
    now: dt.datetime,
) -> list[str]:
    """Bind state, its in-object quality map, Bridge health, and commit marker."""

    blockers: set[str] = set()
    identity = _source_identity(state)
    frame_source = frame.get("source")
    if not isinstance(frame_source, Mapping):
        blockers.add("frame_source_missing")
    else:
        for field, expected in identity.items():
            if frame_source.get(field) != expected:
                blockers.add(f"frame_source_{field}_mismatch")

    state_hash = identity["state_hash"]
    if state_hash is None:
        blockers.add("state_hash_invalid")
    else:
        blockers.update(
            telemetry_v1.health_binding_blockers(
                health,
                state,
                state_hash=state_hash,
            )
        )

    if health.get("replay_mode") is not False:
        blockers.add("health_replay_mode_not_live")
    if health.get("session_start_observed") is not True:
        blockers.add("health_session_start_not_observed")
    if health.get("decision_frame_id") != frame.get("frame_id"):
        blockers.add("health_decision_frame_id_mismatch")
    frame_trust = frame.get("trust")
    if not isinstance(frame_trust, Mapping) or frame_trust.get("status") != "trusted":
        blockers.add("decision_frame_not_trusted")
    if health.get("decision_frame_trust") != "trusted":
        blockers.add("health_decision_frame_trust_not_trusted")

    readiness = frame.get("readiness")
    expected_readiness = {
        stage: bool(entry.get("ready"))
        for stage, entry in readiness.items()
        if isinstance(stage, str) and isinstance(entry, Mapping)
    } if isinstance(readiness, Mapping) else {}
    if health.get("decision_frame_readiness") != expected_readiness:
        blockers.add("health_decision_frame_readiness_mismatch")
    observe = readiness.get("observe") if isinstance(readiness, Mapping) else None
    if not isinstance(observe, Mapping) or observe.get("ready") is not True:
        blockers.add("decision_frame_observe_not_ready")

    projection = _safe_get(frame, "projections.health")
    if not isinstance(projection, Mapping):
        blockers.add("frame_health_projection_missing")
    else:
        # The frame no longer projects snapshot_age_seconds (it changed the
        # frame digest on every poll); freshness is carried by status and
        # trust_ready, which remain bound here.
        for field in (
            "status",
            "trust_ready",
            "replay_mode",
            "session_start_observed",
        ):
            if projection.get(field) != health.get(field):
                blockers.add(f"frame_health_{field}_mismatch")
        if projection.get("snapshot_age_seconds") is not None:
            blockers.add("frame_health_snapshot_age_seconds_projected")

    bridge = state.get("bridge")
    if not isinstance(bridge, Mapping):
        blockers.add("state_bridge_metadata_missing")
    else:
        if bridge.get("stale") is not False:
            blockers.add("state_bridge_stale")
        if bridge.get("trust_ready") is not True:
            blockers.add("state_bridge_trust_not_ready")
        bridge_blockers = bridge.get("trust_blockers")
        if not isinstance(bridge_blockers, list):
            blockers.add("state_bridge_blockers_invalid")
        elif bridge_blockers:
            blockers.add("state_bridge_blockers_nonempty")
        if bridge.get("replay_mode") is not False:
            blockers.add("state_bridge_replay_mode_not_live")

    try:
        snapshot_time = telemetry_v1.parse_aware_timestamp(state.get("ts"), "ts")
        health_time = telemetry_v1.parse_aware_timestamp(
            health.get("updated_at"), "health.updated_at"
        )
        if health_time < snapshot_time:
            blockers.add("health_timestamp_precedes_snapshot")
        if (health_time - now).total_seconds() > state_quality.MAX_FUTURE_CLOCK_SKEW_SECONDS:
            blockers.add("health_timestamp_in_future")
    except telemetry_v1.ContractError:
        blockers.add("health_timestamp_invalid")
    return sorted(blockers)


def material_scan_report(state: Mapping[str, Any]) -> Dict[str, Any]:
    cells = _safe_get(state, "materials.active_world_cells")
    blockers: set[str] = set()
    if not isinstance(cells, Mapping):
        return {
            "ready": False,
            "blockers": ["material_scan_missing"],
            "scan_generation": None,
            "progress_ratio": None,
        }

    candidate = cells.get("candidate_cell_count")
    scanned = cells.get("scanned_candidate_cell_count")
    remaining = cells.get("remaining_candidate_cell_count")
    progress = cells.get("scan_progress_ratio")
    scan_generation = cells.get("scan_generation")
    aggregate_generation = cells.get("aggregate_scan_generation")
    if cells.get("scan_complete") is not True:
        blockers.add("material_scan_not_complete")
    if cells.get("scan_status") != "complete":
        blockers.add("material_scan_status_not_complete")
    if not _is_exact_integer(candidate) or candidate <= 0:
        blockers.add("material_candidate_count_invalid")
    if not _is_exact_integer(scanned) or scanned != candidate:
        blockers.add("material_scanned_count_incomplete")
    if not _is_exact_integer(remaining) or remaining != 0:
        blockers.add("material_remaining_count_nonzero")
    if not _is_finite_number(progress) or abs(float(progress) - 1.0) > 0.000001:
        blockers.add("material_progress_not_complete")
    if not _is_exact_integer(scan_generation) or scan_generation <= 0:
        blockers.add("material_scan_generation_invalid")
    if aggregate_generation != scan_generation:
        blockers.add("material_aggregate_generation_mismatch")
    if cells.get("aggregate_available") is not True:
        blockers.add("material_aggregate_unavailable")

    scan_start = cells.get("scan_window_started_at")
    scan_end = cells.get("scan_window_completed_at")
    aggregate_start = cells.get("aggregate_window_started_at")
    aggregate_end = cells.get("aggregate_window_completed_at")
    try:
        telemetry_v1.parse_aware_timestamp(scan_start, "scan_window_started_at")
        telemetry_v1.parse_aware_timestamp(scan_end, "scan_window_completed_at")
        telemetry_v1.parse_aware_timestamp(aggregate_start, "aggregate_window_started_at")
        telemetry_v1.parse_aware_timestamp(aggregate_end, "aggregate_window_completed_at")
    except telemetry_v1.ContractError:
        blockers.add("material_scan_timestamp_invalid")
    if aggregate_start != scan_start or aggregate_end != scan_end:
        blockers.add("material_aggregate_window_mismatch")

    aggregate_age = cells.get("aggregate_age_seconds")
    aggregate_max_age = cells.get("aggregate_max_age_seconds")
    if (
        not _is_finite_number(aggregate_age)
        or not _is_finite_number(aggregate_max_age)
        or float(aggregate_age) < 0.0
        or float(aggregate_max_age) < 0.0
        or float(aggregate_age) > float(aggregate_max_age)
    ):
        blockers.add("material_aggregate_stale_or_invalid")

    quality = state.get("quality")
    for path in (
        "materials.active_world_cells.scan_complete",
        "materials.active_world_cells.scan_progress_ratio",
    ):
        entry = quality.get(path) if isinstance(quality, Mapping) else None
        if not isinstance(entry, Mapping):
            blockers.add(f"material_quality_missing:{path}")
        elif not (
            entry.get("status") == "observed"
            and entry.get("completeness") == "complete"
            and entry.get("captured_at") == "snapshot.ts"
        ):
            blockers.add(f"material_quality_invalid:{path}")

    return {
        "ready": not blockers,
        "blockers": sorted(blockers),
        "scan_generation": scan_generation,
        "aggregate_scan_generation": aggregate_generation,
        "progress_ratio": progress if _is_finite_number(progress) else None,
        "remaining_candidate_cell_count": remaining if _is_exact_integer(remaining) else None,
    }


def load_contracts(
    v1_contract_path: pathlib.Path = DEFAULT_V1_CONTRACT,
    power_v2_contract_path: pathlib.Path = DEFAULT_POWER_V2_CONTRACT,
) -> Tuple[Dict[str, Any], pathlib.Path, pathlib.Path]:
    v1_path = v1_contract_path.resolve()
    power_path = power_v2_contract_path.resolve()
    v1_contract = telemetry_v1.load_json(v1_path)
    telemetry_v1.validate_contract_definition(v1_contract, v1_path)
    power_contract = power_v2.load_json(power_path)
    power_v2.validate_contract_definition(power_contract, power_path)
    return v1_contract, v1_path, power_path


def _unreadable_report(code: str, *, material_required: bool) -> Dict[str, Any]:
    return {
        "schema": REPORT_SCHEMA,
        "status": "blocked",
        "observation_ready": False,
        "ready": False,
        "read_only": True,
        "legacy_v1_compatible": False,
        "source": None,
        "bundle": {
            "accepted": False,
            "stable_generation": False,
            "decision_frame_id": None,
            "blockers": [code],
        },
        "admission": {"accepted": False, "blockers": ["bundle_not_readable"]},
        "telemetry_v1": {
            "accepted": False,
            "command_eligible": False,
            "blockers": ["not_evaluated"],
            "health_blockers": ["not_evaluated"],
        },
        "power_v2": {
            "accepted": False,
            "storage_mode": None,
            "percent_dependent_actions_eligible": False,
            "percent_dependent_actions_blocked": True,
            "blockers": ["not_evaluated"],
            "action_blockers": ["not_evaluated"],
        },
        "material_scan": {
            "required": material_required,
            "ready": False,
            "blockers": ["not_evaluated"],
        },
        "blockers": [f"bundle:{code}"],
    }


def assess_readiness(
    root: pathlib.Path,
    *,
    now: dt.datetime,
    require_material_scan: bool,
    contracts: Tuple[Dict[str, Any], pathlib.Path, pathlib.Path],
) -> Dict[str, Any]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("readiness time must include a UTC offset")
    try:
        state, health, frame = read_stable_bundle(root)
    except BundleReadError as error:
        return _unreadable_report(error.code, material_required=require_material_scan)

    v1_contract, v1_path, power_v2_path = contracts
    binding = binding_blockers(state, health, frame, now=now)
    admission = state_quality.snapshot_trust_blockers(dict(state), now=now)
    try:
        v1_report = telemetry_v1.validate_live_snapshot(
            dict(state),
            v1_contract,
            now=now,
            health=dict(health),
            contract_path=v1_path,
        )
    except (telemetry_v1.ContractError, TypeError, ValueError, OverflowError):
        v1_report = {
            "accepted": False,
            "command_eligible": False,
            "snapshot_blockers": ["contract_evaluation_failed"],
            "health_blockers": ["contract_evaluation_failed"],
        }
    try:
        power_report = power_v2.verify_snapshot(
            dict(state),
            contract_path=power_v2_path,
            now=now,
        )
    except (power_v2.PowerContractError, telemetry_v1.ContractError, TypeError, ValueError, OverflowError):
        power_report = {
            "accepted": False,
            "percent_dependent_actions_eligible": False,
            "storage_mode": _safe_get(state, "power.storage_mode"),
            "blockers": ["contract_evaluation_failed"],
            "action_blockers": ["contract_evaluation_failed"],
        }
    materials = material_scan_report(state)

    percent_eligible = power_report.get("percent_dependent_actions_eligible") is True
    raw_power_mode = power_report.get("storage_mode")
    power_mode = (
        raw_power_mode
        if isinstance(raw_power_mode, str)
        and raw_power_mode in {"bounded", "unbounded", "invalid"}
        else None
    )
    # Observation admission is deliberately separate from legacy-v1
    # compatibility and action capabilities.  An honest unbounded v2 power
    # sample is useful read-only telemetry even though v1 cannot represent it
    # and no battery-percent action may consume it.
    overall: list[str] = []
    overall.extend(f"bundle:{item}" for item in binding)
    overall.extend(f"admission:{item}" for item in admission)
    if power_report.get("accepted") is not True:
        overall.append("power_v2:not_accepted")
    if require_material_scan and materials.get("ready") is not True:
        overall.append("materials:full_scan_not_ready")
    overall = sorted(set(overall))

    identity = _source_identity(state)
    observation_ready = not overall
    legacy_v1_compatible = v1_report.get("accepted") is True
    return {
        "schema": REPORT_SCHEMA,
        "status": "ready" if observation_ready else "blocked",
        "observation_ready": observation_ready,
        # Backward-compatible alias for callers written before readiness was
        # split from legacy/action capability diagnostics.
        "ready": observation_ready,
        "read_only": True,
        "legacy_v1_compatible": legacy_v1_compatible,
        "source": identity,
        "bundle": {
            "accepted": not binding,
            "stable_generation": True,
            "decision_frame_id": frame.get("frame_id"),
            "blockers": binding,
        },
        "admission": {"accepted": not admission, "blockers": admission},
        "telemetry_v1": {
            "accepted": v1_report.get("accepted") is True,
            "command_eligible": v1_report.get("command_eligible") is True,
            "blockers": list(v1_report.get("snapshot_blockers") or []),
            "health_blockers": list(v1_report.get("health_blockers") or []),
        },
        "power_v2": {
            "accepted": power_report.get("accepted") is True,
            "storage_mode": power_mode,
            "percent_dependent_actions_eligible": percent_eligible,
            "percent_dependent_actions_blocked": not percent_eligible,
            "blockers": list(power_report.get("blockers") or []),
            "action_blockers": list(power_report.get("action_blockers") or []),
        },
        "material_scan": {"required": require_material_scan, **materials},
        "blockers": overall,
    }


def wait_for_readiness(
    root: pathlib.Path,
    *,
    timeout_seconds: float,
    poll_interval_seconds: float,
    require_material_scan: bool,
    contracts: Tuple[Dict[str, Any], pathlib.Path, pathlib.Path],
    now_fn: Callable[[], dt.datetime] = local_now,
    monotonic_fn: Callable[[], float] = time.monotonic,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> Dict[str, Any]:
    start = monotonic_fn()
    deadline = start + timeout_seconds
    maximum_attempts = max(1, int(math.ceil(timeout_seconds / poll_interval_seconds)) + 2)
    attempts = 0
    while True:
        attempts += 1
        report = assess_readiness(
            root,
            now=now_fn(),
            require_material_scan=require_material_scan,
            contracts=contracts,
        )
        current = monotonic_fn()
        report["attempts"] = attempts
        report["elapsed_seconds"] = round(max(0.0, current - start), 3)
        report["timeout_seconds"] = timeout_seconds
        if report["observation_ready"]:
            report["status"] = "ready"
            return report
        if timeout_seconds <= 0.0:
            report["status"] = "not_ready"
            return report
        if current >= deadline or attempts >= maximum_attempts:
            report["status"] = "timeout"
            return report
        sleep_fn(min(poll_interval_seconds, max(0.0, deadline - current)))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bridge-dir", type=pathlib.Path, default=default_bridge_dir())
    parser.add_argument("--timeout-seconds", type=float, default=0.0)
    parser.add_argument("--poll-interval-seconds", type=float, default=1.0)
    parser.add_argument(
        "--wait-for-material-scan",
        action="store_true",
        help="Require a current, generation-matched full active-world material scan.",
    )
    parser.add_argument("--v1-contract", type=pathlib.Path, default=DEFAULT_V1_CONTRACT)
    parser.add_argument(
        "--power-v2-contract",
        type=pathlib.Path,
        default=DEFAULT_POWER_V2_CONTRACT,
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if not _is_finite_number(args.timeout_seconds) or not (
        0.0 <= float(args.timeout_seconds) <= MAX_TIMEOUT_SECONDS
    ):
        raise SystemExit("--timeout-seconds must be finite and between 0 and 3600")
    if not _is_finite_number(args.poll_interval_seconds) or not (
        MIN_POLL_SECONDS <= float(args.poll_interval_seconds) <= MAX_POLL_SECONDS
    ):
        raise SystemExit("--poll-interval-seconds must be finite and between 0.05 and 60")
    try:
        contracts = load_contracts(args.v1_contract, args.power_v2_contract)
    except (OSError, telemetry_v1.ContractError, power_v2.PowerContractError, ValueError):
        payload = {
            "schema": REPORT_SCHEMA,
            "status": "error",
            "observation_ready": False,
            "ready": False,
            "read_only": True,
            "legacy_v1_compatible": False,
            "blockers": ["contract_initialization_failed"],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))
        return 2
    report = wait_for_readiness(
        args.bridge_dir.expanduser().resolve(),
        timeout_seconds=float(args.timeout_seconds),
        poll_interval_seconds=float(args.poll_interval_seconds),
        require_material_scan=bool(args.wait_for_material_scan),
        contracts=contracts,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))
    return 0 if report["observation_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
