#!/usr/bin/env python3
"""Offline verification for the fail-closed unified command-pipeline gate."""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import pathlib
import sys
import tempfile
from typing import Any, Callable, Dict, Mapping


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.canonical_hash import canonical_hash
from agent import rules as agent_rules
from agent.approval_receipt import build_approved_command, validate_approval_receipt
from agent.colony_plan import ColonyPlanError, advance_colony_plan, create_colony_plan, load_active_plan
from agent.command_queue import (
    build_command,
    command_pipeline_readiness,
    ensure_command_dirs,
    stable_command_id,
    sync_pending_commands,
    write_json_atomic,
)
from agent.decision import validate_decision_frame
from agent.field_contracts import ALERTS_CANONICAL_CONTRACT_ID, contract_quality_fixture
from agent.incidents import IncidentTracker
from scripts.verify_colony_plan import priority_definition, state as plan_state, write_live_state
from scripts.verify_command_safety import advice, state, write_authorizing_frame


SAVE_HASH = "sha256:" + "b" * 64


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def policy() -> Dict[str, Any]:
    return {
        "schema": "oni.action-policy.v1",
        "actions_enabled": True,
        "suggestions_enabled": True,
        "allowed_actions": ["set_paused"],
        "allowed_save_hashes": [SAVE_HASH],
        "command_ttl_seconds": 30,
        "max_pending_commands": 20,
    }


