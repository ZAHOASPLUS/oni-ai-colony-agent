#!/usr/bin/env python3
"""Focused offline checks for M01 functional-building diagnostics."""

from __future__ import annotations

import copy
import datetime as dt
import pathlib
import sys
import unittest
from typing import Any, Dict, Iterable, Mapping, Sequence


ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import rules  # noqa: E402
from agent.field_contracts import (  # noqa: E402
    ALERTS_CANONICAL_CONTRACT_ID,
    BUILDING_COMPLETED_COUNT_CONTRACT_ID,
    DUPLICANTS_IDLE_CONTRACT_ID,
    DUPLICANTS_STRESSED_CONTRACT_ID,
    DUPLICANTS_TOTAL_CONTRACT_ID,
    FUNCTIONAL_BUILDING_PREFAB_IDS,
    FUNCTIONAL_BUILDINGS_CONTRACT_ID,
    FUNCTIONAL_BUILDINGS_SCHEMA,
    GAME_PAUSED_CONTRACT_ID,
    REGISTERED_FOOD_CONTRACT_ID,
    RESEARCH_CURRENT_CONTRACT_ID,
    build_condition,
    condition_declaration_blockers,
    contract_by_id,
    contract_for_path,
    contract_quality_fixture,
    read_contract_field,
)
from agent.incidents import IncidentTracker  # noqa: E402


BASE_TIME = dt.datetime(2026, 9, 9, 4, 0, 0, tzinfo=dt.timezone.utc)
BASE_TS = BASE_TIME.isoformat()
SAVE_HASH = "sha256:" + "a" * 64


def iso_at(seconds: float) -> str:
    return (BASE_TIME + dt.timedelta(seconds=seconds)).isoformat()


def instance(
    prefab_id: str,
    instance_id: int,
    operational: Any,
    *,
    world_id: int = 0,
) -> Dict[str, Any]:
    return {
        "instance_id": instance_id,
        "world_id": world_id,
        "prefab_id": prefab_id,
        "operational": operational,
    }


def make_state(
    instances: Sequence[Mapping[str, Any]] = (),
    *,
    captured_at: str = BASE_TS,
    snapshot_ts: str = BASE_TS,
    functional_world_id: int = 0,
    snapshot_world_id: int = 0,
    active_world_id: int = 0,
    complete: bool = True,
    alerts: Iterable[str] = (),
    idle: int = 0,
    seq: int = 1,
) -> Dict[str, Any]:
    functional = {
        "schema": FUNCTIONAL_BUILDINGS_SCHEMA,
        "captured_at": captured_at,
        "world_id": functional_world_id,
        "complete": complete,
        "instances": [copy.deepcopy(dict(item)) for item in instances],
    }
    quality = {
        "game.paused": contract_quality_fixture(GAME_PAUSED_CONTRACT_ID),
        "alerts": contract_quality_fixture(ALERTS_CANONICAL_CONTRACT_ID),
        "duplicants.total": contract_quality_fixture(DUPLICANTS_TOTAL_CONTRACT_ID),
        "duplicants.idle": contract_quality_fixture(DUPLICANTS_IDLE_CONTRACT_ID),
        "duplicants.stressed": contract_quality_fixture(DUPLICANTS_STRESSED_CONTRACT_ID),
        "resources.food_kcal": contract_quality_fixture(REGISTERED_FOOD_CONTRACT_ID),
        "research.current": contract_quality_fixture(RESEARCH_CURRENT_CONTRACT_ID),
        "buildings.oxygen_diffuser": contract_quality_fixture(BUILDING_COMPLETED_COUNT_CONTRACT_ID),
        "buildings.outhouse": contract_quality_fixture(BUILDING_COMPLETED_COUNT_CONTRACT_ID),
        "buildings.manual_generator": contract_quality_fixture(BUILDING_COMPLETED_COUNT_CONTRACT_ID),
        "functional_buildings": contract_quality_fixture(FUNCTIONAL_BUILDINGS_CONTRACT_ID),
    }
    return {
        "schema": "oni.state.v2",
        "event": "state_snapshot",
        "session_id": "m01-functional-test",
        "seq": seq,
        "snapshot_seq": seq,
        "snapshot_id": f"m01-functional-test:{seq}",
        "ts": snapshot_ts,
        "game_build": "cl-test",
        "mod_version": "0.2.0-test",
        "save_id_hash": SAVE_HASH,
        "save_dirty": False,
        "world_id": snapshot_world_id,
        "observation_scope": {
            "active_world_id": active_world_id,
            "populated_world_ids": [active_world_id],
            "covered_world_ids": [active_world_id],
            "complete_for_populated_worlds": True,
        },
        "sample": {"interval_seconds": 5.0},
        "writer": {
            "queue_depth": 0,
            "dropped_snapshot_count": 0,
            "fault_count": 0,
            "non_finite_numeric_count": 0,
        },
        "bridge": {
            "stale": False,
            "replay_mode": False,
            "trust_ready": True,
            "trust_blockers": [],
        },
        "game": {"paused": False},
        "duplicants": {"total": 3, "idle": idle, "stressed": 0},
        "resources": {"food_kcal": 100_000.0},
        "alerts": sorted(alerts),
        "research": {"current": "FarmingTech"},
        # The legacy completed-prefab count remains present and tested as an
        # additive telemetry field; functional diagnostics do not consume it.
        "buildings": {
            "oxygen_diffuser": 1,
            "outhouse": 1,
            "manual_generator": 1,
            "captured_at": snapshot_ts,
        },
        "functional_buildings": functional,
        "quality": quality,
    }


