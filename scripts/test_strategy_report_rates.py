#!/usr/bin/env python3
"""Offline tests for report-derived strategy rates (review 2026-09-26, M7).

The Probe leaves every production/consumption window rate null and publishes
the game's daily-report counters instead.  With ``report_rates=True`` the
strategy derives the oxygen rates from the last closed report in process and
verifies them against the same counters.  No game, Bridge or live directory is
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

from agent.state_quality import canonical_state_hash
from agent.strategy import (
    REPORT_RATE_POLICY_VERSION,
    collect_metrics,
    evaluate_strategy,
    report_rate_proof_verifier,
    with_report_rates,
)
from scripts.verify_strategy import (
    add_functional_bootstrap_buildings,
    base_state,
    objective,
    trusted_fixture_proof,
)


def _raw_quality(period: str, entry: str, sign: str, unit: str, scope: str) -> dict:
    source = "ReportManager.TodaysReport" if period == "current" else "ReportManager.reports[last]"
    kind = "open" if period == "current" else "closed"
    return {
        "status": "observed",
        "source": f"{source}.reportEntries[{entry}].{sign.capitalize()}",
        "semantic": f"raw_{kind}_report_{sign}_counter",
        "unit": unit,
        "scope": scope,
        "completeness": "complete_for_instrumented_report_events",
        "captured_at": "snapshot.ts",
    }


def add_report_counters(
    state: dict,
    *,
    closed_day: int,
    open_day: int,
    positive_kg: float,
    negative_kg: float,
    energy: tuple | None = None,
) -> dict:
    """Attach the Probe's exact ReportTelemetryReader shape."""

    state["report_counters"] = {
        "current": {"report_day": open_day, "oxygen_created": {"positive_kg": 1.0, "negative_kg": -2.0}},
        "previous_closed": {
            "report_day": closed_day,
            "oxygen_created": {"positive_kg": positive_kg, "negative_kg": negative_kg},
        },
    }
    quality = state["quality"]
    for period, source, kind in (
        ("current", "ReportManager.TodaysReport", "open"),
        ("previous_closed", "ReportManager.reports[last]", "closed"),
    ):
        quality[f"report_counters.{period}.report_day"] = {
            "status": "observed",
            "source": f"{source}.day",
            "semantic": f"raw_{kind}_report_epoch",
            "unit": "report_day",
            "scope": "save",
            "completeness": "complete",
            "captured_at": "snapshot.ts",
        }
        for sign in ("positive", "negative"):
            quality[f"report_counters.{period}.oxygen_created.{sign}_kg"] = _raw_quality(
                period, "OxygenCreated", sign, "kg", "save_report_manager_oxygen_created"
            )
    if energy is not None:
        state["report_counters"]["previous_closed"]["energy_created"] = {
            "positive_joules": energy[0],
            "negative_joules": energy[1],
        }
        for sign in ("positive", "negative"):
            quality[f"report_counters.previous_closed.energy_created.{sign}_joules"] = _raw_quality(
                "previous_closed", "EnergyCreated", sign, "J", "save_report_manager_energy_created"
            )
    return state


def probe_shaped_state(**counters) -> dict:
    """The fixture with every window rate as the current Probe publishes it."""

    state = base_state()
    for group in ("production", "consumption"):
        for key in state[group]:
            state[group][key] = None
            state["quality"][f"{group}.{key}"] = {
                "status": "unsupported",
                "source": "none",
                "semantic": "window_rate",
                "scope": "all_populated_worlds",
                "completeness": "unknown",
                "captured_at": "snapshot.ts",
            }
    state["cycle"] = 7
    state["game_time_cycles"] = 7.6
    defaults = {"closed_day": 6, "open_day": 7, "positive_kg": 48.5921936, "negative_kg": -231.112549}
    defaults.update(counters)
    return add_report_counters(state, **defaults)


