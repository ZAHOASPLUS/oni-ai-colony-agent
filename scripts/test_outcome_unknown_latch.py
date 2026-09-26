"""Regression: the outcome_unknown latch is durable (code review 2026-09-26, P0-2).

The pipeline gate ignored a failed outcome_unknown record once the latest
snapshot's save_id_hash differed, but that hash is a content fingerprint that
changes on every successful save, so a planned checkpoint save released the
latch.  Failed mutations without a classification (a raw executing claim
moved to failed/) never latched at all.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import command_custody as custody  # noqa: E402
from agent.command_queue import command_pipeline_readiness  # noqa: E402
from scripts import resolve_outcome_unknown  # noqa: E402

H1 = "sha256:" + "1" * 64
H2 = "sha256:" + "2" * 64


def failed(command_id: str, action: str = "set_paused", **fields) -> dict:
    record = {
        "schema": custody.COMMAND_SCHEMA,
        "id": command_id,
        "action": action,
        "status": "failed",
        "save_id_hash": H1,
        "session_id": "s1",
        "world_id": 0,
    }
    record.update(fields)
    return record


class OutcomeUnknownLatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="oni-latch-")
        self.addCleanup(self.temp.cleanup)
        self.bridge = pathlib.Path(self.temp.name) / "bridge"
        self.bridge.mkdir()
        custody.recover_lifecycle_transactions(self.bridge)

    def put(self, value: dict) -> None:
        path = self.bridge / "commands" / "failed" / f"{value['id']}.json"
        path.write_text(json.dumps(value), encoding="utf-8")

    def gate(self, save_hash: str = H1):
        result = command_pipeline_readiness(
            self.bridge, latest_state={"save_id_hash": save_hash}, proposal_frame=None, policy=None
        )
        return result["ready"], result["blockers"]

    def test_latch_survives_save_hash_rotation(self):
        self.put(failed("cmd-a", result={"classification": "outcome_unknown"}))
        for save_hash in (H1, H2):
            ready, blockers = self.gate(save_hash)
            self.assertFalse(ready)
            self.assertIn("mutation_outcome_unknown_unresolved:cmd-a", blockers)

    def test_unclassified_failed_mutation_latches(self):
        raw_claim = failed(
            "cmd-b",
            status="executing",
            execution_claim={"schema": "oni.execution-claim.v1", "dispatch": {"state": "returned"}},
        )
        self.put(raw_claim)
        ready, blockers = self.gate()
        self.assertFalse(ready)
        self.assertIn("mutation_outcome_unknown_unresolved:cmd-b", blockers)

    def test_definite_pre_dispatch_failures_do_not_latch(self):
        for index, classification in enumerate(("behavior_fail", "rejected", "runtime_fail")):
            self.put(failed(f"cmd-d{index}", result={"classification": classification}))
        self.put(failed("cmd-note", action="focus_note"))
        self.assertEqual(self.gate(), (True, []))

    def test_operator_resolution_clears_the_latch(self):
        raw_claim = failed("cmd-c", status="executing")
        self.put(raw_claim)
        self.assertFalse(self.gate()[0])
        preview = resolve_outcome_unknown.resolve(
            self.bridge, "cmd-c", "effect_absent", "checked in game", what_if=True
        )
        self.assertEqual(preview["status"], "what_if")
        self.assertFalse(self.gate()[0])  # what-if never writes

        result = resolve_outcome_unknown.resolve(
            self.bridge,
            "cmd-c",
            "effect_absent",
            "Game still unpaused; command had no effect.",
            now=dt.datetime(2026, 9, 26, tzinfo=dt.timezone.utc),
        )
        self.assertEqual(result["status"], "resolved")
        self.assertEqual(self.gate(), (True, []))
        stored = json.loads((self.bridge / "commands" / "failed" / "cmd-c.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["status"], "failed")
        self.assertEqual(stored["operator_resolution"]["original_status"], "executing")
        custody.recover_lifecycle_transactions(self.bridge)  # canonical again

    def test_resolution_refuses_records_that_do_not_latch(self):
        self.put(failed("cmd-e", result={"classification": "behavior_fail"}))
        with self.assertRaises(resolve_outcome_unknown.ResolutionError):
            resolve_outcome_unknown.resolve(self.bridge, "cmd-e", "effect_absent", "not needed")
        with self.assertRaises(resolve_outcome_unknown.ResolutionError):
            resolve_outcome_unknown.resolve(self.bridge, "cmd-e", "maybe", "bad outcome")


if __name__ == "__main__":
    unittest.main()
