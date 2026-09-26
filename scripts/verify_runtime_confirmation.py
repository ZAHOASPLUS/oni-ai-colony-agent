#!/usr/bin/env python3
"""Offline adversarial checks for runtime/save-lineage confirmation consumers."""

from __future__ import annotations

import argparse
import copy
import json
import pathlib
import sys
import tempfile
from typing import Any, Dict, Tuple


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.action_outcomes import build_action_outcomes, commit_confirmed_outcomes
from agent.command_queue import write_json_atomic
from agent.runtime_confirmation import (
    CONFIRMED_MUTATION_MODE,
    LEGACY_MUTATION_MODE,
    NON_MUTATING_VERIFIED_MODE,
    annotate_command_confirmations,
    assess_command_confirmation,
)
from scripts.verify_action_outcomes import health_for, prepare
from scripts.verify_runtime import action_check


REPORT_PATH = PROJECT_ROOT / "reports" / "runtime-confirmation-verification.json"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def read_records(path: pathlib.Path) -> list[Dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def append_record(path: pathlib.Path, record: Dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


def confirmed_fixture(
    root: pathlib.Path,
    command_id: str,
    *,
    publish_session: bool = True,
) -> Tuple[Dict[str, Any], Dict[str, Any], list[Dict[str, Any]], pathlib.Path]:
    source, witness, _ = prepare(root, command_id)
    history = root / "sessions" / str(source["session_id"]) / "state.jsonl"
    append_record(history, witness)
    document = build_action_outcomes(root, witness, health_for(witness))
    root_certificate = root / "action-outcomes.json"
    session_certificate = history.parent / "action-outcomes.json"
    write_json_atomic(root_certificate, document)
    publications = [root_certificate]
    if publish_session:
        write_json_atomic(session_certificate, document)
        publications.append(session_certificate)
    committed = commit_confirmed_outcomes(root, document, publication_paths=tuple(publications))
    require(committed == [command_id], f"fixture did not commit {command_id}: {committed!r}")
    done_path = root / "commands" / "done" / f"{command_id}.json"
    command = json.loads(done_path.read_text(encoding="utf-8"))
    command["_directory"] = "done"
    return command, witness, read_records(history), history


def case_valid(root: pathlib.Path) -> Dict[str, Any]:
    command, witness, records, _ = confirmed_fixture(root, "cmd-runtime-confirmation-valid")
    assessment = assess_command_confirmation(root, command, records)
    require(assessment["accepted"] is True, str(assessment["blockers"]))
    require(assessment["mode"] == CONFIRMED_MUTATION_MODE, "wrong confirmation mode")
    require(
        assessment["ack_sequences"] == {
            "accepted": 11,
            "executed": 12,
            "local_effect_observed": 13,
        },
        "ordered ACKs were not bound",
    )
    require(assessment["witness"]["snapshot_id"] == witness["snapshot_id"], "witness binding changed")

    annotate_command_confirmations(root, [command], records)
    runtime = action_check([command], records, "set_paused", "game.paused")
    require(runtime["status"] == "passed", "runtime consumer rejected a valid confirmed mutation")
    return {
        "mode": assessment["mode"],
        "attestation_hash": assessment["attestation_hash"],
        "ordered_acks": assessment["ack_sequences"],
        "witness_seq": assessment["witness"]["seq"],
        "runtime_status": runtime["status"],
    }


def case_legacy(root: pathlib.Path) -> Dict[str, Any]:
    legacy = {
        "schema": "oni.command.v3",
        "id": "cmd-runtime-legacy-after-state",
        "action": "set_paused",
        "status": "verified",
        "_directory": "done",
        "expected_effects": [{"path": "game.paused", "value": True}],
        "result": {
            "before_state": {"game": {"paused": False}},
            "after_state": {"game": {"paused": True}},
        },
    }
    assessment = assess_command_confirmation(root, legacy, [])
    require(assessment["accepted"] is False, "legacy executor result became current acceptance")
    require(assessment["mode"] == LEGACY_MUTATION_MODE, "legacy mutation was not classified read-only")
    annotate_command_confirmations(root, [legacy], [])
    runtime = action_check([legacy], [], "set_paused", "game.paused")
    require(runtime["status"] == "failed", "runtime consumer accepted legacy after_state self-proof")
    require(runtime["classification"] == "legacy_read_only", "legacy runtime classification was lost")

    noop = {
        "schema": "oni.command.v3",
        "id": "cmd-runtime-noop",
        "action": "noop",
        "status": "verified",
        "_directory": "done",
    }
    noop_assessment = assess_command_confirmation(root, noop, [])
    require(
        noop_assessment["accepted"] is True
        and noop_assessment["mode"] == NON_MUTATING_VERIFIED_MODE,
        "noop done/verified compatibility was lost",
    )
    return {
        "legacy_mode": assessment["mode"],
        "legacy_runtime_status": runtime["status"],
        "noop_mode": noop_assessment["mode"],
    }


def case_missing_session_certificate(root: pathlib.Path) -> Dict[str, Any]:
    command, _, records, _ = confirmed_fixture(
        root,
        "cmd-runtime-confirmation-root-only",
        publish_session=False,
    )
    assessment = assess_command_confirmation(root, command, records)
    require(assessment["accepted"] is False, "root-only certificate was accepted")
    require("session_confirmation_certificate_missing" in assessment["blockers"], str(assessment["blockers"]))
    return {"blockers": assessment["blockers"]}


def case_archive_and_payload_tamper(root: pathlib.Path) -> Dict[str, Any]:
    command, _, records, history = confirmed_fixture(root, "cmd-runtime-confirmation-tamper")

    missing_ack_records = [
        record
        for record in records
        if not (
            record.get("event") == "command_ack"
            and record.get("status") == "local_effect_observed"
        )
    ]
    missing_ack = assess_command_confirmation(root, command, missing_ack_records)
    require(missing_ack["accepted"] is False, "missing local-effect ACK was accepted")
    require(
        "local_effect_observed_ack_missing_or_ambiguous" in missing_ack["blockers"],
        str(missing_ack["blockers"]),
    )

    unordered_command = copy.deepcopy(command)
    unordered_command["executed_ack_seq"] = unordered_command["accepted_ack_seq"]
    unordered = assess_command_confirmation(root, unordered_command, records)
    require(unordered["accepted"] is False, "unordered ACK declarations were accepted")
    require("ordered_ack_sequence_invalid" in unordered["blockers"], str(unordered["blockers"]))

    after_tamper_command = copy.deepcopy(command)
    after_tamper_command["result"]["after_state"]["game"]["paused"] = False
    after_tamper = assess_command_confirmation(root, after_tamper_command, records)
    require(after_tamper["accepted"] is False, "tampered executor after_state was accepted")
    require("done_direct_after_state_hash_mismatch" in after_tamper["blockers"], str(after_tamper["blockers"]))

    witness_id = command["confirmation"]["witness"]["snapshot_id"]
    missing_witness_records = [record for record in records if record.get("snapshot_id") != witness_id]
    missing_witness = assess_command_confirmation(root, command, missing_witness_records)
    require(missing_witness["accepted"] is False, "missing trusted witness was accepted")
    require(
        "trusted_witness_snapshot_missing_or_ambiguous" in missing_witness["blockers"],
        str(missing_witness["blockers"]),
    )

    confirming = copy.deepcopy(command)
    confirming["_directory"] = "confirming"
    confirming["status"] = "executed_pending_snapshot"
    confirming_assessment = assess_command_confirmation(root, confirming, records)
    require(confirming_assessment["accepted"] is False, "confirming command was treated as success")

    # Keep the fixture path referenced so this test also proves it used only a
    # temporary isolated archive.
    require(history.is_file(), "fixture history unexpectedly disappeared")
    return {
        "missing_ack_blockers": missing_ack["blockers"],
        "unordered_ack_blockers": unordered["blockers"],
        "after_state_tamper_blockers": after_tamper["blockers"],
        "missing_witness_blockers": missing_witness["blockers"],
        "confirming_blockers": confirming_assessment["blockers"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=pathlib.Path, default=REPORT_PATH)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="oni-runtime-confirmation-") as temporary:
        root = pathlib.Path(temporary)
        cases = {
            "valid_confirmed": case_valid(root / "valid"),
            "legacy_read_only": case_legacy(root / "legacy"),
            "missing_session_certificate": case_missing_session_certificate(root / "root-only"),
            "tamper_gates": case_archive_and_payload_tamper(root / "tamper"),
        }
    report = {
        "schema": "oni.runtime-confirmation-verification.v1",
        "status": "passed",
        "isolated_fixture_only": True,
        "game_launched": False,
        "cases": cases,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(args.report, report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    print(f"Report: {args.report.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
