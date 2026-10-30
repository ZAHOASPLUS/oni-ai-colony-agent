#!/usr/bin/env python3
"""Foreground controller for an externally admitted ONI test-clone run.

Usage: python scripts/oni_autonomous.py [status|start|pause|stop] --config FILE
The default command is read-only status. Start observes the already-prepared
live run in this process; it does not launch ONI, install Mods or load a save.
Pause and stop revoke new action issuance. Stop drains existing custody before
the controller exits; it never kills a game, Bridge or other process.

Config schema oni.autonomous-controller-config.v1 requires bridge_dir,
clone_manifest, clone_manifest_sha256, clone_save, clone_preview, run_id,
controller_id, session_id, world_id, and startup_expectations (all nine external
startup anchors). Paths must be absolute. Optional fields are goal,
poll_seconds, model_deadline_seconds, secrets_dir, round_command_limit
(a positive action bound, unlimited when absent) and checkpoints (boolean):
with checkpoints the run must load the newest verified link of the clone's
checkpoint chain, and a stop saves the next link once custody is quiescent
and the policy is sealed (scripts/stop_checkpoint.py). The clone manifest must
be the versioned named-test-clone manifest, whose exact save and PNG identities
are verified. Main owns preparing the game, policy and trusted Bridge outputs.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import math
import os
import pathlib
import signal
import stat
import sys
import time
from typing import Any, Dict, Iterator, Mapping, Optional, Sequence

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.action_policy import action_policy_hash, action_policy_is_valid
from agent.action_outcomes import command_intent_hash
from agent.canonical_hash import canonical_bytes, loads_strict
from agent.checkpoint_chain import CheckpointChainError, match_loader_anchors
from agent.clone_launch_guard import validate_clone_launch
from agent.command_custody import approval_recovery_blockers, lifecycle_custody, lifecycle_recovery_blockers, read_stable_bytes, validation_control_blockers
from agent.command_queue import COMMAND_DIRS, _is_unresolved_outcome_unknown, has_operator_resolution
from agent.field_contracts import NON_MUTATING_ACTIONS
from agent.model_loop import (
    AUTONOMOUS_CONTROL_NAME, AUTONOMOUS_CONTROL_SCHEMA, AUTONOMOUS_POLICY_SCHEMA,
    ModelLoop, _assert_committed_frame, load_autonomous_control,
    validate_autonomous_control, write_autonomous_document,
)
from agent.state_quality import (
    BRIDGE_PUBLISH_MARGIN_SECONDS,
    parse_snapshot_timestamp,
    safe_get,
    snapshot_trust_blockers,
)
from bridge.oni_bridge import (
    read_new_records,
    replay_initial_records,
    validate_startup_expectations,
    watch,
    write_outputs,
)
from scripts.stop_checkpoint import StopCheckpoint

CONFIG_SCHEMA = "oni.autonomous-controller-config.v1"
STATUS_SCHEMA = "oni.autonomous-controller-status.v1"
STATUS_NAME = "autonomous-controller.json"
LEASE_NAME = "autonomous-controller.lock"
POLICY_SEAL_SCHEMA = "oni.autonomous-policy-seal.v1"
IDENTITY_FIELDS = ("run_id", "controller_id", "session_id", "world_id", "save_id_hash", "startup_expectations")
ACTIVE_STAGES = frozenset({"pending", "approving", "approved", "executing", "confirming"})


class ControllerAlreadyRunningError(RuntimeError):
    """The OS lease is held; this is not by itself progress evidence."""


def _timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _object(path: pathlib.Path, *, required: bool = True) -> Optional[Dict[str, Any]]:
    if not path.exists() and not required:
        return None
    raw, _, _ = read_stable_bytes(path, label=path.name)
    value = loads_strict(raw.decode("utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a strict JSON object")
    return value


def _absolute_path(value: Any, label: str) -> pathlib.Path:
    if not isinstance(value, str) or not value or not pathlib.Path(value).is_absolute():
        raise ValueError(f"{label} must be an explicit absolute path")
    path = pathlib.Path(value)
    for component in (path, *path.parents):
        if component.exists():
            metadata = component.lstat()
            if stat.S_ISLNK(metadata.st_mode) or int(getattr(metadata, "st_file_attributes", 0)) & 0x400:
                raise ValueError(f"{label} contains a link or reparse point")
    return path


def load_config(path: pathlib.Path) -> Dict[str, Any]:
    config = _object(path)
    assert config is not None
    required = {
        "schema", "bridge_dir", "clone_manifest", "clone_manifest_sha256", "clone_save", "clone_preview",
        "run_id", "controller_id", "session_id", "world_id", "startup_expectations",
    }
    optional = {"goal", "poll_seconds", "model_deadline_seconds", "secrets_dir", "round_command_limit", "checkpoints"}
    if not required <= set(config) or set(config) - required - optional or config.get("schema") != CONFIG_SCHEMA:
        raise ValueError("controller config has an invalid exact schema/field set")
    for name in ("bridge_dir", "clone_manifest", "clone_save", "clone_preview"):
        config[name] = _absolute_path(config[name], name)
    if not config["bridge_dir"].is_dir():
        raise ValueError("configured Bridge directory must already exist")
    if "secrets_dir" in config:
        config["secrets_dir"] = _absolute_path(config["secrets_dir"], "secrets_dir")
    anchors = validate_startup_expectations(config["startup_expectations"])
    if anchors is None or anchors["validation_run_id"] != config["run_id"]:
        raise ValueError("controller run and external startup anchors must match")
    config["startup_expectations"] = anchors
    if config["clone_save"].suffix.lower() != ".sav" or config["clone_preview"].suffix.lower() != ".png" or config["clone_save"].stem != config["clone_preview"].stem:
        raise ValueError("configured clone needs an exact matching .sav/.png pair")
    if not config["clone_save"].stem.lower().startswith("oni-"):
        raise ValueError("test clone must use an explicit oni- name")
    manifest_raw, _, _ = read_stable_bytes(config["clone_manifest"], label="configured clone manifest")
    manifest_hash = "sha256:" + hashlib.sha256(manifest_raw).hexdigest()
    if manifest_hash != config["clone_manifest_sha256"]:
        raise ValueError("configured clone manifest hash does not match")
    manifest = loads_strict(manifest_raw.decode("utf-8-sig"))
    if not isinstance(manifest, dict) or manifest.get("run_id") != config["run_id"]:
        raise ValueError("configured clone manifest belongs to another run")
    # This reuse verifies clone content/header/path isolation only. Live
    # admission is independently checked below; no process/queue observation
    # or launch authority is inferred from the clone-only return value.
    clone = validate_clone_launch(config["clone_manifest"], config["clone_save"], config["clone_preview"], bridge_dir=config["bridge_dir"])
    # Named-clone manifests intentionally hash path.lower(); ProbeSession and
    # the runtime startup contract hash the full normalized path.upper().
    # Keep both contracts explicit instead of comparing different digests.
    loader_path_text = str(config["clone_save"].expanduser().resolve()).rstrip("\\/").upper()
    loader_path_id_upper = "sha256:" + hashlib.sha256(loader_path_text.encode("utf-8")).hexdigest()
    if "checkpoints" in config and type(config["checkpoints"]) is not bool:
        raise ValueError("checkpoints must be a boolean when supplied")
    config["checkpoints"] = bool(config.get("checkpoints", False))
    if config["checkpoints"]:
        # With checkpoints the run continues from the newest verified link of
        # the clone's chain (the clone itself while there is none); an older
        # link would silently drop progress, so only the newest is admitted.
        try:
            target = match_loader_anchors(
                config["clone_manifest"], config["clone_save"],
                loader_save_sha256=anchors["loader_save_sha256"],
                loader_path_id_upper_value=anchors["loader_path_id_upper"],
            )
        except CheckpointChainError as exc:
            raise ValueError(f"configured clone does not match the external loader anchors: {exc}") from exc
        config["active_save"] = target["save_path"]
        config["checkpoint_index"] = target["index"]
        config["save_id_hash"] = target["save_sha256"]
    else:
        if clone["clone_save_sha256"] != anchors["loader_save_sha256"] or loader_path_id_upper != anchors["loader_path_id_upper"]:
            raise ValueError("configured clone does not match the external loader anchors")
        config["active_save"] = config["clone_save"]
        config["checkpoint_index"] = 0
        config["save_id_hash"] = clone["clone_save_sha256"]
    config["clone_identity"] = {key: clone[key] for key in ("manifest_sha256", "clone_save_sha256", "clone_preview_sha256", "clone_save_path_id", "clone_preview_path_id")}
    config["clone_identity"]["clone_save_path_id_upper"] = loader_path_id_upper
    goal = config.get("goal", "Keep the colony alive with conservative, reversible actions.")
    if not isinstance(goal, str) or not goal.strip() or len(goal) > 2000:
        raise ValueError("controller goal must be a nonempty string of at most 2000 characters")
    config["goal"] = goal
    if "round_command_limit" in config and (type(config["round_command_limit"]) is not int or config["round_command_limit"] <= 0):
        raise ValueError("round_command_limit must be a positive integer when supplied")
    ledger = _object(config["bridge_dir"] / "model-loop-state.json", required=False)
    if ledger is not None:
        limits = ledger.get("autonomous_limits", {})
        if not isinstance(limits, dict):
            raise ValueError("persisted autonomous limits are malformed")
        stored = limits.get(str(config["run_id"]) + ":" + str(config["controller_id"]))
        if stored is not None:
            if type(stored) is not int or stored <= 0 or config.get("round_command_limit", stored) > stored:
                raise ValueError("persisted round command bound cannot be widened or reset")
            config["round_command_limit"] = min(config.get("round_command_limit", stored), stored)
    for name, default, minimum, maximum in (("poll_seconds", 2.0, 0.1, 30.0), ("model_deadline_seconds", 90.0, 5.0, 600.0)):
        value = config.get(name, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not minimum <= value <= maximum:
            raise ValueError(f"{name} must be between {minimum} and {maximum}")
        config[name] = float(value)
    return config


def _identity(config: Mapping[str, Any]) -> Dict[str, Any]:
    return {field: config[field] for field in IDENTITY_FIELDS}


def _new_control(config: Mapping[str, Any], desired: str) -> Dict[str, Any]:
    return {"schema": AUTONOMOUS_CONTROL_SCHEMA, **_identity(config), "desired_state": desired, "updated_at": _timestamp()}


def _policy_seal_path(config: Mapping[str, Any]) -> pathlib.Path:
    digest = hashlib.sha256(canonical_bytes(_identity(config))).hexdigest()
    return config["bridge_dir"] / f"autonomous-policy-final-{digest}.json"


def _validate_terminal_policy(
    config: Mapping[str, Any], policy: Mapping[str, Any], control: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Recognize only our exact sealed final deauthorization, never new authority."""

    if control is None or control.get("desired_state") != "stopped":
        raise ValueError("unbound policy has no stopped controller evidence")
    seal = _object(_policy_seal_path(config))
    assert seal is not None
    expected_keys = {"schema", "identity", "sealed_at", "policy", "policy_hash", "disabled_policy", "disabled_policy_hash", "quiescent"}
    if set(seal) != expected_keys or seal.get("schema") != POLICY_SEAL_SCHEMA or seal.get("identity") != _identity(config) or seal.get("quiescent") is not True:
        raise ValueError("final autonomous policy seal is invalid")
    if parse_snapshot_timestamp(seal.get("sealed_at")) is None:
        raise ValueError("final autonomous policy seal timestamp is invalid")
    original = seal.get("policy")
    if not isinstance(original, dict) or original.get("schema") != AUTONOMOUS_POLICY_SCHEMA or action_policy_hash(original) != seal.get("policy_hash"):
        raise ValueError("sealed autonomous policy identity/hash is invalid")
    validate_autonomous_control(control, policy=original)
    if any(control.get(field) != config[field] for field in IDENTITY_FIELDS):
        raise ValueError("stopped controller seal identity differs from config")
    if not action_policy_is_valid(policy) or policy.get("schema") != "oni.action-policy.v1" or policy.get("actions_enabled") is not False or policy.get("suggestions_enabled") is not False or policy.get("allowed_save_hashes") != []:
        raise ValueError("terminal policy is not the standard disabled/unbound V1")
    if canonical_bytes(dict(policy)) != canonical_bytes(seal.get("disabled_policy")) or action_policy_hash(policy) != seal.get("disabled_policy_hash"):
        raise ValueError("terminal policy differs from its sealed deauthorization")
    return dict(control)


