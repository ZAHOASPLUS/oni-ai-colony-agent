#!/usr/bin/env python3
"""Offline A08B3 checks for mixed survival and ordinary rule incidents."""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
from typing import Any, Dict, Iterable, List, Mapping, Sequence
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import rules, state_quality  # noqa: E402
from agent.approval_receipt import validate_approval_inputs  # noqa: E402
from agent.decision import validate_decision_frame  # noqa: E402
from agent.field_contracts import (  # noqa: E402
    ALERTS_CANONICAL_CONTRACT_ID,
    BUILDING_COMPLETED_COUNT_CONTRACT_ID,
    CANONICAL_ALERT_IDS,
    DUPLICANTS_IDLE_CONTRACT_ID,
    DUPLICANTS_STRESSED_CONTRACT_ID,
    DUPLICANTS_TOTAL_CONTRACT_ID,
    FUNCTIONAL_BUILDINGS_CONTRACT_ID,
    FUNCTIONAL_BUILDINGS_SCHEMA,
    GAME_PAUSED_CONTRACT_ID,
    REGISTERED_FOOD_CONTRACT_ID,
    RESEARCH_CURRENT_CONTRACT_ID,
    contract_quality_fixture,
)
from agent.incidents import IncidentTracker  # noqa: E402
from bridge import oni_bridge  # noqa: E402


PAUSE_ORDER = (
    "suffocation", "incapacitated", "starvation", "bionic_offline",
    "hyperthermia", "hypothermia", "major_sickness", "low_oxygen",
    "bionic_low_battery",
)
# The rules module is the only incident-catalog authority.  Keep the baseline
# and additive groups separate because older snapshots intentionally omit the
# optional functional-building field.
BASE_ORDINARY_IDS = tuple(rules.BASE_ORDINARY_RULE_INCIDENT_IDS)
OPTIONAL_ORDINARY_IDS = tuple(rules.OPTIONAL_ORDINARY_RULE_INCIDENT_IDS)
ORDINARY_IDS = tuple(rules.ORDINARY_RULE_INCIDENT_IDS)
BASE_EXPECTED_IDS = set(rules.BASE_RULE_INCIDENT_IDS)
EXPECTED_IDS = set(rules.RULE_INCIDENT_IDS)
SAVE_HASH = "sha256:" + "a" * 64
ASSERTIONS = 0


def check(condition: bool, message: str) -> None:
    global ASSERTIONS
    ASSERTIONS += 1
    if not condition:
        raise AssertionError(message)


def iso_at(base: dt.datetime, seconds: float) -> str:
    return (base + dt.timedelta(seconds=seconds)).isoformat(timespec="milliseconds")


