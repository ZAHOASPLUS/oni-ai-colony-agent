"""Small offline verifier for the A09A8 pending-envelope observation channel.

This runner uses only independent temporary fixtures.  It never starts ONI,
Steam, Bridge, pytest, or a child process, and it never touches a real queue.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import pathlib
import sys
import tempfile
import traceback
import uuid
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent import command_custody as cc  # noqa: E402


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


def sha256_file(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def canonical(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def command(command_id: str, *, value: Any = "A") -> Dict[str, Any]:
    return {
        "schema": cc.COMMAND_SCHEMA,
        "id": command_id,
        "status": cc.PENDING_STATUS,
        "payload": {"value": value},
    }


def final_command(command_id: str) -> Dict[str, Any]:
    return {
        "schema": cc.COMMAND_SCHEMA,
        "id": command_id,
        "status": "rejected",
        "reason": "offline-observation-verifier",
    }


def paths(root: pathlib.Path, command_id: str) -> Tuple[pathlib.Path, pathlib.Path]:
    source = root / "commands" / "pending" / f"{command_id}.json"
    target = root / "commands" / "rejected" / f"{command_id}.json"
    return source, target


def setup_pending(
    root: pathlib.Path,
    command_id: str,
    value: Mapping[str, Any],
    *,
    raw: Optional[bytes] = None,
) -> Tuple[pathlib.Path, bytes]:
    cc._ensure_layout(root)
    source, _target = paths(root, command_id)
    payload = canonical(value) if raw is None else raw
    source.write_bytes(payload)
    return source, payload


def replace_with_new_inode(source: pathlib.Path, payload: bytes) -> None:
    swap = source.with_name(source.name + ".swap")
    swap.write_bytes(payload)
    source.unlink()
    swap.replace(source)


class Harness:
    def __init__(self, report_dir: pathlib.Path) -> None:
        self.report_dir = report_dir
        self.events: List[Dict[str, Any]] = []
        self.fixture_records: List[Dict[str, Any]] = []
        self.assertions = 0
        self.passed = 0
        self.failed = 0

    def check(self, name: str, condition: bool, detail: Any = None) -> None:
        self.assertions += 1
        if condition:
            self.passed += 1
        else:
            self.failed += 1
        event: Dict[str, Any] = {"assertion": name, "passed": bool(condition)}
        if detail is not None:
            event["detail"] = detail
        self.events.append(event)

    def expect_error(self, name: str, call: Callable[[], Any]) -> None:
        try:
            call()
        except cc.CommandCustodyError as exc:
            self.check(name, True, {"exception": type(exc).__name__})
        except BaseException as exc:
            self.check(
                name,
                False,
                {"unexpected_exception": type(exc).__name__, "message": str(exc)},
            )
        else:
            self.check(name, False, {"unexpected": "call succeeded"})

    def fixture(self, name: str, body: Callable[[pathlib.Path], None]) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="oni-a09a8-observed-")
        root = pathlib.Path(temporary.name)
        body_start = self.assertions
        body_passed_start = self.passed
        body_error: Optional[BaseException] = None
        try:
            try:
                body(root)
            except BaseException as exc:
                body_error = exc
        finally:
            body_assertions = self.assertions - body_start
            body_passed = self.passed - body_passed_start
            record: Dict[str, Any] = {
                "name": name,
                "body_assertions_before": body_start,
                "body_assertions_after": self.assertions,
                "body_assertions": body_assertions,
                "body_passed_before": body_passed_start,
                "body_passed_after": self.passed,
                "body_passed": body_passed,
            }
            self.fixture_records.append(record)
            if body_error is not None:
                self.check(
                    f"{name}.exception",
                    False,
                    {
                        "exception": type(body_error).__name__,
                        "message": str(body_error),
                        "traceback": "".join(traceback.format_exception(body_error)).strip(),
                    },
                )
            if body_assertions == 0:
                self.check(f"{name}.body_assertions_nonzero", False)
            if body_passed == 0:
                self.check(f"{name}.body_passed_nonzero", False)
            try:
                temporary.cleanup()
            except BaseException as exc:
                self.check(
                    f"{name}.cleanup_exception",
                    False,
                    {"exception": type(exc).__name__, "message": str(exc)},
                )
            else:
                self.check(f"{name}.temporary_removed", not root.exists())


EXPECTED_FIXTURES = (
    "fresh_pretty_success",
    "fresh_compact_success",
    "a_then_b_replacement",
    "same_bytes_new_identity",
    "semantic_whitespace_change",
    "wrong_path_and_missing_expected",
    "stale_observation_missing_source",
    "legacy_compact_behavior",
    "legacy_pretty_behavior",
    "duplicate_owner",
    "nonfinite_json",
    "malformed_json",
    "recovery_evidence",
    "bool_int_semantics",
)


def observe_success(h: Harness, root: pathlib.Path, *, pretty: bool) -> None:
    command_id = "obs-pretty" if pretty else "obs-compact"
    value = command(command_id)
    raw = (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    ).encode("utf-8") if pretty else canonical(value)
    source, source_raw = setup_pending(root, command_id, value, raw=raw)
    _source, target = paths(root, command_id)
    observation = cc.observe_pending_envelope(root, source, expected_source=value)
    h.check(f"{command_id}.observation_type", isinstance(observation, cc.PendingEnvelopeObservation))
    h.check(f"{command_id}.observation_payload", observation.payload == source_raw)
    h.check(f"{command_id}.observation_path", observation.path == source)
    h.check(f"{command_id}.observation_identity", isinstance(observation.identity, tuple) and len(observation.identity) == 2)
    h.check(f"{command_id}.observation_mtime", isinstance(observation.mtime_ns, int) and not isinstance(observation.mtime_ns, bool))
    h.check(f"{command_id}.no_mutable_command_field", not hasattr(observation, "command"))
    h.check(f"{command_id}.source_unchanged", source.read_bytes() == source_raw)
    h.check(f"{command_id}.target_not_written", not target.exists())
    result = cc.transition_pending_envelope(
        root,
        source,
        target,
        final_command(command_id),
        expected_source=value,
        expected_observation=observation,
        operation="rejected",
    )
    h.check(f"{command_id}.transition_success", result == target and target.exists() and not source.exists())


def case_pretty_success(h: Harness) -> None:
    h.fixture("fresh_pretty_success", lambda root: observe_success(h, root, pretty=True))


def case_compact_success(h: Harness) -> None:
    h.fixture("fresh_compact_success", lambda root: observe_success(h, root, pretty=False))


def case_a_then_b_rejected(h: Harness) -> None:
    def body(root: pathlib.Path) -> None:
        command_id = "obs-a-then-b"
        value_a = command(command_id, value="A")
        source, raw_a = setup_pending(root, command_id, value_a)
        _source, target = paths(root, command_id)
        observation = cc.observe_pending_envelope(root, source, expected_source=value_a)
        value_b = command(command_id, value="B")
        replace_with_new_inode(source, canonical(value_b))
        h.expect_error(
            "observe_expected_a_actual_b_rejected",
            lambda: cc.observe_pending_envelope(root, source, expected_source=value_a),
        )
        replacement_before = cc.read_stable_bytes(source, label="A/B replacement before")
        h.check("a_then_b_replacement_is_complete", replacement_before[0] == canonical(value_b))
        h.expect_error(
            "a_then_b_replacement_rejected",
            lambda: cc.transition_pending_envelope(
                root,
                source,
                target,
                final_command(command_id),
                expected_source=value_a,
                expected_observation=observation,
                operation="rejected",
            ),
        )
        replacement_after = cc.read_stable_bytes(source, label="A/B replacement after")
        h.check("a_then_b_source_survives", replacement_after == replacement_before and replacement_after[0] != raw_a)
        h.check("a_then_b_target_not_written", not target.exists())

    h.fixture("a_then_b_replacement", body)


def case_same_bytes_new_identity(h: Harness) -> None:
    def body(root: pathlib.Path) -> None:
        command_id = "obs-same-bytes"
        value = command(command_id)
        source, raw = setup_pending(root, command_id, value)
        _source, target = paths(root, command_id)
        observation = cc.observe_pending_envelope(root, source, expected_source=value)
        replace_with_new_inode(source, raw)
        replacement_before = cc.read_stable_bytes(source, label="same-bytes replacement before")
        h.check("same_bytes_replacement_complete", replacement_before[0] == raw)
        h.check("same_bytes_identity_replaced", replacement_before[1] != observation.identity)
        h.expect_error(
            "same_bytes_new_identity_rejected",
            lambda: cc.transition_pending_envelope(
                root,
                source,
                target,
                final_command(command_id),
                expected_source=value,
                expected_observation=observation,
                operation="rejected",
            ),
        )
        replacement_after = cc.read_stable_bytes(source, label="same-bytes replacement after")
        h.check("same_bytes_replacement_survives", replacement_after == replacement_before)
        h.check("same_bytes_target_not_written", not target.exists())

    h.fixture("same_bytes_new_identity", body)


def case_semantic_whitespace_rejected(h: Harness) -> None:
    def body(root: pathlib.Path) -> None:
        command_id = "obs-whitespace"
        value = command(command_id)
        source, raw = setup_pending(root, command_id, value)
        _source, target = paths(root, command_id)
        observation = cc.observe_pending_envelope(root, source, expected_source=value)
        pretty = (json.dumps(value, indent=4, sort_keys=False) + "\n").encode("utf-8")
        h.check("whitespace_bytes_differ", pretty != raw)
        replace_with_new_inode(source, pretty)
        replacement_before = cc.read_stable_bytes(source, label="whitespace replacement before")
        h.check("whitespace_replacement_complete", replacement_before[0] == pretty)
        h.check("whitespace_identity_replaced", replacement_before[1] != observation.identity)
        h.expect_error(
            "semantic_whitespace_rejected",
            lambda: cc.transition_pending_envelope(
                root,
                source,
                target,
                final_command(command_id),
                expected_source=value,
                expected_observation=observation,
                operation="rejected",
            ),
        )
        replacement_after = cc.read_stable_bytes(source, label="whitespace replacement after")
        h.check("whitespace_replacement_survives", replacement_after == replacement_before)
        h.check("semantic_whitespace_target_not_written", not target.exists())

    h.fixture("semantic_whitespace_change", body)


def case_wrong_path_and_missing_expected(h: Harness) -> None:
    def body(root: pathlib.Path) -> None:
        command_id = "obs-path"
        value = command(command_id)
        source, _raw = setup_pending(root, command_id, value)
        observation = cc.observe_pending_envelope(root, source, expected_source=value)
        wrong_source = root / "commands" / "pending" / "wrong-path.json"
        wrong_target = root / "commands" / "rejected" / "wrong-path.json"
        h.expect_error(
            "wrong_observation_path_rejected",
            lambda: cc.transition_pending_envelope(
                root,
                wrong_source,
                wrong_target,
                final_command("wrong-path"),
                expected_source=value,
                expected_observation=observation,
                operation="rejected",
            ),
        )
        h.expect_error(
            "observation_without_expected_source_rejected",
            lambda: cc.transition_pending_envelope(
                root,
                source,
                root / "commands" / "rejected" / f"{command_id}.json",
                final_command(command_id),
                expected_observation=observation,
                operation="rejected",
            ),
        )

    h.fixture("wrong_path_and_missing_expected", body)


def case_stale_observation_missing_source(h: Harness) -> None:
    def body(root: pathlib.Path) -> None:
        command_id = "obs-stale-missing"
        value = command(command_id)
        source, _raw = setup_pending(root, command_id, value)
        _source, target = paths(root, command_id)
        observation = cc.observe_pending_envelope(root, source, expected_source=value)
        source.unlink()
        h.expect_error(
            "stale_missing_source_rejected",
            lambda: cc.transition_pending_envelope(
                root,
                source,
                target,
                final_command(command_id),
                expected_source=value,
                expected_observation=observation,
                operation="rejected",
            ),
        )
        h.check("stale_missing_source_target_not_written", not target.exists())

    h.fixture("stale_observation_missing_source", body)


def case_legacy_strict_behavior(h: Harness) -> None:
    def compact_body(root: pathlib.Path) -> None:
        command_id = "legacy-compact"
        value = command(command_id)
        source, _raw = setup_pending(root, command_id, value)
        _source, target = paths(root, command_id)
        result = cc.transition_pending_envelope(
            root,
            source,
            target,
            final_command(command_id),
            expected_source=value,
            operation="rejected",
        )
        h.check("legacy_compact_success", result == target and target.exists() and not source.exists())

    def pretty_body(root: pathlib.Path) -> None:
        command_id = "legacy-pretty"
        value = command(command_id)
        pretty = (json.dumps(value, indent=2, sort_keys=False) + "\n").encode("utf-8")
        source, _raw = setup_pending(root, command_id, value, raw=pretty)
        _source, target = paths(root, command_id)
        h.expect_error(
            "legacy_pretty_exact_bytes_rejected",
            lambda: cc.transition_pending_envelope(
                root,
                source,
                target,
                final_command(command_id),
                expected_source=value,
                operation="rejected",
            ),
        )
        h.check("legacy_pretty_source_survives", source.exists() and not target.exists())

    h.fixture("legacy_compact_behavior", compact_body)
    h.fixture("legacy_pretty_behavior", pretty_body)


def case_duplicates_and_malformed(h: Harness) -> None:
    def duplicate_body(root: pathlib.Path) -> None:
        command_id = "obs-duplicate"
        value = command(command_id)
        source, _raw = setup_pending(root, command_id, value)
        duplicate = root / "commands" / "rejected" / source.name
        duplicate.write_bytes(canonical(final_command(command_id)))
        h.expect_error(
            "duplicate_owner_rejected",
            lambda: cc.observe_pending_envelope(root, source, expected_source=value),
        )

    def nonfinite_body(root: pathlib.Path) -> None:
        command_id = "obs-nonfinite"
        value = command(command_id)
        source, _raw = setup_pending(
            root,
            command_id,
            value,
            raw=(
                '{"schema":"%s","id":"%s","status":"%s","payload":{"value":NaN}}\n'
                % (cc.COMMAND_SCHEMA, command_id, cc.PENDING_STATUS)
            ).encode("utf-8"),
        )
        h.expect_error(
            "nonfinite_json_rejected",
            lambda: cc.observe_pending_envelope(root, source, expected_source=value),
        )

    def malformed_body(root: pathlib.Path) -> None:
        command_id = "obs-malformed"
        source, _target = paths(root, command_id)
        cc._ensure_layout(root)
        source.write_bytes(b"{not-json}\n")
        h.expect_error(
            "malformed_json_rejected",
            lambda: cc.observe_pending_envelope(root, source, expected_source=command(command_id)),
        )

    def recovery_evidence_body(root: pathlib.Path) -> None:
        command_id = "obs-recovery-blocker"
        value = command(command_id)
        source, raw = setup_pending(root, command_id, value)
        source_before = cc.read_stable_bytes(source, label="recovery-blocker source")
        recovery = root / "commands" / cc.LIFECYCLE_RECOVERY_DIR_NAME
        evidence = recovery / "blocked.original.bytes"
        evidence.write_bytes(b"unresolved recovery evidence\n")
        evidence_before = cc.read_stable_bytes(evidence, label="recovery-blocker evidence")
        h.expect_error(
            "recovery_evidence_observe_rejected",
            lambda: cc.observe_pending_envelope(root, source, expected_source=value),
        )
        source_after = cc.read_stable_bytes(source, label="recovery-blocker source after")
        evidence_after = cc.read_stable_bytes(evidence, label="recovery-blocker evidence after")
        h.check("recovery_blocker_source_bytes_unchanged", source_after[0] == raw == source_before[0])
        h.check("recovery_blocker_source_identity_unchanged", source_after[1:] == source_before[1:])
        h.check("recovery_blocker_evidence_unchanged", evidence_after == evidence_before)

    def bool_int_body(root: pathlib.Path) -> None:
        command_id = "obs-bool-int"
        actual = command(command_id, value=1)
        expected = command(command_id, value=True)
        source, _raw = setup_pending(root, command_id, actual)
        h.check("bool_int_python_values_are_equal", actual["payload"]["value"] == expected["payload"]["value"])
        h.expect_error(
            "bool_int_semantics_not_confused",
            lambda: cc.observe_pending_envelope(root, source, expected_source=expected),
        )

    h.fixture("duplicate_owner", duplicate_body)
    h.fixture("nonfinite_json", nonfinite_body)
    h.fixture("malformed_json", malformed_body)
    h.fixture("recovery_evidence", recovery_evidence_body)
    h.fixture("bool_int_semantics", bool_int_body)


def main() -> int:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    report_dir = PROJECT_ROOT / "reports" / (
        f"a09a8-observed-envelope-core-{stamp}-pid{os.getpid()}-{uuid.uuid4().hex[:12]}"
    )
    report_dir.mkdir(parents=True, exist_ok=False)
    harness = Harness(report_dir)
    started_at = utc_now()
    core_path = (PROJECT_ROOT / "agent" / "command_custody.py").resolve()
    verifier_path = pathlib.Path(__file__).resolve()
    import_path: Optional[pathlib.Path] = None
    core_before: Optional[str] = None
    verifier_before: Optional[str] = None
    before_capture_ok = True
    try:
        import_path = pathlib.Path(cc.__file__).resolve()
        core_before = sha256_file(core_path)
        verifier_before = sha256_file(verifier_path)
    except BaseException as exc:
        before_capture_ok = False
        harness.check(
            "startup_source_hash_capture",
            False,
            {"exception": type(exc).__name__, "message": str(exc)},
        )

    import_matches = import_path == core_path
    harness.check(
        "import_path_matches_core",
        import_matches,
        {"import_path": str(import_path) if import_path is not None else None, "core_path": str(core_path)},
    )
    if before_capture_ok and import_matches:
        try:
            case_pretty_success(harness)
            case_compact_success(harness)
            case_a_then_b_rejected(harness)
            case_same_bytes_new_identity(harness)
            case_semantic_whitespace_rejected(harness)
            case_wrong_path_and_missing_expected(harness)
            case_stale_observation_missing_source(harness)
            case_legacy_strict_behavior(harness)
            case_duplicates_and_malformed(harness)
        except BaseException as exc:
            harness.check(
                "fixture_dispatch_exception",
                False,
                {"exception": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc(limit=8)},
            )

    core_after: Optional[str] = None
    verifier_after: Optional[str] = None
    try:
        core_after = sha256_file(core_path)
        verifier_after = sha256_file(verifier_path)
    except BaseException as exc:
        harness.check(
            "finish_source_hash_capture",
            False,
            {"exception": type(exc).__name__, "message": str(exc)},
        )
    if core_before is not None and core_after is not None:
        harness.check("core_hash_unchanged", core_before == core_after, {"before": core_before, "after": core_after})
    if verifier_before is not None and verifier_after is not None:
        harness.check(
            "verifier_hash_unchanged",
            verifier_before == verifier_after,
            {"before": verifier_before, "after": verifier_after},
        )
    actual_fixtures = [record["name"] for record in harness.fixture_records]
    if import_matches and before_capture_ok:
        harness.check("fixture_names_exact", tuple(actual_fixtures) == EXPECTED_FIXTURES, {"actual": actual_fixtures, "expected": EXPECTED_FIXTURES})
    harness.check("assertions_nonzero", harness.assertions > 0)
    report = {
        "schema": "oni.a09a8.observed-envelope-verifier.v1",
        "started_at": started_at,
        "finished_at": utc_now(),
        "pid": os.getpid(),
        "import_path": str(import_path) if import_path is not None else None,
        "core_path": str(core_path),
        "source_hashes": {
            "core": {"before": core_before, "after": core_after},
            "verifier": {"before": verifier_before, "after": verifier_after},
        },
        "expected_fixture_names": EXPECTED_FIXTURES,
        "actual_fixture_names": actual_fixtures,
        "expected_case_count": len(EXPECTED_FIXTURES),
        "actual_case_count": len(actual_fixtures),
        "fixtures_executed": bool(actual_fixtures),
        "fixture_records": harness.fixture_records,
        "assertions": harness.assertions,
        "passed": harness.passed,
        "failed": harness.failed,
        "events": harness.events,
        "status": "pass" if harness.failed == 0 and harness.assertions > 0 else "fail",
    }
    report_path = report_dir / "report.json"
    with report_path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"report": str(report_path), "status": report["status"], "assertions": harness.assertions, "passed": harness.passed, "failed": harness.failed}, ensure_ascii=False))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