def _seal_stopped_policy(config: Mapping[str, Any], policy: Mapping[str, Any]) -> None:
    """Called under custody only after all intents are terminal and reconciled."""

    if policy.get("schema") != AUTONOMOUS_POLICY_SCHEMA:
        return
    disabled = {
        "schema": "oni.action-policy.v1", "actions_enabled": False, "suggestions_enabled": False,
        "allowed_actions": list(policy["allowed_actions"]), "allowed_save_hashes": [],
        "command_ttl_seconds": policy["command_ttl_seconds"], "max_pending_commands": policy["max_pending_commands"],
    }
    if not action_policy_is_valid(disabled):
        raise ValueError("final disabled/unbound policy is invalid")
    seal = {
        "schema": POLICY_SEAL_SCHEMA, "identity": _identity(config), "sealed_at": _timestamp(),
        "policy": dict(policy), "policy_hash": action_policy_hash(policy),
        "disabled_policy": disabled, "disabled_policy_hash": action_policy_hash(disabled), "quiescent": True,
    }
    path = _policy_seal_path(config)
    existing = _object(path, required=False)
    if existing is not None:
        for key in set(seal) - {"sealed_at"}:
            if canonical_bytes(existing.get(key)) != canonical_bytes(seal[key]):
                raise ValueError("existing final policy seal conflicts with this stop")
    else:
        # Evidence precedes deauthorization. A crash cannot erase the final
        # V2 policy needed to interpret receipts or forge a new authority.
        write_autonomous_document(path, seal)
    write_autonomous_document(config["bridge_dir"] / "action-policy.json", disabled)