def rehash_frame(frame: Dict[str, Any]) -> None:
    frame_payload = {key: value for key, value in frame.items() if key != "frame_id"}
    frame["frame_id"] = "sha256:" + hashlib.sha256(
        json.dumps(
            frame_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def context(root: pathlib.Path) -> tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    snapshot = state()
    current_policy = policy()
    write_json_atomic(root / "action-policy.json", current_policy)
    frame = write_authorizing_frame(root, snapshot)
    return snapshot, frame, current_policy


def exact_rule_candidate_context(
    root: pathlib.Path,
    *,
    active_alert_ids: list[str] | None = None,
    selected_alert_id: str = "low_oxygen",
) -> tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    """Build the one current mutation that the production rules can emit.

    Synthetic incident ids are useful for queue-isolation tests, but they must
    never qualify as trusted ControlPacket candidates.  This fixture instead
    uses the exact active low-oxygen rule command and binds that incident/risk
    into a fully validated frame projection.
    """

    active_ids = sorted(active_alert_ids or [selected_alert_id])
    final_time = dt.datetime.now(dt.timezone.utc).astimezone().replace(microsecond=321456)
    start_time = final_time - dt.timedelta(seconds=30)
    tracker = IncidentTracker()
    snapshot: Dict[str, Any] = {}
    persistent_ids = {
        alert_id
        for alert_id in active_ids
        if int(agent_rules.SURVIVAL_ALERT_POLICY[alert_id]["samples"]) > 1
        or float(agent_rules.SURVIVAL_ALERT_POLICY[alert_id]["duration"]) > 0.0
    }
    for index in range(7):
        sample = state()
        sample["seq"] = index + 2
        sample["snapshot_seq"] = index + 2
        sample["snapshot_id"] = f"{sample['session_id']}:{sample['seq']}"
        sample["ts"] = (start_time + dt.timedelta(seconds=index * 5)).isoformat()
        sample["alerts"] = sorted(active_ids if index == 6 else persistent_ids)
        sample["quality"]["alerts"] = contract_quality_fixture(
            ALERTS_CANONICAL_CONTRACT_ID
        )
        tracker.update(sample, agent_rules.evaluate_signals(sample))
        snapshot = sample
    require(selected_alert_id in tracker.incidents, "selected rule incident was not tracked")
    selected_incident = tracker.incidents[selected_alert_id]
    require(selected_incident.get("status") == "open", "selected rule incident did not satisfy hysteresis")
    current_policy = policy()
    write_json_atomic(root / "action-policy.json", current_policy)
    frame = write_authorizing_frame(root, snapshot)
    signal = next(
        item
        for item in agent_rules.evaluate_signals(snapshot)
        if item.get("incident_id") == selected_alert_id and item.get("state") == "active"
    )
    rule_advice = {
        "incident_id": signal["incident_id"],
        "incident_opened_at": selected_incident["first_seen"],
        "risk": signal["risk"],
        "title": signal["title"],
        "reason": signal["reason"],
        "evidence": copy.deepcopy(signal["evidence"]),
        "source_snapshot_id": snapshot["snapshot_id"],
        "command": copy.deepcopy(signal["command"]),
    }
    frame = copy.deepcopy(frame)
    open_incidents = [
        item for item in tracker.open_incidents() if item.get("incident_id") in active_ids
    ]
    frame["projections"]["advice"] = {
        "valid": True,
        "count": len(open_incidents),
        "truncated": False,
        "items": [
            {
                "incident_id": item["incident_id"],
                "risk": item["risk"],
                "incident_opened_at": item["first_seen"],
            }
            for item in open_incidents
        ],
    }
    frame["projections"]["incidents"] = {
        "valid": True,
        "count": len(open_incidents),
        "truncated": False,
        "items": [
            {
                key: item.get(key)
                for key in (
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
            }
            for item in open_incidents
        ],
        "survival_emergency": agent_rules.build_survival_arbitration_certificate(
            snapshot,
            open_incidents,
        ),
    }
    rehash_frame(frame)
    validate_decision_frame(frame)
    write_json_atomic(root / "decision-frame.json", frame)
    return snapshot, frame, current_policy, rule_advice


def suggested_command(
    snapshot: Dict[str, Any], frame: Mapping[str, Any], current_policy: Mapping[str, Any]
) -> Dict[str, Any]:
    return build_command(
        snapshot,
        advice(),
        advice()["command"],
        ttl_seconds=30,
        source_frame_id=str(frame["frame_id"]),
        source_policy_hash=canonical_hash(dict(current_policy)),
    )


def write_stage(root: pathlib.Path, stage: str, command: Mapping[str, Any], *, name: str | None = None) -> pathlib.Path:
    path = ensure_command_dirs(root)[stage] / (name or f"{command['id']}.json")
    write_json_atomic(path, dict(command))
    return path


def blocker(gate: Mapping[str, Any], prefix: str) -> str:
    blockers = gate.get("blockers")
    require(isinstance(blockers, list), "proposal blocker list is missing")
    match = next((str(item) for item in blockers if str(item).startswith(prefix)), None)
    require(match is not None, f"expected blocker {prefix!r}; observed {blockers!r}")
    return match


def mutation_advice(snapshot: Mapping[str, Any], incident_id: str) -> Dict[str, Any]:
    item = copy.deepcopy(advice())
    item["incident_id"] = incident_id
    item["incident_opened_at"] = dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")
    item["source_snapshot_id"] = snapshot["snapshot_id"]
    return item


def note_advice(snapshot: Mapping[str, Any], incident_id: str = "pipeline-note") -> Dict[str, Any]:
    return {
        "incident_id": incident_id,
        "incident_opened_at": dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds"),
        "risk": "none",
        "title": "Pipeline note",
        "reason": "Non-mutating fixture",
        "evidence": {},
        "source_snapshot_id": snapshot["snapshot_id"],
        "command": {
            "action": "focus_note",
            "args": {"note": "mutation remains selected"},
            "preconditions": [],
            "expected_effects": [],
        },
    }


def operator_pending_command(
    snapshot: Dict[str, Any],
    frame: Mapping[str, Any],
    source_policy: Mapping[str, Any],
    *,
    mode: str,
    command_id: str,
) -> Dict[str, Any]:
    item = mutation_advice(snapshot, f"{command_id}-fixture")
    command = build_command(
        snapshot,
        item,
        item["command"],
        ttl_seconds=300,
        source_frame_id=str(frame["frame_id"]),
        source_policy_hash=canonical_hash(dict(source_policy)),
    )
    command.update(
        {
            "id": command_id,
            "mode": mode,
            "source_incident_id": None,
        }
    )
    source = dict(command["source"])
    if mode == "runtime_validation":
        source["kind"] = "runtime_validation"
        command["evidence"] = {"source_snapshot_id": snapshot["snapshot_id"]}
    elif mode == "planned":
        source.update(
            {
                "kind": "colony_plan",
                "plan_id": "plan-pending-latch-fixture",
                "plan_revision": 1,
                "step_id": f"step-{command_id}",
            }
        )
        command["evidence"] = {
            "plan_id": source["plan_id"],
            "plan_revision": source["plan_revision"],
            "step_id": source["step_id"],
        }
    command["source"] = source
    return command


def case_clean_ready(root: pathlib.Path) -> Dict[str, Any]:
    snapshot, frame, current_policy = context(root)
    gate = command_pipeline_readiness(
        root,
        latest_state=snapshot,
        proposal_frame=frame,
        policy=current_policy,
    )
    require(gate.get("schema") == "oni.command-pipeline-gate.v1", "gate schema changed")
    require(gate.get("ready") is True and gate.get("blockers") == [], "clean queue was blocked")
    confirmation = gate["readiness"]["mutation_confirmation"]
    require(confirmation["ready"] is True, "clean confirmation pipeline was blocked")
    require(confirmation["candidate_ids"] == [], "clean queue fabricated a confirmation candidate")
    require(gate.get("pending_candidates") == [], "clean queue fabricated a pending candidate")
    return {"proposal_ready": True, "confirmation_ready": True, "counts": gate["counts"]}


def case_one_new_mutation_per_frame_notes_do_not_consume_slot(root: pathlib.Path) -> Dict[str, Any]:
    snapshot, frame, current_policy, first = exact_rule_candidate_context(root)
    second = mutation_advice(snapshot, "single-intent-second")
    note = note_advice(snapshot, "single-intent-note")

    created = sync_pending_commands(
        root,
        snapshot,
        [note, first, second],
        proposal_frame=frame,
    )
    mutating = [item for item in created if item["action"] not in {"focus_note", "noop"}]
    non_mutating = [item for item in created if item["action"] in {"focus_note", "noop"}]
    require(len(mutating) == 1, f"one frame emitted {len(mutating)} mutations")
    require(len(non_mutating) == 1, "the note consumed the frame's mutation-selection slot")
    require(
        all(item.get("source_frame_id") == frame["frame_id"] for item in created),
        "created commands did not bind the same decision frame",
    )

    gate = command_pipeline_readiness(
        root,
        latest_state=snapshot,
        proposal_frame=frame,
        policy=current_policy,
    )
    selection = blocker(gate, f"mutation_pending_selection:{mutating[0]['id']}")
    candidates = gate.get("pending_candidates") or []
    require(len(candidates) == 1, "the exact current pending mutation was not summarized")
    require(candidates[0]["command_id"] == mutating[0]["id"], "pending candidate id drifted")
    require(
        set(candidates[0])
        == {"command_id", "action", "intent_hash", "risk", "requires_approval", "auto_execute"},
        "pending candidate summary leaked or omitted fields",
    )
    require(
        not ({"path", "args", "preconditions", "expected_effects", "body"} & set(candidates[0])),
        "pending candidate summary leaked command material",
    )
    require(
        gate["readiness"]["mutation_confirmation"]["ready"] is True,
        "a pending selection blocked unrelated confirmation recovery",
    )
    return {
        "created_actions": [item["action"] for item in created],
        "mutating_count": len(mutating),
        "non_mutating_count": len(non_mutating),
        "selection_blocker": selection,
    }


def case_preframe_scan_defers_pending_selection(root: pathlib.Path) -> Dict[str, Any]:
    snapshot, frame, current_policy, selected_advice = exact_rule_candidate_context(root)
    selected = build_command(
        snapshot,
        selected_advice,
        selected_advice["command"],
        ttl_seconds=30,
        source_frame_id=str(frame["frame_id"]),
        source_policy_hash=canonical_hash(current_policy),
    )
    write_stage(root, "pending", selected)

    preframe = command_pipeline_readiness(
        root,
        latest_state=snapshot,
        proposal_frame=None,
        policy=current_policy,
    )
    require(
        not any(item.startswith("mutation_pending_selection:") for item in preframe["blockers"]),
        "a pre-frame health scan fabricated a fully-bound pending selection",
    )
    require(preframe["ready"] is True, "pre-frame health scan became self-referentially blocked")
    require(
        preframe.get("evaluation_context")
        == {
            "phase": "pre_frame",
            "proposal_frame_id": None,
            "state_hash": frame["source"]["state_hash"],
            "policy_hash": canonical_hash(current_policy),
        },
        "pre-frame evaluation context drifted",
    )
    forged = command_pipeline_readiness(
        root,
        latest_state=snapshot,
        proposal_frame={"frame_id": frame["frame_id"]},
        policy=current_policy,
    )
    require(
        forged["evaluation_context"]["phase"] == "invalid_frame",
        "a forged id-only frame was marked frame-bound",
    )
    bound = command_pipeline_readiness(
        root,
        latest_state=snapshot,
        proposal_frame=frame,
        policy=current_policy,
    )
    selection = blocker(bound, f"mutation_pending_selection:{selected['id']}")
    require(
        bound["evaluation_context"]
        == {
            "phase": "frame_bound",
            "proposal_frame_id": frame["frame_id"],
            "state_hash": frame["source"]["state_hash"],
            "policy_hash": frame["projections"]["policy"]["policy_hash"],
        },
        "frame-bound evaluation context does not bind frame/state/policy",
    )
    require(preframe.get("pending_candidates") == [], "pre-frame scan exposed an unbound candidate")
    require(
        [item["command_id"] for item in bound.get("pending_candidates") or []] == [selected["id"]],
        "frame-bound scan did not expose the exact candidate",
    )
    return {
        "preframe_ready": preframe["ready"],
        "bound_ready": bound["ready"],
        "selection_blocker": selection,
    }


def case_pending_selection_blocks_next_mutation_but_allows_note(root: pathlib.Path) -> Dict[str, Any]:
    snapshot, frame, current_policy = context(root)
    selected = mutation_advice(snapshot, "pending-selected")
    next_mutation = mutation_advice(snapshot, "pending-next")
    created = sync_pending_commands(root, snapshot, [selected], proposal_frame=frame)
    require(len(created) == 1, "initial mutation selection was not created")
    selected_id = created[0]["id"]

    gate = command_pipeline_readiness(
        root,
        latest_state=snapshot,
        proposal_frame=frame,
        policy=current_policy,
    )
    selection = blocker(gate, f"mutation_pending_selection:{selected_id}")
    note = note_advice(snapshot, "pending-note")
    follow_up = sync_pending_commands(
        root,
        snapshot,
        [selected, next_mutation, note],
        proposal_frame=frame,
    )
    require(
        len(follow_up) == 1 and follow_up[0]["action"] == "focus_note",
        "pending selection did not isolate the next mutation from a non-mutating note",
    )
    pending = [
        json.loads(path.read_text(encoding="utf-8-sig"))
        for path in sorted(ensure_command_dirs(root)["pending"].glob("*.json"))
    ]
    pending_mutations = [item for item in pending if item.get("action") not in {"focus_note", "noop"}]
    require(
        [item["id"] for item in pending_mutations] == [selected_id],
        "the selected mutation was replaced or joined by another mutation",
    )
    return {
        "selection_blocker": selection,
        "selected_id": selected_id,
        "follow_up_actions": [item["action"] for item in follow_up],
    }


def case_tampered_pending_candidate_excluded(root: pathlib.Path) -> Dict[str, Any]:
    excluded: list[str] = []

    def recompute_id(command: Dict[str, Any]) -> None:
        source = command.get("source") if isinstance(command.get("source"), dict) else {}
        command["id"] = stable_command_id(
            command.get("session_id"),
            str(command.get("source_incident_id") or ""),
            source.get("incident_opened_at"),
            str(command.get("action") or ""),
            proposal={
                "args": command.get("args"),
                "preconditions": command.get("preconditions"),
                "expected_effects": command.get("expected_effects"),
            },
            source_frame_id=command.get("source_frame_id"),
            state_hash=command.get("state_hash"),
            source_policy_hash=source.get("policy_hash"),
        )

    def args_type(command: Dict[str, Any]) -> None:
        command["args"] = "forged"
        recompute_id(command)

    def condition_drift(command: Dict[str, Any]) -> None:
        command["preconditions"][0]["value"] = "forged_alert"
        recompute_id(command)

    def id_drift(command: Dict[str, Any]) -> None:
        command["id"] = "cmd-forged-id"

    def reason_drift(command: Dict[str, Any]) -> None:
        command["reason"] = "forged reason"

    def hidden_field(command: Dict[str, Any]) -> None:
        command["hidden"] = "forged"

    def hidden_source_field(command: Dict[str, Any]) -> None:
        command["source"]["hidden"] = "forged"

    def state_timestamp_drift(command: Dict[str, Any]) -> None:
        command["source"]["state_ts"] = "2000-01-01T00:00:00+00:00"

    def opened_at_recomputed_id(command: Dict[str, Any]) -> None:
        command["source"]["incident_opened_at"] = "2000-01-01T00:00:00+00:00"
        recompute_id(command)

    def created_at_drift(command: Dict[str, Any]) -> None:
        command["created_at"] = "2000-01-01T00:00:00+00:00"

    def expiry_extension(command: Dict[str, Any]) -> None:
        command["expires_at"] = "2099-01-01T00:00:00+00:00"

    variants = {
        "args_recomputed_id": args_type,
        "condition_recomputed_id": condition_drift,
        "id_drift": id_drift,
        "reason_drift": reason_drift,
        "hidden_command_field": hidden_field,
        "hidden_source_field": hidden_source_field,
        "source_state_ts_drift": state_timestamp_drift,
        "opened_at_recomputed_id": opened_at_recomputed_id,
        "created_at_drift": created_at_drift,
        "expiry_extension": expiry_extension,
    }
    for name, mutate in variants.items():
        case_root = root / name
        snapshot, frame, current_policy, selected_advice = exact_rule_candidate_context(case_root)
        command = build_command(
            snapshot,
            selected_advice,
            selected_advice["command"],
            ttl_seconds=30,
            source_frame_id=str(frame["frame_id"]),
            source_policy_hash=canonical_hash(current_policy),
        )
        write_stage(case_root, "pending", command)
        baseline = command_pipeline_readiness(
            case_root,
            latest_state=snapshot,
            proposal_frame=frame,
            policy=current_policy,
        )
        require(
            [item["command_id"] for item in baseline.get("pending_candidates") or []]
            == [command["id"]],
            f"{name} fixture did not begin with one exact rules candidate",
        )
        (ensure_command_dirs(case_root)["pending"] / f"{command['id']}.json").unlink()
        mutate(command)
        write_stage(case_root, "pending", command)
        gate = command_pipeline_readiness(
            case_root,
            latest_state=snapshot,
            proposal_frame=frame,
            policy=current_policy,
        )
        blocker(gate, f"mutation_pending_selection:{command['id']}")
        require(
            gate.get("pending_candidates") == [],
            f"{name} remained eligible as a pending candidate",
        )
        excluded.append(name)
    return {"candidate_excluded": sorted(excluded)}


def case_noncanonical_pending_filename_fails_closed(root: pathlib.Path) -> Dict[str, Any]:
    snapshot, frame, current_policy, selected_advice = exact_rule_candidate_context(root)
    command = build_command(
        snapshot,
        selected_advice,
        selected_advice["command"],
        ttl_seconds=30,
        source_frame_id=str(frame["frame_id"]),
        source_policy_hash=canonical_hash(current_policy),
    )
    write_stage(root, "pending", command, name="renamed-safe.json")
    gate = command_pipeline_readiness(
        root,
        latest_state=snapshot,
        proposal_frame=frame,
        policy=current_policy,
    )
    expected = "command_path_noncanonical:pending:renamed-safe.json"
    require(expected in gate.get("blockers", []), "noncanonical pending path lacked an explicit blocker")
    require(gate.get("pending_candidates") == [], "noncanonical pending path became a candidate")
    require(gate.get("counts", {}).get("pending") == 0, "noncanonical pending path entered trusted counts")
    invalid = gate.get("invalid_records") or []
    require(
        invalid == [
            {
                "stage": "pending",
                "path": "commands/pending/renamed-safe.json",
                "reason": "command_path_noncanonical",
            }
        ],
        f"noncanonical path diagnostic drifted: {invalid!r}",
    )
    return {"blocker": expected, "candidate_count": 0}


def case_survival_witness_and_priority_fail_closed(root: pathlib.Path) -> Dict[str, Any]:
    baseline_root = root / "baseline"
    snapshot, frame, current_policy, selected_advice = exact_rule_candidate_context(baseline_root)
    baseline_command = build_command(
        snapshot,
        selected_advice,
        selected_advice["command"],
        ttl_seconds=30,
        source_frame_id=str(frame["frame_id"]),
        source_policy_hash=canonical_hash(current_policy),
    )
    write_stage(baseline_root, "pending", baseline_command)
    baseline_gate = command_pipeline_readiness(
        baseline_root,
        latest_state=snapshot,
        proposal_frame=frame,
        policy=current_policy,
    )
    require(
        [item["command_id"] for item in baseline_gate.get("pending_candidates") or []]
        == [baseline_command["id"]],
        "real 7-sample/30-second witness did not produce one candidate",
    )

    def one_sample(item: Dict[str, Any]) -> None:
        item.update(
            {
                "consecutive_samples": 1,
                "first_seq": item["last_seq"],
                "first_snapshot_seq": item["last_snapshot_seq"],
                "first_seen": item["last_seen"],
            }
        )

    def six_samples(item: Dict[str, Any]) -> None:
        item["consecutive_samples"] = 6
        item["first_snapshot_seq"] = item["last_snapshot_seq"] - 5

    def short_duration(item: Dict[str, Any]) -> None:
        last_seen = dt.datetime.fromisoformat(str(item["last_seen"]).replace("Z", "+00:00"))
        item["first_seen"] = (last_seen - dt.timedelta(seconds=29, microseconds=1)).isoformat()

    witness_mutations: Dict[str, Callable[[Dict[str, Any]], None]] = {
        "one_sample_forged_open": one_sample,
        "six_samples": six_samples,
        "short_duration": short_duration,
        "wrong_session": lambda item: item.__setitem__("session_id", "forged-session"),
        "wrong_snapshot": lambda item: item.__setitem__("snapshot_id", "forged-session:8"),
        "wrong_sequence": lambda item: item.__setitem__("last_seq", item["last_seq"] - 1),
        "snapshot_sequence_gap": lambda item: item.__setitem__(
            "first_snapshot_seq", item["first_snapshot_seq"] - 1
        ),
        "inactive_telemetry": lambda item: item.__setitem__("telemetry_state", "inactive"),
        "wrong_risk": lambda item: item.__setitem__("risk", "low"),
    }
    excluded: list[str] = []
    for label, mutate in witness_mutations.items():
        case_root = root / label
        snapshot, frame, current_policy, selected_advice = exact_rule_candidate_context(case_root)
        incident = next(
            item
            for item in frame["projections"]["incidents"]["items"]
            if item["incident_id"] == "low_oxygen"
        )
        mutate(incident)
        if label in {"one_sample_forged_open", "short_duration"}:
            selected_advice["incident_opened_at"] = incident["first_seen"]
            next(
                item
                for item in frame["projections"]["advice"]["items"]
                if item["incident_id"] == "low_oxygen"
            )["incident_opened_at"] = incident["first_seen"]
        rehash_frame(frame)
        try:
            validate_decision_frame(frame)
        except Exception:
            pass
        command = build_command(
            snapshot,
            selected_advice,
            selected_advice["command"],
            ttl_seconds=30,
            source_frame_id=str(frame["frame_id"]),
            source_policy_hash=canonical_hash(current_policy),
        )
        write_stage(case_root, "pending", command)
        gate = command_pipeline_readiness(
            case_root,
            latest_state=snapshot,
            proposal_frame=frame,
            policy=current_policy,
        )
        require(gate.get("pending_candidates") == [], f"{label} witness remained a candidate")
        excluded.append(label)

    projection_mutations: Dict[str, Callable[[Dict[str, Any]], None]] = {
        "incidents_truncated": lambda value: value["projections"]["incidents"].update(
            {"count": 101, "truncated": True}
        ),
        "advice_truncated": lambda value: value["projections"]["advice"].update(
            {"count": 21, "truncated": True}
        ),
        "incident_omitted": lambda value: value["projections"]["incidents"].update(
            {"count": 0, "truncated": False, "items": []}
        ),
        "incident_duplicate": lambda value: value["projections"]["incidents"].update(
            {
                "count": 2,
                "items": value["projections"]["incidents"]["items"]
                + [copy.deepcopy(value["projections"]["incidents"]["items"][0])],
            }
        ),
        "advice_duplicate": lambda value: value["projections"]["advice"].update(
            {
                "count": 2,
                "items": value["projections"]["advice"]["items"]
                + [copy.deepcopy(value["projections"]["advice"]["items"][0])],
            }
        ),
    }
    for label, mutate in projection_mutations.items():
        case_root = root / label
        snapshot, frame, current_policy, selected_advice = exact_rule_candidate_context(case_root)
        mutate(frame)
        rehash_frame(frame)
        try:
            validate_decision_frame(frame)
        except Exception:
            pass
        command = build_command(
            snapshot,
            selected_advice,
            selected_advice["command"],
            ttl_seconds=30,
            source_frame_id=str(frame["frame_id"]),
            source_policy_hash=canonical_hash(current_policy),
        )
        write_stage(case_root, "pending", command)
        gate = command_pipeline_readiness(
            case_root,
            latest_state=snapshot,
            proposal_frame=frame,
            policy=current_policy,
        )
        require(gate.get("pending_candidates") == [], f"{label} projection remained a candidate")
        excluded.append(label)

    lower_root = root / "higher_priority_lower_candidate"
    snapshot, frame, current_policy, low_advice = exact_rule_candidate_context(
        lower_root,
        active_alert_ids=["low_oxygen", "suffocation"],
        selected_alert_id="low_oxygen",
    )
    low_command = build_command(
        snapshot,
        low_advice,
        low_advice["command"],
        ttl_seconds=30,
        source_frame_id=str(frame["frame_id"]),
        source_policy_hash=canonical_hash(current_policy),
    )
    write_stage(lower_root, "pending", low_command)
    lower_gate = command_pipeline_readiness(
        lower_root,
        latest_state=snapshot,
        proposal_frame=frame,
        policy=current_policy,
    )
    require(lower_gate.get("pending_candidates") == [], "lower-priority hazard displaced suffocation")

    winner_root = root / "higher_priority_winner"
    snapshot, frame, current_policy, winner_advice = exact_rule_candidate_context(
        winner_root,
        active_alert_ids=["low_oxygen", "suffocation"],
        selected_alert_id="suffocation",
    )
    winner = build_command(
        snapshot,
        winner_advice,
        winner_advice["command"],
        ttl_seconds=30,
        source_frame_id=str(frame["frame_id"]),
        source_policy_hash=canonical_hash(current_policy),
    )
    write_stage(winner_root, "pending", winner)
    winner_gate = command_pipeline_readiness(
        winner_root,
        latest_state=snapshot,
        proposal_frame=frame,
        policy=current_policy,
    )
    require(
        [item["command_id"] for item in winner_gate.get("pending_candidates") or []]
        == [winner["id"]],
        "higher-priority suffocation winner did not remain the sole candidate",
    )
    return {
        "baseline_candidate": baseline_command["id"],
        "excluded": sorted(excluded),
        "lower_priority_suppressed": True,
        "higher_priority_selected": "suffocation",
    }


def case_multiple_current_pending_mutations_fail_closed(root: pathlib.Path) -> Dict[str, Any]:
    snapshot, frame, current_policy = context(root)
    first_advice = mutation_advice(snapshot, "legacy-selection-first")
    second_advice = mutation_advice(snapshot, "legacy-selection-second")
    first = build_command(
        snapshot,
        first_advice,
        first_advice["command"],
        ttl_seconds=30,
        source_frame_id=str(frame["frame_id"]),
        source_policy_hash=canonical_hash(current_policy),
    )
    second = build_command(
        snapshot,
        second_advice,
        second_advice["command"],
        ttl_seconds=30,
        source_frame_id=str(frame["frame_id"]),
        source_policy_hash=canonical_hash(current_policy),
    )
    write_stage(root, "pending", first)
    write_stage(root, "pending", second)

    gate = command_pipeline_readiness(
        root,
        latest_state=snapshot,
        proposal_frame=frame,
        policy=current_policy,
    )
    expected = sorted(
        [f"mutation_pending_selection:{first['id']}", f"mutation_pending_selection:{second['id']}"]
    )
    observed = [item for item in gate["blockers"] if item.startswith("mutation_pending_selection:")]
    require(observed == expected, f"multiple pending selections were not exposed exactly: {observed!r}")
    require(
        gate.get("pending_candidates") == [],
        "synthetic multi-selection commands were promoted to trusted candidates",
    )

    note = note_advice(snapshot, "legacy-selection-note")
    created = sync_pending_commands(
        root,
        snapshot,
        [first_advice, second_advice, note],
        proposal_frame=frame,
    )
    require(
        len(created) == 1 and created[0]["action"] == "focus_note",
        "multi-selection state emitted another mutation or blocked the note",
    )
    remaining_ids = sorted(
        json.loads(path.read_text(encoding="utf-8-sig"))["id"]
        for path in ensure_command_dirs(root)["pending"].glob("*.json")
        if json.loads(path.read_text(encoding="utf-8-sig")).get("action") not in {"focus_note", "noop"}
    )
    require(
        remaining_ids == sorted([first["id"], second["id"]]),
        "synchronization silently arbitrated multiple current pending mutations",
    )
    return {
        "selection_blockers": observed,
        "preserved_mutation_ids": remaining_ids,
        "created_actions": [item["action"] for item in created],
    }


def case_stale_operator_pending_latches_without_arbitration(root: pathlib.Path) -> Dict[str, Any]:
    observed: Dict[str, Any] = {}
    variants = (
        ("runtime_validation", "frame"),
        ("runtime_validation", "policy"),
        ("planned", "frame"),
        ("planned", "policy"),
    )
    for mode, stale_binding in variants:
        variant = f"{mode}-{stale_binding}"
        case_root = root / variant
        case_root.mkdir(parents=True, exist_ok=True)
        source, source_frame, source_policy = context(case_root)
        command_id = f"cmd-{mode.replace('_', '-')}-stale-{stale_binding}"
        pending = operator_pending_command(
            source,
            source_frame,
            source_policy,
            mode=mode,
            command_id=command_id,
        )
        if stale_binding == "policy":
            # Operator ownership must not be converted back into the suggested
            # incident lifecycle even if an imported/readable envelope carries
            # an incident-shaped audit label.
            pending["source_incident_id"] = f"{variant}-operator-audit"
        pending_path = write_stage(case_root, "pending", pending)
        original_bytes = pending_path.read_bytes()

        current_state = source
        current_frame = source_frame
        current_policy = source_policy
        if stale_binding == "frame":
            current_state = copy.deepcopy(source)
            current_state["seq"] = int(source["seq"]) + 1
            current_state["snapshot_seq"] = int(source["snapshot_seq"]) + 1
            current_state["snapshot_id"] = f"{source['session_id']}:{current_state['seq']}"
            current_state["ts"] = (
                dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1)
            ).astimezone().isoformat(timespec="seconds")
            current_frame = write_authorizing_frame(case_root, current_state)
            require(
                current_frame["frame_id"] != pending["source_frame_id"],
                f"{variant} fixture did not advance the current frame",
            )
        else:
            current_policy = copy.deepcopy(source_policy)
            current_policy["command_ttl_seconds"] = int(source_policy["command_ttl_seconds"]) + 1
            write_json_atomic(case_root / "action-policy.json", current_policy)
            require(
                canonical_hash(current_policy) != pending["source"]["policy_hash"],
                f"{variant} fixture did not change the current policy",
            )

        expected = f"mutation_pending_operator:{command_id}"
        preframe = command_pipeline_readiness(
            case_root,
            latest_state=current_state,
            proposal_frame=None,
            policy=current_policy,
        )
        require(expected in preframe["blockers"], f"{variant} pre-frame scan ignored operator pending")
        gate = command_pipeline_readiness(
            case_root,
            latest_state=current_state,
            proposal_frame=current_frame,
            policy=current_policy,
        )
        require(expected in gate["blockers"], f"{variant} did not latch the mutation slot")
        require(gate.get("pending_candidates") == [], f"{variant} operator mutation became a candidate")
        require(
            not any(item.startswith("mutation_pending_selection:") for item in gate["blockers"]),
            f"{variant} operator pending was mislabeled as a suggested selection",
        )
        confirmation = gate["readiness"]["mutation_confirmation"]
        require(
            confirmation["ready"] is True and confirmation["blockers"] == [],
            f"{variant} operator latch polluted confirmation readiness",
        )

        replacement = mutation_advice(current_state, f"{variant}-replacement")
        note = note_advice(current_state, f"{variant}-note")
        created = sync_pending_commands(
            case_root,
            current_state,
            [replacement, note],
            proposal_frame=current_frame,
        )
        require(
            len(created) == 1 and created[0]["action"] == "focus_note",
            f"{variant} allowed another mutation or blocked the note",
        )
        require(pending_path.exists(), f"{variant} operator pending was automatically removed")
        require(
            pending_path.read_bytes() == original_bytes,
            f"{variant} operator pending was rewritten during synchronization",
        )
        require(
            not (ensure_command_dirs(case_root)["rejected"] / f"{command_id}.json").exists(),
            f"{variant} operator pending was automatically rejected",
        )
        pending_mutation_ids = sorted(
            item["id"]
            for path in ensure_command_dirs(case_root)["pending"].glob("*.json")
            for item in [json.loads(path.read_text(encoding="utf-8-sig"))]
            if item.get("action") not in {"focus_note", "noop"}
        )
        require(
            pending_mutation_ids == [command_id],
            f"{variant} silently arbitrated or duplicated the operator mutation",
        )
        observed[variant] = {
            "blocker": expected,
            "confirmation_ready": confirmation["ready"],
            "created_actions": [item["action"] for item in created],
            "preserved_id": command_id,
        }
    return observed


def case_unsupported_pending_modes_fail_closed(root: pathlib.Path) -> Dict[str, Any]:
    observed: Dict[str, Any] = {}
    for label, unsupported_mode in (("empty", ""), ("unknown", "manual_override")):
        case_root = root / label
        case_root.mkdir(parents=True, exist_ok=True)
        snapshot, frame, current_policy = context(case_root)
        command_id = f"cmd-pending-mode-{label}"
        pending = operator_pending_command(
            snapshot,
            frame,
            current_policy,
            mode="runtime_validation",
            command_id=command_id,
        )
        pending["mode"] = unsupported_mode
        pending_path = write_stage(case_root, "pending", pending)
        original_bytes = pending_path.read_bytes()
        expected = f"mutation_pending_mode_invalid:{command_id}"

        gate = command_pipeline_readiness(
            case_root,
            latest_state=snapshot,
            proposal_frame=frame,
            policy=current_policy,
        )
        require(expected in gate["blockers"], f"{label} pending mode was silently ignored")
        require(gate.get("pending_candidates") == [], f"{label} unsupported mode became a candidate")
        confirmation = gate["readiness"]["mutation_confirmation"]
        require(
            confirmation["ready"] is True and confirmation["blockers"] == [],
            f"{label} pending mode polluted confirmation readiness",
        )

        replacement = mutation_advice(snapshot, f"unsupported-{label}-replacement")
        note = note_advice(snapshot, f"unsupported-{label}-note")
        created = sync_pending_commands(
            case_root,
            snapshot,
            [replacement, note],
            proposal_frame=frame,
        )
        require(
            len(created) == 1 and created[0]["action"] == "focus_note",
            f"{label} pending mode allowed a mutation or blocked the note",
        )
        require(pending_path.exists(), f"{label} pending mode was automatically removed")
        require(
            pending_path.read_bytes() == original_bytes,
            f"{label} pending mode was rewritten during synchronization",
        )
        require(
            not (ensure_command_dirs(case_root)["rejected"] / f"{command_id}.json").exists(),
            f"{label} pending mode was automatically rejected",
        )
        observed[label] = {
            "mode": unsupported_mode,
            "blocker": expected,
            "confirmation_ready": confirmation["ready"],
            "created_actions": [item["action"] for item in created],
            "preserved_id": command_id,
        }

    for mode in ("runtime_validation", "planned"):
        label = f"{mode}-malformed"
        case_root = root / label
        case_root.mkdir(parents=True, exist_ok=True)
        snapshot, frame, current_policy = context(case_root)
        command_id = f"cmd-{mode.replace('_', '-')}-malformed"
        pending = operator_pending_command(
            snapshot,
            frame,
            current_policy,
            mode=mode,
            command_id=command_id,
        )
        pending["source_frame_id"] = "not-a-canonical-hash"
        write_stage(case_root, "pending", pending)
        gate = command_pipeline_readiness(
            case_root,
            latest_state=snapshot,
            proposal_frame=frame,
            policy=current_policy,
        )
        expected = f"mutation_pending_operator_invalid:{command_id}"
        require(expected in gate["blockers"], f"{label} operator envelope was silently ignored")
        require(gate.get("pending_candidates") == [], f"{label} operator envelope became a candidate")
        observed[label] = {"blocker": expected, "pending_preserved": True}
    return observed


def case_duplicate_ids_fail_closed(root: pathlib.Path) -> Dict[str, Any]:
    snapshot, frame, current_policy = context(root)
    exact = suggested_command(snapshot, frame, current_policy)
    write_stage(root, "pending", exact)
    write_stage(root, "approving", exact, name=f"{exact['id']}-approval-claim.json")

    original = copy.deepcopy(exact)
    original["id"] = "cmd-conflicting-duplicate"
    claimed = copy.deepcopy(original)
    claimed["status"] = "executing"
    claimed["args"] = {"paused": False}
    write_stage(root, "pending", original)
    write_stage(root, "executing", claimed, name=f"{original['id']}-claim.json")

    gate = command_pipeline_readiness(
        root,
        latest_state=snapshot,
        proposal_frame=frame,
        policy=current_policy,
    )
    duplicate = gate.get("duplicate_ids")
    require(isinstance(duplicate, list) and len(duplicate) == 2, "duplicate ids were not grouped")
    exact_duplicate = next(item for item in duplicate if item["command_id"] == exact["id"])
    conflicting_duplicate = next(item for item in duplicate if item["command_id"] == original["id"])
    require(exact_duplicate["conflicting_intents"] is False, "exact lifecycle duplicate became a conflict")
    require(conflicting_duplicate["conflicting_intents"] is True, "conflicting duplicate intent was missed")
    require(gate.get("pending_candidates") == [], "duplicate command id became a pending candidate")
    exact_blocker = blocker(gate, f"duplicate_command_id:{exact['id']}")
    duplicate_blocker = blocker(gate, f"duplicate_command_id:{original['id']}")
    conflict_blocker = blocker(gate, f"conflicting_command_intent:{original['id']}")
    require(gate["readiness"]["mutation_confirmation"]["ready"] is False, "duplicate mutation did not fail confirmation closed")
    return {
        "exact_duplicate_blocker": exact_blocker,
        "duplicate_blocker": duplicate_blocker,
        "conflict_blocker": conflict_blocker,
        "exact_stages": exact_duplicate["stages"],
        "conflicting_stages": conflicting_duplicate["stages"],
    }


def case_stale_frame_after_witness_advance(root: pathlib.Path) -> Dict[str, Any]:
    source, source_frame, current_policy = context(root)
    created = sync_pending_commands(root, source, [advice()], proposal_frame=source_frame)
    require(len(created) == 1, "source proposal was not created")
    command_id = created[0]["id"]

    witness = copy.deepcopy(source)
    witness["seq"] = int(source["seq"]) + 1
    witness["snapshot_seq"] = int(source["snapshot_seq"]) + 1
    witness["snapshot_id"] = f"{source['session_id']}:{witness['seq']}"
    witness["ts"] = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1)).astimezone().isoformat(timespec="seconds")
    witness_frame = write_authorizing_frame(root, witness)

    gate = command_pipeline_readiness(
        root,
        latest_state=witness,
        proposal_frame=witness_frame,
        policy=current_policy,
    )
    snapshot_blocker = blocker(gate, "pending_source_snapshot_stale:")
    frame_blocker = blocker(gate, "pending_source_frame_stale:")
    require(
        not any(item.startswith("mutation_pending_selection:") for item in gate["blockers"]),
        "a stale pending binding was mislabeled as the current mutation selection",
    )
    require(len(gate.get("stale_pending") or []) == 1, "stale pending proposal was not classified")
    require(gate.get("pending_candidates") == [], "stale pending proposal became a candidate")

    refreshed_advice = copy.deepcopy(advice())
    refreshed_advice["source_snapshot_id"] = witness["snapshot_id"]
    replacement = sync_pending_commands(
        root,
        witness,
        [refreshed_advice],
        proposal_frame=witness_frame,
    )
    require(len(replacement) == 1, "stale pending rejection did not re-derive the same incident")
    require(
        replacement[0]["source_incident_id"] == created[0]["source_incident_id"],
        "fresh re-derivation silently changed the incident",
    )
    require(replacement[0]["id"] != command_id, "fresh frame reused the rejected stale command id")
    same_context_retry = sync_pending_commands(
        root,
        witness,
        [refreshed_advice],
        proposal_frame=witness_frame,
    )
    require(same_context_retry == [], "the same exact context was not idempotent")
    pending = list(ensure_command_dirs(root)["pending"].glob("*.json"))
    require(len(pending) == 1, "stale pending proposal survived alongside its replacement")
    rejected = json.loads(
        (ensure_command_dirs(root)["rejected"] / f"{command_id}.json").read_text(encoding="utf-8-sig")
    )
    require(
        rejected.get("rejection_reason") == "pending_source_snapshot_stale",
        f"stale rejection used the wrong reason: {rejected.get('rejection_reason')}",
    )
    return {
        "command_id": command_id,
        "snapshot_blocker": snapshot_blocker,
        "frame_blocker": frame_blocker,
        "rejection_reason": rejected["rejection_reason"],
        "replacement_id": replacement[0]["id"],
        "same_incident_rederived": True,
        "same_context_idempotent": True,
    }


