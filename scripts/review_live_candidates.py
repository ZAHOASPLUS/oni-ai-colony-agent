"""Replay the candidate generator over a recorded live session (read-only).

The controller sends the candidate-set notes only to the model; ``model-loop.json``
keeps the candidate ids of the latest set and no notes.  After a live round this
script regenerates the candidate set for the recorded snapshots so the checks of
roadmap B6, B15 and the Space Heater can be read off one summary:

- which temperature, unlock-research and supply-link notes appeared, and when;
- which candidate families were offered (relief, heater, generator, pump, links);
- whether the Probe published the new fields at all;
- how long the ``blueprints`` read took (``read_metrics_ms``);
- built and operational counts of the supply buildings at the end.

It only reads the archive and the policy.  Stuck-placement ages are not in the
archive, so the stuck-cancel candidates may differ from what the run offered;
everything else is the generator the controller ran.  Example:

    py -3 -B scripts/review_live_candidates.py <bridge>/sessions/<session>/state.jsonl --policy <run>/action-policy.json
"""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
import re
import statistics
import sys
from typing import Any, Dict, Iterator, List, Mapping, Optional

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.candidates import building_status, generate_candidates  # noqa: E402

FAMILIES = (
    ("priority_dig", re.compile(r"^priority_.+_Dig_\d+$")),
    ("research", re.compile(r"^research_")),
    ("space_heater", re.compile(r"^space_heater_")),
    ("manual_generator", re.compile(r"^manual_generator_")),
    ("liquid_pump", re.compile(r"^liquid_pump_")),
    ("electrolyzer", re.compile(r"^electrolyzer_")),
    ("power_link", re.compile(r"^power_link_")),
    ("liquid_link", re.compile(r"^liquid_link_")),
    ("cancel_blueprint_path", re.compile(r"^cancel_blueprint_path_")),
    ("dig", re.compile(r"^dig_")),
    ("resume_game", re.compile(r"^resume_game$")),
    ("pause_game", re.compile(r"^pause_game$")),
)
# Notes whose trailing count varies are grouped by this prefix.
NOTE_GROUPS = re.compile(r"^(temperature_alert:[a-z_]+):")
TRACKED_BUILDINGS = ("Electrolyzer", "ManualGenerator", "LiquidPump", "SpaceHeater")


def snapshots(archive: pathlib.Path) -> Iterator[Dict[str, Any]]:
    with archive.open("rb") as stream:
        for raw in stream:
            if b'"state_snapshot"' not in raw:
                continue
            record = json.loads(raw.decode("utf-8-sig"))
            if record.get("event") == "state_snapshot":
                yield record


def _family(candidate_id: str) -> Optional[str]:
    for name, pattern in FAMILIES:
        if pattern.search(candidate_id):
            return name
    return None


def probe_fields(state: Mapping[str, Any]) -> Dict[str, bool]:
    duplicants = (state.get("duplicants") or {}).get("by_instance_id") or {}
    dig = ((state.get("designations") or {}).get("dig") or {}).get("by_cell") or {}
    catalog = (state.get("blueprints") or {}).get("catalog") or {}
    heater = catalog.get("SpaceHeater") if isinstance(catalog, Mapping) else None
    return {
        "duplicant_temperature_state": any(
            isinstance(entry, Mapping) and "temperature_state" in entry for entry in duplicants.values()
        ),
        "dig_temperature_c": any(isinstance(entry, Mapping) and "temperature_c" in entry for entry in dig.values()),
        "supply_links": (state.get("blueprints") or {}).get("supply_links") is not None,
        "space_heater_catalog_unlock": isinstance(heater, Mapping) and "research_tech_id" in heater,
        "space_heater_count": "space_heater" in (state.get("buildings") or {}),
    }


def review(archive: pathlib.Path, allowed_actions: List[str], stride: int) -> Dict[str, Any]:
    total = 0
    sampled = 0
    notes: "collections.Counter[str]" = collections.Counter()
    families: "collections.Counter[str]" = collections.Counter()
    first_seen: Dict[str, Any] = {}
    fields: "collections.Counter[str]" = collections.Counter()
    blueprint_ms: List[float] = []
    first_operational: Optional[Dict[str, Any]] = None
    last: Optional[Dict[str, Any]] = None
    first_cycle = None

    for record in snapshots(archive):
        total += 1
        last = record
        if first_cycle is None:
            first_cycle = record.get("game_time_cycles")
        metrics = record.get("read_metrics_ms") or {}
        if isinstance(metrics.get("blueprints"), (int, float)):
            blueprint_ms.append(float(metrics["blueprints"]))
        if (total - 1) % stride:
            continue
        sampled += 1
        where = {"snapshot_seq": record.get("snapshot_seq"), "game_time_cycles": record.get("game_time_cycles")}
        for name, present in probe_fields(record).items():
            fields[name] += int(present)
        result = generate_candidates(record, allowed_actions=allowed_actions, autonomous=True)
        for note in result.get("notes") or []:
            match = NOTE_GROUPS.match(note)
            notes[note] += 1
            first_seen.setdefault(f"note:{match.group(1) if match else note}", where)
        for name in sorted({_family(item["candidate_id"]) for item in result.get("items") or []} - {None}):
            families[name] += 1
            first_seen.setdefault(f"candidate:{name}", where)
        if first_operational is None:
            electrolyzer = building_status(record).get("Electrolyzer") or {}
            if isinstance(electrolyzer.get("operational"), int) and electrolyzer["operational"] > 0:
                first_operational = where

    if last is None:
        raise SystemExit("the archive holds no state snapshot")
    status = building_status(last)
    return {
        "archive": str(archive),
        "snapshots": total,
        "sampled": sampled,
        "stride": stride,
        "game_time_cycles": {"first": first_cycle, "last": last.get("game_time_cycles")},
        "probe_fields_present": {name: f"{fields[name]}/{sampled}" for name in probe_fields(last)},
        "notes": dict(sorted(notes.items())),
        "candidate_families": {name: families.get(name, 0) for name, _ in FAMILIES},
        "first_seen": first_seen,
        "electrolyzer_first_operational": first_operational,
        "final_buildings": {name: status.get(name) for name in TRACKED_BUILDINGS},
        "final_alerts": last.get("alerts"),
        "read_metrics_ms_blueprints": None if not blueprint_ms else {
            "count": len(blueprint_ms),
            "median": round(statistics.median(blueprint_ms), 3),
            "max": round(max(blueprint_ms), 3),
        },
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("archive", type=pathlib.Path, help="sessions/<id>/state.jsonl of the live session")
    parser.add_argument("--policy", type=pathlib.Path, required=True, help="the run's action-policy.json")
    parser.add_argument("--stride", type=int, default=1, help="regenerate for every n-th snapshot (default 1)")
    args = parser.parse_args(argv)
    if args.stride < 1:
        parser.error("--stride must be at least 1")
    policy = json.loads(args.policy.read_text(encoding="utf-8-sig"))
    allowed = policy.get("allowed_actions")
    if not isinstance(allowed, list):
        parser.error("the policy has no allowed_actions list")
    print(json.dumps(review(args.archive, allowed, args.stride), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
