#!/usr/bin/env python3
"""M28 offline priority acceptance; no Bridge or live-game adapter."""
from __future__ import annotations

import copy
import dataclasses
import datetime as dt
import hashlib
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import advanced_actions, controller_loop, field_contracts, state_quality
from agent.controller_loop import (
    ActionPolicy, Condition, ControllerCore, ControllerSequence, DurableIntentStore, EffectObservation,
    EffectStatus, Freshness, Observation, ObservationQuality, ReconciliationStatus,
    build_duplicant_priority_policy, decide_bounded_action, normalize_observation,
    reconcile_effect,
)
from agent.field_contracts import (
    DUPLICANT_CHORE_PRIORITY_CONTRACT_ID, GAME_PAUSED_CONTRACT_ID,
    contract_quality_fixture,
)

NOW = dt.datetime(2026, 9, 10, 0, 0, 10, tzinfo=dt.timezone.utc)
START = NOW - dt.timedelta(seconds=10)
TARGET = 123
PREFIX = "duplicants.by_instance_id.id_123"
PRIORITY = PREFIX + ".chore_priorities.Build"
SOURCE = "Components.LiveMinionIdentities+ChoreConsumer"


def snapshot(*, priority=3, seq=10, snapshot_seq=5, seconds=0):
    ts = (START + dt.timedelta(seconds=seconds)).isoformat()
    state = {
        "schema": "oni.state.v2", "event": "state_snapshot",
        "session_id": "offline-m28", "seq": seq, "snapshot_seq": snapshot_seq,
        "snapshot_id": f"offline-m28:{seq}", "ts": ts,
        "save_id_hash": "sha256:" + "a" * 64, "save_dirty": False,
        "world_id": 0, "game_build": "cl-744825", "mod_version": "offline-m28",
        "sample": {"interval_seconds": 5},
        "writer": {"dropped_snapshot_count": 0, "queue_depth": 0,
                   "fault_count": 0, "non_finite_numeric_count": 0, "fault": None},
        "observation_scope": {"active_world_id": 0, "populated_world_ids": [0],
                              "covered_world_ids": [0], "complete_for_populated_worlds": True},
        "game": {"paused": True, "sandbox_mode": False, "instant_build_mode": False},
        "cycle": 11, "game_time_cycles": 11.25,
        "duplicants": {"total": 1, "stressed": 0, "idle": 0, "by_instance_id": {
            "id_123": {"instance_id": TARGET, "name": "Ada", "world_id": 0,
                       "chore_priorities": {"Build": priority, "Dig": 2}}}},
        "resources": {"food_kcal": 12000}, "report_counters": {},
        "materials": {}, "production": {}, "consumption": {}, "power": {},
        "oxygen": {}, "water": {}, "temperature": {}, "rockets": {},
        "space": {}, "resilience": {}, "colony": {}, "alerts": [],
        "research": {"current": "FarmingTech"}, "buildings": {},
        "functional_buildings": {"schema": "oni.functional_buildings.v1",
                                 "captured_at": ts, "world_id": 0, "complete": True, "instances": []},
        "designations": {"dig": {"by_cell": {}}},
        "blueprints": {"catalog": {}, "by_instance_id": {}, "utility_paths": {}},
        "cells": {"by_id": {}}, "read_metrics_ms": {},
        "quality": {"game.paused": contract_quality_fixture(GAME_PAUSED_CONTRACT_ID),
                    PRIORITY: contract_quality_fixture(DUPLICANT_CHORE_PRIORITY_CONTRACT_ID),
                    "cycle": {"status": "observed", "source": "GameClock.GetCycle",
                              "semantic": "display_1_based"},
                    "game_time_cycles": {
                        "status": "observed", "source": "GameClock.GetTimeInCycles",
                        "semantic": "display_1_based_continuous", "unit": "cycle",
                        "scope": "save", "completeness": "complete",
                        "captured_at": "snapshot.ts",
                    }},
    }
    for path in ("duplicants.by_instance_id", PREFIX + ".instance_id", PREFIX + ".name", PREFIX + ".world_id"):
        state["quality"][path] = {"status": "observed", "source": SOURCE}
    return state


def observation(state=None, *, freshness=Freshness.FRESH, quality=ObservationQuality.SAFE):
    payload = snapshot() if state is None else state
    return Observation(payload.get("snapshot_id", "fixture"), freshness, quality, payload,
                       source="offline-recording-adapter")


def policy(*, maximum=3):
    return build_duplicant_priority_policy(snapshot(), instance_id=TARGET, max_attempts=maximum)


def generic_policy(*, action="offline_toggle", maximum=3):
    return ActionPolicy(
        action, {"target": "fixture"}, (Condition("game.paused", "eq", True),),
        (Condition("active", "eq", True),), maximum,
    )


def generic_snapshot(*, active=False, **kwargs):
    state = snapshot(**kwargs)
    state["active"] = active
    return state


def generic_effect(proposal, payload=None, *, status=EffectStatus.APPLIED,
                   freshness=Freshness.FRESH, quality=ObservationQuality.SAFE):
    return EffectObservation(
        proposal.idempotency_key, status, freshness, quality,
        {"active": True} if payload is None else payload,
    )


def effect(proposal, state=None, *, status=EffectStatus.APPLIED,
           freshness=Freshness.FRESH, quality=ObservationQuality.SAFE):
    return EffectObservation(proposal.idempotency_key, status, freshness, quality,
                             snapshot(priority=4, seq=11, snapshot_seq=6, seconds=1)
                             if state is None else state)


def set_path(state, path, value):
    if path.startswith("quality:"):
        parts = path[len("quality:"):].split("|")
        state["quality"][parts[0]][parts[1]] = value
        return
    target = state
    parts = path.split(".")
    for part in parts[:-1]:
        target = target[part]
    target[parts[-1]] = value


class RecordingAdapter:
    def __init__(self, current=None, outcome=None):
        self.current = observation() if current is None else current
        self.outcome = outcome or effect
        self.applied = []
        self.observed = 0
        self.before_apply = None

    def observe(self):
        self.observed += 1
        return self.current

    def apply(self, proposal):
        if self.before_apply is not None:
            self.before_apply(proposal)
        self.applied.append(proposal)
        return self.outcome(proposal)


class AsyncConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oni-async-controller-")
        self.addCleanup(self.temporary.cleanup)
        self.store = DurableIntentStore(self.temporary.name)

    def make_core(self):
        class Adapter(RecordingAdapter):
            def __init__(self):
                super().__init__(outcome=lambda p: effect(p, {}, status=EffectStatus.PENDING))
                self.reader = lambda p: effect(p, {}, status=EffectStatus.PENDING)
                self.effect_reads = 0

            def observe_effect(self, proposal):
                self.effect_reads += 1
                return self.reader(proposal)

        adapter = Adapter()
        return adapter, ControllerCore(adapter, policy(maximum=3), intent_store=self.store)

    def test_matching_raw_snapshot_waits_for_explicit_confirmation(self):
        adapter, core = self.make_core()
        first = core.step(now=NOW)
        self.assertEqual(first.reconciliation.recovery_reason, "effect_pending")
        adapter.current = observation(snapshot(priority=4, seq=11, snapshot_seq=6, seconds=1))
        second = core.step(now=NOW)
        self.assertFalse(second.reconciliation.confirmed)
        self.assertEqual(second.reconciliation.recovery_reason, "effect_pending")
        adapter.reader = effect
        third = core.step(now=NOW)
        self.assertTrue(third.reconciliation.confirmed)
        self.assertEqual(adapter.effect_reads, 2)
        self.assertEqual(len(adapter.applied), 1)
        self.assertEqual(core.step(now=NOW).decision.reason, "intent_already_confirmed")
        self.assertEqual(adapter.effect_reads, 2)

    def test_pending_exhausts_budget_without_redispatch(self):
        adapter, core = self.make_core()
        core.step(now=NOW)
        core.step(now=NOW)
        last = core.step(now=NOW)
        self.assertEqual(last.reconciliation.recovery_reason, "recovery_attempt_limit_exhausted:effect_pending")
        self.assertEqual(core.step(now=NOW).decision.reason, "intent_blocked")
        self.assertEqual(adapter.effect_reads, 2)
        self.assertEqual(len(adapter.applied), 1)

    def test_reader_disappearance_cannot_fall_back_to_matching_state(self):
        adapter, core = self.make_core()
        core.step(now=NOW)
        adapter.current = observation(snapshot(priority=4, seq=11, snapshot_seq=6, seconds=1))
        adapter.observe_effect = None
        result = core.step(now=NOW)
        self.assertEqual(result.reconciliation.recovery_reason, "effect_status_unknown")
        self.assertEqual(len(adapter.applied), 1)

    def test_reader_error_cannot_fall_back_to_matching_state(self):
        adapter, core = self.make_core()
        core.step(now=NOW)
        adapter.current = observation(snapshot(priority=4, seq=11, snapshot_seq=6, seconds=1))
        def broken(_):
            raise RuntimeError("untrusted error detail")
        adapter.reader = broken
        result = core.step(now=NOW)
        self.assertEqual(result.reconciliation.recovery_reason, "effect_status_unknown")
        self.assertNotIn("untrusted error detail", str(result.as_dict()))
        self.assertEqual(len(adapter.applied), 1)

    def test_reader_timeout_then_confirmation_never_resends(self):
        adapter, core = self.make_core()
        core.step(now=NOW)
        def timed_out(_):
            raise TimeoutError()
        adapter.reader = timed_out
        self.assertEqual(core.step(now=NOW).reconciliation.recovery_reason, "effect_timeout")
        adapter.reader = effect
        self.assertTrue(core.step(now=NOW).reconciliation.confirmed)
        self.assertEqual(len(adapter.applied), 1)

    def test_wrong_confirmation_key_stops_even_when_raw_state_matches(self):
        adapter, core = self.make_core()
        core.step(now=NOW)
        adapter.current = observation(snapshot(priority=4, seq=11, snapshot_seq=6, seconds=1))
        adapter.reader = lambda p: EffectObservation("different-key", EffectStatus.APPLIED,
            Freshness.FRESH, ObservationQuality.SAFE, adapter.current.payload)
        self.assertEqual(core.step(now=NOW).reconciliation.recovery_reason, "effect_idempotency_key_mismatch")
        self.assertEqual(len(adapter.applied), 1)

    def test_rejected_confirmation_stops_even_when_raw_state_matches(self):
        adapter, core = self.make_core()
        core.step(now=NOW)
        adapter.current = observation(snapshot(priority=4, seq=11, snapshot_seq=6, seconds=1))
        adapter.reader = lambda p: effect(p, {}, status=EffectStatus.REJECTED)
        self.assertEqual(core.step(now=NOW).reconciliation.recovery_reason, "effect_status_rejected")
        self.assertEqual(len(adapter.applied), 1)


