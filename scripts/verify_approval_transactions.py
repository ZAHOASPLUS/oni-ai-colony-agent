#!/usr/bin/env python3
"""Fault-inject the pending -> approving -> approved single-copy transition."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
import sys
import tempfile
from types import SimpleNamespace
from typing import Any, Dict
from unittest import mock

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.approval_receipt import build_approved_command, validate_approval_receipt  # noqa: E402
from agent.decision_frame_archive import decision_frame_archive_path  # noqa: E402
from scripts import commit_command_approval as approval_commit  # noqa: E402
from scripts.verify_approval_frame_binding import fixture, prepare_bridge  # noqa: E402


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def locations(root: pathlib.Path, command_id: str) -> list[str]:
    result: list[str] = []
    commands = root / "commands"
    if not commands.exists():
        return result
    for directory in sorted(path for path in commands.iterdir() if path.is_dir()):
        if (directory / f"{command_id}.json").exists():
            result.append(directory.name)
    return result


def require_no_replay_duplicate(root: pathlib.Path, command_id: str) -> list[str]:
    current = locations(root, command_id)
    later = [stage for stage in current if stage != "pending"]
    require(
        not ("pending" in current and later),
        f"pending replay copy coexists with later stages: {current}",
    )
    return current


def prepare(root: pathlib.Path) -> tuple[SimpleNamespace, Dict[str, Any], Dict[str, Any]]:
    command, policy, frame = fixture()
    bridge = prepare_bridge(root, command, policy, frame)
    approved = build_approved_command(
        command,
        policy,
        frame,
        approved_by="local_human",
        approved_at=dt.datetime.now(dt.timezone.utc).isoformat(),
    )
    payload = bridge / "approved-payload.tmp"
    payload.write_text(json.dumps(approved, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    source = bridge / "commands" / "pending" / f"{command['id']}.json"
    target = bridge / "commands" / "approved" / f"{command['id']}.json"
    archive = decision_frame_archive_path(bridge, command["source_frame_id"])
    args = SimpleNamespace(
        command=source,
        target=target,
        approved_payload=payload,
        policy=bridge / "action-policy.json",
        frame=archive,
        expected_command_sha256=sha256(source),
        expected_policy_sha256=sha256(bridge / "action-policy.json"),
        expected_frame_sha256=sha256(archive),
    )
    return args, approved, frame


def inject(root: pathlib.Path, fail_on_replace: int | None) -> Dict[str, Any]:
    args, approved, frame = prepare(root)
    original_replace = approval_commit.os.replace
    calls = 0

    def replacement(source: Any, target: Any) -> None:
        nonlocal calls
        calls += 1
        if fail_on_replace is not None and calls == fail_on_replace:
            raise OSError(f"simulated crash before replace {fail_on_replace}")
        original_replace(source, target)

    if fail_on_replace is None:
        approval_commit.commit(args)
        crashed = False
    else:
        try:
            with mock.patch.object(approval_commit.os, "replace", side_effect=replacement):
                approval_commit.commit(args)
        except (OSError, approval_commit.ApprovalCommitError):
            crashed = True
        else:
            crashed = False
        require(crashed, f"replace boundary {fail_on_replace} did not crash")

    command_id = str(approved["id"])
    current = require_no_replay_duplicate(root, command_id)
    require(len(current) == 1, f"boundary {fail_on_replace} exposed {current}")
    require(not list((root / "commands").glob(".approval-*.lock")), "approval lock leaked")
    require(not list((root / "commands" / "approving").glob("*.tmp")), "approval stage leaked")
    if fail_on_replace == 1:
        require(current == ["pending"], "pre-claim crash did not preserve pending")
    elif fail_on_replace in {2, 3}:
        require(current == ["pending"], "caught post-claim failure did not restore pending")
    else:
        require(current == ["approved"], "successful transition did not publish approved")
        committed = json.loads(args.target.read_text(encoding="utf-8-sig"))
        policy = json.loads(args.policy.read_text(encoding="utf-8-sig"))
        validate_approval_receipt(committed, policy, frame)
    return {
        "replace_failure": fail_on_replace,
        "replace_calls": calls if fail_on_replace is not None else 3,
        "visible_location": current[0],
        "single_copy": True,
        "executor_visible": current[0] == "approved",
    }


def inject_post_publish_fault(root: pathlib.Path, fault: str) -> Dict[str, Any]:
    args, _approved, _frame = prepare(root)
    command_id = args.command.stem
    original_replace = approval_commit.os.replace
    original_load = approval_commit._load_object
    original_sha256 = approval_commit._sha256
    original_fsync = approval_commit._fsync_file
    rename_snapshots: list[list[str]] = []

    def tracked_replace(source: Any, target: Any) -> None:
        source_path = pathlib.Path(source)
        target_path = pathlib.Path(target)
        if (
            fault == "reclaim_failure"
            and source_path == args.target
            and target_path.parent.name == "approving"
        ):
            raise PermissionError("simulated atomic approved reclaim failure")
        if (
            fault == "restore_failure"
            and target_path == args.command
            and source_path.parent.name == "approving"
            and source_path.suffix == ".tmp"
        ):
            raise PermissionError("simulated pending recovery restore failure")
        original_replace(source, target)
        rename_snapshots.append(require_no_replay_duplicate(root, command_id))

    def load_object(path: pathlib.Path) -> Dict[str, Any]:
        if fault in {"raw_after_final", "reclaim_failure", "restore_failure"} and path == args.target and args.target.exists():
            raise OSError("simulated raw read failure after final approved rename")
        return original_load(path)

    moved_to_executing = False

    def sha256(path: pathlib.Path) -> str:
        nonlocal moved_to_executing
        published = args.target.exists()
        if published and fault == "policy_read" and path == args.policy:
            raise OSError("simulated post-publish policy read failure")
        if published and fault == "frame_read" and path == args.frame:
            raise OSError("simulated post-publish frame read failure")
        if published and fault == "target_moved_executing" and path == args.policy:
            executing = root / "commands" / "executing" / args.target.name
            executing.parent.mkdir(parents=True, exist_ok=True)
            original_replace(args.target, executing)
            moved_to_executing = True
            rename_snapshots.append(require_no_replay_duplicate(root, command_id))
            raise OSError("simulated executor claim before approval recovery")
        return original_sha256(path)

    def fsync_file(path: pathlib.Path) -> None:
        if fault == "fsync_failure":
            raise OSError("simulated approved file fsync failure")
        original_fsync(path)

    caught: BaseException | None = None
    with mock.patch.object(approval_commit.os, "replace", side_effect=tracked_replace), mock.patch.object(
        approval_commit, "_load_object", side_effect=load_object
    ), mock.patch.object(approval_commit, "_sha256", side_effect=sha256), mock.patch.object(
        approval_commit, "_fsync_file", side_effect=fsync_file
    ):
        try:
            approval_commit.commit(args)
        except approval_commit.ApprovalCommitError as exc:
            caught = exc

    require(caught is not None, f"{fault} did not reject the approval commit")
    current = require_no_replay_duplicate(root, command_id)
    evidence = list((root / "commands" / "approval-recovery").glob("*.ambiguity.json"))
    outcome_unknown = isinstance(caught, approval_commit.ApprovalOutcomeUnknown)
    if fault in {
        "raw_after_final",
        "fsync_failure",
        "policy_read",
        "frame_read",
    }:
        require(not outcome_unknown, f"{fault} became ambiguous despite exclusive reclaim")
        require(current == ["pending"], f"{fault} did not restore exactly one pending command: {current}")
        require(not evidence, f"{fault} emitted false ambiguity evidence")
    elif fault == "target_moved_executing":
        require(moved_to_executing, "executor-race fixture did not move the approved target")
        require(outcome_unknown, "executor claim race was not classified outcome_unknown")
        require(current == ["executing"], f"executor claim race restored a replayable command: {current}")
        require(evidence, "executor claim race did not preserve ambiguity evidence")
    elif fault == "reclaim_failure":
        require(outcome_unknown, "approved reclaim failure was not classified outcome_unknown")
        require(current == ["approved"], f"reclaim failure changed executor ownership: {current}")
        require(evidence, "reclaim failure did not preserve ambiguity evidence")
    elif fault == "restore_failure":
        require(outcome_unknown, "pending restore failure was not classified outcome_unknown")
        require(current == [], f"restore failure exposed a replayable queue copy: {current}")
        require(evidence, "restore failure did not preserve ambiguity evidence")
    else:
        raise AssertionError(f"unsupported fault: {fault}")

    require(rename_snapshots, f"{fault} captured no atomic rename snapshots")
    return {
        "fault": fault,
        "queue_locations": current,
        "outcome_unknown": outcome_unknown,
        "ambiguity_evidence_count": len(evidence),
        "rename_snapshots": rename_snapshots,
        "no_pending_later_duplicate": True,
    }


def case_unowned_lock_is_preserved(root: pathlib.Path) -> Dict[str, Any]:
    args, _approved, _frame = prepare(root)
    lock = root / "commands" / f".approval-{args.command.stem}.lock"
    owner = b"other-process-owner"
    lock.write_bytes(owner)
    try:
        approval_commit.commit(args)
    except approval_commit.ApprovalCommitError as exc:
        require("already owns" in str(exc), "competing lock rejection was not explicit")
    else:
        raise AssertionError("approval acquired a lock owned by another process")
    require(lock.is_file() and lock.read_bytes() == owner, "failed contender deleted another owner's lock")
    require(locations(root, args.command.stem) == ["pending"], "lock rejection changed the command queue")
    return {"unowned_lock_preserved": True, "queue_locations": ["pending"]}


def main() -> int:
    cases: list[Dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="oni-approval-transaction-") as temporary:
        base = pathlib.Path(temporary)
        for name, boundary in (
            ("before_claim", 1),
            ("after_claim_before_payload", 2),
            ("after_payload_before_publish", 3),
            ("success", None),
        ):
            try:
                details = inject(base / name, boundary)
                cases.append({"name": name, "status": "passed", "details": details})
            except Exception as exc:
                cases.append(
                    {
                        "name": name,
                        "status": "failed",
                        "classification": "behavior_fail",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
        for fault in (
            "raw_after_final",
            "fsync_failure",
            "policy_read",
            "frame_read",
            "target_moved_executing",
            "reclaim_failure",
            "restore_failure",
        ):
            try:
                details = inject_post_publish_fault(base / fault, fault)
                cases.append({"name": fault, "status": "passed", "details": details})
            except Exception as exc:
                cases.append(
                    {
                        "name": fault,
                        "status": "failed",
                        "classification": "behavior_fail",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
        try:
            details = case_unowned_lock_is_preserved(base / "unowned-lock")
            cases.append({"name": "unowned_lock_preserved", "status": "passed", "details": details})
        except Exception as exc:
            cases.append(
                {
                    "name": "unowned_lock_preserved",
                    "status": "failed",
                    "classification": "behavior_fail",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    passed = all(item["status"] == "passed" for item in cases)
    report = {
        "schema": "oni.approval-transaction-verification.v1",
        "generated_at": dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds"),
        "status": "passed" if passed else "failed",
        "classification": None if passed else "behavior_fail",
        "cases": cases,
    }
    path = PROJECT_ROOT / "reports" / "approval-transaction-verification.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for item in cases:
        print(f"[{item['status'].upper()}] {item['name']}: {item.get('details') or item.get('error')}")
    print(f"Report: {path}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
