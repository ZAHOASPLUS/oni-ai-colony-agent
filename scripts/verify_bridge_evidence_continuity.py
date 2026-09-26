#!/usr/bin/env python3
"""Verify bounded, fail-closed recovery of unresolved Bridge evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
import tempfile
import time
from typing import Any, Callable, Dict, List


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bridge import oni_bridge


SESSION_ID = "session-evidence-continuity"
ATTEMPT_ID = "attempt-" + "e" * 32


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def canonical(record: Dict[str, Any]) -> str:
    return json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def write_json(path: pathlib.Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_jsonl(path: pathlib.Path, records: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(canonical(record) for record in records) + "\n", encoding="utf-8")


def source(seq: int = 2, *, variant: str = "canonical") -> Dict[str, Any]:
    return {
        "schema": "oni.state.v2",
        "event": "state_snapshot",
        "session_id": SESSION_ID,
        "seq": seq,
        "snapshot_id": f"{SESSION_ID}:{seq}",
        "variant": variant,
    }


def ack(command_id: str, seq: int, status: str, *, variant: str = "canonical") -> Dict[str, Any]:
    return {
        "schema": "oni.event.v2",
        "event": "command_ack",
        "session_id": SESSION_ID,
        "seq": seq,
        "id": command_id,
        "attempt_id": ATTEMPT_ID,
        "status": status,
        "variant": variant,
    }


def command(command_id: str, *, directory: str = "confirming") -> Dict[str, Any]:
    value: Dict[str, Any] = {
        "schema": "oni.command.v2",
        "id": command_id,
        "session_id": SESSION_ID,
        "source_snapshot_id": f"{SESSION_ID}:2",
        "attempt_id": ATTEMPT_ID,
        "accepted_ack_seq": 3,
    }
    if directory == "confirming":
        value.update(
            {
                "status": "executed_pending_snapshot",
                "executed_ack_seq": 4,
                "local_effect_ack_seq": 5,
            }
        )
    else:
        value["status"] = "executing"
    return value


def install_command(root: pathlib.Path, command_id: str, *, directory: str = "confirming") -> None:
    write_json(root / "commands" / directory / f"{command_id}.json", command(command_id, directory=directory))


def evidence_records(command_id: str) -> List[Dict[str, Any]]:
    return [
        source(),
        ack(command_id, 3, "accepted"),
        ack(command_id, 4, "executed"),
        ack(command_id, 5, "local_effect_observed"),
    ]


def archive_records(root: pathlib.Path) -> List[Dict[str, Any]]:
    path = root / "sessions" / SESSION_ID / "state.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def archive_hash(root: pathlib.Path) -> str:
    payload = (root / "sessions" / SESSION_ID / "state.jsonl").read_bytes()
    return hashlib.sha256(payload).hexdigest()


def case_clean_tail(root: pathlib.Path) -> Dict[str, Any]:
    command_id = "command-clean-tail"
    install_command(root, command_id)
    records = [
        {"schema": "oni.event.v2", "event": "session_started", "session_id": SESSION_ID, "seq": 1},
        *evidence_records(command_id),
        {"schema": "oni.event.v2", "event": "heartbeat", "session_id": SESSION_ID, "seq": 6},
    ]
    write_jsonl(root / "state.jsonl", records)
    oni_bridge.replay_initial_records(root / "state.jsonl", root)
    report = oni_bridge.recover_unresolved_command_evidence(root)
    archived = archive_records(root)
    require(report["status"] == "ready", f"clean tail was blocked: {report['blockers']}")
    require(report["recovered_record_count"] == 0, "clean tail was rewritten as recovered history")
    require([item["seq"] for item in archived] == list(range(1, 7)), "clean tail order changed")
    require(len({canonical(item) for item in archived}) == len(archived), "clean tail contains duplicates")
    return {"archive_lines": len(archived), "recovered_records": 0, "status": report["status"]}


def case_gap_over_10k(root: pathlib.Path) -> Dict[str, Any]:
    command_id = "command-gap-over-10k"
    install_command(root, command_id)
    records = [
        {"schema": "oni.event.v2", "event": "session_started", "session_id": SESSION_ID, "seq": 1},
        *evidence_records(command_id),
    ]
    for seq in range(6, oni_bridge.MAX_INITIAL_LINES + 26):
        records.append(
            {"schema": "oni.event.v2", "event": "heartbeat", "session_id": SESSION_ID, "seq": seq}
        )
    write_jsonl(root / "state.jsonl", records)
    oni_bridge.replay_initial_records(root / "state.jsonl", root)
    before = archive_records(root)
    require(min(int(item["seq"]) for item in before) > 5, "fixture did not push evidence outside the replay tail")
    report = oni_bridge.recover_unresolved_command_evidence(root)
    archived = archive_records(root)
    sequences = [int(item["seq"]) for item in archived]
    require(report["status"] == "ready", f">10k recovery was blocked: {report['blockers']}")
    require(report["recovered_record_count"] == 4, "required source/ACK set was not recovered exactly")
    require(sequences[:4] == [2, 3, 4, 5], "recovered history was not merged in producer order")
    require(sequences == sorted(sequences), "archive order regressed after gap recovery")
    require(len({canonical(item) for item in archived}) == len(archived), "gap recovery duplicated canonical records")
    return {
        "source_line_count": len(records),
        "initial_tail_count": len(before),
        "recovered_records": report["recovered_record_count"],
        "first_sequences": sequences[:6],
    }


def case_restart_idempotence(root: pathlib.Path) -> Dict[str, Any]:
    command_id = "command-restart"
    install_command(root, command_id, directory="executing")
    write_jsonl(root / "state.jsonl", [source(), ack(command_id, 3, "accepted")])
    first = oni_bridge.recover_unresolved_command_evidence(root)
    first_hash = archive_hash(root)
    second = oni_bridge.recover_unresolved_command_evidence(root)
    second_hash = archive_hash(root)
    require(first["status"] == "ready" and second["status"] == "ready", "executing evidence was blocked")
    require(first["recovered_record_count"] == 2, "first recovery did not archive source plus accepted ACK")
    require(second["recovered_record_count"] == 0, "restart duplicated already archived evidence")
    require(first_hash == second_hash, "restart changed the canonical session archive")
    return {
        "directory": "executing",
        "first_recovered": first["recovered_record_count"],
        "restart_recovered": second["recovered_record_count"],
        "archive_sha256": first_hash,
    }


def case_missing_source(root: pathlib.Path) -> Dict[str, Any]:
    command_id = "command-missing-source"
    install_command(root, command_id)
    write_jsonl(
        root / "state.jsonl",
        [
            ack(command_id, 3, "accepted"),
            ack(command_id, 4, "executed"),
            ack(command_id, 5, "local_effect_observed"),
        ],
    )
    report = oni_bridge.recover_unresolved_command_evidence(root)
    health: Dict[str, Any] = {"status": "ok", "trust_ready": True, "trust_blockers": []}
    oni_bridge.apply_evidence_continuity_health(health, report)
    require(report["status"] == "blocked", "missing source was accepted")
    require("source_snapshot_missing" in report["blockers"], "missing source blocker was not explicit")
    require(health["trust_ready"] is False, "missing source did not fail health closed")
    require("evidence_continuity_blocked" in health["trust_blockers"], "health blocker was not surfaced")
    oni_bridge.write_outputs(root, oni_bridge.ReplayState())
    published_health = json.loads((root / "health.json").read_text(encoding="utf-8"))
    require(
        published_health.get("evidence_continuity", {}).get("status") == "blocked",
        "write_outputs did not publish the recovery blocker",
    )
    require(
        "evidence_continuity_blocked" in published_health.get("trust_blockers", []),
        "write_outputs lost the confirmation gate",
    )
    pipeline = published_health.get("command_pipeline")
    require(isinstance(pipeline, dict), "write_outputs did not publish the command-pipeline gate")
    require(pipeline.get("schema") == "oni.command-pipeline-gate.v1", "pipeline schema was not preserved")
    require(pipeline.get("proposal_frame_evaluated") is False, "pre-frame gate claimed frame evaluation")
    require(
        pipeline.get("proposal_frame_binding") == "deferred_until_sync_pending_commands",
        "frame-specific enforcement handoff was not explicit",
    )
    proposal = pipeline.get("readiness", {}).get("mutation_proposal", {})
    require(proposal.get("ready") is False, "in-flight confirmation did not block another mutation")
    require(
        f"mutation_confirmation_in_flight:{command_id}" in proposal.get("blockers", []),
        "deterministic in-flight blocker was not published",
    )
    require(
        (root / "commands" / "confirming" / f"{command_id}.json").exists(),
        "blocked command moved out of confirming",
    )
    return {
        "blockers": report["blockers"],
        "trust_ready": health["trust_ready"],
        "published_gate": True,
        "pipeline_proposal_ready": proposal["ready"],
    }


def case_conflicting_duplicate(root: pathlib.Path) -> Dict[str, Any]:
    command_id = "command-conflict"
    install_command(root, command_id)
    write_jsonl(
        root / "state.jsonl.1",
        [source(variant="backup"), ack(command_id, 3, "accepted", variant="backup")],
    )
    write_jsonl(
        root / "state.jsonl",
        [
            source(variant="current"),
            ack(command_id, 3, "accepted", variant="current"),
            ack(command_id, 4, "executed"),
            ack(command_id, 5, "local_effect_observed"),
        ],
    )
    report = oni_bridge.recover_unresolved_command_evidence(root)
    archived = archive_records(root)
    archived_sources = [item for item in archived if item.get("snapshot_id") == f"{SESSION_ID}:2"]
    archived_accepted = [item for item in archived if item.get("status") == "accepted"]
    require(report["status"] == "blocked", "conflicting source variants were accepted")
    require("source_snapshot_ambiguous" in report["blockers"], "source ambiguity was not explicit")
    require("accepted_ack_ambiguous" in report["blockers"], "ACK ambiguity was not explicit")
    require(len(archived_sources) == 2, "conflicting variants were silently collapsed")
    require(len(archived_accepted) == 2, "conflicting ACK variants were silently collapsed")
    require({item["variant"] for item in archived_sources} == {"backup", "current"}, "wrong variants archived")
    return {
        "source_variants": len(archived_sources),
        "accepted_ack_variants": len(archived_accepted),
        "blockers": report["blockers"],
    }


def case_corruption_and_truncation(root: pathlib.Path) -> Dict[str, Any]:
    command_id = "command-damaged-evidence"
    install_command(root, command_id)
    backup = root / "state.jsonl.1"
    write_jsonl(backup, evidence_records(command_id))
    with backup.open("ab") as handle:
        handle.write((f'{{"event":"command_ack","id":"{command_id}",broken\n').encode("utf-8"))
    partial = (
        f'{{"event":"state_snapshot","session_id":"{SESSION_ID}",'
        f'"snapshot_id":"{SESSION_ID}:2"'
    ).encode("utf-8")
    (root / "state.jsonl").write_bytes(partial)
    report = oni_bridge.recover_unresolved_command_evidence(root)
    require(report["status"] == "blocked", "damaged candidate sources were accepted")
    require("evidence_candidate_corrupt" in report["blockers"], "targeted corruption was not surfaced")
    require("evidence_candidate_truncated" in report["blockers"], "targeted truncation was not surfaced")
    require(report["recovered_record_count"] == 4, "valid evidence beside damage was not recovered")
    source_stats = {item["path"]: item for item in report["sources"]}
    require(source_stats["state.jsonl.1"]["invalid_lines"] == 1, "corrupt line count is wrong")
    require(source_stats["state.jsonl"]["partial_bytes"] == len(partial), "partial byte count is wrong")
    return {
        "blockers": report["blockers"],
        "invalid_lines": source_stats["state.jsonl.1"]["invalid_lines"],
        "partial_bytes": source_stats["state.jsonl"]["partial_bytes"],
    }


def run_case(name: str, function: Callable[[pathlib.Path], Dict[str, Any]], root: pathlib.Path) -> Dict[str, Any]:
    started = time.perf_counter()
    try:
        details = function(root / name)
        return {
            "name": name,
            "status": "passed",
            "duration_seconds": round(time.perf_counter() - started, 3),
            "details": details,
        }
    except Exception as exc:
        return {
            "name": name,
            "status": "failed",
            "duration_seconds": round(time.perf_counter() - started, 3),
            "classification": "behavior_fail",
            "error": str(exc),
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report",
        type=pathlib.Path,
        default=PROJECT_ROOT / "reports" / "bridge-evidence-continuity.json",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cases: List[tuple[str, Callable[[pathlib.Path], Dict[str, Any]]]] = [
        ("clean_tail", case_clean_tail),
        ("gap_over_10k", case_gap_over_10k),
        ("restart_idempotence", case_restart_idempotence),
        ("missing_source", case_missing_source),
        ("conflicting_duplicate", case_conflicting_duplicate),
        ("corruption_and_truncation", case_corruption_and_truncation),
    ]
    with tempfile.TemporaryDirectory(prefix="oni-evidence-continuity-") as temporary:
        root = pathlib.Path(temporary)
        results = [run_case(name, function, root) for name, function in cases]

    passed = all(result["status"] == "passed" for result in results)
    report = {
        "schema": "oni.bridge-evidence-continuity-verification.v1",
        "generated_at": oni_bridge.now_iso(),
        "status": "passed" if passed else "failed",
        "classification": None if passed else "behavior_fail",
        "cases": results,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for result in results:
        detail = result.get("error") or json.dumps(result.get("details"), ensure_ascii=False, sort_keys=True)
        print(f"[{result['status'].upper()}] {result['name']}: {detail}")
    print(f"Report: {args.report}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
