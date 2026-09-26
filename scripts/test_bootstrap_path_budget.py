#!/usr/bin/env python3
"""Focused offline tests for the runtime-bootstrap legacy path budget."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
import unittest
from typing import Any, Mapping


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT_ROOT = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))
import manage_live_runtime_bootstrap as bootstrap


RUN18_MANIFEST_COPY = (
    PROJECT_ROOT
    / "reports"
    / "l19"
    / "path"
    / "run18-environment-manifest.json.copy"
)
RUN18_EVIDENCE_ROOT = (
    PROJECT_ROOT / "reports" / "controller-plan-sequence-20260913"
)
RUN18_ONI_ROOT = pathlib.Path(
    r"C:\Users\user\Documents\Klei\OxygenNotIncluded"
)
SHORT_EVIDENCE_ROOT = PROJECT_ROOT / "reports" / "l19"
SHORT_RUN_ID = "user-l19-20260916-01"
SYNTHETIC_TRANSACTION_ID = "rtb-" + ("0" * 24)


def utf8_sha256(path: pathlib.Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def load_run18_manifest() -> dict[str, Any]:
    if not RUN18_MANIFEST_COPY.is_file():
        raise AssertionError(
            "run18 evidence copy is required; no live manifest fallback is permitted"
        )
    value = json.loads(RUN18_MANIFEST_COPY.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError("run18 evidence copy must have an object root")
    return value


def make_plan(
    manifest: Mapping[str, Any],
    roots: bootstrap.Roots,
    *,
    transaction_id: str = SYNTHETIC_TRANSACTION_ID,
    include_archive: bool = True,
) -> dict[str, Any]:
    old_bridge_snapshot = manifest["backup"]["old_bridge"]["snapshot"]
    probe_snapshot = manifest["backup"]["probe"]["snapshot"]
    return {
        "desired": {
            "bootstrap": {
                "bridge_dir": str(
                    roots.oni_root / pathlib.Path(*bootstrap.BRIDGE_RELATIVE.parts)
                )
            },
            "archive_relative": (
                f"{bootstrap.ARCHIVE_ROOT_RELATIVE.as_posix()}/"
                f"{roots.run_id}-preexisting-{transaction_id}"
                if include_archive
                else None
            ),
        },
        "prestate": {
            "old_bridge": {"snapshot": old_bridge_snapshot},
            "probe": {"snapshot": probe_snapshot},
        },
    }


def candidate_view(
    roots: bootstrap.Roots,
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    candidates = bootstrap.legacy_path_candidates(roots, plan)
    max_units = max(bootstrap.utf16_units(path) for path in candidates)
    return {
        "candidate_count": len(candidates),
        "max_units": max_units,
        "max_candidates": [
            bootstrap._legacy_path_disclosure(path, roots)
            | {"utf16_units": bootstrap.utf16_units(path)}
            for path in candidates
            if bootstrap.utf16_units(path) == max_units
        ],
    }


def observe() -> dict[str, Any]:
    manifest = load_run18_manifest()
    run18_roots = bootstrap.Roots(
        manifest["run_id"], RUN18_ONI_ROOT, RUN18_EVIDENCE_ROOT, None
    )
    run18_plan = make_plan(manifest, run18_roots)
    try:
        bootstrap.validate_legacy_path_budget(run18_roots, run18_plan)
    except bootstrap.BootstrapError as error:
        run18_error = str(error)
        run18_diagnostic = error.diagnostic
    else:
        raise AssertionError("run18 evidence must reproduce the legacy rejection")
    if not isinstance(run18_diagnostic, dict):
        raise AssertionError("path-budget refusal must carry a diagnostic")

    short_roots = bootstrap.Roots(
        SHORT_RUN_ID, RUN18_ONI_ROOT, SHORT_EVIDENCE_ROOT, None
    )
    short_plan = make_plan(manifest, short_roots)
    bootstrap.validate_legacy_path_budget(short_roots, short_plan)
    short_view = candidate_view(short_roots, short_plan)

    return {
        "schema": "oni.offline-bootstrap-path-budget-observation.v1",
        "offline": True,
        "live_admission": False,
        "game_launched": False,
        "bridge_launched": False,
        "run18_manifest_copy": str(RUN18_MANIFEST_COPY.relative_to(PROJECT_ROOT)),
        "run18_manifest_copy_sha256": utf8_sha256(RUN18_MANIFEST_COPY),
        "run18": {
            "run_id": run18_roots.run_id,
            "synthetic_transaction_id": SYNTHETIC_TRANSACTION_ID,
            "transaction_id_status": "unknown_not_published",
            "error": run18_error,
            "candidate_view": candidate_view(run18_roots, run18_plan),
            "diagnostic": run18_diagnostic,
        },
        "short_layout": {
            "evidence_root": str(SHORT_EVIDENCE_ROOT.relative_to(PROJECT_ROOT)),
            "run_id": SHORT_RUN_ID,
            "evidence_root_units": bootstrap.utf16_units(SHORT_EVIDENCE_ROOT),
            "candidate_view": short_view,
            "budget_passed": short_view["max_units"] <= 259,
        },
        "source_sha256": utf8_sha256(PROJECT_ROOT / "scripts" / "manage_live_runtime_bootstrap.py"),
    }


class BootstrapPathBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = load_run18_manifest()

    def roots_for(self, evidence_root: pathlib.Path, run_id: str) -> bootstrap.Roots:
        return bootstrap.Roots(run_id, RUN18_ONI_ROOT, evidence_root, None)

    def test_utf16_259_and_260_boundaries(self) -> None:
        self.assertEqual(bootstrap.utf16_units(pathlib.Path("a" * 259)), 259)
        self.assertEqual(bootstrap.utf16_units(pathlib.Path("a" * 260)), 260)

    def test_non_bmp_uses_two_utf16_units(self) -> None:
        path_259 = pathlib.Path("a" * 257 + "😀")
        path_260 = pathlib.Path("a" * 258 + "😀")
        self.assertEqual(len(str(path_259)), 258)
        self.assertEqual(bootstrap.utf16_units(path_259), 259)
        self.assertEqual(bootstrap.utf16_units(path_260), 260)

    def test_publication_temp_and_recovery_candidates_are_enumerated(self) -> None:
        roots = self.roots_for(pathlib.Path(r"C:\e"), "x")
        plan = make_plan(self.manifest, roots, include_archive=True)
        candidates = {
            str(path).replace("\\", "/")
            for path in bootstrap.legacy_path_candidates(roots, plan)
        }
        transaction = "C:/e/x/runtime-bootstrap"
        self.assertIn(transaction + "/owner.json.new", candidates)
        self.assertIn(
            "C:/e/x/environment/runtime-bootstrap-install-stage.json.new",
            candidates,
        )
        self.assertIn(
            transaction + "/recovery/old-probe/OniAiProbeMod.dll", candidates
        )
        self.assertTrue(
            any(
                "/abort/custody/a/" in path
                and path.endswith("/evidence/runtime-intents/cmd-runtime-pause-234005-1c8a5ac1.json")
                for path in candidates
            )
        )

    def test_legacy_gate_passes_at_259_and_refuses_260(self) -> None:
        manifest = {
            "backup": {
                "old_bridge": {"snapshot": {"entries": []}},
                "probe": {"snapshot": {"entries": []}},
            }
        }
        root_259 = pathlib.Path("C:/" + ("e" * 179))
        root_260 = pathlib.Path("C:/" + ("e" * 180))
        roots_259 = self.roots_for(root_259, "x")
        roots_260 = self.roots_for(root_260, "x")
        plan_259 = make_plan(manifest, roots_259, include_archive=False)
        plan_260 = make_plan(manifest, roots_260, include_archive=False)
        self.assertEqual(candidate_view(roots_259, plan_259)["max_units"], 259)
        bootstrap.validate_legacy_path_budget(roots_259, plan_259)
        self.assertEqual(candidate_view(roots_260, plan_260)["max_units"], 260)
        with self.assertRaises(bootstrap.BootstrapError) as context:
            bootstrap.validate_legacy_path_budget(roots_260, plan_260)
        self.assertEqual(
            str(context.exception),
            "runtime bootstrap path exceeds the 259 UTF-16-unit legacy budget",
        )
        self.assertGreaterEqual(context.exception.diagnostic["offending_count"], 1)
        self.assertTrue(
            all(
                item["utf16_units"] > 259
                for item in context.exception.diagnostic["offending_candidates"]
            )
        )

    def test_run18_rejection_diagnostic_is_relative_and_deterministic(self) -> None:
        roots = self.roots_for(RUN18_EVIDENCE_ROOT, self.manifest["run_id"])
        plan = make_plan(self.manifest, roots)
        with self.assertRaises(bootstrap.BootstrapError) as context:
            bootstrap.validate_legacy_path_budget(roots, plan)
        diagnostic = context.exception.diagnostic
        self.assertIsNotNone(diagnostic)
        self.assertEqual(diagnostic["candidate_count"], 306)
        self.assertEqual(diagnostic["offending_count"], 3)
        self.assertEqual(
            sorted(item["utf16_units"] for item in diagnostic["offending_candidates"]),
            [262, 279, 279],
        )
        serialized = json.dumps(diagnostic, ensure_ascii=False, sort_keys=True)
        self.assertNotIn(str(RUN18_EVIDENCE_ROOT), serialized)
        self.assertNotIn(str(RUN18_ONI_ROOT), serialized)
        self.assertTrue(
            all(item["root"] == "evidence_root" for item in diagnostic["offending_candidates"])
        )
        self.assertTrue(
            all(item["relative_path"] for item in diagnostic["offending_candidates"])
        )
        with self.assertRaises(bootstrap.BootstrapError) as second:
            bootstrap.validate_legacy_path_budget(roots, plan)
        self.assertEqual(diagnostic, second.exception.diagnostic)
        self.assertEqual(
            bootstrap.bootstrap_error_document(context.exception)["diagnostic"],
            diagnostic,
        )

    def test_short_existing_workspace_layout_passes(self) -> None:
        roots = self.roots_for(SHORT_EVIDENCE_ROOT, SHORT_RUN_ID)
        plan = make_plan(self.manifest, roots)
        self.assertTrue(SHORT_EVIDENCE_ROOT.is_dir())
        self.assertTrue(bootstrap.path_within(SHORT_EVIDENCE_ROOT, PROJECT_ROOT))
        self.assertFalse(bootstrap.paths_overlap(SHORT_EVIDENCE_ROOT, RUN18_ONI_ROOT))
        self.assertTrue(bootstrap.same_volume(SHORT_EVIDENCE_ROOT, RUN18_ONI_ROOT))
        self.assertIsNotNone(bootstrap.RUN_ID_RE.fullmatch(SHORT_RUN_ID))
        bootstrap.validate_legacy_path_budget(roots, plan)
        view = candidate_view(roots, plan)
        self.assertLessEqual(view["max_units"], 259)
        self.assertEqual(view["max_units"], 217)


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--json", action="store_true")
    parsed, remaining = parser.parse_known_args()
    if parsed.json:
        if remaining:
            parser.error("--json does not accept unittest arguments")
        print(json.dumps(observe(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    unittest_args = [sys.argv[0], *remaining]
    unittest.main(argv=unittest_args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
