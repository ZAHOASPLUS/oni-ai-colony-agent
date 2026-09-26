"""Static guard: per-building telemetry is opt-in (code review 2026-09-26, L13).

Twelve readers enumerated every completed building in the world (POI walls
and props included) on every snapshot and every executor state capture:
about 78% of a cycle-6 snapshot (~2.3 MB compact), ~30 ms of main thread per
read, and a size that grows with the colony until the snapshot crosses the
evidence and node limits.  No decision code reads them.  ProbeStateReader
cannot run outside the game, so this pins the source shape; the compiled Mod
must still be checked in a clone run.
"""

from __future__ import annotations

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
READER = ROOT / "mod" / "Source" / "Probe" / "ProbeStateReader.cs"
KEYS = (
    "geometry", "ports", "occupancy", "health", "flooded", "reachability", "power_network",
    "utility_network", "logic_ports", "utility_endpoint", "utility_topology", "utility_contents",
)


class ProbeSnapshotScopeTests(unittest.TestCase):
    source = READER.read_text(encoding="utf-8")

    def read_state_body(self) -> str:
        start = self.source.index("internal static Dictionary<string, object> ReadState()")
        return self.source[start: self.source.index("\n        }\n", start)]

    def test_each_extended_reader_runs_only_when_enabled(self):
        body = self.read_state_body()
        for key in KEYS:
            self.assertEqual(len(re.findall(rf'Timed\(\s*"functional_building_{key}"', body)), 1, key)
            self.assertRegex(
                body,
                rf'=\s*extended\s*\?\s*Timed\("functional_building_{key}",\s*'
                rf'FunctionalBuilding\w+TelemetryReader\.Read,\s*metrics\)\s*:\s*null;',
            )

    def test_extended_payloads_are_not_published_unconditionally(self):
        body = self.read_state_body()
        for key in KEYS:
            self.assertNotIn(f'{{ "functional_building_{key}", ', body, key)
        self.assertIn("quality.Remove(ExtendedBuildingTelemetryKeys[index]);", body)
        self.assertIn('{ "functional_buildings", functionalBuildings }', body)

    def test_switch_is_read_at_session_start(self):
        self.assertIn('"ONI_AI_EXTENDED_BUILDING_TELEMETRY"', self.source)
        reset = self.source[self.source.index("internal static void ResetSession()"):]
        reset = reset[: reset.index("\n        }\n")]
        self.assertIn("extendedBuildingTelemetry = ReadExtendedBuildingTelemetrySwitch();", reset)
        declared = re.search(r"ExtendedBuildingTelemetryKeys =\s*\{(.*?)\};", self.source, re.S)
        self.assertEqual(re.findall(r'"functional_building_(\w+)"', declared.group(1)), list(KEYS))


if __name__ == "__main__":
    unittest.main()
