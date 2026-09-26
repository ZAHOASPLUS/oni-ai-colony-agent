#!/usr/bin/env python3
"""Adversarial offline verification for immutable decision-frame approval."""

from __future__ import annotations

import argparse
import contextlib
import copy
import datetime as dt
import hashlib
import io
import json
import os
import pathlib
import sys
import tempfile
import time
from types import SimpleNamespace
from typing import Any, Callable, Dict
from unittest import mock


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.approval_receipt import (  # noqa: E402
    ApprovalReceiptError,
    build_approved_command,
    canonical_hash,
)
from agent.colony_plan import advance_colony_plan, create_colony_plan  # noqa: E402
from agent import decision_frame_archive as frame_archive  # noqa: E402
from agent.decision_frame_archive import (  # noqa: E402
    DecisionFrameArchiveError,
    decision_frame_archive_path,
    load_archived_decision_frame,
    load_current_decision_context,
    persist_decision_frame_archive,
    prune_decision_frame_archives,
)
from scripts import commit_command_approval, verify_runtime  # noqa: E402
from scripts.verify_approval_frame_binding import (  # noqa: E402
    fixture,
    prepare_bridge,
    rebind_state_hash,
    run_approval,
    write_json,
)
from scripts.verify_colony_plan import (  # noqa: E402
    only_command,
    priority_definition,
    state as colony_state,
    write_live_state,
)
from scripts.verify_decision_frame import build, healthy_inputs  # noqa: E402


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def case_root_advance_uses_exact_archive(root: pathlib.Path) -> Dict[str, Any]:
    command, policy, source_frame = fixture(mode="runtime_validation")
    bridge = prepare_bridge(root, command, policy, source_frame)
    _other_command, _other_policy, advanced = fixture(
        mode="runtime_validation",
        pipeline_blocked=True,
    )
    require(advanced["frame_id"] != source_frame["frame_id"], "advanced frame id did not change")
    require(
        "COMMAND_PIPELINE_MUTATION_BLOCKED" in advanced["readiness"]["propose"]["blockers"],
        "advanced frame lacks the intended pipeline blocker",
    )
    write_json(bridge / "decision-frame.json", advanced)
    persist_decision_frame_archive(bridge, advanced)

    run_approval(bridge, command["id"], expect_success=True)
    approved = json.loads(
        (bridge / "commands" / "approved" / f"{command['id']}.json").read_text(
            encoding="utf-8-sig"
        )
    )
    require(
        approved["approval_receipt"]["source_frame_id"] == source_frame["frame_id"],
        "approval drifted to the moving root frame",
    )
    return {
        "source_frame_id": source_frame["frame_id"],
        "advanced_root_frame_id": advanced["frame_id"],
        "advanced_root_pipeline_blocked": True,
        "archive_approval_succeeded": True,
    }