class PriorityControllerTests(unittest.TestCase):
    def setUp(self):
        # Receipts are isolated in a private temporary directory: they cannot
        # address the runtime Bridge directory or remain in the workspace.
        self.temporary = tempfile.TemporaryDirectory(prefix="m28-test-")
        self.directory = pathlib.Path(self.temporary.name).resolve()
        self.assertFalse(self.directory.is_relative_to(ROOT.resolve()))
        self.addCleanup(self.temporary.cleanup)
        self.store = DurableIntentStore(self.directory)

    def core(self, adapter, *, maximum=3):
        return ControllerCore(adapter, policy(maximum=maximum), intent_store=self.store)

    def proposal(self):
        decision = decide_bounded_action(normalize_observation(observation(), now=NOW), policy(), now=NOW)
        self.assertIsNotNone(decision.proposal, decision.blockers)
        return decision.proposal

    def assert_no_action(self, state, expected=None):
        adapter = RecordingAdapter(observation(state))
        cycle = self.core(adapter).step(now=NOW)
        self.assertEqual(adapter.applied, [])
        self.assertEqual(cycle.action_count, 0)
        self.assertTrue(cycle.decision.blockers or cycle.normalized_state.blockers)
        if expected:
            self.assertIn(expected, str(cycle.decision.as_dict()) + str(cycle.normalized_state.blockers))

    def test_exact_helper_shape_and_full_later_confirmation(self):
        adapter = RecordingAdapter()
        core = self.core(adapter)
        def receipt_precedes_apply(proposal):
            receipts = list(self.directory.glob("*.json"))
            self.assertEqual(len(receipts), 1)
            record = json.loads(receipts[0].read_text())
            self.assertEqual(record["idempotency_key"], proposal.idempotency_key)
            self.assertEqual(len(core.consumed_intents), 1)
        adapter.before_apply = receipt_precedes_apply
        cycle = core.step(now=NOW)
        expected = advanced_actions.build_duplicant_priority_hint(snapshot(), instance_id=TARGET,
                                                                  chore_group="Build", priority=4)
        actual = cycle.decision.proposal.as_dict()
        for key in ("action", "args", "preconditions", "expected_effects"):
            self.assertEqual(actual[key], expected[key])
        self.assertEqual(actual["source_snapshot"], snapshot())
        self.assertEqual(len(adapter.applied), 1)
        self.assertTrue(cycle.reconciliation.confirmed)
        self.assertFalse(cycle.live_admission)
        self.assertEqual(core.step(now=NOW).decision.reason, "intent_already_confirmed")
        self.assertEqual(len(adapter.applied), 1)

    def test_final_apply_guard_runs_immediately_before_apply(self):
        events = []
        adapter = RecordingAdapter(current=observation(generic_snapshot()))
        adapter.before_apply = lambda _proposal: events.append("apply")

        def guard(_proposal):
            events.append("guard")
            return True

        core = ControllerCore(adapter, generic_policy(), final_apply_guard=guard)
        cycle = core.step(now=NOW)
        self.assertEqual(events, ["guard", "apply"])
        self.assertEqual(len(adapter.applied), 1)
        self.assertFalse(cycle.live_admission)

    def test_final_apply_guard_rejects_toctou_without_adapter_write(self):
        events = []
        adapter = RecordingAdapter(current=observation(generic_snapshot()))
        adapter.before_apply = lambda _proposal: events.append("apply")

        def guard(_proposal):
            events.append("guard")
            return False

        core = ControllerCore(adapter, generic_policy(), final_apply_guard=guard)
        cycle = core.step(now=NOW)
        self.assertEqual(events, ["guard"])
        self.assertEqual(adapter.applied, [])
        self.assertEqual(cycle.effect.status, EffectStatus.REJECTED)
        self.assertEqual(cycle.reconciliation.status, ReconciliationStatus.BLOCKED)
        self.assertFalse(cycle.live_admission)

    def test_final_apply_guard_requires_exact_true(self):
        cases = {
            "false": False,
            "none": None,
            "zero": 0,
            "empty_string": "",
            "exception": RuntimeError("guard unreadable"),
        }
        for label, outcome in cases.items():
            with self.subTest(label=label):
                directory = self.directory / f"guard-{label}"
                directory.mkdir()
                events = []
                adapter = RecordingAdapter(current=observation(generic_snapshot()))
                adapter.before_apply = lambda _proposal: events.append("apply")

                def guard(_proposal, outcome=outcome):
                    events.append("guard")
                    if isinstance(outcome, BaseException):
                        raise outcome
                    return outcome

                core = ControllerCore(
                    adapter,
                    generic_policy(),
                    intent_store=DurableIntentStore(directory),
                    final_apply_guard=guard,
                )
                cycle = core.step(now=NOW)
                self.assertEqual(events, ["guard"])
                self.assertEqual(adapter.applied, [])
                self.assertEqual(cycle.effect.status, EffectStatus.REJECTED)
                self.assertEqual(cycle.reconciliation.status, ReconciliationStatus.BLOCKED)
                self.assertFalse(cycle.live_admission)

    def test_explicit_resume_reuses_persisted_proposal_without_apply(self):
        class ResumableAdapter(RecordingAdapter):
            def observe_effect(self, proposal):
                return effect(proposal, {}, status=EffectStatus.TIMEOUT)

        adapter = ResumableAdapter(outcome=lambda proposal: effect(proposal, {}, status=EffectStatus.TIMEOUT))
        first = self.core(adapter)
        initial = first.step(now=NOW)
        self.assertEqual(initial.reconciliation.status, ReconciliationStatus.RETRYABLE)
        self.assertEqual(len(adapter.applied), 1)
        receipt_path = next(self.directory.glob("*.json"))
        receipt = json.loads(receipt_path.read_text())
        self.assertEqual(receipt["proposal"]["source_snapshot"], snapshot())

        resumed = self.core(adapter)
        recovered = resumed.resume_consumed_intent(now=NOW)
        self.assertIn(recovered.reconciliation.status,
            {ReconciliationStatus.RETRYABLE, ReconciliationStatus.BLOCKED})
        self.assertEqual(recovered.effect.status, EffectStatus.TIMEOUT)
        self.assertEqual(recovered.decision.reason, "intent_pending_reobserve")
        self.assertEqual(len(adapter.applied), 1)

    def test_already_four_is_trusted_noop(self):
        adapter = RecordingAdapter(observation(snapshot(priority=4)))
        cycle = self.core(adapter).step(now=NOW)
        self.assertEqual(cycle.decision.reason, "target_already_at_priority")
        self.assertEqual(adapter.applied, [])
        self.assertEqual(list(self.directory.iterdir()), [])
        # A completed no-op does not become a new send if telemetry regresses.
        core = self.core(adapter)
        core.step(now=NOW)
        adapter.current = observation()
        self.assertEqual(core.step(now=NOW).decision.reason, "intent_already_satisfied")
        self.assertEqual(adapter.applied, [])

    def test_already_four_with_unknown_identity_or_quality_is_blocked(self):
        for path, value in ((PREFIX + ".name", "Other"),
                            ("quality:" + PRIORITY + "|status", "unknown")):
            with self.subTest(path=path):
                state = snapshot(priority=4)
                set_path(state, path, value)
                self.assert_no_action(state)

    def test_wrapper_labels_cannot_admit_missing_legacy_or_projected_snapshot(self):
        for state in ({}, {"ready": True}, {"schema": "oni.state.v1"},
                      {"schema": "oni.state.v2", "event": "state_snapshot",
                       "game": {"paused": True}, "duplicants": snapshot()["duplicants"]}):
            with self.subTest(fields=sorted(state)):
                self.assert_no_action(state)
        state = snapshot()
        del state["cells"]
        self.assert_no_action(state, "snapshot_fields_missing:cells")
        for key, value in (("cells", None), ("duplicants", []), ("power", "unknown"),
                           ("quality", None), ("alerts", {})):
            state = snapshot()
            state[key] = value
            self.assert_no_action(state)

    def test_freshness_and_wrapper_quality_are_fail_closed(self):
        for freshness, quality in ((Freshness.STALE, ObservationQuality.SAFE),
                                  (Freshness.UNKNOWN, ObservationQuality.SAFE),
                                  (Freshness.FRESH, ObservationQuality.PARTIAL),
                                  (Freshness.FRESH, ObservationQuality.UNSAFE),
                                  (Freshness.FRESH, ObservationQuality.UNKNOWN)):
            with self.subTest(freshness=freshness, quality=quality):
                adapter = RecordingAdapter(observation(freshness=freshness, quality=quality))
                self.assertEqual(self.core(adapter).step(now=NOW).action_count, 0)
                self.assertEqual(adapter.applied, [])

    def test_malformed_envelope_writer_scope_and_timestamps_block(self):
        cases = [("schema", "oni.state.v1"), ("event", "error"), ("seq", True),
                 ("seq", "10"), ("snapshot_seq", 11), ("snapshot_seq", 0),
                 ("snapshot_id", "wrong"), ("session_id", ""), ("save_id_hash", "sha256:fake"),
                 ("world_id", True), ("game_build", ""), ("mod_version", None),
                 ("save_dirty", None), ("sample.interval_seconds", 0),
                 ("sample.interval_seconds", float("inf")), ("ts", "2026-09-10T00:00:00"),
                 ("ts", (START - dt.timedelta(seconds=6)).isoformat()),
                 ("ts", (NOW + dt.timedelta(seconds=6)).isoformat()),
                 ("observation_scope.complete_for_populated_worlds", False),
                 ("observation_scope.covered_world_ids", []),
                 ("observation_scope.active_world_id", 1),
                 ("writer.dropped_snapshot_count", 1), ("writer.queue_depth", 1),
                 ("writer.fault_count", 1), ("writer.fault", "fault"),
                 ("writer.non_finite_numeric_count", 1), ("writer.queue_depth", True)]
        for path, value in cases:
            with self.subTest(path=path, value=value):
                state = snapshot()
                set_path(state, path, value)
                self.assert_no_action(state)

    def test_game_clock_scalars_and_quality_cannot_create_decision(self):
        integer_time = snapshot()
        integer_time["game_time_cycles"] = 11
        normalized = normalize_observation(observation(integer_time), now=NOW)
        self.assertTrue(normalized.eligible, normalized.blockers)
        self.assertIsNotNone(decide_bounded_action(normalized, policy(), now=NOW).proposal)

        scalar_cases = [
            ("cycle", "bogus"), ("cycle", {}), ("cycle", []),
            ("cycle", True), ("cycle", None), ("cycle", 0),
            ("cycle", -1), ("cycle", 11.0),
            ("game_time_cycles", "bogus"), ("game_time_cycles", {}),
            ("game_time_cycles", []), ("game_time_cycles", True),
            ("game_time_cycles", None), ("game_time_cycles", float("nan")),
            ("game_time_cycles", float("inf")), ("game_time_cycles", float("-inf")),
            ("game_time_cycles", 10.999), ("game_time_cycles", 12.0),
        ]
        for path, value in scalar_cases:
            with self.subTest(path=path, value=value):
                state = snapshot()
                state[path] = value
                self.assert_no_action(state)

        quality_cases = [
            ("cycle", None),
            ("cycle", {"status": "error", "source": "GameClock.GetCycle", "semantic": "display_1_based"}),
            ("cycle", {"status": "estimated", "source": "GameClock.GetCycle", "semantic": "display_1_based"}),
            ("cycle", {"status": "observed", "source": "wrong", "semantic": "display_1_based"}),
            ("cycle", {"status": "observed", "source": "GameClock.GetCycle", "semantic": "wrong"}),
            ("game_time_cycles", None),
            ("game_time_cycles", {"status": "error", "source": "GameClock.GetTimeInCycles",
                                    "semantic": "display_1_based_continuous", "unit": "cycle",
                                    "scope": "save", "completeness": "complete", "captured_at": "snapshot.ts"}),
            ("game_time_cycles", {"status": "estimated", "source": "GameClock.GetTimeInCycles",
                                    "semantic": "display_1_based_continuous", "unit": "cycle",
                                    "scope": "save", "completeness": "complete", "captured_at": "snapshot.ts"}),
            ("game_time_cycles", {"status": "observed", "source": "wrong",
                                    "semantic": "display_1_based_continuous", "unit": "cycle",
                                    "scope": "save", "completeness": "complete", "captured_at": "snapshot.ts"}),
            ("game_time_cycles", {"status": "observed", "source": "GameClock.GetTimeInCycles",
                                    "semantic": "wrong", "unit": "cycle", "scope": "save",
                                    "completeness": "complete", "captured_at": "snapshot.ts"}),
            ("game_time_cycles", {"status": "observed", "source": "GameClock.GetTimeInCycles",
                                    "semantic": "display_1_based_continuous", "unit": "seconds",
                                    "scope": "save", "completeness": "complete", "captured_at": "snapshot.ts"}),
            ("game_time_cycles", {"status": "observed", "source": "GameClock.GetTimeInCycles",
                                    "semantic": "display_1_based_continuous", "unit": "cycle",
                                    "scope": "world", "completeness": "complete", "captured_at": "snapshot.ts"}),
            ("game_time_cycles", {"status": "observed", "source": "GameClock.GetTimeInCycles",
                                    "semantic": "display_1_based_continuous", "unit": "cycle",
                                    "scope": "save", "completeness": "partial", "captured_at": "snapshot.ts"}),
            ("game_time_cycles", {"status": "observed", "source": "GameClock.GetTimeInCycles",
                                    "semantic": "display_1_based_continuous", "unit": "cycle",
                                    "scope": "save", "completeness": "complete", "captured_at": "old.ts"}),
        ]
        for path, value in quality_cases:
            with self.subTest(quality_path=path, value=value):
                state = snapshot()
                if value is None:
                    del state["quality"][path]
                else:
                    state["quality"][path] = value
                self.assert_no_action(state)

    def test_selected_identity_and_observed_priority_contract_are_required(self):
        cases = [(PREFIX + ".instance_id", True), (PREFIX + ".instance_id", 124),
                 (PREFIX + ".name", "Other"), (PREFIX + ".world_id", 1),
                 (PRIORITY, 2), (PRIORITY, True), (PRIORITY, 3.0), (PRIORITY, None),
                 ("game.paused", False), ("game.paused", 1)]
        for path, value in cases:
            with self.subTest(path=path, value=value):
                state = snapshot()
                set_path(state, path, value)
                self.assert_no_action(state)
        for path in ("game.paused", PRIORITY):
            for key, value in (("status", "unknown"), ("status", "estimated"),
                               ("status", "error"), ("source", "fixture"),
                               ("completeness", "partial"), ("captured_at", "old.ts"),
                               ("contract_id", "wrong")):
                with self.subTest(path=path, key=key, value=value):
                    state = snapshot()
                    state["quality"][path][key] = value
                    self.assert_no_action(state)
        for suffix in ("instance_id", "name", "world_id"):
            state = snapshot()
            del state["quality"][PREFIX + "." + suffix]
            self.assert_no_action(state)
        state = snapshot()
        state["duplicants"]["by_instance_id"]["alias"] = copy.deepcopy(state["duplicants"]["by_instance_id"]["id_123"])
        self.assert_no_action(state, "duplicant_identity_duplicate")

    def test_policy_cannot_change_target_transition_or_helper_contracts(self):
        base = policy()
        for key, value in (("duplicant_instance_id", True), ("expected_name", "Other"),
                           ("chore_group", "Dig"), ("from_priority", 2),
                           ("from_priority", 3.0), ("priority", 5), ("extra", 1)):
            with self.subTest(key=key):
                args = dict(base.args)
                args[key] = value
                result = decide_bounded_action(normalize_observation(observation(), now=NOW),
                                               dataclasses.replace(base, args=args), now=NOW)
                self.assertIsNone(result.proposal)
        for bad in (dataclasses.replace(base, preconditions=base.preconditions[1:]),
                    dataclasses.replace(base, expected_effects=(Condition(PRIORITY, "eq", 4),)),
                    dataclasses.replace(base, max_attempts=4)):
            self.assertIsNone(decide_bounded_action(normalize_observation(observation(), now=NOW), bad, now=NOW).proposal)

    def test_changed_runtime_identity_never_confirms(self):
        proposal = self.proposal()
        cases = [("session_id", "other"), ("save_id_hash", "sha256:" + "b" * 64),
                 ("world_id", 1), ("game_build", "other"), ("mod_version", "other"),
                 (PREFIX + ".instance_id", 124), (PREFIX + ".name", "Other"),
                 (PREFIX + ".world_id", 1)]
        for path, value in cases:
            with self.subTest(path=path):
                later = snapshot(priority=4, seq=11, snapshot_seq=6, seconds=1)
                set_path(later, path, value)
                if path == "session_id":
                    later["snapshot_id"] = "other:11"
                if path == "world_id":
                    later["observation_scope"].update(active_world_id=1, populated_world_ids=[1], covered_world_ids=[1])
                    later["duplicants"]["by_instance_id"]["id_123"]["world_id"] = 1
                self.assertFalse(reconcile_effect(proposal, effect(proposal, later), now=NOW).confirmed)

    def test_sequence_and_timestamp_must_both_strictly_advance(self):
        proposal = self.proposal()
        for seq, snap_seq, seconds in ((10, 5, 0), (9, 4, 1), (11, 5, 1),
                                       (11, 7, 1), (11, 6, 0), (11, 6, -1)):
            with self.subTest(seq=seq, snap_seq=snap_seq, seconds=seconds):
                later = snapshot(priority=4, seq=seq, snapshot_seq=snap_seq, seconds=seconds)
                self.assertFalse(reconcile_effect(proposal, effect(proposal, later), now=NOW).confirmed)

    def test_effect_snapshot_has_same_trust_and_full_payload_gate(self):
        proposal = self.proposal()
        for path, value in (("writer.fault_count", 1), ("writer.queue_depth", 1),
                            ("observation_scope.complete_for_populated_worlds", False),
                            ("game.paused", False), ("ts", START.isoformat()),
                            ("quality:" + PRIORITY + "|status", "unknown"),
                            ("quality:game.paused|status", "unknown"),
                            ("quality:" + PREFIX + ".name|status", "unknown")):
            with self.subTest(path=path):
                later = snapshot(priority=4, seq=11, snapshot_seq=6, seconds=1)
                set_path(later, path, value)
                self.assertFalse(reconcile_effect(proposal, effect(proposal, later), now=NOW).confirmed)
        for state in ({}, {"active": True}, {"duplicants": snapshot(priority=4)["duplicants"]}):
            self.assertFalse(reconcile_effect(proposal, effect(proposal, state), now=NOW).confirmed)
        later = snapshot(priority=4, seq=11, snapshot_seq=6, seconds=1)
        del later["cells"]
        self.assertFalse(reconcile_effect(proposal, effect(proposal, later), now=NOW).confirmed)

    def test_malformed_game_clock_scalars_cannot_confirm_later_effect(self):
        proposal = self.proposal()
        scalar_cases = [
            ("cycle", "bogus"), ("cycle", {}), ("cycle", []),
            ("cycle", True), ("cycle", None), ("cycle", 0),
            ("cycle", 11.0), ("game_time_cycles", "bogus"),
            ("game_time_cycles", {}), ("game_time_cycles", []),
            ("game_time_cycles", True), ("game_time_cycles", None),
            ("game_time_cycles", float("nan")), ("game_time_cycles", float("inf")),
            ("game_time_cycles", float("-inf")),
            ("game_time_cycles", 10.999), ("game_time_cycles", 12.0),
        ]
        for path, value in scalar_cases:
            with self.subTest(path=path, value=value):
                later = snapshot(priority=4, seq=11, snapshot_seq=6, seconds=1)
                later[path] = value
                self.assertFalse(reconcile_effect(proposal, effect(proposal, later), now=NOW).confirmed)

        quality_cases = [
            ("cycle", "status", "error"),
            ("cycle", "status", "estimated"),
            ("cycle", "source", "wrong"),
            ("cycle", "semantic", "wrong"),
            ("game_time_cycles", "status", "estimated"),
            ("game_time_cycles", "source", "wrong"),
            ("game_time_cycles", "semantic", "wrong"),
            ("game_time_cycles", "unit", "seconds"),
            ("game_time_cycles", "scope", "world"),
            ("game_time_cycles", "completeness", "partial"),
            ("game_time_cycles", "captured_at", "old.ts"),
        ]
        for path, key, value in quality_cases:
            with self.subTest(quality_path=path, key=key, value=value):
                later = snapshot(priority=4, seq=11, snapshot_seq=6, seconds=1)
                later["quality"][path][key] = value
                self.assertFalse(reconcile_effect(proposal, effect(proposal, later), now=NOW).confirmed)
        for path in ("cycle", "game_time_cycles"):
            with self.subTest(missing_quality_path=path):
                later = snapshot(priority=4, seq=11, snapshot_seq=6, seconds=1)
                del later["quality"][path]
                self.assertFalse(reconcile_effect(proposal, effect(proposal, later), now=NOW).confirmed)

    def test_unknown_rejected_wrong_key_and_bad_wrapper_never_confirm(self):
        proposal = self.proposal()
        for bad in (effect(proposal, status=EffectStatus.UNKNOWN),
                    effect(proposal, status=EffectStatus.REJECTED),
                    effect(proposal, freshness=Freshness.UNKNOWN),
                    effect(proposal, quality=ObservationQuality.PARTIAL),
                    dataclasses.replace(effect(proposal), idempotency_key="wrong"),
                    None, {"status": "applied"}):
            with self.subTest(effect=type(bad).__name__):
                self.assertFalse(reconcile_effect(proposal, bad, now=NOW).confirmed)

    def test_source_snapshot_is_hash_bound_and_immutable(self):
        proposal = self.proposal()
        with self.assertRaises(TypeError):
            proposal.source_snapshot["session_id"] = "other"
        for bad in (dataclasses.replace(proposal, source_snapshot={}),
                    dataclasses.replace(proposal, state_hash="bad")):
            self.assertFalse(reconcile_effect(bad, effect(bad), now=NOW).confirmed)

    def test_direct_reconciliation_cannot_confirm_outside_attempt_bound(self):
        proposal = self.proposal()
        for attempt, maximum in ((0, 3), (4, 3), (True, 3), (1, 4), (1, True)):
            invalid = dataclasses.replace(proposal, attempt=attempt, max_attempts=maximum)
            self.assertFalse(reconcile_effect(invalid, effect(invalid), now=NOW).confirmed)

    def test_timeout_then_unrelated_state_change_only_reobserves_and_confirms(self):
        adapter = RecordingAdapter(outcome=lambda p: effect(p, {}, status=EffectStatus.TIMEOUT))
        core = self.core(adapter)
        first = core.step(now=NOW)
        self.assertEqual(first.reconciliation.status, ReconciliationStatus.RETRYABLE)
        later = snapshot(priority=4, seq=11, snapshot_seq=6, seconds=1)
        later["resources"]["food_kcal"] += 100
        adapter.current = observation(later)
        second = core.step(now=NOW)
        self.assertNotEqual(first.normalized_state.state_hash, second.normalized_state.state_hash)
        self.assertEqual(second.action_count, 0)
        self.assertTrue(second.reconciliation.confirmed)
        self.assertEqual(len(adapter.applied), 1)

    def test_timeout_and_mismatch_budget_stop_without_resend(self):
        for status in (EffectStatus.TIMEOUT, EffectStatus.MISMATCH):
            with self.subTest(status=status):
                directory = self.directory / status.value
                directory.mkdir()
                adapter = RecordingAdapter(outcome=lambda p: effect(
                    p, snapshot(seq=11, snapshot_seq=6, seconds=1), status=status,
                ))
                core = ControllerCore(adapter, policy(maximum=3), intent_store=DurableIntentStore(directory))
                self.assertEqual(core.step(now=NOW).reconciliation.status, ReconciliationStatus.RETRYABLE)
                for index in (1, 2):
                    adapter.current = observation(snapshot(seq=10 + index, snapshot_seq=5 + index, seconds=index))
                    result = core.step(now=NOW)
                    self.assertEqual(result.action_count, 0)
                self.assertEqual(result.reconciliation.status, ReconciliationStatus.BLOCKED)
                self.assertEqual(core.step(now=NOW).decision.reason, "intent_blocked")
                self.assertEqual(len(adapter.applied), 1)

    def test_adapter_timeout_exception_and_unknown_never_resend(self):
        for error in (TimeoutError, RuntimeError):
            with self.subTest(error=error):
                directory = self.directory / error.__name__
                directory.mkdir()
                def fail(proposal):
                    raise error("may already have sent")
                adapter = RecordingAdapter(outcome=fail)
                core = ControllerCore(adapter, policy(), intent_store=DurableIntentStore(directory))
                core.step(now=NOW)
                adapter.current = observation(snapshot(seq=11, snapshot_seq=6, seconds=1))
                core.step(now=NOW)
                self.assertEqual(len(adapter.applied), 1)
        adapter = RecordingAdapter(outcome=lambda p: effect(p, status=EffectStatus.UNKNOWN))
        core = self.core(adapter)
        first = core.step(now=NOW)
        adapter.current = observation(snapshot(priority=4, seq=11, snapshot_seq=6, seconds=1))
        second = core.step(now=NOW)
        self.assertFalse(first.reconciliation.confirmed)
        self.assertIsNone(second.reconciliation)
        self.assertEqual(second.decision.reason, "intent_blocked")

    def test_same_snapshot_after_timeout_stops_instead_of_replaying(self):
        adapter = RecordingAdapter(outcome=lambda p: effect(p, {}, status=EffectStatus.TIMEOUT))
        core = self.core(adapter)
        core.step(now=NOW)
        later = core.step(now=NOW)
        self.assertEqual(later.action_count, 0)
        self.assertFalse(later.reconciliation.confirmed)
        self.assertEqual(len(adapter.applied), 1)

    def test_new_core_same_receipt_stops_after_changed_hash(self):
        adapter = RecordingAdapter(outcome=lambda p: effect(p, {}, status=EffectStatus.TIMEOUT))
        self.core(adapter).step(now=NOW)
        adapter.current = observation(snapshot(seq=11, snapshot_seq=6, seconds=1))
        restarted = ControllerCore(adapter, policy(), intent_store=DurableIntentStore(self.directory))
        second = restarted.step(now=NOW)
        self.assertEqual(second.decision.reason, "intent_already_consumed")
        self.assertEqual(len(adapter.applied), 1)

    def test_durable_store_required_and_failed_fsync_preserves_stop_receipt(self):
        adapter = RecordingAdapter()
        cycle = ControllerCore(adapter, policy()).step(now=NOW)
        self.assertEqual(cycle.decision.reason, "durable_intent_store_required")
        self.assertEqual(adapter.applied, [])
        core = self.core(adapter)
        with mock.patch.object(controller_loop.os, "fsync", side_effect=OSError("failed")):
            cycle = core.step(now=NOW)
        self.assertEqual(cycle.decision.reason, "intent_consumption_failed")
        self.assertEqual(adapter.applied, [])
        self.assertEqual(len(list(self.directory.glob("*.json"))), 1)
        self.assertEqual(self.core(adapter).step(now=NOW).decision.reason, "intent_already_consumed")

    def test_large_structured_snapshot_kept_whole_and_bound_is_finite(self):
        state = snapshot()
        # Structured tracked-cell telemetry represents the historical 605-650
        # KiB range; this is not a truncated state or an opaque padding string.
        template = {"cell": 0, "x": 10, "y": 10, "world_id": 0, "visible": True,
                    "solid": False, "foundation": False, "element_id": "Oxygen",
                    "mass_kg": 1.2, "damage": 0.0, "object_count": 0,
                    "empty_for_blueprint": True, "can_place_ladder": True,
                    "can_place_wire": True, "blueprint_present": False,
                    "blueprint_instance_id": 0, "blueprint_prefab_id": "none",
                    "blueprint_orientation": "none", "blueprint_material_tags": [],
                    "blueprint_in_progress": False, "blueprint_is_utility": False,
                    "empty_fingerprint": "sha256:" + "a" * 64,
                    "fingerprint": "sha256:" + "b" * 64}
        for i in range(1000):
            state["cells"]["by_id"][f"cell_{i}"] = dict(template, cell=i)
        encoded = controller_loop._json_bytes(state)
        self.assertGreater(len(encoded), 605000)
        self.assertLess(len(encoded), controller_loop.MAX_STATE_BYTES)
        normalized = normalize_observation(observation(state), now=NOW)
        self.assertTrue(normalized.eligible, normalized.blockers)
        self.assertEqual(controller_loop._json_bytes(normalized.state), encoded)
        proposal = decide_bounded_action(normalized, policy(), now=NOW).proposal
        self.assertIsNotNone(proposal)
        later = copy.deepcopy(state)
        later.update(seq=11, snapshot_seq=6, snapshot_id="offline-m28:11", ts=(START + dt.timedelta(seconds=1)).isoformat())
        later["duplicants"]["by_instance_id"]["id_123"]["chore_priorities"]["Build"] = 4
        self.assertTrue(reconcile_effect(proposal, effect(proposal, later), now=NOW).confirmed)
        for i in range(1000, 2000):
            state["cells"]["by_id"][f"cell_{i}"] = dict(template, cell=i)
        self.assertGreater(len(controller_loop._json_bytes(state)), controller_loop.MAX_STATE_BYTES)
        self.assert_no_action(state, "state_too_large")
        self.assertFalse(reconcile_effect(proposal, effect(proposal, state), now=NOW).confirmed)

    def test_selected_comparison_is_evaluated_lazily(self):
        cases = [(True, "eq", True), (3, "eq", 3), ("Ada", "eq", "Ada"),
                 (3, "neq", 4), (3, "lt", 4), (3, "lte", 3),
                 (4, "gt", 3), (4, "gte", 4), ([1, 2], "contains", 2),
                 (2, "in", [1, 2])]
        for actual, op, expected in cases:
            with self.subTest(op=op, actual=actual):
                self.assertEqual(Condition("value", op, expected).blockers_for({"value": actual}), [])
        self.assertTrue(Condition("value", "contains", 1).blockers_for({"value": True}))


