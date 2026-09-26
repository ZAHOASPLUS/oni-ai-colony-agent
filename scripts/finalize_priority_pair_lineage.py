#!/usr/bin/env python3
"""Fail-closed offline finalizer for one priority change and its inverse."""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import math
import os
import pathlib
import re
import stat
import sys
import tempfile
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.action_outcomes import (
    ActionOutcomeError,
    build_action_outcomes,
    command_intent_hash,
    confirmed_command_approval_bundle_path,
    load_confirmed_command_approval_bundle,
    validate_action_outcomes,
    validate_confirmed_command_approval_bundle,
)
from agent.approval_receipt import APPROVAL_RECEIPT_SCHEMA, canonical_hash
from agent.canonical_hash import CanonicalHashError, loads_strict
from agent.command_queue import COMMAND_PIPELINE_SCHEMA, command_pipeline_readiness
from agent.decision import DecisionFrameError, build_decision_frame, validate_decision_frame
from agent.decision_frame_archive import (
    DecisionFrameArchiveError,
    decision_frame_archive_path,
    load_archived_decision_frame,
)
from agent.field_contracts import (
    DUPLICANT_CHORE_PRIORITY_CONTRACT_ID,
    read_contract_field,
)
from agent.runtime_confirmation import (
    CONFIRMED_MUTATION_MODE,
    archived_confirmation_records,
    assess_command_confirmation,
    confirmation_summary,
)
from agent.state_quality import (
    canonical_state_hash,
    parse_positive_sequence,
    parse_snapshot_timestamp,
    snapshot_freshness,
    snapshot_trust_blockers,
)
from bridge import oni_bridge
from scripts.manage_save_lineage import (
    COMMAND_DIRECTORIES,
    SCHEMA as LINEAGE_SCHEMA,
    checkpoint_hash,
    normalized_path,
    path_within,
    sha256_file,
    state_log_paths,
    verify_lineage,
)


REPORT_SCHEMA = "oni.priority-pair-lineage-finalization.v1"
COMMAND_FREEZE_SCHEMA = "oni.priority-command-lineage-freeze.v1"
DEFAULT_FORWARD_STEP = "priority_forward"
DEFAULT_INVERSE_STEP = "priority_inverse"
ACTION = "set_duplicant_chore_priority"
NONTERMINAL_DIRECTORIES = ("pending", "approving", "approved", "executing", "confirming")
PAIR_CREATION_MAX_STATE_AGE_SECONDS = 120.0
BRIDGE_DERIVED_SNAPSHOT_FIELDS = frozenset(
    {
        "stale",
        "snapshot_age_seconds",
        "trust_ready",
        "trust_blockers",
        "replay_mode",
    }
)
BRIDGE_HEALTH_FIELDS = frozenset(
    {
        "schema",
        "updated_at",
        "status",
        "current_session_id",
        "latest_snapshot_id",
        "latest_seq",
        "snapshot_age_seconds",
        "invalid_json_count",
        "duplicate_seq_count",
        "out_of_order_seq_count",
        "seq_gap_count",
        "duplicate_snapshot_seq_count",
        "out_of_order_snapshot_seq_count",
        "snapshot_seq_gap_count",
        "retired_session_event_count",
        "partial_bytes",
        "rotation_count",
        "actions_enabled",
        "policy_error",
        "trust_ready",
        "trust_blockers",
        "session_start_observed",
        "startup_binding",
        "replay_mode",
        "warnings",
        "evidence_continuity",
        "command_pipeline",
        "source",
        "strategy_status",
        "strategy_phase",
        "strategy_confidence",
        "action_outcomes_hash",
        "action_outcomes",
        "decision_frame_id",
        "decision_frame_trust",
        "decision_frame_readiness",
    }
)
BRIDGE_STARTUP_BINDING_FIELDS = frozenset(
    {
        "required",
        "external_expectations_supplied",
        "verified",
        "verified_session_id",
        "validation_process_id",
        "requested_save_path_id",
        "current_active_save_path_id",
        "current_save_id_hash",
        "distinct_path_transition_count",
        "save_evidence_pending",
        "reload_transition_ready",
        "blockers",
    }
)
BRIDGE_COMMAND_PIPELINE_FIELDS = frozenset(
    {
        "schema",
        "ready",
        "blockers",
        "readiness",
        "counts",
        "in_flight_mutation_count",
        "ambiguous_outcome_count",
        "duplicate_id_count",
        "stale_pending_count",
        "invalid_record_count",
        "proposal_frame_evaluated",
        "proposal_frame_binding",
    }
)
BRIDGE_EVIDENCE_CONTINUITY_FIELDS = frozenset(
    {"status", "unresolved_command_count", "recovered_record_count", "blockers"}
)
BRIDGE_ACTION_OUTCOME_COUNT_FIELDS = frozenset(
    {"executed_verified", "awaiting_trusted_snapshot", "snapshot_confirmed", "rejected"}
)
BRIDGE_DECISION_READINESS_FIELDS = frozenset(
    {"observe", "advise", "propose", "advance", "claim_sustainable"}
)
HASH_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
GROUP_PATTERN = re.compile(r"^[A-Za-z0-9_]{1,64}$")
STEP_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,79}$")
RECEIPT_KEYS = frozenset(
    {
        "schema",
        "command_id",
        "command_intent_hash",
        "source_frame_id",
        "source",
        "policy_hash",
        "approved_by",
        "approved_at",
        "receipt_hash",
    }
)
RECEIPT_SOURCE_KEYS = frozenset(
    {"session_id", "save_id_hash", "world_id", "snapshot_id", "state_hash"}
)
COMMAND_FREEZE_KEYS = frozenset(
    {
        "schema",
        "step_id",
        "test_run_id",
        "manifest_binding",
        "approval_bundle",
        "confirmation_certificate",
        "confirmation_records",
        "frozen_at",
        "freeze_hash",
    }
)