def _rejection_fixture(root: pathlib.Path, name: str) -> tuple[pathlib.Path, Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    command, policy, frame = fixture(mode="runtime_validation")
    bridge = prepare_bridge(root / name, command, policy, frame)
    return bridge, command, policy, frame


def case_archive_and_binding_rejections(root: pathlib.Path) -> Dict[str, Any]:
    rejected: Dict[str, bool] = {}

    bridge, command, _policy, _frame = _rejection_fixture(root, "missing")
    decision_frame_archive_path(bridge, command["source_frame_id"]).unlink()
    run_approval(bridge, command["id"], expect_success=False)
    rejected["missing_exact_archive"] = True

    bridge, command, _policy, frame = _rejection_fixture(root, "tampered")
    tampered = copy.deepcopy(frame)
    tampered["source"]["state_hash"] = "sha256:" + "f" * 64
    write_json(decision_frame_archive_path(bridge, command["source_frame_id"]), tampered)
    run_approval(bridge, command["id"], expect_success=False)
    rejected["tampered_archive"] = True

    bridge, command, _policy, _frame = _rejection_fixture(root, "wrong-id")
    _other_command, _other_policy, other_frame = fixture(
        mode="runtime_validation",
        pipeline_blocked=True,
    )
    write_json(decision_frame_archive_path(bridge, command["source_frame_id"]), other_frame)
    run_approval(bridge, command["id"], expect_success=False)
    rejected["wrong_id_payload"] = True

    bridge, command, _policy, _frame = _rejection_fixture(root, "source-mismatch")
    command["source_snapshot_id"] = f"{command['session_id']}:499"
    write_json(bridge / "commands" / "pending" / f"{command['id']}.json", command)
    run_approval(bridge, command["id"], expect_success=False)
    rejected["source_mismatch"] = True

    bridge, command, policy, _frame = _rejection_fixture(root, "policy-stale")
    changed_policy = copy.deepcopy(policy)
    changed_policy["max_pending_commands"] = int(policy["max_pending_commands"]) + 1
    write_json(bridge / "action-policy.json", changed_policy)
    run_approval(bridge, command["id"], expect_success=False)
    rejected["policy_stale"] = True

    bridge, command, _policy, _frame = _rejection_fixture(root, "expired")
    command["expires_at"] = (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
    ).isoformat()
    write_json(bridge / "commands" / "pending" / f"{command['id']}.json", command)
    run_approval(bridge, command["id"], expect_success=False)
    rejected["expired"] = True

    bridge, command, _policy, _frame = _rejection_fixture(root, "arbitrary-history")
    decision_frame_archive_path(bridge, command["source_frame_id"]).unlink()
    _historical_command, _historical_policy, historical_frame = fixture(
        mode="runtime_validation",
        proposal_blocked=True,
    )
    persist_decision_frame_archive(bridge, historical_frame)
    write_json(bridge / "decision-frame.json", historical_frame)
    run_approval(bridge, command["id"], expect_success=False)
    rejected["arbitrary_historical_fallback"] = True

    return rejected


def case_arbitrary_frame_argument_rejected(root: pathlib.Path) -> Dict[str, Any]:
    command, policy, frame = fixture(mode="runtime_validation")
    bridge = prepare_bridge(root, command, policy, frame)
    approved = build_approved_command(
        command,
        policy,
        frame,
        approved_by="local_human",
        approved_at=dt.datetime.now(dt.timezone.utc).isoformat(),
    )
    payload = bridge / "approved-payload.tmp"
    write_json(payload, approved)
    source = bridge / "commands" / "pending" / f"{command['id']}.json"
    target = bridge / "commands" / "approved" / f"{command['id']}.json"

    _other_command, _other_policy, other_frame = fixture(
        mode="runtime_validation",
        proposal_blocked=True,
    )
    arbitrary = persist_decision_frame_archive(bridge, other_frame)
    args = SimpleNamespace(
        command=source,
        target=target,
        approved_payload=payload,
        policy=bridge / "action-policy.json",
        frame=arbitrary,
        expected_command_sha256=sha256(source),
        expected_policy_sha256=sha256(bridge / "action-policy.json"),
        expected_frame_sha256=sha256(arbitrary),
    )
    try:
        commit_command_approval.commit(args)
    except commit_command_approval.ApprovalCommitError:
        pass
    else:
        raise AssertionError("arbitrary historical --frame path was accepted")
    require(source.is_file() and not target.exists(), "arbitrary frame rejection moved the command")
    return {"arbitrary_frame_argument_rejected": True}


def case_concurrent_archive_tamper_detected(root: pathlib.Path) -> Dict[str, Any]:
    command, policy, frame = fixture(mode="runtime_validation")
    bridge = prepare_bridge(root, command, policy, frame)
    approved = build_approved_command(
        command,
        policy,
        frame,
        approved_by="local_human",
        approved_at=dt.datetime.now(dt.timezone.utc).isoformat(),
    )
    payload = bridge / "approved-payload.tmp"
    write_json(payload, approved)
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

    original_replace = commit_command_approval.os.replace
    replace_calls = 0

    def replacement(source_path: Any, target_path: Any) -> None:
        nonlocal replace_calls
        replace_calls += 1
        original_replace(source_path, target_path)
        if replace_calls == 1:
            tampered = copy.deepcopy(frame)
            tampered["source"]["state_hash"] = "sha256:" + "e" * 64
            write_json(archive, tampered)

    try:
        with mock.patch.object(commit_command_approval.os, "replace", side_effect=replacement):
            commit_command_approval.commit(args)
    except commit_command_approval.ApprovalCommitError:
        pass
    else:
        raise AssertionError("concurrent archive tamper was accepted")

    require(source.is_file(), "concurrent tamper did not restore the pending command")
    require(not target.exists(), "concurrent tamper reached the approved queue")
    require(
        not (bridge / "commands" / "approving" / source.name).exists(),
        "concurrent tamper left an approval claim",
    )
    require(not list((bridge / "commands").glob(".approval-*.lock")), "approval lock leaked")
    return {
        "concurrent_archive_tamper_rejected": True,
        "pending_restored": True,
        "replace_calls_including_rollback": replace_calls,
    }


def case_exact_policy_hash_binding(root: pathlib.Path) -> Dict[str, Any]:
    command, policy, frame = fixture(mode="runtime_validation")
    bridge = prepare_bridge(root, command, policy, frame)
    projection = frame["projections"]["policy"]
    require(projection["policy_hash"] == canonical_hash(policy), "frame omitted exact policy hash")

    changed_policy = copy.deepcopy(policy)
    changed_policy["allowed_actions"] = [command["action"], "noop"]
    require(
        len(changed_policy["allowed_actions"]) == len(policy["allowed_actions"]),
        "same-count policy fixture changed cardinality",
    )
    require(command["action"] in changed_policy["allowed_actions"], "fixture removed the command action")
    write_json(bridge / "action-policy.json", changed_policy)
    try:
        load_current_decision_context(bridge, mode="runtime_validation")
    except DecisionFrameArchiveError:
        current_context_rejected = True
    else:
        current_context_rejected = False
    require(current_context_rejected, "same-count current policy replacement matched the old frame")

    rebound = copy.deepcopy(command)
    rebound["source"]["policy_hash"] = canonical_hash(changed_policy)
    try:
        build_approved_command(
            rebound,
            changed_policy,
            frame,
            approved_by="local_human",
            approved_at=dt.datetime.now(dt.timezone.utc).isoformat(),
        )
    except ApprovalReceiptError:
        crafted_rebind_rejected = True
    else:
        crafted_rebind_rejected = False
    require(crafted_rebind_rejected, "crafted command rebound an old frame to a new same-count policy")
    return {
        "frame_policy_hash": projection["policy_hash"],
        "same_count_policy_rejected_by_current_context": current_context_rejected,
        "crafted_command_policy_rebind_rejected": crafted_rebind_rejected,
    }


def case_archive_link_and_identity_guards(root: pathlib.Path) -> Dict[str, Any]:
    frame = _unique_frame(9001)
    path = persist_decision_frame_archive(root, frame)
    alias = path.parent / "hardlink-alias.tmp"
    os.link(path, alias)
    try:
        load_archived_decision_frame(root, frame["frame_id"])
    except DecisionFrameArchiveError:
        hardlink_rejected = True
    else:
        hardlink_rejected = False
    require(hardlink_rejected, "multi-link decision frame archive was accepted")
    alias.unlink()
    load_archived_decision_frame(root, frame["frame_id"])

    replacement = path.parent / "identity-replacement.tmp"
    original = path.parent / "identity-original.tmp"
    replacement.write_bytes(path.read_bytes())
    original_open = frame_archive.os.open
    swapped = False

    def swap_before_open(value: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal swapped
        candidate = pathlib.Path(value)
        if not swapped and candidate == path:
            swapped = True
            os.replace(path, original)
            os.replace(replacement, path)
        return original_open(value, flags, *args, **kwargs)

    try:
        with mock.patch.object(frame_archive.os, "open", side_effect=swap_before_open):
            load_archived_decision_frame(root, frame["frame_id"])
    except DecisionFrameArchiveError:
        identity_swap_rejected = True
    else:
        identity_swap_rejected = False
    finally:
        path.unlink(missing_ok=True)
        os.replace(original, path)
        replacement.unlink(missing_ok=True)
    require(swapped and identity_swap_rejected, "archive path identity swap was accepted")

    original_lstat = pathlib.Path.lstat
    path_lstat_calls = 0

    def drift_after_open(candidate: pathlib.Path, *args: Any, **kwargs: Any) -> Any:
        nonlocal path_lstat_calls
        metadata = original_lstat(candidate, *args, **kwargs)
        if candidate == path:
            path_lstat_calls += 1
            if path_lstat_calls == 2:
                return SimpleNamespace(
                    st_mode=metadata.st_mode,
                    st_nlink=metadata.st_nlink,
                    st_dev=metadata.st_dev,
                    st_ino=int(metadata.st_ino) + 1,
                )
        return metadata

    try:
        with mock.patch.object(pathlib.Path, "lstat", new=drift_after_open):
            load_archived_decision_frame(root, frame["frame_id"])
    except DecisionFrameArchiveError:
        post_open_name_swap_rejected = True
    else:
        post_open_name_swap_rejected = False
    require(
        path_lstat_calls >= 2 and post_open_name_swap_rejected,
        "archive path name swap after descriptor open was accepted",
    )

    residual_frame = _unique_frame(9002)
    original_unlink = pathlib.Path.unlink

    def retain_archive_temp(candidate: pathlib.Path, *args: Any, **kwargs: Any) -> None:
        if candidate.parent == path.parent and candidate.suffix == ".tmp":
            raise PermissionError("simulated residual hard-link alias")
        original_unlink(candidate, *args, **kwargs)

    try:
        with mock.patch.object(pathlib.Path, "unlink", new=retain_archive_temp):
            persist_decision_frame_archive(root, residual_frame)
    except DecisionFrameArchiveError:
        residual_alias_rejected = True
    else:
        residual_alias_rejected = False
    require(residual_alias_rejected, "residual archive hard-link alias did not fail closed")
    residual_path = decision_frame_archive_path(root, residual_frame["frame_id"])
    try:
        load_archived_decision_frame(root, residual_frame["frame_id"])
    except DecisionFrameArchiveError:
        residual_target_blocked = True
    else:
        residual_target_blocked = False
    require(residual_target_blocked, "archive with a residual hard-link alias remained readable")
    residual_aliases = [candidate for candidate in path.parent.glob("*.tmp") if candidate != path]
    for candidate in residual_aliases:
        candidate.unlink(missing_ok=True)
    load_archived_decision_frame(root, residual_frame["frame_id"])
    return {
        "hardlink_rejected": hardlink_rejected,
        "identity_swap_rejected": identity_swap_rejected,
        "post_open_name_swap_rejected": post_open_name_swap_rejected,
        "residual_alias_rejected": residual_alias_rejected,
        "residual_target_blocked_until_cleanup": residual_target_blocked,
    }


def _unique_frame(index: int) -> Dict[str, Any]:
    values = healthy_inputs(actions_enabled=True, suggestions_enabled=True)
    values["state"]["cycle"] = 1000 + index
    rebind_state_hash(values)
    return build(values)


def case_pruning_is_bounded_and_preserves_refs(root: pathlib.Path) -> Dict[str, Any]:
    frames = [_unique_frame(index) for index in range(10)]
    for index, frame in enumerate(frames):
        path = persist_decision_frame_archive(root, frame)
        timestamp = time.time() - (100 - index)
        os.utime(path, (timestamp, timestamp))

    stages = ("pending", "approving", "approved", "executing", "confirming")
    for stage, frame in zip(stages, frames[:5]):
        write_json(
            root / "commands" / stage / f"cmd-retain-{stage}.json",
            {"id": f"cmd-retain-{stage}", "source_frame_id": frame["frame_id"]},
        )

    current = frames[5]
    report = prune_decision_frame_archives(
        root,
        current_frame_id=current["frame_id"],
        max_unreferenced=2,
    )
    for frame in frames[:6]:
        require(
            decision_frame_archive_path(root, frame["frame_id"]).is_file(),
            "pruning removed a referenced or current frame",
        )
    remaining = list((root / "decision-frames").glob("sha256-*.json"))
    require(len(remaining) == 8, f"retention expected 8 frames, found {len(remaining)}")
    require(report["pruned_count"] == 2, "retention did not prune the expected old frames")
    return {
        "active_references_preserved": len(stages),
        "current_preserved": True,
        "unreferenced_limit": 2,
        "remaining_count": len(remaining),
        "pruned_count": report["pruned_count"],
    }


def case_runtime_prepare_binds_exact_context(root: pathlib.Path) -> Dict[str, Any]:
    snapshot = colony_state()
    snapshot["game"]["paused"] = False

    def prepare_args(bridge: pathlib.Path) -> argparse.Namespace:
        return argparse.Namespace(
            bridge_dir=str(bridge),
            action="set_paused",
            paused="true",
            tech=None,
            duplicant_instance_id=None,
            chore_group="Dig",
            priority=None,
            cells=None,
            material_tags=None,
            path_id=None,
        )

    clean = root / "clean"
    write_live_state(clean, snapshot)
    with contextlib.redirect_stdout(io.StringIO()):
        result = verify_runtime.prepare_command(prepare_args(clean))
    require(result == 0, "runtime prepare returned failure")
    _path, command = only_command(clean, "pending")
    frame = json.loads((clean / "decision-frame.json").read_text(encoding="utf-8-sig"))
    state = json.loads((clean / "latest_state.json").read_text(encoding="utf-8-sig"))
    policy = json.loads((clean / "action-policy.json").read_text(encoding="utf-8-sig"))
    require(command["source_frame_id"] == frame["frame_id"], "runtime frame binding drifted")
    require(
        command["source"]["policy_hash"] == canonical_hash(policy),
        "runtime policy binding drifted",
    )
    require(command["source_snapshot_id"] == state["snapshot_id"], "runtime snapshot binding drifted")
    require(
        decision_frame_archive_path(clean, command["source_frame_id"]).is_file(),
        "runtime prepare accepted a frame without an archive",
    )

    suggestions_disabled = root / "suggestions-disabled"
    write_live_state(suggestions_disabled, snapshot, suggestions_enabled=False)
    with contextlib.redirect_stdout(io.StringIO()):
        disabled_result = verify_runtime.prepare_command(prepare_args(suggestions_disabled))
    require(disabled_result == 0, "SUGGESTIONS_DISABLED operator frame was rejected")
    _disabled_path, disabled_command = only_command(suggestions_disabled, "pending")
    disabled_frame = json.loads(
        (suggestions_disabled / "decision-frame.json").read_text(encoding="utf-8-sig")
    )
    require(
        disabled_frame["readiness"]["propose"]
        == {"ready": False, "blockers": ["SUGGESTIONS_DISABLED"]},
        "suggestions-disabled fixture has unexpected blockers",
    )
    require(
        disabled_command["source_frame_id"] == disabled_frame["frame_id"],
        "suggestions-disabled runtime command drifted",
    )

    pipeline_blocked = root / "pipeline-blocked"
    write_live_state(
        pipeline_blocked,
        snapshot,
        proposal_blockers=["mutation_approved_in_flight:cmd-existing"],
    )
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            verify_runtime.prepare_command(prepare_args(pipeline_blocked))
    except RuntimeError:
        pass
    else:
        raise AssertionError("pipeline-blocked runtime frame created a command")
    require(
        not list((pipeline_blocked / "commands" / "pending").glob("*.json")),
        "pipeline-blocked runtime prepare left a pending command",
    )
    return {
        "source_frame_id": command["source_frame_id"],
        "policy_hash": command["source"]["policy_hash"],
        "snapshot_id": command["source_snapshot_id"],
        "exact_archive_exists": True,
        "suggestions_disabled_only_accepted": True,
        "pipeline_blocker_rejected": True,
    }


def case_planned_binding_positive_and_negative(root: pathlib.Path) -> Dict[str, Any]:
    positive = root / "positive"
    initial = colony_state()
    write_live_state(positive, initial)
    plan = create_colony_plan(positive, priority_definition(initial, [4]))
    advance_colony_plan(positive, str(plan["plan_id"]))
    _pending_path, pending = only_command(positive, "pending")
    policy = json.loads((positive / "action-policy.json").read_text(encoding="utf-8-sig"))
    require(pending["source"]["policy_hash"] == canonical_hash(policy), "planned policy hash missing")
    run_approval(positive, pending["id"], expect_success=True)

    negative = root / "missing-policy-binding"
    write_live_state(negative, initial)
    plan = create_colony_plan(negative, priority_definition(initial, [4]))
    advance_colony_plan(negative, str(plan["plan_id"]))
    pending_path, pending = only_command(negative, "pending")
    pending["source"].pop("policy_hash", None)
    write_json(pending_path, pending)
    run_approval(negative, pending["id"], expect_success=False)

    stale = root / "stale-policy-binding"
    write_live_state(stale, initial)
    plan = create_colony_plan(stale, priority_definition(initial, [4]))
    advance_colony_plan(stale, str(plan["plan_id"]))
    _stale_path, stale_pending = only_command(stale, "pending")
    stale_policy = json.loads((stale / "action-policy.json").read_text(encoding="utf-8-sig"))
    stale_policy["max_pending_commands"] = int(stale_policy["max_pending_commands"]) + 1
    write_json(stale / "action-policy.json", stale_policy)
    run_approval(stale, stale_pending["id"], expect_success=False)
    return {
        "planned_exact_binding_approved": True,
        "planned_missing_policy_binding_rejected": True,
        "planned_stale_policy_binding_rejected": True,
    }


def run_case(
    name: str,
    function: Callable[[pathlib.Path], Dict[str, Any]],
    root: pathlib.Path,
) -> Dict[str, Any]:
    started = time.perf_counter()
    try:
        details = function(root / name)
        return {
            "name": name,
            "status": "passed",
            "duration_seconds": round(time.perf_counter() - started, 3),
            "details": details,
        }
    except Exception as exc:
        return {
            "name": name,
            "status": "failed",
            "classification": "behavior_fail",
            "duration_seconds": round(time.perf_counter() - started, 3),
            "error": f"{type(exc).__name__}: {exc}",
        }


def main() -> int:
    cases: tuple[tuple[str, Callable[[pathlib.Path], Dict[str, Any]]], ...] = (
        ("root_advance_uses_exact_archive", case_root_advance_uses_exact_archive),
        ("archive_and_binding_rejections", case_archive_and_binding_rejections),
        ("arbitrary_frame_argument_rejected", case_arbitrary_frame_argument_rejected),
        ("concurrent_archive_tamper_detected", case_concurrent_archive_tamper_detected),
        ("exact_policy_hash_binding", case_exact_policy_hash_binding),
        ("archive_link_and_identity_guards", case_archive_link_and_identity_guards),
        ("pruning_is_bounded_and_preserves_refs", case_pruning_is_bounded_and_preserves_refs),
        ("runtime_prepare_binds_exact_context", case_runtime_prepare_binds_exact_context),
        ("planned_binding_positive_and_negative", case_planned_binding_positive_and_negative),
    )
    with tempfile.TemporaryDirectory(prefix="oni-immutable-frames-") as temporary:
        root = pathlib.Path(temporary)
        results = [run_case(name, function, root) for name, function in cases]

    passed = all(result["status"] == "passed" for result in results)
    report = {
        "schema": "oni.immutable-decision-frame-verification.v1",
        "generated_at": dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds"),
        "status": "passed" if passed else "failed",
        "classification": None if passed else "behavior_fail",
        "case_count": len(results),
        "cases": results,
    }
    report_path = PROJECT_ROOT / "reports" / "immutable-decision-frame-verification.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    for result in results:
        detail = result.get("error") or json.dumps(result.get("details"), sort_keys=True)
        print(f"[{result['status'].upper()}] {result['name']}: {detail}")
    print(f"Report: {report_path}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
