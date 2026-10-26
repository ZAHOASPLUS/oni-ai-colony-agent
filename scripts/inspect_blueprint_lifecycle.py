"""Rebuild every blueprint instance's lifetime from a recorded Probe state log.

Usage: python scripts/inspect_blueprint_lifecycle.py STATE_JSONL [STATE_JSONL ...] [--json OUT]

Each input is a ``state.jsonl`` (optionally ``.gz``) written by the Probe. The
report lists, per ``blueprints.by_instance_id`` entry, when it first and last
appeared, its highest progress and the workers seen on it, plus the session's
starting cycle. A blueprint that disappears is reported as "vanished", never as
"cancelled": a placement also leaves the blueprint list when its construction
completes, which is how the Phase 2 Electrolysers were misread as cancelled
(docs/b2-blueprint-lifecycle-20261027.md). Read-only; it never touches a live
bridge directory beyond reading the given files.
"""

from __future__ import annotations

import argparse
import gzip
import json
import pathlib
import sys
from typing import Any, Dict, Iterator, List


def _lines(path: pathlib.Path) -> Iterator[bytes]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rb") as handle:
        yield from handle


def lifecycle(path: pathlib.Path) -> Dict[str, Any]:
    instances: Dict[str, Dict[str, Any]] = {}
    transitions: List[Dict[str, Any]] = []
    previous: set = set()
    first_cycle = None
    snapshots = 0
    for raw in _lines(path):
        try:
            record = json.loads(raw)
        except ValueError:
            continue
        if record.get("event") != "state_snapshot":
            continue
        snapshots += 1
        ts = record.get("ts")
        game_time = record.get("game_time_cycles")
        paused = (record.get("game") or {}).get("paused")
        if first_cycle is None:
            first_cycle = game_time
        blueprints = (record.get("blueprints") or {}).get("by_instance_id") or {}
        current = set()
        for key, entry in blueprints.items():
            if not isinstance(entry, dict):
                continue
            current.add(key)
            item = instances.setdefault(key, {
                "prefab_id": entry.get("prefab_id"), "cell": entry.get("cell"),
                "first_ts": ts, "first_game_time": game_time,
                "max_progress": None, "workers": [],
            })
            item["last_ts"] = ts
            item["last_game_time"] = game_time
            item["last_progress"] = entry.get("progress_percent")
            progress = entry.get("progress_percent")
            if isinstance(progress, (int, float)) and not isinstance(progress, bool):
                if item["max_progress"] is None or progress > item["max_progress"]:
                    item["max_progress"] = progress
            worker = entry.get("worker_instance_id")
            if worker and worker not in item["workers"]:
                item["workers"].append(worker)
        for key in sorted(current - previous):
            transitions.append({"ts": ts, "paused": paused, "kind": "appear", "id": key})
        for key in sorted(previous - current):
            transitions.append({"ts": ts, "paused": paused, "kind": "vanish", "id": key,
                                "last_progress": instances[key].get("last_progress")})
        previous = current
    return {"source": str(path), "snapshots": snapshots, "first_game_time": first_cycle,
            "instances": instances, "transitions": transitions}


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("logs", nargs="+", type=pathlib.Path)
    parser.add_argument("--json", type=pathlib.Path, help="also write the full result here")
    args = parser.parse_args(argv)
    results = [lifecycle(path) for path in args.logs]
    for result in results:
        print(f"== {result['source']}: {result['snapshots']} snapshots, "
              f"starting at game time {result['first_game_time']}")
        for key, item in result["instances"].items():
            print(f"  {key} {item['prefab_id']} cell {item['cell']} "
                  f"{item['first_ts']} -> {item['last_ts']} "
                  f"max progress {item['max_progress']} workers {item['workers']}")
        for event in result["transitions"]:
            extra = f" (last progress {event['last_progress']})" if event["kind"] == "vanish" else ""
            print(f"  {event['ts']} {'paused ' if event['paused'] else 'running'} "
                  f"{event['kind']} {event['id']}{extra}")
    if args.json:
        args.json.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
