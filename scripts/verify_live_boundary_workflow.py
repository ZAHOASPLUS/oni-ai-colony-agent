#!/usr/bin/env python3
"""Offline verification for the live-boundary capture/clone/restore workflow."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import py_compile
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from typing import Any, Callable


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
REPORT = ROOT / "reports" / "live-boundary-workflow-verification.json"
CAPTURE = SCRIPTS / "capture-live-safety-boundary.ps1"
COMPARE = SCRIPTS / "compare_live_safety_boundaries.py"
CREATE = SCRIPTS / "create-named-test-clone.ps1"
RESTORE = SCRIPTS / "restore-named-test-clone.ps1"
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")


class VerificationFailure(RuntimeError):
    pass


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def write_bytes(path: pathlib.Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def write_json(path: pathlib.Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def run(command: list[str], expected: int | set[int] = 0) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=45,
        check=False,
    )
    allowed = {expected} if isinstance(expected, int) else expected
    if completed.returncode not in allowed:
        detail = (completed.stderr or completed.stdout).strip().replace("\r", " ").replace("\n", " ")
        raise VerificationFailure(
            f"command returned {completed.returncode}; expected {sorted(allowed)}; {detail[-1200:]}"
        )
    return completed


_PS_PARAMETER = re.compile(r"-[A-Za-z][A-Za-z0-9]*(?::\$(?:true|false))?")


def ps_command_text(script: pathlib.Path, arguments: tuple[str, ...]) -> str:
    """Build a -Command invocation that binds switch values on PowerShell 5.1.

    ``powershell.exe -File`` passes ``-Confirm:$false`` as the string
    "$false", which Windows PowerShell 5.1 refuses to bind to a switch.
    Parameter names (and switch values) are emitted verbatim; every other
    argument is a single-quoted literal.  No fixture value starts with "-".
    """

    def quote(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    parts = ["&", quote(str(script))]
    for argument in arguments:
        parts.append(argument if _PS_PARAMETER.fullmatch(argument) else quote(argument))
    return " ".join(parts)


def ps(script: pathlib.Path, *arguments: str, expected: int | set[int] = 0) -> subprocess.CompletedProcess[str]:
    if not POWERSHELL:
        raise VerificationFailure("PowerShell executable is unavailable")
    return run(
        [
            POWERSHELL,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            ps_command_text(script, arguments),
        ],
        expected=expected,
    )


def compare(pre: pathlib.Path, post: pathlib.Path, stem: str | None = None) -> tuple[int, dict[str, Any]]:
    command = [sys.executable, str(COMPARE), "--pre", str(pre), "--post", str(post)]
    if stem is not None:
        command.extend(["--allow-new-save-stem", stem])
    completed = run(command, expected={0, 2})
    return completed.returncode, json.loads(completed.stdout)


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise VerificationFailure(message)


FIXTURE_BASE_NAME = "Fixture-Base"


def oni_save_bytes(
    *,
    is_auto_save: bool = False,
    base_name: str = FIXTURE_BASE_NAME,
    body: bytes = b"fixture-save-v1\x00\x01",
) -> bytes:
    """Bytes with ONI's save header layout (SaveGame.GetHeader) and a fake body."""
    header = json.dumps(
        {
            "numberOfCycles": 6,
            "numberOfDuplicants": 3,
            "baseName": base_name,
            "isAutoSave": is_auto_save,
            "originalSaveName": "Original.sav",
            "clusterId": "fixture",
            "colonyGuid": "00000000-0000-0000-0000-00000000f1a7",
        }
    ).encode("utf-8")
    return struct.pack("<IiIi", 744825, len(header), 1, 1) + header + body


def clone_paths(fixture: dict[str, pathlib.Path], stem: str) -> tuple[pathlib.Path, pathlib.Path]:
    """A clone lives in its own colony folder, named by its stem, beside the source colony."""
    folder = fixture["save_dir"].parent / stem
    return folder / f"{stem}.sav", folder / f"{stem}.png"


