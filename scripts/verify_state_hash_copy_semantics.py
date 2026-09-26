#!/usr/bin/env python3
"""Check canonical hashes against the previous encoder on JSON snapshot trees."""
from __future__ import annotations

import copy
import hashlib
import json
import pathlib
import random
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent.state_quality import canonical_state_hash
from agent.strategy import evaluate_strategy
from scripts.verify_bridge_reliability import make_state


def previous_hash(state):
    payload = copy.deepcopy(state) if isinstance(state, dict) else {}
    for field in ("bridge", "writer", "read_metrics_ms"):
        payload.pop(field, None)
    sample = payload.get("sample")
    if isinstance(sample, dict):
        sample.pop("duration_ms", None)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def main():
    state = make_state("session-hash", 1)
    values = [None, [], 3, {}, state, evaluate_strategy(state),
              {"sample": None}, {"sample": [1, 2]},
              {"sample": {"duration_ms": 10, "interval_seconds": 5,
                          "new_field": [1, {"unicode": "氧气"}]},
               "future_telemetry": {"nested": [True, None, -1, 0.25]}}]
    rng = random.Random(71)

    def tree(depth=0):
        if depth > 3:
            return rng.choice([None, True, False, rng.randint(-100, 100), rng.random(), "氧气"])
        kind = rng.randrange(4)
        if kind == 0:
            return [tree(depth + 1) for _ in range(rng.randrange(5))]
        if kind == 1:
            return {f"k{i}": tree(depth + 1) for i in range(rng.randrange(5))}
        return tree(4)

    for _ in range(1000):
        values.append({"sample": {"duration_ms": rng.random(), "interval_seconds": 5},
                       "new_telemetry": tree(), "bridge": tree(), "writer": tree()})
    for value in values:
        before = copy.deepcopy(value)
        assert canonical_state_hash(value) == previous_hash(value)
        assert value == before, "hashing mutated its input"

    original = {"sample": {"duration_ms": 1, "interval_seconds": 5}, "new": {"value": 1}}
    expected = canonical_state_hash(original)
    for key in ("bridge", "writer", "read_metrics_ms"):
        changed = copy.deepcopy(original)
        changed[key] = {"arbitrary": [1, 2, 3]}
        assert canonical_state_hash(changed) == expected
    changed = copy.deepcopy(original)
    changed["sample"]["duration_ms"] = 999
    assert canonical_state_hash(changed) == expected
    for path in (("sample", "interval_seconds"), ("new", "value")):
        changed = copy.deepcopy(original)
        changed[path[0]][path[1]] = 7
        assert canonical_state_hash(changed) != expected, path

    for invalid in ({"bad": object()}, {"sample": {"bad": object()}}):
        errors = []
        for function in (previous_hash, canonical_state_hash):
            try:
                function(invalid)
            except TypeError:
                errors.append("TypeError")
        assert errors == ["TypeError", "TypeError"]

    report = {"status": "passed", "equivalence_and_nonmutation_cases": len(values),
              "field_binding_cases": 6, "invalid_value_cases": 2,
              "source_sha256": hashlib.sha256((ROOT / "agent/state_quality.py").read_bytes()).hexdigest()}
    output = ROOT / "reports/state-hash-copy-semantics.json"
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
