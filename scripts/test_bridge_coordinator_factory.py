#!/usr/bin/env python3
"""Tests for the explicit, clone-gated Bridge coordinator factory."""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bridge import oni_bridge


class CoordinatorFactoryTests(unittest.TestCase):
    def test_factory_builds_read_only_observer_from_fresh_preflight(self) -> None:
        bridge_dir = pathlib.Path(tempfile.mkdtemp(prefix="oni-factory-bridge-"))
        intent_dir = bridge_dir / "intents"
        intent_dir.mkdir()
        manifest, save, preview = (bridge_dir / name for name in ("manifest.json", "clone.sav", "clone.png"))
        manifest.write_text("{}", encoding="utf-8")
        save.write_bytes(b"save")
        preview.write_bytes(b"preview")
        expected = {"session_id": "s", "snapshot_id": "s:1", "state_hash": "sha256:" + "a" * 64,
                    "world_id": 0, "game_build": "b", "mod_version": "m", "save_id_hash": "sha256:" + "b" * 64}
        fake_probe = mock.Mock(return_value={"schema": "oni.clone-launch-preflight.v1", "launch_allowed": True,
            "bridge_dir": str(bridge_dir), "process_counts": {}, "queue_counts": {},
            "manifest_sha256": "sha256:" + "c" * 64, "clone_save_sha256": expected["save_id_hash"],
            "clone_preview_sha256": "sha256:" + "d" * 64, "clone_save_path_id": "sha256:" + "e" * 64,
            "clone_preview_path_id": "sha256:" + "f" * 64})
        fake_coordinator = mock.Mock()
        fake_coordinator.tick.return_value = {"status": "candidate", "live_admission": False}
        with mock.patch.object(oni_bridge, "build_process_probe", return_value=mock.Mock()), \
             mock.patch.object(oni_bridge, "build_queue_probe", return_value=mock.Mock()), \
             mock.patch.object(oni_bridge, "build_clone_probe", return_value=fake_probe), \
             mock.patch.object(oni_bridge, "PlanSequenceCoordinator", return_value=fake_coordinator) as constructor:
            observer = oni_bridge.build_coordinator_observer(
                bridge_dir=bridge_dir, plan_id="plan", step_ids=("step",), expected_identity=expected,
                intent_directory=intent_dir, clone_manifest=manifest, clone_save=save, clone_preview=preview,
            )
            self.assertEqual(observer(), {"status": "candidate", "live_admission": False})
            constructor.assert_called_once()
            self.assertFalse(constructor.call_args.kwargs["publish_one"])
            fake_probe.assert_called_once()

    def test_factory_rejects_incomplete_clone_paths(self) -> None:
        with self.assertRaisesRegex(ValueError, "complete real clone paths"):
            oni_bridge.build_coordinator_observer(
                bridge_dir=pathlib.Path("."), plan_id="plan", step_ids=("step",), expected_identity={},
                intent_directory=pathlib.Path("."), clone_manifest=None, clone_save=pathlib.Path("save"),
                clone_preview=pathlib.Path("preview"),
            )

    def test_config_factory_rejects_incomplete_or_wrong_schema_before_probe(self) -> None:
        with tempfile.TemporaryDirectory(prefix="oni-coordinator-config-") as directory:
            root = pathlib.Path(directory)
            config = root / "config.json"
            config.write_text('{"schema":"wrong"}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "schema is invalid"):
                oni_bridge.build_read_only_coordinator_observer(root, config)

    def test_cli_coordinator_config_is_explicit_opt_in(self) -> None:
        with mock.patch.object(sys, "argv", ["oni_bridge.py", "--once", "--coordinator-config", "config.json"]):
            args = oni_bridge.parse_args()
        self.assertEqual(args.coordinator_config, pathlib.Path("config.json"))

    def test_cli_default_has_no_coordinator_observer_config(self) -> None:
        with mock.patch.object(sys, "argv", ["oni_bridge.py", "--once"]):
            args = oni_bridge.parse_args()
        self.assertIsNone(args.coordinator_config)
        self.assertIsNone(args.planned_owner_config)

    def test_cli_planned_owner_config_is_explicit_opt_in(self) -> None:
        with mock.patch.object(sys, "argv", ["oni_bridge.py", "--once", "--planned-owner-config", "owner.json"]):
            args = oni_bridge.parse_args()
        self.assertEqual(args.planned_owner_config, pathlib.Path("owner.json"))

    def test_main_forwards_explicit_observer_to_run_once(self) -> None:
        sentinel = object()
        with mock.patch.object(
            sys,
            "argv",
            ["oni_bridge.py", "--once", "--bridge-dir", "bridge", "--coordinator-config", "config.json"],
        ), mock.patch.object(
            oni_bridge, "build_read_only_coordinator_observer", return_value=sentinel,
        ) as factory, mock.patch.object(oni_bridge, "run_once") as run_once:
            oni_bridge.main()
        factory.assert_called_once_with(pathlib.Path("bridge"), pathlib.Path("config.json"))
        self.assertIs(run_once.call_args.kwargs["coordinator_observer"], sentinel)

    def test_main_forwards_explicit_observer_to_watch(self) -> None:
        sentinel = object()
        with mock.patch.object(
            sys,
            "argv",
            ["oni_bridge.py", "--bridge-dir", "bridge", "--coordinator-config", "config.json"],
        ), mock.patch.object(
            oni_bridge, "build_read_only_coordinator_observer", return_value=sentinel,
        ) as factory, mock.patch.object(oni_bridge, "watch") as watch:
            oni_bridge.main()
        factory.assert_called_once_with(pathlib.Path("bridge"), pathlib.Path("config.json"))
        self.assertIs(watch.call_args.kwargs["coordinator_observer"], sentinel)

    def test_main_forwards_explicit_planned_owner_to_run_once(self) -> None:
        observer = object()
        owner = object()
        with mock.patch.object(
            sys, "argv", ["oni_bridge.py", "--once", "--bridge-dir", "bridge",
                           "--planned-owner-config", "owner.json"],
        ), mock.patch.object(oni_bridge, "build_read_only_coordinator_observer", return_value=observer), \
             mock.patch.object(oni_bridge, "build_planned_queue_owner_from_config", return_value=owner), \
             mock.patch.object(oni_bridge, "run_once") as run_once:
            oni_bridge.main()
        self.assertIsNone(run_once.call_args.kwargs["coordinator_observer"])
        self.assertIs(run_once.call_args.kwargs["planned_queue_owner"], owner)


if __name__ == "__main__":
    unittest.main()
