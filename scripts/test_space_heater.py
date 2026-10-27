"""Roadmap B6, second step: a Space Heater against hypothermia.

A heater needs power, and a Manual Generator's output takes one wire, so the
heater gets its own generator and its own link (the oxygen supply keeps the
first).  The Probe names the tech that unlocks each allowlisted building, so a
locked heater is answered with research for exactly that tech.

The base state is the recorded Phase 2 snapshot (paused, ``hypothermia`` open);
the recording predates the completed-building counts, so they are added here.
"""

from __future__ import annotations

import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import candidates as C  # noqa: E402
from agent.advanced_actions import BUILDING_BLUEPRINT_ALLOWLIST  # noqa: E402
from agent.field_contracts import BUILDING_COMPLETED_COUNT_CONTRACT_ID, contract_quality_fixture  # noqa: E402
from scripts.recorded_state import load_recorded_policy  # noqa: E402
from scripts import test_supply_links as supply  # noqa: E402
from scripts.test_temperature_mapping import temperature_state  # noqa: E402

TECH = "TemperatureModulation"


def heater_state(*, heaters=0, generators=0, electrolyzers=0, alerts=None, unlock=None):
    """``unlock`` is (unlocked, tech id, prerequisites complete) for the heater's catalog entry."""

    state = temperature_state(alerts=alerts)
    counts = {
        "space_heater": heaters, "manual_generator": generators, "electrolyzer": electrolyzers,
        "liquid_pump": 0, "oxygen_diffuser": 0, "algae_terrarium": 0, "microbe_musher": 0, "farm_tile": 0,
    }
    for name, value in counts.items():
        if value is None:
            continue
        state["buildings"][name] = value
        state["quality"][f"buildings.{name}"] = contract_quality_fixture(BUILDING_COMPLETED_COUNT_CONTRACT_ID)
    if unlock is not None:
        unlocked, tech, prerequisites = unlock
        entry = state["blueprints"]["catalog"].setdefault("SpaceHeater", {})
        entry.update({"unlocked": unlocked, "research_tech_id": tech, "research_prerequisites_complete": prerequisites})
        for field in ("unlocked", "research_tech_id", "research_prerequisites_complete"):
            state["quality"][f"blueprints.catalog.SpaceHeater.{field}"] = {"status": "observed", "source": "test"}
    return state


class SpaceHeaterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.allowed = load_recorded_policy()["allowed_actions"]

    def generate(self, state):
        return C.generate_candidates(state, allowed_actions=self.allowed, autonomous=True)

    def requested(self, state):
        """The building ids the generator asked to place (the cell scan itself is mocked)."""

        asked = []

        def fake(snapshot, building_def_id, prefix, label, note=""):
            asked.append(building_def_id)
            return None

        with mock.patch.object(C, "_building_candidate", side_effect=fake):
            result = self.generate(state)
        return asked, result

    def test_heater_offered_while_hypothermia_is_open(self) -> None:
        asked, _ = self.requested(heater_state())
        self.assertIn("SpaceHeater", asked)

    def test_heater_not_offered_without_the_alert_or_with_one_built(self) -> None:
        for state in (heater_state(alerts=["urgent_toilet"]), heater_state(heaters=1), heater_state(heaters=None)):
            asked, _ = self.requested(state)
            self.assertNotIn("SpaceHeater", asked)

    def test_heater_is_allowlisted_on_every_side(self) -> None:
        self.assertIn("SpaceHeater", BUILDING_BLUEPRINT_ALLOWLIST)
        for relative in (
            "mod/Source/Probe/ProbeStateReader.cs",
            "mod/Source/Commands/ActionExecutor.cs",
            "mod/Source/Contracts/CommandFieldContracts.cs",
        ):
            self.assertIn('"SpaceHeater"' if "Contracts" not in relative else "SpaceHeater)",
                          (ROOT / relative).read_text(encoding="utf-8"), relative)
        self.assertIn('"can_place_space_heater"', (ROOT / "mod/Source/Probe/ProbeStateReader.cs").read_text(encoding="utf-8"))

    def test_locked_heater_asks_for_its_own_tech_first(self) -> None:
        result = self.generate(heater_state(unlock=(False, TECH, True)))
        first = result["items"][0]
        self.assertEqual(first["candidate_id"], f"research_{TECH}")
        self.assertEqual(first["hint"]["args"], {"tech": TECH})
        self.assertIn("Space Heater", first["summary"])
        ids = [item["candidate_id"] for item in result["items"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_unlock_research_also_offered_while_running(self) -> None:
        state = heater_state(unlock=(False, TECH, True))
        state["game"]["paused"] = False
        self.assertIn(f"research_{TECH}", [item["candidate_id"] for item in self.generate(state)["items"]])

    def test_open_prerequisites_are_noted_not_offered(self) -> None:
        result = self.generate(heater_state(unlock=(False, TECH, False)))
        self.assertIn(f"unlock_research_blocked:SpaceHeater:{TECH}", result["notes"])
        self.assertNotIn(f"research_{TECH}", [item["candidate_id"] for item in result["items"]])

    def test_no_unlock_research_when_unlocked_unneeded_or_running(self) -> None:
        for state in (
            heater_state(unlock=(True, TECH, True)),
            heater_state(unlock=(False, TECH, True), heaters=1),
            heater_state(unlock=(False, TECH, True), alerts=["urgent_toilet"]),
        ):
            self.assertNotIn(f"research_{TECH}", [item["candidate_id"] for item in self.generate(state)["items"]])
        current = heater_state(unlock=(False, TECH, True))
        current["research"]["current"] = TECH
        result = self.generate(current)
        self.assertIn(f"unlock_research_in_progress:SpaceHeater:{TECH}", result["notes"])

    def test_each_powered_group_gets_its_own_generator(self) -> None:
        def wanted(**counts):
            status = {name: {"built": value} for name, value in counts.items()}
            return C._supply_building_wanted(status, "ManualGenerator", counts.get("Electrolyzer"))

        self.assertTrue(wanted(SpaceHeater=1, Electrolyzer=0, ManualGenerator=0))
        self.assertTrue(wanted(SpaceHeater=1, Electrolyzer=1, ManualGenerator=1))
        self.assertFalse(wanted(SpaceHeater=1, Electrolyzer=1, ManualGenerator=2))
        self.assertFalse(wanted(SpaceHeater=0, Electrolyzer=0, ManualGenerator=0))
        self.assertFalse(wanted(SpaceHeater=1, Electrolyzer=0, ManualGenerator=None))
        # The pump rule is unchanged: one, for a built Electrolyzer.
        self.assertFalse(C._supply_building_wanted({"LiquidPump": {"built": 0}}, "LiquidPump", 0))
        self.assertTrue(C._supply_building_wanted({"LiquidPump": {"built": 0}}, "LiquidPump", 1))

    def test_a_heater_link_does_not_wait_for_a_pump(self) -> None:
        ids, notes = supply.PowerLinkOrderTests.links(self, supply.power_link_state(["SpaceHeater"]), 0)
        self.assertEqual(ids, ["power_link_power301n302"])
        self.assertEqual(notes, [])


if __name__ == "__main__":
    unittest.main()
