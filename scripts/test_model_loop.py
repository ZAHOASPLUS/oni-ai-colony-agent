"""End-to-end tests of the minimal model-driven loop (review 2026-09-26, M1).

The fake model stands in for the provider; everything else is the real
Bridge generation (trust, decision frame, archive, pipeline gate), the real
candidate builders, the real approval-receipt issuer and the approval commit
validator used by scripts/approve-command.ps1.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import approval_receipt, candidates  # noqa: E402
from agent.advanced_actions import BLUEPRINT_CELL_FIELDS, blueprint_cell_path, dig_cell_path  # noqa: E402
from agent.command_queue import command_pipeline_readiness, load_policy, move_to_rejected  # noqa: E402
from agent.field_contracts import (  # noqa: E402
    ALERTS_CANONICAL_CONTRACT_ID,
    BLUEPRINT_CATALOG_CONTRACT_ID,
    BLUEPRINT_CELL_CONTRACT_ID,
    DIG_CELL_CONTRACT_ID,
    DIG_TOOL_CONTRACT_ID,
    DUPLICANT_CHORE_PRIORITY_CONTRACT_ID,
    GAME_BUILD_MODE_CONTRACT_ID,
    RESEARCH_COMPLETE_CONTRACT_ID,
    contract_quality_fixture,
)
from agent.llm_planner import ModelRequestError  # noqa: E402
from agent.model_loop import CHOICE_SCHEMA, ModelLoop, colony_summary  # noqa: E402
from bridge import oni_bridge  # noqa: E402
from scripts import commit_command_approval  # noqa: E402
from scripts import verify_bridge_reliability as fixtures  # noqa: E402

SESSION = "session-model-loop"
DIG_CELLS = (5000, 5001, 5002, 7003)
LADDER_CELLS = (9001, 9000)  # unsorted on purpose: the candidate picks the lowest
OXYGEN_DIFFUSER_CELLS = (9100,)
ALGAE_TERRARIUM_CELLS = (9200,)
ALL_CANDIDATE_ACTIONS = (
    "set_paused", "set_research", "set_duplicant_chore_priority", "mark_dig_cells", "place_building_blueprint",
)
TEST_CONFIG = {"api_key": "sk-test-not-a-real-key-000000", "base_url": "https://model.invalid/v1", "model": "fake"}


def enrich(state: dict, priorities: dict | None = None) -> dict:
    """Add duplicant priorities and dig candidates with their field contracts."""

    quality = state["quality"]
    priorities = priorities or {101: ("Ada", {"Dig": 3, "Build": 2}), 102: ("米波", {"Dig": 1, "Build": 4})}
    state["duplicants"]["by_instance_id"] = {}
    for instance_id, (name, groups) in priorities.items():
        state["duplicants"]["by_instance_id"][f"id_{instance_id}"] = {
            "instance_id": instance_id, "name": name, "world_id": 0, "chore_priorities": dict(groups),
        }
        for group in groups:
            path = f"duplicants.by_instance_id.id_{instance_id}.chore_priorities.{group}"
            quality[path] = contract_quality_fixture(DUPLICANT_CHORE_PRIORITY_CONTRACT_ID)
    by_cell = {}
    for index, cell in enumerate(DIG_CELLS):
        x, y = (10 + index, 20) if cell < 7000 else (33, 27)
        by_cell[f"cell_{cell}"] = {
            "cell": cell, "x": x, "y": y, "world_id": 0, "visible": True, "solid": True,
            "foundation": False, "element_id": "SandStone", "mass_kg": 1840.0, "hardness": 10,
            "damage": 0.0, "has_constructable": False, "designation_present": False,
            "designation_instance_id": 0, "designation_kind": "none", "designation_in_progress": False,
            "safe_mark_candidate": True, "fingerprint": "sha256:" + format(cell, "x").rjust(64, "a"),
        }
        for field in ("fingerprint", "element_id", "mass_kg", "damage", "solid", "designation_present",
                      "designation_instance_id", "designation_kind", "designation_in_progress",
                      "safe_mark_candidate"):
            quality[f"{dig_cell_path(cell)}.{field}"] = contract_quality_fixture(DIG_CELL_CONTRACT_ID)
    state["designations"] = {"dig": {
        "tool": {"available": True, "tiles_filter_on": True, "natural_backwall_filter_on": False},
        "candidate_cells": list(DIG_CELLS), "by_cell": by_cell,
    }}
    for path in ("designations.dig.tool.tiles_filter_on", "designations.dig.tool.natural_backwall_filter_on"):
        quality[path] = contract_quality_fixture(DIG_TOOL_CONTRACT_ID)
    # The approved blueprint catalogs, as the Probe reports them: a Ladder on
    # two empty cells, an Oxygen Diffuser and an Algae Terrarium on one each.
    state["game"]["sandbox_mode"] = False
    state["game"]["instant_build_mode"] = False
    for path in ("game.sandbox_mode", "game.instant_build_mode"):
        quality[path] = contract_quality_fixture(GAME_BUILD_MODE_CONTRACT_ID)
    catalogs = {
        "Ladder": {
            "available": True, "prefab_id": "Ladder", "is_utility": False, "cancellable": True, "width": 1,
            "height": 1, "build_location_rule": "Anywhere", "orientation": "Neutral", "facade_id": "DEFAULT_FACADE",
            "material_categories": ["BuildableRaw&BuildingWood"], "material_tags": ["WoodLog"],
            "required_mass_kg": [100.0], "available_mass_kg": [1500.0], "materials_available": True,
            "candidate_cells": list(LADDER_CELLS),
        },
        "MineralDeoxidizer": {
            "available": True, "prefab_id": "MineralDeoxidizer", "is_utility": False, "cancellable": True,
            "width": 1, "height": 2, "build_location_rule": "OnFloor", "orientation": "Neutral",
            "facade_id": "DEFAULT_FACADE", "material_categories": ["Metal"], "material_tags": ["Cuprite"],
            "required_mass_kg": [400.0], "available_mass_kg": [2400.0], "materials_available": True,
            "candidate_cells": list(OXYGEN_DIFFUSER_CELLS),
        },
        "AlgaeHabitat": {
            "available": True, "prefab_id": "AlgaeHabitat", "is_utility": False, "cancellable": True,
            "width": 1, "height": 2, "build_location_rule": "OnFloor", "orientation": "Neutral",
            "facade_id": "DEFAULT_FACADE", "material_categories": ["Farmable"], "material_tags": ["Dirt"],
            "required_mass_kg": [400.0], "available_mass_kg": [800.0], "materials_available": True,
            "candidate_cells": list(ALGAE_TERRARIUM_CELLS),
        },
    }
    for def_id, catalog in catalogs.items():
        for field in catalog:
            quality[f"blueprints.catalog.{def_id}.{field}"] = contract_quality_fixture(BLUEPRINT_CATALOG_CONTRACT_ID)
    by_id = {}
    placements = [(cell, 40 + index, 12, "Ladder") for index, cell in enumerate(LADDER_CELLS)]
    placements += [(cell, 50, 12, "MineralDeoxidizer") for cell in OXYGEN_DIFFUSER_CELLS]
    placements += [(cell, 52, 12, "AlgaeHabitat") for cell in ALGAE_TERRARIUM_CELLS]
    for cell, x, y, def_id in placements:
        empty = "sha256:" + f"{cell:064x}"[-64:]
        by_id[f"cell_{cell}"] = {
            "cell": cell, "x": x, "y": y, "world_id": 0, "visible": True, "solid": False,
            "foundation": False, "element_id": "Vacuum", "mass_kg": 0.0, "damage": 0.0, "object_count": 0,
            "empty_for_blueprint": True, "can_place_ladder": True, "can_place_wire": True,
            "can_place_mineral_deoxidizer": def_id == "MineralDeoxidizer",
            "can_place_algae_habitat": def_id == "AlgaeHabitat",
            "blueprint_present": False, "blueprint_instance_id": 0, "blueprint_prefab_id": "none",
            "blueprint_orientation": "none", "blueprint_material_tags": [], "blueprint_in_progress": False,
            "blueprint_is_utility": False, "empty_fingerprint": empty, "fingerprint": empty,
        }
        for field in BLUEPRINT_CELL_FIELDS + ("can_place_mineral_deoxidizer", "can_place_algae_habitat"):
            quality[f"{blueprint_cell_path(cell)}.{field}"] = contract_quality_fixture(BLUEPRINT_CELL_CONTRACT_ID)
    state["blueprints"] = {"catalog": catalogs, "by_instance_id": {}, "utility_paths": {}}
    state["cells"] = {"by_id": by_id}
    return state


class FakeModel:
    """Answers with a fixed candidate id and records every prompt."""

    def __init__(self, candidate_id: str = "priority_102_Dig_2", *, reject_schema: bool = False):
        self.candidate_id = candidate_id
        self.reject_schema = reject_schema
        self.prompts: list[str] = []
        self.schemas: list = []
        self.release = threading.Event()
        self.release.set()

    def __call__(self, config, prompt_text, response_schema, *, timeout):
        self.prompts.append(prompt_text)
        self.schemas.append(response_schema)
        self.release.wait(10)
        if self.reject_schema and response_schema is not None:
            raise ModelRequestError("http_400")
        content = json.dumps({"schema": CHOICE_SCHEMA, "candidate_id": self.candidate_id,
                              "reason": "Dig is the bottleneck"})
        return {"choices": [{"message": {"content": content}}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


class Clock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value


class ModelLoopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="oni-model-loop-")
        self.addCleanup(self.temp.cleanup)
        self.bridge = pathlib.Path(self.temp.name) / "bridge"
        self.bridge.mkdir()
        self.expected = fixtures.startup_expectations()
        self.binding = dict(active_path_id=self.expected["loader_path_id_upper"],
                            save_id_hash=self.expected["loader_save_sha256"])
        self.seq = 1
        self.policy = {
            "schema": "oni.action-policy.v1", "actions_enabled": True, "suggestions_enabled": True,
            "allowed_actions": ["noop", "focus_note", *ALL_CANDIDATE_ACTIONS],
            "allowed_save_hashes": [self.expected["loader_save_sha256"]],
            "command_ttl_seconds": 120, "max_pending_commands": 20,
        }
        (self.bridge / "action-policy.json").write_text(json.dumps(self.policy), encoding="utf-8")
        records = [fixtures.startup_event(self.expected, SESSION, 1, "session_started",
                                          reason="save_loader_load_complete", **self.binding)]
        records += [self.snapshot() for _ in range(3)]
        with (self.bridge / "state.jsonl").open("wb") as stream:
            for record in records:
                stream.write(fixtures.json_line(record) + b"\n")
        self.runtime, _ = oni_bridge.replay_initial_records(
            self.bridge / "state.jsonl", self.bridge,
            startup_expectations=self.expected, require_startup_binding=True,
        )
        self.clock = Clock()

    def snapshot(self, *, paused: bool = True, priorities: dict | None = None) -> dict:
        self.seq += 1
        record = enrich(
            fixtures.startup_snapshot(self.expected, SESSION, self.seq, self.seq - 1, paused=paused, **self.binding),
            priorities,
        )
        record["ts"] = fixtures.iso_now()
        return record

    def loop(self, model: FakeModel) -> ModelLoop:
        return ModelLoop(model_caller=model, config_override=dict(TEST_CONFIG), min_interval_seconds=0,
                         monotonic=self.clock)

    def tick(self, loop: ModelLoop) -> dict:
        oni_bridge.write_outputs(self.bridge, self.runtime, model_loop=loop)
        return json.loads((self.bridge / "model-loop.json").read_text(encoding="utf-8"))

    def pending(self) -> list[pathlib.Path]:
        return sorted((self.bridge / "commands" / "pending").glob("*.json"))

    def test_choice_is_published_as_one_approvable_model_selected_command(self):
        model = FakeModel()
        loop = self.loop(model)
        first = self.tick(loop)
        self.assertEqual(first["phase"], "awaiting_model")
        self.assertEqual([c["candidate_id"] for c in first["candidates"]],
                         ["research_ImprovedOxygen", "research_PowerRegulation", "priority_102_Dig_2",
                          "priority_101_Build_3", "dig_5000_3", "oxygen_diffuser_9100", "algae_terrarium_9200",
                          "ladder_9000", "resume_game", "wait"])
        self.assertTrue(loop.join_request(5))
        # The structured-output schema limits the answer to the offered ids.
        self.assertEqual(model.schemas[0]["properties"]["candidate_id"]["enum"],
                         [c["candidate_id"] for c in first["candidates"]])

        second = self.tick(loop)
        self.assertEqual(second["phase"], "awaiting_human_approval")
        [path] = self.pending()
        command = json.loads(path.read_text(encoding="utf-8"))
        frame_id = json.loads((self.bridge / "decision-frame.json").read_text(encoding="utf-8"))["frame_id"]
        self.assertEqual(command["mode"], "model_selected")
        self.assertEqual(command["action"], "set_duplicant_chore_priority")
        self.assertEqual(command["args"]["expected_name"], "米波")
        self.assertEqual((command["args"]["from_priority"], command["args"]["priority"]), (1, 2))
        self.assertEqual(command["source"]["kind"], "model_choice")
        self.assertEqual(command["source_frame_id"], frame_id)
        self.assertEqual(second["approve_with"], f"scripts/approve-command.ps1 -CommandId {command['id']}")

        # The unmodified approval chain accepts it against the archived frame.
        frame_path = self.bridge / "decision-frames" / ("sha256-" + frame_id.split(":", 1)[1] + ".json")
        frame = json.loads(frame_path.read_text(encoding="utf-8"))
        policy = load_policy(self.bridge)
        approved = approval_receipt.build_approved_command(
            command, policy, frame, approved_by="local_human", approved_at=fixtures.iso_now(),
        )
        approval_receipt.validate_approval_receipt(approved, policy, frame)
        commit_command_approval._validate_payloads(command, approved, self.bridge / "action-policy.json", frame_path)

        # While it is pending nothing else is requested or published, and the
        # queue reports the model-selection latch.
        third = self.tick(loop)
        self.assertEqual(len(model.prompts), 1)
        self.assertEqual(len(self.pending()), 1)
        self.assertIn("model_command_in_flight", third["blockers"])
        gate = command_pipeline_readiness(self.bridge, policy=policy)
        self.assertIn(f"mutation_pending_model_selection:{command['id']}", gate["blockers"])

    @unittest.skipUnless(shutil.which("powershell"), "Windows PowerShell is required")
    def test_real_approval_script_moves_the_choice_to_approved(self):
        loop = self.loop(FakeModel())
        self.tick(loop)
        loop.join_request(5)
        self.tick(loop)
        [path] = self.pending()
        command_id = path.stem
        temp_root = pathlib.Path(tempfile.gettempdir())
        before = set(temp_root.glob("oni-approval-*"))
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                 str(ROOT / "scripts" / "approve-command.ps1"), "-CommandId", command_id,
                 "-BridgeDir", str(self.bridge)],
                capture_output=True, text=True, errors="replace", timeout=120,
            )
        finally:
            for leftover in set(temp_root.glob("oni-approval-*")) - before:
                if (leftover / f"{command_id}.json").exists():
                    shutil.rmtree(leftover, ignore_errors=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        approved = json.loads((self.bridge / "commands" / "approved" / f"{command_id}.json").read_text(encoding="utf-8"))
        self.assertEqual((approved["status"], approved["mode"]), ("approved", "model_selected"))
        self.assertEqual(approved["args"]["expected_name"], "米波")
        self.assertEqual(approved["approval_receipt"]["command_id"], command_id)
        status = self.tick(loop)
        self.assertEqual(status["recent_outcomes"][0]["stage"], "approved")
        self.assertIn("model_command_in_flight", status["blockers"])

    def test_outcome_is_fed_back_into_the_next_prompt(self):
        model = FakeModel()
        loop = self.loop(model)
        self.tick(loop)
        loop.join_request(5)
        self.tick(loop)
        [path] = self.pending()
        move_to_rejected(path, self.bridge / "commands" / "rejected",
                         json.loads(path.read_text(encoding="utf-8")), "expired_before_approval")
        status = self.tick(loop)
        self.assertEqual(status["recent_outcomes"][0]["stage"], "rejected")
        self.assertEqual(len(model.prompts), 1)  # same candidate set answered recently
        self.clock.value += 301.0
        self.tick(loop)
        loop.join_request(5)
        self.assertEqual(len(model.prompts), 2)
        payload = json.loads(model.prompts[1].split("\n\n", 1)[1])
        [outcome] = payload["recent_outcomes"]
        self.assertEqual((outcome["stage"], outcome["detail"]), ("rejected", "expired_before_approval"))
        self.assertFalse(outcome["effect_confirmed"])

    def test_prompt_carries_only_observed_balance_fields(self):
        # The oxygen report counters and power reach the model only when the
        # Probe marks them observed (review 2026-09-26: the prompt had no
        # resource balance at all).
        state = self.snapshot()
        state["report_counters"] = {
            "current": {"report_day": 6, "oxygen_created": {"positive_kg": 25.5, "negative_kg": -127.0}},
            "previous_closed": {"report_day": 5, "oxygen_created": {"positive_kg": 48.5, "negative_kg": -210.25}},
        }
        state["power"] = {"generator_count": 1, "battery_count": 3, "active_load_watts": 0,
                          "active_rated_generation_watts": 0}
        for path in ("report_counters.current.report_day", "report_counters.current.oxygen_created.positive_kg",
                     "report_counters.current.oxygen_created.negative_kg", "power.generator_count"):
            state["quality"][path] = {"status": "observed", "source": "fixture"}
        state["quality"]["power.battery_count"] = {"status": "error", "source": "fixture"}
        fields = colony_summary(state)["fields"]
        self.assertEqual(fields["report_counters.current.oxygen_created.negative_kg"], -127.0)
        self.assertEqual(fields["report_counters.current.report_day"], 6)
        self.assertEqual(fields["power.generator_count"], 1)
        self.assertIsNone(fields["power.battery_count"])  # reader error
        self.assertIsNone(fields["report_counters.previous_closed.oxygen_created.negative_kg"])  # no quality entry

    def test_cli_refuses_the_model_loop_outside_watch_mode_or_with_the_ranker(self):
        for extra, message in ((["--once"], "needs watch mode"),
                               (["--enable-llm"], "mutually exclusive")):
            result = subprocess.run(
                [sys.executable, "-B", str(ROOT / "bridge" / "oni_bridge.py"), "--bridge-dir",
                 str(self.bridge / "cli"), "--model-loop", *extra],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(message, result.stderr)
        self.assertFalse((self.bridge / "cli").exists())

    def publish_one(self) -> tuple[ModelLoop, pathlib.Path]:
        loop = self.loop(FakeModel())
        self.tick(loop)
        loop.join_request(5)
        self.tick(loop)
        [path] = self.pending()
        return loop, path

    def test_pending_choice_survives_new_snapshots_until_its_ttl(self):
        self.policy["command_ttl_seconds"] = 3
        (self.bridge / "action-policy.json").write_text(json.dumps(self.policy), encoding="utf-8")
        loop, path = self.publish_one()
        # A suggested command would be rejected by the next snapshot; this one
        # is bound to its archived frame and stays approvable.
        self.runtime.process_many([self.snapshot()])
        self.tick(loop)
        self.assertEqual(self.pending(), [path])
        time.sleep(3.2)
        self.runtime.process_many([self.snapshot()])
        status = self.tick(loop)
        self.assertEqual(self.pending(), [])
        rejected = json.loads((self.bridge / "commands" / "rejected" / path.name).read_text(encoding="utf-8"))
        self.assertEqual(rejected["rejection_reason"], "expired_before_approval")
        self.assertEqual(status["recent_outcomes"][0]["detail"], "expired_before_approval")

    def test_disabling_suggestions_withdraws_the_choice(self):
        loop, path = self.publish_one()
        self.policy["suggestions_enabled"] = False
        (self.bridge / "action-policy.json").write_text(json.dumps(self.policy), encoding="utf-8")
        status = self.tick(loop)
        self.assertEqual(self.pending(), [])
        rejected = json.loads((self.bridge / "commands" / "rejected" / path.name).read_text(encoding="utf-8"))
        self.assertEqual(rejected["rejection_reason"], "suggestions_disabled")
        self.assertIn("suggestions_disabled", status["blockers"])

    def test_wait_and_invalid_answers_publish_nothing(self):
        for answer, event in (("wait", "model_choice"), ("build_a_rocket", "request_failed")):
            with self.subTest(answer=answer):
                loop = self.loop(FakeModel(answer))
                self.tick(loop)
                loop.join_request(5)
                status = self.tick(loop)
                self.assertEqual(self.pending(), [])
                self.assertEqual(status["last_event"]["kind"], event)

    def test_structured_output_rejection_falls_back_to_plain_json(self):
        model = FakeModel(reject_schema=True)
        loop = self.loop(model)
        self.tick(loop)
        loop.join_request(5)
        status = self.tick(loop)
        self.assertEqual([schema is None for schema in model.schemas], [False, True])
        self.assertEqual(status["last_event"]["kind"], "published")
        self.assertEqual(len(self.pending()), 1)

    def test_total_deadline_abandons_a_slow_request(self):
        model = FakeModel()
        model.release.clear()
        loop = self.loop(model)
        self.tick(loop)
        self.clock.value += 91.0
        status = self.tick(loop)
        self.assertEqual(status["phase"], "draining_abandoned_request")
        self.assertEqual(status["last_event"]["status"], "deadline_exceeded")
        model.release.set()
        loop.join_request(5)
        self.tick(loop)
        self.assertEqual(self.pending(), [])  # the late answer is never used

    def test_choice_for_a_changed_state_is_not_published(self):
        model = FakeModel()
        model.release.clear()
        loop = self.loop(model)
        self.tick(loop)
        # The duplicant's Dig priority changed while the model was answering.
        changed = {101: ("Ada", {"Dig": 3, "Build": 2}), 102: ("米波", {"Dig": 4, "Build": 4})}
        self.runtime.process_many([self.snapshot(priorities=changed)])
        model.release.set()
        loop.join_request(5)
        status = self.tick(loop)
        self.assertEqual(self.pending(), [])
        self.assertEqual(status["last_event"]["kind"], "choice_stale")

    def test_rule_pause_suggestion_owns_the_pipeline(self):
        model = FakeModel()
        loop = self.loop(model)
        advice = [{"incident_id": "test.suffocation", "risk": "critical", "title": "Suffocation alert observed",
                   "reason": "r", "incident_opened_at": self.runtime.latest["ts"],
                   "command": {"action": "set_paused", "args": {"paused": True}}}]
        with mock.patch.object(oni_bridge.agent_rules, "build_arbitrated_advice", return_value=advice):
            status = self.tick(loop)
        self.assertIn("rules_mutation_suggested", status["blockers"])
        self.assertEqual(model.prompts, [])

    def test_informational_rule_findings_reach_the_model_instead_of_stopping_it(self):
        # A cycle-6 live snapshot keeps "Native manual generator operational
        # check failed" open indefinitely; idle duplicants are routine.
        model = FakeModel()
        loop = self.loop(model)
        opened = self.runtime.latest["ts"]
        advice = [
            {"incident_id": "native.manual_generator", "risk": "medium", "incident_opened_at": opened,
             "title": "Native manual generator operational check failed", "reason": "r"},
            {"incident_id": "idle_duplicants", "risk": "medium", "incident_opened_at": opened,
             "title": "Duplicants are idle", "reason": "r",
             "command": {"action": "focus_note", "args": {"tag": "idle_dupes", "message": "1 idle"}}},
        ]
        with mock.patch.object(oni_bridge.agent_rules, "build_arbitrated_advice", return_value=advice):
            status = self.tick(loop)
            loop.join_request(5)
        self.assertNotIn("rules_mutation_suggested", status["blockers"])
        self.assertEqual(len(model.prompts), 1)
        payload = json.loads(model.prompts[0].split("\n\n", 1)[1])
        self.assertEqual([item["title"] for item in payload["rule_findings"]],
                         ["Native manual generator operational check failed", "Duplicants are idle"])


class CandidateTests(unittest.TestCase):
    def state(self, *, paused: bool, alerts: list | None = None) -> dict:
        record = enrich(fixtures.startup_snapshot(
            fixtures.startup_expectations(), SESSION, 2, 1, paused=paused,
            active_path_id="sha256:" + "4" * 64, save_id_hash="sha256:" + "5" * 64,
        ))
        record["alerts"] = alerts or []
        record["quality"]["alerts"] = contract_quality_fixture(ALERTS_CANONICAL_CONTRACT_ID)
        return record

    def ids(self, state: dict, allowed=("set_paused", "set_duplicant_chore_priority", "mark_dig_cells")) -> list:
        return [item["candidate_id"] for item in candidates.generate_candidates(state, allowed_actions=allowed)["items"]]

    def test_running_game_offers_only_pause(self):
        self.assertEqual(self.ids(self.state(paused=False)), ["pause_game", "wait"])

    def test_resume_requires_an_observed_empty_alert_set(self):
        self.assertNotIn("resume_game", self.ids(self.state(paused=True, alerts=["suffocation"])))

    def test_policy_filters_candidates(self):
        self.assertEqual(self.ids(self.state(paused=True), allowed=("set_paused",)), ["resume_game", "wait"])

    def test_research_candidates_skip_the_current_tech_in_both_pause_states(self):
        # The fixture researches FarmingTech, so the ladder offers the next two tier-1 techs.
        self.assertEqual(
            self.ids(self.state(paused=False), allowed=("set_paused", "set_research")),
            ["pause_game", "research_ImprovedOxygen", "research_PowerRegulation", "wait"],
        )
        self.assertEqual(
            self.ids(self.state(paused=True), allowed=("set_research",)),
            ["research_ImprovedOxygen", "research_PowerRegulation", "wait"],
        )

    def test_research_candidates_skip_finished_techs_reported_by_the_probe(self):
        # ImprovedOxygen and PowerRegulation are finished and FarmingTech is
        # current, so only the last ladder entry remains.
        state = self.state(paused=False)
        state["research"]["complete"] = ["ImprovedOxygen", "PowerRegulation"]
        state["quality"]["research.complete"] = contract_quality_fixture(RESEARCH_COMPLETE_CONTRACT_ID)
        self.assertEqual(self.ids(state, allowed=("set_research",)), ["research_BasicRefinement", "wait"])

    def test_research_candidates_ignore_a_finished_set_outside_its_contract(self):
        # An unsupported (older Probe) or malformed set falls back to offering
        # the ladder; the executor still refuses a finished tech itself.
        unsupported = self.state(paused=False)
        unsupported["research"]["complete"] = None
        unsupported["quality"]["research.complete"] = {"status": "unsupported", "source": "none"}
        unsorted = self.state(paused=False)
        unsorted["research"]["complete"] = ["PowerRegulation", "ImprovedOxygen"]
        unsorted["quality"]["research.complete"] = contract_quality_fixture(RESEARCH_COMPLETE_CONTRACT_ID)
        for state in (unsupported, unsorted):
            self.assertEqual(
                self.ids(state, allowed=("set_research",)),
                ["research_ImprovedOxygen", "research_PowerRegulation", "wait"],
            )

    def test_research_candidate_binds_the_observed_current_selection(self):
        items = candidates.generate_candidates(self.state(paused=False), allowed_actions=("set_research",))["items"]
        hint = next(item for item in items if item["candidate_id"] == "research_ImprovedOxygen")["hint"]
        self.assertEqual(hint["args"], {"tech": "ImprovedOxygen"})
        self.assertEqual([(c["path"], c["value"]) for c in hint["preconditions"]], [("research.current", "FarmingTech")])
        self.assertEqual([(c["path"], c["value"]) for c in hint["expected_effects"]], [("research.current", "ImprovedOxygen")])

    def test_building_candidates_prefer_oxygen_buildings_and_exist_only_while_paused(self):
        running = self.ids(self.state(paused=False), allowed=ALL_CANDIDATE_ACTIONS)
        self.assertFalse({"ladder_9000", "oxygen_diffuser_9100", "algae_terrarium_9200"}.intersection(running))
        items = candidates.generate_candidates(self.state(paused=True), allowed_actions=("place_building_blueprint",))["items"]
        [diffuser, terrarium, ladder, wait] = items
        self.assertEqual(
            [(item["candidate_id"], item["risk"]) for item in (diffuser, terrarium, ladder, wait)],
            [("oxygen_diffuser_9100", "medium"), ("algae_terrarium_9200", "medium"), ("ladder_9000", "medium"), ("wait", "low")],
        )
        self.assertEqual(diffuser["hint"]["args"]["building_def_id"], "MineralDeoxidizer")
        self.assertEqual(diffuser["hint"]["args"]["cells"], [9100])
        self.assertEqual(diffuser["hint"]["args"]["material_tags"], ["Cuprite"])
        self.assertIn("Oxygen Diffuser", diffuser["summary"])
        self.assertIn("x=50, y=12", diffuser["summary"])
        # Each contract binds the placement field of its own building.
        bound = {c["path"].rsplit(".", 1)[-1] for c in diffuser["hint"]["preconditions"]}
        self.assertIn("can_place_mineral_deoxidizer", bound)
        self.assertNotIn("can_place_ladder", bound)
        self.assertEqual(ladder["hint"]["args"]["cells"], [9000])
        self.assertEqual(ladder["hint"]["args"]["material_tags"], ["WoodLog"])
        self.assertIn("x=41, y=12", ladder["summary"])

    def test_a_locked_or_unsupplied_building_yields_no_candidate(self):
        # The Probe lists no candidate cells for a building whose technology is
        # not researched (or whose materials are missing); nothing is offered.
        state = self.state(paused=True)
        state["blueprints"]["catalog"]["AlgaeHabitat"]["candidate_cells"] = []
        ids = self.ids(state, allowed=("place_building_blueprint",))
        self.assertEqual(ids, ["oxygen_diffuser_9100", "ladder_9000", "wait"])

    def test_full_paused_set_keeps_resume_and_wait_within_the_cap(self):
        ids = self.ids(self.state(paused=True), allowed=ALL_CANDIDATE_ACTIONS)
        self.assertEqual(ids, [
            "research_ImprovedOxygen", "research_PowerRegulation", "priority_102_Dig_2", "priority_101_Build_3",
            "dig_5000_3", "oxygen_diffuser_9100", "algae_terrarium_9200", "ladder_9000", "resume_game", "wait",
        ])
        self.assertLessEqual(len(ids), candidates.MAX_CANDIDATES)

    def test_set_is_stable_across_snapshots_of_the_same_state(self):
        first = candidates.generate_candidates(self.state(paused=True), allowed_actions=["mark_dig_cells"])
        later = self.state(paused=True)
        later["seq"], later["snapshot_id"], later["ts"] = 99, f"{SESSION}:99", fixtures.iso_now(5)
        second = candidates.generate_candidates(later, allowed_actions=["mark_dig_cells"])
        self.assertEqual(first["candidate_set_hash"], second["candidate_set_hash"])


if __name__ == "__main__":
    unittest.main()
