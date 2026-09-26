#!/usr/bin/env python3
"""Create and verify an auditable one-save runtime-validation lineage."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import shutil
import sys
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.runtime_confirmation import (
    CONFIRMED_MUTATION_MODE,
    LEGACY_MUTATION_MODE,
    NON_MUTATING_VERIFIED_MODE,
    archived_confirmation_records,
    assess_command_confirmation,
    confirmation_summary,
)
DEFAULT_BRIDGE = pathlib.Path.home() / "Documents" / "Klei" / "OxygenNotIncluded" / "ai-bridge"
DEFAULT_SAVE_ROOTS = (
    pathlib.Path.home() / "Documents" / "Klei" / "OxygenNotIncluded" / "save_files",
    pathlib.Path.home() / "Documents" / "Klei" / "OxygenNotIncluded" / "cloud_save_files",
)
SCHEMA = "oni.test-save-lineage.v1"
REPORT_SCHEMA = "oni.test-save-lineage-verification.v1"
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
SAVE_HASH_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
COMMAND_DIRECTORIES = (
    "pending",
    "approving",
    "approved",
    "executing",
    "confirming",
    "done",
    "failed",
    "rejected",
    "quarantine",
)
ARTIFACT_COMPONENT_PATTERN = re.compile(r"[^A-Za-z0-9._-]+")


class LineageError(RuntimeError):
    pass


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def load_json(path: pathlib.Path) -> Dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise LineageError(f"JSON root is not an object: {path}")
    return value


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def hash_value(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value)).hexdigest()


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def normalized_path(path: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(os.path.abspath(os.path.normpath(str(path.expanduser()))))


def path_id(path: pathlib.Path) -> str:
    normalized = str(normalized_path(path)).rstrip("\\/").upper().encode("utf-8")
    return "sha256:" + hashlib.sha256(normalized).hexdigest()


def path_within(path: pathlib.Path, root: pathlib.Path) -> bool:
    try:
        normalized_path(path).relative_to(normalized_path(root))
        return True
    except ValueError:
        return False


def write_json_atomic(path: pathlib.Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def copy_file_atomic(source: pathlib.Path, target: pathlib.Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    shutil.copyfile(source, temporary)
    os.replace(temporary, target)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise LineageError(message)


def validate_save_hash(value: Any, label: str) -> str:
    text = str(value or "")
    require(bool(SAVE_HASH_PATTERN.fullmatch(text)), f"{label} is not a SHA-256 save identity")
    return text


# The Mod reports its validation-session autosave guard as a top-level
# snapshot object ``autosave`` (status, guard_active, cycle_interval,
# suppression_patch_installed, ...).  ONI's autosave rotation deletes the
# oldest same-colony saves in the active save folder (review P0-1), so a live
# run may only proceed on a snapshot whose guard is enforced with autosave
# disabled.  A missing object means a Mod build without the guard: refused.
def autosave_guard_blockers(state: Dict[str, Any]) -> List[str]:
    guard = state.get("autosave")
    if not isinstance(guard, dict):
        return ["autosave_guard_missing"]
    blockers: List[str] = []
    if guard.get("status") != "enforced":
        blockers.append("autosave_guard_not_enforced")
    if guard.get("guard_active") is not True:
        blockers.append("autosave_guard_inactive")
    interval = guard.get("cycle_interval")
    if type(interval) is not int or interval != 0:
        blockers.append("autosave_cycle_interval_not_zero")
    if guard.get("suppression_patch_installed") is not True:
        blockers.append("autosave_suppression_patch_missing")
    return blockers


def validate_snapshot(state: Dict[str, Any], expected_mod_version: str) -> Dict[str, Any]:
    require(state.get("event") == "state_snapshot", "latest_state.json is not a state snapshot")
    guard_blockers = autosave_guard_blockers(state)
    require(not guard_blockers, "latest state autosave guard is not enforced: " + ", ".join(guard_blockers))
    session_id = str(state.get("session_id") or "")
    snapshot_id = str(state.get("snapshot_id") or "")
    require(bool(session_id), "latest state has no session_id")
    require(bool(snapshot_id), "latest state has no snapshot_id")
    require(state.get("save_dirty") is False, "latest state is save_dirty")
    require(state.get("mod_version") == expected_mod_version, "latest state uses the wrong mod version")
    bridge = state.get("bridge") if isinstance(state.get("bridge"), dict) else {}
    require(bridge.get("stale") is not True, "latest state is stale")
    save_hash = validate_save_hash(state.get("save_id_hash"), "latest save_id_hash")
    seq = state.get("seq")
    require(isinstance(seq, int) and not isinstance(seq, bool) and seq > 0, "latest state has an invalid seq")
    world_id = state.get("world_id")
    require(isinstance(world_id, int) and not isinstance(world_id, bool), "latest state has an invalid world_id")
    return {
        "session_id": session_id,
        "snapshot_id": snapshot_id,
        "seq": seq,
        "save_id_hash": save_hash,
        "world_id": world_id,
        "mod_version": expected_mod_version,
        "snapshot_ts": state.get("ts"),
    }


def state_log_paths(bridge_dir: pathlib.Path) -> List[pathlib.Path]:
    rotations: List[Tuple[int, pathlib.Path]] = []
    for candidate in bridge_dir.glob("state.jsonl.*"):
        suffix = candidate.name.removeprefix("state.jsonl.")
        if suffix.isdigit():
            rotations.append((int(suffix), candidate))
    return [path for _, path in sorted(rotations, reverse=True)] + [bridge_dir / "state.jsonl"]


def read_runtime_records(bridge_dir: pathlib.Path) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    ordinal = 0
    for path in state_log_paths(bridge_dir):
        if not path.exists():
            continue
        with path.open("rb") as handle:
            for raw in handle:
                if not raw.endswith(b"\n"):
                    continue
                try:
                    value = json.loads(raw.decode("utf-8-sig"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not isinstance(value, dict):
                    continue
                value = dict(value)
                value["_lineage_ordinal"] = ordinal
                records.append(value)
                ordinal += 1
    return records


def find_snapshot_record(records: Sequence[Dict[str, Any]], snapshot_id: str) -> Dict[str, Any]:
    matches = [record for record in records if record.get("snapshot_id") == snapshot_id]
    require(len(matches) == 1, f"snapshot evidence is missing or ambiguous: {snapshot_id}")
    return matches[0]


def save_event_matches(
    record: Dict[str, Any],
    *,
    session_id: str,
    minimum_seq: int,
    expected_path_id: str,
    expected_hash: str,
) -> bool:
    return (
        record.get("event") == "save_write_completed"
        and record.get("session_id") == session_id
        and isinstance(record.get("seq"), int)
        and record.get("seq") > minimum_seq
        and record.get("success") is True
        and record.get("is_auto_save") is False
        and record.get("update_save_pointer") is True
        and record.get("file_rewrite_observed") is True
        and record.get("save_file_readable") is True
        and record.get("requested_save_path_id") == expected_path_id
        and record.get("result_save_path_id") == expected_path_id
        and record.get("active_save_path_id") == expected_path_id
        and record.get("written_save_id_hash") == expected_hash
        and record.get("active_save_id_hash") == expected_hash
        and record.get("save_id_hash") == expected_hash
        and record.get("save_dirty") is False
    )


def find_save_event(
    records: Sequence[Dict[str, Any]],
    *,
    session_id: str,
    minimum_seq: int,
    expected_path_id: str,
    expected_hash: str,
) -> Dict[str, Any]:
    matches = [
        record
        for record in records
        if save_event_matches(
            record,
            session_id=session_id,
            minimum_seq=minimum_seq,
            expected_path_id=expected_path_id,
            expected_hash=expected_hash,
        )
    ]
    require(bool(matches), "no matching successful explicit save completion event was found")
    return matches[-1]


def event_evidence(record: Dict[str, Any]) -> Dict[str, Any]:
    keys = (
        "session_id",
        "seq",
        "ts",
        "success",
        "is_auto_save",
        "update_save_pointer",
        "file_rewrite_observed",
        "save_file_readable",
        "requested_save_path_id",
        "result_save_path_id",
        "active_save_path_id",
        "before_save_id_hash",
        "written_save_id_hash",
        "active_save_id_hash",
        "save_id_hash",
        "save_dirty",
    )
    return {key: record.get(key) for key in keys}


def checkpoint_hash(record: Dict[str, Any]) -> str:
    payload = {key: value for key, value in record.items() if key != "checkpoint_hash"}
    return hash_value(payload)


def command_evidence(
    bridge_dir: pathlib.Path,
    command_id: str,
    outcome: str,
    before: Dict[str, Any],
    records: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    matches: List[Tuple[str, pathlib.Path, Dict[str, Any]]] = []
    for directory in COMMAND_DIRECTORIES:
        root = bridge_dir / "commands" / directory
        if not root.exists():
            continue
        for path in root.glob("*.json"):
            try:
                command = load_json(path)
            except Exception:
                continue
            if command.get("id") == command_id:
                matches.append((directory, path, command))
    require(len(matches) == 1, f"command evidence is missing or ambiguous: {command_id}")
    directory, path, command = matches[0]
    require(command.get("session_id") == before["session_id"], "command session does not match checkpoint before state")
    require(command.get("save_id_hash") == before["save_id_hash"], "command save hash does not match checkpoint before state")
    require(command.get("world_id") == before["world_id"], "command world does not match checkpoint before state")
    classification = command.get("classification")
    confirmation: Optional[Dict[str, Any]] = None
    if outcome in {"confirmed", "verified"}:
        assessment = assess_command_confirmation(
            bridge_dir,
            command,
            archived_confirmation_records(bridge_dir, command) or records,
            directory=directory,
        )
        confirmation = confirmation_summary(assessment)
        # Root publication rotates with later sessions.  The session-scoped
        # certificate is the durable lineage artifact.
        confirmation["certificate_publications"] = [
            item
            for item in confirmation.get("certificate_publications") or []
            if str(item.get("path") or "").startswith("sessions/")
        ]
        if outcome == "confirmed":
            require(
                assessment.get("accepted") is True
                and assessment.get("mode") == CONFIRMED_MUTATION_MODE,
                "mutating command lacks done/confirmed snapshot attestation: "
                + ",".join(str(item) for item in assessment.get("blockers") or []),
            )
        else:
            require(
                assessment.get("accepted") is True
                and assessment.get("mode") == NON_MUTATING_VERIFIED_MODE,
                "verified is current only for noop/focus_note; legacy mutating verified evidence is read-only",
            )
    elif outcome == "behavior_fail":
        valid_terminal = (directory == "failed" and command.get("status") == "failed") or (
            directory == "quarantine" and command.get("status") == "quarantined"
        )
        require(valid_terminal and classification == "behavior_fail", "command is not a terminal behavior_fail")
    elif outcome == "quarantined":
        require(directory == "quarantine" and command.get("status") == "quarantined", "command is not quarantined")
    return {
        "id": command_id,
        "action": command.get("action"),
        "directory": directory,
        "status": command.get("status"),
        "classification": classification,
        "ack_status": command.get("ack_status"),
        "confirmation": confirmation,
        "file": str(path.relative_to(bridge_dir)).replace("\\", "/"),
        "file_sha256": sha256_file(path),
    }


def artifact_component(value: str, fallback: str) -> str:
    component = ARTIFACT_COMPONENT_PATTERN.sub("_", value).strip("._-")
    return (component or fallback)[:96]


def artifact_evidence(
    paths: Iterable[str],
    *,
    manifest_path: pathlib.Path,
    checkpoint_index: int,
    step_id: str,
) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    step_component = artifact_component(step_id, "checkpoint")
    artifact_dir = manifest_path.parent / "artifacts" / f"{checkpoint_index:02d}-{step_component}"
    for value in paths:
        path = normalized_path(pathlib.Path(value))
        require(path.is_file(), f"evidence artifact does not exist: {path}")
        digest = sha256_file(path)
        name_component = artifact_component(path.name, "artifact")
        frozen_path = artifact_dir / f"{digest.removeprefix('sha256:')}-{name_component}"
        if frozen_path.exists():
            require(sha256_file(frozen_path) == digest, f"frozen artifact hash conflict: {frozen_path}")
        else:
            copy_file_atomic(path, frozen_path)
            require(sha256_file(frozen_path) == digest, f"frozen artifact copy changed bytes: {path}")
        result.append(
            {
                "path": str(frozen_path),
                "sha256": digest,
                "length": frozen_path.stat().st_size,
                "source_path_id": path_id(path),
            }
        )
    return result


def rehash_checkpoints(manifest: Dict[str, Any]) -> Dict[str, str]:
    initial = manifest.get("initial") if isinstance(manifest.get("initial"), dict) else {}
    previous_hash = str(initial.get("checkpoint_hash") or "")
    require(bool(previous_hash), "lineage activation hash is missing")
    old_to_new: Dict[str, str] = {}
    checkpoints = manifest.get("checkpoints") if isinstance(manifest.get("checkpoints"), list) else []
    for item in checkpoints:
        require(isinstance(item, dict), "lineage checkpoint is not an object")
        old_hash = str(item.get("checkpoint_hash") or "")
        item["previous_checkpoint_hash"] = previous_hash
        item["checkpoint_hash"] = checkpoint_hash(item)
        old_to_new[old_hash] = item["checkpoint_hash"]
        previous_hash = item["checkpoint_hash"]
    manifest["latest_checkpoint_hash"] = previous_hash
    return old_to_new


def freeze_existing_artifacts(args: argparse.Namespace) -> Dict[str, Any]:
    manifest_path = normalized_path(args.manifest)
    manifest = load_json(manifest_path)
    require(manifest.get("schema") == SCHEMA and manifest.get("status") == "active", "lineage is not active")
    checkpoints = manifest.get("checkpoints") if isinstance(manifest.get("checkpoints"), list) else []
    matches = [item for item in checkpoints if isinstance(item, dict) and item.get("step_id") == args.step_id]
    require(len(matches) == 1, "checkpoint step is missing or ambiguous")
    checkpoint_record = matches[0]
    existing = [item for item in checkpoint_record.get("artifacts", []) if isinstance(item, dict)]
    recovered_paths = [normalized_path(pathlib.Path(value)) for value in args.recovered_artifact]
    require(existing and len(existing) == len(recovered_paths), "recovered artifact count does not match checkpoint")

    recovered_by_hash: Dict[str, List[pathlib.Path]] = {}
    for path in recovered_paths:
        require(path.is_file(), f"recovered artifact does not exist: {path}")
        recovered_by_hash.setdefault(sha256_file(path), []).append(path)
    ordered_recovered: List[str] = []
    for artifact in existing:
        digest = str(artifact.get("sha256") or "")
        candidates = recovered_by_hash.get(digest) or []
        require(candidates, f"no recovered artifact matches stored hash: {digest}")
        path = candidates.pop(0)
        require(path.stat().st_size == artifact.get("length"), f"recovered artifact length mismatch: {path}")
        ordered_recovered.append(str(path))

    old_latest_hash = str(manifest.get("latest_checkpoint_hash") or "")
    old_checkpoint_hash = str(checkpoint_record.get("checkpoint_hash") or "")
    old_artifacts = [dict(item) for item in existing]
    checkpoint_record["artifacts"] = artifact_evidence(
        ordered_recovered,
        manifest_path=manifest_path,
        checkpoint_index=int(checkpoint_record.get("index") or 0),
        step_id=str(checkpoint_record.get("step_id") or "checkpoint"),
    )
    hash_migration = rehash_checkpoints(manifest)
    migration = {
        "schema": "oni.test-save-lineage-migration.v1",
        "kind": "freeze_mutable_artifacts",
        "migrated_at": now_iso(),
        "reason": args.reason,
        "step_id": args.step_id,
        "old_checkpoint_hash": old_checkpoint_hash,
        "new_checkpoint_hash": hash_migration.get(old_checkpoint_hash),
        "old_latest_checkpoint_hash": old_latest_hash,
        "new_latest_checkpoint_hash": manifest.get("latest_checkpoint_hash"),
        "artifacts": [
            {
                "old_path_id": path_id(pathlib.Path(str(old.get("path") or ""))),
                "sha256": old.get("sha256"),
                "length": old.get("length"),
            }
            for old in old_artifacts
        ],
    }
    migrations = manifest.get("migrations") if isinstance(manifest.get("migrations"), list) else []
    migrations.append(migration)
    manifest["migrations"] = migrations
    write_json_atomic(manifest_path, manifest)
    return {
        "status": "frozen",
        "manifest": str(manifest_path),
        "step_id": args.step_id,
        "artifact_count": len(checkpoint_record["artifacts"]),
        "latest_checkpoint_hash": manifest.get("latest_checkpoint_hash"),
    }


def disabled_policy_evidence(bridge_dir: pathlib.Path) -> Dict[str, Any]:
    policy = load_json(bridge_dir / "action-policy.json")
    pending_count = len(list((bridge_dir / "commands" / "pending").glob("*.json")))
    approving_count = len(list((bridge_dir / "commands" / "approving").glob("*.json")))
    approved_count = len(list((bridge_dir / "commands" / "approved").glob("*.json")))
    executing_count = len(list((bridge_dir / "commands" / "executing").glob("*.json")))
    confirming_count = len(list((bridge_dir / "commands" / "confirming").glob("*.json")))
    commands_root = bridge_dir / "commands"
    temporary_count = (
        sum(1 for path in commands_root.rglob("*") if path.name.casefold().endswith(".tmp"))
        if commands_root.is_dir()
        else 0
    )
    require(policy.get("schema") == "oni.action-policy.v1", "action policy schema is invalid")
    require(policy.get("actions_enabled") is False, "actions must be disabled before save-lineage transitions")
    require(policy.get("allowed_save_hashes") == [], "disabled action policy must revoke every save hash")
    require(
        pending_count == 0
        and approving_count == 0
        and approved_count == 0
        and executing_count == 0
        and confirming_count == 0
        and temporary_count == 0,
        "pending, approving, approved, executing, confirming, or temporary command files remain before save-lineage transition",
    )
    return {
        "schema": policy.get("schema"),
        "actions_enabled": False,
        "allowed_save_hash_count": 0,
        "pending_count": pending_count,
        "approving_count": approving_count,
        "approved_count": approved_count,
        "executing_count": executing_count,
        "confirming_count": confirming_count,
        "temporary_count": temporary_count,
    }


def reserve(args: argparse.Namespace) -> Dict[str, Any]:
    require(bool(RUN_ID_PATTERN.fullmatch(args.run_id)), "run id must use 1-80 ASCII letters, digits, dot, dash, or underscore")
    bridge_dir = normalized_path(args.bridge_dir)
    save_path = normalized_path(args.save_path)
    require(save_path.suffix.casefold() == ".sav", "reserved save path must end in .sav")
    if not args.allow_external_save:
        require(any(path_within(save_path, root) for root in DEFAULT_SAVE_ROOTS), "save path is outside ONI save roots")
    require(not save_path.exists(), "reserved save path already exists; choose a new save name")
    manifest_path = bridge_dir / "evidence" / "save-lineage" / args.run_id / "manifest.json"
    require(not manifest_path.exists(), "lineage manifest already exists")
    source_state = validate_snapshot(load_json(bridge_dir / "latest_state.json"), args.mod_version)
    reservation_policy = disabled_policy_evidence(bridge_dir)
    manifest = {
        "schema": SCHEMA,
        "test_run_id": args.run_id,
        "status": "reserved",
        "source_kind": args.source_kind,
        "expected_mod_version": args.mod_version,
        "reserved_at": now_iso(),
        "save_path": str(save_path),
        "save_path_id": path_id(save_path),
        "save_scope": "external_test" if args.allow_external_save else "oni_save_root",
        "save_absent_at_reservation": True,
        "reservation_source": source_state,
        "reservation_policy": reservation_policy,
        "initial": None,
        "checkpoints": [],
        "latest": None,
        "latest_checkpoint_hash": None,
    }
    write_json_atomic(manifest_path, manifest)
    return {"status": "reserved", "manifest": str(manifest_path), "test_run_id": args.run_id, "save_path_id": manifest["save_path_id"]}


def activate(args: argparse.Namespace) -> Dict[str, Any]:
    manifest_path = normalized_path(args.manifest)
    manifest = load_json(manifest_path)
    require(manifest.get("schema") == SCHEMA and manifest.get("status") == "reserved", "lineage is not reserved")
    bridge_dir = normalized_path(args.bridge_dir)
    save_path = normalized_path(pathlib.Path(str(manifest.get("save_path") or "")))
    require(save_path.is_file(), "reserved save was not created")
    written_hash = sha256_file(save_path)
    current = validate_snapshot(load_json(bridge_dir / "latest_state.json"), str(manifest["expected_mod_version"]))
    policy_gate = disabled_policy_evidence(bridge_dir)
    source = manifest.get("reservation_source") if isinstance(manifest.get("reservation_source"), dict) else {}
    require(current["session_id"] != source.get("session_id"), "new save has not been reloaded into a new session")
    require(current["save_id_hash"] == written_hash, "reloaded state does not match the new save file hash")
    require(written_hash != source.get("save_id_hash"), "activation save did not rotate the save hash")
    records = read_runtime_records(bridge_dir)
    save_event = find_save_event(
        records,
        session_id=str(source.get("session_id") or ""),
        minimum_seq=int(source.get("seq") or 0),
        expected_path_id=str(manifest["save_path_id"]),
        expected_hash=written_hash,
    )
    require(save_event.get("before_save_id_hash") == source.get("save_id_hash"), "activation save did not start from the reserved source identity")
    reload_record = find_snapshot_record(records, current["snapshot_id"])
    require(reload_record["_lineage_ordinal"] > save_event["_lineage_ordinal"], "reload snapshot precedes save completion")
    initial = {
        "kind": "activation",
        "activated_at": now_iso(),
        "previous_checkpoint_hash": None,
        "source": source,
        "save": event_evidence(save_event),
        "reload": current,
        "policy_gate": policy_gate,
    }
    initial["checkpoint_hash"] = checkpoint_hash(initial)
    manifest["status"] = "active"
    manifest["initial"] = initial
    manifest["latest"] = current
    manifest["latest_checkpoint_hash"] = initial["checkpoint_hash"]
    write_json_atomic(manifest_path, manifest)
    return {"status": "active", "manifest": str(manifest_path), "initial_save_id_hash": written_hash, "session_id": current["session_id"]}


def checkpoint(args: argparse.Namespace) -> Dict[str, Any]:
    manifest_path = normalized_path(args.manifest)
    manifest = load_json(manifest_path)
    require(manifest.get("schema") == SCHEMA and manifest.get("status") == "active", "lineage is not active")
    checkpoints = manifest.get("checkpoints") if isinstance(manifest.get("checkpoints"), list) else []
    require(args.step_id not in {item.get("step_id") for item in checkpoints if isinstance(item, dict)}, "step id already exists")
    bridge_dir = normalized_path(args.bridge_dir)
    save_path = normalized_path(pathlib.Path(str(manifest.get("save_path") or "")))
    require(save_path.is_file(), "lineage save file is missing")
    after_hash = sha256_file(save_path)
    before = manifest.get("latest") if isinstance(manifest.get("latest"), dict) else {}
    validate_save_hash(before.get("save_id_hash"), "checkpoint before save_id_hash")
    current = validate_snapshot(load_json(bridge_dir / "latest_state.json"), str(manifest["expected_mod_version"]))
    policy_gate = disabled_policy_evidence(bridge_dir)
    require(current["session_id"] != before.get("session_id"), "checkpoint save has not been reloaded into a new session")
    require(current["save_id_hash"] == after_hash, "reloaded checkpoint does not match the save file hash")
    require(after_hash != before.get("save_id_hash"), "checkpoint save did not rotate the save hash")
    require(bool(args.evidence), "checkpoint requires at least one frozen evidence artifact")

    records = read_runtime_records(bridge_dir)
    save_event = find_save_event(
        records,
        session_id=str(before.get("session_id") or ""),
        minimum_seq=int(before.get("seq") or 0),
        expected_path_id=str(manifest["save_path_id"]),
        expected_hash=after_hash,
    )
    require(save_event.get("before_save_id_hash") == before.get("save_id_hash"), "save event before hash does not continue the lineage")
    reload_record = find_snapshot_record(records, current["snapshot_id"])
    require(reload_record["_lineage_ordinal"] > save_event["_lineage_ordinal"], "reload snapshot precedes checkpoint save completion")

    command_required = args.outcome in {"confirmed", "verified", "behavior_fail", "quarantined"}
    require(bool(args.command_id) == command_required, "this outcome requires exactly one command id" if command_required else "this outcome must not bind a command id")
    command = (
        command_evidence(bridge_dir, args.command_id, args.outcome, before, records)
        if args.command_id
        else None
    )
    record = {
        "index": len(checkpoints) + 1,
        "step_id": args.step_id,
        "scenario": args.scenario,
        "outcome": args.outcome,
        "expected_mutation": args.expected_mutation,
        "recorded_at": now_iso(),
        "previous_checkpoint_hash": manifest.get("latest_checkpoint_hash"),
        "before": before,
        "command": command,
        "artifacts": artifact_evidence(
            args.evidence,
            manifest_path=manifest_path,
            checkpoint_index=len(checkpoints) + 1,
            step_id=args.step_id,
        ),
        "save": event_evidence(save_event),
        "reload": current,
        "policy_gate": policy_gate,
    }
    record["checkpoint_hash"] = checkpoint_hash(record)
    checkpoints.append(record)
    manifest["checkpoints"] = checkpoints
    manifest["latest"] = current
    manifest["latest_checkpoint_hash"] = record["checkpoint_hash"]
    write_json_atomic(manifest_path, manifest)
    return {
        "status": "recorded",
        "manifest": str(manifest_path),
        "index": record["index"],
        "step_id": args.step_id,
        "save_id_hash": current["save_id_hash"],
        "reload_session_id": current["session_id"],
    }


def verify_lineage(
    manifest_path: pathlib.Path,
    bridge_dir: pathlib.Path,
    *,
    expected_mod_version: Optional[str] = None,
    required_steps: Sequence[str] = (),
    required_step_order: Sequence[str] = (),
    minimum_checkpoints: int = 0,
    verify_live: bool = True,
) -> Dict[str, Any]:
    checks: List[Dict[str, Any]] = []
    bridge_dir = normalized_path(bridge_dir)
    runtime_records = read_runtime_records(bridge_dir)
    legacy_read_only_steps: List[str] = []

    def add(name: str, passed: bool, evidence: Any) -> None:
        checks.append({"name": name, "status": "passed" if passed else "failed", "evidence": evidence})

    try:
        manifest = load_json(normalized_path(manifest_path))
        add("schema", manifest.get("schema") == SCHEMA, manifest.get("schema"))
        add("status", manifest.get("status") == "active", manifest.get("status"))
        run_id = str(manifest.get("test_run_id") or "")
        add("run_id", bool(RUN_ID_PATTERN.fullmatch(run_id)), run_id)
        mod_version = str(expected_mod_version or manifest.get("expected_mod_version") or "")
        add("mod_version", bool(mod_version) and manifest.get("expected_mod_version") == mod_version, mod_version)
        save_path = normalized_path(pathlib.Path(str(manifest.get("save_path") or "")))
        add("new_save_reserved", manifest.get("save_absent_at_reservation") is True, manifest.get("save_absent_at_reservation"))
        reservation_policy = manifest.get("reservation_policy") if isinstance(manifest.get("reservation_policy"), dict) else {}
        add(
            "reservation_policy_disabled",
            reservation_policy.get("actions_enabled") is False
            and reservation_policy.get("allowed_save_hash_count") == 0
            and reservation_policy.get("pending_count") == 0
            and reservation_policy.get("approving_count") == 0
            and reservation_policy.get("approved_count") == 0
            and reservation_policy.get("executing_count") == 0
            and reservation_policy.get("confirming_count") == 0
            and reservation_policy.get("temporary_count", 0) == 0,
            reservation_policy,
        )
        add("save_path_identity", manifest.get("save_path_id") == path_id(save_path), manifest.get("save_path_id"))
        initial = manifest.get("initial") if isinstance(manifest.get("initial"), dict) else {}
        initial_hash = initial.get("checkpoint_hash")
        add("activation_hash", bool(initial) and initial_hash == checkpoint_hash(initial), initial_hash)
        reservation_source = (
            manifest.get("reservation_source")
            if isinstance(manifest.get("reservation_source"), dict)
            else {}
        )
        source = initial.get("source") if isinstance(initial.get("source"), dict) else {}
        initial_reload = initial.get("reload") if isinstance(initial.get("reload"), dict) else {}
        initial_save = initial.get("save") if isinstance(initial.get("save"), dict) else {}
        origin_binding_ok = bool(reservation_source) and reservation_source == source
        add(
            "activation_origin_binding",
            origin_binding_ok,
            {
                "reservation_session": reservation_source.get("session_id"),
                "activation_source_session": source.get("session_id"),
                "reservation_save_id_hash": reservation_source.get("save_id_hash"),
                "activation_source_save_id_hash": source.get("save_id_hash"),
            },
        )
        activation_ok = (
            initial.get("kind") == "activation"
            and origin_binding_ok
            and source.get("session_id") != initial_reload.get("session_id")
            and initial_save.get("success") is True
            and initial_save.get("is_auto_save") is False
            and initial_save.get("update_save_pointer") is True
            and initial_save.get("save_file_readable") is True
            and initial_save.get("session_id") == source.get("session_id")
            and initial_save.get("before_save_id_hash") == source.get("save_id_hash")
            and initial_save.get("active_save_path_id") == manifest.get("save_path_id")
            and initial_save.get("written_save_id_hash") == initial_reload.get("save_id_hash")
            and initial_save.get("active_save_id_hash") == initial_reload.get("save_id_hash")
            and initial_reload.get("save_id_hash") != source.get("save_id_hash")
            and initial_reload.get("world_id") == source.get("world_id")
            and initial_reload.get("mod_version") == mod_version
            and isinstance(initial.get("policy_gate"), dict)
            and initial["policy_gate"].get("actions_enabled") is False
            and initial["policy_gate"].get("allowed_save_hash_count") == 0
            and initial["policy_gate"].get("pending_count") == 0
            and initial["policy_gate"].get("approving_count") == 0
            and initial["policy_gate"].get("approved_count") == 0
            and initial["policy_gate"].get("executing_count") == 0
            and initial["policy_gate"].get("confirming_count") == 0
            and initial["policy_gate"].get("temporary_count", 0) == 0
        )
        add("activation_save_reload", activation_ok, {"source_session": source.get("session_id"), "reload_session": initial_reload.get("session_id")})

        checkpoints = manifest.get("checkpoints") if isinstance(manifest.get("checkpoints"), list) else []
        add("minimum_checkpoints", len(checkpoints) >= minimum_checkpoints, {"observed": len(checkpoints), "minimum": minimum_checkpoints})
        observed_steps = [str(item.get("step_id") or "") for item in checkpoints if isinstance(item, dict)]
        missing_steps = [step for step in required_steps if step not in observed_steps]
        add("required_steps", not missing_steps and len(observed_steps) == len(set(observed_steps)), {"observed": observed_steps, "missing": missing_steps})
        if required_step_order:
            expected_order = list(required_step_order)
            add(
                "required_step_order",
                observed_steps == expected_order,
                {"observed": observed_steps, "expected": expected_order},
            )

        previous_hash = initial_hash
        previous_reload = initial_reload
        chain_errors: List[str] = []
        for expected_index, item in enumerate(checkpoints, start=1):
            if not isinstance(item, dict):
                chain_errors.append(f"checkpoint {expected_index} is not an object")
                continue
            save = item.get("save") if isinstance(item.get("save"), dict) else {}
            reload_state = item.get("reload") if isinstance(item.get("reload"), dict) else {}
            artifacts = item.get("artifacts") if isinstance(item.get("artifacts"), list) else []
            artifact_ok = bool(artifacts) and all(
                isinstance(artifact, dict)
                and path_within(pathlib.Path(str(artifact.get("path") or "")), manifest_path.parent / "artifacts")
                and pathlib.Path(str(artifact.get("path") or "")).is_file()
                and pathlib.Path(str(artifact.get("path") or "")).stat().st_size == artifact.get("length")
                and sha256_file(pathlib.Path(str(artifact.get("path") or ""))) == artifact.get("sha256")
                for artifact in artifacts
            )
            command = item.get("command") if isinstance(item.get("command"), dict) else None
            policy_gate = item.get("policy_gate") if isinstance(item.get("policy_gate"), dict) else {}
            command_ok = True
            if command is not None:
                command_path = bridge_dir / str(command.get("file") or "")
                command_ok = (
                    path_within(command_path, bridge_dir / "commands")
                    and command_path.is_file()
                    and sha256_file(command_path) == command.get("file_sha256")
                )
                if command_ok and item.get("outcome") in {"confirmed", "verified"}:
                    actual_command = load_json(command_path)
                    assessment = assess_command_confirmation(
                        bridge_dir,
                        actual_command,
                        archived_confirmation_records(bridge_dir, actual_command) or runtime_records,
                        directory=command_path.parent.name,
                    )
                    recorded_confirmation = (
                        command.get("confirmation")
                        if isinstance(command.get("confirmation"), dict)
                        else None
                    )
                    if item.get("outcome") == "confirmed":
                        current_summary = confirmation_summary(assessment)
                        current_publications = current_summary.get("certificate_publications") or []
                        recorded_publications = (
                            recorded_confirmation.get("certificate_publications") or []
                            if recorded_confirmation is not None
                            else []
                        )
                        publication_ok = bool(recorded_publications) and all(
                            publication in current_publications for publication in recorded_publications
                        )
                        command_ok = (
                            assessment.get("accepted") is True
                            and assessment.get("mode") == CONFIRMED_MUTATION_MODE
                            and recorded_confirmation is not None
                            and recorded_confirmation.get("mode") == CONFIRMED_MUTATION_MODE
                            and recorded_confirmation.get("accepted") is True
                            and recorded_confirmation.get("attestation_hash")
                            == current_summary.get("attestation_hash")
                            and recorded_confirmation.get("witness") == current_summary.get("witness")
                            and recorded_confirmation.get("ack_sequences")
                            == current_summary.get("ack_sequences")
                            and publication_ok
                        )
                    elif assessment.get("mode") == NON_MUTATING_VERIFIED_MODE:
                        command_ok = assessment.get("accepted") is True
                    elif assessment.get("mode") == LEGACY_MUTATION_MODE:
                        legacy_read_only_steps.append(str(item.get("step_id") or expected_index))
                        # Preserve chain readability, but a separate failed gate
                        # prevents this executor-owned legacy result from becoming
                        # current acceptance evidence.
                        command_ok = True
                    else:
                        command_ok = False
            conditions = (
                item.get("index") == expected_index,
                item.get("previous_checkpoint_hash") == previous_hash,
                item.get("checkpoint_hash") == checkpoint_hash(item),
                item.get("before") == previous_reload,
                save.get("success") is True,
                save.get("is_auto_save") is False,
                save.get("update_save_pointer") is True,
                save.get("file_rewrite_observed") is True,
                save.get("save_file_readable") is True,
                save.get("session_id") == previous_reload.get("session_id"),
                save.get("active_save_path_id") == manifest.get("save_path_id"),
                save.get("before_save_id_hash") == previous_reload.get("save_id_hash"),
                save.get("written_save_id_hash") == reload_state.get("save_id_hash"),
                save.get("active_save_id_hash") == reload_state.get("save_id_hash"),
                reload_state.get("session_id") != previous_reload.get("session_id"),
                reload_state.get("world_id") == previous_reload.get("world_id"),
                reload_state.get("mod_version") == mod_version,
                artifact_ok,
                command_ok,
                policy_gate.get("actions_enabled") is False,
                policy_gate.get("allowed_save_hash_count") == 0,
                policy_gate.get("pending_count") == 0,
                policy_gate.get("approving_count") == 0,
                policy_gate.get("approved_count") == 0,
                policy_gate.get("executing_count") == 0,
                policy_gate.get("confirming_count") == 0,
                policy_gate.get("temporary_count", 0) == 0,
                reload_state.get("save_id_hash") != previous_reload.get("save_id_hash"),
            )
            if not all(conditions):
                chain_errors.append(f"checkpoint {expected_index} failed chain, save, reload, or artifact validation")
            previous_hash = item.get("checkpoint_hash")
            previous_reload = reload_state
        add("checkpoint_chain", not chain_errors, chain_errors)
        add(
            "mutating_confirmation_contract",
            not legacy_read_only_steps,
            {
                "required": "commands/done status=confirmed with embedded/published attestation, ordered ACKs, and later trusted witness",
                "legacy_read_only_steps": legacy_read_only_steps,
            },
        )
        add("latest_pointer", manifest.get("latest_checkpoint_hash") == previous_hash and manifest.get("latest") == previous_reload, manifest.get("latest_checkpoint_hash"))

        if verify_live:
            live_state = validate_snapshot(load_json(bridge_dir / "latest_state.json"), mod_version)
            actual_hash = sha256_file(save_path) if save_path.is_file() else None
            live_ok = (
                actual_hash == previous_reload.get("save_id_hash")
                and live_state["save_id_hash"] == actual_hash
                and live_state["session_id"] == previous_reload.get("session_id")
                and live_state["world_id"] == previous_reload.get("world_id")
                and live_state["seq"] >= int(previous_reload.get("seq") or 0)
            )
            add("live_tip", live_ok, {"save_id_hash": actual_hash, "session_id": live_state.get("session_id")})
    except Exception as exc:
        add("audit_exception", False, str(exc))

    passed = all(item["status"] == "passed" for item in checks)
    non_confirmation_checks_passed = all(
        item["status"] == "passed"
        for item in checks
        if item["name"] != "mutating_confirmation_contract"
    )
    legacy_read_only = (
        not verify_live
        and bool(legacy_read_only_steps)
        and non_confirmation_checks_passed
    )
    return {
        "schema": REPORT_SCHEMA,
        "generated_at": now_iso(),
        "status": "passed" if passed else "legacy_read_only" if legacy_read_only else "incomplete_or_failed",
        "classification": None if passed else "legacy_read_only" if legacy_read_only else "behavior_fail",
        "verification_mode": "live" if verify_live else "offline_read_only",
        "legacy_read_only_steps": legacy_read_only_steps,
        "checks": checks,
    }


def verify_command(args: argparse.Namespace) -> Tuple[int, Dict[str, Any]]:
    report = verify_lineage(
        args.manifest,
        args.bridge_dir,
        expected_mod_version=args.mod_version,
        required_steps=args.required_step,
        required_step_order=args.required_step_order,
        minimum_checkpoints=args.minimum_checkpoints,
        verify_live=not args.offline,
    )
    if args.report:
        write_json_atomic(normalized_path(args.report), report)
    return (0 if report["status"] == "passed" else 1), report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bridge-dir", type=pathlib.Path, default=DEFAULT_BRIDGE)
    subparsers = parser.add_subparsers(dest="command", required=True)

    reserve_parser = subparsers.add_parser("reserve", help="Reserve a save name that does not exist yet.")
    reserve_parser.add_argument("--run-id", required=True)
    reserve_parser.add_argument("--save-path", type=pathlib.Path, required=True)
    reserve_parser.add_argument("--mod-version", required=True)
    reserve_parser.add_argument("--source-kind", choices=("save_as_copy", "new_colony"), default="save_as_copy")
    reserve_parser.add_argument("--allow-external-save", action="store_true", help=argparse.SUPPRESS)
    reserve_parser.set_defaults(func=reserve)

    activate_parser = subparsers.add_parser("activate", help="Prove creation, explicit save, and first reload.")
    activate_parser.add_argument("--manifest", type=pathlib.Path, required=True)
    activate_parser.set_defaults(func=activate)

    checkpoint_parser = subparsers.add_parser("checkpoint", help="Record one validation, explicit save, and reload.")
    checkpoint_parser.add_argument("--manifest", type=pathlib.Path, required=True)
    checkpoint_parser.add_argument("--step-id", required=True)
    checkpoint_parser.add_argument("--scenario", required=True)
    checkpoint_parser.add_argument(
        "--outcome",
        choices=(
            "confirmed",
            "verified",
            "behavior_fail",
            "quarantined",
            "control_plane_verified",
            "observer",
            "known_failure",
        ),
        required=True,
    )
    checkpoint_parser.add_argument("--expected-mutation", choices=("persisted_change", "no_game_mutation"), required=True)
    checkpoint_parser.add_argument("--command-id")
    checkpoint_parser.add_argument("--evidence", action="append", default=[])
    checkpoint_parser.set_defaults(func=checkpoint)

    freeze_parser = subparsers.add_parser(
        "freeze-artifacts",
        help="Migrate matching legacy checkpoint artifacts into immutable lineage storage.",
    )
    freeze_parser.add_argument("--manifest", type=pathlib.Path, required=True)
    freeze_parser.add_argument("--step-id", required=True)
    freeze_parser.add_argument("--recovered-artifact", action="append", required=True)
    freeze_parser.add_argument("--reason", default="legacy checkpoint referenced a mutable runtime marker")
    freeze_parser.set_defaults(func=freeze_existing_artifacts)

    verify_parser = subparsers.add_parser("verify", help="Verify the checkpoint hash chain and current save tip.")
    verify_parser.add_argument("--manifest", type=pathlib.Path, required=True)
    verify_parser.add_argument("--mod-version")
    verify_parser.add_argument("--required-step", action="append", default=[])
    verify_parser.add_argument("--required-step-order", action="append", default=[])
    verify_parser.add_argument("--minimum-checkpoints", type=int, default=0)
    verify_parser.add_argument("--offline", action="store_true")
    verify_parser.add_argument("--report", type=pathlib.Path)
    verify_parser.set_defaults(func=None)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "verify":
            code, payload = verify_command(args)
        else:
            payload = args.func(args)
            code = 0
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return code
    except Exception as exc:
        print(json.dumps({"status": "error", "classification": "infra_fail", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