def _bound_inputs(
    config: Mapping[str, Any], *, ignore_foreign_latest: bool = False
) -> tuple[Dict[str, Any], Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    root = config["bridge_dir"]
    policy = _object(root / "action-policy.json")
    assert policy is not None
    latest = _object(root / "latest_state.json", required=False)
    if ignore_foreign_latest and latest is not None and latest.get("session_id") != config["session_id"]:
        # Before the start bootstrap, latest_state.json may still be the last
        # observation of an earlier run (another session).  It is neither this
        # run's evidence nor a reason to refuse; the bootstrap replaces it and
        # the full identity check then runs against this run's own snapshot.
        latest = None
    if policy.get("schema") == "oni.action-policy.v1":
        terminal_control = _object(root / AUTONOMOUS_CONTROL_NAME, required=False)
        terminal_control = _validate_terminal_policy(config, policy, terminal_control)
        # A finally deauthorized controller is historical evidence. Normal
        # environment restoration may replace live outputs; those outputs
        # cannot upgrade the sealed V1 into new autonomous authority.
        return policy, latest, terminal_control
    candidate = _new_control(config, "paused")
    validate_autonomous_control(candidate, policy=policy, latest_state=latest)
    control = load_autonomous_control(root, policy=policy, latest_state=latest) if (root / AUTONOMOUS_CONTROL_NAME).exists() else None
    if control is not None and any(control.get(field) != config[field] for field in IDENTITY_FIELDS):
        raise ValueError("existing controller identity/startup anchors do not match this config")
    return policy, latest, control


def _queue_view(root: pathlib.Path) -> Dict[str, Any]:
    """Read custody without layout creation, recovery or queue transitions."""

    counts = {stage: 0 for stage in COMMAND_DIRS}
    active = []
    blockers = []
    locations: Dict[str, list[str]] = {}
    commands: Dict[str, Dict[str, Any]] = {}
    for stage in COMMAND_DIRS:
        directory = root / "commands" / stage
        if not directory.exists():
            continue
        metadata = directory.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode) or int(getattr(metadata, "st_file_attributes", 0)) & 0x400:
            raise ValueError("command stage directory is unsafe")
        for path in sorted(directory.iterdir()):
            if path.suffix.lower() != ".json":
                blockers.append(f"unrecognized_custody_entry:{stage}:{path.name}")
                continue
            counts[stage] += 1
            try:
                command = _object(path)
            except Exception:  # noqa: BLE001 - damaged custody blocks safe stop
                blockers.append(f"unreadable_command:{stage}:{path.name}")
                continue
            assert command is not None
            command_id = command.get("id")
            if not isinstance(command_id, str) or path.name != f"{command_id}.json":
                blockers.append(f"command_identity_invalid:{stage}:{path.name}")
                continue
            locations.setdefault(command_id, []).append(stage)
            commands[command_id] = command
            if stage in ACTIVE_STAGES:
                active.append({"command_id": command_id, "stage": stage, "status": command.get("status"), "action": command.get("action")})
            if stage == "failed" and command.get("action") not in NON_MUTATING_ACTIONS:
                classifications = {command.get("classification"), safe_get(command, "result.classification")}
                if _is_unresolved_outcome_unknown(command) or not has_operator_resolution(command) and not classifications & {"behavior_fail", "rejected", "runtime_fail"}:
                    blockers.append(f"unresolved_command_outcome:{command_id}")
    for command_id, stages in locations.items():
        if len(stages) != 1:
            blockers.append(f"duplicate_command_custody:{command_id}")
    blockers.extend(f"lifecycle_recovery_unresolved:{name}" for name in lifecycle_recovery_blockers(root))
    blockers.extend(f"approval_recovery_unresolved:{name}" for name in approval_recovery_blockers(root))
    blockers.extend(f"validation_control_in_flight:{name}" for name in validation_control_blockers(root))
    ledger = _object(root / "model-loop-state.json", required=False)
    if ledger is not None:
        entries = ledger.get("autonomous_intents", [])
        if ledger.get("schema") != "oni.model-loop-state.v1" or not isinstance(entries, list):
            blockers.append("autonomous_intent_ledger_invalid")
        else:
            for entry in entries:
                if not isinstance(entry, dict) or not isinstance(entry.get("command_id"), str):
                    blockers.append("autonomous_intent_ledger_invalid")
                elif entry["command_id"] not in locations:
                    blockers.append(f"autonomous_reserved_outcome_unresolved:{entry['command_id']}")
                elif entry.get("intent_hash") != command_intent_hash(commands[entry["command_id"]]):
                    blockers.append(f"autonomous_intent_identity_conflict:{entry['command_id']}")
    return {"counts": counts, "active": active, "blockers": sorted(set(blockers)), "quiescent": not active and not blockers}


