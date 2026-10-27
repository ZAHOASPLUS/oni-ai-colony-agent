"""Roadmap B15: power and water links for the Electrolyzer.

An Electrolyzer needs power and takes water only through a liquid pipe, so
Phase 2's six Electrolyzers never ran.  A wire or pipe connects a port only by
occupying the port's cell, which a building already occupies; the placement
contract therefore lets a utility share a cell with what is there, binding the
observed object count, while every other building keeps the empty-cell rule.
The Probe computes each link from the built ports (blueprints.supply_links).
"""

from __future__ import annotations

import copy
import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import candidates as C  # noqa: E402
from agent.advanced_actions import (  # noqa: E402
    BLUEPRINT_CELL_FIELDS,
    blueprint_cell_path,
    build_cancel_utility_blueprint_path_hint,
    build_place_building_blueprint_hint,
    build_place_utility_blueprint_path_hint,
)
from agent.field_contracts import (  # noqa: E402
    BLUEPRINT_CATALOG_CONTRACT_ID,
    BLUEPRINT_CELL_CONTRACT_ID,
    BLUEPRINT_INSTANCE_CONTRACT_ID,
    BUILDING_COMPLETED_COUNT_CONTRACT_ID,
    contract_quality_fixture,
)
from scripts import test_model_loop  # noqa: E402

# A pump outlet (occupied by the pump), one free cell, an Electrolyzer inlet.
PUMP_OUTLET, MIDDLE, INLET = 9300, 9301, 9302
LINK_CELLS = [PUMP_OUTLET, MIDDLE, INLET]
PATH_ID = "liquid301n302"
ALLOWED = (
    "set_research", "set_duplicant_chore_priority", "mark_dig_cells",
    "place_building_blueprint", "place_utility_blueprint_path", "cancel_utility_blueprint_path",
)
UTILITY_PATH_CONTRACT_ID = "oni.qc.blueprints.utility_path.v1"


def fingerprint(cell: int, objects: int) -> str:
    return "sha256:" + f"{cell * 10 + objects:064x}"[-64:]


def link_state(*, outlet_objects: int = 1, inlet_objects: int = 1) -> dict:
    """The model-loop fixture plus a Liquid Pipe catalog, three path cells and one link."""

    state = test_model_loop.CandidateTests().state(paused=True)
    quality = state["quality"]
    catalog = {
        "available": True, "prefab_id": "LiquidConduit", "is_utility": True, "cancellable": True,
        "width": 1, "height": 1, "build_location_rule": "Anywhere", "orientation": "Neutral",
        "facade_id": "DEFAULT_FACADE", "material_categories": ["Plumbable"], "material_tags": ["Granite"],
        "required_mass_kg": [100.0], "available_mass_kg": [900.0], "materials_available": True,
        "candidate_cells": [],
    }
    state["blueprints"]["catalog"]["LiquidConduit"] = catalog
    for field in catalog:
        quality[f"blueprints.catalog.LiquidConduit.{field}"] = contract_quality_fixture(BLUEPRINT_CATALOG_CONTRACT_ID)
    for index, cell in enumerate(LINK_CELLS):
        objects = {PUMP_OUTLET: outlet_objects, MIDDLE: 0, INLET: inlet_objects}[cell]
        state["cells"]["by_id"][f"cell_{cell}"] = {
            "cell": cell, "x": 60 + index, "y": 12, "world_id": 0, "visible": True, "solid": False,
            "foundation": False, "element_id": "Oxygen", "mass_kg": 1.2, "damage": 0.0, "object_count": objects,
            "empty_for_blueprint": objects == 0, "can_place_ladder": False, "can_place_wire": True,
            "can_place_liquid_conduit": True,
            "blueprint_present": False, "blueprint_instance_id": 0, "blueprint_prefab_id": "none",
            "blueprint_orientation": "none", "blueprint_material_tags": [], "blueprint_in_progress": False,
            "blueprint_is_utility": False, "empty_fingerprint": fingerprint(cell, 0),
            "fingerprint": fingerprint(cell, objects),
        }
        for field in BLUEPRINT_CELL_FIELDS + ("can_place_liquid_conduit",):
            quality[f"{blueprint_cell_path(cell)}.{field}"] = contract_quality_fixture(BLUEPRINT_CELL_CONTRACT_ID)
    state["blueprints"]["supply_links"] = {
        f"id_{PATH_ID}": {
            "path_id": PATH_ID, "kind": "liquid", "building_def_id": "LiquidConduit",
            "source_instance_id": 301, "source_prefab_id": "LiquidPump",
            "consumer_instance_ids": [302], "consumer_prefab_ids": ["Electrolyzer"], "cells": list(LINK_CELLS),
        }
    }
    quality["blueprints.supply_links"] = {"status": "observed", "source": "BuildingDef+Grid+Constructable+WorldInventory"}
    return state


