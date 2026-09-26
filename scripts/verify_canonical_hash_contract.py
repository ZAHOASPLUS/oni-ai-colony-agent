#!/usr/bin/env python3
"""Offline Python/C# verification for typed canonical approval hashes."""

from __future__ import annotations

import argparse
import copy
import json
import math
import pathlib
import subprocess
import sys
from typing import Any, Mapping


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import agent.action_outcomes as action_outcomes  # noqa: E402
from agent.approval_receipt import canonical_hash as approval_hash  # noqa: E402
from agent.canonical_hash import (  # noqa: E402
    CANONICAL_HASH_CONTRACT,
    CanonicalHashError,
    canonical_hash,
    loads_strict,
)


VECTORS_PATH = PROJECT_ROOT / "tools" / "CanonicalHashHarness" / "vectors.json"
HARNESS_PROJECT = PROJECT_ROOT / "tools" / "CanonicalHashHarness" / "CanonicalHashHarness.csproj"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def read_vectors() -> Mapping[str, Any]:
    value = loads_strict(VECTORS_PATH.read_text(encoding="utf-8"))
    require(isinstance(value, dict), "vector manifest is not an object")
    require(value.get("contract") == CANONICAL_HASH_CONTRACT, "vector contract drifted")
    return value


def verify_python(vectors: Mapping[str, Any]) -> dict[str, Any]:
    hashes: dict[str, str] = {}
    for item in vectors["cases"]:
        name = item["name"]
        actual = canonical_hash(item["value"])
        require(actual == item["expected_hash"], f"Python vector mismatch: {name}")
        require(name not in hashes, f"duplicate vector name: {name}")
        hashes[name] = actual

    for item in vectors["equivalent_pairs"]:
        require(
            canonical_hash(item["left"]) == canonical_hash(item["right"]),
            f"equivalent pair drifted: {item['name']}",
        )

    for item in vectors["tamper_pairs"]:
        require(
            canonical_hash(item["left"]) != canonical_hash(item["right"]),
            f"tamper pair collided: {item['name']}",
        )

    for item in vectors["invalid_json"]:
        try:
            loads_strict(item["json"])
        except CanonicalHashError:
            pass
        else:
            raise AssertionError(f"invalid JSON accepted: {item['name']}")

    rejected = 0
    for name, value in (
        ("nan", math.nan),
        ("positive_infinity", math.inf),
        ("negative_infinity", -math.inf),
        ("unpaired_surrogate", "\ud800"),
        ("unsupported_tuple", (1, 2)),
        ("non_string_key", {1: "value"}),
        ("integer_above_int64", 9223372036854775808),
        ("integer_below_int64", -9223372036854775809),
    ):
        try:
            canonical_hash(value)
        except CanonicalHashError:
            rejected += 1
        else:
            raise AssertionError(f"programmatic invalid value accepted: {name}")

    return {
        "case_hashes": hashes,
        "case_count": len(hashes),
        "equivalent_pair_count": len(vectors["equivalent_pairs"]),
        "tamper_pair_count": len(vectors["tamper_pairs"]),
        "invalid_count": len(vectors["invalid_json"]) + rejected,
    }


def verify_hash_wiring() -> dict[str, bool]:
    command = {
        field: None for field in action_outcomes._COMMAND_INTENT_FIELDS
    }
    command.update(
        {
            "schema": "oni.command.v3",
            "id": "canonical-vector-command",
            "action": "set_paused",
            "args": {"paused": False, "threshold": 1e-7, "signed_zero": -0.0},
            "world_id": 9007199254740993,
            "evidence": ["before\u2028after", "rocket:\U0001f680"],
        }
    )
    projection = {
        field: copy.deepcopy(command.get(field))
        for field in action_outcomes._COMMAND_INTENT_FIELDS
    }
    require(
        action_outcomes.command_intent_hash(command) == canonical_hash(projection),
        "command_intent_hash is not wired to the v1 contract",
    )

    policy = {
        "schema": "oni.action-policy.v1",
        "actions_enabled": True,
        "allowed_actions": ["set_paused"],
        "numeric_boundary": 1e20,
    }
    require(approval_hash(policy) == canonical_hash(policy), "policy_hash path drifted")

    receipt_payload = {
        "schema": "oni.approval-receipt.v1",
        "command_id": command["id"],
        "command_intent_hash": action_outcomes.command_intent_hash(command),
        "policy_hash": approval_hash(policy),
        "approved_by": "local_human",
        "source": {"world_id": command["world_id"]},
    }
    require(
        approval_hash(receipt_payload) == canonical_hash(receipt_payload),
        "receipt_hash path drifted",
    )
    return {
        "command_intent_hash": True,
        "policy_hash": True,
        "receipt_hash": True,
    }


def run_csharp(vectors: Mapping[str, Any], python_result: Mapping[str, Any]) -> Mapping[str, Any]:
    build = subprocess.run(
        ["dotnet", "build", str(HARNESS_PROJECT), "-c", "Release", "--nologo"],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    require(
        build.returncode == 0,
        "C# harness build failed:\n" + (build.stdout + build.stderr)[-6000:],
    )

    run = subprocess.run(
        [
            "dotnet",
            "run",
            "--project",
            str(HARNESS_PROJECT),
            "-c",
            "Release",
            "--no-build",
            "--",
            str(VECTORS_PATH),
        ],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    require(run.returncode == 0, "C# harness failed:\n" + (run.stdout + run.stderr)[-6000:])
    lines = [line for line in run.stdout.splitlines() if line.strip()]
    require(bool(lines), "C# harness produced no result")
    result = loads_strict(lines[-1])
    require(isinstance(result, dict) and result.get("status") == "passed", "C# harness did not pass")
    require(result.get("contract") == CANONICAL_HASH_CONTRACT, "C# contract identifier drifted")
    require(
        result.get("case_hashes") == python_result.get("case_hashes"),
        "Python and C# case hashes differ",
    )
    require(result.get("case_count") == len(vectors["cases"]), "C# case count drifted")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)

    vectors = read_vectors()
    python_result = verify_python(vectors)
    wiring = verify_hash_wiring()
    csharp_result = run_csharp(vectors, python_result)
    summary = {
        "schema": "oni.canonical-hash-verification.v1",
        "status": "passed",
        "contract": CANONICAL_HASH_CONTRACT,
        "python": {key: value for key, value in python_result.items() if key != "case_hashes"},
        "csharp": {key: value for key, value in csharp_result.items() if key != "case_hashes"},
        "wiring": wiring,
        "cross_language_hash_count": len(python_result["case_hashes"]),
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