def controller_status(config: Mapping[str, Any]) -> Dict[str, Any]:
    policy, latest, control = _bound_inputs(config)
    root = config["bridge_dir"]
    queue = _queue_view(root)
    loop = _object(root / "model-loop.json", required=False) or {}
    if loop.get("authorization_mode") == "autonomous_controller":
        if loop.get("controller_identity") != _identity(config):
            raise ValueError("model loop status belongs to a different controller identity")
    else:
        loop = {}
    runtime = _object(root / STATUS_NAME, required=False) or {}
    desired = control["desired_state"] if control is not None else None
    if runtime and any(runtime.get(field) != config[field] for field in ("run_id", "controller_id", "session_id", "world_id", "save_id_hash")):
        raise ValueError("controller status belongs to a different identity")
    blockers = list(queue["blockers"])
    if desired == "running":
        blockers.extend(loop.get("blockers") or [])
    if desired == "running" and policy.get("actions_enabled") is not True:
        blockers.append("actions_disabled")
    terminal = policy.get("schema") == "oni.action-policy.v1" and desired == "stopped"
    if latest is None and not terminal:
        blockers.append("live_observation_missing")
    elif latest is not None and not terminal:
        # Same publishing Bridge, same cadence margin: an independent re-check
        # must not contradict the Bridge's own freshness verdict.
        blockers.extend(
            snapshot_trust_blockers(latest, bridge_margin_seconds=BRIDGE_PUBLISH_MARGIN_SECONDS)
        )
    if control is None:
        phase = "not_started"
    elif desired == "running":
        phase = "blocked" if blockers else str(loop.get("phase") or "awaiting_controller_observation")
    elif queue["blockers"]:
        phase = "stop_blocked" if desired == "stopped" else "pause_blocked"
    elif not queue["quiescent"]:
        phase = "draining_for_stop" if desired == "stopped" else "draining_for_pause"
    elif desired == "stopped":
        phase = "stopped" if runtime.get("phase") == "stopped" else "stop_requested_quiescent"
    else:
        phase = "paused_quiescent"
    outcomes = loop.get("recent_outcomes") if isinstance(loop.get("recent_outcomes"), list) else []
    return {
        "schema": STATUS_SCHEMA, "updated_at": _timestamp(),
        **{field: config[field] for field in ("run_id", "controller_id", "session_id", "world_id", "save_id_hash")},
        "desired_state": desired, "phase": phase, "current_objective": config["goal"],
        "last_action": outcomes[0] if outcomes else loop.get("last_event"),
        "result": outcomes[0].get("status") if outcomes and isinstance(outcomes[0], dict) else None,
        "blockers": sorted(set(blockers)), "queue": queue,
        "actions_enabled": policy.get("actions_enabled") is True, "policy_hash": action_policy_hash(policy),
        "action_budget": loop.get("budget") or {"round_command_limit": config.get("round_command_limit")},
        "clone_identity": config["clone_identity"], "controller_runtime": runtime,
        "scope": "controller only; game shutdown and environment restoration remain main-owned",
    }


