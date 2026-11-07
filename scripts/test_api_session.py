#!/usr/bin/env python3
"""Sealing a run's checkpoints from its audit log."""

from __future__ import annotations

import json
import argparse
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import api_session  # noqa: E402


def write_audit(run_dir: pathlib.Path, *lines: dict) -> None:
    (run_dir / api_session.AUDIT_FILE).write_text(
        "".join(json.dumps(line) + "\n" for line in lines),
        encoding="utf-8",
    )


class SealTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = pathlib.Path(folder.name)
        self.clone = self.root / "oni-test.sav"
        self.manifest = self.root / "manifest.json"

    def seal(self) -> list:
        return api_session.seal_checkpoints(self.manifest, self.clone, self.root)

    def test_no_audit_means_nothing_to_seal(self) -> None:
        self.assertEqual(self.seal(), [])

    def test_refused_saves_and_other_tools_are_ignored(self) -> None:
        write_audit(
            self.root,
            {"tool": "get_status", "ok": True},
            {"tool": "save_checkpoint", "ok": False, "error": {"code": "save_failed"}},
        )
        self.assertEqual(self.seal(), [])

    def test_a_checkpoint_from_another_folder_is_refused(self) -> None:
        write_audit(self.root, {
            "tool": "save_checkpoint",
            "ok": True,
            "cycle": 2.0,
            "result": {"path": str(self.root / "elsewhere" / "oni-test-ckpt-001.sav")},
        })
        with self.assertRaises(api_session.CheckpointChainError):
            self.seal()

    def test_legacy_audit_keeps_old_entries_but_only_seals_current_session(self) -> None:
        (self.root / "session.json").write_text(json.dumps({
            "started_utc": "2026-11-05T18:41:39.821493+00:00",
        }), encoding="utf-8")
        write_audit(
            self.root,
            {"utc": "2026-11-01T08:11:29.0280545+00:00", "tool": "save_checkpoint", "ok": True,
             "cycle": 5.0, "result": {"path": str(self.root / "foreign" / "oni-other-ckpt-001.sav")}},
            {"utc": "2026-11-05T18:56:54.5645380+00:00", "tool": "save_checkpoint", "ok": True,
             "cycle": 199.101, "result": {"path": str(self.root / "oni-test-ckpt-002.sav")}},
        )
        before = (self.root / api_session.AUDIT_FILE).read_bytes()
        with mock.patch.object(api_session, "build_checkpoint_manifest", return_value={}) as build, \
                mock.patch.object(api_session, "publish_checkpoint_manifest"):
            self.assertEqual(self.seal(), ["oni-test-ckpt-002.sav"])
            self.assertEqual(build.call_args.kwargs["game_time_cycles"], 199.101)
        self.assertEqual((self.root / api_session.AUDIT_FILE).read_bytes(), before)

    def test_current_session_foreign_checkpoint_is_still_refused(self) -> None:
        (self.root / "session.json").write_text(json.dumps({
            "started_utc": "2026-11-05T18:41:39+00:00",
        }), encoding="utf-8")
        write_audit(self.root, {
            "utc": "2026-11-05T18:56:54+00:00", "tool": "save_checkpoint", "ok": True,
            "cycle": 5.0, "result": {"path": str(self.root / "foreign" / "oni-other-ckpt-001.sav")},
        })
        with self.assertRaises(api_session.CheckpointChainError):
            self.seal()

    def test_seals_in_order_and_skips_sealed_ones(self) -> None:
        for index in (2, 1, 3):
            write_audit_line = {
                "tool": "save_checkpoint",
                "ok": True,
                "cycle": float(index),
                "result": {"path": str(self.root / f"oni-test-ckpt-{index:03d}.sav")},
            }
            with (self.root / api_session.AUDIT_FILE).open("a", encoding="utf-8") as audit:
                audit.write(json.dumps(write_audit_line) + "\n")
        (self.root / "oni-test-ckpt-001.manifest.json").write_text("{}", encoding="utf-8")

        built = []
        with mock.patch.object(
            api_session,
            "build_checkpoint_manifest",
            side_effect=lambda **kwargs: built.append(kwargs["index"]) or {},
        ), mock.patch.object(api_session, "publish_checkpoint_manifest"):
            sealed = self.seal()

        self.assertEqual(built, [2, 3])
        self.assertEqual(sealed, ["oni-test-ckpt-002.sav", "oni-test-ckpt-003.sav"])


