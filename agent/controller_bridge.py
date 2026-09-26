"""Single-leaf controller adapter for an existing approval-gated Colony Plan.

The adapter never approves, executes, advances a successor, or changes live
admission. Callers explicitly select a plan/leaf and trusted runtime identity.
Effect inspection remains available after a core's finite observation budget.
"""
from __future__ import annotations

import os
import pathlib
import re
import stat
import tempfile
from typing import Any, Mapping

from agent.canonical_hash import canonical_hash, loads_strict
from agent.colony_plan import (PLAN_COMMAND_SOURCE, SAFE_ID, advance_colony_plan,
    load_active_plan, plan_directory, plan_leaf_contract_hash,
    reconcile_colony_plan_leaf, mark_colony_plan_leaf_already_satisfied)
from agent.command_custody import lifecycle_custody, read_stable_bytes
from agent.command_queue import COMMAND_DIRS
from agent.controller_loop import (ActionPolicy, ActionProposal, EffectObservation,
    ControllerCore, ControllerCycle, DurableIntentStore, EffectStatus, Freshness,
    Observation, ObservationQuality, ReconciliationStatus, _plain)
from agent.decision import validate_decision_frame
from agent.runtime_confirmation import archived_confirmation_records, assess_command_confirmation
from agent.state_quality import canonical_state_hash, snapshot_trust_blockers

IDENTITY_FIELDS = ("session_id", "save_id_hash", "world_id", "game_build", "mod_version")
SHAPE_FIELDS = ("action", "args", "preconditions", "expected_effects")
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_CAPTURE_BYTES = 128 * 1024 * 1024


class BridgeAdapterError(RuntimeError):
    pass


def _object(raw: bytes) -> dict[str, Any]:
    value = loads_strict(raw.decode("utf-8-sig"))
    if not isinstance(value, dict):
        raise BridgeAdapterError("object_required")
    return value


def _shape(value: Mapping[str, Any]) -> dict[str, Any]:
    # Plans retain optional condition annotations; Core serializes executable
    # conditions. Compare both through that same representation, retaining
    # strict JSON types and the full args. The guarded leaf hash still binds
    # the original plan document without discarding any annotations.
    normalized = ActionPolicy.from_hint(value).as_dict()
    return {key: normalized[key] for key in SHAPE_FIELDS}


