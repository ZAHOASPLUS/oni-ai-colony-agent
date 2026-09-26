#!/usr/bin/env python3
"""Run the real Bridge CLI with observational call counts for its budget test."""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bridge import oni_bridge as bridge


def main():
    metrics_path = pathlib.Path(sys.argv[1])
    sys.argv = [str(ROOT / "bridge/oni_bridge.py"), *sys.argv[2:]]
    counts = {"process": 0, "evaluate_strategy": 0}
    original_process = bridge.ReplayState.process
    original_evaluate = bridge.evaluate_strategy

    def process(self, record):
        counts["process"] += 1
        return original_process(self, record)

    def evaluate(*args, **kwargs):
        counts["evaluate_strategy"] += 1
        return original_evaluate(*args, **kwargs)

    bridge.ReplayState.process = process
    bridge.evaluate_strategy = evaluate
    code = bridge.main()
    metrics_path.write_text(json.dumps({"counts": counts, "exit_code": code}) + "\n", encoding="utf-8")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
