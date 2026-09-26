#!/usr/bin/env python3
"""Offline fixture regression for the read-only live readiness checker."""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import os
import pathlib
import sys
import tempfile
from typing import Any, Dict, Mapping


ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.decision import build_decision_frame  # noqa: E402
from agent.state_quality import canonical_state_hash  # noqa: E402
from scripts import check_live_readiness as readiness  # noqa: E402
from scripts.verify_decision_frame import healthy_inputs  # noqa: E402
from tools.game_verify import verify_power_telemetry_contract_v2 as power_v2  # noqa: E402
from tools.game_verify import verify_telemetry_contract as telemetry_v1  # noqa: E402


SCENARIO = ROOT / "tools" / "game_verify" / "scenarios" / "oni_telemetry_contract_mock.json"
NOW = dt.datetime.fromisoformat("2026-08-30T08:00:01+08:00")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def quality_entry(contract: Mapping[str, Any], field: str, status: str) -> Dict[str, Any]:
    spec = contract["quality_contracts"][field]
    completeness = (
        contract["blocked_quality"][status]
        if status in contract["blocked_quality"]
        else spec["completeness"]
    )
    return {
        "status": status,
        "source": spec["source"],
        "semantic": spec["semantic"],
        "unit": spec["unit"],
        "scope": spec["scope"],
        "completeness": completeness,
        "captured_at": spec["captured_at"],
    }


def complete_material_scan(snapshot: Dict[str, Any]) -> None:
    cells = snapshot["materials"]["active_world_cells"]
    completed_at = snapshot["ts"]
    started_at = cells["scan_window_started_at"]
    candidate_count = cells["candidate_cell_count"]
    cells.update(
        {
            "scanned_candidate_cell_count": candidate_count,
            "remaining_candidate_cell_count": 0,
            "scan_progress_ratio": 1.0,
            "scan_window_completed_at": completed_at,
            "scan_status": "complete",
            "scan_complete": True,
            "aggregate_available": True,
            "aggregate_scan_generation": cells["scan_generation"],
            "aggregate_window_started_at": started_at,
            "aggregate_window_completed_at": completed_at,
            "aggregate_age_seconds": 0.0,
        }
    )


def bounded_snapshot(
    power_contract: Mapping[str, Any],
    *,
    material_complete: bool,
) -> Dict[str, Any]:
    scenario = telemetry_v1.load_json(SCENARIO)
    snapshot = copy.deepcopy(scenario["telemetry_contract"]["base_snapshot"])
    snapshot["power"]["storage_mode"] = "bounded"
    for field in ("storage_mode", "stored_joules", "capacity_joules", "battery_percent"):
        snapshot["quality"][f"power.{field}"] = quality_entry(
            power_contract, field, "observed"
        )
    snapshot["bridge"] = {
        "stale": False,
        "snapshot_age_seconds": 1.0,
        "trust_ready": True,
        "trust_blockers": [],
        "replay_mode": False,
    }
    if material_complete:
        complete_material_scan(snapshot)
    return snapshot


def unbounded_snapshot(
    power_contract: Mapping[str, Any],
    *,
    material_complete: bool = True,
) -> Dict[str, Any]:
    snapshot = bounded_snapshot(power_contract, material_complete=material_complete)
    snapshot["power"].update(
        {
            "storage_mode": "unbounded",
            "stored_joules": 450000.0,
            "capacity_joules": None,
            "battery_percent": None,
        }
    )
    snapshot["quality"]["power.capacity_joules"] = quality_entry(
        power_contract, "capacity_joules", "unsupported"
    )
    snapshot["quality"]["power.battery_percent"] = quality_entry(
        power_contract, "battery_percent", "unsupported"
    )
    return snapshot


def zero_capacity_snapshot(power_contract: Mapping[str, Any]) -> Dict[str, Any]:
    snapshot = bounded_snapshot(power_contract, material_complete=True)
    snapshot["power"].update(
        {
            "stored_joules": 0.0,
            "capacity_joules": 0.0,
            "battery_percent": None,
        }
    )
    snapshot["quality"]["power.battery_percent"] = quality_entry(
        power_contract, "battery_percent", "unsupported"
    )
    return snapshot