def placed_link_state() -> dict:
    """The same cells after the link was placed: one pipe blueprint on each."""

    state = link_state()
    quality = state["quality"]
    ids = []
    for index, cell in enumerate(LINK_CELLS):
        instance_id = 74001 + index
        ids.append(instance_id)
        values = state["cells"]["by_id"][f"cell_{cell}"]
        objects = values["object_count"] + 1
        values.update({
            "object_count": objects, "empty_for_blueprint": False, "can_place_liquid_conduit": False,
            "blueprint_present": True, "blueprint_instance_id": instance_id, "blueprint_prefab_id": "LiquidConduit",
            "blueprint_orientation": "Neutral", "blueprint_material_tags": ["Granite"],
            "blueprint_in_progress": False, "blueprint_is_utility": True, "fingerprint": fingerprint(cell, objects),
        })
        instance = {
            "instance_id": instance_id, "cell": cell, "world_id": 0, "prefab_id": "LiquidConduit",
            "orientation": "Neutral", "material_tags": ["Granite"], "in_progress": False, "is_utility": True,
            "fingerprint": values["fingerprint"],
        }
        state["blueprints"]["by_instance_id"][f"id_{instance_id}"] = instance
        for field in instance:
            quality[f"blueprints.by_instance_id.id_{instance_id}.{field}"] = contract_quality_fixture(
                BLUEPRINT_INSTANCE_CONTRACT_ID
            )
    path = {
        "path_id": PATH_ID, "building_def_id": "LiquidConduit", "cells": list(LINK_CELLS),
        "blueprint_instance_ids": ids, "active_blueprint_instance_ids": ids, "active": True, "fully_active": True,
    }
    state["blueprints"]["utility_paths"][f"id_{PATH_ID}"] = path
    for field in path:
        quality[f"blueprints.utility_paths.id_{PATH_ID}.{field}"] = contract_quality_fixture(UTILITY_PATH_CONTRACT_ID)
    state["blueprints"]["supply_links"] = {}
    return state


def conditions(items, path):
    return [item for item in items if item["path"] == path]


