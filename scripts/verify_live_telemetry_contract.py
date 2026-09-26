#!/usr/bin/env python3
"""Offline regression checks for live Probe snapshot contract validation."""

from __future__ import annotations

import contextlib
import copy
import io
import json
import pathlib
import sys
import tempfile
from typing import Any, Dict, Sequence


ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.state_quality import canonical_state_hash
from tools.game_verify import verify_telemetry_contract as verifier


SCENARIO = ROOT / "tools" / "game_verify" / "scenarios" / "oni_telemetry_contract_mock.json"
CONTRACT = ROOT / "tools" / "game_verify" / "contracts" / "oni_telemetry.v1.json"
NOW = "2026-08-30T08:00:01+08:00"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def write_json(path: pathlib.Path, payload: Dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def invoke(arguments: Sequence[str]) -> int:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return verifier.main(list(arguments))


def trusted_health(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "schema": "oni.bridge.health.v1",
        "updated_at": NOW,
        "status": "ok",
        "current_session_id": snapshot["session_id"],
        "latest_snapshot_id": snapshot["snapshot_id"],
        "latest_seq": snapshot["seq"],
        "trust_ready": True,
        "trust_blockers": [],
        "source": {
            "session_id": snapshot["session_id"],
            "snapshot_id": snapshot["snapshot_id"],
            "state_hash": canonical_state_hash(snapshot),
        },
    }


def main() -> int:
    scenario = verifier.load_json(SCENARIO)
    snapshot = scenario["telemetry_contract"]["base_snapshot"]
    require(isinstance(snapshot, dict), "scenario base snapshot is missing")

    with tempfile.TemporaryDirectory(prefix="oni-live-contract-") as temporary:
        root = pathlib.Path(temporary)
        snapshot_path = root / "latest_state.json"
        health_path = root / "health.json"
        report_path = root / "report.json"
        write_json(snapshot_path, snapshot)
        write_json(health_path, trusted_health(snapshot))

        code = invoke(
            [
                "--snapshot",
                str(snapshot_path),
                "--health",
                str(health_path),
                "--contract",
                str(CONTRACT),
                "--now",
                NOW,
                "--output",
                str(report_path),
                "--json",
            ]
        )
        report = verifier.load_json(report_path)
        require(code == 0 and report["accepted"] is True, "trusted live snapshot was rejected")
        require(report["command_eligible"] is True, "trusted health did not permit command eligibility")
        require(report["source"]["state_hash"] == canonical_state_hash(snapshot), "state hash binding drifted")

        no_health_report = root / "no-health-report.json"
        code = invoke(
            [
                "--snapshot",
                str(snapshot_path),
                "--contract",
                str(CONTRACT),
                "--now",
                NOW,
                "--output",
                str(no_health_report),
            ]
        )
        report = verifier.load_json(no_health_report)
        require(code == 0 and report["accepted"] is True, "snapshot-only validation did not pass")
        require(report["command_eligible"] is False, "missing health was declared command eligible")
        require(report["blockers"] == [], "missing optional health polluted snapshot blockers")
        require("health_not_provided" in report["command_blockers"], "missing health blocker was omitted")

        wrong_health = trusted_health(snapshot)
        wrong_health["source"]["state_hash"] = "sha256:" + "b" * 64
        write_json(health_path, wrong_health)
        negative_report = root / "negative-health-report.json"
        code = invoke(
            [
                "--snapshot",
                str(snapshot_path),
                "--health",
                str(health_path),
                "--contract",
                str(CONTRACT),
                "--now",
                NOW,
                "--output",
                str(negative_report),
            ]
        )
        report = verifier.load_json(negative_report)
        require(code == 1 and report["accepted"] is True, "health mismatch polluted snapshot acceptance")
        require(report["command_eligible"] is False, "mismatched health source became command eligible")
        require(
            "health_source_state_hash_mismatch" in report["health_blockers"],
            "mismatched health hash blocker was omitted",
        )

        incomplete = copy.deepcopy(snapshot)
        del incomplete["snapshot_seq"]
        incomplete_path = root / "incomplete-state.json"
        incomplete_text = json.dumps(incomplete, ensure_ascii=False, indent=2) + "\n"
        incomplete_path.write_text(incomplete_text, encoding="utf-8")
        incomplete_report = root / "incomplete-report.json"
        code = invoke(
            [
                "--snapshot",
                str(incomplete_path),
                "--contract",
                str(CONTRACT),
                "--now",
                NOW,
                "--output",
                str(incomplete_report),
            ]
        )
        report = verifier.load_json(incomplete_report)
        require(code == 1 and report["accepted"] is False, "missing snapshot field was default-filled")
        require("required_missing:snapshot_seq" in report["snapshot_blockers"], "missing field blocker was omitted")
        require(incomplete_path.read_text(encoding="utf-8") == incomplete_text, "validator mutated the snapshot input")

        def require_snapshot_rejected(
            label: str,
            candidate: Dict[str, Any],
            expected_blocker: str,
        ) -> None:
            candidate_path = root / f"{label}.json"
            candidate_report = root / f"{label}-report.json"
            write_json(candidate_path, candidate)
            result = invoke(
                [
                    "--snapshot",
                    str(candidate_path),
                    "--contract",
                    str(CONTRACT),
                    "--now",
                    NOW,
                    "--output",
                    str(candidate_report),
                ]
            )
            rejected = verifier.load_json(candidate_report)
            require(result == 1 and rejected["accepted"] is False, f"{label} was accepted")
            require(
                expected_blocker in rejected["snapshot_blockers"],
                f"{label} omitted blocker {expected_blocker}: {rejected['snapshot_blockers']}",
            )

        wrong_alert_contract = copy.deepcopy(snapshot)
        wrong_alert_contract["quality"]["alerts"]["contract_id"] = "oni.qc.alerts.fake.v1"
        require_snapshot_rejected(
            "wrong-alert-contract",
            wrong_alert_contract,
            "quality_contract_id_mismatch:alerts",
        )

        wrong_idle_semantic = copy.deepcopy(snapshot)
        wrong_idle_semantic["quality"]["duplicants.idle"]["semantic"] = "display_idle_count"
        require_snapshot_rejected(
            "wrong-idle-semantic",
            wrong_idle_semantic,
            "quality_semantic_mismatch:duplicants.idle",
        )

        malformed_alert = copy.deepcopy(snapshot)
        malformed_alert["alerts"] = ["low oxygen"]
        require_snapshot_rejected("malformed-alert", malformed_alert, "item_invalid:alerts")

        unknown_alert = copy.deepcopy(snapshot)
        unknown_alert["alerts"] = ["unknown_hazard"]
        require_snapshot_rejected("unknown-alert", unknown_alert, "item_not_allowed:alerts")

        duplicate_alert = copy.deepcopy(snapshot)
        duplicate_alert["alerts"] = ["low_oxygen", "low_oxygen"]
        require_snapshot_rejected("duplicate-alert", duplicate_alert, "list_duplicates:alerts")

        unsorted_alert = copy.deepcopy(snapshot)
        unsorted_alert["alerts"] = ["starvation", "low_oxygen"]
        require_snapshot_rejected("unsorted-alert", unsorted_alert, "list_unsorted:alerts")

        oversized_alerts = copy.deepcopy(snapshot)
        oversized_alerts["alerts"] = [f"alert_{index:03d}" for index in range(129)]
        require_snapshot_rejected("oversized-alerts", oversized_alerts, "list_too_long:alerts")

    print(
        "PASS live telemetry contract: trusted, health-optional, identity mismatch, "
        "no-default, strict quality metadata, canonical alert list cases"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
