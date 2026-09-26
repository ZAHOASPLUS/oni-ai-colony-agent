#!/usr/bin/env python3
"""Audit the complete V2.7 evidence set instead of inferring completion."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import io
import json
import pathlib
import re
import subprocess
import sys
from typing import Any, Dict, List, Optional


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.advanced_actions import load_catalog
from agent.command_queue import load_policy
from scripts.manage_save_lineage import verify_lineage
from scripts.verify_runtime import ALL_MARKERS, REQUIRED_LINEAGE_STEPS


DEFAULT_BRIDGE = pathlib.Path.home() / "Documents" / "Klei" / "OxygenNotIncluded" / "ai-bridge"
DEFAULT_INSTALLED_DLL = (
    pathlib.Path.home()
    / "Documents"
    / "Klei"
    / "OxygenNotIncluded"
    / "mods"
    / "Dev"
    / "oni-ai-colony-agent"
    / "OniAiProbeMod.dll"
)
EXPECTED_ACTION_IDS = {
    "set_duplicant_chore_priority",
    "mark_dig_cells",
    "cancel_dig_cells",
    "place_building_blueprint",
    "cancel_building_blueprint",
    "place_utility_blueprint_path",
    "cancel_utility_blueprint_path",
    "create_colony_plan",
    "advance_colony_plan",
    "pause_colony_plan",
    "resume_colony_plan",
    "abort_colony_plan",
    "recover_colony_plan",
}
REQUIRED_REPORTS = {
    "advanced_actions": ("reports/advanced-actions.json", "status", "passed"),
    "colony_plan_static": ("reports/colony-plan-verification.json", "status", "passed"),
    "colony_plan_live": ("reports/colony-plan-live.json", "status", "passed"),
    "command_safety": ("reports/command-safety.json", "status", "passed"),
    "bridge_reliability": ("reports/bridge-reliability.json", "status", "passed"),
    "llm_fallback": ("reports/llm-fallback.json", "status", "passed"),
    "runtime_final": ("reports/runtime-acceptance.json", "status", "passed"),
    "save_lineage_static": ("reports/save-lineage-verification.json", "status", "passed"),
    "v27_workflow_static": ("reports/v27-workflow-verification.json", "status", "passed"),
}
EXPECTED_POSITIVE_SCENARIO_IDS = {
    "oni_ai_colony_agent_static_probe",
    "oni_ai_command_queue_mock",
    "oni_runtime_blueprints",
    "oni_runtime_colony_plan",
    "oni_runtime_command_safety",
    "oni_runtime_dig_designations",
    "oni_runtime_duplicant_priority",
    "oni_runtime_observer",
    "oni_runtime_pause",
    "oni_runtime_probe_performance",
    "oni_runtime_research",
}
KNOWN_FAILURE_SCENARIO_ID = "oni_known_behavior_failure"


def load_json(path: pathlib.Path) -> Dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root is not an object: {path}")
    return value


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_utf8_lf(path: pathlib.Path) -> str:
    text = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_timestamp(value: Any) -> Optional[dt.datetime]:
    try:
        parsed = dt.datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def check(name: str, passed: bool, evidence: Any, *, required: bool = True) -> Dict[str, Any]:
    return {
        "name": name,
        "status": "passed" if passed else "failed" if required else "not_run",
        "required": required,
        "evidence": evidence,
    }


def process_evidence() -> Dict[str, Any]:
    if sys.platform != "win32":
        return {"supported": False, "oni": [], "bridge": []}
    process = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-Command",
            "$OutputEncoding=[Console]::OutputEncoding=[Text.UTF8Encoding]::new(); Get-CimInstance Win32_Process | Select-Object Name,CommandLine | ConvertTo-Csv -NoTypeInformation",
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=20,
        check=False,
    )
    if process.returncode != 0:
        return {"supported": False, "error": (process.stderr or process.stdout)[-500:], "oni": [], "bridge": []}
    rows = list(csv.DictReader(io.StringIO(process.stdout)))
    oni = [row for row in rows if str(row.get("Name") or "").lower() == "oxygennotincluded.exe"]
    bridge = [row for row in rows if "oni_bridge.py" in str(row.get("CommandLine") or "")]
    return {"supported": True, "oni": oni, "bridge": bridge}


def audit(args: argparse.Namespace) -> Dict[str, Any]:
    results: List[Dict[str, Any]] = []
    runtime_report: Dict[str, Any] = {}
    runtime_report_path = PROJECT_ROOT / REQUIRED_REPORTS["runtime_final"][0]
    catalog = load_catalog()
    actions = [item for item in catalog.get("actions") or [] if isinstance(item, dict)]
    ids = {str(item.get("id")) for item in actions}
    catalog_ok = (
        catalog.get("catalog_complete") is True
        and ids == EXPECTED_ACTION_IDS
        and all(item.get("status") == "verified" for item in actions)
        and all(not value for value in (catalog.get("batch_blockers") or {}).values())
    )
    results.append(
        check(
            "catalog_complete",
            catalog_ok,
            {
                "action_count": len(actions),
                "verified_count": sum(item.get("status") == "verified" for item in actions),
                "blockers": catalog.get("batch_blockers"),
            },
        )
    )

    for name, (relative, field, expected) in REQUIRED_REPORTS.items():
        path = PROJECT_ROOT / relative
        try:
            report = load_json(path)
            passed = report.get(field) == expected
            evidence = {"path": str(path), field: report.get(field)}
            if name == "runtime_final":
                runtime_report = report
                checks = report.get("checks") if isinstance(report.get("checks"), dict) else {}
                passed = (
                    passed
                    and report.get("phase") == "final"
                    and report.get("current_mod_version") == args.mod_version
                    and report.get("required_checks") == list(ALL_MARKERS)
                    and isinstance(checks.get("save_lineage_runtime"), dict)
                    and checks["save_lineage_runtime"].get("status") == "passed"
                    and isinstance(checks.get("colony_plan_runtime"), dict)
                    and checks["colony_plan_runtime"].get("status") == "passed"
                )
                evidence.update(
                    {
                        "phase": report.get("phase"),
                        "session_id": report.get("current_session_id"),
                        "mod_version": report.get("current_mod_version"),
                        "required_checks": report.get("required_checks"),
                        "save_lineage_test_run_id": report.get("save_lineage_test_run_id"),
                    }
                )
            if name == "colony_plan_live":
                passed = passed and report.get("game_command_executed") is False
                evidence["game_command_executed"] = report.get("game_command_executed")
        except Exception as exc:
            passed = False
            evidence = {"path": str(path), "error": str(exc)}
        results.append(check(name, passed, evidence))

    positive_path = PROJECT_ROOT / "reports" / "game-verify" / args.positive_run / "results.json"
    try:
        positive = load_json(positive_path)
        summary = positive.get("summary") if isinstance(positive.get("summary"), dict) else {}
        positive_results = positive.get("results") if isinstance(positive.get("results"), list) else []
        positive_ids = {
            str(item.get("id") or "")
            for item in positive_results
            if isinstance(item, dict) and item.get("status") == "pass"
        }
        runtime_generated_at = parse_timestamp(runtime_report.get("generated_at"))
        positive_finished_at = parse_timestamp(positive.get("finished_at"))
        positive_ok = (
            summary.get("total") == args.positive_total == len(EXPECTED_POSITIVE_SCENARIO_IDS)
            and summary.get("by_status") == {"pass": args.positive_total}
            and len(positive_results) == args.positive_total
            and positive_ids == EXPECTED_POSITIVE_SCENARIO_IDS
            and runtime_generated_at is not None
            and positive_finished_at is not None
            and positive_finished_at >= runtime_generated_at
        )
        positive_evidence = {
            "path": str(positive_path),
            "summary": summary,
            "scenario_ids": sorted(positive_ids),
            "finished_at": positive.get("finished_at"),
            "runtime_generated_at": runtime_report.get("generated_at"),
        }
    except Exception as exc:
        positive_ok = False
        positive_evidence = {"path": str(positive_path), "error": str(exc)}
    results.append(check("game_verify_positive", positive_ok, positive_evidence))

    failure_path = PROJECT_ROOT / "reports" / "game-verify" / args.known_failure_run / "results.json"
    try:
        failure = load_json(failure_path)
        summary = failure.get("summary") if isinstance(failure.get("summary"), dict) else {}
        failure_results = failure.get("results") if isinstance(failure.get("results"), list) else []
        failure_result = failure_results[0] if len(failure_results) == 1 and isinstance(failure_results[0], dict) else {}
        failure_finished_at = parse_timestamp(failure.get("finished_at"))
        runtime_generated_at = parse_timestamp(runtime_report.get("generated_at"))
        failure_ok = (
            summary.get("total") == 1
            and summary.get("by_status") == {"fail": 1}
            and summary.get("by_failure_category") == {"behavior_fail": 1}
            and failure_result.get("id") == KNOWN_FAILURE_SCENARIO_ID
            and failure_result.get("status") == "fail"
            and failure_result.get("failure_category") == "behavior_fail"
            and runtime_generated_at is not None
            and failure_finished_at is not None
            and failure_finished_at >= runtime_generated_at
        )
        failure_evidence = {
            "path": str(failure_path),
            "summary": summary,
            "scenario_id": failure_result.get("id"),
            "finished_at": failure.get("finished_at"),
            "runtime_generated_at": runtime_report.get("generated_at"),
        }
    except Exception as exc:
        failure_ok = False
        failure_evidence = {"path": str(failure_path), "error": str(exc)}
    results.append(check("known_behavior_failure", failure_ok, failure_evidence))

    source_dll = PROJECT_ROOT / "mod" / "OniAiProbeMod.dll"
    installed_dll = args.installed_dll.expanduser().resolve()
    dll_paths = [source_dll, PROJECT_ROOT / "mod" / "bin" / "Release" / "OniAiProbeMod.dll", installed_dll]
    dll_hashes: Dict[str, Optional[str]] = {}
    for path in dll_paths:
        dll_hashes[str(path)] = sha256(path) if path.exists() else None
    hash_values = list(dll_hashes.values())
    results.append(check("dll_hash_match", all(hash_values) and len(set(hash_values)) == 1, dll_hashes))

    mod_source = (PROJECT_ROOT / "mod" / "Source" / "Mod.cs").read_text(encoding="utf-8")
    version_match = re.search(r'ModVersion\s*=\s*"([^"]+)"', mod_source)
    results.append(
        check(
            "mod_version_source",
            version_match is not None and version_match.group(1) == args.mod_version,
            {"expected": args.mod_version, "observed": None if version_match is None else version_match.group(1)},
        )
    )

    policy = load_policy(args.bridge_dir.expanduser().resolve())
    results.append(
        check(
            "action_policy_disabled",
            policy.get("schema") == "oni.action-policy.v1"
            and policy.get("actions_enabled") is False
            and policy.get("suggestions_enabled") is False
            and policy.get("allowed_save_hashes") == [],
            {
                "schema": policy.get("schema"),
                "actions_enabled": policy.get("actions_enabled"),
                "suggestions_enabled": policy.get("suggestions_enabled"),
                "allowed_save_hash_count": len(policy.get("allowed_save_hashes") or []),
            },
        )
    )

    lineage_path = args.save_lineage.expanduser().resolve()
    required_lineage_steps = tuple(args.required_lineage_step or REQUIRED_LINEAGE_STEPS)
    minimum_lineage_checkpoints = max(args.minimum_lineage_checkpoints, len(REQUIRED_LINEAGE_STEPS))
    lineage_report = verify_lineage(
        lineage_path,
        args.bridge_dir.expanduser().resolve(),
        expected_mod_version=args.mod_version,
        required_steps=required_lineage_steps,
        required_step_order=required_lineage_steps,
        minimum_checkpoints=minimum_lineage_checkpoints,
        verify_live=not args.require_clean_processes,
    )
    results.append(
        check(
            "save_lineage_complete",
            lineage_report.get("status") == "passed",
            {
                "manifest": str(lineage_path),
                "required_steps": required_lineage_steps,
                "minimum_checkpoints": minimum_lineage_checkpoints,
                "verification_mode": "offline_after_shutdown" if args.require_clean_processes else "live",
                "verification": lineage_report,
            },
        )
    )

    try:
        lineage_manifest = load_json(lineage_path)
        save_path = pathlib.Path(str(lineage_manifest.get("save_path") or "")).expanduser().resolve()
        latest = lineage_manifest.get("latest") if isinstance(lineage_manifest.get("latest"), dict) else {}
        disk_hash = "sha256:" + sha256(save_path)
        disk_tip_ok = save_path.is_file() and disk_hash == latest.get("save_id_hash")
        disk_tip_evidence = {
            "save_path": str(save_path),
            "disk_hash": disk_hash,
            "manifest_tip": latest.get("save_id_hash"),
        }
    except Exception as exc:
        disk_tip_ok = False
        disk_tip_evidence = {"error": str(exc)}
    results.append(check("save_lineage_disk_tip", disk_tip_ok, disk_tip_evidence))

    try:
        lineage_manifest = load_json(lineage_path)
        runtime_digest = "sha256:" + sha256(runtime_report_path)
        marker_results = []
        for scenario in ALL_MARKERS:
            marker_path = PROJECT_ROOT / "reports" / "runtime-markers" / f"{scenario}.passed"
            marker = load_json(marker_path)
            marker_ok = (
                marker.get("schema") == "oni.runtime-marker.v2"
                and marker.get("scenario") == scenario
                and marker.get("status") == "passed"
                and marker.get("test_run_id") == lineage_manifest.get("test_run_id")
                and marker.get("lineage_checkpoint_hash") == lineage_manifest.get("latest_checkpoint_hash")
                and marker.get("runtime_report_sha256") == runtime_digest
                and marker.get("current_save_id_hash") == lineage_manifest.get("latest", {}).get("save_id_hash")
                and marker.get("generated_at") == runtime_report.get("generated_at")
            )
            marker_results.append({"scenario": scenario, "status": "passed" if marker_ok else "failed"})
        markers_ok = (
            runtime_report.get("save_lineage_test_run_id") == lineage_manifest.get("test_run_id")
            and runtime_report.get("save_lineage_checkpoint_hash") == lineage_manifest.get("latest_checkpoint_hash")
            and all(item["status"] == "passed" for item in marker_results)
        )
        marker_evidence = {
            "runtime_report_sha256": runtime_digest,
            "test_run_id": lineage_manifest.get("test_run_id"),
            "latest_checkpoint_hash": lineage_manifest.get("latest_checkpoint_hash"),
            "markers": marker_results,
        }
    except Exception as exc:
        markers_ok = False
        marker_evidence = {"error": str(exc)}
    results.append(check("runtime_markers_bound", markers_ok, marker_evidence))

    delegation_path = PROJECT_ROOT / "docs" / "cli-v27-20260711" / "delegation.json"
    try:
        delegation = load_json(delegation_path)
        work_packages = delegation.get("work_packages") if isinstance(delegation.get("work_packages"), dict) else {}
        tasks = delegation.get("tasks") if isinstance(delegation.get("tasks"), list) else []
        accepted_tasks = []
        for task in tasks:
            if not isinstance(task, dict) or task.get("review_state") != "accepted":
                continue
            result_value = task.get("result")
            expected_hash = task.get("result_sha256")
            if not isinstance(result_value, str) or not isinstance(expected_hash, str):
                continue
            result_path = delegation_path.parent / result_value
            if result_path.is_file() and "sha256:" + sha256_utf8_lf(result_path) == expected_hash:
                accepted_tasks.append(str(task.get("id") or ""))
        total = int(work_packages.get("remaining_total_at_delegation") or 0)
        completed_fraction = (len(accepted_tasks) / total) if total > 0 else 0.0
        delegation_ok = (
            delegation.get("schema") == "oni.cli-delegation.v1"
            and delegation.get("status") == "passed"
            and delegation.get("result_hash_mode") == "utf8_lf"
            and completed_fraction >= 0.4
            and abs(float(work_packages.get("completed_fraction") or 0.0) - completed_fraction) < 0.000001
        )
        delegation_evidence = {
            "path": str(delegation_path),
            "status": delegation.get("status"),
            "accepted_tasks": accepted_tasks,
            "remaining_total_at_delegation": total,
            "completed_fraction": completed_fraction,
        }
    except Exception as exc:
        delegation_ok = False
        delegation_evidence = {"path": str(delegation_path), "error": str(exc)}
    results.append(check("cli_delegation_40_percent", delegation_ok, delegation_evidence))

    processes = process_evidence()
    clean_supported = processes.get("supported") is True
    clean = clean_supported and not processes.get("oni") and not processes.get("bridge")
    results.append(check("runtime_processes_stopped", clean, processes, required=args.require_clean_processes))

    passed = all(item["status"] == "passed" for item in results if item["required"])
    return {
        "schema": "oni.v27-completion-audit.v2",
        "status": "passed" if passed else "incomplete_or_failed",
        "classification": None if passed else "behavior_fail",
        "checks": results,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bridge-dir", type=pathlib.Path, default=DEFAULT_BRIDGE)
    parser.add_argument("--installed-dll", type=pathlib.Path, default=DEFAULT_INSTALLED_DLL)
    parser.add_argument("--mod-version", default="0.7.0")
    parser.add_argument("--positive-run", default="v27-final-positive-20260711")
    parser.add_argument("--positive-total", type=int, default=11)
    parser.add_argument("--known-failure-run", default="v27-final-known-failure-20260711")
    parser.add_argument("--save-lineage", type=pathlib.Path, required=True)
    parser.add_argument("--required-lineage-step", action="append", default=[])
    parser.add_argument("--minimum-lineage-checkpoints", type=int, default=len(REQUIRED_LINEAGE_STEPS))
    parser.add_argument("--require-clean-processes", action="store_true")
    parser.add_argument("--report", type=pathlib.Path, default=PROJECT_ROOT / "reports" / "v27-completion-audit.json")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = audit(args)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for item in report["checks"]:
        print(f"[{item['status'].upper()}] {item['name']}")
    print(f"Report: {args.report}")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
