#!/usr/bin/env python3
"""Fixture-only tests for the clone launch preflight guard."""

from __future__ import annotations

import hashlib
import json
import pathlib
import struct
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


def digest(path: pathlib.Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def path_id(path: pathlib.Path) -> str:
    return "sha256:" + hashlib.sha256(str(path.resolve()).lower().encode()).hexdigest()


def oni_save_bytes(*, is_auto_save: bool = False, base_name: str = "Fixture Base",
                   header: bytes | None = None, body: bytes = b"clone-save") -> bytes:
    """A save with ONI's header layout: buildVersion, headerSize, headerVersion, compression, GameInfo JSON."""
    if header is None:
        header = json.dumps({
            "numberOfCycles": 6, "numberOfDuplicants": 3, "baseName": base_name,
            "isAutoSave": is_auto_save, "originalSaveName": "Fixture Base.sav",
            "clusterId": "fixture", "colonyGuid": "00000000-0000-0000-0000-000000000001",
        }).encode("utf-8")
    return struct.pack("<IiIi", 744825, len(header), 1, 1) + header + body


class CloneLaunchGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="oni-clone-guard-")
        self.root = pathlib.Path(self.temp.name)
        save_root = self.root / "save_files"
        self.source_colony = save_root / "Fixture Colony"
        self.redirect_colony = save_root / "Fixture Base"
        self.clone_dir = save_root / "named-clone"
        for directory in (self.source_colony, self.clone_dir):
            directory.mkdir(parents=True)
        self.save = self.clone_dir / "named-clone.sav"
        self.preview = self.clone_dir / "named-clone.png"
        self.save.write_bytes(oni_save_bytes())
        self.preview.write_bytes(b"clone-preview")
        self.manifest = self.root / "manifest.json"
        source_save = self.source_colony / "source.sav"
        source_preview = self.source_colony / "source.png"
        source_save.write_bytes(oni_save_bytes())
        source_preview.write_bytes(b"clone-preview")
        self.write_manifest({
            "schema": "oni.named-test-clone.v1", "created": True,
            "source": {
                "save": {"path_id": path_id(source_save)}, "preview": {"path_id": path_id(source_preview)},
                "colony_directory_path_id": path_id(self.source_colony),
                "redirect_colony_directory_path_id": path_id(self.redirect_colony),
            },
            "target": {
                "save_sha256": digest(self.save), "preview_sha256": digest(self.preview),
                "save_path_id": path_id(self.save), "preview_path_id": path_id(self.preview),
            },
        })

    def write_manifest(self, payload: dict) -> None:
        self.manifest.write_text(json.dumps(payload), encoding="utf-8")

    def manifest_payload(self) -> dict:
        return json.loads(self.manifest.read_text(encoding="utf-8"))

    def move_clone_to(self, directory: pathlib.Path, content: bytes | None = None) -> None:
        """Place the clone pair at ``directory`` and point the manifest at it."""
        directory.mkdir(parents=True, exist_ok=True)
        save, preview = directory / self.save.name, directory / self.preview.name
        payload_bytes = self.save.read_bytes() if content is None else content
        preview_bytes = self.preview.read_bytes()
        save.write_bytes(payload_bytes)
        preview.write_bytes(preview_bytes)
        payload = self.manifest_payload()
        payload["target"].update({
            "save_sha256": digest(save), "save_path_id": path_id(save),
            "preview_path_id": path_id(preview),
        })
        self.write_manifest(payload)
        self.save, self.preview = save, preview

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

    def test_same_content_copy_elsewhere_is_rejected(self) -> None:
        # A fresh clone equals its source byte for byte; any other copy of the
        # original save must not pass as the clone (review 2026-09-26, M9).
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        copy_save, copy_preview = elsewhere / "named-clone.sav", elsewhere / "named-clone.png"
        copy_save.write_bytes(self.save.read_bytes())
        copy_preview.write_bytes(self.preview.read_bytes())
        with self.assertRaises(CloneLaunchGuardError):
            validate_clone_launch(
                self.manifest, copy_save, copy_preview, bridge_dir=self.root / "bridge",
                process_names=(), process_probe=lambda: {}, queue_counts={},
            )

    def test_clone_inside_source_colony_folder_is_rejected(self) -> None:
        # Autosave rotation in the source colony's auto_save folder deleted
        # the user's Cycle 6 autosave on 2026-09-13 (review P0-1).
        self.move_clone_to(self.source_colony)
        with self.assertRaisesRegex(CloneLaunchGuardError, "inside its source colony"):
            self.call()

    def test_clone_below_source_colony_folder_is_rejected(self) -> None:
        self.move_clone_to(self.source_colony / "nested" / "deeper")
        with self.assertRaisesRegex(CloneLaunchGuardError, "inside its source colony"):
            self.call()

    def test_clone_inside_autosave_redirect_folder_is_rejected(self) -> None:
        self.move_clone_to(self.redirect_colony)
        with self.assertRaisesRegex(CloneLaunchGuardError, "inside its source colony"):
            self.call()

    def test_colony_check_resolves_dotdot_spelling(self) -> None:
        self.move_clone_to(self.source_colony)
        dotted = self.clone_dir / ".." / self.source_colony.name / self.save.name
        with self.assertRaisesRegex(CloneLaunchGuardError, "inside its source colony"):
            validate_clone_launch(
                self.manifest, dotted, dotted.with_suffix(".png"), bridge_dir=self.root / "bridge",
                process_names=(), process_probe=lambda: {}, queue_counts={},
            )

    def test_manifest_without_source_colony_folders_is_rejected(self) -> None:
        original = self.manifest_payload()
        for name in ("colony_directory_path_id", "redirect_colony_directory_path_id"):
            with self.subTest(name):
                payload = json.loads(json.dumps(original))
                del payload["source"][name]
                self.write_manifest(payload)
                with self.assertRaisesRegex(CloneLaunchGuardError, "source colony folders"):
                    self.call()

    def test_autosave_header_is_rejected(self) -> None:
        # Loading an autosave points the active save back at
        # <save root>/<baseName>/<originalSaveName> (SaveLoader.cs Load).
        self.move_clone_to(self.clone_dir, oni_save_bytes(is_auto_save=True))
        with self.assertRaisesRegex(CloneLaunchGuardError, "autosave"):
            self.call()

    def test_malformed_or_ambiguous_header_is_rejected(self) -> None:
        cases = {
            "not_json": oni_save_bytes(header=b"not-json"),
            "truncated": struct.pack("<Ii", 744825, 4096),
            "oversized": struct.pack("<IiIi", 744825, 1 << 30, 1, 1) + b"{}",
            "missing_flag": oni_save_bytes(header=b'{"baseName":"Fixture Base"}'),
            "string_flag": oni_save_bytes(header=b'{"baseName":"B","isAutoSave":"false"}'),
            "case_variant": oni_save_bytes(header=b'{"baseName":"B","isAutoSave":false,"IsAutoSave":true}'),
            "duplicate": oni_save_bytes(header=b'{"baseName":"B","isAutoSave":false,"isAutoSave":true}'),
        }
        for label, content in cases.items():
            with self.subTest(label):
                self.move_clone_to(self.clone_dir, content)
                with self.assertRaises(CloneLaunchGuardError):
                    self.call()

    def test_header_without_compression_field_parses(self) -> None:
        header = b'{"baseName":"B","isAutoSave":false}'
        self.move_clone_to(self.clone_dir, struct.pack("<IiI", 744825, len(header), 0) + header + b"body")
        self.assertTrue(self.call()["launch_allowed"])

    def test_manifest_without_target_paths_is_rejected(self) -> None:
        payload = json.loads(self.manifest.read_text(encoding="utf-8"))
        del payload["target"]["save_path_id"], payload["target"]["preview_path_id"]
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
            side_effect=OSError("changed during read"),
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
