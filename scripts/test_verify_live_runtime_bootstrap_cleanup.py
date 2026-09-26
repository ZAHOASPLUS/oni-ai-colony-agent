#!/usr/bin/env python3
"""Unit tests for batch fixture cleanup helpers in verify_live_runtime_bootstrap.py."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts import verify_live_runtime_bootstrap as core


class VerifyLiveRuntimeBootstrapCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = tempfile.TemporaryDirectory(prefix="oni-rtb-cleanup-")
        self.base = Path(self.workspace.name)

    def tearDown(self) -> None:
        self.workspace.cleanup()

    def _round_root(self, name: str = "round") -> Path:
        root = self.base / name
        root.mkdir(parents=True, exist_ok=True)
        return root

    @staticmethod
    def _normal_fixture_result(batch_root: Path) -> str:
        fixture = batch_root / "fixture"
        fixture.mkdir()
        (fixture / "payload").write_text("fixture-data", encoding="utf-8")
        return "ok"

    def test_run_batch_successfully_cleans_fixtures_and_batch_root(self) -> None:
        round_root = self._round_root("success")

        result = core._run_batch(
            round_root,
            core._batch_root_name(0),
            self._normal_fixture_result,
        )

        self.assertEqual(result, "ok")
        self.assertFalse((round_root / "b000").exists())

    def test_run_batch_empty_callback_removes_batch_root(self) -> None:
        round_root = self._round_root("empty")

        result = core._run_batch(
            round_root,
            core._batch_root_name(0),
            lambda _batch_root: "empty-ok",
        )

        self.assertEqual(result, "empty-ok")
        self.assertFalse(os.path.lexists(round_root / "b000"))

    def test_handle_bound_helper_deletes_ordinary_tree(self) -> None:
        batch_root = self._round_root("handle-delete") / "b000"
        nested = batch_root / "fixture" / "nested"
        nested.mkdir(parents=True)
        (nested / "payload.txt").write_text("payload", encoding="utf-8")

        core._delete_handle_bound_tree(batch_root)

        self.assertFalse(os.path.lexists(batch_root))

    def test_fixture_hardlink_alias_self_release_preserves_original(self) -> None:
        fixture_root = self._round_root("hardlink-self-release") / "fixture"
        parent = fixture_root / "release"
        parent.mkdir(parents=True)
        original = parent / "payload.dll"
        alias = parent / "payload.dll.test-alias"
        original.write_bytes(b"fixture-hardlink-payload")
        original_identity = tuple(core.core.node_identity(os.lstat(original)))
        original_parent_identity = tuple(core.core.node_identity(os.lstat(parent)))

        token = core._create_fixture_hardlink_alias(fixture_root, original, alias)

        self.assertEqual(os.lstat(original).st_nlink, 2)
        self.assertEqual(os.lstat(alias).st_nlink, 2)
        core._release_fixture_hardlink_alias(token)

        self.assertFalse(os.path.lexists(alias))
        self.assertEqual(
            tuple(core.core.node_identity(os.lstat(original))),
            original_identity,
        )
        self.assertEqual(
            tuple(core.core.node_identity(os.lstat(parent))),
            original_parent_identity,
        )
        self.assertEqual(os.lstat(original).st_nlink, 1)
        self.assertEqual(original.read_bytes(), b"fixture-hardlink-payload")

    def test_fixture_hardlink_alias_third_link_refuses_and_preserves_all(self) -> None:
        fixture_root = self._round_root("hardlink-third-link") / "fixture"
        parent = fixture_root / "release"
        parent.mkdir(parents=True)
        original = parent / "payload.dll"
        alias = parent / "payload.dll.test-alias"
        third = parent / "payload.dll.third-link"
        original.write_bytes(b"fixture-hardlink-payload")
        token = core._create_fixture_hardlink_alias(fixture_root, original, alias)
        os.link(original, third)

        with self.assertRaises(AssertionError) as error:
            core._release_fixture_hardlink_alias(token)

        self.assertIn("link count changed", str(error.exception))
        identities = {
            tuple(core.core.node_identity(os.lstat(path)))
            for path in (original, alias, third)
        }
        self.assertEqual(len(identities), 1)
        self.assertTrue(all(os.path.lexists(path) for path in (original, alias, third)))
        self.assertTrue(all(os.lstat(path).st_nlink == 3 for path in (original, alias, third)))

    def test_run_batch_residual_hardlink_still_fails_closed_and_is_preserved(self) -> None:
        round_root = self._round_root("residual-hardlink")
        batch_root = round_root / core._batch_root_name(0)

        def leave_hardlink(target: Path) -> str:
            fixture = target / "fixture"
            fixture.mkdir()
            original = fixture / "payload.bin"
            original.write_bytes(b"residual-hardlink")
            os.link(original, fixture / "payload.bin.alias")
            return "callback-passed"

        with self.assertRaises(AssertionError) as error:
            core._run_batch(
                round_root,
                core._batch_root_name(0),
                leave_hardlink,
            )

        self.assertIn("hard-linked file", str(error.exception))
        original = batch_root / "fixture" / "payload.bin"
        alias = batch_root / "fixture" / "payload.bin.alias"
        self.assertTrue(os.path.lexists(original))
        self.assertTrue(os.path.lexists(alias))
        self.assertEqual(
            tuple(core.core.node_identity(os.lstat(original))),
            tuple(core.core.node_identity(os.lstat(alias))),
        )
        self.assertEqual(os.lstat(original).st_nlink, 2)

    def test_handle_bound_helper_blocks_name_replacement_and_preserves_sentinel(self) -> None:
        batch_root = self._round_root("handle-replacement") / "b000"
        nested = batch_root / "fixture" / "nested"
        nested.mkdir(parents=True)
        payload = nested / "payload.txt"
        payload.write_text("original", encoding="utf-8")
        external = self.base / "external-move-target"
        external.mkdir()
        held_file = external / "payload-held.txt"
        held_parent = external / "nested-held"
        sentinel = external / "external-sentinel.txt"
        sentinel.write_text("external-must-survive", encoding="utf-8")
        fired = False
        blocked_winerrors: dict[str, int | None] = {}

        def replace_after_open(stage: str, path: Path) -> None:
            nonlocal fired
            if stage == "after_delete_handle_open" and path == payload and not fired:
                fired = True
                for label, source, destination in (
                    ("file", path, held_file),
                    ("parent", nested, held_parent),
                ):
                    try:
                        source.rename(destination)
                    except OSError as error:
                        blocked_winerrors[label] = getattr(error, "winerror", None)
                    else:
                        raise AssertionError(
                            f"delete-bound {label} remained renameable outside the batch"
                        )

        previous_hook = core._CLEANUP_TEST_HOOK
        core._CLEANUP_TEST_HOOK = replace_after_open
        try:
            core._delete_handle_bound_tree(batch_root)
        finally:
            core._CLEANUP_TEST_HOOK = previous_hook

        self.assertTrue(fired)
        self.assertEqual(set(blocked_winerrors), {"file", "parent"})
        self.assertTrue(
            all(winerror in {5, 32} for winerror in blocked_winerrors.values()),
            blocked_winerrors,
        )
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "external-must-survive")
        self.assertFalse(os.path.lexists(held_file))
        self.assertFalse(os.path.lexists(held_parent))
        self.assertFalse(os.path.lexists(batch_root))

    def test_run_batch_stray_direct_file_is_refused_and_preserved(self) -> None:
        round_root = self._round_root("stray-direct-file")
        batch_root = round_root / core._batch_root_name(0)

        def make_stray(target: Path) -> None:
            (target / "fixture").mkdir()
            (target / "stray.txt").write_text("retain-me", encoding="utf-8")

        with self.assertRaises(AssertionError) as error:
            core._run_batch(round_root, core._batch_root_name(0), make_stray)

        self.assertIn("not a regular directory", str(error.exception))
        self.assertTrue((batch_root / "fixture").is_dir())
        self.assertEqual(
            (batch_root / "stray.txt").read_text(encoding="utf-8"),
            "retain-me",
        )

    def test_run_batch_callback_exception_preserves_created_fixture_and_lock(self) -> None:
        round_root = self._round_root("callback-exception")
        batch_root = round_root / core._batch_root_name(0)
        lock_path = batch_root / "transaction.lock"

        def explode(target: Path) -> None:
            (target / "fixture").mkdir()
            lock_path.write_text("locked", encoding="utf-8")
            raise RuntimeError("callback failure")

        with self.assertRaises(RuntimeError):
            core._run_batch(round_root, core._batch_root_name(0), explode)

        self.assertTrue(batch_root.exists())
        self.assertTrue((batch_root / "fixture").is_dir())
        self.assertTrue(lock_path.exists())

    def test_external_attack_assertion_preserves_bootstrap_error_and_hook_state(self) -> None:
        refusal = core.core.BootstrapError(
            "pre-hook custody refusal",
            diagnostic={"operation": "fixture-probe"},
        )

        with self.assertRaises(AssertionError) as raised:
            core._raise_external_attack_assertion(
                "external replacement probe did not reach its hook",
                refusal,
                hook_fired=False,
                held_exists=False,
                target_exists=True,
            )

        self.assertIs(raised.exception.__cause__, refusal)
        message = str(raised.exception)
        self.assertIn("caught_bootstrap_error=pre-hook custody refusal", message)
        self.assertIn("hook_fired=False", message)
        self.assertIn("held_exists=False", message)
        self.assertIn("target_exists=True", message)
        self.assertIn("'operation': 'fixture-probe'", message)

    def test_run_batch_rejects_round_base_identity_drift(self) -> None:
        round_root = self._round_root("round-drift")
        fallback_root = round_root.with_name("round-drift-backup")
        sentinel = self.base / "round-drift-sentinel.txt"
        sentinel.write_text("survives", encoding="utf-8")

        def drift(target: Path) -> None:
            target.parent.rename(fallback_root)
            target.parent.mkdir()

        with self.assertRaises((AssertionError, OSError)) as error:
            core._run_batch(round_root, core._batch_root_name(0), drift)
        if isinstance(error.exception, AssertionError):
            self.assertIn("identity", str(error.exception))
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "survives")
        self.assertTrue(round_root.exists())

    def test_run_batch_rejects_batch_identity_name_swap(self) -> None:
        round_root = self._round_root("batch-swap")
        swapped = round_root / "b000-swapped"
        sentinel = self.base / "batch-swap-sentinel.txt"
        sentinel.write_text("survives", encoding="utf-8")

        def swap(target: Path) -> None:
            target.rename(swapped)
            target.mkdir()

        with self.assertRaises((AssertionError, OSError)) as error:
            core._run_batch(round_root, core._batch_root_name(0), swap)
        if isinstance(error.exception, AssertionError):
            self.assertIn("identity changed", str(error.exception))
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "survives")

    def test_main_final_cleanup_failure_still_writes_failed_report(self) -> None:
        report_path = self.base / "forced-cleanup-report.json"
        real_delete = core._delete_handle_bound_tree
        created_base: Path | None = None

        def fake_round(_base: Path, round_index: int) -> dict[str, object]:
            return {
                "round": round_index,
                "status": "passed",
                "scenario_count": 0,
                "scenarios": [],
            }

        def fail_final_cleanup(path: Path) -> None:
            nonlocal created_base
            created_base = Path(path)
            raise AssertionError("forced final cleanup failure")

        with (
            mock.patch.object(core, "REPORT", report_path),
            mock.patch.object(core, "run_static_contract_cases", return_value=[]),
            mock.patch.object(core, "assert_actual_fixture_path_budget"),
            mock.patch.object(core, "run_round", side_effect=fake_round),
            mock.patch.object(core, "_delete_handle_bound_tree", side_effect=fail_final_cleanup),
            mock.patch.object(sys, "argv", ["verify", "--rounds", "2"]),
        ):
            return_code = core.main()

        self.assertEqual(return_code, 1)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "failed")
        self.assertIn("forced final cleanup failure", report["failure"])
        self.assertIsNotNone(report["preserved_test_root"])
        self.assertEqual(report["preserved_test_root"], str(created_base.absolute()))
        self.assertTrue(created_base.is_dir())
        real_delete(created_base)

    def test_actual_path_budget_full_contract_rejects_before_round(self) -> None:
        report_path = self.base / "path-budget-report.json"
        verifier_base = self.base / "path-budget-preserved-root"
        verifier_base.mkdir()
        observed: dict[str, object] = {}
        run_round = mock.Mock(
            return_value={
                "round": 1,
                "status": "passed",
                "scenario_count": 0,
                "scenarios": [],
            }
        )

        def candidate_spy(
            roots: object,
            plan: object,
            abort_plan: object = None,
            explicit: object = None,
        ) -> list[Path]:
            observed.update(
                roots=roots,
                plan=plan,
                abort_plan=abort_plan,
                explicit=explicit,
            )
            if abort_plan is None or explicit is None:
                return [Path("short")]
            return [Path("x" * 260)]

        with (
            mock.patch.object(core, "REPORT", report_path),
            mock.patch.object(core, "run_static_contract_cases", return_value=[]),
            mock.patch.object(core, "run_round", run_round),
            mock.patch.object(core.tempfile, "mkdtemp", return_value=str(verifier_base)),
            mock.patch.object(
                core.core,
                "legacy_path_candidates",
                side_effect=candidate_spy,
            ),
            mock.patch.object(sys, "argv", ["verify", "--rounds", "2"]),
        ):
            return_code = core.main()

        self.assertEqual(return_code, 1)
        run_round.assert_not_called()
        self.assertIsInstance(observed["abort_plan"], dict)
        self.assertEqual(
            set(observed["explicit"]),
            {
                "pre_boundary",
                "clone_manifest",
                "source_save",
                "source_preview",
                "loader_save",
                "loader_preview",
            },
        )
        events = {
            item["event"]
            for item in observed["abort_plan"]["steps"]
        }
        self.assertIn("archive_root_mtime_restored", events)
        self.assertIn("probe_container_mtime_restored", events)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "failed")
        self.assertIn("exceeds the 259 UTF-16-unit", report["failure"])
        self.assertEqual(report["preserved_test_root"], str(verifier_base.absolute()))
        self.assertTrue(verifier_base.is_dir())

    def test_run_batch_rejects_fixture_identity_swap(self) -> None:
        round_root = self._round_root("fixture-swap")

        def swap_fixture(target: Path) -> None:
            fixture = target / "fixture"
            fixture.mkdir()
            fixture_file = target / "fixture-file"
            fixture_file.write_text("replacement", encoding="utf-8")
            fixture.rmdir()
            fixture_file.replace(fixture)

        with self.assertRaises(AssertionError) as error:
            core._run_batch(round_root, core._batch_root_name(0), swap_fixture)
        self.assertIn("batch fixture is not a regular directory", str(error.exception))

    def test_run_batch_rejects_reparse_child_symlink(self) -> None:
        round_root = self._round_root("reparse-symlink")
        batch_root = round_root / core._batch_root_name(0)
        target = batch_root / "target"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("target", encoding="utf-8")
        probe_link = self.base / "symlink-probe"

        try:
            os.symlink(target, probe_link)
        except OSError as error:
            if getattr(error, "winerror", None) in (5, 1314):
                self.skipTest("symlink creation requires elevated privilege in this environment")
            raise
        else:
            probe_link.unlink()

        def make_symlink(target: Path) -> None:
            target_file = target / "target"
            target_file.write_text("target", encoding="utf-8")
            os.symlink(target_file, target / "reparse-link")

        with self.assertRaises(AssertionError) as error:
            core._run_batch(round_root, core._batch_root_name(0), make_symlink)
        self.assertIn("batch root contains reparse child", str(error.exception))

    def test_run_batch_rejects_reparse_child_junction(self) -> None:
        round_root = self._round_root("reparse-junction")
        batch_root = round_root / core._batch_root_name(0)
        batch_root.mkdir(parents=True, exist_ok=True)
        target = batch_root / "junction-target"
        target.mkdir()
        link = batch_root / "junction-link"
        command = f'mklink /J "{link}" "{target}"'
        proc = subprocess.run(
            ["cmd", "/c", command],
            check=False,
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            link.unlink()
        else:
            output = (proc.stdout + proc.stderr).lower()
            if "denied" in output or "权限" in output:
                self.skipTest("junction creation denied in this environment")
            self.skipTest("junction creation is not available in this environment")

        def make_junction(target_path: Path) -> None:
            (target_path / "junction-target").mkdir()
            subprocess.check_call(
                ["cmd", "/c", f'mklink /J "{target_path / "junction-link"}" "{target_path / "junction-target"}"'],
                shell=False,
            )

        with self.assertRaises(AssertionError) as error:
            core._run_batch(round_root, core._batch_root_name(0), make_junction)
        self.assertIn("batch root contains reparse child", str(error.exception))

    def test_non_direct_and_path_escape_fixtures_rejected(self) -> None:
        round_root = self._round_root("path-escape")
        batch_root = round_root / core._batch_root_name(0)
        batch_root.mkdir()

        direct = batch_root / "direct" / "nested"
        direct.parent.mkdir(parents=True)
        direct.mkdir()

        with self.assertRaises(AssertionError) as error:
            core._assert_removable_batch_fixture(direct, batch_root)
        self.assertIn("escaped its parent directory", str(error.exception))

    def test_run_batch_basename_collision_is_rejected(self) -> None:
        round_root = self._round_root("basename-collision")
        (round_root / core._batch_root_name(0)).mkdir(parents=True)
        with self.assertRaises(AssertionError) as error:
            core._run_batch(round_root, core._batch_root_name(0), self._normal_fixture_result)
        self.assertIn("batch root already exists", str(error.exception))

    def test_two_rounds_reuse_b000(self) -> None:
        shared_base = self._round_root("same-base")

        result_one = core._run_batch(
            shared_base,
            core._batch_root_name(0),
            self._normal_fixture_result,
        )
        result_two = core._run_batch(
            shared_base,
            core._batch_root_name(0),
            self._normal_fixture_result,
        )

        self.assertEqual(result_one, "ok")
        self.assertEqual(result_two, "ok")
        self.assertFalse(os.path.lexists(shared_base / "b000"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