def _request_state(config: Mapping[str, Any], desired: str, *, controller_pid: Optional[int] = None) -> Dict[str, Any]:
    root = config["bridge_dir"]
    with lifecycle_custody(root):
        policy, latest, existing = _bound_inputs(config)
        if policy.get("schema") == "oni.action-policy.v1" and desired != "stopped":
            raise ValueError("a finally stopped run needs fresh external admission; legacy policy cannot be upgraded")
        if desired == "running" and policy.get("actions_enabled") is not True and (existing is None or existing["desired_state"] == "running"):
            raise ValueError("first start requires an externally admitted enabled autonomous policy")
        control = _new_control(config, desired)
        owner_pid = (controller_pid if controller_pid is not None else os.getpid()) if desired == "running" else (existing or {}).get("controller_pid")
        if owner_pid is not None:
            control["controller_pid"] = owner_pid
        # Revocation is published first. An approved/executing/confirming
        # command retains its original policy hash until real confirmation.
        write_autonomous_document(root / AUTONOMOUS_CONTROL_NAME, control)
        if policy.get("schema") == "oni.action-policy.v1":
            _validate_terminal_policy(config, policy, _object(root / AUTONOMOUS_CONTROL_NAME))
        else:
            validate_autonomous_control(_object(root / AUTONOMOUS_CONTROL_NAME), policy=policy, latest_state=latest)
        queue = _queue_view(root)
        if desired == "stopped" and queue["quiescent"]:
            _seal_stopped_policy(config, policy)
        elif desired != "running" and queue["quiescent"] and policy.get("actions_enabled") is True:
            disabled = dict(policy)
            disabled["actions_enabled"] = False
            if not action_policy_is_valid(disabled):
                raise ValueError("quiescent revoked policy would be invalid")
            write_autonomous_document(root / "action-policy.json", disabled)
        elif desired == "running" and policy.get("actions_enabled") is not True:
            if not queue["quiescent"]:
                raise ValueError("cannot change policy authority while an intent is unresolved")
            resumed = dict(policy)
            resumed["actions_enabled"] = True
            if not action_policy_is_valid(resumed):
                raise ValueError("resumed policy would be invalid")
            write_autonomous_document(root / "action-policy.json", resumed)
    return controller_status(config)