def make_state(
    *,
    session_id: str = "a08b3-mixed",
    seq: int = 1,
    snapshot_seq: int | None = None,
    timestamp: str | None = None,
    alerts: Sequence[str] = (),
    paused: bool = False,
    idle: int = 0,
    stressed: int = 0,
    food_kcal: float = 100_000.0,
    research: str = "FarmingTech",
    buildings: Sequence[int] = (1, 1, 1),
    food_quality: bool = True,
) -> Dict[str, Any]:
    snapshot_ts = timestamp or "2026-01-01T00:00:00.000+00:00"
    oxygen_diffuser, outhouse, manual_generator = buildings
    quality: Dict[str, Any] = {
        "game.paused": contract_quality_fixture(GAME_PAUSED_CONTRACT_ID),
        "alerts": contract_quality_fixture(ALERTS_CANONICAL_CONTRACT_ID),
        "duplicants.total": contract_quality_fixture(DUPLICANTS_TOTAL_CONTRACT_ID),
        "duplicants.idle": contract_quality_fixture(DUPLICANTS_IDLE_CONTRACT_ID),
        "duplicants.stressed": contract_quality_fixture(DUPLICANTS_STRESSED_CONTRACT_ID),
        "resources.food_kcal": contract_quality_fixture(REGISTERED_FOOD_CONTRACT_ID),
        "research.current": contract_quality_fixture(RESEARCH_CURRENT_CONTRACT_ID),
        "buildings.oxygen_diffuser": contract_quality_fixture(BUILDING_COMPLETED_COUNT_CONTRACT_ID),
        "buildings.outhouse": contract_quality_fixture(BUILDING_COMPLETED_COUNT_CONTRACT_ID),
        "buildings.manual_generator": contract_quality_fixture(BUILDING_COMPLETED_COUNT_CONTRACT_ID),
    }
    if not food_quality:
        quality.pop("resources.food_kcal")
    return {
        "schema": "oni.state.v2",
        "event": "state_snapshot",
        "ts": snapshot_ts,
        "session_id": session_id,
        "seq": seq,
        "snapshot_seq": seq if snapshot_seq is None else snapshot_seq,
        "snapshot_id": f"{session_id}:{seq}",
        "game_build": "cl-test",
        "mod_version": "0.2.0-test",
        "save_id_hash": SAVE_HASH,
        "save_dirty": False,
        "world_id": 0,
        "observation_scope": {
            "active_world_id": 0,
            "populated_world_ids": [0],
            "covered_world_ids": [0],
            "complete_for_populated_worlds": True,
        },
        "cycle": 10,
        "game": {"paused": paused},
        "duplicants": {"total": 3, "stressed": stressed, "idle": idle},
        "resources": {
            "food_kcal": food_kcal,
            "oxygen_kg_estimate": 100.0,
            "water_kg_estimate": 1_000.0,
        },
        "alerts": sorted(alerts),
        "research": {"current": research, "complete": []},
        "buildings": {
            "oxygen_diffuser": oxygen_diffuser,
            "outhouse": outhouse,
            "manual_generator": manual_generator,
            "captured_at": snapshot_ts,
            "_scanned": 3,
            "_source": "test_fixture",
            "_sample_ids": ["OxygenDiffuser", "Outhouse", "ManualGenerator"],
            "_top_prefabs": [],
        },
        "quality": quality,
        "bridge": {
            "stale": False,
            "replay_mode": False,
            "trust_ready": True,
            "trust_blockers": [],
        },
        "sample": {"interval_seconds": 5.0, "duration_ms": 1.0},
        "writer": {
            "queue_depth": 0,
            "dropped_snapshot_count": 0,
            "fault_count": 0,
            "non_finite_numeric_count": 0,
        },
    }


def update(tracker: IncidentTracker, state: Dict[str, Any]) -> List[Dict[str, Any]]:
    signals = rules.evaluate_signals(state)
    tracker.update(state, signals)
    return signals


def with_functional_building_telemetry(state: Mapping[str, Any]) -> Dict[str, Any]:
    """Add a valid optional field so the complete additive catalog is emitted."""

    result = copy.deepcopy(dict(state))
    captured_at = str(result["ts"])
    result["functional_buildings"] = {
        "schema": FUNCTIONAL_BUILDINGS_SCHEMA,
        "captured_at": captured_at,
        "world_id": result["world_id"],
        "complete": True,
        "instances": [],
    }
    quality = result.setdefault("quality", {})
    if isinstance(quality, dict):
        quality["functional_buildings"] = contract_quality_fixture(
            FUNCTIONAL_BUILDINGS_CONTRACT_ID
        )
    return result


