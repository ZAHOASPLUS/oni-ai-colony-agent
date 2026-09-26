"""Temporary-directory integration: real approval, simulated Probe, real confirmation.

These tests never connect to the user's Bridge or launch ONI. Simulated executor
records validate orchestration, not real-game acceptance.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import io
import json
import os
import pathlib
import shutil
import secrets
import tempfile
import threading
import time
import unittest
from unittest import mock

from agent.action_outcomes import build_action_outcomes, commit_confirmed_outcomes
from agent.command_custody import lifecycle_custody
from agent.field_contracts import ALERTS_CANONICAL_CONTRACT_ID, contract_quality_fixture
from scripts import verify_runtime as runtime
from scripts import verify_colony_plan as fixtures
from scripts.verify_action_outcomes import direct_state, make_command_ack, health_for


SHELL = pathlib.Path(shutil.which("pwsh") or pathlib.Path.home() /
    ".cache/codex-runtimes/codex-primary-runtime/dependencies/native/powershell/pwsh.exe")


@unittest.skipUnless(SHELL.is_file(), "PowerShell 7 is required for the actual approval entrypoint")
class PauseRoundtripTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="oni-pause-offline-")
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        if os.environ.get("ONI_TEST_EVIDENCE_DIR"):
            evidence_root = pathlib.Path(os.environ["ONI_TEST_EVIDENCE_DIR"]).resolve()
            evidence_root.mkdir(parents=True, exist_ok=True)
            self.addCleanup(shutil.copytree, self.root, evidence_root / self._testMethodName)
        self.bridge = self.root / "offline-bridge"
        self.evidence = self.root / "evidence"
        self.scratch = self.root / "approval-temp"
        self.scratch.mkdir()
        self.expected = {"session_id": "offline-pause-session", "validation_run_id": "offline-run",
                         "validation_process_id": 12345, "startup_binding_hash": "sha256:" + "b" * 64}
        snapshot = fixtures.state(session_id=self.expected["session_id"], seq=10)
        snapshot.update(self.expected)
        snapshot["quality"]["alerts"] = contract_quality_fixture(ALERTS_CANONICAL_CONTRACT_ID)
        snapshot["bridge"].update(trust_ready=True, trust_blockers=[], replay_mode=False)
        self.publish(snapshot)
        self.args = argparse.Namespace(bridge_dir=str(self.bridge), evidence_dir=self.evidence,
            session_id=self.expected["session_id"], run_id=self.expected["validation_run_id"],
            validation_process_id=12345, startup_binding_hash=self.expected["startup_binding_hash"],
            powershell=SHELL, phase_timeout_seconds=10.0, poll_seconds=0.05)
        self.stop = threading.Event()
        self.simulator_errors = []
        self.executed = []

    def publish(self, snapshot, *, blocked=False, preserve_ts=False):
        staged = {}
        with (mock.patch.object(fixtures, "iso_now", return_value=snapshot["ts"]) if preserve_ts else contextlib.nullcontext()), \
             mock.patch.object(fixtures, "write_json_atomic", side_effect=lambda path, value: staged.update({path: copy.deepcopy(value)})):
            fixtures.write_live_state(self.bridge, snapshot,
                proposal_blockers=["mutation_confirming_in_flight:offline-command"] if blocked else None)
        frame = staged[self.bridge / "decision-frame.json"]
        health = staged[self.bridge / "health.json"]
        health.update(updated_at=runtime.local_now().isoformat(timespec="milliseconds"),
                      decision_frame_id=frame["frame_id"], decision_frame_trust=frame["trust"]["status"],
                      decision_frame_readiness={key: bool(value["ready"]) for key, value in frame["readiness"].items()})
        for path, value in staged.items():
            runtime.write_json_atomic(path, value)
        return runtime.load_json(self.bridge / "latest_state.json")

    def simulator(self, *, omit_ack=False, trust_loss=False, unknown_result=False):
        try:
            while not self.stop.wait(0.01):
                files = list((self.bridge / "commands/approved").glob("*.json"))
                if not files:
                    continue
                approval_log = self.evidence / f"C{len(self.executed) + 1}" / "approval.stdout.txt"
                if not approval_log.exists() or b"Approved command" not in approval_log.read_bytes():
                    continue
                path = files[0]
                command = runtime.load_json(path)
                source = runtime.load_json(self.bridge / "latest_state.json")
                seq = source["seq"]
                target = command["args"]["paused"]
                witness = copy.deepcopy(source)
                witness.update(seq=seq + 4, snapshot_seq=source["snapshot_seq"] + 1,
                               snapshot_id=source["session_id"] + ":" + str(seq + 4))
                witness["game"]["paused"] = target
                command.update(status="executed_pending_snapshot", attempt_id="attempt-" + secrets.token_hex(16),
                               accepted_ack_seq=seq + 1, executed_ack_seq=seq + 2, local_effect_ack_seq=seq + 3,
                               completed_at=runtime.local_now().isoformat(timespec="milliseconds"),
                               result={"before_state": direct_state(source), "after_state": direct_state(witness)})
                confirming = self.bridge / "commands/confirming" / path.name
                # Only the executor is simulated; production approval already created this command.
                with lifecycle_custody(self.bridge):
                    path.unlink()
                    runtime.write_json_atomic(confirming, command)
                self.executed.append(command["id"])
                if unknown_result:
                    return
                history = self.bridge / "sessions" / source["session_id"] / "state.jsonl"
                history.parent.mkdir(parents=True, exist_ok=True)
                # F1 is already in the session log when it becomes C2's source.
                records = [] if history.exists() else [source]
                for status in ("accepted", "executed", "local_effect_observed"):
                    if omit_ack and status == "local_effect_observed":
                        continue
                    ack = make_command_ack(source, command, status)
                    ack["ts"] = runtime.local_now().isoformat(timespec="milliseconds")
                    records.append(ack)
                witness = self.publish(witness, blocked=True)
                records.append(witness)
                with history.open("a", encoding="utf-8") as stream:
                    for record in records:
                        stream.write(json.dumps(record) + "\n")
                # Require the runner to persist F1 while proposals are blocked, before confirmation.
                rejection = self.evidence / f"C{len(self.executed)}" / "first-candidate-rejection.json"
                deadline = time.monotonic() + 3
                while not rejection.exists() and not self.stop.is_set() and time.monotonic() < deadline:
                    time.sleep(0.01)
                if not rejection.exists():
                    raise AssertionError("target observation was not immediately preserved before confirmation")
                if trust_loss:
                    health = runtime.load_json(self.bridge / "health.json")
                    health.update(status="untrusted_snapshot", trust_ready=False, trust_blockers=["sequence_gap"])
                    runtime.write_json_atomic(self.bridge / "health.json", health)
                    return
                certificate = build_action_outcomes(self.bridge, witness, health_for(witness))
                paths = [self.bridge / "action-outcomes.json", history.parent / "action-outcomes.json"]
                for certificate_path in paths:
                    runtime.write_json_atomic(certificate_path, certificate)
                committed = commit_confirmed_outcomes(self.bridge, certificate, publication_paths=tuple(paths))
                if omit_ack:
                    self.assertEqual(committed, [])
                    return
                self.assertIn(command["id"], committed,
                              [(item.get("confirmation_status"), item.get("blockers"))
                               for item in certificate.get("outcomes", [])])
                self.publish(witness, preserve_ts=True)
        except Exception as exc:
            self.simulator_errors.append(repr(exc))

    def run_driver(self, **simulator_options):
        worker = threading.Thread(target=self.simulator, kwargs=simulator_options, daemon=True)
        worker.start()
        try:
            with mock.patch.dict(os.environ, {"TMP": str(self.scratch), "TEMP": str(self.scratch)}), \
                 contextlib.redirect_stdout(io.StringIO()):
                code = runtime.pause_roundtrip(self.args)
        finally:
            self.stop.set()
            worker.join(4)
        self.assertFalse(worker.is_alive(), "offline simulator did not stop")
        return code, runtime.load_json(self.evidence / "pause-roundtrip.json")

    def test_formal_approval_and_confirmation_true_false_true(self):
        large = runtime.load_json(self.bridge / "latest_state.json")
        large["offline_size_payload"] = "x" * (3 * 1024 * 1024)
        self.publish(large)
        code, report = self.run_driver()
        self.assertEqual(self.simulator_errors, [])
        self.assertEqual(code, 0, report["first_failure"])
        self.assertEqual(len(set(report["commands"])), 2)
        self.assertEqual([report["stages"][name]["paused"] for name in ("F0", "F1", "F2")], [True, False, True])
        self.assertTrue(report["actions_disabled"])
        for number in (1, 2):
            archive = self.evidence / f"C{number}"
            command = runtime.load_json(archive / "confirmed-command.json")
            self.assertIn("approval_receipt", command)
            self.assertEqual(command["status"], "confirmed")
            self.assertTrue(runtime.load_json(archive / "confirmation.json")["accepted"])
            self.assertTrue((archive / "execution-records.jsonl").is_file())
            self.assertTrue((archive / "first-candidate-rejection.json").is_file())
            self.assertGreater((archive / "source/latest_state.json").stat().st_size, 3 * 1024 * 1024)
        c2 = runtime.load_json(self.evidence / "C2" / "pending-command.json")
        self.assertEqual(c2["source_snapshot_id"], report["stages"]["F1"]["snapshot_id"])

    def test_missing_real_ack_never_authorizes_c2(self):
        self.args.phase_timeout_seconds = 2.5
        code, report = self.run_driver(omit_ack=True)
        self.assertEqual(self.simulator_errors, [])
        self.assertEqual(code, 2)
        self.assertEqual(len(report["commands"]), 1)
        self.assertNotIn("C2", report["stages"])
        self.assertTrue(report["actions_disabled"])
        self.assertIn("phase_timeout", report["first_failure"]["error"])

    def test_trust_loss_preserves_candidate_and_stops(self):
        code, report = self.run_driver(trust_loss=True)
        self.assertEqual(self.simulator_errors, [])
        self.assertEqual(code, 2)
        self.assertEqual(len(report["commands"]), 1)
        self.assertIn("trusted_observation_rejected", report["first_failure"]["error"])
        self.assertTrue((self.evidence / "C1/first-candidate-rejection.json").is_file())
        self.assertNotIn("C2", report["stages"])

    def test_wrong_session_cannot_mutate_policy_or_commands(self):
        self.args.session_id = "wrong-session"
        original = (self.bridge / "action-policy.json").read_bytes()
        with contextlib.redirect_stdout(io.StringIO()):
            code = runtime.pause_roundtrip(self.args)
        report = runtime.load_json(self.evidence / "pause-roundtrip.json")
        self.assertEqual(code, 2)
        self.assertEqual(original, (self.bridge / "action-policy.json").read_bytes())
        self.assertEqual(report["commands"], [])
        self.assertIn("identity_mismatch:session_id", report["first_failure"]["error"])

    def test_expired_approval_is_preserved_without_replay(self):
        base = runtime.command_base

        def expired(state, action, label, ttl_seconds, **kwargs):
            return base(state, action, label, -1, **kwargs)

        with mock.patch.object(runtime, "command_base", side_effect=expired):
            code, report = self.run_driver()
        self.assertEqual(code, 2)
        self.assertEqual(self.executed, [])
        self.assertEqual(len(report["commands"]), 1)
        self.assertNotIn("C2", report["stages"])
        self.assertLess(report["first_failure"]["command_remaining_seconds"], 0)
        stderr = (self.evidence / "C1/approval.stderr.txt").read_text(encoding="utf-8-sig")
        self.assertIn("Command expired before approval", stderr)
        command = runtime.load_json(self.evidence / "C1/pending-command.json")
        still_pending = runtime.load_json(self.bridge / "commands/pending" / (command["id"] + ".json"))
        self.assertEqual(command, still_pending)

    def test_unknown_execution_result_is_not_resent(self):
        self.args.phase_timeout_seconds = 2.5
        code, report = self.run_driver(unknown_result=True)
        self.assertEqual(code, 2)
        self.assertEqual(len(report["commands"]), 1)
        self.assertEqual(len(self.executed), 1)
        self.assertNotIn("F1", report["stages"])
        self.assertNotIn("C2", report["stages"])
        self.assertEqual(report["retries"], 0)
        self.assertTrue((self.bridge / "commands/confirming" / (report["commands"][0] + ".json")).exists())

    def test_prepare_failure_after_publication_preserves_pending_evidence(self):
        with mock.patch.object(runtime, "record_runtime_intent", side_effect=OSError("offline intent write failure")):
            code, report = self.run_driver()
        self.assertEqual(code, 2)
        self.assertEqual(self.executed, [])
        self.assertEqual(len(report["commands"]), 1)
        self.assertEqual(report["commands_discovered_after_prepare_failure"], report["commands"])
        self.assertIn("offline intent write failure", report["first_failure"]["error"])
        self.assertTrue((self.evidence / (report["commands"][0] + "-final-copies.json")).exists())
        self.assertTrue((self.bridge / "commands/pending" / (report["commands"][0] + ".json")).exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