def signals_by_id(state: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {
        item["incident_id"]: item
        for item in rules.evaluate_signals(dict(state))
    }


class FunctionalBuildingDiagnosticsTests(unittest.TestCase):
    def test_contract_read_and_static_producer_wiring(self) -> None:
        contract = contract_by_id(FUNCTIONAL_BUILDINGS_CONTRACT_ID)
        self.assertIsNotNone(contract)
        self.assertIs(contract_for_path("functional_buildings"), contract)
        self.assertFalse(contract.mutating_condition_allowed)

        state = make_state(
            [instance("OxygenDiffuser", 101, True), instance("Outhouse", 102, False)]
        )
        read = read_contract_field(
            state,
            "functional_buildings",
            FUNCTIONAL_BUILDINGS_CONTRACT_ID,
        )
        self.assertTrue(read.usable, read.blockers)
        self.assertEqual(read.value["schema"], FUNCTIONAL_BUILDINGS_SCHEMA)
        self.assertEqual(
            set(read.value), {"schema", "captured_at", "world_id", "complete", "instances"}
        )
        self.assertEqual(
            set(read.value["instances"][0]),
            {"instance_id", "world_id", "prefab_id", "operational"},
        )

        condition = build_condition(
            "functional_buildings",
            "eq",
            state["functional_buildings"],
            FUNCTIONAL_BUILDINGS_CONTRACT_ID,
        )
        self.assertIn(
            "quality_contract_not_mutation_eligible",
            condition_declaration_blockers(condition, action="set_research"),
        )

        reader_text = (
            ROOT / "mod" / "Source" / "Probe" / "FunctionalBuildingTelemetryReader.cs"
        ).read_text(encoding="utf-8-sig")
        probe_text = (
            ROOT / "mod" / "Source" / "Probe" / "ProbeStateReader.cs"
        ).read_text(encoding="utf-8-sig")
        for marker in (
            "Components.BuildingCompletes.GetWorldItems(activeWorldId)",
            "BuildingComplete",
            "KPrefabID",
            "Operational",
            "IsOperational",
            '"complete"',
            '"instances"',
            '"operational_component_missing"',
        ):
            self.assertIn(marker, reader_text)
        self.assertIn("if (operational == null)", reader_text)
        self.assertNotIn("GetComponentRegistryItems", reader_text)
        self.assertNotIn("reflection_all_worlds", reader_text)
        self.assertNotIn("AddSeconds(30)", reader_text)
        self.assertIn("FunctionalBuildingTelemetryReader.Read", probe_text)
        self.assertIn('"functional_buildings"', probe_text)
        self.assertIn('"oni.qc.buildings.functional_instances.v1"', probe_text)
        self.assertIn(
            'functionalBuildingQuality["captured_at"] = "functional_buildings.captured_at";',
            probe_text,
        )

    def test_true_false_mixed_and_no_instances_classification(self) -> None:
        false_state = make_state(
            [
                instance("OxygenDiffuser", 101, False),
                instance("Outhouse", 102, True),
                instance("ManualGenerator", 103, True),
            ]
        )
        false_signals = signals_by_id(false_state)
        self.assertEqual(
            false_signals["non_operational_oxygen_diffuser"]["state"], "active"
        )
        self.assertIsNone(false_signals["non_operational_oxygen_diffuser"]["command"])
        self.assertEqual(
            false_signals["non_operational_outhouse"]["state"], "inactive"
        )

        mixed_state = make_state(
            [
                instance("OxygenDiffuser", 101, True),
                instance("OxygenDiffuser", 104, False),
            ]
        )
        self.assertEqual(
            signals_by_id(mixed_state)["non_operational_oxygen_diffuser"]["state"],
            "active",
        )

        true_state = make_state(
            [
                instance("OxygenDiffuser", 101, True),
                instance("Outhouse", 102, True),
                instance("ManualGenerator", 103, True),
            ]
        )
        true_signals = signals_by_id(true_state)
        for incident_id in rules.FUNCTIONAL_BUILDING_INCIDENT_IDS:
            self.assertEqual(true_signals[incident_id]["state"], "inactive")
            self.assertIsNone(true_signals[incident_id]["command"])

        empty_signals = signals_by_id(make_state())
        for incident_id in rules.FUNCTIONAL_BUILDING_INCIDENT_IDS:
            self.assertEqual(empty_signals[incident_id]["state"], "inactive")

        tracker = IncidentTracker()
        tracker.update(false_state, rules.evaluate_signals(false_state))
        advice = rules.build_arbitrated_advice(false_state, tracker.open_incidents())
        oxygen_advice = next(
            item for item in advice if item["incident_id"] == "non_operational_oxygen_diffuser"
        )
        self.assertNotIn("command", oxygen_advice)

    def test_installed_mineral_deoxidizer_is_explicit_oxygen_alias(self) -> None:
        state = make_state([instance("MineralDeoxidizer", 120, False)])
        signals = signals_by_id(state)
        self.assertEqual(
            signals["non_operational_oxygen_diffuser"]["state"], "active"
        )
        self.assertIsNone(signals["non_operational_oxygen_diffuser"]["command"])

        mixed = make_state(
            [
                instance("OxygenDiffuser", 121, True),
                instance("MineralDeoxidizer", 122, False),
            ]
        )
        self.assertEqual(
            signals_by_id(mixed)["non_operational_oxygen_diffuser"]["state"],
            "active",
        )

    def test_false_to_true_reconciliation_and_unknown_does_not_resolve(self) -> None:
        tracker = IncidentTracker()
        false_state = make_state([instance("Outhouse", 201, False)], seq=1)
        tracker.update(false_state, rules.evaluate_signals(false_state))
        self.assertEqual(
            tracker.incidents["non_operational_outhouse"]["status"], "open"
        )

        true_state = make_state(
            [instance("Outhouse", 201, True)],
            snapshot_ts=iso_at(5),
            seq=2,
        )
        tracker.update(true_state, rules.evaluate_signals(true_state))
        resolved = tracker.incidents["non_operational_outhouse"]
        self.assertEqual(resolved["status"], "resolved")
        self.assertIsNotNone(resolved["resolved_at"])
        self.assertNotIn(
            "non_operational_outhouse",
            {item["incident_id"] for item in tracker.open_incidents()},
        )

        unknown_tracker = IncidentTracker()
        unknown_tracker.update(false_state, rules.evaluate_signals(false_state))
        unknown_state = make_state(
            [instance("Outhouse", 201, None)],
            snapshot_ts=iso_at(5),
            seq=2,
        )
        unknown_tracker.update(unknown_state, rules.evaluate_signals(unknown_state))
        blocked = unknown_tracker.incidents["non_operational_outhouse"]
        self.assertEqual(blocked["status"], "blocked_unknown")
        self.assertIsNone(blocked["resolved_at"])
        self.assertFalse(
            any(
                event["event"] == "incident_resolved"
                and event["incident_id"] == "non_operational_outhouse"
                for event in unknown_tracker.timeline
            )
        )

    def test_null_and_malformed_instance_data_fail_closed(self) -> None:
        null_state = make_state(
            [instance("OxygenDiffuser", 101, True), instance("OxygenDiffuser", 104, None)]
        )
        null_read = read_contract_field(null_state, "functional_buildings")
        self.assertTrue(null_read.usable, null_read.blockers)
        self.assertEqual(
            signals_by_id(null_state)["non_operational_oxygen_diffuser"]["state"],
            "unknown",
        )

        missing_instance = instance("OxygenDiffuser", 101, True)
        del missing_instance["operational"]
        missing_state = make_state([missing_instance])
        missing_read = read_contract_field(missing_state, "functional_buildings")
        self.assertFalse(missing_read.usable)
        self.assertIn("functional_building_instance_shape_invalid", missing_read.blockers)
        self.assertEqual(
            signals_by_id(missing_state)["non_operational_oxygen_diffuser"]["state"],
            "unknown",
        )

        extra_key_state = make_state([instance("OxygenDiffuser", 101, False)])
        extra_key_state["functional_buildings"]["producer_debug"] = True
        extra_key_read = read_contract_field(extra_key_state, "functional_buildings")
        self.assertFalse(extra_key_read.usable)
        self.assertIn(
            "functional_buildings_payload_shape_invalid",
            extra_key_read.blockers,
        )
        self.assertEqual(
            signals_by_id(extra_key_state)["non_operational_oxygen_diffuser"]["state"],
            "unknown",
        )

    def test_bad_duplicate_and_wrong_world_ids_fail_closed(self) -> None:
        duplicate_state = make_state(
            [instance("OxygenDiffuser", 101, False), instance("Outhouse", 101, True)]
        )
        duplicate_read = read_contract_field(duplicate_state, "functional_buildings")
        self.assertFalse(duplicate_read.usable)
        self.assertIn("functional_building_instance_id_duplicate", duplicate_read.blockers)
        self.assertEqual(
            signals_by_id(duplicate_state)["non_operational_oxygen_diffuser"]["state"],
            "unknown",
        )

        bad_id = make_state([instance("OxygenDiffuser", 0, False)])
        bad_id_read = read_contract_field(bad_id, "functional_buildings")
        self.assertFalse(bad_id_read.usable)
        self.assertIn("functional_building_instance_id_invalid", bad_id_read.blockers)

        wrong_prefab = make_state([instance("NotAFunctionalBuilding", 101, False)])
        wrong_prefab_read = read_contract_field(wrong_prefab, "functional_buildings")
        self.assertFalse(wrong_prefab_read.usable)
        self.assertIn("functional_building_prefab_id_invalid", wrong_prefab_read.blockers)

        wrong_world = make_state(
            [instance("OxygenDiffuser", 101, False, world_id=7)],
            functional_world_id=7,
        )
        wrong_world_read = read_contract_field(wrong_world, "functional_buildings")
        self.assertFalse(wrong_world_read.usable)
        self.assertIn("functional_buildings_snapshot_world_mismatch", wrong_world_read.blockers)
        self.assertEqual(
            signals_by_id(wrong_world)["non_operational_oxygen_diffuser"]["state"],
            "unknown",
        )

    def test_incomplete_error_stale_and_future_payloads_fail_closed(self) -> None:
        incomplete = make_state([instance("OxygenDiffuser", 101, False)], complete=False)
        incomplete_read = read_contract_field(incomplete, "functional_buildings")
        self.assertFalse(incomplete_read.usable)
        self.assertIn("functional_buildings_incomplete", incomplete_read.blockers)

        error_quality = make_state([instance("OxygenDiffuser", 101, False)])
        error_quality["quality"]["functional_buildings"].update(
            {"status": "error", "completeness": "unknown"}
        )
        self.assertFalse(read_contract_field(error_quality, "functional_buildings").usable)

        stale = make_state(
            [instance("OxygenDiffuser", 101, False)],
            captured_at=BASE_TS,
            snapshot_ts=iso_at(6),
        )
        stale_read = read_contract_field(stale, "functional_buildings")
        self.assertFalse(stale_read.usable)
        self.assertIn("quality_capture_stale", stale_read.blockers)

        future = make_state(
            [instance("OxygenDiffuser", 101, False)],
            captured_at=iso_at(10),
            snapshot_ts=BASE_TS,
        )
        future_read = read_contract_field(future, "functional_buildings")
        self.assertFalse(future_read.usable)
        self.assertIn("quality_capture_in_future", future_read.blockers)

        for candidate in (incomplete, error_quality, stale, future):
            self.assertEqual(
                signals_by_id(candidate)["non_operational_oxygen_diffuser"]["state"],
                "unknown",
            )

    def test_quality_missing_or_estimated_is_unknown(self) -> None:
        missing_quality = make_state([instance("OxygenDiffuser", 101, False)])
        missing_quality["quality"].pop("functional_buildings")
        missing_read = read_contract_field(missing_quality, "functional_buildings")
        self.assertFalse(missing_read.usable)
        self.assertIn("quality_not_observed", missing_read.blockers)
        self.assertEqual(
            signals_by_id(missing_quality)["non_operational_oxygen_diffuser"]["state"],
            "unknown",
        )

        estimated = make_state([instance("OxygenDiffuser", 101, False)])
        estimated["quality"]["functional_buildings"].update(
            {"status": "estimated", "completeness": "complete"}
        )
        estimated_read = read_contract_field(estimated, "functional_buildings")
        self.assertFalse(estimated_read.usable)
        self.assertEqual(
            signals_by_id(estimated)["non_operational_oxygen_diffuser"]["state"],
            "unknown",
        )

    def test_idle_is_not_a_building_fault_and_suffocation_keeps_sole_pause(self) -> None:
        idle_state = make_state(
            [instance("OxygenDiffuser", 101, True)],
            idle=1,
        )
        idle_signals = signals_by_id(idle_state)
        self.assertEqual(idle_signals["idle_duplicants"]["state"], "active")
        self.assertEqual(
            idle_signals["non_operational_oxygen_diffuser"]["state"], "inactive"
        )

        emergency_state = make_state(
            [instance("OxygenDiffuser", 101, False)],
            alerts=("suffocation",),
        )
        tracker = IncidentTracker()
        tracker.update(emergency_state, rules.evaluate_signals(emergency_state))
        advice = rules.build_arbitrated_advice(emergency_state, tracker.open_incidents())
        commands = [item for item in advice if item.get("command") is not None]
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0]["incident_id"], "suffocation")
        self.assertEqual(commands[0]["command"]["action"], "set_paused")
        building_advice = next(
            item
            for item in advice
            if item["incident_id"] == "non_operational_oxygen_diffuser"
        )
        self.assertIsNone(building_advice.get("command"))


if __name__ == "__main__":
    unittest.main()
