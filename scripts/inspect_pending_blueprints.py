"""Report whether the pending-blueprint count matches the actual construction state."""

from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.candidates import _pending_blueprint_count  # noqa: E402

KLEI = pathlib.Path.home() / "Documents" / "Klei" / "OxygenNotIncluded"
BD = KLEI / "ai-bridge"


def main() -> int:
    state = json.loads((BD / "latest_state.json").read_text(encoding="utf-8-sig"))
    buildings = state.get("buildings") or {}
    instances = (state.get("blueprints") or {}).get("by_instance_id") or {}
    print("=== construction counts ===")
    print("buildings.oxygen_diffuser (completed):", buildings.get("oxygen_diffuser"))
    print("blueprints.by_instance_id entries    :", len(instances))
    print("_pending_blueprint_count()           :", _pending_blueprint_count(state))
    print()
    for key, entry in instances.items():
        print(f"blueprint {key}:")
        for field in (
            "prefab_id",
            "cell",
            "in_progress",
            "progress_percent",
            "material_tags",
            "is_utility",
        ):
            if field in entry:
                print(f"   {field} = {json.dumps(entry[field], ensure_ascii=False)}")
        lineage = entry.get("material_lineage")
        if lineage:
            print(f"   material_lineage = {json.dumps(lineage, ensure_ascii=False)}")
    print()
    print("=== per-cell blueprint state ===")
    cells = (state.get("cells") or {}).get("by_id") or {}
    for key, entry in instances.items():
        cell_id = "cell_" + str(entry.get("cell"))
        cell = cells.get(cell_id)
        if isinstance(cell, dict):
            keep = {
                k: cell.get(k)
                for k in (
                    "element_id",
                    "mass_kg",
                    "blueprint_present",
                    "blueprint_in_progress",
                    "blueprint_progress_percent",
                    "empty_for_blueprint",
                )
                if k in cell
            }
            print(f"  {cell_id}: {json.dumps(keep, ensure_ascii=False)}")
    print()
    print("=== quality entries that could signal completion ===")
    quality = state.get("quality") or {}
    for path in sorted(quality):
        if "id_14588" in path:
            entry = quality[path]
            print(f"  {path.split('by_instance_id.')[-1]:<34} status={entry.get('status')} semantic={entry.get('semantic')}")
    print()
    print("=== colony snapshot ===")
    print("cycle:", state.get("cycle"), "| paused:", (state.get("game") or {}).get("paused"))
    print("game_time_cycles:", state.get("game_time_cycles"))
    print("duplicants:", (state.get("duplicants") or {}).get("total"), "| idle:", (state.get("duplicants") or {}).get("idle"))
    print("alerts:", state.get("alerts"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