def output_bundle(
    state: Dict[str, Any],
    *,
    replay_mode: bool = False,
    session_start_observed: bool = True,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    state = copy.deepcopy(state)
    state["bridge"].update(
        {
            "stale": False,
            "trust_ready": True,
            "trust_blockers": [],
            "replay_mode": replay_mode,
        }
    )
    values = healthy_inputs(actions_enabled=False, suggestions_enabled=False)
    # The shared decision-frame fixture uses a fixed historical timestamp.
    # Rebind advice lifecycle timestamps to this bundle's actual source sample
    # so the test exercises readiness rather than an accidental future-advice
    # rejection.
    for item in values["advice"].get("items", []):
        if isinstance(item, dict):
            item["incident_opened_at"] = state["ts"]
    identity = {
        "session_id": state["session_id"],
        "snapshot_id": state["snapshot_id"],
        "state_hash": canonical_state_hash(state),
    }
    for name in ("health", "advice", "incidents", "plan", "strategy", "policy"):
        values[name]["source"] = copy.deepcopy(identity)

    health = values["health"]
    health.update(
        {
            "schema": "oni.bridge.health.v1",
            "updated_at": NOW.isoformat(),
            "status": "ok",
            "current_session_id": state["session_id"],
            "latest_snapshot_id": state["snapshot_id"],
            "latest_seq": state["seq"],
            "snapshot_age_seconds": 1.0,
            "trust_ready": True,
            "trust_blockers": [],
            "replay_mode": replay_mode,
            "session_start_observed": session_start_observed,
        }
    )
    frame = build_decision_frame(
        state,
        health,
        values["advice"],
        values["incidents"],
        values["plan"],
        values["strategy"],
        values["policy"],
    )
    health["decision_frame_id"] = frame["frame_id"]
    health["decision_frame_trust"] = frame["trust"]["status"]
    health["decision_frame_readiness"] = {
        stage: bool(entry["ready"]) for stage, entry in frame["readiness"].items()
    }
    return state, {"health": health, "frame": frame}


def write_json(path: pathlib.Path, value: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def write_bundle(
    root: pathlib.Path,
    state: Dict[str, Any],
    *,
    replay_mode: bool = False,
    session_start_observed: bool = True,
) -> None:
    state_value, outputs = output_bundle(
        state,
        replay_mode=replay_mode,
        session_start_observed=session_start_observed,
    )
    write_json(root / "latest_state.json", state_value)
    write_json(root / "health.json", outputs["health"])
    # The fixture follows Bridge publication order: commit marker last.
    write_json(root / "decision-frame.json", outputs["frame"])


def bundle_hashes(root: pathlib.Path) -> Dict[str, str]:
    return {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in ("latest_state.json", "health.json", "decision-frame.json")
    }


def assess(
    root: pathlib.Path,
    contracts: tuple[Dict[str, Any], pathlib.Path, pathlib.Path],
    *,
    require_material_scan: bool,
    now: dt.datetime = NOW,
) -> Dict[str, Any]:
    return readiness.assess_readiness(
        root,
        now=now,
        require_material_scan=require_material_scan,
        contracts=contracts,
    )


def main() -> int:
    contracts = readiness.load_contracts()
    power_contract = power_v2.load_json(readiness.DEFAULT_POWER_V2_CONTRACT)
    results: Dict[str, Any] = {}

    with tempfile.TemporaryDirectory(prefix="oni-readiness-ready-") as temporary:
        root = pathlib.Path(temporary)
        write_bundle(root, bounded_snapshot(power_contract, material_complete=True))
        before = bundle_hashes(root)
        report = assess(root, contracts, require_material_scan=True)
        after = bundle_hashes(root)
        require(report["ready"] is True, f"bounded complete fixture was rejected: {report['blockers']}")
        require(report["observation_ready"] is True, "bounded observation was not ready")
        require(report["legacy_v1_compatible"] is True, "bounded fixture lost v1 compatibility")
        require(report["bundle"]["accepted"] is True, "committed bundle was not accepted")
        require(report["admission"]["accepted"] is True, "trusted state admission failed")
        require(report["telemetry_v1"]["accepted"] is True, "bounded v1 telemetry failed")
        require(report["power_v2"]["accepted"] is True, "bounded power v2 failed")
        require(
            report["power_v2"]["percent_dependent_actions_blocked"] is False,
            "bounded positive capacity stayed percent-blocked",
        )
        require(report["material_scan"]["ready"] is True, "full material scan was not ready")
        require(before == after, "readiness checker mutated its Bridge inputs")
        results["bounded_complete_ready"] = True
        results["read_only_hashes_preserved"] = True

    # The decision frame is Bridge's generation commit marker.  A state and
    # health pair without that marker must remain a clearly diagnosed,
    # unreadable bundle; readiness must not fall through to partial telemetry
    # or manufacture a source identity.
    with tempfile.TemporaryDirectory(prefix="oni-readiness-missing-frame-") as temporary:
        root = pathlib.Path(temporary)
        state = bounded_snapshot(power_contract, material_complete=True)
        state_value, outputs = output_bundle(state)
        write_json(root / "latest_state.json", state_value)
        write_json(root / "health.json", outputs["health"])
        before = {
            path.name: path.read_bytes()
            for path in root.iterdir()
            if path.is_file()
        }
        missing = assess(root, contracts, require_material_scan=True)
        after = {
            path.name: path.read_bytes()
            for path in root.iterdir()
            if path.is_file()
        }
        require(missing["ready"] is False, "missing decision frame passed readiness")
        require(missing["observation_ready"] is False, "missing decision frame passed observation")
        require(
            missing["bundle"]
            == {
                "accepted": False,
                "stable_generation": False,
                "decision_frame_id": None,
                "blockers": ["decision_frame_missing"],
            },
            "missing decision frame diagnostic changed or became optimistic",
        )
        require(missing["source"] is None, "missing decision frame manufactured source identity")
        require(before == after, "missing decision frame check mutated Bridge inputs")
        results["missing_decision_frame_fail_closed"] = True

    with tempfile.TemporaryDirectory(prefix="oni-readiness-material-") as temporary:
        root = pathlib.Path(temporary)
        partial = bounded_snapshot(power_contract, material_complete=False)
        complete = bounded_snapshot(power_contract, material_complete=True)
        write_bundle(root, partial)
        optional = assess(root, contracts, require_material_scan=False)
        required = assess(root, contracts, require_material_scan=True)
        require(optional["ready"] is True, "optional material scan blocked bounded telemetry")
        require(required["ready"] is False, "partial material scan satisfied the full-scan gate")
        swapped = False

        def swap_on_sleep(_: float) -> None:
            nonlocal swapped
            require(not swapped, "wait loop slept after the complete fixture was published")
            write_bundle(root, complete)
            swapped = True

        waited = readiness.wait_for_readiness(
            root,
            timeout_seconds=1.0,
            poll_interval_seconds=0.1,
            require_material_scan=True,
            contracts=contracts,
            now_fn=lambda: NOW,
            sleep_fn=swap_on_sleep,
        )
        require(waited["ready"] is True and waited["attempts"] == 2, "material wait did not converge")
        results["partial_optional"] = True
        results["material_wait_attempts"] = waited["attempts"]

    with tempfile.TemporaryDirectory(prefix="oni-readiness-power-") as temporary:
        root = pathlib.Path(temporary)
        write_bundle(root, unbounded_snapshot(power_contract))
        unbounded = assess(root, contracts, require_material_scan=True)
        require(unbounded["observation_ready"] is True, "honest unbounded v2 observation was blocked")
        require(unbounded["ready"] is True, "ready alias diverged from observation readiness")
        require(unbounded["legacy_v1_compatible"] is False, "unbounded telemetry claimed v1 compatibility")
        require(unbounded["telemetry_v1"]["accepted"] is False, "v1 accepted unbounded null gauges")
        require(unbounded["power_v2"]["accepted"] is True, "v2 rejected honest unbounded telemetry")
        require(
            unbounded["power_v2"]["percent_dependent_actions_blocked"] is True,
            "unbounded telemetry became percent-actionable",
        )

        write_bundle(root, zero_capacity_snapshot(power_contract))
        zero = assess(root, contracts, require_material_scan=True)
        require(zero["observation_ready"] is True, "honest zero-capacity observation was blocked")
        require(zero["power_v2"]["accepted"] is True, "v2 rejected honest zero capacity")
        require(
            zero["power_v2"]["percent_dependent_actions_blocked"] is True,
            "zero capacity became percent-actionable",
        )
        results["v1_v2_split_reported"] = True
        results["zero_capacity_fail_closed"] = True

    with tempfile.TemporaryDirectory(prefix="oni-readiness-base-invalid-") as temporary:
        root = pathlib.Path(temporary)
        invalid_base = bounded_snapshot(power_contract, material_complete=True)
        invalid_base["cycle"] = "not-an-integer"
        write_bundle(root, invalid_base)
        rejected = assess(root, contracts, require_material_scan=True)
        require(rejected["observation_ready"] is False, "invalid base telemetry passed observation")
        require(rejected["power_v2"]["accepted"] is False, "power v2 skipped base-contract failure")
        require(
            any("cycle" in blocker for blocker in rejected["power_v2"]["blockers"]),
            "base-field blocker was not reported",
        )
        results["base_contract_fail_closed"] = True

    with tempfile.TemporaryDirectory(prefix="oni-readiness-scope-forgery-") as temporary:
        root = pathlib.Path(temporary)
        forged_scope = bounded_snapshot(power_contract, material_complete=True)
        forged_scope["observation_scope"].update(
            {
                "populated_world_ids": [1],
                "covered_world_ids": [0, 1],
                "complete_for_populated_worlds": True,
            }
        )
        write_bundle(root, forged_scope)
        rejected_scope = assess(root, contracts, require_material_scan=True)
        require(
            rejected_scope["observation_ready"] is False,
            "forged multi-world complete scope passed readiness",
        )
        for blocker in (
            "complete_populated_world_ids_not_active_world_only",
            "covered_world_ids_not_active_world_only",
        ):
            require(
                blocker in rejected_scope["admission"]["blockers"],
                f"state admission omitted scope blocker: {blocker}",
            )
            require(
                blocker in rejected_scope["telemetry_v1"]["blockers"],
                f"v1 readiness omitted scope blocker: {blocker}",
            )
        results["forged_multiworld_scope_rejected"] = True

    with tempfile.TemporaryDirectory(prefix="oni-readiness-huge-int-") as temporary:
        root = pathlib.Path(temporary)
        oversized = bounded_snapshot(power_contract, material_complete=True)
        oversized["sample"]["interval_seconds"] = 10 ** 309
        oversized["power"]["stored_joules"] = 10 ** 309
        oversized["materials"]["active_world_cells"]["scan_progress_ratio"] = 10 ** 309
        write_bundle(root, oversized)
        rejected = assess(root, contracts, require_material_scan=True)
        require(rejected["observation_ready"] is False, "oversized JSON integers passed readiness")
        require(
            "sample_interval_invalid" in rejected["admission"]["blockers"],
            "oversized sample interval did not produce an admission blocker",
        )
        require(rejected["power_v2"]["accepted"] is False, "oversized power integer passed v2")
        require(
            "power_stored_joules_invalid" in rejected["power_v2"]["blockers"],
            "oversized power integer blocker was omitted",
        )
        require(
            "material_progress_not_complete" in rejected["material_scan"]["blockers"],
            "oversized material ratio did not fail closed",
        )
        results["huge_integer_fail_closed"] = True

    with tempfile.TemporaryDirectory(prefix="oni-readiness-binding-") as temporary:
        root = pathlib.Path(temporary)
        snapshot = bounded_snapshot(power_contract, material_complete=True)
        write_bundle(root, snapshot)
        health = telemetry_v1.load_json(root / "health.json")
        health["source"]["state_hash"] = "sha256:" + "b" * 64
        write_json(root / "health.json", health)
        mismatch = assess(root, contracts, require_material_scan=True)
        require(mismatch["ready"] is False, "mismatched health source passed readiness")
        require(
            "health_source_state_hash_mismatch" in mismatch["bundle"]["blockers"],
            "health source mismatch blocker was omitted",
        )

        write_bundle(root, snapshot)
        state = telemetry_v1.load_json(root / "latest_state.json")
        state["quality"]["power.battery_percent"]["status"] = "error"
        write_json(root / "latest_state.json", state)
        quality_mismatch = assess(root, contracts, require_material_scan=True)
        require(quality_mismatch["ready"] is False, "unbound quality mutation passed readiness")
        require(
            "frame_source_state_hash_mismatch" in quality_mismatch["bundle"]["blockers"],
            "quality/state hash mismatch blocker was omitted",
        )
        results["state_quality_health_bound"] = True

    with tempfile.TemporaryDirectory(prefix="oni-readiness-replay-") as temporary:
        root = pathlib.Path(temporary)
        snapshot = bounded_snapshot(power_contract, material_complete=True)
        write_bundle(root, snapshot, replay_mode=True)
        replay = assess(root, contracts, require_material_scan=True)
        require(replay["ready"] is False, "replay output passed live readiness")
        require(
            "health_replay_mode_not_live" in replay["bundle"]["blockers"],
            "replay blocker was omitted",
        )

        write_bundle(root, snapshot, session_start_observed=False)
        no_start = assess(root, contracts, require_material_scan=True)
        require(no_start["ready"] is False, "missing session start passed readiness")
        require(
            "health_session_start_not_observed" in no_start["bundle"]["blockers"],
            "session-start blocker was omitted",
        )
        results["live_session_only"] = True

    with tempfile.TemporaryDirectory(prefix="oni-readiness-stale-") as temporary:
        root = pathlib.Path(temporary)
        write_bundle(root, bounded_snapshot(power_contract, material_complete=True))
        stale = assess(
            root,
            contracts,
            require_material_scan=True,
            now=NOW + dt.timedelta(seconds=120),
        )
        require(stale["ready"] is False, "stale snapshot passed readiness")
        require("snapshot_stale" in stale["admission"]["blockers"], "stale blocker was omitted")

        (root / "latest_state.json").write_text(
            '{"schema":"oni.state.v2","power":{"capacity_joules":NaN}}\n',
            encoding="utf-8",
        )
        invalid_json = assess(root, contracts, require_material_scan=True)
        require(invalid_json["ready"] is False, "non-standard NaN passed the bundle reader")
        require(
            invalid_json["bundle"]["blockers"] == ["latest_state_json_invalid"],
            "strict JSON blocker was not stable",
        )
        results["stale_and_nonfinite_rejected"] = True

    with tempfile.TemporaryDirectory(prefix="oni-readiness-legacy-") as temporary:
        root = pathlib.Path(temporary)
        legacy = bounded_snapshot(power_contract, material_complete=True)
        del legacy["power"]["storage_mode"]
        del legacy["quality"]["power.storage_mode"]
        write_bundle(root, legacy)
        missing_mode = assess(root, contracts, require_material_scan=True)
        require(missing_mode["ready"] is False, "missing storage mode passed readiness")
        require(missing_mode["power_v2"]["storage_mode"] is None, "missing mode was not normalized")
        json.dumps(missing_mode, allow_nan=False)
        results["legacy_missing_mode_reported"] = True

    with tempfile.TemporaryDirectory(prefix="oni-readiness-timeout-") as temporary:
        root = pathlib.Path(temporary)
        write_bundle(root, bounded_snapshot(power_contract, material_complete=False))
        ticks = iter((0.0, 0.5, 1.1))
        timed_out = readiness.wait_for_readiness(
            root,
            timeout_seconds=1.0,
            poll_interval_seconds=0.1,
            require_material_scan=True,
            contracts=contracts,
            now_fn=lambda: NOW,
            monotonic_fn=lambda: next(ticks),
            sleep_fn=lambda _: None,
        )
        require(timed_out["status"] == "timeout", "bounded wait did not terminate at timeout")
        require(timed_out["ready"] is False, "timeout returned an optimistic readiness result")
        results["timeout_fail_closed"] = True

    with tempfile.TemporaryDirectory(prefix="oni-readiness-file-identity-") as temporary:
        root = pathlib.Path(temporary)
        write_bundle(root, bounded_snapshot(power_contract, material_complete=True))
        external = root / "external-state.json"
        external.write_bytes((root / "latest_state.json").read_bytes())
        (root / "latest_state.json").unlink()
        os.link(external, root / "latest_state.json")
        hardlink = assess(root, contracts, require_material_scan=True)
        require(hardlink["ready"] is False, "hardlinked state passed readiness")
        require(
            hardlink["bundle"]["blockers"] == ["latest_state_hardlink_forbidden"],
            "hardlink blocker was not explicit",
        )
        results["hardlink_bundle_rejected"] = True

    print(
        json.dumps(
            {
                "schema": "oni.live-readiness-verification.v1",
                "status": "passed",
                "fixture_cases": 15,
                **results,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