def case_confirmed_preparation_in_flight(root: pathlib.Path) -> Dict[str, Any]:
    snapshot, frame, current_policy = context(root)
    command = suggested_command(snapshot, frame, current_policy)
    command["status"] = "confirmed"
    command["confirmation"] = {
        "schema": "oni.action-outcome.v1",
        "command_id": command["id"],
        "confirmation_status": "snapshot_confirmed",
    }
    write_stage(root, "confirming", command)

    gate = command_pipeline_readiness(
        root,
        latest_state=snapshot,
        proposal_frame=frame,
        policy=current_policy,
    )
    proposal_blocker = blocker(gate, "mutation_confirmation_commit_pending:")
    in_flight = gate.get("in_flight_mutations") or []
    require(len(in_flight) == 1, "prepared confirmation was not classified in flight")
    require(gate.get("pending_candidates") == [], "in-flight confirmation became a pending candidate")
    require(in_flight[0]["classification"] == "confirmed_preparation", "prepared state became ambiguous")
    confirmation = gate["readiness"]["mutation_confirmation"]
    require(confirmation["ready"] is True, "known prepared confirmation blocked its own recovery")
    require(confirmation["candidate_ids"] == [command["id"]], "prepared candidate id was lost")
    return {
        "proposal_blocker": proposal_blocker,
        "classification": in_flight[0]["classification"],
        "confirmation_ready": confirmation["ready"],
    }


