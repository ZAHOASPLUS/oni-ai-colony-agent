#!/usr/bin/env python3
"""Offline tests: trust and alerts across worlds (review 2026-09-26, M8/G3).

Snapshot trust used to require exactly one populated world equal to the
camera world, so a duplicant stepping into a rocket, or the player looking at
another asteroid, made the whole chain untrusted.  The Probe now reports
alerts for every world with live duplicants (scope ``all_worlds``) and an
optional ``alerts_by_world`` breakdown.  No game, Bridge or live directory is
involved.
"""

from __future__ import annotations

import copy
import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.field_contracts import ALERTS_CANONICAL_CONTRACT_ID, read_contract_field
from agent.rules import _evaluate_alert_signals
from agent.state_quality import (
    ALERTS_ACTIVE_WORLD_SCOPE,
    ALERTS_ACTIVE_WORLD_SOURCE,
    ALERTS_ALL_WORLDS_SCOPE,
    ALERTS_ALL_WORLDS_SOURCE,
    snapshot_trust_blockers,
)
from scripts.verify_strategy import base_state, evaluate_fixture


def rocket_state(*, legacy_alerts: bool = False) -> dict:
    """Camera on world 0, a second populated world 2 (a rocket interior)."""

    state = base_state()
    state["observation_scope"].update(
        {"populated_world_ids": [0, 2], "covered_world_ids": [0], "complete_for_populated_worlds": False}
    )
    if legacy_alerts:
        state["quality"]["alerts"].update(
            {"scope": ALERTS_ACTIVE_WORLD_SCOPE, "source": ALERTS_ACTIVE_WORLD_SOURCE}
        )
    return state


def with_groups(state: dict, groups: list, **quality) -> dict:
    state["alerts_by_world"] = groups
    entry = {
        "status": "observed",
        "source": state["quality"]["alerts"]["source"],
        "scope": state["quality"]["alerts"]["scope"],
        "semantic": "canonical_active_alert_ids_by_world",
        "unit": "world_alert_groups",
        "completeness": "complete",
        "captured_at": "snapshot.ts",
    }
    entry.update(quality)
    state["quality"]["alerts_by_world"] = entry
    return state


def alerts_usable(state: dict) -> bool:
    return read_contract_field(state, "alerts", ALERTS_CANONICAL_CONTRACT_ID).usable


class MultiWorldTrustTests(unittest.TestCase):
    def test_second_populated_world_keeps_trust_with_all_world_alerts(self) -> None:
        self.assertEqual(snapshot_trust_blockers(rocket_state()), [])
        result = evaluate_fixture(rocket_state())
        self.assertTrue(result["telemetry"]["trust_ready"])
        self.assertEqual(result["telemetry"]["trust_blockers"], [])

    def test_active_world_alerts_still_require_a_single_world(self) -> None:
        state = rocket_state(legacy_alerts=True)
        self.assertIn("populated_world_coverage_incomplete", snapshot_trust_blockers(state))
        self.assertFalse(evaluate_fixture(state)["telemetry"]["trust_ready"])

    def test_camera_on_an_empty_world_keeps_trust(self) -> None:
        state = base_state()
        state["world_id"] = 1
        state["observation_scope"].update(
            {"active_world_id": 1, "populated_world_ids": [0], "covered_world_ids": [1], "complete_for_populated_worlds": False}
        )
        self.assertEqual(snapshot_trust_blockers(state), [])

    def test_forged_scope_is_still_rejected(self) -> None:
        state = base_state()
        state["observation_scope"].update(
            {"populated_world_ids": [1], "covered_world_ids": [0, 1], "complete_for_populated_worlds": True}
        )
        blockers = snapshot_trust_blockers(state)
        self.assertIn("covered_world_ids_not_active_world_only", blockers)
        self.assertIn("complete_populated_world_ids_not_active_world_only", blockers)

    def test_unusable_all_world_alerts_do_not_widen_trust(self) -> None:
        state = rocket_state()
        state["alerts"] = ["low_oxygen", "high_stress"]  # unsorted: contract-invalid
        self.assertIn("populated_world_coverage_incomplete", snapshot_trust_blockers(state))

    def test_scope_and_source_must_pair(self) -> None:
        state = base_state()
        state["quality"]["alerts"]["source"] = ALERTS_ACTIVE_WORLD_SOURCE
        self.assertEqual(state["quality"]["alerts"]["scope"], ALERTS_ALL_WORLDS_SCOPE)
        self.assertFalse(alerts_usable(state))
        legacy = base_state()
        legacy["quality"]["alerts"].update({"scope": ALERTS_ACTIVE_WORLD_SCOPE, "source": ALERTS_ACTIVE_WORLD_SOURCE})
        self.assertTrue(alerts_usable(legacy))
        self.assertEqual(base_state()["quality"]["alerts"]["source"], ALERTS_ALL_WORLDS_SOURCE)


