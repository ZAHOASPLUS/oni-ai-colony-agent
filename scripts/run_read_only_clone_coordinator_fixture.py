#!/usr/bin/env python3
"""Run one isolated, read-only clone-gated coordinator tick.

The fixture is created under a temporary directory and removed after the
evidence report is written.  It uses real clone file hashes and a plan built
through the production Colony Plan helpers, then calls the explicit
``build_read_only_coordinator_observer`` seam exactly once.  No ONI, Steam,
Bridge, or model process is launched.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
import shutil
import struct
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.advanced_actions import build_duplicant_priority_hint
from agent.colony_plan import PLAN_DEFINITION_SCHEMA, create_colony_plan, load_active_plan
from agent.command_queue import ensure_command_dirs
from agent.controller_bridge import IDENTITY_FIELDS
from bridge import oni_bridge
from scripts.test_controller_loop import snapshot
from scripts.verify_colony_plan import write_live_state


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def path_id(path: pathlib.Path) -> str:
    return sha256_bytes(str(path.resolve()).lower().encode("utf-8"))


def count_json(path: pathlib.Path) -> int:
    return sum(1 for item in path.glob("*.json") if item.is_file())


def command_tree_snapshot(root: pathlib.Path) -> dict[str, str]:
    """Return a deterministic file/dir snapshot for the command boundary."""
    commands_root = root / "commands"
    snapshot: dict[str, str] = {}
    for path in sorted(commands_root.rglob("*")):
        relative = str(path.relative_to(commands_root)).replace("\\", "/")
        if path.is_dir():
            snapshot[relative + "/"] = "directory"
        elif path.is_file():
            snapshot[relative] = sha256_bytes(path.read_bytes())
    return snapshot


def main() -> int:
    work = pathlib.Path(tempfile.mkdtemp(prefix="oni-readonly-clone-plan-"))
    try:
        bridge = work / "bridge"
        clone_dir = work / "clone"
        source_dir = work / "source"
        intent_dir = work / "intents"
        for directory in (bridge, clone_dir, source_dir, intent_dir):
            directory.mkdir(parents=True)
        ensure_command_dirs(bridge)
        for name in ("approved", "inflight"):
            (bridge / "validation-controls" / name).mkdir(parents=True)

        clone_save = clone_dir / "named-test-clone.sav"
        clone_preview = clone_dir / "named-test-clone.png"
        source_save = source_dir / "original.sav"
        source_preview = source_dir / "original.png"
        # The launch guard parses ONI's save header: buildVersion, headerSize,
        # headerVersion, compression, then GameInfo JSON (non-autosave).
        header = json.dumps({"baseName": "Readonly Fixture Base", "isAutoSave": False}).encode("utf-8")
        clone_save.write_bytes(
            struct.pack("<IiIi", 744825, len(header), 1, 1) + header
            + b"oni-readonly-clone-save-20260913\n"
        )
        clone_preview.write_bytes(b"PNG-fixture-preview-20260913\n")
        source_save.write_bytes(b"immutable-source-placeholder\n")
        source_preview.write_bytes(b"immutable-source-preview-placeholder\n")
        clone_save_hash = sha256_bytes(clone_save.read_bytes())
        clone_preview_hash = sha256_bytes(clone_preview.read_bytes())
        manifest = {
            "schema": "oni.named-test-clone.v1",
            "created": True,
            "clone_id": "readonly-coordinator-fixture-20260913",
            "source": {
                "save": {"path_id": path_id(source_save)},
                "preview": {"path_id": path_id(source_preview)},
                "colony_directory_path_id": path_id(source_dir),
                "redirect_colony_directory_path_id": path_id(work / "Readonly Fixture Base"),
            },
            "target": {
                "save_sha256": clone_save_hash,
                "preview_sha256": clone_preview_hash,
                "save": {"path_id": path_id(clone_save)},
                "preview": {"path_id": path_id(clone_preview)},
            },
        }
        manifest_path = clone_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )

        # Use the complete Probe-shaped offline snapshot so the controller's
        # full-snapshot trust gate can produce a positive candidate.
        initial = snapshot(priority=3, seq=10, snapshot_seq=5, seconds=0)
        initial["session_id"] = "readonly-coordinator-session-20260913"
        initial["snapshot_id"] = f"{initial['session_id']}:{initial['seq']}"
        # The runtime identity must bind to the selected clone's actual bytes.
        initial["save_id_hash"] = clone_save_hash
        write_live_state(bridge, initial)
        # ``run_once`` consumes a JSONL input path in replay mode.  Supplying
        # this isolated record keeps the fixture independent of any live
        # Bridge state while exercising the production run_once wiring.
        input_path = work / "state.jsonl"
        # ``write_live_state`` refreshes the fixture timestamp so the
        # coordinator's freshness gate sees a current observation.  Reuse
        # that committed snapshot rather than the deterministic source value
        # (which intentionally carries an old test timestamp).
        replay_snapshot = json.loads(
            (bridge / "latest_state.json").read_text(encoding="utf-8-sig")
        )
        input_path.write_text(
            json.dumps(replay_snapshot, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        hint = build_duplicant_priority_hint(initial, instance_id=123, chore_group="Build", priority=4)
        definition = {
            "schema": PLAN_DEFINITION_SCHEMA,
            "title": "Read-only clone coordinator fixture",
            "goal": "Produce one bounded candidate without publishing a command.",
            "steps": [{
                "id": "priority_1",
                "action": hint["action"],
                "args": hint["args"],
                "dependencies": [],
                "preconditions": hint["preconditions"],
                "expected_effects": hint["expected_effects"],
                "reason": "Isolated read-only candidate fixture",
            }],
        }
        plan = create_colony_plan(bridge, definition)
        plan_id = str(plan["plan_id"])
        assert plan["status"] == "draft"
        assert plan["steps"][0]["status"] == "waiting"
        identity = {key: initial[key] for key in IDENTITY_FIELDS}
        config = {
            "schema": oni_bridge.COORDINATOR_CONFIG_SCHEMA,
            "bridge_dir": str(bridge),
            "plan_id": plan_id,
            "step_ids": [str(plan["steps"][0]["id"])],
            "expected_identity": identity,
            "intent_directory": str(intent_dir),
            "clone_manifest": str(manifest_path),
            "clone_save": str(clone_save),
            "clone_preview": str(clone_preview),
            "state_path": str(intent_dir / "coordinator-state.json"),
        }
        config_path = work / "coordinator-config.json"
        config_path.write_text(
            json.dumps(config, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )

        pending_before = count_json(bridge / "commands" / "pending")
        command_tree_before = command_tree_snapshot(bridge)
        observer = oni_bridge.build_read_only_coordinator_observer(bridge, config_path)
        observer_calls = 0

        def counted_observer() -> dict[str, object]:
            nonlocal observer_calls
            observer_calls += 1
            return dict(observer())

        # Invoke the real production entry point with an explicit observer.
        # This verifies that run_once forwards the callback through
        # write_outputs and persists coordinator-observation.json.
        oni_bridge.run_once(bridge, input_path=input_path, coordinator_observer=counted_observer)
        assert observer_calls == 1, observer_calls
        observation_path = bridge / "coordinator-observation.json"
        assert observation_path.is_file(), "run_once did not persist coordinator observation"
        observation = json.loads(observation_path.read_text(encoding="utf-8-sig"))
        assert isinstance(observation, dict)
        assert observation.get("schema") == "oni.coordinator-observation.v1", observation
        assert observation.get("live_admission") is False, observation
        result = observation.get("result")
        assert isinstance(result, dict), observation
        pending_after = count_json(bridge / "commands" / "pending")
        command_tree_after = command_tree_snapshot(bridge)
        active = load_active_plan(bridge, plan_id)
        state_path = intent_dir / "coordinator-state.json"
        persisted = state_path.exists()
        side_effects = result.get("side_effects")
        if not isinstance(side_effects, dict):
            side_effects = {}
        side_effects_bounded = all(
            side_effects.get(key) is False
            for key in ("command_published", "approval", "execution", "successor_advanced")
        )
        assert result.get("status") in {"candidate", "blocked"}, result
        assert result.get("live_admission") is False, result
        # Replay input is intentionally non-actionable.  Keep the bounded
        # adapter diagnostic in evidence so this production-entrypoint check
        # proves *why* the candidate was blocked without exposing raw errors.
        if result.get("status") == "blocked":
            assert isinstance(result.get("observation_blocker"), str), result
        assert side_effects_bounded, result
        assert pending_before == 0 and pending_after == 0
        assert command_tree_before == command_tree_after
        assert active["steps"][0]["status"] == "waiting"

        evidence = {
            "schema": "oni.read-only-coordinator-clone-tick-evidence.v1",
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "status": "passed",
            "mode": "build_read_only_coordinator_observer",
            "entrypoint": "oni_bridge.run_once",
            "observer_callback_invocations": observer_calls,
            "live_admission": result.get("live_admission"),
            "tick_status": result.get("status"),
            "stop_reason": result.get("stop_reason"),
            "observation_blocker": result.get("observation_blocker"),
            "bounded_status": result.get("status") in {"candidate", "blocked"},
            "side_effects": side_effects,
            "side_effects_bounded": side_effects_bounded,
            "pending_command_count_before": pending_before,
            "pending_command_count_after": pending_after,
            "command_tree_unchanged": command_tree_before == command_tree_after,
            "command_tree_file_count": sum(
                1 for key in command_tree_after if not key.endswith("/")
            ),
            "pending_command_files_after": sorted(
                p.name for p in (bridge / "commands" / "pending").glob("*.json")
            ),
            "observation_file": observation_path.name,
            "observation_schema": observation.get("schema"),
            "plan_id": plan_id,
            "plan_step_id": plan["steps"][0]["id"],
            "plan_step_status_after": active["steps"][0]["status"],
            "clone": {
                "manifest_sha256": sha256_bytes(manifest_path.read_bytes()),
                "clone_save_sha256": clone_save_hash,
                "clone_preview_sha256": clone_preview_hash,
                "clone_save_path_id": path_id(clone_save),
                "clone_preview_path_id": path_id(clone_preview),
                "source_save_path_id": path_id(source_save),
                "source_preview_path_id": path_id(source_preview),
                "distinct_from_source_paths": (
                    path_id(clone_save) != path_id(source_save)
                    and path_id(clone_preview) != path_id(source_preview)
                ),
            },
            "identity": identity,
            "process_counts": result.get("process_counts"),
            "observation": {
                "id": result.get("observation_id"),
                "quality": result.get("observation_quality"),
                "freshness": result.get("observation_freshness"),
                "state_hash": result.get("state_hash"),
            },
            "coordinator_state_persisted": persisted,
            "fixture_root": str(work),
            "fixture_cleanup": "removed_after_report_write",
        }
        report = ROOT / "reports" / "controller-plan-sequence-20260913" / "read-only-clone-coordinator-tick-20260913.json"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(
            json.dumps(evidence, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        print(json.dumps({"report": str(report), "evidence": evidence}, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
