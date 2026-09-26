from __future__ import annotations

import hashlib
import json
import pathlib
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from agent.action_outcomes import (
    CONFIRMATION_CONFIRMED,
    confirmed_command_commit_blockers,
    read_action_outcome,
)
from agent.field_contracts import NON_MUTATING_ACTIONS
from agent.state_quality import canonical_state_hash, parse_positive_sequence


CONFIRMED_MUTATION_MODE = "snapshot_confirmed_mutation_v1"
NON_MUTATING_VERIFIED_MODE = "non_mutating_verified_v1"
LEGACY_MUTATION_MODE = "legacy_executor_verified_read_only"
INVALID_CONFIRMATION_MODE = "invalid_or_unconfirmed"
REQUIRED_MUTATION_ACKS: Tuple[str, ...] = (
    "accepted",
    "executed",
    "local_effect_observed",
)


def _read_json_object(path: pathlib.Path) -> Optional[Dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return dict(value) if isinstance(value, dict) else None


def _file_sha256(path: pathlib.Path) -> Optional[str]:
    try:
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _certificate_paths(bridge_dir: pathlib.Path, session_id: str) -> List[pathlib.Path]:
    bridge_dir = bridge_dir.expanduser().resolve()
    result = [bridge_dir / "action-outcomes.json"]
    sessions = bridge_dir / "sessions"
    if sessions.is_dir():
        for path in sorted(sessions.glob("*/action-outcomes.json")):
            document = _read_json_object(path)
            source = document.get("source") if isinstance(document, dict) else None
            if isinstance(source, Mapping) and source.get("session_id") == session_id:
                result.append(path)
    return result


def _matching_outcome(
    bridge_dir: pathlib.Path,
    command: Mapping[str, Any],
) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]], List[str]]:
    command_id = str(command.get("id") or "")
    session_id = str(command.get("session_id") or "")
    matches: List[Tuple[Dict[str, Any], pathlib.Path, Dict[str, Any]]] = []
    blockers: List[str] = []
    for path in _certificate_paths(bridge_dir, session_id):
        document = _read_json_object(path)
        if not isinstance(document, dict):
            continue
        source = document.get("source") if isinstance(document.get("source"), Mapping) else {}
        if source.get("session_id") != session_id:
            continue
        outcome, read_blockers = read_action_outcome(path.parent, command_id)
        if outcome is None:
            if read_blockers != ["action_outcome_missing"]:
                blockers.extend(f"certificate_{item}" for item in read_blockers)
            continue
        matches.append((outcome, path, document))

    if not matches:
        blockers.append("snapshot_confirmation_certificate_missing")
        return None, [], sorted(set(blockers))

    attestation_hashes = {str(item[0].get("attestation_hash") or "") for item in matches}
    if len(attestation_hashes) != 1:
        blockers.append("snapshot_confirmation_certificate_ambiguous")
        return None, [], sorted(set(blockers))

    outcome = matches[0][0]
    publications: List[Dict[str, Any]] = []
    session_copy_present = False
    for candidate, path, document in matches:
        if candidate != outcome:
            blockers.append("snapshot_confirmation_publications_disagree")
        trust = document.get("trust") if isinstance(document.get("trust"), Mapping) else {}
        source = document.get("source") if isinstance(document.get("source"), Mapping) else {}
        witness = outcome.get("witness") if isinstance(outcome.get("witness"), Mapping) else {}
        if (
            trust.get("status") != "trusted"
            or trust.get("blockers") != []
            or trust.get("replay_mode") is not False
        ):
            blockers.append("snapshot_confirmation_publication_untrusted")
        if (
            source.get("session_id") != command.get("session_id")
            or source.get("save_id_hash") != command.get("save_id_hash")
            or source.get("world_id") != command.get("world_id")
            or source.get("snapshot_id") != witness.get("snapshot_id")
            or source.get("seq") != witness.get("seq")
            or source.get("state_hash") != witness.get("state_hash")
        ):
            blockers.append("snapshot_confirmation_publication_source_mismatch")
        relative = None
        try:
            relative = path.relative_to(bridge_dir.expanduser().resolve()).as_posix()
        except ValueError:
            relative = str(path)
        if relative.startswith("sessions/"):
            session_copy_present = True
        publications.append(
            {
                "path": relative,
                "file_sha256": _file_sha256(path),
                "outcomes_hash": document.get("outcomes_hash"),
            }
        )
    if not session_copy_present:
        blockers.append("session_confirmation_certificate_missing")
    return outcome, publications, sorted(set(blockers))


def _matching_ack_records(
    records: Iterable[Mapping[str, Any]],
    command: Mapping[str, Any],
) -> List[Mapping[str, Any]]:
    return [
        record
        for record in records
        if record.get("event") == "command_ack"
        and record.get("id") == command.get("id")
        and record.get("session_id") == command.get("session_id")
        and record.get("attempt_id") == command.get("attempt_id")
    ]


