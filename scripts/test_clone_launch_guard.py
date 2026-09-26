#!/usr/bin/env python3
"""Fixture-only tests for the clone launch preflight guard."""

from __future__ import annotations

import hashlib
import json
import pathlib
import sys
import tempfile
import unittest
import subprocess
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.clone_launch_guard import (
    CloneLaunchGuardError,
    build_clone_probe,
    build_process_probe,
    build_queue_probe,
    validate_clone_launch,
)
from agent.command_custody import CommandCustodyConflict


def digest(path: pathlib.Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def path_id(path: pathlib.Path) -> str:
    return "sha256:" + hashlib.sha256(str(path.resolve()).lower().encode()).hexdigest()


class CloneLaunchGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="oni-clone-guard-")
        self.root = pathlib.Path(self.temp.name)
        self.save = self.root / "named-clone.sav"
        self.preview = self.root / "named-clone.png"
        self.save.write_bytes(b"clone-save")
        self.preview.write_bytes(b"clone-preview")
        self.manifest = self.root / "manifest.json"
        source_save = self.root / "source.sav"
        source_preview = self.root / "source.png"
        source_save.write_bytes(b"source-save")
        source_preview.write_bytes(b"source-preview")
        payload = {
            "schema": "oni.named-test-clone.v1", "created": True,
            "source": {"save": {"path_id": path_id(source_save)}, "preview": {"path_id": path_id(source_preview)}},
            "target": {"save_sha256": digest(self.save), "preview_sha256": digest(self.preview)},
        }
        self.manifest.write_text(json.dumps(payload), encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def call(self, **kwargs):
        options = {
            "process_names": ("OxygenNotIncluded", "oni_bridge.py"),
            "process_probe": lambda: {},
            "queue_counts": {},
        }
        options.update(kwargs)
        return validate_clone_launch(
            self.manifest, self.save, self.preview, bridge_dir=self.root / "bridge",
            **options,
        )

    def test_valid_pair_and_empty_boundary_are_launch_eligible(self) -> None:
        result = self.call()
        self.assertTrue(result["launch_allowed"])
        self.assertEqual(result["clone_save_sha256"], digest(self.save))

    def test_wrong_clone_content_is_rejected(self) -> None:
        self.save.write_bytes(b"changed")
        with self.assertRaises(CloneLaunchGuardError):
            self.call()

    def test_running_process_or_nonempty_queue_is_rejected(self) -> None:
        with self.assertRaises(CloneLaunchGuardError):
            self.call(process_probe=lambda: {"OxygenNotIncluded": 1})
        with self.assertRaises(CloneLaunchGuardError):
            self.call(queue_counts={"commands/pending": 1})

    def test_source_path_collision_is_rejected_even_when_content_matches(self) -> None:
        payload = json.loads(self.manifest.read_text(encoding="utf-8"))
        payload["source"]["save"]["path_id"] = path_id(self.save)
        self.manifest.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(CloneLaunchGuardError):
            self.call()

    def test_probe_rereads_manifest_and_clone_content_each_call(self) -> None:
        process = {"OxygenNotIncluded": 0, "oni_bridge.py": 0}
        queues = {"commands/pending": 0}
        probe = build_clone_probe(
            self.manifest, self.save, self.preview,
            bridge_dir=self.root / "bridge",
            process_names=tuple(process),
            process_probe=lambda: dict(process),
            queue_probe=lambda: dict(queues),
        )
        first = probe()
        self.assertTrue(first["launch_allowed"])
        self.preview.write_bytes(b"changed-preview")
        with self.assertRaises(CloneLaunchGuardError):
            probe()

    def test_probe_rejects_late_process_or_queue_drift(self) -> None:
        process = {"OxygenNotIncluded": 0, "oni_bridge.py": 0}
        queues = {"commands/pending": 0}
        probe = build_clone_probe(
            self.manifest, self.save, self.preview,
            bridge_dir=self.root / "bridge",
            process_names=tuple(process),
            process_probe=lambda: dict(process),
            queue_probe=lambda: dict(queues),
        )
        probe()
        process["OxygenNotIncluded"] = 1
        with self.assertRaises(CloneLaunchGuardError):
            probe()
        process["OxygenNotIncluded"] = 0
        queues["commands/pending"] = 1
        with self.assertRaises(CloneLaunchGuardError):
            probe()

    def test_stable_read_failure_is_fail_closed(self) -> None:
        with mock.patch(
            "agent.clone_launch_guard.read_stable_bytes",
            side_effect=CommandCustodyConflict("changed during read"),
        ):
            with self.assertRaisesRegex(CloneLaunchGuardError, "unreadable"):
                self.call()

    def test_process_probe_counts_only_protected_names(self) -> None:
        probe = build_process_probe(
            ("OxygenNotIncluded", "oni_bridge"),
            lister=lambda: ["OxygenNotIncluded", "other", "oni_bridge", "oni_bridge"],
        )
        self.assertEqual(probe(), {"OxygenNotIncluded": 1, "oni_bridge": 2})

    def test_process_probe_identifies_bridge_from_exact_python_script_argv(self) -> None:
        records = [
            {"Name": "python.exe", "CommandLine": ["python.exe", "-u", "C:\\runtime\\oni_bridge.py", "--once"]},
            {"Name": "pythonw.exe", "CommandLine": "pythonw.exe C:/runtime/ONI_BRIDGE.PY --quiet"},
            {"Name": "py.exe", "CommandLine": "py.exe -m fixture.worker oni_bridge.py"},
            {"Name": "python.exe", "CommandLine": 'python.exe -c "print(\'oni_bridge.py\')"'},
            {"Name": "python.exe", "CommandLine": "python.exe C:\\tools\\worker.py --label oni_bridge.py"},
            {"Name": "python.exe", "CommandLine": "python.exe C:\\tools\\oni_bridge.py.bak"},
        ]
        probe = build_process_probe(("OxygenNotIncluded", "oni_bridge", "steam"), lister=lambda: records)
        self.assertEqual(probe(), {"OxygenNotIncluded": 0, "oni_bridge": 2, "steam": 0})

    def test_process_probe_fails_closed_when_bridge_command_line_is_denied(self) -> None:
        probe = build_process_probe(
            ("OxygenNotIncluded", "oni_bridge"),
            lister=lambda: [{"Name": "python.exe", "CommandLine": None}],
        )
        with self.assertRaisesRegex(CloneLaunchGuardError, "command line"):
            probe()

    def test_process_probe_fails_closed_when_process_table_permission_fails(self) -> None:
        probe = build_process_probe(
            ("OxygenNotIncluded", "oni_bridge"),
            lister=lambda: (_ for _ in ()).throw(PermissionError("access denied")),
        )
        with self.assertRaisesRegex(CloneLaunchGuardError, "unreadable"):
            probe()

    def test_windows_process_probe_reads_native_command_lines(self) -> None:
        if sys.platform != "win32":
            self.skipTest("Windows fallback only")
        records = json.dumps([
            {"Name": "python.exe", "ExecutablePath": "C:\\Python\\python.exe",
             "CommandLine": "python.exe C:\\runtime\\oni_bridge.py --once"},
        ])
        completed = subprocess.CompletedProcess(["powershell"], 0, stdout=records, stderr="")
        with mock.patch("agent.clone_launch_guard.shutil.which", side_effect=lambda name: name), \
             mock.patch("agent.clone_launch_guard.subprocess.run", return_value=completed):
            self.assertEqual(
                build_process_probe(("OxygenNotIncluded", "oni_bridge", "steam"))(),
                {"OxygenNotIncluded": 0, "oni_bridge": 1, "steam": 0},
            )

    def test_queue_probe_is_read_only_and_requires_real_directories(self) -> None:
        queue = self.root / "commands" / "pending"
        queue.mkdir(parents=True)
        (queue / "unexpected.txt").write_text("x", encoding="utf-8")
        probe = build_queue_probe(self.root, ("commands/pending",))
        self.assertEqual(probe(), {"commands/pending": 1})
        self.assertFalse((self.root / "commands" / "approved").exists())
        with self.assertRaises(CloneLaunchGuardError):
            build_queue_probe(self.root, ("commands/approved",))()


if __name__ == "__main__":
    unittest.main(verbosity=2)
