#!/usr/bin/env python3
"""Revalidate the named ONI test clone and runtime boundary without mutation.

This is a read-only evidence command.  It never launches ONI/Steam/Bridge,
creates queue directories, invokes a model, or calls a command adapter.  Two
fresh ``build_clone_probe`` calls are compared so a single stale preflight
cannot be mistaken for durable clone identity.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import pathlib
import sys
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.clone_launch_guard import (  # noqa: E402
    CloneLaunchGuardError,
    build_clone_probe,
    build_process_probe,
    build_queue_probe,
)

PROCESSES = ("OxygenNotIncluded", "oni_bridge", "steam")
QUEUES = (
    "commands/pending", "commands/approving", "commands/approved",
    "commands/executing", "commands/confirming",
    "validation-controls/approved", "validation-controls/inflight",
)


def digest(path: pathlib.Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def descriptor(path: pathlib.Path) -> dict[str, Any]:
    stat = path.stat()
    return {"sha256": digest(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def command_tree(root: pathlib.Path) -> dict[str, str]:
    commands = root / "commands"
    result: dict[str, str] = {}
    if not commands.is_dir():
        return result
    for path in sorted(commands.rglob("*")):
        rel = str(path.relative_to(commands)).replace("\\", "/")
        result[rel + ("/" if path.is_dir() else "")] = (
            "directory" if path.is_dir() else digest(path)
        )
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clone-root", type=pathlib.Path, required=True)
    parser.add_argument("--bridge-dir", type=pathlib.Path, required=True)
    parser.add_argument("--report", type=pathlib.Path, required=True)
    parser.add_argument("--clone-save", type=pathlib.Path)
    parser.add_argument("--clone-preview", type=pathlib.Path)
    parser.add_argument("--manifest", type=pathlib.Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.clone_root.expanduser().resolve()
    bridge = args.bridge_dir.expanduser().resolve()
    manifest = (args.manifest or root / "manifest.json").resolve()
    save = (args.clone_save or next(root.glob("*.sav"))).resolve()
    preview = (args.clone_preview or next(root.glob("*.png"))).resolve()
    before = {
        "clone_save": descriptor(save),
        "clone_preview": descriptor(preview),
        "manifest": descriptor(manifest),
        "command_tree": command_tree(bridge),
    }
    process_probe = build_process_probe(PROCESSES)
    queue_probe = build_queue_probe(bridge, QUEUES)
    probe = build_clone_probe(
        manifest, save, preview, bridge_dir=bridge,
        process_names=PROCESSES, process_probe=process_probe,
        queue_probe=queue_probe,
    )
    first = probe()
    second = probe()
    after = {
        "clone_save": descriptor(save),
        "clone_preview": descriptor(preview),
        "manifest": descriptor(manifest),
        "command_tree": command_tree(bridge),
    }
    stable_keys = (
        "manifest_sha256", "clone_save_sha256", "clone_preview_sha256",
        "clone_save_path_id", "clone_preview_path_id", "bridge_dir",
        "process_counts", "queue_counts", "launch_allowed",
    )
    stable = all(first.get(key) == second.get(key) for key in stable_keys)
    unchanged = before == after
    if not stable or not unchanged or first.get("launch_allowed") is not True:
        raise RuntimeError("clone boundary was not stable or read-only")
    report = {
        "schema": "oni.real-clone-boundary-verification.v1",
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "status": "passed",
        "live_admission": False,
        "game_launched": False,
        "clone_root": str(root),
        "bridge_dir": str(bridge),
        "manifest": str(manifest),
        "clone_save": str(save),
        "clone_preview": str(preview),
        "fresh_probe_calls": 2,
        "probe_identity_stable": stable,
        "inputs_and_command_tree_unchanged": unchanged,
        "preflight": second,
        "before": before,
        "after": after,
        "rollback": "no mutation; no rollback required",
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (CloneLaunchGuardError, OSError, StopIteration, RuntimeError) as exc:
        print(f"blocked: {exc}", file=sys.stderr)
        raise SystemExit(2)