class GenericControllerGuardTests(unittest.TestCase):
    def normalized(self, **kwargs):
        return normalize_observation(observation(generic_snapshot(**kwargs)), now=NOW)

    def proposal(self):
        result = decide_bounded_action(self.normalized(), generic_policy(), now=NOW)
        self.assertIsNotNone(result.proposal, result.blockers)
        return result.proposal

    def test_json_equality_and_membership_distinguish_nested_booleans(self):
        unequal = [(True, 1), (False, 0), ([True], [1]),
                   ({"value": [True]}, {"value": [1]})]
        equal = [(True, True), (None, None), (1, 1.0),
                 ([{"value": 1}], [{"value": 1.0}])]
        for actual, expected, matches in [(a, b, False) for a, b in unequal] + [(a, b, True) for a, b in equal]:
            with self.subTest(actual=actual, expected=expected):
                self.assertEqual(not Condition("value", "eq", expected).blockers_for({"value": actual}), matches)
                self.assertEqual(not Condition("value", "neq", expected).blockers_for({"value": actual}), not matches)
                self.assertEqual(not Condition("value", "contains", expected).blockers_for({"value": [actual]}), matches)
                self.assertEqual(not Condition("value", "in", [expected]).blockers_for({"value": actual}), matches)
        self.assertEqual(Condition("value", "contains", "oxy").blockers_for({"value": "oxygen"}), [])
        self.assertTrue(Condition("value", "contains", 1).blockers_for({"value": {"1": True}}))

    def test_invalid_condition_values_block_before_proposal_hashing(self):
        for value in (float("nan"), float("inf"), object(), {1: "bad"}, {"bad": object()}):
            for position in ("preconditions", "expected_effects"):
                with self.subTest(value=type(value).__name__, position=position):
                    invalid = dataclasses.replace(generic_policy(), **{position: (Condition("active", "eq", value),)})
                    decision = decide_bounded_action(self.normalized(), invalid, now=NOW)
                    self.assertIsNone(decision.proposal)
                    self.assertEqual(decision.reason, "action_policy_invalid")
                    self.assertTrue(any("condition_value" in b for b in decision.blockers))
        valid = dataclasses.replace(generic_policy(), args={"number": 1.5, "nested": [True, None]})
        self.assertIsNotNone(decide_bounded_action(self.normalized(), valid, now=NOW).proposal)

    def test_invalid_argument_keys_and_nonfinite_values_are_bounded(self):
        for args in ({1: "bad"}, {True: "bad"}, {"nested": {0: "bad"}}, {"v": float("inf")}, {"v": object()}):
            with self.subTest(args=str(args)):
                decision = decide_bounded_action(self.normalized(), dataclasses.replace(generic_policy(), args=args), now=NOW)
                self.assertIsNone(decision.proposal)
                self.assertIn("action_args_non_finite_or_non_json", decision.blockers)
        with mock.patch.object(controller_loop, "_json_bytes", side_effect=ValueError("cannot encode")):
            self.assertIn("condition_value_not_canonicalizable", Condition("active", "eq", True).declaration_blockers())
            self.assertIn("action_args_not_canonicalizable", controller_loop._policy_blockers(generic_policy()))

    def test_satisfied_generic_effect_is_noop_only_after_all_gates(self):
        decision = decide_bounded_action(self.normalized(active=True), generic_policy(), now=NOW)
        self.assertEqual(decision.reason, "expected_effects_already_satisfied")
        self.assertIsNone(decision.proposal)
        self.assertIsNotNone(decide_bounded_action(self.normalized(), generic_policy(), now=NOW).proposal)
        second_effect = dataclasses.replace(generic_policy(), expected_effects=(Condition("active", "eq", True), Condition("game.paused", "eq", False)))
        self.assertIsNotNone(decide_bounded_action(self.normalized(active=True), second_effect, now=NOW).proposal)
        for state, freshness in ((generic_snapshot(active=True), Freshness.STALE),
                                 (dict(generic_snapshot(active=True), game={"paused": False}), Freshness.FRESH)):
            normalized = normalize_observation(observation(state, freshness=freshness), now=NOW)
            result = decide_bounded_action(normalized, generic_policy(), now=NOW)
            self.assertIsNone(result.proposal)
            self.assertNotEqual(result.reason, "expected_effects_already_satisfied")

    def test_satisfied_generic_intent_latches_without_later_send(self):
        adapter = RecordingAdapter(current=observation(generic_snapshot(active=True)), outcome=generic_effect)
        core = ControllerCore(adapter, generic_policy())
        self.assertEqual(core.step(now=NOW).decision.reason, "expected_effects_already_satisfied")
        adapter.current = observation(generic_snapshot(active=False, seq=11, snapshot_seq=6, seconds=1))
        self.assertEqual(core.step(now=NOW).decision.reason, "intent_already_satisfied")
        self.assertEqual(adapter.applied, [])

    def test_generic_effect_rejects_bad_shape_keys_values_and_oversize(self):
        proposal = self.proposal()
        bad_payloads = [[], None, "bad", {"active": True, 1: "bad"},
                        {"active": True, "nested": {False: 1}},
                        {"active": True, "bad": float("nan")},
                        {"active": True, "padding": "x" * (controller_loop.MAX_STATE_BYTES + 1)}]
        for payload in bad_payloads:
            with self.subTest(payload_type=type(payload).__name__):
                observed = EffectObservation(proposal.idempotency_key, EffectStatus.APPLIED, Freshness.FRESH, ObservationQuality.SAFE, payload)
                self.assertEqual(reconcile_effect(proposal, observed, now=NOW).status, ReconciliationStatus.BLOCKED)
        valid = generic_effect(proposal, {"active": True, "padding": "x" * (128 * 1024)})
        self.assertTrue(reconcile_effect(proposal, valid, now=NOW).confirmed)
        with mock.patch.object(controller_loop, "_json_bytes", side_effect=ValueError("cannot encode")):
            self.assertEqual(reconcile_effect(proposal, valid, now=NOW).recovery_reason, "effect_payload_not_canonicalizable")

    def test_generic_clock_metadata_has_native_shape_and_finite_controller_bound(self):
        proposal = self.proposal()
        class IntSubclass(int):
            pass
        class FloatSubclass(float):
            pass
        for name, value in [("cycle", "bogus"), ("cycle", True), ("cycle", 0), ("cycle", 11.0),
                            ("cycle", controller_loop.MAX_CYCLE_VALUE + 1), ("cycle", 10**400),
                            ("game_time_cycles", IntSubclass(11)), ("game_time_cycles", FloatSubclass(11.25)),
                            ("game_time_cycles", float("inf")), ("game_time_cycles", 10.99), ("game_time_cycles", 12),
                            ("game_time_cycles", controller_loop.MAX_CYCLE_VALUE + 1)]:
            with self.subTest(name=name, value_type=type(value).__name__):
                state = generic_snapshot(active=True)
                state[name] = value
                self.assertEqual(reconcile_effect(proposal, generic_effect(proposal, state), now=NOW).status, ReconciliationStatus.BLOCKED)
                self.assertFalse(normalize_observation(observation(state), now=NOW).eligible)
        for cycle, clock in ((11, 11), (11, 11.25), (controller_loop.MAX_CYCLE_VALUE, controller_loop.MAX_CYCLE_VALUE)):
            state = generic_snapshot(active=True)
            state.update(cycle=cycle, game_time_cycles=clock)
            self.assertTrue(normalize_observation(observation(state), now=NOW).eligible)
            self.assertTrue(reconcile_effect(proposal, generic_effect(proposal, state), now=NOW).confirmed)
        malformed = generic_snapshot(active=True)
        malformed["quality"]["cycle"]["source"] = "wrong"
        self.assertEqual(reconcile_effect(proposal, generic_effect(proposal, malformed), now=NOW).status, ReconciliationStatus.BLOCKED)

    def test_missing_or_untrusted_mismatch_evidence_is_terminal(self):
        proposal = self.proposal()
        for observed in (generic_effect(proposal, {}), generic_effect(proposal, {}, status=EffectStatus.MISMATCH),
                         generic_effect(proposal, {"active": False}, status=EffectStatus.MISMATCH, freshness=Freshness.STALE),
                         generic_effect(proposal, {"active": False}, status=EffectStatus.MISMATCH, quality=ObservationQuality.PARTIAL)):
            self.assertEqual(reconcile_effect(proposal, observed, now=NOW).status, ReconciliationStatus.BLOCKED)
        for status in (EffectStatus.APPLIED, EffectStatus.MISMATCH):
            result = reconcile_effect(proposal, generic_effect(proposal, {"active": False}, status=status), now=NOW)
            self.assertEqual(result.status, ReconciliationStatus.RETRYABLE)
            self.assertEqual(result.recovery.next_step, "reobserve")
        self.assertFalse(reconcile_effect(proposal, generic_effect(proposal, {"active": 1}), now=NOW).confirmed)
        contains = dataclasses.replace(proposal, expected_effects=(Condition("active", "contains", "ready"),))
        for payload in ({"active": True}, {"active": 3}):
            result = reconcile_effect(contains, generic_effect(contains, payload), now=NOW)
            self.assertEqual(result.status, ReconciliationStatus.BLOCKED)
            self.assertIn("condition_value_mismatch", result.recovery_reason)
        self.assertTrue(reconcile_effect(contains, generic_effect(contains, {"active": ["ready"]}), now=NOW).confirmed)

    def test_invalid_expected_condition_cannot_become_effect_confirmation(self):
        proposal = self.proposal()
        for expected in ((), (Condition("active", "eq", object()),), (Condition("active", "eq", float("nan")),)):
            invalid = dataclasses.replace(proposal, expected_effects=expected)
            self.assertEqual(reconcile_effect(invalid, generic_effect(invalid), now=NOW).status, ReconciliationStatus.BLOCKED)

    def test_malformed_direct_reply_never_falls_back_or_resends(self):
        adapter = RecordingAdapter(current=observation(generic_snapshot()))
        def malformed(proposal):
            data = {"active": True}
            data["recursive"] = data
            return {"idempotency_key": proposal.idempotency_key, "status": "applied", "freshness": "fresh", "quality": "safe", "payload": data}
        adapter.outcome = malformed
        adapter.observe_effect = mock.Mock(side_effect=AssertionError("unexpected effect read"))
        core = ControllerCore(adapter, generic_policy())
        self.assertEqual(core.step(now=NOW).reconciliation.status, ReconciliationStatus.BLOCKED)
        adapter.current = observation(generic_snapshot(seq=11, snapshot_seq=6, seconds=1))
        self.assertEqual(core.step(now=NOW).decision.reason, "intent_blocked")
        adapter.observe_effect.assert_not_called()
        self.assertEqual(len(adapter.applied), 1)