def archived_confirmation_records(
    bridge_dir: pathlib.Path,
    command: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    """Read only the ACK/witness rows needed from the durable session archive."""

    bridge_dir = bridge_dir.expanduser().resolve()
    session_id = command.get("session_id")
    embedded = command.get("confirmation") if isinstance(command.get("confirmation"), Mapping) else {}
    witness = embedded.get("witness") if isinstance(embedded.get("witness"), Mapping) else {}
    witness_id = witness.get("snapshot_id")
    result: List[Dict[str, Any]] = []
    seen: set[str] = set()
    sessions = bridge_dir / "sessions"
    if not sessions.is_dir():
        return result
    for certificate_path in sorted(sessions.glob("*/action-outcomes.json")):
        certificate = _read_json_object(certificate_path)
        source = certificate.get("source") if isinstance(certificate, Mapping) else {}
        if not isinstance(source, Mapping) or source.get("session_id") != session_id:
            continue
        try:
            with (certificate_path.parent / "state.jsonl").open("r", encoding="utf-8-sig") as handle:
                for line in handle:
                    try:
                        record = json.loads(line)
                    except (json.JSONDecodeError, UnicodeError):
                        continue
                    if not isinstance(record, dict):
                        continue
                    wanted = (
                        record.get("event") == "command_ack"
                        and record.get("id") == command.get("id")
                        and record.get("attempt_id") == command.get("attempt_id")
                    ) or (
                        record.get("event") == "state_snapshot"
                        and record.get("snapshot_id") == witness_id
                    )
                    if not wanted:
                        continue
                    fingerprint = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    if fingerprint not in seen:
                        seen.add(fingerprint)
                        result.append(record)
        except (FileNotFoundError, OSError, UnicodeError):
            continue
    return result


def _ack_blockers(
    records: Iterable[Mapping[str, Any]],
    command: Mapping[str, Any],
    outcome: Mapping[str, Any],
) -> Tuple[Dict[str, Optional[int]], List[str]]:
    declared = {
        "accepted": parse_positive_sequence(command.get("accepted_ack_seq")),
        "executed": parse_positive_sequence(command.get("executed_ack_seq")),
        "local_effect_observed": parse_positive_sequence(command.get("local_effect_ack_seq")),
    }
    blockers: List[str] = []
    if any(value is None for value in declared.values()):
        blockers.append("ordered_ack_sequence_missing")
    elif not (
        int(declared["accepted"] or 0)
        < int(declared["executed"] or 0)
        < int(declared["local_effect_observed"] or 0)
    ):
        blockers.append("ordered_ack_sequence_invalid")

    matching = _matching_ack_records(records, command)
    for stage in REQUIRED_MUTATION_ACKS:
        stage_records = [
            record
            for record in matching
            if record.get("status") == stage and record.get("seq") == declared.get(stage)
        ]
        if len(stage_records) != 1:
            blockers.append(f"{stage}_ack_missing_or_ambiguous")
    if any(record.get("status") in {"failed", "rejected", "skipped_stale"} for record in matching):
        blockers.append("contradictory_terminal_ack")

    witness = outcome.get("witness") if isinstance(outcome.get("witness"), Mapping) else {}
    witness_seq = parse_positive_sequence(witness.get("seq"))
    if (
        witness_seq is None
        or declared["local_effect_observed"] is None
        or witness_seq <= int(declared["local_effect_observed"] or 0)
    ):
        blockers.append("trusted_witness_not_after_local_effect_ack")
    return declared, sorted(set(blockers))


def _witness_state(
    records: Iterable[Mapping[str, Any]],
    command: Mapping[str, Any],
    outcome: Mapping[str, Any],
) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    witness = outcome.get("witness") if isinstance(outcome.get("witness"), Mapping) else {}
    matches = [
        dict(record)
        for record in records
        if record.get("event") == "state_snapshot"
        and record.get("session_id") == command.get("session_id")
        and record.get("snapshot_id") == witness.get("snapshot_id")
        and record.get("seq") == witness.get("seq")
    ]
    blockers: List[str] = []
    if len(matches) != 1:
        blockers.append("trusted_witness_snapshot_missing_or_ambiguous")
        return None, blockers
    state = {
        key: value
        for key, value in matches[0].items()
        if not str(key).startswith("_")
    }
    if canonical_state_hash(state) != witness.get("state_hash"):
        blockers.append("trusted_witness_state_hash_mismatch")
    if (
        state.get("save_id_hash") != command.get("save_id_hash")
        or state.get("world_id") != command.get("world_id")
        or state.get("save_dirty") is not False
    ):
        blockers.append("trusted_witness_identity_mismatch")
    return state, sorted(set(blockers))


def assess_command_confirmation(
    bridge_dir: pathlib.Path,
    command: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
    *,
    directory: Optional[str] = None,
) -> Dict[str, Any]:
    """Classify terminal command evidence without upgrading legacy executor claims.

    ``accepted`` is true only for a non-mutating ``done/verified`` command or a
    mutating ``done/confirmed`` command whose embedded and published Bridge
    attestation, ordered archived ACKs, and later archived witness all agree.
    Legacy ``done/verified`` mutations remain readable but never become current
    acceptance evidence.
    """

    location = str(directory or command.get("_directory") or "")
    action = str(command.get("action") or "")
    status = str(command.get("status") or "")
    mutating = action not in NON_MUTATING_ACTIONS
    result: Dict[str, Any] = {
        "accepted": False,
        "mode": INVALID_CONFIRMATION_MODE,
        "mutating": mutating,
        "directory": location,
        "status": status,
        "blockers": [],
        "attestation_hash": None,
        "witness": None,
        "ack_sequences": {},
        "certificate_publications": [],
        "legacy_read_only": False,
        "witness_state": None,
    }

    if not mutating:
        if location == "done" and status == "verified":
            result.update(
                {
                    "accepted": True,
                    "mode": NON_MUTATING_VERIFIED_MODE,
                    "blockers": [],
                }
            )
        else:
            result["blockers"] = ["non_mutating_command_not_done_verified"]
        return result

    if location == "done" and status == "verified":
        result.update(
            {
                "mode": LEGACY_MUTATION_MODE,
                "legacy_read_only": True,
                "blockers": ["legacy_executor_verified_without_snapshot_confirmation"],
            }
        )
        return result

    blockers: List[str] = []
    if location != "done":
        blockers.append("mutating_command_not_in_done")
    if status != "confirmed":
        blockers.append("mutating_command_status_not_confirmed")
    outcome, publications, publication_blockers = _matching_outcome(bridge_dir, command)
    blockers.extend(publication_blockers)
    if outcome is not None:
        blockers.extend(confirmed_command_commit_blockers(command, outcome))
        if outcome.get("confirmation_status") != CONFIRMATION_CONFIRMED:
            blockers.append("snapshot_confirmation_not_confirmed")
        if outcome.get("blockers") != []:
            blockers.append("snapshot_confirmation_contains_blockers")
        ack_sequences, ack_blockers = _ack_blockers(records, command, outcome)
        witness_state, witness_blockers = _witness_state(records, command, outcome)
        blockers.extend(ack_blockers)
        blockers.extend(witness_blockers)
        result.update(
            {
                "attestation_hash": outcome.get("attestation_hash"),
                "witness": dict(outcome.get("witness") or {}),
                "ack_sequences": ack_sequences,
                "certificate_publications": publications,
                "witness_state": witness_state,
            }
        )
    blockers = sorted(set(blockers))
    result.update(
        {
            "accepted": not blockers,
            "mode": CONFIRMED_MUTATION_MODE if not blockers else INVALID_CONFIRMATION_MODE,
            "blockers": blockers,
        }
    )
    return result


def confirmation_summary(assessment: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        key: assessment.get(key)
        for key in (
            "accepted",
            "mode",
            "mutating",
            "directory",
            "status",
            "blockers",
            "attestation_hash",
            "witness",
            "ack_sequences",
            "certificate_publications",
            "legacy_read_only",
        )
    }


def annotate_command_confirmations(
    bridge_dir: pathlib.Path,
    commands: Iterable[Dict[str, Any]],
    records: Sequence[Mapping[str, Any]],
) -> None:
    for command in commands:
        assessment = assess_command_confirmation(bridge_dir, command, records)
        command["_confirmation_contract"] = confirmation_summary(assessment)
        command["_confirmation_witness"] = assessment.get("witness_state")


def confirmed_mutating_command(command: Mapping[str, Any]) -> bool:
    contract = command.get("_confirmation_contract")
    return (
        isinstance(contract, Mapping)
        and contract.get("accepted") is True
        and contract.get("mode") == CONFIRMED_MUTATION_MODE
        and command.get("_directory") == "done"
        and command.get("status") == "confirmed"
    )


__all__ = [
    "CONFIRMED_MUTATION_MODE",
    "INVALID_CONFIRMATION_MODE",
    "LEGACY_MUTATION_MODE",
    "NON_MUTATING_VERIFIED_MODE",
    "REQUIRED_MUTATION_ACKS",
    "annotate_command_confirmations",
    "archived_confirmation_records",
    "assess_command_confirmation",
    "confirmation_summary",
    "confirmed_mutating_command",
]
