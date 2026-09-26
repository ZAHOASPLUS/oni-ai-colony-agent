#!/usr/bin/env python3
"""Prepare and verify probe-backed ONI runtime scenarios."""

from __future__ import annotations

import argparse
import collections
import contextlib
import copy
import datetime as dt
import hashlib
import io
import json
import math
import os
import pathlib
import secrets
import statistics
import subprocess
import sys
import tempfile
import time
import traceback
from typing import Any, Dict, Iterable, List, Optional, Tuple


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.advanced_actions import (
    blueprint_cell_path,
    build_cancel_building_blueprint_hint,
    build_cancel_dig_hint,
    build_cancel_utility_blueprint_path_hint,
    build_duplicant_priority_hint,
    build_mark_dig_hint,
    build_place_building_blueprint_hint,
    build_place_utility_blueprint_path_hint,
    dig_cell_path,
    duplicant_priority_path,
    utility_path_path,
)
from agent.command_queue import COMMAND_DIRS, ensure_command_dirs, load_policy, write_json_atomic
from agent.command_custody import CommandCustodyTimeout, lifecycle_custody
from agent.decision import validate_decision_frame
from agent.decision_frame_archive import (
    CurrentDecisionContext,
    DecisionFrameArchiveError,
    assert_current_decision_context,
    load_current_decision_context,
)
from agent.field_contracts import (
    ALERTS_CANONICAL_CONTRACT_ID,
    GAME_PAUSED_CONTRACT_ID,
    RESEARCH_CURRENT_CONTRACT_ID,
    build_condition,
    read_contract_field,
)
from agent.state_quality import canonical_state_hash, safe_get, snapshot_age_seconds
from agent.runtime_confirmation import (
    LEGACY_MUTATION_MODE,
    REQUIRED_MUTATION_ACKS,
    annotate_command_confirmations,
    archived_confirmation_records,
    assess_command_confirmation,
    confirmed_mutating_command,
)
from scripts.manage_save_lineage import load_json as load_lineage_json
from scripts.manage_save_lineage import sha256_file as lineage_sha256_file
from scripts.manage_save_lineage import verify_lineage
from scripts.check_live_readiness import _read_json_object, binding_blockers


DEFAULT_BRIDGE = pathlib.Path.home() / "Documents" / "Klei" / "OxygenNotIncluded" / "ai-bridge"
DEFAULT_PLAYER_LOG = pathlib.Path(os.environ.get("USERPROFILE", str(pathlib.Path.home()))) / "AppData" / "LocalLow" / "Klei" / "Oxygen Not Included" / "Player.log"
RUNTIME_REPORT = PROJECT_ROOT / "reports" / "runtime-acceptance.json"
MARKER_DIR = PROJECT_ROOT / "reports" / "runtime-markers"
COLONY_PLAN_LIVE_REPORT = PROJECT_ROOT / "reports" / "colony-plan-live.json"
CRITICAL_QUALITY = (
    "game.paused",
    "cycle",
    "duplicants.total",
    "resources.food_kcal",
    "research.current",
)
ALL_MARKERS = (
    "observer_start",
    "pause_runtime",
    "research_runtime",
    "duplicant_priority_runtime",
    "duplicant_priority_invalid_target",
    "dig_designation_runtime",
    "dig_designation_invalid_target",
    "building_blueprint_runtime",
    "utility_blueprint_path_runtime",
    "blueprint_invalid_target",
    "stale_command_quarantine",
    "duplicate_command_idempotency",
    "colony_plan_runtime",
    "probe_performance_10m",
    "save_lineage_runtime",
)
REQUIRED_LINEAGE_STEPS = (
    "observer_start",
    "pause_runtime",
    "research_runtime",
    "duplicant_priority_runtime",
    "duplicant_priority_invalid_target",
    "dig_designation_runtime",
    "dig_designation_invalid_target",
    "building_blueprint_runtime",
    "utility_blueprint_path_runtime",
    "blueprint_invalid_target",
    "stale_command_quarantine",
    "duplicate_command_idempotency",
    "colony_plan_runtime",
    "probe_performance_10m",
)
BATCH_A_MARKERS = (
    "observer_start",
    "duplicant_priority_runtime",
    "duplicant_priority_invalid_target",
)
BATCH_B_MARKERS = (
    "observer_start",
    "dig_designation_runtime",
    "dig_designation_invalid_target",
)
BATCH_C_MARKERS = (
    "observer_start",
    "building_blueprint_runtime",
    "utility_blueprint_path_runtime",
    "blueprint_invalid_target",
)


def local_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).astimezone()


def now_iso() -> str:
    return local_now().isoformat(timespec="seconds")


def load_json(path: pathlib.Path) -> Dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def bridge_dir(value: Optional[str]) -> pathlib.Path:
    return pathlib.Path(value).expanduser().resolve() if value else DEFAULT_BRIDGE.resolve()


def read_latest_state(root: pathlib.Path) -> Dict[str, Any]:
    path = root / "latest_state.json"
    if not path.exists():
        raise RuntimeError("Bridge latest_state.json is missing; start ONI and the Bridge first.")
    state = load_json(path)
    return validate_latest_state(state)


def validate_latest_state(state: Dict[str, Any]) -> Dict[str, Any]:
    if state.get("schema") != "oni.state.v2" or state.get("event") != "state_snapshot":
        raise RuntimeError("Bridge does not contain a current oni.state.v2 snapshot.")
    if safe_get(state, "bridge.stale", False):
        raise RuntimeError("Latest state is stale; no runtime command was created.")
    age = snapshot_age_seconds(state)
    if age is not None and age > 20:
        raise RuntimeError(f"Latest state is too old for a command ({age:.1f}s).")
    return state


