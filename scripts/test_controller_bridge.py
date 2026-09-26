#!/usr/bin/env python3
"""Real plan/queue/confirmation integration in isolated temporary directories."""
from __future__ import annotations

import copy
import contextlib
import dataclasses
import datetime as dt
import io
import json
import os
import pathlib
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent import controller_bridge, command_custody
from agent.advanced_actions import build_duplicant_priority_hint
from agent.colony_plan import (PLAN_DEFINITION_SCHEMA, ColonyPlanError,
    advance_colony_plan, create_colony_plan, load_active_plan, plan_leaf_contract_hash)
from agent.command_queue import ensure_command_dirs, write_json_atomic
from agent.controller_bridge import (ColonyPlanControllerAdapter, IDENTITY_FIELDS,
    create_colony_plan_controller)
from agent.plan_coordinator import PlanCoordinatorError, PlanSequenceCoordinator
from agent.controller_loop import (ControllerCore, DurableIntentStore, EffectStatus,
    ObservationQuality, ReconciliationStatus, decide_bounded_action, normalize_observation)
from agent.llm_planner import colony_plan_definition_from_plan
from agent.state_quality import canonical_state_hash
from scripts.test_controller_loop import snapshot
from scripts.verify_colony_plan import complete_pending, only_command, write_live_state
from scripts import run_plan_coordinator


