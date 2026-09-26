#!/usr/bin/env python3
"""Offline contract tests for the explicit six-phase industry strategy.

The fixture verifier is the existing strategy test helper.  Its proof is
synthetic and in-process; this file never starts ONI/Steam/Bridge, touches a
command queue, or claims live admission.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import agent.strategy as strategy_module
from agent.strategy import (
    PHASE_EXIT_GATES,
    PHASE_ORDER,
    PHASE_STABILITY_SAMPLES,
    PhaseEvidenceTracker,
    StrategyError,
    validate_strategy,
)
from scripts.verify_strategy import base_state, evaluate_fixture, observed, objective


INDUSTRY_FIELDS = {
    "industry.production.completed": ("production", "completed"),
    "industry.delivery.completed": ("delivery", "completed"),
    "industry.inputs.supplied": ("inputs", "supplied"),
    "industry.heat.removed": ("heat", "removed"),
    "industry.waste.handled": ("waste", "handled"),
}


def industry_state(*, values: dict[str, object] | None = None) -> dict:
    state = base_state()
    values = values or {path: True for path in INDUSTRY_FIELDS}
    state["industry"] = {
        "production": {"completed": values.get("industry.production.completed", True)},
        "delivery": {"completed": values.get("industry.delivery.completed", True)},
        "inputs": {"supplied": values.get("industry.inputs.supplied", True)},
        "heat": {"removed": values.get("industry.heat.removed", True)},
        "waste": {"handled": values.get("industry.waste.handled", True)},
    }
    for path in INDUSTRY_FIELDS:
        state["quality"][path] = observed(path)
    return state


class IndustryStrategyTests(unittest.TestCase):
    def test_phase_order_and_gate_contract_are_explicit(self) -> None:
        self.assertEqual(PHASE_ORDER, ("bootstrap", "stabilize", "industry", "expand", "space", "sustain"))
        self.assertEqual(
            PHASE_EXIT_GATES["industry"],
            (
                "industry_production",
                "industry_delivery",
                "industry_inputs",
                "industry_thermal_safety",
                "industry_waste_handling",
            ),
        )
        self.assertIn("industry", PHASE_STABILITY_SAMPLES)

    def test_missing_industry_evidence_is_unknown_and_blocks_progression(self) -> None:
        missing = base_state()
        missing.pop("industry", None)
        for path in INDUSTRY_FIELDS:
            missing["quality"].pop(path, None)
        result = evaluate_fixture(missing)
        self.assertEqual(result["phase"]["detected_from_single_snapshot"], "industry")
        self.assertEqual(result["gates"][2]["phase"], "industry")
        self.assertEqual(result["gates"][2]["status"], "unknown")
        for gate_id in PHASE_EXIT_GATES["industry"]:
            self.assertEqual(objective(result, gate_id)["status"], "unknown")

    def test_estimated_or_failed_deliverable_never_certifies(self) -> None:
        estimated = industry_state()
        estimated["quality"]["industry.production.completed"]["status"] = "estimated"
        estimated_result = evaluate_fixture(estimated)
        self.assertEqual(objective(estimated_result, "industry_production")["status"], "unknown")
        self.assertEqual(estimated_result["phase"]["detected_from_single_snapshot"], "industry")

        failed = industry_state(values={"industry.waste.handled": False})
        failed_result = evaluate_fixture(failed)
        self.assertEqual(objective(failed_result, "industry_waste_handling")["status"], "at_risk")
        self.assertEqual(failed_result["phase"]["detected_from_single_snapshot"], "industry")

    def test_failed_industry_safety_gate_sets_top_level_at_risk(self) -> None:
        failed_heat = industry_state(values={"industry.heat.removed": False})
        heat_result = evaluate_fixture(failed_heat)
        self.assertEqual(objective(heat_result, "industry_thermal_safety")["status"], "at_risk")
        self.assertEqual(heat_result["status"], "at_risk")
        self.assertEqual(heat_result["operating_mode"], "emergency")

        failed_waste = industry_state(values={"industry.waste.handled": False})
        waste_result = evaluate_fixture(failed_waste)
        self.assertEqual(objective(waste_result, "industry_waste_handling")["status"], "at_risk")
        self.assertEqual(waste_result["status"], "at_risk")
        self.assertEqual(waste_result["operating_mode"], "emergency")

    def test_observed_deliverables_satisfy_only_single_snapshot_gate(self) -> None:
        result = evaluate_fixture(industry_state())
        for gate_id in PHASE_EXIT_GATES["industry"]:
            self.assertEqual(objective(result, gate_id)["status"], "satisfied")
        self.assertEqual(result["gates"][2]["status"], "satisfied")
        # Detection is informational; forward promotion still needs a tracker
        # window and therefore remains held on one snapshot.
        self.assertEqual(result["phase"]["id"], "bootstrap")
        self.assertEqual(result["readiness"]["stable_window_verified"], False)

    def test_tracker_reaches_industry_only_after_prior_windows(self) -> None:
        tracker = PhaseEvidenceTracker()
        start = dt.datetime.now(dt.timezone.utc)
        final = None
        for index in range(1, PHASE_STABILITY_SAMPLES["bootstrap"] + PHASE_STABILITY_SAMPLES["stabilize"] + 1):
            state = industry_state()
            state["seq"] = index
            state["snapshot_seq"] = index
            state["snapshot_id"] = f"{state['session_id']}:{index}"
            state["ts"] = (start + dt.timedelta(seconds=index * 5)).isoformat(timespec="seconds")
            state["game_time_cycles"] = 200.0 + index * 0.1
            state["cycle"] = int(state["game_time_cycles"])
            final = tracker.update(evaluate_fixture(state))
        assert final is not None
        self.assertEqual(final["phase"]["id"], "industry")
        self.assertEqual(final["phase_evidence"]["current_phase"], "industry")
        self.assertFalse(final["readiness"]["stable_window_verified"])

    def test_validation_requires_each_industry_objective(self) -> None:
        result = evaluate_fixture(industry_state())
        tampered = copy.deepcopy(result)
        tampered["objectives"] = [
            item for item in tampered["objectives"] if item["id"] != "industry_delivery"
        ]
        with self.assertRaisesRegex(StrategyError, "missing one or more phase-gate objectives"):
            validate_strategy(tampered)


def main() -> int:
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(IndustryStrategyTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    strategy_path = pathlib.Path(strategy_module.__file__).resolve()
    summary = {
        "schema": "oni.strategy-industry-tests.v1",
        "test_count": result.testsRun,
        "failure_count": len(result.failures),
        "error_count": len(result.errors),
        "exit_code": 0 if result.wasSuccessful() else 1,
        "status": "passed" if result.wasSuccessful() else "failed",
        "strategy_import_origin": str(strategy_path),
        "strategy_sha256": hashlib.sha256(strategy_path.read_bytes()).hexdigest(),
        "live_admission": False,
        # The command is required to run with ``python -B``; no bytecode is
        # written by this test process.
        "pycache_created": False,
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return summary["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