def make_fixture(base: pathlib.Path, name: str) -> dict[str, pathlib.Path]:
    case = base / name
    oni = case / "oni-user"
    save_dir = oni / "save_files" / "FixtureColony"
    source = save_dir / "Original.sav"
    preview = save_dir / "Original.png"
    write_bytes(source, oni_save_bytes())
    write_bytes(preview, b"fixture-preview-v1\x89PNG")
    write_bytes(oni / "kplayerprefs.yaml", b"SaveFilenameKey: Original\n")
    write_bytes(oni / "mods" / "mods.json", b'{"version":1,"mods":[]}\n')
    write_bytes(oni / "mods" / "Dev" / "oni-ai-colony-agent" / "probe.txt", b"probe-v1\n")
    write_bytes(oni / "ai-bridge" / "action-policy.json", b'{"mode":"deny"}\n')
    for stage in (
        "pending",
        "approving",
        "approved",
        "executing",
        "confirming",
        "done",
        "failed",
        "rejected",
        "quarantine",
    ):
        (oni / "ai-bridge" / "commands" / stage).mkdir(parents=True, exist_ok=True)
    idle = case / "process-idle.json"
    game = case / "process-game.json"
    bridge = case / "process-bridge.json"
    write_json(
        idle,
        {
            "schema": "oni.process-state-fixture.v1",
            "oxygen_not_included_count": 0,
            "bridge_count": 0,
        },
    )
    write_json(
        game,
        {
            "schema": "oni.process-state-fixture.v1",
            "oxygen_not_included_count": 1,
            "bridge_count": 0,
        },
    )
    write_json(
        bridge,
        {
            "schema": "oni.process-state-fixture.v1",
            "oxygen_not_included_count": 0,
            "bridge_count": 1,
        },
    )
    return {
        "case": case,
        "oni": oni,
        "save_dir": save_dir,
        "source": source,
        "preview": preview,
        "idle": idle,
        "game": game,
        "bridge": bridge,
        "boundaries": case / "reports" / "live-boundaries",
        "runtime": case / "reports" / "runtime",
    }


def capture_with_receipt(
    fixture: dict[str, pathlib.Path],
    run_id: str,
    phase: str,
) -> tuple[pathlib.Path, dict[str, Any]]:
    completed = ps(
        CAPTURE,
        "-RunId",
        run_id,
        "-Phase",
        phase,
        "-OniUserRoot",
        str(fixture["oni"]),
        "-ReportRoot",
        str(fixture["boundaries"]),
        "-TestRoot",
        str(fixture["case"]),
        "-ProcessStateFixture",
        str(fixture["idle"]),
    )
    receipt = json.loads(completed.stdout.lstrip("\ufeff"))
    if not isinstance(receipt, dict):
        raise VerificationFailure("capture receipt root is not an object")
    return fixture["boundaries"] / run_id / f"{phase}-boundary.json", receipt


def capture(fixture: dict[str, pathlib.Path], run_id: str, phase: str) -> pathlib.Path:
    return capture_with_receipt(fixture, run_id, phase)[0]


def create_clone(
    fixture: dict[str, pathlib.Path],
    run_id: str,
    stem: str,
    process_fixture: str = "idle",
    what_if: bool = False,
    expected: int | set[int] = 0,
) -> subprocess.CompletedProcess[str]:
    arguments = [
        "-SourceSave",
        str(fixture["source"]),
        "-TargetStem",
        stem,
        "-RunId",
        run_id,
        "-OniUserRoot",
        str(fixture["oni"]),
        "-ReportRoot",
        str(fixture["runtime"]),
        "-TestRoot",
        str(fixture["case"]),
        "-ProcessStateFixture",
        str(fixture[process_fixture]),
    ]
    if what_if:
        arguments.append("-WhatIf")
    return ps(CREATE, *arguments, expected=expected)