class ColonyPlanControllerAdapter:
    requires_durable_intent_store = True

    def __init__(self, bridge_dir: pathlib.Path, *, plan_id: str, step_id: str,
                 expected_identity: Mapping[str, Any], allow_pending_commands: bool = False,
                 final_apply_guard: Any = None):
        self.root = pathlib.Path(os.path.abspath(pathlib.Path(bridge_dir).expanduser()))
        self.plan_id = plan_id
        self.step_id = step_id
        self.plan_path = plan_directory(self.root, plan_id)
        if not isinstance(step_id, str) or SAFE_ID.fullmatch(step_id) is None:
            raise BridgeAdapterError("step_id_invalid")
        if set(expected_identity) != set(IDENTITY_FIELDS):
            raise BridgeAdapterError("complete_runtime_identity_required")
        self.identity = dict(expected_identity)
        if (type(self.identity["world_id"]) is not int or self.identity["world_id"] < 0
                or any(not isinstance(self.identity[key], str) or not self.identity[key] for key in IDENTITY_FIELDS if key != "world_id")
                or re.fullmatch(r"sha256:[0-9a-f]{64}", self.identity["save_id_hash"]) is None):
            raise BridgeAdapterError("runtime_identity_invalid")
        self.allow_pending_commands = allow_pending_commands is True
        if final_apply_guard is not None and not callable(final_apply_guard):
            raise TypeError("final_apply_guard must be callable")
        # The coordinator supplies this same guard to ControllerCore and to
        # the custody-held adapter path.  The second invocation closes the
        # check-to-write gap while the lifecycle lock is held.
        self.final_apply_guard = final_apply_guard
        self._submitted = False
        self.last_blocker: str | None = None
        self._anchor_identities = self._directories(self.root)
        self._root_identity = self._anchor_identities[self.root]

    def _directories(self, path: pathlib.Path) -> dict[pathlib.Path, tuple[int, int]]:
        result = {}
        for directory in reversed((path, *path.parents)):
            metadata = directory.lstat()
            if (not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)
                    or int(getattr(metadata, "st_file_attributes", 0)) & 0x400):
                raise BridgeAdapterError("directory_link_or_type_invalid")
            result[directory] = (metadata.st_dev, metadata.st_ino)
        if hasattr(self, "_root_identity") and result.get(self.root) != self._root_identity:
            raise BridgeAdapterError("bridge_root_identity_changed")
        if hasattr(self, "_anchor_identities") and any(result.get(path) != identity
                for path, identity in self._anchor_identities.items()):
            raise BridgeAdapterError("bridge_ancestor_identity_changed")
        return result

    def _read(self, path: pathlib.Path, limit: int = MAX_JSON_BYTES) -> bytes:
        if not path.is_relative_to(self.root):
            raise BridgeAdapterError("path_outside_bridge")
        before = self._directories(path.parent)
        raw, _, _ = read_stable_bytes(path, label="controller evidence", max_bytes=limit)
        if self._directories(path.parent) != before:
            raise BridgeAdapterError("evidence_ancestor_changed")
        return raw

    def _plan(self) -> tuple[dict[str, Any], dict[str, Any]]:
        paths = [self.root / "colony-plans/active.json", self.plan_path / "current.json"]
        captured = {path: self._read(path) for path in paths}
        current = _object(captured[paths[1]])
        revision = current.get("revision")
        if type(revision) is not int or not 1 <= revision <= 1_000_000:
            raise BridgeAdapterError("plan_revision_invalid")
        revision_path = self.plan_path / "revisions" / f"{revision:06d}.json"
        captured[revision_path] = self._read(revision_path)
        plan = load_active_plan(self.root, self.plan_id)
        if canonical_hash(plan) != canonical_hash(current) or any(self._read(path) != data for path, data in captured.items()):
            raise BridgeAdapterError("plan_changed_during_read")
        if any(plan.get(key) != self.identity[key] for key in ("session_id", "save_id_hash", "world_id")):
            raise BridgeAdapterError("plan_runtime_identity_mismatch")
        steps = [step for step in plan.get("steps", []) if step.get("id") == self.step_id]
        if len(steps) != 1:
            raise BridgeAdapterError("selected_step_missing_or_ambiguous")
        return plan, steps[0]

    def policy(self) -> ActionPolicy:
        """Use one selected real plan leaf with three bounded effect observations."""
        _, step = self._plan()
        return ActionPolicy.from_hint(step, max_attempts=3)

    def _observation(self) -> Observation:
        paths = [self.root / name for name in ("decision-frame.json", "latest_state.json", "health.json")]
        captured = {path: self._read(path) for path in paths}
        frame, state, health = (_object(captured[path]) for path in paths)
        validate_decision_frame(frame)
        expected = {key: state.get(key) for key in ("session_id", "save_id_hash", "world_id", "snapshot_id", "seq", "snapshot_seq", "ts")}
        expected["state_hash"] = canonical_state_hash(state)
        if (canonical_hash(frame.get("source")) != canonical_hash(expected)
                or frame.get("trust", {}).get("status") != "trusted"
                or health.get("status") != "ok"
                or any(health.get("source", {}).get(key) != expected[key] for key in ("session_id", "snapshot_id", "state_hash"))
                or canonical_hash({key: state.get(key) for key in IDENTITY_FIELDS}) != canonical_hash(self.identity)
                or snapshot_trust_blockers(state)):
            raise BridgeAdapterError("observation_not_trusted_or_bound")
        if any(self._read(path) != data for path, data in captured.items()):
            raise BridgeAdapterError("observation_generation_changed")
        return Observation(state["snapshot_id"], Freshness.FRESH, ObservationQuality.SAFE,
                           state, source="bridge_committed_bundle", observed_at=state["ts"])

    def observe(self) -> Observation:
        try:
            self.last_blocker = None
            return self._observation()
        except Exception as exc:
            self.last_blocker = str(exc) if isinstance(exc, BridgeAdapterError) else "bridge_observation_unreadable"
            return Observation("", Freshness.UNKNOWN, ObservationQuality.UNSAFE, {}, source="bridge_committed_bundle")

    def _effect(self, proposal: ActionProposal, status: EffectStatus, payload=None) -> EffectObservation:
        accepted = status is EffectStatus.APPLIED
        return EffectObservation(proposal.idempotency_key, status,
            Freshness.FRESH if accepted else Freshness.UNKNOWN,
            ObservationQuality.SAFE if accepted else ObservationQuality.UNKNOWN,
            payload or {}, source="bridge_command_confirmation")

    def apply(self, proposal: ActionProposal) -> EffectObservation:
        if not self.allow_pending_commands or self._submitted:
            return self._effect(proposal, EffectStatus.REJECTED)
        self._submitted = True  # Any uncertain exception permanently consumes this adapter's send.
        try:
            if proposal.attempt != 1:
                raise BridgeAdapterError("initial_proposal_required")
            self._directories(self.root)
            with lifecycle_custody(self.root, expected_bridge_identity=self._root_identity):
                observation = self._observation()
                plan, step = self._plan()
                shape = proposal.as_dict()
                if (plan.get("open_command_id") is not None or step.get("command_id") is not None
                        or step.get("status") != "waiting"
                        or canonical_hash(_shape(step)) != canonical_hash(_shape(shape))
                        or observation.observation_id != proposal.source_observation_id
                        or canonical_state_hash(observation.as_dict()["payload"]) != proposal.state_hash):
                    raise BridgeAdapterError("proposal_does_not_match_selected_leaf")
                if self.final_apply_guard is not None:
                    try:
                        if self.final_apply_guard(proposal) is not True:
                            raise BridgeAdapterError("final_apply_guard_rejected")
                    except BridgeAdapterError:
                        raise
                    except Exception as exc:
                        raise BridgeAdapterError("final_apply_guard_unreadable") from exc
                advance_colony_plan(self.root, self.plan_id, expected_step_id=self.step_id,
                    expected_state_hash=proposal.state_hash, expected_step_hash=plan_leaf_contract_hash(step))
            # A successful function return is not a queue or effect witness.
            return self.observe_effect(proposal)
        except Exception as exc:
            self.last_blocker = str(exc) if isinstance(exc, BridgeAdapterError) else "plan_publication_not_confirmed"
            return self._effect(proposal, EffectStatus.UNKNOWN)

    def _command(self, proposal: ActionProposal):
        _, step = self._plan()
        command_id = step.get("command_id")
        if not isinstance(command_id, str) or re.fullmatch(r"[A-Za-z0-9._-]{1,160}", command_id) is None:
            raise BridgeAdapterError("selected_leaf_command_missing")
        matches = []
        for stage in COMMAND_DIRS:
            directory = self.root / "commands" / stage
            self._directories(directory)
            entries = list(directory.glob("*.json"))
            if len(entries) > 2048:
                raise BridgeAdapterError("command_scan_limit")
            for path in entries:
                raw = self._read(path)
                command = _object(raw)
                if path.stem == command_id or command.get("id") == command_id:
                    if path.stem != command_id or command.get("id") != command_id:
                        raise BridgeAdapterError("command_path_identity_mismatch")
                    matches.append((stage, path, raw, command))
        if len(matches) != 1:
            raise BridgeAdapterError("command_location_missing_or_ambiguous")
        stage, path, raw, command = matches[0]
        source = command.get("source", {})
        original = proposal.as_dict()
        if (command.get("mode") != "planned" or command.get("requires_approval") is not True
                or source.get("kind") != PLAN_COMMAND_SOURCE or source.get("plan_id") != self.plan_id
                or source.get("step_id") != self.step_id
                or command.get("state_hash") != proposal.state_hash
                or command.get("source_snapshot_id") != proposal.source_observation_id
                or any(command.get(key) != self.identity[key] for key in ("session_id", "save_id_hash", "world_id"))
                or canonical_hash(_shape(command)) != canonical_hash(_shape(original))
                or canonical_hash(_shape(step)) != canonical_hash(_shape(original))):
            raise BridgeAdapterError("command_proposal_binding_mismatch")
        return stage, path, raw, command

    def observe_effect(self, proposal: ActionProposal) -> EffectObservation:
        """Read only; never advance the plan or consume another intent."""
        try:
            self.last_blocker = None
            stage, path, raw, command = self._command(proposal)
            statuses = {"pending": {"pending_human_approval"},
                        "approving": {"pending_human_approval", "approved"},
                        "approved": {"approved", "executing"}, "executing": {"executing"},
                        "confirming": {"executed_pending_snapshot", "confirmed"}}
            if (stage in statuses and command.get("status") in statuses[stage]
                    and command.get("classification") != "outcome_unknown"
                    and command.get("result", {}).get("classification") != "outcome_unknown"):
                return self._effect(proposal, EffectStatus.PENDING)
            if stage == "rejected" and command.get("status") == "rejected":
                return self._effect(proposal, EffectStatus.REJECTED)
            if stage != "done" or command.get("status") != "confirmed":
                raise BridgeAdapterError("command_not_confirmed")
            captured = {path: raw}
            session_root = self.root / "sessions"
            self._directories(session_root)
            certificates = sorted(session_root.glob("*/action-outcomes.json"))
            if len(certificates) > 1024:
                raise BridgeAdapterError("certificate_scan_limit")
            root_certificate = self.root / "action-outcomes.json"
            root_present = root_certificate.exists() or root_certificate.is_symlink()
            if root_present:
                captured[root_certificate] = self._read(root_certificate)
            for certificate in certificates:
                data = self._read(certificate)
                captured[certificate] = data
                if _object(data).get("source", {}).get("session_id") == self.identity["session_id"]:
                    archive = certificate.parent / "state.jsonl"
                    captured[archive] = self._read(archive, MAX_ARCHIVE_BYTES)
                    for line in captured[archive].decode("utf-8-sig").splitlines():
                        if line.strip():
                            _object(line.encode("utf-8"))
                if sum(len(value) for value in captured.values()) > MAX_CAPTURE_BYTES:
                    raise BridgeAdapterError("confirmation_capture_limit")
            # Existing confirmation code sees a frozen copy of captured real
            # evidence, preventing its legacy path reads from following links
            # or observing a different generation midway through assessment.
            with tempfile.TemporaryDirectory(prefix="oni-confirmation-capture-") as temporary:
                frozen = pathlib.Path(temporary)
                for original, data in captured.items():
                    destination = frozen / original.relative_to(self.root)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(data)
                records = archived_confirmation_records(frozen, command)
                assessment = assess_command_confirmation(frozen, command, records, directory="done")
            if (root_present != (root_certificate.exists() or root_certificate.is_symlink())
                    or sorted(session_root.glob("*/action-outcomes.json")) != certificates
                    or any(self._read(original, MAX_ARCHIVE_BYTES if original.suffix == ".jsonl" else MAX_JSON_BYTES) != data for original, data in captured.items())
                    or self._command(proposal)[0:3] != (stage, path, raw)):
                raise BridgeAdapterError("confirmation_evidence_changed")
            witness = assessment.get("witness_state")
            if (assessment.get("accepted") is not True or not isinstance(witness, dict)
                    or canonical_hash({key: witness.get(key) for key in IDENTITY_FIELDS}) != canonical_hash(self.identity)
                    or snapshot_trust_blockers(witness)):
                raise BridgeAdapterError("confirmation_not_accepted_or_fresh")
            self.last_blocker = None
            return self._effect(proposal, EffectStatus.APPLIED, witness)
        except Exception as exc:
            self.last_blocker = str(exc) if isinstance(exc, BridgeAdapterError) else "confirmation_evidence_unreadable"
            return self._effect(proposal, EffectStatus.UNKNOWN)

    def commit_confirmed_effect(self, proposal: ActionProposal, effect: EffectObservation) -> EffectObservation:
        """Persist only this already-confirmed leaf; never call plan advance."""
        if effect.status is not EffectStatus.APPLIED:
            return effect
        try:
            if effect.idempotency_key != proposal.idempotency_key:
                raise BridgeAdapterError("confirmation_effect_idempotency_mismatch")
            witness = _plain(effect.payload)
            if not isinstance(witness, Mapping):
                raise BridgeAdapterError("confirmation_effect_witness_missing")
            if (
                any(witness.get(key) != self.identity[key] for key in ("session_id", "save_id_hash", "world_id"))
                or not isinstance(witness.get("snapshot_id"), str)
                or type(witness.get("seq")) is not int
                or witness.get("seq") <= 0
            ):
                raise BridgeAdapterError("confirmation_effect_witness_identity_invalid")
            witness_state_hash = canonical_state_hash(witness)
            _, step = self._plan()
            command_id = step.get("command_id")
            if not isinstance(command_id, str):
                raise BridgeAdapterError("selected_leaf_command_missing")
            plan = reconcile_colony_plan_leaf(self.root, self.plan_id,
                expected_step_id=self.step_id, expected_command_id=command_id,
                expected_witness_snapshot_id=witness["snapshot_id"],
                expected_witness_seq=witness["seq"],
                expected_witness_state_hash=witness_state_hash)
            if plan.get("open_command_id") is not None or plan.get("current_step") is not None:
                raise BridgeAdapterError("leaf_reconciliation_left_open_command")
            return effect
        except Exception as exc:
            self.last_blocker = str(exc) if isinstance(exc, BridgeAdapterError) else "plan_leaf_reconciliation_failed"
            return self._effect(proposal, EffectStatus.UNKNOWN)

    def commit_already_satisfied(self, state: Mapping[str, Any]) -> bool:
        """Persist a satisfied waiting leaf without creating a command."""
        if not isinstance(state, Mapping):
            return False
        try:
            state_hash = canonical_state_hash(_plain(state))
            plan = mark_colony_plan_leaf_already_satisfied(self.root, self.plan_id,
                expected_step_id=self.step_id, expected_state_hash=state_hash)
            return plan.get("open_command_id") is None
        except Exception as exc:
            self.last_blocker = str(exc) if isinstance(exc, BridgeAdapterError) else "plan_noop_reconciliation_failed:" + type(exc).__name__
            return False

    reconcile = observe_effect