class ReportRateTests(unittest.TestCase):
    def test_rates_are_null_without_opt_in(self) -> None:
        result = evaluate_strategy(probe_shaped_state())
        self.assertIsNone(result["metrics"]["renewable_oxygen_kg_per_cycle"])
        self.assertIsNone(result["metrics"]["oxygen_demand_kg_per_cycle"])

    def test_closed_report_gives_observed_oxygen_rates_and_risk(self) -> None:
        state = probe_shaped_state()
        before = copy.deepcopy(state)
        result = evaluate_strategy(state, report_rates=True)
        metrics = result["metrics"]
        self.assertEqual(metrics["renewable_oxygen_kg_per_cycle"], 48.5921936)
        self.assertEqual(metrics["oxygen_demand_kg_per_cycle"], 231.112549)
        self.assertEqual(metrics["qualities"]["renewable_oxygen"], "observed")
        self.assertEqual(metrics["qualities"]["oxygen_demand"], "observed")
        self.assertAlmostEqual(metrics["oxygen_deficit_kg_per_cycle"], 182.5203554)
        oxygen = objective(result, "oxygen_security")
        self.assertEqual(oxygen["status"], "at_risk")
        self.assertIn("below consumption", oxygen["reason"])
        self.assertEqual(objective(result, "renewable_oxygen")["status"], "at_risk")
        # The input snapshot is not mutated and the strategy stays bound to it.
        self.assertEqual(state, before)
        self.assertEqual(result["state_hash"], canonical_state_hash(before))
        self.assertEqual(result["state_hash"], evaluate_strategy(before)["state_hash"])

    def test_positive_balance_satisfies_oxygen_with_observed_stock(self) -> None:
        state = probe_shaped_state(positive_kg=300.0, negative_kg=-240.0)
        oxygen = objective(evaluate_strategy(state, report_rates=True), "oxygen_security")
        self.assertEqual(oxygen["status"], "satisfied")

    def test_complete_view_without_a_diffuser_does_not_hide_the_balance(self) -> None:
        # The live colony has no OxygenDiffuser/MineralDeoxidizer; a complete
        # functional view listing none is an observation, not unknown data.
        state = add_functional_bootstrap_buildings(
            probe_shaped_state(positive_kg=300.0, negative_kg=-240.0), generator=[True]
        )
        result = evaluate_strategy(state, report_rates=True)
        self.assertEqual(result["metrics"]["qualities"]["functional_diffuser_operational"], "absent")
        self.assertEqual(objective(result, "oxygen_security")["status"], "satisfied")
        unknown = add_functional_bootstrap_buildings(
            probe_shaped_state(positive_kg=300.0, negative_kg=-240.0), diffuser=[None]
        )
        self.assertEqual(objective(evaluate_strategy(unknown, report_rates=True), "oxygen_security")["status"], "unknown")

    def test_energy_counters_become_watts_when_present(self) -> None:
        state = probe_shaped_state(energy=(720_000.0, -600_000.0))
        # Without the in-process verifier the derived rate is not trusted.
        metrics = collect_metrics(with_report_rates(state))
        self.assertIsNone(metrics["renewable_power_watts"])
        result = evaluate_strategy(state, report_rates=True)
        self.assertEqual(result["metrics"]["renewable_power_watts"], 1200.0)
        self.assertEqual(result["metrics"]["power_demand_watts"], 1000.0)

    def test_unusable_counters_give_no_rate(self) -> None:
        cases = {
            "first_report_is_partial": probe_shaped_state(closed_day=1, open_day=2),
            "open_not_next_day": probe_shaped_state(closed_day=5, open_day=7),
            "closed_report_is_old": probe_shaped_state(closed_day=3, open_day=4),
            "positive_sign": probe_shaped_state(negative_kg=5.0),
            "negative_production": probe_shaped_state(positive_kg=-1.0),
            "bool_day": probe_shaped_state(closed_day=True, open_day=2),
        }
        wrong_source = probe_shaped_state()
        wrong_source["quality"]["report_counters.previous_closed.oxygen_created.positive_kg"]["source"] = "self"
        cases["wrong_source"] = wrong_source
        not_observed = probe_shaped_state()
        not_observed["quality"]["report_counters.previous_closed.report_day"]["status"] = "error"
        cases["day_error"] = not_observed
        open_counter = probe_shaped_state()
        open_counter["quality"]["report_counters.previous_closed.oxygen_created.negative_kg"]["semantic"] = (
            "raw_open_report_negative_counter"
        )
        cases["open_report_semantic"] = open_counter
        for label, state in cases.items():
            with self.subTest(label):
                metrics = evaluate_strategy(state, report_rates=True)["metrics"]
                self.assertIsNone(metrics["renewable_oxygen_kg_per_cycle"])
                self.assertIsNone(metrics["oxygen_demand_kg_per_cycle"])

    def test_self_declared_rate_cannot_borrow_the_report_proof(self) -> None:
        state = probe_shaped_state()
        derived = with_report_rates(state)
        entry = derived["quality"]["production.oxygen_kg_per_cycle"]
        self.assertEqual(entry["policy_version"], REPORT_RATE_POLICY_VERSION)
        self.assertTrue(report_rate_proof_verifier(derived, "production.oxygen_kg_per_cycle", entry))

        forged = copy.deepcopy(state)
        forged["production"]["oxygen_kg_per_cycle"] = 400.0
        forged["quality"]["production.oxygen_kg_per_cycle"] = copy.deepcopy(entry)
        metrics = evaluate_strategy(forged, report_rates=True)["metrics"]
        self.assertIsNone(metrics["renewable_oxygen_kg_per_cycle"])
        self.assertFalse(report_rate_proof_verifier(forged, "production.oxygen_kg_per_cycle", entry))
        self.assertFalse(report_rate_proof_verifier(derived, "production.water_kg_per_cycle", entry))

    def test_supplied_verifier_still_decides_other_proofs(self) -> None:
        # The fixture's own rates are observed; they are not replaced, and the
        # fixture verifier still certifies them next to the report verifier.
        state = add_report_counters(
            base_state(), closed_day=199, open_day=200, positive_kg=1.0, negative_kg=-500.0
        )
        result = evaluate_strategy(state, report_rates=True, trusted_proof_verifier=trusted_fixture_proof)
        self.assertEqual(result["metrics"]["renewable_oxygen_kg_per_cycle"], 200.0)
        self.assertEqual(result["metrics"]["qualities"]["renewable_oxygen"], "observed")
        self.assertEqual(result["metrics"]["renewable_food_kcal_per_cycle"], 4000.0)


if __name__ == "__main__":
    unittest.main()
