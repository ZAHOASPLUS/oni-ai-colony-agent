#!/usr/bin/env python3
from __future__ import annotations

import pathlib
import sys
import unittest
from unittest.mock import patch


ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.colony_plan import _evaluate_condition
from agent.field_contracts import ALERTS_CANONICAL_CONTRACT_ID, build_condition
from scripts.verify_rule_quality_contracts import state


class ContainsConditionTests(unittest.TestCase):
    def test_membership_uses_strict_json_equality_after_contract_validation(self) -> None:
        cases = (
            ([True], 1, False),
            ([1], True, False),
            ([False], 0, False),
            ([0], False, False),
            ([[True]], [1], False),
            ([{"value": True}], {"value": 1}, False),
            ([1], 1, True),
            ([1.0], 1, True),
            ([True], True, True),
            ([None], None, True),
            (["low_oxygen"], "low_oxygen", True),
            (["low_oxygen"], "oxygen", False),
            ([], 1, False),
            ("low_oxygen", "low_oxygen", False),
            ({"low_oxygen": True}, "low_oxygen", False),
        )
        with patch("agent.colony_plan.condition_contract_blockers", return_value=[]):
            for actual, expected, matches in cases:
                with self.subTest(actual=actual, expected=expected):
                    condition = {"path": "items", "op": "contains", "value": expected}
                    self.assertIs(_evaluate_condition({"items": actual}, condition), matches)

    def test_canonical_alert_membership_retains_real_contract_gate(self) -> None:
        snapshot = state()
        condition = build_condition(
            "alerts", "contains", "low_oxygen", ALERTS_CANONICAL_CONTRACT_ID
        )
        snapshot["alerts"] = ["low_oxygen"]
        self.assertTrue(_evaluate_condition(snapshot, condition, action="set_paused"))
        snapshot["alerts"] = []
        self.assertFalse(_evaluate_condition(snapshot, condition, action="set_paused"))
        snapshot["alerts"] = [True]
        self.assertFalse(_evaluate_condition(snapshot, condition, action="set_paused"))

    def test_missing_or_invalid_contract_cannot_authorize_membership(self) -> None:
        snapshot = state()
        snapshot["alerts"] = ["low_oxygen"]
        condition = {"path": "alerts", "op": "contains", "value": "low_oxygen"}
        self.assertFalse(_evaluate_condition(snapshot, condition, action="set_paused"))
        condition = build_condition(
            "alerts", "contains", "low_oxygen", ALERTS_CANONICAL_CONTRACT_ID
        )
        snapshot["quality"]["alerts"]["status"] = "estimated"
        self.assertFalse(_evaluate_condition(snapshot, condition, action="set_paused"))


if __name__ == "__main__":
    unittest.main()