def case_authorizing_policy_change_expires_pending(root: pathlib.Path) -> Dict[str, Any]:
    snapshot, frame, source_policy, selected_advice = exact_rule_candidate_context(root)
    created = sync_pending_commands(root, snapshot, [selected_advice], proposal_frame=frame)
    require(len(created) == 1, "policy-bound proposal was not created")
    command_id = created[0]["id"]
    approved = build_approved_command(
        created[0],
        source_policy,
        frame,
        approved_by="local_human",
        approved_at=dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds"),
    )
    validate_approval_receipt(approved, source_policy, frame)
    changed_policy = copy.deepcopy(source_policy)
    changed_policy["command_ttl_seconds"] = int(source_policy["command_ttl_seconds"]) + 1
    write_json_atomic(root / "action-policy.json", changed_policy)

    gate = command_pipeline_readiness(
        root,
        latest_state=snapshot,
        proposal_frame=frame,
        policy=changed_policy,
    )
    policy_blocker = blocker(gate, "pending_source_policy_stale:")
    sync_pending_commands(root, snapshot, [selected_advice], proposal_frame=frame)
    require(not list(ensure_command_dirs(root)["pending"].glob("*.json")), "old-policy proposal survived")
    rejected = json.loads(
        (ensure_command_dirs(root)["rejected"] / f"{command_id}.json").read_text(encoding="utf-8-sig")
    )
    require(
        rejected.get("rejection_reason") == "pending_source_policy_stale",
        f"policy rejection used the wrong reason: {rejected.get('rejection_reason')}",
    )
    return {
        "approval_receipt_compatible": True,
        "blocker": policy_blocker,
        "rejection_reason": rejected["rejection_reason"],
    }


