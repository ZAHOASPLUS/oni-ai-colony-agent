#!/usr/bin/env python3
"""Session-aware local bridge for ONI AI Probe V2."""

from __future__ import annotations

import argparse
import collections
import copy
import datetime as dt
import errno
import json
import os
import pathlib
import re
import stat
import sys
import tempfile
import time
from typing import Any, Callable, Deque, Dict, Iterable, List, Mapping, Optional, Set, Tuple

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent import rules as agent_rules
from agent.action_outcomes import (
    build_action_outcomes,
    recover_staged_confirmed_outcomes,
    stage_confirmed_outcomes_commit,
)
from agent.command_queue import command_pipeline_readiness, load_policy, sync_pending_commands
from agent.control_packet import build_control_packet, validate_control_packet
from agent.decision import build_decision_frame
from agent.decision_frame_archive import (
    persist_decision_frame_archive,
    prune_decision_frame_archives,
)
from agent.incidents import IncidentTracker
from agent.llm_planner import colony_plan_definition_from_plan, generate_plan
from agent.model_loop import ModelLoop
from agent.colony_plan import (
    abort_colony_plan,
    advance_colony_plan,
    create_colony_plan,
    load_active_plan,
    plan_leaf_contract_hash,
)
from agent.strategy import PhaseEvidenceTracker, STRATEGY_GOAL, build_strategy_markdown, evaluate_strategy
from agent.state_quality import (
    canonical_state_hash,
    field_quality,
    is_session_started,
    is_state_snapshot,
    mark_stale,
    parse_positive_sequence,
    quality_summary,
    safe_get,
    snapshot_age_seconds,
    snapshot_freshness,
    snapshot_trust_blockers,
)
from agent.clone_launch_guard import (
    CloneLaunchGuardError,
    build_clone_probe,
    build_process_probe,
    build_queue_probe,
    validate_clone_launch,
)
from agent.controller_bridge import BridgeAdapterError
from agent.plan_coordinator import PlanCoordinatorError, PlanSequenceCoordinator


MAX_INITIAL_LINES = 10000
MAX_EVENT_WINDOW = 30
MAX_LOG_BYTES = 20 * 1024 * 1024
LOG_BACKUPS = 3
MAX_SUMMARY_BYTES = 8 * 1024
MAX_SUMMARY_LINES = 100
_ATOMIC_REPLACE_ATTEMPTS = 8
_ATOMIC_REPLACE_INITIAL_DELAY_SECONDS = 0.025
_ATOMIC_REPLACE_MAX_DELAY_SECONDS = 0.2
_RETRYABLE_REPLACE_ERRNOS = {errno.EACCES, errno.EBUSY, errno.EPERM}
_RETRYABLE_REPLACE_WINERRORS = {5, 32, 33}
MAX_EVIDENCE_COMMANDS = 256
MAX_EVIDENCE_VARIANTS_PER_REQUIREMENT = 16
MAX_EVIDENCE_RECORD_BYTES = 4 * 1024 * 1024
MAX_EVIDENCE_MATCH_BYTES = 32 * 1024 * 1024
EVIDENCE_CONTINUITY_SCHEMA = "oni.evidence-continuity.v1"
COORDINATOR_CONFIG_SCHEMA = "oni.bridge-coordinator-config.v1"
PLANNED_OWNER_CONFIG_SCHEMA = "oni.planned-owner-config.v1"
_PLANNED_OWNER_QUEUE_NAMES = (
    "commands/pending",
    "commands/approving",
    "commands/approved",
    "commands/executing",
    "commands/confirming",
    "validation-controls/approved",
    "validation-controls/inflight",
)
_EVIDENCE_PIPELINE_DIRS = ("executing", "confirming")
_ACK_SEQUENCE_FIELDS = {
    "accepted": "accepted_ack_seq",
    "executed": "executed_ack_seq",
    "local_effect_observed": "local_effect_ack_seq",
}
_STICKY_SNAPSHOT_INTEGRITY_BLOCKERS = frozenset(
    {
        "snapshot_envelope_invalid",
        "session_id_invalid",
        "sequence_invalid",
        "snapshot_sequence_invalid",
        "snapshot_id_invalid",
        "save_id_hash_invalid",
        "world_id_invalid",
        "game_build_invalid",
        "mod_version_invalid",
        "save_dirty_invalid",
        "quality_missing",
        "writer_status_missing",
        "writer_drop_count_invalid",
        "writer_dropped_snapshots",
        "writer_queue_depth_invalid",
        "writer_fault_count_invalid",
        "writer_faults_detected",
        "writer_non_finite_count_invalid",
        "writer_non_finite_numbers",
        "writer_fault",
    }
)

_HASH_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_VALIDATION_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_TRUSTED_SESSION_START_REASON = "save_loader_load_complete"
_STARTUP_EXPECTATION_FIELDS = (
    "validation_run_id",
    "validation_process_id",
    "startup_binding_hash",
    "startup_intent_hash",
    "runtime_config_evidence_sha256",
    "loader_path_id_upper",
    "loader_save_sha256",
    "game_build",
    "mod_version",
)


