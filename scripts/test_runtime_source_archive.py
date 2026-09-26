"""Offline tests for exact source archival before the command TTL starts."""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import io
import pathlib
import tempfile
import unittest
from unittest import mock

from scripts import verify_runtime as runtime
from scripts.verify_colony_plan import state, write_live_state
from scripts.verify_immutable_decision_frames import case_runtime_prepare_binds_exact_context


class RuntimeSourceArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="source-archive-")
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.bridge = self.root / "offline-bridge"
        snapshot = state()
        snapshot["game"]["paused"] = False
        snapshot["bridge"].update(trust_ready=True, trust_blockers=[], replay_mode=False)
        write_live_state(self.bridge, snapshot)
        frame = runtime.load_json(self.bridge / "decision-frame.json")
        health = runtime.load_json(self.bridge / "health.json")
        health.update(
            updated_at=runtime.local_now().isoformat(timespec="milliseconds"),
            decision_frame_id=frame["frame_id"],
            decision_frame_trust=frame["trust"]["status"],
            decision_frame_readiness={key: bool(value["ready"]) for key, value in frame["readiness"].items()},
        )
        runtime.write_json_atomic(self.bridge / "health.json", health)
        self.evidence = self.root / "evidence"
        self.args = argparse.Namespace(bridge_dir=str(self.bridge), action="set_paused",
                                       paused="true", source_evidence_dir=self.evidence)

    def prepare(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return runtime.prepare_command(self.args)

    def pending(self):
        return list((self.bridge / "commands" / "pending").glob("*.json"))

    def test_exact_bundle_archived_before_ttl(self):
        clock = [runtime.local_now()]
        initial = clock[0]
        real_reader = runtime._read_json_object
        advanced = []

        def delayed_archive_read(path, label):
            self.assertEqual(self.pending(), [])
            if not advanced:
                clock[0] += dt.timedelta(seconds=5)
                advanced.append(True)
            return real_reader(path, label)

        with mock.patch.object(runtime, "local_now", side_effect=lambda: clock[0]), \
             mock.patch.object(runtime, "_read_json_object", side_effect=delayed_archive_read):
            self.assertEqual(self.prepare(), 0)
        command = runtime.load_json(self.pending()[0])
        manifest = runtime.load_json(self.evidence / "source-manifest.json")
        created = dt.datetime.fromisoformat(command["created_at"])
        expires = dt.datetime.fromisoformat(command["expires_at"])
        self.assertGreaterEqual(created, (initial + dt.timedelta(seconds=5)).replace(microsecond=0))
        self.assertEqual((expires - created).total_seconds(), 30)
        self.assertEqual(manifest["status"], "archived")
        self.assertLessEqual(dt.datetime.fromisoformat(manifest["completed_at"]), created)
        self.assertEqual(command["source_frame_id"], manifest["source_frame_id"])
        self.assertEqual(command["source_snapshot_id"], manifest["source_snapshot_id"])
        self.assertEqual(command["source"]["policy_hash"], manifest["source_policy_hash"])
        self.assertEqual(set(manifest["files"]), {"latest_state.json", "health.json",
                         "decision-frame.json", "archived-decision-frame.json", "action-policy.json"})
        for name, description in manifest["files"].items():
            payload = (self.evidence / name).read_bytes()
            self.assertEqual(description["sha256"], hashlib.sha256(payload).hexdigest())
            self.assertEqual(description["bytes"], len(payload))
            if name != "archived-decision-frame.json":
                self.assertEqual(payload, (self.bridge / name).read_bytes())

    def test_source_change_during_copy_rejects_without_command(self):
        real_reader = runtime._read_json_object

        def changed_reader(path, label):
            value, payload = real_reader(path, label)
            if path.name == "latest_state.json":
                path.write_bytes(payload + b" ")
            return value, payload

        with mock.patch.object(runtime, "_read_json_object", side_effect=changed_reader):
            with self.assertRaisesRegex(runtime.DecisionFrameArchiveError, "changed during command preparation"):
                self.prepare()
        self.assertEqual(self.pending(), [])
        self.assertEqual(runtime.load_json(self.evidence / "source-manifest.json")["status"], "archive_failed")

    def test_mismatched_health_rejects_before_archival_or_command(self):
        health = runtime.load_json(self.bridge / "health.json")
        health["decision_frame_id"] = "wrong-frame"
        runtime.write_json_atomic(self.bridge / "health.json", health)
        with self.assertRaisesRegex(RuntimeError, "health_decision_frame_id_mismatch"):
            self.prepare()
        self.assertFalse(self.evidence.exists())
        self.assertEqual(self.pending(), [])

    def test_existing_evidence_is_not_overwritten(self):
        self.evidence.mkdir()
        marker = self.evidence / "keep.txt"
        marker.write_text("original", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            self.prepare()
        self.assertEqual(marker.read_text(encoding="utf-8"), "original")
        self.assertEqual(self.pending(), [])

    def test_bridge_cannot_be_evidence_destination(self):
        self.args.source_evidence_dir = self.bridge / "evidence"
        with self.assertRaisesRegex(RuntimeError, "outside the Bridge"):
            self.prepare()
        self.assertEqual(self.pending(), [])

    def test_existing_prepare_binding_contract_without_new_option(self):
        case_runtime_prepare_binds_exact_context(self.root / "existing-contract")


if __name__ == "__main__":
    unittest.main(verbosity=2)
