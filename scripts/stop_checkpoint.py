"""Save a checkpoint while the autonomous controller stops (roadmap B16).

A game launch always loaded the original clone, so every process lost its
progress (Phase 2 session start cycles 15.8 -> 11.7 -> 10.0 -> 6.4).  On a
stop, once custody is quiescent and the autonomous policy is sealed (the Mod
accepts a checkpoint control only while the action policy is disabled and
unbound), the controller asks the Mod to save the game as the next link of the
clone's checkpoint chain and then publishes that link's manifest
(``agent.checkpoint_chain``).

The work is a small state machine advanced once per Bridge iteration from the
controller's ``stop_requested`` callback.  It never blocks: the controller is
also the Bridge, and the later trusted snapshot that confirms the save is only
published while the Bridge keeps running.  It never retries: a failed, rejected
or timed-out control is recorded and the stop completes without a checkpoint.
"""

from __future__ import annotations

import datetime as dt
import pathlib
import secrets
import time
from typing import Any, Callable, Dict, Mapping, Optional

from agent.checkpoint_chain import (
    CheckpointChainError,
    build_checkpoint_manifest,
    checkpoint_paths,
    publish_checkpoint_manifest,
    select_latest_checkpoint,
)
from scripts import manage_validation_checkpoint as checkpoint_cli

PENDING = "pending"
VERIFIED = "verified"
FAILED = "failed"
DEFAULT_TIMEOUT_SECONDS = 120.0
CONTROL_TTL_SECONDS = 120