def restore_clone(
    fixture: dict[str, pathlib.Path],
    run_id: str,
    stem: str,
    source: pathlib.Path | None = None,
    manifest: pathlib.Path | None = None,
    process_fixture: str = "idle",
    what_if: bool = False,
    expected: int | set[int] = 0,
) -> subprocess.CompletedProcess[str]:
    target = clone_paths(fixture, stem)[0]
    arguments = [
        "-CloneManifest",
        str(manifest or fixture["runtime"] / run_id / "test-clone-manifest.json"),
        "-SourceSave",
        str(source or fixture["source"]),
        "-TargetSave",
        str(target),
        "-ConfirmRestore",
        "-OniUserRoot",
        str(fixture["oni"]),
        "-ReportRoot",
        str(fixture["runtime"]),
        "-TestRoot",
        str(fixture["case"]),
        "-ProcessStateFixture",
        str(fixture[process_fixture]),
        "-Confirm:$false",
    ]
    if what_if:
        arguments.append("-WhatIf")
    return ps(RESTORE, *arguments, expected=expected)


def test_syntax(base: pathlib.Path) -> dict[str, Any]:
    del base
    parser_command = (
        "$files=@('scripts/capture-live-safety-boundary.ps1',"
        "'scripts/create-named-test-clone.ps1','scripts/restore-named-test-clone.ps1');"
        "$all=@();foreach($f in $files){$t=$null;$e=$null;"
        "[Management.Automation.Language.Parser]::ParseFile((Resolve-Path -LiteralPath $f).Path,"
        "[ref]$t,[ref]$e)|Out-Null;$all+=@($e)};if($all.Count){exit 1}"
    )
    run([POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", parser_command])
    with tempfile.NamedTemporaryFile(suffix=".pyc", delete=False) as handle:
        compiled = pathlib.Path(handle.name)
    try:
        py_compile.compile(str(COMPARE), cfile=str(compiled), doraise=True)
        py_compile.compile(str(pathlib.Path(__file__).resolve()), cfile=str(compiled), doraise=True)
    finally:
        compiled.unlink(missing_ok=True)
    return {"powershell_parser_errors": 0, "py_compile": "passed"}


def test_capture_determinism(base: pathlib.Path) -> dict[str, Any]:
    fixture = make_fixture(base, "capture-determinism")
    first = json.loads(capture(fixture, "determinism-a", "checkpoint").read_text(encoding="utf-8"))
    second = json.loads(capture(fixture, "determinism-b", "checkpoint").read_text(encoding="utf-8"))
    sections = ("saves", "mod_configs", "installed_probe", "bridge_policies")
    assert_true(first["oni_root_id"] == second["oni_root_id"], "root path id was not deterministic")
    path_ids: list[str] = []
    for section in sections:
        assert_true(first[section]["entries"] == second[section]["entries"], f"{section} evidence changed")
        path_ids.extend(entry["path_id"] for entry in first[section]["entries"])
    assert_true(len(path_ids) == len(set(path_ids)), "path identifiers were not unique")
    serialized = json.dumps(first, ensure_ascii=False)
    assert_true(str(fixture["case"]) not in serialized and "Original" not in serialized, "raw path/name leaked")
    return {"entry_count": len(path_ids), "unique_path_ids": len(set(path_ids)), "privacy": "passed"}


def test_capture_receipt_raw_identity_binding(base: pathlib.Path) -> dict[str, Any]:
    fixture = make_fixture(base, "capture-receipt")
    report, receipt = capture_with_receipt(fixture, "receipt", "checkpoint")
    receipt_keys = {
        "schema",
        "status",
        "report_path_id",
        "report_relative_path",
        "report_sha256",
        "report_byte_length",
        "report_identity",
        "report_parent_identity",
        "save_count",
        "mod_config_count",
        "probe_file_count",
        "bridge_policy_count",
        "queue_nonterminal_count",
        "oxygen_not_included_count",
        "bridge_count",
    }
    identity_keys = {"volume_serial_number", "file_index", "number_of_links"}
    parent_identity_keys = {"volume_serial_number", "file_index"}

    def path_id(path: pathlib.Path) -> str:
        normalized = os.path.abspath(os.path.normpath(str(path))).lower()
        return "sha256:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def matches(candidate: dict[str, Any]) -> bool:
        try:
            metadata = os.lstat(report)
            parent_metadata = os.lstat(report.parent)
            report_identity = candidate["report_identity"]
            parent_identity = candidate["report_parent_identity"]
            return bool(
                set(candidate) == receipt_keys
                and candidate["schema"]
                == "oni.live-safety-boundary-capture-receipt.v2"
                and candidate["status"] == "captured"
                and candidate["report_path_id"] == path_id(report)
                and str(candidate["report_relative_path"]).replace("\\", "/")
                == "receipt/checkpoint-boundary.json"
                and candidate["report_sha256"] == sha256(report)
                and candidate["report_byte_length"] == metadata.st_size
                and set(report_identity) == identity_keys
                and set(parent_identity) == parent_identity_keys
                and report_identity["file_index"] == metadata.st_ino
                and report_identity["number_of_links"] == metadata.st_nlink == 1
                and parent_identity["file_index"] == parent_metadata.st_ino
                and report_identity["volume_serial_number"]
                == parent_identity["volume_serial_number"]
                and not bool(
                    getattr(metadata, "st_file_attributes", 0)
                    & getattr(os.stat_result, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                )
                and not bool(
                    getattr(parent_metadata, "st_file_attributes", 0) & 0x400
                )
            )
        except (KeyError, OSError, TypeError, ValueError):
            return False

    assert_true(matches(receipt), "writer receipt did not bind exact raw bytes and identities")
    tampered = json.loads(json.dumps(receipt))
    tampered["report_sha256"] = "sha256:" + "0" * 64
    assert_true(not matches(tampered), "tampered raw report hash still matched the receipt")
    extra = json.loads(json.dumps(receipt))
    extra["unexpected"] = True
    assert_true(not matches(extra), "extra receipt field was accepted")

    owner = report.with_name("writer-owned-report.json")
    os.replace(report, owner)
    shutil.copy2(owner, report)
    assert_true(
        sha256(report) == receipt["report_sha256"] and not matches(receipt),
        "same-byte report name swap retained writer identity authorization",
    )
    report.unlink()
    os.replace(owner, report)

    alias = report.with_name("writer-report-hardlink.json")
    os.link(report, alias)
    assert_true(not matches(receipt), "hardlinked report still matched single-link receipt")
    alias.unlink()
    assert_true(matches(receipt), "receipt did not recover after removing the TestRoot alias")

    original_parent = report.parent
    parent_owner = original_parent.with_name("receipt-parent-owner")
    os.replace(original_parent, parent_owner)
    original_parent.mkdir()
    shutil.copy2(parent_owner / report.name, report)
    assert_true(
        sha256(report) == receipt["report_sha256"] and not matches(receipt),
        "same-byte report parent swap retained writer parent authorization",
    )
    return {
        "receipt_schema": receipt["schema"],
        "raw_sha256_and_length": "exact",
        "writer_file_and_parent_identity": "exact",
        "receipt_hash_tamper": "refused",
        "receipt_extra_key": "refused",
        "same_byte_name_swap": "identity_refused",
        "hardlink": "single_link_refused",
        "parent_swap": "identity_refused",
    }


def test_unmodified_and_changed(base: pathlib.Path) -> dict[str, Any]:
    stable = make_fixture(base, "unmodified")
    pre = capture(stable, "stable", "pre")
    post = capture(stable, "stable", "post")
    code, payload = compare(pre, post)
    assert_true(code == 0 and payload["status"] == "passed", "unmodified comparison did not pass")

    changed = make_fixture(base, "changed-original")
    pre_changed = capture(changed, "changed", "pre")
    write_bytes(changed["source"], b"changed-original-save")
    post_changed = capture(changed, "changed", "post")
    code, payload = compare(pre_changed, post_changed)
    assert_true(code == 2 and "preexisting_save_changed" in payload["blockers"], "changed original was not blocked")

    prefs = make_fixture(base, "changed-player-prefs")
    pre_prefs = capture(prefs, "prefs", "pre")
    write_bytes(prefs["oni"] / "kplayerprefs.yaml", b"SaveFilenameKey: Changed\n")
    post_prefs = capture(prefs, "prefs", "post")
    code, payload = compare(pre_prefs, post_prefs)
    assert_true(code == 2 and "mod_or_player_config_not_restored" in payload["blockers"], "kplayerprefs change was not blocked")
    return {"unmodified": "passed", "changed_original": "blocked", "changed_kplayerprefs": "blocked"}


def test_clone_pair_policy(base: pathlib.Path) -> dict[str, Any]:
    fixture = make_fixture(base, "clone-pair")
    pre = capture(fixture, "pair", "pre")
    create_clone(fixture, "clone-run", "CodexBoundaryTest")
    target_save, target_preview = clone_paths(fixture, "CodexBoundaryTest")
    manifest = fixture["runtime"] / "clone-run" / "test-clone-manifest.json"
    assert_true(target_save.is_file() and target_preview.is_file() and manifest.is_file(), "clone pair/manifest missing")
    post = capture(fixture, "pair", "post")
    code, payload = compare(pre, post, "CodexBoundaryTest")
    assert_true(code == 0 and payload["status"] == "passed", "allowed named clone pair did not pass")
    assert_true(str(fixture["case"]) not in manifest.read_text(encoding="utf-8"), "manifest leaked a raw path")

    incomplete = make_fixture(base, "incomplete-pair")
    pre_incomplete = capture(incomplete, "incomplete", "pre")
    write_bytes(incomplete["save_dir"] / "LonelyClone.sav", b"lonely")
    post_incomplete = capture(incomplete, "incomplete", "post")
    code, payload = compare(pre_incomplete, post_incomplete, "LonelyClone")
    assert_true(
        code == 2 and "allowed_save_pair_incomplete_or_ambiguous" in payload["blockers"],
        "incomplete allowed pair was not blocked",
    )

    ambiguous = make_fixture(base, "ambiguous-pair")
    pre_ambiguous = capture(ambiguous, "ambiguous", "pre")
    for directory in (
        ambiguous["save_dir"],
        ambiguous["oni"] / "cloud_save_files" / "OtherFixtureColony",
    ):
        write_bytes(directory / "RepeatedClone.sav", b"repeated-save")
        write_bytes(directory / "RepeatedClone.png", b"repeated-preview")
    post_ambiguous = capture(ambiguous, "ambiguous", "post")
    code, payload = compare(pre_ambiguous, post_ambiguous, "RepeatedClone")
    assert_true(
        code == 2 and "allowed_save_pair_incomplete_or_ambiguous" in payload["blockers"],
        "multiple pairs for one allowed stem were not blocked",
    )
    return {
        "complete_named_pair": "passed",
        "incomplete_pair": "blocked",
        "multiple_pairs_same_stem": "blocked",
    }


def test_whatif_restore_and_refusals(base: pathlib.Path) -> dict[str, Any]:
    whatif_create = make_fixture(base, "create-whatif")
    create_clone(whatif_create, "whatif-run", "WhatIfClone", what_if=True)
    assert_true(not clone_paths(whatif_create, "WhatIfClone")[0].parent.exists(), "create WhatIf wrote a clone folder")
    assert_true(not (whatif_create["runtime"] / "whatif-run").exists(), "create WhatIf wrote evidence")

    fixture = make_fixture(base, "restore")
    create_clone(fixture, "restore-run", "RestoreClone")
    target_save, target_preview = clone_paths(fixture, "RestoreClone")
    write_bytes(target_save, b"post-action-save")
    write_bytes(target_preview, b"post-action-preview")
    before = (sha256(target_save), sha256(target_preview))
    restore_clone(fixture, "restore-run", "RestoreClone", what_if=True)
    assert_true(before == (sha256(target_save), sha256(target_preview)), "restore WhatIf changed target")
    assert_true(not (fixture["runtime"] / "restore-run" / "post-action-clone").exists(), "restore WhatIf wrote backup")

    source_two = fixture["save_dir"] / "OtherSource.sav"
    preview_two = fixture["save_dir"] / "OtherSource.png"
    write_bytes(source_two, b"other-source")
    write_bytes(preview_two, b"other-preview")
    restore_clone(fixture, "restore-run", "RestoreClone", source=source_two, expected={1})
    assert_true(before == (sha256(target_save), sha256(target_preview)), "wrong source changed target")

    manifest = fixture["runtime"] / "restore-run" / "test-clone-manifest.json"
    manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
    manifest_payload["target"]["save_path_id"] = "sha256:" + "0" * 64
    write_json(manifest, manifest_payload)
    restore_clone(fixture, "restore-run", "RestoreClone", expected={1})
    assert_true(before == (sha256(target_save), sha256(target_preview)), "wrong manifest changed target")
    assert_true(not (fixture["runtime"] / "restore-run" / "post-action-clone").exists(), "refusal wrote backup")

    positive = make_fixture(base, "restore-positive")
    create_clone(positive, "positive-run", "PositiveClone")
    positive_save, positive_preview = clone_paths(positive, "PositiveClone")
    write_bytes(positive_save, b"post-action-positive-save")
    write_bytes(positive_preview, b"post-action-positive-preview")
    positive_before = (sha256(positive_save), sha256(positive_preview))
    restore_clone(positive, "positive-run", "PositiveClone")
    backup = positive["runtime"] / "positive-run" / "post-action-clone"
    assert_true(sha256(positive_save) == sha256(positive["source"]), "restore save mismatch")
    assert_true(sha256(positive_preview) == sha256(positive["preview"]), "restore preview mismatch")
    assert_true(
        sha256(backup / "target-before-restore.sav") == positive_before[0]
        and sha256(backup / "target-before-restore.png") == positive_before[1]
        and (backup / "restore-result.json").is_file(),
        "verified restore backup/evidence missing",
    )
    leftovers = [path for path in positive["case"].rglob("*.tmp")]
    assert_true(not leftovers, "temporary restore artifacts remained")
    return {
        "create_whatif": "no_mutation",
        "restore_whatif": "no_mutation",
        "wrong_source": "refused",
        "wrong_manifest": "refused",
        "verified_restore": "passed",
    }


def test_process_and_path_guards(base: pathlib.Path) -> dict[str, Any]:
    fixture = make_fixture(base, "guards")
    for process_fixture, stem, run_id in (
        ("game", "GameBlocked", "game-blocked"),
        ("bridge", "BridgeBlocked", "bridge-blocked"),
    ):
        create_clone(fixture, run_id, stem, process_fixture=process_fixture, expected={1})
        assert_true(not clone_paths(fixture, stem)[0].parent.exists(), "active-process guard allowed clone")
        assert_true(not (fixture["runtime"] / run_id).exists(), "active-process guard wrote evidence")

    create_clone(fixture, "guard-restore", "GuardRestore")
    target = clone_paths(fixture, "GuardRestore")[0]
    write_bytes(target, b"guard-restore-target")
    before = sha256(target)
    restore_clone(fixture, "guard-restore", "GuardRestore", process_fixture="game", expected={1})
    assert_true(sha256(target) == before, "restore process guard changed target")
    assert_true(not (fixture["runtime"] / "guard-restore" / "post-action-clone").exists(), "guard wrote backup")

    completed = ps(
        CAPTURE,
        "-RunId",
        "no-test-root",
        "-Phase",
        "checkpoint",
        "-OniUserRoot",
        str(fixture["oni"]),
        "-ReportRoot",
        str(fixture["boundaries"]),
        expected={1},
    )
    assert_true(completed.returncode != 0, "arbitrary live-root override was accepted")
    return {"game_guard": "passed", "bridge_guard": "passed", "live_override": "blocked"}


def listing(directory: pathlib.Path) -> list[str]:
    return sorted(str(path.relative_to(directory)) for path in directory.rglob("*"))


def lower_path_id(path: pathlib.Path) -> str:
    normalized = os.path.abspath(os.path.normpath(str(path))).lower()
    return "sha256:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def test_clone_colony_isolation(base: pathlib.Path) -> dict[str, Any]:
    """Review P0-1: a clone gets its own colony folder and autosaves are never cloned."""
    fixture = make_fixture(base, "isolation")
    source_listing = listing(fixture["save_dir"])
    create_clone(fixture, "isolated-run", "IsolatedClone")
    clone_save, clone_preview = clone_paths(fixture, "IsolatedClone")
    assert_true(clone_save.is_file() and clone_preview.is_file(), "clone pair is not in its own colony folder")
    assert_true(listing(clone_save.parent) == ["IsolatedClone.png", "IsolatedClone.sav"], "clone folder holds extra files")
    assert_true(listing(fixture["save_dir"]) == source_listing, "clone creation wrote into the source colony folder")
    manifest = json.loads(
        (fixture["runtime"] / "isolated-run" / "test-clone-manifest.json").read_text(encoding="utf-8-sig")
    )
    save_root = fixture["oni"] / "save_files"
    expected = {
        ("target", "isolation"): "own_colony_folder",
        ("target", "directory_path_id"): lower_path_id(clone_save.parent),
        ("target", "save_base_path_id"): lower_path_id(save_root),
        ("source", "colony_directory_path_id"): lower_path_id(fixture["save_dir"]),
        ("source", "redirect_colony_directory_path_id"): lower_path_id(save_root / FIXTURE_BASE_NAME),
    }
    for (branch, key), value in expected.items():
        assert_true(manifest[branch].get(key) == value, f"manifest {branch}.{key} is wrong")
    assert_true(manifest["source"]["header"]["is_auto_save"] is False, "manifest header flag is wrong")

    refusals: dict[str, str] = {}

    def refused(case: dict[str, pathlib.Path], label: str, run_id: str, stem: str) -> None:
        before = listing(case["oni"])
        create_clone(case, run_id, stem, expected={1})
        assert_true(listing(case["oni"]) == before, f"{label}: refused clone still wrote ONI data")
        assert_true(not (case["runtime"] / run_id).exists(), f"{label}: refused clone wrote evidence")
        refusals[label] = "refused_zero_write"

    header = make_fixture(base, "autosave-header")
    write_bytes(header["source"], oni_save_bytes(is_auto_save=True))
    refused(header, "autosave_header", "autosave-header", "HeaderClone")

    folder = make_fixture(base, "autosave-folder")
    autosave = folder["save_dir"] / "auto_save" / "Original Cycle 6.sav"
    write_bytes(autosave, oni_save_bytes())
    write_bytes(autosave.with_suffix(".png"), b"fixture-preview-v1\x89PNG")
    folder["source"] = autosave
    refused(folder, "auto_save_folder", "autosave-folder", "FolderClone")

    for label, content in {
        "unparseable_header": b"fixture-save-v1\x00\x01",
        "missing_flag": struct.pack("<IiIi", 744825, 2, 1, 1) + b"{}",
        "case_variant_flag": (
            lambda raw: struct.pack("<IiIi", 744825, len(raw), 1, 1) + raw
        )(b'{"baseName":"B","isAutoSave":false,"IsAutoSave":true}'),
    }.items():
        case = make_fixture(base, f"header-{label}")
        write_bytes(case["source"], content)
        refused(case, label, f"header-{label}", "HeaderClone")

    existing = make_fixture(base, "existing-folder")
    write_bytes(clone_paths(existing, "ExistingClone")[0].parent / "keep.txt", b"keep")
    refused(existing, "clone_folder_exists", "existing-folder", "ExistingClone")

    same = make_fixture(base, "same-folder")
    refused(same, "clone_folder_is_source_colony", "same-folder", "FixtureColony")
    redirect = make_fixture(base, "redirect-folder")
    refused(redirect, "clone_folder_is_redirect_colony", "redirect-folder", FIXTURE_BASE_NAME)

    flat = make_fixture(base, "flat-layout")
    flat_source = flat["oni"] / "save_files" / "Flat.sav"
    write_bytes(flat_source, oni_save_bytes())
    write_bytes(flat_source.with_suffix(".png"), b"fixture-preview-v1\x89PNG")
    flat["source"] = flat_source
    refused(flat, "source_not_in_colony_folder", "flat-layout", "FlatClone")

    cloud = make_fixture(base, "cloud-layout")
    cloud_dir = cloud["oni"] / "cloud_save_files" / "76561190000000000" / "CloudColony"
    write_bytes(cloud_dir / "Original.sav", oni_save_bytes())
    write_bytes(cloud_dir / "Original.png", b"fixture-preview-v1\x89PNG")
    cloud["source"], cloud["save_dir"] = cloud_dir / "Original.sav", cloud_dir
    create_clone(cloud, "cloud-run", "CloudClone")
    assert_true(clone_paths(cloud, "CloudClone")[0].is_file(), "cloud clone is not beside its source colony")

    legacy = make_fixture(base, "legacy-manifest")
    create_clone(legacy, "legacy-run", "LegacyClone")
    legacy_manifest = legacy["runtime"] / "legacy-run" / "test-clone-manifest.json"
    payload = json.loads(legacy_manifest.read_text(encoding="utf-8-sig"))
    del payload["target"]["isolation"]
    write_json(legacy_manifest, payload)
    legacy_save = clone_paths(legacy, "LegacyClone")[0]
    write_bytes(legacy_save, b"post-action-legacy")
    restore_clone(legacy, "legacy-run", "LegacyClone", expected={1})
    assert_true(legacy_save.read_bytes() == b"post-action-legacy", "legacy-layout manifest was restored")
    refusals["restore_without_isolation"] = "refused"
    return {"own_colony_folder": "passed", "cloud_layout": "passed", **refusals}


def sanitize_error(error: BaseException, fixture_root: pathlib.Path) -> str:
    text = str(error).replace(str(fixture_root), "<fixture_root>").replace(str(ROOT), "<workspace>")
    return text[:500]


def main() -> int:
    if not POWERSHELL:
        raise SystemExit("PowerShell is required")
    tests: list[tuple[str, Callable[[pathlib.Path], dict[str, Any]]]] = [
        ("syntax", test_syntax),
        ("capture_determinism_unique_ids_privacy", test_capture_determinism),
        ("capture_receipt_raw_identity_binding", test_capture_receipt_raw_identity_binding),
        ("exact_pre_post_comparison", test_unmodified_and_changed),
        ("named_clone_pair_policy", test_clone_pair_policy),
        ("whatif_restore_and_manifest_refusals", test_whatif_restore_and_refusals),
        ("process_and_live_path_guards", test_process_and_path_guards),
        ("clone_colony_isolation_and_autosave_refusals", test_clone_colony_isolation),
    ]
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="oni-live-boundary-") as temporary:
        fixture_root = pathlib.Path(temporary).resolve()
        for name, test in tests:
            try:
                evidence = test(fixture_root)
                results.append({"name": name, "status": "passed", "evidence": evidence})
            except BaseException as exc:  # report every independent offline case
                results.append(
                    {
                        "name": name,
                        "status": "failed",
                        "error": sanitize_error(exc, fixture_root),
                    }
                )

    passed = sum(result["status"] == "passed" for result in results)
    payload = {
        "schema": "oni.live-boundary-workflow-verification.v1",
        "status": "passed" if passed == len(results) else "failed",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "offline_temporary_fixtures_only",
        "live_oni_accessed": False,
        "test_count": len(results),
        "passed_count": passed,
        "failed_count": len(results) - passed,
        "scripts": {
            path.name: sha256(path)
            for path in (CAPTURE, COMPARE, CREATE, RESTORE, pathlib.Path(__file__).resolve())
        },
        "tests": results,
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    temporary_report = REPORT.with_name(f".{REPORT.name}.{os.getpid()}.tmp")
    temporary_report.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary_report, REPORT)
    print(json.dumps({"status": payload["status"], "passed": passed, "total": len(results)}))
    return 0 if payload["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