@contextlib.contextmanager
def _controller_lease(root: pathlib.Path) -> Iterator[None]:
    """An OS-held lease prevents duplicate controllers without PID killing."""

    path = root / LEASE_NAME
    if path.exists():
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode) or metadata.st_nlink != 1 or int(getattr(metadata, "st_file_attributes", 0)) & 0x400:
            raise ValueError("controller lease is not a private regular file")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(str(path), flags, 0o600)
    locked = False
    try:
        opened = os.fstat(descriptor)
        named = path.lstat()
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
            raise ValueError("controller lease changed identity during open")
        if opened.st_size == 0:
            os.write(descriptor, b"ONI autonomous controller lease v1\n")
            os.fsync(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ControllerAlreadyRunningError("controller lease is already held; verify its progress before resuming") from exc
        locked = True
        os.lseek(descriptor, 0, os.SEEK_SET)
        if os.read(descriptor, 4096) != b"ONI autonomous controller lease v1\n":
            raise ValueError("controller lease header is not recognized")
        yield
    finally:
        if locked:
            os.lseek(descriptor, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _observation_is_untrusted(latest: Optional[Mapping[str, Any]]) -> bool:
    return (
        latest is None
        or bool(snapshot_trust_blockers(latest, bridge_margin_seconds=BRIDGE_PUBLISH_MARGIN_SECONDS))
        or safe_get(latest, "bridge.trust_ready") is not True
        or safe_get(latest, "bridge.replay_mode") is not False
    )


def _bootstrap_observation(config: Mapping[str, Any]) -> None:
    """Publish one trusted observation before the controller's start checks.

    The controller owns the Bridge for its run (see the ``watch`` call below), so
    on a fresh run nothing has published ``latest_state.json`` yet and the start
    precondition could never be met.  Replay the admitted log once and publish the
    outputs, then the normal checks run against a real observation.

    The replay plus the publish take about 27 s on a rotated 31 MB log, so the
    snapshot published by that first pass is already that old and the freshness
    check can refuse it (2026-10-26 round 28).  A second pass over only the
    records that arrived meanwhile costs milliseconds and publishes a snapshot
    that is current, which is what the check measures.

    A standalone bridge must NOT be used to prime this: two watches on one bridge
    directory race each other's log cursor and rotation (2026-10-25 rounds 5-15).
    """

    root = config["bridge_dir"]
    runtime, position = replay_initial_records(
        root / "state.jsonl",
        root,
        startup_expectations=config["startup_expectations"],
        require_startup_binding=True,
        strategy_goal=config["goal"],
        session_archive_prefix=True,
    )
    write_outputs(
        root,
        runtime,
        enable_llm=False,
        secrets_dir=config.get("secrets_dir"),
        goal=config["goal"],
    )
    # Catch up on whatever the Probe wrote during the pass above.
    records, _position, invalid, partial = read_new_records(root / "state.jsonl", position)
    if records or partial:
        runtime.process_many(records)
        runtime.invalid_json_count += invalid
        runtime.partial_bytes = partial
        write_outputs(
            root,
            runtime,
            enable_llm=False,
            secrets_dir=config.get("secrets_dir"),
            goal=config["goal"],
        )


def start_controller(config: Mapping[str, Any]) -> int:
    root = config["bridge_dir"]
    _, latest, _ = _bound_inputs(config, ignore_foreign_latest=True)
    if _observation_is_untrusted(latest):
        _bootstrap_observation(config)
        _, latest, _ = _bound_inputs(config)
    if _observation_is_untrusted(latest):
        raise ValueError("start requires a fresh trustworthy live observation from the externally admitted run")
    frame = _object(root / "decision-frame.json")
    assert frame is not None
    _assert_committed_frame(root, frame)
    if safe_get(frame, "trust.status") != "trusted":
        raise ValueError("start requires a trusted committed decision frame")
    with _controller_lease(root):
        loop = ModelLoop(autonomous=True, autonomous_identity=_identity(config), secrets_dir=config.get("secrets_dir"), goal=config["goal"], deadline_seconds=config["model_deadline_seconds"], round_command_limit=config.get("round_command_limit"))
        loop.bind_autonomous_budget(root)
        _request_state(config, "running")
        interrupted = False
        progress_signature: Optional[bytes] = None
        progress_at = 0.0

        def request_stop(_signum: int, _frame: Any) -> None:
            nonlocal interrupted
            interrupted = True

        previous_handlers = {signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)}
        for signum in previous_handlers:
            signal.signal(signum, request_stop)

        checkpoint = (
            StopCheckpoint(
                bridge_dir=root,
                clone_manifest=config["clone_manifest"],
                clone_save=config["clone_save"],
                run_id=config["run_id"],
                expected_mod_version=config["startup_expectations"]["mod_version"],
            )
            if config.get("checkpoints")
            else None
        )

        def stop_requested() -> bool:
            nonlocal interrupted, progress_signature, progress_at
            if interrupted:
                _request_state(config, "stopped")
                interrupted = False
            with lifecycle_custody(root):
                policy, latest_state, control = _bound_inputs(config)
                if control is None:
                    raise ValueError("running controller lost its authority control record")
                queue = _queue_view(root)
                requested = control["desired_state"]
                safe = queue["quiescent"]
                checkpointing = False
                if requested == "stopped" and safe:
                    _seal_stopped_policy(config, policy)
                    # The Mod saves only while the action policy is disabled and
                    # unbound, i.e. after the seal; the Bridge must keep running
                    # until a later snapshot confirms the save, so the stop
                    # completes only when the checkpoint has finished either way.
                    if checkpoint is not None:
                        checkpointing = checkpoint.step(latest_state) == "pending"
                elif requested != "running" and safe and policy.get("actions_enabled") is True:
                    disabled = dict(policy)
                    disabled["actions_enabled"] = False
                    if not action_policy_is_valid(disabled):
                        raise ValueError("quiescent revoked policy is invalid")
                    write_autonomous_document(root / "action-policy.json", disabled)
                status = controller_status(config)
                status.pop("controller_runtime", None)
                status["controller_pid"] = os.getpid()
                if requested == "stopped" and safe:
                    status["phase"] = "checkpointing" if checkpointing else "stopped"
                if checkpoint is not None:
                    status["checkpoint"] = dict(checkpoint.record)
                write_autonomous_document(root / STATUS_NAME, status)
                progress = {key: status[key] for key in ("phase", "desired_state", "current_objective", "last_action", "result", "blockers")}
                if checkpoint is not None:
                    progress["checkpoint"] = status["checkpoint"]
                signature = canonical_bytes(progress)
                current = time.monotonic()
                if signature != progress_signature or current - progress_at >= 30.0:
                    print(json.dumps(progress, ensure_ascii=False, allow_nan=False), flush=True)
                    progress_signature, progress_at = signature, current
                return requested == "stopped" and safe and not checkpointing

        try:
            print(json.dumps({"phase": "controller_started", "run_id": config["run_id"], "controller_id": config["controller_id"], "controller_pid": os.getpid(), "scope": "foreground watch of the existing admitted run"}, ensure_ascii=False), flush=True)
            # The controller IS the Bridge for its run: this watch owns the log
            # cursor, the archives and every published output.  A second watch on
            # the same bridge directory (a standalone bridge process started
            # alongside the controller) races it: each rotates the log and keeps
            # its own cursor, so records are lost, ``seq_gap`` false positives
            # appear and the two loops overwrite health.json and latest_state.json
            # (2026-10-25 rounds 5-15).  Run the controller alone.
            #
            # Rotation is safe again with a single owner, and it is required: with
            # it disabled the append-only log reached 998 MB, so every startup
            # replay had to read the whole file and the controller failed to start
            # (round 22).  ``watch`` continues from the pre-rotation cursor in the
            # moved file, so the records written across the rename are not lost.
            watch(
                root,
                config["poll_seconds"],
                startup_expectations=config["startup_expectations"],
                secrets_dir=config.get("secrets_dir"),
                goal=config["goal"],
                model_loop=loop,
                stop_requested=stop_requested,
                allow_log_rotation=bool(config.get("allow_log_rotation", True)),
            )
        except BaseException:
            # Failure revokes issuance, retains all queues and evidence, and
            # reports that a safe drain was not observed. Never claim stopped.
            try:
                _request_state(config, "paused")
                status = controller_status(config)
                status.pop("controller_runtime", None)
                status["phase"] = "controller_fault_drain_unverified"
                status["controller_pid"] = os.getpid()
                write_autonomous_document(root / STATUS_NAME, status)
            except Exception:
                pass
            raise
        finally:
            for signum, previous in previous_handlers.items():
                signal.signal(signum, previous)
    return 0


def request_existing_resume(config: Mapping[str, Any]) -> Dict[str, Any]:
    """A lease alone cannot justify 'started'; require a fresh owner heartbeat."""

    _, _, control = _bound_inputs(config)
    status = _object(config["bridge_dir"] / STATUS_NAME)
    assert status is not None
    if control is None or control.get("desired_state") == "stopped":
        raise ValueError("a stopping or unidentified controller cannot be resumed")
    if any(status.get(field) != config[field] for field in ("run_id", "controller_id", "session_id", "world_id", "save_id_hash")):
        raise ValueError("lease owner heartbeat identity is mismatched")
    if status.get("current_objective") != config["goal"]:
        raise ValueError("resuming cannot silently change the running controller objective")
    timestamp = parse_snapshot_timestamp(status.get("updated_at"))
    age = None if timestamp is None else (dt.datetime.now(dt.timezone.utc) - timestamp).total_seconds()
    if age is None or not 0 <= age <= 60 or status.get("phase") in {"stopped", "controller_fault_drain_unverified"}:
        raise ValueError("lease owner has no fresh verified progress heartbeat")
    owner_pid = control.get("controller_pid")
    if type(owner_pid) is not int or owner_pid <= 0 or status.get("controller_pid") != owner_pid:
        raise ValueError("lease owner process diagnostics are mismatched")
    if control["desired_state"] == "paused":
        result = _request_state(config, "running", controller_pid=owner_pid)
        result["phase"] = "resume_requested"
    else:
        result = controller_status(config)
        result["phase"] = "controller_progress_verified"
    return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", default="status", choices=("status", "start", "pause", "stop"))
    parser.add_argument("--config", type=pathlib.Path, required=True, help="explicit controller clone/run configuration")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "start":
            try:
                return start_controller(config)
            except ControllerAlreadyRunningError:
                result = request_existing_resume(config)
                print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
                return 0
        result = controller_status(config) if args.command == "status" else _request_state(config, "paused" if args.command == "pause" else "stopped")
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 2 if result["queue"]["blockers"] else 0
    except Exception as exc:  # noqa: BLE001 - fail closed without credential or raw response disclosure
        print(json.dumps({"schema": STATUS_SCHEMA, "phase": "refused", "error_type": type(exc).__name__, "detail": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