class StopCheckpoint:
    """Advance one checkpoint save step by step; see the module docstring."""

    def __init__(
        self,
        *,
        bridge_dir: pathlib.Path,
        clone_manifest: pathlib.Path,
        clone_save: pathlib.Path,
        run_id: str,
        expected_mod_version: str,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.bridge = pathlib.Path(bridge_dir)
        self.clone_manifest = pathlib.Path(clone_manifest)
        self.clone_save = pathlib.Path(clone_save)
        self.run_id = run_id
        self.mod_version = expected_mod_version
        self.timeout = float(timeout_seconds)
        self.clock = clock
        self.phase = "idle"
        self.outcome: Optional[str] = None
        self.record: Dict[str, Any] = {"phase": "idle"}
        self._started: Optional[float] = None
        self._policy_written = False
        self._control_id: Optional[str] = None
        self._target: Optional[pathlib.Path] = None
        self._index: Optional[int] = None
        self._source: Dict[str, Any] = {}
        self._completed_at: Optional[str] = None
        self._active_hash: Optional[str] = None
        self._root_save: Optional[pathlib.Path] = None

    # -- public -------------------------------------------------------------

    def step(self, latest: Optional[Mapping[str, Any]]) -> str:
        """Advance once; returns ``pending``, ``verified`` or ``failed``."""

        if self.outcome is not None:
            return self.outcome
        try:
            if self.phase == "idle":
                self._issue(latest or {})
            elif self.phase == "awaiting_result":
                self._await_result()
            elif self.phase == "awaiting_state":
                self._await_state(latest or {})
        except (checkpoint_cli.CheckpointError, CheckpointChainError, OSError, ValueError) as exc:
            self._finish(FAILED, f"{type(exc).__name__}: {exc}")
        if self.outcome is None and self._started is not None and self.clock() - self._started > self.timeout:
            self._finish(FAILED, f"timed out in phase {self.phase}")
        return self.outcome or PENDING

    # -- phases -------------------------------------------------------------

    def _issue(self, latest: Mapping[str, Any]) -> None:
        current = select_latest_checkpoint(self.clone_manifest, self.clone_save)
        active = pathlib.Path(current["save_path"])
        self._index = current["index"] + 1
        self._root_save = pathlib.Path(current["root_save_path"])
        self._target = checkpoint_paths(self._root_save, self._index)["save"]
        state = dict(latest)
        # The same preconditions the operator CLI enforces: the Probe's save
        # identity is the file on disk, nothing is dirty, the action policy is
        # disabled and unbound, the action queues are empty, and the target is
        # a new file beside the active save.
        checkpoint_cli.validate_environment(self.bridge, state, active, self.mod_version)
        checkpoint_cli.validate_target(active, self._target, "save_checkpoint")
        if state.get("save_id_hash") != current["save_sha256"]:
            raise checkpoint_cli.CheckpointError("The active save is not the newest link of the checkpoint chain.")
        self._control_id = "ctl-save-stop-" + secrets.token_hex(6)
        source_path_id = checkpoint_cli.path_id(active)
        policy = checkpoint_cli.build_policy(
            enabled=True,
            test_run_id=self.run_id,
            operation="save_checkpoint",
            save_hash=str(state["save_id_hash"]),
            save_path_id=source_path_id,
            target_save_path_id=checkpoint_cli.path_id(self._target),
            ttl_seconds=CONTROL_TTL_SECONDS,
        )
        control = checkpoint_cli.build_control(
            control_id=self._control_id,
            operation="save_checkpoint",
            test_run_id=self.run_id,
            state=state,
            save_path_id=source_path_id,
            expected_mod_version=self.mod_version,
            target_save_path=self._target,
            ttl_seconds=CONTROL_TTL_SECONDS,
        )
        control["reason"] = "Autonomous controller stop: save the next checkpoint of the clone."
        control["source"] = {"kind": "autonomous_controller_stop"}
        self._source = {"session_id": state.get("session_id"), "seq": state.get("seq")}
        self._started = self.clock()
        checkpoint_cli.write_json_atomic(self.bridge / "validation-control-policy.json", policy)
        self._policy_written = True
        checkpoint_cli.write_json_atomic(
            self.bridge / "validation-controls" / "approved" / f"{self._control_id}.json", control
        )
        self.phase = "awaiting_result"
        self.record = {
            "phase": self.phase, "control_id": self._control_id, "index": self._index,
            "target": self._target.name, "parent_index": current["index"],
        }

    def _await_result(self) -> None:
        assert self._control_id is not None and self._target is not None
        path = checkpoint_cli.find_result(self.bridge, self._control_id)
        if path is None:
            return
        completed = checkpoint_cli.load_json(path)
        if completed.get("status") != "verified":
            message = (completed.get("result") or {}).get("message") if isinstance(completed.get("result"), dict) else None
            raise checkpoint_cli.CheckpointError(f"control ended as {completed.get('status')}: {message}")
        result = completed.get("result") if isinstance(completed.get("result"), dict) else {}
        active_hash = str(result.get("active_save_id_hash") or "")
        if not checkpoint_cli.HASH_PATTERN.fullmatch(active_hash):
            raise checkpoint_cli.CheckpointError("verified control has no active save hash")
        if checkpoint_cli.sha256_file(self._target) != active_hash:
            raise checkpoint_cli.CheckpointError("checkpoint file does not match the verified save hash")
        completed_at = str(completed.get("completed_at") or "")
        dt.datetime.fromisoformat(completed_at)
        self._completed_at = completed_at
        self._active_hash = active_hash
        self.phase = "awaiting_state"
        self.record["phase"] = self.phase

    def _await_state(self, latest: Mapping[str, Any]) -> None:
        # One later trusted snapshot must report the new save identity, as in
        # manage_validation_checkpoint.wait_for_state, without blocking.
        assert self._completed_at is not None and self._index is not None
        try:
            state_at = dt.datetime.fromisoformat(str(latest.get("ts") or ""))
        except ValueError:
            return
        bridge = latest.get("bridge") if isinstance(latest.get("bridge"), dict) else {}
        if not (
            latest.get("event") == "state_snapshot"
            and latest.get("save_id_hash") == self._active_hash
            and latest.get("save_dirty") is False
            and latest.get("session_id") == self._source.get("session_id")
            and latest.get("mod_version") == self.mod_version
            and state_at >= dt.datetime.fromisoformat(self._completed_at)
            and bridge.get("stale") is not True
        ):
            return
        game_time = latest.get("game_time_cycles")
        if isinstance(game_time, bool) or not isinstance(game_time, (int, float)):
            raise checkpoint_cli.CheckpointError("the confirming snapshot has no game time")
        manifest = build_checkpoint_manifest(
            clone_manifest=self.clone_manifest,
            clone_save=self.clone_save,
            index=self._index,
            run_id=self.run_id,
            session_id=str(latest.get("session_id")),
            game_time_cycles=float(game_time),
        )
        assert self._root_save is not None
        paths = checkpoint_paths(self._root_save, self._index)
        publish_checkpoint_manifest(paths["manifest"], manifest)
        self.record.update({"save_sha256": manifest["save"]["sha256"], "game_time_cycles": manifest["game_time_cycles"]})
        self._finish(VERIFIED, None)

    def _finish(self, outcome: str, error: Optional[str]) -> None:
        self.outcome = outcome
        self.phase = outcome
        self.record["phase"] = outcome
        if error is not None:
            self.record["error"] = error[:400]
        if self._policy_written:
            try:
                checkpoint_cli.write_disabled_policy(self.bridge)
            except OSError as exc:  # recorded; the stop itself still completes
                self.record["policy_disable_error"] = str(exc)[:200]
