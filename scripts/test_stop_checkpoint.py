"""Stop-time checkpoint (roadmap B16): the state machine against a fake Mod.

The Mod's side is simulated by writing the checkpoint save and the control's
terminal record exactly where ValidationCheckpointController puts them
(validation-controls/done|failed|quarantine/<control id>.json).  Self-contained
temporary directories; nothing under the real Klei folder is touched.
"""

from __future__ import annotations

import datetime as dt
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
from scripts.stop_checkpoint import FAILED, PENDING, VERIFIED, StopCheckpoint  # noqa: E402
from scripts.test_clone_launch_guard import oni_save_bytes  # noqa: E402

MOD_VERSION = "0.7.0"


def digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def path_id(path: pathlib.Path) -> str:
    return "sha256:" + hashlib.sha256(str(path.resolve()).lower().encode()).hexdigest()


def stamp(offset_seconds: float = 0.0) -> str:
    moment = dt.datetime.now(dt.timezone.utc).astimezone() + dt.timedelta(seconds=offset_seconds)
    return moment.isoformat(timespec="milliseconds")


class Clock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value


class StopCheckpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="oni-stop-checkpoint-")
        self.addCleanup(self.temp.cleanup)
        base = pathlib.Path(self.temp.name)
        self.clone_dir = base / "saves" / "oni-m1-terra"
        self.clone_dir.mkdir(parents=True)
        self.save = self.clone_dir / "oni-m1-terra.sav"
        self.save.write_bytes(oni_save_bytes(body=b"cycle-1"))
        self.manifest = base / "test-clone-manifest.json"
        self.manifest.write_text(json.dumps({
            "schema": "oni.named-test-clone.v1", "created": True, "source": {},
            "target": {"save_sha256": digest(self.save.read_bytes()), "save_path_id": path_id(self.save)},
        }), encoding="utf-8")
        self.bridge = base / "ai-bridge"
        (self.bridge / "commands").mkdir(parents=True)
        self.write_policy({
            "schema": "oni.action-policy.v1", "actions_enabled": False, "suggestions_enabled": False,
            "allowed_actions": ["set_paused"], "allowed_save_hashes": [],
            "command_ttl_seconds": 300, "max_pending_commands": 1,
        })
        self.clock = Clock()

    def write_policy(self, policy: dict) -> None:
        (self.bridge / "action-policy.json").write_text(json.dumps(policy), encoding="utf-8")

    def machine(self) -> StopCheckpoint:
        return StopCheckpoint(
            bridge_dir=self.bridge, clone_manifest=self.manifest, clone_save=self.save,
            run_id="m1-terra-r01", expected_mod_version=MOD_VERSION, timeout_seconds=60, clock=self.clock,
        )

    def latest(self, save_hash: str, *, ts: str | None = None, dirty: bool = False, game_time: float = 8.5) -> dict:
        return {
            "event": "state_snapshot", "schema": "oni.state.v2", "session_id": "session-a", "seq": 40,
            "snapshot_id": "snap-40", "ts": ts or stamp(), "mod_version": MOD_VERSION,
            "save_id_hash": save_hash, "save_dirty": dirty, "game_time_cycles": game_time, "bridge": {"stale": False},
        }

    def control(self) -> dict:
        approved = list((self.bridge / "validation-controls" / "approved").glob("*.json"))
        self.assertEqual(len(approved), 1)
        return json.loads(approved[0].read_text(encoding="utf-8"))

    def mod_answers(self, control: dict, *, status: str = "verified", body: bytes = b"cycle-9") -> str:
        target = pathlib.Path(control["args"]["target_save_path"])
        target.write_bytes(oni_save_bytes(body=body))
        written = digest(target.read_bytes())
        stage = {"verified": "done", "failed": "failed", "rejected": "quarantine"}[status]
        record = dict(control, status=status, completed_at=stamp(), result={
            "active_save_id_hash": written, "message": "fixture " + status,
        })
        folder = self.bridge / "validation-controls" / stage
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{control['id']}.json").write_text(json.dumps(record), encoding="utf-8")
        return written

    def validation_policy(self) -> dict:
        return json.loads((self.bridge / "validation-control-policy.json").read_text(encoding="utf-8"))

    def test_a_verified_save_becomes_the_next_link(self):
        machine = self.machine()
        root_hash = digest(self.save.read_bytes())
        self.assertEqual(machine.step(self.latest(root_hash)), PENDING)
        policy = self.validation_policy()
        self.assertTrue(policy["controls_enabled"])
        self.assertEqual(policy["allowed_operations"], ["save_checkpoint"])
        control = self.control()
        self.assertTrue(control["args"]["target_save_path"].endswith("oni-m1-terra-ckpt-001.sav"))
        self.assertEqual(control["source"], {"kind": "autonomous_controller_stop"})
        written = self.mod_answers(control)
        self.assertEqual(machine.step(self.latest(root_hash)), PENDING)
        self.assertEqual(machine.record["phase"], "awaiting_state")
        # A snapshot from before the save completed does not confirm it.
        self.assertEqual(machine.step(self.latest(written, ts=stamp(-600))), PENDING)
        self.assertEqual(machine.step(self.latest(written, ts=stamp(5), game_time=9.25)), VERIFIED)
        latest = K.select_latest_checkpoint(self.manifest, self.save)
        self.assertEqual(latest["index"], 1)
        self.assertEqual(latest["save_sha256"], written)
        self.assertEqual(latest["game_time_cycles"], 9.25)
        self.assertFalse(self.validation_policy()["controls_enabled"])
        self.assertEqual(machine.step(self.latest(written)), VERIFIED)

    def test_the_next_stop_extends_the_chain(self):
        first = self.machine()
        root_hash = digest(self.save.read_bytes())
        first.step(self.latest(root_hash))
        written = self.mod_answers(self.control(), body=b"cycle-9")
        first.step(self.latest(root_hash))
        self.assertEqual(first.step(self.latest(written, ts=stamp(5))), VERIFIED)
        for path in (self.bridge / "validation-controls" / "approved").glob("*.json"):
            path.unlink()
        second = self.machine()
        self.assertEqual(second.step(self.latest(written)), PENDING)
        control = self.control()
        self.assertTrue(control["args"]["target_save_path"].endswith("oni-m1-terra-ckpt-002.sav"))
        newer = self.mod_answers(control, body=b"cycle-14")
        second.step(self.latest(written))
        self.assertEqual(second.step(self.latest(newer, ts=stamp(5))), VERIFIED)
        self.assertEqual(K.select_latest_checkpoint(self.manifest, self.save)["chain"], [0, 1, 2])

    def test_a_rejected_control_fails_without_a_link(self):
        machine = self.machine()
        root_hash = digest(self.save.read_bytes())
        machine.step(self.latest(root_hash))
        self.mod_answers(self.control(), status="rejected")
        self.assertEqual(machine.step(self.latest(root_hash)), FAILED)
        self.assertIn("rejected", machine.record["error"])
        self.assertFalse(self.validation_policy()["controls_enabled"])
        self.assertFalse(K.checkpoint_paths(self.save, 1)["manifest"].exists())

    def test_no_answer_times_out(self):
        machine = self.machine()
        machine.step(self.latest(digest(self.save.read_bytes())))
        self.clock.value += 61
        self.assertEqual(machine.step(None), FAILED)
        self.assertIn("timed out", machine.record["error"])
        self.assertFalse(self.validation_policy()["controls_enabled"])

    def test_an_unsealed_policy_is_refused_before_anything_is_written(self):
        self.write_policy({"schema": "oni.action-policy.v2", "actions_enabled": True,
                           "suggestions_enabled": True, "allowed_save_hashes": ["sha256:" + "a" * 64]})
        machine = self.machine()
        self.assertEqual(machine.step(self.latest(digest(self.save.read_bytes()))), FAILED)
        self.assertFalse((self.bridge / "validation-control-policy.json").exists())
        self.assertFalse((self.bridge / "validation-controls").exists())

    def test_a_dirty_or_foreign_save_is_refused(self):
        root_hash = digest(self.save.read_bytes())
        self.assertEqual(self.machine().step(self.latest(root_hash, dirty=True)), FAILED)
        self.assertEqual(self.machine().step(self.latest("sha256:" + "b" * 64)), FAILED)

    def test_an_existing_target_is_never_overwritten(self):
        K.checkpoint_paths(self.save, 1)["save"].write_bytes(oni_save_bytes(body=b"stray"))
        self.assertEqual(self.machine().step(self.latest(digest(self.save.read_bytes()))), FAILED)


class LoaderAnchorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="oni-loader-anchor-")
        self.addCleanup(self.temp.cleanup)
        base = pathlib.Path(self.temp.name)
        self.save = base / "oni-m1-terra" / "oni-m1-terra.sav"
        self.save.parent.mkdir()
        self.save.write_bytes(oni_save_bytes(body=b"cycle-1"))
        self.manifest = base / "manifest.json"
        self.manifest.write_text(json.dumps({
            "schema": "oni.named-test-clone.v1", "created": True, "source": {},
            "target": {"save_sha256": digest(self.save.read_bytes()), "save_path_id": path_id(self.save)},
        }), encoding="utf-8")

    def anchors_for(self, path: pathlib.Path) -> dict:
        return {"loader_save_sha256": digest(path.read_bytes()),
                "loader_path_id_upper_value": K.loader_path_id_upper(path)}

    def test_only_the_newest_link_is_admitted(self):
        self.assertEqual(K.match_loader_anchors(self.manifest, self.save, **self.anchors_for(self.save))["index"], 0)
        paths = K.checkpoint_paths(self.save, 1)
        paths["save"].write_bytes(oni_save_bytes(body=b"cycle-9"))
        K.publish_checkpoint_manifest(paths["manifest"], K.build_checkpoint_manifest(
            clone_manifest=self.manifest, clone_save=self.save, index=1,
            run_id="m1-terra-r01", session_id="session-a", game_time_cycles=9.0,
        ))
        target = K.match_loader_anchors(self.manifest, self.save, **self.anchors_for(paths["save"]))
        self.assertEqual(target["index"], 1)
        with self.assertRaisesRegex(K.CheckpointChainError, "newest link"):
            K.match_loader_anchors(self.manifest, self.save, **self.anchors_for(self.save))

    def test_the_path_id_matches_the_runtime_contract(self):
        text = str(self.save.resolve()).rstrip("\\/").upper()
        self.assertEqual(K.loader_path_id_upper(self.save), digest(text.encode("utf-8")))