def case_unresolved_outcome_unknown(root: pathlib.Path) -> Dict[str, Any]:
    snapshot, frame, current_policy = context(root)
    unknown = suggested_command(snapshot, frame, current_policy)
    unknown["status"] = "failed"
    unknown["result"] = {"classification": "outcome_unknown", "message": "fixture ambiguity"}
    write_stage(root, "failed", unknown)

    harmless = copy.deepcopy(unknown)
    harmless["id"] = "cmd-non-mutating-unknown"
    harmless["action"] = "focus_note"
    write_stage(root, "failed", harmless)
    gate = command_pipeline_readiness(
        root,
        latest_state=snapshot,
        proposal_frame=frame,
        policy=current_policy,
    )
    unknown_blocker = blocker(gate, "mutation_outcome_unknown_unresolved:")
    require("cmd-non-mutating-unknown" not in unknown_blocker, "focus_note engaged the mutation latch")
    ambiguous = gate.get("ambiguous_outcomes") or []
    require(
        any(item.get("command_id") == unknown["id"] and item.get("classification") == "outcome_unknown" for item in ambiguous),
        "mutating unknown outcome was not exposed",
    )
    require(
        all(item.get("command_id") != harmless["id"] for item in ambiguous),
        "non-mutating unknown polluted mutation ambiguity",
    )
    require(gate.get("pending_candidates") == [], "unknown outcome became a pending candidate")
    return {"blocker": unknown_blocker, "ignored_non_mutating_id": harmless["id"]}


