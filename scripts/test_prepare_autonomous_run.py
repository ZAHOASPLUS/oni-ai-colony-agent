"""Offline tests for the generic autonomous-run preparation (scripts/prepare_autonomous_run.py)."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.checkpoint_chain import build_checkpoint_manifest, checkpoint_paths, publish_checkpoint_manifest  # noqa: E402
from scripts import prepare_autonomous_run as prepare  # noqa: E402
from scripts.test_clone_launch_guard import oni_save_bytes  # noqa: E402

RUN_ID = "m1-terra-r01"


def digest(path: pathlib.Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def path_id(path: pathlib.Path) -> str:
    return "sha256:" + hashlib.sha256(str(path.resolve()).lower().encode()).hexdigest()


class PrepareAutonomousRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="oni-prepare-run-")
        self.root = pathlib.Path(self.temp.name).resolve()
        saves = self.root / "save_files"
        source = saves / "Terra Source"
        self.clone_dir = saves / "oni-m1-terra"
        for directory in (source, self.clone_dir, self.root / "bridge", self.root / "mod"):
            directory.mkdir(parents=True)
        self.save = self.clone_dir / "oni-m1-terra.sav"
        self.preview = self.clone_dir / "oni-m1-terra.png"
        self.save.write_bytes(oni_save_bytes(base_name="Terra Source"))
        self.preview.write_bytes(b"preview")
        (source / "m1-terra-src.sav").write_bytes(self.save.read_bytes())
        (source / "m1-terra-src.png").write_bytes(b"preview")
        self.manifest = self.root / "clones" / RUN_ID / "test-clone-manifest.json"
        self.manifest.parent.mkdir(parents=True)
        self.manifest.write_text(json.dumps({
            "schema": "oni.named-test-clone.v1", "run_id": RUN_ID, "created": True,
            "source": {
                "save": {"path_id": path_id(source / "m1-terra-src.sav")},
                "preview": {"path_id": path_id(source / "m1-terra-src.png")},
                "colony_directory_path_id": path_id(source),
                "redirect_colony_directory_path_id": path_id(saves / "Terra Source"),
            },
            "target": {
                "save_sha256": digest(self.save), "preview_sha256": digest(self.preview),
                "save_path_id": path_id(self.save), "preview_path_id": path_id(self.preview),
            },
        }), encoding="utf-8")
        dll = self.root / "mod" / "OniAiProbeMod.dll"
        dll.write_bytes(b"probe-dll")
        self.process = None
        self.env = prepare.Environment(
            bridge_dir=self.root / "bridge", mod_dll=dll, runs_root=self.root / "runs",
            game_process=lambda: self.process,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def bind(self, **kwargs):
        options = {"clone_manifest": self.manifest, "clone_save": self.save}
        options.update(kwargs)
        return prepare.prepare_binding(self.env, **options)

    def add_checkpoint(self, index: int = 1) -> pathlib.Path:
        paths = checkpoint_paths(self.save, index)
        paths["save"].write_bytes(oni_save_bytes(base_name="Terra Source", body=b"checkpoint-%d" % index))
        manifest = build_checkpoint_manifest(
            clone_manifest=self.manifest, clone_save=self.save, index=index,
            run_id=RUN_ID, session_id="session-ckpt", game_time_cycles=12.5,
        )
        publish_checkpoint_manifest(paths["manifest"], manifest)
        return paths["save"]

    def write_session(self, prepared: dict, pid: int = 4242, **overrides) -> None:
        record = {
            "schema": "oni.event.v2", "event": "session_started", "session_id": "session-1",
            "validation_process_id": pid, "validation_run_id": prepared["run_id"],
            "startup_binding_hash": prepared["binding"]["sha256"],
            "startup_intent_hash": prepared["binding"]["startup_intent_hash"],
            "runtime_config_evidence_sha256": prepared["binding"]["runtime_config_evidence_sha256"],
            "game_build": prepare.GAME_BUILD, "mod_version": prepare.MOD_VERSION,
            "startup_binding_status": "verified", "reason": prepare.TRUSTED_START_REASON,
            "active_save_path_id": prepared["loader"]["path_id_upper"],
            "save_id_hash": prepared["loader"]["save_sha256"],
        }
        record.update(overrides)
        with (self.root / "bridge" / "state.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"event": "state_snapshot", "seq": 9}) + "\n")
            handle.write(json.dumps(record) + "\n")

    # -- binding -------------------------------------------------------------

    def test_binding_writes_the_exact_document_and_prepare_record(self) -> None:
        record = self.bind()
        binding_path = self.root / "bridge" / "runtime-startup-binding.json"
        binding = json.loads(binding_path.read_text(encoding="utf-8"))
        self.assertEqual(set(binding), prepare.BINDING_FIELDS)
        self.assertEqual(binding["validation_run_id"], RUN_ID)
        self.assertEqual(binding["requested_loader_save_sha256"], digest(self.save))
        self.assertEqual(binding["requested_loader_save_length"], self.save.stat().st_size)
        self.assertEqual(record["binding"]["sha256"], digest(binding_path))
        self.assertEqual(record["loader"]["index"], 0)
        self.assertFalse(record["checkpoints"])
        stored = json.loads((self.root / "runs" / RUN_ID / "prepare-run.json").read_text(encoding="utf-8"))
        self.assertEqual(stored, record)

    def test_binding_with_checkpoints_requests_the_newest_link(self) -> None:
        checkpoint = self.add_checkpoint(1)
        record = self.bind(checkpoints=True)
        self.assertEqual(record["loader"]["index"], 1)
        self.assertEqual(record["loader"]["save_sha256"], digest(checkpoint))
        self.assertEqual(pathlib.Path(record["loader"]["save"]), checkpoint.resolve())

    def assert_refused(self, pattern: str, **kwargs) -> None:
        with self.assertRaisesRegex(prepare.PrepareError, pattern):
            self.bind(**kwargs)
        self.assertFalse((self.root / "bridge" / "runtime-startup-binding.json").exists())
        self.assertFalse((self.root / "runs").exists())

    def test_binding_refuses_while_the_game_runs(self) -> None:
        self.process = (1, "now")
        self.assert_refused("is running")

    def test_binding_refuses_another_run_id(self) -> None:
        self.assert_refused("not the clone manifest's run id", run_id="other-run")

    def test_binding_refuses_changed_clone_content(self) -> None:
        self.save.write_bytes(oni_save_bytes(base_name="Terra Source", body=b"changed"))
        self.assert_refused("clone check failed")

    def test_binding_refuses_a_clone_without_the_oni_prefix(self) -> None:
        self.assert_refused("oni- name", clone_save=self.clone_dir / "terra.sav")

    # -- config --------------------------------------------------------------

    def test_config_waits_for_the_game_and_its_session(self) -> None:
        prepared = self.bind()
        with self.assertRaisesRegex(prepare.PrepareError, "not running"):
            prepare.prepare_config(self.env, run_id=RUN_ID)
        self.process = (4242, "now")
        with self.assertRaisesRegex(prepare.PrepareError, "state log missing"):
            prepare.prepare_config(self.env, run_id=RUN_ID)
        self.write_session(prepared, pid=999)
        with self.assertRaisesRegex(prepare.PrepareError, "no session_started"):
            prepare.prepare_config(self.env, run_id=RUN_ID)

    def test_config_names_the_fields_a_session_disagrees_on(self) -> None:
        prepared = self.bind()
        self.process = (4242, "now")
        self.write_session(prepared, save_id_hash="sha256:" + "0" * 64, startup_binding_status="rejected")
        with self.assertRaisesRegex(prepare.PrepareError, "save_id_hash, startup_binding_status"):
            prepare.prepare_config(self.env, run_id=RUN_ID)

    def test_config_is_written_and_loads_through_the_controller(self) -> None:
        prepared = self.bind()
        self.process = (4242, "now")
        self.write_session(prepared)
        report = prepare.prepare_config(self.env, run_id=RUN_ID, controller_id="terra-controller")
        config = json.loads(pathlib.Path(report["config"]["path"]).read_text(encoding="utf-8"))
        self.assertEqual(config["startup_expectations"]["validation_process_id"], 4242)
        self.assertEqual(config["session_id"], "session-1")
        self.assertNotIn("checkpoints", config)
        self.assertEqual(report["save_id_hash"], digest(self.save))
        self.assertEqual(report["checkpoint_index"], 0)
        self.assertIn("--save-hash " + digest(self.save), report["next"])

    def test_config_with_checkpoints_continues_from_the_newest_link(self) -> None:
        checkpoint = self.add_checkpoint(1)
        prepared = self.bind(checkpoints=True)
        self.process = (4242, "now")
        self.write_session(prepared)
        report = prepare.prepare_config(self.env, run_id=RUN_ID)
        config = json.loads(pathlib.Path(report["config"]["path"]).read_text(encoding="utf-8"))
        self.assertIs(config["checkpoints"], True)
        self.assertEqual(report["checkpoint_index"], 1)
        self.assertEqual(report["save_id_hash"], digest(checkpoint))

    def test_a_newer_checkpoint_after_binding_makes_the_config_refuse(self) -> None:
        self.add_checkpoint(1)
        prepared = self.bind(checkpoints=True)
        self.process = (4242, "now")
        self.write_session(prepared)
        self.add_checkpoint(2)
        with self.assertRaisesRegex(prepare.PrepareError, "does not load"):
            prepare.prepare_config(self.env, run_id=RUN_ID)

    def test_config_refuses_a_binding_replaced_after_the_binding_phase(self) -> None:
        prepared = self.bind()
        self.process = (4242, "now")
        self.write_session(prepared)
        binding_path = self.root / "bridge" / "runtime-startup-binding.json"
        binding_path.write_text(binding_path.read_text(encoding="utf-8") + " ", encoding="utf-8")
        with self.assertRaisesRegex(prepare.PrepareError, "binding changed"):
            prepare.prepare_config(self.env, run_id=RUN_ID)

    def test_main_reports_a_refusal_as_json_with_exit_code_2(self) -> None:
        self.process = (1, "now")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = prepare.main(
                ["binding", "--clone-manifest", str(self.manifest), "--clone-save", str(self.save)], env=self.env
            )
        self.assertEqual(code, 2)
        self.assertIn("running", json.loads(output.getvalue())["error"])


if __name__ == "__main__":
    unittest.main()
