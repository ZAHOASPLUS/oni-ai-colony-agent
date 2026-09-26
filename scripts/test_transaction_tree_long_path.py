#!/usr/bin/env python3
"""Regression for Windows extended-path transaction ownership capture."""

from __future__ import annotations

import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import manage_live_validation_environment as workflow


def remove_extended(path: pathlib.Path) -> None:
    io_path = workflow._io_path(path)
    for entry in list(os.scandir(io_path)):
        child = path / entry.name
        if entry.is_dir(follow_symlinks=False):
            remove_extended(child)
        else:
            os.unlink(workflow._io_path(child))
    os.rmdir(io_path)


def main() -> int:
    root = ROOT / "reports" / "controller-plan-sequence-20260913" / "_long-path-fixture"
    if root.exists():
        remove_extended(root)
    deep = root
    for _ in range(7):
        deep /= "nested-" + "y" * 35
    try:
        os.makedirs(workflow._io_path(deep))
        with open(workflow._io_path(deep / "payload.txt"), "wb") as handle:
            handle.write(b"owner")
        if len(str(deep)) <= 260:
            raise AssertionError("fixture did not cross MAX_PATH")
        captured = workflow.capture_transaction_node(root)
        if captured.get("kind") != "directory" or not any(
            item[0].endswith("payload.txt") for item in captured["node_identities"]
        ):
            raise AssertionError("long-path transaction identity capture was incomplete")
        print(f"long_path_transaction_capture=ok path_chars={len(str(deep))}")
        return 0
    finally:
        if root.exists():
            remove_extended(root)


if __name__ == "__main__":
    raise SystemExit(main())