class ControllerSequenceTests(unittest.TestCase):
    setUp = PriorityControllerTests.setUp

    def adapter(self, *, active=False, outcome=generic_effect):
        return RecordingAdapter(current=observation(generic_snapshot(active=active)), outcome=outcome)

    def policies(self):
        return (generic_policy(), generic_policy(action="offline_finish"))

    def assert_stopped(self, sequence, adapter):
        counts = (adapter.observed, len(adapter.applied))
        with self.assertRaises(StopIteration):
            sequence.step(now=NOW)
        self.assertEqual((adapter.observed, len(adapter.applied)), counts)

    def test_confirmed_sequence_dispatches_one_intent_per_call(self):
        adapter = self.adapter()
        sequence = ControllerSequence(adapter, self.policies(), max_steps=2)
        for index in (1, 2):
            result = sequence.step(now=NOW)
            self.assertTrue(result.reconciliation.confirmed)
            self.assertEqual(result.action_count, 1)
            self.assertEqual(sequence.policy_index, index)
        self.assertTrue(sequence.completed)
        self.assertEqual(sequence.stop_reason, "sequence_completed")
        self.assertEqual([p.action for p in adapter.applied], [p.action for p in self.policies()])
        self.assert_stopped(sequence, adapter)

    def test_whole_sequence_prevalidates_policies_and_budgets_before_adapter(self):
        adapter = self.adapter()
        invalid = [dataclasses.replace(generic_policy(), action=""),
                   dataclasses.replace(generic_policy(), args={1: "bad"}),
                   dataclasses.replace(generic_policy(), expected_effects=()),
                   dataclasses.replace(generic_policy(), expected_effects=(Condition("active", "eq", float("nan")),)),
                   dataclasses.replace(generic_policy(), max_attempts=True)]
        for bad in invalid:
            for index in (0, 1):
                chosen = list(self.policies())
                chosen[index] = bad
                with self.subTest(index=index), self.assertRaisesRegex(ValueError, f"policy invalid at index {index}:"):
                    ControllerSequence(adapter, chosen, max_steps=3)
        for maximum in (True, 0, -1, 1.5, "2"):
            with self.assertRaises(ValueError):
                ControllerSequence(adapter, self.policies(), max_steps=maximum)
        for chosen in ((), [generic_policy(), generic_policy()], "bad", None):
            with self.assertRaises(ValueError):
                ControllerSequence(adapter, chosen, max_steps=2)
        self.assertEqual(adapter.observed, 0)
        self.assertEqual(adapter.applied, [])

    def test_named_selection_is_ordered_frozen_and_validated(self):
        adapter = self.adapter()
        catalog = {"start": generic_policy().as_dict(), "finish": self.policies()[1], "unused": None}
        selected = ["start", "finish"]
        sequence = ControllerSequence.from_named_policies(adapter, catalog, selected, max_steps=2)
        catalog["start"]["args"]["target"] = "changed"
        catalog.clear()
        selected.reverse()
        sequence.step(now=NOW)
        sequence.step(now=NOW)
        self.assertEqual([p.action for p in adapter.applied], ["offline_toggle", "offline_finish"])
        self.assertEqual(adapter.applied[0].args["target"], "fixture")
        self.assert_stopped(sequence, adapter)
        clean = self.adapter()
        catalog = {"start": generic_policy(), "alias": generic_policy()}
        for names in ((), "start", b"start", None, {"start"}, ("missing",), ("start", "missing"),
                      ("start", "start"), ("",), (True,), ([],), ("start", "alias")):
            with self.subTest(names=str(names)), self.assertRaises(ValueError):
                ControllerSequence.from_named_policies(clean, catalog, names, max_steps=2)
        for bad_catalog in ({}, None, [], {"start": None}, {"start": {}}):
            with self.assertRaises(ValueError):
                ControllerSequence.from_named_policies(clean, bad_catalog, ("start",), max_steps=2)
        self.assertEqual(clean.observed, 0)
        self.assertEqual(clean.applied, [])

    def test_cancel_before_and_between_steps_prevents_all_future_adapter_access(self):
        for completed_steps in (0, 1):
            adapter = self.adapter()
            sequence = ControllerSequence(adapter, self.policies(), max_steps=3)
            for _ in range(completed_steps):
                sequence.step(now=NOW)
            sequence.cancel()
            sequence.cancel()
            self.assertEqual(sequence.stop_reason, "sequence_cancelled")
            self.assertEqual(sequence.policy_index, completed_steps)
            self.assert_stopped(sequence, adapter)
            self.assertEqual(len(adapter.applied), completed_steps)

    def test_step_budget_and_completion_latch_without_extra_sampling(self):
        for maximum, terminal in ((1, "sequence_step_budget_exhausted"), (2, "sequence_completed")):
            adapter = self.adapter()
            sequence = ControllerSequence(adapter, self.policies(), max_steps=maximum)
            for _ in range(maximum):
                sequence.step(now=NOW)
            sequence.cancel()
            self.assertEqual(sequence.stop_reason, terminal)
            self.assert_stopped(sequence, adapter)
        adapter = self.adapter()
        adapter.current = observation(generic_snapshot(), freshness=Freshness.UNKNOWN)
        sequence = ControllerSequence(adapter, self.policies(), max_steps=2)
        for _ in range(2):
            self.assertEqual(sequence.step(now=NOW).action_count, 0)
        self.assertEqual(sequence.stop_reason, "sequence_step_budget_exhausted")
        self.assertEqual(adapter.applied, [])
        self.assert_stopped(sequence, adapter)

    def test_unknown_or_invalid_effect_stops_before_successor(self):
        for outcome in (lambda p: generic_effect(p, status=EffectStatus.UNKNOWN),
                        lambda p: generic_effect(p, {}),
                        lambda p: generic_effect(p, {"active": False}, status=EffectStatus.MISMATCH, freshness=Freshness.STALE)):
            adapter = self.adapter(outcome=outcome)
            sequence = ControllerSequence(adapter, self.policies(), max_steps=3)
            self.assertEqual(sequence.step(now=NOW).reconciliation.status, ReconciliationStatus.BLOCKED)
            previous = sequence.stop_reason
            sequence.cancel()
            self.assertEqual(sequence.stop_reason, previous)
            self.assertEqual(sequence.policy_index, 0)
            self.assertEqual(len(adapter.applied), 1)
            self.assert_stopped(sequence, adapter)

    def test_timeout_and_mismatch_only_reobserve_before_advancing(self):
        for status in (EffectStatus.TIMEOUT, EffectStatus.MISMATCH):
            adapter = self.adapter(outcome=lambda p: generic_effect(p, {"active": False}, status=status))
            sequence = ControllerSequence(adapter, self.policies(), max_steps=3)
            self.assertEqual(sequence.step(now=NOW).reconciliation.status, ReconciliationStatus.RETRYABLE)
            adapter.current = observation(generic_snapshot(active=True, seq=11, snapshot_seq=6, seconds=1))
            result = sequence.step(now=NOW)
            self.assertEqual(result.action_count, 0)
            self.assertTrue(result.reconciliation.confirmed)
            self.assertEqual(sequence.policy_index, 1)
            self.assertEqual(len(adapter.applied), 1)
            self.assertEqual(sequence.step(now=NOW).decision.reason, "expected_effects_already_satisfied")
            self.assertTrue(sequence.completed)
            self.assertEqual(len(adapter.applied), 1)
            self.assert_stopped(sequence, adapter)

    def test_exhausted_observation_budget_stops_without_resend_or_successor(self):
        adapter = self.adapter(outcome=lambda p: generic_effect(p, {"active": False}))
        sequence = ControllerSequence(adapter, self.policies(), max_steps=5)
        for index in range(3):
            adapter.current = observation(generic_snapshot(seq=10 + index, snapshot_seq=5 + index, seconds=index))
            result = sequence.step(now=NOW)
            self.assertEqual(result.action_count, 1 if index == 0 else 0)
        self.assertEqual(result.reconciliation.status, ReconciliationStatus.BLOCKED)
        self.assertIn("recovery_attempt_limit_exhausted", sequence.stop_reason)
        self.assertEqual(len(adapter.applied), 1)
        self.assertEqual(sequence.policy_index, 0)
        self.assert_stopped(sequence, adapter)

    def test_generic_and_priority_noops_advance_without_effect_confirmation(self):
        adapter = self.adapter(active=True)
        adapter.current = observation(generic_snapshot(active=True, priority=4))
        sequence = ControllerSequence(adapter, (generic_policy(), policy()), max_steps=2, intent_store=self.store)
        for reason in ("expected_effects_already_satisfied", "target_already_at_priority"):
            result = sequence.step(now=NOW)
            self.assertEqual(result.decision.reason, reason)
            self.assertIsNone(result.reconciliation)
            self.assertEqual(result.action_count, 0)
        self.assertTrue(sequence.completed)
        self.assertEqual(adapter.applied, [])
        self.assertEqual(list(self.directory.glob("*.json")), [])
        self.assert_stopped(sequence, adapter)

    def test_priority_sequence_shares_durable_store_and_reobserves_only(self):
        adapter = self.adapter(outcome=lambda p: effect(p, {}, status=EffectStatus.TIMEOUT))
        sequence = ControllerSequence.from_named_policies(
            adapter, {"priority": policy(), "finish": generic_policy()}, ("priority", "finish"),
            max_steps=3, intent_store=self.store,
        )
        self.assertEqual(sequence.step(now=NOW).reconciliation.status, ReconciliationStatus.RETRYABLE)
        self.assertEqual(len(list(self.directory.glob("*.json"))), 1)
        adapter.current = observation(generic_snapshot(priority=4, seq=11, snapshot_seq=6, seconds=1))
        result = sequence.step(now=NOW)
        self.assertTrue(result.reconciliation.confirmed)
        self.assertEqual(result.action_count, 0)
        adapter.outcome = generic_effect
        self.assertTrue(sequence.step(now=NOW).reconciliation.confirmed)
        self.assertTrue(sequence.completed)
        self.assertEqual(len(adapter.applied), 2)
        self.assertEqual(len(list(self.directory.glob("*.json"))), 2)
        self.assert_stopped(sequence, adapter)

    def test_restart_with_consumed_intent_stops_entire_sequence(self):
        adapter = self.adapter(outcome=lambda p: effect(p, {}, status=EffectStatus.TIMEOUT))
        policies = (policy(), generic_policy())
        original = ControllerSequence(adapter, policies, max_steps=3, intent_store=self.store)
        original.step(now=NOW)
        adapter.current = observation(generic_snapshot(seq=11, snapshot_seq=6, seconds=1))
        resumed = ControllerSequence(adapter, policies, max_steps=3, intent_store=DurableIntentStore(self.directory))
        self.assertEqual(resumed.step(now=NOW).decision.reason, "intent_already_consumed")
        self.assertEqual(resumed.stop_reason, "intent_already_consumed")
        self.assertFalse(resumed.completed)
        self.assertEqual(len(adapter.applied), 1)
        self.assert_stopped(resumed, adapter)

    def test_missing_store_and_invalid_priority_successor_reject_before_dispatch(self):
        adapter = self.adapter()
        with self.assertRaisesRegex(ValueError, "durable_intent_store_required"):
            ControllerSequence(adapter, (generic_policy(), policy()), max_steps=2)
        invalid = dataclasses.replace(policy(), args=dict(policy().args, priority=5))
        with self.assertRaisesRegex(ValueError, "policy invalid at index 1"):
            ControllerSequence(adapter, (generic_policy(), invalid), max_steps=2, intent_store=self.store)
        self.assertEqual(adapter.observed, 0)
        self.assertEqual(adapter.applied, [])

    def test_failed_receipt_stops_sequence_without_dispatch(self):
        adapter = self.adapter()
        sequence = ControllerSequence(adapter, (policy(), generic_policy()), max_steps=2, intent_store=self.store)
        with mock.patch.object(controller_loop.os, "fsync", side_effect=OSError("failed")):
            self.assertEqual(sequence.step(now=NOW).decision.reason, "intent_consumption_failed")
        self.assertEqual(sequence.stop_reason, "intent_consumption_failed")
        self.assertEqual(adapter.applied, [])
        self.assertEqual(len(list(self.directory.glob("*.json"))), 1)
        self.assert_stopped(sequence, adapter)

    def test_reentrant_step_cannot_skip_or_dispatch_a_successor(self):
        adapter = self.adapter()
        sequence = ControllerSequence(adapter, self.policies(), max_steps=2)
        def nested(_proposal):
            with self.assertRaisesRegex(RuntimeError, "sequence_step_in_progress"):
                sequence.step(now=NOW)
        adapter.before_apply = nested
        self.assertTrue(sequence.step(now=NOW).reconciliation.confirmed)
        self.assertEqual(sequence.policy_index, 1)
        self.assertEqual(adapter.observed, 1)
        self.assertEqual(len(adapter.applied), 1)


def main():
    started = dt.datetime.now(dt.timezone.utc).isoformat()
    modules = (controller_loop, advanced_actions, field_contracts, state_quality)
    origins = {m.__name__: str(pathlib.Path(m.__file__).resolve()) for m in modules}
    for origin in origins.values():
        if not pathlib.Path(origin).is_relative_to(ROOT.resolve()):
            raise RuntimeError("candidate_module_origin_mismatch")
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    files = [pathlib.Path(__file__).resolve()] + [pathlib.Path(m.__file__).resolve() for m in modules]
    summary = {
        "schema": "oni.controller-core-test-summary.v2", "tests_run": result.testsRun,
        "failures": len(result.failures), "errors": len(result.errors),
        "successful": result.wasSuccessful(), "live_admission": False,
        "started_at": started, "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "module_origins": origins, "dont_write_bytecode": sys.dont_write_bytecode,
        "source_hashes": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
        "max_state_bytes": controller_loop.MAX_STATE_BYTES,
    }
    print(json.dumps(summary, ensure_ascii=True, sort_keys=True))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
