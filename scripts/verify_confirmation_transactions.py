#!/usr/bin/env python3
"""Offline crash-recovery checks for Bridge confirmation transactions."""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import pathlib
import sys
import tempfile
from typing import Any, Callable, Dict


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent import action_outcomes as action_outcomes_module
from agent.action_outcomes import (
    ActionOutcomeError,
    build_action_outcomes,
    recover_staged_confirmed_outcomes,
    stage_confirmed_outcomes_commit,
)
from agent.colony_plan import advance_colony_plan, create_colony_plan
from agent.command_queue import ensure_command_dirs, write_json_atomic
from scripts import verify_action_outcomes as outcome_fixture
from scripts import verify_colony_plan as plan_fixture


class SimulatedCrash(RuntimeError):
    pass


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="milliseconds")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def read_json(path: pathlib.Path) -> Dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    require(isinstance(value, dict), f"{path} is not a JSON object")
    return value


def published_fixture(
    root: pathlib.Path,
    command_id: str,
    *,
    publish_session: bool = True,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    _, witness, command = outcome_fixture.prepare(root, command_id)
    document = build_action_outcomes(root, witness, outcome_fixture.health_for(witness))
    outcome = outcome_fixture.assert_status(document, outcome_fixture.CONFIRMATION_CONFIRMED)
    write_json_atomic(root / "action-outcomes.json", document)
    if publish_session:
        session_path = root / "sessions" / str(document["source"]["session_id"]) / "action-outcomes.json"
        write_json_atomic(session_path, document)
    return document, outcome


def command_locations(root: pathlib.Path, command_id: str) -> Dict[str, list[pathlib.Path]]:
    matches: Dict[str, list[pathlib.Path]] = {}
    for name, directory in ensure_command_dirs(root).items():
        paths = []
        for path in directory.glob("*.json"):
            try:
                value = read_json(path)
            except (OSError, json.JSONDecodeError, UnicodeError):
                continue
            if value.get("id") == command_id:
                paths.append(path)
        if paths:
            matches[name] = paths
    return matches


def case_requires_two_identical_publications(root: pathlib.Path) -> Dict[str, Any]:
    command_id = "cmd-transaction-two-publications"
    document, _ = published_fixture(root, command_id, publish_session=False)
    try:
        stage_confirmed_outcomes_commit(root, document)
    except ActionOutcomeError as exc:
        missing_session_error = str(exc)
    else:
        raise AssertionError("root-only certificate publication staged a command commit")
    require(recover_staged_confirmed_outcomes(root) == [], "unstaged root-only publication committed")
    require(set(command_locations(root, command_id)) == {"confirming"}, "root-only publication moved command")

    mismatch_root = root / "mismatched-session"
    document, _ = published_fixture(mismatch_root, command_id + "-mismatch")
    _, witness, _ = outcome_fixture.fixture(command_id + "-mismatch")
    replay_document = build_action_outcomes(
        mismatch_root,
        witness,
        outcome_fixture.health_for(witness),
        replay_mode=True,
    )
    session_path = (
        mismatch_root
        / "sessions"
        / str(document["source"]["session_id"])
        / "action-outcomes.json"
    )
    write_json_atomic(session_path, replay_document)
    try:
        stage_confirmed_outcomes_commit(mismatch_root, document)
    except ActionOutcomeError as exc:
        mismatch_error = str(exc)
    else:
        raise AssertionError("different valid root/session certificates staged a command commit")
    require(
        set(command_locations(mismatch_root, command_id + "-mismatch")) == {"confirming"},
        "mismatched publications moved command",
    )
    return {
        "missing_session": missing_session_error,
        "mismatched_session": mismatch_error,
        "command_location": "confirming",
    }


def case_recovers_prepared_confirming(root: pathlib.Path) -> Dict[str, Any]:
    command_id = "cmd-transaction-prepared-crash"
    document, outcome = published_fixture(root, command_id)
    marker = stage_confirmed_outcomes_commit(root, document)
    original_rename = action_outcomes_module._rename_with_retry

    def crash_before_rename(source: pathlib.Path, target: pathlib.Path) -> None:
        raise SimulatedCrash(f"prepared {source.name} before rename to {target.parent.name}")

    action_outcomes_module._rename_with_retry = crash_before_rename
    try:
        try:
            recover_staged_confirmed_outcomes(root)
        except SimulatedCrash:
            pass
        else:
            raise AssertionError("injected crash did not interrupt the confirming-to-done rename")
    finally:
        action_outcomes_module._rename_with_retry = original_rename

    locations = command_locations(root, command_id)
    require(set(locations) == {"confirming"}, f"prepared crash locations are {sorted(locations)}")
    prepared = read_json(locations["confirming"][0])
    require(prepared.get("status") == "confirmed", "prepared command did not persist confirmed status")
    require(prepared.get("confirmation") == outcome, "prepared command did not embed exact certificate")

    marker_path = root / "action-outcomes-commit.json"
    valid_marker = read_json(marker_path)
    invalid_marker = copy.deepcopy(valid_marker)
    invalid_marker["transaction_hash"] = "sha256:" + "0" * 64
    write_json_atomic(marker_path, invalid_marker)
    try:
        recover_staged_confirmed_outcomes(root)
    except ActionOutcomeError as exc:
        invalid_marker_error = str(exc)
    else:
        raise AssertionError("prepared command continued without a valid commit journal")
    require(
        set(command_locations(root, command_id)) == {"confirming"},
        "invalid journal changed the prepared command",
    )
    write_json_atomic(marker_path, valid_marker)

    first = recover_staged_confirmed_outcomes(root)
    second = recover_staged_confirmed_outcomes(root)
    require(first == [command_id] and second == [command_id], "recovery was not idempotent")
    final_locations = command_locations(root, command_id)
    require(set(final_locations) == {"done"}, f"recovery locations are {sorted(final_locations)}")
    done = read_json(final_locations["done"][0])
    require(done.get("confirmation") == outcome, "recovery changed the immutable certificate")
    return {
        "command_id": command_id,
        "transaction_hash": marker["transaction_hash"],
        "invalid_marker_error": invalid_marker_error,
        "first_recovery": first,
        "second_recovery": second,
        "final_location": "done",
    }


def _leave_legacy_duplicate(root: pathlib.Path, command_id: str) -> tuple[Dict[str, Any], Dict[str, Any]]:
    document, outcome = published_fixture(root, command_id)
    stage_confirmed_outcomes_commit(root, document)
    original_rename = action_outcomes_module._rename_with_retry

    def copy_done_then_crash(source: pathlib.Path, target: pathlib.Path) -> None:
        write_json_atomic(target, read_json(source))
        raise SimulatedCrash("legacy crash after done write and before confirming unlink")

    action_outcomes_module._rename_with_retry = copy_done_then_crash
    try:
        try:
            recover_staged_confirmed_outcomes(root)
        except SimulatedCrash:
            pass
        else:
            raise AssertionError("legacy duplicate crash injection did not fire")
    finally:
        action_outcomes_module._rename_with_retry = original_rename
    require(
        set(command_locations(root, command_id)) == {"confirming", "done"},
        "legacy crash did not leave the expected duplicate",
    )
    return document, outcome


def case_recovers_only_exact_legacy_duplicate(root: pathlib.Path) -> Dict[str, Any]:
    exact_root = root / "exact"
    exact_id = "cmd-transaction-legacy-exact"
    _leave_legacy_duplicate(exact_root, exact_id)
    recovered = recover_staged_confirmed_outcomes(exact_root)
    require(recovered == [exact_id], "exact legacy duplicate was not recovered")
    require(set(command_locations(exact_root, exact_id)) == {"done"}, "exact duplicate was not collapsed")

    conflict_root = root / "conflict"
    conflict_id = "cmd-transaction-legacy-conflict"
    document, _ = _leave_legacy_duplicate(conflict_root, conflict_id)
    done_path = command_locations(conflict_root, conflict_id)["done"][0]
    conflicting = read_json(done_path)
    conflicting["result"]["confirmed_seq"] = int(conflicting["result"]["confirmed_seq"]) + 1
    write_json_atomic(done_path, conflicting)
    try:
        recover_staged_confirmed_outcomes(conflict_root)
    except ActionOutcomeError as exc:
        conflict_error = str(exc)
    else:
        raise AssertionError("conflicting done file did not stop transaction recovery")
    require(
        set(command_locations(conflict_root, conflict_id)) == {"confirming", "done"},
        "conflicting duplicate was destructively collapsed",
    )
    witness = outcome_fixture.fixture(conflict_id)[1]
    rebuilt = build_action_outcomes(
        conflict_root,
        witness,
        outcome_fixture.health_for(witness),
    )
    rebuilt_outcome = outcome_fixture.only_outcome(rebuilt)
    require(
        rebuilt_outcome.get("confirmation_status") == outcome_fixture.CONFIRMATION_REJECTED,
        "duplicate command locations were hidden by the stored-certificate fast path",
    )
    require(
        "command_location_ambiguous" in list(rebuilt_outcome.get("blockers") or []),
        "duplicate location rejection omitted the ambiguity blocker",
    )
    return {
        "exact_recovery": recovered,
        "conflict_recovery": "blocked",
        "conflict_error": conflict_error,
        "conflict_locations": sorted(command_locations(conflict_root, conflict_id)),
        "duplicate_rebuild_status": rebuilt_outcome["confirmation_status"],
        "published_hash": document["outcomes_hash"],
    }


def _one_step_plan_done(root: pathlib.Path) -> tuple[str, str]:
    initial = plan_fixture.state(priority=2)
    plan_fixture.write_live_state(root, initial)
    plan = create_colony_plan(root, plan_fixture.priority_definition(initial, [4]))
    plan = advance_colony_plan(root, str(plan["plan_id"]))
    command_id = plan_fixture.complete_pending(root, plan_fixture.state(priority=4))
    return str(plan["plan_id"]), command_id


def case_plan_consumes_only_matching_done(root: pathlib.Path) -> Dict[str, Any]:
    confirming_root = root / "still-confirming"
    confirming_plan_id, confirming_id = _one_step_plan_done(confirming_root)
    done_path = command_locations(confirming_root, confirming_id)["done"][0]
    confirming_path = ensure_command_dirs(confirming_root)["confirming"] / done_path.name
    done_path.rename(confirming_path)
    waiting = advance_colony_plan(confirming_root, confirming_plan_id)
    require(waiting["status"] == "active", "confirming certificate terminalized the plan")
    require(waiting["steps"][0]["status"] == "awaiting_confirmation", "confirming step advanced")
    require(waiting.get("last_checkpoint", {}).get("command_id") is None, "confirming created checkpoint")

    mismatch_root = root / "mismatched-done"
    mismatch_plan_id, mismatch_id = _one_step_plan_done(mismatch_root)
    mismatch_done_path = command_locations(mismatch_root, mismatch_id)["done"][0]
    mismatch_done = read_json(mismatch_done_path)
    mismatch_done["confirmation"] = copy.deepcopy(mismatch_done["confirmation"])
    mismatch_done["confirmation"]["attestation_hash"] = "sha256:" + "0" * 64
    write_json_atomic(mismatch_done_path, mismatch_done)
    paused = advance_colony_plan(mismatch_root, mismatch_plan_id)
    require(paused["status"] == "paused", "mismatched embedded certificate advanced the plan")
    require(paused["pause_reason"] == "leaf_done_confirmation_mismatch", "wrong pause reason")
    require(paused.get("last_checkpoint", {}).get("command_id") is None, "mismatch created checkpoint")

    exact_root = root / "exact-done"
    exact_plan_id, exact_id = _one_step_plan_done(exact_root)
    completed = advance_colony_plan(exact_root, exact_plan_id)
    require(completed["status"] == "completed", "exact committed done did not complete plan")
    require(completed["last_checkpoint"]["command_id"] == exact_id, "exact done checkpoint is wrong")
    return {
        "confirming_step_status": waiting["steps"][0]["status"],
        "mismatched_done_status": paused["status"],
        "mismatched_done_reason": paused["pause_reason"],
        "exact_done_status": completed["status"],
        "exact_done_checkpoint": completed["last_checkpoint"]["command_id"],
    }


def run_case(
    name: str,
    callback: Callable[[pathlib.Path], Dict[str, Any]],
    root: pathlib.Path,
) -> Dict[str, Any]:
    case_root = root / name
    case_root.mkdir(parents=True, exist_ok=True)
    try:
        return {"name": name, "status": "passed", "details": callback(case_root)}
    except Exception as exc:
        return {
            "name": name,
            "status": "failed",
            "classification": "behavior_fail",
            "error": str(exc),
        }


def run(report_path: pathlib.Path) -> int:
    cases_to_run = [
        ("requires_two_identical_publications", case_requires_two_identical_publications),
        ("recovers_prepared_confirming", case_recovers_prepared_confirming),
        ("recovers_only_exact_legacy_duplicate", case_recovers_only_exact_legacy_duplicate),
        ("plan_consumes_only_matching_done", case_plan_consumes_only_matching_done),
    ]
    with tempfile.TemporaryDirectory(prefix="oni-confirmation-transactions-") as temporary:
        root = pathlib.Path(temporary)
        cases = [run_case(name, callback, root) for name, callback in cases_to_run]
    passed = all(case["status"] == "passed" for case in cases)
    report = {
        "schema": "oni.confirmation-transaction-verification.v1",
        "generated_at": now_iso(),
        "mode": "offline_fault_injection",
        "game_launched": False,
        "normal_bridge_touched": False,
        "status": "passed" if passed else "failed",
        "classification": None if passed else "behavior_fail",
        "cases": cases,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    for case in cases:
        print(
            f"[{case['status'].upper()}] {case['name']}: "
            f"{json.dumps(case.get('details') or case.get('error'), ensure_ascii=False, sort_keys=True)}"
        )
    print(f"Report: {report_path}")
    return 0 if passed else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report",
        type=pathlib.Path,
        default=PROJECT_ROOT / "reports" / "confirmation-transaction-verification.json",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return run(args.report.expanduser().resolve())


if __name__ == "__main__":
    raise SystemExit(main())
