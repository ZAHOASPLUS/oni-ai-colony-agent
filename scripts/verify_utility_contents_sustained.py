#!/usr/bin/env python3
"""Offline verifier for repeated utility-content rate observations."""
from __future__ import annotations
import copy, datetime as dt, json, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from agent.utility_flow import sustained_contents_rates

BASE = dt.datetime(2026, 9, 13, tzinfo=dt.timezone.utc)


def snap(seconds: int, mass: float, world: int = 0):
    side = {"declared": True, "cell": 10, "content_kind": "gas", "element": "Oxygen",
            "mass": mass, "temperature": 293.15, "disease_count": 0, "solid_present": None}
    empty = {"declared": False, "cell": None, "content_kind": None, "element": None,
             "mass": None, "temperature": None, "disease_count": None, "solid_present": None}
    return {"world_id": world, "functional_building_utility_contents": {
        "schema": "oni.functional_building_utility_contents.v1",
        "captured_at": (BASE + dt.timedelta(seconds=seconds)).isoformat(),
        "world_id": world, "complete": True,
        "instances": [{"instance_id": 1, "world_id": world, "prefab_id": "Pump",
                       "input": side, "output": empty}]}}


def main():
    stable = sustained_contents_rates([snap(0, 1), snap(10, 3), snap(20, 5)])
    assert stable["status"] == "observed" and stable["streams"][0]["stable"] is True
    drift = sustained_contents_rates([snap(0, 1), snap(10, 3), snap(20, 7)])
    assert drift["status"] == "observed" and drift["streams"][0]["stable"] is False
    mismatch = sustained_contents_rates([snap(0, 1), snap(10, 3), snap(20, 5, world=9)])
    assert mismatch["status"] == "unknown" and mismatch["reason"] == "world_mismatch"
    short = sustained_contents_rates([snap(0, 1), snap(10, 3)])
    assert short["status"] == "unknown" and short["reason"] == "insufficient_snapshots"
    malformed = copy.deepcopy(snap(20, 5)); malformed["functional_building_utility_contents"]["instances"] = "bad"
    invalid = sustained_contents_rates([snap(0, 1), snap(10, 3), malformed])
    assert invalid["status"] == "unknown" and invalid["reason"] == "instances_invalid"
    tolerance = sustained_contents_rates([snap(0, 1), snap(10, 3), snap(20, 5)], relative_tolerance=float("nan"))
    assert tolerance["status"] == "unknown" and tolerance["reason"] == "tolerance_invalid"
    report = {"schema": "oni.utility-contents-sustained-verification.v1", "status": "passed",
              "case_count": 6, "live_admission": False,
              "results": ["stable", "drift_unstable", "world_mismatch", "insufficient_snapshots", "instances_invalid", "tolerance_invalid"],
              "source": str((ROOT / "agent/utility_flow.py").resolve())}
    (ROOT / "reports/utility-contents-sustained-verification.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
