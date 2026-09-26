#!/usr/bin/env python3
"""Offline contract checks for bounded blueprint root/footprint telemetry."""

from __future__ import annotations

import datetime as dt
import pathlib
import re
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
SOURCE = (ROOT / "mod/Source/Probe/ProbeStateReader.cs").read_text(encoding="utf-8")


def state(path: str, value: object) -> dict:
    parts = path.split(".")
    payload: dict = {}
    cursor = payload
    for part in parts[:-1]:
        child: dict = {}
        cursor[part] = child
        cursor = child
    cursor[parts[-1]] = value
    contract_id = (
        BLUEPRINT_CELL_CONTRACT_ID
        if path.startswith("cells.")
        else BLUEPRINT_INSTANCE_CONTRACT_ID
    )
    return {
        "schema": "oni.state.v2",
        "seq": 1,
        "snapshot_seq": 1,
        "snapshot_id": "blueprint-footprint-contract:1",
        "ts": TS,
        "quality": {path: contract_quality_fixture(contract_id)},
        **payload,
    }


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail}".rstrip())


def main() -> int:
    helper = re.search(
        r"private static bool TryReadBlueprintFootprint\(.*?\n        }\n\n        private static void RefreshBlueprintCandidates",
        SOURCE,
        re.DOTALL,
    )
    check("footprint helper present", helper is not None)
    helper_text = helper.group(0)
    for marker in ("GetBottomLeftCell()", "PlacementCells", "Grid.IsValidCellInWorld", "ordered.Sort()", "ReadActiveWorldId() != worldId"):
        check(f"source marker: {marker}", marker in helper_text)
    check("bounded footprint", "ordered.Count > 64" in helper_text)
    check("root belongs to footprint", "ordered.Contains(candidateRoot)" in helper_text)
    check("root identity binding", "blueprint_root_cell" in SOURCE and "canonical root" in SOURCE)
    cases = [
        ("cell_single", "cells.by_id.cell_1.blueprint_footprint_cells", [1], True),
        ("cell_multi_sorted", "cells.by_id.cell_1.blueprint_footprint_cells", [1, 2, 3, 4, 5, 6], True),
        ("instance_multi", "blueprints.by_instance_id.id_42.footprint_cells", [10, 11], True),
        ("empty", "cells.by_id.cell_1.blueprint_footprint_cells", [], False),
        ("duplicate", "cells.by_id.cell_1.blueprint_footprint_cells", [1, 1], False),
        ("unsorted", "cells.by_id.cell_1.blueprint_footprint_cells", [2, 1], False),
        ("negative", "cells.by_id.cell_1.blueprint_footprint_cells", [-1], False),
        ("wrong_type", "cells.by_id.cell_1.blueprint_footprint_cells", [True], False),
        ("too_many", "cells.by_id.cell_1.blueprint_footprint_cells", list(range(65)), False),
    ]
    results = []
    for label, path, value, expected in cases:
        read = read_contract_field(state(path, value), path)
        check(label, bool(read.usable) is expected, f"usable={read.usable}, blockers={read.blockers}")
        results.append({"id": label, "usable": bool(read.usable), "blockers": list(read.blockers)})
    print({"cases": len(results), "passed": len(results), "results": results})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
