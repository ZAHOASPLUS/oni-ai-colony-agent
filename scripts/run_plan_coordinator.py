#!/usr/bin/env python3
"""Run one bounded, clone-gated Colony Plan coordinator tick.

The command is read-only by default.  ``--publish-one`` is an explicit opt-in
for publishing the current leaf's pending command; approval and execution are
always external.  It never starts ONI, Steam, Bridge, or a model request.
Fixture mode is explicitly isolated below a dedicated ``--test-root`` in the
system temporary directory.  Its clone-preflight and process-count JSON files
are caller-supplied snapshots; they are not a live process probe or admission
proof. In ``--live-boundary-probe`` mode, the preflight is built fresh from the
named clone files and current process/queue probes.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import tempfile
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.clone_launch_guard import (CloneLaunchGuardError, build_clone_probe,
                                       build_process_probe, build_queue_probe)
from agent.plan_coordinator import PlanCoordinatorError, PlanSequenceCoordinator


PROTECTED_PROCESS_NAMES = ("OxygenNotIncluded", "oni_bridge", "steam")
PROTECTED_QUEUE_NAMES = (
    "commands/pending", "commands/approving", "commands/approved",
    "commands/executing", "commands/confirming",
    "validation-controls/approved", "validation-controls/inflight",
)


def _json_object(path: pathlib.Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"), parse_constant=_reject_constant)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise PlanCoordinatorError(f"{label}_unreadable") from exc
    if not isinstance(value, dict):
        raise PlanCoordinatorError(f"{label}_object_required")
    return value


def _reject_constant(value: str) -> Any:
    raise ValueError(f"non-finite JSON constant: {value}")


def _fixture_root(path: pathlib.Path) -> pathlib.Path:
    """Resolve an isolated fixture root beneath the system temp directory."""
    raw = pathlib.Path(path).expanduser()
    if raw.is_symlink():
        raise PlanCoordinatorError("fixture_test_root_must_not_be_symlink")
    try:
        root = raw.resolve(strict=True)
        temp_root = pathlib.Path(tempfile.gettempdir()).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise PlanCoordinatorError("fixture_test_root_unreadable") from exc
    if not root.is_dir() or root == temp_root or not root.is_relative_to(temp_root):
        raise PlanCoordinatorError("fixture_test_root_must_be_temp_descendant")
    # A temporary directory under the project is still a path into the
    # working tree.  Keep fixture mode disjoint from repository data even if
    # the project is later copied under the system temp directory.
    if root == ROOT or root.is_relative_to(ROOT):
        raise PlanCoordinatorError("fixture_test_root_overlaps_workspace")
    return root


def _fixture_path(path: pathlib.Path, root: pathlib.Path, label: str) -> pathlib.Path:
    """Return a path strictly below ``root``; reject links and escapes."""
    raw = pathlib.Path(path).expanduser()
    if raw.is_symlink():
        raise PlanCoordinatorError(f"{label}_symlink_forbidden")
    try:
        candidate = raw.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise PlanCoordinatorError(f"{label}_unreadable") from exc
    if candidate == root or not candidate.is_relative_to(root):
        raise PlanCoordinatorError(f"{label}_outside_fixture_root")
    return candidate


def _fixture_directory(path: pathlib.Path, root: pathlib.Path, label: str) -> pathlib.Path:
    candidate = _fixture_path(path, root, label)
    try:
        if not candidate.is_dir():
            raise PlanCoordinatorError(f"{label}_directory_required")
    except OSError as exc:
        raise PlanCoordinatorError(f"{label}_unreadable") from exc
    return candidate


def _validate_fixture_process_counts(value: dict[str, Any]) -> dict[str, int]:
    if set(value) != set(PROTECTED_PROCESS_NAMES):
        raise PlanCoordinatorError("process_counts_keys_incomplete")
    if any(type(count) is not int or count < 0 for count in value.values()):
        raise PlanCoordinatorError("process_counts_invalid")
    return {name: int(value[name]) for name in PROTECTED_PROCESS_NAMES}


def _validate_fixture_preflight(value: dict[str, Any], bridge_dir: pathlib.Path) -> dict[str, Any]:
    if value.get("schema") != "oni.clone-launch-preflight.v1":
        raise PlanCoordinatorError("clone_preflight_schema_invalid")
    if value.get("launch_allowed") is not True:
        raise PlanCoordinatorError("clone_launch_not_allowed")
    try:
        claimed_bridge = pathlib.Path(str(value.get("bridge_dir") or "")).expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        raise PlanCoordinatorError("clone_preflight_bridge_dir_invalid") from exc
    if claimed_bridge != bridge_dir:
        raise PlanCoordinatorError("clone_preflight_bridge_dir_mismatch")
    process_counts = value.get("process_counts")
    if not isinstance(process_counts, dict):
        raise PlanCoordinatorError("process_counts_object_required")
    _validate_fixture_process_counts(process_counts)
    if any(count != 0 for count in process_counts.values()):
        raise PlanCoordinatorError("clone_preflight_boundary_nonempty")
    queue_counts = value.get("queue_counts")
    if not isinstance(queue_counts, dict) or set(queue_counts) != set(PROTECTED_QUEUE_NAMES):
        raise PlanCoordinatorError("queue_counts_keys_incomplete")
    if any(type(count) is not int or count < 0 for count in queue_counts.values()):
        raise PlanCoordinatorError("queue_counts_invalid")
    if any(count != 0 for count in queue_counts.values()):
        raise PlanCoordinatorError("clone_preflight_boundary_nonempty")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bridge-dir", type=pathlib.Path, required=True)
    parser.add_argument("--plan-id", required=True)
    parser.add_argument("--step-ids", nargs="+", required=True)
    parser.add_argument("--expected-identity-json", type=pathlib.Path, required=True)
    parser.add_argument("--intent-dir", type=pathlib.Path, required=True)
    parser.add_argument("--clone-preflight-json", type=pathlib.Path,
                        help="caller-supplied preflight snapshot for fixture mode")
    parser.add_argument("--process-counts-json", type=pathlib.Path,
                        help="caller-supplied process snapshot for fixture mode")
    parser.add_argument("--live-boundary-probe", action="store_true",
                        help="read the current process table and existing queue directories each probe")
    parser.add_argument("--clone-manifest", type=pathlib.Path,
                        help="clone manifest path; required with the live boundary probe")
    parser.add_argument("--clone-save", type=pathlib.Path,
                        help="clone .sav path; required with the live boundary probe")
    parser.add_argument("--clone-preview", type=pathlib.Path,
                        help="clone preview path; required with the live boundary probe")
    parser.add_argument("--test-root", type=pathlib.Path,
                        help="fixture-only root; must be a dedicated directory below the system temp root")
    parser.add_argument("--state-path", type=pathlib.Path)
    parser.add_argument("--publish-one", action="store_true",
                        help="explicitly publish at most the current leaf's pending command")
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        fixture_mode = not args.live_boundary_probe
        if fixture_mode:
            if args.publish_one:
                raise PlanCoordinatorError("publish_one_requires_live_boundary_probe")
            if args.test_root is None:
                raise PlanCoordinatorError("fixture_test_root_required")
            test_root = _fixture_root(args.test_root)
            bridge_dir = _fixture_directory(args.bridge_dir, test_root, "bridge_dir")
            intent_directory = _fixture_directory(args.intent_dir, test_root, "intent_dir")
            identity_path = _fixture_path(args.expected_identity_json, test_root, "expected_identity")
            clone_preflight_path = (
                _fixture_path(args.clone_preflight_json, test_root, "clone_preflight")
                if args.clone_preflight_json is not None else None
            )
            process_counts_path = (
                _fixture_path(args.process_counts_json, test_root, "process_counts")
                if args.process_counts_json is not None else None
            )
            clone_manifest = (
                _fixture_path(args.clone_manifest, test_root, "clone_manifest")
                if args.clone_manifest is not None else None
            )
            clone_save = (
                _fixture_path(args.clone_save, test_root, "clone_save")
                if args.clone_save is not None else None
            )
            clone_preview = (
                _fixture_path(args.clone_preview, test_root, "clone_preview")
                if args.clone_preview is not None else None
            )
            state_path = (
                _fixture_path(args.state_path, test_root, "state_path")
                if args.state_path is not None else None
            )
            if args.clone_preflight_json is None or args.process_counts_json is None:
                raise PlanCoordinatorError("fixture_snapshots_required")
        else:
            if args.test_root is not None:
                raise PlanCoordinatorError("test_root_forbidden_with_live_probe")
            if args.clone_preflight_json is not None or args.process_counts_json is not None:
                raise PlanCoordinatorError("fixture_snapshots_forbidden_with_live_probe")
            bridge_dir = args.bridge_dir
            intent_directory = args.intent_dir
            identity_path = args.expected_identity_json
            clone_preflight_path = None
            process_counts_path = None
            clone_manifest = args.clone_manifest
            clone_save = args.clone_save
            clone_preview = args.clone_preview
            state_path = args.state_path

        identity = _json_object(identity_path, "expected_identity")
        if args.live_boundary_probe:
            process_probe = build_process_probe(PROTECTED_PROCESS_NAMES)
            queue_probe = build_queue_probe(bridge_dir, PROTECTED_QUEUE_NAMES)
            process_probe()
        else:
            preflight = _validate_fixture_preflight(
                _json_object(clone_preflight_path, "clone_preflight"), bridge_dir,
            )
            process_counts = _validate_fixture_process_counts(
                _json_object(process_counts_path, "process_counts"),
            )
            process_probe = lambda: process_counts
            queue_probe = lambda: preflight.get("queue_counts", {})
        clone_paths = (clone_manifest, clone_save, clone_preview)
        if any(path is not None for path in clone_paths) and not all(path is not None for path in clone_paths):
            raise PlanCoordinatorError("clone_paths_incomplete")
        if args.live_boundary_probe and not all(path is not None for path in clone_paths):
            raise PlanCoordinatorError("live_probe_requires_clone_paths")
        if all(path is not None for path in clone_paths):
            clone_probe = build_clone_probe(
                clone_manifest, clone_save, clone_preview,
                bridge_dir=bridge_dir,
                process_names=PROTECTED_PROCESS_NAMES,
                process_probe=process_probe,
                queue_probe=queue_probe,
            )
            if args.live_boundary_probe:
                preflight = clone_probe()
        else:
            clone_probe = lambda: preflight
        coordinator = PlanSequenceCoordinator(
            bridge_dir,
            plan_id=args.plan_id,
            step_ids=args.step_ids,
            expected_identity=identity,
            intent_directory=intent_directory,
            clone_preflight=preflight,
            clone_probe=clone_probe,
            process_probe=process_probe,
            publish_one=args.publish_one,
            state_path=state_path,
        )
        result = coordinator.tick()
    except (CloneLaunchGuardError, PlanCoordinatorError, OSError, ValueError) as exc:
        payload = {"schema": "oni.plan-coordinator-tick.v1", "status": "blocked",
                   "stop_reason": "coordinator_input_invalid", "live_admission": False}
        if args.json_output:
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        else:
            print("blocked: coordinator_input_invalid", file=sys.stderr)
        return 2
    if args.json_output:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    if result.get("status") == "blocked":
        return 2
    if args.publish_one and result.get("side_effects", {}).get("command_published") is True:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