def case_sync_blocks_mutation_but_allows_note(root: pathlib.Path) -> Dict[str, Any]:
    snapshot, frame, current_policy = context(root)
    executing = suggested_command(snapshot, frame, current_policy)
    executing["id"] = "cmd-existing-execution"
    executing["status"] = "executing"
    write_stage(root, "executing", executing)

    note = note_advice(snapshot)
    created = sync_pending_commands(root, snapshot, [advice(), note], proposal_frame=frame)
    require(len(created) == 1 and created[0]["action"] == "focus_note", "proposal gate did not isolate mutating advice")
    pending = list(ensure_command_dirs(root)["pending"].glob("*.json"))
    require(len(pending) == 1, "blocked mutation entered pending alongside the note")
    return {"created_actions": [item["action"] for item in created], "blocked_action": "set_paused"}


def case_colony_leaf_progression_blocked(root: pathlib.Path) -> Dict[str, Any]:
    snapshot = plan_state()
    write_live_state(root, snapshot)
    plan = create_colony_plan(root, priority_definition(snapshot, [4]))
    blocking = {
        "schema": "oni.command.v3",
        "id": "cmd-unrelated-approved-mutation",
        "mode": "suggested",
        "status": "approved",
        "action": "set_paused",
        "args": {"paused": True},
        "session_id": snapshot["session_id"],
        "save_id_hash": snapshot["save_id_hash"],
        "world_id": snapshot["world_id"],
    }
    write_stage(root, "approved", blocking)
    try:
        advance_colony_plan(root, str(plan["plan_id"]))
    except ColonyPlanError as exc:
        error = str(exc)
    else:
        raise AssertionError("colony plan emitted a leaf during an approved mutation")
    require("mutation_approved_in_flight" in error, f"planner blocker was not explicit: {error}")
    require(not list(ensure_command_dirs(root)["pending"].glob("*.json")), "blocked planner wrote a leaf command")
    unchanged = load_active_plan(root, str(plan["plan_id"]))
    require(unchanged["status"] == "draft", "blocked progression mutated the draft plan")
    return {"plan_id": plan["plan_id"], "status": unchanged["status"], "error": error}


