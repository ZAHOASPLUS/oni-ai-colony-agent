"""Regression: action outcomes no longer re-parse the whole session archive (review 2026-09-26, L7).

Every Bridge generation looked up the source snapshot and acks of every
confirming/done command in the session by json-parsing every archive line (each
a multi-megabyte snapshot), even for done commands whose intact stored
attestation is returned without reading them.  Per-generation cost grew with
the session (8.4 s at 377 MB) until the Bridge lost trust.
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest
from typing import Any, Dict, Iterable, List
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import action_outcomes  # noqa: E402

SESSION = "session-20260926-120000-abcdef01"


def legacy_source_snapshots(path: pathlib.Path, wanted_ids: Iterable[str]) -> Dict[str, List[Dict[str, Any]]]:
    """The pre-fix implementation, kept verbatim as the reference."""

    wanted = set(wanted_ids)
    found: Dict[str, List[Dict[str, Any]]] = {snapshot_id: [] for snapshot_id in wanted}
    with path.open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            try:
                value = json.loads(line)
            except (json.JSONDecodeError, UnicodeError):
                continue
            if isinstance(value, dict) and value.get("snapshot_id") in wanted:
                found[str(value["snapshot_id"])].append(value)
    return found


def legacy_command_acks(path: pathlib.Path, wanted_ids: Iterable[str]) -> Dict[str, List[Dict[str, Any]]]:
    wanted = set(wanted_ids)
    found: Dict[str, List[Dict[str, Any]]] = {command_id: [] for command_id in wanted}
    with path.open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            try:
                value = json.loads(line)
            except (json.JSONDecodeError, UnicodeError):
                continue
            if isinstance(value, dict) and value.get("event") == "command_ack" and value.get("id") in wanted:
                found[str(value["id"])].append(value)
    return found


def canonical(record: Dict[str, Any]) -> str:
    # The archive writer's exact serialization (bridge/oni_bridge.py).
    return json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class ArchiveScanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="oni-archive-scan-")
        self.addCleanup(self.temp.cleanup)
        self.bridge = pathlib.Path(self.temp.name)
        self.archive = self.bridge / "sessions" / SESSION / "state.jsonl"
        self.archive.parent.mkdir(parents=True)

    def write(self, lines: List[str], *, bom: bool = False, trailing_partial: str = "") -> None:
        payload = ("\ufeff" if bom else "") + "".join(line + "\n" for line in lines) + trailing_partial
        self.archive.write_bytes(payload.encode("utf-8"))

    def tricky_archive(self) -> None:
        big = "x" * 50_000
        lines = [canonical({"schema": "oni.state.v2", "event": "state_snapshot", "snapshot_id": f"{SESSION}:{seq}",
                            "seq": seq, "padding": big, "name": "米波"}) for seq in range(1, 40)]
        lines.insert(5, "not json at all")
        lines.insert(9, canonical(["a", "list", f"{SESSION}:3"]))
        # The wanted id inside another field must not count as a match.
        lines.append(canonical({"event": "note", "evidence": {"snapshot_id_ref": f"{SESSION}:7"},
                                "id": "cmd-a"}))
        # A duplicate snapshot line (ambiguity must stay visible) and acks.
        lines.append(canonical({"event": "state_snapshot", "snapshot_id": f"{SESSION}:7", "seq": 7}))
        lines.append(canonical({"event": "command_ack", "id": "cmd-a", "status": "accepted", "seq": 50}))
        lines.append(canonical({"event": "command_result", "id": "cmd-a", "status": "executed", "seq": 51}))
        lines.append(canonical({"event": "command_ack", "id": "cmd-b", "status": "accepted", "seq": 52}))
        self.write(lines, bom=True, trailing_partial=canonical(
            {"event": "command_ack", "id": "cmd-a", "status": "executed", "seq": 53}))

    def test_scans_match_the_legacy_results(self):
        self.tricky_archive()
        wanted = [f"{SESSION}:1", f"{SESSION}:3", f"{SESSION}:7", f"{SESSION}:99"]
        self.assertEqual(
            action_outcomes._source_snapshots(self.bridge, SESSION, wanted),
            legacy_source_snapshots(self.archive, wanted),
        )
        self.assertEqual(
            action_outcomes._command_acks(self.bridge, SESSION, ["cmd-a", "cmd-b", "cmd-c"]),
            legacy_command_acks(self.archive, ["cmd-a", "cmd-b", "cmd-c"]),
        )
        self.assertEqual(len(action_outcomes._source_snapshots(self.bridge, SESSION, wanted)[f"{SESSION}:7"]), 2)

    def test_only_candidate_lines_are_parsed(self):
        self.tricky_archive()
        real_loads = json.loads
        calls = []

        def counting_loads(text, *args, **kwargs):
            calls.append(len(text))
            return real_loads(text, *args, **kwargs)

        with mock.patch.object(action_outcomes.json, "loads", side_effect=counting_loads):
            action_outcomes._source_snapshots(self.bridge, SESSION, [f"{SESSION}:20"])
        self.assertEqual(len(calls), 1)  # one line out of 46

    def test_stored_confirmations_need_no_archive_lookup(self):
        commands = {
            "cmd-confirmed": {"id": "cmd-confirmed", "session_id": SESSION, "action": "set_paused",
                              "source_snapshot_id": f"{SESSION}:1"},
            "cmd-confirming": {"id": "cmd-confirming", "session_id": SESSION, "action": "set_paused",
                               "source_snapshot_id": f"{SESSION}:2"},
        }
        grouped = {
            "cmd-confirmed": [("done", self.bridge / "x.json", commands["cmd-confirmed"])],
            "cmd-confirming": [("confirming", self.bridge / "y.json", commands["cmd-confirming"])],
        }
        requested = {}

        def stored(command, *, directory_name, location_count):
            return {"stored": True} if command["id"] == "cmd-confirmed" else None

        def sources(bridge_dir, session_id, wanted):
            requested["sources"] = sorted(wanted)
            return {item: [] for item in wanted}

        def acks(bridge_dir, session_id, wanted):
            requested["acks"] = sorted(wanted)
            return {item: [] for item in wanted}

        with mock.patch.object(action_outcomes, "_command_locations", return_value=grouped), \
                mock.patch.object(action_outcomes, "_stored_confirmation", side_effect=stored), \
                mock.patch.object(action_outcomes, "_source_snapshots", side_effect=sources), \
                mock.patch.object(action_outcomes, "_command_acks", side_effect=acks):
            document = action_outcomes.build_action_outcomes(
                self.bridge, {"session_id": SESSION}, {"status": "ok", "trust_ready": True, "trust_blockers": []},
            )
        self.assertEqual(requested, {"sources": [f"{SESSION}:2"], "acks": ["cmd-confirming"]})
        self.assertEqual(document["counts"]["executed_verified"], 2)


if __name__ == "__main__":
    unittest.main()
