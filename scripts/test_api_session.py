#!/usr/bin/env python3
"""Sealing a run's checkpoints from its audit log."""

from __future__ import annotations

import json
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


if __name__ == "__main__":
    unittest.main()