class AuditArchiveTests(unittest.TestCase):
    def test_moves_preexisting_audit_and_never_overwrites_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            bridge, run = root / "bridge", root / "run"
            bridge.mkdir()
            run.mkdir()
            source = bridge / api_session.AUDIT_FILE
            source.write_bytes(b"previous session\n")
            self.assertTrue(api_session.archive_existing_audit(bridge, run))
            self.assertFalse(source.exists())
            self.assertEqual((run / api_session.PREEXISTING_AUDIT_FILE).read_bytes(), b"previous session\n")
            self.assertFalse(api_session.archive_existing_audit(bridge, run))
            source.write_bytes(b"another session\n")
            with self.assertRaises(api_session.CloneLaunchGuardError):
                api_session.archive_existing_audit(bridge, run)
            self.assertEqual(source.read_bytes(), b"another session\n")


class BridgeDirTests(unittest.TestCase):
    def test_bridge_dir_follows_the_same_variable_as_the_mod(self) -> None:
        with mock.patch.dict(api_session.os.environ, {"ONI_AI_BRIDGE_DIR": r"D:\custom-bridge"}):
            self.assertEqual(api_session.bridge_dir(), pathlib.Path(r"D:\custom-bridge"))
        with mock.patch.dict(api_session.os.environ, {"ONI_AI_BRIDGE_DIR": ""}), \
                mock.patch.object(api_session, "oni_root", return_value=pathlib.Path("root")):
            self.assertEqual(api_session.bridge_dir(), pathlib.Path("root") / "ai-bridge")


class StopRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = pathlib.Path(folder.name)
        self.run = self.root / "run"
        self.bridge = self.root / "ai-bridge"
        self.run.mkdir()
        self.bridge.mkdir()
        (self.run / "session.json").write_text("{}", encoding="utf-8")
        self.args = argparse.Namespace(run_dir=str(self.run))

    def test_old_run_conflict_refuses_before_close_or_preferences_restore(self) -> None:
        for location in (self.run, self.bridge):
            (location / api_session.AUDIT_FILE).write_text("{}\n", encoding="utf-8")
        with mock.patch.object(api_session, "oni_root", return_value=self.root), \
                mock.patch.object(api_session, "game_process_ids", return_value=[123]), \
                mock.patch.object(api_session, "_close_game_windows") as close, \
                mock.patch.object(api_session.shutil, "copy2") as restore:
            with self.assertRaises(api_session.CloneLaunchGuardError):
                api_session.stop(self.args)
            close.assert_not_called()
            restore.assert_not_called()

    def test_retry_preserves_completed_report_and_checkpoint_count(self) -> None:
        report = {"checkpoints_sealed": ["oni-test-ckpt-002.sav"], "kplayerprefs_restored": True}
        end = self.run / "session-end.json"
        end.write_text(json.dumps(report), encoding="utf-8")
        original = end.read_bytes()
        with mock.patch.object(api_session, "oni_root", return_value=self.root), \
                mock.patch.object(api_session, "game_process_ids", return_value=[]), \
                mock.patch.object(api_session, "seal_checkpoints") as seal, \
                mock.patch.object(api_session.shutil, "copy2") as restore:
            self.assertEqual(api_session.stop(self.args), report)
            self.assertEqual(api_session.stop(self.args), report)
            seal.assert_not_called()
            restore.assert_not_called()
        self.assertEqual(end.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
