#!/usr/bin/env python3
"""Offline guard checks for fail-closed blueprint construction mutations."""

from __future__ import annotations

import math
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "mod/Source/Commands/ActionExecutor.cs").read_text(encoding="utf-8")


def check(label: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(label)


def state(worker: bool, progress: float) -> tuple[bool, bool]:
    """Model the native sentinel and fail-closed boundary used by the helper."""
    if not math.isfinite(progress) or progress > 1.0 or (progress < 0.0 and progress != -1.0):
        return False, False
    return True, worker or progress > 0.0


def main() -> int:
    helper = re.search(
        r"private static bool TryReadBlueprintConstructionState\(.*?\n        }\n\n        private static bool TryValidateDigMarkTarget",
        SOURCE,
        re.DOTALL,
    )
    check("shared helper present", helper is not None)
    helper_text = helper.group(0)
    for marker in (
        "GetWorker()",
        "GetPercentComplete()",
        "float.IsNaN",
        "float.IsInfinity",
        "progress > 1f",
        "progress < 0f && progress != -1f",
        "catch",
    ):
        check(f"helper marker: {marker}", marker in helper_text)
    check("cancel-created call site", "TryReadBlueprintConstructionState(constructable, out bool inProgress" in SOURCE)
    check("cancel-target call site", "TryReadBlueprintConstructionState(constructable, out bool inProgress, out string constructionError)" in SOURCE)
    check("verify-placed call site", SOURCE.count("TryReadBlueprintConstructionState(constructable, out bool inProgress, out _)") >= 2)

    cases = [
        ("native_not_started_sentinel", False, -1.0, True, False),
        ("zero_progress", False, 0.0, True, False),
        ("worker_claimed", True, 0.0, True, True),
        ("positive_progress", False, 0.25, True, True),
        ("nan", False, float("nan"), False, False),
        ("infinity", False, float("inf"), False, False),
        ("above_one", False, 1.01, False, False),
        ("below_sentinel", False, -1.01, False, False),
        ("fractional_negative", False, -0.5, False, False),
    ]
    for label, worker, progress, expected_valid, expected_in_progress in cases:
        valid, in_progress = state(worker, progress)
        check(label, (valid, in_progress) == (expected_valid, expected_in_progress))
    print({"cases": len(cases), "passed": len(cases), "fail_closed": True})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
