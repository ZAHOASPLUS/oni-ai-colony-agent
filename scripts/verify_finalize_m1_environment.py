#!/usr/bin/env python3
"""Offline fixture verification for the M1 finalize path.

Builds the same TestRoot fixture as ``verify_live_validation_environment.py``
(prepare, runtime bootstrap install, runtime config), simulates an M1 model
loop session (a done ``set_paused`` command, rejected suggestions, decision
frames, mutated configs/probe/loader), then exercises
``scripts/finalize_m1_environment.py``:

* what-if is read-only and returns the plan,
* apply restores every manifest binding exactly, preserves the runtime bridge
  as evidence, publishes a self-hashed result and passes the post-boundary
  comparison,
* replay returns the same immutable result,
* refusals: manifest anchor mismatch, game running, nonterminal queue entry,
  quarantine entry, no done evidence (abort case), enabled action policy,
  expected done ids mismatch,
* an injected fault mid-transaction rolls the ONI root back exactly and
  leaves a non-replay latch.

Never touches the live ONI user directory.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from typing import Any, Callable

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
TARGET = SCRIPTS / "finalize_m1_environment.py"
REPORT = ROOT / "reports" / "finalize-m1-environment-verification.json"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import verify_live_validation_environment as env  # noqa: E402
from scripts import manage_live_validation_environment as workflow  # noqa: E402

VerificationFailure = env.VerificationFailure
assert_true = env.assert_true
sha256 = env.sha256


def m1_args(fixture: dict[str, pathlib.Path | str], *extra: str, apply: bool = False, what_if: bool = False) -> list[str]:
    paths = env.fixture_paths(fixture)
    arguments = [
        sys.executable,
        str(TARGET),
        *env.common_args(fixture),
        "--manifest",
        str(paths["manifest"]),
        "--expected-environment-manifest-sha256",
        sha256(paths["manifest"]),
        "--confirm-live-run-id",
        str(fixture["run_id"]),
        *extra,
    ]
    if apply:
        arguments.append("--apply")
    elif what_if:
        arguments.append("--what-if")
    return arguments


def run_m1(
    fixture: dict[str, pathlib.Path | str],
    *extra: str,
    apply: bool = False,
    what_if: bool = False,
    expected: int | set[int] = 0,
) -> subprocess.CompletedProcess[str]:
    return env.run(m1_args(fixture, *extra, apply=apply, what_if=what_if), expected=expected, timeout=180)


def refused(fixture: dict[str, pathlib.Path | str], *extra: str, needle: str) -> str:
    completed = run_m1(fixture, *extra, apply=True, expected=2)
    payload = json.loads(completed.stderr.strip().splitlines()[-1])
    assert_true(payload.get("status") == "refused", "refusal did not report status=refused")
    message = str(payload.get("error") or "")
    assert_true(needle in message, f"refusal reason mismatch: expected {needle!r} in {message!r}")
    return message


def simulate_m1_session(fixture: dict[str, pathlib.Path | str]) -> dict[str, Any]:
    """Runtime installed and configured, then one model-loop session ran."""

    oni = pathlib.Path(fixture["oni"])
    paths = env.fixture_paths(fixture)
    prepared = env.prepare_configured_runtime(fixture)
    bridge = prepared["bridge"]
    probe = prepared["probe"]
    # The Bridge disabled the policy again at its safe stop.
    policy = env.disabled_policy()
    policy["generated_at"] = "2026-09-27T02:31:29+00:00"
    env.write_json(bridge / "action-policy.json", policy)
    done_id = "cmd-model-8312df0daf08b123cd9792c17a7a52e2"
    env.write_json(
        bridge / "commands" / "done" / f"{done_id}.json",
        {
            "id": done_id,
            "action": "set_paused",
            "args": {"paused": False},
            "status": "confirmed",
            "approved_by": "local_human",
            "completed_at": "2026-09-27T10:19:30+08:00",
        },
    )
    for index in range(3):
        rejected_id = f"cmd-{index:032x}"
        env.write_json(
            bridge / "commands" / "rejected" / f"{rejected_id}.json",
            {"id": rejected_id, "action": "focus_note", "status": "rejected", "reason": "suggestions_disabled"},
        )
    for index in range(4):
        env.write_json(
            bridge / "decision-frames" / f"sha256-{index:064x}.json",
            {"schema": "oni.decision-frame.v2", "frame_id": f"sha256:{index:064x}"},
        )
    env.write_jsonl(
        bridge / "state.jsonl",
        [{"schema": "oni.state.v2", "event": "state_snapshot", "session_id": "session-m1-fixture", "seq": 1}],
    )
    env.write_json(bridge / "health.json", {"schema": "oni.bridge-health.v1", "status": "stopped"})
    # The game and the runtime install changed configs, the probe, and the loader.
    env.write_bytes(oni / "kplayerprefs.yaml", b"SaveFilenameKey: RuntimeLoader\n")
    env.write_bytes(oni / "mods" / "mods.json", b'{"version":1,"mods":["runtime"]}\n')
    env.write_bytes(oni / "mods" / "mods.json.bak", b'{"runtime":true}\n')
    env.write_bytes(oni / "mods" / "config.json", b'{"runtime_only":true}\n')
    env.write_bytes(probe / "runtime" / "new-file.bin", b"runtime-only")
    env.write_bytes(paths["loader"], b"mutated-loader-save")
    env.write_bytes(paths["loader_preview"], b"mutated-loader-preview")
    return {"bridge": bridge, "probe": probe, "archived_old": prepared["archived_old"], "done_id": done_id}


def manifest_expectations(fixture: dict[str, pathlib.Path | str]) -> dict[str, Any]:
    paths = env.fixture_paths(fixture)
    return json.loads(paths["manifest"].read_text(encoding="utf-8"))


def assert_restored(fixture: dict[str, pathlib.Path | str], label: str) -> None:
    oni = pathlib.Path(fixture["oni"])
    paths = env.fixture_paths(fixture)
    manifest = manifest_expectations(fixture)
    bindings = manifest["bindings"]
    assert_true(
        workflow.file_state(pathlib.Path(fixture["source"])) == bindings["source_save"]["state"],
        f"{label}: source save changed",
    )
    assert_true(
        workflow.file_state(paths["loader"]) == bindings["loader_save"]["state"],
        f"{label}: loader save not restored exactly",
    )
    assert_true(
        workflow.file_state(paths["loader_preview"]) == bindings["loader_preview"]["state"],
        f"{label}: loader preview not restored exactly",
    )
    for record in manifest["backup"]["configs"]:
        actual = workflow.optional_file_state(oni / pathlib.Path(record["relative"]))
        expected = {key: value for key, value in record.items() if key in {"exists", "state"}}
        assert_true(actual == expected, f"{label}: config {record['relative']} not restored exactly")
    workflow.verify_tree(oni / "mods" / "Dev" / "oni-ai-colony-agent", manifest["backup"]["probe"]["snapshot"], label)
    workflow.verify_tree(oni / "ai-bridge", manifest["backup"]["old_bridge"]["snapshot"], label)
    workflow.verify_tree(oni / "ai-bridge-archives", manifest["backup"]["bridge_archive_root"]["snapshot"], label)


def test_syntax(base: pathlib.Path) -> dict[str, Any]:
    del base
    import py_compile

    with tempfile.NamedTemporaryFile(suffix=".pyc", delete=False) as handle:
        compiled = pathlib.Path(handle.name)
    try:
        py_compile.compile(str(TARGET), cfile=str(compiled), doraise=True)
        py_compile.compile(str(pathlib.Path(__file__).resolve()), cfile=str(compiled), doraise=True)
    finally:
        compiled.unlink(missing_ok=True)
    help_text = env.run([sys.executable, str(TARGET), "--help"]).stdout
    assert_true("--expected-environment-manifest-sha256" in help_text, "parser lacks the manifest anchor")
    assert_true("--test-fail-after-step" not in help_text, "test-only option leaked into help")
    return {"compiled": True, "help_ok": True}


def test_whatif_apply_replay(base: pathlib.Path) -> dict[str, Any]:
    fixture = env.make_fixture(base, "m1-success", run_id="m1-fixture-success")
    env.prepare_fixture(fixture)
    session = simulate_m1_session(fixture)
    oni = pathlib.Path(fixture["oni"])
    paths = env.fixture_paths(fixture)
    environment = paths["environment"]
    before = env.file_inventory(oni)
    runtime_bridge_before = workflow.snapshot_tree(session["bridge"])

    plan = json.loads(run_m1(fixture, what_if=True).stdout)
    assert_true(plan.get("status") == "would_finalize_m1", "what-if status is wrong")
    assert_true(plan.get("write_count") == 0, "what-if reported writes")
    assert_true(env.file_inventory(oni) == before, "what-if changed the ONI fixture")
    assert_true(not (environment / "finalization-m1").exists(), "what-if created the finalization root")
    labels = [step["label"] for step in plan["steps"]]
    for required in (
        "archive_loader_save",
        "restore_loader_save",
        "archive_current_probe",
        "restore_probe",
        "archive_runtime_bridge",
        "restore_old_bridge",
        "restore_archive_root_timestamp",
    ):
        assert_true(required in labels, f"plan lacks step {required}")
    assert_true(
        [item["command_id"] for item in plan["session_window"]["done_commands"]] == [session["done_id"]],
        "plan did not bind the done command",
    )
    assert_true(plan["session_window"]["command_stage_counts"]["rejected"] == 3, "rejected count is wrong")
    assert_true(plan["session_window"]["decision_frame_count"] == 4, "decision frame count is wrong")

    result = json.loads(run_m1(fixture, "--expect-done-command-id", session["done_id"], apply=True).stdout)
    assert_true(result.get("status") == "finalized", "apply did not finalize")
    assert_true(
        result.get("document_hash")
        == workflow.contract_hash_without(result, "document_hash", "result"),
        "result self-hash is wrong",
    )
    assert_true(result["post_boundary"]["status"] == "passed", "post-boundary comparison did not pass")
    assert_restored(fixture, "apply")
    rb = environment / "rb"
    workflow.verify_tree(rb, runtime_bridge_before, "preserved runtime bridge")
    assert_true((rb / "commands" / "done" / f"{session['done_id']}.json").is_file(), "done evidence not preserved")
    assert_true(not session["archived_old"].exists(), "archived old bridge was left in the archive root")
    final_root = environment / "finalization-m1"
    for name in ("finalize-m1-result.json", "session-window.json", "transaction-plan.json", "post-boundary-comparison.json"):
        assert_true((final_root / name).is_file(), f"missing evidence {name}")
    assert_true(
        (final_root / "runtime-current" / "probe" / "runtime" / "new-file.bin").is_file(),
        "runtime probe was not preserved as evidence",
    )
    assert_true(
        (final_root / "loader-before-restore" / paths["loader"].name).read_bytes() == b"mutated-loader-save",
        "pre-restore loader was not preserved",
    )
    journal = workflow.verify_journal(environment / "restore-journal.jsonl")
    assert_true(journal[-1]["event"] == "environment_finalized_m1", "terminal journal event missing")
    result_sha = sha256(final_root / "finalize-m1-result.json")
    assert_true(journal[-1]["data"]["finalize_m1_result_sha256"] == result_sha, "journal does not bind the result")

    after = env.file_inventory(oni)
    replay = json.loads(run_m1(fixture, apply=True).stdout)
    assert_true(replay.get("status") == "already_finalized", "replay status is wrong")
    assert_true(replay.get("result") == result, "replay returned a different result")
    assert_true(env.file_inventory(oni) == after, "replay changed the ONI fixture")
    assert_true(sha256(final_root / "finalize-m1-result.json") == result_sha, "replay rewrote the result")
    replay_plan = json.loads(run_m1(fixture, what_if=True).stdout)
    assert_true(replay_plan.get("status") == "already_finalized", "what-if after finalize is not a replay")

    # A tampered result must be refused rather than trusted.
    tampered = dict(result)
    tampered["status"] = "forged"
    env.make_fixture_file_writable(final_root / "finalize-m1-result.json")
    (final_root / "finalize-m1-result.json").write_text(json.dumps(tampered), encoding="utf-8")
    refused(fixture, needle="self-hash")
    return {
        "whatif_read_only": True,
        "apply_restored_exact": True,
        "runtime_bridge_preserved": True,
        "post_boundary_passed": True,
        "replay_identical": True,
        "tampered_result_refused": True,
        "step_count": len(plan["steps"]),
    }


def test_refusals(base: pathlib.Path) -> dict[str, Any]:
    fixture = env.make_fixture(base, "m1-refusals", run_id="m1-fixture-refusals")
    env.prepare_fixture(fixture)
    session = simulate_m1_session(fixture)
    oni = pathlib.Path(fixture["oni"])
    paths = env.fixture_paths(fixture)
    bridge = session["bridge"]
    before = env.file_inventory(oni)
    evidence: dict[str, Any] = {}

    # 1. Manifest anchor mismatch.
    bad = m1_args(fixture, apply=True)
    bad[bad.index("--expected-environment-manifest-sha256") + 1] = "sha256:" + "0" * 64
    completed = env.run(bad, expected=2, timeout=180)
    assert_true("external SHA-256 anchor" in completed.stderr, "manifest anchor mismatch was not refused")
    evidence["manifest_anchor"] = "refused"

    # 2. Game still running (process fixture says one ONI process).
    running = m1_args(fixture, apply=True)
    running[running.index("--process-state-fixture") + 1] = str(fixture["game"])
    completed = env.run(running, expected=2, timeout=180)
    assert_true("must be stopped" in completed.stderr, "running game was not refused")
    evidence["game_running"] = "refused"

    # 3. Nonterminal queue entry.
    pending = bridge / "commands" / "pending" / "cmd-pending.json"
    env.write_json(pending, {"id": "cmd-pending", "action": "noop", "status": "pending"})
    refused(fixture, needle="nonterminal")
    pending.unlink()
    evidence["nonterminal_entry"] = "refused"

    # 4. Quarantine entry.
    quarantine = bridge / "commands" / "quarantine" / "cmd-q.json"
    env.write_json(quarantine, {"id": "cmd-q", "action": "noop", "status": "quarantined"})
    refused(fixture, needle="quarantine")
    quarantine.unlink()
    evidence["quarantine_entry"] = "refused"

    # 5. Expected done ids mismatch.
    refused(fixture, "--expect-done-command-id", "cmd-not-there", needle="expected done command ids")
    evidence["expected_done_mismatch"] = "refused"

    # 6. Action policy still enabled.
    enabled = env.disabled_policy()
    enabled["actions_enabled"] = True
    policy_path = bridge / "action-policy.json"
    original_policy = policy_path.read_bytes()
    policy_stat = policy_path.stat()
    env.write_json(policy_path, enabled)
    refused(fixture, needle="action policy")
    policy_path.write_bytes(original_policy)
    os.utime(policy_path, ns=(policy_stat.st_atime_ns, policy_stat.st_mtime_ns))
    evidence["policy_enabled"] = "refused"

    # 7. No done evidence at all: this is the abort case, not M1.
    done = bridge / "commands" / "done" / f"{session['done_id']}.json"
    kept = done.read_bytes()
    done_stat = done.stat()
    done.unlink()
    refused(fixture, needle="not an M1 run")
    done.write_bytes(kept)
    os.utime(done, ns=(done_stat.st_atime_ns, done_stat.st_mtime_ns))
    evidence["no_done_evidence"] = "refused"

    # 8. Source save tampered after prepare.
    source = pathlib.Path(fixture["source"])
    source_bytes = source.read_bytes()
    source_stat = source.stat()
    env.make_fixture_file_writable(source)
    source.write_bytes(source_bytes + b"x")
    os.utime(source, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))
    refused(fixture, needle="source_save")
    source.write_bytes(source_bytes)
    os.utime(source, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))
    evidence["source_tamper"] = "refused"

    assert_true(env.file_inventory(oni) == before, "a refusal mutated the ONI fixture")
    assert_true(not (paths["environment"] / "finalization-m1").exists(), "a refusal created the finalization root")
    assert_true(not (paths["environment"] / "rb").exists(), "a refusal moved the runtime bridge")
    evidence["zero_writes_across_refusals"] = True
    return evidence


def test_fault_rollback_and_latch(base: pathlib.Path) -> dict[str, Any]:
    fixture = env.make_fixture(base, "m1-fault", run_id="m1-fixture-fault")
    env.prepare_fixture(fixture)
    simulate_m1_session(fixture)
    oni = pathlib.Path(fixture["oni"])
    paths = env.fixture_paths(fixture)
    plan = json.loads(run_m1(fixture, what_if=True).stdout)
    step_count = len(plan["steps"])
    fail_after = step_count - 2  # after the runtime bridge moved, before the archive timestamp
    before = env.file_inventory(oni)
    completed = run_m1(fixture, "--test-fail-after-step", str(fail_after), apply=True, expected=2)
    assert_true("injected" in completed.stderr, "injected fault did not surface")
    after = env.file_inventory(oni)
    assert_true(after == before, "rollback did not restore the ONI root exactly")
    journal = workflow.verify_journal(paths["environment"] / "restore-journal.jsonl")
    events = [entry["event"] for entry in journal]
    assert_true("restore_rollback_complete" in events, "rollback completion was not journaled")
    assert_true(events.count("restore_step_completed") == fail_after, "step count before fault is wrong")
    # The attempt left evidence without a result: replay must be refused.
    refused(fixture, needle="manual recovery required")
    assert_true(env.file_inventory(oni) == before, "the refused replay mutated the ONI root")
    return {
        "fault_after_step": fail_after,
        "rollback_exact": True,
        "rollback_journaled": True,
        "replay_after_fault_refused": True,
    }


def main() -> int:
    if not env.POWERSHELL:
        raise SystemExit("PowerShell is required")
    tests: list[tuple[str, Callable[[pathlib.Path], dict[str, Any]]]] = [
        ("syntax_and_parser", test_syntax),
        ("whatif_apply_replay_exact_restore", test_whatif_apply_replay),
        ("zero_write_refusals", test_refusals),
        ("fault_rollback_and_nonreplay_latch", test_fault_rollback_and_latch),
    ]
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="om1-") as temporary:
        fixture_root = pathlib.Path(temporary).resolve()
        for name, test in tests:
            try:
                results.append({"name": name, "status": "passed", "evidence": test(fixture_root)})
            except BaseException as exc:  # noqa: BLE001 - report every failure
                results.append({"name": name, "status": "failed", "error": env.sanitize_error(exc, fixture_root)})
    passed = sum(item["status"] == "passed" for item in results)
    payload = {
        "schema": "oni.finalize-m1-environment-verification.v1",
        "status": "passed" if passed == len(results) else "failed",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "validation_level": "offline_manifest_transaction_fixture",
        "scope": "temporary_TestRoot_only",
        "live_oni_accessed": False,
        "game_or_bridge_launched": False,
        "test_count": len(results),
        "passed_count": passed,
        "failed_count": len(results) - passed,
        "scripts": {
            TARGET.name: sha256(TARGET),
            "manage_live_validation_environment.py": sha256(SCRIPTS / "manage_live_validation_environment.py"),
            pathlib.Path(__file__).name: sha256(pathlib.Path(__file__).resolve()),
        },
        "tests": results,
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    temporary_report = REPORT.with_name(f".{REPORT.name}.{os.getpid()}.tmp")
    temporary_report.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary_report, REPORT)
    print(json.dumps({"status": payload["status"], "passed": passed, "total": len(results)}))
    for item in results:
        if item["status"] != "passed":
            print(f"{item['name']}: {item['error']}", file=sys.stderr)
    return 0 if payload["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
