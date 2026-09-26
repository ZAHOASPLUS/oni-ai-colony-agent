"""One-tick, restart-safe coordinator for an explicit Colony Plan sequence.

The coordinator is deliberately narrower than the Bridge watch loop.  It
binds one current plan leaf per tick, persists only a bounded coordinator
receipt, and never approves or executes a command.  A caller must opt in to
``publish_one`` before the first waiting leaf may reach ``apply``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
import re
from collections.abc import Mapping, Sequence
from typing import Any, Callable, Optional

from agent.command_queue import write_json_atomic
from agent.colony_plan import ColonyPlanError, load_active_plan, plan_leaf_contract_hash
from agent.controller_bridge import (
    BridgeAdapterError,
    ColonyPlanControllerAdapter,
    IDENTITY_FIELDS,
)
from agent.controller_loop import (
    ControllerCore,
    DurableIntentStore,
    EffectStatus,
    ReconciliationStatus,
    decide_bounded_action,
    normalize_observation,
    _plain,
)
from agent.state_quality import canonical_state_hash


COORDINATOR_SCHEMA = "oni.plan-coordinator-tick.v1"
_RESUMABLE_STATUSES = {"waiting", "awaiting_approval", "awaiting_confirmation", "completed"}
_PROTECTED_PROCESS_KEYS = ("OxygenNotIncluded", "oni_bridge", "steam")


class PlanCoordinatorError(RuntimeError):
    """A durable plan or coordinator boundary cannot be trusted."""


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _hash_identity(identity: Mapping[str, Any]) -> str:
    payload = json.dumps(dict(identity), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


class PlanSequenceCoordinator:
    """Drive at most one explicit leaf during one safe coordinator tick."""

    def __init__(
        self,
        bridge_dir: pathlib.Path,
        *,
        plan_id: str,
        step_ids: Sequence[str],
        expected_identity: Mapping[str, Any],
        intent_directory: pathlib.Path,
        clone_preflight: Mapping[str, Any],
        clone_probe: Optional[Callable[[], Mapping[str, Any]]] = None,
        process_probe: Optional[Callable[[], Mapping[str, int]]] = None,
        publish_one: bool = False,
        state_path: Optional[pathlib.Path] = None,
    ) -> None:
        self.bridge_dir = pathlib.Path(bridge_dir).expanduser().resolve()
        self.plan_id = plan_id
        if isinstance(step_ids, (str, bytes)):
            raise PlanCoordinatorError("step_sequence_invalid")
        self.step_ids = tuple(step_ids)
        if not self.step_ids or len(self.step_ids) > 32 or len(set(self.step_ids)) != len(self.step_ids):
            raise PlanCoordinatorError("step_sequence_invalid")
        if set(expected_identity) != set(IDENTITY_FIELDS):
            raise PlanCoordinatorError("complete_runtime_identity_required")
        self.expected_identity = dict(expected_identity)
        if (
            type(self.expected_identity.get("world_id")) is not int
            or self.expected_identity["world_id"] < 0
            or any(
                not isinstance(self.expected_identity.get(key), str)
                or not self.expected_identity[key]
                for key in IDENTITY_FIELDS
                if key != "world_id"
            )
            or re.fullmatch(r"sha256:[0-9a-f]{64}", self.expected_identity["save_id_hash"]) is None
        ):
            raise PlanCoordinatorError("runtime_identity_invalid")
        if (
            not isinstance(clone_preflight, Mapping)
            or clone_preflight.get("schema") != "oni.clone-launch-preflight.v1"
            or clone_preflight.get("launch_allowed") is not True
            or pathlib.Path(str(clone_preflight.get("bridge_dir") or "")).expanduser().resolve() != self.bridge_dir
        ):
            raise PlanCoordinatorError("clone_preflight_required")
        if process_probe is None:
            raise PlanCoordinatorError("process_probe_required")
        if clone_probe is None:
            raise PlanCoordinatorError("clone_probe_required")
        self.clone_preflight = dict(clone_preflight)
        self.clone_probe = clone_probe
        boundary_keys: dict[str, tuple[str, ...]] = {}
        for key in ("process_counts", "queue_counts"):
            counts = self.clone_preflight.get(key)
            if (not isinstance(counts, Mapping)
                    or any(not isinstance(name, str) or not name for name in counts)
                    or any(type(value) is not int or value < 0 for value in counts.values())
                    or any(int(value) != 0 for value in counts.values())):
                raise PlanCoordinatorError("clone_preflight_boundary_nonempty")
            boundary_keys[key] = tuple(sorted(counts))
        self._protected_process_keys = boundary_keys["process_counts"]
        self._protected_queue_keys = boundary_keys["queue_counts"]
        if set(_PROTECTED_PROCESS_KEYS) - set(self._protected_process_keys):
            raise PlanCoordinatorError("clone_preflight_process_keys_incomplete")
        for key in ("manifest_sha256", "clone_save_sha256", "clone_preview_sha256",
                    "clone_save_path_id", "clone_preview_path_id"):
            if re.fullmatch(r"sha256:[0-9a-f]{64}", str(self.clone_preflight.get(key) or "")) is None:
                raise PlanCoordinatorError("clone_preflight_identity_missing")
        if self.clone_preflight["clone_save_sha256"] != self.expected_identity["save_id_hash"]:
            raise PlanCoordinatorError("clone_preflight_save_hash_mismatch")
        self.process_probe = process_probe
        self.publish_one = publish_one is True
        self.intent_directory = pathlib.Path(intent_directory).expanduser().resolve(strict=True)
        self.state_path = (
            pathlib.Path(state_path).expanduser().resolve()
            if state_path is not None
            else self.intent_directory / "plan-coordinator-state.json"
        )
        if not self.state_path.is_relative_to(self.intent_directory):
            raise PlanCoordinatorError("state_path_outside_intent_directory")
        self.state_path.parent.mkdir(parents=True, exist_ok=True)

    def _plan_and_index(self) -> tuple[dict[str, Any], int]:
        try:
            plan = load_active_plan(self.bridge_dir, self.plan_id)
        except (ColonyPlanError, OSError, ValueError) as exc:
            raise PlanCoordinatorError("plan_state_unreadable") from exc
        steps = plan.get("steps")
        if not isinstance(steps, list) or [
            item.get("id") if isinstance(item, Mapping) else None for item in steps
        ] != list(self.step_ids):
            raise PlanCoordinatorError("plan_step_sequence_mismatch")
        statuses = [item.get("status") for item in steps]
        if any(status not in _RESUMABLE_STATUSES for status in statuses):
            raise PlanCoordinatorError("plan_resume_status_blocked")
        all_completed = bool(statuses) and all(status == "completed" for status in statuses)
        if (plan.get("status") == "completed") != all_completed:
            raise PlanCoordinatorError("plan_status_inconsistent")
        index = next((i for i, status in enumerate(statuses) if status != "completed"), len(statuses))
        if any(status == "completed" for status in statuses[index:]):
            raise PlanCoordinatorError("plan_resume_status_nonmonotonic")
        return plan, index

    def _base_result(self, plan: Mapping[str, Any], index: int) -> dict[str, Any]:
        return {
            "schema": COORDINATOR_SCHEMA,
            "tick_id": "tick-" + hashlib.sha256(
                f"{self.plan_id}:{plan.get('revision')}:{index}:{_utc_now().isoformat()}".encode("utf-8")
            ).hexdigest()[:24],
            "plan_id": self.plan_id,
            "plan_revision": plan.get("revision"),
            "plan_revision_hash": plan.get("revision_hash"),
            "step_index": index,
            "step_id": self.step_ids[index] if index < len(self.step_ids) else None,
            "live_admission": False,
            "side_effects": {
                "command_published": False,
                "approval": False,
                "execution": False,
                "successor_advanced": False,
            },
        }

    def _persist(self, result: Mapping[str, Any]) -> None:
        document = {
            "schema": "oni.plan-coordinator-state.v1",
            "plan_id": self.plan_id,
            "step_ids": list(self.step_ids),
            "identity_hash": _hash_identity(self.expected_identity),
            "plan_revision": result.get("plan_revision"),
            "plan_revision_hash": result.get("plan_revision_hash"),
            "step_index": result.get("step_index"),
            "step_id": result.get("step_id"),
            "status": result.get("status"),
            "stop_reason": result.get("stop_reason"),
            "tick_id": result.get("tick_id"),
            "observation_id": result.get("observation_id"),
            "observation_quality": result.get("observation_quality"),
            "observation_freshness": result.get("observation_freshness"),
            "observation_blocker": result.get("observation_blocker"),
            "state_hash": result.get("state_hash"),
            "command_id": result.get("command_id"),
            "clone_preflight_hash": _hash_identity(self.clone_preflight),
            "live_admission": False,
        }
        write_json_atomic(self.state_path, document)

    def tick(self, *, now: Optional[dt.datetime] = None) -> dict[str, Any]:
        """Observe or publish one leaf, then persist the tick receipt.

        ``publish_one=False`` is the default and performs no command queue
        mutation.  Existing awaiting leaves are always handled through the
        explicit durable-intent recovery path, never through ``apply``.
        """
        plan, index = self._plan_and_index()
        result = self._base_result(plan, index)
        def validate_clone_boundary(candidate: Mapping[str, Any]) -> tuple[dict[str, int], dict[str, int]]:
            """Validate every mutable admission field from one clone probe."""
            if candidate.get("schema") != "oni.clone-launch-preflight.v1":
                raise PlanCoordinatorError("clone_probe_schema_invalid")
            if candidate.get("launch_allowed") is not True:
                raise PlanCoordinatorError("clone_launch_not_allowed")
            for key in ("bridge_dir", "manifest_sha256", "clone_save_sha256", "clone_preview_sha256",
                        "clone_save_path_id", "clone_preview_path_id"):
                if candidate.get(key) != self.clone_preflight.get(key):
                    raise PlanCoordinatorError("clone_preflight_changed")

            def counts_for(key: str, expected: tuple[str, ...]) -> dict[str, int]:
                counts = candidate.get(key)
                if not isinstance(counts, Mapping):
                    raise PlanCoordinatorError(f"clone_{key}_invalid")
                if set(counts) != set(expected):
                    raise PlanCoordinatorError(f"clone_{key}_incomplete")
                normalized: dict[str, int] = {}
                for name in expected:
                    value = counts.get(name)
                    if type(value) is not int or value < 0:
                        raise PlanCoordinatorError(f"clone_{key}_invalid")
                    normalized[name] = value
                return normalized

            process_counts = counts_for("process_counts", self._protected_process_keys)
            queue_counts = counts_for("queue_counts", self._protected_queue_keys)
            if any(value != 0 for value in process_counts.values()):
                raise PlanCoordinatorError("protected_process_running")
            if any(value != 0 for value in queue_counts.values()):
                raise PlanCoordinatorError("protected_queue_nonempty")
            if process_counts != self.clone_preflight["process_counts"]:
                raise PlanCoordinatorError("clone_process_counts_changed")
            if queue_counts != self.clone_preflight["queue_counts"]:
                raise PlanCoordinatorError("clone_queue_counts_changed")
            return process_counts, queue_counts

        try:
            fresh_clone = self.clone_probe()
        except Exception as exc:
            raise PlanCoordinatorError("clone_probe_unreadable") from exc
        if not isinstance(fresh_clone, Mapping):
            raise PlanCoordinatorError("clone_probe_invalid")
        try:
            _fresh_process_counts, fresh_queue_counts = validate_clone_boundary(fresh_clone)
        except PlanCoordinatorError as exc:
            result.update({"status": "blocked", "stop_reason": str(exc)})
            self._persist(result)
            return result
        result["queue_counts"] = fresh_queue_counts
        before_revision = plan.get("revision")
        before_command_id = None
        if index < len(plan.get("steps", [])) and isinstance(plan.get("steps", [])[index], Mapping):
            before_command_id = plan["steps"][index].get("command_id")
        def read_processes() -> dict[str, int]:
            try:
                observed_processes = self.process_probe()
                if (not isinstance(observed_processes, Mapping)
                        or set(observed_processes) != set(self._protected_process_keys)):
                    raise PlanCoordinatorError("process_probe_incomplete")
                live = {name: observed_processes[name] for name in self._protected_process_keys}
            except PlanCoordinatorError:
                raise
            except Exception as exc:
                raise PlanCoordinatorError("process_probe_unreadable") from exc
            if any(type(value) is not int or value < 0 for value in live.values()):
                raise PlanCoordinatorError("process_probe_invalid")
            return live

        # The preflight above is intentionally only an early admission check.
        # Between observation and publication the protected process set or the
        # selected clone can change.  Keep a final, caller-owned guard at the
        # ControllerCore apply boundary so no adapter write is reachable after
        # a process/lifecycle TOCTOU change.  The guard returns False rather
        # than exposing probe errors to the adapter; ControllerCore records a
        # rejected effect and the receipt remains consumed for at-most-once
        # recovery.
        final_guard_reason: list[str] = []

        def final_apply_guard(_proposal: Any) -> bool:
            final_guard_reason.clear()
            try:
                latest_processes = read_processes()
            except PlanCoordinatorError as exc:
                final_guard_reason.append(str(exc))
                return False
            result["process_counts"] = latest_processes
            if any(value != 0 for value in latest_processes.values()):
                final_guard_reason.append("protected_process_running")
                return False
            try:
                latest_clone = self.clone_probe()
            except Exception:
                final_guard_reason.append("clone_probe_unreadable")
                return False
            if not isinstance(latest_clone, Mapping):
                final_guard_reason.append("clone_probe_invalid")
                return False
            try:
                latest_clone_processes, latest_queue_counts = validate_clone_boundary(latest_clone)
            except PlanCoordinatorError as exc:
                final_guard_reason.append(str(exc))
                return False
            result["queue_counts"] = latest_queue_counts
            if latest_clone_processes != latest_processes:
                final_guard_reason.append("process_counts_mismatch")
                return False
            return True

        live_processes = read_processes()
        if any(value != 0 for value in live_processes.values()):
            result.update({"status": "blocked", "stop_reason": "protected_process_running",
                           "process_counts": live_processes})
            self._persist(result)
            return result
        result["process_counts"] = live_processes
        if self.publish_one:
            live_processes = read_processes()
            result["process_counts"] = live_processes
            if any(value != 0 for value in live_processes.values()):
                result.update({"status": "blocked", "stop_reason": "protected_process_running"})
                self._persist(result)
                return result
        if index == len(self.step_ids):
            result.update({"status": "completed", "stop_reason": "sequence_completed"})
            self._persist(result)
            return result
        if plan.get("status") in {"paused", "failed", "aborted"}:
            result.update({"status": "blocked", "stop_reason": "plan_" + str(plan.get("status"))})
            self._persist(result)
            return result

        step = plan["steps"][index]
        adapter = ColonyPlanControllerAdapter(
            self.bridge_dir,
            plan_id=self.plan_id,
            step_id=self.step_ids[index],
            expected_identity=self.expected_identity,
            allow_pending_commands=self.publish_one,
            final_apply_guard=final_apply_guard,
        )
        observation = adapter.observe()
        result.update({
            "observation_id": observation.observation_id,
            "observation_quality": observation.quality.value,
            "observation_freshness": observation.freshness.value,
            "state_hash": canonical_state_hash(_plain(observation.payload))
            if isinstance(observation.payload, Mapping) and observation.payload else None,
        })
        # Preserve the adapter's bounded, code-only read blocker in the tick
        # receipt.  ``observe`` intentionally redacts arbitrary exceptions;
        # exposing this stable diagnostic lets a read-only caller distinguish
        # a replay/non-trusted bundle from a missing plan or clone without
        # leaking paths, credentials, or raw error bodies.
        if isinstance(adapter.last_blocker, str) and adapter.last_blocker:
            result["observation_blocker"] = adapter.last_blocker

        status = step.get("status")
        if status in {"awaiting_approval", "awaiting_confirmation"}:
            # A restart must not call ControllerCore.step(), because that
            # would try to consume the old intent again.  Recovery is explicit
            # and reaches only the adapter's read-only effect path.
            core = ControllerCore(
                adapter,
                adapter.policy(),
                intent_store=DurableIntentStore(self.intent_directory),
                final_apply_guard=final_apply_guard,
            )
            cycle = core.resume_consumed_intent(now=now)
            if cycle.decision.reason.startswith("intent_recovery_"):
                result.update({"status": "blocked", "stop_reason": cycle.decision.reason,
                               "cycle": cycle.as_dict()})
                self._persist(result)
                return result
        elif status == "waiting":
            normalized = normalize_observation(observation, now=now)
            decision = decide_bounded_action(normalized, adapter.policy(), now=now)
            result["decision"] = decision.as_dict()
            if not self.publish_one:
                result.update({
                    "status": "candidate" if decision.proposal is not None else "blocked",
                    "stop_reason": "read_only_candidate" if decision.proposal is not None else decision.reason,
                })
                self._persist(result)
                return result
            core = ControllerCore(
                adapter,
                adapter.policy(),
                intent_store=DurableIntentStore(self.intent_directory),
                final_apply_guard=final_apply_guard,
            )
            cycle = core.step(now=now)
        else:
            raise PlanCoordinatorError("plan_step_status_invalid")

        result["cycle"] = cycle.as_dict()
        effect = cycle.effect
        reconciliation = cycle.reconciliation
        if effect is not None:
            result["effect_status"] = effect.status.value
        if reconciliation is not None:
            result["reconciliation_status"] = reconciliation.status.value
            result["recovery_reason"] = reconciliation.recovery_reason
        if final_guard_reason:
            result.update({"status": "blocked", "stop_reason": final_guard_reason[0]})
        elif effect is not None and effect.status is EffectStatus.PENDING:
            result.update({"status": "awaiting_external_confirmation", "stop_reason": "external_approval_or_execution"})
        elif reconciliation is not None and reconciliation.confirmed:
            result.update({"status": "leaf_completed", "stop_reason": "confirmed_leaf_persisted"})
        elif reconciliation is not None and reconciliation.status is ReconciliationStatus.BLOCKED:
            result.update({"status": "blocked", "stop_reason": reconciliation.recovery_reason})
        else:
            result.update({"status": "observed", "stop_reason": cycle.decision.reason})
        result["side_effects"] = dict(result["side_effects"])
        try:
            final_plan, _ = self._plan_and_index()
            final_step = final_plan.get("steps", [])[index]
        except (PlanCoordinatorError, IndexError, TypeError):
            final_plan, final_step = plan, step
        result["plan_revision"] = final_plan.get("revision")
        result["plan_revision_hash"] = final_plan.get("revision_hash")
        if isinstance(final_step, Mapping):
            result["command_id"] = final_step.get("command_id")
        result["side_effects"]["command_published"] = bool(
            status == "waiting"
            and before_revision is not None
            and final_plan.get("revision", 0) > before_revision
            and before_command_id is None
            and isinstance(final_step, Mapping)
            and final_step.get("status") in {"awaiting_approval", "awaiting_confirmation"}
            and isinstance(final_step.get("command_id"), str)
        )
        if isinstance(final_step, Mapping) and final_step.get("status") == "completed":
            result.update({"status": "leaf_completed", "stop_reason": "leaf_checkpoint_persisted"})
        self._persist(result)
        return result


__all__ = ["COORDINATOR_SCHEMA", "PlanCoordinatorError", "PlanSequenceCoordinator"]
