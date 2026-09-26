#!/usr/bin/env python3
"""Tests for the explicit, read-only Bridge coordinator observation seam."""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bridge.oni_bridge import write_coordinator_observation


class ReadOnlyCoordinatorObservationTests(unittest.TestCase):
    def test_writes_bounded_observation_without_touching_commands(self) -> None:
        with tempfile.TemporaryDirectory(prefix="oni-watch-observation-") as temp:
            root = pathlib.Path(temp)
            commands = root / "commands" / "pending"
            commands.mkdir(parents=True)
            marker = commands / "untouched.txt"
            marker.write_text("keep", encoding="utf-8")
            payload = write_coordinator_observation(
                root,
                lambda: {
                    "status": "candidate",
                    "live_admission": False,
                    "side_effects": {
                        "command_published": False,
                        "approval": False,
                        "execution": False,
                        "successor_advanced": False,
                    },
                },
            )
            self.assertEqual(payload["schema"], "oni.coordinator-observation.v1")
            self.assertFalse(payload["live_admission"])
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
            self.assertTrue((root / "coordinator-observation.json").is_file())

    def test_rejects_observer_advertising_side_effects_before_write(self) -> None:
        with tempfile.TemporaryDirectory(prefix="oni-watch-observation-") as temp:
            root = pathlib.Path(temp)
            with self.assertRaisesRegex(RuntimeError, "side effects"):
                write_coordinator_observation(
                    root,
                    lambda: {"side_effects": {"command_published": True}},
                )
            self.assertFalse((root / "coordinator-observation.json").exists())

    def test_rejects_observer_advertising_live_admission_before_write(self) -> None:
        with tempfile.TemporaryDirectory(prefix="oni-watch-observation-") as temp:
            root = pathlib.Path(temp)
            with self.assertRaisesRegex(RuntimeError, "live admission"):
                write_coordinator_observation(
                    root,
                    lambda: {"live_admission": True, "status": "candidate"},
                )
            self.assertFalse((root / "coordinator-observation.json").exists())

    def test_rejects_non_mapping_result(self) -> None:
        with tempfile.TemporaryDirectory(prefix="oni-watch-observation-") as temp:
            with self.assertRaisesRegex(ValueError, "mapping"):
                write_coordinator_observation(pathlib.Path(temp), lambda: None)  # type: ignore[return-value]


if __name__ == "__main__":
    unittest.main(verbosity=2)
