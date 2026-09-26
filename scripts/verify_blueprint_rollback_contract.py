#!/usr/bin/env python3
"""Verify that the published blueprint rollback contract matches the executor.

Static/offline only: this script reads source and protocol text and never
starts ONI, writes a queue, or grants live admission.
"""

from __future__ import annotations

import hashlib
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
PROTOCOL = ROOT / "docs" / "protocol.md"
EXECUTOR = ROOT / "mod" / "Source" / "Commands" / "ActionExecutor.cs"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    protocol = PROTOCOL.read_text(encoding="utf-8")
    executor = EXECUTOR.read_text(encoding="utf-8-sig")
    require("outcome is recorded as\n`outcome_unknown`" in protocol, "protocol omits unknown outcome")
    require("mutation safety latch is set" in protocol, "protocol omits safety latch")
    require("no automatic blueprint cancellation is claimed" in protocol, "protocol still claims timeout cancellation")
    unknown_start = executor.index("private static void FinishPendingUnknown")
    unknown_end = executor.index("private static string PauseForUnknownOutcome", unknown_start)
    unknown = executor[unknown_start:unknown_end]
    require('"outcome_unknown"' in unknown, "unknown path lacks outcome_unknown classification")
    require("mutationSafetyLatch = true" in unknown, "unknown path lacks mutation latch")
    require("CancelCreatedBlueprints" not in unknown, "unknown path performs automatic blueprint cancellation")
    failure_start = executor.index("private static void FinishPendingFailure")
    failure_end = executor.index("private static void FinishPendingUnknown", failure_start)
    failure = executor[failure_start:failure_end]
    require("CancelCreatedBlueprints" in failure, "unambiguous failure path lacks rollback")
    report = {
        "schema": "oni.blueprint-rollback-contract-verification.v1",
        "status": "passed",
        "live_admission": False,
        "unknown_path": "outcome_unknown+latch+pause+no_automatic_cancel",
        "failure_path": "behavior_fail+automatic_cancel",
        "protocol_sha256": hashlib.sha256(PROTOCOL.read_bytes()).hexdigest(),
        "executor_sha256": hashlib.sha256(EXECUTOR.read_bytes()).hexdigest(),
    }
    output = ROOT / "reports" / "blueprint-rollback-contract-verification.json"
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