def case_colony_leaf_blocked_by_pending_selection(root: pathlib.Path) -> Dict[str, Any]:
    snapshot = plan_state()
    write_live_state(root, snapshot)
    plan = create_colony_plan(root, priority_definition(snapshot, [4]))
    current_state = json.loads((root / "latest_state.json").read_text(encoding="utf-8-sig"))
    current_frame = json.loads((root / "decision-frame.json").read_text(encoding="utf-8-sig"))
    current_policy = json.loads((root / "action-policy.json").read_text(encoding="utf-8-sig"))
    selected_advice = mutation_advice(current_state, "plan-blocking-pending-selection")
    selected = build_command(
        current_state,
        selected_advice,
        selected_advice["command"],
        ttl_seconds=30,
        source_frame_id=str(current_frame["frame_id"]),
        source_policy_hash=canonical_hash(current_policy),
    )
    write_stage(root, "pending", selected)

    try:
        advance_colony_plan(root, str(plan["plan_id"]))
    except ColonyPlanError as exc:
        error = str(exc)
    else:
        raise AssertionError("colony plan emitted a leaf during a pending mutation selection")
    expected = f"mutation_pending_selection:{selected['id']}"
    require(expected in error, f"pending planner blocker was not explicit: {error}")
    pending_ids = sorted(
        json.loads(path.read_text(encoding="utf-8-sig"))["id"]
        for path in ensure_command_dirs(root)["pending"].glob("*.json")
    )
    require(pending_ids == [selected["id"]], "blocked planner changed the pending selection")
    unchanged = load_active_plan(root, str(plan["plan_id"]))
    require(unchanged["status"] == "draft", "pending selection mutated the draft plan")
    return {
        "plan_id": plan["plan_id"],
        "status": unchanged["status"],
        "pending_id": selected["id"],
        "error": error,
    }