class AlertsByWorldTests(unittest.TestCase):
    def test_absent_or_null_groups_change_nothing(self) -> None:
        state = rocket_state()
        state["alerts"] = ["suffocation"]
        self.assertTrue(alerts_usable(state))
        state["alerts_by_world"] = None
        self.assertTrue(alerts_usable(state))

    def test_consistent_groups_are_accepted(self) -> None:
        state = rocket_state()
        state["alerts"] = ["low_oxygen", "suffocation"]
        with_groups(
            state,
            [
                {"world_id": None, "duplicant_count": 1, "alerts": []},
                {"world_id": 0, "duplicant_count": 3, "alerts": ["low_oxygen"]},
                {"world_id": 2, "duplicant_count": 1, "alerts": ["low_oxygen", "suffocation"]},
            ],
        )
        self.assertTrue(alerts_usable(state))

    def test_inconsistent_groups_make_alerts_unusable(self) -> None:
        good = [
            {"world_id": 0, "duplicant_count": 3, "alerts": []},
            {"world_id": 2, "duplicant_count": 1, "alerts": ["suffocation"]},
        ]
        cases = {
            "union_missing_alert": (["low_oxygen", "suffocation"], good, {}),
            "union_extra_alert": ([], good, {}),
            "unordered": (["suffocation"], list(reversed(good)), {}),
            "null_world_not_first": (
                ["suffocation"],
                good + [{"world_id": None, "duplicant_count": 1, "alerts": []}],
                {},
            ),
            "unknown_alert": (["suffocation"], good + [{"world_id": 3, "duplicant_count": 1, "alerts": ["x"]}], {}),
            "extra_key": (["suffocation"], [dict(good[1], note="x")], {}),
            "bool_world": (["suffocation"], [{"world_id": True, "duplicant_count": 1, "alerts": ["suffocation"]}], {}),
            "not_a_list": (["suffocation"], {"2": ["suffocation"]}, {}),
            "quality_semantic": (["suffocation"], good, {"semantic": "other"}),
            "quality_source": (["suffocation"], good, {"source": ALERTS_ACTIVE_WORLD_SOURCE}),
        }
        for label, (alerts, groups, quality) in cases.items():
            with self.subTest(label):
                state = rocket_state()
                state["alerts"] = alerts
                with_groups(state, copy.deepcopy(groups), **quality)
                self.assertFalse(alerts_usable(state))

    def test_rules_pause_for_an_alert_on_another_world(self) -> None:
        state = rocket_state()
        state["alerts"] = ["suffocation"]
        with_groups(
            state,
            [
                {"world_id": 0, "duplicant_count": 3, "alerts": []},
                {"world_id": 2, "duplicant_count": 1, "alerts": ["suffocation"]},
            ],
        )
        signals = {item["incident_id"]: item for item in _evaluate_alert_signals(state)}
        self.assertEqual(signals["suffocation"]["state"], "active")
        self.assertEqual(signals["suffocation"]["command"]["action"], "set_paused")
        self.assertEqual(signals["low_oxygen"]["state"], "inactive")


if __name__ == "__main__":
    unittest.main()
