#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import itertools
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Dict, List


ROOT = pathlib.Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "bridge" / "oni_bridge.py"
MANIFEST = ROOT / "scenarios" / "scenario_manifest.json"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.field_contracts import (
    ALERTS_CANONICAL_CONTRACT_ID,
    BUILDING_COMPLETED_COUNT_CONTRACT_ID,
    CANONICAL_ALERT_IDS,
    DUPLICANTS_IDLE_CONTRACT_ID,
    DUPLICANTS_STRESSED_CONTRACT_ID,
    DUPLICANTS_TOTAL_CONTRACT_ID,
    GAME_PAUSED_CONTRACT_ID,
    REGISTERED_FOOD_CONTRACT_ID,
    RESEARCH_CURRENT_CONTRACT_ID,
    contract_by_id,
    contract_quality_fixture,
)
from agent.incidents import IncidentTracker
from agent import rules as agent_rules


_RULE_FIXTURE_CONTRACTS = {
    "game.paused": GAME_PAUSED_CONTRACT_ID,
    "duplicants.total": DUPLICANTS_TOTAL_CONTRACT_ID,
    "duplicants.idle": DUPLICANTS_IDLE_CONTRACT_ID,
    "duplicants.stressed": DUPLICANTS_STRESSED_CONTRACT_ID,
    "resources.food_kcal": REGISTERED_FOOD_CONTRACT_ID,
    "alerts": ALERTS_CANONICAL_CONTRACT_ID,
    "research.current": RESEARCH_CURRENT_CONTRACT_ID,
    "buildings.oxygen_diffuser": BUILDING_COMPLETED_COUNT_CONTRACT_ID,
    "buildings.outhouse": BUILDING_COMPLETED_COUNT_CONTRACT_ID,
    "buildings.manual_generator": BUILDING_COMPLETED_COUNT_CONTRACT_ID,
}


def _upgrade_legacy_rule_quality(record: Dict[str, Any]) -> None:
    """Certify only legacy offline Mock entries whose status a contract allows."""

    quality = record.get("quality")
    if not isinstance(quality, dict):
        return
    upgraded_building = False
    for path, contract_id in _RULE_FIXTURE_CONTRACTS.items():
        entry = quality.get(path)
        if not isinstance(entry, dict) or entry.get("source") != "fixture":
            continue
        status = entry.get("status")
        contract = contract_by_id(contract_id)
        if not isinstance(status, str) or contract is None or status not in contract.statuses:
            continue
        quality[path] = contract_quality_fixture(contract_id, status)
        upgraded_building = upgraded_building or path.startswith("buildings.")
    if upgraded_building:
        buildings = record.get("buildings")
        if isinstance(buildings, dict):
            buildings["captured_at"] = record.get("ts")


def _upgrade_legacy_mock_identity(record: Dict[str, Any]) -> None:
    """Give throwaway replay fixtures a syntactically valid, stable save identity."""

    value = record.get("save_id_hash")
    if isinstance(value, str) and len(value) == 71 and value.startswith("sha256:"):
        try:
            int(value[7:], 16)
        except ValueError:
            pass
        else:
            return
    seed = str(value or record.get("session_id") or "offline-mock")
    record["save_id_hash"] = "sha256:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()


def run_bridge(input_path: pathlib.Path, output_dir: pathlib.Path) -> None:
    subprocess.run(
        [
            sys.executable,
            str(BRIDGE),
            "--bridge-dir",
            str(output_dir),
            "--input",
            str(input_path),
            "--allow-replay-suggestions",
            "--once",
        ],
        check=True,
        cwd=str(ROOT),
    )


def run_bridge_streaming(input_path: pathlib.Path, output_dir: pathlib.Path) -> None:
    from bridge.oni_bridge import ReplayState, write_outputs

    runtime = ReplayState(allow_untrusted_snapshot_baseline=True)
    for line in input_path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        runtime.process(json.loads(line))
        write_outputs(output_dir, runtime, replay_mode=True, allow_replay_suggestions=True)


