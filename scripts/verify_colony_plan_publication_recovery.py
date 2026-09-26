#!/usr/bin/env python3
"""Small offline evidence runner for colony-plan publication recovery custody."""

from __future__ import annotations

import argparse
import contextlib
import copy
import datetime as dt
import hashlib
import json
import os
import pathlib
import sys
import tempfile
import uuid
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple
from unittest import mock


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent import colony_plan as cp  # noqa: E402
from agent import command_custody as custody  # noqa: E402
from agent import command_queue as queue  # noqa: E402
from agent.decision_frame_archive import DecisionFrameArchiveError  # noqa: E402
from scripts import verify_colony_plan as legacy  # noqa: E402


SOURCE_FILES = {
    "verifier": pathlib.Path(__file__).resolve(),
    "colony_plan": PROJECT_ROOT / "agent" / "colony_plan.py",
    "command_custody": PROJECT_ROOT / "agent" / "command_custody.py",
    "command_queue": PROJECT_ROOT / "agent" / "command_queue.py",
    "legacy_fixture": PROJECT_ROOT / "scripts" / "verify_colony_plan.py",
}


def _utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


def _sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _identity(metadata: os.stat_result) -> List[int]:
    return [int(metadata.st_dev), int(metadata.st_ino)]


def _fingerprint(path: pathlib.Path) -> Dict[str, Any]:
    try:
        first = path.lstat()
        payload = path.read_bytes()
        second = path.lstat()
        return {
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest().upper(),
            "identity": _identity(second),
            "mtime_ns": int(getattr(second, "st_mtime_ns", int(second.st_mtime * 1_000_000_000))),
            "stable": _identity(first) == _identity(second)
            and int(first.st_size) == len(payload),
        }
    except BaseException as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def _json(path: pathlib.Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _check(case: Dict[str, Any], name: str, passed: bool, observed: Any = None) -> None:
    assertion = {"name": name, "passed": bool(passed)}
    if observed is not None:
        assertion["observed"] = observed
    case.setdefault("assertions", []).append(assertion)
    if not passed:
        case.setdefault("failures", []).append(name)


def _expect(
    case: Dict[str, Any],
    name: str,
    operation: Callable[[], Any],
    expected: type[BaseException],
) -> Optional[BaseException]:
    try:
        operation()
    except expected as exc:
        _check(case, name, True, type(exc).__name__)
        return exc
    except BaseException as exc:
        _check(case, name, False, f"unexpected {type(exc).__name__}: {exc}")
        return exc
    _check(case, name, False, "no exception")
    return None


def _finish(case: Dict[str, Any]) -> Dict[str, Any]:
    assertions = case.get("assertions") or []
    failures = list(dict.fromkeys(str(item) for item in case.get("failures") or []))
    if int(case.get("body_assertion_count") or 0) <= 0:
        failures.append("zero_body_assertions")
    if case.get("status_error"):
        failures.append("unexpected_case_exception")
    if case.get("cleanup", {}).get("temporary_root_removed") is not True:
        failures.append("cleanup_failed")
    passed = sum(item.get("passed") is True for item in assertions)
    case["failures"] = list(dict.fromkeys(failures))
    case["assertion_counts"] = {
        "total": len(assertions),
        "passed": passed,
        "failed": len(assertions) - passed,
    }
    case["status"] = "passed" if assertions and not case["failures"] and passed == len(assertions) else "failed"
    case["passed"] = case["status"] == "passed"
    case["finished_at_utc"] = _utc()
    return case


def _run_case(name: str, body: Callable[[pathlib.Path, Dict[str, Any]], None]) -> Dict[str, Any]:
    case: Dict[str, Any] = {"name": name, "started_at_utc": _utc(), "assertions": []}
    root_text = ""
    try:
        with tempfile.TemporaryDirectory(prefix="a09a8-publication-recovery-") as text:
            root_text = text
            body(pathlib.Path(text), case)
    except BaseException as exc:
        case["status_error"] = f"{type(exc).__name__}: {exc}"
    finally:
        case["body_assertion_count"] = len(case.get("assertions") or [])
        removed = bool(root_text) and not pathlib.Path(root_text).exists()
        case["cleanup"] = {"temporary_root_removed": removed}
        _check(case, "temporary_root_removed", removed, removed)
    return _finish(case)


def _fixture(root: pathlib.Path) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    initial = legacy.state(priority=2)
    legacy.write_live_state(root, initial)
    queue.ensure_command_dirs(root)
    return initial, cp.create_colony_plan(root, legacy.priority_definition(initial, [4]))


def _pending(root: pathlib.Path) -> Tuple[pathlib.Path, Dict[str, Any]]:
    paths = sorted(queue.ensure_command_dirs(root)["pending"].glob("*.json"))
    if len(paths) != 1:
        raise AssertionError(f"expected one pending command, found {len(paths)}")
    return paths[0], _json(paths[0])


def _revision_chain(root: pathlib.Path, plan_id: str) -> Tuple[bool, int]:
    revisions = sorted((cp.plan_directory(root, plan_id) / "revisions").glob("*.json"))
    previous: Optional[str] = None
    expected = 1
    for path in revisions:
        value = _json(path)
        if value.get("revision") != expected or value.get("previous_revision_hash") != previous:
            return False, len(revisions)
        if value.get("revision_hash") != cp._canonical_hash(value):
            return False, len(revisions)
        previous = value.get("revision_hash")
        expected += 1
    return bool(revisions), len(revisions)


def _case_normal_publish_abort(root: pathlib.Path, case: Dict[str, Any]) -> None:
    _initial, draft = _fixture(root)
    active = cp.advance_colony_plan(root, str(draft["plan_id"]))
    pending_path, pending = _pending(root)
    aborted = cp.abort_colony_plan(root, str(active["plan_id"]))
    quarantine = sorted(queue.ensure_command_dirs(root)["quarantine"].glob("*.json"))
    _check(case, "draft_revision_one", draft.get("status") == "draft" and draft.get("revision") == 1)
    _check(case, "active_pending_requires_approval", active.get("status") == "active" and pending.get("requires_approval") is True)
    _check(case, "abort_is_terminal", aborted.get("status") == "aborted" and aborted.get("revision") == 3)
    _check(case, "one_canonical_quarantine", len(quarantine) == 1 and quarantine[0].name == pending_path.name)
    if quarantine:
        value = _json(quarantine[0])
        _check(case, "abort_quarantine_reason", value.get("quarantine_reason") == "colony_plan_aborted")
        _check(case, "abort_plan_binding", value.get("aborted_plan_id") == active.get("plan_id"))
    _check(case, "pending_empty", not list(queue.ensure_command_dirs(root)["pending"].glob("*.json")))
    valid, count = _revision_chain(root, str(active["plan_id"]))
    _check(case, "history_hashchain_readable", valid and count == 3, {"valid": valid, "count": count})


def _case_postpublish_context_change(root: pathlib.Path, case: Dict[str, Any]) -> None:
    _initial, draft = _fixture(root)
    original_publish = cp.publish_pending_command

    def publish_then_change(bridge: pathlib.Path, command: Mapping[str, Any]) -> pathlib.Path:
        result = original_publish(bridge, command)
        changed = legacy.state(priority=3, seq=2)
        legacy.write_live_state(bridge, changed)
        return result

    with mock.patch.object(cp, "publish_pending_command", publish_then_change):
        caught = _expect(case, "postpublish_context_change_is_error", lambda: cp.advance_colony_plan(root), cp.ColonyPlanError)
    _check(case, "postpublish_exception_is_plain_colony_plan_error", type(caught) is cp.ColonyPlanError)
    plan = cp.load_active_plan(root)
    quarantine = sorted(queue.ensure_command_dirs(root)["quarantine"].glob("*.json"))
    _check(case, "paused_and_blocked", plan.get("status") == "paused" and plan.get("pause_reason") == "leaf_publication_context_changed" and plan.get("revision") == 3)
    blocked = [step for step in plan.get("steps") or [] if step.get("status") == "blocked"]
    _check(case, "blocked_step_keeps_real_command", len(blocked) == 1 and blocked[0].get("command_id") and plan.get("open_command_id") is None and plan.get("current_step") is None)
    _check(case, "unique_quarantine_no_pending", len(quarantine) == 1 and not list(queue.ensure_command_dirs(root)["pending"].glob("*.json")))
    if quarantine:
        value = _json(quarantine[0])
        _check(case, "context_quarantine_reason_without_abort_id", value.get("quarantine_reason") == "colony_plan_decision_context_changed" and "aborted_plan_id" not in value)
    before_revision = plan.get("revision")
    _expect(case, "resume_rejects_blocked_same_id", lambda: cp.resume_colony_plan(root), cp.ColonyPlanError)
    after = cp.load_active_plan(root)
    _check(case, "resume_does_not_republish", after.get("revision") == before_revision and not list(queue.ensure_command_dirs(root)["pending"].glob("*.json")))
    valid, count = _revision_chain(root, str(plan["plan_id"]))
    _check(case, "postpublish_history_hashchain", valid and count == 3, {"valid": valid, "count": count})


def _case_isolation_unknown(root: pathlib.Path, case: Dict[str, Any]) -> None:
    _initial, draft = _fixture(root)
    original_publish = cp.publish_pending_command

    def publish_then_change(bridge: pathlib.Path, command: Mapping[str, Any]) -> pathlib.Path:
        result = original_publish(bridge, command)
        legacy.write_live_state(bridge, legacy.state(priority=3, seq=2))
        return result

    observed_source: Dict[str, Any] = {}

    def inject_unknown(
        _bridge: pathlib.Path,
        path: pathlib.Path,
        _command: Mapping[str, Any],
        _plan_id: str,
        *,
        reason: str,
    ) -> pathlib.Path:
        observed_source["path"] = path
        observed_source["bytes"] = path.read_bytes()
        observed_source["fingerprint"] = _fingerprint(path)
        raise custody.CommandCustodyUnknown("injected isolation uncertainty")

    with mock.patch.object(cp, "publish_pending_command", publish_then_change):
        with mock.patch.object(cp, "_quarantine_pending_command", inject_unknown):
            _expect(case, "unknown_isolation_requires_recovery", lambda: cp.advance_colony_plan(root), cp.PlanRecoveryRequired)
        # The command was published before the injected isolation boundary.
    pending_path, pending_before = _pending(root)
    source_before = observed_source.get("bytes")
    fingerprint_before = observed_source.get("fingerprint")
    plan = cp.load_active_plan(root)
    _check(case, "paused_nonterminal_with_open_evidence", plan.get("status") == "paused" and plan.get("open_command_id") == pending_before.get("id") and plan.get("current_step") == "priority_1")
    _check(case, "source_observation_valid", observed_source.get("path") == pending_path and isinstance(source_before, bytes) and isinstance(fingerprint_before, dict) and fingerprint_before.get("stable") is True)
    _check(case, "source_bytes_identity_retained", isinstance(source_before, bytes) and pending_path.read_bytes() == source_before and _fingerprint(pending_path) == fingerprint_before)
    _check(case, "no_false_quarantine_success", not list(queue.ensure_command_dirs(root)["quarantine"].glob("*.json")))
    _check(case, "unknown_isolation_revision_is_first_pause", plan.get("revision") == 2)


def _case_prepublish_context_change(root: pathlib.Path, case: Dict[str, Any]) -> None:
    _initial, draft = _fixture(root)

    def reject_before_publish(_context: Any) -> None:
        raise DecisionFrameArchiveError("injected prepublication context change")

    with mock.patch.object(cp, "assert_current_decision_context", reject_before_publish):
        _expect(case, "prepublish_context_change_is_error", lambda: cp.advance_colony_plan(root), cp.ColonyPlanError)
    plan = cp.load_active_plan(root)
    _check(case, "prepublish_has_no_command", not list(queue.ensure_command_dirs(root)["pending"].glob("*.json")))
    _check(case, "prepublish_has_no_new_revision", plan.get("status") == "draft" and plan.get("revision") == draft.get("revision"))


def _case_duplicate_id_publisher(root: pathlib.Path, case: Dict[str, Any]) -> None:
    _initial, draft = _fixture(root)
    foreign_path: Optional[pathlib.Path] = None
    foreign_bytes = b""
    original_publish = cp.publish_pending_command

    def inject_foreign_then_publish(bridge: pathlib.Path, command: Mapping[str, Any]) -> pathlib.Path:
        nonlocal foreign_path, foreign_bytes
        foreign_path = queue.ensure_command_dirs(bridge)["pending"] / f"{command['id']}.json"
        foreign = copy.deepcopy(dict(command))
        foreign_args = dict(foreign.get("args") or {})
        foreign_args["priority"] = 3 if foreign_args.get("priority") != 3 else 2
        foreign["args"] = foreign_args
        queue.write_json_atomic(foreign_path, foreign)
        foreign_bytes = foreign_path.read_bytes()
        foreign_fingerprint = _fingerprint(foreign_path)
        inject_foreign_then_publish.fingerprint = foreign_fingerprint  # type: ignore[attr-defined]
        return original_publish(bridge, command)

    with mock.patch.object(cp, "publish_pending_command", inject_foreign_then_publish):
        _expect(case, "duplicate_publisher_is_rejected", lambda: cp.advance_colony_plan(root), cp.ColonyPlanError)
    plan = cp.load_active_plan(root)
    _check(case, "foreign_duplicate_bytes_not_overwritten", foreign_path is not None and foreign_path.read_bytes() == foreign_bytes)
    _check(case, "foreign_duplicate_identity_not_overwritten", foreign_path is not None and _fingerprint(foreign_path) == getattr(inject_foreign_then_publish, "fingerprint", None))
    _check(case, "duplicate_publisher_no_plan_revision", plan.get("status") == "draft" and plan.get("revision") == draft.get("revision"))
    _check(case, "duplicate_has_one_pending_owner", len(list(queue.ensure_command_dirs(root)["pending"].glob("*.json"))) == 1)


def _case_abort_blocks_all_stages(root: pathlib.Path, case: Dict[str, Any]) -> None:
    stages = ("approving", "approved", "executing", "confirming")
    for stage in stages:
        with tempfile.TemporaryDirectory(prefix=f"a09a8-abort-{stage}-") as text:
            stage_root = pathlib.Path(text)
            _initial, draft = _fixture(stage_root)
            active = cp.advance_colony_plan(stage_root, str(draft["plan_id"]))
            stage_id = legacy.move_pending(stage_root, stage, status="executed_pending_snapshot" if stage == "confirming" else "approved")
            before = cp.load_active_plan(stage_root)
            path = queue.ensure_command_dirs(stage_root)[stage] / f"{stage_id}.json"
            bytes_before = path.read_bytes()
            _expect(case, f"abort_rejects_{stage}", lambda: cp.abort_colony_plan(stage_root), cp.ColonyPlanError)
            after = cp.load_active_plan(stage_root)
            _check(case, f"{stage}_plan_unchanged", after == before)
            _check(case, f"{stage}_source_unchanged", path.read_bytes() == bytes_before)
            _check(case, f"{stage}_fixture_active", active.get("status") == "active")
        _check(case, f"{stage}_temporary_removed", not pathlib.Path(text).exists())


def _case_legacy_lock(root: pathlib.Path, case: Dict[str, Any]) -> None:
    _initial, draft = _fixture(root)
    legacy_lock = root / "colony-plans" / ".mutation.lock"
    legacy_lock.parent.mkdir(parents=True, exist_ok=True)
    legacy_lock.write_bytes(b"retired-lock-preimage")
    plan_before = cp.load_active_plan(root)
    _expect(case, "legacy_plan_lock_rejects_operation", lambda: cp.advance_colony_plan(root), cp.ColonyPlanError)
    plan_after = cp.load_active_plan(root)
    _check(case, "legacy_lock_not_deleted_or_overwritten", legacy_lock.read_bytes() == b"retired-lock-preimage")
    _check(case, "legacy_lock_plan_unchanged", plan_after == plan_before and plan_after.get("revision") == draft.get("revision"))
    _check(case, "legacy_lock_has_no_pending", not list(queue.ensure_command_dirs(root)["pending"].glob("*.json")))


CASES: Tuple[Tuple[str, Callable[[pathlib.Path, Dict[str, Any]], None]], ...] = (
    ("normal_publish_abort", _case_normal_publish_abort),
    ("postpublish_context_change_quarantine", _case_postpublish_context_change),
    ("context_change_isolation_unknown", _case_isolation_unknown),
    ("prepublish_context_change_no_mutation", _case_prepublish_context_change),
    ("duplicate_id_publisher_conflict", _case_duplicate_id_publisher),
    ("abort_blocks_all_active_stages", _case_abort_blocks_all_stages),
    ("legacy_plan_lock_failclosed", _case_legacy_lock),
)


def _capture_sources() -> Tuple[Dict[str, Optional[str]], Dict[str, str]]:
    hashes: Dict[str, Optional[str]] = {}
    errors: Dict[str, str] = {}
    for name, path in SOURCE_FILES.items():
        try:
            hashes[name] = _sha256(path)
        except BaseException as exc:
            hashes[name] = None
            errors[name] = f"{type(exc).__name__}: {exc}"
    return hashes, errors


def _import_paths() -> Dict[str, Optional[str]]:
    modules = {"verifier": sys.modules.get(__name__), "colony_plan": cp, "command_custody": custody, "command_queue": queue, "legacy_fixture": legacy}
    result: Dict[str, Optional[str]] = {}
    for name, module in modules.items():
        path = getattr(module, "__file__", None)
        result[name] = str(pathlib.Path(path).resolve()) if path else None
    return result


EXPECTED_CASE_NAMES = (
    "normal_publish_abort",
    "postpublish_context_change_quarantine",
    "context_change_isolation_unknown",
    "prepublish_context_change_no_mutation",
    "duplicate_id_publisher_conflict",
    "abort_blocks_all_active_stages",
    "legacy_plan_lock_failclosed",
)


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-root", type=pathlib.Path, default=PROJECT_ROOT / "reports")
    args = parser.parse_args(list(argv) if argv is not None else None)

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    report_dir = args.report_root / f"a09a8-plan-publication-recovery-{stamp}-{os.getpid()}-{uuid.uuid4().hex[:12]}"
    report_dir.mkdir(parents=True, exist_ok=False)
    source_before, source_errors = _capture_sources()
    report: Dict[str, Any] = {
        "schema": "oni.colony-plan-publication-recovery-report.v1",
        "started_at_utc": _utc(),
        "pid": os.getpid(),
        "report_dir": str(report_dir.resolve()),
        "source_before": source_before,
        "source_before_errors": source_errors,
        "import_paths": _import_paths(),
        "cases": [],
    }
    expected_imports = {name: str(path.resolve()) for name, path in SOURCE_FILES.items()}
    report["import_paths_expected"] = expected_imports
    import_paths = report["import_paths"]
    if source_errors:
        report["status_error"] = "source hash capture failed"
    elif import_paths != expected_imports:
        report["status_error"] = "import path mismatch; fixtures were not run"
    elif tuple(name for name, _body in CASES) != EXPECTED_CASE_NAMES or len(CASES) != 7:
        report["status_error"] = "case inventory mismatch; fixtures were not run"
    else:
        report["cases"] = [_run_case(name, body) for name, body in CASES]
    source_after, after_errors = _capture_sources()
    report["source_after"] = source_after
    report["source_after_errors"] = after_errors
    report["source_unchanged"] = source_before == source_after and not after_errors
    failures = [case["name"] for case in report["cases"] if case.get("status") != "passed"]
    if report.get("status_error") or source_errors or after_errors or not report["source_unchanged"]:
        failures.append("source_or_runner_integrity")
    report["failures"] = list(dict.fromkeys(failures))
    report["status"] = "passed" if not report["failures"] else "failed"
    report["finished_at_utc"] = _utc()
    report_path = report_dir / "report.json"
    with report_path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "report": str(report_path.resolve()), "cases": len(report["cases"]), "failures": report["failures"]}, ensure_ascii=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