def run_case(name: str, callback: Callable[[pathlib.Path], Dict[str, Any]], root: pathlib.Path) -> Dict[str, Any]:
    case_root = root / name
    case_root.mkdir(parents=True, exist_ok=True)
    try:
        return {"name": name, "status": "passed", "details": callback(case_root)}
    except Exception as exc:  # noqa: BLE001 - verifier must report every isolated case
        return {"name": name, "status": "failed", "error": f"{type(exc).__name__}: {exc}"}


def verify(output: pathlib.Path) -> Dict[str, Any]:
    cases: list[tuple[str, Callable[[pathlib.Path], Dict[str, Any]]]] = [
        ("clean_ready", case_clean_ready),
        (
            "one_new_mutation_per_frame_notes_do_not_consume_slot",
            case_one_new_mutation_per_frame_notes_do_not_consume_slot,
        ),
        ("preframe_scan_defers_pending_selection", case_preframe_scan_defers_pending_selection),
        (
            "pending_selection_blocks_next_mutation_but_allows_note",
            case_pending_selection_blocks_next_mutation_but_allows_note,
        ),
        ("tampered_pending_candidate_excluded", case_tampered_pending_candidate_excluded),
        ("noncanonical_pending_filename_fails_closed", case_noncanonical_pending_filename_fails_closed),
        ("survival_witness_and_priority_fail_closed", case_survival_witness_and_priority_fail_closed),
        ("multiple_current_pending_mutations_fail_closed", case_multiple_current_pending_mutations_fail_closed),
        (
            "stale_operator_pending_latches_without_arbitration",
            case_stale_operator_pending_latches_without_arbitration,
        ),
        ("unsupported_pending_modes_fail_closed", case_unsupported_pending_modes_fail_closed),
        ("duplicate_ids_fail_closed", case_duplicate_ids_fail_closed),
        ("stale_frame_after_witness_advance", case_stale_frame_after_witness_advance),
        ("confirmed_preparation_in_flight", case_confirmed_preparation_in_flight),
        ("authorizing_policy_change_expires_pending", case_authorizing_policy_change_expires_pending),
        ("unresolved_outcome_unknown", case_unresolved_outcome_unknown),
        ("sync_blocks_mutation_but_allows_note", case_sync_blocks_mutation_but_allows_note),
        ("colony_leaf_progression_blocked", case_colony_leaf_progression_blocked),
        ("colony_leaf_blocked_by_pending_selection", case_colony_leaf_blocked_by_pending_selection),
    ]
    with tempfile.TemporaryDirectory(prefix="oni-command-pipeline-gate-") as temporary:
        root = pathlib.Path(temporary)
        results = [run_case(name, callback, root) for name, callback in cases]
    passed = all(item["status"] == "passed" for item in results)
    report = {
        "schema": "oni.command-pipeline-gate-verification.v1",
        "generated_at": dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds"),
        "status": "passed" if passed else "failed",
        "summary": {
            "total": len(results),
            "passed": sum(item["status"] == "passed" for item in results),
            "failed": sum(item["status"] != "passed" for item in results),
        },
        "cases": results,
    }
    write_json_atomic(output.expanduser().resolve(), report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=pathlib.Path,
        default=PROJECT_ROOT / "reports" / "command-pipeline-gate-verification.json",
    )
    args = parser.parse_args()
    report = verify(args.output)
    for item in report["cases"]:
        detail = item.get("details") if item["status"] == "passed" else item.get("error")
        print(f"[{item['status'].upper()}] {item['name']}: {json.dumps(detail, ensure_ascii=False, sort_keys=True)}")
    print(f"Report: {args.output.expanduser().resolve()}")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