def records_by_id(items: Iterable[Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {
        str(item["incident_id"]): copy.deepcopy(dict(item))
        for item in items
        if isinstance(item, Mapping) and isinstance(item.get("incident_id"), str)
    }


def open_alert_records(
    alert_id: str,
    *,
    sample_count: int | None = None,
    duration: float | None = None,
) -> tuple[Dict[str, Any], List[Dict[str, Any]], List[str]]:
    policy = rules.SURVIVAL_ALERT_POLICY[alert_id]
    count = int(policy["samples"]) if sample_count is None else sample_count
    span = float(policy["duration"]) if duration is None else duration
    base = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    tracker = IncidentTracker()
    statuses: List[str] = []
    last_state: Dict[str, Any] = {}
    for index in range(count):
        offset = 0.0 if count <= 1 else span * index / (count - 1)
        state = make_state(
            session_id="a08b3-alert-" + alert_id,
            seq=index + 1,
            snapshot_seq=index + 1,
            timestamp=iso_at(base, offset),
            alerts=[alert_id],
        )
        signals = update(tracker, state)
        check(
            {item["incident_id"] for item in signals} == BASE_EXPECTED_IDS,
            f"evaluate_signals catalog drifted for {alert_id}",
        )
        statuses.append(str(tracker.incidents[alert_id]["status"]))
        last_state = state
    return last_state, [copy.deepcopy(item) for item in tracker.payload()["items"]], statuses


def with_ordinary_record(
    survival_items: Sequence[Mapping[str, Any]],
    ordinary: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    result = [copy.deepcopy(dict(item)) for item in survival_items]
    result = [item for item in result if item.get("incident_id") != ordinary.get("incident_id")]
    result.append(copy.deepcopy(dict(ordinary)))
    return result


def cert(state: Mapping[str, Any], items: Any) -> Dict[str, Any]:
    return rules.build_survival_arbitration_certificate(state, items)


def case_catalog_and_ordinary_statuses() -> Dict[str, Any]:
    catalog_blockers = rules.rule_incident_catalog_blockers()
    check(
        catalog_blockers == [],
        f"rule incident catalog is inconsistent: {catalog_blockers}",
    )
    check(
        tuple(CANONICAL_ALERT_IDS) == tuple(rules.CANONICAL_ALERT_IDS),
        "canonical alert catalog export drifted",
    )
    check(
        tuple(ORDINARY_IDS) == tuple(BASE_ORDINARY_IDS + OPTIONAL_ORDINARY_IDS),
        "ordinary catalog groups are not composed in order",
    )
    check(
        set(OPTIONAL_ORDINARY_IDS) == set(rules.FUNCTIONAL_BUILDING_INCIDENT_IDS),
        "optional ordinary catalog does not cover functional-building incidents",
    )
    base_state = make_state()
    signals = rules.evaluate_signals(base_state)
    actual_ids = [str(item["incident_id"]) for item in signals]
    check(
        len(actual_ids) == len(BASE_EXPECTED_IDS),
        "evaluate_signals did not emit the complete baseline rule catalog",
    )
    check(len(actual_ids) == len(set(actual_ids)), "evaluate_signals emitted duplicate IDs")
    check(
        set(actual_ids) == BASE_EXPECTED_IDS,
        "evaluate_signals baseline catalog differs from rules export",
    )

    tracker = IncidentTracker()
    update(tracker, base_state)
    initial = records_by_id(tracker.payload()["items"])
    check(
        set(initial) == BASE_EXPECTED_IDS,
        "initial tracker did not retain the complete baseline catalog",
    )

    functional_state = with_functional_building_telemetry(base_state)
    functional_signals = rules.evaluate_signals(functional_state)
    functional_ids = [str(item["incident_id"]) for item in functional_signals]
    check(
        len(functional_ids) == len(EXPECTED_IDS),
        "evaluate_signals did not emit the complete additive rule catalog",
    )
    check(
        len(functional_ids) == len(set(functional_ids)),
        "functional telemetry emitted duplicate rule IDs",
    )
    check(
        set(functional_ids) == EXPECTED_IDS,
        "functional telemetry catalog differs from rules export",
    )
    check(
        set(functional_ids) - set(actual_ids) == set(OPTIONAL_ORDINARY_IDS),
        "functional telemetry did not add exactly the optional ordinary incidents",
    )

    base = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    def step(seq: int, seconds: float, **changes: Any) -> Dict[str, Any]:
        state = make_state(
            session_id="a08b3-statuses",
            seq=seq,
            snapshot_seq=seq,
            timestamp=iso_at(base, seconds),
            food_quality=False,
            **changes,
        )
        update(tracker, state)
        return state

    step(2, 5.0, idle=1)
    step(3, 10.0, idle=1)
    step(4, 15.0, idle=0)
    resolved_idle = copy.deepcopy(tracker.incidents["idle_duplicants"])
    step(5, 20.0, buildings=(1, 0, 1))
    step(6, 25.0, buildings=(1, 1, 1))
    step(7, 30.0, buildings=(1, 0, 1))
    cooldown_outhouse = copy.deepcopy(tracker.incidents["missing_outhouse"])
    step(8, 35.0, stressed=1)
    pending_stressed = copy.deepcopy(tracker.incidents["stressed_duplicants"])
    final_status_state = step(9, 40.0, research="")
    open_research = copy.deepcopy(tracker.incidents["no_active_research"])
    blocked_food = copy.deepcopy(tracker.incidents["low_food"])
    status_records = {
        "inactive": initial["missing_manual_generator"],
        "resolved": resolved_idle,
        "pending": pending_stressed,
        "open": open_research,
        "blocked_unknown": blocked_food,
        "cooldown": cooldown_outhouse,
    }
    check(
        {name: item["status"] for name, item in status_records.items()}
        == {name: name for name in status_records},
        "IncidentTracker did not produce every ordinary status",
    )

    low_state, low_items, low_statuses = open_alert_records("low_oxygen")
    check(low_statuses[:6] == ["pending"] * 6, "low oxygen opened before seven samples")
    check(low_statuses[-1] == "open", "low oxygen did not open at seven samples/30 seconds")
    coexistence = {}
    for status, ordinary in status_records.items():
        result = cert(low_state, with_ordinary_record(low_items, ordinary))
        check(result["status"] == "selected", f"ordinary {status} blocked a valid low-oxygen witness")
        check(result["selected_incident_id"] == "low_oxygen", f"ordinary {status} changed selection")
        check(result["blockers"] == [], f"ordinary {status} added certificate blockers")
        coexistence[status] = result["status"]
    return {
        "actual_catalog_count": len(actual_ids),
        "additive_catalog_count": len(functional_ids),
        "ordinary_catalog": list(ORDINARY_IDS),
        "optional_ordinary_catalog": list(OPTIONAL_ORDINARY_IDS),
        "ordinary_statuses": {name: item["status"] for name, item in status_records.items()},
        "low_oxygen_statuses": low_statuses,
        "coexistence": coexistence,
        "final_status_session": final_status_state["session_id"],
    }


def case_pause_boundaries_and_ordinary_only() -> Dict[str, Any]:
    selected: Dict[str, str] = {}
    for alert_id in PAUSE_ORDER:
        state, items, statuses = open_alert_records(alert_id)
        result = cert(state, items)
        check(result["status"] == "selected", f"{alert_id} did not select")
        check(result["selected_incident_id"] == alert_id, f"{alert_id} selected the wrong incident")
        check(result["active_pause_alert_ids"] == [alert_id], f"{alert_id} active order drifted")
        check(result["blockers"] == [], f"{alert_id} acquired unexpected blockers")
        selected[alert_id] = statuses[-1]

    six_state, six_items, six_statuses = open_alert_records("low_oxygen", sample_count=6)
    six_result = cert(six_state, six_items)
    check(six_statuses[-1] == "pending", "sixth low-oxygen sample was not pending")
    check(six_result["status"] != "selected", "six low-oxygen samples became a candidate")
    check(six_result["selected_incident_id"] is None, "six-sample result selected an incident")
    check(six_result["command_hint_hash"] is None, "six-sample result emitted a command hint")

    short_state, short_items, short_statuses = open_alert_records("low_oxygen", duration=29.999)
    short_result = cert(short_state, short_items)
    check(short_statuses[-1] == "pending", "under-30-second low oxygen opened")
    check(short_result["status"] != "selected", "under-30-second low oxygen became a candidate")
    check(short_result["selected_incident_id"] is None, "short-duration result selected an incident")

    ordinary_state = make_state(
        session_id="a08b3-ordinary-only",
        alerts=[],
        idle=1,
        stressed=1,
        research="",
        buildings=(0, 0, 0),
    )
    ordinary_tracker = IncidentTracker()
    ordinary_signals = update(ordinary_tracker, ordinary_state)
    ordinary_items = ordinary_tracker.payload()["items"]
    ordinary_result = cert(ordinary_state, ordinary_items)
    check(ordinary_result["active_pause_alert_ids"] == [], "ordinary rules populated pause IDs")
    check(ordinary_result["status"] == "none", "ordinary-only records did not remain observation-only")
    check(ordinary_result["selected_incident_id"] is None, "ordinary-only records selected a pause")
    check(ordinary_result["command_hint_hash"] is None, "ordinary-only records emitted a pause command")
    advice = rules.build_arbitrated_advice(ordinary_state, ordinary_tracker.open_incidents())
    check(
        not any(
            isinstance(item.get("command"), Mapping)
            and item["command"].get("action") == "set_paused"
            for item in advice
        ),
        "ordinary-only advice acquired pause authority",
    )
    return {
        "selected_pause_statuses": selected,
        "low_oxygen_six_status": six_result["status"],
        "low_oxygen_short_status": short_result["status"],
        "ordinary_signal_count": len(ordinary_signals),
        "ordinary_open_count": len(ordinary_tracker.open_incidents()),
        "ordinary_only_status": ordinary_result["status"],
    }


def expect_blocked(
    label: str, state: Mapping[str, Any], items: Any, blocker: str
) -> Dict[str, Any]:
    result = cert(state, items)
    check(result["status"] == "blocked", f"{label} was not blocked")
    check(result["selected_incident_id"] is None, f"{label} selected an incident")
    check(result["command_hint_hash"] is None, f"{label} emitted a command hint")
    check(blocker in result["blockers"], f"{label} omitted {blocker}")
    return result


def case_malformed_and_priority_fail_closed() -> Dict[str, Any]:
    low_state, low_items, _ = open_alert_records("low_oxygen")
    mutations: Dict[str, tuple[Any, str]] = {
        "unknown_id": (
            low_items + [{"incident_id": "low_oxygen_shadow", "status": "inactive"}],
            "open_incident_alert_invalid",
        ),
        "wrong_id_type": (
            low_items + [{"incident_id": 7, "status": "inactive"}],
            "open_incidents_invalid",
        ),
        "duplicate_id": (
            low_items + [copy.deepcopy(low_items[0])],
            "open_incidents_duplicate",
        ),
        "non_mapping_item": (low_items + [None], "open_incidents_invalid"),
        "corrupt_container": (None, "open_incidents_invalid"),
        "mapping_container": ({"incident_id": "low_oxygen"}, "open_incidents_invalid"),
    }
    mutation_blockers: Dict[str, List[str]] = {}
    for label, (items, blocker) in mutations.items():
        result = expect_blocked(label, low_state, items, blocker)
        mutation_blockers[label] = result["blockers"]

    higher_state = copy.deepcopy(low_state)
    higher_state["alerts"] = ["low_oxygen", "suffocation"]
    without_higher = [
        copy.deepcopy(item)
        for item in low_items
        if item.get("incident_id") != "suffocation"
    ]
    missing_result = expect_blocked(
        "missing_higher_priority_witness",
        higher_state,
        without_higher,
        "incident_witness_missing:suffocation",
    )
    low_record = records_by_id(low_items)["low_oxygen"]
    invalid_higher = copy.deepcopy(low_record)
    invalid_higher.update({"incident_id": "suffocation", "telemetry_state": "unknown"})
    invalid_items = without_higher + [invalid_higher]
    invalid_result = expect_blocked(
        "invalid_higher_priority_witness",
        higher_state,
        invalid_items,
        "incident_witness_telemetry_not_active:suffocation",
    )
    return {
        "mutation_blockers": mutation_blockers,
        "missing_higher_blockers": missing_result["blockers"],
        "invalid_higher_blockers": invalid_result["blockers"],
    }


def candidate_runtime() -> oni_bridge.ReplayState:
    runtime = oni_bridge.ReplayState()
    session_id = "a08b3-bridge-candidate"
    runtime.process(
        {
            "schema": "oni.event.v2",
            "event": "session_started",
            "session_id": session_id,
            "seq": 1,
        }
    )
    base = dt.datetime.now(dt.timezone.utc).astimezone() - dt.timedelta(seconds=30)
    for index in range(7):
        state = make_state(
            session_id=session_id,
            seq=index + 2,
            snapshot_seq=index + 1,
            timestamp=iso_at(base, index * 5.0),
            alerts=["low_oxygen"],
        )
        observed_at = dt.datetime.fromisoformat(state["ts"])
        with (
            mock.patch.object(
                oni_bridge,
                "snapshot_trust_blockers",
                side_effect=lambda value, now=None, clock=observed_at: state_quality.snapshot_trust_blockers(
                    value, now=now or clock
                ),
            ),
            mock.patch.object(
                oni_bridge,
                "snapshot_age_seconds",
                side_effect=lambda value, now=None, clock=observed_at: state_quality.snapshot_age_seconds(
                    value, now=now or clock
                ),
            ),
        ):
            runtime.process(state)
    return runtime


def read_json(path: pathlib.Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def case_bridge_frame_and_approval(root: pathlib.Path) -> Dict[str, Any]:
    output = root / "candidate"
    policy = {
        "schema": "oni.action-policy.v1",
        "actions_enabled": True,
        "suggestions_enabled": True,
        "allowed_actions": ["set_paused"],
        "allowed_save_hashes": [SAVE_HASH],
        "command_ttl_seconds": 30,
        "max_pending_commands": 20,
    }
    oni_bridge.atomic_write_json(output / "action-policy.json", policy)
    oni_bridge.write_outputs(output, candidate_runtime())
    frame = read_json(output / "decision-frame.json")
    validate_decision_frame(frame)
    incidents = frame["projections"]["incidents"]
    certificate = incidents["survival_emergency"]
    check(incidents["valid"] is True and incidents["count"] == 21, "Bridge frame lost mixed incidents")
    check(
        set(item["incident_id"] for item in incidents["items"]) == BASE_EXPECTED_IDS,
        "Bridge frame baseline catalog drifted",
    )
    check(certificate["status"] == "selected", "Bridge frame did not select low oxygen")
    check(certificate["selected_incident_id"] == "low_oxygen", "Bridge frame selected the wrong incident")
    pending = sorted((output / "commands" / "pending").glob("*.json"))
    check(len(pending) == 1, "Bridge did not publish one pending suggestion")
    command = read_json(pending[0])
    validate_approval_inputs(command, policy, frame)
    check(command["source_incident_id"] == "low_oxygen", "approval input was not bound to low oxygen")
    return {
        "frame_validated": True,
        "incident_count": incidents["count"],
        "certificate_status": certificate["status"],
        "pending_command_count": len(pending),
        "approval_inputs_validated": True,
        "command_id": command["id"],
    }


def case_bridge_subprocess_replay(root: pathlib.Path) -> Dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    bridge_dir = root / "subprocess-bridge"
    input_path = root / "subprocess-input.state.jsonl"
    state = make_state(
        session_id="a08b3-subprocess",
        seq=2,
        snapshot_seq=1,
        timestamp=iso_at(dt.datetime.now(dt.timezone.utc).astimezone(), 0.0),
        alerts=["low_oxygen"],
    )
    records = [
        {
            "schema": "oni.event.v2",
            "event": "session_started",
            "session_id": state["session_id"],
            "seq": 1,
        },
        state,
    ]
    input_path.write_text(
        "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in records),
        encoding="utf-8",
    )
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [
            sys.executable,
            "-B",
            str(ROOT / "bridge" / "oni_bridge.py"),
            "--bridge-dir",
            str(bridge_dir),
            "--input",
            str(input_path),
            "--once",
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
    )
    check(completed.returncode == 0, "explicit Bridge replay subprocess did not exit 0")
    frame = read_json(bridge_dir / "decision-frame.json")
    validate_decision_frame(frame)
    incidents = frame["projections"]["incidents"]
    certificate = incidents["survival_emergency"]
    check(incidents["count"] == 21, "subprocess Bridge frame lost mixed incidents")
    check(certificate["status"] == "blocked", "replay subprocess was not blocked")
    check(certificate["selected_incident_id"] is None, "replay subprocess selected a mutation")
    pending = list((bridge_dir / "commands" / "pending").glob("*.json"))
    check(not pending, "replay subprocess created a mutating suggestion")
    return {
        "exit_code": completed.returncode,
        "explicit_bridge_dir": str(bridge_dir),
        "explicit_input": str(input_path),
        "frame_validated": True,
        "incident_count": incidents["count"],
        "certificate_status": certificate["status"],
        "pending_command_count": len(pending),
        "stdout_tail": completed.stdout[-400:],
        "stderr_tail": completed.stderr[-400:],
    }


def sha256_file(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def main() -> int:
    cases = [
        ("catalog_and_ordinary_statuses", lambda _root: case_catalog_and_ordinary_statuses()),
        ("pause_boundaries_and_ordinary_only", lambda _root: case_pause_boundaries_and_ordinary_only()),
        ("malformed_and_priority_fail_closed", lambda _root: case_malformed_and_priority_fail_closed()),
        ("bridge_frame_and_approval", case_bridge_frame_and_approval),
        ("bridge_subprocess_replay", case_bridge_subprocess_replay),
    ]
    results: List[Dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="oni-a08b3-mixed-") as temporary:
        root = pathlib.Path(temporary)
        for name, function in cases:
            started = dt.datetime.now(dt.timezone.utc)
            try:
                details = function(root / name)
            except Exception as exc:  # noqa: BLE001 - report one bounded case failure
                results.append(
                    {
                        "name": name,
                        "status": "failed",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "duration_seconds": round(
                            (dt.datetime.now(dt.timezone.utc) - started).total_seconds(), 3
                        ),
                    }
                )
            else:
                results.append(
                    {
                        "name": name,
                        "status": "passed",
                        "details": details,
                        "duration_seconds": round(
                            (dt.datetime.now(dt.timezone.utc) - started).total_seconds(), 3
                        ),
                    }
                )
    passed = all(item["status"] == "passed" for item in results)
    report = {
        "schema": "oni.survival-mixed-incidents-verification.v1",
        "status": "passed" if passed else "failed",
        "offline_only": True,
        "case_count": len(cases),
        "assertion_count": ASSERTIONS,
        "source_hashes": {
            "agent/rules.py": sha256_file(ROOT / "agent" / "rules.py"),
            "scripts/verify_survival_mixed_incidents.py": sha256_file(pathlib.Path(__file__)),
            "scripts/verify_survival_certificate.py": sha256_file(ROOT / "scripts" / "verify_survival_certificate.py"),
            "scripts/verify_bridge_reliability.py": sha256_file(ROOT / "scripts" / "verify_bridge_reliability.py"),
        },
        "cases": results,
        "limitations": [
            "offline fixtures do not authenticate Probe sample origin",
            "no ONI, Klei save, normal Mod, live Bridge, or bootstrap admission is claimed",
        ],
    }
    report_path = ROOT / "reports" / "a08b3-mixed-incidents" / "verify.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for item in results:
        detail = item.get("error") or json.dumps(item.get("details", {}), ensure_ascii=False, sort_keys=True)
        print(f"[{item['status'].upper()}] {item['name']}: {detail}")
    print(f"Cases: {len(cases)}; assertions: {ASSERTIONS}; report: {report_path}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