class PriorityPairError(RuntimeError):
    """A stable fail-closed blocker for the G0 priority pair."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def require(condition: bool, code: str, detail: str = "") -> None:
    if not condition:
        raise PriorityPairError(code, detail)


def _strict_object(path: pathlib.Path, label: str) -> Dict[str, Any]:
    try:
        metadata = path.lstat()
        require(not path.is_symlink() and stat.S_ISREG(metadata.st_mode), "evidence_file_not_regular", label)
        value = loads_strict(path.read_text(encoding="utf-8-sig"))
    except PriorityPairError:
        raise
    except (OSError, UnicodeError, CanonicalHashError, TypeError, ValueError) as exc:
        raise PriorityPairError("evidence_json_invalid", f"{label}: {exc}") from exc
    require(isinstance(value, dict), "evidence_json_not_object", label)
    return dict(value)


def _regular_file_state(path: pathlib.Path, label: str) -> Tuple[int, int, int, int, int, int]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise PriorityPairError("evidence_file_unavailable", f"{label}: {exc}") from exc
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    attributes = int(getattr(metadata, "st_file_attributes", 0) or 0)
    require(
        not stat.S_ISLNK(metadata.st_mode)
        and stat.S_ISREG(metadata.st_mode)
        and int(metadata.st_nlink) == 1
        and not (attributes & reparse_flag),
        "evidence_file_not_regular",
        label,
    )
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(stat.S_IFMT(metadata.st_mode)),
        int(metadata.st_nlink),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
    )


def _stable_binary_snapshot(
    path: pathlib.Path,
    label: str,
    *,
    maximum: int = 64 * 1024 * 1024,
) -> Dict[str, Any]:
    """Read through one pinned descriptor and rebind its pathname afterward."""

    before = _regular_file_state(path, label)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    descriptor: Optional[int] = None
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise PriorityPairError("evidence_file_unavailable", f"{label}: {exc}") from exc
    try:
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            descriptor = None
            opened = os.fstat(handle.fileno())
            opened_state = (
                int(opened.st_dev),
                int(opened.st_ino),
                int(stat.S_IFMT(opened.st_mode)),
                int(opened.st_nlink),
                int(opened.st_size),
                int(opened.st_mtime_ns),
            )
            before_descriptor_state = before
            require(
                opened_state == before_descriptor_state,
                "evidence_file_name_swapped",
                label,
            )
            raw = handle.read(maximum + 1)
            require(len(raw) <= maximum, "evidence_file_size_invalid", label)
            after_handle = os.fstat(handle.fileno())
            after_handle_state = (
                int(after_handle.st_dev),
                int(after_handle.st_ino),
                int(stat.S_IFMT(after_handle.st_mode)),
                int(after_handle.st_nlink),
                int(after_handle.st_size),
                int(after_handle.st_mtime_ns),
            )
    finally:
        if descriptor is not None:
            os.close(descriptor)
    after = _regular_file_state(path, label)
    require(
        before == after
        and opened_state == after_handle_state == before_descriptor_state
        and len(raw) == before[4],
        "evidence_file_changed_during_read",
        label,
    )
    return {
        "raw": raw,
        "file_sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
        "file_state": before,
    }


def _strict_object_snapshot(path: pathlib.Path, label: str) -> Dict[str, Any]:
    """Read one create-stable regular JSON file and bind its exact bytes."""

    binary = _stable_binary_snapshot(path, label, maximum=16 * 1024 * 1024)
    raw = binary["raw"]
    try:
        value = loads_strict(raw.decode("utf-8-sig", errors="strict"))
    except (UnicodeError, CanonicalHashError, TypeError, ValueError) as exc:
        raise PriorityPairError("evidence_json_invalid", f"{label}: {exc}") from exc
    require(isinstance(value, dict), "evidence_json_not_object", label)
    try:
        document_hash = canonical_hash(value)
    except (CanonicalHashError, TypeError, ValueError, OverflowError) as exc:
        raise PriorityPairError("canonical_hash_invalid", label) from exc
    return {
        "document": dict(value),
        "file_sha256": binary["file_sha256"],
        "document_hash": document_hash,
        "file_state": binary["file_state"],
    }


def _confirm_object_snapshot(path: pathlib.Path, label: str, frozen: Mapping[str, Any]) -> None:
    current = _strict_object_snapshot(path, label)
    require(
        current.get("file_sha256") == frozen.get("file_sha256")
        and current.get("document_hash") == frozen.get("document_hash")
        and current.get("file_state") == frozen.get("file_state"),
        "evidence_file_changed_after_validation",
        label,
    )


def _confirm_binary_snapshot(
    path: pathlib.Path,
    label: str,
    frozen: Mapping[str, Any],
    *,
    maximum: int = 256 * 1024 * 1024,
) -> None:
    current = _stable_binary_snapshot(path, label, maximum=maximum)
    require(
        current.get("file_sha256") == frozen.get("file_sha256")
        and current.get("file_state") == frozen.get("file_state"),
        "evidence_file_changed_after_validation",
        label,
    )


def _aware_time(value: Any, code: str) -> dt.datetime:
    parsed = parse_snapshot_timestamp(value)
    require(parsed is not None, code, str(value))
    assert parsed is not None
    return parsed


def _utc_now() -> dt.datetime:
    """Return the pair-publication clock used by the freshness proof."""

    return dt.datetime.now(dt.timezone.utc)


def _hash_document(value: Mapping[str, Any], hash_field: str) -> str:
    payload = copy.deepcopy(dict(value))
    payload.pop(hash_field, None)
    try:
        return canonical_hash(payload)
    except (CanonicalHashError, TypeError, ValueError, OverflowError) as exc:
        raise PriorityPairError("canonical_hash_invalid", hash_field) from exc


def _canonical_equal(left: Any, right: Any) -> bool:
    try:
        return canonical_hash(left) == canonical_hash(right)
    except (CanonicalHashError, TypeError, ValueError, OverflowError):
        return False


def snapshot_core_without_bridge(state: Mapping[str, Any]) -> Dict[str, Any]:
    """Return the Probe-owned surface, excluding Bridge publication data.

    A raw Probe record is never trusted to self-declare a ``bridge`` object.
    The live Bridge overwrites that one field before publishing
    ``latest_state.json``; every other field remains exact raw evidence.
    """

    require(isinstance(state, Mapping), "snapshot_not_object")
    core = copy.deepcopy(dict(state))
    core.pop("bridge", None)
    return core


def _nonnegative_finite_number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        numeric = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) and numeric >= 0.0 else None


def _nonnegative_integer(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def validate_enriched_latest_against_raw(
    raw_state: Mapping[str, Any],
    enriched_state: Mapping[str, Any],
    health: Mapping[str, Any],
) -> Dict[str, Any]:
    """Bind a Bridge publication to one raw Probe snapshot, fail closed.

    This public helper deliberately treats only top-level ``bridge`` as
    Bridge-derived.  The health document is not accepted as a loose assertion:
    its complete live-output shape and every trust-affecting projection are
    checked here.  The caller must additionally bind the checked values to the
    durable state log and Bridge companion files; the pair finalizer does that
    before publication.
    """

    require(isinstance(raw_state, Mapping), "raw_tip_snapshot_not_object")
    require(isinstance(enriched_state, Mapping), "latest_state_not_object")
    require(isinstance(health, Mapping), "bridge_health_not_object")
    raw_core = snapshot_core_without_bridge(raw_state)
    enriched_core = snapshot_core_without_bridge(enriched_state)
    require(_canonical_equal(raw_core, enriched_core), "latest_state_core_mismatch")
    core_hash = canonical_state_hash(raw_core)
    require(
        canonical_state_hash(dict(enriched_state)) == core_hash,
        "latest_state_core_hash_mismatch",
    )

    bridge_value = enriched_state.get("bridge")
    bridge = bridge_value if isinstance(bridge_value, Mapping) else {}
    require(
        set(bridge) == BRIDGE_DERIVED_SNAPSHOT_FIELDS,
        "latest_state_bridge_fields_invalid",
    )
    bridge_age = _nonnegative_finite_number(bridge.get("snapshot_age_seconds"))
    require(
        bridge.get("stale") is False
        and bridge.get("replay_mode") is False
        and isinstance(bridge.get("trust_ready"), bool)
        and bridge.get("trust_blockers") == []
        and bridge_age is not None,
        "latest_state_bridge_projection_invalid",
    )

    require(
        set(health) == BRIDGE_HEALTH_FIELDS,
        "bridge_health_fields_invalid",
    )
    require(
        health.get("schema") == "oni.bridge.health.v1"
        and health.get("status") in {"ok", "session_ended"}
        and health.get("replay_mode") is False
        and health.get("current_session_id") == raw_core.get("session_id")
        and health.get("latest_snapshot_id") == raw_core.get("snapshot_id")
        and health.get("latest_seq") == raw_core.get("seq")
        and health.get("trust_blockers") == []
        and isinstance(health.get("trust_ready"), bool)
        and health.get("session_start_observed") is True,
        "bridge_health_projection_invalid",
    )
    integrity_counters = (
        "invalid_json_count",
        "duplicate_seq_count",
        "out_of_order_seq_count",
        "seq_gap_count",
        "duplicate_snapshot_seq_count",
        "out_of_order_snapshot_seq_count",
        "snapshot_seq_gap_count",
        "retired_session_event_count",
        "partial_bytes",
        "rotation_count",
    )
    require(
        all(_nonnegative_integer(health.get(field)) is not None for field in integrity_counters),
        "bridge_health_integrity_counters_invalid",
    )
    require(
        all(
            health.get(field) == 0
            for field in (
                "invalid_json_count",
                "duplicate_seq_count",
                "out_of_order_seq_count",
                "seq_gap_count",
                "duplicate_snapshot_seq_count",
                "out_of_order_snapshot_seq_count",
                "snapshot_seq_gap_count",
                "partial_bytes",
            )
        ),
        "bridge_health_integrity_blocker_present",
    )
    require(
        health.get("actions_enabled") is False
        and health.get("policy_error") is None
        and isinstance(health.get("warnings"), list)
        and len(health.get("warnings")) <= 20
        and all(isinstance(item, str) and bool(item) for item in health.get("warnings")),
        "bridge_health_shutdown_gate_invalid",
    )

    startup_value = health.get("startup_binding")
    startup = startup_value if isinstance(startup_value, Mapping) else {}
    require(
        set(startup) == BRIDGE_STARTUP_BINDING_FIELDS,
        "bridge_health_startup_fields_invalid",
    )
    required = startup.get("required")
    require(required is True, "bridge_health_startup_required_invalid")
    transition_count = _nonnegative_integer(startup.get("distinct_path_transition_count"))
    process_id = startup.get("validation_process_id")
    requested_path = startup.get("requested_save_path_id")
    current_path = startup.get("current_active_save_path_id")
    current_save = startup.get("current_save_id_hash")
    require(
        startup.get("external_expectations_supplied") is True
        and startup.get("verified") is True
        and startup.get("verified_session_id") == raw_core.get("session_id")
        and isinstance(process_id, int)
        and not isinstance(process_id, bool)
        and process_id > 0
        and process_id == raw_core.get("validation_process_id")
        and requested_path == raw_core.get("requested_save_path_id")
        and current_path == raw_core.get("active_save_path_id")
        and current_save == raw_core.get("save_id_hash")
        and isinstance(requested_path, str)
        and HASH_PATTERN.fullmatch(requested_path) is not None
        and isinstance(current_path, str)
        and HASH_PATTERN.fullmatch(current_path) is not None
        and isinstance(current_save, str)
        and HASH_PATTERN.fullmatch(current_save) is not None
        and raw_core.get("startup_binding_status") == "verified"
        and transition_count is not None
        and startup.get("save_evidence_pending") is False
        and startup.get("reload_transition_ready") is False
        and startup.get("blockers") == [],
        "bridge_health_startup_projection_invalid",
    )

    continuity_value = health.get("evidence_continuity")
    continuity = continuity_value if isinstance(continuity_value, Mapping) else {}
    require(
        set(continuity) == BRIDGE_EVIDENCE_CONTINUITY_FIELDS
        and continuity.get("status") == "ready"
        and _nonnegative_integer(continuity.get("unresolved_command_count")) == 0
        and _nonnegative_integer(continuity.get("recovered_record_count")) is not None
        and continuity.get("blockers") == [],
        "bridge_health_evidence_continuity_invalid",
    )

    command_value = health.get("command_pipeline")
    command = command_value if isinstance(command_value, Mapping) else {}
    readiness_value = command.get("readiness")
    readiness = readiness_value if isinstance(readiness_value, Mapping) else {}
    proposal = readiness.get("mutation_proposal")
    confirmation = readiness.get("mutation_confirmation")
    counts_value = command.get("counts")
    counts = counts_value if isinstance(counts_value, Mapping) else {}
    require(
        set(command) == BRIDGE_COMMAND_PIPELINE_FIELDS
        and command.get("schema") == COMMAND_PIPELINE_SCHEMA
        and command.get("ready") is True
        and command.get("blockers") == []
        and set(readiness) == {"mutation_proposal", "mutation_confirmation"}
        and proposal == {"ready": True, "blockers": []}
        and confirmation == {"ready": True, "blockers": [], "candidate_ids": []}
        and set(counts)
        == {"pending", "approving", "approved", "executing", "confirming", "failed"}
        and all(_nonnegative_integer(value) == 0 for value in counts.values())
        and all(
            _nonnegative_integer(command.get(field)) == 0
            for field in (
                "in_flight_mutation_count",
                "ambiguous_outcome_count",
                "duplicate_id_count",
                "stale_pending_count",
                "invalid_record_count",
            )
        )
        and command.get("proposal_frame_evaluated") is False
        and command.get("proposal_frame_binding")
        == "deferred_until_sync_pending_commands",
        "bridge_health_command_pipeline_invalid",
    )

    outcome_counts_value = health.get("action_outcomes")
    outcome_counts = outcome_counts_value if isinstance(outcome_counts_value, Mapping) else {}
    decision_readiness_value = health.get("decision_frame_readiness")
    decision_readiness = (
        decision_readiness_value
        if isinstance(decision_readiness_value, Mapping)
        else {}
    )
    require(
        isinstance(health.get("action_outcomes_hash"), str)
        and HASH_PATTERN.fullmatch(str(health.get("action_outcomes_hash"))) is not None
        and set(outcome_counts) == BRIDGE_ACTION_OUTCOME_COUNT_FIELDS
        and all(_nonnegative_integer(value) is not None for value in outcome_counts.values()),
        "bridge_health_action_outcomes_invalid",
    )
    require(
        isinstance(health.get("decision_frame_id"), str)
        and HASH_PATTERN.fullmatch(str(health.get("decision_frame_id"))) is not None
        and health.get("decision_frame_trust") in {"trusted", "blocked"}
        and set(decision_readiness) == BRIDGE_DECISION_READINESS_FIELDS
        and all(isinstance(value, bool) for value in decision_readiness.values()),
        "bridge_health_decision_frame_invalid",
    )
    require(
        all(
            isinstance(health.get(field), str) and bool(health.get(field))
            for field in ("strategy_status", "strategy_phase", "strategy_confidence")
        ),
        "bridge_health_strategy_invalid",
    )

    expected_trust_ready = health.get("status") == "ok"
    require(
        health.get("trust_ready") is expected_trust_ready
        and bridge.get("trust_ready") is expected_trust_ready
        and bridge.get("trust_blockers") == health.get("trust_blockers"),
        "bridge_health_trust_mismatch",
    )
    if health.get("status") == "session_ended":
        require(
            health.get("decision_frame_trust") == "blocked"
            and all(value is False for value in decision_readiness.values()),
            "bridge_health_terminal_decision_invalid",
        )

    health_age = _nonnegative_finite_number(health.get("snapshot_age_seconds"))
    require(
        health_age is not None and abs(float(bridge_age) - float(health_age)) <= 1.1,
        "bridge_health_age_mismatch",
    )
    observed_at = _aware_time(raw_core.get("ts"), "raw_tip_timestamp_invalid")
    published_at = _aware_time(health.get("updated_at"), "bridge_health_timestamp_invalid")
    require(observed_at <= published_at, "raw_tip_after_health_publication")
    computed_age = max(0.0, (published_at - observed_at).total_seconds())
    require(
        abs(float(bridge_age) - computed_age) <= 1.1
        and abs(float(health_age) - computed_age) <= 1.1,
        "bridge_publication_age_mismatch",
    )
    require(
        snapshot_trust_blockers(raw_core, now=published_at) == [],
        "raw_tip_snapshot_untrusted",
    )

    expected_source = {
        "session_id": raw_core.get("session_id"),
        "snapshot_id": raw_core.get("snapshot_id"),
        "state_hash": core_hash,
    }
    source = health.get("source") if isinstance(health.get("source"), Mapping) else {}
    require(
        set(source) == set(expected_source) and _canonical_equal(source, expected_source),
        "bridge_health_source_mismatch",
    )
    try:
        enriched_hash = canonical_hash(dict(enriched_state))
    except (CanonicalHashError, TypeError, ValueError, OverflowError) as exc:
        raise PriorityPairError("latest_state_enriched_hash_invalid") from exc
    return {
        "core_state_hash": core_hash,
        "enriched_state_hash": enriched_hash,
        "health_status": health.get("status"),
        "terminal": health.get("status") == "session_ended",
        "bridge": copy.deepcopy(dict(bridge)),
        "health_updated_at": published_at.isoformat(timespec="microseconds"),
        "startup_binding_required": required,
    }


def _bounded_bridge_replay(
    bridge_dir: pathlib.Path,
    raw_state: Mapping[str, Any],
    health: Mapping[str, Any],
    *,
    published_at: dt.datetime,
) -> Tuple[Dict[str, Any], Dict[str, Dict[str, Any]]]:
    """Rebuild the live Bridge trust surface from its bounded durable input."""

    paths = [path for path in state_log_paths(bridge_dir) if path.is_file()]
    require(bool(paths), "bridge_state_log_missing")
    records: list[Dict[str, Any]] = []
    total_lines = 0
    file_hashes: list[Dict[str, Any]] = []
    frozen_logs: Dict[str, Dict[str, Any]] = {}
    for path in paths:
        binary = _stable_binary_snapshot(
            path,
            f"Bridge state log:{path.name}",
            maximum=256 * 1024 * 1024,
        )
        raw_bytes = binary["raw"]
        require(
            not raw_bytes or raw_bytes.endswith(b"\n"),
            "bridge_state_log_partial_record",
            path.name,
        )
        lines = raw_bytes.splitlines()
        total_lines += len(lines)
        require(
            total_lines <= oni_bridge.MAX_INITIAL_LINES,
            "bridge_state_log_replay_limit_exceeded",
            str(total_lines),
        )
        file_hashes.append(
            {
                "path": _relative(path, bridge_dir),
                "file_sha256": binary["file_sha256"],
                "line_count": len(lines),
            }
        )
        frozen_logs[str(path)] = {
            "file_sha256": binary["file_sha256"],
            "file_state": binary["file_state"],
        }
        for raw_line in lines:
            try:
                record, invalid = oni_bridge.parse_json_line(
                    raw_line.decode("utf-8-sig", errors="strict")
                )
            except UnicodeError as exc:
                raise PriorityPairError("bridge_state_log_encoding_invalid", path.name) from exc
            require(
                not invalid and isinstance(record, dict),
                "bridge_state_log_record_invalid",
                path.name,
            )
            records.append(record)

    validation_run_id = raw_state.get("validation_run_id")
    require(
        isinstance(validation_run_id, str) and bool(validation_run_id),
        "bridge_replay_validation_run_id_invalid",
    )
    origin_candidates = [
        record
        for record in records
        if record.get("event") == "session_started"
        and record.get("reason") == "save_loader_load_complete"
        and record.get("validation_run_id") == validation_run_id
    ]
    require(bool(origin_candidates), "bridge_replay_origin_start_missing")
    origin = origin_candidates[0]
    require(
        origin.get("requested_save_path_id") == origin.get("active_save_path_id"),
        "bridge_replay_origin_loader_path_mismatch",
    )
    startup_expectations = {
        "validation_run_id": validation_run_id,
        "validation_process_id": origin.get("validation_process_id"),
        "startup_binding_hash": origin.get("startup_binding_hash"),
        "startup_intent_hash": origin.get("startup_intent_hash"),
        "runtime_config_evidence_sha256": origin.get("runtime_config_evidence_sha256"),
        "loader_path_id_upper": origin.get("active_save_path_id"),
        "loader_save_sha256": origin.get("save_id_hash"),
        "game_build": origin.get("game_build"),
        "mod_version": origin.get("mod_version"),
    }
    try:
        startup_expectations = oni_bridge.validate_startup_expectations(startup_expectations)
    except ValueError as exc:
        raise PriorityPairError("bridge_replay_startup_expectations_invalid", str(exc)) from exc
    require(startup_expectations is not None, "bridge_replay_startup_expectations_invalid")
    # Live Bridge freshness is evaluated when each record arrives.  A later
    # offline replay cannot reconstruct those individual arrival clocks: an
    # otherwise valid historical snapshot will naturally be stale relative to
    # the final health publication.  Validate every immutable trust property
    # against that publication (discarding *only* the transient stale result),
    # then use ReplayState's non-actionable baseline mode so its internal wall
    # clock cannot manufacture new historical stale warnings.  The exact final
    # raw snapshot is still checked against ``published_at`` below.
    runtime = oni_bridge.ReplayState(
        allow_untrusted_snapshot_baseline=True,
        startup_expectations=startup_expectations,
        require_startup_binding=True,
    )
    path_index = 0
    record_index = 0
    for path_entry in file_hashes:
        line_count = int(path_entry["line_count"])
        if path_index > 0:
            runtime.rotation_count += 1
            runtime.warnings.append("state_log_rotated")
        for record in records[record_index : record_index + line_count]:
            if record.get("event") == "state_snapshot":
                observed_at = parse_snapshot_timestamp(record.get("ts"))
                require(
                    observed_at is not None,
                    "bridge_replay_historical_snapshot_untrusted",
                    '["timestamp_invalid"]',
                )
                assert observed_at is not None
                require(
                    observed_at <= published_at,
                    "bridge_replay_historical_snapshot_after_publication",
                )
                historical_blockers = set(
                    snapshot_trust_blockers(record, now=published_at)
                )
                historical_blockers.discard("snapshot_stale")
                require(
                    not historical_blockers,
                    "bridge_replay_historical_snapshot_untrusted",
                    json.dumps(sorted(historical_blockers)),
                )
            runtime.process(record)
        record_index += line_count
        path_index += 1
    require(runtime.latest is not None, "bridge_replay_latest_missing")
    assert runtime.latest is not None
    require(
        _canonical_equal(
            snapshot_core_without_bridge(runtime.latest),
            snapshot_core_without_bridge(raw_state),
        ),
        "bridge_replay_latest_mismatch",
    )
    expected_startup = {
        "required": True,
        "external_expectations_supplied": True,
        "verified": bool(runtime.startup_verified_session_id)
        and not runtime.startup_integrity_blockers,
        "verified_session_id": runtime.startup_verified_session_id,
        "validation_process_id": runtime.validation_process_id,
        "requested_save_path_id": runtime.startup_requested_save_path_id,
        "current_active_save_path_id": runtime.startup_current_active_save_path_id,
        "current_save_id_hash": runtime.startup_current_save_id_hash,
        "distinct_path_transition_count": runtime.startup_distinct_path_transition_count,
        "save_evidence_pending": runtime.startup_pending_save_evidence is not None,
        "reload_transition_ready": runtime.startup_reload_ready_session_id is not None,
        "blockers": sorted(runtime.startup_integrity_blockers),
    }
    require(
        health.get("startup_binding") == expected_startup,
        "bridge_health_startup_replay_mismatch",
    )
    counter_fields = (
        "invalid_json_count",
        "duplicate_seq_count",
        "out_of_order_seq_count",
        "seq_gap_count",
        "duplicate_snapshot_seq_count",
        "out_of_order_snapshot_seq_count",
        "snapshot_seq_gap_count",
        "retired_session_event_count",
        "partial_bytes",
    )
    expected_counters = {
        field: int(getattr(runtime, field))
        for field in counter_fields
    }
    require(
        all(health.get(field) == expected_counters[field] for field in counter_fields),
        "bridge_health_counter_replay_mismatch",
    )
    reported_rotation_count = _nonnegative_integer(health.get("rotation_count"))
    require(
        reported_rotation_count is not None
        and reported_rotation_count >= runtime.rotation_count,
        "bridge_health_rotation_projection_invalid",
    )
    require(
        "untrusted_snapshot:snapshot_stale" not in health.get("warnings", []),
        "bridge_health_historical_stale_warning",
    )
    replay_warnings_without_rotation = [
        item for item in runtime.warnings if item != "state_log_rotated"
    ]
    health_warnings_without_rotation = [
        item for item in health.get("warnings", []) if item != "state_log_rotated"
    ]
    require(
        health_warnings_without_rotation == replay_warnings_without_rotation
        and health.get("warnings", []).count("state_log_rotated")
        <= reported_rotation_count,
        "bridge_health_warning_replay_mismatch",
        json.dumps(
            {
                "health": health.get("warnings", []),
                "replay": list(runtime.warnings),
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
    )
    trust_blockers = sorted(
        set(snapshot_trust_blockers(runtime.latest, now=published_at))
        | set(runtime.session_integrity_blockers)
        | set(runtime.startup_integrity_blockers)
        | ({"session_start_not_observed"} if not runtime.session_start_observed else set())
    )
    if runtime.latest is None:
        expected_status = "waiting_current_session_snapshot"
    elif runtime.session_ended:
        expected_status = "session_ended"
    elif "snapshot_stale" in trust_blockers:
        expected_status = "stale"
    elif trust_blockers:
        expected_status = "untrusted_snapshot"
    else:
        expected_status = "ok"
    require(
        health.get("status") == expected_status
        and health.get("trust_blockers") == trust_blockers
        and health.get("trust_ready") is (expected_status == "ok")
        and health.get("session_start_observed") is runtime.session_start_observed
        and health.get("current_session_id") == runtime.current_session_id,
        "bridge_health_replay_projection_mismatch",
    )
    return (
        {
            "record_count": len(records),
            "reported_rotation_count": reported_rotation_count,
            "retained_rotation_boundary_count": runtime.rotation_count,
            "state_logs": file_hashes,
            "startup_expectations_hash": canonical_hash(startup_expectations),
            "warnings": list(health.get("warnings", [])),
            "counters": expected_counters,
        },
        frozen_logs,
    )


def _terminal_archive_evidence(
    bridge_dir: pathlib.Path,
    raw_state: Mapping[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    session_id = raw_state.get("session_id")
    require(isinstance(session_id, str) and bool(session_id), "terminal_session_id_invalid")
    archive = bridge_dir / "sessions" / oni_bridge.safe_session_name(session_id) / "state.jsonl"
    binary = _stable_binary_snapshot(
        archive,
        "terminal session archive",
        maximum=256 * 1024 * 1024,
    )
    raw_bytes = binary["raw"]
    require(raw_bytes.endswith(b"\n"), "terminal_session_archive_partial")
    records: list[Dict[str, Any]] = []
    for raw_line in raw_bytes.splitlines():
        try:
            value = loads_strict(raw_line.decode("utf-8-sig", errors="strict"))
        except (UnicodeError, CanonicalHashError, TypeError, ValueError) as exc:
            raise PriorityPairError("terminal_session_archive_json_invalid") from exc
        require(isinstance(value, dict), "terminal_session_archive_record_invalid")
        records.append(dict(value))
    require(bool(records), "terminal_session_archive_empty")
    require(
        all(record.get("session_id") == session_id for record in records),
        "terminal_session_archive_cross_session",
    )
    sequences = [parse_positive_sequence(record.get("seq")) for record in records]
    require(
        None not in sequences
        and sequences == list(range(1, len(records) + 1)),
        "terminal_session_archive_sequence_invalid",
    )
    snapshots = [record for record in records if record.get("event") == "state_snapshot"]
    snapshot_sequences = [parse_positive_sequence(record.get("snapshot_seq")) for record in snapshots]
    require(
        None not in snapshot_sequences
        and snapshot_sequences == list(range(1, len(snapshots) + 1)),
        "terminal_session_archive_snapshot_sequence_invalid",
    )
    snapshot_ids = [record.get("snapshot_id") for record in snapshots]
    require(
        all(isinstance(item, str) and bool(item) for item in snapshot_ids)
        and len(set(snapshot_ids)) == len(snapshot_ids),
        "terminal_session_archive_snapshot_id_invalid",
    )
    raw_core = snapshot_core_without_bridge(raw_state)
    matches = [
        index
        for index, record in enumerate(records)
        if record.get("event") == "state_snapshot"
        and _canonical_equal(snapshot_core_without_bridge(record), raw_core)
    ]
    require(len(matches) == 1, "terminal_tip_missing_or_ambiguous")
    tip_index = matches[0]
    require(
        snapshots and snapshots[-1].get("snapshot_id") == raw_core.get("snapshot_id"),
        "terminal_tip_not_last_snapshot",
    )
    tail = records[tip_index + 1 :]
    require(
        len(tail) == 2
        and tail[0].get("event") == "session_ended"
        and tail[0].get("reason") == "application_quit"
        and tail[1].get("event") == "probe_shutdown",
        "terminal_archive_tail_invalid",
    )
    tip_at = _aware_time(raw_core.get("ts"), "terminal_tip_timestamp_invalid")
    ended_at = _aware_time(tail[0].get("ts"), "terminal_end_timestamp_invalid")
    shutdown_at = _aware_time(tail[1].get("ts"), "terminal_shutdown_timestamp_invalid")
    require(tip_at < ended_at <= shutdown_at, "terminal_archive_time_order_invalid")
    return (
        {
            "path": _relative(archive, bridge_dir),
            "file_sha256": binary["file_sha256"],
            "record_count": len(records),
            "snapshot_count": len(snapshots),
            "tip_seq": raw_core.get("seq"),
            "tip_ts": tip_at.isoformat(timespec="microseconds"),
            "session_ended_at": ended_at.isoformat(timespec="microseconds"),
            "probe_shutdown_at": shutdown_at.isoformat(timespec="microseconds"),
            "terminal_tail": ["session_ended:application_quit", "probe_shutdown"],
        },
        {
            "path": archive,
            "file_sha256": binary["file_sha256"],
            "file_state": binary["file_state"],
        },
    )


def _bridge_companion_evidence(
    bridge_dir: pathlib.Path,
    raw_state: Mapping[str, Any],
    enriched_state: Mapping[str, Any],
    health: Mapping[str, Any],
    gate: Mapping[str, Any],
    policy: Mapping[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Dict[str, Any]]]:
    expected_pipeline = {
        "schema": gate.get("schema"),
        "ready": bool(gate.get("ready")),
        "blockers": list(gate.get("blockers") or []),
        "readiness": copy.deepcopy(gate.get("readiness") or {}),
        "counts": dict(sorted((gate.get("counts") or {}).items())),
        "in_flight_mutation_count": len(gate.get("in_flight_mutations") or []),
        "ambiguous_outcome_count": len(gate.get("ambiguous_outcomes") or []),
        "duplicate_id_count": len(gate.get("duplicate_ids") or []),
        "stale_pending_count": len(gate.get("stale_pending") or []),
        "invalid_record_count": len(gate.get("invalid_records") or []),
        "proposal_frame_evaluated": False,
        "proposal_frame_binding": "deferred_until_sync_pending_commands",
    }
    require(
        health.get("command_pipeline") == expected_pipeline,
        "bridge_health_command_pipeline_recompute_mismatch",
    )

    snapshots: Dict[str, Dict[str, Any]] = {}
    for name, label in (
        ("evidence-continuity.json", "evidence continuity"),
        ("action-outcomes.json", "action outcomes"),
        ("decision-frame.json", "decision frame"),
        ("strategy.json", "strategy"),
        ("advice.json", "advice"),
        ("incidents.json", "incidents"),
        ("plan.json", "plan"),
    ):
        snapshots[name] = _strict_object_snapshot(bridge_dir / name, label)

    continuity = snapshots["evidence-continuity.json"]["document"]
    require(
        continuity.get("schema") == "oni.evidence-continuity.v1",
        "evidence_continuity_schema_invalid",
    )
    continuity_projection = {
        "status": continuity.get("status"),
        "unresolved_command_count": continuity.get("unresolved_command_count"),
        "recovered_record_count": continuity.get("recovered_record_count"),
        "blockers": list(continuity.get("blockers") or []),
    }
    require(
        health.get("evidence_continuity") == continuity_projection,
        "bridge_health_evidence_continuity_mismatch",
    )

    outcomes = snapshots["action-outcomes.json"]["document"]
    try:
        validate_action_outcomes(outcomes)
    except ActionOutcomeError as exc:
        raise PriorityPairError("action_outcomes_invalid", str(exc)) from exc
    expected_outcome_source = {
        "session_id": raw_state.get("session_id"),
        "save_id_hash": raw_state.get("save_id_hash"),
        "world_id": raw_state.get("world_id"),
        "snapshot_id": raw_state.get("snapshot_id"),
        "seq": raw_state.get("seq"),
        "state_hash": canonical_state_hash(dict(raw_state)),
    }
    require(
        outcomes.get("source") == expected_outcome_source
        and outcomes.get("outcomes_hash") == health.get("action_outcomes_hash")
        and outcomes.get("counts") == health.get("action_outcomes")
        and outcomes.get("trust")
        == {"status": "untrusted", "blockers": [], "replay_mode": False},
        "bridge_health_action_outcomes_mismatch",
    )
    recomputed_outcomes = build_action_outcomes(
        bridge_dir,
        enriched_state,
        health,
        replay_mode=False,
    )
    require(
        outcomes.get("source") == recomputed_outcomes.get("source")
        and outcomes.get("trust") == recomputed_outcomes.get("trust")
        and outcomes.get("counts") == recomputed_outcomes.get("counts")
        and outcomes.get("outcomes") == recomputed_outcomes.get("outcomes"),
        "bridge_action_outcomes_recompute_mismatch",
    )

    frame = snapshots["decision-frame.json"]["document"]
    try:
        validate_decision_frame(frame)
    except DecisionFrameError as exc:
        raise PriorityPairError("decision_frame_invalid", str(exc)) from exc
    frame_readiness = frame.get("readiness") if isinstance(frame.get("readiness"), Mapping) else {}
    readiness_projection = {
        stage: bool(entry.get("ready"))
        for stage, entry in frame_readiness.items()
        if isinstance(entry, Mapping)
    }
    frame_source = frame.get("source") if isinstance(frame.get("source"), Mapping) else {}
    require(
        frame.get("frame_id") == health.get("decision_frame_id")
        and frame.get("trust", {}).get("status") == health.get("decision_frame_trust")
        and readiness_projection == health.get("decision_frame_readiness")
        and frame_source.get("session_id") == raw_state.get("session_id")
        and frame_source.get("snapshot_id") == raw_state.get("snapshot_id")
        and frame_source.get("seq") == raw_state.get("seq")
        and frame_source.get("snapshot_seq") == raw_state.get("snapshot_seq")
        and frame_source.get("save_id_hash") == raw_state.get("save_id_hash")
        and frame_source.get("world_id") == raw_state.get("world_id")
        and frame_source.get("state_hash") == canonical_state_hash(dict(raw_state)),
        "bridge_health_decision_frame_mismatch",
    )

    strategy = snapshots["strategy.json"]["document"]
    phase = strategy.get("phase") if isinstance(strategy.get("phase"), Mapping) else {}
    expected_source = {
        "session_id": raw_state.get("session_id"),
        "snapshot_id": raw_state.get("snapshot_id"),
        "state_hash": canonical_state_hash(dict(raw_state)),
    }
    require(
        strategy.get("schema") == "oni.strategy.v2"
        and strategy.get("source") == expected_source
        and strategy.get("status") == health.get("strategy_status")
        and phase.get("id") == health.get("strategy_phase")
        and phase.get("confidence") == health.get("strategy_confidence"),
        "bridge_health_strategy_mismatch",
    )
    policy_for_frame = copy.deepcopy(dict(policy))
    policy_for_frame["source"] = copy.deepcopy(expected_source)
    recomputed_frame = build_decision_frame(
        dict(enriched_state),
        dict(health),
        snapshots["advice.json"]["document"],
        snapshots["incidents.json"]["document"],
        snapshots["plan.json"]["document"],
        strategy,
        policy_for_frame,
    )
    require(
        _canonical_equal(frame, recomputed_frame),
        "bridge_decision_frame_recompute_mismatch",
    )
    evidence = {
        name: {
            "file_sha256": snapshot["file_sha256"],
            "document_hash": snapshot["document_hash"],
        }
        for name, snapshot in snapshots.items()
    }
    return evidence, snapshots


def _command_tree_snapshot(bridge_dir: pathlib.Path) -> Dict[str, Any]:
    root = bridge_dir / "commands"
    require(root.is_dir(), "command_tree_missing")
    entries: list[Dict[str, Any]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_dir():
            continue
        # The lifecycle lock is a persistent OS byte-range lock, not a JSON
        # command envelope.  It is deliberately excluded from the canonical
        # command-tree document hash; custody code validates its header and
        # identity separately.
        if path.name == ".mutation-lifecycle.lock":
            continue
        snapshot = _strict_object_snapshot(path, f"command tree:{_relative(path, bridge_dir)}")
        entries.append(
            {
                "path": _relative(path, bridge_dir),
                "file_sha256": snapshot["file_sha256"],
                "document_hash": snapshot["document_hash"],
                "file_state": list(snapshot["file_state"]),
            }
        )
    return {
        "entry_count": len(entries),
        "tree_hash": canonical_hash(entries),
        "entries": entries,
    }


def _lineage_freeze_path(manifest_path: pathlib.Path, step_id: str) -> pathlib.Path:
    require(STEP_PATTERN.fullmatch(step_id) is not None, "priority_step_id_invalid", step_id)
    return normalized_path(manifest_path).parent / "priority-command-freezes" / f"{step_id}.json"


def _relative(path: pathlib.Path, root: pathlib.Path) -> str:
    try:
        return normalized_path(path).relative_to(normalized_path(root)).as_posix()
    except ValueError as exc:
        raise PriorityPairError("evidence_path_outside_bridge", str(path)) from exc


def _queue_evidence(bridge_dir: pathlib.Path) -> Dict[str, Any]:
    root = bridge_dir / "commands"
    counts: Dict[str, int] = {}
    for name in NONTERMINAL_DIRECTORIES:
        directory = root / name
        entries = (
            [path for path in directory.iterdir() if not path.name.casefold().endswith(".tmp")]
            if directory.is_dir()
            else []
        )
        counts[name] = len(entries)
    temporary_paths = sorted(
        _relative(path, bridge_dir)
        for path in root.rglob("*")
        if path.name.casefold().endswith(".tmp")
    ) if root.is_dir() else []
    require(not any(counts.values()), "nonterminal_command_queue_not_empty", json.dumps(counts, sort_keys=True))
    require(not temporary_paths, "command_temporary_files_present", ",".join(temporary_paths))
    return {"nonterminal": counts, "temporary_file_count": 0}


def _command_pipeline_evidence(
    bridge_dir: pathlib.Path,
    latest_state: Mapping[str, Any],
    policy: Mapping[str, Any],
    *,
    raw_state: Mapping[str, Any],
) -> Dict[str, Any]:
    """Run the authoritative pipeline gate against the exact final inputs."""

    checked_at = _utc_now()
    latest_path = bridge_dir / "latest_state.json"
    policy_path = bridge_dir / "action-policy.json"
    health_path = bridge_dir / "health.json"
    latest_snapshot = _strict_object_snapshot(latest_path, "current latest state")
    policy_snapshot = _strict_object_snapshot(policy_path, "current action policy")
    health_snapshot = _strict_object_snapshot(health_path, "current bridge health")
    require(
        _canonical_equal(latest_snapshot["document"], latest_state),
        "latest_state_changed_before_pipeline_validation",
    )
    require(
        _canonical_equal(policy_snapshot["document"], policy),
        "action_policy_changed_before_pipeline_validation",
    )
    health = health_snapshot["document"]
    publication = validate_enriched_latest_against_raw(raw_state, latest_state, health)
    published_at = _aware_time(
        health.get("updated_at"),
        "bridge_health_timestamp_invalid",
    )
    require(
        publication.get("terminal") is True,
        "bridge_terminal_publication_required",
    )
    require(
        published_at <= checked_at,
        "bridge_health_publication_from_future",
    )
    require(
        health.get("actions_enabled") is bool(policy.get("actions_enabled"))
        and health.get("policy_error") == policy.get("policy_error"),
        "bridge_health_policy_mismatch",
    )
    terminal_archive, terminal_archive_snapshot = _terminal_archive_evidence(
        bridge_dir,
        raw_state,
    )
    terminal_shutdown_at = _aware_time(
        terminal_archive.get("probe_shutdown_at"),
        "terminal_shutdown_timestamp_invalid",
    )
    require(
        terminal_shutdown_at <= published_at,
        "terminal_archive_after_health_publication",
    )
    replay, replay_snapshots = _bounded_bridge_replay(
        bridge_dir,
        raw_state,
        health,
        published_at=published_at,
    )
    age, threshold, freshness_blockers = snapshot_freshness(
        dict(latest_state),
        now=checked_at,
    )
    structural_blockers = [
        blocker for blocker in freshness_blockers if blocker != "snapshot_stale"
    ]
    require(
        age is not None
        and threshold is not None
        and structural_blockers == []
        and age <= PAIR_CREATION_MAX_STATE_AGE_SECONDS,
        "command_pipeline_snapshot_not_fresh",
        json.dumps(
            {
                "age_seconds": age,
                "threshold_seconds": PAIR_CREATION_MAX_STATE_AGE_SECONDS,
                "blockers": freshness_blockers,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
    )
    sample = latest_state.get("sample")
    sample_interval = (
        sample.get("interval_seconds")
        if isinstance(sample, Mapping)
        else None
    )
    freshness = {
        "checked_at": checked_at.isoformat(timespec="microseconds"),
        "snapshot_ts": latest_state.get("ts"),
        "sample_interval_seconds": sample_interval,
        "age_seconds": age,
        "threshold_seconds": PAIR_CREATION_MAX_STATE_AGE_SECONDS,
        "blockers": structural_blockers,
    }

    command_tree_before = _command_tree_snapshot(bridge_dir)
    try:
        gate = command_pipeline_readiness(
            bridge_dir,
            latest_state=latest_state,
            policy=policy,
        )
    except (OSError, UnicodeError, TypeError, ValueError, OverflowError) as exc:
        raise PriorityPairError("command_pipeline_gate_unavailable", str(exc)) from exc
    require(isinstance(gate, Mapping), "command_pipeline_gate_invalid")
    require(gate.get("schema") == COMMAND_PIPELINE_SCHEMA, "command_pipeline_schema_invalid")
    proposal = (
        gate.get("readiness", {}).get("mutation_proposal")
        if isinstance(gate.get("readiness"), Mapping)
        else None
    )
    require(
        isinstance(proposal, Mapping)
        and proposal.get("ready") is True
        and proposal.get("blockers") == []
        and gate.get("ready") is True
        and gate.get("blockers") == [],
        "command_pipeline_mutation_not_ready",
        json.dumps(
            proposal.get("blockers") if isinstance(proposal, Mapping) else [],
            ensure_ascii=False,
            sort_keys=True,
        ),
    )
    for field, code in (
        ("ambiguous_outcomes", "command_pipeline_ambiguous_outcomes_present"),
        ("duplicate_ids", "command_pipeline_duplicate_ids_present"),
        ("stale_pending", "command_pipeline_stale_pending_present"),
        ("invalid_records", "command_pipeline_invalid_records_present"),
        ("in_flight_mutations", "command_pipeline_in_flight_mutations_present"),
        ("commands", "command_pipeline_relevant_commands_present"),
    ):
        require(gate.get(field) == [], code)
    counts = gate.get("counts")
    require(
        isinstance(counts, Mapping)
        and all(isinstance(value, int) and not isinstance(value, bool) and value == 0 for value in counts.values()),
        "command_pipeline_counts_not_empty",
    )
    companion_evidence, companion_snapshots = _bridge_companion_evidence(
        bridge_dir,
        raw_state,
        latest_state,
        health,
        gate,
        policy,
    )
    for name, snapshot in companion_snapshots.items():
        _confirm_object_snapshot(bridge_dir / name, name, snapshot)
    for path_text, snapshot in replay_snapshots.items():
        _confirm_binary_snapshot(
            pathlib.Path(path_text),
            f"Bridge state log:{pathlib.Path(path_text).name}",
            snapshot,
        )
    _confirm_binary_snapshot(
        terminal_archive_snapshot["path"],
        "terminal session archive",
        terminal_archive_snapshot,
    )
    _confirm_object_snapshot(latest_path, "current latest state", latest_snapshot)
    _confirm_object_snapshot(policy_path, "current action policy", policy_snapshot)
    _confirm_object_snapshot(health_path, "current bridge health", health_snapshot)
    command_tree_after = _command_tree_snapshot(bridge_dir)
    require(
        command_tree_after == command_tree_before,
        "command_tree_changed_during_validation",
    )
    try:
        gate_hash = canonical_hash(gate)
        policy_hash = canonical_hash(policy_snapshot["document"])
    except (CanonicalHashError, TypeError, ValueError, OverflowError) as exc:
        raise PriorityPairError("command_pipeline_hash_invalid", str(exc)) from exc
    return {
        "schema": gate.get("schema"),
        "gate_hash": gate_hash,
        "inputs": {
            "latest_state_file_sha256": latest_snapshot["file_sha256"],
            "latest_state_hash": publication["enriched_state_hash"],
            "latest_state_core_hash": publication["core_state_hash"],
            "health_file_sha256": health_snapshot["file_sha256"],
            "health_hash": health_snapshot["document_hash"],
            "health_updated_at": publication["health_updated_at"],
            "health_status": publication["health_status"],
            "terminal_publication": publication["terminal"],
            "bridge_projection": publication["bridge"],
            "startup_binding": copy.deepcopy(health["startup_binding"]),
            "policy_file_sha256": policy_snapshot["file_sha256"],
            "policy_hash": policy_hash,
            "terminal_archive": terminal_archive,
            "bridge_replay": replay,
            "companion_outputs": companion_evidence,
            "command_tree_hash": command_tree_before["tree_hash"],
            "command_tree_entry_count": command_tree_before["entry_count"],
            "snapshot_freshness": freshness,
        },
        "mutation_proposal": copy.deepcopy(dict(proposal)),
        "counts": copy.deepcopy(dict(counts)),
        "command_count": 0,
        "ambiguous_outcome_count": 0,
        "duplicate_id_count": 0,
        "stale_pending_count": 0,
        "invalid_record_count": 0,
    }


def _command_locations(bridge_dir: pathlib.Path) -> Dict[str, list[Tuple[str, pathlib.Path, Dict[str, Any]]]]:
    grouped: Dict[str, list[Tuple[str, pathlib.Path, Dict[str, Any]]]] = {}
    for directory_name in COMMAND_DIRECTORIES:
        directory = bridge_dir / "commands" / directory_name
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.json")):
            command = _strict_object(path, f"command:{directory_name}/{path.name}")
            command_id = command.get("id")
            require(isinstance(command_id, str) and bool(command_id), "command_id_missing", path.name)
            grouped.setdefault(command_id, []).append((directory_name, path, command))
    duplicates = sorted(command_id for command_id, items in grouped.items() if len(items) != 1)
    require(not duplicates, "duplicate_command_id", ",".join(duplicates))
    return grouped


def _checkpoint_policy_gate(checkpoint: Mapping[str, Any], step_id: str) -> None:
    gate = checkpoint.get("policy_gate") if isinstance(checkpoint.get("policy_gate"), Mapping) else {}
    expected_zero = (
        "allowed_save_hash_count",
        "pending_count",
        "approving_count",
        "approved_count",
        "executing_count",
        "confirming_count",
        "temporary_count",
    )
    require(gate.get("actions_enabled") is False, "checkpoint_policy_not_disabled", step_id)
    require(
        all(gate.get(field) == 0 for field in expected_zero),
        "checkpoint_queue_gate_incomplete",
        f"{step_id}:{json.dumps(dict(gate), sort_keys=True)}",
    )


def _priority_target(args: Mapping[str, Any], label: str) -> Tuple[int, str, str, int, int]:
    expected_keys = {
        "duplicant_instance_id",
        "expected_name",
        "chore_group",
        "from_priority",
        "priority",
    }
    require(set(args) == expected_keys, "priority_args_invalid", label)
    instance_id = args.get("duplicant_instance_id")
    expected_name = args.get("expected_name")
    chore_group = args.get("chore_group")
    from_priority = args.get("from_priority")
    priority = args.get("priority")
    require(
        isinstance(instance_id, int) and not isinstance(instance_id, bool) and instance_id > 0,
        "priority_instance_invalid",
        label,
    )
    require(isinstance(expected_name, str) and 0 < len(expected_name) <= 128, "priority_name_invalid", label)
    require(
        isinstance(chore_group, str) and GROUP_PATTERN.fullmatch(chore_group) is not None,
        "priority_group_invalid",
        label,
    )
    for value, field in ((from_priority, "from_priority"), (priority, "priority")):
        require(
            isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 5,
            "priority_value_invalid",
            f"{label}:{field}",
        )
    require(from_priority != priority, "priority_noop_not_allowed", label)
    return int(instance_id), expected_name, chore_group, int(from_priority), int(priority)


def validate_priority_inverse(
    first_target: Tuple[int, str, str, int, int],
    inverse_target: Tuple[int, str, str, int, int],
) -> None:
    """Validate exact target identity and a mathematical priority inverse."""

    require(first_target[:3] == inverse_target[:3], "priority_targets_differ")
    require(
        inverse_target[3] == first_target[4] and inverse_target[4] == first_target[3],
        "priority_commands_not_inverse",
    )


def _receipt_and_frame(
    bridge_dir: pathlib.Path,
    command: Mapping[str, Any],
    checkpoint: Mapping[str, Any],
    label: str,
    *,
    frozen_frame: Optional[Mapping[str, Any]] = None,
    frozen_frame_file_sha256: Optional[str] = None,
) -> Dict[str, Any]:
    receipt = command.get("approval_receipt")
    require(isinstance(receipt, Mapping), "approval_receipt_missing", label)
    assert isinstance(receipt, Mapping)
    require(set(receipt) == RECEIPT_KEYS, "approval_receipt_shape_invalid", label)
    require(receipt.get("schema") == APPROVAL_RECEIPT_SCHEMA, "approval_receipt_schema_invalid", label)
    require(receipt.get("command_id") == command.get("id"), "approval_receipt_command_mismatch", label)
    try:
        intent_hash = command_intent_hash(command)
    except (CanonicalHashError, TypeError, ValueError, OverflowError) as exc:
        raise PriorityPairError("command_intent_invalid", label) from exc
    require(receipt.get("command_intent_hash") == intent_hash, "approval_receipt_intent_mismatch", label)
    frame_id = command.get("source_frame_id")
    require(
        isinstance(frame_id, str) and HASH_PATTERN.fullmatch(frame_id) is not None,
        "source_frame_id_invalid",
        label,
    )
    require(receipt.get("source_frame_id") == frame_id, "approval_receipt_frame_mismatch", label)
    expected_source = {
        "session_id": command.get("session_id"),
        "save_id_hash": command.get("save_id_hash"),
        "world_id": command.get("world_id"),
        "snapshot_id": command.get("source_snapshot_id"),
        "state_hash": command.get("state_hash"),
    }
    receipt_source = receipt.get("source")
    require(
        isinstance(receipt_source, Mapping)
        and set(receipt_source) == RECEIPT_SOURCE_KEYS
        and dict(receipt_source) == expected_source,
        "approval_receipt_source_mismatch",
        label,
    )
    command_source = command.get("source") if isinstance(command.get("source"), Mapping) else {}
    policy_hash = receipt.get("policy_hash")
    require(
        isinstance(policy_hash, str)
        and HASH_PATTERN.fullmatch(policy_hash) is not None
        and command_source.get("policy_hash") == policy_hash,
        "source_policy_binding_invalid",
        label,
    )
    require(
        receipt.get("approved_by") == "local_human"
        and command.get("approved_by") == "local_human"
        and receipt.get("approved_at") == command.get("approved_at"),
        "approval_audit_invalid",
        label,
    )
    _aware_time(receipt.get("approved_at"), "approval_timestamp_invalid")
    receipt_hash = receipt.get("receipt_hash")
    require(
        isinstance(receipt_hash, str)
        and HASH_PATTERN.fullmatch(receipt_hash) is not None
        and receipt_hash == _hash_document(receipt, "receipt_hash"),
        "approval_receipt_hash_mismatch",
        label,
    )

    frame_path = decision_frame_archive_path(bridge_dir, frame_id)
    if frozen_frame is None:
        try:
            frame, frame_path, _archive_hash = load_archived_decision_frame(bridge_dir, frame_id)
        except DecisionFrameArchiveError as exc:
            raise PriorityPairError("source_frame_archive_invalid", f"{label}:{exc}") from exc
        frame_file_sha256 = sha256_file(frame_path)
    else:
        frame = dict(frozen_frame)
        try:
            validate_decision_frame(frame)
        except (DecisionFrameError, TypeError, ValueError, OverflowError) as exc:
            raise PriorityPairError("frozen_source_frame_invalid", f"{label}:{exc}") from exc
        require(frame.get("frame_id") == frame_id, "frozen_source_frame_id_mismatch", label)
        require(
            isinstance(frozen_frame_file_sha256, str)
            and HASH_PATTERN.fullmatch(frozen_frame_file_sha256) is not None,
            "frozen_source_frame_file_hash_invalid",
            label,
        )
        frame_file_sha256 = frozen_frame_file_sha256
        # Archive pruning is allowed after the pre-done bundle exists.  When
        # the original is still present it must remain byte/hash equivalent.
        if frame_path.exists() or frame_path.is_symlink():
            try:
                archived, loaded_path, _archive_hash = load_archived_decision_frame(bridge_dir, frame_id)
            except DecisionFrameArchiveError as exc:
                raise PriorityPairError("source_frame_archive_conflicts_with_freeze", f"{label}:{exc}") from exc
            require(
                _canonical_equal(archived, frame)
                and sha256_file(loaded_path) == frozen_frame_file_sha256,
                "source_frame_archive_conflicts_with_freeze",
                label,
            )
    frame_source = frame.get("source") if isinstance(frame.get("source"), Mapping) else {}
    require(
        all(frame_source.get(field) == expected_source[field] for field in expected_source),
        "source_frame_identity_mismatch",
        label,
    )
    source_seq = parse_positive_sequence(frame_source.get("seq"))
    declared_seq = parse_positive_sequence(command_source.get("state_seq"))
    require(source_seq is not None and declared_seq == source_seq, "source_frame_sequence_mismatch", label)
    require(frame_source.get("ts") == command_source.get("state_ts"), "source_frame_timestamp_mismatch", label)
    require(
        isinstance(frame.get("trust"), Mapping) and frame["trust"].get("status") == "trusted",
        "source_frame_untrusted",
        label,
    )
    readiness = frame.get("readiness") if isinstance(frame.get("readiness"), Mapping) else {}
    propose = readiness.get("propose") if isinstance(readiness.get("propose"), Mapping) else {}
    mode = command.get("mode")
    allowed_propose = {"ready": True, "blockers": []}
    operator_blocked = {"ready": False, "blockers": ["SUGGESTIONS_DISABLED"]}
    require(
        dict(propose) == allowed_propose
        or (mode in {"runtime_validation", "planned"} and dict(propose) == operator_blocked),
        "source_frame_not_authorizing",
        label,
    )
    projections = frame.get("projections") if isinstance(frame.get("projections"), Mapping) else {}
    policy_projection = projections.get("policy") if isinstance(projections.get("policy"), Mapping) else {}
    require(
        policy_projection.get("valid") is True
        and policy_projection.get("policy_hash") == policy_hash
        and policy_projection.get("actions_enabled") is True
        and isinstance(policy_projection.get("allowed_actions_count"), int)
        and policy_projection.get("allowed_actions_count") > 0
        and isinstance(policy_projection.get("allowed_save_hash_count"), int)
        and policy_projection.get("allowed_save_hash_count") > 0,
        "source_frame_policy_projection_invalid",
        label,
    )

    before = checkpoint.get("before") if isinstance(checkpoint.get("before"), Mapping) else {}
    require(
        frame_source.get("session_id") == before.get("session_id")
        and frame_source.get("save_id_hash") == before.get("save_id_hash")
        and frame_source.get("world_id") == before.get("world_id"),
        "source_frame_checkpoint_identity_mismatch",
        label,
    )
    before_seq = parse_positive_sequence(before.get("seq"))
    require(before_seq is not None and source_seq >= before_seq, "source_frame_precedes_checkpoint_before", label)
    if source_seq == before_seq:
        require(
            frame_source.get("snapshot_id") == before.get("snapshot_id"),
            "source_frame_same_sequence_wrong_snapshot",
            label,
        )
    require(
        _aware_time(frame_source.get("ts"), "source_frame_timestamp_invalid")
        >= _aware_time(before.get("snapshot_ts"), "checkpoint_before_timestamp_invalid"),
        "source_frame_precedes_checkpoint_before",
        label,
    )
    return {
        "intent_hash": intent_hash,
        "receipt_hash": receipt_hash,
        "policy_hash": policy_hash,
        "frame_id": frame_id,
        "frame_path": frame_path,
        "frame_file_sha256": frame_file_sha256,
        "frame_source": dict(frame_source),
    }


def _find_ack(
    records: Sequence[Mapping[str, Any]],
    command: Mapping[str, Any],
    stage: str,
    expected_seq: int,
    label: str,
) -> Mapping[str, Any]:
    matches = [
        record
        for record in records
        if record.get("event") == "command_ack"
        and record.get("id") == command.get("id")
        and record.get("session_id") == command.get("session_id")
        and record.get("attempt_id") == command.get("attempt_id")
        and record.get("status") == stage
        and record.get("seq") == expected_seq
    ]
    require(len(matches) == 1, "command_ack_missing_or_ambiguous", f"{label}:{stage}")
    return matches[0]


def freeze_confirmed_command(
    manifest_path: pathlib.Path,
    bridge_dir: pathlib.Path,
    *,
    step_id: str,
    command_id: str,
) -> Dict[str, Any]:
    """Bind durable confirmation evidence to the current lineage tip.

    The source frame is already protected by the automatic pre-done approval
    bundle.  This second, manifest-local create-only freeze adds the session
    certificate and the exact ACK/witness rows needed by the pair finalizer.
    It must run before the explicit checkpoint save/reload, so the stored
    manifest tip becomes that checkpoint's exact ``before`` identity.
    """

    manifest_path = normalized_path(manifest_path)
    bridge_dir = normalized_path(bridge_dir)
    require(STEP_PATTERN.fullmatch(step_id) is not None, "priority_step_id_invalid", step_id)
    manifest = _strict_object(manifest_path, "lineage manifest")
    require(manifest.get("schema") == LINEAGE_SCHEMA, "lineage_schema_invalid")
    require(manifest.get("status") == "active", "lineage_not_active")
    checkpoints = manifest.get("checkpoints") if isinstance(manifest.get("checkpoints"), list) else []
    require(
        not any(isinstance(item, Mapping) and item.get("step_id") == step_id for item in checkpoints),
        "priority_step_already_checkpointed",
        step_id,
    )
    before = manifest.get("latest") if isinstance(manifest.get("latest"), Mapping) else {}
    require(bool(before), "lineage_tip_missing")

    locations = _command_locations(bridge_dir)
    matches = list(locations.get(command_id) or [])
    require(len(matches) == 1, "checkpoint_command_missing_or_ambiguous", command_id)
    directory, command_path, command = matches[0]
    require(
        directory == "done"
        and command.get("status") == "confirmed"
        and command.get("action") == ACTION,
        "command_not_done_confirmed",
        command_id,
    )
    require(
        command.get("session_id") == before.get("session_id")
        and command.get("save_id_hash") == before.get("save_id_hash")
        and command.get("world_id") == before.get("world_id"),
        "command_lineage_tip_identity_mismatch",
        command_id,
    )
    _priority_target(command.get("args") if isinstance(command.get("args"), Mapping) else {}, step_id)

    try:
        approval_bundle, approval_bundle_path = load_confirmed_command_approval_bundle(
            bridge_dir,
            command_id,
        )
    except Exception as exc:
        raise PriorityPairError("automatic_approval_bundle_invalid", f"{command_id}:{exc}") from exc
    require(
        _canonical_equal(approval_bundle.get("final_command"), command),
        "automatic_approval_bundle_command_mismatch",
        command_id,
    )

    records = archived_confirmation_records(bridge_dir, command)
    require(bool(records), "archived_confirmation_records_missing", step_id)
    assessment = assess_command_confirmation(bridge_dir, command, records, directory="done")
    require(
        assessment.get("accepted") is True
        and assessment.get("mode") == CONFIRMED_MUTATION_MODE
        and assessment.get("blockers") == [],
        "command_confirmation_invalid",
        f"{step_id}:{','.join(str(item) for item in assessment.get('blockers') or [])}",
    )
    publications = assessment.get("certificate_publications")
    session_publications = [
        item
        for item in publications or []
        if isinstance(item, Mapping) and str(item.get("path") or "").startswith("sessions/")
    ]
    require(len(session_publications) == 1, "session_confirmation_certificate_ambiguous", step_id)
    session_publication = session_publications[0]
    certificate_path = bridge_dir / str(session_publication.get("path") or "")
    certificate = _strict_object(certificate_path, f"confirmation certificate:{step_id}")
    require(
        sha256_file(certificate_path) == session_publication.get("file_sha256"),
        "confirmation_publication_hash_mismatch",
        step_id,
    )
    _queue_evidence(bridge_dir)

    freeze: Dict[str, Any] = {
        "schema": COMMAND_FREEZE_SCHEMA,
        "step_id": step_id,
        "test_run_id": manifest.get("test_run_id"),
        "manifest_binding": {
            "latest_checkpoint_hash": manifest.get("latest_checkpoint_hash"),
            "before": copy.deepcopy(dict(before)),
        },
        "approval_bundle": {
            "path": _relative(approval_bundle_path, bridge_dir),
            "file_sha256": sha256_file(approval_bundle_path),
            "bundle_hash": approval_bundle.get("bundle_hash"),
            "document": copy.deepcopy(approval_bundle),
        },
        "confirmation_certificate": {
            "path": _relative(certificate_path, bridge_dir),
            "file_sha256": sha256_file(certificate_path),
            "outcomes_hash": certificate.get("outcomes_hash"),
            "document": copy.deepcopy(certificate),
        },
        "confirmation_records": [copy.deepcopy(dict(record)) for record in records],
        "frozen_at": dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds"),
        "freeze_hash": "",
    }
    freeze["freeze_hash"] = _hash_document(freeze, "freeze_hash")
    target = _lineage_freeze_path(manifest_path, step_id)
    write_report_create_only(target, freeze)
    stored = _strict_object(target, f"priority command freeze:{step_id}")
    require(_canonical_equal(stored, freeze), "priority_command_freeze_conflict", step_id)
    return {
        "status": "frozen",
        "schema": COMMAND_FREEZE_SCHEMA,
        "step_id": step_id,
        "command_id": command_id,
        "path": str(target),
        "file_sha256": sha256_file(target),
        "freeze_hash": freeze["freeze_hash"],
        "approval_bundle_hash": approval_bundle.get("bundle_hash"),
    }


def _load_command_freeze(
    manifest_path: pathlib.Path,
    bridge_dir: pathlib.Path,
    checkpoint: Mapping[str, Any],
    step_id: str,
) -> Dict[str, Any]:
    path = _lineage_freeze_path(manifest_path, step_id)
    freeze = _strict_object(path, f"priority command freeze:{step_id}")
    require(set(freeze) == COMMAND_FREEZE_KEYS, "priority_command_freeze_shape_invalid", step_id)
    require(
        freeze.get("schema") == COMMAND_FREEZE_SCHEMA
        and freeze.get("step_id") == step_id
        and freeze.get("freeze_hash") == _hash_document(freeze, "freeze_hash"),
        "priority_command_freeze_hash_invalid",
        step_id,
    )
    binding = freeze.get("manifest_binding") if isinstance(freeze.get("manifest_binding"), Mapping) else {}
    require(
        binding.get("latest_checkpoint_hash") == checkpoint.get("previous_checkpoint_hash")
        and _canonical_equal(binding.get("before"), checkpoint.get("before")),
        "priority_command_freeze_manifest_binding_invalid",
        step_id,
    )

    frozen_bundle = freeze.get("approval_bundle") if isinstance(freeze.get("approval_bundle"), Mapping) else {}
    bundle = frozen_bundle.get("document")
    require(isinstance(bundle, Mapping), "automatic_approval_bundle_missing", step_id)
    try:
        validate_confirmed_command_approval_bundle(bundle)
    except Exception as exc:
        raise PriorityPairError("automatic_approval_bundle_invalid", f"{step_id}:{exc}") from exc
    command_id = bundle.get("command_id")
    expected_bundle_path = confirmed_command_approval_bundle_path(bridge_dir, command_id)
    require(
        frozen_bundle.get("path") == _relative(expected_bundle_path, bridge_dir)
        and frozen_bundle.get("bundle_hash") == bundle.get("bundle_hash")
        and expected_bundle_path.is_file()
        and sha256_file(expected_bundle_path) == frozen_bundle.get("file_sha256"),
        "automatic_approval_bundle_file_mismatch",
        step_id,
    )
    current_bundle = _strict_object(expected_bundle_path, f"automatic approval bundle:{step_id}")
    require(_canonical_equal(current_bundle, bundle), "automatic_approval_bundle_drift", step_id)

    certificate = (
        freeze.get("confirmation_certificate")
        if isinstance(freeze.get("confirmation_certificate"), Mapping)
        else {}
    )
    certificate_document = certificate.get("document")
    require(isinstance(certificate_document, Mapping), "frozen_confirmation_certificate_missing", step_id)
    certificate_path = bridge_dir / str(certificate.get("path") or "")
    require(path_within(certificate_path, bridge_dir), "frozen_confirmation_certificate_path_invalid", step_id)
    if certificate_path.exists() or certificate_path.is_symlink():
        require(
            certificate_path.is_file()
            and sha256_file(certificate_path) == certificate.get("file_sha256")
            and _canonical_equal(
                _strict_object(certificate_path, f"current confirmation certificate:{step_id}"),
                certificate_document,
            ),
            "frozen_confirmation_certificate_drift",
            step_id,
        )
    records = freeze.get("confirmation_records")
    require(
        isinstance(records, list) and records and all(isinstance(item, Mapping) for item in records),
        "frozen_confirmation_records_invalid",
        step_id,
    )

    freeze_file_hash = sha256_file(path)
    artifacts = checkpoint.get("artifacts") if isinstance(checkpoint.get("artifacts"), list) else []
    matching_artifacts = [
        item
        for item in artifacts
        if isinstance(item, Mapping) and item.get("sha256") == freeze_file_hash
    ]
    require(len(matching_artifacts) == 1, "checkpoint_does_not_freeze_priority_bundle", step_id)
    return {
        "path": path,
        "file_sha256": freeze_file_hash,
        "document": freeze,
        "approval_bundle": dict(bundle),
        "certificate": dict(certificate_document),
        "records": [dict(item) for item in records],
    }


def _assess_frozen_confirmation(
    command: Mapping[str, Any],
    certificate: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Run the production confirmation assessor against only frozen evidence."""

    with tempfile.TemporaryDirectory(prefix="oni-priority-frozen-confirmation-") as temporary:
        root = pathlib.Path(temporary)
        done_path = root / "commands" / "done" / f"{command.get('id')}.json"
        done_path.parent.mkdir(parents=True, exist_ok=True)
        done_path.write_text(
            json.dumps(command, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        session_root = root / "sessions" / "frozen"
        session_root.mkdir(parents=True, exist_ok=True)
        (session_root / "action-outcomes.json").write_text(
            json.dumps(certificate, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        (session_root / "state.jsonl").write_text(
            "".join(
                json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
                + "\n"
                for record in records
            ),
            encoding="utf-8",
        )
        archived = archived_confirmation_records(root, command)
        return assess_command_confirmation(root, command, archived, directory="done")


def _command_evidence(
    manifest_path: pathlib.Path,
    bridge_dir: pathlib.Path,
    checkpoint: Mapping[str, Any],
    step_id: str,
    locations: Mapping[str, Sequence[Tuple[str, pathlib.Path, Dict[str, Any]]]],
) -> Dict[str, Any]:
    require(checkpoint.get("outcome") == "confirmed", "checkpoint_not_confirmed", step_id)
    require(checkpoint.get("expected_mutation") == "persisted_change", "checkpoint_mutation_contract_invalid", step_id)
    _checkpoint_policy_gate(checkpoint, step_id)
    recorded = checkpoint.get("command") if isinstance(checkpoint.get("command"), Mapping) else None
    require(recorded is not None, "checkpoint_command_missing", step_id)
    assert recorded is not None
    command_id = recorded.get("id")
    require(isinstance(command_id, str) and bool(command_id), "checkpoint_command_id_invalid", step_id)
    matches = list(locations.get(command_id) or [])
    require(len(matches) == 1, "checkpoint_command_missing_or_ambiguous", step_id)
    directory, command_path, command = matches[0]
    require(directory == "done" and command.get("status") == "confirmed", "command_not_done_confirmed", step_id)
    require(command.get("schema") == "oni.command.v3", "command_schema_invalid", step_id)
    require(command.get("action") == ACTION, "command_action_invalid", step_id)
    require(
        recorded.get("directory") == "done"
        and recorded.get("status") == "confirmed"
        and recorded.get("action") == ACTION,
        "checkpoint_command_summary_invalid",
        step_id,
    )
    relative_path = _relative(command_path, bridge_dir)
    require(relative_path == recorded.get("file"), "checkpoint_command_path_mismatch", step_id)
    require(sha256_file(command_path) == recorded.get("file_sha256"), "checkpoint_command_hash_mismatch", step_id)
    require(command_path.parent == bridge_dir / "commands" / "done", "command_path_not_done", step_id)

    frozen = _load_command_freeze(manifest_path, bridge_dir, checkpoint, step_id)
    frozen_command = frozen["approval_bundle"].get("final_command")
    require(
        isinstance(frozen_command, Mapping) and _canonical_equal(frozen_command, command),
        "frozen_command_drift",
        step_id,
    )

    before = checkpoint.get("before") if isinstance(checkpoint.get("before"), Mapping) else {}
    require(
        command.get("session_id") == before.get("session_id")
        and command.get("save_id_hash") == before.get("save_id_hash")
        and command.get("world_id") == before.get("world_id"),
        "command_checkpoint_identity_mismatch",
        step_id,
    )
    target = _priority_target(command.get("args") if isinstance(command.get("args"), Mapping) else {}, step_id)
    frozen_source_frame = (
        frozen["approval_bundle"].get("source_frame")
        if isinstance(frozen["approval_bundle"].get("source_frame"), Mapping)
        else {}
    )
    frame = _receipt_and_frame(
        bridge_dir,
        command,
        checkpoint,
        step_id,
        frozen_frame=(
            frozen_source_frame.get("document")
            if isinstance(frozen_source_frame.get("document"), Mapping)
            else None
        ),
        frozen_frame_file_sha256=frozen_source_frame.get("archive_file_sha256"),
    )

    records = frozen["records"]
    require(bool(records), "archived_confirmation_records_missing", step_id)
    assessment = _assess_frozen_confirmation(
        command,
        frozen["certificate"],
        records,
    )
    require(
        assessment.get("accepted") is True
        and assessment.get("mode") == CONFIRMED_MUTATION_MODE
        and assessment.get("blockers") == [],
        "command_confirmation_invalid",
        f"{step_id}:{','.join(str(item) for item in assessment.get('blockers') or [])}",
    )
    recorded_confirmation = recorded.get("confirmation")
    current_summary = confirmation_summary(assessment)
    require(isinstance(recorded_confirmation, Mapping), "checkpoint_confirmation_missing", step_id)
    for key in ("accepted", "mode", "attestation_hash", "witness", "ack_sequences"):
        require(
            recorded_confirmation.get(key) == current_summary.get(key),
            "checkpoint_confirmation_drift",
            f"{step_id}:{key}",
        )
    ack_sequences = assessment.get("ack_sequences") if isinstance(assessment.get("ack_sequences"), Mapping) else {}
    source_seq = parse_positive_sequence(frame["frame_source"].get("seq"))
    accepted_seq = parse_positive_sequence(ack_sequences.get("accepted"))
    executed_seq = parse_positive_sequence(ack_sequences.get("executed"))
    local_seq = parse_positive_sequence(ack_sequences.get("local_effect_observed"))
    witness = assessment.get("witness") if isinstance(assessment.get("witness"), Mapping) else {}
    witness_seq = parse_positive_sequence(witness.get("seq"))
    require(
        None not in (source_seq, accepted_seq, executed_seq, local_seq, witness_seq)
        and int(source_seq or 0) < int(accepted_seq or 0) < int(executed_seq or 0)
        < int(local_seq or 0) < int(witness_seq or 0),
        "command_evidence_order_invalid",
        step_id,
    )
    accepted_ack = _find_ack(records, command, "accepted", int(accepted_seq or 0), step_id)
    _find_ack(records, command, "executed", int(executed_seq or 0), step_id)
    _find_ack(records, command, "local_effect_observed", int(local_seq or 0), step_id)

    witness_state = assessment.get("witness_state")
    require(isinstance(witness_state, Mapping), "trusted_witness_state_missing", step_id)
    priority_path = f"duplicants.by_instance_id.id_{target[0]}.chore_priorities.{target[2]}"
    witness_read = read_contract_field(witness_state, priority_path, DUPLICANT_CHORE_PRIORITY_CONTRACT_ID)
    require(
        witness_read.usable and witness_read.value == target[4],
        "trusted_witness_priority_mismatch",
        f"{step_id}:{witness_read.blockers}",
    )

    save = checkpoint.get("save") if isinstance(checkpoint.get("save"), Mapping) else {}
    reload_state = checkpoint.get("reload") if isinstance(checkpoint.get("reload"), Mapping) else {}
    before_seq = parse_positive_sequence(before.get("seq"))
    save_seq = parse_positive_sequence(save.get("seq"))
    require(
        save.get("session_id") == before.get("session_id")
        and before_seq is not None
        and save_seq is not None
        and save_seq > int(witness_seq or 0),
        "checkpoint_save_not_after_witness",
        step_id,
    )
    witness_at = _aware_time(witness.get("ts"), "witness_timestamp_invalid")
    save_at = _aware_time(save.get("ts"), "checkpoint_save_timestamp_invalid")
    reload_at = _aware_time(reload_state.get("snapshot_ts"), "checkpoint_reload_timestamp_invalid")
    require(witness_at < save_at <= reload_at, "checkpoint_temporal_order_invalid", step_id)

    return {
        "step_id": step_id,
        "checkpoint_index": checkpoint.get("index"),
        "checkpoint_hash": checkpoint.get("checkpoint_hash"),
        "command": command,
        "command_path": command_path,
        "command_file_sha256": sha256_file(command_path),
        "target": target,
        "frame": frame,
        "assessment": assessment,
        "records": records,
        "accepted_ack": dict(accepted_ack),
        "witness_at": witness_at,
        "save_at": save_at,
        "reload_at": reload_at,
        "priority_path": priority_path,
        "freeze": frozen,
    }


def _iter_log_records(bridge_dir: pathlib.Path) -> Iterable[Dict[str, Any]]:
    paths = list(state_log_paths(bridge_dir))
    sessions = bridge_dir / "sessions"
    if sessions.is_dir():
        paths.extend(sorted(sessions.glob("*/state.jsonl")))
    for path in paths:
        if not path.is_file():
            continue
        try:
            with path.open("rb") as handle:
                for raw in handle:
                    if not raw.endswith(b"\n"):
                        continue
                    try:
                        value = loads_strict(raw.decode("utf-8-sig", errors="strict"))
                    except (UnicodeError, CanonicalHashError, TypeError, ValueError):
                        continue
                    if isinstance(value, dict):
                        yield dict(value)
        except OSError as exc:
            raise PriorityPairError("state_log_unreadable", str(path)) from exc


def _unique_snapshot(bridge_dir: pathlib.Path, identity: Mapping[str, Any]) -> Dict[str, Any]:
    matches = [
        record
        for record in _iter_log_records(bridge_dir)
        if record.get("event") == "state_snapshot"
        and record.get("session_id") == identity.get("session_id")
        and record.get("snapshot_id") == identity.get("snapshot_id")
        and record.get("seq") == identity.get("seq")
    ]
    unique: Dict[str, Dict[str, Any]] = {}
    for record in matches:
        try:
            # ``canonical_state_hash`` intentionally excludes writer/timing
            # transport fields.  Archive uniqueness is stricter: only the
            # untrusted raw ``bridge`` self-declaration may be ignored.
            digest = canonical_hash(snapshot_core_without_bridge(record))
        except (CanonicalHashError, TypeError, ValueError, OverflowError) as exc:
            raise PriorityPairError("tip_snapshot_hash_invalid") from exc
        unique.setdefault(digest, record)
    require(len(unique) == 1, "tip_snapshot_missing_or_ambiguous", str(identity.get("snapshot_id")))
    return next(iter(unique.values()))


def _checkpoint_chain_guard(manifest: Mapping[str, Any]) -> None:
    initial = manifest.get("initial") if isinstance(manifest.get("initial"), Mapping) else {}
    previous_hash = initial.get("checkpoint_hash")
    previous_reload = initial.get("reload") if isinstance(initial.get("reload"), Mapping) else {}
    for expected_index, checkpoint in enumerate(manifest.get("checkpoints") or [], start=1):
        require(isinstance(checkpoint, Mapping), "checkpoint_not_object", str(expected_index))
        assert isinstance(checkpoint, Mapping)
        require(checkpoint.get("index") == expected_index, "checkpoint_index_invalid", str(expected_index))
        require(checkpoint.get("previous_checkpoint_hash") == previous_hash, "checkpoint_hash_chain_broken", str(expected_index))
        require(checkpoint.get("checkpoint_hash") == checkpoint_hash(dict(checkpoint)), "checkpoint_hash_invalid", str(expected_index))
        require(checkpoint.get("before") == previous_reload, "checkpoint_before_chain_broken", str(expected_index))
        save = checkpoint.get("save") if isinstance(checkpoint.get("save"), Mapping) else {}
        reload_state = checkpoint.get("reload") if isinstance(checkpoint.get("reload"), Mapping) else {}
        require(
            save.get("session_id") == previous_reload.get("session_id")
            and save.get("before_save_id_hash") == previous_reload.get("save_id_hash")
            and save.get("active_save_id_hash") == reload_state.get("save_id_hash")
            and reload_state.get("session_id") != previous_reload.get("session_id"),
            "checkpoint_save_reload_chain_invalid",
            str(expected_index),
        )
        previous_hash = checkpoint.get("checkpoint_hash")
        previous_reload = reload_state
    require(
        manifest.get("latest_checkpoint_hash") == previous_hash and manifest.get("latest") == previous_reload,
        "manifest_latest_pointer_invalid",
    )


def verify_priority_pair(
    manifest_path: pathlib.Path,
    bridge_dir: pathlib.Path,
    *,
    forward_step: str = DEFAULT_FORWARD_STEP,
    inverse_step: str = DEFAULT_INVERSE_STEP,
) -> Dict[str, Any]:
    """Verify and freeze one G0 forward/inverse pair without reading live game state."""

    manifest_path = normalized_path(manifest_path)
    bridge_dir = normalized_path(bridge_dir)
    manifest_snapshot = _strict_object_snapshot(manifest_path, "lineage manifest")
    manifest = manifest_snapshot["document"]
    require(manifest.get("schema") == LINEAGE_SCHEMA, "lineage_schema_invalid")
    require(manifest.get("status") == "active", "lineage_not_active")
    require(forward_step != inverse_step, "priority_step_ids_not_distinct")

    generic = verify_lineage(
        manifest_path,
        bridge_dir,
        expected_mod_version=str(manifest.get("expected_mod_version") or ""),
        required_steps=(forward_step, inverse_step),
        minimum_checkpoints=2,
        verify_live=False,
    )
    require(generic.get("status") == "passed", "base_lineage_verification_failed")
    _checkpoint_chain_guard(manifest)

    checkpoints = manifest.get("checkpoints") if isinstance(manifest.get("checkpoints"), list) else []
    forward_matches = [item for item in checkpoints if isinstance(item, Mapping) and item.get("step_id") == forward_step]
    inverse_matches = [item for item in checkpoints if isinstance(item, Mapping) and item.get("step_id") == inverse_step]
    require(len(forward_matches) == 1, "forward_checkpoint_missing_or_ambiguous")
    require(len(inverse_matches) == 1, "inverse_checkpoint_missing_or_ambiguous")
    forward_checkpoint = forward_matches[0]
    inverse_checkpoint = inverse_matches[0]
    forward_index = forward_checkpoint.get("index")
    inverse_index = inverse_checkpoint.get("index")
    require(
        isinstance(forward_index, int)
        and not isinstance(forward_index, bool)
        and isinstance(inverse_index, int)
        and not isinstance(inverse_index, bool)
        and forward_index < inverse_index,
        "priority_checkpoint_order_invalid",
    )

    locations = _command_locations(bridge_dir)
    forward = _command_evidence(manifest_path, bridge_dir, forward_checkpoint, forward_step, locations)
    inverse = _command_evidence(manifest_path, bridge_dir, inverse_checkpoint, inverse_step, locations)
    require(
        forward["command"].get("id") != inverse["command"].get("id"),
        "priority_command_ids_not_distinct",
    )
    require(forward["frame"]["frame_id"] != inverse["frame"]["frame_id"], "priority_source_frames_not_distinct")

    first_target = forward["target"]
    inverse_target = inverse["target"]
    validate_priority_inverse(first_target, inverse_target)

    inverse_source_at = _aware_time(
        inverse["frame"]["frame_source"].get("ts"),
        "inverse_source_timestamp_invalid",
    )
    inverse_accepted_at = _aware_time(
        inverse["accepted_ack"].get("ts"),
        "inverse_accepted_timestamp_invalid",
    )
    require(
        forward["witness_at"] < inverse_source_at
        and forward["witness_at"] < inverse_accepted_at,
        "forward_witness_not_before_inverse_source",
    )
    require(
        inverse["frame"]["frame_source"].get("session_id")
        != forward["frame"]["frame_source"].get("session_id"),
        "inverse_not_after_reload_session",
    )

    policy = _strict_object(bridge_dir / "action-policy.json", "current action policy")
    require(policy.get("schema") == "oni.action-policy.v1", "final_policy_schema_invalid")
    require(
        policy.get("actions_enabled") is False and policy.get("allowed_save_hashes") == [],
        "final_policy_not_disabled",
    )
    queues = _queue_evidence(bridge_dir)

    latest = manifest.get("latest") if isinstance(manifest.get("latest"), Mapping) else {}
    tip_state = _unique_snapshot(bridge_dir, latest)
    require(
        tip_state.get("session_id") == latest.get("session_id")
        and tip_state.get("save_id_hash") == latest.get("save_id_hash")
        and tip_state.get("world_id") == latest.get("world_id")
        and tip_state.get("save_dirty") is False,
        "tip_snapshot_identity_invalid",
    )
    current_latest_state = _strict_object(bridge_dir / "latest_state.json", "current latest state")
    pipeline_gate = _command_pipeline_evidence(
        bridge_dir,
        current_latest_state,
        policy,
        raw_state=tip_state,
    )
    expected_name = first_target[1]
    duplicant = (
        tip_state.get("duplicants", {})
        .get("by_instance_id", {})
        .get(f"id_{first_target[0]}", {})
        if isinstance(tip_state.get("duplicants"), Mapping)
        else {}
    )
    require(isinstance(duplicant, Mapping) and duplicant.get("name") == expected_name, "tip_target_name_mismatch")
    tip_read = read_contract_field(tip_state, forward["priority_path"], DUPLICANT_CHORE_PRIORITY_CONTRACT_ID)
    require(
        tip_read.usable and tip_read.value == first_target[3],
        "tip_priority_not_restored",
        ",".join(tip_read.blockers),
    )

    def frozen_command(item: Mapping[str, Any]) -> Dict[str, Any]:
        assessment = item["assessment"]
        command = item["command"]
        freeze = item["freeze"]
        freeze_document = freeze["document"]
        certificate = freeze_document["confirmation_certificate"]
        approval_bundle = freeze_document["approval_bundle"]
        return {
            "step_id": item["step_id"],
            "checkpoint_index": item["checkpoint_index"],
            "checkpoint_hash": item["checkpoint_hash"],
            "id": command.get("id"),
            "action": command.get("action"),
            "args": copy.deepcopy(command.get("args")),
            "file": _relative(item["command_path"], bridge_dir),
            "file_sha256": item["command_file_sha256"],
            "command_intent_hash": item["frame"]["intent_hash"],
            "approval_receipt_hash": item["frame"]["receipt_hash"],
            "policy_hash": item["frame"]["policy_hash"],
            "automatic_approval_bundle": {
                "path": approval_bundle.get("path"),
                "file_sha256": approval_bundle.get("file_sha256"),
                "bundle_hash": approval_bundle.get("bundle_hash"),
            },
            "lineage_freeze": {
                "path": str(freeze["path"]),
                "file_sha256": freeze["file_sha256"],
                "freeze_hash": freeze_document.get("freeze_hash"),
            },
            "source_frame": {
                "frame_id": item["frame"]["frame_id"],
                "archive_file": _relative(item["frame"]["frame_path"], bridge_dir),
                "archive_file_sha256": item["frame"]["frame_file_sha256"],
                "archive_currently_present": item["frame"]["frame_path"].is_file(),
            },
            "confirmation": {
                "attestation_hash": assessment.get("attestation_hash"),
                "ack_sequences": copy.deepcopy(assessment.get("ack_sequences")),
                "witness": copy.deepcopy(assessment.get("witness")),
                "certificate": {
                    "path": certificate.get("path"),
                    "file_sha256": certificate.get("file_sha256"),
                    "outcomes_hash": certificate.get("outcomes_hash"),
                },
            },
        }

    _confirm_object_snapshot(manifest_path, "lineage manifest", manifest_snapshot)
    report: Dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "status": "passed",
        "classification": None,
        "verification_mode": "offline_read_only",
        "fail_closed": True,
        "manifest": {
            "path": str(manifest_path),
            "file_sha256": manifest_snapshot["file_sha256"],
            "test_run_id": manifest.get("test_run_id"),
            "latest_checkpoint_hash": manifest.get("latest_checkpoint_hash"),
        },
        "pair": {
            "forward_step": forward_step,
            "inverse_step": inverse_step,
            "target": {
                "duplicant_instance_id": first_target[0],
                "expected_name": first_target[1],
                "chore_group": first_target[2],
            },
            "original_priority": first_target[3],
            "temporary_priority": first_target[4],
            "commands": [frozen_command(forward), frozen_command(inverse)],
        },
        "final_tip": {
            "session_id": latest.get("session_id"),
            "snapshot_id": latest.get("snapshot_id"),
            "save_id_hash": latest.get("save_id_hash"),
            "state_hash": canonical_state_hash(tip_state),
            "priority_path": forward["priority_path"],
            "observed_priority": tip_read.value,
        },
        "shutdown": {
            "policy_file_sha256": pipeline_gate["inputs"]["policy_file_sha256"],
            "actions_enabled": False,
            "allowed_save_hash_count": 0,
            "queues": queues,
            "command_pipeline": pipeline_gate,
        },
        "report_hash": "",
    }
    report["report_hash"] = _hash_document(report, "report_hash")
    return report


def write_report_create_only(path: pathlib.Path, report: Mapping[str, Any]) -> pathlib.Path:
    """Create the finalization report once; never overwrite prior evidence."""

    target = normalized_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError as exc:
        raise PriorityPairError("finalization_report_already_exists", str(target)) from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    require(sha256_file(target) == "sha256:" + hashlib.sha256(payload).hexdigest(), "finalization_report_write_mismatch")
    return target


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    freeze_parser = subparsers.add_parser(
        "freeze-command",
        help="Freeze one already confirmed priority command before its save/reload checkpoint.",
    )
    freeze_parser.add_argument("--manifest", type=pathlib.Path, required=True)
    freeze_parser.add_argument("--bridge-dir", type=pathlib.Path, required=True)
    freeze_parser.add_argument("--step-id", required=True)
    freeze_parser.add_argument("--command-id", required=True)

    finalize_parser = subparsers.add_parser(
        "finalize",
        help="Verify the two checkpoint-bound freezes and optionally publish the final report.",
    )
    finalize_parser.add_argument("--manifest", type=pathlib.Path, required=True)
    finalize_parser.add_argument("--bridge-dir", type=pathlib.Path, required=True)
    finalize_parser.add_argument("--forward-step", default=DEFAULT_FORWARD_STEP)
    finalize_parser.add_argument("--inverse-step", default=DEFAULT_INVERSE_STEP)
    finalize_parser.add_argument("--report", type=pathlib.Path)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.operation == "freeze-command":
            result = freeze_confirmed_command(
                args.manifest,
                args.bridge_dir,
                step_id=args.step_id,
                command_id=args.command_id,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))
            return 0
        report = verify_priority_pair(
            args.manifest,
            args.bridge_dir,
            forward_step=args.forward_step,
            inverse_step=args.inverse_step,
        )
        if args.report:
            report_path = write_report_create_only(args.report, report)
            report = dict(report)
            report["report_path"] = str(report_path)
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))
        return 0
    except PriorityPairError as exc:
        print(
            json.dumps(
                {
                    "schema": REPORT_SCHEMA,
                    "status": "incomplete_or_failed",
                    "classification": "behavior_fail",
                    "verification_mode": "offline_read_only",
                    "blockers": [exc.code],
                    "detail": exc.detail,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 1
    except Exception as exc:
        print(
            json.dumps(
                {
                    "schema": REPORT_SCHEMA,
                    "status": "incomplete_or_failed",
                    "classification": "infra_fail",
                    "verification_mode": "offline_read_only",
                    "blockers": ["audit_exception"],
                    "detail": str(exc),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
