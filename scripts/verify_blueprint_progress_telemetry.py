#!/usr/bin/env python3
"""Offline contract checks for additive blueprint progress telemetry.

This is a synthetic field-contract test only. It never starts ONI, Steam, a
bridge, or a command queue, and it cannot grant live admission.
"""

from __future__ import annotations

import datetime as dt
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.field_contracts import (  # noqa: E402
    BLUEPRINT_CELL_CONTRACT_ID,
    BLUEPRINT_INSTANCE_CONTRACT_ID,
    contract_quality_fixture,
    read_contract_field,
)

TS = dt.datetime(2026, 9, 12, tzinfo=dt.timezone.utc).isoformat()


def _state(path: str, value: object) -> dict:
    parts = path.split(".")
    cursor: dict = {}
    root = cursor
    for part in parts[:-1]:
        child: dict = {}
        cursor[part] = child
        cursor = child
    cursor[parts[-1]] = value
    cursor = root
    quality = {
        path: contract_quality_fixture(
        BLUEPRINT_CELL_CONTRACT_ID
        if path.startswith("cells.")
        else BLUEPRINT_INSTANCE_CONTRACT_ID
        )
    }
    return {
        "schema": "oni.state.v2",
        "seq": 1,
        "snapshot_seq": 1,
        "snapshot_id": "blueprint-progress-contract:1",
        "ts": TS,
        "quality": quality,
        **root,
    }


def _check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail}".rstrip())


def main() -> int:
    cases = [
        ("cell_progress_0", "cells.by_id.cell_1.blueprint_progress_percent", 0, True),
        ("cell_progress_100", "cells.by_id.cell_1.blueprint_progress_percent", 100, True),
        ("cell_worker", "cells.by_id.cell_1.blueprint_worker_instance_id", 42, True),
        ("instance_progress", "blueprints.by_instance_id.id_42.progress_percent", 37, True),
        ("instance_worker", "blueprints.by_instance_id.id_42.worker_instance_id", 7, True),
        ("progress_overflow", "cells.by_id.cell_1.blueprint_progress_percent", 101, False),
        ("progress_negative", "cells.by_id.cell_1.blueprint_progress_percent", -1, False),
        ("worker_type", "cells.by_id.cell_1.blueprint_worker_instance_id", True, False),
    ]
    results = []
    for label, path, value, expected_usable in cases:
        read = read_contract_field(_state(path, value), path)
        _check(label, bool(read.usable) is expected_usable, f"usable={read.usable}, blockers={read.blockers}")
        results.append({"id": label, "usable": bool(read.usable), "blockers": list(read.blockers)})
    print({"cases": len(results), "passed": len(results), "results": results})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
