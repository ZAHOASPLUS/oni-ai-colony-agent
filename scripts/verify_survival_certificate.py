#!/usr/bin/env python3
"""Offline, independent checks for survival-arbitration certificate parity.

This verifier uses fixed policy/rank expectations as its oracle.  It calls the
production reducer and certificate validator only for the behavior under test;
it never treats one production result as proof that another production result
is correct.  All fixtures are in-memory or temporary test-fixture objects.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import itertools
import json
import pathlib
import sys
import time
from typing import Any, Callable, Dict, Iterable, List, Mapping, MutableMapping, Sequence


ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import rules  # noqa: E402
from agent.approval_receipt import (  # noqa: E402
    ApprovalReceiptError,
    validate_approval_inputs,
)
from agent.canonical_hash import canonical_hash  # noqa: E402
from agent.decision import DecisionFrameError, validate_decision_frame  # noqa: E402
from agent.field_contracts import (  # noqa: E402
    ALERTS_CANONICAL_CONTRACT_ID,
    CANONICAL_ALERT_IDS,
    GAME_PAUSED_CONTRACT_ID,
    contract_quality_fixture,
)
from agent.incidents import IncidentTracker  # noqa: E402
from agent.state_quality import canonical_state_hash, parse_snapshot_timestamp  # noqa: E402
from scripts.verify_approval_frame_binding import fixture as approval_fixture  # noqa: E402
from scripts.verify_decision_frame import build as build_frame  # noqa: E402
from scripts.verify_decision_frame import healthy_inputs  # noqa: E402


PAUSE_ORDER = [
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
OBSERVE_IDS = ["exhaustion", "high_stress", "sickness", "urgent_toilet", "wounded"]
SAMPLES = {
    "suffocation": 1,
    "incapacitated": 1,
    "starvation": 1,
    "bionic_offline": 1,
    "hyperthermia": 1,
    "hypothermia": 1,
    "major_sickness": 1,
    "low_oxygen": 7,
    "bionic_low_battery": 2,
}
DURATIONS = {
    "suffocation": 0.0,
    "incapacitated": 0.0,
    "starvation": 0.0,
    "bionic_offline": 0.0,
    "hyperthermia": 0.0,
    "hypothermia": 0.0,
    "major_sickness": 0.0,
    "low_oxygen": 30.0,
    "bionic_low_battery": 5.0,
}

_assertion_count = 0


def check(condition: bool, message: str) -> None:
    global _assertion_count
    _assertion_count += 1
    if not condition:
        raise AssertionError(message)


def make_state(
    alerts: Sequence[str],
    *,
    paused: bool = False,
    timestamp: str = "2026-01-01T00:01:40+00:00",
    session_id: str = "certificate-fixture",
    seq: int = 100,
    snapshot_seq: int = 100,
    snapshot_id: str | None = None,
) -> Dict[str, Any]:
    snapshot = snapshot_id or f"{session_id}:{seq}"
    return {
        "schema": "oni.state.v2",
        "event": "state_snapshot",
        "ts": timestamp,
        "session_id": session_id,
        "seq": seq,
        "snapshot_seq": snapshot_seq,
        "snapshot_id": snapshot,
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


def source_for(state: Mapping[str, Any], *, include_snapshot_seq: bool = False) -> Dict[str, Any]:
    source = {
        "session_id": state.get("session_id"),
        "save_id_hash": state.get("save_id_hash"),
        "world_id": state.get("world_id"),
        "snapshot_id": state.get("snapshot_id"),
        "seq": state.get("seq"),
        "ts": state.get("ts"),
        "state_hash": canonical_state_hash(dict(state)),
    }
    if include_snapshot_seq:
        source["snapshot_seq"] = state.get("snapshot_seq")
    return source


def witness(
    state: Mapping[str, Any],
    alert_id: str,
    *,
    status: str = "open",
    telemetry_state: str = "active",
    risk: str = "high",
    samples: int | None = None,
    duration: float | None = None,
    **changes: Any,
) -> Dict[str, Any]:
    sample_count = SAMPLES[alert_id] if samples is None else samples
    duration_seconds = DURATIONS[alert_id] if duration is None else duration
    parsed_last = parse_snapshot_timestamp(state.get("ts"))
    check(parsed_last is not None, f"fixture timestamp is invalid for {alert_id}")
    assert parsed_last is not None
    parsed_first = parsed_last - dt.timedelta(seconds=duration_seconds)
    last_seq = int(state["seq"])
    last_snapshot_seq = int(state["snapshot_seq"])
    item: Dict[str, Any] = {
        "incident_id": alert_id,
        "status": status,
        "risk": risk,
        "telemetry_state": telemetry_state,
        "session_id": state.get("session_id"),
        "snapshot_id": state.get("snapshot_id"),
        "first_seq": last_seq - sample_count + 1,
        "last_seq": last_seq,
        "first_snapshot_seq": last_snapshot_seq - sample_count + 1,
        "last_snapshot_seq": last_snapshot_seq,
        "consecutive_samples": sample_count,
        "first_seen": parsed_first.isoformat(timespec="microseconds"),
        "last_seen": parsed_last.isoformat(timespec="microseconds"),
    }
    item.update(changes)
    return item


def projected_incident(item: Mapping[str, Any]) -> Dict[str, Any]:
    keys = (
        "incident_id",
        "status",
        "risk",
        "telemetry_state",
        "session_id",
        "snapshot_id",
        "first_seq",
        "last_seq",
        "first_snapshot_seq",
        "last_snapshot_seq",
        "consecutive_samples",
        "first_seen",
        "last_seen",
    )
    return {key: item.get(key) for key in keys}


def signal_for(state: Dict[str, Any], alert_id: str) -> Dict[str, Any]:
    return next(
        item
        for item in rules.evaluate_signals(state)
        if item.get("incident_id") == alert_id
    )


def rehash_frame(frame: MutableMapping[str, Any]) -> None:
    payload = {key: value for key, value in frame.items() if key != "frame_id"}
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    frame["frame_id"] = "sha256:" + hashlib.sha256(encoded).hexdigest()


def rebind_values(values: Dict[str, Dict[str, Any]]) -> None:
    state_hash = canonical_state_hash(values["state"])
    for name in ("health", "advice", "incidents", "plan", "strategy", "policy"):
        values[name]["source"]["state_hash"] = state_hash
    values["health"]["latest_state_hash"] = state_hash
    values["strategy"]["state_hash"] = state_hash
    for name, child_key in (("advice", "items"), ("incidents", "items"), ("plan", "plan")):
        for item in values[name][child_key]:
            item["state_hash"] = state_hash


def valid_direct_certificate(alerts: Sequence[str], records: Sequence[Mapping[str, Any]]) -> tuple[Dict[str, Any], Dict[str, Any], List[Dict[str, Any]]]:
    state = make_state(alerts)
    raw_records = [dict(item) for item in records]
    certificate = rules.build_survival_arbitration_certificate(state, raw_records)
    projected = [projected_incident(item) for item in raw_records]
    return certificate, source_for(state), projected


def case_legacy_hidden_higher_witness() -> Dict[str, Any]:
    command, policy, frame = approval_fixture()
    frame_source = dict(frame["source"])
    high = copy.deepcopy(frame["projections"]["incidents"]["items"][0])
    check(high.get("incident_id") == "suffocation", "legacy fixture did not start with suffocation")
    high["telemetry_state"] = "inactive"
    state = make_state(
        ["suffocation", "low_oxygen"],
        timestamp=str(frame_source["ts"]),
        session_id=str(frame_source["session_id"]),
        seq=int(frame_source["seq"]),
        snapshot_seq=120,
        snapshot_id=str(frame_source["snapshot_id"]),
    )
    low = witness(state, "low_oxygen")
    low_signal = rules._alert_signal("low_oxygen", True)
    incidents_projection = frame["projections"]["incidents"]
    incidents_projection["items"] = [high, projected_incident(low)]
    incidents_projection["count"] = 2
    incidents_projection["survival_emergency"] = {
        "schema": rules._SURVIVAL_CERTIFICATE_SCHEMA,
        "policy_hash": rules.survival_alert_policy_hash(),
        "status": "selected",
        "blockers": [],
        "game_paused": False,
        "active_pause_alert_ids": ["suffocation", "low_oxygen"],
        "eligible_incident_ids": ["low_oxygen"],
        "selected_incident_id": "low_oxygen",
        "command_hint_hash": canonical_hash(low_signal["command"]),
    }
    frame["projections"]["advice"]["items"] = [
        {
            "incident_id": "low_oxygen",
            "risk": "high",
            "incident_opened_at": low["first_seen"],
        }
    ]
    expected_command = low_signal["command"]
    command = copy.deepcopy(command)
    for key in ("action", "args", "preconditions", "expected_effects"):
        command[key] = copy.deepcopy(expected_command[key])
    command.update(
        {
            "source_incident_id": "low_oxygen",
            "risk": "high",
            "reason": low_signal["reason"],
            "evidence": copy.deepcopy(low_signal["evidence"]),
        }
    )
    command["source"] = dict(command["source"])
    command["source"].update(
        {
            "incident_opened_at": low["first_seen"],
            "title": low_signal["title"],
        }
    )
    rehash_frame(frame)
    command["source_frame_id"] = frame["frame_id"]

    try:
        validate_decision_frame(frame)
    except DecisionFrameError as exc:
        decision_error = str(exc)
    else:
        decision_error = ""
    try:
        validate_approval_inputs(command, policy, frame)
    except ApprovalReceiptError as exc:
        approval_error = str(exc)
    else:
        approval_error = ""
    check(decision_error, "legacy hidden higher witness passed decision validation")
    check(approval_error, "legacy hidden higher witness passed approval validation")
    check("survival emergency certificate is invalid" in decision_error, "decision rejection used an unrelated layer")
    check("survival emergency certificate is invalid" in approval_error, "approval rejection used an unrelated layer")
    check("survival_certificate_status_mismatch" in decision_error, "decision rejection lacked status mismatch")
    check("survival_certificate_status_mismatch" in approval_error, "approval rejection lacked status mismatch")

    fresh_high = witness(state, "suffocation", telemetry_state="inactive")
    fresh_low = witness(state, "low_oxygen")
    fresh = rules.arbitrate_survival_emergency(state, [fresh_high, fresh_low])
    check(fresh["status"] == "blocked", "fresh arbiter accepted invalid higher witness")
    check(fresh["selected_incident_id"] is None, "fresh arbiter selected a lower incident")
    check(fresh["command"] is None, "fresh arbiter emitted a command")
    expected_blocker = "incident_witness_telemetry_not_active:suffocation"
    check(expected_blocker in fresh["blockers"], "fresh arbiter lacked the concrete higher-witness blocker")
    return {
        "decision_rejected": True,
        "approval_rejected": True,
        "fresh_status": fresh["status"],
        "fresh_blockers": fresh["blockers"],
        "fresh_selected": fresh["selected_incident_id"],
    }


def case_policy_and_controls() -> Dict[str, Any]:
    check(set(rules.SURVIVAL_ALERT_POLICY) == set(CANONICAL_ALERT_IDS), "policy/catalog keyset drifted")
    check(rules.survival_alert_policy_blockers() == [], "policy is not closed")
    actual_pause_order = sorted(
        [
            alert_id
            for alert_id, item in rules.SURVIVAL_ALERT_POLICY.items()
            if item.get("disposition") == "pause"
        ],
        key=lambda item: int(rules.SURVIVAL_ALERT_POLICY[item]["rank"]),
    )
    check(actual_pause_order == PAUSE_ORDER, "pause priority order drifted")
    actual_observe = sorted(
        alert_id
        for alert_id, item in rules.SURVIVAL_ALERT_POLICY.items()
        if item.get("disposition") == "observe"
    )
    check(actual_observe == OBSERVE_IDS, "observe-only catalog drifted")

    empty_state = make_state([])
    empty_result = rules.arbitrate_survival_emergency(empty_state, [])
    check(empty_result["status"] == "none", "empty alert control was not none")
    check(empty_result["blockers"] == [], "empty alert control had blockers")
    check(empty_result["command"] is None, "empty alert control emitted a command")
    empty_cert = rules.build_survival_arbitration_certificate(empty_state, [])
    check(rules.survival_certificate_blockers(empty_cert, source_for(empty_state), []) == [], "empty certificate rejected")

    observed = {}
    for alert_id in OBSERVE_IDS:
        state = make_state([alert_id])
        result = rules.arbitrate_survival_emergency(state, [{"incident_id": alert_id, "status": "open"}])
        check(result["status"] == "none", f"observe-only {alert_id} acquired pause authority")
        check(result["command"] is None, f"observe-only {alert_id} emitted a command")
        observed[alert_id] = result["status"]
    return {"pause_order": PAUSE_ORDER, "observe_only": observed, "empty_control": True}


def case_positive_direct_and_frame() -> Dict[str, Any]:
    state = make_state(["suffocation"])
    tracker = IncidentTracker()
    tracker.update(state, [signal_for(state, "suffocation")])
    item = copy.deepcopy(tracker.incidents["suffocation"])
    check(item["status"] == "open", "direct suffocation did not open")
    result = rules.arbitrate_survival_emergency(state, [item])
    check(result["status"] == "selected", "direct suffocation was not selected")
    check(result["selected_incident_id"] == "suffocation", "direct suffocation selected wrong id")
    check(result["blockers"] == [], "direct suffocation had blockers")
    check(isinstance(result["command"], dict) and result["command"]["action"] == "set_paused", "direct suffocation lacked pause command")

    certificate = rules.build_survival_arbitration_certificate(state, [item])
    projected = [projected_incident(item)]
    check(rules.survival_certificate_blockers(certificate, source_for(state), projected) == [], "direct certificate was rejected")
    check(certificate["status"] == "selected", "direct certificate status drifted")
    check(certificate["selected_incident_id"] == "suffocation", "direct certificate selection drifted")

    command, policy, frame = approval_fixture()
    validate_decision_frame(frame)
    check(rules.survival_suggested_command_frame_blockers(command, frame) == [], "positive command lost survival binding")
    validate_approval_inputs(command, policy, frame)
    return {
        "tracker_status": item["status"],
        "certificate_status": certificate["status"],
        "frame_validated": True,
        "approval_inputs_validated": True,
    }


def case_hysteresis_boundaries() -> Dict[str, Any]:
    base = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    low_tracker = IncidentTracker()
    low_statuses: List[str] = []
    last_low_state: Dict[str, Any] = {}
    for index in range(7):
        state = make_state(
            ["low_oxygen"],
            timestamp=(base + dt.timedelta(seconds=index * 5)).isoformat(timespec="seconds"),
            seq=index + 1,
            snapshot_seq=index + 1,
            snapshot_id=f"certificate-fixture:{index + 1}",
        )
        low_tracker.update(state, [signal_for(state, "low_oxygen")])
        low_statuses.append(str(low_tracker.incidents["low_oxygen"]["status"]))
        last_low_state = state
    check(low_statuses[0] == "pending", "low oxygen opened at sample 1")
    check(low_statuses[5] == "pending", "low oxygen opened at sample 6")
    check(low_statuses[6] == "open", "low oxygen did not open at sample 7/30s")
    low_records = [copy.deepcopy(low_tracker.incidents["low_oxygen"])]
    low_certificate = rules.build_survival_arbitration_certificate(last_low_state, low_records)
    check(low_certificate["status"] == "selected", "real low oxygen certificate was not selected")
    check(
        rules.survival_certificate_blockers(
            low_certificate,
            source_for(last_low_state),
            [projected_incident(low_records[0])],
        )
        == [],
        "real low oxygen certificate failed validation",
    )

    battery_tracker = IncidentTracker()
    battery_statuses: List[str] = []
    last_battery_state: Dict[str, Any] = {}
    for index in range(2):
        state = make_state(
            ["bionic_low_battery"],
            timestamp=(base + dt.timedelta(seconds=index * 5)).isoformat(timespec="seconds"),
            seq=index + 1,
            snapshot_seq=index + 1,
            snapshot_id=f"certificate-battery:{index + 1}",
        )
        battery_tracker.update(state, [signal_for(state, "bionic_low_battery")])
        battery_statuses.append(str(battery_tracker.incidents["bionic_low_battery"]["status"]))
        last_battery_state = state
    check(battery_statuses == ["pending", "open"], "battery 2/5 hysteresis drifted")
    battery_record = copy.deepcopy(battery_tracker.incidents["bionic_low_battery"])
    battery_certificate = rules.build_survival_arbitration_certificate(last_battery_state, [battery_record])
    check(battery_certificate["status"] == "selected", "real battery certificate was not selected")
    check(
        rules.survival_certificate_blockers(
            battery_certificate,
            source_for(last_battery_state),
            [projected_incident(battery_record)],
        )
        == [],
        "real battery certificate failed validation",
    )

    boundary_state = make_state(["low_oxygen"])
    for sample_count in (1, 6):
        record = witness(
            boundary_state,
            "low_oxygen",
            samples=sample_count,
            first_seq=100 - sample_count + 1,
            first_snapshot_seq=100 - sample_count + 1,
        )
        result = rules.arbitrate_survival_emergency(boundary_state, [record])
        check(result["status"] == "blocked", f"low oxygen sample {sample_count} was accepted")
        check(
            f"incident_witness_samples_insufficient:low_oxygen" in result["blockers"],
            f"low oxygen sample {sample_count} lacked a fixed samples blocker",
        )
        check(result["command"] is None, f"low oxygen sample {sample_count} emitted a command")

    for duration in (29.999, 30.0):
        parsed = parse_snapshot_timestamp(boundary_state["ts"])
        assert parsed is not None
        record = witness(
            boundary_state,
            "low_oxygen",
            first_seen=(parsed - dt.timedelta(seconds=duration)).isoformat(timespec="microseconds"),
        )
        result = rules.arbitrate_survival_emergency(boundary_state, [record])
        if duration == 29.999:
            check(result["status"] == "blocked", "29.999 seconds was accepted")
            check(
                "incident_witness_duration_insufficient:low_oxygen" in result["blockers"],
                "29.999 seconds lacked a duration blocker",
            )
        else:
            check(result["status"] == "selected", "30 seconds was not accepted")
            check(result["selected_incident_id"] == "low_oxygen", "30 seconds selected wrong incident")
    paused = make_state(["suffocation"], paused=True)
    paused_result = rules.arbitrate_survival_emergency(paused, [])
    check(paused_result["status"] == "none", "already-paused control was not none")
    check(paused_result["blockers"] == ["already_contained"], "already-paused blocker drifted")
    paused_certificate = rules.build_survival_arbitration_certificate(paused, [])
    check(
        rules.survival_certificate_blockers(paused_certificate, source_for(paused), []) == [],
        "already-paused certificate was rejected",
    )
    return {
        "low_oxygen_statuses": low_statuses,
        "battery_statuses": battery_statuses,
        "duration_boundaries": [29.999, 30.0],
        "paused_control": "already_contained",
    }


def case_higher_priority_pairwise_fail_closed() -> Dict[str, Any]:
    variants = ("invalid", "missing", "stale", "non_open", "wrongrisk")
    case_count = 0
    blocker_counts: Dict[str, int] = {variant: 0 for variant in variants}
    pair_names: List[str] = []
    for higher, lower in itertools.combinations(PAUSE_ORDER, 2):
        pair_names.append(f"{higher}>{lower}")
        state = make_state([higher, lower])
        higher_record = witness(state, higher)
        lower_record = witness(state, lower)
        for variant in variants:
            case_count += 1
            records: List[Dict[str, Any]] = [copy.deepcopy(higher_record), copy.deepcopy(lower_record)]
            if variant == "invalid":
                records[0]["telemetry_state"] = "inactive"
                expected_direct = f"incident_witness_telemetry_not_active:{higher}"
            elif variant == "missing":
                records = [copy.deepcopy(lower_record)]
                if SAMPLES[higher] == 1 and DURATIONS[higher] == 0.0:
                    expected_direct = f"incident_witness_missing:{higher}"
                else:
                    expected_direct = f"incident_witness_higher_priority_missing:{higher}"
            elif variant == "stale":
                parsed = parse_snapshot_timestamp(str(state["ts"]))
                assert parsed is not None
                records[0]["last_seen"] = (parsed - dt.timedelta(seconds=1)).isoformat(timespec="microseconds")
                expected_direct = f"incident_witness_not_current:{higher}"
            elif variant == "non_open":
                records[0]["status"] = "pending"
                expected_direct = "open_incident_status_invalid"
            else:
                records[0]["risk"] = "low"
                expected_direct = f"incident_witness_risk_mismatch:{higher}"

            direct = rules.arbitrate_survival_emergency(state, records)
            check(direct["status"] == "blocked", f"{variant} {higher}>{lower} downgraded to lower selection")
            check(direct["selected_incident_id"] is None, f"{variant} {higher}>{lower} selected an incident")
            check(direct["command"] is None, f"{variant} {higher}>{lower} emitted a command")
            check(expected_direct in direct["blockers"], f"{variant} {higher}>{lower} lacked {expected_direct}")
            blocker_counts[variant] += 1

            forged = {
                "schema": rules._SURVIVAL_CERTIFICATE_SCHEMA,
                "policy_hash": rules.survival_alert_policy_hash(),
                "status": "selected",
                "blockers": [],
                "game_paused": False,
                "active_pause_alert_ids": [higher, lower],
                "eligible_incident_ids": [lower],
                "selected_incident_id": lower,
                "command_hint_hash": canonical_hash(rules._alert_signal(lower, True)["command"]),
            }
            certificate_blockers = rules.survival_certificate_blockers(
                forged,
                source_for(state),
                [projected_incident(item) for item in records],
            )
            check(certificate_blockers, f"certificate hid {variant} higher witness {higher}")
            check(
                "survival_certificate_status_mismatch" in certificate_blockers,
                f"certificate {variant} {higher}>{lower} did not reject selected status",
            )
    check(case_count == 36 * 5, "pairwise case count drifted")
    return {
        "pair_count": len(pair_names),
        "variant_count": case_count,
        "variants": list(variants),
        "fixed_direct_blocker_counts": blocker_counts,
    }


def case_certificate_tamper_and_projection_edges() -> Dict[str, Any]:
    state = make_state(["suffocation"])
    record = witness(state, "suffocation")
    certificate, source, projected = valid_direct_certificate(["suffocation"], [record])
    tamper_expected = {
        "status": "survival_certificate_status_mismatch",
        "eligible": "survival_certificate_eligibility_mismatch",
        "selected": "survival_certificate_selection_mismatch",
        "blockers": "survival_certificate_blockers_mismatch",
        "game_paused": "survival_certificate_status_mismatch",
        "hint_hash": "survival_certificate_command_hash_mismatch",
    }
    for label, mutate in (
        ("status", lambda item: item.__setitem__("status", "none")),
        ("eligible", lambda item: item.__setitem__("eligible_incident_ids", [])),
        ("selected", lambda item: item.__setitem__("selected_incident_id", None)),
        ("blockers", lambda item: item.__setitem__("blockers", ["forged_blocker"])),
        ("game_paused", lambda item: item.__setitem__("game_paused", True)),
        ("hint_hash", lambda item: item.__setitem__("command_hint_hash", "sha256:" + "f" * 64)),
    ):
        forged = copy.deepcopy(certificate)
        mutate(forged)
        blockers = rules.survival_certificate_blockers(forged, source, projected)
        check(blockers, f"certificate {label} tamper was accepted")
        check(tamper_expected[label] in blockers, f"certificate {label} tamper lacked fixed blocker")

    duplicate = copy.deepcopy(certificate)
    duplicate["active_pause_alert_ids"] = ["suffocation", "suffocation"]
    blockers = rules.survival_certificate_blockers(duplicate, source, projected)
    check("survival_certificate_active_pause_alert_ids_invalid" in blockers, "duplicate active certificate IDs were accepted")

    outside_state = make_state([])
    outside_record = witness(outside_state, "suffocation")
    outside_result = rules.arbitrate_survival_emergency(outside_state, [outside_record])
    check(outside_result["status"] == "blocked", "active-outside open witness was accepted")
    check(
        "incident_witness_alert_inactive:suffocation" in outside_result["blockers"],
        "active-outside witness lacked a concrete blocker",
    )
    outside_cert = rules.build_survival_arbitration_certificate(outside_state, [outside_record])
    check(
        rules.survival_certificate_blockers(
            outside_cert,
            source_for(outside_state),
            [projected_incident(outside_record)],
        )
        == [],
        "blocked active-outside certificate failed to reproduce",
    )

    duplicate_records = [witness(state, "suffocation"), witness(state, "suffocation")]
    duplicate_result = rules.arbitrate_survival_emergency(state, duplicate_records)
    check(duplicate_result["status"] == "blocked", "duplicate incident IDs were accepted")
    check("open_incidents_duplicate" in duplicate_result["blockers"], "duplicate incident IDs lacked a concrete blocker")
    duplicate_cert = rules.build_survival_arbitration_certificate(state, duplicate_records)
    check(
        rules.survival_certificate_blockers(
            duplicate_cert,
            source_for(state),
            [projected_incident(item) for item in duplicate_records],
        )
        == [],
        "duplicate blocked certificate failed to reproduce",
    )
    return {
        "tampered_fields": sorted(tamper_expected),
        "outside_open_blocked": True,
        "duplicate_ids_blocked": True,
    }


def case_identity_timestamp_and_hysteresis_binding() -> Dict[str, Any]:
    state = make_state(["low_oxygen"])
    valid_record = witness(state, "low_oxygen")
    valid_certificate = rules.build_survival_arbitration_certificate(state, [valid_record])
    explicit_source = source_for(state, include_snapshot_seq=True)
    check(
        rules.survival_certificate_blockers(
            valid_certificate,
            explicit_source,
            [projected_incident(valid_record)],
        )
        == [],
        "explicit frame snapshot_seq did not reproduce the certificate",
    )
    forged_source = copy.deepcopy(explicit_source)
    forged_source["snapshot_seq"] -= 1
    explicit_blockers = rules.survival_certificate_blockers(
        valid_certificate,
        forged_source,
        [projected_incident(valid_record)],
    )
    check(explicit_blockers, "tampered explicit frame snapshot_seq was accepted")
    for invalid in (None, "100"):
        invalid_source = copy.deepcopy(explicit_source)
        invalid_source["snapshot_seq"] = invalid
        invalid_blockers = rules.survival_certificate_blockers(
            valid_certificate,
            invalid_source,
            [projected_incident(valid_record)],
        )
        check(invalid_blockers, f"invalid explicit snapshot_seq {invalid!r} was accepted")
    mutation_expectations = {
        "session": "incident_witness_session_mismatch:low_oxygen",
        "snapshot": "incident_witness_snapshot_mismatch:low_oxygen",
        "sequence": "incident_witness_sequence_mismatch:low_oxygen",
        "snapshot_sequence": "incident_witness_snapshot_sequence_mismatch:low_oxygen",
        "timestamp": "incident_witness_not_current:low_oxygen",
    }
    mutations: Dict[str, Callable[[Dict[str, Any]], None]] = {
        "session": lambda item: item.__setitem__("session_id", "other-session"),
        "snapshot": lambda item: item.__setitem__("snapshot_id", "other-session:100"),
        "sequence": lambda item: item.__setitem__("last_seq", item["last_seq"] - 1),
        "snapshot_sequence": lambda item: item.__setitem__("last_snapshot_seq", item["last_snapshot_seq"] - 1),
        "timestamp": lambda item: item.__setitem__("last_seen", "2026-01-01T00:01:39+00:00"),
    }
    for label, mutate in mutations.items():
        forged = copy.deepcopy(valid_record)
        mutate(forged)
        result = rules.arbitrate_survival_emergency(state, [forged])
        check(result["status"] == "blocked", f"{label} witness mutation was accepted")
        check(mutation_expectations[label] in result["blockers"], f"{label} witness lacked its fixed blocker")
        blockers = rules.survival_certificate_blockers(
            valid_certificate,
            source_for(state),
            [projected_incident(forged)],
        )
        check(blockers, f"certificate accepted {label} witness mutation")

    zone = dt.timezone(dt.timedelta(hours=8))
    timezone_state = copy.deepcopy(state)
    timezone_state["ts"] = dt.datetime.fromisoformat(state["ts"]).astimezone(zone).isoformat(timespec="microseconds")
    timezone_record = copy.deepcopy(valid_record)
    for key in ("first_seen", "last_seen"):
        timezone_record[key] = dt.datetime.fromisoformat(valid_record[key]).astimezone(zone).isoformat(timespec="microseconds")
    check(
        rules.survival_certificate_blockers(
            valid_certificate,
            source_for(timezone_state),
            [projected_incident(timezone_record)],
        )
        == [],
        "equivalent timezone timestamps were rejected",
    )

    fractional_state = make_state(["suffocation"], timestamp="2026-01-01T00:00:00.5339642+00:00")
    fractional_record = witness(fractional_state, "suffocation")
    fractional_record["first_seen"] = fractional_state["ts"]
    fractional_record["last_seen"] = fractional_state["ts"]
    fractional_result = rules.arbitrate_survival_emergency(fractional_state, [fractional_record])
    check(fractional_result["status"] == "selected", "seven-digit fractional timestamp lost immediate pause")
    fractional_certificate = rules.build_survival_arbitration_certificate(fractional_state, [fractional_record])
    check(
        rules.survival_certificate_blockers(
            fractional_certificate,
            source_for(fractional_state),
            [projected_incident(fractional_record)],
        )
        == [],
        "seven-digit fractional certificate was rejected",
    )

    command, _policy, frame = approval_fixture()
    check(rules.survival_suggested_command_frame_blockers(command, frame) == [], "baseline source episode binding failed")
    metadata_tamper = copy.deepcopy(command)
    metadata_tamper["reason"] = "tampered reason"
    metadata_blockers = rules.survival_suggested_command_frame_blockers(metadata_tamper, frame)
    check("survival_rule_metadata_mismatch" in metadata_blockers, "command metadata tamper was accepted")
    hint_tamper = copy.deepcopy(command)
    hint_tamper["args"] = {"paused": False}
    hint_blockers = rules.survival_suggested_command_frame_blockers(hint_tamper, frame)
    check("survival_command_hint_mismatch" in hint_blockers, "command hint tamper was accepted")
    episode_tamper = copy.deepcopy(command)
    episode_tamper["source"] = dict(episode_tamper["source"])
    episode_tamper["source"]["incident_opened_at"] = "2026-08-30T08:59:59+00:00"
    episode_blockers = rules.survival_suggested_command_frame_blockers(episode_tamper, frame)
    check("survival_incident_opened_at_mismatch" in episode_blockers, "source episode tamper was accepted")
    advice_tamper = copy.deepcopy(frame)
    advice_tamper["projections"]["advice"]["items"][0]["incident_opened_at"] = "2026-08-30T08:59:59+00:00"
    advice_blockers = rules.survival_suggested_command_frame_blockers(command, advice_tamper)
    check("survival_advice_binding_mismatch" in advice_blockers, "advice episode tamper was accepted")

    legacy_frame = copy.deepcopy(frame)
    legacy_frame["projections"]["incidents"].pop("survival_emergency", None)
    rehash_frame(legacy_frame)
    legacy_command = copy.deepcopy(command)
    legacy_command["source_frame_id"] = legacy_frame["frame_id"]
    try:
        validate_approval_inputs(legacy_command, _policy, legacy_frame)
    except ApprovalReceiptError as exc:
        legacy_error = str(exc).lower()
    else:
        legacy_error = ""
    check("survival certificate" in legacy_error, "missing legacy certificate was accepted")
    return {
        "identity_mutations": sorted(mutations),
        "timezone_equivalent": True,
        "probe_fractional_timestamp": "2026-01-01T00:00:00.5339642+00:00",
        "metadata_binding": True,
        "explicit_snapshot_seq_binding": True,
        "legacy_certificate_rejected": True,
    }


def case_blocked_context_and_operator_compatibility() -> Dict[str, Any]:
    trust_state = make_state(["suffocation"])
    trust_state["bridge"]["trust_ready"] = False
    trust_certificate = rules.build_survival_arbitration_certificate(trust_state, [])
    check(trust_certificate["status"] == "blocked", "untrusted producer context was not blocked")
    check(trust_certificate["blockers"] == ["state_not_trusted_live"], "untrusted producer blocker drifted")
    check(trust_certificate["active_pause_alert_ids"] == [], "untrusted producer published active IDs")
    check(trust_certificate["selected_incident_id"] is None, "untrusted producer published selection")
    check(rules.survival_certificate_blockers(trust_certificate, source_for(trust_state), []) == [], "untrusted blocked envelope was rejected")

    quality_state = make_state(["suffocation"])
    del quality_state["quality"]["alerts"]
    quality_certificate = rules.build_survival_arbitration_certificate(quality_state, [])
    check(quality_certificate["status"] == "blocked", "unknown-quality producer context was not blocked")
    check(quality_certificate["blockers"] == ["alerts_contract_unusable"], "unknown-quality blocker drifted")
    check(quality_certificate["selected_incident_id"] is None, "unknown-quality producer published selection")
    check(rules.survival_certificate_blockers(quality_certificate, source_for(quality_state), []) == [], "unknown-quality blocked envelope was rejected")

    trust_values = healthy_inputs(actions_enabled=False, suggestions_enabled=True)
    trust_values["state"]["bridge"].update(
        {"stale": True, "replay_mode": False, "trust_ready": False, "trust_blockers": ["fixture"]}
    )
    trust_values["health"]["status"] = "stale"
    rebind_values(trust_values)
    trust_frame = build_frame(trust_values)
    validate_decision_frame(trust_frame)
    trust_projection = trust_frame["projections"]["incidents"]["survival_emergency"]
    check(trust_frame["trust"]["status"] == "blocked", "untrusted observation frame was not blocked")
    check(trust_projection["status"] == "blocked", "untrusted observation certificate was not blocked")

    quality_values = healthy_inputs(actions_enabled=False, suggestions_enabled=True)
    quality_values["state"]["bridge"].update(
        {"stale": False, "replay_mode": False, "trust_ready": True, "trust_blockers": []}
    )
    quality_values["state"]["game"]["paused"] = False
    quality_values["state"]["alerts"] = ["suffocation"]
    quality_values["state"]["quality"]["game.paused"] = contract_quality_fixture(GAME_PAUSED_CONTRACT_ID)
    quality_values["state"]["quality"].pop("alerts", None)
    rebind_values(quality_values)
    quality_frame = build_frame(quality_values)
    validate_decision_frame(quality_frame)
    quality_projection = quality_frame["projections"]["incidents"]["survival_emergency"]
    check(quality_projection["status"] == "blocked", "unknown-quality observation certificate was not blocked")
    check(quality_projection["blockers"] == ["alerts_contract_unusable"], "unknown-quality observation blocker drifted")

    accepted_modes: List[str] = []
    for mode in ("runtime_validation", "planned"):
        command, policy, frame = approval_fixture(proposal_blocked=True, mode=mode)
        validate_approval_inputs(command, policy, frame)
        accepted_modes.append(mode)
    check(accepted_modes == ["runtime_validation", "planned"], "operator/planned approval compatibility drifted")
    return {
        "blocked_frames": ["trust", "quality"],
        "operator_modes_accepted": accepted_modes,
    }


def run_case(name: str, function: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
    started = time.perf_counter()
    try:
        details = function()
        return {
            "name": name,
            "status": "passed",
            "duration_seconds": round(time.perf_counter() - started, 3),
            "details": details,
        }
    except Exception as exc:  # noqa: BLE001 - report every case as structured evidence
        return {
            "name": name,
            "status": "failed",
            "duration_seconds": round(time.perf_counter() - started, 3),
            "error": f"{type(exc).__name__}: {exc}",
        }


def main() -> int:
    cases: Iterable[tuple[str, Callable[[], Dict[str, Any]]]] = (
        ("legacy_hidden_higher_witness", case_legacy_hidden_higher_witness),
        ("policy_and_controls", case_policy_and_controls),
        ("positive_direct_and_frame", case_positive_direct_and_frame),
        ("hysteresis_boundaries", case_hysteresis_boundaries),
        ("higher_priority_pairwise_fail_closed", case_higher_priority_pairwise_fail_closed),
        ("certificate_tamper_and_projection_edges", case_certificate_tamper_and_projection_edges),
        ("identity_timestamp_and_hysteresis_binding", case_identity_timestamp_and_hysteresis_binding),
        ("blocked_context_and_operator_compatibility", case_blocked_context_and_operator_compatibility),
    )
    results = [run_case(name, function) for name, function in cases]
    passed = all(item["status"] == "passed" for item in results)
    report = {
        "schema": "oni.survival-certificate-verification.v1",
        "status": "passed" if passed else "failed",
        "case_count": len(results),
        "assertion_count": _assertion_count,
        "pair_count": 36,
        "pair_variant_count": 180,
        "oracle": "fixed_policy_rank_and_blocker_expectations",
        "offline_only": True,
        "cases": results,
        "limitations": [
            "current decision-frame.v2 source binds snapshot_seq explicitly; legacy certificate archives may still derive it only from agreeing active witnesses",
            "certificate consistency does not authenticate Probe sample origin or prove every sample was real",
            "no live Klei/save/Mod/Bridge/bootstrap or formal end-to-end acceptance is claimed",
        ],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