class BridgeIntegrationTests(unittest.TestCase):
    annotate_conditions = False
    plan_targets = (4, 3)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="oni-plan-controller-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name) / "bridge"
        self.root.mkdir()
        ensure_command_dirs(self.root)
        write_live_state(self.root, snapshot())
        self.initial = self.latest()
        working = copy.deepcopy(self.initial)
        steps = []
        for index, target in enumerate(self.plan_targets, start=1):
            if steps and target == working["duplicants"]["by_instance_id"]["id_123"]["chore_priorities"]["Build"]:
                hint = {key: copy.deepcopy(steps[-1][key]) for key in controller_bridge.SHAPE_FIELDS}
            else:
                hint = build_duplicant_priority_hint(working, instance_id=123, chore_group="Build", priority=target)
            if self.annotate_conditions:
                for condition in hint["preconditions"]:
                    condition["note"] = "Human review annotation retained by Colony Plan"
            steps.append({"id": f"priority_{index}", "dependencies": [] if index == 1 else ["priority_1"],
                          **{key: hint[key] for key in controller_bridge.SHAPE_FIELDS}})
            working["duplicants"]["by_instance_id"]["id_123"]["chore_priorities"]["Build"] = target
        self.plan = create_colony_plan(self.root, {"schema": PLAN_DEFINITION_SCHEMA,
            "title": "Controller integration", "goal": "Confirm exactly one selected leaf", "steps": steps})
        self.identity = {key: self.initial[key] for key in IDENTITY_FIELDS}
        self.adapter = self.make_adapter()
        intent_dir = pathlib.Path(self.temp.name) / "intents"
        intent_dir.mkdir()
        self.store = DurableIntentStore(intent_dir)
        self.core = ControllerCore(self.adapter, self.adapter.policy(), intent_store=self.store)

    def make_adapter(self, **overrides):
        args = {"plan_id": self.plan["plan_id"], "step_id": "priority_1",
                "expected_identity": self.identity, "allow_pending_commands": True}
        args.update(overrides)
        return ColonyPlanControllerAdapter(self.root, **args)

    def latest(self):
        return json.loads((self.root / "latest_state.json").read_text(encoding="utf-8-sig"))

    def proposal(self):
        result = decide_bounded_action(normalize_observation(self.adapter.observe()), self.adapter.policy())
        self.assertIsNotNone(result.proposal, result.as_dict())
        return result.proposal

    def publish(self):
        result = self.core.step()
        self.assertIsNotNone(result.reconciliation, result.as_dict())
        self.assertEqual(result.effect.status, EffectStatus.PENDING, self.adapter.last_blocker)
        path, command = only_command(self.root, "pending")
        self.assertIs(command["requires_approval"], True)
        self.assertEqual(command["mode"], "planned")
        self.assertFalse(list((self.root / "commands/approved").glob("*.json")))
        return result.decision.proposal, path, command

    def confirm(self):
        after = copy.deepcopy(self.initial)
        after["duplicants"]["by_instance_id"]["id_123"]["chore_priorities"]["Build"] = 4
        complete_pending(self.root, after)
        # Mirror Bridge's durable session publication: the older plan fixture
        # only exercises the root certificate and commit, not runtime acceptance.
        session = self.root / "sessions" / self.initial["session_id"]
        (session / "action-outcomes.json").write_bytes((self.root / "action-outcomes.json").read_bytes())
        with (session / "state.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(self.latest(), ensure_ascii=False) + "\n")

    def tree(self):
        return {str(path.relative_to(self.root)): path.read_bytes()
                for path in self.root.rglob("*") if path.is_file()}

    def test_publish_then_real_confirmation_does_not_advance_successor(self):
        proposal, _, _ = self.publish()
        self.confirm()
        before = self.tree()
        before_revision = load_active_plan(self.root)["revision"]
        result = self.core.step()
        self.assertTrue(result.reconciliation.confirmed, (result.as_dict(), self.adapter.last_blocker))
        after = self.tree()
        self.assertNotEqual(before, after)
        changed = {key.replace("\\", "/") for key in set(before) | set(after) if before.get(key) != after.get(key)}
        self.assertTrue(changed <= {"colony-plans/active.json", "colony-plan.json",
            f"colony-plans/{self.plan['plan_id']}/current.json",
            f"colony-plans/{self.plan['plan_id']}/revisions/{before_revision + 1:06d}.json"}, changed)
        self.assertEqual(load_active_plan(self.root)["steps"][0]["status"], "completed")
        self.assertEqual(load_active_plan(self.root)["steps"][1]["status"], "waiting")
        self.assertFalse(list((self.root / "commands/pending").glob("*.json")))
        self.assertEqual(self.adapter.reconcile(proposal).status, EffectStatus.APPLIED)

    def test_forged_confirmed_effect_cannot_write_leaf_checkpoint(self):
        proposal, _, _ = self.publish()
        self.confirm()
        before = self.tree()
        forged = copy.deepcopy(self.latest())
        forged["snapshot_id"] = "forged-witness"
        effect = controller_bridge.EffectObservation(
            proposal.idempotency_key, EffectStatus.APPLIED,
            controller_bridge.Freshness.FRESH, controller_bridge.ObservationQuality.SAFE,
            forged, source="bridge_command_confirmation")
        result = self.adapter.commit_confirmed_effect(proposal, effect)
        self.assertEqual(result.status, EffectStatus.UNKNOWN)
        self.assertEqual(before, self.tree())
        self.assertEqual(load_active_plan(self.root)["steps"][0]["status"], "awaiting_approval")

    def test_matching_raw_state_without_command_confirmation_stays_pending(self):
        self.publish()
        after = copy.deepcopy(self.initial)
        after["duplicants"]["by_instance_id"]["id_123"]["chore_priorities"]["Build"] = 4
        after.update(seq=11, snapshot_seq=6, snapshot_id="offline-m28:11")
        write_live_state(self.root, after)
        result = self.core.step()
        self.assertFalse(result.reconciliation.confirmed)
        self.assertEqual(result.effect.status, EffectStatus.PENDING)
        self.assertEqual(len(list((self.root / "commands/pending").glob("*.json"))), 1)

    def test_wait_budget_stops_sends_but_readonly_reconcile_remains_available(self):
        proposal, _, _ = self.publish()
        self.core.step()
        result = self.core.step()
        self.assertIn("limit_exhausted", result.reconciliation.recovery_reason)
        self.assertEqual(self.core.step().decision.reason, "intent_blocked")
        self.confirm()
        self.assertEqual(self.adapter.reconcile(proposal).status, EffectStatus.APPLIED)

    def test_disabled_publication_preserves_tree(self):
        adapter = self.make_adapter(allow_pending_commands=False)
        before = self.tree()
        self.assertEqual(adapter.apply(self.proposal()).status, EffectStatus.REJECTED)
        self.assertEqual(before, self.tree())

    def test_custody_held_final_guard_rejects_before_plan_publication(self):
        calls = []
        guarded = self.make_adapter(final_apply_guard=lambda proposal: calls.append(proposal.idempotency_key) or False)
        proposal = decide_bounded_action(normalize_observation(guarded.observe()), guarded.policy()).proposal
        self.assertIsNotNone(proposal)
        self.assertEqual(guarded.apply(proposal).status, EffectStatus.UNKNOWN)
        self.assertEqual(len(calls), 1)
        self.assertFalse(list((self.root / "commands/pending").glob("*.json")))

    def test_custody_held_final_guard_requires_exact_true(self):
        cases = {
            "none": None,
            "zero": 0,
            "empty_string": "",
            "exception": RuntimeError("guard unreadable"),
        }
        for label, outcome in cases.items():
            with self.subTest(label=label):
                calls = []

                def guard(proposal, outcome=outcome):
                    calls.append(proposal.idempotency_key)
                    if isinstance(outcome, BaseException):
                        raise outcome
                    return outcome

                guarded = self.make_adapter(final_apply_guard=guard)
                proposal = decide_bounded_action(
                    normalize_observation(guarded.observe()), guarded.policy()
                ).proposal
                self.assertIsNotNone(proposal)
                before = self.tree()
                self.assertEqual(guarded.apply(proposal).status, EffectStatus.UNKNOWN)
                self.assertEqual(len(calls), 1)
                self.assertEqual(before, self.tree())
                self.assertFalse(list((self.root / "commands/pending").glob("*.json")))

    def test_sequence_propagates_final_guard_before_any_leaf_publication(self):
        from agent.controller_bridge import ColonyPlanControllerSequence
        calls = []
        sequence = ColonyPlanControllerSequence(
            self.root,
            plan_id=self.plan["plan_id"],
            step_ids=["priority_1", "priority_2"],
            expected_identity=self.identity,
            intent_directory=self.store.directory,
            allow_pending_commands=True,
            final_apply_guard=lambda proposal: calls.append(proposal.idempotency_key) or False,
        )
        before = self.tree()
        cycle = sequence.step()
        self.assertEqual(cycle.effect.status, EffectStatus.REJECTED)
        self.assertGreaterEqual(len(calls), 1)
        self.assertFalse(list((self.root / "commands/pending").glob("*.json")))
        self.assertEqual(before, self.tree())
        self.assertEqual(sequence.step_index, 0)

    def test_duplicate_queue_location_cannot_confirm(self):
        proposal, path, command = self.publish()
        write_json_atomic(self.root / "commands/approved" / path.name, command)
        self.assertEqual(self.adapter.reconcile(proposal).status, EffectStatus.UNKNOWN)

    def test_legacy_done_status_cannot_confirm(self):
        proposal, path, command = self.publish()
        command["status"] = "verified"
        write_json_atomic(self.root / "commands/done" / path.name, command)
        path.unlink()
        self.assertEqual(self.adapter.reconcile(proposal).status, EffectStatus.UNKNOWN)

    def test_missing_certificate_cannot_confirm(self):
        proposal, _, _ = self.publish()
        self.confirm()
        for path in self.root.rglob("action-outcomes.json"):
            path.unlink()
        self.assertEqual(self.adapter.reconcile(proposal).status, EffectStatus.UNKNOWN)

    def test_claim_and_prepared_commit_stages_are_pending(self):
        proposal, path, command = self.publish()
        for stage, status in (("approving", "pending_human_approval"), ("approving", "approved"),
                ("approved", "executing"), ("executing", "executing"),
                ("confirming", "executed_pending_snapshot"), ("confirming", "confirmed")):
            with self.subTest(stage=stage, status=status):
                target = self.root / "commands" / stage / path.name
                command["status"] = status
                path.unlink()
                write_json_atomic(target, command)
                self.assertEqual(self.adapter.reconcile(proposal).status, EffectStatus.PENDING)
                path = target

    def test_untrusted_or_changed_identity_observation_is_unusable(self):
        for change in ({"game_build": "different"}, {"world_id": True}, {"writer": {"fault_count": 1}}):
            with self.subTest(change=change):
                state = copy.deepcopy(self.initial)
                state.update(change)
                write_live_state(self.root, state)
                self.assertEqual(self.adapter.observe().quality, ObservationQuality.UNSAFE)

    def test_guard_refuses_wrong_state_leaf_and_leaf_content(self):
        args = {"expected_step_id": "priority_1", "expected_state_hash": canonical_state_hash(self.initial),
                "expected_step_hash": plan_leaf_contract_hash(self.plan["steps"][0])}
        for override in ({"expected_step_id": "priority_2"}, {"expected_state_hash": "sha256:" + "b" * 64},
                         {"expected_step_hash": "sha256:" + "b" * 64}):
            with self.subTest(override=override):
                before = self.tree()
                with self.assertRaises(ColonyPlanError):
                    advance_colony_plan(self.root, self.plan["plan_id"], **{**args, **override})
                self.assertEqual(before, self.tree())

    def test_guard_cannot_reconcile_open_command(self):
        self.publish()
        before = self.tree()
        with self.assertRaises(ColonyPlanError):
            advance_colony_plan(self.root, self.plan["plan_id"], expected_step_id="priority_1",
                expected_state_hash=canonical_state_hash(self.initial),
                expected_step_hash=plan_leaf_contract_hash(self.plan["steps"][0]))
        self.assertEqual(before, self.tree())

    def test_source_drift_consumes_send_without_publication(self):
        proposal = self.proposal()
        state = copy.deepcopy(self.initial)
        state["resources"]["food_kcal"] += 1
        write_live_state(self.root, state)
        self.assertEqual(self.adapter.apply(proposal).status, EffectStatus.UNKNOWN)
        self.assertEqual(self.adapter.apply(proposal).status, EffectStatus.REJECTED)
        self.assertFalse(list((self.root / "commands/pending").glob("*.json")))

    def test_hardlinked_command_cannot_confirm(self):
        proposal, path, _ = self.publish()
        os.link(path, pathlib.Path(self.temp.name) / "command-alias.json")
        self.assertEqual(self.adapter.reconcile(proposal).status, EffectStatus.UNKNOWN)

    def test_certificate_change_during_assessment_cannot_confirm(self):
        proposal, _, _ = self.publish()
        self.confirm()
        real = controller_bridge.assess_command_confirmation
        def changed(*args, **kwargs):
            result = real(*args, **kwargs)
            path = self.root / "action-outcomes.json"
            path.write_bytes(path.read_bytes() + b" ")
            return result
        with mock.patch.object(controller_bridge, "assess_command_confirmation", changed):
            self.assertEqual(self.adapter.reconcile(proposal).status, EffectStatus.UNKNOWN)

    def test_factory_requires_existing_durable_store_and_survives_restart_without_resend(self):
        kwargs = {"plan_id": self.plan["plan_id"], "step_id": "priority_1", "expected_identity": self.identity,
                  "allow_pending_commands": True}
        with self.assertRaises(FileNotFoundError):
            create_colony_plan_controller(self.root, intent_directory=pathlib.Path(self.temp.name) / "absent", **kwargs)
        first = create_colony_plan_controller(self.root, intent_directory=self.store.directory, **kwargs)
        self.assertEqual(first.step().effect.status, EffectStatus.PENDING)
        before = self.tree()
        restarted = create_colony_plan_controller(self.root, intent_directory=self.store.directory, **kwargs)
        self.assertEqual(restarted.step().decision.reason, "intent_already_consumed")
        self.assertEqual(before, self.tree())

    def test_core_enforces_bridge_durable_store(self):
        core = ControllerCore(self.adapter, self.adapter.policy())
        before = self.tree()
        self.assertEqual(core.step().decision.reason, "durable_intent_store_required")
        self.assertEqual(before, self.tree())

    def test_replaced_root_is_rejected_before_custody_creates_layout(self):
        proposal = self.proposal()
        old = self.root.with_name("original-bridge")
        self.root.rename(old)
        self.root.mkdir()
        self.assertEqual(self.adapter.apply(proposal).status, EffectStatus.UNKNOWN)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_custody_expected_identity_rejects_replacement_without_writes(self):
        unrelated = pathlib.Path(self.temp.name) / "new-bridge"
        unrelated.mkdir()
        with self.assertRaises(command_custody.CommandCustodyConflict):
            with command_custody.lifecycle_custody(unrelated, expected_bridge_identity=self.adapter._root_identity):
                self.fail("replacement custody admitted")
        self.assertEqual(list(unrelated.iterdir()), [])

    def test_failed_unknown_and_rejected_results_do_not_confirm(self):
        proposal, path, command = self.publish()
        command.update(status="failed", classification="outcome_unknown")
        target = self.root / "commands/failed" / path.name
        path.unlink()
        write_json_atomic(target, command)
        self.assertEqual(self.adapter.reconcile(proposal).status, EffectStatus.UNKNOWN)
        command.update(status="rejected", classification="policy_rejected")
        target.unlink()
        write_json_atomic(self.root / "commands/rejected" / path.name, command)
        self.assertEqual(self.adapter.reconcile(proposal).status, EffectStatus.REJECTED)

    def test_hardlinked_archive_cannot_confirm(self):
        proposal, _, _ = self.publish()
        self.confirm()
        archive = self.root / "sessions" / self.initial["session_id"] / "state.jsonl"
        os.link(archive, pathlib.Path(self.temp.name) / "archive-alias.jsonl")
        self.assertEqual(self.adapter.reconcile(proposal).status, EffectStatus.UNKNOWN)

    def test_new_conflicting_certificate_during_assessment_cannot_confirm(self):
        proposal, _, _ = self.publish()
        self.confirm()
        real = controller_bridge.assess_command_confirmation
        def added(*args, **kwargs):
            result = real(*args, **kwargs)
            extra = self.root / "sessions/added/action-outcomes.json"
            extra.parent.mkdir()
            extra.write_bytes((self.root / "action-outcomes.json").read_bytes())
            return result
        with mock.patch.object(controller_bridge, "assess_command_confirmation", added):
            self.assertEqual(self.adapter.reconcile(proposal).status, EffectStatus.UNKNOWN)

    def test_observation_generation_mismatch_is_unusable_without_writes(self):
        state = self.latest()
        state["seq"] += 1
        write_json_atomic(self.root / "latest_state.json", state)
        before = self.tree()
        self.assertEqual(self.adapter.observe().quality, ObservationQuality.UNSAFE)
        self.assertEqual(before, self.tree())

    def test_already_satisfied_leaf_is_completed_without_command_or_confirmation(self):
        fixture = BridgeIntegrationTests()
        fixture.plan_targets = (4,)
        fixture.setUp()
        try:
            satisfied = copy.deepcopy(fixture.initial)
            satisfied["duplicants"]["by_instance_id"]["id_123"]["chore_priorities"]["Build"] = 4
            write_live_state(fixture.root, satisfied)
            before = fixture.tree()
            cycle = fixture.core.step()
            self.assertIn(cycle.decision.reason, {"expected_effects_already_satisfied", "target_already_at_priority"}, fixture.adapter.last_blocker)
            self.assertIsNone(cycle.reconciliation)
            self.assertFalse(list((fixture.root / "commands/pending").glob("*.json")))
            plan = load_active_plan(fixture.root)
            self.assertEqual(plan["status"], "completed")
            self.assertEqual(plan["steps"][0]["completion_mode"], "already_satisfied_without_command")
            self.assertNotEqual(before, fixture.tree())
        finally:
            fixture.doCleanups()

    def test_sequence_commits_first_leaf_then_marks_second_satisfied_without_publishing(self):
        fixture = BridgeIntegrationTests()
        fixture.plan_targets = (4, 4)
        fixture.setUp()
        try:
            from agent.controller_bridge import ColonyPlanControllerSequence
            sequence = ColonyPlanControllerSequence(fixture.root, plan_id=fixture.plan["plan_id"],
                step_ids=["priority_1", "priority_2"], expected_identity=fixture.identity,
                intent_directory=fixture.store.directory, allow_pending_commands=True, max_calls=6)
            first = sequence.step()
            self.assertEqual(first.effect.status, EffectStatus.PENDING)
            after = copy.deepcopy(fixture.initial)
            after["duplicants"]["by_instance_id"]["id_123"]["chore_priorities"]["Build"] = 4
            fixture.confirm()
            second = sequence.step()
            self.assertTrue(second.reconciliation.confirmed, second.as_dict())
            self.assertEqual(sequence.step_index, 1)
            self.assertFalse(list((fixture.root / "commands/pending").glob("*.json")))
            third = sequence.step()
            self.assertIn(third.decision.reason, {"target_already_at_priority", "expected_effects_already_satisfied"})
            self.assertTrue(sequence.completed)
            plan = load_active_plan(fixture.root)
            self.assertEqual([step["status"] for step in plan["steps"]], ["completed", "completed"])
            self.assertEqual(plan["steps"][1]["completion_mode"], "already_satisfied_without_command")
        finally:
            fixture.doCleanups()

    def test_sequence_restart_resumes_from_persisted_first_uncompleted_leaf(self):
        fixture = BridgeIntegrationTests()
        fixture.plan_targets = (4, 4)
        fixture.setUp()
        try:
            from agent.controller_bridge import ColonyPlanControllerSequence
            kwargs = dict(bridge_dir=fixture.root, plan_id=fixture.plan["plan_id"],
                step_ids=["priority_1", "priority_2"], expected_identity=fixture.identity,
                intent_directory=fixture.store.directory, allow_pending_commands=True, max_calls=6)
            sequence = ColonyPlanControllerSequence(**kwargs)
            self.assertEqual(sequence.step_index, 0)
            self.assertEqual(sequence.step().effect.status, EffectStatus.PENDING)
            fixture.confirm()
            self.assertTrue(sequence.step().reconciliation.confirmed)
            self.assertEqual(sequence.step_index, 1)
            before = fixture.tree()

            # A new coordinator must load the durable plan revision and resume
            # at leaf 2. It must not re-submit leaf 1 or mutate the tree while
            # merely being constructed.
            resumed = ColonyPlanControllerSequence(**kwargs)
            self.assertEqual(resumed.step_index, 1)
            self.assertFalse(resumed.completed)
            self.assertEqual(before, fixture.tree())
            self.assertIn(resumed.step().decision.reason,
                {"expected_effects_already_satisfied", "target_already_at_priority"})
            self.assertTrue(resumed.completed)
        finally:
            fixture.doCleanups()

    def test_explicit_core_resume_reads_existing_pending_leaf_without_resend(self):
        fixture = BridgeIntegrationTests()
        fixture.plan_targets = (4, 4)
        fixture.setUp()
        try:
            from agent.controller_bridge import create_colony_plan_controller
            kwargs = dict(plan_id=fixture.plan["plan_id"], step_id="priority_1",
                expected_identity=fixture.identity, allow_pending_commands=True)
            first = create_colony_plan_controller(fixture.root,
                intent_directory=fixture.store.directory, **kwargs)
            self.assertEqual(first.step().effect.status, EffectStatus.PENDING)
            before = fixture.tree()
            resumed = create_colony_plan_controller(fixture.root,
                intent_directory=fixture.store.directory, **kwargs)
            cycle = resumed.resume_consumed_intent()
            self.assertEqual(cycle.effect.status, EffectStatus.PENDING)
            self.assertEqual(cycle.reconciliation.status, ReconciliationStatus.RETRYABLE)
            self.assertEqual(before, fixture.tree())
            self.assertEqual(len(list((fixture.root / "commands/pending").glob("*.json"))), 1)
        finally:
            fixture.doCleanups()

    def test_plan_coordinator_read_only_then_restart_pending_without_resend(self):
        fixture = BridgeIntegrationTests()
        fixture.plan_targets = (4, 4)
        fixture.setUp()
        try:
            kwargs = dict(bridge_dir=fixture.root, plan_id=fixture.plan["plan_id"],
                step_ids=["priority_1", "priority_2"], expected_identity=fixture.identity,
                intent_directory=fixture.store.directory,
                clone_preflight={"schema": "oni.clone-launch-preflight.v1",
                    "launch_allowed": True, "clone_id": "fixture",
                    "bridge_dir": str(fixture.root),
                    "process_counts": {"OxygenNotIncluded": 0, "oni_bridge": 0, "steam": 0},
                    "queue_counts": {},
                    "manifest_sha256": "sha256:" + "a" * 64,
                    "clone_save_sha256": fixture.identity["save_id_hash"],
                    "clone_preview_sha256": "sha256:" + "b" * 64,
                    "clone_save_path_id": "sha256:" + "c" * 64,
                    "clone_preview_path_id": "sha256:" + "d" * 64},
                process_probe=lambda: {"OxygenNotIncluded": 0, "oni_bridge": 0, "steam": 0})
            kwargs["clone_probe"] = lambda: kwargs["clone_preflight"]
            observer = PlanSequenceCoordinator(**kwargs)
            candidate = observer.tick()
            self.assertEqual(candidate["status"], "candidate")
            self.assertFalse(candidate["side_effects"]["command_published"])
            self.assertFalse(list((fixture.root / "commands/pending").glob("*.json")))

            publisher = PlanSequenceCoordinator(**kwargs, publish_one=True)
            published = publisher.tick()
            self.assertEqual(published["status"], "awaiting_external_confirmation")
            pending = list((fixture.root / "commands/pending").glob("*.json"))
            self.assertEqual(len(pending), 1)
            before = fixture.tree()
            resumed = PlanSequenceCoordinator(**kwargs)
            waiting = resumed.tick()
            self.assertEqual(waiting["status"], "awaiting_external_confirmation")
            self.assertEqual(waiting["effect_status"], "pending")
            self.assertFalse(waiting["side_effects"]["command_published"])
            self.assertEqual(before, fixture.tree())
            self.assertEqual(len(list((fixture.root / "commands/pending").glob("*.json"))), 1)
        finally:
            fixture.doCleanups()

    def test_plan_coordinator_cli_defaults_to_read_only(self):
        fixture = BridgeIntegrationTests()
        fixture.plan_targets = (4, 4)
        fixture.setUp()
        try:
            root = pathlib.Path(fixture.temp.name)
            identity_path = root / "identity.json"
            preflight_path = root / "preflight.json"
            process_path = root / "processes.json"
            identity_path.write_text(json.dumps(fixture.identity), encoding="utf-8")
            preflight_path.write_text(json.dumps({
                "schema": "oni.clone-launch-preflight.v1", "launch_allowed": True,
                "clone_id": "fixture",
                "bridge_dir": str(fixture.root),
                "process_counts": {"OxygenNotIncluded": 0, "oni_bridge": 0, "steam": 0},
                "queue_counts": {
                    "commands/pending": 0, "commands/approving": 0,
                    "commands/approved": 0, "commands/executing": 0,
                    "commands/confirming": 0,
                    "validation-controls/approved": 0,
                    "validation-controls/inflight": 0,
                },
                "manifest_sha256": "sha256:" + "a" * 64,
                "clone_save_sha256": fixture.identity["save_id_hash"],
                "clone_preview_sha256": "sha256:" + "b" * 64,
                "clone_save_path_id": "sha256:" + "c" * 64,
                "clone_preview_path_id": "sha256:" + "d" * 64,
            }), encoding="utf-8")
            process_path.write_text(json.dumps({"OxygenNotIncluded": 0, "oni_bridge": 0, "steam": 0}), encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = run_plan_coordinator.main([
                    "--bridge-dir", str(fixture.root), "--plan-id", fixture.plan["plan_id"],
                    "--step-ids", "priority_1", "priority_2",
                    "--expected-identity-json", str(identity_path), "--intent-dir", str(fixture.store.directory),
                "--clone-preflight-json", str(preflight_path), "--process-counts-json", str(process_path),
                    "--test-root", str(root),
                    "--json",
                ])
            payload = json.loads(output.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "candidate")
            self.assertFalse(list((fixture.root / "commands/pending").glob("*.json")))
        finally:
            fixture.doCleanups()

    def test_plan_coordinator_fixture_rejects_publish(self):
        fixture = BridgeIntegrationTests()
        fixture.plan_targets = (4, 4)
        fixture.setUp()
        try:
            root = pathlib.Path(fixture.temp.name)
            identity_path = root / "identity.json"
            preflight_path = root / "preflight.json"
            process_path = root / "processes.json"
            identity_path.write_text(json.dumps(fixture.identity), encoding="utf-8")
            preflight_path.write_text(json.dumps({
                "schema": "oni.clone-launch-preflight.v1", "launch_allowed": True,
                "bridge_dir": str(fixture.root),
                "process_counts": {name: 0 for name in ("OxygenNotIncluded", "oni_bridge", "steam")},
                "queue_counts": {name: 0 for name in (
                    "commands/pending", "commands/approving", "commands/approved",
                    "commands/executing", "commands/confirming",
                    "validation-controls/approved", "validation-controls/inflight",
                )},
                "manifest_sha256": "sha256:" + "a" * 64,
                "clone_save_sha256": fixture.identity["save_id_hash"],
                "clone_preview_sha256": "sha256:" + "b" * 64,
                "clone_save_path_id": "sha256:" + "c" * 64,
                "clone_preview_path_id": "sha256:" + "d" * 64,
            }), encoding="utf-8")
            process_path.write_text(json.dumps({name: 0 for name in (
                "OxygenNotIncluded", "oni_bridge", "steam",
            )}), encoding="utf-8")
            before = fixture.tree()
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = run_plan_coordinator.main([
                    "--bridge-dir", str(fixture.root), "--plan-id", fixture.plan["plan_id"],
                    "--step-ids", "priority_1", "priority_2",
                    "--expected-identity-json", str(identity_path),
                    "--intent-dir", str(fixture.store.directory),
                    "--clone-preflight-json", str(preflight_path),
                    "--process-counts-json", str(process_path),
                    "--test-root", str(root), "--publish-one", "--json",
                ])
            payload = json.loads(output.getvalue())
            self.assertEqual(code, 2)
            self.assertEqual(payload["status"], "blocked")
            self.assertEqual(before, fixture.tree())
            self.assertFalse(list((fixture.root / "commands/pending").glob("*.json")))
        finally:
            fixture.doCleanups()

    def test_plan_coordinator_fixture_rejects_snapshot_escape(self):
        fixture = BridgeIntegrationTests()
        fixture.plan_targets = (4, 4)
        fixture.setUp()
        try:
            root = pathlib.Path(fixture.temp.name)
            identity_path = root / "identity.json"
            process_path = root / "processes.json"
            outside = pathlib.Path(tempfile.mkdtemp(prefix="oni-coordinator-outside-"))
            self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
            preflight_path = outside / "preflight.json"
            identity_path.write_text(json.dumps(fixture.identity), encoding="utf-8")
            preflight_path.write_text("{}", encoding="utf-8")
            process_path.write_text(json.dumps({name: 0 for name in (
                "OxygenNotIncluded", "oni_bridge", "steam",
            )}), encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = run_plan_coordinator.main([
                    "--bridge-dir", str(fixture.root), "--plan-id", fixture.plan["plan_id"],
                    "--step-ids", "priority_1", "priority_2",
                    "--expected-identity-json", str(identity_path),
                    "--intent-dir", str(fixture.store.directory),
                    "--clone-preflight-json", str(preflight_path),
                    "--process-counts-json", str(process_path),
                    "--test-root", str(root), "--json",
                ])
            payload = json.loads(output.getvalue())
            self.assertEqual(code, 2)
            self.assertEqual(payload["status"], "blocked")
            self.assertFalse(list((fixture.root / "commands/pending").glob("*.json")))
        finally:
            fixture.doCleanups()

    def test_plan_coordinator_fixture_rejects_runtime_path_escape(self):
        fixture = BridgeIntegrationTests()
        fixture.plan_targets = (4, 4)
        fixture.setUp()
        try:
            root = pathlib.Path(fixture.temp.name)
            identity_path = root / "identity.json"
            preflight_path = root / "preflight.json"
            process_path = root / "processes.json"
            identity_path.write_text(json.dumps(fixture.identity), encoding="utf-8")
            preflight_path.write_text("{}", encoding="utf-8")
            process_path.write_text("{}", encoding="utf-8")
            outside = pathlib.Path(tempfile.mkdtemp(prefix="oni-coordinator-runtime-outside-"))
            self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
            outside_bridge = outside / "bridge"
            outside_intent = outside / "intents"
            outside_bridge.mkdir()
            outside_intent.mkdir()
            base = [
                "--bridge-dir", str(fixture.root), "--plan-id", fixture.plan["plan_id"],
                "--step-ids", "priority_1", "priority_2",
                "--expected-identity-json", str(identity_path),
                "--intent-dir", str(fixture.store.directory),
                "--clone-preflight-json", str(preflight_path),
                "--process-counts-json", str(process_path),
                "--test-root", str(root), "--json",
            ]
            cases = {
                "bridge": ("--bridge-dir", str(outside_bridge)),
                "intent": ("--intent-dir", str(outside_intent)),
                "state": ("--state-path", str(outside / "state.json")),
            }
            for label, (flag, value) in cases.items():
                with self.subTest(path=label):
                    output = io.StringIO()
                    argv = list(base)
                    if flag in {"--bridge-dir", "--intent-dir"}:
                        index = argv.index(flag)
                        argv[index + 1] = value
                    else:
                        argv.extend([flag, value])
                    with contextlib.redirect_stdout(output):
                        code = run_plan_coordinator.main(argv)
                    payload = json.loads(output.getvalue())
                    self.assertEqual(code, 2)
                    self.assertEqual(payload["status"], "blocked")
        finally:
            fixture.doCleanups()

    def test_plan_coordinator_fixture_rejects_forged_snapshot_boundary(self):
        fixture = BridgeIntegrationTests()
        fixture.plan_targets = (4, 4)
        fixture.setUp()
        try:
            root = pathlib.Path(fixture.temp.name)
            identity_path = root / "identity.json"
            preflight_path = root / "preflight.json"
            process_path = root / "processes.json"
            identity_path.write_text(json.dumps(fixture.identity), encoding="utf-8")
            preflight_path.write_text(json.dumps({
                "schema": "oni.clone-launch-preflight.v1", "launch_allowed": True,
                "bridge_dir": str(root / "forged-bridge"),
                "process_counts": {name: 0 for name in ("OxygenNotIncluded", "oni_bridge", "steam")},
                "queue_counts": {name: 0 for name in (
                    "commands/pending", "commands/approving", "commands/approved",
                    "commands/executing", "commands/confirming",
                    "validation-controls/approved", "validation-controls/inflight",
                )},
                "manifest_sha256": "sha256:" + "a" * 64,
                "clone_save_sha256": fixture.identity["save_id_hash"],
                "clone_preview_sha256": "sha256:" + "b" * 64,
                "clone_save_path_id": "sha256:" + "c" * 64,
                "clone_preview_path_id": "sha256:" + "d" * 64,
            }), encoding="utf-8")
            process_path.write_text(json.dumps({name: 0 for name in (
                "OxygenNotIncluded", "oni_bridge", "steam",
            )}), encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = run_plan_coordinator.main([
                    "--bridge-dir", str(fixture.root), "--plan-id", fixture.plan["plan_id"],
                    "--step-ids", "priority_1", "priority_2",
                    "--expected-identity-json", str(identity_path),
                    "--intent-dir", str(fixture.store.directory),
                    "--clone-preflight-json", str(preflight_path),
                    "--process-counts-json", str(process_path),
                    "--test-root", str(root), "--json",
                ])
            payload = json.loads(output.getvalue())
            self.assertEqual(code, 2)
            self.assertEqual(payload["status"], "blocked")
        finally:
            fixture.doCleanups()

    def test_plan_coordinator_live_probe_requires_real_clone_paths(self):
        fixture = BridgeIntegrationTests()
        fixture.plan_targets = (4, 4)
        fixture.setUp()
        try:
            root = pathlib.Path(fixture.temp.name)
            identity_path = root / "identity.json"
            identity_path.write_text(json.dumps(fixture.identity), encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = run_plan_coordinator.main([
                    "--bridge-dir", str(fixture.root), "--plan-id", fixture.plan["plan_id"],
                    "--step-ids", "priority_1", "priority_2",
                    "--expected-identity-json", str(identity_path),
                    "--intent-dir", str(fixture.store.directory),
                    "--live-boundary-probe", "--json",
                ])
            payload = json.loads(output.getvalue())
            self.assertEqual(code, 2)
            self.assertEqual(payload["status"], "blocked")
            self.assertFalse(list((fixture.root / "commands/pending").glob("*.json")))
        finally:
            fixture.doCleanups()

    def test_plan_coordinator_rejects_bad_preflight_and_incomplete_probe(self):
        fixture = BridgeIntegrationTests()
        fixture.plan_targets = (4, 4)
        fixture.setUp()
        try:
            base = dict(bridge_dir=fixture.root, plan_id=fixture.plan["plan_id"],
                step_ids=["priority_1", "priority_2"], expected_identity=fixture.identity,
                intent_directory=fixture.store.directory,
                clone_preflight={"schema": "oni.clone-launch-preflight.v1", "launch_allowed": True,
                    "bridge_dir": str(fixture.root), "process_counts": {"OxygenNotIncluded": 0, "oni_bridge": 0, "steam": 0},
                    "queue_counts": {}, "manifest_sha256": "sha256:" + "a" * 64,
                    "clone_save_sha256": fixture.identity["save_id_hash"],
                    "clone_preview_sha256": "sha256:" + "b" * 64,
                    "clone_save_path_id": "sha256:" + "c" * 64,
                    "clone_preview_path_id": "sha256:" + "d" * 64})
            base["clone_probe"] = lambda: base["clone_preflight"]
            with self.assertRaisesRegex(PlanCoordinatorError, "clone_preflight_save_hash_mismatch"):
                PlanSequenceCoordinator(**{**base, "clone_preflight": {
                    **base["clone_preflight"], "clone_save_sha256": "sha256:" + "e" * 64},
                    "process_probe": lambda: {"OxygenNotIncluded": 0, "oni_bridge": 0, "steam": 0}})
            with self.assertRaisesRegex(PlanCoordinatorError, "process_probe_incomplete"):
                PlanSequenceCoordinator(**base, process_probe=lambda: {}).tick()
            probes = iter(({"OxygenNotIncluded": 0, "oni_bridge": 0, "steam": 0},
                           {"OxygenNotIncluded": 1, "oni_bridge": 0, "steam": 0}))
            guarded = PlanSequenceCoordinator(**base, process_probe=lambda: next(probes), publish_one=True)
            self.assertEqual(guarded.tick()["stop_reason"], "protected_process_running")
            self.assertFalse(list((fixture.root / "commands/pending").glob("*.json")))
        finally:
            fixture.doCleanups()

    def test_plan_coordinator_receipt_keeps_bounded_observation_blocker(self):
        fixture = BridgeIntegrationTests()
        fixture.plan_targets = (4, 4)
        fixture.setUp()
        try:
            # Break only the committed frame in the isolated fixture.  The
            # adapter must return an unsafe observation and redact the parse
            # failure; the coordinator receipt should retain that stable code
            # so operators can diagnose the blocked tick without raw errors.
            (fixture.root / "decision-frame.json").write_text("{}\n", encoding="utf-8")
            preflight = {
                "schema": "oni.clone-launch-preflight.v1", "launch_allowed": True,
                "clone_id": "fixture", "bridge_dir": str(fixture.root),
                "process_counts": {"OxygenNotIncluded": 0, "oni_bridge": 0, "steam": 0},
                "queue_counts": {}, "manifest_sha256": "sha256:" + "a" * 64,
                "clone_save_sha256": fixture.identity["save_id_hash"],
                "clone_preview_sha256": "sha256:" + "b" * 64,
                "clone_save_path_id": "sha256:" + "c" * 64,
                "clone_preview_path_id": "sha256:" + "d" * 64,
            }
            coordinator = PlanSequenceCoordinator(
                bridge_dir=fixture.root, plan_id=fixture.plan["plan_id"],
                step_ids=["priority_1", "priority_2"], expected_identity=fixture.identity,
                intent_directory=fixture.store.directory, clone_preflight=preflight,
                clone_probe=lambda: preflight,
                process_probe=lambda: {"OxygenNotIncluded": 0, "oni_bridge": 0, "steam": 0},
            )
            result = coordinator.tick()
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["observation_blocker"], "bridge_observation_unreadable")
            receipt = json.loads((fixture.store.directory / "plan-coordinator-state.json").read_text(encoding="utf-8"))
            self.assertEqual(receipt["observation_blocker"], "bridge_observation_unreadable")
            self.assertFalse(result["live_admission"])
            self.assertFalse(result["side_effects"]["command_published"])
        finally:
            fixture.doCleanups()

    def test_plan_coordinator_final_process_guard_blocks_toctou_before_apply(self):
        fixture = BridgeIntegrationTests()
        fixture.plan_targets = (4, 4)
        fixture.setUp()
        try:
            preflight = {
                "schema": "oni.clone-launch-preflight.v1", "launch_allowed": True,
                "clone_id": "fixture", "bridge_dir": str(fixture.root),
                "process_counts": {"OxygenNotIncluded": 0, "oni_bridge": 0, "steam": 0},
                "queue_counts": {}, "manifest_sha256": "sha256:" + "a" * 64,
                "clone_save_sha256": fixture.identity["save_id_hash"],
                "clone_preview_sha256": "sha256:" + "b" * 64,
                "clone_save_path_id": "sha256:" + "c" * 64,
                "clone_preview_path_id": "sha256:" + "d" * 64,
            }
            probes = iter((
                {"OxygenNotIncluded": 0, "oni_bridge": 0, "steam": 0},
                {"OxygenNotIncluded": 0, "oni_bridge": 0, "steam": 0},
                {"OxygenNotIncluded": 1, "oni_bridge": 0, "steam": 0},
            ))
            coordinator = PlanSequenceCoordinator(
                bridge_dir=fixture.root,
                plan_id=fixture.plan["plan_id"],
                step_ids=["priority_1", "priority_2"],
                expected_identity=fixture.identity,
                intent_directory=fixture.store.directory,
                clone_preflight=preflight,
                clone_probe=lambda: preflight,
                process_probe=lambda: next(probes),
                publish_one=True,
            )
            before = fixture.tree()
            result = coordinator.tick()
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["stop_reason"], "protected_process_running")
            self.assertFalse(result["live_admission"])
            self.assertFalse(list((fixture.root / "commands/pending").glob("*.json")))
            self.assertEqual(before, fixture.tree())
        finally:
            fixture.doCleanups()

    def test_plan_coordinator_final_clone_boundary_guard_blocks_late_probe_drift(self):
        """A second clone probe must gate every mutable admission field."""
        fixture = BridgeIntegrationTests()
        fixture.plan_targets = (4, 4)
        fixture.setUp()
        try:
            process_counts = {
                "OxygenNotIncluded": 0,
                "oni_bridge": 0,
                "steam": 0,
            }
            queue_counts = {
                "commands/pending": 0,
                "commands/approving": 0,
                "commands/approved": 0,
                "commands/executing": 0,
                "commands/confirming": 0,
                "validation-controls/approved": 0,
                "validation-controls/inflight": 0,
            }
            preflight = {
                "schema": "oni.clone-launch-preflight.v1",
                "launch_allowed": True,
                "clone_id": "fixture",
                "bridge_dir": str(fixture.root),
                "process_counts": process_counts,
                "queue_counts": queue_counts,
                "manifest_sha256": "sha256:" + "a" * 64,
                "clone_save_sha256": fixture.identity["save_id_hash"],
                "clone_preview_sha256": "sha256:" + "b" * 64,
                "clone_save_path_id": "sha256:" + "c" * 64,
                "clone_preview_path_id": "sha256:" + "d" * 64,
            }

            cases = {
                "launch_not_allowed": ({**preflight, "launch_allowed": False}, "clone_launch_not_allowed"),
                "queue_nonempty": (
                    {**preflight, "queue_counts": {**queue_counts, "commands/pending": 1}},
                    "protected_queue_nonempty",
                ),
                "clone_process_nonzero": (
                    {**preflight, "process_counts": {**process_counts, "steam": 1}},
                    "protected_process_running",
                ),
                "process_key_missing": (
                    {**preflight, "process_counts": {"OxygenNotIncluded": 0, "oni_bridge": 0}},
                    "clone_process_counts_incomplete",
                ),
            }
            for label, (late_probe, expected_reason) in cases.items():
                with self.subTest(label=label):
                    probes = iter((preflight, late_probe))
                    coordinator = PlanSequenceCoordinator(
                        bridge_dir=fixture.root,
                        plan_id=fixture.plan["plan_id"],
                        step_ids=["priority_1", "priority_2"],
                        expected_identity=fixture.identity,
                        intent_directory=fixture.store.directory,
                        clone_preflight=preflight,
                        clone_probe=lambda: next(probes),
                        process_probe=lambda: dict(process_counts),
                        publish_one=True,
                    )
                    result = coordinator.tick()
                    self.assertEqual(result["status"], "blocked")
                    self.assertEqual(result["stop_reason"], expected_reason)
                    self.assertFalse(result["live_admission"])
                    self.assertFalse(list((fixture.root / "commands/pending").glob("*.json")))
                    for receipt in fixture.store.directory.glob("*.json"):
                        receipt.unlink()
        finally:
            fixture.doCleanups()


class BoundedReadTests(unittest.TestCase):
    def test_size_limit_and_growth_are_rejected(self):
        with tempfile.TemporaryDirectory(prefix="oni-bounded-read-") as temp:
            path = pathlib.Path(temp) / "evidence.json"
            path.write_bytes(b"1234")
            with self.assertRaises(command_custody.CommandCustodyError):
                command_custody.read_stable_bytes(path, max_bytes=3)
            self.assertEqual(command_custody.read_stable_bytes(path, max_bytes=4)[0], b"1234")
            with mock.patch.object(command_custody.os, "read", return_value=b"12345"):
                with self.assertRaises(command_custody.CommandCustodyError):
                    command_custody.read_stable_bytes(path, max_bytes=4)


class AnnotatedLeafTest(unittest.TestCase):
    def test_accepted_plan_condition_annotations_remain_usable(self):
        fixture = BridgeIntegrationTests()
        fixture.annotate_conditions = True
        fixture.setUp()
        try:
            proposal, _, command = fixture.publish()
            self.assertTrue(any("note" in item for item in command["preconditions"]))
            fixture.confirm()
            self.assertEqual(fixture.adapter.reconcile(proposal).status, EffectStatus.APPLIED)
        finally:
            fixture.doCleanups()


class LlmPlanIntegrationTests(unittest.TestCase):
    def test_planner_projection_persists_provenance_in_real_plan_bundle(self):
        with tempfile.TemporaryDirectory(prefix="oni-llm-plan-integration-") as temp:
            root = pathlib.Path(temp) / "bridge"
            root.mkdir()
            ensure_command_dirs(root)
            write_live_state(root, snapshot())
            state = json.loads((root / "latest_state.json").read_text(encoding="utf-8-sig"))
            hint = build_duplicant_priority_hint(state, instance_id=123, chore_group="Build", priority=4)
            advice = [{"incident_id": "inc-build", "reason": "stabilize build priority", "command": hint}]
            planner = {
                "schema": "oni.plan.v2", "mode": "llm_ranked", "goal": "preserve colony",
                "prompt_hash": "sha256:" + "a" * 64,
                "incident_fingerprint": "sha256:" + "b" * 64,
                "source": {"session_id": state["session_id"], "save_id_hash": state["save_id_hash"],
                           "world_id": state["world_id"], "snapshot_id": state["snapshot_id"],
                           "state_hash": canonical_state_hash(state)},
                "plan": [{"incident_id": "inc-build"}],
                "llm": {"status": "ok", "model": "cpa-oni/synthetic",
                         "response_hash": "sha256:" + "c" * 64},
            }
            definition = colony_plan_definition_from_plan(planner, advice)
            created = create_colony_plan(root, definition)
            self.assertEqual(created["decision_provenance"]["mode"], "llm_ranked")
            self.assertEqual(created["decision_provenance"]["response_hash"], "sha256:" + "c" * 64)
            self.assertEqual(created["steps"][0]["action"], hint["action"])
            self.assertEqual(created["steps"][0]["args"], hint["args"])

    def test_planner_projection_rejects_stale_source_snapshot(self):
        with tempfile.TemporaryDirectory(prefix="oni-llm-plan-stale-") as temp:
            root = pathlib.Path(temp) / "bridge"
            root.mkdir()
            ensure_command_dirs(root)
            write_live_state(root, snapshot())
            state = json.loads((root / "latest_state.json").read_text(encoding="utf-8-sig"))
            hint = build_duplicant_priority_hint(state, instance_id=123, chore_group="Build", priority=4)
            advice = [{"incident_id": "inc-build", "command": hint}]
            planner = {"schema": "oni.plan.v2", "mode": "llm_ranked", "goal": "preserve",
                       "prompt_hash": "sha256:" + "a" * 64, "incident_fingerprint": "sha256:" + "b" * 64,
                       "source": {"session_id": state["session_id"], "save_id_hash": state["save_id_hash"],
                                  "world_id": state["world_id"], "snapshot_id": "old", "state_hash": "sha256:" + "d" * 64},
                       "plan": [{"incident_id": "inc-build"}],
                       "llm": {"status": "ok", "model": "x", "response_hash": "sha256:" + "c" * 64}}
            definition = colony_plan_definition_from_plan(planner, advice)
            with self.assertRaises(ColonyPlanError):
                create_colony_plan(root, definition)


if __name__ == "__main__":
    unittest.main(verbosity=2)
