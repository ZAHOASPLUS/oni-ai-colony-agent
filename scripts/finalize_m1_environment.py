#!/usr/bin/env python3
"""Finalize an M1 (model-loop) live validation run: restore the environment.

Why a separate path
-------------------
The G0 ``finalize`` in ``manage_live_validation_environment.py`` proves a
priority-pair lineage (forward/inverse command bundles, a derivative save, a
shutdown certificate) before it restores anything.  An M1 run never produces
those artifacts: the model loop only proposes ``set_paused``/``noop``-class
commands and the autosave guard forbids new saves.  ``abort`` cannot be used
either because it refuses any session or command evidence.  Until 2026-09-27
M1 runs were therefore restored by a hand-written script kept in ``%TEMP%``.

This module is that restoration as a first-class, tested command.  It keeps
the same evidence anchors as the transactional workflows:

* the prepare-time environment manifest (external SHA-256 anchor required),
* the runtime bootstrap (``v27-runtime-bootstrap.json``) that names the
  archived pre-existing Bridge,
* a terminal command pipeline: every nonterminal queue empty, no quarantine,
  at least one ``commands/done`` record (that is what makes the run M1 rather
  than an abortable pre-session run),
* both game processes stopped,
* a passing post-boundary comparison against the pre-boundary report.

Physical restoration reuses the workflow module's ``FileTransaction`` (move,
copy_file, copy_tree, set_mtime with journaled rollback) and its exact-copy
primitives, so a fault mid-transaction rolls the ONI root back and leaves a
non-replay recovery latch instead of a half-restored environment.

The command never launches ONI or the Bridge.  ``--what-if`` performs every
read-only check and returns the plan; ``--apply`` requires
``--confirm-live-run-id`` in live mode (enforced by ``resolve_roots``).
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
from typing import Any, Iterable, Mapping, Optional

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts import manage_live_validation_environment as wf  # noqa: E402

WorkflowError = wf.WorkflowError

RESULT_SCHEMA = "oni.live-validation-m1-finalization.v1"
PLAN_SCHEMA = "oni.live-validation-m1-finalize-plan.v1"
REPLAY_SCHEMA = "oni.live-validation-m1-finalize-replay.v1"
WINDOW_SCHEMA = "oni.live-validation-m1-session-window.v1"
FINAL_ROOT_NAME = "finalization-m1"
RESULT_NAME = "finalize-m1-result.json"
TERMINAL_EVENT = "environment_finalized_m1"

COMMAND_TERMINAL_STAGES = ("done", "failed", "rejected")
COMMAND_ANOMALY_STAGES = ("quarantine", "lifecycle-recovery")
CONTROL_NONTERMINAL_STAGES = ("approved", "inflight")
CONTROL_TERMINAL_STAGES = ("done", "failed", "quarantine")

RESULT_KEYS = frozenset(
    {
        "schema",
        "status",
        "run_id",
        "mode",
        "environment_manifest_sha256",
        "session_window",
        "restored",
        "evidence",
        "post_boundary",
        "document_hash",
    }
)


# --------------------------------------------------------------------------
# Session window: what proves this run is an M1 run with a terminal pipeline
# --------------------------------------------------------------------------


# The executor quarantines an approved command whose fresh current-state
# precondition no longer holds at dispatch (the game moved on while the human
# approved; live run m1-0930b: FarmingTech finished before set_research ran).
# That is a terminal behavior_fail without any mutation, not an anomaly.
PRECONDITION_QUARANTINE_PREFIX = "Current-state precondition failed"

# The Mod's ValidationCheckpointController keeps this empty subdirectory inside
# the validation-control quarantine stage (live run m1-0930a); anything inside
# it is reload-recovery evidence and refuses the finalization.
CONTROL_QUARANTINE_SUBDIRECTORIES = wf.CONTROL_QUARANTINE_SUBDIRECTORIES


def _json_files(
    directory: pathlib.Path,
    label: str,
    *,
    allowed_empty_subdirectories: tuple[str, ...] = (),
) -> list[pathlib.Path]:
    if not directory.is_dir() or wf.is_reparse(directory):
        raise WorkflowError(f"{label} stage directory is missing or unsafe")
    entries = sorted(directory.iterdir(), key=lambda item: item.name.casefold())
    files: list[pathlib.Path] = []
    for entry in entries:
        if wf.is_reparse(entry):
            raise WorkflowError(f"{label} stage contains a reparse point")
        if entry.is_dir():
            if entry.name not in allowed_empty_subdirectories:
                raise WorkflowError(f"{label} stage contains a directory")
            if any(entry.iterdir()):
                raise WorkflowError(f"{label} stage contains {entry.name} evidence")
            continue
        if entry.name.casefold().endswith(".tmp"):
            raise WorkflowError(f"{label} stage contains a temporary file")
        files.append(entry)
    return files


def _command_record(path: pathlib.Path, stage: str) -> dict[str, Any]:
    document = wf.load_json(path, f"{stage} command {path.name}")
    command_id = document.get("id")
    if not isinstance(command_id, str) or not command_id:
        command_id = path.stem
    return {
        "stage": stage,
        "file": path.name,
        "command_id": command_id,
        "action": document.get("action") if isinstance(document.get("action"), str) else None,
        "status": document.get("status") if isinstance(document.get("status"), str) else None,
        "file_sha256": wf.sha256_file(path),
        "length": int(path.stat().st_size),
    }


def m1_session_window(
    bridge: pathlib.Path,
    *,
    expected_done_command_ids: Optional[Iterable[str]] = None,
    allow_no_done_commands: bool = False,
    allow_precondition_quarantine: bool = False,
) -> dict[str, Any]:
    """Prove the terminal M1 pipeline and bind every command record.

    Raises when a nonterminal queue, quarantine, or lifecycle-recovery holds
    anything, when the action policy is still enabled, or when the run left
    no ``commands/done`` record at all (that would be an abort case) unless
    ``allow_no_done_commands`` says so explicitly: a run that produced session
    evidence but no completed command (live run m1-0930a, 2026-09-30, where the
    game hung before the first approval) is closed to ``abort`` as well and
    still needs its environment restored.
    """

    gate = wf.bridge_gate(bridge, require_bridge=True)
    commands = bridge / "commands"
    if not commands.is_dir() or wf.is_reparse(commands):
        raise WorkflowError("M1 finalize requires the complete command queue root")
    stage_counts: dict[str, int] = dict(gate["nonterminal"])
    records: list[dict[str, Any]] = []
    for stage in COMMAND_TERMINAL_STAGES:
        entries = _json_files(commands / stage, f"command {stage}")
        stage_counts[stage] = len(entries)
        if stage == "done":
            for entry in entries:
                records.append(_command_record(entry, stage))
    quarantined: list[dict[str, Any]] = []
    for stage in COMMAND_ANOMALY_STAGES:
        directory = commands / stage
        if not directory.exists():
            stage_counts[stage] = 0
            continue
        entries = _json_files(directory, f"command {stage}")
        if entries and stage == "quarantine" and allow_precondition_quarantine:
            for entry in entries:
                record = _command_record(entry, stage)
                document = wf.load_json(entry, f"quarantine command {entry.name}")
                reason = str(document.get("quarantine_reason") or document.get("quarantine_detail") or "")
                if (
                    document.get("status") != "quarantined"
                    or document.get("classification") != "behavior_fail"
                    or not reason.startswith(PRECONDITION_QUARANTINE_PREFIX)
                ):
                    raise WorkflowError(
                        "command quarantine holds a record that is not a current-state precondition failure; "
                        "M1 finalize refuses anomalies"
                    )
                record["quarantine_reason"] = reason
                quarantined.append(record)
            stage_counts[stage] = len(entries)
            continue
        if entries:
            raise WorkflowError(f"command {stage} is not empty; M1 finalize refuses anomalies")
        stage_counts[stage] = 0
    if not records and not allow_no_done_commands:
        raise WorkflowError(
            "no commands/done evidence: this run is not an M1 run (use abort, or "
            "--allow-no-done-commands for a run that produced session evidence but no completed command)"
        )
    done_ids = [record["command_id"] for record in records]
    if len(set(done_ids)) != len(done_ids):
        raise WorkflowError("commands/done contains duplicate command ids")
    if expected_done_command_ids is not None:
        expected = sorted(set(expected_done_command_ids))
        if sorted(done_ids) != expected:
            raise WorkflowError("commands/done does not match the expected done command ids")

    controls = bridge / "validation-controls"
    control_counts: dict[str, int] = {}
    if controls.exists():
        if not controls.is_dir() or wf.is_reparse(controls):
            raise WorkflowError("validation-controls root is unsafe")
        for stage in CONTROL_NONTERMINAL_STAGES:
            directory = controls / stage
            entries = _json_files(directory, f"validation-control {stage}") if directory.exists() else []
            if entries:
                raise WorkflowError("validation-control nonterminal stage is not empty")
            control_counts[stage] = 0
        for stage in CONTROL_TERMINAL_STAGES:
            directory = controls / stage
            control_counts[stage] = len(
                _json_files(
                    directory,
                    f"validation-control {stage}",
                    allowed_empty_subdirectories=(
                        CONTROL_QUARANTINE_SUBDIRECTORIES if stage == "quarantine" else ()
                    ),
                )
                if directory.exists()
                else []
            )

    frames = bridge / "decision-frames"
    frame_count = 0
    if frames.exists():
        if not frames.is_dir() or wf.is_reparse(frames):
            raise WorkflowError("decision-frames root is unsafe")
        frame_count = sum(1 for entry in frames.iterdir() if entry.is_file())

    snapshot = wf.snapshot_tree(bridge)
    return {
        "schema": WINDOW_SCHEMA,
        "policy_disabled": True,
        "policy_sha256": gate["policy_sha256"],
        "allow_no_done_commands": bool(allow_no_done_commands),
        "allow_precondition_quarantine": bool(allow_precondition_quarantine),
        "quarantined_commands": quarantined,
        "command_stage_counts": stage_counts,
        "control_stage_counts": control_counts,
        "done_commands": records,
        "decision_frame_count": frame_count,
        "runtime_bridge_snapshot_hash": wf.sha256_bytes(wf.canonical_bytes(snapshot)),
        "runtime_bridge_file_count": int(snapshot.get("file_count", 0)),
        "runtime_bridge_total_bytes": int(snapshot.get("total_bytes", 0)),
        "_snapshot": snapshot,
    }


# --------------------------------------------------------------------------
# Transaction planning
# --------------------------------------------------------------------------


def m1_file_operations(
    roots: "wf.Roots",
    manifest: Mapping[str, Any],
    *,
    source_save: pathlib.Path,
    loader_save: pathlib.Path,
    final_root: pathlib.Path,
    bridge: pathlib.Path,
    old_archive: Optional[pathlib.Path],
) -> list[dict[str, Any]]:
    """The G0 restore plan without its lineage-derivative steps."""

    operations: list[dict[str, Any]] = []
    loader_preview = loader_save.with_suffix(".png")
    for path, label in ((loader_save, "archive_loader_save"), (loader_preview, "archive_loader_preview")):
        if path.exists():
            operations.append(
                {
                    "kind": "move",
                    "source": path,
                    "target": final_root / "loader-before-restore" / path.name,
                    "label": label,
                }
            )
    operations.append(
        {"kind": "copy_file", "source": source_save, "target": loader_save, "label": "restore_loader_save"}
    )
    operations.append(
        {
            "kind": "copy_file",
            "source": source_save.with_suffix(".png"),
            "target": loader_preview,
            "label": "restore_loader_preview",
        }
    )
    for record in manifest["backup"]["configs"]:
        relative = pathlib.Path(record["relative"])
        current = roots.oni_root / relative
        label = record["relative"].replace("/", "_").replace("\\", "_")
        if current.exists():
            operations.append(
                {
                    "kind": "move",
                    "source": current,
                    "target": final_root / "runtime-current" / "config" / relative,
                    "label": f"archive_current_{label}",
                }
            )
        if record["exists"]:
            operations.append(
                {
                    "kind": "copy_file",
                    "source": roots.environment_dir / pathlib.Path(record["backup_relative"]),
                    "target": current,
                    "label": f"restore_{label}",
                }
            )
    probe_record = manifest["backup"]["probe"]
    probe = roots.oni_root / wf.PROBE_RELATIVE
    if probe.exists():
        operations.append(
            {
                "kind": "move",
                "source": probe,
                "target": final_root / "runtime-current" / "probe",
                "label": "archive_current_probe",
            }
        )
    if probe_record["snapshot"]["exists"]:
        operations.append(
            {
                "kind": "copy_tree",
                "source": roots.environment_dir / pathlib.Path(probe_record["backup_relative"]),
                "target": probe,
                "expected": probe_record["snapshot"],
                "label": "restore_probe",
            }
        )
    operations.append(
        {"kind": "move", "source": bridge, "target": roots.environment_dir / "rb", "label": "archive_runtime_bridge"}
    )
    if manifest["backup"]["old_bridge"]["snapshot"]["exists"]:
        if old_archive is None:
            raise WorkflowError("old bridge archive disappeared before transaction planning")
        operations.append({"kind": "move", "source": old_archive, "target": bridge, "label": "restore_old_bridge"})
    archive_snapshot = manifest["backup"]["bridge_archive_root"]["snapshot"]
    archive_root = roots.oni_root / wf.BRIDGE_ARCHIVE_RELATIVE
    if archive_snapshot["exists"]:
        if not archive_root.exists():
            raise WorkflowError("preexisting bridge archive root disappeared")
        operations.append(
            {
                "kind": "set_mtime",
                "source": archive_root,
                "target": archive_root,
                "value": int(archive_snapshot["root_last_write_ns"]),
                "label": "restore_archive_root_timestamp",
            }
        )
    elif archive_root.exists():
        leftovers = [
            entry for entry in archive_root.iterdir() if old_archive is None or not wf.same_path(entry, old_archive)
        ]
        if leftovers:
            raise WorkflowError("new bridge archive root contains unexpected entries")
        operations.append(
            {
                "kind": "move",
                "source": archive_root,
                "target": final_root / "runtime-current" / "empty-ai-bridge-archives",
                "label": "archive_empty_archive_root",
            }
        )
    return operations


def _plan_document(roots: "wf.Roots", operations: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    plan: list[dict[str, Any]] = []
    for operation in operations:
        item = {"kind": operation["kind"], "label": operation["label"]}
        for key in ("source", "target"):
            path = wf.normalized(operation[key])
            for root_name, root in (("oni", roots.oni_root), ("evidence", roots.evidence_root)):
                if wf.path_within(path, root, strict=False):
                    item[key] = f"{root_name}:{path.relative_to(root).as_posix()}"
                    break
            else:
                raise WorkflowError(f"transaction path escaped both roots: {operation['label']}")
        if "value" in operation:
            item["value"] = int(operation["value"])
        plan.append(item)
    return plan


# --------------------------------------------------------------------------
# Restored-state verification
# --------------------------------------------------------------------------


def verify_m1_restored_state(
    roots: "wf.Roots",
    manifest: Mapping[str, Any],
    source_save: pathlib.Path,
    loader_save: pathlib.Path,
    *,
    runtime_bridge_snapshot: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    bindings = manifest["bindings"]
    if wf.file_state(source_save) != bindings["source_save"]["state"]:
        raise WorkflowError("immutable source save changed during M1 finalization")
    if wf.file_state(source_save.with_suffix(".png")) != bindings["source_preview"]["state"]:
        raise WorkflowError("immutable source preview changed during M1 finalization")
    if wf.file_state(loader_save) != bindings["loader_save"]["state"]:
        raise WorkflowError("loader save content/timestamp was not restored exactly")
    if wf.file_state(loader_save.with_suffix(".png")) != bindings["loader_preview"]["state"]:
        raise WorkflowError("loader preview content/timestamp was not restored exactly")
    configs: list[dict[str, Any]] = []
    for record in manifest["backup"]["configs"]:
        actual = wf.optional_file_state(roots.oni_root / pathlib.Path(record["relative"]))
        expected = {key: value for key, value in record.items() if key in {"exists", "state"}}
        if actual != expected:
            raise WorkflowError(f"config {record['relative']} content/timestamp restoration is not exact")
        configs.append({"relative": record["relative"], "status": "restored_exact" if record["exists"] else "absent"})
    probe_record = manifest["backup"]["probe"]
    wf.verify_tree(roots.oni_root / wf.PROBE_RELATIVE, probe_record["snapshot"], "restored probe")
    old_record = manifest["backup"]["old_bridge"]
    wf.verify_tree(roots.oni_root / wf.BRIDGE_RELATIVE, old_record["snapshot"], "restored old bridge")
    archive_snapshot = manifest["backup"]["bridge_archive_root"]["snapshot"]
    wf.verify_tree(roots.oni_root / wf.BRIDGE_ARCHIVE_RELATIVE, archive_snapshot, "restored bridge archive root")
    if runtime_bridge_snapshot is not None:
        wf.verify_tree(roots.environment_dir / "rb", runtime_bridge_snapshot, "preserved runtime bridge")
    return {
        "source_save": "unchanged",
        "source_preview": "unchanged",
        "loader_save": "restored_exact",
        "loader_preview": "restored_exact",
        "configs": configs,
        "probe": "restored_exact" if probe_record["snapshot"]["exists"] else "absent",
        "old_bridge": "restored_exact" if old_record["snapshot"]["exists"] else "absent",
        "bridge_archive_root": "restored_exact" if archive_snapshot["exists"] else "absent",
    }


# --------------------------------------------------------------------------
# Command
# --------------------------------------------------------------------------


def _load_result(final_root: pathlib.Path) -> Optional[dict[str, Any]]:
    path = final_root / RESULT_NAME
    if not os.path.lexists(path):
        return None
    document = wf.load_json(path, "M1 finalize result")
    if set(document) != RESULT_KEYS or document.get("schema") != RESULT_SCHEMA:
        raise WorkflowError("M1 finalize result shape is invalid")
    if document.get("document_hash") != wf.contract_hash_without(document, "document_hash", "M1 finalize result"):
        raise WorkflowError("M1 finalize result self-hash is invalid")
    return document


def finalize_m1_locked(args: argparse.Namespace, roots: "wf.Roots") -> dict[str, Any]:
    _manifest_path, manifest, manifest_hash = wf.load_environment_manifest(args, roots)
    expected_manifest_hash = wf.require_hash(
        args.expected_environment_manifest_sha256, "external environment manifest anchor"
    )
    if manifest_hash != expected_manifest_hash:
        raise WorkflowError("environment manifest does not match its external SHA-256 anchor")
    final_root = roots.environment_dir / FINAL_ROOT_NAME
    journal = roots.environment_dir / "restore-journal.jsonl"
    source_save, loader_save = wf.verify_base_bindings(args, roots, manifest)

    # Replay: an exact prior result is returned once the restored state is
    # re-verified.  Anything else left behind by an earlier attempt is a
    # non-replay condition that a person must inspect.
    existing = _load_result(final_root)
    if existing is not None:
        if existing.get("run_id") != roots.run_id or existing.get("environment_manifest_sha256") != manifest_hash:
            raise WorkflowError("existing M1 finalize result binds a different run or manifest")
        rb_snapshot = wf.load_json(roots.environment_dir / "rb-snapshot.json", "preserved runtime bridge snapshot")
        verify_m1_restored_state(roots, manifest, source_save, loader_save, runtime_bridge_snapshot=rb_snapshot)
        return {"schema": REPLAY_SCHEMA, "status": "already_finalized", "run_id": roots.run_id, "result": existing}
    if os.path.lexists(final_root / "transaction-recovery.json"):
        raise WorkflowError("a prior M1 finalize attempt left a recovery latch; manual recovery required")
    if os.path.lexists(final_root) or os.path.lexists(roots.environment_dir / "rb"):
        raise WorkflowError("a prior M1 finalize attempt left evidence without a result; manual recovery required")

    wf.verify_backups(roots, manifest)
    counts = wf.process_counts(args, roots)
    wf.require_processes_stopped(counts)
    bridge = roots.oni_root / wf.BRIDGE_RELATIVE
    _bootstrap_path, bootstrap, old_archive = wf.validate_bootstrap(roots, manifest, bridge)
    window = m1_session_window(
        bridge,
        expected_done_command_ids=args.expect_done_command_id or None,
        allow_no_done_commands=bool(getattr(args, "allow_no_done_commands", False)),
        allow_precondition_quarantine=bool(getattr(args, "allow_precondition_quarantine", False)),
    )
    runtime_bridge_snapshot = window.pop("_snapshot")
    operations = m1_file_operations(
        roots,
        manifest,
        source_save=source_save,
        loader_save=loader_save,
        final_root=final_root,
        bridge=bridge,
        old_archive=old_archive,
    )
    plan = _plan_document(roots, operations)
    if not args.apply:
        return {
            "schema": PLAN_SCHEMA,
            "status": "would_finalize_m1",
            "run_id": roots.run_id,
            "write_count": 0,
            "environment_manifest_sha256": manifest_hash,
            "bootstrap_prepared_at": bootstrap.get("prepared_at"),
            "session_window": window,
            "step_count": len(plan),
            "steps": plan,
        }

    final_root.mkdir(parents=True, exist_ok=False)
    wf.write_json_once(
        final_root / "session-window.json",
        {**window, "run_id": roots.run_id, "environment_manifest_sha256": manifest_hash},
        "M1 session window",
    )
    wf.write_json_once(roots.environment_dir / "rb-snapshot.json", runtime_bridge_snapshot, "runtime bridge snapshot")
    wf.write_json_once(
        final_root / "transaction-plan.json",
        {
            "schema": "oni.live-validation-m1-transaction-plan.v1",
            "run_id": roots.run_id,
            "environment_manifest_sha256": manifest_hash,
            "steps": plan,
        },
        "M1 transaction plan",
    )
    wf.append_journal(
        journal,
        "finalize_m1_preflight_passed",
        {
            "environment_manifest_sha256": manifest_hash,
            "runtime_bridge_snapshot_hash": window["runtime_bridge_snapshot_hash"],
            "done_command_count": len(window["done_commands"]),
            "step_count": len(plan),
        },
    )

    # Rebuild every mutable guard immediately before the first rename.
    source_check, loader_check = wf.verify_base_bindings(args, roots, manifest)
    if not wf.same_path(source_check, source_save) or not wf.same_path(loader_check, loader_save):
        raise WorkflowError("source/loader path binding changed before the M1 transaction")
    wf.verify_backups(roots, manifest)
    wf.require_processes_stopped(wf.process_counts(args, roots))
    _bootstrap_path, _bootstrap, current_old_archive = wf.validate_bootstrap(roots, manifest, bridge)
    if (old_archive is None) != (current_old_archive is None) or (
        old_archive is not None and current_old_archive is not None and not wf.same_path(old_archive, current_old_archive)
    ):
        raise WorkflowError("archived old Bridge changed before the M1 transaction")
    current_window = m1_session_window(
        bridge,
        expected_done_command_ids=args.expect_done_command_id or None,
        allow_no_done_commands=bool(getattr(args, "allow_no_done_commands", False)),
        allow_precondition_quarantine=bool(getattr(args, "allow_precondition_quarantine", False)),
    )
    current_window.pop("_snapshot")
    if current_window != window:
        raise WorkflowError("runtime bridge changed before the M1 transaction")
    wf.verify_tree(bridge, runtime_bridge_snapshot, "runtime bridge pretransaction")

    tx = wf.FileTransaction(
        journal,
        final_root,
        fault_after=int(getattr(args, "test_fail_after_step", 0) or 0),
        recovery_context={
            "schema": "oni.live-validation-m1-transaction-recovery.v1",
            "run_id": roots.run_id,
            "environment_manifest_sha256": manifest_hash,
        },
    )
    wf.append_journal(
        journal,
        "restore_transaction_started",
        {"transaction_kind": "finalize_m1_restore", "expected_step_count": len(plan)},
    )
    try:
        wf._execute_finalize_file_operations(tx, operations)
        restored = verify_m1_restored_state(
            roots,
            manifest,
            source_save,
            loader_save,
            runtime_bridge_snapshot=runtime_bridge_snapshot,
        )
        wf.require_processes_stopped(wf.process_counts(args, roots))
        tx.commit()
    except Exception:
        if not tx.committed:
            tx.rollback()
        raise

    # The physical restore is committed.  A post-boundary failure from here on
    # must not be hidden or replayed: publish a latch and stop.
    try:
        comparison = wf.run_post_boundary(args, roots, final_root, manifest)
        restored_again = verify_m1_restored_state(
            roots, manifest, source_save, loader_save, runtime_bridge_snapshot=runtime_bridge_snapshot
        )
        if restored_again != restored:
            raise WorkflowError("restored state changed during the post-boundary comparison")
    except Exception as exc:
        wf.write_json_once(
            final_root / "transaction-recovery.json",
            {
                "schema": "oni.live-validation-m1-transaction-recovery.v1",
                "state": "post_boundary_blocked_after_physical_commit",
                "run_id": roots.run_id,
                "environment_manifest_sha256": manifest_hash,
                "error_type": type(exc).__name__,
                "recovery_required": True,
            },
            "M1 post-boundary recovery",
        )
        raise

    result: dict[str, Any] = {
        "schema": RESULT_SCHEMA,
        "status": "finalized",
        "run_id": roots.run_id,
        "mode": "live" if roots.live_mode else "test_fixture",
        "environment_manifest_sha256": manifest_hash,
        "session_window": window,
        "restored": restored,
        "evidence": {
            "runtime_bridge_relative": "rb",
            "runtime_bridge_snapshot_relative": "rb-snapshot.json",
            "session_window_relative": f"{FINAL_ROOT_NAME}/session-window.json",
            "transaction_plan_relative": f"{FINAL_ROOT_NAME}/transaction-plan.json",
            "runtime_current_relative": f"{FINAL_ROOT_NAME}/runtime-current",
            "loader_before_restore_relative": f"{FINAL_ROOT_NAME}/loader-before-restore",
            "post_boundary_comparison_relative": f"{FINAL_ROOT_NAME}/post-boundary-comparison.json",
            "post_boundary_comparison_sha256": wf.sha256_file(final_root / "post-boundary-comparison.json"),
        },
        "post_boundary": {
            "status": comparison.get("status"),
            "new_save_count": comparison.get("new_save_count"),
            "preexisting_save_count": comparison.get("preexisting_save_count"),
            "temporary_file_count": comparison.get("temporary_file_count"),
            "blockers": list(comparison.get("blockers") or []),
        },
    }
    result["document_hash"] = wf.contract_canonical_hash(result)
    result_path = final_root / RESULT_NAME
    try:
        wf.write_json_atomic(result_path, result, create_only=True)
        wf.append_journal(
            journal,
            TERMINAL_EVENT,
            {
                "finalize_m1_result_sha256": wf.sha256_file(result_path),
                "post_boundary_status": comparison.get("status"),
                "runtime_bridge_snapshot_hash": window["runtime_bridge_snapshot_hash"],
            },
        )
    except Exception as exc:
        wf.write_json_once(
            final_root / "transaction-recovery.json",
            {
                "schema": "oni.live-validation-m1-transaction-recovery.v1",
                "state": "result_publication_outcome_unknown",
                "run_id": roots.run_id,
                "environment_manifest_sha256": manifest_hash,
                "candidate_result": result,
                "error_type": type(exc).__name__,
                "recovery_required": True,
            },
            "M1 result recovery",
        )
        raise WorkflowError("M1 finalize result publication failed after physical commit") from exc
    return result


def finalize_m1(args: argparse.Namespace) -> dict[str, Any]:
    roots = wf.resolve_roots(args)
    if getattr(args, "test_fail_after_step", 0) and roots.test_root is None:
        raise WorkflowError("fault injection is allowed only with TestRoot")
    if not args.apply:
        return finalize_m1_locked(args, roots)
    with wf.exclusive_finalize_lock(roots):
        return finalize_m1_locked(args, roots)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    wf.add_common(parser)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--expected-environment-manifest-sha256", required=True)
    parser.add_argument(
        "--expect-done-command-id",
        action="append",
        help="When given, commands/done must contain exactly these command ids.",
    )
    parser.add_argument(
        "--allow-no-done-commands",
        action="store_true",
        help=(
            "Restore a run that produced session evidence but no commands/done record "
            "(for example the game hung before the first approval); such a run is closed to abort."
        ),
    )
    parser.add_argument(
        "--allow-precondition-quarantine",
        action="store_true",
        help=(
            "Admit quarantine records whose reason is a current-state precondition failure at dispatch "
            "(the game moved on while the human approved); any other quarantine record still refuses."
        ),
    )
    parser.add_argument("--test-fail-after-step", type=int, default=0, help=argparse.SUPPRESS)
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    args.command = "finalize-m1"
    try:
        result = finalize_m1(args)
    except WorkflowError as exc:
        print(json.dumps({"status": "refused", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
