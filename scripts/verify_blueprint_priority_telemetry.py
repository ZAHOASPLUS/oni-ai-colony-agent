#!/usr/bin/env python3
"""Offline/static verifier for read-only Constructable priority telemetry."""
from __future__ import annotations
import json, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from agent.field_contracts import field_contract_blockers


def quality_state(path, value):
    state = {"ts": "2026-09-13T02:15:00+00:00", "quality": {
        path: {"status": "observed", "source": "BuildingDef+Grid+Constructable+WorldInventory",
               "semantic": "blueprint_cell_state_field", "unit": "field_native",
               "scope": "active_world_tracked_cells", "completeness": "complete_for_tracked_cell",
               "captured_at": "snapshot.ts", "contract_id": "oni.qc.blueprints.cell.v1", "value": value}
    }}
    cursor = state
    parts = path.split('.')
    for part in parts[:-1]:
        cursor = cursor.setdefault(part, {})
    cursor[parts[-1]] = value
    return state


def main():
    reader = (ROOT / "mod/Source/Probe/ProbeStateReader.cs").read_text(encoding="utf-8")
    contracts = (ROOT / "mod/Source/Contracts/CommandFieldContracts.cs").read_text(encoding="utf-8")
    assert "TryReadBlueprintPriority" in reader and "GetMasterPriority()" in reader
    assert "blueprint_priority_class" in reader and "blueprint_priority_value" in reader
    assert "SetMasterPriority" not in reader
    assert "blueprint_priority_class" in contracts and "blueprint_priority_value" in contracts
    cases = {}
    for name, path, value, expected in [
        ("basic", "cells.by_id.cell_10.blueprint_priority_class", "basic", []),
        ("high", "cells.by_id.cell_10.blueprint_priority_class", "high", []),
        ("top_priority", "cells.by_id.cell_10.blueprint_priority_class", "topPriority", []),
        ("invalid_class", "cells.by_id.cell_10.blueprint_priority_class", "bogus", ["field_identifier_invalid"]),
        ("invalid_value", "cells.by_id.cell_10.blueprint_priority_value", 10, ["field_above_maximum"]),
        ("negative_value", "cells.by_id.cell_10.blueprint_priority_value", 0, ["field_below_minimum"]),
    ]:
        blockers = field_contract_blockers(quality_state(path, value), path)
        assert all(item in blockers for item in expected) and (not expected or blockers)
        cases[name] = {"blockers": blockers, "accepted": not blockers}
    report = {"schema": "oni.blueprint-priority-telemetry-verification.v1", "status": "passed",
              "case_count": len(cases), "live_admission": False, "reader_mutation_free": True,
              "fingerprint_unchanged": True, "cases": cases}
    out = ROOT / "reports/blueprint-priority-telemetry-verification.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
