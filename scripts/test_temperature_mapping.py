"""Roadmap B6: temperature observation and the alert-to-candidate mapping.

Phase 2 ran into ``hypothermia`` on a world whose material went down to
-86.5 °C, and nothing in the candidate set answered it.  The Probe now
publishes which duplicant has the temperature state, their body and cell
temperatures, and each dig candidate cell's temperature (diagnostic only).  The
candidates then lower the exposed duplicant's Dig priority, withhold dig runs in
extreme material and offer the mildest run first.

The base state is the recorded Phase 2 snapshot: paused, with ``hypothermia``
open, so only the new Probe fields are added here.
"""

from __future__ import annotations

import copy
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import candidates as C  # noqa: E402
from agent.advanced_actions import build_duplicant_priority_hint  # noqa: E402
from agent.model_loop import colony_summary  # noqa: E402
from scripts.recorded_state import load_recorded_policy, load_recorded_state  # noqa: E402

COLD_ID = 6617  # Dig priority 5 in the recording.
PROBE_SOURCE = ROOT / "mod" / "Source" / "Probe" / "ProbeStateReader.cs"


def _observed() -> dict:
    return {"status": "observed", "source": "test", "contract_id": None}


def temperature_state(*, exposed=None, dig_temperatures=None, alerts=None):
    """Phase 2 state plus the B6 Probe fields.

    ``exposed`` maps instance id to temperature_state (others are normal);
    ``dig_temperatures`` maps cell to °C, or is a callable of the cell.
    """

    state = copy.deepcopy(load_recorded_state())
    quality = state["quality"]
    for key, entry in state["duplicants"]["by_instance_id"].items():
        prefix = f"duplicants.by_instance_id.{key}"
        value = (exposed or {}).get(entry["instance_id"], "normal")
        entry.update({
            "temperature_state": value,
            "body_temperature_c": 33.5 if value == "hypothermic" else 37.0,
            "cell": 1000 + entry["instance_id"],
            "cell_temperature_c": -42.0 if value == "hypothermic" else 12.0,
        })
        for field in ("temperature_state", "body_temperature_c", "cell", "cell_temperature_c"):
            quality[f"{prefix}.{field}"] = _observed()
    if dig_temperatures is not None:
        for key, details in state["designations"]["dig"]["by_cell"].items():
            cell = details["cell"]
            value = dig_temperatures(cell) if callable(dig_temperatures) else dig_temperatures.get(cell)
            if value is None:
                continue
            details["temperature_c"] = value
            quality[f"designations.dig.by_cell.{key}.temperature_c"] = _observed()
    if alerts is not None:
        state["alerts"] = list(alerts)
        for world in state["alerts_by_world"]:
            world["alerts"] = list(alerts)
    return state


class TemperatureMappingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.allowed = load_recorded_policy()["allowed_actions"]

    def generate(self, state):
        return C.generate_candidates(state, allowed_actions=self.allowed, autonomous=True)

    def ids(self, state):
        return [item["candidate_id"] for item in self.generate(state)["items"]]

    def runs(self):
        return sorted(C._dig_runs(load_recorded_state()), key=lambda run: -len(run))

    def test_probe_publishes_the_fields_the_candidates_read(self) -> None:
        source = PROBE_SOURCE.read_text(encoding="utf-8")
        for field in ("temperature_state", "body_temperature_c", "cell_temperature_c", "temperature_c"):
            self.assertIn(f'"{field}"', source)
        for value in ("hypothermic", "hyperthermic", "normal"):
            self.assertIn(f'"{value}"', source)

    def test_older_probe_only_notes_the_alert(self) -> None:
        result = self.generate(load_recorded_state())
        self.assertIn("temperature_alert:hypothermia:exposed_unobserved", result["notes"])
        ids = [item["candidate_id"] for item in result["items"]]
        self.assertIn(f"dig_{self.runs()[0][0]}_{len(self.runs()[0])}", ids)
        self.assertNotIn(f"priority_{COLD_ID}_Dig_4", ids)

    def test_exposed_duplicant_gets_a_dig_relief_first(self) -> None:
        state = temperature_state(exposed={COLD_ID: "hypothermic"})
        result = self.generate(state)
        self.assertIn("temperature_alert:hypothermia:exposed=1", result["notes"])
        items = result["items"]
        ids = [item["candidate_id"] for item in items]
        relief_id = f"priority_{COLD_ID}_Dig_4"
        self.assertEqual(ids[0], relief_id)
        self.assertEqual(len(ids), len(set(ids)))
        relief = items[0]
        self.assertIn("hypothermic", relief["summary"])
        self.assertIn("-42 °C", relief["summary"])
        self.assertEqual(
            relief["hint"],
            build_duplicant_priority_hint(state, instance_id=COLD_ID, chore_group="Dig", priority=4),
        )
        self.assertIn("resume_game", ids)

    def test_no_relief_below_priority_one(self) -> None:
        state = temperature_state(exposed={COLD_ID: "hypothermic"})
        state["duplicants"]["by_instance_id"][f"id_{COLD_ID}"]["chore_priorities"]["Dig"] = 0
        self.assertFalse(self.ids(state)[0].startswith(f"priority_{COLD_ID}_Dig_"))
        self.assertEqual(C._temperature_relief_candidates(state, C.temperature_exposure(state)), [])

    def test_stale_state_without_the_alert_is_ignored(self) -> None:
        state = temperature_state(exposed={COLD_ID: "hypothermic"}, alerts=["urgent_toilet"])
        result = self.generate(state)
        self.assertFalse(any(note.startswith("temperature_alert") for note in result["notes"]))
        self.assertNotEqual(result["items"][0]["candidate_id"], f"priority_{COLD_ID}_Dig_4")

    def test_cold_dig_runs_are_withheld_and_the_warmest_goes_first(self) -> None:
        runs = self.runs()
        self.assertGreater(len(runs), 2)
        coldest, warm, mild = runs[0], runs[1], runs[2]
        table = {cell: -60.0 for cell in coldest}
        table.update({cell: -15.0 for cell in mild})
        table.update({cell: 4.0 for cell in warm})
        state = temperature_state(dig_temperatures=lambda cell: table.get(cell, -30.0))
        dig = C._dig_candidate(state, C.temperature_exposure(state))
        self.assertEqual(dig["candidate_id"], f"dig_{warm[0]}_{len(warm)}")
        self.assertIn("(4 to 4 °C)", dig["summary"])
        # Without the alert the longest run is kept, now with its temperature.
        calm = temperature_state(dig_temperatures=lambda cell: table.get(cell, -30.0), alerts=[])
        dig = C._dig_candidate(calm, C.temperature_exposure(calm))
        self.assertEqual(dig["candidate_id"], f"dig_{coldest[0]}_{len(coldest)}")
        self.assertIn("(-60 to -60 °C)", dig["summary"])

    def test_no_dig_when_every_run_is_too_cold(self) -> None:
        state = temperature_state(dig_temperatures=lambda cell: -50.0)
        self.assertIsNone(C._dig_candidate(state, C.temperature_exposure(state)))
        self.assertFalse(any(candidate_id.startswith("dig_") for candidate_id in self.ids(state)))

    def test_unmeasured_dig_runs_follow_the_measured_ones(self) -> None:
        runs = self.runs()
        measured = runs[-1]
        state = temperature_state(dig_temperatures={cell: 2.0 for cell in measured})
        dig = C._dig_candidate(state, C.temperature_exposure(state))
        self.assertEqual(dig["candidate_id"], f"dig_{measured[0]}_{len(measured)}")
        # Nothing measured at all: the old order stands.
        plain = temperature_state()
        dig = C._dig_candidate(plain, C.temperature_exposure(plain))
        self.assertEqual(dig["candidate_id"], f"dig_{runs[0][0]}_{len(runs[0])}")

    def test_hyperthermia_withholds_hot_runs(self) -> None:
        runs = self.runs()
        hot = runs[0]
        table = {cell: 80.0 for cell in hot}
        state = temperature_state(
            exposed={COLD_ID: "hyperthermic"},
            dig_temperatures=lambda cell: table.get(cell, 25.0),
            alerts=["hyperthermia"],
        )
        exposure = C.temperature_exposure(state)
        self.assertEqual(exposure["dig_steering"], "hyperthermia")
        dig = C._dig_candidate(state, exposure)
        self.assertNotEqual(dig["candidate_id"], f"dig_{hot[0]}_{len(hot)}")
        self.assertEqual(self.ids(state)[0], f"priority_{COLD_ID}_Dig_4")

    def test_only_the_matching_state_gets_relief(self) -> None:
        # A hyperthermic duplicant does not answer an open hypothermia alert.
        state = temperature_state(exposed={COLD_ID: "hyperthermic"})
        self.assertEqual(C._temperature_relief_candidates(state, C.temperature_exposure(state)), [])
        self.assertIn("temperature_alert:hypothermia:exposed=0", self.generate(state)["notes"])

    def test_running_game_notes_without_relief(self) -> None:
        state = temperature_state(exposed={COLD_ID: "hypothermic"})
        state["game"]["paused"] = False
        result = self.generate(state)
        self.assertIn("temperature_alert:hypothermia:exposed=1", result["notes"])
        self.assertNotIn(f"priority_{COLD_ID}_Dig_4", [item["candidate_id"] for item in result["items"]])

    def test_candidate_set_is_stable(self) -> None:
        state = temperature_state(exposed={COLD_ID: "hypothermic"}, dig_temperatures=lambda cell: 1.0)
        self.assertEqual(
            self.generate(state)["candidate_set_hash"],
            self.generate(copy.deepcopy(state))["candidate_set_hash"],
        )

    def test_colony_summary_shows_the_exposure(self) -> None:
        temperature = colony_summary(temperature_state(exposed={COLD_ID: "hypothermic"}))["temperature"]
        self.assertEqual(temperature["alerts"], ["hypothermia"])
        self.assertNotIn("dig_steering", temperature)
        self.assertEqual([entry["instance_id"] for entry in temperature["duplicants"]], [COLD_ID])
        self.assertEqual(temperature["duplicants"][0]["cell_temperature_c"], -42.0)
        self.assertIsNotNone(temperature["world_material_min_c"])


if __name__ == "__main__":
    unittest.main()
