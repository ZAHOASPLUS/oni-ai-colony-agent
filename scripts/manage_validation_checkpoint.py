#!/usr/bin/env python3
"""Run save/reload validation checkpoints without desktop input automation."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import secrets
import sys
import time
from typing import Any, Dict, Iterable, Optional


DEFAULT_BRIDGE = pathlib.Path.home() / "Documents" / "Klei" / "OxygenNotIncluded" / "ai-bridge"
HASH_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
OPERATIONS = ("save_checkpoint", "reload_checkpoint")
ACTIVE_ACTION_QUEUES = ("pending", "approving", "approved", "executing", "confirming")


class CheckpointError(RuntimeError):
    pass


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).astimezone()


def now_iso() -> str:
    return now().isoformat(timespec="seconds")


def normalized_path(path: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(os.path.abspath(os.path.normpath(str(path.expanduser()))))


def path_id(path: pathlib.Path) -> str:
    value = str(normalized_path(path)).rstrip("\\/").upper().encode("utf-8")
    return "sha256:" + hashlib.sha256(value).hexdigest()


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def load_json(path: pathlib.Path) -> Dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise CheckpointError(f"JSON root is not an object: {path}")
    return value


def write_json_atomic(path: pathlib.Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def build_policy(
    *,
    enabled: bool,
    test_run_id: Optional[str] = None,
    operation: Optional[str] = None,
    save_hash: Optional[str] = None,
    save_path_id: Optional[str] = None,
    target_save_path_id: Optional[str] = None,
    ttl_seconds: int = 60,
) -> Dict[str, Any]:
    current = now()
    return {
        "schema": "oni.validation-control-policy.v1",
        "controls_enabled": enabled,
        "test_run_id": test_run_id if enabled else None,
        "allowed_operations": [operation] if enabled and operation else [],
        "allowed_save_hashes": [save_hash] if enabled and save_hash else [],
        "allowed_save_path_ids": [save_path_id] if enabled and save_path_id else [],
        "allowed_target_save_path_ids": (
            [target_save_path_id] if enabled and target_save_path_id else []
        ),
        "generated_at": current.isoformat(timespec="seconds"),
        "expires_at": (
            (current + dt.timedelta(seconds=ttl_seconds)).isoformat(timespec="seconds")
            if enabled
            else None
        ),
    }


def build_control(
    *,
    control_id: str,
    operation: str,
    test_run_id: str,
    state: Dict[str, Any],
    save_path_id: str,
    expected_mod_version: str,
    target_save_path: Optional[pathlib.Path],
    ttl_seconds: int,
) -> Dict[str, Any]:
    created = now()
    args: Dict[str, Any] = {}
    if target_save_path is not None:
        args = {
            "target_save_path": str(normalized_path(target_save_path)),
            "target_save_path_id": path_id(target_save_path),
        }
    return {
        "schema": "oni.validation-control.v1",
        "id": control_id,
        "operation": operation,
        "status": "approved",
        "requires_approval": True,
        "test_run_id": test_run_id,
        "expected_mod_version": expected_mod_version,
        "source_session_id": state.get("session_id"),
        "source_snapshot_id": state.get("snapshot_id"),
        "save_id_hash": state.get("save_id_hash"),
        "save_path_id": save_path_id,
        "args": args,
        "created_at": created.isoformat(timespec="seconds"),
        "approved_at": created.isoformat(timespec="seconds"),
        "expires_at": (created + dt.timedelta(seconds=ttl_seconds)).isoformat(timespec="seconds"),
        "reason": "User-requested V2.7 same-save checkpoint validation.",
        "source": {"kind": "validation_checkpoint_cli"},
    }


def read_latest_state(bridge: pathlib.Path) -> Dict[str, Any]:
    path = bridge / "latest_state.json"
    if not path.exists():
        raise CheckpointError("Bridge latest_state.json is missing.")
    state = load_json(path)
    if state.get("schema") != "oni.state.v2" or state.get("event") != "state_snapshot":
        raise CheckpointError("Bridge latest_state.json is not an ONI state snapshot.")
    timestamp = state.get("ts")
    try:
        age = (now() - dt.datetime.fromisoformat(str(timestamp))).total_seconds()
    except (TypeError, ValueError):
        age = float("inf")
    if age > 20:
        raise CheckpointError(f"Latest state is stale ({age:.1f}s).")
    bridge_state = state.get("bridge") if isinstance(state.get("bridge"), dict) else {}
    if bridge_state.get("stale") is True:
        raise CheckpointError("Bridge marks the latest state stale.")
    return state


def validate_environment(
    bridge: pathlib.Path,
    state: Dict[str, Any],
    save_path: pathlib.Path,
    expected_mod_version: str,
) -> None:
    if state.get("mod_version") != expected_mod_version:
        raise CheckpointError("Loaded mod version does not match the requested checkpoint version.")
    if state.get("save_dirty") is not False:
        raise CheckpointError("Latest state is save_dirty.")
    save_hash = str(state.get("save_id_hash") or "")
    if not HASH_PATTERN.fullmatch(save_hash):
        raise CheckpointError("Latest state has no valid save identity.")
    if not save_path.is_file():
        raise CheckpointError(f"Save file is missing: {save_path}")
    if sha256_file(save_path) != save_hash:
        raise CheckpointError("Disk save hash does not match the active probe identity.")

    action_policy = load_json(bridge / "action-policy.json")
    if (
        action_policy.get("schema") != "oni.action-policy.v1"
        or action_policy.get("actions_enabled") is not False
        or action_policy.get("suggestions_enabled") is not False
        or action_policy.get("allowed_save_hashes") != []
    ):
        raise CheckpointError("Normal action policy must disable actions and suggestions and remain unbound.")
    for name in ACTIVE_ACTION_QUEUES:
        directory = bridge / "commands" / name
        if directory.exists() and any(directory.glob("*.json")):
            raise CheckpointError(
                "Normal action pending, approving, approved, executing, and confirming queues must be empty."
            )


def validate_target(save_path: pathlib.Path, target: Optional[pathlib.Path], operation: str) -> None:
    if target is None:
        return
    if operation != "save_checkpoint":
        raise CheckpointError("Only save_checkpoint accepts --target-save-path.")
    resolved = normalized_path(target)
    if resolved.suffix.lower() != ".sav":
        raise CheckpointError("Target save path must end in .sav.")
    if resolved.parent != normalized_path(save_path).parent:
        raise CheckpointError("Target save must be beside the active save.")
    if resolved == normalized_path(save_path):
        raise CheckpointError("Target save path must differ from the active save.")
    if resolved.exists():
        raise CheckpointError("Target save path already exists; save-as checkpoints never overwrite.")


def find_result(bridge: pathlib.Path, control_id: str) -> Optional[pathlib.Path]:
    root = bridge / "validation-controls"
    for name in ("done", "failed", "quarantine"):
        directory = root / name
        matches = sorted(directory.glob(control_id + "*.json")) if directory.exists() else []
        if matches:
            return matches[-1]
    return None


def wait_for_result(bridge: pathlib.Path, control_id: str, timeout_seconds: int) -> Dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        path = find_result(bridge, control_id)
        if path is not None:
            result = load_json(path)
            result["_result_path"] = str(path)
            return result
        time.sleep(0.25)
    raise CheckpointError(f"Timed out waiting for validation control: {control_id}")


def wait_for_state(
    bridge: pathlib.Path,
    *,
    expected_hash: str,
    expected_session: Optional[str],
    require_new_session: bool,
    minimum_seq: int,
    completed_at: str,
    expected_mod_version: str,
    timeout_seconds: int,
) -> Dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last: Dict[str, Any] = {}
    while time.monotonic() < deadline:
        try:
            last = load_json(bridge / "latest_state.json")
        except (OSError, json.JSONDecodeError, CheckpointError):
            time.sleep(0.25)
            continue
        session_matches = (
            str(last.get("session_id") or "") != str(expected_session or "")
            if require_new_session
            else str(last.get("session_id") or "") == str(expected_session or "")
        )
        try:
            state_at = dt.datetime.fromisoformat(str(last.get("ts") or ""))
            terminal_at = dt.datetime.fromisoformat(completed_at)
            snapshot_age = (now() - state_at).total_seconds()
        except (TypeError, ValueError):
            state_at = None
            terminal_at = None
            snapshot_age = float("inf")
        seq = last.get("seq")
        bridge_state = last.get("bridge") if isinstance(last.get("bridge"), dict) else {}
        trusted_later_state = (
            last.get("schema") == "oni.state.v2"
            and isinstance(last.get("snapshot_id"), str)
            and bool(last.get("snapshot_id"))
            and isinstance(seq, int)
            and not isinstance(seq, bool)
            and seq > 0
            and (require_new_session or seq > minimum_seq)
            and state_at is not None
            and terminal_at is not None
            and state_at >= terminal_at
            and snapshot_age <= 20
            and last.get("mod_version") == expected_mod_version
            and bridge_state.get("stale") is not True
        )
        if (
            last.get("event") == "state_snapshot"
            and last.get("save_id_hash") == expected_hash
            and last.get("save_dirty") is False
            and session_matches
            and trusted_later_state
        ):
            return last
        time.sleep(0.25)
    raise CheckpointError(
        "Timed out waiting for a matching post-control state snapshot; "
        f"last session={last.get('session_id')} hash={last.get('save_id_hash')}"
    )


def write_disabled_policy(bridge: pathlib.Path) -> None:
    write_json_atomic(bridge / "validation-control-policy.json", build_policy(enabled=False))


def execute(args: argparse.Namespace) -> int:
    if args.timeout_seconds <= 0:
        raise CheckpointError("timeout_seconds must be positive.")
    bridge = normalized_path(pathlib.Path(args.bridge_dir))
    save_path = normalized_path(pathlib.Path(args.save_path))
    target_path = normalized_path(pathlib.Path(args.target_save_path)) if args.target_save_path else None
    state = read_latest_state(bridge)
    validate_environment(bridge, state, save_path, args.expected_mod_version)
    validate_target(save_path, target_path, args.operation)

    test_run_id = args.test_run_id
    if not ID_PATTERN.fullmatch(test_run_id):
        raise CheckpointError("test_run_id is invalid.")
    control_id = args.control_id or (
        "ctl-" + args.operation.replace("_checkpoint", "") + "-" + now().strftime("%H%M%S") + "-" + secrets.token_hex(4)
    )
    if not ID_PATTERN.fullmatch(control_id):
        raise CheckpointError("control_id is invalid.")

    source_path_id = path_id(save_path)
    target_path_id = path_id(target_path) if target_path is not None else None
    policy = build_policy(
        enabled=True,
        test_run_id=test_run_id,
        operation=args.operation,
        save_hash=str(state["save_id_hash"]),
        save_path_id=source_path_id,
        target_save_path_id=target_path_id,
        ttl_seconds=args.ttl_seconds,
    )
    control = build_control(
        control_id=control_id,
        operation=args.operation,
        test_run_id=test_run_id,
        state=state,
        save_path_id=source_path_id,
        expected_mod_version=args.expected_mod_version,
        target_save_path=target_path,
        ttl_seconds=args.ttl_seconds,
    )

    policy_path = bridge / "validation-control-policy.json"
    request_path = bridge / "validation-controls" / "approved" / f"{control_id}.json"
    policy_written = False
    try:
        write_json_atomic(policy_path, policy)
        policy_written = True
        write_json_atomic(request_path, control)
        completed = wait_for_result(bridge, control_id, args.timeout_seconds)
        if completed.get("status") != "verified":
            raise CheckpointError(
                f"Validation control ended as {completed.get('status')}: "
                f"{completed.get('result', {}).get('message')}"
            )
        result = completed.get("result") if isinstance(completed.get("result"), dict) else {}
        terminal_completed_at = str(completed.get("completed_at") or "")
        try:
            dt.datetime.fromisoformat(terminal_completed_at)
        except ValueError as exc:
            raise CheckpointError("Validation control terminal record has no valid completed_at.") from exc
        active_hash = str(result.get("active_save_id_hash") or "")
        if not HASH_PATTERN.fullmatch(active_hash):
            raise CheckpointError("Verified control result has no active save hash.")
        result_save_path = target_path if target_path is not None else save_path
        if sha256_file(result_save_path) != active_hash:
            raise CheckpointError("Post-control disk save hash does not match the verified result.")
        post_state = wait_for_state(
            bridge,
            expected_hash=active_hash,
            expected_session=str(state.get("session_id") or ""),
            require_new_session=args.operation == "reload_checkpoint",
            minimum_seq=int(state.get("seq") or 0),
            completed_at=terminal_completed_at,
            expected_mod_version=args.expected_mod_version,
            timeout_seconds=args.timeout_seconds,
        )
        report = {
            "schema": "oni.validation-checkpoint-cli-result.v1",
            "status": "passed",
            "classification": "control_plane_verified",
            "acceptance_scope": "validation_save_reload_control_only",
            "game_action_confirmation": False,
            "operation": args.operation,
            "control_id": control_id,
            "test_run_id": test_run_id,
            "source": {
                "session_id": state.get("session_id"),
                "snapshot_id": state.get("snapshot_id"),
                "save_id_hash": state.get("save_id_hash"),
                "save_path_id": source_path_id,
            },
            "result": result,
            "post_state": {
                "session_id": post_state.get("session_id"),
                "snapshot_id": post_state.get("snapshot_id"),
                "save_id_hash": post_state.get("save_id_hash"),
                "mod_version": post_state.get("mod_version"),
                "seq": post_state.get("seq"),
                "ts": post_state.get("ts"),
            },
            "confirmation": {
                "status": "later_trusted_post_state_confirmed",
                "controller_terminal_status": completed.get("status"),
                "controller_completed_at": terminal_completed_at,
                "disk_hash_matches": True,
                "later_snapshot_matches": True,
                "note": "This is not a game-action commands/done confirmation.",
            },
            "control_record": completed.get("_result_path"),
            "completed_at": now_iso(),
        }
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    finally:
        if policy_written:
            write_disabled_policy(bridge)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bridge-dir", default=str(DEFAULT_BRIDGE))
    parser.add_argument("--operation", choices=OPERATIONS, required=True)
    parser.add_argument("--test-run-id", required=True)
    parser.add_argument("--save-path", required=True)
    parser.add_argument("--target-save-path")
    parser.add_argument("--expected-mod-version", default="0.7.0")
    parser.add_argument("--control-id")
    parser.add_argument("--ttl-seconds", type=int, choices=range(1, 121), default=60)
    parser.add_argument("--timeout-seconds", type=int, default=30)
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    try:
        return execute(build_parser().parse_args(argv))
    except (CheckpointError, OSError, json.JSONDecodeError) as exc:
        print(f"validation checkpoint failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
