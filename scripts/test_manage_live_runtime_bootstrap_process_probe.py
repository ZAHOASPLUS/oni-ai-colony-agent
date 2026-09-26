#!/usr/bin/env python3
"""Regression tests for the live runtime-bootstrap process census."""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import manage_live_runtime_bootstrap as core


class RuntimeBootstrapProcessProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.roots = core.Roots(
            "process-probe-regression",
            pathlib.Path("C:/offline-process-probe/oni"),
            pathlib.Path("C:/offline-process-probe/evidence"),
            None,
        )
        self.args = argparse.Namespace(process_state_fixture=None)

    @staticmethod
    def candidate(command_line: object) -> dict[str, object]:
        return {
            "ProcessId": 123,
            "Name": "python.exe",
            "CommandLine": command_line,
        }

    def run_census(self, candidates: list[dict[str, object]]) -> dict[str, int]:
        response = {
            "get_process_succeeded": True,
            "get_cim_succeeded": True,
            "OxygenNotIncluded": 0,
            "candidates": candidates,
        }
        completed = subprocess.CompletedProcess(
            ["powershell.exe"],
            0,
            stdout=json.dumps(response),
            stderr="",
        )
        with mock.patch.object(core.subprocess, "run", return_value=completed):
            return core.read_process_counts(self.args, self.roots)

    def assert_command_line_refused(self, command_line: object, reason: str) -> None:
        response = {
            "get_process_succeeded": True,
            "get_cim_succeeded": True,
            "OxygenNotIncluded": 0,
            "candidates": [self.candidate(command_line)],
        }
        completed = subprocess.CompletedProcess(
            ["powershell.exe"],
            0,
            stdout=json.dumps(response),
            stderr="",
        )
        with mock.patch.object(core.subprocess, "run", return_value=completed):
            with self.assertRaisesRegex(
                core.BootstrapError,
                "could not read a Python candidate command line",
            ) as context:
                core.read_process_counts(self.args, self.roots)
        self.assertIsNotNone(context.exception.diagnostic)
        diagnostic = context.exception.diagnostic
        assert diagnostic is not None
        self.assertEqual(
            diagnostic["schema"],
            "oni.live-runtime-bootstrap-process-census-refusal.v1",
        )
        self.assertEqual(diagnostic["status"], "blocked")
        self.assertEqual(diagnostic["action_outcome"], "unknown")
        self.assertEqual(diagnostic["reason"], reason)
        self.assertNotIn("CommandLine", diagnostic)

    def test_empty_command_line_is_refused(self) -> None:
        self.assert_command_line_refused("", "command_line_empty")

    def test_non_string_command_line_is_refused(self) -> None:
        for command_line in (None, 7, ["python.exe", "oni_bridge.py"]):
            with self.subTest(command_line=command_line):
                reason = (
                    "command_line_unreadable"
                    if command_line is None
                    else "command_line_invalid_type"
                )
                self.assert_command_line_refused(command_line, reason)

    def test_process_census_permission_error_is_refused_with_diagnostic(self) -> None:
        with mock.patch.object(
            core.subprocess,
            "run",
            side_effect=PermissionError("access denied"),
        ):
            with self.assertRaisesRegex(
                core.BootstrapError,
                "could not read Python candidate command lines",
            ) as context:
                core.read_process_counts(self.args, self.roots)
        diagnostic = context.exception.diagnostic
        self.assertIsNotNone(diagnostic)
        assert diagnostic is not None
        self.assertEqual(diagnostic["reason"], "process_census_read_failed")
        self.assertEqual(diagnostic["error_type"], "PermissionError")
        self.assertEqual(diagnostic["action_outcome"], "unknown")

    def test_valid_command_line_counts_only_exact_bridge_script(self) -> None:
        self.assertEqual(
            self.run_census(
                [
                    self.candidate('python.exe "C:\\runtime\\oni_bridge.py" --once'),
                    self.candidate("python.exe -c \"print('oni_bridge.py')\""),
                    self.candidate("python.exe -m fixture.worker oni_bridge.py"),
                    self.candidate("python.exe C:\\runtime\\oni_bridge.py.bak"),
                ]
            ),
            {"OxygenNotIncluded": 0, "oni_bridge.py": 1},
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