def validate_policy_for_command(
    root: pathlib.Path,
    state: Dict[str, Any],
    action: str,
    *,
    policy: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    policy = policy if isinstance(policy, dict) else load_policy(root)
    if policy.get("schema") != "oni.action-policy.v1" or policy.get("actions_enabled") is not True:
        raise RuntimeError("Action policy is not explicitly enabled.")
    if action not in policy.get("allowed_actions", []):
        raise RuntimeError(f"Action is not allowed by policy: {action}")
    if state.get("save_id_hash") not in policy.get("allowed_save_hashes", []):
        raise RuntimeError("Current save hash is not whitelisted by action policy.")
    return policy


def load_runtime_decision_context(
    root: pathlib.Path,
    action: str,
) -> tuple[CurrentDecisionContext, Dict[str, Any], Dict[str, Any]]:
    try:
        context = load_current_decision_context(root, mode="runtime_validation")
    except DecisionFrameArchiveError as exc:
        raise RuntimeError(f"Runtime command decision context is invalid: {exc}") from exc
    state = validate_latest_state(context.state)
    policy = validate_policy_for_command(root, state, action, policy=context.policy)
    return context, state, policy


def archive_runtime_command_source(
    context: CurrentDecisionContext,
    output_dir: pathlib.Path,
) -> Dict[str, Any]:
    """Persist the exact source bundle before a command's TTL starts.

    A partially written evidence directory is retained on failure. No command
    exists yet, and all normal decision-context checks still run afterwards.
    """
    started = now_iso()
    tick = time.monotonic()
    output = output_dir.expanduser().resolve()
    root = context.bridge_dir.resolve()
    if output == root or root in output.parents or output in root.parents:
        raise RuntimeError("Source evidence must be outside the Bridge directory.")
    assert_current_decision_context(context)
    health, health_raw = _read_json_object(root / "health.json", "health")
    blockers = binding_blockers(context.state, health, context.frame, now=local_now())
    if blockers:
        raise RuntimeError("Source evidence bundle is not coherent: " + ", ".join(blockers))
    output.mkdir(parents=True, exist_ok=False)
    manifest: Dict[str, Any] = {
        "schema": "oni.runtime-command-source-evidence.v1",
        "started_at": started,
        "source_frame_id": context.frame_id,
        "source_policy_hash": context.policy_hash,
        "session_id": context.state.get("session_id"),
        "source_snapshot_id": context.state.get("snapshot_id"),
        "source_snapshot_ts": context.state.get("ts"),
        "files": {},
    }
    try:
        for path, expected in context.file_hashes.items():
            _value, payload = _read_json_object(path, path.name)
            if hashlib.sha256(payload).hexdigest().upper() != expected:
                raise RuntimeError("Source evidence changed during archival: " + path.name)
            name = "archived-decision-frame.json" if path == context.archive_path else path.name
            with (output / name).open("xb") as stream:
                stream.write(payload)
            manifest["files"][name] = {"sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload)}
        with (output / "health.json").open("xb") as stream:
            stream.write(health_raw)
        manifest["files"]["health.json"] = {
            "sha256": hashlib.sha256(health_raw).hexdigest(), "bytes": len(health_raw),
        }
        assert_current_decision_context(context)
        validate_latest_state(context.state)
        manifest.update(status="archived", completed_at=now_iso(),
                        elapsed_seconds=time.monotonic() - tick)
        write_json_atomic(output / "source-manifest.json", manifest)
    except Exception as exc:
        manifest.update(status="archive_failed", completed_at=now_iso(),
                        elapsed_seconds=time.monotonic() - tick, error=str(exc))
        write_json_atomic(output / "source-manifest.json", manifest)
        raise
    return manifest


def quality_entry(snapshot: Dict[str, Any], path: str) -> Dict[str, Any]:
    quality = snapshot.get("quality") if isinstance(snapshot.get("quality"), dict) else {}
    entry = quality.get(path) if isinstance(quality.get(path), dict) else {}
    return entry


def command_base(
    state: Dict[str, Any],
    action: str,
    label: str,
    ttl_seconds: int,
    *,
    source_frame_id: Optional[str] = None,
    source_policy_hash: Optional[str] = None,
) -> Dict[str, Any]:
    created = local_now()
    command_id = f"cmd-runtime-{label}-{created.strftime('%H%M%S')}-{secrets.token_hex(4)}"
    return {
        "schema": "oni.command.v3",
        "id": command_id,
        "mode": "runtime_validation",
        "status": "pending_human_approval",
        "action": action,
        "args": {},
        "session_id": state.get("session_id"),
        "save_id_hash": state.get("save_id_hash"),
        "world_id": state.get("world_id"),
        # Runtime acceptance commands are operator-created and are not tied to
        # an incident lifecycle. Keeping this null prevents the bridge from
        # treating the synthetic validation label as a resolved incident.
        "source_incident_id": None,
        "source_snapshot_id": state.get("snapshot_id"),
        "state_hash": canonical_state_hash(state),
        "source_frame_id": source_frame_id,
        "created_at": created.isoformat(timespec="seconds"),
        "expires_at": (created + dt.timedelta(seconds=ttl_seconds)).isoformat(timespec="seconds"),
        "risk": "low",
        "reason": f"Probe-backed runtime validation for {action}",
        "evidence": {"source_snapshot_id": state.get("snapshot_id")},
        "preconditions": [],
        "expected_effects": [],
        "requires_approval": True,
        "source": {
            "kind": "runtime_validation",
            "state_seq": state.get("seq"),
            "state_ts": state.get("ts"),
            **(
                {"policy_hash": source_policy_hash}
                if isinstance(source_policy_hash, str) and source_policy_hash
                else {}
            ),
        },
    }


def prepare_command(args: argparse.Namespace) -> int:
    root = bridge_dir(args.bridge_dir)
    context, state, policy = load_runtime_decision_context(root, args.action)
    expected_snapshot = getattr(args, "expected_source_snapshot_id", None)
    if expected_snapshot is not None and state.get("snapshot_id") != expected_snapshot:
        raise RuntimeError("Runtime source snapshot changed before command preparation.")
    source_evidence_dir = getattr(args, "source_evidence_dir", None)
    if source_evidence_dir is not None:
        archive_runtime_command_source(context, pathlib.Path(source_evidence_dir))
    ttl_seconds = min(int(policy.get("command_ttl_seconds") or 30), 30)
    label = {
        "set_paused": "pause",
        "set_research": "research",
        "set_duplicant_chore_priority": "priority",
        "mark_dig_cells": "dig-mark",
        "cancel_dig_cells": "dig-cancel",
        "place_building_blueprint": "blueprint-place",
        "cancel_building_blueprint": "blueprint-cancel",
        "place_utility_blueprint_path": "utility-place",
        "cancel_utility_blueprint_path": "utility-cancel",
    }[args.action]
    command = command_base(
        state,
        args.action,
        label,
        ttl_seconds,
        source_frame_id=context.frame_id,
        source_policy_hash=context.policy_hash,
    )

    if args.action == "set_paused":
        paused_read = read_contract_field(state, "game.paused", GAME_PAUSED_CONTRACT_ID)
        if not paused_read.usable:
            raise RuntimeError(
                "game.paused does not satisfy its trusted command contract: "
                + ", ".join(paused_read.blockers)
            )
        current = paused_read.value
        if args.paused == "auto":
            target = not current
        else:
            target = args.paused == "true"
        if target == current:
            raise RuntimeError("Pause runtime validation requires an actual state change; use --paused auto.")
        command["args"] = {"paused": target}
        command["preconditions"] = [
            build_condition(
                "game.paused",
                "eq",
                current,
                GAME_PAUSED_CONTRACT_ID,
            )
        ]
        if target is False:
            alerts_read = read_contract_field(state, "alerts", ALERTS_CANONICAL_CONTRACT_ID)
            if not alerts_read.usable or alerts_read.value != []:
                raise RuntimeError(
                    "Unpause validation requires a complete canonical empty alert set."
                )
            command["preconditions"].append(
                build_condition(
                    "alerts",
                    "eq",
                    [],
                    ALERTS_CANONICAL_CONTRACT_ID,
                )
            )
        command["expected_effects"] = [
            build_condition(
                "game.paused",
                "eq",
                target,
                GAME_PAUSED_CONTRACT_ID,
            )
        ]
    elif args.action == "set_research":
        research_read = read_contract_field(
            state,
            "research.current",
            RESEARCH_CURRENT_CONTRACT_ID,
        )
        if not research_read.usable:
            raise RuntimeError(
                "research.current does not satisfy its trusted command contract: "
                + ", ".join(research_read.blockers)
            )
        current = research_read.value
        if not args.tech or args.tech == current:
            raise RuntimeError("--tech must name a different research id.")
        command["args"] = {"tech": args.tech}
        command["preconditions"] = [
            build_condition(
                "research.current",
                "eq",
                current,
                RESEARCH_CURRENT_CONTRACT_ID,
            )
        ]
        command["expected_effects"] = [
            build_condition(
                "research.current",
                "eq",
                args.tech,
                RESEARCH_CURRENT_CONTRACT_ID,
            )
        ]
    elif args.action == "set_duplicant_chore_priority":
        if args.duplicant_instance_id is None or args.priority is None:
            raise RuntimeError("--duplicant-instance-id and --priority are required for batch-A validation.")
        hint = build_duplicant_priority_hint(
            state,
            instance_id=args.duplicant_instance_id,
            chore_group=args.chore_group,
            priority=args.priority,
        )
        command["args"] = hint["args"]
        command["preconditions"] = hint["preconditions"]
        command["expected_effects"] = hint["expected_effects"]
        command["risk"] = "medium"
    elif args.action in {"mark_dig_cells", "cancel_dig_cells"}:
        if not args.cells:
            raise RuntimeError("--cells is required for batch-B validation.")
        if args.action == "mark_dig_cells":
            hint = build_mark_dig_hint(state, cells=args.cells)
            command["risk"] = "medium"
        else:
            hint = build_cancel_dig_hint(state, cells=args.cells)
            command["risk"] = "low"
        command["args"] = hint["args"]
        command["preconditions"] = hint["preconditions"]
        command["expected_effects"] = hint["expected_effects"]
    elif args.action in {
        "place_building_blueprint",
        "cancel_building_blueprint",
        "place_utility_blueprint_path",
        "cancel_utility_blueprint_path",
    }:
        if args.action in {"place_building_blueprint", "place_utility_blueprint_path"} and not args.material_tags:
            raise RuntimeError("--material-tags is required for blueprint placement validation.")
        if args.action in {"place_building_blueprint", "cancel_building_blueprint", "place_utility_blueprint_path"} and not args.cells:
            raise RuntimeError("--cells is required for this blueprint validation action.")
        if args.action in {"place_utility_blueprint_path", "cancel_utility_blueprint_path"} and not args.path_id:
            raise RuntimeError("--path-id is required for utility blueprint path validation.")
        if args.action == "place_building_blueprint":
            hint = build_place_building_blueprint_hint(
                state,
                cells=args.cells,
                material_tags=args.material_tags,
            )
            command["risk"] = "high"
        elif args.action == "cancel_building_blueprint":
            hint = build_cancel_building_blueprint_hint(state, cells=args.cells)
            command["risk"] = "low"
        elif args.action == "place_utility_blueprint_path":
            hint = build_place_utility_blueprint_path_hint(
                state,
                path_id=args.path_id,
                cells=args.cells,
                material_tags=args.material_tags,
            )
            command["risk"] = "high"
        else:
            hint = build_cancel_utility_blueprint_path_hint(state, path_id=args.path_id)
            command["risk"] = "low"
        command["args"] = hint["args"]
        command["preconditions"] = hint["preconditions"]
        command["expected_effects"] = hint["expected_effects"]
    else:
        raise RuntimeError(f"Unsupported runtime action: {args.action}")

    return write_pending_runtime_command(
        root,
        command,
        "pending_human_approval",
        decision_context=context,
    )


def write_pending_runtime_command(
    root: pathlib.Path,
    command: Dict[str, Any],
    intent: str,
    *,
    decision_context: Optional[CurrentDecisionContext] = None,
) -> int:
    mutating_operator = (
        command.get("schema") == "oni.command.v3"
        and command.get("mode") in {"runtime_validation", "planned"}
        and command.get("action") not in {"noop", "focus_note"}
    )
    if mutating_operator:
        if decision_context is None:
            raise RuntimeError("Mutating runtime command lacks an immutable decision context.")
        source = command.get("source") if isinstance(command.get("source"), dict) else {}
        if (
            command.get("source_frame_id") != decision_context.frame_id
            or source.get("policy_hash") != decision_context.policy_hash
        ):
            raise RuntimeError("Mutating runtime command decision binding drifted before publication.")
        try:
            assert_current_decision_context(decision_context)
        except DecisionFrameArchiveError as exc:
            raise RuntimeError(f"Runtime command decision context changed: {exc}") from exc
    dirs = ensure_command_dirs(root)
    command_path = dirs["pending"] / f"{command['id']}.json"
    if command_path.exists():
        raise RuntimeError("Generated command id already exists.")
    write_json_atomic(command_path, command)
    if decision_context is not None:
        try:
            assert_current_decision_context(decision_context)
        except DecisionFrameArchiveError as exc:
            command_path.unlink(missing_ok=True)
            raise RuntimeError(
                f"Runtime command decision context changed during publication: {exc}"
            ) from exc
    record_runtime_intent(root, command, intent)
    print(
        json.dumps(
            {
                "id": command["id"],
                "action": command["action"],
                "args": command["args"],
                "session_id": command["session_id"],
                "save_id_hash": command["save_id_hash"],
                "expires_at": command["expires_at"],
                "path": str(command_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def prepare_invalid_priority_target(args: argparse.Namespace) -> int:
    root = bridge_dir(args.bridge_dir)
    context, state, policy = load_runtime_decision_context(root, "set_duplicant_chore_priority")
    ttl_seconds = min(int(policy.get("command_ttl_seconds") or 30), 30)
    hint = build_duplicant_priority_hint(
        state,
        instance_id=args.duplicant_instance_id,
        chore_group=args.chore_group,
        priority=args.priority,
    )
    command = command_base(
        state,
        "set_duplicant_chore_priority",
        "priority-invalid",
        ttl_seconds,
        source_frame_id=context.frame_id,
        source_policy_hash=context.policy_hash,
    )
    command["args"] = copy.deepcopy(hint["args"])
    suffix = "__invalid_target__"
    expected_name = str(command["args"]["expected_name"])
    command["args"]["expected_name"] = expected_name[: 128 - len(suffix)] + suffix
    command["preconditions"] = hint["preconditions"]
    command["expected_effects"] = hint["expected_effects"]
    command["risk"] = "medium"
    command["reason"] = "Probe-backed negative validation for a mismatched duplicant identity"
    command["source"]["kind"] = "runtime_negative_validation"
    return write_pending_runtime_command(
        root,
        command,
        "pending_negative_human_approval",
        decision_context=context,
    )


def prepare_invalid_dig_target(args: argparse.Namespace) -> int:
    root = bridge_dir(args.bridge_dir)
    context, state, policy = load_runtime_decision_context(root, "mark_dig_cells")
    ttl_seconds = min(int(policy.get("command_ttl_seconds") or 30), 30)
    hint = build_mark_dig_hint(state, cells=[args.cell])
    command = command_base(
        state,
        "mark_dig_cells",
        "dig-invalid",
        ttl_seconds,
        source_frame_id=context.frame_id,
        source_policy_hash=context.policy_hash,
    )
    command["args"] = hint["args"]
    command["preconditions"] = copy.deepcopy(hint["preconditions"])
    command["expected_effects"] = hint["expected_effects"]
    command["risk"] = "medium"
    command["reason"] = "Probe-backed negative validation for a mismatched dig-cell fingerprint"

    fingerprint_path = f"{dig_cell_path(args.cell)}.fingerprint"
    source_fingerprint = safe_get(state, fingerprint_path)
    if not isinstance(source_fingerprint, str) or not source_fingerprint.startswith("sha256:"):
        raise RuntimeError("The selected dig cell does not expose a valid observed fingerprint.")
    invalid_fingerprint = "sha256:" + ("f" if source_fingerprint != "sha256:" + "f" * 64 else "e") * 64
    fingerprint_condition = next(
        (
            item
            for item in command["preconditions"]
            if isinstance(item, dict) and item.get("path") == fingerprint_path and item.get("op") == "eq"
        ),
        None,
    )
    if fingerprint_condition is None:
        raise RuntimeError("The generated mark command did not bind the target fingerprint.")
    fingerprint_condition["value"] = invalid_fingerprint
    command["evidence"].update(
        {
            "negative_case": "mismatched_dig_cell_fingerprint",
            "cell": args.cell,
            "expected_unchanged_fingerprint": source_fingerprint,
            "injected_fingerprint": invalid_fingerprint,
        }
    )
    return write_pending_runtime_command(
        root,
        command,
        "pending_negative_human_approval",
        decision_context=context,
    )


def prepare_invalid_blueprint_target(args: argparse.Namespace) -> int:
    root = bridge_dir(args.bridge_dir)
    context, state, policy = load_runtime_decision_context(root, "place_building_blueprint")
    ttl_seconds = min(int(policy.get("command_ttl_seconds") or 30), 30)
    hint = build_place_building_blueprint_hint(
        state,
        cells=[args.cell],
        material_tags=args.material_tags,
    )
    command = command_base(
        state,
        "place_building_blueprint",
        "blueprint-invalid",
        ttl_seconds,
        source_frame_id=context.frame_id,
        source_policy_hash=context.policy_hash,
    )
    command["args"] = hint["args"]
    command["preconditions"] = copy.deepcopy(hint["preconditions"])
    command["expected_effects"] = hint["expected_effects"]
    command["risk"] = "high"
    command["reason"] = "Probe-backed negative validation for a mismatched empty-cell blueprint fingerprint"
    command["source"]["kind"] = "runtime_negative_validation"

    fingerprint_path = f"{blueprint_cell_path(args.cell)}.fingerprint"
    source_fingerprint = safe_get(state, fingerprint_path)
    if not isinstance(source_fingerprint, str) or not source_fingerprint.startswith("sha256:"):
        raise RuntimeError("The selected blueprint cell does not expose a valid observed fingerprint.")
    invalid_fingerprint = "sha256:" + ("f" if source_fingerprint != "sha256:" + "f" * 64 else "e") * 64
    fingerprint_condition = next(
        (
            item
            for item in command["preconditions"]
            if isinstance(item, dict) and item.get("path") == fingerprint_path and item.get("op") == "eq"
        ),
        None,
    )
    if fingerprint_condition is None:
        raise RuntimeError("The generated blueprint command did not bind the target fingerprint.")
    fingerprint_condition["value"] = invalid_fingerprint
    command["evidence"].update(
        {
            "negative_case": "mismatched_blueprint_cell_fingerprint",
            "cell": args.cell,
            "expected_unchanged_fingerprint": source_fingerprint,
            "injected_fingerprint": invalid_fingerprint,
        }
    )
    return write_pending_runtime_command(
        root,
        command,
        "pending_negative_human_approval",
        decision_context=context,
    )


def inject_stale(args: argparse.Namespace) -> int:
    root = bridge_dir(args.bridge_dir)
    state = read_latest_state(root)
    validate_policy_for_command(root, state, "noop")
    created = local_now() - dt.timedelta(seconds=60)
    command = command_base(state, "noop", "stale", 30)
    command.update(
        {
            "status": "approved",
            "args": {},
            "created_at": created.isoformat(timespec="seconds"),
            "expires_at": (created + dt.timedelta(seconds=30)).isoformat(timespec="seconds"),
            "approved_at": local_now().isoformat(timespec="seconds"),
            "approved_by": "runtime_fault_injection",
        }
    )
    dirs = ensure_command_dirs(root)
    target = dirs["approved"] / f"{command['id']}.json"
    write_json_atomic(target, command)
    record_runtime_intent(root, command, "injected_expired_approved")
    print(json.dumps({"id": command["id"], "expected": "skipped_stale", "path": str(target)}, indent=2))
    return 0


def inject_replay(args: argparse.Namespace) -> int:
    root = bridge_dir(args.bridge_dir)
    dirs = ensure_command_dirs(root)
    source = dirs["done"] / f"{args.command_id}.json"
    if not source.exists():
        raise RuntimeError("Verified done command was not found for replay injection.")
    command = load_json(source)
    current = local_now()
    command["status"] = "approved"
    command["created_at"] = current.isoformat(timespec="seconds")
    command["expires_at"] = (current + dt.timedelta(seconds=30)).isoformat(timespec="seconds")
    command["approved_at"] = current.isoformat(timespec="seconds")
    command["approved_by"] = "runtime_replay_fault_injection"
    command.pop("completed_at", None)
    command.pop("result", None)
    target = dirs["approved"] / f"{args.command_id}.json"
    if target.exists():
        raise RuntimeError("Approved replay target already exists.")
    write_json_atomic(target, command)
    record_runtime_intent(root, command, "injected_duplicate_id")
    print(json.dumps({"id": args.command_id, "expected": "skipped_stale_duplicate", "path": str(target)}, indent=2))
    return 0


def record_runtime_intent(root: pathlib.Path, command: Dict[str, Any], status: str) -> None:
    path = root / "evidence" / "runtime-intents" / f"{command['id']}.json"
    payload = {
        "schema": "oni.runtime-intent.v1",
        "recorded_at": now_iso(),
        "status": status,
        "id": command.get("id"),
        "action": command.get("action"),
        "args": command.get("args"),
        "session_id": command.get("session_id"),
        "save_id_hash": command.get("save_id_hash"),
        "source_snapshot_id": command.get("source_snapshot_id"),
    }
    write_json_atomic(path, payload)


def iter_recent_jsonl(path: pathlib.Path, limit: int = 100_000) -> Tuple[List[Dict[str, Any]], int]:
    lines: collections.deque[bytes] = collections.deque(maxlen=limit)
    if not path.exists():
        return [], 0
    with path.open("rb") as handle:
        for raw in handle:
            if raw.endswith(b"\n"):
                lines.append(raw)
    records: List[Dict[str, Any]] = []
    invalid = 0
    for raw in lines:
        try:
            value = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            invalid += 1
            continue
        if isinstance(value, dict):
            records.append(value)
        else:
            invalid += 1
    return records, invalid


def current_session_records(records: List[Dict[str, Any]]) -> Tuple[Optional[str], List[Dict[str, Any]], List[Dict[str, Any]]]:
    snapshots = [record for record in records if record.get("event") == "state_snapshot" and record.get("session_id")]
    if not snapshots:
        return None, [], []
    session_id = str(snapshots[-1]["session_id"])
    session_records = [record for record in records if str(record.get("session_id") or "") == session_id]
    session_snapshots = [record for record in session_records if record.get("event") == "state_snapshot"]
    return session_id, session_records, session_snapshots


def safe_session_name(value: Optional[str]) -> str:
    text = value or "waiting"
    safe = "".join(character if character.isascii() and (character.isalnum() or character in "._-") else "_" for character in text)
    return safe[:120] or "waiting"


def root_state_log_paths(root: pathlib.Path) -> List[pathlib.Path]:
    state_log = root / "state.jsonl"
    rotations: List[Tuple[int, pathlib.Path]] = []
    for candidate in root.glob("state.jsonl.*"):
        suffix = candidate.name.removeprefix("state.jsonl.")
        if suffix.isdigit():
            rotations.append((int(suffix), candidate))
    return [path for _, path in sorted(rotations, reverse=True)] + [state_log]


def runtime_record_sources(root: pathlib.Path) -> Dict[str, Any]:
    root_logs = root_state_log_paths(root)
    lifecycle_records: List[Dict[str, Any]] = []
    lifecycle_invalid = 0
    for path in root_logs:
        records, invalid = iter_recent_jsonl(path)
        lifecycle_records.extend(records)
        lifecycle_invalid += invalid

    latest_state: Dict[str, Any] = {}
    try:
        latest_state = load_json(root / "latest_state.json")
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        pass
    session_id = str(latest_state.get("session_id") or "") or None
    if session_id is None:
        session_id, _, _ = current_session_records(lifecycle_records)

    archive_log = root / "sessions" / safe_session_name(session_id) / "state.jsonl" if session_id else None
    archived_records: List[Dict[str, Any]] = []
    archive_invalid = 0
    if archive_log is not None and archive_log.exists():
        archived_records, archive_invalid = iter_recent_jsonl(archive_log)

    source_records = archived_records if archived_records else lifecycle_records
    if session_id:
        session_records = [
            record for record in source_records if str(record.get("session_id") or "") == session_id
        ]
    else:
        session_records = []
    snapshots = [record for record in session_records if record.get("event") == "state_snapshot"]
    if not snapshots:
        session_id, session_records, snapshots = current_session_records(lifecycle_records)
        archive_log = None

    return {
        "session_id": session_id,
        "lifecycle_records": lifecycle_records,
        "session_records": session_records,
        "snapshots": snapshots,
        "invalid_lines": lifecycle_invalid + archive_invalid,
        "session_log": archive_log or (root / "state.jsonl"),
        "session_archive_used": bool(archive_log and archived_records),
        "root_logs": root_logs,
    }


def lineage_session_record_sources(
    root: pathlib.Path,
    session_ids: Iterable[str],
    fallback_records: List[Dict[str, Any]],
) -> Dict[str, Any]:
    records: List[Dict[str, Any]] = []
    archive_paths: List[pathlib.Path] = []
    invalid_lines = 0
    for session_id in sorted({str(value) for value in session_ids if str(value)}):
        archive = root / "sessions" / safe_session_name(session_id) / "state.jsonl"
        archived_records: List[Dict[str, Any]] = []
        archive_invalid = 0
        if archive.exists():
            archived_records, archive_invalid = iter_recent_jsonl(archive)
            archived_records = [
                record
                for record in archived_records
                if str(record.get("session_id") or "") == session_id
            ]
        if archived_records:
            records.extend(archived_records)
            archive_paths.append(archive)
            invalid_lines += archive_invalid
        else:
            records.extend(
                record
                for record in fallback_records
                if str(record.get("session_id") or "") == session_id
            )
    return {
        "records": records,
        "invalid_lines": invalid_lines,
        "archive_paths": archive_paths,
    }


def quality_status(snapshot: Dict[str, Any], path: str) -> Optional[str]:
    entry = quality_entry(snapshot, path)
    value = entry.get("status")
    return str(value) if value is not None else None


def observer_check(
    session_id: Optional[str],
    all_records: List[Dict[str, Any]],
    session_records: List[Dict[str, Any]],
    snapshots: List[Dict[str, Any]],
    invalid_lines: int,
    player: Dict[str, Any],
) -> Dict[str, Any]:
    last_three = snapshots[-3:]
    sequences = [int(item.get("seq") or 0) for item in last_three]
    envelope_fields = ("session_id", "seq", "game_build", "mod_version", "save_id_hash", "world_id", "snapshot_id")
    envelope_ok = bool(last_three) and all(all(item.get(field) is not None for field in envelope_fields) for item in last_three)
    sequence_ok = len(last_three) == 3 and all(right > left for left, right in zip(sequences, sequences[1:]))
    quality_maps = [item.get("quality") for item in last_three]
    quality_consistent = len(last_three) == 3 and all(
        isinstance(value, dict) and all(path in value for path in CRITICAL_QUALITY) for value in quality_maps
    )
    if quality_consistent:
        for path in CRITICAL_QUALITY:
            statuses = [quality_status(item, path) for item in last_three]
            if len(set(statuses)) != 1:
                quality_consistent = False
                break
    latest = snapshots[-1] if snapshots else {}
    critical_usable = all(quality_status(latest, path) in {"observed", "estimated"} for path in CRITICAL_QUALITY)
    source_cross_checks = {
        "cycle": {
            "value": latest.get("cycle"),
            "quality": quality_status(latest, "cycle"),
            "source": quality_entry(latest, "cycle").get("source"),
        },
        "duplicants_total": {
            "value": safe_get(latest, "duplicants.total"),
            "quality": quality_status(latest, "duplicants.total"),
            "source": quality_entry(latest, "duplicants.total").get("source"),
        },
        "food_kcal": {
            "value": safe_get(latest, "resources.food_kcal"),
            "quality": quality_status(latest, "resources.food_kcal"),
            "source": quality_entry(latest, "resources.food_kcal").get("source"),
        },
    }
    last_probe_load = max(
        (index for index, record in enumerate(all_records) if record.get("event") == "probe_loaded"),
        default=-1,
    )
    bootstrap_records = all_records[last_probe_load:] if last_probe_load >= 0 else []
    patch_registration_observed = any(
        record.get("event") == "probe_patch_registered" for record in bootstrap_records
    )
    patch_execution_observed = any(
        record.get("event") in {"session_started", "save_loaded"}
        and record.get("reason") in {"game_lifecycle_patch", "save_loader_load_complete"}
        for record in session_records
    )
    lifecycle_patch_observed = patch_registration_observed or patch_execution_observed
    probe_errors = [
        record
        for record in bootstrap_records + session_records
        if record.get("event") == "probe_error"
    ]
    passed = bool(session_id) and envelope_ok and sequence_ok and quality_consistent and critical_usable and lifecycle_patch_observed and not probe_errors and invalid_lines == 0 and player.get("passed")
    return {
        "status": "passed" if passed else "failed",
        "classification": None if passed else ("infra_fail" if not session_id or not player.get("exists") else "behavior_fail"),
        "session_id": session_id,
        "snapshot_count": len(snapshots),
        "last_sequences": sequences,
        "envelope_ok": envelope_ok,
        "sequence_ok": sequence_ok,
        "quality_consistent": quality_consistent,
        "critical_quality_usable": critical_usable,
        "lifecycle_patch_registered": lifecycle_patch_observed,
        "lifecycle_patch_registration_event": patch_registration_observed,
        "lifecycle_patch_execution_event": patch_execution_observed,
        "source_cross_checks": source_cross_checks,
        "probe_error_count": len(probe_errors),
        "invalid_json_lines": invalid_lines,
        "player_log": player,
    }


def load_command_files(root: pathlib.Path) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    dirs = ensure_command_dirs(root)
    for directory_name, directory in dirs.items():
        for path in sorted(directory.glob("*.json")):
            try:
                value = load_json(path)
            except Exception:
                continue
            value = dict(value)
            value["_directory"] = directory_name
            value["_path"] = str(path)
            result.append(value)
    return result


def ack_statuses(records: Iterable[Dict[str, Any]], command_id: str) -> List[str]:
    return [
        str(record.get("status"))
        for record in records
        if record.get("event") == "command_ack" and str(record.get("id")) == command_id
    ]


def confirmation_contract(command: Dict[str, Any]) -> Dict[str, Any]:
    value = command.get("_confirmation_contract")
    return dict(value) if isinstance(value, dict) else {
        "accepted": False,
        "mode": "confirmation_not_assessed",
        "blockers": ["confirmation_not_assessed"],
        "legacy_read_only": False,
    }


def confirmation_witness(command: Dict[str, Any]) -> Dict[str, Any]:
    value = command.get("_confirmation_witness")
    return dict(value) if isinstance(value, dict) else {}


def confirmation_failure_classification(commands: Iterable[Dict[str, Any]]) -> str:
    contracts = [confirmation_contract(command) for command in commands]
    if contracts and all(contract.get("mode") == LEGACY_MUTATION_MODE for contract in contracts):
        return "legacy_read_only"
    return "behavior_fail"


def latest_snapshot_for_command(records: Iterable[Dict[str, Any]], command: Dict[str, Any]) -> Dict[str, Any]:
    session_id = command.get("session_id")
    snapshots = [
        record
        for record in records
        if record.get("event") == "state_snapshot" and record.get("session_id") == session_id
    ]
    return snapshots[-1] if snapshots else {}


def fingerprint_precondition_reason(reason: str) -> bool:
    normalized = reason.casefold()
    return "fingerprint" in normalized and (
        "precondition" in normalized
        or ("matching observed" in normalized and "current fingerprint" in normalized)
    )


def action_check(
    commands: List[Dict[str, Any]],
    records: List[Dict[str, Any]],
    action: str,
    path: str,
) -> Dict[str, Any]:
    candidates = [
        command
        for command in commands
        if command.get("action") == action
        and str(command.get("id") or "").startswith("cmd-runtime-")
        and command.get("_directory") == "done"
        and command.get("status") in {"confirmed", "verified"}
    ]
    confirmed = [command for command in candidates if confirmed_mutating_command(command)]
    evidence: List[Dict[str, Any]] = []
    for command in confirmed:
        result = command.get("result") if isinstance(command.get("result"), dict) else {}
        before = safe_get(result, f"before_state.{path}")
        after = safe_get(confirmation_witness(command), path)
        expected_effects = command.get("expected_effects") if isinstance(command.get("expected_effects"), list) else []
        expected = expected_effects[0].get("value") if expected_effects and isinstance(expected_effects[0], dict) else None
        statuses = ack_statuses(records, str(command.get("id")))
        evidence.append(
            {
                "id": command.get("id"),
                "before": before,
                "after": after,
                "expected": expected,
                "ack_statuses": statuses,
                "confirmation": confirmation_contract(command),
                "state_changed": before != after,
                "verified_effect": after == expected,
                "required_ack_stages": all(stage in statuses for stage in REQUIRED_MUTATION_ACKS),
            }
        )
    passed = any(
        item["state_changed"] and item["verified_effect"] and item["required_ack_stages"] for item in evidence
    )
    return {
        "status": "passed" if passed else "not_run" if not candidates else "failed",
        "classification": None if passed or not candidates else confirmation_failure_classification(candidates),
        "confirmed_command_count": len(confirmed),
        "candidate_command_count": len(candidates),
        "confirmation_rejections": [
            {"id": command.get("id"), "confirmation": confirmation_contract(command)}
            for command in candidates
            if not confirmed_mutating_command(command)
        ],
        "evidence": evidence,
    }


def duplicant_priority_check(
    commands: List[Dict[str, Any]],
    records: List[Dict[str, Any]],
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    candidates = [
        command
        for command in commands
        if command.get("action") == "set_duplicant_chore_priority"
        and str(command.get("id") or "").startswith("cmd-runtime-priority-")
        and (session_id is None or command.get("session_id") == session_id)
        and command.get("_directory") == "done"
        and command.get("status") in {"confirmed", "verified"}
    ]
    confirmed = [command for command in candidates if confirmed_mutating_command(command)]
    confirmed.sort(key=lambda item: str(item.get("completed_at") or ""))
    evidence: List[Dict[str, Any]] = []
    for command in confirmed:
        args = command.get("args") if isinstance(command.get("args"), dict) else {}
        instance_id = args.get("duplicant_instance_id")
        group = args.get("chore_group")
        if not isinstance(instance_id, int) or not isinstance(group, str):
            continue
        path = duplicant_priority_path(instance_id, group)
        result = command.get("result") if isinstance(command.get("result"), dict) else {}
        before = safe_get(result, f"before_state.{path}")
        after = safe_get(confirmation_witness(command), path)
        statuses = ack_statuses(records, str(command.get("id")))
        evidence.append(
            {
                "id": command.get("id"),
                "duplicant_instance_id": instance_id,
                "expected_name": args.get("expected_name"),
                "chore_group": group,
                "before": before,
                "after": after,
                "expected": args.get("priority"),
                "ack_statuses": statuses,
                "confirmation": confirmation_contract(command),
                "state_changed": before != after,
                "verified_effect": after == args.get("priority"),
                "required_ack_stages": all(stage in statuses for stage in REQUIRED_MUTATION_ACKS),
            }
        )

    restore_pairs: List[Dict[str, Any]] = []
    for first_index, first in enumerate(evidence):
        for second in evidence[first_index + 1 :]:
            same_target = (
                first["duplicant_instance_id"] == second["duplicant_instance_id"]
                and first["chore_group"] == second["chore_group"]
            )
            restored = second["before"] == first["after"] and second["after"] == first["before"]
            if same_target and restored and first["required_ack_stages"] and second["required_ack_stages"]:
                restore_pairs.append({"forward_id": first["id"], "restore_id": second["id"]})

    passed = bool(restore_pairs) and all(item["state_changed"] and item["verified_effect"] for item in evidence)
    return {
        "status": "passed" if passed else "not_run" if not candidates else "failed",
        "classification": None if passed or not candidates else confirmation_failure_classification(candidates),
        "confirmed_command_count": len(confirmed),
        "candidate_command_count": len(candidates),
        "confirmation_rejections": [
            {"id": command.get("id"), "confirmation": confirmation_contract(command)}
            for command in candidates
            if not confirmed_mutating_command(command)
        ],
        "restore_pairs": restore_pairs,
        "evidence": evidence,
    }


def duplicant_priority_invalid_target_check(
    commands: List[Dict[str, Any]],
    records: List[Dict[str, Any]],
    snapshots: List[Dict[str, Any]],
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    rejected = [
        command
        for command in commands
        if command.get("action") == "set_duplicant_chore_priority"
        and str(command.get("id") or "").startswith("cmd-runtime-priority-invalid-")
        and (session_id is None or command.get("session_id") == session_id)
        and command.get("_directory") == "quarantine"
        and command.get("status") == "quarantined"
    ]
    evidence: List[Dict[str, Any]] = []
    for command in rejected:
        latest = latest_snapshot_for_command(records, command) or (snapshots[-1] if snapshots else {})
        args = command.get("args") if isinstance(command.get("args"), dict) else {}
        instance_id = args.get("duplicant_instance_id")
        group = args.get("chore_group")
        if not isinstance(instance_id, int) or not isinstance(group, str):
            continue
        path = duplicant_priority_path(instance_id, group)
        statuses = ack_statuses(records, str(command.get("id")))
        latest_value = safe_get(latest, path)
        reason = str(command.get("quarantine_reason") or "")
        evidence.append(
            {
                "id": command.get("id"),
                "ack_statuses": statuses,
                "ack_status": command.get("ack_status"),
                "classification": command.get("classification"),
                "quarantine_reason": reason,
                "no_execution_ack": not any(
                    stage in statuses
                    for stage in ("accepted", "executed", "local_effect_observed", "verified")
                ),
                "identity_mismatch_rejected": "expected_name did not" in reason,
                "latest_priority": latest_value,
                "expected_unchanged_priority": args.get("from_priority"),
                "latest_priority_quality": quality_entry(latest, path).get("status"),
                "state_unchanged": latest_value == args.get("from_priority"),
            }
        )

    passed = bool(evidence) and all(
        item["ack_status"] == "rejected"
        and item["classification"] == "behavior_fail"
        and item["no_execution_ack"]
        and item["identity_mismatch_rejected"]
        and item["latest_priority_quality"] == "observed"
        and item["state_unchanged"]
        for item in evidence
    )
    return {
        "status": "passed" if passed else "not_run" if not rejected else "failed",
        "classification": None if passed or not rejected else "behavior_fail",
        "rejected_command_count": len(rejected),
        "evidence": evidence,
    }


def dig_designation_check(
    commands: List[Dict[str, Any]],
    records: List[Dict[str, Any]],
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    def candidates(action: str, prefix: str) -> List[Dict[str, Any]]:
        result = [
            command
            for command in commands
            if command.get("action") == action
            and str(command.get("id") or "").startswith(prefix)
            and (session_id is None or command.get("session_id") == session_id)
            and command.get("_directory") == "done"
            and command.get("status") in {"confirmed", "verified"}
        ]
        result.sort(key=lambda item: str(item.get("completed_at") or ""))
        return result

    mark_candidates = candidates("mark_dig_cells", "cmd-runtime-dig-mark-")
    cancel_candidates = candidates("cancel_dig_cells", "cmd-runtime-dig-cancel-")
    marks = [command for command in mark_candidates if confirmed_mutating_command(command)]
    cancels = [command for command in cancel_candidates if confirmed_mutating_command(command)]
    mark_evidence: List[Dict[str, Any]] = []
    cancel_evidence: List[Dict[str, Any]] = []

    for command in marks:
        args = command.get("args") if isinstance(command.get("args"), dict) else {}
        cells = args.get("cells") if isinstance(args.get("cells"), list) else []
        result = command.get("result") if isinstance(command.get("result"), dict) else {}
        before = result.get("before_state") if isinstance(result.get("before_state"), dict) else {}
        after = confirmation_witness(command)
        statuses = ack_statuses(records, str(command.get("id")))
        per_cell: List[Dict[str, Any]] = []
        for cell in cells:
            if isinstance(cell, bool) or not isinstance(cell, int):
                continue
            prefix = dig_cell_path(cell)
            before_fingerprint = safe_get(before, f"{prefix}.fingerprint")
            after_instance_id = safe_get(after, f"{prefix}.designation_instance_id")
            unchanged = all(
                safe_get(before, f"{prefix}.{field}") == safe_get(after, f"{prefix}.{field}")
                for field in ("element_id", "mass_kg", "damage", "solid")
            )
            created = (
                safe_get(before, f"{prefix}.designation_present") is False
                and safe_get(before, f"{prefix}.safe_mark_candidate") is True
                and safe_get(after, f"{prefix}.designation_present") is True
                and isinstance(after_instance_id, int)
                and not isinstance(after_instance_id, bool)
                and after_instance_id > 0
                and safe_get(after, f"{prefix}.designation_kind") == "tile"
                and safe_get(after, f"{prefix}.safe_mark_candidate") is False
            )
            per_cell.append(
                {
                    "cell": cell,
                    "before_fingerprint": before_fingerprint,
                    "after_fingerprint": safe_get(after, f"{prefix}.fingerprint"),
                    "designation_instance_id": after_instance_id,
                    "created": created,
                    "tile_unchanged": unchanged,
                }
            )
        required_acks = all(stage in statuses for stage in REQUIRED_MUTATION_ACKS)
        mark_evidence.append(
            {
                "id": command.get("id"),
                "completed_at": command.get("completed_at"),
                "cells": cells,
                "ack_statuses": statuses,
                "confirmation": confirmation_contract(command),
                "required_ack_stages": required_acks,
                "per_cell": per_cell,
                "valid": bool(per_cell) and required_acks and all(item["created"] and item["tile_unchanged"] for item in per_cell),
            }
        )

    for command in cancels:
        args = command.get("args") if isinstance(command.get("args"), dict) else {}
        targets = args.get("targets") if isinstance(args.get("targets"), list) else []
        result = command.get("result") if isinstance(command.get("result"), dict) else {}
        before = result.get("before_state") if isinstance(result.get("before_state"), dict) else {}
        after = confirmation_witness(command)
        statuses = ack_statuses(records, str(command.get("id")))
        per_cell = []
        for target in targets:
            if not isinstance(target, dict):
                continue
            cell = target.get("cell")
            target_id = target.get("designation_instance_id")
            if isinstance(cell, bool) or not isinstance(cell, int):
                continue
            prefix = dig_cell_path(cell)
            unchanged = all(
                safe_get(before, f"{prefix}.{field}") == safe_get(after, f"{prefix}.{field}")
                for field in ("element_id", "mass_kg", "damage", "solid")
            )
            removed = (
                safe_get(before, f"{prefix}.designation_present") is True
                and safe_get(before, f"{prefix}.designation_instance_id") == target_id
                and safe_get(before, f"{prefix}.designation_kind") == "tile"
                and safe_get(before, f"{prefix}.designation_in_progress") is False
                and safe_get(after, f"{prefix}.designation_present") is False
                and safe_get(after, f"{prefix}.designation_instance_id") == 0
                and safe_get(after, f"{prefix}.designation_kind") == "none"
                and safe_get(after, f"{prefix}.safe_mark_candidate") is True
            )
            per_cell.append(
                {
                    "cell": cell,
                    "target_instance_id": target_id,
                    "before_fingerprint": safe_get(before, f"{prefix}.fingerprint"),
                    "after_fingerprint": safe_get(after, f"{prefix}.fingerprint"),
                    "removed": removed,
                    "tile_unchanged": unchanged,
                }
            )
        required_acks = all(stage in statuses for stage in REQUIRED_MUTATION_ACKS)
        cancel_evidence.append(
            {
                "id": command.get("id"),
                "completed_at": command.get("completed_at"),
                "targets": targets,
                "ack_statuses": statuses,
                "confirmation": confirmation_contract(command),
                "required_ack_stages": required_acks,
                "per_cell": per_cell,
                "valid": bool(per_cell) and required_acks and all(item["removed"] and item["tile_unchanged"] for item in per_cell),
            }
        )

    restore_pairs: List[Dict[str, Any]] = []
    for mark in mark_evidence:
        mark_cells = {item["cell"] for item in mark["per_cell"]}
        mark_by_cell = {item["cell"]: item for item in mark["per_cell"]}
        for cancel in cancel_evidence:
            cancel_cells = {item["cell"] for item in cancel["per_cell"]}
            cancel_by_cell = {item["cell"]: item for item in cancel["per_cell"]}
            ordered = str(cancel.get("completed_at") or "") > str(mark.get("completed_at") or "")
            exact_handoff = bool(mark_cells) and mark_cells == cancel_cells and all(
                mark_by_cell[cell]["designation_instance_id"] == cancel_by_cell[cell]["target_instance_id"]
                and mark_by_cell[cell]["after_fingerprint"] == cancel_by_cell[cell]["before_fingerprint"]
                for cell in mark_cells
            )
            restored = exact_handoff and all(
                mark_by_cell[cell]["before_fingerprint"] == cancel_by_cell[cell]["after_fingerprint"]
                for cell in mark_cells
            )
            if ordered and mark["valid"] and cancel["valid"] and exact_handoff and restored:
                restore_pairs.append(
                    {
                        "mark_id": mark["id"],
                        "cancel_id": cancel["id"],
                        "cells": sorted(mark_cells),
                        "exact_instance_handoff": True,
                        "fingerprint_restored": True,
                    }
                )

    all_candidates = mark_candidates + cancel_candidates
    all_evidence = mark_evidence + cancel_evidence
    passed = bool(restore_pairs) and bool(all_evidence) and all(item["valid"] for item in all_evidence)
    return {
        "status": "passed" if passed else "not_run" if not all_candidates else "failed",
        "classification": None if passed or not all_candidates else confirmation_failure_classification(all_candidates),
        "confirmed_mark_count": len(marks),
        "confirmed_cancel_count": len(cancels),
        "candidate_command_count": len(all_candidates),
        "confirmation_rejections": [
            {"id": command.get("id"), "confirmation": confirmation_contract(command)}
            for command in all_candidates
            if not confirmed_mutating_command(command)
        ],
        "restore_pairs": restore_pairs,
        "mark_evidence": mark_evidence,
        "cancel_evidence": cancel_evidence,
    }


def dig_invalid_target_check(
    commands: List[Dict[str, Any]],
    records: List[Dict[str, Any]],
    snapshots: List[Dict[str, Any]],
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    rejected = [
        command
        for command in commands
        if command.get("action") == "mark_dig_cells"
        and str(command.get("id") or "").startswith("cmd-runtime-dig-invalid-")
        and (session_id is None or command.get("session_id") == session_id)
        and command.get("_directory") == "quarantine"
        and command.get("status") == "quarantined"
    ]
    evidence: List[Dict[str, Any]] = []
    for command in rejected:
        latest = latest_snapshot_for_command(records, command) or (snapshots[-1] if snapshots else {})
        command_evidence = command.get("evidence") if isinstance(command.get("evidence"), dict) else {}
        cell = command_evidence.get("cell")
        if isinstance(cell, bool) or not isinstance(cell, int):
            continue
        prefix = dig_cell_path(cell)
        statuses = ack_statuses(records, str(command.get("id")))
        reason = str(command.get("quarantine_reason") or "")
        expected_fingerprint = command_evidence.get("expected_unchanged_fingerprint")
        final_fingerprint = safe_get(latest, f"{prefix}.fingerprint")
        final_state_unchanged = (
            isinstance(expected_fingerprint, str)
            and final_fingerprint == expected_fingerprint
            and safe_get(latest, f"{prefix}.designation_present") is False
            and safe_get(latest, f"{prefix}.designation_instance_id") == 0
            and safe_get(latest, f"{prefix}.safe_mark_candidate") is True
            and safe_get(latest, f"{prefix}.solid") is True
        )
        observed_final = all(
            quality_entry(latest, f"{prefix}.{field}").get("status") == "observed"
            for field in ("fingerprint", "designation_present", "designation_instance_id", "safe_mark_candidate", "solid")
        )
        evidence.append(
            {
                "id": command.get("id"),
                "cell": cell,
                "ack_statuses": statuses,
                "ack_status": command.get("ack_status"),
                "classification": command.get("classification"),
                "quarantine_reason": reason,
                "no_accept_or_execution_ack": not any(
                    stage in statuses
                    for stage in ("accepted", "executed", "local_effect_observed", "verified")
                ),
                "fingerprint_precondition_rejected": fingerprint_precondition_reason(reason),
                "expected_unchanged_fingerprint": expected_fingerprint,
                "final_fingerprint": final_fingerprint,
                "final_state_observed": observed_final,
                "final_state_unchanged": final_state_unchanged,
            }
        )

    passed = bool(evidence) and all(
        item["ack_status"] == "rejected"
        and item["classification"] == "behavior_fail"
        and item["no_accept_or_execution_ack"]
        and item["fingerprint_precondition_rejected"]
        and item["final_state_observed"]
        and item["final_state_unchanged"]
        for item in evidence
    )
    return {
        "status": "passed" if passed else "not_run" if not rejected else "failed",
        "classification": None if passed or not rejected else "behavior_fail",
        "rejected_command_count": len(rejected),
        "evidence": evidence,
    }


def blueprint_restore_check(
    commands: List[Dict[str, Any]],
    records: List[Dict[str, Any]],
    *,
    place_action: str,
    cancel_action: str,
    place_prefix: str,
    cancel_prefix: str,
    def_id: str,
    is_utility: bool,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    def candidates(action: str, prefix: str) -> List[Dict[str, Any]]:
        result = [
            command
            for command in commands
            if command.get("action") == action
            and str(command.get("id") or "").startswith(prefix)
            and (session_id is None or command.get("session_id") == session_id)
            and command.get("_directory") == "done"
            and command.get("status") in {"confirmed", "verified"}
        ]
        result.sort(key=lambda item: str(item.get("completed_at") or ""))
        return result

    placement_candidates = candidates(place_action, place_prefix)
    cancellation_candidates = candidates(cancel_action, cancel_prefix)
    placements = [command for command in placement_candidates if confirmed_mutating_command(command)]
    cancellations = [command for command in cancellation_candidates if confirmed_mutating_command(command)]
    placement_evidence: List[Dict[str, Any]] = []
    cancellation_evidence: List[Dict[str, Any]] = []

    for command in placements:
        args = command.get("args") if isinstance(command.get("args"), dict) else {}
        cells = args.get("cells") if isinstance(args.get("cells"), list) else []
        materials = args.get("material_tags") if isinstance(args.get("material_tags"), list) else []
        result = command.get("result") if isinstance(command.get("result"), dict) else {}
        before = result.get("before_state") if isinstance(result.get("before_state"), dict) else {}
        after = confirmation_witness(command)
        statuses = ack_statuses(records, str(command.get("id")))
        catalog_prefix = f"blueprints.catalog.{def_id}"
        materials_available_before = safe_get(before, f"{catalog_prefix}.materials_available")
        available_mass_kg_before = safe_get(before, f"{catalog_prefix}.available_mass_kg")
        zero_inventory_observed = (
            materials_available_before is False
            and isinstance(available_mass_kg_before, list)
            and len(available_mass_kg_before) == len(materials)
            and bool(available_mass_kg_before)
            and all(
                isinstance(value, (int, float)) and not isinstance(value, bool) and value == 0
                for value in available_mass_kg_before
            )
            and all(
                quality_entry(before, f"{catalog_prefix}.{field}").get("status") == "observed"
                for field in ("materials_available", "available_mass_kg")
            )
        )
        per_cell: List[Dict[str, Any]] = []
        for cell in cells:
            if isinstance(cell, bool) or not isinstance(cell, int):
                continue
            prefix = blueprint_cell_path(cell)
            instance_id = safe_get(after, f"{prefix}.blueprint_instance_id")
            before_fingerprint = safe_get(before, f"{prefix}.fingerprint")
            empty_fingerprint = safe_get(before, f"{prefix}.empty_fingerprint")
            physical_unchanged = all(
                safe_get(before, f"{prefix}.{field}") == safe_get(after, f"{prefix}.{field}")
                for field in ("element_id", "mass_kg", "damage")
            )
            created = (
                safe_get(before, f"{prefix}.blueprint_present") is False
                and safe_get(before, f"{prefix}.blueprint_instance_id") == 0
                and safe_get(before, f"{prefix}.object_count") == 0
                and before_fingerprint == empty_fingerprint
                and safe_get(after, f"{prefix}.blueprint_present") is True
                and isinstance(instance_id, int)
                and not isinstance(instance_id, bool)
                and instance_id > 0
                and safe_get(after, f"{prefix}.blueprint_prefab_id") == def_id
                and safe_get(after, f"{prefix}.blueprint_orientation") == "Neutral"
                and safe_get(after, f"{prefix}.blueprint_material_tags") == materials
                and safe_get(after, f"{prefix}.blueprint_in_progress") is False
                and safe_get(after, f"{prefix}.blueprint_is_utility") is is_utility
                and safe_get(after, f"{prefix}.fingerprint") != before_fingerprint
                and safe_get(after, f"{prefix}.empty_fingerprint") == empty_fingerprint
            )
            per_cell.append(
                {
                    "cell": cell,
                    "before_fingerprint": before_fingerprint,
                    "after_fingerprint": safe_get(after, f"{prefix}.fingerprint"),
                    "empty_fingerprint": empty_fingerprint,
                    "blueprint_instance_id": instance_id,
                    "created": created,
                    "physical_unchanged": physical_unchanged,
                }
            )
        path_valid = True
        if is_utility:
            path_id = args.get("path_id")
            path_prefix = utility_path_path(path_id) if isinstance(path_id, str) else ""
            path_valid = bool(path_prefix) and (
                safe_get(after, f"{path_prefix}.path_id") == path_id
                and safe_get(after, f"{path_prefix}.building_def_id") == def_id
                and safe_get(after, f"{path_prefix}.cells") == cells
                and safe_get(after, f"{path_prefix}.blueprint_instance_ids")
                == [item["blueprint_instance_id"] for item in per_cell]
                and safe_get(after, f"{path_prefix}.active_blueprint_instance_ids")
                == [item["blueprint_instance_id"] for item in per_cell]
                and safe_get(after, f"{path_prefix}.fully_active") is True
            )
        required_acks = all(stage in statuses for stage in REQUIRED_MUTATION_ACKS)
        placement_evidence.append(
            {
                "id": command.get("id"),
                "completed_at": command.get("completed_at"),
                "cells": cells,
                "ack_statuses": statuses,
                "confirmation": confirmation_contract(command),
                "required_ack_stages": required_acks,
                "path_valid": path_valid,
                "materials_available_before": materials_available_before,
                "available_mass_kg_before": available_mass_kg_before,
                "zero_inventory_observed": zero_inventory_observed,
                "per_cell": per_cell,
                "valid": bool(per_cell)
                and required_acks
                and path_valid
                and zero_inventory_observed
                and all(item["created"] and item["physical_unchanged"] for item in per_cell),
            }
        )

    for command in cancellations:
        args = command.get("args") if isinstance(command.get("args"), dict) else {}
        targets = args.get("targets") if isinstance(args.get("targets"), list) else []
        result = command.get("result") if isinstance(command.get("result"), dict) else {}
        before = result.get("before_state") if isinstance(result.get("before_state"), dict) else {}
        after = confirmation_witness(command)
        statuses = ack_statuses(records, str(command.get("id")))
        per_cell: List[Dict[str, Any]] = []
        for target in targets:
            if not isinstance(target, dict):
                continue
            cell = target.get("cell")
            instance_id = target.get("blueprint_instance_id")
            if isinstance(cell, bool) or not isinstance(cell, int):
                continue
            prefix = blueprint_cell_path(cell)
            empty_fingerprint = safe_get(before, f"{prefix}.empty_fingerprint")
            physical_unchanged = all(
                safe_get(before, f"{prefix}.{field}") == safe_get(after, f"{prefix}.{field}")
                for field in ("element_id", "mass_kg", "damage")
            )
            removed = (
                safe_get(before, f"{prefix}.blueprint_present") is True
                and safe_get(before, f"{prefix}.blueprint_instance_id") == instance_id
                and safe_get(before, f"{prefix}.blueprint_prefab_id") == def_id
                and safe_get(before, f"{prefix}.blueprint_in_progress") is False
                and safe_get(after, f"{prefix}.blueprint_present") is False
                and safe_get(after, f"{prefix}.blueprint_instance_id") == 0
                and safe_get(after, f"{prefix}.object_count") == 0
                and safe_get(after, f"{prefix}.fingerprint") == empty_fingerprint
            )
            per_cell.append(
                {
                    "cell": cell,
                    "target_instance_id": instance_id,
                    "before_fingerprint": safe_get(before, f"{prefix}.fingerprint"),
                    "after_fingerprint": safe_get(after, f"{prefix}.fingerprint"),
                    "empty_fingerprint": empty_fingerprint,
                    "removed": removed,
                    "physical_unchanged": physical_unchanged,
                }
            )
        path_valid = True
        if is_utility:
            path_id = args.get("path_id")
            path_prefix = utility_path_path(path_id) if isinstance(path_id, str) else ""
            path_valid = bool(path_prefix) and (
                safe_get(after, f"{path_prefix}.active_blueprint_instance_ids") == []
                and safe_get(after, f"{path_prefix}.active") is False
                and safe_get(after, f"{path_prefix}.fully_active") is False
            )
        required_acks = all(stage in statuses for stage in REQUIRED_MUTATION_ACKS)
        cancellation_evidence.append(
            {
                "id": command.get("id"),
                "completed_at": command.get("completed_at"),
                "targets": targets,
                "ack_statuses": statuses,
                "confirmation": confirmation_contract(command),
                "required_ack_stages": required_acks,
                "path_valid": path_valid,
                "per_cell": per_cell,
                "valid": bool(per_cell)
                and required_acks
                and path_valid
                and all(item["removed"] and item["physical_unchanged"] for item in per_cell),
            }
        )

    restore_pairs: List[Dict[str, Any]] = []
    for placement in placement_evidence:
        placed = {item["cell"]: item for item in placement["per_cell"]}
        for cancellation in cancellation_evidence:
            cancelled = {item["cell"]: item for item in cancellation["per_cell"]}
            ordered = str(cancellation.get("completed_at") or "") > str(placement.get("completed_at") or "")
            exact = bool(placed) and set(placed) == set(cancelled) and all(
                placed[cell]["blueprint_instance_id"] == cancelled[cell]["target_instance_id"]
                and placed[cell]["after_fingerprint"] == cancelled[cell]["before_fingerprint"]
                and placed[cell]["before_fingerprint"] == cancelled[cell]["after_fingerprint"]
                for cell in placed
            )
            if ordered and placement["valid"] and cancellation["valid"] and exact:
                restore_pairs.append(
                    {
                        "place_id": placement["id"],
                        "cancel_id": cancellation["id"],
                        "cells": sorted(placed),
                        "exact_instance_handoff": True,
                        "fingerprint_restored": True,
                    }
                )
    all_candidates = placement_candidates + cancellation_candidates
    all_evidence = placement_evidence + cancellation_evidence
    passed = bool(restore_pairs) and bool(all_evidence) and all(item["valid"] for item in all_evidence)
    return {
        "status": "passed" if passed else "not_run" if not all_candidates else "failed",
        "classification": None if passed or not all_candidates else confirmation_failure_classification(all_candidates),
        "confirmed_place_count": len(placements),
        "confirmed_cancel_count": len(cancellations),
        "candidate_command_count": len(all_candidates),
        "confirmation_rejections": [
            {"id": command.get("id"), "confirmation": confirmation_contract(command)}
            for command in all_candidates
            if not confirmed_mutating_command(command)
        ],
        "restore_pairs": restore_pairs,
        "placement_evidence": placement_evidence,
        "cancellation_evidence": cancellation_evidence,
    }


def blueprint_invalid_target_check(
    commands: List[Dict[str, Any]],
    records: List[Dict[str, Any]],
    snapshots: List[Dict[str, Any]],
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    rejected = [
        command
        for command in commands
        if command.get("action") == "place_building_blueprint"
        and str(command.get("id") or "").startswith("cmd-runtime-blueprint-invalid-")
        and (session_id is None or command.get("session_id") == session_id)
        and command.get("_directory") == "quarantine"
        and command.get("status") == "quarantined"
    ]
    evidence: List[Dict[str, Any]] = []
    for command in rejected:
        latest = latest_snapshot_for_command(records, command) or (snapshots[-1] if snapshots else {})
        command_evidence = command.get("evidence") if isinstance(command.get("evidence"), dict) else {}
        cell = command_evidence.get("cell")
        if isinstance(cell, bool) or not isinstance(cell, int):
            continue
        prefix = blueprint_cell_path(cell)
        statuses = ack_statuses(records, str(command.get("id")))
        reason = str(command.get("quarantine_reason") or "")
        expected_fingerprint = command_evidence.get("expected_unchanged_fingerprint")
        final_fingerprint = safe_get(latest, f"{prefix}.fingerprint")
        observed_final = all(
            quality_entry(latest, f"{prefix}.{field}").get("status") == "observed"
            for field in ("fingerprint", "blueprint_present", "blueprint_instance_id", "object_count")
        )
        final_state_unchanged = (
            isinstance(expected_fingerprint, str)
            and final_fingerprint == expected_fingerprint
            and safe_get(latest, f"{prefix}.blueprint_present") is False
            and safe_get(latest, f"{prefix}.blueprint_instance_id") == 0
            and safe_get(latest, f"{prefix}.object_count") == 0
        )
        evidence.append(
            {
                "id": command.get("id"),
                "cell": cell,
                "ack_statuses": statuses,
                "ack_status": command.get("ack_status"),
                "classification": command.get("classification"),
                "quarantine_reason": reason,
                "no_accept_or_execution_ack": not any(
                    stage in statuses
                    for stage in ("accepted", "executed", "local_effect_observed", "verified")
                ),
                "fingerprint_precondition_rejected": fingerprint_precondition_reason(reason),
                "expected_unchanged_fingerprint": expected_fingerprint,
                "final_fingerprint": final_fingerprint,
                "final_state_observed": observed_final,
                "final_state_unchanged": final_state_unchanged,
            }
        )
    passed = bool(evidence) and all(
        item["ack_status"] == "rejected"
        and item["classification"] == "behavior_fail"
        and item["no_accept_or_execution_ack"]
        and item["fingerprint_precondition_rejected"]
        and item["final_state_observed"]
        and item["final_state_unchanged"]
        for item in evidence
    )
    return {
        "status": "passed" if passed else "not_run" if not rejected else "failed",
        "classification": None if passed or not rejected else "behavior_fail",
        "rejected_command_count": len(rejected),
        "evidence": evidence,
    }


def stale_check(
    commands: List[Dict[str, Any]],
    records: List[Dict[str, Any]],
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    stale = [
        command
        for command in commands
        if str(command.get("id") or "").startswith("cmd-runtime-stale-")
        and command.get("_directory") == "quarantine"
        and (session_id is None or command.get("session_id") == session_id)
    ]
    evidence = []
    for command in stale:
        statuses = ack_statuses(records, str(command.get("id")))
        evidence.append(
            {
                "id": command.get("id"),
                "directory": command.get("_directory"),
                "ack_status": command.get("ack_status"),
                "ack_statuses": statuses,
                "reason": command.get("quarantine_reason"),
            }
        )
    passed = any(item["ack_status"] == "skipped_stale" and "skipped_stale" in item["ack_statuses"] for item in evidence)
    return {
        "status": "passed" if passed else "not_run" if not stale else "failed",
        "classification": None if passed or not stale else "behavior_fail",
        "evidence": evidence,
    }


def duplicate_check(
    commands: List[Dict[str, Any]],
    records: List[Dict[str, Any]],
    replay_session_id: Optional[str] = None,
) -> Dict[str, Any]:
    done_by_id = {
        str(command.get("id")): command
        for command in commands
        if command.get("_directory") == "done"
        and str(command.get("id") or "").startswith("cmd-runtime-pause-")
        and command.get("status") in {"confirmed", "verified"}
    }
    quarantined_ids = {
        str(command.get("id"))
        for command in commands
        if command.get("_directory") == "quarantine" and str(command.get("id")) in done_by_id
    }
    evidence = []
    for command_id in sorted(quarantined_ids):
        original = done_by_id[command_id]
        original_session_id = str(original.get("session_id") or "") or None
        original_statuses = [
            str(record.get("status"))
            for record in records
            if record.get("event") == "command_ack"
            and str(record.get("id")) == command_id
            and (original_session_id is None or str(record.get("session_id") or "") == original_session_id)
        ]
        replay_statuses = [
            str(record.get("status"))
            for record in records
            if record.get("event") == "command_ack"
            and str(record.get("id")) == command_id
            and (replay_session_id is None or str(record.get("session_id") or "") == replay_session_id)
        ]
        statuses = ack_statuses(records, command_id) if replay_session_id is None else original_statuses + replay_statuses
        session_boundary_verified = (
            replay_session_id is None
            or (
                original_session_id is not None
                and replay_session_id is not None
                and original_session_id != replay_session_id
            )
        )
        evidence.append(
            {
                "id": command_id,
                "ack_statuses": statuses,
                "original_session_id": original_session_id,
                "replay_session_id": replay_session_id,
                "original_ack_statuses": original_statuses,
                "replay_ack_statuses": replay_statuses,
                "session_boundary_verified": session_boundary_verified,
                "confirmation": confirmation_contract(original),
                "snapshot_confirmation_valid": confirmed_mutating_command(original),
                "accepted_count": original_statuses.count("accepted"),
                "executed_count": original_statuses.count("executed"),
                "local_effect_observed_count": original_statuses.count("local_effect_observed"),
                "skipped_stale_count": replay_statuses.count("skipped_stale"),
            }
        )
    passed = any(
        item["snapshot_confirmation_valid"]
        and item["accepted_count"] == 1
        and item["executed_count"] == 1
        and item["local_effect_observed_count"] == 1
        and item["skipped_stale_count"] >= 1
        and item["session_boundary_verified"]
        for item in evidence
    )
    return {
        "status": "passed" if passed else "not_run" if not evidence else "failed",
        "classification": None if passed or not evidence else "behavior_fail",
        "evidence": evidence,
    }


def percentile95(values: List[float]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * 0.95) - 1))
    return ordered[index]


def performance_check(snapshots: List[Dict[str, Any]], minimum_duration_seconds: float) -> Dict[str, Any]:
    timestamps = [parse_timestamp(item.get("ts")) for item in snapshots]
    timestamps = [item for item in timestamps if item is not None]
    duration = (timestamps[-1] - timestamps[0]).total_seconds() if len(timestamps) >= 2 else 0.0
    samples = [
        float(safe_get(item, "sample.duration_ms"))
        for item in snapshots
        if isinstance(safe_get(item, "sample.duration_ms"), (int, float))
    ]
    queue_depths = [
        int(safe_get(item, "writer.queue_depth"))
        for item in snapshots
        if isinstance(safe_get(item, "writer.queue_depth"), (int, float))
    ]
    dropped = [
        int(safe_get(item, "writer.dropped_snapshot_count"))
        for item in snapshots
        if isinstance(safe_get(item, "writer.dropped_snapshot_count"), (int, float))
    ]
    p95 = percentile95(samples)
    maximum = max(samples) if samples else None
    max_queue = max(queue_depths) if queue_depths else None
    dropped_max = max(dropped) if dropped else None
    passed = (
        duration >= minimum_duration_seconds
        and p95 is not None
        and p95 <= 20.0
        and maximum is not None
        and maximum <= 50.0
        and max_queue is not None
        and max_queue < 100
        and dropped_max == 0
    )
    not_run = duration < minimum_duration_seconds
    return {
        "status": "passed" if passed else "not_run" if not_run else "failed",
        "classification": None if passed or not_run else "behavior_fail",
        "snapshot_count": len(snapshots),
        "duration_seconds": round(duration, 3),
        "required_duration_seconds": minimum_duration_seconds,
        "sample_duration_ms": {
            "count": len(samples),
            "mean": None if not samples else round(statistics.fmean(samples), 3),
            "p95": None if p95 is None else round(p95, 3),
            "max": None if maximum is None else round(maximum, 3),
        },
        "writer": {"max_queue_depth": max_queue, "max_dropped_snapshot_count": dropped_max},
    }


def parse_timestamp(value: Any) -> Optional[dt.datetime]:
    if not value:
        return None
    try:
        result = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=dt.timezone.utc)
    return result.astimezone()


def file_sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def player_log_check(path: pathlib.Path) -> Dict[str, Any]:
    if not path.exists():
        return {"exists": False, "passed": False, "classification": "infra_fail"}
    data = path.read_bytes()[-5 * 1024 * 1024 :]
    text = data.decode("utf-8", errors="replace")
    markers = (
        "[ONI AI Probe] Loading observer-default probe V2",
        "ONI AI Probe: Successfully loaded from path 'root' with content 'DLL'",
    )
    start = max(text.rfind(marker) for marker in markers)
    segment = text[start:] if start >= 0 else ""
    relevant = [
        line.strip()
        for line in segment.splitlines()
        if "ONI AI Probe" in line or "OniAiProbeMod" in line
    ]
    error_lines = [
        line
        for line in relevant
        if any(token in line.lower() for token in ("failed", "exception", "fatal", "error"))
    ]
    code_log_found = any("[ONI AI Probe] Loading observer-default probe V2" in line for line in relevant)
    mod_manager_load_found = any("Successfully loaded from path" in line for line in relevant)
    passed = start >= 0 and (code_log_found or mod_manager_load_found) and not error_lines
    return {
        "exists": True,
        "passed": passed,
        "classification": None if passed else "runtime_fail" if start >= 0 else "infra_fail",
        "size_bytes": path.stat().st_size,
        "sha256": "sha256:" + file_sha256(path),
        "load_marker_found": start >= 0,
        "code_log_found": code_log_found,
        "mod_manager_load_found": mod_manager_load_found,
        "relevant_line_count": len(relevant),
        "error_line_count": len(error_lines),
        "summary_lines": relevant[-20:],
    }


def save_lineage_runtime_context(
    lineage_path: Optional[str],
    root: pathlib.Path,
    *,
    require_complete: bool,
    verify_live: bool = True,
) -> Tuple[Dict[str, Any], set[str], Optional[Dict[str, Any]]]:
    if not lineage_path:
        return (
            {
                "status": "not_run",
                "classification": "infra_fail",
                "reason": "--save-lineage is required for final validation",
            },
            set(),
            None,
        )
    path = pathlib.Path(lineage_path).expanduser().resolve()
    try:
        manifest = load_lineage_json(path)
        report = verify_lineage(
            path,
            root,
            expected_mod_version=str(manifest.get("expected_mod_version") or ""),
            required_steps=REQUIRED_LINEAGE_STEPS if require_complete else (),
            required_step_order=REQUIRED_LINEAGE_STEPS if require_complete else (),
            minimum_checkpoints=len(REQUIRED_LINEAGE_STEPS) if require_complete else 1,
            verify_live=verify_live,
        )
        latest = manifest.get("latest") if isinstance(manifest.get("latest"), dict) else {}
        save_path = pathlib.Path(str(manifest.get("save_path") or "")).expanduser().resolve()
        disk_tip_ok = save_path.is_file() and lineage_sha256_file(save_path) == latest.get("save_id_hash")
        sessions: set[str] = set()
        initial = manifest.get("initial") if isinstance(manifest.get("initial"), dict) else {}
        initial_reload = initial.get("reload") if isinstance(initial.get("reload"), dict) else {}
        if initial_reload.get("session_id"):
            sessions.add(str(initial_reload["session_id"]))
        checkpoints = manifest.get("checkpoints") if isinstance(manifest.get("checkpoints"), list) else []
        for checkpoint in checkpoints:
            if not isinstance(checkpoint, dict):
                continue
            for field in ("before", "reload"):
                identity = checkpoint.get(field) if isinstance(checkpoint.get(field), dict) else {}
                if identity.get("session_id"):
                    sessions.add(str(identity["session_id"]))
        passed = report.get("status") == "passed" and bool(sessions) and (verify_live or disk_tip_ok)
        lineage_status = (
            "passed"
            if passed
            else "legacy_read_only"
            if report.get("status") == "legacy_read_only"
            else "failed"
        )
        return (
            {
                "status": lineage_status,
                "classification": None if passed else report.get("classification") or "behavior_fail",
                "manifest": str(path),
                "test_run_id": manifest.get("test_run_id"),
                "checkpoint_count": len(checkpoints),
                "session_count": len(sessions),
                "verification_mode": "live" if verify_live else "offline_after_shutdown",
                "disk_tip_verified": disk_tip_ok,
                "verification": report,
            },
            sessions,
            manifest,
        )
    except Exception as exc:
        return (
            {
                "status": "failed",
                "classification": "infra_fail",
                "manifest": str(path),
                "error": str(exc),
            },
            set(),
            None,
        )


def colony_plan_runtime_check(
    lineage_sessions: set[str],
    lineage_manifest: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    try:
        report = load_json(COLONY_PLAN_LIVE_REPORT)
        report_digest = "sha256:" + file_sha256(COLONY_PLAN_LIVE_REPORT)
        checkpoints = (
            lineage_manifest.get("checkpoints")
            if isinstance(lineage_manifest, dict) and isinstance(lineage_manifest.get("checkpoints"), list)
            else []
        )
        matches = [
            checkpoint
            for checkpoint in checkpoints
            if isinstance(checkpoint, dict) and checkpoint.get("step_id") == "colony_plan_runtime"
        ]
        if len(matches) != 1:
            return {
                "status": "failed",
                "classification": "behavior_fail",
                "error": "colony_plan_runtime checkpoint is missing or ambiguous",
                "checkpoint_count": len(matches),
            }
        checkpoint = matches[0]
        before = checkpoint.get("before") if isinstance(checkpoint.get("before"), dict) else {}
        frozen_report_artifacts = []
        for artifact in checkpoint.get("artifacts", []):
            if not isinstance(artifact, dict) or artifact.get("sha256") != report_digest:
                continue
            path = pathlib.Path(str(artifact.get("path") or "")).resolve()
            if (
                path.is_file()
                and path.name.endswith("-colony-plan-live.json")
                and "sha256:" + file_sha256(path) == report_digest
            ):
                frozen_report_artifacts.append(path)
        passed = (
            report.get("schema") == "oni.colony-plan-verification.v1"
            and report.get("mode") == "live"
            and report.get("status") == "passed"
            and report.get("session_id") in lineage_sessions
            and report.get("session_id") == before.get("session_id")
            and report.get("save_id_hash") == before.get("save_id_hash")
            and report.get("final_status") == "aborted"
            and report.get("command_requires_approval") is True
            and report.get("command_final_directory") == "quarantine"
            and report.get("game_command_executed") is False
            and len(frozen_report_artifacts) == 1
            and checkpoint.get("outcome") == "control_plane_verified"
            and checkpoint.get("command") is None
        )
        return {
            "status": "passed" if passed else "failed",
            "classification": None if passed else "behavior_fail",
            "report": str(COLONY_PLAN_LIVE_REPORT),
            "session_id": report.get("session_id"),
            "save_id_hash": report.get("save_id_hash"),
            "plan_id": report.get("plan_id"),
            "final_status": report.get("final_status"),
            "game_command_executed": report.get("game_command_executed"),
            "checkpoint_bound": len(matches) == 1,
            "artifact_hash_bound": len(frozen_report_artifacts) == 1,
        }
    except Exception as exc:
        return {"status": "failed", "classification": "infra_fail", "error": str(exc)}


def checkpoint_before_session(lineage_manifest: Optional[Dict[str, Any]], step_id: str) -> Optional[str]:
    if not isinstance(lineage_manifest, dict):
        return None
    checkpoints = lineage_manifest.get("checkpoints")
    if not isinstance(checkpoints, list):
        return None
    matches = [
        checkpoint
        for checkpoint in checkpoints
        if isinstance(checkpoint, dict) and checkpoint.get("step_id") == step_id
    ]
    if len(matches) != 1:
        return None
    before = matches[0].get("before") if isinstance(matches[0].get("before"), dict) else {}
    return str(before.get("session_id") or "") or None


def collect(args: argparse.Namespace) -> int:
    if args.offline_after_shutdown and args.phase != "final":
        raise RuntimeError("--offline-after-shutdown is only valid for final collection.")
    root = bridge_dir(args.bridge_dir)
    sources = runtime_record_sources(root)
    records = sources["lifecycle_records"]
    session_id = sources["session_id"]
    session_records = sources["session_records"]
    snapshots = sources["snapshots"]
    invalid = sources["invalid_lines"]
    player = player_log_check(pathlib.Path(args.player_log).expanduser().resolve())
    commands = load_command_files(root)
    lineage_check, lineage_sessions, lineage_manifest = save_lineage_runtime_context(
        args.save_lineage,
        root,
        require_complete=args.phase == "final",
        verify_live=not args.offline_after_shutdown,
    )
    lineage_sources: Dict[str, Any] = {"records": [], "invalid_lines": 0, "archive_paths": []}
    if lineage_sessions:
        lineage_sources = lineage_session_record_sources(root, lineage_sessions, records)
        action_records = lineage_sources["records"]
        action_commands = [command for command in commands if str(command.get("session_id") or "") in lineage_sessions]
        action_snapshots = [record for record in action_records if record.get("event") == "state_snapshot"]
        action_session_id: Optional[str] = None
    else:
        action_records = session_records
        action_commands = commands
        action_snapshots = snapshots
        action_session_id = session_id

    # Runtime success is never inferred from executor-owned done/result fields.
    # Each mutating candidate is bound to the published+embedded Bridge
    # attestation, ordered durable ACKs, and the archived later witness before
    # scenario-specific checks consume it.
    annotate_command_confirmations(root, action_commands, action_records)

    def checkpoint_context(
        step_id: str,
    ) -> Tuple[Optional[str], List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
        checkpoint_session_id = checkpoint_before_session(lineage_manifest, step_id)
        if lineage_manifest is not None:
            if not checkpoint_session_id:
                return None, [], [], []
            bound_commands = [
                command
                for command in action_commands
                if str(command.get("session_id") or "") == checkpoint_session_id
            ]
            bound_records = [
                record
                for record in action_records
                if str(record.get("session_id") or "") == checkpoint_session_id
            ]
            bound_snapshots = [record for record in bound_records if record.get("event") == "state_snapshot"]
            return checkpoint_session_id, bound_commands, bound_records, bound_snapshots
        return action_session_id, action_commands, action_records, action_snapshots

    observer_session_id, _, observer_records, observer_snapshots = checkpoint_context("observer_start")
    pause_session_id, pause_commands, pause_records, _ = checkpoint_context("pause_runtime")
    research_session_id, research_commands, research_records, _ = checkpoint_context("research_runtime")
    priority_session_id, priority_commands, priority_records, priority_snapshots = checkpoint_context(
        "duplicant_priority_runtime"
    )
    invalid_priority_session_id, invalid_priority_commands, invalid_priority_records, invalid_priority_snapshots = (
        checkpoint_context("duplicant_priority_invalid_target")
    )
    dig_session_id, dig_commands, dig_records, _ = checkpoint_context("dig_designation_runtime")
    invalid_dig_session_id, invalid_dig_commands, invalid_dig_records, invalid_dig_snapshots = checkpoint_context(
        "dig_designation_invalid_target"
    )
    building_session_id, building_commands, building_records, _ = checkpoint_context("building_blueprint_runtime")
    utility_session_id, utility_commands, utility_records, _ = checkpoint_context("utility_blueprint_path_runtime")
    invalid_blueprint_session_id, invalid_blueprint_commands, invalid_blueprint_records, invalid_blueprint_snapshots = (
        checkpoint_context("blueprint_invalid_target")
    )
    stale_session_id, stale_commands, stale_records, _ = checkpoint_context("stale_command_quarantine")
    duplicate_session_id = checkpoint_before_session(lineage_manifest, "duplicate_command_idempotency")
    performance_session_id = checkpoint_before_session(lineage_manifest, "probe_performance_10m")
    if performance_session_id:
        performance_snapshots = [
            record
            for record in action_records
            if record.get("event") == "state_snapshot" and record.get("session_id") == performance_session_id
        ]
    else:
        performance_snapshots = snapshots
    performance_minimum = 600.0 if args.phase == "observer" else float(args.minimum_duration_seconds)
    checks = {
        "observer_start": observer_check(
            observer_session_id,
            records,
            observer_records,
            observer_snapshots,
            lineage_sources["invalid_lines"] if lineage_manifest is not None else invalid,
            player,
        ),
        "pause_runtime": action_check(pause_commands, pause_records, "set_paused", "game.paused"),
        "research_runtime": action_check(research_commands, research_records, "set_research", "research.current"),
        "duplicant_priority_runtime": duplicant_priority_check(
            priority_commands,
            priority_records,
            priority_session_id,
        ),
        "duplicant_priority_invalid_target": duplicant_priority_invalid_target_check(
            invalid_priority_commands,
            invalid_priority_records,
            invalid_priority_snapshots,
            invalid_priority_session_id,
        ),
        "dig_designation_runtime": dig_designation_check(dig_commands, dig_records, dig_session_id),
        "dig_designation_invalid_target": dig_invalid_target_check(
            invalid_dig_commands,
            invalid_dig_records,
            invalid_dig_snapshots,
            invalid_dig_session_id,
        ),
        "building_blueprint_runtime": blueprint_restore_check(
            building_commands,
            building_records,
            place_action="place_building_blueprint",
            cancel_action="cancel_building_blueprint",
            place_prefix="cmd-runtime-blueprint-place-",
            cancel_prefix="cmd-runtime-blueprint-cancel-",
            def_id="Ladder",
            is_utility=False,
            session_id=building_session_id,
        ),
        "utility_blueprint_path_runtime": blueprint_restore_check(
            utility_commands,
            utility_records,
            place_action="place_utility_blueprint_path",
            cancel_action="cancel_utility_blueprint_path",
            place_prefix="cmd-runtime-utility-place-",
            cancel_prefix="cmd-runtime-utility-cancel-",
            def_id="Wire",
            is_utility=True,
            session_id=utility_session_id,
        ),
        "blueprint_invalid_target": blueprint_invalid_target_check(
            invalid_blueprint_commands,
            invalid_blueprint_records,
            invalid_blueprint_snapshots,
            invalid_blueprint_session_id,
        ),
        "stale_command_quarantine": stale_check(stale_commands, stale_records, stale_session_id),
        "duplicate_command_idempotency": duplicate_check(
            action_commands,
            action_records,
            duplicate_session_id,
        ),
        "colony_plan_runtime": colony_plan_runtime_check(lineage_sessions, lineage_manifest),
        "probe_performance_10m": performance_check(performance_snapshots, performance_minimum),
        "save_lineage_runtime": lineage_check,
    }
    checkpoint_sessions = {
        "observer_start": observer_session_id,
        "pause_runtime": pause_session_id,
        "research_runtime": research_session_id,
        "duplicant_priority_runtime": priority_session_id,
        "duplicant_priority_invalid_target": invalid_priority_session_id,
        "dig_designation_runtime": dig_session_id,
        "dig_designation_invalid_target": invalid_dig_session_id,
        "building_blueprint_runtime": building_session_id,
        "utility_blueprint_path_runtime": utility_session_id,
        "blueprint_invalid_target": invalid_blueprint_session_id,
        "stale_command_quarantine": stale_session_id,
        "duplicate_command_idempotency": duplicate_session_id,
        "colony_plan_runtime": checkpoint_before_session(lineage_manifest, "colony_plan_runtime"),
        "probe_performance_10m": performance_session_id,
    }
    for check_name, checkpoint_session_id in checkpoint_sessions.items():
        checks[check_name]["checkpoint_session_id"] = checkpoint_session_id
        checks[check_name]["session_id"] = checkpoint_session_id
    checks["probe_performance_10m"]["session_id"] = performance_session_id or session_id
    if args.phase == "observer":
        required = ["observer_start"]
    elif args.phase == "batch-a":
        required = list(BATCH_A_MARKERS)
    elif args.phase == "batch-b":
        required = list(BATCH_B_MARKERS)
    elif args.phase == "batch-c":
        required = list(BATCH_C_MARKERS)
    else:
        required = list(ALL_MARKERS)
    passed = all(checks[name]["status"] == "passed" for name in required)
    report = {
        "schema": "oni.runtime-acceptance.v1",
        "mutating_success_contract": "snapshot_confirmed_mutation_v1",
        "legacy_mutating_results": "read_only_not_accepted",
        "generated_at": now_iso(),
        "phase": args.phase,
        "status": "passed" if passed else "incomplete_or_failed",
        "classification": None
        if passed
        else next(
            (checks[name].get("classification") for name in required if checks[name]["status"] == "failed"),
            "infra_fail" if not session_id else "behavior_fail",
        ),
        "bridge_dir": str(root),
        "state_log": str(sources["session_log"]),
        "session_archive_used": sources["session_archive_used"],
        "lineage_session_archives": [str(path) for path in lineage_sources["archive_paths"]],
        "lineage_archive_invalid_json_lines": lineage_sources["invalid_lines"],
        "bootstrap_state_logs": [str(path) for path in sources["root_logs"]],
        "player_log": str(pathlib.Path(args.player_log).expanduser().resolve()),
        "current_session_id": session_id,
        "current_game_build": snapshots[-1].get("game_build") if snapshots else None,
        "current_mod_version": snapshots[-1].get("mod_version") if snapshots else None,
        "current_save_id_hash": snapshots[-1].get("save_id_hash") if snapshots else None,
        "save_lineage_manifest": None if lineage_manifest is None else str(pathlib.Path(args.save_lineage).expanduser().resolve()),
        "save_lineage_test_run_id": None if lineage_manifest is None else lineage_manifest.get("test_run_id"),
        "save_lineage_checkpoint_hash": None
        if lineage_manifest is None
        else lineage_manifest.get("latest_checkpoint_hash"),
        "lineage_verification_mode": "offline_after_shutdown" if args.offline_after_shutdown else "live",
        "checks": checks,
        "required_checks": required,
    }
    RUNTIME_REPORT.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(RUNTIME_REPORT, report)
    runtime_report_sha256 = "sha256:" + file_sha256(RUNTIME_REPORT)
    MARKER_DIR.mkdir(parents=True, exist_ok=True)
    for name in ALL_MARKERS:
        marker = MARKER_DIR / f"{name}.passed"
        marker.unlink(missing_ok=True)
        if checks[name]["status"] == "passed" and (name != "probe_performance_10m" or args.phase == "final"):
            marker.write_text(
                json.dumps(
                    {
                        "schema": "oni.runtime-marker.v2",
                        "scenario": name,
                        "status": "passed",
                        "mutating_success_contract": "snapshot_confirmed_mutation_v1",
                        "session_id": checks[name].get("session_id", session_id),
                        "test_run_id": report["save_lineage_test_run_id"],
                        "lineage_checkpoint_hash": report["save_lineage_checkpoint_hash"],
                        "runtime_report_sha256": runtime_report_sha256,
                        "current_save_id_hash": report["current_save_id_hash"],
                        "generated_at": report["generated_at"],
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if passed else 1


def show_latest(args: argparse.Namespace) -> int:
    root = bridge_dir(args.bridge_dir)
    state = read_latest_state(root)
    by_cell = safe_get(state, "designations.dig.by_cell", {})
    dig_targets = [
        value
        for value in (by_cell.values() if isinstance(by_cell, dict) else [])
        if isinstance(value, dict)
        and (value.get("safe_mark_candidate") is True or value.get("designation_present") is True)
    ]
    dig_targets.sort(key=lambda value: int(value.get("cell") or 0))
    blueprint_cells = safe_get(state, "cells.by_id", {})
    blueprint_targets = [
        value
        for value in (blueprint_cells.values() if isinstance(blueprint_cells, dict) else [])
        if isinstance(value, dict)
        and (
            value.get("empty_for_blueprint") is True
            or value.get("blueprint_present") is True
        )
    ]
    blueprint_targets.sort(key=lambda value: int(value.get("cell") or 0))
    payload = {
        "session_id": state.get("session_id"),
        "seq": state.get("seq"),
        "snapshot_id": state.get("snapshot_id"),
        "save_id_hash": state.get("save_id_hash"),
        "save_dirty": state.get("save_dirty"),
        "world_id": state.get("world_id"),
        "cycle": state.get("cycle"),
        "paused": safe_get(state, "game.paused"),
        "sandbox_mode": safe_get(state, "game.sandbox_mode"),
        "instant_build_mode": safe_get(state, "game.instant_build_mode"),
        "duplicants_total": safe_get(state, "duplicants.total"),
        "food_kcal": safe_get(state, "resources.food_kcal"),
        "research_current": safe_get(state, "research.current"),
        "dig_tool": safe_get(state, "designations.dig.tool"),
        "dig_candidate_cells": safe_get(state, "designations.dig.candidate_cells"),
        "dig_targets": [
            {
                "cell": value.get("cell"),
                "x": value.get("x"),
                "y": value.get("y"),
                "element_id": value.get("element_id"),
                "mass_kg": value.get("mass_kg"),
                "designation_present": value.get("designation_present"),
                "designation_instance_id": value.get("designation_instance_id"),
                "designation_kind": value.get("designation_kind"),
                "designation_in_progress": value.get("designation_in_progress"),
                "safe_mark_candidate": value.get("safe_mark_candidate"),
                "fingerprint": value.get("fingerprint"),
            }
            for value in dig_targets[:32]
        ],
        "blueprint_catalog": safe_get(state, "blueprints.catalog"),
        "utility_blueprint_paths": safe_get(state, "blueprints.utility_paths"),
        "blueprint_targets": [
            {
                "cell": value.get("cell"),
                "x": value.get("x"),
                "y": value.get("y"),
                "element_id": value.get("element_id"),
                "mass_kg": value.get("mass_kg"),
                "can_place_ladder": value.get("can_place_ladder"),
                "can_place_wire": value.get("can_place_wire"),
                "blueprint_present": value.get("blueprint_present"),
                "blueprint_instance_id": value.get("blueprint_instance_id"),
                "blueprint_prefab_id": value.get("blueprint_prefab_id"),
                "blueprint_in_progress": value.get("blueprint_in_progress"),
                "fingerprint": value.get("fingerprint"),
                "empty_fingerprint": value.get("empty_fingerprint"),
            }
            for value in blueprint_targets[:64]
        ],
        "duplicant_targets": [
            {
                "instance_id": value.get("instance_id"),
                "name": value.get("name"),
                "chore_priorities": value.get("chore_priorities"),
            }
            for value in (
                safe_get(state, "duplicants.by_instance_id", {}).values()
                if isinstance(safe_get(state, "duplicants.by_instance_id", {}), dict)
                else []
            )
            if isinstance(value, dict)
        ],
        "snapshot_age_seconds": snapshot_age_seconds(state),
        "critical_quality": {path: quality_entry(state, path).get("status") for path in CRITICAL_QUALITY},
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def pause_observation(root: pathlib.Path, expected: Dict[str, Any]) -> Dict[str, Any]:
    """Read raw observation evidence without requiring permission to propose."""
    frame, frame_raw = _read_json_object(root / "decision-frame.json", "decision_frame")
    state, state_raw = _read_json_object(root / "latest_state.json", "latest_state")
    health, health_raw = _read_json_object(root / "health.json", "health")
    _, after_raw = _read_json_object(root / "decision-frame.json", "decision_frame")
    if frame_raw != after_raw:
        raise RuntimeError("pause_observation:bundle_generation_changed")
    validate_decision_frame(frame)
    blockers = binding_blockers(state, health, frame, now=local_now())
    for key, value in expected.items():
        if state.get(key) != value:
            blockers.append("identity_mismatch:" + key)
    paused = read_contract_field(state, "game.paused", GAME_PAUSED_CONTRACT_ID)
    if not paused.usable or not isinstance(paused.value, bool):
        blockers.append("paused_field_contract_invalid")
    try:
        validate_latest_state(state)
    except RuntimeError as exc:
        blockers.append(str(exc))
    return {"state": state, "health": health, "frame": frame, "paused": paused.value,
            "blockers": sorted(set(blockers)), "observed_at": local_now().isoformat(),
            "raw": {"latest_state.json": state_raw, "health.json": health_raw,
                    "decision-frame.json": frame_raw}}


def persist_pause_observation(bundle: Dict[str, Any], output: pathlib.Path) -> Dict[str, Any]:
    output.mkdir(parents=True, exist_ok=False)
    files = {}
    for name, raw in bundle["raw"].items():
        with (output / name).open("xb") as stream:
            stream.write(raw)
        files[name] = {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
    manifest = {"observed_at": bundle["observed_at"], "saved_at": local_now().isoformat(),
                "session_id": bundle["state"].get("session_id"),
                "snapshot_id": bundle["state"].get("snapshot_id"),
                "seq": bundle["state"].get("seq"),
                "snapshot_seq": bundle["state"].get("snapshot_seq"),
                "frame_id": bundle["frame"].get("frame_id"), "paused": bundle["paused"],
                "blockers": bundle["blockers"], "files": files, "path": str(output)}
    write_json_atomic(output / "observation.json", manifest)
    return manifest


def pause_command_copies(root: pathlib.Path, command_id: str) -> List[Dict[str, Any]]:
    """Read every current copy; absence or multiple copies never authorizes replay."""
    found = []
    with lifecycle_custody(root, timeout_seconds=0.1):
        for stage in COMMAND_DIRS:
            path = root / "commands" / stage / (command_id + ".json")
            try:
                value, raw = _read_json_object(path, "command")
            except Exception:
                if not path.exists():
                    continue
                raise
            found.append({"stage": stage, "command": value, "raw": raw})
    return found


def pause_roundtrip(args: argparse.Namespace) -> int:
    """Run exactly two new pause commands using the existing approval/Bridge path."""
    root = bridge_dir(args.bridge_dir)
    output = pathlib.Path(args.evidence_dir).expanduser().resolve()
    if output == root or root in output.parents or output in root.parents:
        raise RuntimeError("Pause evidence must be outside the Bridge directory.")
    if not (0.05 <= args.poll_seconds <= 1 and 1 <= args.phase_timeout_seconds <= 120):
        raise RuntimeError("Pause runner needs bounded polling and phase timeouts.")
    shell = pathlib.Path(args.powershell).resolve(strict=True)
    output.mkdir(parents=True, exist_ok=False)
    expected = {"session_id": args.session_id, "validation_run_id": args.run_id,
                "validation_process_id": args.validation_process_id,
                "startup_binding_hash": args.startup_binding_hash}
    report: Dict[str, Any] = {"schema": "oni.pause-roundtrip.v1", "expected": expected,
                            "started_at": local_now().isoformat(), "status": "running",
                            "stages": {}, "first_failure": None, "commands": [],
                            "actions_disabled": False, "retries": 0}
    phase = "F0"
    active_command: Optional[Dict[str, Any]] = None
    approval_process = None
    policy_control_authorized = False
    observation_count = 0
    seen_observations: Dict[tuple, Dict[str, Any]] = {}
    initial_command_ids = set()
    started = time.monotonic()

    def remaining() -> Optional[float]:
        if active_command is None:
            return None
        return (dt.datetime.fromisoformat(active_command["expires_at"]) - local_now()).total_seconds()

    def event(kind: str, **detail: Any) -> None:
        row = {"at": local_now().isoformat(), "elapsed_seconds": time.monotonic() - started,
               "phase": phase, "event": kind, "command_remaining_seconds": remaining(), **detail}
        with (output / "timeline.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")

    def observe() -> tuple[Dict[str, Any], Dict[str, Any]]:
        nonlocal observation_count
        read_deadline = time.monotonic() + 2
        saved_mixed = set()
        while True:
            try:
                bundle = pause_observation(root, expected)
                state, health, frame = bundle["state"], bundle["health"], bundle["frame"]
                mixed_generation = (
                    health.get("decision_frame_id") != frame.get("frame_id")
                    or state.get("snapshot_id") != safe_get(frame, "source.snapshot_id")
                    or health.get("latest_snapshot_id") != state.get("snapshot_id"))
                trusted_parts = (health.get("trust_ready") is True and health.get("trust_blockers") == []
                    and safe_get(state, "bridge.trust_ready") is True
                    and safe_get(state, "bridge.trust_blockers") == []
                    and safe_get(frame, "trust.status") == "trusted"
                    and all(state.get(key) == value for key, value in expected.items())
                    and health.get("current_session_id") == expected["session_id"]
                    and safe_get(frame, "source.session_id") == expected["session_id"])
                if bundle["blockers"] and mixed_generation and trusted_parts:
                    mixed_key = (state.get("snapshot_id"), frame.get("frame_id"), tuple(bundle["blockers"]))
                    if mixed_key not in saved_mixed:
                        saved_mixed.add(mixed_key)
                        observation_count += 1
                        saved = persist_pause_observation(bundle, output / "observations" / f"{observation_count:04d}")
                        event("publication_not_committed", blockers=bundle["blockers"], path=saved["path"])
                    if time.monotonic() >= read_deadline:
                        raise RuntimeError("observation_publication_timeout:" + ",".join(bundle["blockers"]))
                    time.sleep(args.poll_seconds)
                    continue
                break
            except Exception as exc:
                # Only a read interrupted by publication is retried, not a trust failure.
                transient = any(code in str(exc) for code in
                                ("bundle_generation_changed", "_changed_during_read",
                                 "_identity_changed_before_read"))
                event("observation_read_rejected", error=str(exc), transient=transient)
                if not transient or time.monotonic() >= read_deadline:
                    raise
                time.sleep(args.poll_seconds)
        key = (bundle["state"].get("snapshot_id"), bundle["frame"].get("frame_id"))
        saved = seen_observations.get(key)
        if saved is None or bundle["blockers"]:
            observation_count += 1
            saved = persist_pause_observation(bundle, output / "observations" / f"{observation_count:04d}")
            seen_observations[key] = saved
            event("observation_saved", **{k: saved[k] for k in
                                         ("snapshot_id", "frame_id", "paused", "blockers", "path")})
        if bundle["blockers"]:
            raise RuntimeError("trusted_observation_rejected:" + ",".join(bundle["blockers"]))
        return bundle, saved

    try:
        initial_command_ids = {path.stem for stage in COMMAND_DIRS
                               for path in (root / "commands" / stage).glob("*.json")}
        for stage in ("pending", "approving", "approved", "executing", "confirming"):
            if any((root / "commands" / stage).glob("*.json")):
                raise RuntimeError("active_queue_not_empty:" + stage)
        source_bundle, source_evidence = observe()
        if source_bundle["paused"] is not True:
            raise RuntimeError("F0_paused_is_not_true")
        report["stages"]["F0"] = source_evidence
        validate_policy_for_command(root, source_bundle["state"], "set_paused")
        policy_control_authorized = True
        event("F0_accepted", snapshot_id=source_evidence["snapshot_id"])

        for number, target in ((1, False), (2, True)):
            phase = f"C{number}.prepare"
            command_dir = output / f"C{number}"
            command_dir.mkdir()
            prepare_args = argparse.Namespace(
                bridge_dir=str(root), action="set_paused", paused=str(target).lower(),
                source_evidence_dir=command_dir / "source",
                expected_source_snapshot_id=source_evidence["snapshot_id"])
            stdout = io.StringIO()
            phase_tick = time.monotonic()
            event("prepare_started", source_snapshot_id=source_evidence["snapshot_id"])
            with contextlib.redirect_stdout(stdout):
                prepare_command(prepare_args)
            prepared = json.loads(stdout.getvalue())
            active_command, raw = _read_json_object(pathlib.Path(prepared["path"]), "pending_command")
            (command_dir / "pending-command.json").write_bytes(raw)
            report["commands"].append(active_command["id"])
            event("prepare_completed", command_id=active_command["id"],
                  duration_seconds=time.monotonic() - phase_tick)
            phase = f"C{number}.approve_and_confirm"
            argv = [str(shell), "-NoLogo", "-NoProfile", "-File",
                    str(PROJECT_ROOT / "scripts" / "approve-command.ps1"),
                    "-BridgeDir", str(root), "-CommandId", active_command["id"]]
            event("approval_started", argv=argv)
            phase_tick = time.monotonic()
            deadline = phase_tick + args.phase_timeout_seconds
            last_location = None
            first_candidate_rejection = None
            with (command_dir / "approval.stdout.txt").open("wb") as approval_out, \
                 (command_dir / "approval.stderr.txt").open("wb") as approval_err:
                approval_process = subprocess.Popen(
                    argv, stdout=approval_out, stderr=approval_err,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                event("approval_process", pid=approval_process.pid)
                approval_recorded = False
                while time.monotonic() < deadline:
                    bundle, evidence = observe()
                    try:
                        copies = pause_command_copies(root, active_command["id"])
                    except CommandCustodyTimeout:
                        event("queue_custody_busy_no_replay")
                        time.sleep(args.poll_seconds)
                        continue
                    if len(copies) > 1:
                        raise RuntimeError("command_has_multiple_inflight_copies")
                    if not copies:
                        event("command_temporarily_absent_no_replay")
                        time.sleep(args.poll_seconds)
                        continue
                    copy_item = copies[0]
                    command = copy_item["command"]
                    stage = copy_item["stage"]
                    location = (stage, command.get("status"))
                    if location != last_location:
                        last_location = location
                        (command_dir / f"queue-{stage}-{len(list(command_dir.glob('queue-*.json'))):02d}.json").write_bytes(copy_item["raw"])
                        event("command_stage", directory=stage, status=command.get("status"))
                    approval_exit = approval_process.poll()
                    if approval_exit is not None and not approval_recorded:
                        event("approval_completed", exit_code=approval_exit,
                              duration_seconds=time.monotonic() - phase_tick)
                        approval_recorded = True
                    if approval_exit not in (None, 0):
                        error_text = (command_dir / "approval.stderr.txt").read_text(encoding="utf-8-sig", errors="replace")
                        first_error = next((line.strip() for line in error_text.splitlines() if line.strip()), "no stderr")
                        raise RuntimeError("formal_approval_failed:" + first_error[:1000])
                    if stage in ("failed", "rejected", "quarantine"):
                        raise RuntimeError("command_terminal_rejection:" + str(command.get("reason") or command.get("status")))
                    if bundle["paused"] is target:
                        assessment = assess_command_confirmation(
                            root, command, archived_confirmation_records(root, command), directory=stage)
                        if not assessment["accepted"]:
                            if first_candidate_rejection is None:
                                first_candidate_rejection = {"snapshot_id": evidence["snapshot_id"],
                                    "observed_at": evidence["observed_at"], "directory": stage,
                                    "blockers": assessment["blockers"]}
                                write_json_atomic(command_dir / "first-candidate-rejection.json", first_candidate_rejection)
                                event("candidate_confirmation_rejected", **first_candidate_rejection)
                        elif approval_exit == 0:
                            witness_id = assessment["witness"]["snapshot_id"]
                            witness_evidence = next((value for value in seen_observations.values()
                                if value["snapshot_id"] == witness_id and value["paused"] is target
                                and not value["blockers"]), None)
                            if witness_evidence is None:
                                raise RuntimeError("trusted_witness_raw_bundle_not_captured")
                            field = read_contract_field(assessment["witness_state"], "game.paused", GAME_PAUSED_CONTRACT_ID)
                            if not field.usable or field.value is not target:
                                raise RuntimeError("formal_witness_paused_field_mismatch")
                            records = archived_confirmation_records(root, command)
                            write_json_atomic(command_dir / "confirmation.json", assessment)
                            write_json_atomic(command_dir / "confirmed-command.json", command)
                            with (command_dir / "execution-records.jsonl").open("x", encoding="utf-8") as stream:
                                for record in records:
                                    stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                            for index, publication in enumerate(assessment["certificate_publications"]):
                                path = root / publication["path"]
                                certificate_raw = path.read_bytes()
                                if "sha256:" + hashlib.sha256(certificate_raw).hexdigest() != publication["file_sha256"]:
                                    raise RuntimeError("confirmation_certificate_changed_during_capture")
                                (command_dir / f"certificate-{index}.json").write_bytes(certificate_raw)
                            report["stages"][f"C{number}"] = {"id": command["id"], "confirmed": True,
                                "ack_sequences": assessment["ack_sequences"], "witness_snapshot_id": witness_id}
                            report["stages"][f"F{number}"] = witness_evidence
                            event("snapshot_confirmation_accepted", command_id=command["id"],
                                  witness_snapshot_id=witness_id, paused=target)
                            source_bundle, source_evidence = bundle, evidence
                            # C2 is bound to the actual trusted F1, not an invented future recovery frame.
                            if number == 1 and evidence["snapshot_id"] != witness_id:
                                raise RuntimeError("F1_is_no_longer_current_for_C2;no_command_sent")
                            break
                    time.sleep(args.poll_seconds)
                else:
                    raise RuntimeError("phase_timeout:" + phase)
            approval_process = None
        phase = "complete"
        report["status"] = "paused_field_roundtrip_passed"
    except Exception as exc:
        report["status"] = "failed"
        failure_site = traceback.extract_tb(exc.__traceback__)[-1]
        report["first_failure"] = {"phase": phase, "error": str(exc),
                                   "at": local_now().isoformat(), "command_remaining_seconds": remaining(),
                                   "code_location": f"{failure_site.filename}:{failure_site.lineno}"}
        event("first_failure", error=str(exc))
    finally:
        # Preserve all command copies; never repair their status or replay an uncertain command.
        if policy_control_authorized:
            discovered = {path.stem for stage in COMMAND_DIRS
                          for path in (root / "commands" / stage).glob("*.json")} - initial_command_ids
            unreported = sorted(discovered - set(report["commands"]))
            if unreported:
                report["commands_discovered_after_prepare_failure"] = unreported
                report["commands"].extend(unreported)
                event("unreported_command_copies_found_no_replay", ids=unreported)
        for command_id in report["commands"]:
            try:
                copies = pause_command_copies(root, command_id)
                write_json_atomic(output / (command_id + "-final-copies.json"),
                                  {"copies": [{"directory": item["stage"], "command": item["command"]}
                                              for item in copies]})
            except Exception as exc:
                event("final_queue_read_failed", command_id=command_id, error=str(exc))
        if approval_process is not None:
            try:
                approval_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                event("approval_process_still_running_no_replay", pid=approval_process.pid)
            report["approval_process_at_stop"] = {"pid": approval_process.pid, "exit_code": approval_process.poll()}
        phase = "disable_actions"
        try:
            if policy_control_authorized:
                with (output / "disable-policy.stdout.txt").open("wb") as policy_out, \
                     (output / "disable-policy.stderr.txt").open("wb") as policy_err:
                    disabled = subprocess.run([str(shell), "-NoLogo", "-NoProfile", "-File",
                        str(PROJECT_ROOT / "scripts" / "write-action-policy.ps1"), "-BridgeDir", str(root),
                        "-DisableSuggestions", "-AllowedActions", "set_paused", "-MaxPendingCommands", "1"],
                        stdout=policy_out, stderr=policy_err, timeout=15,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                report["actions_disabled"] = disabled.returncode == 0 and load_policy(root).get("actions_enabled") is False
            else:
                report["actions_disabled"] = load_policy(root).get("actions_enabled") is False
                report["policy_untouched_before_identity_and_F0_validation"] = True
        except Exception as exc:
            report["disable_error"] = str(exc)
        if not report["actions_disabled"] and report["first_failure"] is None:
            report["first_failure"] = {"phase": phase, "error": "policy_disable_not_verified"}
        report["completed_at"] = local_now().isoformat()
        report["elapsed_seconds"] = time.monotonic() - started
        write_json_atomic(output / "pause-roundtrip.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "paused_field_roundtrip_passed" and report["actions_disabled"] else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bridge-dir")
    subparsers = parser.add_subparsers(dest="command", required=True)

    latest = subparsers.add_parser("latest", help="Show the current safe runtime identity and decision fields.")
    latest.set_defaults(func=show_latest)

    prepare = subparsers.add_parser("prepare", help="Create a V3 command in pending for explicit approval.")
    prepare.add_argument(
        "--action",
        required=True,
        choices=(
            "set_paused",
            "set_research",
            "set_duplicant_chore_priority",
            "mark_dig_cells",
            "cancel_dig_cells",
            "place_building_blueprint",
            "cancel_building_blueprint",
            "place_utility_blueprint_path",
            "cancel_utility_blueprint_path",
        ),
    )
    prepare.add_argument("--paused", choices=("auto", "true", "false"), default="auto")
    prepare.add_argument("--tech")
    prepare.add_argument("--duplicant-instance-id", type=int)
    prepare.add_argument("--chore-group", default="Dig")
    prepare.add_argument("--priority", type=int)
    prepare.add_argument("--cells", type=int, nargs="+")
    prepare.add_argument("--material-tags", nargs="+")
    prepare.add_argument("--path-id")
    prepare.add_argument("--source-evidence-dir", type=pathlib.Path,
                         help="Create an exact source bundle archive before starting the command TTL.")
    prepare.set_defaults(func=prepare_command)

    pause = subparsers.add_parser("pause-roundtrip", help="Run one bounded true-false-true pause validation using formal approval.")
    pause.add_argument("--evidence-dir", type=pathlib.Path, required=True)
    pause.add_argument("--session-id", required=True)
    pause.add_argument("--run-id", required=True)
    pause.add_argument("--validation-process-id", type=int, required=True)
    pause.add_argument("--startup-binding-hash", required=True)
    pause.add_argument("--powershell", type=pathlib.Path, required=True)
    pause.add_argument("--phase-timeout-seconds", type=float, default=45)
    pause.add_argument("--poll-seconds", type=float, default=0.1)
    pause.set_defaults(func=pause_roundtrip)

    invalid_priority = subparsers.add_parser(
        "prepare-invalid-priority-target",
        help="Create a deliberately mismatched duplicant identity command for rejection validation.",
    )
    invalid_priority.add_argument("--duplicant-instance-id", type=int, required=True)
    invalid_priority.add_argument("--chore-group", default="Dig")
    invalid_priority.add_argument("--priority", type=int, required=True)
    invalid_priority.set_defaults(func=prepare_invalid_priority_target)

    invalid_dig = subparsers.add_parser(
        "prepare-invalid-dig-target",
        help="Create a mark command with a deliberately mismatched cell fingerprint.",
    )
    invalid_dig.add_argument("--cell", type=int, required=True)
    invalid_dig.set_defaults(func=prepare_invalid_dig_target)

    invalid_blueprint = subparsers.add_parser(
        "prepare-invalid-blueprint-target",
        help="Create a Ladder placement command with a deliberately mismatched empty-cell fingerprint.",
    )
    invalid_blueprint.add_argument("--cell", type=int, required=True)
    invalid_blueprint.add_argument("--material-tags", nargs="+", required=True)
    invalid_blueprint.set_defaults(func=prepare_invalid_blueprint_target)

    stale = subparsers.add_parser("inject-stale", help="Inject an expired approved noop for quarantine validation.")
    stale.set_defaults(func=inject_stale)

    replay = subparsers.add_parser("inject-replay", help="Reinsert a verified command id to validate idempotency.")
    replay.add_argument("--command-id", required=True)
    replay.set_defaults(func=inject_replay)

    collect_parser = subparsers.add_parser("collect", help="Build runtime acceptance evidence and marker files.")
    collect_parser.add_argument(
        "--phase",
        choices=("observer", "batch-a", "batch-b", "batch-c", "final"),
        default="final",
    )
    collect_parser.add_argument("--minimum-duration-seconds", type=float, default=600.0)
    collect_parser.add_argument("--player-log", default=str(DEFAULT_PLAYER_LOG))
    collect_parser.add_argument("--save-lineage")
    collect_parser.add_argument(
        "--offline-after-shutdown",
        action="store_true",
        help="Replay final archived evidence after normal shutdown while still requiring the disk save tip.",
    )
    collect_parser.set_defaults(func=collect)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return int(args.func(args))
    except Exception as exc:
        print(json.dumps({"status": "error", "classification": "infra_fail", "error": str(exc)}, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