class LoadConfigTests(unittest.TestCase):
    """``oni_autonomous.load_config`` admits the run's save by the checkpoints flag."""

    def setUp(self) -> None:
        from scripts import oni_autonomous

        self.load_config = oni_autonomous.load_config
        self.temp = tempfile.TemporaryDirectory(prefix="oni-load-config-")
        self.addCleanup(self.temp.cleanup)
        base = pathlib.Path(self.temp.name)
        saves = base / "save_files"
        source = saves / "Source Colony"
        source.mkdir(parents=True)
        (source / "source.sav").write_bytes(oni_save_bytes())
        (source / "source.png").write_bytes(b"preview")
        self.save = saves / "oni-m1-terra" / "oni-m1-terra.sav"
        self.save.parent.mkdir()
        self.save.write_bytes(oni_save_bytes(body=b"cycle-1"))
        self.preview = self.save.with_suffix(".png")
        self.preview.write_bytes(b"preview")
        self.manifest = base / "test-clone-manifest.json"
        self.manifest.write_text(json.dumps({
            "schema": "oni.named-test-clone.v1", "created": True, "run_id": "m1-terra-r01",
            "source": {
                "save": {"path_id": path_id(source / "source.sav")},
                "preview": {"path_id": path_id(source / "source.png")},
                "colony_directory_path_id": path_id(source),
                "redirect_colony_directory_path_id": path_id(saves / "Fixture Base"),
            },
            "target": {
                "save_sha256": digest(self.save.read_bytes()), "preview_sha256": digest(self.preview.read_bytes()),
                "save_path_id": path_id(self.save), "preview_path_id": path_id(self.preview),
            },
        }), encoding="utf-8")
        self.bridge = base / "ai-bridge"
        self.bridge.mkdir()
        self.config_path = base / "config.json"

    def write_config(self, loaded: pathlib.Path, **extra) -> pathlib.Path:
        sha = "sha256:" + "c" * 64
        config = {
            "schema": "oni.autonomous-controller-config.v1", "bridge_dir": str(self.bridge),
            "clone_manifest": str(self.manifest), "clone_manifest_sha256": digest(self.manifest.read_bytes()),
            "clone_save": str(self.save), "clone_preview": str(self.preview), "run_id": "m1-terra-r01",
            "controller_id": "m1-controller", "session_id": "session-a", "world_id": 0,
            "startup_expectations": {
                "validation_run_id": "m1-terra-r01", "validation_process_id": 4242,
                "startup_binding_hash": sha, "startup_intent_hash": sha, "runtime_config_evidence_sha256": sha,
                "loader_path_id_upper": K.loader_path_id_upper(loaded),
                "loader_save_sha256": digest(loaded.read_bytes()),
                "game_build": "cl-744825", "mod_version": MOD_VERSION,
            },
        }
        config.update(extra)
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        return self.config_path

    def add_checkpoint(self) -> pathlib.Path:
        paths = K.checkpoint_paths(self.save, 1)
        paths["save"].write_bytes(oni_save_bytes(body=b"cycle-9"))
        K.publish_checkpoint_manifest(paths["manifest"], K.build_checkpoint_manifest(
            clone_manifest=self.manifest, clone_save=self.save, index=1,
            run_id="m1-terra-r01", session_id="session-a", game_time_cycles=9.0,
        ))
        return paths["save"]

    def test_without_checkpoints_only_the_clone_is_admitted(self):
        config = self.load_config(self.write_config(self.save))
        self.assertFalse(config["checkpoints"])
        self.assertEqual(config["checkpoint_index"], 0)
        self.assertEqual(config["save_id_hash"], digest(self.save.read_bytes()))
        checkpoint = self.add_checkpoint()
        with self.assertRaisesRegex(ValueError, "loader anchors"):
            self.load_config(self.write_config(checkpoint))

    def test_with_checkpoints_the_newest_link_is_admitted(self):
        self.assertEqual(self.load_config(self.write_config(self.save, checkpoints=True))["checkpoint_index"], 0)
        checkpoint = self.add_checkpoint()
        config = self.load_config(self.write_config(checkpoint, checkpoints=True))
        self.assertEqual(config["checkpoint_index"], 1)
        self.assertEqual(config["save_id_hash"], digest(checkpoint.read_bytes()))
        self.assertEqual(pathlib.Path(config["active_save"]).name, "oni-m1-terra-ckpt-001.sav")
        # The clone itself is now an older link and would drop progress.
        with self.assertRaisesRegex(ValueError, "newest link"):
            self.load_config(self.write_config(self.save, checkpoints=True))

    def test_the_flag_must_be_a_boolean(self):
        with self.assertRaisesRegex(ValueError, "boolean"):
            self.load_config(self.write_config(self.save, checkpoints="yes"))


if __name__ == "__main__":
    unittest.main()
