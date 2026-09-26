#!/usr/bin/env python3
"""Offline verification for source-frame-bound human approval receipts."""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import pathlib
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Dict, Mapping


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.approval_receipt import (  # noqa: E402
    APPROVAL_RECEIPT_SCHEMA,
    ApprovalReceiptError,
    canonical_hash,
    validate_approval_receipt,
)
from agent import rules as agent_rules  # noqa: E402
from agent.command_queue import build_command  # noqa: E402
from agent.decision_frame_archive import decision_frame_archive_path  # noqa: E402
from agent.field_contracts import (  # noqa: E402
    ALERTS_CANONICAL_CONTRACT_ID,
    GAME_PAUSED_CONTRACT_ID,
    contract_quality_fixture,
)
from agent.incidents import IncidentTracker  # noqa: E402
from agent.state_quality import canonical_state_hash  # noqa: E402
from scripts.verify_decision_frame import build, command_pipeline, healthy_inputs  # noqa: E402


APPROVE_SCRIPT = PROJECT_ROOT / "scripts" / "approve-command.ps1"
MUTATING_ACTION = "set_paused"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def write_json(path: pathlib.Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def rehash_frame(frame: Dict[str, Any]) -> None:
    payload = {key: value for key, value in frame.items() if key != "frame_id"}
    frame["frame_id"] = "sha256:" + hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def rebind_state_hash(values: Dict[str, Dict[str, Any]]) -> None:
    state_hash = canonical_state_hash(values["state"])
    for name in ("health", "advice", "incidents", "plan", "strategy", "policy"):
        values[name]["source"]["state_hash"] = state_hash
    values["health"]["latest_state_hash"] = state_hash
    values["strategy"]["state_hash"] = state_hash
    for name, child_key in (("advice", "items"), ("incidents", "items"), ("plan", "plan")):
        for item in values[name][child_key]:
            item["state_hash"] = state_hash


def fixture(
    *,
    blocked: bool = False,
    proposal_blocked: bool = False,
    mode: str = "suggested",
    pipeline_blocked: bool = False,
    replay_mode: bool = False,
    plan_invalid: bool = False,
    policy_invalid: bool = False,
) -> tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    values = healthy_inputs(actions_enabled=True, suggestions_enabled=not proposal_blocked)
    values["policy"]["allowed_actions"] = [MUTATING_ACTION, "focus_note"]
    values["policy"].update(
        {
            "command_ttl_seconds": 300,
            "max_pending_commands": 20,
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
    )
    if pipeline_blocked:
        values["health"]["command_pipeline"] = command_pipeline(
            proposal_blockers=["mutation_execution_claimed:cmd-existing-mutation"]
        )
    if replay_mode:
        values["health"]["replay_mode"] = True
    if plan_invalid:
        values["plan"]["schema"] = "oni.plan.invalid"
    if policy_invalid:
        values["policy"]["schema"] = "oni.action-policy.invalid"

    signal: Dict[str, Any] | None = None
    tracked_incident: Dict[str, Any] | None = None
    if mode == "suggested":
        # A suggested pause is authorizing only when its DecisionFrame carries
        # an exact, current survival-episode witness and arbitration
        # certificate.  Build that evidence through the same IncidentTracker
        # path used by the Bridge instead of synthesizing an unpause envelope.
        values["state"]["game"]["paused"] = False
        values["state"]["alerts"] = ["suffocation"]
        values["state"]["bridge"].update(
            {
                "stale": False,
                "replay_mode": False,
                "trust_ready": True,
                "trust_blockers": [],
            }
        )
        values["state"]["quality"].update(
            {
                "alerts": contract_quality_fixture(ALERTS_CANONICAL_CONTRACT_ID),
                "game.paused": contract_quality_fixture(GAME_PAUSED_CONTRACT_ID),
            }
        )
        signal = next(
            item
            for item in agent_rules.evaluate_signals(values["state"])
            if item.get("incident_id") == "suffocation"
            and item.get("state") == "active"
        )
        tracker = IncidentTracker()
        tracker.update(values["state"], [signal])
        tracked_incident = copy.deepcopy(tracker.incidents["suffocation"])
        require(
            tracked_incident.get("status") == "open",
            "suggested approval fixture did not open its immediate hazard",
        )
        values["incidents"]["items"] = [copy.deepcopy(tracked_incident)]
        values["advice"]["items"] = [
            {
                "incident_id": signal["incident_id"],
                "risk": signal["risk"],
                "incident_opened_at": tracked_incident["first_seen"],
                "source_snapshot_id": values["state"]["snapshot_id"],
                "state_hash": "deferred",
                "title": signal["title"],
                "reason": signal["reason"],
                "evidence": copy.deepcopy(signal["evidence"]),
                "command": copy.deepcopy(signal["command"]),
            }
        ]
    if blocked:
        values["state"]["bridge"]["stale"] = True
        values["health"]["status"] = "stale"
    rebind_state_hash(values)
    frame = build(values)
    if mode == "suggested":
        require(signal is not None and tracked_incident is not None, "missing survival fixture")
        hint = copy.deepcopy(signal["command"])
        advice_item = {
            "incident_id": signal["incident_id"],
            "incident_opened_at": tracked_incident["first_seen"],
            "risk": signal["risk"],
            "reason": signal["reason"],
            "evidence": copy.deepcopy(signal["evidence"]),
            "title": signal["title"],
        }
    else:
        hint = {
            "action": MUTATING_ACTION,
            "args": {"paused": False},
            "preconditions": [{"path": "game.paused", "op": "eq", "value": True}],
            "expected_effects": [{"path": "game.paused", "op": "eq", "value": False}],
        }
        advice_item = {
            "incident_id": "approval_frame_binding",
            "incident_opened_at": "2026-08-30T09:00:00+00:00",
            "risk": "medium",
            "reason": "offline approval binding fixture",
            "evidence": ["trusted decision frame"],
            "title": "Approval frame binding",
        }
    command = build_command(
        values["state"],
        advice_item,
        hint,
        ttl_seconds=300,
        source_frame_id=frame["frame_id"],
    )
    if mode != "suggested":
        command["mode"] = mode
        command["source_incident_id"] = None
        command["source"] = {
            "kind": "runtime_validation" if mode == "runtime_validation" else "colony_plan"
        }
    policy = {
        key: copy.deepcopy(value)
        for key, value in values["policy"].items()
        if key != "source"
    }
    source = command.get("source") if isinstance(command.get("source"), dict) else {}
    source["policy_hash"] = canonical_hash(policy)
    command["source"] = source
    return command, policy, frame


def prepare_bridge(
    root: pathlib.Path,
    command: Mapping[str, Any],
    policy: Mapping[str, Any],
    frame: Mapping[str, Any] | None,
) -> pathlib.Path:
    bridge = root
    write_json(bridge / "commands" / "pending" / f"{command['id']}.json", command)
    write_json(bridge / "action-policy.json", policy)
    if frame is not None:
        write_json(bridge / "decision-frame.json", frame)
        write_json(
            decision_frame_archive_path(bridge, command["source_frame_id"]),
            frame,
        )
    return bridge


def run_approval(
    bridge: pathlib.Path,
    command_id: str,
    *,
    expect_success: bool,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [
            "pwsh",
            "-NoLogo",
            "-NoProfile",
            "-File",
            str(APPROVE_SCRIPT),
            "-BridgeDir",
            str(bridge),
            "-CommandId",
            command_id,
        ],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        # PowerShell error text uses the console code page while PYTHONUTF8=1
        # decodes as UTF-8; a decode failure left stderr=None.
        errors="replace",
        timeout=30,
        check=False,
    )
    if expect_success:
        require(
            result.returncode == 0,
            f"approval unexpectedly failed: {result.stdout.strip()} {result.stderr.strip()}",
        )
    else:
        require(result.returncode != 0, "invalid approval unexpectedly succeeded")
        require(
            (bridge / "commands" / "pending" / f"{command_id}.json").is_file(),
            "rejected command did not remain pending",
        )
        require(
            not (bridge / "commands" / "approved" / f"{command_id}.json").exists(),
            "rejected command entered the approved queue",
        )
    return result


def read_approved(bridge: pathlib.Path, command_id: str) -> Dict[str, Any]:
    path = bridge / "commands" / "approved" / f"{command_id}.json"
    return json.loads(path.read_text(encoding="utf-8-sig"))


def expect_receipt_rejected(
    command: Mapping[str, Any], policy: Mapping[str, Any], frame: Mapping[str, Any]
) -> str:
    try:
        validate_approval_receipt(command, policy, frame)
    except ApprovalReceiptError as exc:
        return str(exc)
    raise AssertionError("tampered approval receipt inputs were accepted")


def case_valid_bound_approval(root: pathlib.Path) -> Dict[str, Any]:
    command, policy, frame = fixture()
    bridge = prepare_bridge(root, command, policy, frame)
    run_approval(bridge, command["id"], expect_success=True)
    approved = read_approved(bridge, command["id"])
    receipt = approved.get("approval_receipt")
    require(isinstance(receipt, dict), "approved mutation has no approval_receipt")
    require(receipt.get("schema") == APPROVAL_RECEIPT_SCHEMA, "receipt schema drifted")
    require(receipt.get("source_frame_id") == frame["frame_id"], "receipt frame binding drifted")
    require(receipt.get("policy_hash") == canonical_hash(policy), "receipt policy binding drifted")
    require(approved.get("approved_by") == receipt.get("approved_by"), "approved_by is not bound")
    require(approved.get("approved_at") == receipt.get("approved_at"), "approved_at is not bound")
    validate_approval_receipt(approved, policy, frame)
    require(
        not (bridge / "commands" / "pending" / f"{command['id']}.json").exists(),
        "approved command remained pending",
    )
    return {
        "receipt_schema": receipt["schema"],
        "command_intent_hash": receipt["command_intent_hash"],
        "source_frame_id": receipt["source_frame_id"],
        "policy_hash": receipt["policy_hash"],
        "receipt_hash": receipt["receipt_hash"],
    }


def case_missing_frame_rejected(root: pathlib.Path) -> Dict[str, Any]:
    command, policy, _frame = fixture()
    bridge = prepare_bridge(root, command, policy, None)
    result = run_approval(bridge, command["id"], expect_success=False)
    return {"rejected": True, "stderr_has_frame": "decision-frame" in result.stderr.lower()}


def case_tampered_frame_rejected(root: pathlib.Path) -> Dict[str, Any]:
    command, policy, frame = fixture()
    frame["source"]["state_hash"] = "sha256:" + "f" * 64
    bridge = prepare_bridge(root, command, policy, frame)
    run_approval(bridge, command["id"], expect_success=False)
    return {"digest_tamper_rejected": True}


def case_blocked_frame_rejected(root: pathlib.Path) -> Dict[str, Any]:
    command, policy, frame = fixture(blocked=True)
    require(frame["trust"]["status"] == "blocked", "blocked fixture is not blocked")
    bridge = prepare_bridge(root, command, policy, frame)
    run_approval(bridge, command["id"], expect_success=False)
    return {"trust": frame["trust"]["status"], "blocked_rejected": True}


def case_suggested_suggestions_disabled_rejected(root: pathlib.Path) -> Dict[str, Any]:
    command, policy, frame = fixture(proposal_blocked=True)
    require(frame["trust"]["status"] == "trusted", "proposal-blocked fixture lost source trust")
    require(frame["readiness"]["propose"]["ready"] is False, "proposal-blocked fixture is ready")
    require(
        frame["readiness"]["propose"]["blockers"] == ["SUGGESTIONS_DISABLED"],
        "suggested fixture has an unexpected proposal blocker",
    )
    bridge = prepare_bridge(root, command, policy, frame)
    run_approval(bridge, command["id"], expect_success=False)
    return {
        "mode": "suggested",
        "trusted": True,
        "propose_blockers": ["SUGGESTIONS_DISABLED"],
        "approval_rejected": True,
    }


def case_operator_suggestions_disabled_accepted(root: pathlib.Path) -> Dict[str, Any]:
    accepted: Dict[str, Any] = {}
    for mode in ("runtime_validation", "planned"):
        command, policy, frame = fixture(proposal_blocked=True, mode=mode)
        require(frame["trust"]["status"] == "trusted", f"{mode} fixture lost source trust")
        require(
            frame["readiness"]["propose"]
            == {"ready": False, "blockers": ["SUGGESTIONS_DISABLED"]},
            f"{mode} fixture has an unexpected proposal gate",
        )
        bridge = prepare_bridge(root / mode, command, policy, frame)
        run_approval(bridge, command["id"], expect_success=True)
        approved = read_approved(bridge, command["id"])
        validate_approval_receipt(approved, policy, frame)
        accepted[mode] = {
            "receipt_bound": isinstance(approved.get("approval_receipt"), dict),
            "propose_blockers": frame["readiness"]["propose"]["blockers"],
        }
    return {"accepted": accepted}


def case_operator_pipeline_blocked_rejected(root: pathlib.Path) -> Dict[str, Any]:
    command, policy, frame = fixture(
        proposal_blocked=True,
        mode="runtime_validation",
        pipeline_blocked=True,
    )
    require(frame["trust"]["status"] == "trusted", "pipeline fixture lost state trust")
    expected = ["COMMAND_PIPELINE_MUTATION_BLOCKED", "SUGGESTIONS_DISABLED"]
    require(frame["readiness"]["propose"]["blockers"] == expected, "pipeline blocker was not projected")
    bridge = prepare_bridge(root, command, policy, frame)
    run_approval(bridge, command["id"], expect_success=False)
    return {"mode": command["mode"], "propose_blockers": expected, "approval_rejected": True}


def case_operator_stale_frame_rejected(root: pathlib.Path) -> Dict[str, Any]:
    command, policy, frame = fixture(
        blocked=True,
        proposal_blocked=True,
        mode="runtime_validation",
    )
    require(frame["trust"]["status"] == "blocked", "stale operator fixture remained trusted")
    bridge = prepare_bridge(root, command, policy, frame)
    run_approval(bridge, command["id"], expect_success=False)
    return {
        "trust": frame["trust"]["status"],
        "propose_blockers": frame["readiness"]["propose"]["blockers"],
        "approval_rejected": True,
    }


def case_operator_replay_frame_rejected(root: pathlib.Path) -> Dict[str, Any]:
    command, policy, frame = fixture(
        proposal_blocked=True,
        mode="planned",
        replay_mode=True,
    )
    expected = ["REPLAY_MODE", "SUGGESTIONS_DISABLED"]
    require(frame["trust"]["status"] == "trusted", "replay fixture unexpectedly lost state trust")
    require(frame["readiness"]["propose"]["blockers"] == expected, "replay blocker was not projected")
    bridge = prepare_bridge(root, command, policy, frame)
    run_approval(bridge, command["id"], expect_success=False)
    return {"mode": command["mode"], "propose_blockers": expected, "approval_rejected": True}


def case_operator_extra_blocker_rejected(root: pathlib.Path) -> Dict[str, Any]:
    command, policy, frame = fixture(
        proposal_blocked=True,
        mode="runtime_validation",
        plan_invalid=True,
    )
    expected = ["PLAN_INVALID", "SUGGESTIONS_DISABLED"]
    require(frame["trust"]["status"] == "trusted", "plan-invalid fixture unexpectedly lost state trust")
    require(frame["readiness"]["propose"]["blockers"] == expected, "extra blocker fixture drifted")
    bridge = prepare_bridge(root, command, policy, frame)
    run_approval(bridge, command["id"], expect_success=False)
    return {"mode": command["mode"], "propose_blockers": expected, "approval_rejected": True}


def case_operator_policy_blocker_rejected(root: pathlib.Path) -> Dict[str, Any]:
    command, policy, frame = fixture(
        proposal_blocked=True,
        mode="runtime_validation",
        policy_invalid=True,
    )
    expected = ["POLICY_INVALID"]
    require(frame["trust"]["status"] == "trusted", "policy-invalid fixture unexpectedly lost state trust")
    require(frame["readiness"]["propose"]["blockers"] == expected, "policy blocker was not projected")
    bridge = prepare_bridge(root, command, policy, frame)
    run_approval(bridge, command["id"], expect_success=False)
    return {"mode": command["mode"], "propose_blockers": expected, "approval_rejected": True}


def case_unsupported_mutating_mode_rejected(root: pathlib.Path) -> Dict[str, Any]:
    command, policy, frame = fixture(mode="manual_override")
    require(frame["readiness"]["propose"] == {"ready": True, "blockers": []}, "clean frame is blocked")
    bridge = prepare_bridge(root, command, policy, frame)
    run_approval(bridge, command["id"], expect_success=False)
    return {"mode": command["mode"], "clean_frame": True, "approval_rejected": True}


def case_stale_source_rejected(root: pathlib.Path) -> Dict[str, Any]:
    command, policy, frame = fixture()
    command["source_snapshot_id"] = f"{command['session_id']}:499"
    bridge = prepare_bridge(root, command, policy, frame)
    run_approval(bridge, command["id"], expect_success=False)
    return {"source_snapshot_mismatch_rejected": True}


def case_missing_survival_certificate_rejected(root: pathlib.Path) -> Dict[str, Any]:
    command, policy, frame = fixture()
    frame["projections"]["incidents"].pop("survival_emergency", None)
    rehash_frame(frame)
    command["source_frame_id"] = frame["frame_id"]
    bridge = prepare_bridge(root, command, policy, frame)
    result = run_approval(bridge, command["id"], expect_success=False)
    combined = (result.stdout + result.stderr).lower()
    require(
        "survival certificate" in combined,
        "missing certificate was rejected for an unexpected reason",
    )
    return {"legacy_uncertified_suggested_pause_rejected": True}


def case_nonmutating_compatibility(root: pathlib.Path) -> Dict[str, Any]:
    command, policy, _frame = fixture()
    command["action"] = "focus_note"
    command["args"] = {"tag": "approval-frame-binding"}
    command["preconditions"] = []
    command["expected_effects"] = []
    command["source_frame_id"] = None
    policy["allowed_actions"] = ["focus_note"]
    bridge = prepare_bridge(root, command, policy, None)
    run_approval(bridge, command["id"], expect_success=True)
    approved = read_approved(bridge, command["id"])
    require("approval_receipt" not in approved, "non-mutating command received a mutation receipt")
    require(approved.get("approved_by") == "local_human", "non-mutating audit field drifted")
    return {"approved_without_frame": True, "approval_receipt_required": False}


def approved_fixture(root: pathlib.Path) -> tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    command, policy, frame = fixture()
    bridge = prepare_bridge(root, command, policy, frame)
    run_approval(bridge, command["id"], expect_success=True)
    return read_approved(bridge, command["id"]), policy, frame


def case_postapproval_command_tamper_rejected(root: pathlib.Path) -> Dict[str, Any]:
    approved, policy, frame = approved_fixture(root)
    approved["expires_at"] = "2099-01-01T00:00:00+00:00"
    error = expect_receipt_rejected(approved, policy, frame)
    require("command changed" in error, f"unexpected rejection: {error}")
    return {"command_intent_tamper_rejected": True}


def case_postapproval_policy_change_rejected(root: pathlib.Path) -> Dict[str, Any]:
    approved, policy, frame = approved_fixture(root)
    policy["max_pending_commands"] = 19
    error = expect_receipt_rejected(approved, policy, frame)
    require(
        "policy changed" in error or "does not bind the current action policy" in error,
        f"unexpected rejection: {error}",
    )
    return {"policy_change_rejected": True}


def case_postapproval_receipt_tamper_rejected(root: pathlib.Path) -> Dict[str, Any]:
    approved, policy, frame = approved_fixture(root)
    approved["approval_receipt"]["approved_by"] = "someone_else"
    error = expect_receipt_rejected(approved, policy, frame)
    require("approved_by" in error or "tampered" in error, f"unexpected rejection: {error}")
    return {"receipt_tamper_rejected": True}


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
        ("valid_bound_approval", case_valid_bound_approval),
        ("missing_frame_rejected", case_missing_frame_rejected),
        ("tampered_frame_rejected", case_tampered_frame_rejected),
        ("blocked_frame_rejected", case_blocked_frame_rejected),
        ("suggested_suggestions_disabled_rejected", case_suggested_suggestions_disabled_rejected),
        ("operator_suggestions_disabled_accepted", case_operator_suggestions_disabled_accepted),
        ("operator_pipeline_blocked_rejected", case_operator_pipeline_blocked_rejected),
        ("operator_stale_frame_rejected", case_operator_stale_frame_rejected),
        ("operator_replay_frame_rejected", case_operator_replay_frame_rejected),
        ("operator_extra_blocker_rejected", case_operator_extra_blocker_rejected),
        ("operator_policy_blocker_rejected", case_operator_policy_blocker_rejected),
        ("unsupported_mutating_mode_rejected", case_unsupported_mutating_mode_rejected),
        ("stale_source_rejected", case_stale_source_rejected),
        ("missing_survival_certificate_rejected", case_missing_survival_certificate_rejected),
        ("nonmutating_compatibility", case_nonmutating_compatibility),
        ("postapproval_command_tamper_rejected", case_postapproval_command_tamper_rejected),
        ("postapproval_policy_change_rejected", case_postapproval_policy_change_rejected),
        ("postapproval_receipt_tamper_rejected", case_postapproval_receipt_tamper_rejected),
    )
    with tempfile.TemporaryDirectory(prefix="oni-approval-frame-") as temporary:
        root = pathlib.Path(temporary)
        results = [run_case(name, function, root) for name, function in cases]

    passed = all(result["status"] == "passed" for result in results)
    report = {
        "schema": "oni.approval-frame-binding-verification.v1",
        "generated_at": dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds"),
        "status": "passed" if passed else "failed",
        "classification": None if passed else "behavior_fail",
        "case_count": len(results),
        "cases": results,
    }
    report_path = PROJECT_ROOT / "reports" / "approval-frame-binding.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    for result in results:
        detail = result.get("error") or json.dumps(result.get("details"), ensure_ascii=False, sort_keys=True)
        print(f"[{result['status'].upper()}] {result['name']}: {detail}")
    print(f"Report: {report_path}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