def create_colony_plan_controller(bridge_dir: pathlib.Path, *, plan_id: str, step_id: str,
        expected_identity: Mapping[str, Any], intent_directory: pathlib.Path,
        allow_pending_commands: bool = False, final_apply_guard: Any = None) -> ControllerCore:
    """Bind one explicit leaf to an existing private durable-intent directory.

    Approval remains external. Three calls bound the initial publication plus
    two effect reads; adapter.reconcile(proposal) remains a read-only operation.
    """
    adapter = ColonyPlanControllerAdapter(bridge_dir, plan_id=plan_id, step_id=step_id,
        expected_identity=expected_identity, allow_pending_commands=allow_pending_commands,
        final_apply_guard=final_apply_guard)
    return ControllerCore(adapter, adapter.policy(), intent_store=DurableIntentStore(intent_directory),
        final_apply_guard=final_apply_guard)


class ColonyPlanControllerSequence:
    """Finite coordinator for explicit leaves of one Colony Plan.

    Each leaf owns a fresh adapter latch. The coordinator advances its index
    only after that adapter has persisted the selected leaf completion. It does
    not call ``advance_colony_plan`` itself; the next leaf is published only by
    the next ``step`` call after the prior revision is committed.
    """
    def __init__(self, bridge_dir: pathlib.Path, *, plan_id: str, step_ids: list[str],
                 expected_identity: Mapping[str, Any], intent_directory: pathlib.Path,
                 allow_pending_commands: bool = False, max_calls: int | None = None,
                 final_apply_guard: Any = None):
        if not step_ids or len(step_ids) > 32 or len(set(step_ids)) != len(step_ids):
            raise BridgeAdapterError("step_sequence_invalid")
        if final_apply_guard is not None and not callable(final_apply_guard):
            raise TypeError("final_apply_guard must be callable")
        bridge_root = pathlib.Path(os.path.abspath(pathlib.Path(bridge_dir).expanduser()))
        # The plan revision is the durable source of truth for restart
        # recovery.  Never infer progress from an intent file or from a local
        # process counter: those can outlive a plan revision or be incomplete
        # after an uncertain result.
        try:
            persisted_plan = load_active_plan(bridge_root, plan_id)
        except Exception as exc:
            raise BridgeAdapterError("plan_resume_state_unreadable") from exc
        persisted_steps = persisted_plan.get("steps")
        if not isinstance(persisted_steps, list) or [
                item.get("id") if isinstance(item, Mapping) else None
                for item in persisted_steps
        ] != list(step_ids):
            raise BridgeAdapterError("plan_step_sequence_mismatch")
        statuses = [item.get("status") for item in persisted_steps]
        allowed_statuses = {"waiting", "awaiting_approval", "awaiting_confirmation", "completed"}
        if any(status not in allowed_statuses for status in statuses):
            raise BridgeAdapterError("plan_resume_status_blocked")
        first_open = next((index for index, status in enumerate(statuses) if status != "completed"), len(statuses))
        # A later completed leaf with an earlier non-completed leaf would mean
        # the persisted plan is non-monotonic.  Refuse to guess which command
        # or dependency should be resumed.
        if any(status == "completed" for status in statuses[first_open:]):
            raise BridgeAdapterError("plan_resume_status_nonmonotonic")
        self._store = DurableIntentStore(intent_directory)
        self._cores = []
        for step_id in step_ids:
            adapter = ColonyPlanControllerAdapter(bridge_root, plan_id=plan_id, step_id=step_id,
                expected_identity=expected_identity, allow_pending_commands=allow_pending_commands,
                final_apply_guard=final_apply_guard)
            self._cores.append(ControllerCore(adapter, adapter.policy(), intent_store=self._store,
                final_apply_guard=final_apply_guard))
        self._index = first_open
        self._calls = 0
        self._max_calls = max_calls if type(max_calls) is int and max_calls >= 1 else len(step_ids) * 3
        plan_status = persisted_plan.get("status")
        self._stop_reason: str | None = (
            "sequence_completed" if self._index == len(self._cores) else
            "plan_paused" if plan_status == "paused" else
            "plan_failed" if plan_status == "failed" else None
        )

    @property
    def completed(self) -> bool:
        return self._index == len(self._cores)

    @property
    def step_index(self) -> int:
        return self._index

    @property
    def stop_reason(self) -> str | None:
        return self._stop_reason

    def step(self, *, now=None) -> ControllerCycle:
        if self._stop_reason is not None:
            raise StopIteration(self._stop_reason)
        if self.completed:
            raise StopIteration("sequence_completed")
        self._calls += 1
        cycle = self._cores[self._index].step(now=now)
        reconciliation = cycle.reconciliation
        if (reconciliation is not None and reconciliation.status is ReconciliationStatus.BLOCKED
                and not reconciliation.confirmed):
            self._stop_reason = reconciliation.recovery_reason
        elif (reconciliation is not None and reconciliation.confirmed) or cycle.decision.reason in {
                "expected_effects_already_satisfied", "target_already_at_priority",
                "intent_already_satisfied"}:
            self._index += 1
        if self.completed:
            self._stop_reason = "sequence_completed"
        elif self._stop_reason is None and self._calls >= self._max_calls:
            self._stop_reason = "sequence_call_budget_exhausted"
        return cycle