def load_json(path: pathlib.Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def prepare_input(item: Dict[str, Any], temp_dir: pathlib.Path) -> pathlib.Path:
    if item.get("steps"):
        return prepare_step_input(item, temp_dir)

    source = ROOT / "scenarios" / item["input"]
    repeat = int(item.get("repeat", 1))
    records = [
        json.loads(line)
        for line in source.read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]
    if repeat <= 1:
        # Upgrade legacy source fixtures only in the throwaway Mock stream.
        # Production admission remains strict and has no missing-field
        # compatibility path.
        snapshot_sequence = 1
        for record in records:
            if record.get("event") == "state_snapshot" or record.get("schema") == "oni.state.v2":
                record["snapshot_seq"] = snapshot_sequence
                snapshot_sequence += 1
                _upgrade_legacy_rule_quality(record)
                _upgrade_legacy_mock_identity(record)
        output = temp_dir / "prepared-input.jsonl"
        output.write_text(
            "\n".join(json.dumps(record, ensure_ascii=False, separators=(",", ":")) for record in records) + "\n",
            encoding="utf-8",
        )
        return output

    if len(records) != 1:
        raise ValueError(f"Repeat scenarios require exactly one template record: {source}")

    template = records[0]
    timestamp = dt.datetime.fromisoformat(str(template["ts"]).replace("Z", "+00:00"))
    interval = float(item.get("repeat_interval_seconds", 5))
    start_seq = int(template.get("seq", 1))
    session_id = str(template.get("session_id") or "mock-session")
    output = temp_dir / "expanded-input.jsonl"
    lines: List[str] = []
    for index in range(repeat):
        record = json.loads(json.dumps(template))
        sequence = start_seq + index
        record["seq"] = sequence
        record["snapshot_seq"] = index + 1
        record["snapshot_id"] = f"{session_id}:{sequence}"
        record["ts"] = (timestamp + dt.timedelta(seconds=index * interval)).isoformat()
        _upgrade_legacy_rule_quality(record)
        _upgrade_legacy_mock_identity(record)
        lines.append(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return output


def prepare_step_input(item: Dict[str, Any], temp_dir: pathlib.Path) -> pathlib.Path:
    steps = item.get("steps") or []
    if not steps:
        raise ValueError("Step scenario must contain at least one step")

    first_source = ROOT / "scenarios" / steps[0]["input"]
    first_records = [json.loads(line) for line in first_source.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    if len(first_records) != 1:
        raise ValueError(f"Step fixtures require one template record: {first_source}")
    first = first_records[0]
    session_id = str(item.get("session_id") or first.get("session_id") or "mock-session")
    sequence = int(item.get("start_seq", first.get("seq", 1)))
    snapshot_sequence = int(item.get("start_snapshot_seq", 1))
    timestamp = dt.datetime.fromisoformat(str(first["ts"]).replace("Z", "+00:00"))
    lines: List[str] = []

    for step in steps:
        source = ROOT / "scenarios" / step["input"]
        records = [json.loads(line) for line in source.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
        if len(records) != 1:
            raise ValueError(f"Step fixtures require one template record: {source}")
        template = records[0]
        repeat = max(1, int(step.get("repeat", 1)))
        interval = float(step.get("repeat_interval_seconds", 5))
        for _ in range(repeat):
            record = json.loads(json.dumps(template))
            record["session_id"] = session_id
            record["seq"] = sequence
            record["snapshot_seq"] = snapshot_sequence
            record["snapshot_id"] = f"{session_id}:{sequence}"
            record["ts"] = timestamp.isoformat()
            _upgrade_legacy_rule_quality(record)
            _upgrade_legacy_mock_identity(record)
            lines.append(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
            sequence += 1
            snapshot_sequence += 1
            timestamp += dt.timedelta(seconds=interval)

    output = temp_dir / "expanded-steps.jsonl"
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return output


def verify_scenario(item: Dict[str, Any], keep_outputs: bool = False) -> Dict[str, Any]:
    temp_dir = pathlib.Path(tempfile.mkdtemp(prefix="oni-ai-scenario-"))
    try:
        input_path = prepare_input(item, temp_dir)
        if item.get("stream_outputs"):
            run_bridge_streaming(input_path, temp_dir)
        else:
            run_bridge(input_path, temp_dir)
        advice = load_json(temp_dir / "advice.json")
        latest = load_json(temp_dir / "latest_state.json")
        plan = load_json(temp_dir / "plan.json")
        incidents = load_json(temp_dir / "incidents.json")
        health = load_json(temp_dir / "health.json")
        summary = (temp_dir / "state_summary.md").read_text(encoding="utf-8")
        pending_dir = temp_dir / "commands" / "pending"
        commands = [load_json(path) for path in sorted(pending_dir.glob("*.json"))] if pending_dir.exists() else []
        command_dir_payloads = {
            name: [load_json(path) for path in sorted((temp_dir / "commands" / name).glob("*.json"))]
            for name in ("pending", "approved", "done", "failed", "rejected", "quarantine")
        }

        advice_titles = [entry.get("title") for entry in advice.get("items", [])]
        command_actions = [entry.get("action") for entry in commands]
        incident_statuses = {
            str(entry.get("incident_id")): entry.get("status")
            for entry in incidents.get("items", [])
            if entry.get("incident_id")
        }
        timeline_events = [entry.get("event") for entry in incidents.get("timeline", [])]

        missing_advice = [title for title in item.get("expect_advice", []) if title not in advice_titles]
        forbidden_advice = [title for title in item.get("forbid_advice", []) if title in advice_titles]
        missing_actions = [action for action in item.get("expect_commands", []) if action not in command_actions]
        forbidden_actions = [action for action in item.get("forbid_commands", []) if action in command_actions]
        missing_summary = [text for text in item.get("expect_summary_contains", []) if text not in summary]
        wrong_incidents = {
            incident_id: {
                "expected": expected,
                "actual": incident_statuses.get(incident_id),
            }
            for incident_id, expected in item.get("expect_incident_status", {}).items()
            if incident_statuses.get(incident_id) != expected
        }
        forbidden_incidents = {
            incident_id: forbidden
            for incident_id, forbidden in item.get("forbid_incident_status", {}).items()
            if incident_statuses.get(incident_id) == forbidden
        }
        missing_timeline_events = [
            event for event in item.get("expect_timeline_events", []) if event not in timeline_events
        ]
        wrong_command_dir_counts = {
            name: {"expected": expected, "actual": len(command_dir_payloads.get(name, []))}
            for name, expected in item.get("expect_command_dir_counts", {}).items()
            if len(command_dir_payloads.get(name, [])) != expected
        }
        rejection_reasons = [
            command.get("rejection_reason")
            for command in command_dir_payloads["rejected"]
        ]
        missing_rejection_reasons = [
            reason for reason in item.get("expect_rejection_reasons", []) if reason not in rejection_reasons
        ]
        expected_health = item.get("expect_health_status")
        wrong_health = expected_health is not None and health.get("status") != expected_health
        expected_schema = item.get("expect_latest_schema", "oni.state.v2")
        wrong_schema = latest.get("schema") != expected_schema

        ok = not (
            missing_advice
            or forbidden_advice
            or missing_actions
            or forbidden_actions
            or missing_summary
            or wrong_incidents
            or forbidden_incidents
            or missing_timeline_events
            or wrong_command_dir_counts
            or missing_rejection_reasons
            or wrong_health
            or wrong_schema
        )
        return {
            "name": item["name"],
            "ok": ok,
            "latest_cycle": latest.get("cycle"),
            "advice_titles": advice_titles,
            "command_actions": command_actions,
            "incident_statuses": incident_statuses,
            "health_status": health.get("status"),
            "plan_mode": plan.get("mode"),
            "missing_advice": missing_advice,
            "forbidden_advice": forbidden_advice,
            "missing_actions": missing_actions,
            "forbidden_actions": forbidden_actions,
            "missing_summary": missing_summary,
            "wrong_incidents": wrong_incidents,
            "forbidden_incidents": forbidden_incidents,
            "timeline_events": timeline_events,
            "missing_timeline_events": missing_timeline_events,
            "wrong_command_dir_counts": wrong_command_dir_counts,
            "rejection_reasons": rejection_reasons,
            "missing_rejection_reasons": missing_rejection_reasons,
            "wrong_health": wrong_health,
            "wrong_schema": wrong_schema,
            "output_dir": str(temp_dir) if keep_outputs else None,
        }
    finally:
        if not keep_outputs:
            shutil.rmtree(temp_dir, ignore_errors=True)


def verify_static() -> Dict[str, Any]:
    required = [
        ROOT / "mod" / "OniAiProbeMod.csproj",
        ROOT / "mod" / "mod.yaml",
        ROOT / "mod" / "mod_info.yaml",
        ROOT / "mod" / "Source" / "Commands" / "ActionExecutor.cs",
        ROOT / "mod" / "Source" / "Probe" / "ValidationCheckpointController.cs",
        ROOT / "bridge" / "oni_bridge.py",
        ROOT / "agent" / "rules.py",
        ROOT / "agent" / "incidents.py",
        ROOT / "agent" / "state_quality.py",
        ROOT / "agent" / "command_queue.py",
        ROOT / "agent" / "llm_planner.py",
        ROOT / "agent" / "advanced_actions.py",
        ROOT / "agent" / "colony_plan.py",
        ROOT / "config" / "action-policy.example.json",
        ROOT / "config" / "validation-control-policy.example.json",
        ROOT / "config" / "advanced-actions.v1.json",
        ROOT / "scripts" / "verify_advanced_actions.py",
        ROOT / "scripts" / "manage_colony_plan.py",
        ROOT / "scripts" / "verify_colony_plan.py",
        ROOT / "scripts" / "verify_v27_completion.py",
        ROOT / "scripts" / "manage_validation_checkpoint.py",
        ROOT / "scripts" / "verify_validation_checkpoint.py",
    ]
    missing = [str(path.relative_to(ROOT)) for path in required if not path.exists()]
    return {
        "name": "static_probe",
        "ok": not missing,
        "missing": missing,
    }


def _arbiter_state(
    alerts: List[str],
    *,
    paused: bool = False,
    timestamp: dt.datetime | None = None,
) -> Dict[str, Any]:
    observed_at = timestamp or dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    return {
        "schema": "oni.state.v2",
        "event": "state_snapshot",
        "ts": observed_at.isoformat(timespec="seconds"),
        "session_id": "offline-survival-arbiter",
        "seq": 100,
        "snapshot_seq": 100,
        "snapshot_id": "offline-survival-arbiter:100",
        "save_id_hash": "sha256:" + "a" * 64,
        "save_dirty": False,
        "world_id": 0,
        "alerts": sorted(alerts),
        "game": {"paused": paused},
        "bridge": {
            "stale": False,
            "replay_mode": False,
            "trust_ready": True,
            "trust_blockers": [],
        },
        "quality": {
            "alerts": contract_quality_fixture(ALERTS_CANONICAL_CONTRACT_ID),
            "game.paused": contract_quality_fixture(GAME_PAUSED_CONTRACT_ID),
        },
    }


def _arbiter_witness(state: Dict[str, Any], alert_id: str) -> Dict[str, Any]:
    policy = agent_rules.SURVIVAL_ALERT_POLICY[alert_id]
    samples = int(policy["samples"])
    duration = float(policy["duration"])
    last_seen = dt.datetime.fromisoformat(str(state["ts"]).replace("Z", "+00:00"))
    first_seen = last_seen - dt.timedelta(seconds=duration)
    return {
        "incident_id": alert_id,
        "status": "open",
        "risk": policy["risk"],
        "telemetry_state": "active",
        "session_id": state["session_id"],
        "snapshot_id": state["snapshot_id"],
        "first_seq": int(state["seq"]) - samples + 1,
        "last_seq": state["seq"],
        "first_snapshot_seq": int(state["snapshot_seq"]) - samples + 1,
        "last_snapshot_seq": state["snapshot_seq"],
        "consecutive_samples": samples,
        "first_seen": first_seen.isoformat(timespec="microseconds"),
        "last_seen": last_seen.isoformat(timespec="microseconds"),
    }


def verify_survival_emergency_arbiter() -> Dict[str, Any]:
    """Exhaust the bounded alert-to-pause authority before any live run."""

    failures: List[str] = []

    def check(condition: bool, message: str) -> None:
        if not condition:
            failures.append(message)

    expected_pause_order = [
        "suffocation",
        "incapacitated",
        "starvation",
        "bionic_offline",
        "hyperthermia",
        "hypothermia",
        "major_sickness",
        "low_oxygen",
        "bionic_low_battery",
    ]
    pause_ids = sorted(
        (
            alert_id
            for alert_id, item in agent_rules.SURVIVAL_ALERT_POLICY.items()
            if item.get("disposition") == "pause"
        ),
        key=lambda alert_id: int(agent_rules.SURVIVAL_ALERT_POLICY[alert_id]["rank"]),
    )
    observe_ids = sorted(
        alert_id
        for alert_id, item in agent_rules.SURVIVAL_ALERT_POLICY.items()
        if item.get("disposition") == "observe"
    )
    check(
        set(agent_rules.SURVIVAL_ALERT_POLICY) == set(CANONICAL_ALERT_IDS),
        "alert policy/catalog keyset drifted",
    )
    check(agent_rules.survival_alert_policy_blockers() == [], "alert policy is not closed and valid")
    check(pause_ids == expected_pause_order, "pause priority order drifted")
    check(len(observe_ids) == 5, "observe-only alert count drifted")

    for alert_id in pause_ids:
        snapshot = _arbiter_state([alert_id])
        result = agent_rules.arbitrate_survival_emergency(
            snapshot,
            [_arbiter_witness(snapshot, alert_id)],
        )
        check(result.get("status") == "selected", f"{alert_id} was not selected")
        check(result.get("selected_alert_id") == alert_id, f"{alert_id} selected the wrong alert")
        command = result.get("command") if isinstance(result.get("command"), dict) else {}
        check(command.get("action") == "set_paused", f"{alert_id} emitted the wrong action")
        check(command.get("args") == {"paused": True}, f"{alert_id} did not emit pause=true")

    for alert_id in observe_ids:
        snapshot = _arbiter_state([alert_id])
        result = agent_rules.arbitrate_survival_emergency(
            snapshot,
            [{"incident_id": alert_id, "status": "open"}],
        )
        check(result.get("status") == "none", f"observe-only {alert_id} acquired mutation authority")
        check(result.get("command") is None, f"observe-only {alert_id} emitted a command")

    all_pause_state = _arbiter_state(pause_ids)
    all_pause_witnesses = {
        alert_id: _arbiter_witness(all_pause_state, alert_id) for alert_id in pause_ids
    }
    permutation_count = 0
    for incident_order in itertools.permutations(pause_ids):
        permutation_count += 1
        result = agent_rules.arbitrate_survival_emergency(
            all_pause_state,
            [all_pause_witnesses[alert_id] for alert_id in incident_order],
        )
        if not (
            result.get("status") == "selected"
            and result.get("selected_alert_id") == "suffocation"
        ):
            failures.append("pause arbitration depended on open-incident order")
            break

    already_paused = agent_rules.arbitrate_survival_emergency(
        _arbiter_state(["suffocation"], paused=True),
        [],
    )
    check(already_paused.get("status") == "none", "paused game emitted another pause")
    check(already_paused.get("blockers") == ["already_contained"], "paused containment was not explicit")
    hysteresis = agent_rules.arbitrate_survival_emergency(
        _arbiter_state(["low_oxygen"]),
        [],
    )
    check(hysteresis.get("status") == "none", "unopened low oxygen incident was selected")
    check(hysteresis.get("blockers") == ["pending_hysteresis"], "hysteresis wait was not explicit")

    witness_state = _arbiter_state(["low_oxygen"])
    valid_witness = _arbiter_witness(witness_state, "low_oxygen")
    witness_tampers = {
        "one_sample_forged_open": lambda item: item.update(
            {
                "consecutive_samples": 1,
                "first_seq": item["last_seq"],
                "first_snapshot_seq": item["last_snapshot_seq"],
                "first_seen": item["last_seen"],
            }
        ),
        "six_samples": lambda item: item.update(
            {"consecutive_samples": 6, "first_snapshot_seq": item["last_snapshot_seq"] - 5}
        ),
        "short_duration": lambda item: item.__setitem__(
            "first_seen",
            (
                dt.datetime.fromisoformat(item["last_seen"])
                - dt.timedelta(seconds=29, microseconds=1)
            ).isoformat(timespec="microseconds"),
        ),
        "wrong_session": lambda item: item.__setitem__("session_id", "other-session"),
        "wrong_snapshot": lambda item: item.__setitem__("snapshot_id", "other-session:100"),
        "wrong_seq": lambda item: item.__setitem__("last_seq", item["last_seq"] - 1),
        "snapshot_sequence_gap": lambda item: item.__setitem__(
            "first_snapshot_seq", item["first_snapshot_seq"] - 1
        ),
        "inactive_telemetry": lambda item: item.__setitem__("telemetry_state", "inactive"),
        "wrong_risk": lambda item: item.__setitem__("risk", "low"),
    }
    for label, mutate in witness_tampers.items():
        forged = copy.deepcopy(valid_witness)
        mutate(forged)
        result = agent_rules.arbitrate_survival_emergency(witness_state, [forged])
        check(result.get("status") == "blocked", f"{label} witness did not fail closed")
        check(result.get("command") is None, f"{label} witness emitted a command")

    missing_direct = agent_rules.arbitrate_survival_emergency(
        _arbiter_state(["suffocation"]),
        [],
    )
    check(missing_direct.get("status") == "blocked", "missing immediate-hazard witness was accepted")
    priority_state = _arbiter_state(["low_oxygen", "suffocation"])
    lower_only = agent_rules.arbitrate_survival_emergency(
        priority_state,
        [_arbiter_witness(priority_state, "low_oxygen")],
    )
    check(lower_only.get("status") == "blocked", "missing higher-priority witness selected a lower hazard")

    malformed_inputs = {
        "duplicate_open_incident": (
            _arbiter_state(["suffocation"]),
            [
                _arbiter_witness(_arbiter_state(["suffocation"]), "suffocation"),
                _arbiter_witness(_arbiter_state(["suffocation"]), "suffocation"),
            ],
        ),
        "string_open_incidents": (_arbiter_state(["suffocation"]), "suffocation"),
        "missing_state": (None, []),
    }
    replay = _arbiter_state(["suffocation"])
    replay["bridge"]["replay_mode"] = True
    malformed_inputs["replay_state"] = (replay, [_arbiter_witness(replay, "suffocation")])
    stale = _arbiter_state(["suffocation"])
    stale["bridge"]["stale"] = True
    malformed_inputs["stale_state"] = (stale, [_arbiter_witness(stale, "suffocation")])
    untrusted = _arbiter_state(["suffocation"])
    untrusted["bridge"]["trust_ready"] = False
    malformed_inputs["untrusted_state"] = (untrusted, [_arbiter_witness(untrusted, "suffocation")])
    bad_alerts = _arbiter_state(["suffocation"])
    bad_alerts["alerts"] = ["unknown_hazard"]
    malformed_inputs["bad_alert_contract"] = (bad_alerts, [_arbiter_witness(bad_alerts, "suffocation")])
    bad_paused = _arbiter_state(["suffocation"])
    del bad_paused["quality"]["game.paused"]
    malformed_inputs["bad_paused_contract"] = (bad_paused, [_arbiter_witness(bad_paused, "suffocation")])
    for label, (snapshot, open_ids) in malformed_inputs.items():
        result = agent_rules.arbitrate_survival_emergency(snapshot, open_ids)  # type: ignore[arg-type]
        check(result.get("status") == "blocked", f"{label} did not fail closed")
        check(result.get("command") is None, f"{label} emitted a command")

    policy_copy = copy.deepcopy(agent_rules.SURVIVAL_ALERT_POLICY)
    try:
        agent_rules.SURVIVAL_ALERT_POLICY["incapacitated"]["rank"] = 0
        check(
            "alert_policy_rank_duplicate" in agent_rules.survival_alert_policy_blockers(),
            "duplicate policy rank was accepted",
        )
        del agent_rules.SURVIVAL_ALERT_POLICY["wounded"]
        check(
            "canonical_alert_policy_not_closed" in agent_rules.survival_alert_policy_blockers(),
            "incomplete alert policy was accepted",
        )
    finally:
        agent_rules.SURVIVAL_ALERT_POLICY.clear()
        agent_rules.SURVIVAL_ALERT_POLICY.update(policy_copy)

    base_time = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    low_tracker = IncidentTracker()
    for index in range(6):
        snapshot = _arbiter_state(["low_oxygen"], timestamp=base_time + dt.timedelta(seconds=index * 5))
        snapshot["seq"] = index + 1
        snapshot["snapshot_seq"] = index + 1
        snapshot["snapshot_id"] = f"offline-survival-arbiter:{index + 1}"
        low_signal = next(
            signal for signal in agent_rules.evaluate_signals(snapshot)
            if signal.get("incident_id") == "low_oxygen"
        )
        low_tracker.update(snapshot, [low_signal])
    check(low_tracker.incidents["low_oxygen"]["status"] == "pending", "low oxygen opened before 7 samples/30s")
    snapshot = _arbiter_state(["low_oxygen"], timestamp=base_time + dt.timedelta(seconds=30))
    snapshot.update({"seq": 7, "snapshot_seq": 7, "snapshot_id": "offline-survival-arbiter:7"})
    low_signal = next(
        signal for signal in agent_rules.evaluate_signals(snapshot)
        if signal.get("incident_id") == "low_oxygen"
    )
    low_tracker.update(snapshot, [low_signal])
    check(low_tracker.incidents["low_oxygen"]["status"] == "open", "low oxygen did not open at 7 samples/30s")

    battery_tracker = IncidentTracker()
    for index in range(2):
        snapshot = _arbiter_state(
            ["bionic_low_battery"],
            timestamp=base_time + dt.timedelta(seconds=index * 5),
        )
        snapshot.update(
            {
                "seq": index + 1,
                "snapshot_seq": index + 1,
                "snapshot_id": f"offline-survival-arbiter:battery:{index + 1}",
            }
        )
        signal = next(
            item for item in agent_rules.evaluate_signals(snapshot)
            if item.get("incident_id") == "bionic_low_battery"
        )
        battery_tracker.update(snapshot, [signal])
        expected_status = "pending" if index == 0 else "open"
        check(
            battery_tracker.incidents["bionic_low_battery"]["status"] == expected_status,
            f"bionic battery hysteresis step {index + 1} was wrong",
        )

    direct_tracker = IncidentTracker()
    direct_state = _arbiter_state(list(CANONICAL_ALERT_IDS))
    direct_tracker.update(direct_state, agent_rules.evaluate_signals(direct_state))
    check(direct_tracker.incidents["suffocation"]["status"] == "open", "direct suffocation did not open in one sample")
    advice = agent_rules.build_arbitrated_advice(direct_state, direct_tracker.open_incidents())
    mutating = [
        item for item in advice
        if isinstance(item.get("command"), dict) and item["command"].get("action") == "set_paused"
    ]
    check(len(mutating) == 1, "combined advice did not contain exactly one pause command")
    check(bool(advice) and advice[0].get("incident_id") == "suffocation", "winner was not first in advice")

    fractional_state = _arbiter_state(["suffocation"])
    fractional_state["ts"] = "2026-01-01T00:00:00.5339642+00:00"
    fractional_tracker = IncidentTracker()
    fractional_tracker.update(fractional_state, agent_rules.evaluate_signals(fractional_state))
    fractional_advice = agent_rules.build_arbitrated_advice(
        fractional_state,
        fractional_tracker.open_incidents(),
    )
    fractional_pause = [
        item for item in fractional_advice
        if isinstance(item.get("command"), dict) and item["command"].get("action") == "set_paused"
    ]
    check(len(fractional_pause) == 1, "fractional Probe timestamp suppressed an immediate pause")

    tampered_incidents = copy.deepcopy(direct_tracker.open_incidents())
    suffocation = next(item for item in tampered_incidents if item.get("incident_id") == "suffocation")
    suffocation["reason"] = "tampered"
    tampered_advice = agent_rules.build_arbitrated_advice(direct_state, tampered_incidents)
    check(
        all(
            not (isinstance(item.get("command"), dict) and item["command"].get("action") == "set_paused")
            for item in tampered_advice
        ),
        "tampered winning incident retained pause authority",
    )

    return {
        "name": "survival_emergency_arbiter",
        "ok": not failures,
        "pause_order": pause_ids,
        "observe_only": observe_ids,
        "permutations_checked": permutation_count,
        "hysteresis": {"low_oxygen_samples": 7, "bionic_low_battery_samples": 2},
        "witness_tampers": sorted(witness_tampers),
        "failures": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep-outputs", action="store_true")
    args = parser.parse_args()

    manifest = load_json(MANIFEST)
    results: List[Dict[str, Any]] = [verify_static(), verify_survival_emergency_arbiter()]
    for item in manifest["scenarios"]:
        results.append(verify_scenario(item, keep_outputs=args.keep_outputs))

    ok = all(item["ok"] for item in results)
    report = {
        "schema": "oni.verification.v1",
        "ok": ok,
        "results": results,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