class UtilityOverlayContractTests(unittest.TestCase):
    def test_a_pipe_may_share_a_port_cell_and_binds_what_is_there(self) -> None:
        hint = build_place_utility_blueprint_path_hint(
            link_state(), path_id=PATH_ID, cells=LINK_CELLS, material_tags=["Granite"], building_def_id="LiquidConduit"
        )
        self.assertEqual(hint["args"]["building_def_id"], "LiquidConduit")
        outlet = blueprint_cell_path(PUMP_OUTLET)
        middle = blueprint_cell_path(MIDDLE)
        [count] = conditions(hint["preconditions"], f"{outlet}.object_count")
        self.assertEqual(count["value"], 1)
        [grown] = conditions(hint["expected_effects"], f"{outlet}.object_count")
        self.assertEqual((grown["op"], grown["value"]), ("gt", 1))
        [current] = conditions(hint["preconditions"], f"{outlet}.fingerprint")
        self.assertEqual(current["value"], fingerprint(PUMP_OUTLET, 1))
        # The free cell in between keeps the exact empty-cell binding.
        [empty] = conditions(hint["preconditions"], f"{middle}.fingerprint")
        self.assertEqual(empty["value"], fingerprint(MIDDLE, 0))
        [flag] = conditions(hint["preconditions"], f"{middle}.empty_for_blueprint")
        self.assertIs(flag["value"], True)
        self.assertEqual(conditions(hint["preconditions"], f"{outlet}.can_place_liquid_conduit")[0]["value"], True)

    def test_an_ordinary_building_still_needs_an_empty_cell(self) -> None:
        state = test_model_loop.CandidateTests().state(paused=True)
        values = state["cells"]["by_id"]["cell_9000"]
        values["object_count"] = 1
        values["fingerprint"] = "sha256:" + "e" * 64
        with self.assertRaisesRegex(ValueError, "not a verified free Ladder target"):
            build_place_building_blueprint_hint(state, cells=[9000], material_tags=["WoodLog"], building_def_id="Ladder")

    def test_a_cell_the_probe_refuses_is_refused(self) -> None:
        state = link_state()
        state["cells"]["by_id"][f"cell_{MIDDLE}"]["can_place_liquid_conduit"] = False
        with self.assertRaises(ValueError):
            build_place_utility_blueprint_path_hint(
                state, path_id=PATH_ID, cells=LINK_CELLS, material_tags=["Granite"], building_def_id="LiquidConduit"
            )

    def test_cancelling_an_overlay_path_restores_the_ports_not_empty_cells(self) -> None:
        hint = build_cancel_utility_blueprint_path_hint(placed_link_state(), path_id=PATH_ID)
        outlet = blueprint_cell_path(PUMP_OUTLET)
        middle = blueprint_cell_path(MIDDLE)
        self.assertEqual(conditions(hint["preconditions"], f"{outlet}.object_count")[0]["value"], 2)
        self.assertEqual(conditions(hint["expected_effects"], f"{outlet}.object_count")[0]["value"], 1)
        [outlet_print] = conditions(hint["expected_effects"], f"{outlet}.fingerprint")
        self.assertEqual((outlet_print["op"], outlet_print["value"]), ("ne", fingerprint(PUMP_OUTLET, 2)))
        self.assertEqual(conditions(hint["expected_effects"], f"{middle}.object_count")[0]["value"], 0)
        self.assertEqual(conditions(hint["expected_effects"], f"{middle}.fingerprint")[0]["value"], fingerprint(MIDDLE, 0))
        # Emptiness is not promised for a utility cell (a port stays occupied).
        self.assertEqual(conditions(hint["expected_effects"], f"{outlet}.empty_for_blueprint"), [])


class SupplyLinkCandidateTests(unittest.TestCase):
    def ids(self, state) -> list:
        return [item["candidate_id"] for item in C.generate_candidates(state, allowed_actions=ALLOWED, autonomous=True)["items"]]

    def test_a_link_is_offered_right_after_research(self) -> None:
        ids = self.ids(link_state())
        link = f"liquid_link_{PATH_ID}"
        self.assertIn(link, ids)
        research = [index for index, value in enumerate(ids) if value.startswith("research_")]
        self.assertEqual(ids.index(link), (max(research) + 1) if research else 0)

    def test_the_link_summary_names_both_ends(self) -> None:
        items = C.generate_candidates(link_state(), allowed_actions=ALLOWED, autonomous=True)["items"]
        [link] = [item for item in items if item["candidate_id"] == f"liquid_link_{PATH_ID}"]
        self.assertIn("Liquid Pipe", link["summary"])
        self.assertIn("LiquidPump", link["summary"])
        self.assertIn("Electrolyzer", link["summary"])
        self.assertEqual(link["action"], "place_utility_blueprint_path")

    def test_malformed_or_unobserved_links_are_ignored(self) -> None:
        cases = {}
        unobserved = link_state()
        del unobserved["quality"]["blueprints.supply_links"]
        cases["unobserved"] = unobserved
        renamed = link_state()
        renamed["blueprints"]["supply_links"]["id_other"] = renamed["blueprints"]["supply_links"].pop(f"id_{PATH_ID}")
        cases["key_mismatch"] = renamed
        foreign = link_state()
        foreign["blueprints"]["supply_links"][f"id_{PATH_ID}"]["building_def_id"] = "GasConduit"
        cases["unknown_utility"] = foreign
        for name, state in cases.items():
            with self.subTest(name):
                self.assertFalse(any(item.startswith("liquid_link_") for item in self.ids(state)))

    def test_an_unbuilt_path_can_be_cancelled_as_a_whole(self) -> None:
        ids = self.ids(placed_link_state())
        self.assertIn(f"cancel_blueprint_path_{PATH_ID}", ids)
        # A path a duplicant has started is not offered.
        started = placed_link_state()
        started["blueprints"]["by_instance_id"]["id_74002"]["in_progress"] = True
        self.assertNotIn(f"cancel_blueprint_path_{PATH_ID}", self.ids(started))


