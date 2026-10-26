"""Checkpoint manifest chain for a named test clone (roadmap B16, decision D4)."""

from __future__ import annotations

import hashlib
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import checkpoint_chain as K  # noqa: E402
from scripts.test_clone_launch_guard import oni_save_bytes  # noqa: E402


def digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def path_id(path: pathlib.Path) -> str:
    return "sha256:" + hashlib.sha256(str(path.resolve()).lower().encode()).hexdigest()


class CheckpointChainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="oni-checkpoint-chain-")
        self.addCleanup(self.temp.cleanup)
        self.dir = pathlib.Path(self.temp.name) / "oni-m1-terra"
        self.dir.mkdir()
        self.save = self.dir / "oni-m1-terra.sav"
        self.save.write_bytes(oni_save_bytes(body=b"cycle-1"))
        self.manifest = pathlib.Path(self.temp.name) / "test-clone-manifest.json"
        self.manifest.write_text(json.dumps({
            "schema": "oni.named-test-clone.v1",
            "created": True,
            "source": {},
            "target": {"save_sha256": digest(self.save.read_bytes()), "save_path_id": path_id(self.save)},
        }), encoding="utf-8")

    def add(self, index: int, body: bytes, *, game_time: float = 7.0, auto: bool = False) -> dict:
        paths = K.checkpoint_paths(self.save, index)
        paths["save"].write_bytes(oni_save_bytes(body=body, is_auto_save=auto))
        manifest = K.build_checkpoint_manifest(
            clone_manifest=self.manifest, clone_save=self.save, index=index,
            run_id="m1-terra-r01", session_id="session-test", game_time_cycles=game_time,
        )
        K.publish_checkpoint_manifest(paths["manifest"], manifest)
        return manifest

    def latest(self) -> dict:
        return K.select_latest_checkpoint(self.manifest, self.save)

    def test_without_checkpoints_the_clone_itself_is_selected(self):
        latest = self.latest()
        self.assertEqual(latest["index"], 0)
        self.assertEqual(latest["save_path"], self.save.resolve())
        self.assertEqual(latest["chain"], [0])

    def test_the_newest_intact_link_is_selected(self):
        first = self.add(1, b"cycle-8", game_time=8.2)
        self.add(2, b"cycle-9", game_time=9.5)
        latest = self.latest()
        self.assertEqual(latest["index"], 2)
        self.assertEqual(latest["chain"], [0, 1, 2])
        self.assertEqual(latest["game_time_cycles"], 9.5)
        self.assertEqual(latest["save_path"].name, "oni-m1-terra-ckpt-002.sav")
        self.assertEqual(first["parent"], {"index": 0, "save_sha256": digest(self.save.read_bytes())})

    def test_a_checkpoint_must_extend_the_current_end(self):
        self.add(1, b"cycle-8")
        K.checkpoint_paths(self.save, 3)["save"].write_bytes(oni_save_bytes(body=b"x"))
        with self.assertRaisesRegex(K.CheckpointChainError, "does not extend|gap"):
            K.build_checkpoint_manifest(
                clone_manifest=self.manifest, clone_save=self.save, index=3,
                run_id="m1-terra-r01", session_id="session-test", game_time_cycles=9.0,
            )

    def test_changed_checkpoint_bytes_break_the_chain(self):
        self.add(1, b"cycle-8")
        K.checkpoint_paths(self.save, 1)["save"].write_bytes(oni_save_bytes(body=b"edited"))
        with self.assertRaisesRegex(K.CheckpointChainError, "does not verify"):
            self.latest()

    def test_a_gap_fails_closed(self):
        K.checkpoint_paths(self.save, 2)["save"].write_bytes(oni_save_bytes(body=b"x"))
        with self.assertRaisesRegex(K.CheckpointChainError, "gap"):
            self.latest()

    def test_a_save_without_its_manifest_fails_closed(self):
        self.add(1, b"cycle-8")
        K.checkpoint_paths(self.save, 2)["save"].write_bytes(oni_save_bytes(body=b"x"))
        with self.assertRaisesRegex(K.CheckpointChainError, "lacks"):
            self.latest()

    def test_an_autosave_checkpoint_is_refused(self):
        K.checkpoint_paths(self.save, 1)["save"].write_bytes(oni_save_bytes(body=b"x", is_auto_save=True))
        with self.assertRaisesRegex(K.CheckpointChainError, "autosave"):
            K.build_checkpoint_manifest(
                clone_manifest=self.manifest, clone_save=self.save, index=1,
                run_id="m1-terra-r01", session_id="session-test", game_time_cycles=7.0,
            )

    def test_a_wrong_parent_is_a_fork(self):
        manifest = self.add(1, b"cycle-8")
        path = K.checkpoint_paths(self.save, 1)["manifest"]
        manifest["parent"]["save_sha256"] = "sha256:" + "0" * 64
        path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(K.CheckpointChainError, "does not verify"):
            self.latest()

    def test_a_link_from_another_root_is_refused(self):
        manifest = self.add(1, b"cycle-8")
        manifest["root"]["clone_manifest_sha256"] = "sha256:" + "1" * 64
        K.checkpoint_paths(self.save, 1)["manifest"].write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(K.CheckpointChainError, "does not verify"):
            self.latest()

    def test_malformed_sections_fail_closed(self):
        manifest = self.add(1, b"cycle-8")
        manifest["save"] = ["not", "a", "mapping"]
        K.checkpoint_paths(self.save, 1)["manifest"].write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(K.CheckpointChainError):
            self.latest()

    def test_a_manifest_is_never_replaced(self):
        manifest = self.add(1, b"cycle-8")
        with self.assertRaisesRegex(K.CheckpointChainError, "already exists"):
            K.publish_checkpoint_manifest(K.checkpoint_paths(self.save, 1)["manifest"], manifest)

    def test_a_changed_clone_save_invalidates_the_root(self):
        self.save.write_bytes(oni_save_bytes(body=b"changed-root"))
        with self.assertRaisesRegex(K.CheckpointChainError, "does not match"):
            self.latest()

    def test_files_of_other_clones_are_ignored(self):
        (self.dir / "other-ckpt-001.sav").write_bytes(b"unrelated")
        self.assertEqual(self.latest()["index"], 0)

    def test_an_existing_link_cannot_be_described_again(self):
        self.add(1, b"cycle-8")
        with self.assertRaisesRegex(K.CheckpointChainError, "not a new save"):
            K.build_checkpoint_manifest(
                clone_manifest=self.manifest, clone_save=self.save, index=1,
                run_id="m1-terra-r01", session_id="session-test", game_time_cycles=8.0,
            )

    def test_a_manifest_needs_its_save_on_disk(self):
        with self.assertRaises(K.CheckpointChainError):
            K.build_checkpoint_manifest(
                clone_manifest=self.manifest, clone_save=self.save, index=1,
                run_id="m1-terra-r01", session_id="session-test", game_time_cycles=8.0,
            )

    def test_index_bounds(self):
        for bad in (0, 1000, True, "1"):
            with self.assertRaises(K.CheckpointChainError):
                K.checkpoint_paths(self.save, bad)


if __name__ == "__main__":
    unittest.main()
