"""Regression: candidate summaries must state what is actually true.

The model reasons from the summaries.  Twice it acted on a belief the summaries
invited rather than contradicted:

* it resumed the game "with pending dig orders marked" while no cell carried a
  designation, because an OFFERED dig candidate reads like a MARKED one;
* it paused "so priorities can be changed" and then resumed without changing
  anything, because the pause summary named no specific pending change.

The fixtures derive from the recorded live state so every reader contract is
satisfied, then override only the fields under test.
"""

from __future__ import annotations

import copy
import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.candidates import (  # noqa: E402
    _marked_dig_count,
    _non_operational_building_count,
    _pause_candidate,
    _queued_work_phrases,
    _resume_candidate,
)

BRIDGE = pathlib.Path.home() / "Documents" / "Klei" / "OxygenNotIncluded" / "ai-bridge"


def live_state() -> dict:
    path = BRIDGE / "latest_state.json"
    if not path.is_file():
        raise unittest.SkipTest("no recorded live state")
    return json.loads(path.read_text(encoding="utf-8-sig"))


def with_digs(state: dict, marked: bool) -> dict:
    snapshot = copy.deepcopy(state)
    cells = snapshot.setdefault("designations", {}).setdefault("dig", {}).setdefault("by_cell", {})
    if not cells:
        cells["cell_138727"] = {
            "cell": 138727, "x": 79, "y": 218, "element_id": "Snow",
            "safe_mark_candidate": True, "solid": True,
        }
    for entry in cells.values():
        entry["designation_present"] = marked
    return snapshot


class CandidateSummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = live_state()

    def test_an_offered_dig_is_not_a_marked_dig(self):
        self.assertEqual(_marked_dig_count(with_digs(self.base, marked=False)), 0)
        self.assertGreater(_marked_dig_count(with_digs(self.base, marked=True)), 0)

    def test_queued_work_phrases_track_the_observation(self):
        idle = with_digs(self.base, marked=False)
        idle["blueprints"] = dict(idle.get("blueprints") or {})
        idle["blueprints"]["by_instance_id"] = {}
        idle["functional_buildings"] = {"instances": []}
        self.assertEqual(_queued_work_phrases(idle), [])

        busy = with_digs(self.base, marked=True)
        busy["functional_buildings"] = {
            "instances": [{"instance_id": 1, "prefab_id": "MineralDeoxidizer", "operational": False}]
        }
        phrases = _queued_work_phrases(busy)
        self.assertTrue(any("marked dig" in item for item in phrases), phrases)
        self.assertTrue(any("not operating" in item for item in phrases), phrases)

    def test_non_operational_buildings_are_counted(self):
        snapshot = copy.deepcopy(self.base)
        snapshot["functional_buildings"] = {
            "instances": [{"instance_id": 1, "prefab_id": "X", "operational": False}]
        }
        self.assertEqual(_non_operational_building_count(snapshot), 1)
        snapshot["functional_buildings"] = {
            "instances": [{"instance_id": 1, "prefab_id": "X", "operational": True}]
        }
        self.assertEqual(_non_operational_building_count(snapshot), 0)

    def test_the_pause_summary_never_claims_a_marked_dig(self):
        summary = _pause_candidate(with_digs(self.base, marked=False))["summary"]
        self.assertNotIn("marked", summary)
        self.assertIn("Pause the game", summary)

    def test_resume_never_claims_marked_digs_that_do_not_exist(self):
        snapshot = with_digs(self.base, marked=False)
        candidate = _resume_candidate(snapshot)
        if candidate is None:
            self.skipTest("resume is refused in the recorded state (a hard alert is open)")
        self.assertNotIn("marked dig", candidate["summary"])
        if not _queued_work_phrases(snapshot):
            self.assertIn("nothing is queued yet", candidate["summary"])

    def test_resume_names_marked_digs_when_they_exist(self):
        snapshot = with_digs(self.base, marked=True)
        candidate = _resume_candidate(snapshot)
        if candidate is None:
            self.skipTest("resume is refused in the recorded state (a hard alert is open)")
        self.assertIn("marked dig cell(s)", candidate["summary"])


if __name__ == "__main__":
    unittest.main()