class SupplyBuildingGatingTests(unittest.TestCase):
    def offered(self, counts: dict) -> list:
        state = link_state()
        state["blueprints"]["supply_links"] = {}
        state.setdefault("buildings", {})
        for name, value in counts.items():
            state["buildings"][name] = value
            state["quality"][f"buildings.{name}"] = contract_quality_fixture(BUILDING_COMPLETED_COUNT_CONTRACT_ID)
        offered = []

        def fake_building(snapshot, building_def_id, prefix, label, note=""):
            offered.append(building_def_id)
            return None

        with mock.patch.object(C, "_building_candidate", side_effect=fake_building):
            C.generate_candidates(state, allowed_actions=ALLOWED, autonomous=True)
        return offered

    def test_supply_buildings_follow_the_first_electrolyzer(self) -> None:
        none = self.offered({"electrolyzer": 0, "manual_generator": 0, "liquid_pump": 0})
        self.assertNotIn("ManualGenerator", none)
        self.assertNotIn("LiquidPump", none)
        first = self.offered({"electrolyzer": 1, "manual_generator": 0, "liquid_pump": 0})
        self.assertIn("ManualGenerator", first)
        self.assertIn("LiquidPump", first)
        supplied = self.offered({"electrolyzer": 1, "manual_generator": 1, "liquid_pump": 1})
        self.assertNotIn("ManualGenerator", supplied)
        self.assertNotIn("LiquidPump", supplied)

    def test_an_unobserved_count_offers_nothing(self) -> None:
        offered = self.offered({"electrolyzer": 1})
        self.assertNotIn("ManualGenerator", offered)



def power_link_state(consumers) -> dict:
    """The link fixture as a Wire trunk from a Manual Generator."""

    state = link_state()
    catalog = dict(state["blueprints"]["catalog"]["LiquidConduit"], prefab_id="Wire")
    state["blueprints"]["catalog"]["Wire"] = catalog
    for field in catalog:
        state["quality"][f"blueprints.catalog.Wire.{field}"] = contract_quality_fixture(BLUEPRINT_CATALOG_CONTRACT_ID)
    link = state["blueprints"]["supply_links"].pop(f"id_{PATH_ID}")
    link.update(
        path_id="power301n302", kind="power", building_def_id="Wire", source_prefab_id="ManualGenerator",
        consumer_prefab_ids=list(consumers), consumer_instance_ids=list(range(302, 302 + len(consumers))),
    )
    state["blueprints"]["supply_links"]["id_power301n302"] = link
    return state


class PowerLinkOrderTests(unittest.TestCase):
    """Review fix: one generator port takes one wire, so it must reach the pump too."""

    def links(self, state, pumps):
        notes = []
        status = {"LiquidPump": {"built": pumps, "operational": None, "idle_status": None}}
        with mock.patch.object(C, "building_status", return_value=status):
            ids = [item["candidate_id"] for item in C._supply_link_candidates(state, notes)]
        return ids, notes

    def test_waits_while_no_pump_is_built(self) -> None:
        for pumps in (0, None):
            ids, notes = self.links(power_link_state(["Electrolyzer"]), pumps)
            self.assertEqual(ids, [])
            self.assertEqual(notes, ["power_link_waits_for_liquid_pump"])

    def test_offered_once_a_pump_is_built(self) -> None:
        ids, notes = self.links(power_link_state(["Electrolyzer"]), 1)
        self.assertEqual(ids, ["power_link_power301n302"])
        self.assertEqual(notes, [])

    def test_a_trunk_that_reaches_the_pump_is_offered(self) -> None:
        ids, _ = self.links(power_link_state(["LiquidPump", "Electrolyzer"]), 0)
        self.assertEqual(ids, ["power_link_power301n302"])

    def test_liquid_links_are_not_gated(self) -> None:
        ids, notes = self.links(link_state(), 0)
        self.assertEqual(ids, [f"liquid_link_{PATH_ID}"])
        self.assertEqual(notes, [])


class UtilityCancelCountTests(unittest.TestCase):
    def test_a_cell_without_the_blueprint_is_refused(self) -> None:
        # The executor refuses a cancellation count below 1; so does the builder.
        for count in (0, None):
            state = placed_link_state()
            state["cells"]["by_id"][f"cell_{MIDDLE}"]["object_count"] = count
            with self.assertRaises(ValueError):
                build_cancel_utility_blueprint_path_hint(state, path_id=PATH_ID)


if __name__ == "__main__":
    unittest.main()
