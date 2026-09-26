#!/usr/bin/env python3
"""Offline tests: contract validation reports bad numbers instead of raising.

Review 2026-09-26 (P2, decision layer): the material-lineage and
utility-contents validators called ``float`` on producer values, so a JSON
integer beyond binary64 (``10**400``) raised OverflowError and a null
required mass raised TypeError, instead of returning a blocker.
"""

from __future__ import annotations

import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import agent.field_contracts as fc
from agent.field_contracts import (
    BLUEPRINT_MATERIAL_LINEAGE_CONTRACT_ID,
    FUNCTIONAL_BUILDING_UTILITY_CONTENTS_CONTRACT_ID,
    FUNCTIONAL_BUILDING_UTILITY_CONTENTS_SCHEMA,
    contract_quality_fixture,
    field_contract_blockers,
)

HUGE = 10**400
TS = "2026-09-27T00:00:00+00:00"


def lineage_item(**changes) -> dict:
    item = {
        "tag": "SandStone",
        "required_mass_kg": 100.0,
        "delivered_mass_kg": 40.0,
        "remaining_mass_kg": 60.0,
        "delivery_status": "pending",
    }
    item.update(changes)
    return item


def contents_payload(**side_changes) -> dict:
    side = {
        "declared": True,
        "cell": 5,
        "content_kind": "gas",
        "element": "Oxygen",
        "mass": 1.0,
        "temperature": 300.0,
        "disease_count": 0,
        "solid_present": None,
    }
    changed = dict(side, **side_changes)
    return {
        "schema": FUNCTIONAL_BUILDING_UTILITY_CONTENTS_SCHEMA,
        "captured_at": TS,
        "world_id": 0,
        "complete": True,
        "instances": [{"instance_id": 1, "world_id": 0, "prefab_id": "GasPump", "input": changed, "output": dict(side)}],
    }


class NumericBlockerTests(unittest.TestCase):
    def test_lineage_reports_bad_numbers(self) -> None:
        self.assertEqual(fc._blueprint_material_lineage_blockers([lineage_item()]), [])
        cases = {
            "required_huge": ({"required_mass_kg": HUGE}, "blueprint_material_lineage_required_invalid"),
            "required_negative_huge": ({"required_mass_kg": -HUGE}, "blueprint_material_lineage_required_invalid"),
            "required_null": ({"required_mass_kg": None}, "blueprint_material_lineage_required_invalid"),
            "required_text": ({"required_mass_kg": "100"}, "blueprint_material_lineage_required_invalid"),
            "required_nan": ({"required_mass_kg": float("nan")}, "blueprint_material_lineage_required_invalid"),
            "delivered_huge": ({"delivered_mass_kg": HUGE}, "blueprint_material_lineage_delivered_invalid"),
            "remaining_huge": ({"remaining_mass_kg": -HUGE}, "blueprint_material_lineage_remaining_invalid"),
            "remaining_bool": ({"remaining_mass_kg": True}, "blueprint_material_lineage_remaining_invalid"),
        }
        for label, (changes, expected) in cases.items():
            with self.subTest(label):
                self.assertIn(expected, fc._blueprint_material_lineage_blockers([lineage_item(**changes)]))

    def test_lineage_through_the_public_contract(self) -> None:
        path = "cells.by_id.cell_7.blueprint_material_lineage"
        state = {
            "schema": "oni.state.v2",
            "ts": TS,
            "cells": {"by_id": {"cell_7": {"blueprint_material_lineage": [lineage_item(required_mass_kg=HUGE)]}}},
            "quality": {path: contract_quality_fixture(BLUEPRINT_MATERIAL_LINEAGE_CONTRACT_ID)},
        }
        blockers = field_contract_blockers(state, path, BLUEPRINT_MATERIAL_LINEAGE_CONTRACT_ID)
        self.assertIn("blueprint_material_lineage_required_invalid", blockers)

    def test_utility_contents_reports_bad_numbers(self) -> None:
        self.assertEqual(fc._functional_building_utility_contents_payload_blockers(contents_payload()), [])
        for changes in ({"mass": HUGE}, {"temperature": -HUGE}, {"mass": float("inf")}, {"mass": -1.0}, {"temperature": "hot"}):
            with self.subTest(changes=changes):
                self.assertIn(
                    "functional_building_utility_contents_numeric_invalid",
                    fc._functional_building_utility_contents_payload_blockers(contents_payload(**changes)),
                )

    def test_no_contract_value_check_raises(self) -> None:
        values = [None, HUGE, -HUGE, float("nan"), float("inf"), "x", True, [], {}, [None], [HUGE], {"a": HUGE}]
        paths = {c.contract_id: list(c.paths) for c in fc._CONTRACTS}
        paths[BLUEPRINT_MATERIAL_LINEAGE_CONTRACT_ID] = ["cells.by_id.cell_7.blueprint_material_lineage"]
        for contract in fc._CONTRACTS:
            for path in paths[contract.contract_id]:
                for value in values + [[lineage_item(required_mass_kg=HUGE)], contents_payload(mass=HUGE)]:
                    with self.subTest(contract=contract.contract_id, value=repr(value)[:30]):
                        self.assertIsInstance(fc._value_blockers(value, contract, path), list)
        self.assertIn(FUNCTIONAL_BUILDING_UTILITY_CONTENTS_CONTRACT_ID, paths)


if __name__ == "__main__":
    unittest.main()