def validate_startup_expectations(value: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Validate externally supplied startup anchors without reading the binding file.

    The live Bridge receives these values from the runtime-config transaction.  It
    deliberately never discovers expected values from ``runtime-startup-binding.json``;
    otherwise a replaced local binding could certify its own forged Probe records.
    """

    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != set(_STARTUP_EXPECTATION_FIELDS):
        raise ValueError("startup expectations have an invalid exact field set")
    normalized: Dict[str, Any] = {}
    process_id = value.get("validation_process_id")
    if isinstance(process_id, bool) or not isinstance(process_id, int) or process_id <= 0:
        raise ValueError("startup expectation validation_process_id must be a positive integer")
    normalized["validation_process_id"] = process_id
    for field in _STARTUP_EXPECTATION_FIELDS:
        if field == "validation_process_id":
            continue
        item = value.get(field)
        if not isinstance(item, str) or not item:
            raise ValueError(f"startup expectation {field} must be a non-empty string")
        normalized[field] = item
    if _VALIDATION_RUN_ID_RE.fullmatch(normalized["validation_run_id"]) is None:
        raise ValueError("startup validation run id is invalid")
    for field in (
        "startup_binding_hash",
        "startup_intent_hash",
        "runtime_config_evidence_sha256",
        "loader_path_id_upper",
        "loader_save_sha256",
    ):
        if _HASH_ID_RE.fullmatch(normalized[field]) is None:
            raise ValueError(f"startup expectation {field} is not a canonical SHA-256 id")
    if len(normalized["game_build"]) > 80 or len(normalized["mod_version"]) > 80:
        raise ValueError("startup build/version expectation is too long")
    return normalized


def default_bridge_dir() -> pathlib.Path:
    configured = os.environ.get("ONI_AI_BRIDGE_DIR")
    if configured:
        return pathlib.Path(configured)
    return pathlib.Path.home() / "Documents" / "Klei" / "OxygenNotIncluded" / "ai-bridge"


def local_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).astimezone()


def now_iso() -> str:
    return local_now().isoformat(timespec="seconds")


def reject_nonfinite_json_constant(token: str) -> None:
    raise ValueError(f"non-finite JSON constant: {token}")


def parse_json_line(line: str) -> Tuple[Optional[Dict[str, Any]], bool]:
    text = line.strip()
    if not text:
        return None, False
    try:
        value = json.loads(text, parse_constant=reject_nonfinite_json_constant)
    except (json.JSONDecodeError, ValueError):
        return {
            "schema": "oni.bridge.warning.v1",
            "event": "invalid_json",
            "raw_length": len(text),
        }, True
    if isinstance(value, dict):
        return value, False
    return {
        "schema": "oni.bridge.warning.v1",
        "event": "non_object_json",
    }, True


def load_recent_raw_lines(path: pathlib.Path, limit: int = MAX_INITIAL_LINES) -> Tuple[Deque[bytes], int, int]:
    if not path.exists():
        return collections.deque(maxlen=limit), 0, 0
    lines: Deque[bytes] = collections.deque(maxlen=limit)
    complete_size = 0
    partial_size = 0
    with path.open("rb") as handle:
        while True:
            raw = handle.readline()
            if not raw:
                break
            if raw.endswith(b"\n"):
                complete_size += len(raw)
                lines.append(raw[:-1].rstrip(b"\r"))
                partial_size = 0
            else:
                partial_size = len(raw)
    return lines, complete_size, partial_size


def load_recent_records(path: pathlib.Path, limit: int = MAX_INITIAL_LINES) -> Tuple[List[Dict[str, Any]], int, int]:
    lines, complete_size, _ = load_recent_raw_lines(path, limit)
    records: List[Dict[str, Any]] = []
    invalid = 0
    for raw in lines:
        record, bad = parse_json_line(raw.decode("utf-8-sig", errors="replace"))
        if record is not None:
            records.append(record)
        invalid += int(bad)
    return records, complete_size, invalid


def read_new_records(path: pathlib.Path, position: int) -> Tuple[List[Dict[str, Any]], int, int, int]:
    if not path.exists():
        return [], position, 0, 0
    size = path.stat().st_size
    if size < position:
        position = 0
    with path.open("rb") as handle:
        handle.seek(position)
        data = handle.read()
    if not data:
        return [], position, 0, 0
    complete_size = data.rfind(b"\n") + 1
    if complete_size == 0:
        return [], position, 0, len(data)
    complete = data[:complete_size]
    records: List[Dict[str, Any]] = []
    invalid = 0
    for raw in complete.splitlines():
        record, bad = parse_json_line(raw.decode("utf-8-sig", errors="replace"))
        if record is not None:
            records.append(record)
        invalid += int(bad)
    return records, position + complete_size, invalid, len(data) - complete_size


def compact_event(record: Dict[str, Any]) -> Dict[str, Any]:
    compact: Dict[str, Any] = {
        "ts": record.get("ts"),
        "event": record.get("event"),
        "schema": record.get("schema"),
        "session_id": record.get("session_id"),
        "seq": record.get("seq"),
    }
    for key in ("stage", "message", "cycle", "status", "action", "id", "incident_id"):
        if key in record:
            compact[key] = record[key]
    return compact


class ReplayState:
    def __init__(
        self,
        *,
        allow_untrusted_snapshot_baseline: bool = False,
        startup_expectations: Optional[Dict[str, Any]] = None,
        require_startup_binding: bool = False,
        strategy_goal: str = STRATEGY_GOAL,
    ) -> None:
        # The goal is part of every strategy payload.  Per-record observation
        # and write_outputs must evaluate the same snapshot with the same goal,
        # otherwise the tracker sees a duplicate-snapshot conflict and resets
        # its evidence window on every publication.
        self.strategy_goal = strategy_goal
        self.current_session_id: Optional[str] = None
        self.retired_session_ids: set[str] = set()
        self.latest: Optional[Dict[str, Any]] = None
        self.events: Deque[Dict[str, Any]] = collections.deque(maxlen=MAX_EVENT_WINDOW)
        self.incidents = IncidentTracker()
        self._last_signal_input: Optional[Dict[str, Any]] = None
        self._last_signals: Optional[List[Dict[str, Any]]] = None
        self.strategy_tracker = PhaseEvidenceTracker()
        self.last_seq: Optional[int] = None
        self.last_snapshot_seq: Optional[int] = None
        self.invalid_json_count = 0
        self.duplicate_seq_count = 0
        self.out_of_order_seq_count = 0
        self.seq_gap_count = 0
        self.duplicate_snapshot_seq_count = 0
        self.out_of_order_snapshot_seq_count = 0
        self.snapshot_seq_gap_count = 0
        self.retired_session_event_count = 0
        self.partial_bytes = 0
        self.rotation_count = 0
        self.rotation_tail_records_total = 0
        self.last_rotation_tail: Optional[Dict[str, Any]] = None
        self.warnings: Deque[str] = collections.deque(maxlen=20)
        self.session_ended = False
        self.session_start_observed = False
        self.session_integrity_blockers: set[str] = set()
        self.allow_untrusted_snapshot_baseline = allow_untrusted_snapshot_baseline
        self.require_startup_binding = bool(require_startup_binding)
        self.startup_expectations = validate_startup_expectations(startup_expectations)
        self.startup_integrity_blockers: set[str] = set()
        self.validation_process_id: Optional[int] = None
        self.startup_verified_session_id: Optional[str] = None
        self.startup_requested_save_path_id: Optional[str] = None
        self.startup_current_active_save_path_id: Optional[str] = None
        self.startup_current_save_id_hash: Optional[str] = None
        self.startup_distinct_path_transition_count = 0
        self.startup_pending_save_evidence: Optional[Dict[str, Any]] = None
        self.startup_seen_save_control_ids: set[str] = set()
        self.startup_reload_unload_evidence: Optional[Dict[str, Any]] = None
        self.startup_reload_ready_session_id: Optional[str] = None

        if self.require_startup_binding and self.startup_expectations is None:
            self.startup_integrity_blockers.add("startup_expectations_missing")
        elif self.startup_expectations is not None:
            self.startup_requested_save_path_id = self.startup_expectations[
                "loader_path_id_upper"
            ]

    def process_many(self, records: Iterable[Dict[str, Any]]) -> int:
        count = 0
        for record in records:
            self.process(record)
            count += 1
        return count

    def process(self, record: Dict[str, Any]) -> None:
        if record.get("event") in {"invalid_json", "non_object_json"}:
            self.invalid_json_count += 1
            if self.current_session_id:
                self.session_integrity_blockers.add("invalid_json_in_session")
                if self.startup_pending_save_evidence is not None:
                    self.startup_integrity_blockers.add("startup_save_evidence_order_invalid")
                    self.startup_pending_save_evidence = None
                if self.startup_reload_unload_evidence is not None:
                    self.startup_integrity_blockers.add("startup_reload_evidence_order_invalid")
                    self.startup_reload_unload_evidence = None
                if self.startup_reload_ready_session_id is not None:
                    self.startup_integrity_blockers.add("startup_reload_evidence_order_invalid")
                    self.startup_reload_ready_session_id = None
            self.events.append(compact_event(record))
            return

        event_name = str(record.get("event") or "")
        session_id = record.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            self.warnings.append("missing_session_id")
            self.events.append(compact_event(record))
            return
        session_id = str(session_id)

        declares_snapshot = event_name == "state_snapshot" or record.get("schema") == "oni.state.v2"
        if declares_snapshot and not is_state_snapshot(record):
            self.warnings.append("snapshot_envelope_invalid")
            if self.current_session_id == session_id:
                self.session_integrity_blockers.add("snapshot_envelope_invalid")
                self._validate_sequence(record.get("seq"))
            self.events.append(compact_event(record))
            return

        if event_name == "session_started" and not is_session_started(record):
            self.warnings.append("session_start_envelope_invalid")
            if self.current_session_id == session_id:
                self.session_integrity_blockers.add("session_start_envelope_invalid")
            self.events.append(compact_event(record))
            return
        session_started = is_session_started(record)
        session_start_sequence = parse_positive_sequence(record.get("seq")) if session_started else None
        if session_started and session_start_sequence != 1:
            self.warnings.append("session_start_order_invalid")
            if self.current_session_id == session_id:
                self.session_integrity_blockers.add("session_start_order_invalid")
                self._validate_sequence(record.get("seq"))
            self.events.append(compact_event(record))
            return

        # The process-scoped Probe session emits diagnostics while the one-shot
        # save request is still only ``pending_request``.  Those records carry
        # null requested/active paths by design and can also be preceded by
        # envelopes from an older process in the append-only log.  Until a
        # target save session is established, only the trusted load-complete
        # start is eligible for external-binding validation.
        if (
            self.require_startup_binding
            and self.startup_expectations is not None
            and self.startup_verified_session_id is None
            and self.current_session_id is None
        ):
            if not session_started:
                self.warnings.append("pre_session_envelope_ignored")
                return
            if record.get("reason") != _TRUSTED_SESSION_START_REASON:
                if record.get("validation_run_id") == self.startup_expectations["validation_run_id"]:
                    self.startup_integrity_blockers.add("startup_start_reason_invalid")
                    self.events.append(compact_event(record))
                else:
                    self.warnings.append("non_target_session_start_ignored")
                return

        # Once the externally qualified target exists, process-scoped or other
        # non-target session envelopes are observational noise.  Validate every
        # record in the current target session, plus a trusted seq=1 reload
        # candidate; never let an unrelated process archive poison the sticky
        # startup contract after the target ends.
        if (
            self.require_startup_binding
            and self.startup_expectations is not None
            and self.startup_verified_session_id is not None
            and self.current_session_id is not None
        ):
            current_target_record = session_id == self.current_session_id
            trusted_reload_candidate = (
                session_started
                and record.get("reason") == _TRUSTED_SESSION_START_REASON
            )
            if not current_target_record and not trusted_reload_candidate:
                self.warnings.append("non_target_envelope_ignored")
                return

        startup_contract_valid = self._validate_startup_contract(
            record,
            session_started=session_started,
        )
        if (
            session_started
            and self.require_startup_binding
            and self.startup_expectations is not None
            and not startup_contract_valid
        ):
            self.events.append(compact_event(record))
            return

        if self.current_session_id != session_id:
            if session_id in self.retired_session_ids:
                self.retired_session_event_count += 1
                self.warnings.append(f"retired_session_event:{session_id}")
                return
            transition_allowed = session_started
            if is_state_snapshot(record):
                nonbaseline = {
                    "snapshot_stale",
                    "writer_status_missing",
                    "writer_dropped_snapshots",
                    "writer_drop_count_invalid",
                    "writer_queue_depth_invalid",
                    "writer_queue_not_empty",
                    "writer_fault_count_invalid",
                    "writer_faults_detected",
                    "writer_non_finite_count_invalid",
                    "writer_non_finite_numbers",
                    "writer_fault",
                }
                transition_allowed = self.allow_untrusted_snapshot_baseline or not (
                    set(snapshot_trust_blockers(record)) - nonbaseline
                )
            if not transition_allowed:
                self.warnings.append(f"unexpected_session_transition:{session_id}")
                return
            self._switch_session(session_id, session_start_observed=session_started)
        elif session_started:
            # A start envelope is valid only as the first seq=1 record that
            # establishes a session.  It cannot retroactively certify a
            # snapshot-first baseline or be replayed within the same session.
            self.warnings.append("session_start_order_invalid")
            self.session_integrity_blockers.add("session_start_order_invalid")
            self._validate_sequence(record.get("seq"))
            self.events.append(compact_event(record))
            return

        sequence_is_current = self._validate_sequence(record.get("seq"))
        self.events.append(compact_event(record))
        if not sequence_is_current:
            return
        if is_state_snapshot(record) and not self._validate_snapshot_sequence(record.get("snapshot_seq")):
            return

        self.session_ended = event_name in {"session_ended", "probe_shutdown"}

        if event_name == "probe_error" and "writer" in str(record.get("stage") or "").lower():
            self.session_integrity_blockers.add("writer_fault")

        if is_state_snapshot(record):
            self.latest = record
            self.session_ended = False
            # Historical/bounded replay validates each retained snapshot
            # against its frozen publication clock before it reaches this
            # baseline state machine.  Do not consult the current wall clock
            # again here: even an ignored freshness result would violate the
            # offline replay contract and make its behavior time-dependent.
            blockers = (
                []
                if self.allow_untrusted_snapshot_baseline
                else snapshot_trust_blockers(record)
            )
            if blockers:
                self.session_integrity_blockers.update(
                    set(blockers).intersection(_STICKY_SNAPSHOT_INTEGRITY_BLOCKERS)
                )
                for blocker in blockers:
                    self.warnings.append(f"untrusted_snapshot:{blocker}")
                if self.strategy_tracker.last_snapshot_id is not None:
                    self.strategy_tracker.reset(self.current_session_id)
                return
            # Signal evaluation is deterministic for one semantic telemetry
            # payload.  High-volume replay fixtures often change only sequence
            # identity fields; reuse the previous signal list while retaining
            # the timestamp/quality metadata in the equality key so freshness
            # changes still force a recalculation.
            signal_input = {
                key: value
                for key, value in record.items()
                if key not in {"snapshot_id", "snapshot_seq", "seq", "bridge"}
            }
            if self._last_signal_input == signal_input and self._last_signals is not None:
                signals = self._last_signals
            else:
                signals = agent_rules.evaluate_signals(record)
                self._last_signal_input = signal_input
                self._last_signals = signals
            self.incidents.update(record, signals)
            # Never trust a raw record's self-declared ``bridge`` object.  In
            # live mode only this ReplayState instance can establish the
            # session/sequence/freshness boundary, so overwrite the field
            # before accumulating strategy evidence.  ``--input`` replay is
            # non-actionable and intentionally skips discarded intermediate
            # strategy renders; the final diagnostic generation is evaluated
            # once by write_outputs.
            if self.allow_untrusted_snapshot_baseline:
                return
            if (
                self.session_start_observed
                and not self.session_integrity_blockers
                and not self.startup_integrity_blockers
            ):
                verified_record = dict(record)
                verified_record["bridge"] = {
                    "stale": False,
                    "snapshot_age_seconds": snapshot_age_seconds(record),
                    "trust_ready": True,
                    "trust_blockers": [],
                    "replay_mode": False,
                }
                try:
                    # evaluate_strategy validates the complete strategy before
                    # returning it.  Use the tracker fast path to avoid a
                    # second full schema walk for every replayed snapshot;
                    # public callers still get validation through observe().
                    self.strategy_tracker.observe_validated(
                        evaluate_strategy(verified_record, goal=self.strategy_goal)
                    )
                except Exception:
                    self.session_integrity_blockers.add("strategy_evaluation_error")
                    self.warnings.append("strategy_evaluation_error")
            elif self.strategy_tracker.last_snapshot_id is not None:
                self.strategy_tracker.reset(self.current_session_id)

    def _validate_startup_contract(
        self,
        record: Dict[str, Any],
        *,
        session_started: bool,
    ) -> bool:
        """Apply the external startup contract as a validation-run-sticky gate.

        The externally supplied loader path and initial file hash certify only
        the first trusted load.  The requested loader path remains a
        process-scoped anchor, while the current active path/save identity can
        advance only through the exact synchronous checkpoint evidence emitted
        by the Probe.  All migration state is deliberately retained across
        save-session switches so a complete ``state.jsonl`` replay reconstructs
        the same tip after a Bridge restart.
        """

        expected = self.startup_expectations
        if expected is None:
            return not self.require_startup_binding

        exact_fields = (
            ("validation_run_id", "validation_run_id", "startup_validation_run_mismatch"),
            ("startup_binding_hash", "startup_binding_hash", "startup_binding_hash_mismatch"),
            ("startup_intent_hash", "startup_intent_hash", "startup_intent_hash_mismatch"),
            (
                "runtime_config_evidence_sha256",
                "runtime_config_evidence_sha256",
                "startup_runtime_config_hash_mismatch",
            ),
            ("game_build", "game_build", "startup_game_build_mismatch"),
            ("mod_version", "mod_version", "startup_mod_version_mismatch"),
        )
        for record_field, expected_field, blocker in exact_fields:
            if record.get(record_field) != expected[expected_field]:
                self.startup_integrity_blockers.add(blocker)

        if record.get("startup_binding_status") != "verified":
            self.startup_integrity_blockers.add("startup_binding_unverified")

        if session_started and record.get("reason") != _TRUSTED_SESSION_START_REASON:
            self.startup_integrity_blockers.add("startup_start_reason_invalid")

        process_id = record.get("validation_process_id")
        if isinstance(process_id, bool) or not isinstance(process_id, int) or process_id <= 0:
            self.startup_integrity_blockers.add("startup_process_id_invalid")
        elif process_id != expected["validation_process_id"]:
            self.startup_integrity_blockers.add("startup_process_id_mismatch")
        elif self.validation_process_id is None:
            if session_started:
                self.validation_process_id = process_id
        elif process_id != self.validation_process_id:
            self.startup_integrity_blockers.add("startup_process_id_mismatch")

        if session_started:
            self._validate_startup_session_start(record, expected)
        else:
            self._validate_startup_session_record(record, expected)

        if self.startup_integrity_blockers and self.strategy_tracker.last_snapshot_id is not None:
            self.strategy_tracker.reset(self.current_session_id)
        return not self.startup_integrity_blockers

    def _validate_startup_session_start(
        self,
        record: Dict[str, Any],
        expected: Dict[str, Any],
    ) -> None:
        session_id = record.get("session_id")
        requested_path_id = record.get("requested_save_path_id")
        active_path_id = record.get("active_save_path_id")
        save_id_hash = record.get("save_id_hash")

        if requested_path_id != self.startup_requested_save_path_id:
            self.startup_integrity_blockers.add("startup_requested_path_mismatch")

        if self.startup_verified_session_id is None:
            if active_path_id != expected["loader_path_id_upper"]:
                self.startup_integrity_blockers.add("startup_active_path_mismatch")
            if save_id_hash != expected["loader_save_sha256"]:
                self.startup_integrity_blockers.add("startup_initial_save_hash_mismatch")
            if not self.startup_integrity_blockers:
                self.startup_current_active_save_path_id = active_path_id
                self.startup_current_save_id_hash = save_id_hash
        else:
            if self.startup_pending_save_evidence is not None:
                self.startup_integrity_blockers.add("startup_save_evidence_incomplete")
            if (
                not isinstance(session_id, str)
                or not session_id
                or session_id == self.current_session_id
                or session_id in self.retired_session_ids
            ):
                self.startup_integrity_blockers.add("startup_reload_session_invalid")
            if self.startup_reload_ready_session_id != self.current_session_id:
                self.startup_integrity_blockers.add("startup_reload_transition_missing")
            if active_path_id != self.startup_current_active_save_path_id:
                self.startup_integrity_blockers.add("startup_reload_active_path_mismatch")
            if save_id_hash != self.startup_current_save_id_hash:
                self.startup_integrity_blockers.add("startup_reload_save_hash_mismatch")

        if not self.startup_integrity_blockers and isinstance(session_id, str) and session_id:
            self.startup_verified_session_id = session_id
            self.startup_reload_ready_session_id = None
            self.startup_reload_unload_evidence = None

    def _validate_startup_session_record(
        self,
        record: Dict[str, Any],
        expected: Dict[str, Any],
    ) -> None:
        event_name = str(record.get("event") or "")

        if self.startup_reload_ready_session_id is not None:
            self.startup_integrity_blockers.add("startup_reload_evidence_order_invalid")
            self.startup_reload_ready_session_id = None

        pending = self.startup_pending_save_evidence
        if pending is not None:
            expected_event = (
                "save_write_started" if pending.get("stage") == "control" else "save_write_completed"
            )
            if event_name != expected_event:
                self.startup_integrity_blockers.add("startup_save_evidence_order_invalid")
                self.startup_pending_save_evidence = None

        unload = self.startup_reload_unload_evidence
        if unload is not None and event_name != "session_ended":
            self.startup_integrity_blockers.add("startup_reload_evidence_order_invalid")
            self.startup_reload_unload_evidence = None

        if event_name == "save_write_started":
            self._validate_startup_save_started(record)
            return
        if event_name == "save_write_completed":
            self._validate_startup_save_completed(record)
            return

        self._validate_startup_current_identity(record)

        if event_name == "validation_control_started":
            if record.get("operation") == "save_checkpoint":
                self._validate_startup_save_control(record, expected)
            elif record.get("operation") == "reload_checkpoint":
                self._validate_startup_reload_control(record, expected)
            else:
                self.startup_integrity_blockers.add("startup_validation_control_invalid")
        elif event_name == "save_unloaded":
            self._validate_startup_reload_unload(record)
        elif event_name == "session_ended":
            self._validate_startup_reload_end(record)

    def _validate_startup_current_identity(self, record: Dict[str, Any]) -> None:
        if record.get("requested_save_path_id") != self.startup_requested_save_path_id:
            self.startup_integrity_blockers.add("startup_requested_path_mismatch")
        if record.get("active_save_path_id") != self.startup_current_active_save_path_id:
            self.startup_integrity_blockers.add("startup_active_path_mismatch")
        if record.get("save_id_hash") != self.startup_current_save_id_hash:
            self.startup_integrity_blockers.add("startup_save_hash_mismatch")

    def _startup_record_is_next(self, record: Dict[str, Any], previous_seq: Optional[int]) -> bool:
        sequence = parse_positive_sequence(record.get("seq"))
        return sequence is not None and previous_seq is not None and sequence == previous_seq + 1

    def _validate_startup_save_control(
        self,
        record: Dict[str, Any],
        expected: Dict[str, Any],
    ) -> None:
        control_id = record.get("control_id")
        target_path_id = record.get("target_save_path_id")
        valid_control_id = (
            isinstance(control_id, str)
            and _VALIDATION_RUN_ID_RE.fullmatch(control_id) is not None
        )
        valid = (
            not self.startup_integrity_blockers
            and self.startup_pending_save_evidence is None
            and self._startup_record_is_next(record, self.last_seq)
            and record.get("session_id") == self.current_session_id
            and record.get("operation") == "save_checkpoint"
            and record.get("test_run_id") == expected["validation_run_id"]
            and valid_control_id
            and control_id not in self.startup_seen_save_control_ids
            and isinstance(target_path_id, str)
            and _HASH_ID_RE.fullmatch(target_path_id) is not None
        )
        if not valid:
            self.startup_integrity_blockers.add("startup_save_control_invalid")
            self.startup_pending_save_evidence = None
            return
        if (
            target_path_id != self.startup_current_active_save_path_id
            and (
                self.startup_distinct_path_transition_count != 0
                or self.startup_current_active_save_path_id != expected["loader_path_id_upper"]
            )
        ):
            self.startup_integrity_blockers.add("startup_duplicate_path_transition")
            self.startup_pending_save_evidence = None
            return
        assert isinstance(control_id, str)
        self.startup_seen_save_control_ids.add(control_id)
        self.startup_pending_save_evidence = {
            "stage": "control",
            "session_id": self.current_session_id,
            "control_id": control_id,
            "target_path_id": target_path_id,
            "before_save_id_hash": self.startup_current_save_id_hash,
            "control_seq": record.get("seq"),
        }

    def _validate_startup_reload_control(
        self,
        record: Dict[str, Any],
        expected: Dict[str, Any],
    ) -> None:
        control_id = record.get("control_id")
        valid = (
            not self.startup_integrity_blockers
            and self._startup_record_is_next(record, self.last_seq)
            and record.get("session_id") == self.current_session_id
            and record.get("test_run_id") == expected["validation_run_id"]
            and isinstance(control_id, str)
            and _VALIDATION_RUN_ID_RE.fullmatch(control_id) is not None
            and record.get("expected_save_path_id")
            == self.startup_current_active_save_path_id
            and record.get("expected_save_id_hash") == self.startup_current_save_id_hash
        )
        if not valid:
            self.startup_integrity_blockers.add("startup_reload_control_invalid")

    def _validate_startup_save_started(self, record: Dict[str, Any]) -> None:
        pending = self.startup_pending_save_evidence
        valid = (
            not self.startup_integrity_blockers
            and isinstance(pending, dict)
            and pending.get("stage") == "control"
            and record.get("session_id") == pending.get("session_id") == self.current_session_id
            and self._startup_record_is_next(record, pending.get("control_seq"))
            and self.last_seq == pending.get("control_seq")
            and record.get("is_auto_save") is False
            and record.get("update_save_pointer") is True
            and record.get("requested_save_path_id") == pending.get("target_path_id")
            and record.get("active_save_path_id") == self.startup_current_active_save_path_id
            and record.get("before_save_id_hash") == self.startup_current_save_id_hash
            and record.get("save_id_hash") == self.startup_current_save_id_hash
        )
        if not valid:
            self.startup_integrity_blockers.add("startup_save_start_invalid")
            self.startup_pending_save_evidence = None
            return
        pending["stage"] = "started"
        pending["start_seq"] = record.get("seq")

    def _validate_startup_save_completed(self, record: Dict[str, Any]) -> None:
        pending = self.startup_pending_save_evidence
        target_path_id = None if pending is None else pending.get("target_path_id")
        written_save_id_hash = record.get("written_save_id_hash")
        valid_written_hash = (
            isinstance(written_save_id_hash, str)
            and _HASH_ID_RE.fullmatch(written_save_id_hash) is not None
        )
        valid = (
            not self.startup_integrity_blockers
            and isinstance(pending, dict)
            and pending.get("stage") == "started"
            and record.get("session_id") == pending.get("session_id") == self.current_session_id
            and self._startup_record_is_next(record, pending.get("start_seq"))
            and self.last_seq == pending.get("start_seq")
            and record.get("startup_binding_status") == "verified"
            and record.get("success") is True
            and record.get("is_auto_save") is False
            and record.get("update_save_pointer") is True
            and record.get("save_file_readable") is True
            and record.get("file_rewrite_observed") is True
            and isinstance(target_path_id, str)
            and _HASH_ID_RE.fullmatch(target_path_id) is not None
            and record.get("requested_save_path_id") == target_path_id
            and record.get("result_save_path_id") == target_path_id
            and record.get("active_save_path_id") == target_path_id
            and record.get("before_save_id_hash") == self.startup_current_save_id_hash
            and record.get("before_save_id_hash") == pending.get("before_save_id_hash")
            and valid_written_hash
            and record.get("active_save_id_hash") == written_save_id_hash
            and record.get("save_id_hash") == written_save_id_hash
        )
        if not valid:
            self.startup_integrity_blockers.add("startup_save_completion_invalid")
            self.startup_pending_save_evidence = None
            return

        if target_path_id != self.startup_current_active_save_path_id:
            if self.startup_distinct_path_transition_count != 0:
                self.startup_integrity_blockers.add("startup_duplicate_path_transition")
                self.startup_pending_save_evidence = None
                return
            self.startup_current_active_save_path_id = target_path_id
            self.startup_distinct_path_transition_count += 1
        self.startup_current_save_id_hash = written_save_id_hash
        self.startup_pending_save_evidence = None

    def _validate_startup_reload_unload(self, record: Dict[str, Any]) -> None:
        if record.get("reason") != "save_loader_load_start":
            self.startup_reload_unload_evidence = None
            self.startup_reload_ready_session_id = None
            return
        if (
            self.startup_integrity_blockers
            or not self._startup_record_is_next(record, self.last_seq)
            or record.get("session_id") != self.current_session_id
        ):
            self.startup_integrity_blockers.add("startup_reload_unload_invalid")
            self.startup_reload_unload_evidence = None
            return
        self.startup_reload_unload_evidence = {
            "session_id": self.current_session_id,
            "seq": record.get("seq"),
            "active_save_path_id": self.startup_current_active_save_path_id,
            "save_id_hash": self.startup_current_save_id_hash,
        }

    def _validate_startup_reload_end(self, record: Dict[str, Any]) -> None:
        if record.get("reason") != "save_loader_load_start":
            self.startup_reload_unload_evidence = None
            self.startup_reload_ready_session_id = None
            return
        unload = self.startup_reload_unload_evidence
        valid = (
            not self.startup_integrity_blockers
            and isinstance(unload, dict)
            and unload.get("session_id") == self.current_session_id == record.get("session_id")
            and self._startup_record_is_next(record, unload.get("seq"))
            and self.last_seq == unload.get("seq")
            and unload.get("active_save_path_id") == self.startup_current_active_save_path_id
            and unload.get("save_id_hash") == self.startup_current_save_id_hash
        )
        if not valid:
            self.startup_integrity_blockers.add("startup_reload_end_invalid")
            self.startup_reload_unload_evidence = None
            self.startup_reload_ready_session_id = None
            return
        self.startup_reload_ready_session_id = self.current_session_id
        self.startup_reload_unload_evidence = None

    def _switch_session(self, session_id: str, *, session_start_observed: bool = False) -> None:
        if self.current_session_id:
            self.retired_session_ids.add(self.current_session_id)
        self.current_session_id = session_id
        self.latest = None
        self.events = collections.deque(maxlen=MAX_EVENT_WINDOW)
        self.incidents.reset(session_id)
        self._last_signal_input = None
        self._last_signals = None
        self.strategy_tracker.reset(session_id)
        self.last_seq = None
        self.last_snapshot_seq = None
        self.session_ended = False
        self.session_start_observed = session_start_observed
        self.session_integrity_blockers = set()

    def _validate_sequence(self, value: Any) -> bool:
        sequence = parse_positive_sequence(value)
        if sequence is None:
            self.warnings.append("invalid_sequence")
            self.session_integrity_blockers.add("sequence_invalid")
            self.strategy_tracker.reset(self.current_session_id)
            return False
        is_current = True
        if self.last_seq is not None:
            if sequence == self.last_seq:
                self.duplicate_seq_count += 1
                self.warnings.append(f"duplicate_seq:{sequence}")
                self.session_integrity_blockers.add("duplicate_sequence")
                self.strategy_tracker.reset(self.current_session_id)
                is_current = False
            elif sequence < self.last_seq:
                self.out_of_order_seq_count += 1
                self.warnings.append(f"out_of_order_seq:{sequence}")
                self.session_integrity_blockers.add("out_of_order_sequence")
                self.strategy_tracker.reset(self.current_session_id)
                is_current = False
            elif sequence > self.last_seq + 1:
                self.seq_gap_count += 1
                self.warnings.append(f"seq_gap:{self.last_seq}->{sequence}")
                self.session_integrity_blockers.add("sequence_gap")
                self.strategy_tracker.reset(self.current_session_id)
        self.last_seq = max(sequence, self.last_seq or sequence)
        return is_current

    def _validate_snapshot_sequence(self, value: Any) -> bool:
        sequence = parse_positive_sequence(value)
        if sequence is None:
            self.warnings.append("invalid_snapshot_sequence")
            self.session_integrity_blockers.add("snapshot_sequence_invalid")
            self.strategy_tracker.reset(self.current_session_id)
            return False
        is_current = True
        if self.last_snapshot_seq is None:
            if sequence != 1:
                self.snapshot_seq_gap_count += 1
                self.warnings.append(f"snapshot_seq_gap:0->{sequence}")
                self.session_integrity_blockers.add("snapshot_sequence_gap")
                self.strategy_tracker.reset(self.current_session_id)
        elif sequence == self.last_snapshot_seq:
            self.duplicate_snapshot_seq_count += 1
            self.warnings.append(f"duplicate_snapshot_seq:{sequence}")
            self.session_integrity_blockers.add("duplicate_snapshot_sequence")
            self.strategy_tracker.reset(self.current_session_id)
            is_current = False
        elif sequence < self.last_snapshot_seq:
            self.out_of_order_snapshot_seq_count += 1
            self.warnings.append(f"out_of_order_snapshot_seq:{sequence}")
            self.session_integrity_blockers.add("out_of_order_snapshot_sequence")
            self.strategy_tracker.reset(self.current_session_id)
            is_current = False
        elif sequence > self.last_snapshot_seq + 1:
            self.snapshot_seq_gap_count += 1
            self.warnings.append(f"snapshot_seq_gap:{self.last_snapshot_seq}->{sequence}")
            self.session_integrity_blockers.add("snapshot_sequence_gap")
            self.strategy_tracker.reset(self.current_session_id)
        self.last_snapshot_seq = max(sequence, self.last_snapshot_seq or sequence)
        return is_current

    def effective_latest(self, *, ignore_stale: bool = False) -> Optional[Dict[str, Any]]:
        if not isinstance(self.latest, dict):
            return None
        result = copy.deepcopy(self.latest)
        age, _, freshness_blockers = snapshot_freshness(result)
        if not ignore_stale and "snapshot_stale" in freshness_blockers:
            assert age is not None
            result = mark_stale(result, age)
        else:
            result["bridge"] = {
                "stale": False,
                "snapshot_age_seconds": None if age is None else round(age, 3),
            }
        return result

    def health(
        self,
        latest: Optional[Dict[str, Any]],
        policy: Dict[str, Any],
        *,
        replay_mode: bool = False,
    ) -> Dict[str, Any]:
        # Freeze one full-precision publication clock for both the projected
        # age and the causal publication timestamp.  Probe snapshots carry
        # sub-second timestamps; truncating health.updated_at to whole seconds
        # can otherwise make a freshly consumed snapshot appear to have been
        # observed after the Bridge publication that contains it.
        published_at = local_now()
        age = snapshot_age_seconds(latest, now=published_at)
        stale = bool(safe_get(latest or {}, "bridge.stale", False))
        trust_blockers: List[str] = []
        if not replay_mode:
            trust_blockers.extend(snapshot_trust_blockers(latest, now=published_at))
            trust_blockers.extend(self.session_integrity_blockers)
            trust_blockers.extend(self.startup_integrity_blockers)
            if not self.session_start_observed:
                trust_blockers.append("session_start_not_observed")
        trust_blockers = sorted(set(trust_blockers))
        if latest is None:
            status = "waiting_current_session_snapshot"
        elif self.session_ended:
            status = "session_ended"
        elif stale or "snapshot_stale" in trust_blockers:
            status = "stale"
        elif trust_blockers:
            status = "untrusted_snapshot"
        else:
            status = "ok"
        return {
            "schema": "oni.bridge.health.v1",
            "updated_at": published_at.isoformat(timespec="microseconds"),
            "status": status,
            "current_session_id": self.current_session_id,
            "latest_snapshot_id": None if latest is None else latest.get("snapshot_id"),
            "latest_seq": None if latest is None else latest.get("seq"),
            "snapshot_age_seconds": None if age is None else round(age, 3),
            "invalid_json_count": self.invalid_json_count,
            "duplicate_seq_count": self.duplicate_seq_count,
            "out_of_order_seq_count": self.out_of_order_seq_count,
            "seq_gap_count": self.seq_gap_count,
            "duplicate_snapshot_seq_count": self.duplicate_snapshot_seq_count,
            "out_of_order_snapshot_seq_count": self.out_of_order_snapshot_seq_count,
            "snapshot_seq_gap_count": self.snapshot_seq_gap_count,
            "retired_session_event_count": self.retired_session_event_count,
            "partial_bytes": self.partial_bytes,
            "rotation_count": self.rotation_count,
            "rotation_tail_records_total": self.rotation_tail_records_total,
            "last_rotation_tail": self.last_rotation_tail,
            "last_event_seq": self.last_seq,
            "last_snapshot_seq": self.last_snapshot_seq,
            "actions_enabled": bool(policy.get("actions_enabled", False)),
            "policy_error": policy.get("policy_error"),
            "trust_ready": status == "ok",
            "trust_blockers": trust_blockers,
            "session_start_observed": self.session_start_observed,
            "startup_binding": {
                "required": self.require_startup_binding,
                "external_expectations_supplied": self.startup_expectations is not None,
                "verified": bool(self.startup_verified_session_id)
                and not self.startup_integrity_blockers,
                "verified_session_id": self.startup_verified_session_id,
                "validation_process_id": self.validation_process_id,
                "requested_save_path_id": self.startup_requested_save_path_id,
                "current_active_save_path_id": self.startup_current_active_save_path_id,
                "current_save_id_hash": self.startup_current_save_id_hash,
                "distinct_path_transition_count": self.startup_distinct_path_transition_count,
                "save_evidence_pending": self.startup_pending_save_evidence is not None,
                "reload_transition_ready": self.startup_reload_ready_session_id is not None,
                "blockers": sorted(self.startup_integrity_blockers),
            },
            "replay_mode": replay_mode,
            "warnings": list(self.warnings),
        }


def format_unknown(value: Any) -> str:
    return "unknown" if value is None else str(value)


def truncate_text(value: Any, limit: int = 200) -> str:
    text = format_unknown(value).replace("\r", " ").replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def format_sequence(value: Any, limit: int = 8) -> str:
    if not isinstance(value, list):
        return "unknown"
    if not value:
        return "none observed"
    return ", ".join(str(item) for item in value[:limit])


def format_top_prefabs(value: Any, limit: int = 6) -> str:
    if not isinstance(value, list):
        return "unknown"
    if not value:
        return "none observed"
    formatted: List[str] = []
    for item in value[:limit]:
        if isinstance(item, dict) and item.get("id") is not None and item.get("count") is not None:
            formatted.append(f"{item['id']} x{item['count']}")
    return ", ".join(formatted) if formatted else "unknown"


def format_duplicant_priority_targets(value: Any, limit: int = 8) -> List[str]:
    if not isinstance(value, dict):
        return ["- unknown"]
    if not value:
        return ["- none observed"]
    targets = [item for item in value.values() if isinstance(item, dict)]
    targets.sort(key=lambda item: int(item.get("instance_id") or 0))
    lines: List[str] = []
    for item in targets[:limit]:
        priorities = item.get("chore_priorities") if isinstance(item.get("chore_priorities"), dict) else {}
        values = ", ".join(f"{key}={priorities[key]}" for key in sorted(priorities))
        lines.append(
            f"- id_{format_unknown(item.get('instance_id'))} {truncate_text(item.get('name'), 80)}: "
            + (values or "priorities unavailable")
        )
    return lines


def format_dig_targets(value: Any, limit: int = 8) -> List[str]:
    if not isinstance(value, dict):
        return ["- unknown"]
    targets = [item for item in value.values() if isinstance(item, dict)]
    targets.sort(key=lambda item: int(item.get("cell") or 0))
    visible = [
        item
        for item in targets
        if item.get("designation_present") is True or item.get("safe_mark_candidate") is True
    ]
    if not visible:
        return ["- none observed"]
    lines: List[str] = []
    for item in visible[:limit]:
        if item.get("designation_present") is True:
            state = (
                f"designation={format_unknown(item.get('designation_instance_id'))} "
                f"kind={format_unknown(item.get('designation_kind'))} "
                f"in_progress={format_unknown(item.get('designation_in_progress'))}"
            )
        else:
            state = "safe mark candidate"
        lines.append(
            f"- cell_{format_unknown(item.get('cell'))} "
            f"({format_unknown(item.get('x'))},{format_unknown(item.get('y'))}) "
            f"{format_unknown(item.get('element_id'))}: {state}"
        )
    return lines


def format_blueprint_catalog(value: Any) -> List[str]:
    if not isinstance(value, dict):
        return ["- catalog unavailable"]
    lines: List[str] = []
    for def_id in ("Ladder", "Wire"):
        item = value.get(def_id) if isinstance(value.get(def_id), dict) else None
        if item is None:
            lines.append(f"- {def_id}: unavailable")
            continue
        lines.append(
            f"- {def_id}: materials={format_sequence(item.get('material_tags'))}; "
            f"available={format_unknown(item.get('materials_available'))}; "
            f"candidates={format_sequence(item.get('candidate_cells'))}"
        )
    return lines


def format_blueprint_targets(value: Any, limit: int = 12) -> List[str]:
    if not isinstance(value, dict):
        return ["- cells unavailable"]
    targets = [item for item in value.values() if isinstance(item, dict)]
    targets.sort(key=lambda item: int(item.get("cell") or 0))
    visible = [
        item
        for item in targets
        if item.get("blueprint_present") is True
        or item.get("can_place_ladder") is True
        or item.get("can_place_wire") is True
    ]
    if not visible:
        return ["- none observed"]
    lines: List[str] = []
    for item in visible[:limit]:
        if item.get("blueprint_present") is True:
            state = (
                f"blueprint={format_unknown(item.get('blueprint_instance_id'))} "
                f"prefab={format_unknown(item.get('blueprint_prefab_id'))} "
                f"in_progress={format_unknown(item.get('blueprint_in_progress'))}"
            )
        else:
            allowed = []
            if item.get("can_place_ladder") is True:
                allowed.append("Ladder")
            if item.get("can_place_wire") is True:
                allowed.append("Wire")
            state = "safe for " + ",".join(allowed)
        lines.append(
            f"- cell_{format_unknown(item.get('cell'))} "
            f"({format_unknown(item.get('x'))},{format_unknown(item.get('y'))}) "
            f"{format_unknown(item.get('element_id'))}: {state}"
        )
    return lines


def format_utility_paths(value: Any, limit: int = 8) -> List[str]:
    if not isinstance(value, dict):
        return ["- paths unavailable"]
    if not value:
        return ["- none observed"]
    lines: List[str] = []
    for key in sorted(value)[:limit]:
        item = value.get(key) if isinstance(value.get(key), dict) else {}
        lines.append(
            f"- {format_unknown(item.get('path_id'))}: "
            f"{format_unknown(item.get('building_def_id'))} "
            f"cells={format_sequence(item.get('cells'))} "
            f"instances={format_sequence(item.get('active_blueprint_instance_ids'))} "
            f"fully_active={format_unknown(item.get('fully_active'))}"
        )
    return lines


def format_quality_value(state: Dict[str, Any], path: str, value: Any, suffix: str = "") -> str:
    status = str(field_quality(state, path).get("status") or "unsupported")
    if status not in {"observed", "estimated"}:
        return f"unknown ({status})"
    text = format_unknown(value)
    return text + suffix if suffix else text


def build_summary(
    latest: Optional[Dict[str, Any]],
    events: Iterable[Dict[str, Any]],
    incident_payload: Dict[str, Any],
    health: Dict[str, Any],
) -> str:
    lines: List[str] = [
        "# ONI AI Colony State",
        "",
        f"Updated: {now_iso()}",
        f"Bridge status: {health.get('status')}",
        f"Session: {health.get('current_session_id') or 'unknown'}",
        "",
    ]
    if latest is None:
        lines.extend(
            [
                "No state snapshot has been observed for the current session.",
                "",
                "The bridge will not reuse a snapshot from an older session.",
            ]
        )
        return "\n".join(lines) + "\n"

    duplicants = latest.get("duplicants") if isinstance(latest.get("duplicants"), dict) else {}
    resources = latest.get("resources") if isinstance(latest.get("resources"), dict) else {}
    research = latest.get("research") if isinstance(latest.get("research"), dict) else {}
    buildings = latest.get("buildings") if isinstance(latest.get("buildings"), dict) else {}
    designations = latest.get("designations") if isinstance(latest.get("designations"), dict) else {}
    dig = designations.get("dig") if isinstance(designations.get("dig"), dict) else {}
    dig_tool = dig.get("tool") if isinstance(dig.get("tool"), dict) else {}
    blueprints = latest.get("blueprints") if isinstance(latest.get("blueprints"), dict) else {}
    cells = latest.get("cells") if isinstance(latest.get("cells"), dict) else {}
    alerts = latest.get("alerts")
    observed, estimated, blocked = quality_summary(latest)

    lines.extend(
        [
            "## Snapshot",
            "",
            f"- Snapshot: {latest.get('snapshot_id') or 'legacy'}",
            f"- Sequence: {format_unknown(latest.get('seq'))}",
            f"- Age: {format_unknown(health.get('snapshot_age_seconds'))} seconds",
            f"- Game build: {format_unknown(latest.get('game_build'))}",
            f"- Mod version: {format_unknown(latest.get('mod_version'))}",
            f"- World: {format_unknown(latest.get('world_id'))}",
            f"- Cycle: {format_quality_value(latest, 'cycle', latest.get('cycle'))}",
            f"- Paused: {format_quality_value(latest, 'game.paused', safe_get(latest, 'game.paused'))}",
            f"- Duplicants total: {format_quality_value(latest, 'duplicants.total', duplicants.get('total'))}",
            f"- Stressed: {format_quality_value(latest, 'duplicants.stressed', duplicants.get('stressed'))}",
            f"- Idle: {format_quality_value(latest, 'duplicants.idle', duplicants.get('idle'))}",
            f"- Food: {format_quality_value(latest, 'resources.food_kcal', resources.get('food_kcal'), ' kcal')}",
            f"- Oxygen estimate: {format_quality_value(latest, 'resources.oxygen_kg_estimate', resources.get('oxygen_kg_estimate'), ' kg')}",
            f"- Water estimate: {format_quality_value(latest, 'resources.water_kg_estimate', resources.get('water_kg_estimate'), ' kg')}",
            f"- Research: {format_quality_value(latest, 'research.current', research.get('current'))}",
            f"- Alerts: {format_quality_value(latest, 'alerts', format_sequence(alerts))}",
            f"- Quality: observed {observed}, estimated {estimated}, blocked {blocked}",
            "",
            "## Duplicant Priority Targets",
            "",
        ]
    )
    lines.extend(format_duplicant_priority_targets(duplicants.get("by_instance_id")))
    lines.extend(
        [
            "",
            "## Dig Designations",
            "",
            f"- Tool available: {format_quality_value(latest, 'designations.dig.tool.available', dig_tool.get('available'))}",
            f"- Tiles filter: {format_quality_value(latest, 'designations.dig.tool.tiles_filter_on', dig_tool.get('tiles_filter_on'))}",
            f"- Natural backwall filter: {format_quality_value(latest, 'designations.dig.tool.natural_backwall_filter_on', dig_tool.get('natural_backwall_filter_on'))}",
            f"- Safe candidates: {format_quality_value(latest, 'designations.dig.candidate_cells', format_sequence(dig.get('candidate_cells')))}",
            "",
        ]
    )
    lines.extend(format_dig_targets(dig.get("by_cell")))
    lines.extend(
        [
            "",
            "## Blueprint Actions",
            "",
            f"- Sandbox mode: {format_quality_value(latest, 'game.sandbox_mode', safe_get(latest, 'game.sandbox_mode'))}",
            f"- Instant build mode: {format_quality_value(latest, 'game.instant_build_mode', safe_get(latest, 'game.instant_build_mode'))}",
        ]
    )
    lines.extend(format_blueprint_catalog(blueprints.get("catalog")))
    lines.append("")
    lines.extend(format_blueprint_targets(cells.get("by_id")))
    lines.append("")
    lines.extend(format_utility_paths(blueprints.get("utility_paths")))
    lines.extend(
        [
            "",
            "## Core Buildings",
            "",
            f"- Oxygen diffuser: {format_quality_value(latest, 'buildings.oxygen_diffuser', buildings.get('oxygen_diffuser'))}",
            f"- Outhouse: {format_quality_value(latest, 'buildings.outhouse', buildings.get('outhouse'))}",
            f"- Manual generator: {format_quality_value(latest, 'buildings.manual_generator', buildings.get('manual_generator'))}",
            f"- Building scan count: {format_unknown(buildings.get('_scanned'))}",
            f"- Building scan source: {format_unknown(buildings.get('_source'))}",
            f"- Building samples: {format_sequence(buildings.get('_sample_ids'))}",
            f"- Top building prefabs: {format_top_prefabs(buildings.get('_top_prefabs'))}",
            "",
            "## Incidents",
            "",
        ]
    )

    incident_items = incident_payload.get("items") if isinstance(incident_payload, dict) else []
    visible = [
        item
        for item in incident_items or []
        if item.get("status") in {"open", "pending", "blocked_unknown", "cooldown"}
    ]
    if not visible:
        lines.append("- none")
    else:
        for item in visible[:12]:
            lines.append(
                f"- {item.get('incident_id')}: {item.get('status')} "
                f"(samples={item.get('consecutive_samples')}, telemetry={item.get('telemetry_state')})"
            )

    lines.extend(["", "## Recent Events", ""])
    recent = list(events)[-10:]
    if not recent:
        lines.append("- none")
    else:
        for event in recent:
            message = event.get("message")
            suffix = f" - {truncate_text(message)}" if message else ""
            lines.append(
                f"- {format_unknown(event.get('ts'))}: "
                f"{format_unknown(event.get('event'))} seq={format_unknown(event.get('seq'))}{suffix}"
            )
    return bound_summary("\n".join(lines[:MAX_SUMMARY_LINES]).rstrip() + "\n")


def bound_summary(text: str) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= MAX_SUMMARY_BYTES:
        return text
    suffix = "\n[summary truncated]\n"
    room = MAX_SUMMARY_BYTES - len(suffix.encode("utf-8"))
    clipped = encoded[: max(0, room)].decode("utf-8", errors="ignore").rstrip()
    return clipped + suffix


def _replace_with_retry(temporary: pathlib.Path, path: pathlib.Path) -> None:
    """Replace ``path`` while tolerating short-lived sharing locks."""

    delay = _ATOMIC_REPLACE_INITIAL_DELAY_SECONDS
    for attempt in range(_ATOMIC_REPLACE_ATTEMPTS):
        try:
            temporary.replace(path)
            return
        except OSError as exc:
            if attempt + 1 >= _ATOMIC_REPLACE_ATTEMPTS or not _is_retryable_replace_error(exc):
                raise
            time.sleep(delay)
            delay = min(delay * 2, _ATOMIC_REPLACE_MAX_DELAY_SECONDS)


def _is_retryable_replace_error(exc: OSError) -> bool:
    return (
        isinstance(exc, PermissionError)
        or exc.errno in _RETRYABLE_REPLACE_ERRNOS
        or getattr(exc, "winerror", None) in _RETRYABLE_REPLACE_WINERRORS
    )


def atomic_write_text(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Optional[pathlib.Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", delete=False, dir=str(path.parent), suffix=".tmp"
        ) as handle:
            temporary = pathlib.Path(handle.name)
            handle.write(text)
        _replace_with_retry(temporary, path)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def atomic_write_json(path: pathlib.Path, payload: Any) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def publish_bridge_fault(bridge_dir: pathlib.Path, exc: BaseException) -> None:
    """Leave an explicit untrusted health document before the Bridge exits.

    A crash used to leave the last ``ok`` health in place until it aged out
    (run18: CommandCustodyConflict).  Consumers require status ``ok`` and
    trust_ready, so this document fails closed immediately.  Best effort: the
    process exits non-zero regardless.
    """

    try:
        atomic_write_json(
            bridge_dir / "health.json",
            {
                "schema": "oni.bridge.health.v1",
                "status": "bridge_fault",
                "trust_ready": False,
                "replay_mode": False,
                "session_start_observed": False,
                "published_at": now_iso(),
                "fault": {
                    "type": type(exc).__name__,
                    "message": str(exc)[:500],
                },
            },
        )
    except Exception:  # noqa: BLE001 - the original exception is re-raised
        pass


def safe_session_name(value: Optional[str]) -> str:
    text = value or "waiting"
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text)[:120] or "waiting"


def output_source_identity(latest: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Bind one loose-output generation to the exact state used to build it."""

    state = latest if isinstance(latest, dict) else {}
    return {
        "session_id": state.get("session_id"),
        "snapshot_id": state.get("snapshot_id"),
        "state_hash": canonical_state_hash(state),
    }


def archive_session_records(
    bridge_dir: pathlib.Path,
    records: Iterable[Dict[str, Any]],
    *,
    rebuild: bool,
) -> None:
    grouped: Dict[str, List[str]] = collections.defaultdict(list)
    for record in records:
        session_name = safe_session_name(str(record.get("session_id") or "legacy"))
        grouped[session_name].append(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")))

    for session_name, lines in grouped.items():
        path = bridge_dir / "sessions" / session_name / "state.jsonl"
        text = "\n".join(lines) + "\n"
        if rebuild:
            atomic_write_text(path, text)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(text)


class SessionArchiveBuilder:
    def __init__(self, bridge_dir: pathlib.Path) -> None:
        self.bridge_dir = bridge_dir
        # Only the bounded replay input is retained in memory.  Existing session
        # history is copied and de-duplicated as a stream during commit instead
        # of being loaded wholesale on every Bridge restart.
        self.entries: Dict[str, Tuple[pathlib.Path, List[str], Set[str]]] = {}
        self.temporaries: Set[pathlib.Path] = set()

    @staticmethod
    def _canonical_line(record: Dict[str, Any]) -> str:
        return json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @classmethod
    def _existing_canonical_line(cls, raw: bytes) -> Optional[str]:
        try:
            value = json.loads(
                raw.decode("utf-8-sig"),
                parse_constant=reject_nonfinite_json_constant,
            )
        except (UnicodeError, json.JSONDecodeError, ValueError):
            return None
        return cls._canonical_line(value) if isinstance(value, dict) else None

    @staticmethod
    def _identity_version(metadata: os.stat_result) -> Tuple[int, int, int, int, int, int, int]:
        """Return the fields which must stay stable across an archive no-op.

        The file identity prevents a same-name replacement from being accepted;
        size and timestamps reject in-place rewrites before the byte comparison.
        ``st_mode`` and ``st_nlink`` keep the no-op limited to one ordinary file.
        """

        return (
            int(metadata.st_dev),
            int(metadata.st_ino),
            stat.S_IFMT(metadata.st_mode),
            int(metadata.st_nlink),
            int(metadata.st_size),
            int(metadata.st_mtime_ns),
            int(metadata.st_ctime_ns),
        )

    @classmethod
    def _path_matches_identity_version(
        cls,
        metadata: os.stat_result,
        expected: Tuple[int, int, int, int, int, int, int],
    ) -> bool:
        """Match path metadata without Windows' handle/path ctime cache skew."""

        current = cls._identity_version(metadata)
        return current[:6] == expected[:6]

    def _archive_path_safe_for_identity_noop(self, target: pathlib.Path) -> bool:
        """Reject hard links and reparse/symlink paths from identity-preserving no-op."""

        target_metadata = target.lstat()
        if (
            not stat.S_ISREG(target_metadata.st_mode)
            or int(target_metadata.st_nlink) != 1
            or bool(int(getattr(target_metadata, "st_file_attributes", 0)) & 0x400)
        ):
            return False

        boundary = self.bridge_dir.absolute()
        current = target.parent.absolute()
        while True:
            metadata = current.lstat()
            if stat.S_ISLNK(metadata.st_mode) or bool(int(getattr(metadata, "st_file_attributes", 0)) & 0x400):
                return False
            if current == boundary:
                return True
            parent = current.parent
            if parent == current:
                return False
            current = parent

    @staticmethod
    def _only_expected_archive_temporary(
        target: pathlib.Path,
        temporary: pathlib.Path,
    ) -> bool:
        """Require the commit candidate to be the directory's only ``*.tmp``."""

        temporaries = [
            child.absolute()
            for child in target.parent.iterdir()
            if child.name.endswith(".tmp")
        ]
        if temporaries != [temporary.absolute()]:
            return False
        metadata = temporary.lstat()
        return (
            stat.S_ISREG(metadata.st_mode)
            and int(metadata.st_nlink) == 1
            and not bool(int(getattr(metadata, "st_file_attributes", 0)) & 0x400)
        )

    @classmethod
    def _files_have_identical_bytes(
        cls,
        candidate: pathlib.Path,
        target: pathlib.Path,
        expected_target_version: Tuple[int, int, int, int, int, int, int],
    ) -> bool:
        """Compare a candidate while pinning the target's identity and version."""

        with target.open("rb") as target_handle, candidate.open("rb") as candidate_handle:
            if cls._identity_version(os.fstat(target_handle.fileno())) != expected_target_version:
                return False
            while True:
                target_chunk = target_handle.read(1024 * 1024)
                candidate_chunk = candidate_handle.read(1024 * 1024)
                if target_chunk != candidate_chunk:
                    return False
                if not target_chunk:
                    break
            if cls._identity_version(os.fstat(target_handle.fileno())) != expected_target_version:
                return False
        return cls._path_matches_identity_version(target.lstat(), expected_target_version)

    def add(self, record: Dict[str, Any]) -> None:
        session_name = safe_session_name(str(record.get("session_id") or "legacy"))
        entry = self.entries.get(session_name)
        if entry is None:
            target = self.bridge_dir / "sessions" / session_name / "state.jsonl"
            target.parent.mkdir(parents=True, exist_ok=True)
            entry = (target, [], set())
            self.entries[session_name] = entry
        line = self._canonical_line(record)
        if line in entry[2]:
            return
        entry[1].append(line)
        entry[2].add(line)

    def commit(self) -> None:
        for target, incoming_lines, incoming_set in self.entries.values():
            temporary: Optional[pathlib.Path] = None
            remaining = set(incoming_set)
            target_version: Optional[Tuple[int, int, int, int, int, int, int]] = None
            archive_is_exact_canonical_input = False
            preexisting_temporary = True
            try:
                try:
                    preexisting_temporary = any(
                        child.name.endswith(".tmp") for child in target.parent.iterdir()
                    )
                except OSError:
                    # Directory enumeration is part of the no-op proof.  If it
                    # cannot be completed, retain the existing atomic replace.
                    preexisting_temporary = True
                with tempfile.NamedTemporaryFile(
                    "w+b",
                    delete=False,
                    dir=str(target.parent),
                    suffix=".tmp",
                ) as output:
                    temporary = pathlib.Path(output.name)
                    self.temporaries.add(temporary)
                    last_byte_was_newline = True
                    if target.exists():
                        with target.open("rb") as source:
                            target_version = self._identity_version(os.fstat(source.fileno()))
                            archive_is_exact_canonical_input = True
                            existing_lines: Set[str] = set()
                            incoming_index = 0
                            while True:
                                raw = source.readline(MAX_EVIDENCE_RECORD_BYTES + 1)
                                if not raw:
                                    break
                                output.write(raw)
                                last_byte_was_newline = raw.endswith(b"\n")
                                if len(raw) > MAX_EVIDENCE_RECORD_BYTES:
                                    archive_is_exact_canonical_input = False
                                if len(raw) > MAX_EVIDENCE_RECORD_BYTES and not last_byte_was_newline:
                                    # Preserve an oversized existing record without
                                    # retaining it.  It cannot equal a bounded new
                                    # record byte-for-byte, so no de-duplication is
                                    # lost by streaming the remainder directly.
                                    while not last_byte_was_newline:
                                        raw = source.readline(64 * 1024)
                                        if not raw:
                                            break
                                        output.write(raw)
                                        last_byte_was_newline = raw.endswith(b"\n")
                                    continue
                                canonical = self._existing_canonical_line(raw.rstrip(b"\r\n"))
                                if canonical is not None:
                                    remaining.discard(canonical)
                                canonical_raw = (
                                    (canonical + "\n").encode("utf-8")
                                    if canonical is not None
                                    else None
                                )
                                if (
                                    canonical_raw != raw
                                    or canonical in existing_lines
                                    or incoming_index >= len(incoming_lines)
                                    or canonical != incoming_lines[incoming_index]
                                ):
                                    archive_is_exact_canonical_input = False
                                if canonical is not None:
                                    existing_lines.add(canonical)
                                incoming_index += 1
                            if incoming_index != len(incoming_lines):
                                archive_is_exact_canonical_input = False
                            if self._identity_version(os.fstat(source.fileno())) != target_version:
                                archive_is_exact_canonical_input = False
                    if target.exists() and target.stat().st_size > 0 and not last_byte_was_newline:
                        output.write(b"\n")
                    for line in incoming_lines:
                        if line in remaining:
                            output.write((line + "\n").encode("utf-8"))
                    output.flush()
                    os.fsync(output.fileno())
                preserve_identity = False
                if (
                    target_version is not None
                    and archive_is_exact_canonical_input
                    and not remaining
                    and not preexisting_temporary
                ):
                    try:
                        preserve_identity = (
                            self._archive_path_safe_for_identity_noop(target)
                            and self._path_matches_identity_version(target.lstat(), target_version)
                            and self._only_expected_archive_temporary(target, temporary)
                            and self._files_have_identical_bytes(temporary, target, target_version)
                            and self._only_expected_archive_temporary(target, temporary)
                            and self._archive_path_safe_for_identity_noop(target)
                            and self._path_matches_identity_version(target.lstat(), target_version)
                        )
                    except OSError:
                        preserve_identity = False
                if preserve_identity:
                    temporary.unlink()
                    self.temporaries.discard(temporary)
                    continue
                _replace_with_retry(temporary, target)
                self.temporaries.discard(temporary)
            finally:
                if temporary is not None and temporary.exists():
                    temporary.unlink(missing_ok=True)
                    self.temporaries.discard(temporary)

    def abort(self) -> None:
        for temporary in list(self.temporaries):
            temporary.unlink(missing_ok=True)
            self.temporaries.discard(temporary)


def _strict_json_object(path: pathlib.Path) -> Optional[Dict[str, Any]]:
    try:
        with path.open("rb") as handle:
            payload = handle.read(MAX_EVIDENCE_RECORD_BYTES + 1)
        if len(payload) > MAX_EVIDENCE_RECORD_BYTES:
            return None
        value = json.loads(
            payload.decode("utf-8-sig"),
            parse_constant=reject_nonfinite_json_constant,
        )
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _relative_evidence_path(bridge_dir: pathlib.Path, path: pathlib.Path) -> str:
    try:
        return str(path.relative_to(bridge_dir))
    except ValueError:
        return str(path)


def _new_evidence_command(directory_name: str, path: pathlib.Path, command: Dict[str, Any]) -> Dict[str, Any]:
    command_id = command.get("id")
    session_id = command.get("session_id")
    source_snapshot_id = command.get("source_snapshot_id")
    attempt_id = command.get("attempt_id")
    blockers: Set[str] = set()
    if not isinstance(command_id, str) or not command_id:
        blockers.add("command_id_invalid")
    if not isinstance(session_id, str) or not session_id:
        blockers.add("session_id_invalid")
    if not isinstance(source_snapshot_id, str) or not source_snapshot_id:
        blockers.add("source_snapshot_id_invalid")
    if not isinstance(attempt_id, str) or not attempt_id:
        blockers.add("attempt_id_invalid")
    return {
        "directory": directory_name,
        "path": path,
        "command_id": command_id,
        "session_id": session_id,
        "source_snapshot_id": source_snapshot_id,
        "attempt_id": attempt_id,
        "ack_sequences": {
            stage: parse_positive_sequence(command.get(field))
            for stage, field in _ACK_SEQUENCE_FIELDS.items()
        },
        "blockers": blockers,
        "source_matches": {},
        "ack_matches": {},
        "archive_source_lines": set(),
        "archive_ack_lines": set(),
    }


def _discover_unresolved_evidence_commands(bridge_dir: pathlib.Path) -> Tuple[List[Dict[str, Any]], Set[str]]:
    commands: List[Dict[str, Any]] = []
    global_blockers: Set[str] = set()
    for directory_name in _EVIDENCE_PIPELINE_DIRS:
        directory = bridge_dir / "commands" / directory_name
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.json")):
            if len(commands) >= MAX_EVIDENCE_COMMANDS:
                global_blockers.add("unresolved_command_limit_exceeded")
                break
            command = _strict_json_object(path)
            if command is None:
                commands.append(
                    {
                        "directory": directory_name,
                        "path": path,
                        "command_id": None,
                        "session_id": None,
                        "source_snapshot_id": None,
                        "attempt_id": None,
                        "ack_sequences": {},
                        "blockers": {"command_document_invalid"},
                        "source_matches": {},
                        "ack_matches": {},
                        "archive_source_lines": set(),
                        "archive_ack_lines": set(),
                    }
                )
                continue
            commands.append(_new_evidence_command(directory_name, path, command))
    by_id: Dict[str, List[Dict[str, Any]]] = collections.defaultdict(list)
    safe_sessions: Dict[str, Set[str]] = collections.defaultdict(set)
    for entry in commands:
        if isinstance(entry.get("command_id"), str):
            by_id[str(entry["command_id"])].append(entry)
        if isinstance(entry.get("session_id"), str):
            safe_sessions[safe_session_name(str(entry["session_id"]))].add(str(entry["session_id"]))
    for duplicates in by_id.values():
        if len(duplicates) > 1:
            for entry in duplicates:
                entry["blockers"].add("pipeline_command_ambiguous")
    for session_ids in safe_sessions.values():
        if len(session_ids) > 1:
            for entry in commands:
                if entry.get("session_id") in session_ids:
                    entry["blockers"].add("session_archive_path_ambiguous")
    return commands, global_blockers


def _record_match(
    entry: Dict[str, Any],
    bucket_name: str,
    record: Dict[str, Any],
    canonical: str,
    match_budget: Dict[str, int],
    *,
    from_archive: bool,
) -> None:
    bucket = entry[bucket_name]
    if canonical in bucket:
        if from_archive:
            archive_bucket = "archive_source_lines" if bucket_name == "source_matches" else "archive_ack_lines"
            entry[archive_bucket].add(canonical)
        return
    if len(bucket) >= MAX_EVIDENCE_VARIANTS_PER_REQUIREMENT:
        entry["blockers"].add("evidence_variant_limit_exceeded")
        return
    canonical_bytes = len(canonical.encode("utf-8"))
    if int(match_budget.get("bytes") or 0) + canonical_bytes > MAX_EVIDENCE_MATCH_BYTES:
        entry["blockers"].add("evidence_match_budget_exceeded")
        return
    match_budget["bytes"] = int(match_budget.get("bytes") or 0) + canonical_bytes
    bucket[canonical] = record
    if from_archive:
        archive_bucket = "archive_source_lines" if bucket_name == "source_matches" else "archive_ack_lines"
        entry[archive_bucket].add(canonical)


def _mark_damaged_candidate(raw: bytes, entries: List[Dict[str, Any]], blocker: str) -> None:
    for entry in entries:
        source_id = entry.get("source_snapshot_id")
        command_id = entry.get("command_id")
        attempt_id = entry.get("attempt_id")
        markers = [
            value.encode("utf-8")
            for value in (source_id, command_id, attempt_id)
            if isinstance(value, str) and value
        ]
        if any(marker in raw for marker in markers):
            entry["blockers"].add(blocker)


def _scan_evidence_source(
    bridge_dir: pathlib.Path,
    path: pathlib.Path,
    kind: str,
    archive_session_id: Optional[str],
    entries: List[Dict[str, Any]],
    match_budget: Dict[str, int],
) -> Dict[str, Any]:
    summary: Dict[str, Any] = {
        "path": _relative_evidence_path(bridge_dir, path),
        "kind": kind,
        "complete_lines": 0,
        "invalid_lines": 0,
        "oversized_lines": 0,
        "partial_bytes": 0,
        "io_error": False,
    }
    snapshots: Dict[str, List[Dict[str, Any]]] = collections.defaultdict(list)
    acks: Dict[Tuple[str, str], List[Dict[str, Any]]] = collections.defaultdict(list)
    for entry in entries:
        if isinstance(entry.get("source_snapshot_id"), str):
            snapshots[str(entry["source_snapshot_id"])].append(entry)
        if isinstance(entry.get("command_id"), str) and isinstance(entry.get("attempt_id"), str):
            acks[(str(entry["command_id"]), str(entry["attempt_id"]))].append(entry)
    try:
        with path.open("rb") as handle:
            while True:
                raw = handle.readline(MAX_EVIDENCE_RECORD_BYTES + 1)
                if not raw:
                    break
                if len(raw) > MAX_EVIDENCE_RECORD_BYTES and not raw.endswith(b"\n"):
                    summary["oversized_lines"] += 1
                    marker_window = bytearray(raw[-512:])
                    _mark_damaged_candidate(raw, entries, "evidence_candidate_oversized")
                    while not raw.endswith(b"\n"):
                        raw = handle.readline(64 * 1024)
                        if not raw:
                            break
                        combined = bytes(marker_window) + raw
                        _mark_damaged_candidate(combined, entries, "evidence_candidate_oversized")
                        marker_window = bytearray(combined[-512:])
                    continue
                if not raw.endswith(b"\n"):
                    summary["partial_bytes"] = len(raw)
                    _mark_damaged_candidate(raw, entries, "evidence_candidate_truncated")
                    break
                summary["complete_lines"] += 1
                payload = raw.rstrip(b"\r\n")
                try:
                    value = json.loads(
                        payload.decode("utf-8-sig"),
                        parse_constant=reject_nonfinite_json_constant,
                    )
                except (UnicodeError, json.JSONDecodeError, ValueError):
                    summary["invalid_lines"] += 1
                    _mark_damaged_candidate(payload, entries, "evidence_candidate_corrupt")
                    continue
                if not isinstance(value, dict):
                    summary["invalid_lines"] += 1
                    continue
                canonical = SessionArchiveBuilder._canonical_line(value)
                snapshot_id = value.get("snapshot_id")
                if isinstance(snapshot_id, str):
                    for entry in snapshots.get(snapshot_id, []):
                        if value.get("session_id") != entry.get("session_id"):
                            entry["blockers"].add("source_candidate_session_mismatch")
                            continue
                        _record_match(
                            entry,
                            "source_matches",
                            value,
                            canonical,
                            match_budget,
                            from_archive=kind == "session_archive" and archive_session_id == entry.get("session_id"),
                        )
                if value.get("event") != "command_ack":
                    continue
                command_id = value.get("id")
                attempt_id = value.get("attempt_id")
                if not isinstance(command_id, str) or not isinstance(attempt_id, str):
                    continue
                for entry in acks.get((command_id, attempt_id), []):
                    if value.get("session_id") != entry.get("session_id"):
                        entry["blockers"].add("ack_candidate_session_mismatch")
                        continue
                    _record_match(
                        entry,
                        "ack_matches",
                        value,
                        canonical,
                        match_budget,
                        from_archive=kind == "session_archive" and archive_session_id == entry.get("session_id"),
                    )
    except OSError:
        summary["io_error"] = True
        for entry in entries:
            entry["blockers"].add("evidence_source_io_error")
    return summary


def _validate_archive_for_order(
    path: pathlib.Path,
    pending: Set[str],
) -> Tuple[Set[str], Set[str], Set[str]]:
    existing: Set[str] = set()
    duplicate_required: Set[str] = set()
    blockers: Set[str] = set()
    previous_seq: Optional[int] = None
    if not path.exists():
        return existing, duplicate_required, blockers
    try:
        with path.open("rb") as handle:
            while True:
                raw = handle.readline(MAX_EVIDENCE_RECORD_BYTES + 1)
                if not raw:
                    break
                if len(raw) > MAX_EVIDENCE_RECORD_BYTES and not raw.endswith(b"\n"):
                    blockers.add("session_archive_record_oversized")
                    break
                if not raw.endswith(b"\n"):
                    blockers.add("session_archive_truncated")
                    break
                try:
                    value = json.loads(
                        raw.decode("utf-8-sig"),
                        parse_constant=reject_nonfinite_json_constant,
                    )
                except (UnicodeError, json.JSONDecodeError, ValueError):
                    blockers.add("session_archive_corrupt")
                    continue
                if not isinstance(value, dict):
                    blockers.add("session_archive_corrupt")
                    continue
                canonical = SessionArchiveBuilder._canonical_line(value)
                if canonical in pending:
                    if canonical in existing:
                        duplicate_required.add(canonical)
                    else:
                        existing.add(canonical)
                sequence = parse_positive_sequence(value.get("seq"))
                if sequence is None:
                    blockers.add("session_archive_sequence_invalid")
                    continue
                if previous_seq is not None and sequence < previous_seq:
                    blockers.add("session_archive_order_invalid")
                previous_seq = sequence
    except OSError:
        blockers.add("session_archive_io_error")
    return existing, duplicate_required, blockers


def _merge_recovered_session_records(
    bridge_dir: pathlib.Path,
    session_id: str,
    records: Dict[str, Dict[str, Any]],
) -> Tuple[int, Set[str]]:
    target = bridge_dir / "sessions" / safe_session_name(session_id) / "state.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    existing, duplicate_required, blockers = _validate_archive_for_order(target, set(records))
    if blockers:
        return 0, blockers
    pending: List[Tuple[int, str]] = []
    for canonical, record in records.items():
        if canonical in existing:
            continue
        sequence = parse_positive_sequence(record.get("seq"))
        if sequence is None:
            blockers.add("recovered_record_sequence_invalid")
            continue
        pending.append((sequence, canonical))
    if blockers or (not pending and not duplicate_required):
        return 0, blockers
    pending.sort(key=lambda item: (item[0], item[1]))
    temporary: Optional[pathlib.Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            "w+b",
            delete=False,
            dir=str(target.parent),
            suffix=".tmp",
        ) as output:
            temporary = pathlib.Path(output.name)
            pending_index = 0
            written_required: Set[str] = set()
            if target.exists():
                with target.open("rb") as source:
                    while True:
                        raw = source.readline(MAX_EVIDENCE_RECORD_BYTES + 1)
                        if not raw:
                            break
                        value = json.loads(
                            raw.decode("utf-8-sig"),
                            parse_constant=reject_nonfinite_json_constant,
                        )
                        assert isinstance(value, dict)
                        sequence = int(value["seq"])
                        while pending_index < len(pending) and pending[pending_index][0] < sequence:
                            output.write((pending[pending_index][1] + "\n").encode("utf-8"))
                            pending_index += 1
                        canonical = SessionArchiveBuilder._canonical_line(value)
                        if canonical in records:
                            if canonical in written_required:
                                continue
                            written_required.add(canonical)
                        output.write(raw)
            while pending_index < len(pending):
                output.write((pending[pending_index][1] + "\n").encode("utf-8"))
                pending_index += 1
            output.flush()
            os.fsync(output.fileno())
        _replace_with_retry(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return len(pending), blockers


def _finalize_evidence_command(
    entry: Dict[str, Any],
    source_summaries: List[Dict[str, Any]],
) -> None:
    blockers: Set[str] = entry["blockers"]
    source_matches = entry["source_matches"]
    if isinstance(entry.get("source_snapshot_id"), str):
        if not source_matches:
            blockers.add("source_snapshot_missing")
        elif len(source_matches) != 1:
            blockers.add("source_snapshot_ambiguous")
    ack_matches = list(entry["ack_matches"].values())
    required_stages = _ACK_SEQUENCE_FIELDS if entry.get("directory") == "confirming" else {
        stage: field
        for stage, field in _ACK_SEQUENCE_FIELDS.items()
        if entry.get("ack_sequences", {}).get(stage) is not None
    }
    for stage in required_stages:
        declared_seq = entry.get("ack_sequences", {}).get(stage)
        if declared_seq is None:
            blockers.add(f"{stage}_ack_sequence_invalid")
            continue
        matches = [
            record
            for record in ack_matches
            if record.get("status") == stage and record.get("seq") == declared_seq
        ]
        if not matches:
            blockers.add(f"{stage}_ack_missing")
        elif len(matches) != 1:
            blockers.add(f"{stage}_ack_ambiguous")
    if any(record.get("status") in {"failed", "rejected", "skipped_stale"} for record in ack_matches):
        blockers.add("contradictory_terminal_ack")
    # A damaged line cannot prove that it was unrelated before corruption, so
    # uniqueness is inconclusive even when one plausible variant was recovered.
    if any(int(summary.get("invalid_lines") or 0) > 0 for summary in source_summaries):
        blockers.add("evidence_source_corrupt")
    if any(int(summary.get("partial_bytes") or 0) > 0 for summary in source_summaries):
        blockers.add("evidence_source_truncated")
    if any(int(summary.get("oversized_lines") or 0) > 0 for summary in source_summaries):
        blockers.add("evidence_source_oversized")


def recover_unresolved_command_evidence(bridge_dir: pathlib.Path) -> Dict[str, Any]:
    """Recover only evidence named by unresolved execution claims.

    Root logs and their bounded rotation backups are scanned as streams.  Exact
    canonical duplicates collapse, conflicting variants remain visible, and
    required records are merged into the session archive by producer sequence.
    Any uncertainty is published as a blocker before action confirmation runs.
    """

    bridge_dir = bridge_dir.expanduser().resolve()
    bridge_dir.mkdir(parents=True, exist_ok=True)
    entries, global_blockers = _discover_unresolved_evidence_commands(bridge_dir)
    source_summaries: List[Dict[str, Any]] = []
    match_budget = {"bytes": 0}
    if entries:
        session_ids = sorted(
            {
                str(entry["session_id"])
                for entry in entries
                if isinstance(entry.get("session_id"), str) and entry.get("session_id")
            }
        )
        for session_id in session_ids:
            archive = bridge_dir / "sessions" / safe_session_name(session_id) / "state.jsonl"
            if archive.exists():
                source_summaries.append(
                    _scan_evidence_source(
                        bridge_dir,
                        archive,
                        "session_archive",
                        session_id,
                        entries,
                        match_budget,
                    )
                )
        state_log = bridge_dir / "state.jsonl"
        root_sources = [pathlib.Path(str(state_log) + f".{index}") for index in range(LOG_BACKUPS, 0, -1)]
        root_sources.append(state_log)
        for source in root_sources:
            if source.exists():
                source_summaries.append(
                    _scan_evidence_source(bridge_dir, source, "root_log", None, entries, match_budget)
                )

    for entry in entries:
        _finalize_evidence_command(entry, source_summaries)

    recovered_by_session: Dict[str, Dict[str, Dict[str, Any]]] = collections.defaultdict(dict)
    entries_by_session: Dict[str, List[Dict[str, Any]]] = collections.defaultdict(list)
    for entry in entries:
        session_id = entry.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            continue
        entries_by_session[session_id].append(entry)
        if "session_archive_path_ambiguous" in entry["blockers"]:
            continue
        for bucket_name in ("source_matches", "ack_matches"):
            for canonical, record in entry[bucket_name].items():
                recovered_by_session[session_id][canonical] = record

    recovered_count = 0
    for session_id, records in sorted(recovered_by_session.items()):
        count, merge_blockers = _merge_recovered_session_records(bridge_dir, session_id, records)
        recovered_count += count
        for entry in entries_by_session[session_id]:
            entry["blockers"].update(merge_blockers)

    command_reports: List[Dict[str, Any]] = []
    all_blockers = set(global_blockers)
    for entry in entries:
        blockers = sorted(entry["blockers"])
        all_blockers.update(blockers)
        ack_counts: Dict[str, int] = collections.Counter(
            str(record.get("status") or "unknown") for record in entry["ack_matches"].values()
        )
        command_reports.append(
            {
                "directory": entry.get("directory"),
                "path": _relative_evidence_path(bridge_dir, entry["path"]),
                "command_id": entry.get("command_id"),
                "session_id": entry.get("session_id"),
                "source_snapshot_id": entry.get("source_snapshot_id"),
                "attempt_id": entry.get("attempt_id"),
                "status": "ready" if not blockers else "blocked",
                "blockers": blockers,
                "source_variant_count": len(entry["source_matches"]),
                "ack_variant_counts": dict(sorted(ack_counts.items())),
            }
        )
    status = "ready" if not all_blockers else "blocked"
    report = {
        "schema": EVIDENCE_CONTINUITY_SCHEMA,
        "generated_at": now_iso(),
        "status": status,
        "unresolved_command_count": len(entries),
        "recovered_record_count": recovered_count,
        "blockers": sorted(all_blockers),
        "commands": command_reports,
        "sources": source_summaries,
        "limits": {
            "max_unresolved_commands": MAX_EVIDENCE_COMMANDS,
            "max_variants_per_requirement": MAX_EVIDENCE_VARIANTS_PER_REQUIREMENT,
            "max_record_bytes": MAX_EVIDENCE_RECORD_BYTES,
            "max_match_canonical_bytes": MAX_EVIDENCE_MATCH_BYTES,
            "matched_canonical_bytes": match_budget["bytes"],
        },
    }
    atomic_write_json(bridge_dir / "evidence-continuity.json", report)
    return report


def apply_evidence_continuity_health(health: Dict[str, Any], report: Dict[str, Any]) -> None:
    health["evidence_continuity"] = {
        "status": report.get("status"),
        "unresolved_command_count": report.get("unresolved_command_count"),
        "recovered_record_count": report.get("recovered_record_count"),
        "blockers": list(report.get("blockers") or []),
    }
    if report.get("status") != "blocked":
        return
    trust_blockers = set(health.get("trust_blockers") or [])
    trust_blockers.add("evidence_continuity_blocked")
    health["trust_blockers"] = sorted(trust_blockers)
    health["trust_ready"] = False
    if health.get("status") == "ok":
        health["status"] = "untrusted_snapshot"


def publish_command_pipeline_health(
    health: Dict[str, Any],
    gate: Dict[str, Any],
) -> None:
    """Project the durable queue gate without claiming frame-bound evaluation."""

    readiness = gate.get("readiness") if isinstance(gate.get("readiness"), dict) else {}
    counts = gate.get("counts") if isinstance(gate.get("counts"), dict) else {}
    health["command_pipeline"] = {
        "schema": gate.get("schema"),
        "ready": bool(gate.get("ready")),
        "blockers": list(gate.get("blockers") or []),
        "readiness": copy.deepcopy(readiness),
        "counts": dict(sorted(counts.items())),
        "in_flight_mutation_count": len(gate.get("in_flight_mutations") or []),
        "ambiguous_outcome_count": len(gate.get("ambiguous_outcomes") or []),
        "duplicate_id_count": len(gate.get("duplicate_ids") or []),
        "stale_pending_count": len(gate.get("stale_pending") or []),
        "invalid_record_count": len(gate.get("invalid_records") or []),
        "proposal_frame_evaluated": False,
        "proposal_frame_binding": "deferred_until_sync_pending_commands",
    }


def replay_initial_records(
    path: pathlib.Path,
    bridge_dir: pathlib.Path,
    *,
    allow_untrusted_snapshot_baseline: bool = False,
    startup_expectations: Optional[Dict[str, Any]] = None,
    require_startup_binding: bool = False,
    strategy_goal: str = STRATEGY_GOAL,
) -> Tuple[ReplayState, int]:
    raw_lines, position, partial = load_recent_raw_lines(path)
    runtime = ReplayState(
        allow_untrusted_snapshot_baseline=allow_untrusted_snapshot_baseline,
        startup_expectations=startup_expectations,
        require_startup_binding=require_startup_binding,
        strategy_goal=strategy_goal,
    )
    runtime.partial_bytes = partial
    archive = SessionArchiveBuilder(bridge_dir)
    try:
        for raw in raw_lines:
            record, _ = parse_json_line(raw.decode("utf-8-sig", errors="replace"))
            if record is None:
                continue
            runtime.process(record)
            archive.add(record)
        archive.commit()
    except Exception:
        archive.abort()
        raise
    return runtime, position


def write_output_set(
    target: pathlib.Path,
    latest: Optional[Dict[str, Any]],
    summary: str,
    advice_payload: Dict[str, Any],
    incident_payload: Dict[str, Any],
    plan: Dict[str, Any],
    health: Dict[str, Any],
    strategy: Optional[Dict[str, Any]] = None,
    action_outcomes: Optional[Dict[str, Any]] = None,
    decision_frame: Optional[Dict[str, Any]] = None,
) -> None:
    atomic_write_json(target / "latest_state.json", latest or {})
    atomic_write_text(target / "state_summary.md", summary)
    atomic_write_json(target / "advice.json", advice_payload)
    atomic_write_text(
        target / "advice.md",
        agent_rules.build_advice_markdown(advice_payload.get("items") or [], advice_payload["updated_at"]),
    )
    atomic_write_json(target / "incidents.json", incident_payload)
    atomic_write_json(target / "plan.json", plan)
    atomic_write_json(target / "health.json", health)
    if strategy is not None:
        atomic_write_json(target / "strategy.json", strategy)
        atomic_write_text(target / "strategy.md", build_strategy_markdown(strategy))
    if action_outcomes is not None:
        atomic_write_json(target / "action-outcomes.json", action_outcomes)
    # This file is the commit marker for the whole generation.  Consumers that
    # make decisions must read it instead of trying to join the compatibility
    # files above, which may span generations if publication is interrupted.
    if decision_frame is not None:
        atomic_write_json(target / "decision-frame.json", decision_frame)


def _planned_owner_queue_entries(
    bridge_dir: pathlib.Path,
) -> List[Tuple[str, pathlib.Path, Dict[str, Any]]]:
    """Read every protected queue entry used by the explicit plan owner.

    A plan owner cannot safely reason about ``pending`` in isolation: an
    approval claim, executor claim, confirmation, or validation-control file
    is still an active custody boundary.  Read all entries and fail closed on
    anything that is not a readable JSON object with a durable id.
    """
    root = pathlib.Path(bridge_dir).expanduser().resolve()
    entries: List[Tuple[str, pathlib.Path, Dict[str, Any]]] = []
    seen_ids: Set[str] = set()
    for queue_name in _PLANNED_OWNER_QUEUE_NAMES:
        queue = root / queue_name
        if queue.is_symlink():
            raise RuntimeError(f"planned queue owner found an invalid protected queue: {queue_name}")
        if not queue.exists():
            continue
        if not queue.is_dir():
            raise RuntimeError(f"planned queue owner found an invalid protected queue: {queue_name}")
        try:
            paths = sorted(queue.iterdir(), key=lambda item: item.name)
        except OSError as exc:
            raise RuntimeError(f"planned queue owner could not read protected queue: {queue_name}") from exc
        for path in paths:
            if not path.is_file() or path.suffix.lower() != ".json" or path.is_symlink():
                raise RuntimeError(
                    f"planned queue owner found a non-json protected queue entry: {queue_name}/{path.name}"
                )
            try:
                payload = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    f"planned queue owner found an unreadable protected queue entry: {queue_name}/{path.name}"
                ) from exc
            if not isinstance(payload, dict) or not isinstance(payload.get("id"), str) or not payload["id"]:
                raise RuntimeError(
                    f"planned queue owner found an invalid protected queue entry: {queue_name}/{path.name}"
                )
            command_id = payload["id"]
            if command_id in seen_ids:
                raise RuntimeError(f"planned queue owner found a duplicate protected queue id: {command_id}")
            seen_ids.add(command_id)
            entries.append((queue_name, path, payload))
    return entries


def _planned_owner_pending_inventory(bridge_dir: pathlib.Path) -> Dict[str, str]:
    """Read all protected queues as ``entry id -> mode``.

    The historical helper name is retained for callers, but its inventory is
    intentionally no longer limited to ``commands/pending``.
    """
    return {
        payload["id"]: str(payload.get("mode") or "")
        for _queue_name, _path, payload in _planned_owner_queue_entries(bridge_dir)
    }


def build_planned_queue_owner(
    *,
    title: str = "LLM ranked colony response",
    goal: Optional[str] = None,
) -> Callable[[pathlib.Path, Mapping[str, Any], List[Mapping[str, Any]], Mapping[str, Any]], Mapping[str, Any]]:
    """Build an opt-in, idempotent owner for model-ranked planned leaves.

    The owner is deliberately separate from the default suggested-command
    path.  It creates a plan only when no non-terminal plan exists, advances at
    most one guarded leaf per generation, and refuses any suggested-command
    coexistence or live-admission claim.
    """

    def owner(
        bridge_dir: pathlib.Path,
        generated_plan: Mapping[str, Any],
        generated_advice: List[Mapping[str, Any]],
        frame: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        root = pathlib.Path(bridge_dir).expanduser().resolve()
        frame_id = frame.get("frame_id")
        if not isinstance(frame_id, str) or not frame_id:
            raise RuntimeError("planned queue owner requires a committed decision frame")
        before = _planned_owner_pending_inventory(root)
        if any(mode == "suggested" for mode in before.values()):
            raise RuntimeError("planned queue owner refuses coexistence with suggested commands")

        active = None
        active_path = root / "colony-plans" / "active.json"
        if active_path.exists():
            active = load_active_plan(root)
        if isinstance(active, Mapping) and active.get("status") not in {"completed", "failed", "aborted"}:
            open_id = active.get("open_command_id")
            if isinstance(open_id, str) and open_id:
                if set(before) != {open_id}:
                    raise RuntimeError(
                        "planned queue owner refuses coexistence with another protected queue entry"
                    )
                mode = before.get(open_id)
                if mode != "planned":
                    raise RuntimeError("planned queue owner found an unresolved non-planned open command")
                return {
                    "schema": "oni.planned-queue-owner-result.v1",
                    "status": "noop",
                    "live_admission": False,
                    "plan_id": active.get("plan_id"),
                    "command_id": open_id,
                    "published_count": 0,
                    "source_frame_id": frame_id,
                }
            if before:
                raise RuntimeError(
                    "planned queue owner requires all protected queues to be empty before advancing"
                )
            advanced = advance_colony_plan(root, str(active.get("plan_id")))
            plan_id = advanced.get("plan_id")
            step_id = advanced.get("current_step")
            status = "advanced" if advanced.get("open_command_id") else "noop"
        else:
            if before:
                raise RuntimeError(
                    "planned queue owner requires all protected queues to be empty before creating a plan"
                )
            definition = colony_plan_definition_from_plan(
                copy.deepcopy(dict(generated_plan)),
                copy.deepcopy(list(generated_advice)),
                title=title,
                goal=goal,
            )
            draft = create_colony_plan(root, definition)
            step = draft["steps"][0]
            source = generated_plan.get("source") if isinstance(generated_plan.get("source"), Mapping) else {}
            state_hash = source.get("state_hash")
            if not isinstance(state_hash, str):
                raise RuntimeError("planned queue owner requires a planner source state hash")
            advanced = advance_colony_plan(
                root,
                str(draft["plan_id"]),
                expected_step_id=str(step["id"]),
                expected_state_hash=state_hash,
                expected_step_hash=plan_leaf_contract_hash(step),
            )
            plan_id = advanced.get("plan_id")
            step_id = advanced.get("current_step")
            status = "published" if advanced.get("open_command_id") else "blocked"

        after = _planned_owner_pending_inventory(root)
        new_ids = set(after) - set(before)
        if any(after[command_id] == "suggested" for command_id in new_ids):
            raise RuntimeError("planned queue owner created a suggested command")
        planned_new = [command_id for command_id in new_ids if after[command_id] == "planned"]
        if len(planned_new) > 1:
            raise RuntimeError("planned queue owner published more than one planned command")
        for command_id in planned_new:
            payload = json.loads((root / "commands" / "pending" / f"{command_id}.json").read_text(encoding="utf-8-sig"))
            if payload.get("source_frame_id") != frame_id:
                raise RuntimeError("planned command is bound to a different decision frame")
        return {
            "schema": "oni.planned-queue-owner-result.v1",
            "status": status,
            "live_admission": False,
            "plan_id": plan_id,
            "step_id": step_id,
            "command_id": advanced.get("open_command_id"),
            "published_count": len(planned_new),
            "source_frame_id": frame_id,
        }

    return owner


def build_clone_gated_planned_queue_owner(
    *,
    bridge_dir: pathlib.Path,
    clone_manifest: pathlib.Path,
    clone_save: pathlib.Path,
    clone_preview: pathlib.Path,
    title: str = "LLM ranked colony response",
    goal: Optional[str] = None,
) -> Callable[[pathlib.Path, Mapping[str, Any], List[Mapping[str, Any]], Mapping[str, Any]], Mapping[str, Any]]:
    """Build a planned owner that rechecks the named clone at every tick.

    This is an explicit opt-in factory.  It never launches a process and does
    not make the default Bridge watch path live capable.  Clone identity and
    protected-process counts are checked before and after the inner owner; any
    drift fails closed while leaving any already durable pending command for
    human review.
    """
    root = pathlib.Path(bridge_dir).expanduser().resolve()
    manifest = pathlib.Path(clone_manifest).expanduser().resolve()
    save = pathlib.Path(clone_save).expanduser().resolve()
    preview = pathlib.Path(clone_preview).expanduser().resolve()
    protected = ("OxygenNotIncluded", "oni_bridge", "steam")
    queue_names = _PLANNED_OWNER_QUEUE_NAMES
    process_probe = build_process_probe(protected)
    queue_probe = build_queue_probe(root, queue_names)
    owned_queue_ids: Set[str] = set()

    def admission_queue_probe() -> Mapping[str, int]:
        """Return live queue counts, subtracting only this owner's command.

        ``validate_clone_launch`` requires an empty boundary.  Once this
        owner has durably published its one planned command, mask that exact
        id from the validation view while retaining live counts for every
        other protected command and validation-control queue.
        """
        observed = dict(queue_probe())
        if not owned_queue_ids:
            return observed
        entries = _planned_owner_queue_entries(root)
        locations = {payload["id"] for _name, _path, payload in entries}
        missing = owned_queue_ids - locations
        if missing:
            raise RuntimeError("clone-gated planned owner lost its protected command")
        adjusted = dict(observed)
        for queue_name, _path, payload in entries:
            if payload["id"] in owned_queue_ids:
                adjusted[queue_name] = adjusted.get(queue_name, 0) - 1
        if any(type(value) is not int or value < 0 for value in adjusted.values()):
            raise RuntimeError("clone-gated planned owner queue counts are inconsistent")
        return adjusted

    clone_probe = build_clone_probe(
        manifest, save, preview, bridge_dir=root, process_names=protected,
        process_probe=process_probe, queue_probe=admission_queue_probe,
    )
    baseline = clone_probe()
    identity_keys = (
        "manifest_sha256", "clone_save_sha256", "clone_preview_sha256",
        "clone_save_path_id", "clone_preview_path_id", "bridge_dir",
    )

    def admit(reference: Mapping[str, Any]) -> Mapping[str, Any]:
        entries = _planned_owner_queue_entries(root)
        if any(str(payload.get("mode") or "") == "suggested" for _name, _path, payload in entries):
            raise RuntimeError("clone-gated planned owner refuses coexistence with suggested commands")
        fresh = validate_clone_launch(
            manifest, save, preview, bridge_dir=root, process_names=protected,
            process_probe=process_probe,
            queue_probe=admission_queue_probe,
        )
        if not isinstance(fresh, Mapping) or fresh.get("launch_allowed") is not True:
            raise RuntimeError("clone-gated planned owner preflight is blocked")
        if any(fresh.get(key) != reference.get(key) for key in identity_keys):
            raise RuntimeError("clone-gated planned owner identity changed")
        queue_ids = {payload["id"] for _name, _path, payload in entries}
        if queue_ids != owned_queue_ids:
            raise RuntimeError("clone-gated planned owner protected queue ownership changed")
        if any(str(payload.get("mode") or "") != "planned" for _name, _path, payload in entries):
            raise RuntimeError("clone-gated planned owner found a non-planned protected command")
        counts = fresh.get("process_counts") if isinstance(fresh.get("process_counts"), Mapping) else {}
        if set(counts) != set(protected) or any(type(value) is not int or value != 0 for value in counts.values()):
            raise RuntimeError("clone-gated planned owner found a protected process")
        return fresh

    admit(baseline)
    inner = build_planned_queue_owner(title=title, goal=goal)

    def gated_owner(
        callback_bridge_dir: pathlib.Path,
        generated_plan: Mapping[str, Any],
        generated_advice: List[Mapping[str, Any]],
        frame: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        callback_root = pathlib.Path(callback_bridge_dir).expanduser().resolve()
        if callback_root != root:
            raise RuntimeError("clone-gated planned owner bridge directory changed")
        if owned_queue_ids:
            current_ids = {
                payload["id"]
                for _name, _path, payload in _planned_owner_queue_entries(root)
            }
            if not owned_queue_ids.issubset(current_ids):
                active = _planned_owner_active_plan_snapshot(root)
                if active is None or active.get("status") in {"completed", "failed", "aborted"}:
                    # A prior post-sync failure may have quarantined the
                    # owner's command and terminally aborted its plan.  The
                    # next tick starts a new ownership generation.
                    owned_queue_ids.clear()
                else:
                    raise RuntimeError("clone-gated planned owner lost its protected command")
        admit(baseline)
        result = inner(callback_root, generated_plan, generated_advice, frame)
        if not isinstance(result, Mapping):
            raise RuntimeError("planned queue owner result is invalid")
        command_id = result.get("command_id")
        owned_queue_ids.clear()
        if isinstance(command_id, str) and command_id:
            owned_queue_ids.add(command_id)
        admit(baseline)
        return result

    return gated_owner


def build_planned_queue_owner_from_config(
    bridge_dir: pathlib.Path,
    config_path: pathlib.Path,
) -> Callable[[pathlib.Path, Mapping[str, Any], List[Mapping[str, Any]], Mapping[str, Any]], Mapping[str, Any]]:
    """Load an explicit clone-gated planned-owner configuration.

    The configuration contains only paths and presentation metadata.  Clone
    hashes and process state are always obtained from a fresh probe rather than
    trusted from the file.
    """
    root = pathlib.Path(bridge_dir).expanduser().resolve()
    config_file = pathlib.Path(config_path).expanduser().resolve()
    try:
        config = json.loads(config_file.read_text(encoding="utf-8-sig"), parse_constant=reject_nonfinite_json_constant)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("planned owner config is unreadable") from exc
    if not isinstance(config, dict) or config.get("schema") != PLANNED_OWNER_CONFIG_SCHEMA:
        raise ValueError("planned owner config schema is invalid")
    required = {"schema", "bridge_dir", "clone_manifest", "clone_save", "clone_preview"}
    optional = {"title", "goal"}
    if set(config) - required - optional or not required.issubset(config):
        raise ValueError("planned owner config field set is invalid")
    configured_root = pathlib.Path(str(config.get("bridge_dir") or "")).expanduser().resolve()
    if configured_root != root:
        raise ValueError("planned owner config bridge_dir mismatch")

    def path_field(name: str) -> pathlib.Path:
        value = config.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"planned owner config {name} is invalid")
        return pathlib.Path(value).expanduser().resolve()

    title = config.get("title", "LLM ranked colony response")
    goal = config.get("goal")
    if not isinstance(title, str) or len(title) > 160:
        raise ValueError("planned owner config title is invalid")
    if goal is not None and (not isinstance(goal, str) or len(goal) > 1000):
        raise ValueError("planned owner config goal is invalid")
    return build_clone_gated_planned_queue_owner(
        bridge_dir=root,
        clone_manifest=path_field("clone_manifest"),
        clone_save=path_field("clone_save"),
        clone_preview=path_field("clone_preview"),
        title=title,
        goal=goal,
    )


def _planned_owner_active_plan_snapshot(bridge_dir: pathlib.Path) -> Optional[Dict[str, Any]]:
    """Return the active plan when it is readable, otherwise ``None``."""
    pointer = pathlib.Path(bridge_dir).expanduser().resolve() / "colony-plans" / "active.json"
    if not pointer.exists():
        return None
    try:
        value = load_active_plan(pointer.parent.parent)
    except Exception:
        return None
    return copy.deepcopy(value) if isinstance(value, Mapping) else None


def _cleanup_planned_owner_failure(
    bridge_dir: pathlib.Path,
    before_queue: Mapping[str, str],
    before_plan: Optional[Mapping[str, Any]],
) -> None:
    """Terminate owner-side effects after a post-publication gate failure.

    ``advance_colony_plan`` publishes a pending command before committing the
    plan revision.  If the later Bridge control-packet/custody gate fails,
    leaving that pair in place would make the next tick replay an orphaned
    leaf.  Abort the changed plan through its custody-aware API; this
    quarantines a pending command and commits a terminal plan revision.  Any
    residual protected queue entry is treated as an unresolved custody error.
    """
    root = pathlib.Path(bridge_dir).expanduser().resolve()
    after_queue = _planned_owner_pending_inventory(root)
    active = _planned_owner_active_plan_snapshot(root)
    before_plan_id = before_plan.get("plan_id") if isinstance(before_plan, Mapping) else None
    before_revision = before_plan.get("revision") if isinstance(before_plan, Mapping) else None
    changed_queue = set(after_queue) - set(before_queue)
    changed_plan = bool(
        isinstance(active, Mapping)
        and (
            active.get("plan_id") != before_plan_id
            or active.get("revision") != before_revision
            or (
                isinstance(before_plan, Mapping)
                and any(
                    active.get(key) != before_plan.get(key)
                    for key in ("status", "current_step", "open_command_id", "revision_hash")
                )
            )
        )
    )
    if not changed_queue and not changed_plan:
        return
    if not isinstance(active, Mapping) or not isinstance(active.get("plan_id"), str):
        raise RuntimeError("planned owner failure left an unreadable plan/queue boundary")
    try:
        abort_colony_plan(root, str(active["plan_id"]))
    except Exception as exc:
        raise RuntimeError("planned owner failure could not terminate the changed plan") from exc
    remaining = _planned_owner_pending_inventory(root)
    residual = set(remaining) - set(before_queue)
    if residual:
        raise RuntimeError(
            "planned owner failure left protected queue entries after termination: "
            + ", ".join(sorted(residual))
        )


def write_outputs(
    bridge_dir: pathlib.Path,
    runtime: ReplayState,
    *,
    enable_llm: bool = False,
    secrets_dir: Optional[pathlib.Path] = None,
    goal: str = STRATEGY_GOAL,
    replay_mode: bool = False,
    allow_replay_suggestions: bool = False,
    coordinator_observer: Optional[Callable[[], Mapping[str, Any]]] = None,
    planned_queue_owner: Optional[
        Callable[[pathlib.Path, Mapping[str, Any], List[Mapping[str, Any]], Mapping[str, Any]], Mapping[str, Any]]
    ] = None,
    model_loop: Optional[ModelLoop] = None,
) -> None:
    bridge_dir.mkdir(parents=True, exist_ok=True)
    # Finish any root+session publication that crashed after its durable commit
    # journal but before the confirming command's atomic rename to done.  This
    # must precede rebuilding action outcomes, because a prepared confirming
    # command intentionally already contains its immutable confirmation.
    recover_staged_confirmed_outcomes(bridge_dir)
    latest = runtime.effective_latest(ignore_stale=replay_mode)
    evidence_continuity = recover_unresolved_command_evidence(bridge_dir)
    policy = load_policy(bridge_dir)
    health = runtime.health(latest, policy, replay_mode=replay_mode)
    pipeline_gate = command_pipeline_readiness(
        bridge_dir,
        latest_state=latest,
        proposal_frame=None,
        policy=policy,
    )
    publish_command_pipeline_health(health, pipeline_gate)
    apply_evidence_continuity_health(health, evidence_continuity)
    if isinstance(latest, dict):
        bridge_meta = latest.get("bridge") if isinstance(latest.get("bridge"), dict) else {}
        bridge_meta.update(
            {
                "trust_ready": bool(health.get("trust_ready")),
                "trust_blockers": list(health.get("trust_blockers") or []),
                "replay_mode": replay_mode,
            }
        )
        latest["bridge"] = bridge_meta
    incident_payload = runtime.incidents.payload()
    source = output_source_identity(latest)
    health["source"] = copy.deepcopy(source)
    incident_payload["source"] = copy.deepcopy(source)
    stale = bool(safe_get(latest or {}, "bridge.stale", False))
    trusted = health.get("status") == "ok" and bool(health.get("trust_ready"))
    open_incidents = [] if stale or not trusted else runtime.incidents.open_incidents()
    advice = agent_rules.build_arbitrated_advice(latest, open_incidents)
    advice_payload = {
        "schema": "oni.advice.v2",
        "updated_at": now_iso(),
        "session_id": runtime.current_session_id,
        "source": copy.deepcopy(source),
        "items": advice,
    }
    if replay_mode and not allow_replay_suggestions:
        health["replay_suggestions_suppressed"] = True
    plan = generate_plan(
        bridge_dir,
        latest,
        runtime.events,
        advice,
        enable_llm=enable_llm,
        secrets_dir=secrets_dir,
        goal=goal,
    )
    plan["source"] = copy.deepcopy(source)
    # Keep the long-horizon strategy separate from executable advice.  The
    # strategy layer is deterministic and non-executing; all game changes still
    # go through the existing command queue, policy, and human approval gates.
    strategy = runtime.strategy_tracker.update(
        evaluate_strategy(
            latest,
            goal=runtime.strategy_goal,
            previous_phase=runtime.strategy_tracker.current_phase,
        )
    )
    strategy["source"] = copy.deepcopy(source)
    health["strategy_status"] = strategy.get("status")
    phase = strategy.get("phase") if isinstance(strategy.get("phase"), dict) else {}
    health["strategy_phase"] = phase.get("id")
    health["strategy_confidence"] = phase.get("confidence")
    policy_for_frame = copy.deepcopy(policy)
    policy_for_frame["source"] = copy.deepcopy(source)
    action_outcomes = build_action_outcomes(
        bridge_dir,
        latest,
        health,
        replay_mode=replay_mode,
    )
    action_counts = action_outcomes.get("counts") if isinstance(action_outcomes.get("counts"), dict) else {}
    health["action_outcomes_hash"] = action_outcomes.get("outcomes_hash")
    health["action_outcomes"] = {
        "executed_verified": int(action_counts.get("executed_verified") or 0),
        "awaiting_trusted_snapshot": int(action_counts.get("awaiting_trusted_snapshot") or 0),
        "snapshot_confirmed": int(action_counts.get("snapshot_confirmed") or 0),
        "rejected": int(action_counts.get("rejected") or 0),
    }
    decision_frame = build_decision_frame(
        latest,
        health,
        advice_payload,
        incident_payload,
        plan,
        strategy,
        policy_for_frame,
    )
    health["decision_frame_id"] = decision_frame["frame_id"]
    health["decision_frame_trust"] = decision_frame["trust"]["status"]
    health["decision_frame_readiness"] = {
        stage: bool(entry.get("ready"))
        for stage, entry in decision_frame["readiness"].items()
    }
    summary = build_summary(latest, runtime.events, incident_payload, health)

    write_output_set(
        bridge_dir,
        latest,
        summary,
        advice_payload,
        incident_payload,
        plan,
        health,
        strategy,
        action_outcomes,
        decision_frame,
    )
    session_dir = bridge_dir / "sessions" / safe_session_name(runtime.current_session_id)
    write_output_set(
        session_dir,
        latest,
        summary,
        advice_payload,
        incident_payload,
        plan,
        health,
        strategy,
        action_outcomes,
        decision_frame,
    )
    # The moving root commit marker is never used as historical approval
    # authority.  Archive the exact validated frame only after both root and
    # session-local generations are fully committed, and before any command can
    # be queued against that frame id.
    persist_decision_frame_archive(bridge_dir, decision_frame)
    # The journal is written only after both complete output sets (including
    # their decision-frame commit markers) returned successfully.  A generation
    # without a confirmed command has no command-side transaction to stage; in
    # particular, session-isolation output may intentionally have no actionable
    # certificate for the current archive directory.
    if int(action_counts.get("snapshot_confirmed") or 0) > 0:
        stage_confirmed_outcomes_commit(bridge_dir, action_outcomes)
        recover_staged_confirmed_outcomes(bridge_dir)
    # The decision frame is the generation commit marker.  Only after both
    # root and session-local frames are durably published may a queue owner
    # publish a command bound to that exact frame by ``source_frame_id``.
    # An explicit planned owner is the sole queue writer for this generation;
    # the default path below remains the existing suggested-command owner.
    planned_owner_before_queue: Optional[Dict[str, str]] = None
    planned_owner_before_plan: Optional[Dict[str, Any]] = None
    planned_owner_transaction = False
    if planned_queue_owner is not None:
        if replay_mode:
            raise RuntimeError("planned queue owner is unavailable in replay mode")
        planned_owner_before_queue = _planned_owner_pending_inventory(bridge_dir)
        planned_owner_before_plan = _planned_owner_active_plan_snapshot(bridge_dir)
        planned_owner_transaction = True
        # Keep the committed generation objects immutable from the callback:
        # the same frame and plan are still used below for the post-sync gate
        # and control packet.  A queue owner may inspect or copy these values,
        # but cannot mutate the authorization context in place.
        try:
            owner_result = planned_queue_owner(
                bridge_dir,
                copy.deepcopy(plan),
                copy.deepcopy(advice),
                copy.deepcopy(decision_frame),
            )
            if not isinstance(owner_result, Mapping):
                raise RuntimeError("planned queue owner must return a mapping")
            if owner_result.get("live_admission") is True:
                raise RuntimeError("planned queue owner returned live admission")
        except Exception:
            _cleanup_planned_owner_failure(
                bridge_dir,
                planned_owner_before_queue,
                planned_owner_before_plan,
            )
            raise
    elif not replay_mode or allow_replay_suggestions:
        sync_pending_commands(
            bridge_dir,
            latest,
            advice,
            proposal_frame=decision_frame,
            allow_non_actionable_replay=replay_mode and allow_replay_suggestions,
            policy_override=policy,
        )
        if model_loop is not None and not replay_mode:
            # The explicit model loop publishes at most one model_selected
            # command bound to this committed frame, after rule suggestions
            # had their turn and before the post-sync control packet.
            model_loop.step(bridge_dir, latest, decision_frame, policy, advice)
    # ControlPacket is a post-sync, non-executing commit marker.  Reuse the
    # exact state, frame, and already-loaded policy that entered this
    # generation: re-reading action-policy.json here could bind the queue and
    # packet to different policy generations.  If this structured scan raises,
    # neither packet is replaced; an older packet then fails closed against the
    # newly committed frame id.
    try:
        post_sync_gate = command_pipeline_readiness(
            bridge_dir,
            latest_state=latest,
            proposal_frame=decision_frame,
            policy=policy,
        )
        control_packet = build_control_packet(decision_frame, post_sync_gate)
        validate_control_packet(control_packet, decision_frame, post_sync_gate)
        # Session-local publication precedes the moving root marker.  The root file
        # is therefore the global commit marker for this post-sync control view.
        atomic_write_json(session_dir / "control-packet.json", control_packet)
        atomic_write_json(bridge_dir / "control-packet.json", control_packet)
        # Keep a bounded rolling history for indefinite watch mode, but never prune
        # the current frame or a frame referenced by a nonterminal command.
        prune_decision_frame_archives(
            bridge_dir,
            current_frame_id=str(decision_frame["frame_id"]),
        )
        if coordinator_observer is not None:
            write_coordinator_observation(bridge_dir, coordinator_observer)
    except Exception:
        if planned_owner_transaction and planned_owner_before_queue is not None:
            _cleanup_planned_owner_failure(
                bridge_dir,
                planned_owner_before_queue,
                planned_owner_before_plan,
            )
        raise


def write_coordinator_observation(
    bridge_dir: pathlib.Path,
    observer: Callable[[], Mapping[str, Any]],
    *,
    file_name: str = "coordinator-observation.json",
) -> Dict[str, Any]:
    """Persist one explicitly injected coordinator result as read-only evidence.

    The observer must already be configured for read-only operation.  This
    boundary rejects any result advertising approval, execution, successor
    advancement, or command publication before writing the evidence file.
    It never creates or mutates a command queue.
    """
    if not callable(observer):
        raise TypeError("coordinator observer must be callable")
    result = observer()
    if not isinstance(result, Mapping):
        raise ValueError("coordinator observer must return a mapping")
    if result.get("live_admission") is True:
        raise RuntimeError("coordinator observer returned live admission")
    side_effects = result.get("side_effects")
    if isinstance(side_effects, Mapping) and any(
        side_effects.get(key) is True
        for key in ("command_published", "approval", "execution", "successor_advanced")
    ):
        raise RuntimeError("coordinator observer returned side effects")
    payload: Dict[str, Any] = {
        "schema": "oni.coordinator-observation.v1",
        "live_admission": False,
        "result": copy.deepcopy(dict(result)),
    }
    target = pathlib.Path(bridge_dir) / file_name
    atomic_write_json(target, payload)
    return payload


def build_coordinator_observer(
    *,
    bridge_dir: pathlib.Path,
    plan_id: str,
    step_ids: Iterable[str],
    expected_identity: Mapping[str, Any],
    intent_directory: pathlib.Path,
    clone_manifest: pathlib.Path,
    clone_save: pathlib.Path,
    clone_preview: pathlib.Path,
    publish_one: bool = False,
    state_path: Optional[pathlib.Path] = None,
) -> Callable[[], Mapping[str, Any]]:
    """Build an explicit, clone-gated coordinator observer for opt-in callers.

    Construction itself performs one fresh clone/runtime preflight.  Each
    callback then re-probes the same files, protected process names, and queue
    directories before producing a bounded coordinator result.  The helper is
    intentionally not wired into the default Bridge CLI/watch path.
    """
    paths = (clone_manifest, clone_save, clone_preview)
    if any(path is None for path in paths):
        raise ValueError("complete real clone paths are required")
    protected = ("OxygenNotIncluded", "oni_bridge", "steam")
    queues = (
        "commands/pending", "commands/approving", "commands/approved",
        "commands/executing", "commands/confirming",
        "validation-controls/approved", "validation-controls/inflight",
    )
    process_probe = build_process_probe(protected)
    queue_probe = build_queue_probe(pathlib.Path(bridge_dir), queues)
    clone_probe = build_clone_probe(
        pathlib.Path(clone_manifest), pathlib.Path(clone_save), pathlib.Path(clone_preview),
        bridge_dir=pathlib.Path(bridge_dir), process_names=protected,
        process_probe=process_probe, queue_probe=queue_probe,
    )
    preflight = clone_probe()
    coordinator = PlanSequenceCoordinator(
        pathlib.Path(bridge_dir), plan_id=plan_id, step_ids=step_ids,
        expected_identity=expected_identity, intent_directory=pathlib.Path(intent_directory),
        clone_preflight=preflight, clone_probe=clone_probe,
        process_probe=process_probe, publish_one=publish_one is True, state_path=state_path,
    )
    return coordinator.tick


def build_read_only_coordinator_observer(
    bridge_dir: pathlib.Path,
    config_path: pathlib.Path,
) -> Callable[[], Mapping[str, Any]]:
    """Build an explicitly opted-in, read-only coordinator observer.

    The normal Bridge watch path does not discover a plan or a clone.  This
    helper is the narrow wiring seam for a caller that supplies a complete
    coordinator configuration and all three named-clone files.  It performs a
    fresh, read-only clone/process/queue preflight before constructing
    :class:`PlanSequenceCoordinator`; the coordinator is permanently created
    with ``publish_one=False``.  If the preflight is currently blocked, the
    returned callback emits a bounded ``blocked`` result on each tick instead
    of allowing a partial configuration to reach the command adapter.
    """

    root = pathlib.Path(bridge_dir).expanduser().resolve()
    config_file = pathlib.Path(config_path).expanduser().resolve()
    try:
        raw = config_file.read_text(encoding="utf-8-sig")
        config = json.loads(raw, parse_constant=reject_nonfinite_json_constant)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("coordinator config is unreadable") from exc
    if not isinstance(config, dict) or config.get("schema") != COORDINATOR_CONFIG_SCHEMA:
        raise ValueError("coordinator config schema is invalid")
    required = {
        "schema",
        "bridge_dir",
        "plan_id",
        "step_ids",
        "expected_identity",
        "intent_directory",
        "clone_manifest",
        "clone_save",
        "clone_preview",
    }
    optional = {"state_path"}
    if set(config) - required - optional or not required.issubset(config):
        raise ValueError("coordinator config field set is invalid")
    configured_root = pathlib.Path(str(config.get("bridge_dir") or "")).expanduser().resolve()
    if configured_root != root:
        raise ValueError("coordinator config bridge_dir mismatch")
    if not isinstance(config.get("plan_id"), str) or not config["plan_id"]:
        raise ValueError("coordinator config plan_id is invalid")
    step_ids = config.get("step_ids")
    if (not isinstance(step_ids, list) or not step_ids
            or any(not isinstance(item, str) or not item for item in step_ids)):
        raise ValueError("coordinator config step_ids are invalid")
    expected_identity = config.get("expected_identity")
    if not isinstance(expected_identity, dict):
        raise ValueError("coordinator config expected_identity is invalid")

    def _path_field(name: str) -> pathlib.Path:
        value = config.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"coordinator config {name} is invalid")
        return pathlib.Path(value).expanduser().resolve()

    intent_directory = _path_field("intent_directory")
    if not intent_directory.is_dir() or intent_directory.is_symlink():
        raise ValueError("coordinator config intent_directory is not a directory")
    manifest_path = _path_field("clone_manifest")
    clone_save = _path_field("clone_save")
    clone_preview = _path_field("clone_preview")
    state_path = _path_field("state_path") if "state_path" in config else None

    protected_process_names = ("OxygenNotIncluded", "oni_bridge", "steam")
    queue_names = (
        "commands/pending",
        "commands/approving",
        "commands/approved",
        "commands/executing",
        "commands/confirming",
        "validation-controls/approved",
        "validation-controls/inflight",
    )
    try:
        process_probe = build_process_probe(protected_process_names)
        queue_probe = build_queue_probe(root, queue_names)
        clone_probe = build_clone_probe(
            manifest_path,
            clone_save,
            clone_preview,
            bridge_dir=root,
            process_names=protected_process_names,
            process_probe=process_probe,
            queue_probe=queue_probe,
        )
        # The constructor must receive an actual fresh preflight.  A static
        # mapping in the config would make production watch trust caller data
        # and is therefore deliberately unsupported.
        clone_preflight = clone_probe()
        coordinator = PlanSequenceCoordinator(
            root,
            plan_id=config["plan_id"],
            step_ids=step_ids,
            expected_identity=expected_identity,
            intent_directory=intent_directory,
            clone_preflight=clone_preflight,
            clone_probe=clone_probe,
            process_probe=process_probe,
            publish_one=False,
            state_path=state_path,
        )
    except (BridgeAdapterError, CloneLaunchGuardError, PlanCoordinatorError, OSError, ValueError) as exc:
        reason = "coordinator_preflight_blocked"
        detail = str(exc) or exc.__class__.__name__

        def blocked_observer() -> Mapping[str, Any]:
            return {
                "schema": "oni.plan-coordinator-tick.v1",
                "status": "blocked",
                "stop_reason": reason,
                "blocker": detail[:160],
                "live_admission": False,
                "side_effects": {
                    "command_published": False,
                    "approval": False,
                    "execution": False,
                    "successor_advanced": False,
                },
            }

        return blocked_observer

    def observe() -> Mapping[str, Any]:
        try:
            result = coordinator.tick()
        except (BridgeAdapterError, CloneLaunchGuardError, PlanCoordinatorError, OSError, ValueError) as exc:
            return {
                "schema": "oni.plan-coordinator-tick.v1",
                "status": "blocked",
                "stop_reason": "coordinator_tick_blocked",
                "blocker": (str(exc) or exc.__class__.__name__)[:160],
                "live_admission": False,
                "side_effects": {
                    "command_published": False,
                    "approval": False,
                    "execution": False,
                    "successor_advanced": False,
                },
            }
        if not isinstance(result, Mapping):
            return {
                "schema": "oni.plan-coordinator-tick.v1",
                "status": "blocked",
                "stop_reason": "coordinator_result_invalid",
                "live_admission": False,
                "side_effects": {
                    "command_published": False,
                    "approval": False,
                    "execution": False,
                    "successor_advanced": False,
                },
            }
        return result

    return observe


def rotate_log(path: pathlib.Path) -> bool:
    if not path.exists() or path.stat().st_size < MAX_LOG_BYTES:
        return False
    try:
        oldest = pathlib.Path(str(path) + f".{LOG_BACKUPS}")
        oldest.unlink(missing_ok=True)
        for index in range(LOG_BACKUPS - 1, 0, -1):
            source = pathlib.Path(str(path) + f".{index}")
            target = pathlib.Path(str(path) + f".{index + 1}")
            if source.exists():
                source.replace(target)
        path.replace(pathlib.Path(str(path) + ".1"))
        return True
    except OSError:
        return False


def read_rotated_tail(
    path: pathlib.Path,
    position: int,
) -> Tuple[List[Dict[str, Any]], int, int, int]:
    """Read records appended before a watched log was moved to ``.1``.

    The probe and bridge do not share a lock around append and rotation.  A
    writer can therefore append after ``read_new_records`` sampled the active
    file but before ``rotate_log`` renames it.  The append moves with the old
    inode to ``.1``; starting the next read at byte zero of the new active file
    silently loses that tail.  Continue from the pre-rotation cursor in the
    moved file instead.  The cursor is intentionally supplied by the caller so
    already-consumed records are not replayed.
    """
    rotated = pathlib.Path(str(path) + ".1")
    return read_new_records(rotated, position)


def run_once(
    bridge_dir: pathlib.Path,
    input_path: Optional[pathlib.Path] = None,
    *,
    enable_llm: bool = False,
    secrets_dir: Optional[pathlib.Path] = None,
    goal: str = STRATEGY_GOAL,
    allow_replay_suggestions: bool = False,
    startup_expectations: Optional[Dict[str, Any]] = None,
    coordinator_observer: Optional[Callable[[], Mapping[str, Any]]] = None,
    planned_queue_owner: Optional[
        Callable[[pathlib.Path, Mapping[str, Any], List[Mapping[str, Any]], Mapping[str, Any]], Mapping[str, Any]]
    ] = None,
    model_loop: Optional[ModelLoop] = None,
) -> ReplayState:
    state_log = input_path or bridge_dir / "state.jsonl"
    if input_path is not None and allow_replay_suggestions:
        unsafe_markers = (
            bridge_dir / "state.jsonl",
            bridge_dir / "action-policy.json",
            bridge_dir / "commands",
        )
        if any(path.exists() for path in unsafe_markers):
            raise RuntimeError("Replay suggestions require a fresh isolated output directory.")
    try:
        runtime, _ = replay_initial_records(
            state_log,
            bridge_dir,
            allow_untrusted_snapshot_baseline=input_path is not None,
            startup_expectations=startup_expectations if input_path is None else None,
            require_startup_binding=input_path is None,
            strategy_goal=goal,
        )
        write_outputs(
            bridge_dir,
            runtime,
            enable_llm=enable_llm,
            secrets_dir=secrets_dir,
            goal=goal,
            replay_mode=input_path is not None,
            allow_replay_suggestions=allow_replay_suggestions,
            coordinator_observer=coordinator_observer,
            planned_queue_owner=planned_queue_owner,
            model_loop=model_loop,
        )
    except Exception as exc:
        publish_bridge_fault(bridge_dir, exc)
        raise
    return runtime


def watch(
    bridge_dir: pathlib.Path,
    poll_seconds: float,
    *,
    enable_llm: bool = False,
    secrets_dir: Optional[pathlib.Path] = None,
    goal: str = STRATEGY_GOAL,
    startup_expectations: Optional[Dict[str, Any]] = None,
    coordinator_observer: Optional[Callable[[], Mapping[str, Any]]] = None,
    planned_queue_owner: Optional[
        Callable[[pathlib.Path, Mapping[str, Any], List[Mapping[str, Any]], Mapping[str, Any]], Mapping[str, Any]]
    ] = None,
    model_loop: Optional[ModelLoop] = None,
) -> None:
    state_log = bridge_dir / "state.jsonl"
    # Any failure leaves an explicit untrusted health before the Bridge
    # exits non-zero; a partial generation is never retried in-process
    # because records may already be consumed but not archived.
    try:
        runtime, position = replay_initial_records(
            state_log,
            bridge_dir,
            startup_expectations=startup_expectations,
            require_startup_binding=True,
            strategy_goal=goal,
        )
        write_outputs(
            bridge_dir,
            runtime,
            enable_llm=enable_llm,
            secrets_dir=secrets_dir,
            goal=goal,
            coordinator_observer=coordinator_observer,
            planned_queue_owner=planned_queue_owner,
            model_loop=model_loop,
        )

        while True:
            records, position, invalid, partial = read_new_records(state_log, position)
            runtime.partial_bytes = partial
            runtime.process_many(records)
            runtime.invalid_json_count += invalid
            archive_session_records(bridge_dir, records, rebuild=False)
            write_outputs(
                bridge_dir,
                runtime,
                enable_llm=enable_llm,
                secrets_dir=secrets_dir,
                goal=goal,
                coordinator_observer=coordinator_observer,
                planned_queue_owner=planned_queue_owner,
                model_loop=model_loop,
            )
            if rotate_log(state_log):
                runtime.rotation_count += 1
                runtime.warnings.append("state_log_rotated")
                event_seq_before = runtime.last_seq
                snapshot_seq_before = runtime.last_snapshot_seq
                rotated_records, rotated_position, rotated_invalid, rotated_partial = read_rotated_tail(
                    state_log,
                    position,
                )
                if rotated_records:
                    runtime.process_many(rotated_records)
                    archive_session_records(bridge_dir, rotated_records, rebuild=False)
                runtime.invalid_json_count += rotated_invalid
                runtime.partial_bytes = rotated_partial
                runtime.rotation_tail_records_total += len(rotated_records)
                runtime.last_rotation_tail = {
                    "rotation_count": runtime.rotation_count,
                    "session_id": runtime.current_session_id,
                    "offset_before": position,
                    "offset_after": rotated_position,
                    "records": len(rotated_records),
                    "invalid_json": rotated_invalid,
                    "partial_bytes": rotated_partial,
                    "event_seq_before": event_seq_before,
                    "event_seq_after": runtime.last_seq,
                    "snapshot_seq_before": snapshot_seq_before,
                    "snapshot_seq_after": runtime.last_snapshot_seq,
                }
                if rotated_records or rotated_invalid or rotated_partial:
                    write_outputs(
                        bridge_dir,
                        runtime,
                        enable_llm=enable_llm,
                        secrets_dir=secrets_dir,
                        goal=goal,
                        coordinator_observer=coordinator_observer,
                        planned_queue_owner=planned_queue_owner,
                    )
                position = 0
            time.sleep(max(0.1, poll_seconds))
    except Exception as exc:
        publish_bridge_fault(bridge_dir, exc)
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Aggregate ONI AI Probe V2 JSONL into session-safe local outputs.")
    parser.add_argument("--bridge-dir", type=pathlib.Path, default=default_bridge_dir())
    parser.add_argument("--input", type=pathlib.Path, help="Read a specific JSONL file and exit.")
    parser.add_argument("--once", action="store_true", help="Process current input and exit.")
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--enable-llm", action="store_true")
    parser.add_argument("--secrets-dir", type=pathlib.Path)
    parser.add_argument(
        "--coordinator-config", type=pathlib.Path,
        help="explicit read-only clone-gated coordinator config; never enabled by default",
    )
    parser.add_argument(
        "--planned-owner-config", type=pathlib.Path,
        help="explicit clone-gated planned queue owner config; never enabled by default",
    )
    parser.add_argument("--goal", default=STRATEGY_GOAL)
    parser.add_argument(
        "--model-loop", action="store_true",
        help="let the configured model choose one catalogued candidate per decision; "
             "every choice still needs local human approval (watch mode only)",
    )
    parser.add_argument("--model-deadline-seconds", type=float, default=90.0)
    parser.add_argument("--expected-validation-run-id")
    parser.add_argument("--expected-validation-process-id", type=int)
    parser.add_argument("--expected-startup-binding-sha256")
    parser.add_argument("--expected-startup-intent-hash")
    parser.add_argument("--expected-runtime-config-evidence-sha256")
    parser.add_argument("--expected-loader-path-id-upper")
    parser.add_argument("--expected-loader-save-sha256")
    parser.add_argument("--expected-game-build")
    parser.add_argument("--expected-mod-version")
    parser.add_argument(
        "--allow-replay-suggestions",
        action="store_true",
        help="Allow pending suggestions only in a fresh isolated --input output directory.",
    )
    return parser.parse_args()


def startup_expectations_from_args(args: argparse.Namespace) -> Optional[Dict[str, Any]]:
    mapping = {
        "validation_run_id": args.expected_validation_run_id,
        "validation_process_id": args.expected_validation_process_id,
        "startup_binding_hash": args.expected_startup_binding_sha256,
        "startup_intent_hash": args.expected_startup_intent_hash,
        "runtime_config_evidence_sha256": args.expected_runtime_config_evidence_sha256,
        "loader_path_id_upper": args.expected_loader_path_id_upper,
        "loader_save_sha256": args.expected_loader_save_sha256,
        "game_build": args.expected_game_build,
        "mod_version": args.expected_mod_version,
    }
    supplied = [value is not None for value in mapping.values()]
    if any(supplied) and not all(supplied):
        raise ValueError("startup expectation CLI arguments must be supplied as one exact set")
    return validate_startup_expectations(mapping if all(supplied) else None)


def main() -> None:
    args = parse_args()
    try:
        startup_expectations = startup_expectations_from_args(args)
    except ValueError as exc:
        raise SystemExit(f"invalid startup expectations: {exc}") from exc
    try:
        coordinator_observer = (
            build_read_only_coordinator_observer(args.bridge_dir, args.coordinator_config)
            if args.coordinator_config is not None else None
        )
        planned_queue_owner = (
            build_planned_queue_owner_from_config(args.bridge_dir, args.planned_owner_config)
            if args.planned_owner_config is not None else None
        )
    except (CloneLaunchGuardError, PlanCoordinatorError, OSError, ValueError) as exc:
        raise SystemExit(f"invalid Bridge config: {exc}") from exc
    if coordinator_observer is not None and planned_queue_owner is not None:
        raise SystemExit("coordinator and planned-owner configs are mutually exclusive")
    if planned_queue_owner is not None and args.input is not None:
        raise SystemExit("planned owner cannot be used with replay --input")
    model_loop: Optional[ModelLoop] = None
    if args.model_loop:
        if args.once or args.input is not None:
            raise SystemExit("--model-loop needs watch mode; its model call is asynchronous")
        if planned_queue_owner is not None:
            raise SystemExit("--model-loop and the planned owner are mutually exclusive queue owners")
        if not 5.0 <= args.model_deadline_seconds <= 600.0:
            raise SystemExit("--model-deadline-seconds must be between 5 and 600")
        model_loop = ModelLoop(
            secrets_dir=args.secrets_dir,
            goal=args.goal,
            deadline_seconds=args.model_deadline_seconds,
        )
    if args.once or args.input:
        run_once(
            args.bridge_dir,
            args.input,
            enable_llm=args.enable_llm,
            secrets_dir=args.secrets_dir,
            goal=args.goal,
            allow_replay_suggestions=args.allow_replay_suggestions,
            startup_expectations=startup_expectations,
            coordinator_observer=coordinator_observer,
            planned_queue_owner=planned_queue_owner,
        )
    else:
        watch(
            args.bridge_dir,
            args.poll_seconds,
            enable_llm=args.enable_llm,
            secrets_dir=args.secrets_dir,
            goal=args.goal,
            startup_expectations=startup_expectations,
            coordinator_observer=coordinator_observer,
            planned_queue_owner=planned_queue_owner,
            model_loop=model_loop,
        )


if __name__ == "__main__":
    main()
