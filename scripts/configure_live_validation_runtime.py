#!/usr/bin/env python3
"""Fail-closed, manifest-bound ONI live-validation runtime configuration.

The command is a read-only plan unless ``--apply`` is present.  Every apply,
including a TestRoot apply, also requires an exact ``--confirm-live-run-id``.
It never launches ONI or the bridge.  The environment finalizer remains the
owner of restoring the original configuration bytes captured by prepare.
"""

from __future__ import annotations

import argparse
import base64
import copy
import ctypes
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
ENVIRONMENT_SCHEMA = "oni.live-validation-environment.v1"
JOURNAL_SCHEMA = "oni.live-validation-restore-journal.v1"
JOURNAL_ENTRY_KEYS = frozenset(
    {
        "schema",
        "index",
        "recorded_at",
        "event",
        "data",
        "previous_entry_hash",
        "entry_hash",
    }
)
BOUNDARY_SCHEMA = "oni.live-safety-boundary.v1"
CLONE_SCHEMA = "oni.named-test-clone.v1"
BOOTSTRAP_SCHEMA = "oni.v27-runtime-bootstrap.v1"
POLICY_SCHEMA = "oni.action-policy.v1"
EVIDENCE_SCHEMA = "oni.live-validation-runtime-config.v5"
STARTUP_INTENT_SCHEMA = "oni.live-validation-startup-intent.v2"
STARTUP_BINDING_SCHEMA = "oni.runtime-startup-binding.v2"
RECOVERY_SCHEMA = "oni.live-validation-runtime-config-recovery.v1"
PLAN_SCHEMA = "oni.live-validation-runtime-config-plan.v5"
RESULT_SCHEMA = "oni.live-validation-runtime-config-result.v5"
VERIFY_RESULT_SCHEMA = "oni.live-validation-runtime-config-verification-result.v5"
ERROR_SCHEMA = "oni.live-validation-runtime-config-error.v1"
RUNTIME_BOOTSTRAP_RESULT_SCHEMA = "oni.live-runtime-bootstrap-result.v1"
RUNTIME_BOOTSTRAP_RESULT_CANDIDATE_SCHEMA = (
    "oni.live-runtime-bootstrap-result-candidate.v1"
)
RUNTIME_BOOTSTRAP_JOURNAL_SCHEMA = "oni.live-runtime-bootstrap-journal-entry.v1"
RUNTIME_BOOTSTRAP_INSTALL_STAGE_SCHEMA = (
    "oni.live-runtime-bootstrap-install-stage.v1"
)
RUNTIME_BOOTSTRAP_INTENT_SCHEMA = "oni.live-runtime-bootstrap-intent.v1"
RUNTIME_BOOTSTRAP_PLAN_SCHEMA = "oni.live-runtime-bootstrap-plan.v1"
RUNTIME_BOOTSTRAP_LOCK_CLAIM_SCHEMA = (
    "oni.live-runtime-bootstrap-environment-lock-claim.v1"
)
RUNTIME_BOOTSTRAP_LOCK_RELEASE_SCHEMA = (
    "oni.live-runtime-bootstrap-environment-lock-release.v1"
)

EXPECTED_MOD_VERSION = "0.7.0"
EXPECTED_GAME_BUILD = "cl-744825"
AUTO_RESUME_STARTUP_SEMANTICS = "one_shot_nonempty_exact_loader"
PROBE_STATIC_ID = "ass.OniAiProbe"
PROBE_RELATIVE = pathlib.PurePosixPath("mods/Dev/oni-ai-colony-agent")
BRIDGE_RELATIVE = pathlib.PurePosixPath("ai-bridge")
RUNTIME_BOOTSTRAP_RELATIVE = pathlib.PurePosixPath("runtime-bootstrap")
RUNTIME_BOOTSTRAP_ARTIFACT_NAMES = (
    "OniAiProbeMod.dll",
    "OniAiProbeMod.pdb",
    "mod.yaml",
    "mod_info.yaml",
)
CONFIG_RELATIVES = (
    "kplayerprefs.yaml",
    "mods/mods.json",
    "mods/mods.json.bak",
    "mods/config.json",
)
TARGET_CONFIG_RELATIVES = ("kplayerprefs.yaml", "mods/mods.json")
EVIDENCE_NAME = "runtime-config-evidence.json"
STARTUP_BINDING_NAME = "runtime-startup-binding.json"
LOCK_NAME = ".finalize.lock"
LIVE_RELEASE_DLL = PROJECT_ROOT / "mod" / "OniAiProbeMod.dll"
LIVE_GAME_MANAGED_DIR = pathlib.Path(
    r"C:\Program Files (x86)\Steam\steamapps\common\OxygenNotIncluded"
    r"\OxygenNotIncluded_Data\Managed"
)
NONTERMINAL_QUEUES = ("pending", "approving", "approved", "executing", "confirming")
NONTERMINAL_VALIDATION_CONTROL_QUEUES = ("approved", "inflight")
RUNTIME_DYNAMIC_BRIDGE_ROOT_FILES = frozenset(
    {
        "action-outcomes.json",
        "action-outcomes-commit.json",
        "advice.json",
        "advice.md",
        "colony-plan.json",
        "decision-frame.json",
        "evidence-continuity.json",
        "health.json",
        "incidents.json",
        "latest_state.json",
        "llm-health.json",
        "llm-state.json",
        "plan.json",
        "state.jsonl",
        "state_summary.md",
        "strategy.json",
        "strategy.md",
    }
)
RUNTIME_DYNAMIC_BRIDGE_ROOT_DIRECTORIES = frozenset(
    {
        "colony-plans",
        "confirmed-command-evidence",
        "decision-frames",
        "sessions",
    }
)
AUTO_RESUME_KEY = "AutoResumeSaveFile"
SAVE_FILENAME_KEY = "SaveFilenameKey/"
MANAGED_ASSEMBLY_NAMES = (
    "Assembly-CSharp.dll",
    "Assembly-CSharp-firstpass.dll",
)
LIVE_MANAGED_ASSEMBLY_SHA256 = {
    "Assembly-CSharp.dll": "sha256:a58e04d0ffdf89b86fb28b71ad900625b3b539db30d67f8c6269f73a9f5ae599",
    "Assembly-CSharp-firstpass.dll": "sha256:72d21f51cda136443bfa1a2f6eadb08167a4c508c3d03f558b8cdb60160f249c",
}
BUILT_IN_AUTO_RESUME_SEMANTICS = {
    "game_changelist": 744825,
    "expected_probe_game_build": "cl-744825",
    "game_build_audit": {
        "source": "ecma335_constant_metadata",
        "type": "KleiVersion",
        "field": "ChangeList",
        "constant_type": "UInt32",
        "value": 744825,
        "expected_probe_game_build": "cl-744825",
        "bound_assembly_sha256": LIVE_MANAGED_ASSEMBLY_SHA256["Assembly-CSharp.dll"],
    },
    "consumer_type": "MainMenu",
    "startup_symbol": "MainMenu.OnSpawn",
    "consumption_symbol": "MainMenu.ResumeGame",
    "preference_key": AUTO_RESUME_KEY,
    "distribution_platform_steam_initialization_completes_before_main_menu_spawn": True,
    "main_menu_on_spawn_before_auto_resume": [
        "InitLoadScreen",
        "ShowMigrationIfNecessary",
        "save_directory_write_probe",
        "modManager.Report",
        "modManager.SetModLoadingInProgress(false)",
        "safe_mode_dialog",
    ],
    "initial_load_screen_activation_and_refresh_colony_list_side_effects_precede_auto_resume": True,
    "auto_resume_only_avoids_second_user_load_screen_activation": True,
    "auto_resume_does_not_bypass_initial_activation_or_refresh_colony_list": True,
    "consumption_order": [
        "KPlayerPrefs.HasKey",
        "KPlayerPrefs.GetString",
        "KPlayerPrefs.DeleteKey",
        "string.IsNullOrEmpty",
        "SaveLoader.SetActiveSaveFilePath",
        "KPlayerPrefs.SetString(SaveFilenameKey/)",
        "KPlayerPrefs.Save",
        "LoadingOverlay.Load(SaveLoader.LoadScene)",
    ],
    "has_key_triggers_regardless_of_stored_type_or_value": True,
    "delete_key_persistence": "memory_only_until_KPlayerPrefs.Save",
    "nonempty_value_persists_active_pointer_and_deletion_before_load": True,
    "empty_value_has_no_immediate_persistence": True,
    "invalid_path_or_load_failure_restores_deleted_key": False,
    "persistence_writer": "YamlIO direct StreamWriter overwrite",
    "persistence_write_is_atomic_or_fsynced": False,
    "persistence_errors_propagate_to_caller": False,
}
BOOTSTRAP_KEYS = frozenset(
    {
        "schema",
        "run_id",
        "prepared_at",
        "expected_mod_version",
        "bridge_dir",
        "archived_previous_bridge",
        "built_dll_sha256",
        "installed_dll_sha256",
        "installed_release",
        "action_policy_disabled",
    }
)
POLICY_KEYS = frozenset(
    {
        "schema",
        "actions_enabled",
        "suggestions_enabled",
        "allowed_save_hashes",
        "allowed_actions",
        "command_ttl_seconds",
        "max_pending_commands",
        "generated_at",
    }
)
EVIDENCE_KEYS = frozenset(
    {
        "schema",
        "run_id",
        "mode",
        "configured_at",
        "transaction_id",
        "environment_manifest_sha256",
        "bindings",
        "runtime",
        "configs",
        "loader_policy",
        "startup_intent",
        "mod_policy",
        "restoration",
        "document_hash",
    }
)
STARTUP_INTENT_KEYS = frozenset(
    {
        "schema",
        "validation_run_id",
        "startup_nonce",
        "environment_manifest_sha256",
        "requested_loader_path_id_lower",
        "requested_loader_path_id_upper",
        "requested_loader_save_sha256",
        "requested_loader_save_length",
        "expected_game_build",
        "expected_mod_version",
        "expected_probe_dll_sha256",
        "runtime_bootstrap_result_path_id_lower",
        "runtime_bootstrap_result_sha256",
        "runtime_bootstrap_result_hash",
        "runtime_bootstrap_transaction_id",
        "installed_probe_tree_content_hash",
        "auto_resume_semantics",
        "document_hash",
    }
)
STARTUP_BINDING_KEYS = frozenset(
    {
        "schema",
        "validation_run_id",
        "startup_nonce",
        "startup_intent_hash",
        "runtime_config_evidence_sha256",
        "environment_manifest_sha256",
        "expected_game_build",
        "expected_mod_version",
        "expected_probe_dll_sha256",
        "runtime_bootstrap_result_path_id_lower",
        "runtime_bootstrap_result_sha256",
        "runtime_bootstrap_result_hash",
        "runtime_bootstrap_transaction_id",
        "installed_probe_tree_content_hash",
        "requested_loader_path_id_upper",
        "requested_loader_save_sha256",
        "requested_loader_save_length",
        "created_at",
        "document_hash",
    }
)
RUNTIME_EVIDENCE_KEYS = frozenset(
    {
        "bootstrap",
        "runtime_bootstrap_result",
        "release_dll",
        "installed_probe",
        "initial_disabled_policy",
        "mod_version",
        "probe_static_id",
        "dll_four_way_identical",
    }
)
BOOTSTRAP_EVIDENCE_KEYS = frozenset(
    {
        "path_id_lower",
        "raw_sha256",
        "raw_byte_length",
        "raw_utf8_base64",
        "document_hash",
        "document",
    }
)
POLICY_EVIDENCE_KEYS = BOOTSTRAP_EVIDENCE_KEYS
RELEASE_EVIDENCE_KEYS = frozenset(
    {"path_id_lower", "external_expected_sha256", "file_sha256"}
)
INSTALLED_PROBE_EVIDENCE_KEYS = frozenset(
    {
        "dll_sha256",
        "pdb_sha256",
        "manifest_sha256",
        "info_sha256",
        "tree_content_hash",
    }
)
RUNTIME_BOOTSTRAP_RESULT_EVIDENCE_KEYS = frozenset(
    {
        "path_id_lower",
        "external_expected_sha256",
        "raw_sha256",
        "raw_byte_length",
        "raw_utf8_base64",
        "result_hash",
        "transaction_id",
        "probe_tree_content_hash",
        "document",
    }
)
RUNTIME_BOOTSTRAP_RESULT_KEYS = frozenset(
    {
        "schema",
        "status",
        "classification",
        "run_id",
        "transaction_id",
        "plan_hash",
        "environment_manifest_sha256",
        "release_dll_sha256",
        "expected_mod_version",
        "probe_tree_content_hash",
        "installed_probe_artifacts",
        "installed_probe_tree_identity",
        "bridge_tree_content_hash",
        "bootstrap_document_hash",
        "bootstrap_file_sha256",
        "archive_relative",
        "old_probe_recovery_relative",
        "install_commit_entry",
        "install_commit_entry_hash",
        "install_stage_sha256",
        "install_stage_hash",
        "producer",
        "install_lock_epoch",
        "install_lock_claim",
        "install_lock_claim_hash",
        "install_lock_release",
        "install_lock_release_hash",
        "ready_lock_epoch",
        "ready_lock_claim",
        "ready_lock_claim_hash",
        "ready_lock_release",
        "ready_lock_release_hash",
        "candidate_sha256",
        "candidate_hash",
        "physical_commit_complete",
        "shared_lock_released",
        "game_launched",
        "result_hash",
    }
)

RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
TRANSACTION_ID_RE = re.compile(r"^tx-[0-9a-f]{32}$")
RUNTIME_BOOTSTRAP_TRANSACTION_ID_RE = re.compile(r"^rtb-[0-9a-f]{24}$")
STARTUP_NONCE_RE = re.compile(r"^startup-[0-9a-f]{32}$")
HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
NUMBER_RE = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
RESERVED_WINDOWS_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{value}" for value in range(1, 10)),
    *(f"lpt{value}" for value in range(1, 10)),
}
MAX_JSON_BYTES = 32 * 1024 * 1024
MAX_MANAGED_ASSEMBLY_BYTES = 64 * 1024 * 1024
REPARSE_ATTRIBUTE = 0x400

# Offline verification may install a hook only while --test-root is active.
_TEST_HOOK: Optional[Callable[[str, "RuntimeContext"], None]] = None
_TEST_FAULTS: set[str] = set()
_TEST_PUBLICATION_PATHS: Optional[tuple[pathlib.Path, pathlib.Path]] = None
_TEST_STAGED_PATH: Optional[pathlib.Path] = None
_TEST_ATOMIC_UNLINK_PATH: Optional[pathlib.Path] = None
_TEST_MANAGED_ASSEMBLY_HOOK: Optional[Callable[[str, pathlib.Path], None]] = None


class ConfigError(RuntimeError):
    """A fail-closed validation or transaction error."""


class OutcomeUnknownError(ConfigError):
    """A mutation outcome cannot be safely normalized automatically."""


class PublicationError(ConfigError):
    """Create-only publication failed with an explicit ownership outcome."""

    def __init__(self, message: str, *, outcome: str) -> None:
        super().__init__(message)
        self.outcome = outcome


class LockOwnershipError(OutcomeUnknownError):
    """The shared environment lock name no longer denotes our descriptor."""


class LockFinalVerificationError(ConfigError):
    """A post-body lock-release checkpoint invalidated the transaction."""


class MutationOwnershipError(OutcomeUnknownError):
    """A config mutation preserved externally changed state and/or a recovery claim."""

    def __init__(
        self,
        message: str,
        *,
        relative: str,
        recovery_claims: Iterable[pathlib.Path] = (),
    ) -> None:
        super().__init__(message)
        self.relative = relative
        self.recovery_claims = tuple(recovery_claims)


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds")


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_typed_equal(left: Any, right: Any) -> bool:
    """Compare JSON values without Python's bool/int equality coercion."""

    try:
        return canonical_bytes(left) == canonical_bytes(right)
    except (TypeError, ValueError):
        return False


def json_document_bytes(value: Mapping[str, Any]) -> bytes:
    """Render the one accepted create-only JSON representation."""

    return json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8") + b"\n"


def self_hash_document(value: Mapping[str, Any]) -> dict[str, Any]:
    document = dict(value)
    if "document_hash" in document:
        raise ConfigError("self-hashed document input already contains document_hash")
    document["document_hash"] = sha256_bytes(canonical_bytes(document))
    return document


def require_canonical_utc_timestamp(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{label} is not a canonical UTC timestamp")
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError as exc:
        raise ConfigError(f"{label} is not a canonical UTC timestamp") from exc
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() != dt.timedelta(0)
        or parsed.isoformat(timespec="microseconds") != value
    ):
        raise ConfigError(f"{label} is not a canonical UTC timestamp")
    return value


def sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def normalized(path: pathlib.Path | str) -> pathlib.Path:
    return pathlib.Path(os.path.abspath(os.path.normpath(str(pathlib.Path(path).expanduser()))))


def protected_documents_root() -> pathlib.Path:
    """Resolve Documents without trusting USERPROFILE on Windows."""

    if os.name != "nt":
        return normalized(pathlib.Path.home() / "Documents")
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    get_folder = shell32.SHGetFolderPathW
    get_folder.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_wchar_p,
    ]
    get_folder.restype = ctypes.c_long
    buffer = ctypes.create_unicode_buffer(32768)
    result = int(get_folder(None, 0x0005, None, 0, buffer))  # CSIDL_PERSONAL
    if result != 0 or not buffer.value:
        raise ConfigError("Windows Documents known folder cannot be resolved safely")
    return require_absolute_local_path(buffer.value, "Windows Documents known folder")


def require_absolute_local_path(value: pathlib.Path | str, label: str) -> pathlib.Path:
    raw = str(value)
    candidate = pathlib.Path(raw)
    if (
        not raw
        or raw.startswith(("\\\\", "//"))
        or not candidate.is_absolute()
        or "\x00" in raw
    ):
        raise ConfigError(f"{label} must be an absolute non-UNC local path")
    return normalized(candidate)


def canonical_path_text(path: pathlib.Path | str) -> str:
    return os.path.normcase(str(normalized(path)).rstrip("\\/"))


def same_path(left: pathlib.Path | str, right: pathlib.Path | str) -> bool:
    return canonical_path_text(left) == canonical_path_text(right)


def path_within(path: pathlib.Path | str, root: pathlib.Path | str, *, strict: bool = True) -> bool:
    candidate = canonical_path_text(path)
    boundary = canonical_path_text(root)
    try:
        common = os.path.commonpath((candidate, boundary))
    except ValueError:
        return False
    return common == boundary and (not strict or candidate != boundary)


def paths_overlap(left: pathlib.Path | str, right: pathlib.Path | str) -> bool:
    return path_within(left, right, strict=False) or path_within(right, left, strict=False)


def path_id(path: pathlib.Path | str, *, contract: str) -> str:
    text = str(normalized(path)).rstrip("\\/")
    if contract == "lower":
        text = text.lower()
    elif contract == "upper":
        text = text.upper()
    else:
        raise ConfigError("unsupported path-id contract")
    return sha256_bytes(text.encode("utf-8"))


def text_id(value: str) -> str:
    return sha256_bytes(value.lower().encode("utf-8"))


def require_hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or HASH_RE.fullmatch(value) is None:
        raise ConfigError(f"{label} is not a canonical SHA-256 identifier")
    return value


def validate_windows_segment(value: str, label: str) -> None:
    if (
        not value
        or value in {".", ".."}
        or value.endswith((" ", "."))
        or ":" in value
        or "\x00" in value
        or value.split(".", 1)[0].lower() in RESERVED_WINDOWS_NAMES
    ):
        raise ConfigError(f"{label} contains an unsafe Windows path segment")


def validate_relative(value: str, label: str) -> pathlib.PurePosixPath:
    relative = pathlib.PurePosixPath(value.replace("\\", "/"))
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ConfigError(f"{label} is not a safe relative path")
    for part in relative.parts:
        validate_windows_segment(part, label)
    return relative


def is_reparse(path: pathlib.Path) -> bool:
    metadata = os.lstat(path)
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & REPARSE_ATTRIBUTE
    )


def assert_no_reparse(
    path: pathlib.Path,
    boundary: pathlib.Path,
    *,
    allow_missing_leaf: bool = False,
) -> None:
    path = normalized(path)
    boundary = normalized(boundary)
    if not path_within(path, boundary, strict=False):
        raise ConfigError("path escaped its declared safety boundary")
    if not boundary.exists() or not boundary.is_dir() or is_reparse(boundary):
        raise ConfigError("safety boundary is missing or is a reparse point")
    relative = pathlib.Path(os.path.relpath(path, boundary))
    current = boundary
    for index, part in enumerate(relative.parts):
        if part == ".":
            continue
        validate_windows_segment(part, "path")
        current = current / part
        if os.path.lexists(current):
            if is_reparse(current):
                raise ConfigError("reparse points are forbidden in workflow paths")
        elif not (allow_missing_leaf and index == len(relative.parts) - 1):
            raise ConfigError("required workflow path is missing")


def assert_safe_root(path: pathlib.Path, label: str) -> pathlib.Path:
    root = require_absolute_local_path(path, label)
    if root.parent == root:
        raise ConfigError(f"{label} cannot be UNC or a filesystem root")
    if not root.exists() or not root.is_dir():
        raise ConfigError(f"{label} must be an existing directory")
    current = pathlib.Path(root.anchor)
    for part in root.parts[1:]:
        current = current / part
        if os.path.lexists(current) and is_reparse(current):
            raise ConfigError(f"{label} ancestry contains a reparse point")
    assert_no_reparse(root, root)
    return root


def same_volume(*paths: pathlib.Path) -> bool:
    drives = {os.path.splitdrive(str(normalized(path)))[0].casefold() for path in paths}
    return len(drives) == 1


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError("JSON contains a duplicate object key")
        result[key] = value
    return result


def metadata_identity(metadata: os.stat_result) -> tuple[int, int]:
    return int(metadata.st_dev), int(metadata.st_ino)


def identity_id(identity: tuple[int, int]) -> str:
    return sha256_bytes(f"{identity[0]}:{identity[1]}".encode("ascii"))


def read_descriptor_all(descriptor: int, maximum: int = MAX_JSON_BYTES) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(descriptor, min(1024 * 1024, maximum + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > maximum:
            raise ConfigError("regular file exceeds the bounded read limit")
    return b"".join(chunks)


def read_snapshot(
    path: pathlib.Path,
    *,
    maximum: int = MAX_JSON_BYTES,
    expected_links: Optional[int] = 1,
) -> tuple[bytes, dict[str, Any]]:
    path = normalized(path)
    try:
        named_before = os.lstat(path)
    except OSError as exc:
        raise ConfigError("required regular file is missing") from exc
    if is_reparse(path) or not stat.S_ISREG(named_before.st_mode) or named_before.st_size > maximum:
        raise ConfigError("file is not a bounded regular non-reparse file")
    if expected_links is not None and named_before.st_nlink != expected_links:
        raise ConfigError("file does not have the required exclusive link count")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        descriptor_before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(descriptor_before.st_mode)
            or metadata_identity(descriptor_before) != metadata_identity(named_before)
            or (
                expected_links is not None
                and descriptor_before.st_nlink != expected_links
            )
        ):
            raise ConfigError("file name changed while its descriptor was opened")
        payload = read_descriptor_all(descriptor, maximum)
        descriptor_after = os.fstat(descriptor)
        named_after = os.lstat(path)
        if (
            metadata_identity(descriptor_before) != metadata_identity(descriptor_after)
            or descriptor_before.st_size != descriptor_after.st_size
            or descriptor_before.st_mtime_ns != descriptor_after.st_mtime_ns
            or is_reparse(path)
            or metadata_identity(named_after) != metadata_identity(descriptor_after)
            or named_after.st_size != descriptor_after.st_size
            or named_after.st_mtime_ns != descriptor_after.st_mtime_ns
            or (
                expected_links is not None
                and (
                    descriptor_after.st_nlink != expected_links
                    or named_after.st_nlink != expected_links
                )
            )
        ):
            raise ConfigError("file identity changed while evidence was captured")
        if len(payload) != descriptor_after.st_size:
            raise ConfigError("descriptor read length changed unexpectedly")
        return payload, {
            "length": descriptor_after.st_size,
            "last_write_ns": descriptor_after.st_mtime_ns,
            "sha256": sha256_bytes(payload),
        }
    finally:
        os.close(descriptor)


def open_read_shared_delete(path: pathlib.Path) -> int:
    """Open read-only while permitting a hostile rename that identity checks detect."""

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if os.name != "nt":
        return os.open(path, flags)
    import msvcrt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p
    handle = create_file(
        str(path),
        0x80000000,  # GENERIC_READ
        0x1 | 0x4,  # share read/delete; deny external byte writers
        None,
        3,  # OPEN_EXISTING
        0x00200000,  # FILE_FLAG_OPEN_REPARSE_POINT
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle == invalid:
        raise OSError(ctypes.get_last_error(), "managed assembly open failed")
    try:
        return msvcrt.open_osfhandle(int(handle), flags)
    except Exception:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        raise


def parse_json_payload(payload: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            payload.decode("utf-8-sig"),
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=lambda item: (_ for _ in ()).throw(
                ConfigError(f"non-finite JSON constant: {item}")
            ),
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ConfigError(f"{label} root must be an object")
    return value


def load_json(path: pathlib.Path, label: str) -> dict[str, Any]:
    payload, _state = read_snapshot(path)
    return parse_json_payload(payload, label)


def file_state(path: pathlib.Path) -> dict[str, Any]:
    _payload, state_value = read_snapshot(path)
    return state_value


def optional_state(path: pathlib.Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False}
    return {"exists": True, "state": file_state(path)}


@dataclass(frozen=True)
class Roots:
    run_id: str
    oni_root: pathlib.Path
    evidence_root: pathlib.Path
    boundary_root: pathlib.Path
    expected_environment_manifest_sha256: str
    runtime_bootstrap_result: pathlib.Path
    expected_runtime_bootstrap_result_sha256: str
    release_dll: pathlib.Path
    expected_release_dll_sha256: str
    game_managed_dir: pathlib.Path
    managed_assembly_expected_sha256: tuple[tuple[str, str], ...]
    test_root: Optional[pathlib.Path]
    live_mode: bool

    @property
    def environment_dir(self) -> pathlib.Path:
        return self.evidence_root / self.run_id / "environment"

    @property
    def runtime_bootstrap_dir(self) -> pathlib.Path:
        return self.evidence_root / self.run_id / pathlib.Path(
            *RUNTIME_BOOTSTRAP_RELATIVE.parts
        )


@dataclass(frozen=True)
class ActiveParentFinalizeLock:
    """In-process proof for the environment finalizer's already-open lock."""

    fd: int
    identity: tuple[int, int, int, int]
    payload: bytes
    payload_sha256: str
    owner_token: str


def full_file_identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(stat.S_IFMT(metadata.st_mode)),
        int(metadata.st_nlink),
    )


def require_current_active_parent_finalize_lock(
    roots: Roots,
    proof: ActiveParentFinalizeLock,
) -> None:
    if type(proof) is not ActiveParentFinalizeLock:
        raise ConfigError("active parent finalize lock proof type is invalid")
    if type(proof.fd) is not int or proof.fd < 0:
        raise ConfigError("active parent finalize lock descriptor is invalid")
    if (
        type(proof.identity) is not tuple
        or len(proof.identity) != 4
        or any(type(item) is not int or item < 0 for item in proof.identity)
        or proof.identity[2] != stat.S_IFREG
        or proof.identity[3] != 1
    ):
        raise ConfigError("active parent finalize lock identity is invalid")
    if type(proof.payload) is not bytes or not proof.payload:
        raise ConfigError("active parent finalize lock payload is invalid")
    payload_sha256 = require_hash(
        proof.payload_sha256,
        "active parent finalize lock payload hash",
    )
    if (
        sha256_bytes(proof.payload) != payload_sha256
        or not isinstance(proof.owner_token, str)
        or re.fullmatch(r"[0-9a-f]{32}", proof.owner_token) is None
    ):
        raise ConfigError("active parent finalize lock payload proof is invalid")
    document = parse_json_payload(proof.payload, "active parent finalize lock")
    if (
        set(document) != {"schema", "run_id", "owner_token"}
        or document.get("schema") != "oni.live-validation-finalize-lock.v1"
        or document.get("run_id") != roots.run_id
        or document.get("owner_token") != proof.owner_token
        or proof.payload != canonical_bytes(document) + b"\n"
    ):
        raise ConfigError("active parent finalize lock document is not exact")

    try:
        descriptor_before = os.fstat(proof.fd)
        original_offset = os.lseek(proof.fd, 0, os.SEEK_CUR)
        try:
            descriptor_payload = read_descriptor_all(proof.fd)
        finally:
            os.lseek(proof.fd, original_offset, os.SEEK_SET)
        descriptor_after = os.fstat(proof.fd)
    except (OSError, ValueError) as exc:
        raise ConfigError("active parent finalize lock descriptor is unavailable") from exc
    if (
        not stat.S_ISREG(descriptor_before.st_mode)
        or descriptor_before.st_nlink != 1
        or full_file_identity(descriptor_before) != proof.identity
        or full_file_identity(descriptor_after) != proof.identity
        or descriptor_before.st_size != descriptor_after.st_size
        or descriptor_before.st_mtime_ns != descriptor_after.st_mtime_ns
        or descriptor_payload != proof.payload
    ):
        raise ConfigError("active parent finalize lock descriptor custody changed")

    lock_path = roots.environment_dir / LOCK_NAME
    try:
        named_before = os.lstat(lock_path)
        if is_reparse(lock_path):
            raise ConfigError("active parent finalize lock path is a reparse point")
        descriptor = os.open(lock_path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        try:
            opened_before = os.fstat(descriptor)
            named_payload = read_descriptor_all(descriptor)
            opened_after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        named_after = os.lstat(lock_path)
    except ConfigError:
        raise
    except OSError as exc:
        raise ConfigError("active parent finalize lock path is unavailable") from exc
    if (
        is_reparse(lock_path)
        or full_file_identity(named_before) != proof.identity
        or full_file_identity(opened_before) != proof.identity
        or full_file_identity(opened_after) != proof.identity
        or full_file_identity(named_after) != proof.identity
        or named_before.st_size != opened_before.st_size
        or opened_before.st_size != opened_after.st_size
        or opened_after.st_size != named_after.st_size
        or named_before.st_mtime_ns != opened_before.st_mtime_ns
        or opened_before.st_mtime_ns != opened_after.st_mtime_ns
        or opened_after.st_mtime_ns != named_after.st_mtime_ns
        or named_before.st_size != named_after.st_size
        or named_before.st_mtime_ns != named_after.st_mtime_ns
        or named_payload != proof.payload
        or sha256_bytes(named_payload) != payload_sha256
    ):
        raise ConfigError("active parent finalize lock named-path custody changed")


@dataclass
class ImmutableFileBinding:
    path: pathlib.Path
    boundary: pathlib.Path
    state: dict[str, Any]
    identity: tuple[int, int]
    label: str


@dataclass
class ImmutableDirectoryBinding:
    path: pathlib.Path
    boundary: pathlib.Path
    identity: tuple[int, int]
    label: str


@dataclass
class RuntimeContext:
    roots: Roots
    phase: str
    manifest_path: pathlib.Path
    manifest: dict[str, Any]
    manifest_sha256: str
    source_save: pathlib.Path
    source_preview: pathlib.Path
    loader_save: pathlib.Path
    loader_preview: pathlib.Path
    source_save_identity: Optional[tuple[int, int]]
    source_preview_identity: Optional[tuple[int, int]]
    loader_save_identity: Optional[tuple[int, int]]
    loader_preview_identity: Optional[tuple[int, int]]
    immutable_environment_files: tuple[ImmutableFileBinding, ...]
    immutable_runtime_directories: tuple[ImmutableDirectoryBinding, ...]
    loader_stem: str
    config_records: dict[str, dict[str, Any]]
    backup_bytes: dict[str, bytes]
    current_bytes: dict[str, bytes]
    current_states: dict[str, dict[str, Any]]
    current_identities: dict[str, tuple[int, int]]
    bootstrap_path: pathlib.Path
    bootstrap: dict[str, Any]
    policy_path: pathlib.Path
    probe_dll: pathlib.Path
    bridge_identity: tuple[int, int]
    probe_identity: tuple[int, int]
    runtime_bindings: dict[str, Any]
    managed_assembly_binding: dict[str, Any]
    active_config_lock_identity: Optional[tuple[int, int]] = None
    active_parent_finalize_lock: Optional[ActiveParentFinalizeLock] = None

    @property
    def evidence_path(self) -> pathlib.Path:
        return self.roots.environment_dir / EVIDENCE_NAME

    @property
    def startup_binding_path(self) -> pathlib.Path:
        return (
            self.roots.oni_root
            / pathlib.Path(*BRIDGE_RELATIVE.parts)
            / STARTUP_BINDING_NAME
        )


def capture_bound_regular_identity(
    path: pathlib.Path,
    boundary: pathlib.Path,
    expected_state: Mapping[str, Any],
    label: str,
) -> tuple[int, int]:
    assert_no_reparse(path, boundary)
    payload, state = read_snapshot(path)
    metadata = os.lstat(path)
    if (
        state != expected_state
        or sha256_bytes(payload) != expected_state.get("sha256")
        or is_reparse(path)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size != state["length"]
        or metadata.st_mtime_ns != state["last_write_ns"]
    ):
        raise ConfigError(f"{label} identity/content binding is invalid")
    return metadata_identity(metadata)


def require_current_source_binding(ctx: RuntimeContext) -> None:
    manifest_bindings = ctx.manifest["bindings"]
    for path, identity, key, label in (
        (
            ctx.source_save,
            ctx.source_save_identity,
            "source_save",
            "source save",
        ),
        (
            ctx.source_preview,
            ctx.source_preview_identity,
            "source_preview",
            "source preview",
        ),
    ):
        if identity is None:
            raise ConfigError("source identity was not captured for the transaction")
        current_identity = capture_bound_regular_identity(
            path,
            save_root_for(path, ctx.roots.oni_root),
            manifest_bindings[key]["state"],
            label,
        )
        if current_identity != identity:
            raise ConfigError(f"{label} identity changed after environment validation")


def require_current_save_clone_binding(ctx: RuntimeContext) -> None:
    require_current_source_binding(ctx)
    manifest_bindings = ctx.manifest["bindings"]
    for path, identity, key, label in (
        (
            ctx.loader_save,
            ctx.loader_save_identity,
            "loader_save",
            "loader save",
        ),
        (
            ctx.loader_preview,
            ctx.loader_preview_identity,
            "loader_preview",
            "loader preview",
        ),
    ):
        if identity is None:
            raise ConfigError("loader identity was not captured for the apply transaction")
        current_identity = capture_bound_regular_identity(
            path,
            save_root_for(path, ctx.roots.oni_root),
            manifest_bindings[key]["state"],
            label,
        )
        if current_identity != identity:
            raise ConfigError(f"{label} identity changed after environment validation")


def require_current_environment_manifest_anchor(ctx: RuntimeContext) -> None:
    """Re-read the manifest against the caller-supplied, immutable trust anchor."""

    assert_no_reparse(ctx.manifest_path, ctx.roots.evidence_root)
    payload, current_state = read_snapshot(ctx.manifest_path)
    expected = ctx.roots.expected_environment_manifest_sha256
    if (
        current_state.get("sha256") != expected
        or sha256_bytes(payload) != expected
        or ctx.manifest_sha256 != expected
    ):
        raise ConfigError(
            "environment manifest does not match its external SHA-256 anchor"
        )
    require_current_runtime_bootstrap_result_anchor(ctx)


def require_current_runtime_bootstrap_result_anchor(ctx: RuntimeContext) -> None:
    """Re-read the installed-ready certificate against its caller anchor."""

    result_path = ctx.roots.runtime_bootstrap_result
    assert_no_reparse(result_path, ctx.roots.evidence_root)
    payload, current_state = read_snapshot(result_path)
    expected = ctx.roots.expected_runtime_bootstrap_result_sha256
    if (
        current_state.get("sha256") != expected
        or sha256_bytes(payload) != expected
        or ctx.runtime_bindings.get("runtime_bootstrap_result", {}).get("raw_sha256")
        != expected
    ):
        raise ConfigError(
            "runtime bootstrap result does not match its external SHA-256 anchor"
        )


def require_current_immutable_environment_binding(ctx: RuntimeContext) -> None:
    require_current_environment_manifest_anchor(ctx)
    if not ctx.immutable_environment_files:
        raise ConfigError("immutable environment file identities were not captured")
    for binding in ctx.immutable_runtime_directories:
        try:
            metadata = os.lstat(binding.path)
        except OSError as exc:
            raise ConfigError(f"{binding.label} is missing") from exc
        if (
            is_reparse(binding.path)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata_identity(metadata) != binding.identity
        ):
            raise ConfigError(
                f"{binding.label} identity changed after environment validation"
            )
    for binding in ctx.immutable_environment_files:
        current_identity = capture_bound_regular_identity(
            binding.path,
            binding.boundary,
            binding.state,
            binding.label,
        )
        if current_identity != binding.identity:
            raise ConfigError(
                f"{binding.label} identity changed after environment validation"
            )
    for binding in ctx.immutable_runtime_directories:
        metadata = os.lstat(binding.path)
        if (
            is_reparse(binding.path)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata_identity(metadata) != binding.identity
        ):
            raise ConfigError(
                f"{binding.label} identity changed during immutable file recapture"
            )


def require_current_bridge_probe_directory_identity(ctx: RuntimeContext) -> None:
    for path, identity, label in (
        (ctx.bootstrap_path.parent, ctx.bridge_identity, "runtime bridge directory"),
        (ctx.probe_dll.parent, ctx.probe_identity, "installed Probe directory"),
    ):
        try:
            metadata = os.lstat(path)
        except OSError as exc:
            raise ConfigError(f"{label} is missing") from exc
        if (
            is_reparse(path)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata_identity(metadata) != identity
        ):
            raise ConfigError(f"{label} identity changed after environment validation")


def require_current_bridge_probe_binding(
    ctx: RuntimeContext,
    *,
    require_disabled_policy: bool,
) -> None:
    require_current_bridge_probe_directory_identity(ctx)
    (
        bootstrap_path,
        bootstrap,
        policy_path,
        probe_dll,
        runtime,
        _runtime_files,
        _runtime_directories,
    ) = validate_bridge_and_probe(
        ctx.roots,
        ctx.manifest,
        EXPECTED_MOD_VERSION,
        phase=ctx.phase,
        require_disabled_policy=require_disabled_policy,
        allowed_config_lock_identity=ctx.active_config_lock_identity,
        active_parent_finalize_lock=ctx.active_parent_finalize_lock,
    )
    if (
        not same_path(bootstrap_path, ctx.bootstrap_path)
        or bootstrap != ctx.bootstrap
        or not same_path(policy_path, ctx.policy_path)
        or not same_path(probe_dll, ctx.probe_dll)
        or runtime != ctx.runtime_bindings
    ):
        raise ConfigError("runtime Bridge/Probe binding changed after environment validation")
    require_current_bridge_probe_directory_identity(ctx)


@dataclass(frozen=True)
class CommitRecord:
    relative: str
    identity: tuple[int, int]
    sha256: str
    length: int


@dataclass(frozen=True)
class PublicationRecord:
    path: pathlib.Path
    identity: tuple[int, int]
    sha256: str
    length: int


@dataclass(frozen=True)
class StagedRecord:
    path: pathlib.Path
    identity: tuple[int, int]
    sha256: str
    length: int


@dataclass(frozen=True)
class RollbackOutcome:
    unrestored: tuple[str, ...]
    recovery_claims: tuple[pathlib.Path, ...]


def require_test_controls_mode(
    args: argparse.Namespace,
    test_root: Optional[pathlib.Path],
) -> None:
    fault_step = getattr(args, "test_fail_after_replace", 0)
    if (
        isinstance(fault_step, bool)
        or not isinstance(fault_step, int)
        or fault_step not in {0, 1, 2}
    ):
        raise ConfigError("partial-commit fault option is invalid")
    if test_root is None and (
        fault_step != 0
        or _TEST_HOOK is not None
        or _TEST_MANAGED_ASSEMBLY_HOOK is not None
        or bool(_TEST_FAULTS)
    ):
        raise ConfigError("test-only fault controls are forbidden in live mode")


def resolve_roots(args: argparse.Namespace) -> Roots:
    if RUN_ID_RE.fullmatch(args.run_id or "") is None:
        raise ConfigError("run id is invalid")
    validate_windows_segment(args.loader_stem or "", "loader stem")
    if args.expected_mod_version != EXPECTED_MOD_VERSION:
        raise ConfigError(f"this gate requires Probe version {EXPECTED_MOD_VERSION}")
    expected_release_hash = require_hash(
        args.expected_release_dll_sha256,
        "external Release DLL anchor",
    )
    expected_environment_manifest_hash = require_hash(
        getattr(args, "expected_environment_manifest_sha256", None),
        "external environment manifest anchor",
    )
    expected_runtime_bootstrap_result_hash = require_hash(
        getattr(args, "expected_runtime_bootstrap_result_sha256", None),
        "external runtime bootstrap result anchor",
    )
    if args.apply and args.confirm_live_run_id != args.run_id:
        raise ConfigError("apply requires an exact --confirm-live-run-id")

    test_root = assert_safe_root(pathlib.Path(args.test_root), "TestRoot") if args.test_root else None
    require_test_controls_mode(args, test_root)
    managed_arg = getattr(args, "game_managed_dir", None)
    test_main_hash = getattr(args, "test_managed_assembly_csharp_sha256", None)
    test_firstpass_hash = getattr(
        args,
        "test_managed_assembly_csharp_firstpass_sha256",
        None,
    )
    if test_root is None and (
        test_main_hash is not None or test_firstpass_hash is not None
    ):
        raise ConfigError("live managed assembly hash overrides are forbidden")
    oni_root = assert_safe_root(pathlib.Path(args.oni_root), "ONI root")
    evidence_root = assert_safe_root(pathlib.Path(args.evidence_root), "evidence root")
    boundary_root = assert_safe_root(pathlib.Path(args.boundary_report_root), "boundary report root")
    runtime_bootstrap_result = require_absolute_local_path(
        args.runtime_bootstrap_result,
        "runtime bootstrap result",
    )
    release_dll = require_absolute_local_path(args.release_dll, "Release DLL")
    live_default = normalized(
        protected_documents_root() / "Klei" / "OxygenNotIncluded"
    )
    if test_root is not None:
        expected_fixture_oni = test_root / "oni-user"
        expected_fixture_evidence = test_root / "evidence"
        expected_fixture_boundary = expected_fixture_evidence / "live-boundaries"
        if not same_path(oni_root, expected_fixture_oni):
            raise ConfigError("TestRoot ONI root must use the exact oni-user fixture path")
        if not same_path(evidence_root, expected_fixture_evidence):
            raise ConfigError("TestRoot evidence root must use the exact evidence fixture path")
        if not same_path(boundary_root, expected_fixture_boundary):
            raise ConfigError("TestRoot boundary root must use the exact live-boundaries fixture path")
        if paths_overlap(test_root, live_default) or paths_overlap(test_root, PROJECT_ROOT):
            raise ConfigError("TestRoot cannot overlap live ONI data or the workspace")
        expected_fixture_release = test_root / "workspace" / "mod" / "OniAiProbeMod.dll"
        if not same_path(release_dll, expected_fixture_release):
            raise ConfigError("TestRoot Release DLL must use the exact workspace/mod fixture path")
        assert_no_reparse(release_dll, test_root)
    else:
        if not same_path(oni_root, live_default):
            raise ConfigError("live mode is restricted to the default ONI user-data root")
        if not path_within(evidence_root, PROJECT_ROOT):
            raise ConfigError("live evidence must stay under the workspace")
        if not path_within(boundary_root, evidence_root, strict=False):
            raise ConfigError("live boundary reports must stay under the evidence root")
        if not same_path(release_dll, LIVE_RELEASE_DLL):
            raise ConfigError("live Release DLL must be the exact workspace mod/OniAiProbeMod.dll")
        assert_no_reparse(release_dll, PROJECT_ROOT)
    if paths_overlap(oni_root, evidence_root) or not same_volume(oni_root, evidence_root):
        raise ConfigError("ONI and evidence roots must be disjoint on one local volume")
    assert_no_reparse(oni_root, test_root or oni_root)
    assert_no_reparse(evidence_root, test_root or PROJECT_ROOT)
    assert_no_reparse(boundary_root, evidence_root)
    expected_runtime_bootstrap_result_path = (
        evidence_root
        / args.run_id
        / pathlib.Path(*RUNTIME_BOOTSTRAP_RELATIVE.parts)
        / "result.json"
    )
    if not same_path(
        runtime_bootstrap_result,
        expected_runtime_bootstrap_result_path,
    ):
        raise ConfigError("runtime bootstrap result is not at its unique run path")
    assert_no_reparse(runtime_bootstrap_result, evidence_root)
    runtime_result_payload, runtime_result_state = read_snapshot(
        runtime_bootstrap_result
    )
    if (
        runtime_result_state.get("sha256")
        != expected_runtime_bootstrap_result_hash
        or sha256_bytes(runtime_result_payload)
        != expected_runtime_bootstrap_result_hash
    ):
        raise ConfigError(
            "runtime bootstrap result does not match its external SHA-256 anchor"
        )
    if not release_dll.is_file() or file_state(release_dll)["sha256"] != expected_release_hash:
        raise ConfigError("workspace Release DLL does not match the external SHA-256 anchor")

    if test_root is None:
        game_managed_dir = assert_safe_root(
            pathlib.Path(managed_arg) if managed_arg else LIVE_GAME_MANAGED_DIR,
            "game Managed directory",
        )
        if not same_path(game_managed_dir, LIVE_GAME_MANAGED_DIR):
            raise ConfigError("live game Managed directory must be the exact audited ONI path")
        expected_managed_hashes = LIVE_MANAGED_ASSEMBLY_SHA256
    else:
        if not managed_arg:
            raise ConfigError("TestRoot mode requires --game-managed-dir")
        game_managed_dir = assert_safe_root(
            pathlib.Path(managed_arg),
            "game Managed directory",
        )
        expected_fixture_managed = (
            test_root / "game" / "OxygenNotIncluded_Data" / "Managed"
        )
        if not same_path(game_managed_dir, expected_fixture_managed):
            raise ConfigError("TestRoot game Managed directory must use the exact fixture path")
        if paths_overlap(game_managed_dir, oni_root) or paths_overlap(
            game_managed_dir,
            evidence_root,
        ):
            raise ConfigError("fixture game Managed directory must be disjoint from runtime roots")
        if test_main_hash is None or test_firstpass_hash is None:
            raise ConfigError("TestRoot mode requires both managed assembly hash fixtures")
        expected_managed_hashes = {
            "Assembly-CSharp.dll": require_hash(
                test_main_hash,
                "TestRoot Assembly-CSharp.dll hash",
            ),
            "Assembly-CSharp-firstpass.dll": require_hash(
                test_firstpass_hash,
                "TestRoot Assembly-CSharp-firstpass.dll hash",
            ),
        }
    return Roots(
        run_id=args.run_id,
        oni_root=oni_root,
        evidence_root=evidence_root,
        boundary_root=boundary_root,
        expected_environment_manifest_sha256=expected_environment_manifest_hash,
        runtime_bootstrap_result=runtime_bootstrap_result,
        expected_runtime_bootstrap_result_sha256=(
            expected_runtime_bootstrap_result_hash
        ),
        release_dll=release_dll,
        expected_release_dll_sha256=expected_release_hash,
        game_managed_dir=game_managed_dir,
        managed_assembly_expected_sha256=tuple(
            (name, expected_managed_hashes[name]) for name in MANAGED_ASSEMBLY_NAMES
        ),
        test_root=test_root,
        live_mode=test_root is None,
    )


def managed_directory_ancestry_binding(managed: pathlib.Path) -> list[dict[str, str]]:
    managed = require_absolute_local_path(managed, "game Managed directory")
    candidates: list[pathlib.Path] = []
    current = pathlib.Path(managed.anchor)
    candidates.append(current)
    for part in managed.parts[1:]:
        current = current / part
        candidates.append(current)
    records: list[dict[str, str]] = []
    for directory in candidates:
        try:
            metadata = os.lstat(directory)
        except OSError as exc:
            raise ConfigError("game Managed ancestry is missing") from exc
        if is_reparse(directory) or not stat.S_ISDIR(metadata.st_mode):
            raise ConfigError("game Managed ancestry is not stable local directories")
        records.append(
            {
                "path_id": path_id(directory, contract="lower"),
                "identity_id": identity_id(metadata_identity(metadata)),
            }
        )
    return records


def capture_managed_assembly_binding(roots: Roots) -> dict[str, Any]:
    """Capture the exact audited Managed pair through stable open descriptors."""

    managed = roots.game_managed_dir
    assert_safe_root(managed, "game Managed directory")
    ancestry_before = managed_directory_ancestry_binding(managed)
    try:
        with os.scandir(managed) as entries:
            actual_names = {entry.name for entry in entries}
    except OSError as exc:
        raise ConfigError("game Managed directory cannot be enumerated safely") from exc
    paths: dict[str, pathlib.Path] = {}
    named_before: dict[str, os.stat_result] = {}
    descriptors: dict[str, int] = {}
    descriptor_before: dict[str, os.stat_result] = {}
    expected_hashes = dict(roots.managed_assembly_expected_sha256)
    try:
        for name in MANAGED_ASSEMBLY_NAMES:
            if name not in actual_names:
                if any(item.casefold() == name.casefold() for item in actual_names):
                    raise ConfigError("managed assembly does not use its exact audited filename")
                raise ConfigError("required managed assembly is missing")
            path = normalized(managed / name)
            if path.name != name or not path_within(path, managed, strict=True):
                raise ConfigError("managed assembly escaped its exact Managed path")
            assert_no_reparse(path, managed)
            try:
                named = os.lstat(path)
            except OSError as exc:
                raise ConfigError("required managed assembly is missing") from exc
            if (
                is_reparse(path)
                or not stat.S_ISREG(named.st_mode)
                or named.st_nlink != 1
                or named.st_size < 0
                or named.st_size > MAX_MANAGED_ASSEMBLY_BYTES
            ):
                raise ConfigError(
                    "managed assembly must be a bounded regular non-reparse single-link file"
                )
            try:
                descriptor = open_read_shared_delete(path)
            except OSError as exc:
                raise ConfigError(
                    "managed assembly descriptor cannot be opened safely"
                ) from exc
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or metadata_identity(opened) != metadata_identity(named)
                or opened.st_size != named.st_size
                or opened.st_mtime_ns != named.st_mtime_ns
            ):
                os.close(descriptor)
                raise ConfigError(
                    "managed assembly name changed while its descriptor was opened"
                )
            paths[name] = path
            named_before[name] = named
            descriptors[name] = descriptor
            descriptor_before[name] = opened

        first_reads: dict[str, tuple[bytes, int, str]] = {}
        second_reads: dict[str, tuple[bytes, int, str]] = {}
        for name in MANAGED_ASSEMBLY_NAMES:
            payload = read_descriptor_all(
                descriptors[name],
                MAX_MANAGED_ASSEMBLY_BYTES,
            )
            first_reads[name] = (payload, len(payload), sha256_bytes(payload))
            if _TEST_MANAGED_ASSEMBLY_HOOK is not None:
                if roots.test_root is None:
                    raise ConfigError("managed assembly test hooks are forbidden in live mode")
                _TEST_MANAGED_ASSEMBLY_HOOK(name, paths[name])
        for name in MANAGED_ASSEMBLY_NAMES:
            payload = read_descriptor_all(
                descriptors[name],
                MAX_MANAGED_ASSEMBLY_BYTES,
            )
            second_reads[name] = (payload, len(payload), sha256_bytes(payload))

        records: list[dict[str, Any]] = []
        for name in MANAGED_ASSEMBLY_NAMES:
            opened_before = descriptor_before[name]
            opened_after = os.fstat(descriptors[name])
            try:
                named_after = os.lstat(paths[name])
            except OSError as exc:
                raise ConfigError("managed assembly name disappeared during capture") from exc
            first_payload, first_length, first_hash = first_reads[name]
            second_payload, second_length, second_hash = second_reads[name]
            if (
                is_reparse(paths[name])
                or not stat.S_ISREG(named_after.st_mode)
                or opened_after.st_nlink != 1
                or named_after.st_nlink != 1
                or metadata_identity(opened_before) != metadata_identity(opened_after)
                or metadata_identity(opened_after) != metadata_identity(named_after)
                or opened_before.st_size != opened_after.st_size
                or opened_after.st_size != named_after.st_size
                or opened_before.st_mtime_ns != opened_after.st_mtime_ns
                or opened_after.st_mtime_ns != named_after.st_mtime_ns
            ):
                raise ConfigError("managed assembly descriptor/name identity was not stable")
            if (
                first_payload != second_payload
                or first_length != second_length
                or first_hash != second_hash
                or first_length != opened_after.st_size
            ):
                raise ConfigError("managed assembly descriptor reads were not stable")
            if first_hash != expected_hashes[name]:
                raise ConfigError("managed assembly does not match the fixed audited SHA-256")
            records.append(
                {
                    "file_name": name,
                    "path_id": path_id(paths[name], contract="lower"),
                    "identity_id": identity_id(metadata_identity(opened_after)),
                    "length": first_length,
                    "sha256": first_hash,
                }
            )
        if _TEST_MANAGED_ASSEMBLY_HOOK is not None:
            if roots.test_root is None:
                raise ConfigError("managed assembly test hooks are forbidden in live mode")
            _TEST_MANAGED_ASSEMBLY_HOOK("__before_final_revalidation__", managed)
        assert_safe_root(managed, "game Managed directory")
        ancestry_after = managed_directory_ancestry_binding(managed)
        if ancestry_after != ancestry_before:
            raise ConfigError("game Managed directory ancestry identity was not stable")
        try:
            with os.scandir(managed) as entries:
                final_names = {entry.name for entry in entries}
        except OSError as exc:
            raise ConfigError("game Managed directory final enumeration failed") from exc
        for name in MANAGED_ASSEMBLY_NAMES:
            if name not in final_names or any(
                item.casefold() == name.casefold() and item != name
                for item in final_names
            ):
                raise ConfigError("managed assembly exact filenames changed during capture")
            assert_no_reparse(paths[name], managed)
            final_named = os.lstat(paths[name])
            final_descriptor = os.fstat(descriptors[name])
            if (
                is_reparse(paths[name])
                or not stat.S_ISREG(final_named.st_mode)
                or final_named.st_nlink != 1
                or final_descriptor.st_nlink != 1
                or metadata_identity(final_named)
                != metadata_identity(final_descriptor)
                or final_named.st_size != final_descriptor.st_size
                or final_named.st_mtime_ns != final_descriptor.st_mtime_ns
            ):
                raise ConfigError("managed assembly final name/descriptor identity changed")
        return {
            "binding_mode": "live_audited" if roots.live_mode else "test_fixture",
            "managed_directory_path_id": path_id(managed, contract="lower"),
            "managed_directory_identity_id": ancestry_after[-1]["identity_id"],
            "managed_ancestry": ancestry_after,
            "descriptor_read_count_per_assembly": 2,
            "assemblies": records,
        }
    finally:
        for descriptor in descriptors.values():
            os.close(descriptor)


def bound_builtin_auto_resume_semantics(
    managed_assembly_binding: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind audited source semantics to the descriptor-stable current DLL pair."""

    if not isinstance(managed_assembly_binding, Mapping):
        raise ConfigError("managed assembly binding is required for built-in semantics")
    expected_keys = {
        "binding_mode",
        "managed_directory_path_id",
        "managed_directory_identity_id",
        "managed_ancestry",
        "descriptor_read_count_per_assembly",
        "assemblies",
    }
    if set(managed_assembly_binding) != expected_keys:
        raise ConfigError("managed assembly binding field set is not exact")
    assemblies = managed_assembly_binding.get("assemblies")
    if (
        managed_assembly_binding.get("descriptor_read_count_per_assembly") != 2
        or not isinstance(assemblies, list)
        or len(assemblies) != len(MANAGED_ASSEMBLY_NAMES)
    ):
        raise ConfigError("managed assembly binding is incomplete")
    for expected_name, record in zip(MANAGED_ASSEMBLY_NAMES, assemblies):
        if (
            not isinstance(record, dict)
            or set(record) != {
                "file_name",
                "path_id",
                "identity_id",
                "length",
                "sha256",
            }
            or record.get("file_name") != expected_name
            or not isinstance(record.get("length"), int)
            or isinstance(record.get("length"), bool)
            or record["length"] < 0
        ):
            raise ConfigError("managed assembly binding record is invalid")
        require_hash(record.get("path_id"), "managed assembly path id")
        require_hash(record.get("identity_id"), "managed assembly identity id")
        require_hash(record.get("sha256"), "managed assembly SHA-256")
    require_hash(
        managed_assembly_binding.get("managed_directory_path_id"),
        "game Managed directory path id",
    )
    require_hash(
        managed_assembly_binding.get("managed_directory_identity_id"),
        "game Managed directory identity id",
    )
    ancestry = managed_assembly_binding.get("managed_ancestry")
    if not isinstance(ancestry, list) or not ancestry:
        raise ConfigError("game Managed ancestry binding is incomplete")
    for record in ancestry:
        if not isinstance(record, dict) or set(record) != {"path_id", "identity_id"}:
            raise ConfigError("game Managed ancestry binding record is invalid")
        require_hash(record.get("path_id"), "game Managed ancestry path id")
        require_hash(record.get("identity_id"), "game Managed ancestry identity id")
    if (
        ancestry[-1]["path_id"]
        != managed_assembly_binding["managed_directory_path_id"]
        or ancestry[-1]["identity_id"]
        != managed_assembly_binding["managed_directory_identity_id"]
    ):
        raise ConfigError("game Managed directory and ancestry bindings diverge")
    binding_mode = managed_assembly_binding.get("binding_mode")
    if binding_mode not in {"live_audited", "test_fixture"}:
        raise ConfigError("managed assembly binding mode is invalid")
    if binding_mode == "live_audited":
        actual_hashes = {
            record["file_name"]: record["sha256"] for record in assemblies
        }
        if actual_hashes != LIVE_MANAGED_ASSEMBLY_SHA256:
            raise ConfigError("live managed assembly binding lost its fixed audit hashes")
        audit = BUILT_IN_AUTO_RESUME_SEMANTICS["game_build_audit"]
        if (
            actual_hashes["Assembly-CSharp.dll"]
            != audit["bound_assembly_sha256"]
            or BUILT_IN_AUTO_RESUME_SEMANTICS["game_changelist"]
            != audit["value"]
            or BUILT_IN_AUTO_RESUME_SEMANTICS["expected_probe_game_build"]
            != audit["expected_probe_game_build"]
            or audit["expected_probe_game_build"] != "cl-744825"
        ):
            raise ConfigError("live game build audit binding is inconsistent")
    semantics = copy.deepcopy(BUILT_IN_AUTO_RESUME_SEMANTICS)
    semantics["managed_assembly_binding"] = copy.deepcopy(
        dict(managed_assembly_binding)
    )
    return semantics


def require_current_managed_assembly_binding(ctx: RuntimeContext) -> None:
    if capture_managed_assembly_binding(ctx.roots) != ctx.managed_assembly_binding:
        raise ConfigError("managed assembly binding changed during configuration")


def validate_process_fixture(path: pathlib.Path, test_root: pathlib.Path) -> dict[str, int]:
    path = require_absolute_local_path(path, "process fixture")
    if not path_within(path, test_root):
        raise ConfigError("process fixture escaped TestRoot")
    assert_no_reparse(path, test_root)
    value = load_json(path, "process fixture")
    if value.get("schema") != "oni.process-state-fixture.v1":
        raise ConfigError("process fixture schema is invalid")
    result: dict[str, int] = {}
    for key in ("oxygen_not_included_count", "bridge_count"):
        item = value.get(key)
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise ConfigError("process fixture contains an invalid count")
        result[key] = item
    return result


def split_windows_command_line(value: str) -> list[str]:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("python process command line is unavailable")
    if os.name != "nt":
        return value.split()
    argc = ctypes.c_int(0)
    command_line_to_argv = ctypes.windll.shell32.CommandLineToArgvW
    command_line_to_argv.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    command_line_to_argv.restype = ctypes.POINTER(ctypes.c_wchar_p)
    argv = command_line_to_argv(value, ctypes.byref(argc))
    if not argv:
        raise ConfigError("python process command line is invalid")
    try:
        return [argv[index] for index in range(argc.value)]
    finally:
        local_free = ctypes.windll.kernel32.LocalFree
        local_free.argtypes = [ctypes.c_void_p]
        local_free.restype = ctypes.c_void_p
        local_free(ctypes.cast(argv, ctypes.c_void_p))


def is_bridge_command(command_line: str) -> bool:
    arguments = split_windows_command_line(command_line)
    consumes_next = False
    for argument in arguments[1:]:
        folded = argument.casefold()
        if consumes_next:
            consumes_next = False
            continue
        if folded in {"-c", "-m"}:
            return False
        if folded in {"-w", "-x", "--check-hash-based-pycs"}:
            consumes_next = True
            continue
        if folded == "-":
            return False
        if folded == "--" or folded.startswith("-"):
            continue
        return pathlib.PureWindowsPath(argument).name.casefold() == "oni_bridge.py"
    return False


def live_process_counts() -> dict[str, int]:
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if not powershell:
        raise ConfigError("PowerShell is unavailable for the live process guard")
    command = (
        "$g=@(Get-Process -Name 'OxygenNotIncluded' -ErrorAction SilentlyContinue).Count;"
        "$n=@('python.exe','pythonw.exe','py.exe');"
        "$p=@(Get-CimInstance Win32_Process -ErrorAction Stop|"
        "Where-Object{$n -contains $_.Name}|Select-Object Name,CommandLine);"
        "@{oxygen_not_included_count=$g;python_processes=@($p)}|ConvertTo-Json -Depth 4 -Compress"
    )
    try:
        completed = subprocess.run(
            [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ConfigError("live process guard failed closed") from exc
    if completed.returncode != 0:
        raise ConfigError("live process guard failed closed")
    try:
        value = json.loads(completed.stdout)
        records = value.get("python_processes", [])
        if isinstance(records, dict):
            records = [records]
        if not isinstance(records, list):
            raise TypeError
        bridge_count = 0
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("CommandLine"), str):
                raise TypeError
            if is_bridge_command(record["CommandLine"]):
                bridge_count += 1
        return {
            "oxygen_not_included_count": int(value["oxygen_not_included_count"]),
            "bridge_count": bridge_count,
        }
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ConfigError("live process guard returned invalid evidence") from exc


def process_counts(args: argparse.Namespace, roots: Roots) -> dict[str, int]:
    if roots.test_root is not None:
        if not args.process_state_fixture:
            raise ConfigError("TestRoot mode requires a process-state fixture")
        return validate_process_fixture(pathlib.Path(args.process_state_fixture), roots.test_root)
    if args.process_state_fixture:
        raise ConfigError("process fixtures are forbidden in live mode")
    return live_process_counts()


def require_processes_stopped(counts: Mapping[str, int]) -> None:
    if int(counts.get("oxygen_not_included_count", -1)) != 0:
        raise ConfigError("Oxygen Not Included must be stopped")
    if int(counts.get("bridge_count", -1)) != 0:
        raise ConfigError("oni_bridge.py must be stopped")


def verify_journal(path: pathlib.Path) -> list[dict[str, Any]]:
    payload, _state = read_snapshot(path)
    entries: list[dict[str, Any]] = []
    previous: Optional[str] = None
    for raw in payload.splitlines():
        try:
            entry = json.loads(raw.decode("utf-8"), object_pairs_hook=reject_duplicate_keys)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ConfigError("restore journal contains invalid JSON") from exc
        if (
            not isinstance(entry, dict)
            or set(entry) != JOURNAL_ENTRY_KEYS
            or entry.get("schema") != JOURNAL_SCHEMA
            or type(entry.get("index")) is not int
            or entry["index"] < 1
            or not isinstance(entry.get("event"), str)
            or not entry["event"]
            or not isinstance(entry.get("data"), dict)
            or not isinstance(entry.get("recorded_at"), str)
        ):
            raise ConfigError("restore journal schema is invalid")
        try:
            recorded_at = dt.datetime.fromisoformat(
                entry["recorded_at"].replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise ConfigError("restore journal timestamp is invalid") from exc
        if recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
            raise ConfigError("restore journal timestamp is invalid")
        claimed = require_hash(entry.get("entry_hash"), "journal entry hash")
        unsigned = dict(entry)
        del unsigned["entry_hash"]
        if claimed != sha256_bytes(canonical_bytes(unsigned)):
            raise ConfigError("restore journal hash chain is invalid")
        if entry.get("index") != len(entries) + 1 or entry.get("previous_entry_hash") != previous:
            raise ConfigError("restore journal order/hash link is invalid")
        entries.append(entry)
        previous = claimed
    if not entries:
        raise ConfigError("restore journal is empty")
    return entries


def save_root_for(path: pathlib.Path, oni_root: pathlib.Path) -> pathlib.Path:
    path = normalized(path)
    for relative in ("save_files", "cloud_save_files"):
        root = oni_root / relative
        if root.is_dir() and path_within(path, root):
            assert_no_reparse(path, root)
            return root
    raise ConfigError("save path is outside the allowed ONI save roots")


def validate_simple_yaml_scalar(payload: bytes, key: str, expected: str) -> None:
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeError as exc:
        raise ConfigError("Probe manifest is not UTF-8") from exc
    pattern = re.compile(rf"^[ \t]*{re.escape(key)}[ \t]*:[ \t]*(.*?)[ \t]*$", re.MULTILINE)
    matches = pattern.findall(text)
    if len(matches) != 1:
        raise ConfigError(f"Probe manifest must contain exactly one {key}")
    value = matches[0].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1]
    if value != expected:
        raise ConfigError(f"Probe manifest {key} is invalid")


def validate_disabled_policy_document(policy: Mapping[str, Any]) -> None:
    if (
        set(policy) != POLICY_KEYS
        or policy.get("schema") != POLICY_SCHEMA
        or policy.get("actions_enabled") is not False
        or policy.get("suggestions_enabled") is not False
        or policy.get("allowed_save_hashes") != []
        or policy.get("allowed_actions") != ["focus_note", "noop"]
        or policy.get("command_ttl_seconds") != 30
        or policy.get("max_pending_commands") != 20
        or not isinstance(policy.get("generated_at"), str)
        or not policy["generated_at"]
    ):
        raise ConfigError("Bridge initial policy is not the strict disabled/unbound document")


def validate_bootstrap_document(
    bootstrap: Mapping[str, Any],
    roots: Roots,
    bridge: pathlib.Path,
    expected_mod_version: str,
) -> tuple[str, str]:
    if (
        set(bootstrap) != BOOTSTRAP_KEYS
        or bootstrap.get("schema") != BOOTSTRAP_SCHEMA
        or bootstrap.get("run_id") != roots.run_id
        or bootstrap.get("expected_mod_version") != expected_mod_version
        or bootstrap.get("installed_release") is not True
        or bootstrap.get("action_policy_disabled") is not True
        or not isinstance(bootstrap.get("prepared_at"), str)
        or not bootstrap["prepared_at"]
        or not isinstance(bootstrap.get("bridge_dir"), str)
        or not pathlib.Path(bootstrap["bridge_dir"]).is_absolute()
        or bootstrap["bridge_dir"].startswith(("\\\\", "//"))
        or not same_path(bootstrap["bridge_dir"], bridge)
    ):
        raise ConfigError("runtime bootstrap does not bind this run/version/bridge")
    built_hash = require_hash(bootstrap.get("built_dll_sha256"), "built Probe DLL hash")
    installed_hash = require_hash(
        bootstrap.get("installed_dll_sha256"), "installed Probe DLL hash"
    )
    return built_hash, installed_hash


def validate_bridge_surface(bridge: pathlib.Path, *, phase: str) -> None:
    if phase not in {"before", "evidence"}:
        raise ConfigError("internal runtime Bridge validation phase is invalid")
    commands_root = bridge / "commands"
    validation_controls_root = bridge / "validation-controls"
    evidence_root = bridge / "evidence"
    required_directories = {
        commands_root,
        validation_controls_root,
        evidence_root,
        *(commands_root / name for name in (
            "pending",
            "approving",
            "approved",
            "executing",
            "confirming",
            "done",
            "failed",
            "rejected",
            "quarantine",
        )),
        *(validation_controls_root / name for name in (
            "approved",
            "inflight",
            "done",
            "failed",
            "quarantine",
        )),
    }
    for directory in required_directories:
        assert_no_reparse(directory, bridge)
        if not directory.is_dir():
            raise ConfigError("runtime Bridge static directory topology is incomplete")
    for candidate in bridge.rglob("*"):
        if is_reparse(candidate):
            raise ConfigError("runtime bridge contains a reparse point")
        if not candidate.is_file() and not candidate.is_dir():
            raise ConfigError("runtime Bridge contains a non-file node")
        if candidate.is_file() and os.lstat(candidate).st_nlink != 1:
            raise ConfigError("runtime Bridge contains a hard-linked file")
        if candidate.name.casefold().endswith((".tmp", ".partial")):
            raise ConfigError("runtime bridge contains a temporary file")
    required_root_directories = {
        "commands",
        "validation-controls",
        "evidence",
    }
    required_root_files = {
        "action-policy.json",
        "validation-control-policy.json",
        "v27-runtime-bootstrap.json",
    }
    allowed_root_names = required_root_directories | required_root_files | {
        STARTUP_BINDING_NAME
    }
    if phase == "evidence":
        allowed_root_names |= RUNTIME_DYNAMIC_BRIDGE_ROOT_FILES
        allowed_root_names |= RUNTIME_DYNAMIC_BRIDGE_ROOT_DIRECTORIES
    actual_root_names = {item.name for item in bridge.iterdir()}
    unexpected_root_names = actual_root_names - allowed_root_names
    required_root_names = required_root_directories | required_root_files
    if unexpected_root_names or not required_root_names.issubset(actual_root_names):
        raise ConfigError("runtime Bridge contains an unexpected static root entry")
    for name in RUNTIME_DYNAMIC_BRIDGE_ROOT_FILES & actual_root_names:
        if not (bridge / name).is_file():
            raise ConfigError("runtime Bridge dynamic root file has the wrong node type")
    for name in RUNTIME_DYNAMIC_BRIDGE_ROOT_DIRECTORIES & actual_root_names:
        if not (bridge / name).is_dir():
            raise ConfigError("runtime Bridge dynamic root directory has the wrong node type")
    for name in NONTERMINAL_QUEUES:
        queue = commands_root / name
        assert_no_reparse(queue, bridge)
        if not queue.is_dir() or any(queue.iterdir()):
            raise ConfigError("Bridge has a non-empty or unsafe nonterminal command queue")
    for name in NONTERMINAL_VALIDATION_CONTROL_QUEUES:
        queue = validation_controls_root / name
        assert_no_reparse(queue, bridge)
        if not queue.is_dir() or any(queue.iterdir()):
            raise ConfigError(
                "Bridge has a non-empty or unsafe nonterminal validation-control queue"
            )


def require_exact_nonnegative_int(value: Any, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ConfigError(f"{label} must be an exact non-negative integer")
    return value


def node_identity_list(metadata: os.stat_result) -> list[int]:
    return [
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(stat.S_IFMT(metadata.st_mode)),
    ]


def require_identity_list(value: Any, label: str) -> list[int]:
    if (
        not isinstance(value, list)
        or len(value) != 3
        or any(type(item) is not int or item < 0 for item in value)
    ):
        raise ConfigError(f"{label} is not an exact filesystem identity")
    return list(value)


def read_exact_json_document(
    path: pathlib.Path,
    label: str,
    *,
    boundary: pathlib.Path,
    expected_raw_sha256: Optional[str] = None,
) -> tuple[dict[str, Any], bytes, dict[str, Any]]:
    assert_no_reparse(path, boundary)
    payload, state_value = read_snapshot(path)
    if payload.startswith(b"\xef\xbb\xbf"):
        raise ConfigError(f"{label} must not contain a UTF-8 BOM")
    if expected_raw_sha256 is not None and (
        state_value.get("sha256") != expected_raw_sha256
        or sha256_bytes(payload) != expected_raw_sha256
    ):
        raise ConfigError(f"{label} does not match its external SHA-256 anchor")
    document = parse_json_payload(payload, label)
    if payload != json_document_bytes(document):
        raise ConfigError(f"{label} raw serialization is not exact")
    return document, payload, state_value


def classify_safe_probe_node(
    metadata: os.stat_result,
    *,
    static_artifact: bool,
) -> str:
    if stat.S_ISDIR(metadata.st_mode):
        if static_artifact:
            raise ConfigError("installed Probe static artifact is not a file")
        return "directory"
    if not stat.S_ISREG(metadata.st_mode):
        raise ConfigError("installed Probe tree contains a non-file node")
    if metadata.st_nlink != 1:
        raise ConfigError("installed Probe tree contains a non-exclusive file")
    return "file"


def capture_exact_probe_tree(
    probe: pathlib.Path,
    *,
    allow_runtime_nodes: bool = False,
) -> dict[str, Any]:
    try:
        root_before = os.lstat(probe)
    except OSError as exc:
        raise ConfigError("installed Probe directory is missing") from exc
    if is_reparse(probe) or not stat.S_ISDIR(root_before.st_mode):
        raise ConfigError("installed Probe directory is unsafe")
    paths = sorted(probe.rglob("*"), key=lambda item: str(item).casefold())
    relatives = [item.relative_to(probe).as_posix() for item in paths]
    expected_relatives = sorted(RUNTIME_BOOTSTRAP_ARTIFACT_NAMES, key=str.casefold)
    if (
        not allow_runtime_nodes
        and relatives != expected_relatives
        or allow_runtime_nodes
        and not set(expected_relatives).issubset(relatives)
    ):
        raise ConfigError("installed Probe tree is not the exact four-file tree")
    entries: list[dict[str, Any]] = []
    identities: list[dict[str, Any]] = []
    artifacts: dict[str, dict[str, Any]] = {}
    payloads: dict[str, bytes] = {}
    states: dict[str, dict[str, Any]] = {}
    stable_nodes: dict[str, tuple[str, tuple[int, int], int, int]] = {}
    for path in paths:
        relative = path.relative_to(probe).as_posix()
        assert_no_reparse(path, probe)
        metadata_before = os.lstat(path)
        if path.name.casefold().endswith((".tmp", ".partial")):
            raise ConfigError("installed Probe tree contains a temporary node")
        kind = classify_safe_probe_node(
            metadata_before,
            static_artifact=relative in expected_relatives,
        )
        if kind == "directory":
            stable_nodes[relative] = (
                "directory",
                metadata_identity(metadata_before),
                int(metadata_before.st_mtime_ns),
                int(metadata_before.st_size),
            )
            continue
        stable_nodes[relative] = (
            "file",
            metadata_identity(metadata_before),
            int(metadata_before.st_mtime_ns),
            int(metadata_before.st_size),
        )
        if relative not in expected_relatives:
            continue
        payload, state_value = read_snapshot(path, maximum=MAX_MANAGED_ASSEMBLY_BYTES)
        metadata_after = os.lstat(path)
        if (
            metadata_identity(metadata_before) != metadata_identity(metadata_after)
            or metadata_before.st_mtime_ns != metadata_after.st_mtime_ns
            or metadata_before.st_size != metadata_after.st_size
            or metadata_after.st_nlink != 1
        ):
            raise ConfigError("installed Probe tree changed during capture")
        entry = {
            "kind": "file",
            "relative": relative,
            "length": state_value["length"],
            "sha256": state_value["sha256"],
        }
        entries.append(entry)
        identities.append(
            {
                "relative": relative,
                "kind": "file",
                "identity": node_identity_list(metadata_after),
            }
        )
        artifacts[relative] = {
            "length": state_value["length"],
            "sha256": state_value["sha256"],
        }
        payloads[relative] = payload
        states[relative] = state_value
    paths_after = sorted(probe.rglob("*"), key=lambda item: str(item).casefold())
    root_after = os.lstat(probe)
    if (
        [item.relative_to(probe).as_posix() for item in paths_after] != relatives
        or metadata_identity(root_after) != metadata_identity(root_before)
        or root_after.st_mtime_ns != root_before.st_mtime_ns
    ):
        raise ConfigError("installed Probe tree changed during capture")
    for path in paths_after:
        relative = path.relative_to(probe).as_posix()
        kind, identity, last_write_ns, length = stable_nodes[relative]
        metadata = os.lstat(path)
        if (
            is_reparse(path)
            or metadata_identity(metadata) != identity
            or int(metadata.st_mtime_ns) != last_write_ns
            or int(metadata.st_size) != length
            or (kind == "directory" and not stat.S_ISDIR(metadata.st_mode))
            or (kind == "file" and not stat.S_ISREG(metadata.st_mode))
            or (kind == "file" and metadata.st_nlink != 1)
        ):
            raise ConfigError("installed Probe node changed during capture")
    content = {"exists": True, "entries": entries}
    return {
        "artifacts": artifacts,
        "content": content,
        "content_hash": sha256_bytes(canonical_bytes(content)),
        "identity": {
            "exists": True,
            "root_identity": node_identity_list(root_before),
            "entries": identities,
        },
        "payloads": payloads,
        "states": states,
    }


def capture_runtime_bridge_content(bridge: pathlib.Path) -> dict[str, Any]:
    """Capture the bootstrap-owned Bridge tree as one stable content view.

    ``runtime-startup-binding.json`` is config-owned and is deliberately
    excluded: it does not exist when the bootstrap certificate is issued and
    is separately create-only/external-hash verified by this transaction.
    No other extra path is excluded.
    """

    try:
        root_before = os.lstat(bridge)
    except OSError as exc:
        raise ConfigError("runtime Bridge directory is missing") from exc
    if is_reparse(bridge) or not stat.S_ISDIR(root_before.st_mode):
        raise ConfigError("runtime Bridge directory is unsafe")
    paths = sorted(
        (
            item
            for item in bridge.rglob("*")
            if item.relative_to(bridge).as_posix() != STARTUP_BINDING_NAME
        ),
        key=lambda item: str(item).casefold(),
    )
    relatives = [item.relative_to(bridge).as_posix() for item in paths]
    entries: list[dict[str, Any]] = []
    stable: dict[str, tuple[str, tuple[int, int], int, Optional[dict[str, Any]]]] = {}
    for path in paths:
        relative = path.relative_to(bridge).as_posix()
        assert_no_reparse(path, bridge)
        metadata = os.lstat(path)
        if stat.S_ISDIR(metadata.st_mode):
            entries.append({"kind": "directory", "relative": relative})
            stable[relative] = (
                "directory",
                metadata_identity(metadata),
                int(metadata.st_mtime_ns),
                None,
            )
        elif stat.S_ISREG(metadata.st_mode):
            if metadata.st_nlink != 1:
                raise ConfigError("runtime Bridge contains a hard-linked file")
            _payload, state_value = read_snapshot(
                path, maximum=MAX_MANAGED_ASSEMBLY_BYTES
            )
            entries.append(
                {
                    "kind": "file",
                    "relative": relative,
                    "length": state_value["length"],
                    "sha256": state_value["sha256"],
                }
            )
            stable[relative] = (
                "file",
                metadata_identity(metadata),
                int(metadata.st_mtime_ns),
                state_value,
            )
        else:
            raise ConfigError("runtime Bridge contains a non-file node")
    paths_after = sorted(
        (
            item
            for item in bridge.rglob("*")
            if item.relative_to(bridge).as_posix() != STARTUP_BINDING_NAME
        ),
        key=lambda item: str(item).casefold(),
    )
    if [item.relative_to(bridge).as_posix() for item in paths_after] != relatives:
        raise ConfigError("runtime Bridge path set changed during capture")
    for path in paths_after:
        relative = path.relative_to(bridge).as_posix()
        kind, identity, last_write_ns, state_value = stable[relative]
        metadata = os.lstat(path)
        if (
            is_reparse(path)
            or metadata_identity(metadata) != identity
            or int(metadata.st_mtime_ns) != last_write_ns
        ):
            raise ConfigError("runtime Bridge node changed during capture")
        if kind == "directory":
            if not stat.S_ISDIR(metadata.st_mode):
                raise ConfigError("runtime Bridge directory changed kind")
        else:
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ConfigError("runtime Bridge file changed kind/custody")
            _payload, current_state = read_snapshot(
                path, maximum=MAX_MANAGED_ASSEMBLY_BYTES
            )
            if not canonical_typed_equal(current_state, state_value):
                raise ConfigError("runtime Bridge file changed during capture")
    root_after = os.lstat(bridge)
    if metadata_identity(root_after) != metadata_identity(root_before):
        raise ConfigError("runtime Bridge root changed during capture")
    content = {"exists": True, "entries": entries}
    return {
        "content": content,
        "content_hash": sha256_bytes(canonical_bytes(content)),
    }


def validate_identity_tree(value: Any, expected: Mapping[str, Any]) -> None:
    if not isinstance(value, dict) or set(value) != {
        "exists",
        "root_identity",
        "entries",
    }:
        raise ConfigError("runtime bootstrap Probe identity tree shape is invalid")
    if value.get("exists") is not True:
        raise ConfigError("runtime bootstrap Probe identity tree is not present")
    require_identity_list(value.get("root_identity"), "Probe root identity")
    entries = value.get("entries")
    if not isinstance(entries, list):
        raise ConfigError("runtime bootstrap Probe identity entries are invalid")
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"relative", "kind", "identity"}
            or not isinstance(entry.get("relative"), str)
            or entry.get("kind") not in {"file", "directory"}
        ):
            raise ConfigError("runtime bootstrap Probe identity entry is invalid")
        require_identity_list(entry.get("identity"), "Probe child identity")
    if not canonical_typed_equal(value, expected):
        raise ConfigError("runtime bootstrap Probe identity does not bind the live tree")


def validate_runtime_tree_contract(
    snapshot: Any,
    identities: Any,
    label: str,
) -> None:
    if not isinstance(snapshot, dict) or not isinstance(identities, dict):
        raise ConfigError(f"runtime bootstrap {label} prestate tree is invalid")
    if snapshot.get("exists") is False:
        if not canonical_typed_equal(
            snapshot,
            {
                "exists": False,
                "entries": [],
                "tree_hash": sha256_bytes(b"[]"),
            },
        ) or not canonical_typed_equal(identities, {"exists": False, "entries": []}):
            raise ConfigError(f"runtime bootstrap absent {label} tree is not exact")
        return
    if set(snapshot) != {
        "exists",
        "root_last_write_ns",
        "entries",
        "file_count",
        "total_bytes",
        "tree_hash",
    } or snapshot.get("exists") is not True:
        raise ConfigError(f"runtime bootstrap present {label} snapshot is not exact")
    snapshot_hash = require_hash(
        snapshot.get("tree_hash"), f"runtime bootstrap {label} tree hash"
    )
    unsigned_snapshot = dict(snapshot)
    del unsigned_snapshot["tree_hash"]
    if sha256_bytes(canonical_bytes(unsigned_snapshot)) != snapshot_hash:
        raise ConfigError(f"runtime bootstrap {label} tree hash is invalid")
    require_exact_nonnegative_int(
        snapshot.get("root_last_write_ns"), f"runtime bootstrap {label} root mtime"
    )
    file_count = require_exact_nonnegative_int(
        snapshot.get("file_count"), f"runtime bootstrap {label} file count"
    )
    total_bytes = require_exact_nonnegative_int(
        snapshot.get("total_bytes"), f"runtime bootstrap {label} total bytes"
    )
    entries = snapshot.get("entries")
    if not isinstance(entries, list):
        raise ConfigError(f"runtime bootstrap {label} snapshot entries are invalid")
    shapes: list[tuple[str, str]] = []
    computed_files = 0
    computed_bytes = 0
    for entry in entries:
        if not isinstance(entry, dict):
            raise ConfigError(f"runtime bootstrap {label} snapshot entry is invalid")
        kind = entry.get("kind")
        relative = entry.get("relative")
        if not isinstance(relative, str):
            raise ConfigError(f"runtime bootstrap {label} relative path is invalid")
        validate_relative(relative, f"runtime bootstrap {label} relative path")
        if kind == "directory":
            if set(entry) != {"kind", "relative", "last_write_ns"}:
                raise ConfigError(f"runtime bootstrap {label} directory entry is not exact")
        elif kind == "file":
            if set(entry) != {
                "kind",
                "relative",
                "length",
                "last_write_ns",
                "sha256",
            }:
                raise ConfigError(f"runtime bootstrap {label} file entry is not exact")
            computed_files += 1
            computed_bytes += require_exact_nonnegative_int(
                entry.get("length"), f"runtime bootstrap {label} file length"
            )
            require_hash(entry.get("sha256"), f"runtime bootstrap {label} file hash")
        else:
            raise ConfigError(f"runtime bootstrap {label} snapshot kind is invalid")
        require_exact_nonnegative_int(
            entry.get("last_write_ns"), f"runtime bootstrap {label} entry mtime"
        )
        shapes.append((relative, kind))
    if shapes != sorted(shapes, key=lambda item: item[0].casefold()):
        raise ConfigError(f"runtime bootstrap {label} snapshot is not path-sorted")
    if len({relative.casefold() for relative, _kind in shapes}) != len(shapes):
        raise ConfigError(f"runtime bootstrap {label} snapshot path set is ambiguous")
    if computed_files != file_count or computed_bytes != total_bytes:
        raise ConfigError(f"runtime bootstrap {label} snapshot totals are invalid")
    if set(identities) != {"exists", "root_identity", "entries"} or identities.get(
        "exists"
    ) is not True:
        raise ConfigError(f"runtime bootstrap {label} identity tree is not exact")
    require_identity_list(
        identities.get("root_identity"), f"runtime bootstrap {label} root identity"
    )
    identity_entries = identities.get("entries")
    if not isinstance(identity_entries, list) or len(identity_entries) != len(shapes):
        raise ConfigError(f"runtime bootstrap {label} identity entries are invalid")
    identity_shapes: list[tuple[str, str]] = []
    for entry in identity_entries:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"relative", "kind", "identity"}
            or not isinstance(entry.get("relative"), str)
            or entry.get("kind") not in {"file", "directory"}
        ):
            raise ConfigError(f"runtime bootstrap {label} identity entry is not exact")
        require_identity_list(
            entry.get("identity"), f"runtime bootstrap {label} child identity"
        )
        identity_shapes.append((entry["relative"], entry["kind"]))
    if identity_shapes != shapes:
        raise ConfigError(f"runtime bootstrap {label} content/identity shapes diverge")


def validate_runtime_bootstrap_stage_contract(
    roots: Roots,
    manifest: Mapping[str, Any],
    result: Mapping[str, Any],
    bootstrap: Mapping[str, Any],
    *,
    allow_current_journal_append: bool,
) -> tuple[ImmutableFileBinding, list[ImmutableFileBinding]]:
    if type(allow_current_journal_append) is not bool:
        raise ConfigError("internal restore journal append policy is invalid")
    stage_path = roots.environment_dir / "runtime-bootstrap-install-stage.json"
    stage, _stage_payload, stage_state = read_exact_json_document(
        stage_path,
        "runtime bootstrap install stage",
        boundary=roots.evidence_root,
        expected_raw_sha256=require_hash(
            result.get("install_stage_sha256"),
            "runtime bootstrap install stage raw hash",
        ),
    )
    if set(stage) != {
        "schema",
        "run_id",
        "transaction_id",
        "plan_hash",
        "install_intent_hash",
        "transaction_path_id_lower",
        "stage_nonce",
        "install_intent",
        "install_plan",
        "producer",
        "stage_hash",
    }:
        raise ConfigError("runtime bootstrap install stage field set is not exact")
    stage_hash = require_hash(stage.get("stage_hash"), "runtime bootstrap stage hash")
    unsigned_stage = dict(stage)
    del unsigned_stage["stage_hash"]
    if (
        stage_hash != result.get("install_stage_hash")
        or sha256_bytes(canonical_bytes(unsigned_stage)) != stage_hash
        or stage.get("schema") != RUNTIME_BOOTSTRAP_INSTALL_STAGE_SCHEMA
        or stage.get("run_id") != roots.run_id
        or stage.get("transaction_id") != result.get("transaction_id")
        or stage.get("plan_hash") != result.get("plan_hash")
        or stage.get("transaction_path_id_lower")
        != path_id(roots.runtime_bootstrap_dir, contract="lower")
        or not isinstance(stage.get("stage_nonce"), str)
        or re.fullmatch(r"[0-9a-f]{32}", stage["stage_nonce"]) is None
    ):
        raise ConfigError("runtime bootstrap install stage binding is invalid")

    producer = stage.get("producer")
    if not isinstance(producer, dict) or set(producer) != {"length", "sha256"}:
        raise ConfigError("runtime bootstrap producer record is not exact")
    require_exact_nonnegative_int(producer.get("length"), "runtime bootstrap producer length")
    require_hash(producer.get("sha256"), "runtime bootstrap producer hash")
    if not canonical_typed_equal(producer, result.get("producer")):
        raise ConfigError("runtime bootstrap producer stage/result binding is invalid")

    intent = stage.get("install_intent")
    if not isinstance(intent, dict) or set(intent) != {
        "schema",
        "transaction_id",
        "run_id",
        "plan_hash",
        "environment_manifest_sha256",
        "release_dll_sha256",
        "expected_mod_version",
        "confirm_live_run_id",
        "operation",
        "intent_hash",
    }:
        raise ConfigError("runtime bootstrap install intent field set is not exact")
    intent_hash = require_hash(intent.get("intent_hash"), "runtime bootstrap intent hash")
    unsigned_intent = dict(intent)
    del unsigned_intent["intent_hash"]
    if (
        sha256_bytes(canonical_bytes(unsigned_intent)) != intent_hash
        or stage.get("install_intent_hash") != intent_hash
        or intent.get("schema") != RUNTIME_BOOTSTRAP_INTENT_SCHEMA
        or intent.get("transaction_id") != result.get("transaction_id")
        or intent.get("run_id") != roots.run_id
        or intent.get("plan_hash") != result.get("plan_hash")
        or intent.get("environment_manifest_sha256")
        != roots.expected_environment_manifest_sha256
        or intent.get("release_dll_sha256") != result.get("release_dll_sha256")
        or intent.get("expected_mod_version") != result.get("expected_mod_version")
        or intent.get("confirm_live_run_id") != roots.run_id
        or intent.get("operation") != "install_prebuilt_release"
    ):
        raise ConfigError("runtime bootstrap install intent binding is invalid")

    plan = stage.get("install_plan")
    if not isinstance(plan, dict) or set(plan) != {
        "schema",
        "transaction_id",
        "run_id",
        "expected_mod_version",
        "seed_hash",
        "inputs",
        "prestate",
        "targets",
        "desired",
        "physical_steps",
        "plan_hash",
    }:
        raise ConfigError("runtime bootstrap install plan field set is not exact")
    plan_hash = require_hash(plan.get("plan_hash"), "runtime bootstrap plan hash")
    unsigned_plan = dict(plan)
    del unsigned_plan["plan_hash"]
    if (
        sha256_bytes(canonical_bytes(unsigned_plan)) != plan_hash
        or plan_hash != result.get("plan_hash")
        or plan.get("schema") != RUNTIME_BOOTSTRAP_PLAN_SCHEMA
        or plan.get("transaction_id") != result.get("transaction_id")
        or plan.get("run_id") != roots.run_id
        or plan.get("expected_mod_version") != result.get("expected_mod_version")
    ):
        raise ConfigError("runtime bootstrap install plan binding is invalid")

    inputs = plan.get("inputs")
    if not isinstance(inputs, dict) or set(inputs) != {
        "environment_manifest",
        "release_artifacts",
    }:
        raise ConfigError("runtime bootstrap plan inputs are not exact")
    environment_binding = inputs.get("environment_manifest")
    if not isinstance(environment_binding, dict) or set(environment_binding) != {
        "path_id_lower",
        "sha256",
        "length",
        "last_write_ns",
        "identity",
        "initial_restore_journal_entry_hash",
        "restore_journal_identity",
        "restore_journal_sha256",
        "restore_journal_length",
        "restore_journal_tip_hash",
        "restore_journal_entry_count",
        "environment_directory_identity",
    }:
        raise ConfigError("runtime bootstrap environment input binding is not exact")
    manifest_path = roots.environment_dir / "environment-manifest.json"
    manifest_payload, manifest_state = read_snapshot(manifest_path)
    manifest_metadata = os.lstat(manifest_path)
    journal_path = roots.environment_dir / "restore-journal.jsonl"
    journal_payload, journal_state = read_snapshot(journal_path)
    journal_metadata = os.lstat(journal_path)
    journal_entries = verify_journal(journal_path)
    canonical_journal_payload = b"".join(
        canonical_bytes(entry) + b"\n" for entry in journal_entries
    )
    if journal_payload != canonical_journal_payload:
        raise ConfigError("runtime bootstrap restore journal serialization is not exact")
    if not allow_current_journal_append and len(journal_entries) != 1:
        raise ConfigError("runtime bootstrap requires the unique environment journal origin")
    origin_payload = canonical_bytes(journal_entries[0]) + b"\n"
    expected_environment_binding = {
        "path_id_lower": path_id(manifest_path, contract="lower"),
        "sha256": roots.expected_environment_manifest_sha256,
        "length": len(manifest_payload),
        "last_write_ns": manifest_state["last_write_ns"],
        "identity": node_identity_list(manifest_metadata),
        "initial_restore_journal_entry_hash": journal_entries[0]["entry_hash"],
        "restore_journal_identity": node_identity_list(journal_metadata),
        "restore_journal_sha256": sha256_bytes(origin_payload),
        "restore_journal_length": len(origin_payload),
        "restore_journal_tip_hash": journal_entries[0]["entry_hash"],
        "restore_journal_entry_count": 1,
        "environment_directory_identity": node_identity_list(
            os.lstat(roots.environment_dir)
        ),
    }
    if not canonical_typed_equal(environment_binding, expected_environment_binding):
        raise ConfigError("runtime bootstrap plan lost its external environment binding")

    release_artifacts = inputs.get("release_artifacts")
    if not isinstance(release_artifacts, dict) or set(release_artifacts) != set(
        RUNTIME_BOOTSTRAP_ARTIFACT_NAMES
    ):
        raise ConfigError("runtime bootstrap plan release artifact set is not exact")
    installed_artifacts = result.get("installed_probe_artifacts")
    if not isinstance(installed_artifacts, dict):
        raise ConfigError("runtime bootstrap installed artifact projection is invalid")
    release_parent = roots.release_dll.parent
    release_parent_metadata = os.lstat(release_parent)
    release_parent_identity = node_identity_list(release_parent_metadata)
    release_parent_mtime = int(release_parent_metadata.st_mtime_ns)
    release_bindings: list[ImmutableFileBinding] = []
    for name in RUNTIME_BOOTSTRAP_ARTIFACT_NAMES:
        record = release_artifacts.get(name)
        if not isinstance(record, dict) or set(record) != {
            "path_id_lower",
            "path_id_upper",
            "parent_identity",
            "parent_last_write_ns",
            "identity",
            "length",
            "last_write_ns",
            "sha256",
        }:
            raise ConfigError("runtime bootstrap release artifact record is not exact")
        for key in ("parent_last_write_ns", "length", "last_write_ns"):
            require_exact_nonnegative_int(
                record.get(key), f"runtime bootstrap release artifact {key}"
            )
        require_identity_list(record.get("parent_identity"), "release parent identity")
        require_identity_list(record.get("identity"), "release artifact identity")
        artifact_hash = require_hash(
            record.get("sha256"), "runtime bootstrap release artifact hash"
        )
        artifact_path = release_parent / name
        artifact_payload, artifact_state = read_snapshot(
            artifact_path, maximum=MAX_MANAGED_ASSEMBLY_BYTES
        )
        artifact_metadata = os.lstat(artifact_path)
        expected_projection = installed_artifacts.get(name)
        if (
            not isinstance(expected_projection, dict)
            or set(expected_projection) != {"length", "sha256"}
            or type(expected_projection.get("length")) is not int
            or expected_projection["length"] < 0
            or require_hash(
                expected_projection.get("sha256"),
                "runtime bootstrap installed artifact hash",
            )
            != artifact_hash
            or expected_projection["length"] != len(artifact_payload)
            or record.get("path_id_lower")
            != path_id(artifact_path, contract="lower")
            or record.get("path_id_upper")
            != path_id(artifact_path, contract="upper")
            or not canonical_typed_equal(record.get("parent_identity"), release_parent_identity)
            or record.get("parent_last_write_ns") != release_parent_mtime
            or not canonical_typed_equal(
                record.get("identity"), node_identity_list(artifact_metadata)
            )
            or record.get("length") != artifact_state["length"]
            or record.get("last_write_ns") != artifact_state["last_write_ns"]
            or artifact_state["sha256"] != artifact_hash
        ):
            raise ConfigError("runtime bootstrap release artifact custody is invalid")
        release_bindings.append(
            ImmutableFileBinding(
                path=artifact_path,
                boundary=roots.test_root or PROJECT_ROOT,
                state=copy.deepcopy(artifact_state),
                identity=metadata_identity(artifact_metadata),
                label=f"runtime bootstrap Release artifact {name}",
            )
        )
    if release_artifacts[RUNTIME_BOOTSTRAP_ARTIFACT_NAMES[0]]["sha256"] != result.get(
        "release_dll_sha256"
    ):
        raise ConfigError("runtime bootstrap Release DLL projection is invalid")

    prestate = plan.get("prestate")
    if not isinstance(prestate, dict) or set(prestate) != {
        "probe",
        "old_bridge",
        "archive_root",
        "parent_directories",
    }:
        raise ConfigError("runtime bootstrap prestate field set is not exact")
    backup = manifest.get("backup") if isinstance(manifest.get("backup"), dict) else {}
    for key, backup_key in (
        ("probe", "probe"),
        ("old_bridge", "old_bridge"),
        ("archive_root", "bridge_archive_root"),
    ):
        record = prestate.get(key)
        if not isinstance(record, dict) or set(record) != {"snapshot", "identity"}:
            raise ConfigError(f"runtime bootstrap {key} prestate is not exact")
        expected_snapshot = (
            backup.get(backup_key, {}).get("snapshot")
            if isinstance(backup.get(backup_key), dict)
            else None
        )
        if not canonical_typed_equal(record.get("snapshot"), expected_snapshot):
            raise ConfigError(f"runtime bootstrap {key} prestate lost its manifest binding")
        validate_runtime_tree_contract(record.get("snapshot"), record.get("identity"), key)
    parent_directories = prestate.get("parent_directories")
    expected_parent_paths = {
        "oni_root": roots.oni_root,
        "probe_container": roots.oni_root / "mods",
        "probe_parent": roots.oni_root / "mods" / "Dev",
    }
    if not isinstance(parent_directories, dict) or set(parent_directories) != set(
        expected_parent_paths
    ):
        raise ConfigError("runtime bootstrap parent-directory prestate is not exact")
    for key, path in expected_parent_paths.items():
        custody = parent_directories.get(key)
        metadata = os.lstat(path)
        if (
            not isinstance(custody, dict)
            or set(custody) != {"identity", "last_write_ns"}
            or not canonical_typed_equal(
                require_identity_list(custody.get("identity"), f"{key} identity"),
                node_identity_list(metadata),
            )
        ):
            raise ConfigError("runtime bootstrap parent-directory custody is invalid")
        # The install legitimately mutates the parent directory timestamps;
        # only their stable identities remain current-state custody anchors.
        require_exact_nonnegative_int(custody.get("last_write_ns"), f"{key} mtime")

    targets = {
        "probe_relative": PROBE_RELATIVE.as_posix(),
        "bridge_relative": BRIDGE_RELATIVE.as_posix(),
        "archive_root_relative": "ai-bridge-archives",
        "evidence_relative": f"{roots.run_id}/{RUNTIME_BOOTSTRAP_RELATIVE.as_posix()}",
    }
    if not canonical_typed_equal(plan.get("targets"), targets):
        raise ConfigError("runtime bootstrap plan target set is not exact")
    desired = plan.get("desired")
    expected_action_policy = {
        "schema": POLICY_SCHEMA,
        "actions_enabled": False,
        "suggestions_enabled": False,
        "allowed_save_hashes": [],
        "allowed_actions": ["focus_note", "noop"],
        "command_ttl_seconds": 30,
        "max_pending_commands": 20,
        "generated_at": manifest.get("created_at"),
    }
    expected_control_policy = {
        "schema": "oni.validation-control-policy.v1",
        "controls_enabled": False,
        "test_run_id": None,
        "allowed_operations": [],
        "allowed_save_hashes": [],
        "allowed_save_path_ids": [],
        "allowed_target_save_path_ids": [],
        "generated_at": manifest.get("created_at"),
        "expires_at": None,
    }
    old_bridge_exists = prestate["old_bridge"]["snapshot"].get("exists") is True
    expected_archive_relative = (
        f"ai-bridge-archives/{roots.run_id}-preexisting-{result['transaction_id']}"
        if old_bridge_exists
        else None
    )
    old_probe_exists = prestate["probe"]["snapshot"].get("exists") is True
    expected_probe_recovery = (
        "recovery/old-probe"
        if old_probe_exists
        else None
    )
    if (
        not isinstance(desired, dict)
        or set(desired) != {
            "bootstrap",
            "action_policy",
            "validation_control_policy",
            "archive_relative",
        }
        or not canonical_typed_equal(desired.get("bootstrap"), bootstrap)
        or not canonical_typed_equal(desired.get("action_policy"), expected_action_policy)
        or not canonical_typed_equal(
            desired.get("validation_control_policy"), expected_control_policy
        )
        or desired.get("archive_relative") != expected_archive_relative
        or result.get("archive_relative") != expected_archive_relative
        or result.get("old_probe_recovery_relative") != expected_probe_recovery
    ):
        raise ConfigError("runtime bootstrap desired document set is not exact")
    expected_steps = [
        "stage_probe",
        "stage_bridge",
        "ensure_archive_root_if_needed",
        "archive_old_bridge_if_present",
        "recover_old_probe_if_present",
        "install_probe",
        "install_bridge",
    ]
    if not canonical_typed_equal(plan.get("physical_steps"), expected_steps):
        raise ConfigError("runtime bootstrap physical step sequence is not exact")
    seed = {
        "schema": "oni.live-runtime-bootstrap-seed.v1",
        "run_id": roots.run_id,
        "expected_mod_version": result.get("expected_mod_version"),
        "inputs": copy.deepcopy(inputs),
        "prestate": copy.deepcopy(prestate),
        "targets": targets,
    }
    seed_hash = sha256_bytes(canonical_bytes(seed))
    expected_transaction_id = "rtb-" + seed_hash.removeprefix("sha256:")[:24]
    if (
        plan.get("seed_hash") != seed_hash
        or result.get("transaction_id") != expected_transaction_id
    ):
        raise ConfigError("runtime bootstrap deterministic transaction identity is invalid")

    stage_binding = ImmutableFileBinding(
        path=stage_path,
        boundary=roots.evidence_root,
        state=copy.deepcopy(stage_state),
        identity=capture_bound_regular_identity(
            stage_path,
            roots.evidence_root,
            stage_state,
            "runtime bootstrap install stage",
        ),
        label="runtime bootstrap install stage",
    )
    return stage_binding, release_bindings


def validate_runtime_bootstrap_commit_entry(
    result: Mapping[str, Any],
) -> None:
    entry = result.get("install_commit_entry")
    if not isinstance(entry, dict) or set(entry) != {
        "schema",
        "index",
        "transaction_id",
        "plan_hash",
        "event",
        "data",
        "previous_entry_hash",
    }:
        raise ConfigError("runtime bootstrap commit entry shape is invalid")
    data = entry.get("data")
    if not isinstance(data, dict) or set(data) != {
        "probe_tree_content_hash",
        "bridge_tree_content_hash",
        "archive_relative",
        "old_probe_recovery",
    }:
        raise ConfigError("runtime bootstrap commit entry data shape is invalid")
    require_exact_nonnegative_int(entry.get("index"), "runtime bootstrap commit index")
    for key in (
        "plan_hash",
        "previous_entry_hash",
        "probe_tree_content_hash",
        "bridge_tree_content_hash",
    ):
        value = entry.get(key) if key in entry else data.get(key)
        require_hash(value, f"runtime bootstrap commit {key}")
    if (
        entry.get("schema") != RUNTIME_BOOTSTRAP_JOURNAL_SCHEMA
        or entry.get("transaction_id") != result.get("transaction_id")
        or entry.get("plan_hash") != result.get("plan_hash")
        or entry.get("event") != "commit_verified"
        or data.get("probe_tree_content_hash")
        != result.get("probe_tree_content_hash")
        or data.get("bridge_tree_content_hash")
        != result.get("bridge_tree_content_hash")
        or data.get("archive_relative") != result.get("archive_relative")
        or data.get("old_probe_recovery")
        != result.get("old_probe_recovery_relative")
        or (
            data.get("archive_relative") is not None
            and not isinstance(data.get("archive_relative"), str)
        )
        or (
            data.get("old_probe_recovery") is not None
            and not isinstance(data.get("old_probe_recovery"), str)
        )
    ):
        raise ConfigError("runtime bootstrap commit entry cross-binding is invalid")
    entry_hash = require_hash(
        result.get("install_commit_entry_hash"),
        "runtime bootstrap commit entry hash",
    )
    if sha256_bytes(canonical_bytes(entry)) != entry_hash:
        raise ConfigError("runtime bootstrap commit entry hash is invalid")


def validate_runtime_bootstrap_lock_contract(
    roots: Roots,
    result: Mapping[str, Any],
    *,
    prefix: str,
    purpose: str,
    allowed_config_lock_identity: Optional[tuple[int, int]],
    active_parent_finalize_lock: Optional[ActiveParentFinalizeLock],
) -> tuple[list[ImmutableFileBinding], list[ImmutableDirectoryBinding]]:
    epoch = require_exact_nonnegative_int(
        result.get(f"{prefix}_lock_epoch"),
        f"runtime bootstrap {prefix} lock epoch",
    )
    if epoch > 9999:
        raise ConfigError("runtime bootstrap lock epoch exceeds its path contract")
    claim = result.get(f"{prefix}_lock_claim")
    release = result.get(f"{prefix}_lock_release")
    if not isinstance(claim, dict) or set(claim) != {
        "schema",
        "transaction_id",
        "plan_hash",
        "epoch",
        "purpose",
        "intent_hash",
        "payload_sha256",
        "lock_identity",
        "claim_hash",
    }:
        raise ConfigError(f"runtime bootstrap {prefix} lock claim shape is invalid")
    if not isinstance(release, dict) or set(release) != {
        "schema",
        "transaction_id",
        "plan_hash",
        "epoch",
        "purpose",
        "intent_hash",
        "claim_hash",
        "released_identity",
        "payload_sha256",
        "release_hash",
    }:
        raise ConfigError(f"runtime bootstrap {prefix} lock release shape is invalid")
    lock_identity = require_identity_list(
        claim.get("lock_identity"),
        f"runtime bootstrap {prefix} lock identity",
    )
    require_exact_nonnegative_int(
        claim.get("epoch"), f"runtime bootstrap {prefix} claim epoch"
    )
    require_exact_nonnegative_int(
        release.get("epoch"), f"runtime bootstrap {prefix} release epoch"
    )
    require_identity_list(
        release.get("released_identity"),
        f"runtime bootstrap {prefix} released identity",
    )
    claim_hash = require_hash(
        claim.get("claim_hash"), f"runtime bootstrap {prefix} claim hash"
    )
    release_hash = require_hash(
        release.get("release_hash"), f"runtime bootstrap {prefix} release hash"
    )
    for document, hash_key, claimed, label in (
        (claim, "claim_hash", claim_hash, "claim"),
        (release, "release_hash", release_hash, "release"),
    ):
        unsigned = dict(document)
        del unsigned[hash_key]
        if sha256_bytes(canonical_bytes(unsigned)) != claimed:
            raise ConfigError(
                f"runtime bootstrap {prefix} lock {label} self-hash is invalid"
            )
    if (
        claim.get("schema") != RUNTIME_BOOTSTRAP_LOCK_CLAIM_SCHEMA
        or release.get("schema") != RUNTIME_BOOTSTRAP_LOCK_RELEASE_SCHEMA
        or claim.get("transaction_id") != result.get("transaction_id")
        or release.get("transaction_id") != result.get("transaction_id")
        or claim.get("plan_hash") != result.get("plan_hash")
        or release.get("plan_hash") != result.get("plan_hash")
        or claim.get("epoch") != epoch
        or release.get("epoch") != epoch
        or claim.get("purpose") != purpose
        or release.get("purpose") != purpose
        or release.get("intent_hash") != claim.get("intent_hash")
        or release.get("claim_hash") != claim_hash
        or release.get("payload_sha256") != claim.get("payload_sha256")
        or not canonical_typed_equal(release.get("released_identity"), lock_identity)
        or result.get(f"{prefix}_lock_claim_hash") != claim_hash
        or result.get(f"{prefix}_lock_release_hash") != release_hash
    ):
        raise ConfigError(f"runtime bootstrap {prefix} lock cross-binding is invalid")
    require_hash(
        claim.get("intent_hash"), f"runtime bootstrap {prefix} lock intent hash"
    )
    payload_hash = require_hash(
        claim.get("payload_sha256"),
        f"runtime bootstrap {prefix} lock payload hash",
    )
    attempt_root = roots.runtime_bootstrap_dir / "environment-lock-attempts"
    attempt = attempt_root / f"{epoch:04d}-{purpose}"
    assert_no_reparse(attempt_root, roots.evidence_root)
    assert_no_reparse(attempt, roots.evidence_root)
    attempt_root_metadata = os.lstat(attempt_root)
    attempt_metadata = os.lstat(attempt)
    if (
        not stat.S_ISDIR(attempt_root_metadata.st_mode)
        or not stat.S_ISDIR(attempt_metadata.st_mode)
    ):
        raise ConfigError("runtime bootstrap lock attempt custody is not directory-backed")
    claim_path = attempt / "claim.json"
    release_path = attempt / "released.json"
    released_lock_path = attempt / "released.lock"
    claim_document, _claim_payload, claim_state = read_exact_json_document(
        claim_path,
        f"runtime bootstrap {prefix} lock claim",
        boundary=roots.evidence_root,
    )
    release_document, _release_payload, release_state = read_exact_json_document(
        release_path,
        f"runtime bootstrap {prefix} lock release",
        boundary=roots.evidence_root,
    )
    released_payload, released_state = read_snapshot(released_lock_path)
    released_metadata = os.lstat(released_lock_path)
    if (
        not canonical_typed_equal(claim_document, claim)
        or not canonical_typed_equal(release_document, release)
        or released_state.get("sha256") != payload_hash
        or sha256_bytes(released_payload) != payload_hash
        or node_identity_list(released_metadata) != lock_identity
        or released_metadata.st_nlink != 1
    ):
        raise ConfigError(f"runtime bootstrap {prefix} lock disk custody is invalid")
    live_lock = roots.environment_dir / LOCK_NAME
    if (
        allowed_config_lock_identity is not None
        and active_parent_finalize_lock is not None
    ):
        raise ConfigError("multiple active environment lock proofs are forbidden")
    if active_parent_finalize_lock is not None:
        require_current_active_parent_finalize_lock(
            roots,
            active_parent_finalize_lock,
        )
    if os.path.lexists(live_lock):
        if active_parent_finalize_lock is not None:
            pass
        elif allowed_config_lock_identity is None:
            raise ConfigError("runtime bootstrap shared lock is still named")
        else:
            assert_no_reparse(live_lock, roots.evidence_root)
            live_payload, _live_state = read_snapshot(live_lock)
            live_metadata = os.lstat(live_lock)
            live_document = parse_json_payload(
                live_payload,
                "runtime config environment lock",
            )
            if (
                metadata_identity(live_metadata) != allowed_config_lock_identity
                or live_metadata.st_nlink != 1
                or set(live_document) != {"schema", "run_id", "owner_token"}
                or live_document.get("schema")
                != "oni.live-validation-runtime-config-lock.v1"
                or live_document.get("run_id") != roots.run_id
                or not isinstance(live_document.get("owner_token"), str)
                or re.fullmatch(r"[0-9a-f]{32}", live_document["owner_token"])
                is None
            ):
                raise ConfigError("runtime bootstrap shared lock release custody is invalid")
    file_bindings: list[ImmutableFileBinding] = []
    for path, state_value, label in (
        (claim_path, claim_state, f"runtime bootstrap {prefix} lock claim"),
        (release_path, release_state, f"runtime bootstrap {prefix} lock release"),
        (released_lock_path, released_state, f"runtime bootstrap {prefix} released lock"),
    ):
        file_bindings.append(
            ImmutableFileBinding(
                path=path,
                boundary=roots.evidence_root,
                state=copy.deepcopy(state_value),
                identity=capture_bound_regular_identity(
                    path, roots.evidence_root, state_value, label
                ),
                label=label,
            )
        )
    directory_bindings = [
        ImmutableDirectoryBinding(
            path=attempt_root,
            boundary=roots.evidence_root,
            identity=metadata_identity(attempt_root_metadata),
            label="runtime bootstrap lock attempt root",
        ),
        ImmutableDirectoryBinding(
            path=attempt,
            boundary=roots.evidence_root,
            identity=metadata_identity(attempt_metadata),
            label=f"runtime bootstrap {prefix} lock attempt",
        ),
    ]
    return file_bindings, directory_bindings


def validate_runtime_bootstrap_result(
    roots: Roots,
    manifest: Mapping[str, Any],
    bootstrap: Mapping[str, Any],
    bootstrap_payload: bytes,
    bootstrap_state: Mapping[str, Any],
    probe_tree: Mapping[str, Any],
    bridge_content: Mapping[str, Any],
    *,
    phase: str,
    allowed_config_lock_identity: Optional[tuple[int, int]],
    active_parent_finalize_lock: Optional[ActiveParentFinalizeLock],
    require_current_bridge_content: bool,
    _allow_historical_journal_append: bool = False,
) -> tuple[
    dict[str, Any],
    list[ImmutableFileBinding],
    list[ImmutableDirectoryBinding],
]:
    if type(_allow_historical_journal_append) is not bool or (
        _allow_historical_journal_append and phase != "evidence"
    ):
        raise ConfigError("internal historical runtime result mode is invalid")
    result, result_payload, result_state = read_exact_json_document(
        roots.runtime_bootstrap_result,
        "runtime bootstrap installed-ready result",
        boundary=roots.evidence_root,
        expected_raw_sha256=roots.expected_runtime_bootstrap_result_sha256,
    )
    if set(result) != RUNTIME_BOOTSTRAP_RESULT_KEYS:
        raise ConfigError("runtime bootstrap result field set is not exact")
    result_hash = require_hash(result.get("result_hash"), "runtime bootstrap result hash")
    unsigned_result = dict(result)
    del unsigned_result["result_hash"]
    if sha256_bytes(canonical_bytes(unsigned_result)) != result_hash:
        raise ConfigError("runtime bootstrap result self-hash is invalid")
    transaction_id = result.get("transaction_id")
    if (
        result.get("schema") != RUNTIME_BOOTSTRAP_RESULT_SCHEMA
        or result.get("status") != "installed_ready"
        or result.get("classification") != "static_probe"
        or result.get("run_id") != roots.run_id
        or not isinstance(transaction_id, str)
        or RUNTIME_BOOTSTRAP_TRANSACTION_ID_RE.fullmatch(transaction_id) is None
        or result.get("environment_manifest_sha256")
        != roots.expected_environment_manifest_sha256
        or result.get("release_dll_sha256") != roots.expected_release_dll_sha256
        or result.get("expected_mod_version") != EXPECTED_MOD_VERSION
        or result.get("physical_commit_complete") is not True
        or result.get("shared_lock_released") is not True
        or result.get("game_launched") is not False
    ):
        raise ConfigError("runtime bootstrap result does not bind the installed run")
    for key in (
        "plan_hash",
        "environment_manifest_sha256",
        "release_dll_sha256",
        "probe_tree_content_hash",
        "bridge_tree_content_hash",
        "bootstrap_document_hash",
        "bootstrap_file_sha256",
        "install_commit_entry_hash",
        "install_stage_sha256",
        "install_stage_hash",
        "install_lock_claim_hash",
        "install_lock_release_hash",
        "ready_lock_claim_hash",
        "ready_lock_release_hash",
        "candidate_sha256",
        "candidate_hash",
    ):
        require_hash(result.get(key), f"runtime bootstrap result {key}")
    for key in ("archive_relative", "old_probe_recovery_relative"):
        value = result.get(key)
        if value is not None:
            if not isinstance(value, str):
                raise ConfigError(f"runtime bootstrap result {key} is invalid")
            validate_relative(value, f"runtime bootstrap result {key}")
    if (
        result.get("archive_relative") is not None
        and not str(result["archive_relative"]).startswith("ai-bridge-archives/")
    ):
        raise ConfigError("runtime bootstrap archive projection is outside its fixed root")
    if result.get("old_probe_recovery_relative") not in {
        None,
        "recovery/old-probe",
    }:
        raise ConfigError("runtime bootstrap old-Probe recovery projection is invalid")

    installed_artifacts = result.get("installed_probe_artifacts")
    if not isinstance(installed_artifacts, dict) or set(installed_artifacts) != set(
        RUNTIME_BOOTSTRAP_ARTIFACT_NAMES
    ):
        raise ConfigError("runtime bootstrap installed artifact field set is not exact")
    for name in RUNTIME_BOOTSTRAP_ARTIFACT_NAMES:
        item = installed_artifacts.get(name)
        if not isinstance(item, dict) or set(item) != {"length", "sha256"}:
            raise ConfigError("runtime bootstrap installed artifact record is not exact")
        require_exact_nonnegative_int(
            item.get("length"), f"runtime bootstrap installed {name} length"
        )
        require_hash(item.get("sha256"), f"runtime bootstrap installed {name} hash")
    if not canonical_typed_equal(installed_artifacts, probe_tree.get("artifacts")):
        raise ConfigError("runtime bootstrap artifact projection differs from live Probe")
    if (
        result.get("probe_tree_content_hash") != probe_tree.get("content_hash")
        or (
            require_current_bridge_content
            and result.get("bridge_tree_content_hash")
            != bridge_content.get("content_hash")
        )
        or installed_artifacts[RUNTIME_BOOTSTRAP_ARTIFACT_NAMES[0]]["sha256"]
        != roots.expected_release_dll_sha256
    ):
        raise ConfigError("runtime bootstrap installed tree content binding is invalid")
    validate_identity_tree(
        result.get("installed_probe_tree_identity"),
        probe_tree.get("identity") if isinstance(probe_tree.get("identity"), dict) else {},
    )

    if bootstrap_payload != json_document_bytes(bootstrap):
        raise ConfigError("runtime bootstrap live document serialization is not exact")
    if (
        result.get("bootstrap_document_hash")
        != sha256_bytes(canonical_bytes(bootstrap))
        or result.get("bootstrap_file_sha256") != bootstrap_state.get("sha256")
        or result.get("bootstrap_file_sha256") != sha256_bytes(bootstrap_payload)
        or bootstrap.get("prepared_at") != manifest.get("created_at")
    ):
        raise ConfigError("runtime bootstrap live bootstrap hash/cross-binding is invalid")
    archived_bridge = bootstrap.get("archived_previous_bridge")
    if archived_bridge is None:
        expected_archive_relative: Optional[str] = None
    else:
        if not isinstance(archived_bridge, str):
            raise ConfigError("runtime bootstrap archive path is invalid")
        archive_path = require_absolute_local_path(
            archived_bridge, "runtime bootstrap archived Bridge"
        )
        if not path_within(archive_path, roots.oni_root):
            raise ConfigError("runtime bootstrap archive path escaped the ONI root")
        expected_archive_relative = pathlib.Path(
            os.path.relpath(archive_path, roots.oni_root)
        ).as_posix()
        validate_relative(expected_archive_relative, "runtime bootstrap archive relative")
    if result.get("archive_relative") != expected_archive_relative:
        raise ConfigError("runtime bootstrap archive path/result projection is invalid")

    validate_runtime_bootstrap_commit_entry(result)
    stage_binding, release_bindings = validate_runtime_bootstrap_stage_contract(
        roots,
        manifest,
        result,
        bootstrap,
        allow_current_journal_append=(
            phase == "evidence" and active_parent_finalize_lock is not None
        )
        or _allow_historical_journal_append,
    )
    install_files, install_directories = validate_runtime_bootstrap_lock_contract(
        roots,
        result,
        prefix="install",
        purpose="install",
        allowed_config_lock_identity=allowed_config_lock_identity,
        active_parent_finalize_lock=active_parent_finalize_lock,
    )
    ready_files, ready_directories = validate_runtime_bootstrap_lock_contract(
        roots,
        result,
        prefix="ready",
        purpose="replay",
        allowed_config_lock_identity=allowed_config_lock_identity,
        active_parent_finalize_lock=active_parent_finalize_lock,
    )
    if result["ready_lock_epoch"] <= result["install_lock_epoch"]:
        raise ConfigError("runtime bootstrap ready lock did not follow install lock")

    candidate: dict[str, Any] = {
        "schema": RUNTIME_BOOTSTRAP_RESULT_CANDIDATE_SCHEMA,
        "status": "candidate_not_ready",
        "classification": result["classification"],
        "run_id": result["run_id"],
        "transaction_id": result["transaction_id"],
        "plan_hash": result["plan_hash"],
        "environment_manifest_sha256": result["environment_manifest_sha256"],
        "release_dll_sha256": result["release_dll_sha256"],
        "expected_mod_version": result["expected_mod_version"],
        "probe_tree_content_hash": result["probe_tree_content_hash"],
        "installed_probe_artifacts": copy.deepcopy(result["installed_probe_artifacts"]),
        "installed_probe_tree_identity": copy.deepcopy(
            result["installed_probe_tree_identity"]
        ),
        "bridge_tree_content_hash": result["bridge_tree_content_hash"],
        "bootstrap_document_hash": result["bootstrap_document_hash"],
        "bootstrap_file_sha256": result["bootstrap_file_sha256"],
        "archive_relative": result["archive_relative"],
        "old_probe_recovery_relative": result["old_probe_recovery_relative"],
        "install_commit_entry": copy.deepcopy(result["install_commit_entry"]),
        "install_commit_entry_hash": result["install_commit_entry_hash"],
        "install_stage_sha256": result["install_stage_sha256"],
        "install_stage_hash": result["install_stage_hash"],
        "producer": copy.deepcopy(result["producer"]),
        "install_lock_epoch": result["install_lock_epoch"],
        "install_lock_claim": copy.deepcopy(result["install_lock_claim"]),
        "install_lock_claim_hash": result["install_lock_claim_hash"],
        "install_lock_release": copy.deepcopy(result["install_lock_release"]),
        "install_lock_release_hash": result["install_lock_release_hash"],
        "physical_commit_complete": True,
        "game_launched": False,
    }
    candidate["candidate_hash"] = sha256_bytes(canonical_bytes(candidate))
    if (
        candidate["candidate_hash"] != result.get("candidate_hash")
        or sha256_bytes(json_document_bytes(candidate)) != result.get("candidate_sha256")
    ):
        raise ConfigError("runtime bootstrap candidate reconstruction is invalid")

    producer_path = PROJECT_ROOT / "scripts" / "manage_live_runtime_bootstrap.py"
    producer_payload, producer_state = read_snapshot(producer_path)
    producer_metadata = os.lstat(producer_path)
    producer_projection = {
        "length": len(producer_payload),
        "sha256": producer_state["sha256"],
    }
    if not canonical_typed_equal(result.get("producer"), producer_projection):
        raise ConfigError("runtime bootstrap result producer does not match frozen source")

    result_binding = ImmutableFileBinding(
        path=roots.runtime_bootstrap_result,
        boundary=roots.evidence_root,
        state=copy.deepcopy(result_state),
        identity=capture_bound_regular_identity(
            roots.runtime_bootstrap_result,
            roots.evidence_root,
            result_state,
            "runtime bootstrap installed-ready result",
        ),
        label="runtime bootstrap installed-ready result",
    )
    producer_binding = ImmutableFileBinding(
        path=producer_path,
        boundary=PROJECT_ROOT,
        state=copy.deepcopy(producer_state),
        identity=metadata_identity(producer_metadata),
        label="runtime bootstrap producer",
    )
    evidence = {
        "path_id_lower": path_id(roots.runtime_bootstrap_result, contract="lower"),
        "external_expected_sha256": roots.expected_runtime_bootstrap_result_sha256,
        "raw_sha256": result_state["sha256"],
        "raw_byte_length": len(result_payload),
        "raw_utf8_base64": base64.b64encode(result_payload).decode("ascii"),
        "result_hash": result_hash,
        "transaction_id": transaction_id,
        "probe_tree_content_hash": result["probe_tree_content_hash"],
        "document": copy.deepcopy(result),
    }
    directories: list[ImmutableDirectoryBinding] = []
    seen_directories: set[str] = set()
    for binding in install_directories + ready_directories:
        key = canonical_path_text(binding.path)
        if key not in seen_directories:
            seen_directories.add(key)
            directories.append(binding)
    files = [
        result_binding,
        stage_binding,
        producer_binding,
        *release_bindings,
        *install_files,
        *ready_files,
    ]
    return evidence, files, directories


def validate_bridge_and_probe(
    roots: Roots,
    manifest: Mapping[str, Any],
    expected_mod_version: str,
    *,
    phase: str,
    require_disabled_policy: bool,
    allowed_config_lock_identity: Optional[tuple[int, int]] = None,
    active_parent_finalize_lock: Optional[ActiveParentFinalizeLock] = None,
) -> tuple[
    pathlib.Path,
    dict[str, Any],
    pathlib.Path,
    pathlib.Path,
    dict[str, Any],
    tuple[ImmutableFileBinding, ...],
    tuple[ImmutableDirectoryBinding, ...],
]:
    if phase not in {"before", "evidence"} or require_disabled_policy != (
        phase == "before"
    ):
        raise ConfigError("internal Bridge/Probe validation phase is inconsistent")
    bridge = roots.oni_root / pathlib.Path(*BRIDGE_RELATIVE.parts)
    assert_no_reparse(bridge, roots.oni_root)
    if not bridge.is_dir():
        raise ConfigError("runtime bridge is missing")
    bootstrap_path = bridge / "v27-runtime-bootstrap.json"
    policy_path = bridge / "action-policy.json"
    validation_policy_path = bridge / "validation-control-policy.json"
    bootstrap, bootstrap_payload, bootstrap_state = read_exact_json_document(
        bootstrap_path,
        "runtime bootstrap",
        boundary=roots.oni_root,
    )
    built_hash, installed_hash = validate_bootstrap_document(
        bootstrap,
        roots,
        bridge,
        expected_mod_version,
    )
    validation_policy, _validation_policy_payload, _validation_policy_state = (
        read_exact_json_document(
            validation_policy_path,
            "runtime validation-control policy",
            boundary=roots.oni_root,
        )
    )
    expected_validation_policy = {
        "schema": "oni.validation-control-policy.v1",
        "controls_enabled": False,
        "test_run_id": None,
        "allowed_operations": [],
        "allowed_save_hashes": [],
        "allowed_save_path_ids": [],
        "allowed_target_save_path_ids": [],
        "generated_at": manifest.get("created_at"),
        "expires_at": None,
    }
    if not canonical_typed_equal(validation_policy, expected_validation_policy):
        raise ConfigError("runtime Bridge static validation-control policy changed")
    release_payload, release_state = read_snapshot(roots.release_dll)
    if (
        release_state["sha256"] != roots.expected_release_dll_sha256
        or sha256_bytes(release_payload) != roots.expected_release_dll_sha256
        or built_hash != roots.expected_release_dll_sha256
        or installed_hash != roots.expected_release_dll_sha256
    ):
        raise ConfigError("external/Release/bootstrap DLL anchors are not four-way identical")

    policy: Optional[dict[str, Any]] = None
    policy_state: Optional[dict[str, Any]] = None
    if require_disabled_policy:
        policy_payload, policy_state = read_snapshot(policy_path)
        if policy_payload.startswith(b"\xef\xbb\xbf"):
            raise ConfigError("action policy must not contain a UTF-8 BOM")
        policy = parse_json_payload(policy_payload, "action policy")
        validate_disabled_policy_document(policy)
    validate_bridge_surface(bridge, phase=phase)
    bridge_content = capture_runtime_bridge_content(bridge)

    probe = roots.oni_root / pathlib.Path(*PROBE_RELATIVE.parts)
    assert_no_reparse(probe, roots.oni_root)
    if not probe.is_dir():
        raise ConfigError("installed Probe directory is missing")
    dll = probe / "OniAiProbeMod.dll"
    pdb = probe / "OniAiProbeMod.pdb"
    mod_yaml = probe / "mod.yaml"
    mod_info = probe / "mod_info.yaml"
    for item in (dll, pdb, mod_yaml, mod_info):
        assert_no_reparse(item, probe)
        if not item.is_file():
            raise ConfigError("installed Probe exact four-file artifact set is missing")
    probe_tree = capture_exact_probe_tree(
        probe,
        allow_runtime_nodes=phase == "evidence",
    )
    dll_payload, dll_state = read_snapshot(dll)
    if dll_state["sha256"] != installed_hash or dll_payload != release_payload:
        raise ConfigError("installed Probe DLL changed after runtime bootstrap")
    mod_yaml_payload, mod_yaml_state = read_snapshot(mod_yaml)
    mod_info_payload, mod_info_state = read_snapshot(mod_info)
    validate_simple_yaml_scalar(mod_yaml_payload, "staticID", PROBE_STATIC_ID)
    validate_simple_yaml_scalar(mod_info_payload, "APIVersion", "2")

    backup = manifest.get("backup") if isinstance(manifest.get("backup"), dict) else {}
    probe_record = backup.get("probe") if isinstance(backup.get("probe"), dict) else {}
    if (
        probe_record.get("relative") != PROBE_RELATIVE.as_posix()
        or probe_record.get("path_id_lower") != path_id(probe, contract="lower")
        or probe_record.get("path_id_upper") != path_id(probe, contract="upper")
    ):
        raise ConfigError("environment manifest Probe binding is invalid")
    result_evidence, runtime_files, runtime_directories = (
        validate_runtime_bootstrap_result(
            roots,
            manifest,
            bootstrap,
            bootstrap_payload,
            bootstrap_state,
            probe_tree,
            bridge_content,
            phase=phase,
            allowed_config_lock_identity=allowed_config_lock_identity,
            active_parent_finalize_lock=active_parent_finalize_lock,
            require_current_bridge_content=require_disabled_policy,
        )
    )
    runtime = {
        "bootstrap": {
            "path_id_lower": path_id(bootstrap_path, contract="lower"),
            "raw_sha256": bootstrap_state["sha256"],
            "raw_byte_length": len(bootstrap_payload),
            "raw_utf8_base64": base64.b64encode(bootstrap_payload).decode("ascii"),
            "document_hash": sha256_bytes(canonical_bytes(bootstrap)),
            "document": copy.deepcopy(bootstrap),
        },
        "runtime_bootstrap_result": result_evidence,
        "release_dll": {
            "path_id_lower": path_id(roots.release_dll, contract="lower"),
            "external_expected_sha256": roots.expected_release_dll_sha256,
            "file_sha256": release_state["sha256"],
        },
        "installed_probe": {
            "dll_sha256": probe_tree["artifacts"]["OniAiProbeMod.dll"]["sha256"],
            "pdb_sha256": probe_tree["artifacts"]["OniAiProbeMod.pdb"]["sha256"],
            "manifest_sha256": probe_tree["artifacts"]["mod.yaml"]["sha256"],
            "info_sha256": probe_tree["artifacts"]["mod_info.yaml"]["sha256"],
            "tree_content_hash": probe_tree["content_hash"],
        },
        "mod_version": expected_mod_version,
        "probe_static_id": PROBE_STATIC_ID,
        "dll_four_way_identical": True,
    }
    if policy is not None and policy_state is not None:
        runtime["initial_disabled_policy"] = {
            "path_id_lower": path_id(policy_path, contract="lower"),
            "raw_sha256": policy_state["sha256"],
            "raw_byte_length": len(policy_payload),
            "raw_utf8_base64": base64.b64encode(policy_payload).decode("ascii"),
            "document_hash": sha256_bytes(canonical_bytes(policy)),
            "document": copy.deepcopy(policy),
        }
    return (
        bootstrap_path,
        bootstrap,
        policy_path,
        dll,
        runtime,
        tuple(runtime_files),
        tuple(runtime_directories),
    )


def validate_environment(
    args: argparse.Namespace,
    roots: Roots,
    *,
    phase: str,
    allowed_config_lock_identity: Optional[tuple[int, int]] = None,
    active_parent_finalize_lock: Optional[ActiveParentFinalizeLock] = None,
) -> RuntimeContext:
    if phase not in {"before", "evidence"}:
        raise ConfigError("internal config phase is invalid")
    immutable_environment_files: list[ImmutableFileBinding] = []
    immutable_runtime_directories: list[ImmutableDirectoryBinding] = []

    def bind_immutable_environment_file(
        path: pathlib.Path,
        boundary: pathlib.Path,
        expected_state: dict[str, Any],
        label: str,
    ) -> None:
        immutable_environment_files.append(
            ImmutableFileBinding(
                path=path,
                boundary=boundary,
                state=copy.deepcopy(expected_state),
                identity=capture_bound_regular_identity(
                    path,
                    boundary,
                    expected_state,
                    label,
                ),
                label=label,
            )
        )

    manifest_path = require_absolute_local_path(args.environment_manifest, "environment manifest")
    expected_manifest = roots.environment_dir / "environment-manifest.json"
    if not same_path(manifest_path, expected_manifest):
        raise ConfigError("environment manifest is not at its unique run path")
    assert_no_reparse(manifest_path, roots.evidence_root)
    manifest_payload, manifest_state = read_snapshot(manifest_path)
    expected_manifest_hash = roots.expected_environment_manifest_sha256
    if (
        manifest_state.get("sha256") != expected_manifest_hash
        or sha256_bytes(manifest_payload) != expected_manifest_hash
    ):
        raise ConfigError(
            "environment manifest does not match its external SHA-256 anchor"
        )
    manifest = parse_json_payload(manifest_payload, "environment manifest")
    expected_mode = "live" if roots.live_mode else "test_fixture"
    if (
        manifest.get("schema") != ENVIRONMENT_SCHEMA
        or manifest.get("run_id") != roots.run_id
        or manifest.get("mode") != expected_mode
        or manifest.get("path_disclosure") != "root_ids_and_relative_paths_only"
    ):
        raise ConfigError("environment manifest does not bind this run/mode")
    root_binding = manifest.get("roots") if isinstance(manifest.get("roots"), dict) else {}
    if (
        root_binding.get("oni_root_id_lower") != path_id(roots.oni_root, contract="lower")
        or root_binding.get("oni_root_id_upper") != path_id(roots.oni_root, contract="upper")
        or root_binding.get("evidence_root_id_lower") != path_id(roots.evidence_root, contract="lower")
        or root_binding.get("evidence_root_id_upper") != path_id(roots.evidence_root, contract="upper")
        or root_binding.get("same_local_volume") is not True
    ):
        raise ConfigError("environment manifest root identity is wrong")
    # Keep every downstream projection bound to the external value.  Never
    # derive the trust anchor from the manifest or restore journal.
    manifest_hash = expected_manifest_hash
    bind_immutable_environment_file(
        manifest_path,
        roots.evidence_root,
        manifest_state,
        "environment manifest",
    )
    journal_path = roots.environment_dir / "restore-journal.jsonl"
    _journal_payload, journal_state = read_snapshot(journal_path)
    journal = verify_journal(journal_path)
    if journal[0].get("event") != "environment_prepared" or (
        journal[0].get("data") or {}
    ).get("environment_manifest_sha256") != manifest_hash:
        raise ConfigError("restore journal does not bind the environment manifest")
    bind_immutable_environment_file(
        journal_path,
        roots.environment_dir,
        journal_state,
        "restore journal",
    )

    pre_path = require_absolute_local_path(args.pre_boundary, "pre-boundary report")
    clone_path = require_absolute_local_path(args.clone_manifest, "clone manifest")
    if not path_within(pre_path, roots.boundary_root) or not path_within(clone_path, roots.evidence_root):
        raise ConfigError("pre-boundary or clone manifest escaped its evidence root")
    assert_no_reparse(pre_path, roots.boundary_root)
    assert_no_reparse(clone_path, roots.evidence_root)
    pre_payload, pre_state = read_snapshot(pre_path)
    clone_payload, clone_state = read_snapshot(clone_path)
    pre = parse_json_payload(pre_payload, "pre-boundary report")
    clone = parse_json_payload(clone_payload, "clone manifest")
    bindings = manifest.get("bindings") if isinstance(manifest.get("bindings"), dict) else {}
    pre_binding = bindings.get("pre_boundary") if isinstance(bindings.get("pre_boundary"), dict) else {}
    clone_binding = bindings.get("clone_manifest") if isinstance(bindings.get("clone_manifest"), dict) else {}
    if (
        pre_binding.get("path_id") != path_id(pre_path, contract="lower")
        or pre_binding.get("sha256") != pre_state["sha256"]
        or clone_binding.get("path_id") != path_id(clone_path, contract="lower")
        or clone_binding.get("sha256") != clone_state["sha256"]
    ):
        raise ConfigError("pre-boundary or clone manifest changed after environment prepare")
    if (
        pre.get("schema") != BOUNDARY_SCHEMA
        or pre.get("phase") != "pre"
        or pre.get("run_id") != roots.run_id
        or pre.get("oni_root_id") != path_id(roots.oni_root, contract="lower")
        or pre.get("path_disclosure") != "identifiers_only"
        or pre_binding.get("oni_root_id") != pre.get("oni_root_id")
    ):
        raise ConfigError("pre-boundary report is not the frozen report for this run")
    if (
        clone.get("schema") != CLONE_SCHEMA
        or clone.get("run_id") != roots.run_id
        or clone.get("created") is not True
        or clone.get("path_disclosure") != "identifiers_only"
        or clone.get("oni_root_id") != path_id(roots.oni_root, contract="lower")
        or clone.get("target_stem_id") != text_id(args.loader_stem)
        or clone_binding.get("target_stem_id") != clone.get("target_stem_id")
    ):
        raise ConfigError("clone manifest is not the named loader clone for this run")
    bind_immutable_environment_file(
        pre_path,
        roots.boundary_root,
        pre_state,
        "pre-boundary report",
    )
    bind_immutable_environment_file(
        clone_path,
        roots.evidence_root,
        clone_state,
        "clone manifest",
    )

    source = require_absolute_local_path(args.source_save, "source save")
    loader = require_absolute_local_path(args.loader_save, "loader save")
    if source.suffix != ".sav" or loader.suffix != ".sav":
        raise ConfigError("source and loader must use the exact .sav extension")
    source_root = save_root_for(source, roots.oni_root)
    loader_root = save_root_for(loader, roots.oni_root)
    if (
        not same_path(source_root, loader_root)
        or not same_path(source.parent, loader.parent)
        or same_path(source, loader)
        or loader.stem != args.loader_stem
    ):
        raise ConfigError("source/loader are not the exact sibling named-clone pair")
    source_preview = source.with_suffix(".png")
    loader_preview = loader.with_suffix(".png")
    for item in (source, source_preview, loader, loader_preview):
        assert_no_reparse(item, source_root)
        if not item.is_file():
            raise ConfigError("source/loader save or preview is missing")
    expected_actual = {
        "source_save": source,
        "source_preview": source_preview,
        "loader_save": loader,
        "loader_preview": loader_preview,
    }
    for key, item in expected_actual.items():
        record = bindings.get(key) if isinstance(bindings.get(key), dict) else {}
        if (
            record.get("path_id_lower") != path_id(item, contract="lower")
            or record.get("path_id_upper") != path_id(item, contract="upper")
        ):
            raise ConfigError(f"environment manifest {key} binding changed")
        if key.startswith("source_") or phase == "before":
            if record.get("state") != file_state(item):
                raise ConfigError(f"environment manifest {key} content binding changed")
    source_save_identity: Optional[tuple[int, int]] = capture_bound_regular_identity(
        source,
        source_root,
        bindings["source_save"]["state"],
        "source save",
    )
    source_preview_identity: Optional[tuple[int, int]] = capture_bound_regular_identity(
        source_preview,
        source_root,
        bindings["source_preview"]["state"],
        "source preview",
    )
    loader_save_identity: Optional[tuple[int, int]] = None
    loader_preview_identity: Optional[tuple[int, int]] = None
    if phase == "before":
        loader_save_identity = capture_bound_regular_identity(
            loader,
            source_root,
            bindings["loader_save"]["state"],
            "loader save",
        )
        loader_preview_identity = capture_bound_regular_identity(
            loader_preview,
            source_root,
            bindings["loader_preview"]["state"],
            "loader preview",
        )
    loader_binding = bindings.get("loader_save") or {}
    if loader_binding.get("stem_id") != text_id(args.loader_stem):
        raise ConfigError("environment manifest loader stem binding is wrong")
    clone_source = clone.get("source") if isinstance(clone.get("source"), dict) else {}
    clone_target = clone.get("target") if isinstance(clone.get("target"), dict) else {}
    if (
        (clone_source.get("save") or {}).get("sha256") != file_state(source)["sha256"]
        or (clone_source.get("preview") or {}).get("sha256") != file_state(source_preview)["sha256"]
        or clone_target.get("directory_path_id") != path_id(loader.parent, contract="lower")
        or clone_target.get("save_path_id") != path_id(loader, contract="lower")
        or clone_target.get("preview_path_id") != path_id(loader_preview, contract="lower")
        or clone_target.get("save_sha256") != (bindings.get("loader_save") or {}).get("state", {}).get("sha256")
        or clone_target.get("preview_sha256") != (bindings.get("loader_preview") or {}).get("state", {}).get("sha256")
    ):
        raise ConfigError("clone manifest content/path binding no longer matches")

    backup = manifest.get("backup") if isinstance(manifest.get("backup"), dict) else {}
    records = backup.get("configs") if isinstance(backup.get("configs"), list) else []
    if len(records) != len(CONFIG_RELATIVES):
        raise ConfigError("environment config backup manifest is incomplete")
    config_records: dict[str, dict[str, Any]] = {}
    backup_bytes: dict[str, bytes] = {}
    current_bytes: dict[str, bytes] = {}
    current_states: dict[str, dict[str, Any]] = {}
    current_identities: dict[str, tuple[int, int]] = {}
    for raw_record in records:
        if not isinstance(raw_record, dict):
            raise ConfigError("environment config backup record is invalid")
        relative = str(raw_record.get("relative") or "")
        validate_relative(relative, "config backup")
        if relative not in CONFIG_RELATIVES or relative in config_records:
            raise ConfigError("environment config backup path set is invalid")
        current = roots.oni_root / pathlib.Path(*pathlib.PurePosixPath(relative).parts)
        if phase == "before":
            assert_no_reparse(current, roots.oni_root, allow_missing_leaf=not bool(raw_record.get("exists")))
        else:
            assert_no_reparse(current.parent, roots.oni_root)
            if os.path.lexists(current):
                assert_no_reparse(current, roots.oni_root)
        if (
            raw_record.get("path_id_lower") != path_id(current, contract="lower")
            or raw_record.get("path_id_upper") != path_id(current, contract="upper")
            or not isinstance(raw_record.get("exists"), bool)
        ):
            raise ConfigError("environment config path identity is invalid")
        config_records[relative] = raw_record
        if raw_record["exists"]:
            expected_backup = f"backup/config/{relative}"
            if raw_record.get("backup_relative") != expected_backup:
                raise ConfigError("environment config backup location is invalid")
            backup_path = roots.environment_dir / pathlib.Path(*pathlib.PurePosixPath(expected_backup).parts)
            assert_no_reparse(backup_path, roots.environment_dir)
            payload, state = read_snapshot(backup_path)
            if state != raw_record.get("state"):
                raise ConfigError("environment config backup bytes or timestamp changed")
            bind_immutable_environment_file(
                backup_path,
                roots.environment_dir,
                state,
                f"config backup {relative}",
            )
            backup_bytes[relative] = payload
        elif raw_record.get("backup_relative") is not None:
            raise ConfigError("absent config unexpectedly names a backup")
        if phase == "before":
            current_value = optional_state(current)
            expected_value = {
                key: value for key, value in raw_record.items() if key in {"exists", "state"}
            }
            if current_value != expected_value:
                raise ConfigError("current config drifted from the environment prepare snapshot")
        if relative in TARGET_CONFIG_RELATIVES and phase == "before":
            if not raw_record["exists"] or not current.is_file():
                raise ConfigError("required runtime config did not exist at environment prepare")
            payload, state = read_snapshot(current)
            current_bytes[relative] = payload
            current_states[relative] = state
            metadata = os.lstat(current)
            if (
                is_reparse(current)
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or state != file_state(current)
            ):
                raise ConfigError("current config identity changed after prepare-state validation")
            current_identities[relative] = metadata_identity(metadata)
    if set(config_records) != set(CONFIG_RELATIVES):
        raise ConfigError("environment config backup path set is incomplete")

    (
        bootstrap_path,
        bootstrap,
        policy_path,
        dll,
        runtime,
        runtime_files,
        runtime_directories,
    ) = validate_bridge_and_probe(
        roots,
        manifest,
        args.expected_mod_version,
        phase=phase,
        require_disabled_policy=phase == "before",
        allowed_config_lock_identity=allowed_config_lock_identity,
        active_parent_finalize_lock=active_parent_finalize_lock,
    )
    immutable_environment_files.extend(runtime_files)
    immutable_runtime_directories.extend(runtime_directories)
    bridge_metadata = os.lstat(bootstrap_path.parent)
    probe_metadata = os.lstat(dll.parent)
    if (
        is_reparse(bootstrap_path.parent)
        or not stat.S_ISDIR(bridge_metadata.st_mode)
        or is_reparse(dll.parent)
        or not stat.S_ISDIR(probe_metadata.st_mode)
    ):
        raise ConfigError("runtime Bridge/Probe directory identity is invalid")
    managed_assembly_binding = capture_managed_assembly_binding(roots)
    return RuntimeContext(
        roots=roots,
        phase=phase,
        manifest_path=manifest_path,
        manifest=manifest,
        manifest_sha256=manifest_hash,
        source_save=source,
        source_preview=source_preview,
        loader_save=loader,
        loader_preview=loader_preview,
        source_save_identity=source_save_identity,
        source_preview_identity=source_preview_identity,
        loader_save_identity=loader_save_identity,
        loader_preview_identity=loader_preview_identity,
        immutable_environment_files=tuple(immutable_environment_files),
        loader_stem=args.loader_stem,
        config_records=config_records,
        backup_bytes=backup_bytes,
        current_bytes=current_bytes,
        current_states=current_states,
        current_identities=current_identities,
        immutable_runtime_directories=tuple(immutable_runtime_directories),
        bootstrap_path=bootstrap_path,
        bootstrap=bootstrap,
        policy_path=policy_path,
        probe_dll=dll,
        bridge_identity=metadata_identity(bridge_metadata),
        probe_identity=metadata_identity(probe_metadata),
        runtime_bindings=runtime,
        managed_assembly_binding=managed_assembly_binding,
        active_config_lock_identity=allowed_config_lock_identity,
        active_parent_finalize_lock=active_parent_finalize_lock,
    )


def decode_utf8_preserving_bom(payload: bytes, label: str) -> tuple[str, bytes]:
    bom = b"\xef\xbb\xbf" if payload.startswith(b"\xef\xbb\xbf") else b""
    body = payload[len(bom) :]
    try:
        return body.decode("utf-8"), bom
    except UnicodeError as exc:
        raise ConfigError(f"{label} is not UTF-8") from exc


def _yaml_semantic_key_occurrences(text: str, key: str) -> list[re.Match[str]]:
    spelling = rf'(?:{re.escape(key)}|\'{re.escape(key)}\'|"{re.escape(key)}")'
    prefix = r"(?:^[ \t]*(?:[-?][ \t]+)?|(?:\{|,)[ \t]*)"
    pattern = re.compile(
        prefix + spelling + r"[ \t]*(?::|$)",
        re.MULTILINE,
    )
    return list(pattern.finditer(text))


def _plain_yaml_scalar_matches(text: str, key: str) -> list[re.Match[str]]:
    pattern = re.compile(
        rf"^(?P<indent>[ \t]*)(?P<key>{re.escape(key)})"
        r"(?P<separator>[ \t]*:[ \t]*)(?P<value>[^\r\n]*)"
        r"(?P<newline>\r\n|\n|\r|$)",
        re.MULTILINE,
    )
    return list(pattern.finditer(text))


def _indent_width(value: str) -> int:
    return len(value.expandtabs(8))


def _strings_mapping_bounds(text: str) -> tuple[re.Match[str], int, int, int, int]:
    semantic = _yaml_semantic_key_occurrences(text, "strings")
    headers = _plain_yaml_scalar_matches(text, "strings")
    if len(semantic) != 1 or len(headers) != 1 or headers[0].group("value"):
        raise ConfigError("kplayerprefs.yaml must contain one exact strings mapping")
    header = headers[0]
    if header.group("indent") or not header.group("newline"):
        raise ConfigError("kplayerprefs.yaml strings mapping must be one top-level block mapping")
    parent_width = _indent_width(header.group("indent"))
    block_start = header.end()
    block_end = len(text)
    direct_child_width: Optional[int] = None
    line_pattern = re.compile(
        r"^(?P<indent>[ \t]*)(?P<body>[^\r\n]*)(?:\r\n|\n|\r|$)",
        re.MULTILINE,
    )
    for line in line_pattern.finditer(text, block_start):
        body = line.group("body").lstrip()
        if not body or body.startswith("#"):
            continue
        width = _indent_width(line.group("indent"))
        if width <= parent_width:
            block_end = line.start()
            break
        if direct_child_width is None or width < direct_child_width:
            direct_child_width = width
    if direct_child_width is None or direct_child_width <= parent_width:
        raise ConfigError("kplayerprefs.yaml strings mapping has no direct scalar entries")
    return header, block_start, block_end, parent_width, direct_child_width


def _require_direct_strings_scalar(
    match: re.Match[str],
    *,
    block_start: int,
    block_end: int,
    parent_width: int,
    direct_child_width: int,
    label: str,
) -> None:
    if (
        match.start() < block_start
        or match.start() >= block_end
        or _indent_width(match.group("indent")) != direct_child_width
    ):
        raise ConfigError(f"{label} must be a direct scalar in the strings mapping")


def _require_exact_loader_scalar(
    match: re.Match[str],
    loader: pathlib.Path,
    label: str,
) -> None:
    value = match.group("value")
    try:
        candidate = require_absolute_local_path(value, label)
    except ConfigError as exc:
        raise ConfigError(f"{label} must be the exact absolute manifest-bound loader") from exc
    if value != str(loader) or not same_path(candidate, loader):
        raise ConfigError(f"{label} must be the exact absolute manifest-bound loader")


def _loader_scalar_policy_from_kplayerprefs(
    payload: bytes,
    loader: pathlib.Path,
) -> dict[str, Any]:
    text, _bom = decode_utf8_preserving_bom(payload, "kplayerprefs.yaml")
    loader = require_absolute_local_path(loader, "loader save")
    _header, block_start, block_end, parent_width, direct_child_width = _strings_mapping_bounds(text)
    scalar_matches: dict[str, re.Match[str]] = {}
    for key in (SAVE_FILENAME_KEY, AUTO_RESUME_KEY):
        semantic = _yaml_semantic_key_occurrences(text, key)
        plain = _plain_yaml_scalar_matches(text, key)
        if len(semantic) != 1 or len(plain) != 1:
            raise ConfigError(f"kplayerprefs.yaml must contain one exact {key} scalar")
        match = plain[0]
        _require_direct_strings_scalar(
            match,
            block_start=block_start,
            block_end=block_end,
            parent_width=parent_width,
            direct_child_width=direct_child_width,
            label=key,
        )
        _require_exact_loader_scalar(match, loader, key)
        scalar_matches[key] = match
    if scalar_matches[SAVE_FILENAME_KEY].group("indent") != scalar_matches[AUTO_RESUME_KEY].group(
        "indent"
    ):
        raise ConfigError("loader keys must use identical strings-mapping indentation")
    return {
        "save_filename_key_count": 1,
        "auto_resume_save_file_key_count": 1,
        "save_filename_key_exact_loader": True,
        "auto_resume_save_file_exact_loader": True,
        "loader_path_id_lower": path_id(loader, contract="lower"),
        "points_only_to_loader_clone": True,
        "source_continue_disabled": True,
    }


def loader_policy_from_kplayerprefs(
    payload: bytes,
    loader: pathlib.Path,
    managed_assembly_binding: Mapping[str, Any],
) -> dict[str, Any]:
    policy = _loader_scalar_policy_from_kplayerprefs(payload, loader)
    policy["built_in_semantics"] = bound_builtin_auto_resume_semantics(
        managed_assembly_binding
    )
    policy["expected_one_shot_consumption"] = True
    return policy


def build_kplayerprefs(payload: bytes, source: pathlib.Path, loader: pathlib.Path) -> bytes:
    text, bom = decode_utf8_preserving_bom(payload, "kplayerprefs.yaml")
    if _yaml_semantic_key_occurrences(text, AUTO_RESUME_KEY):
        raise ConfigError("kplayerprefs.yaml must not already contain AutoResumeSaveFile")
    source = require_absolute_local_path(source, "source save")
    loader = require_absolute_local_path(loader, "loader save")
    if same_path(source, loader):
        raise ConfigError("source and loader saves must be distinct")
    _header, block_start, block_end, parent_width, direct_child_width = _strings_mapping_bounds(text)
    semantic = _yaml_semantic_key_occurrences(text, SAVE_FILENAME_KEY)
    matches = _plain_yaml_scalar_matches(text, SAVE_FILENAME_KEY)
    if len(semantic) != 1 or len(matches) != 1:
        raise ConfigError("kplayerprefs.yaml must contain one exact SaveFilenameKey/ scalar")
    match = matches[0]
    _require_direct_strings_scalar(
        match,
        block_start=block_start,
        block_end=block_end,
        parent_width=parent_width,
        direct_child_width=direct_child_width,
        label=SAVE_FILENAME_KEY,
    )
    current_value = match.group("value")
    try:
        current_source = require_absolute_local_path(current_value, SAVE_FILENAME_KEY)
    except ConfigError as exc:
        raise ConfigError(
            "SaveFilenameKey/ does not point to the manifest-bound source save"
        ) from exc
    if current_value != str(source) or not same_path(current_source, source):
        raise ConfigError("SaveFilenameKey/ does not point to the manifest-bound source save")
    newline = match.group("newline")
    if not newline:
        raise ConfigError("SaveFilenameKey/ must end with a reusable line ending")
    prefix = match.group("indent") + SAVE_FILENAME_KEY + match.group("separator")
    replacement = prefix + str(loader) + newline
    insertion = (
        match.group("indent")
        + AUTO_RESUME_KEY
        + match.group("separator")
        + str(loader)
        + newline
    )
    changed = text[: match.start()] + replacement + insertion + text[match.end() :]
    desired = bom + changed.encode("utf-8")
    if desired == payload:
        raise ConfigError("kplayerprefs.yaml loader configuration did not change")
    _loader_scalar_policy_from_kplayerprefs(desired, loader)
    return desired


def _skip_ws(text: str, position: int) -> int:
    while position < len(text) and text[position] in " \t\r\n":
        position += 1
    return position


def _scan_string(text: str, position: int) -> tuple[str, int]:
    if position >= len(text) or text[position] != '"':
        raise ConfigError("mods.json lexical structure is invalid")
    try:
        return json.decoder.scanstring(text, position + 1, True)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ConfigError("mods.json string token is invalid") from exc


def _skip_value(text: str, position: int) -> int:
    position = _skip_ws(text, position)
    if position >= len(text):
        raise ConfigError("mods.json value is truncated")
    token = text[position]
    if token == '"':
        _value, end = _scan_string(text, position)
        return end
    if token == "{":
        _members, end = _object_members(text, position)
        return end
    if token == "[":
        _elements, end = _array_elements(text, position)
        return end
    for literal in ("true", "false", "null"):
        if text.startswith(literal, position):
            return position + len(literal)
    match = NUMBER_RE.match(text, position)
    if match is None:
        raise ConfigError("mods.json contains an unsupported token")
    return match.end()


def _object_members(text: str, position: int) -> tuple[list[tuple[str, int, int]], int]:
    if position >= len(text) or text[position] != "{":
        raise ConfigError("mods.json object span is invalid")
    members: list[tuple[str, int, int]] = []
    position = _skip_ws(text, position + 1)
    if position < len(text) and text[position] == "}":
        return members, position + 1
    while True:
        key, position = _scan_string(text, position)
        position = _skip_ws(text, position)
        if position >= len(text) or text[position] != ":":
            raise ConfigError("mods.json object member is invalid")
        value_start = _skip_ws(text, position + 1)
        value_end = _skip_value(text, value_start)
        members.append((key, value_start, value_end))
        position = _skip_ws(text, value_end)
        if position >= len(text):
            raise ConfigError("mods.json object is truncated")
        if text[position] == "}":
            return members, position + 1
        if text[position] != ",":
            raise ConfigError("mods.json object delimiter is invalid")
        position = _skip_ws(text, position + 1)


def _array_elements(text: str, position: int) -> tuple[list[tuple[int, int]], int]:
    if position >= len(text) or text[position] != "[":
        raise ConfigError("mods.json array span is invalid")
    elements: list[tuple[int, int]] = []
    position = _skip_ws(text, position + 1)
    if position < len(text) and text[position] == "]":
        return elements, position + 1
    while True:
        start = position
        end = _skip_value(text, start)
        elements.append((start, end))
        position = _skip_ws(text, end)
        if position >= len(text):
            raise ConfigError("mods.json array is truncated")
        if text[position] == "]":
            return elements, position + 1
        if text[position] != ",":
            raise ConfigError("mods.json array delimiter is invalid")
        position = _skip_ws(text, position + 1)


# ONI enables mods per game version: base game ("") and Spaced Out.
MOD_ACTIVE_VERSIONS = ("", "EXPANSION1_ID")
PROBE_ENABLED_FOR_DLC = '["", "EXPANSION1_ID"]'


def build_mods_json(payload: bytes) -> tuple[bytes, int]:
    text, bom = decode_utf8_preserving_bom(payload, "mods/mods.json")
    try:
        document = json.loads(
            text,
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=lambda item: (_ for _ in ()).throw(ConfigError(item)),
        )
    except json.JSONDecodeError as exc:
        raise ConfigError("mods/mods.json is invalid JSON") from exc
    if not isinstance(document, dict) or not isinstance(document.get("mods"), list):
        raise ConfigError("mods/mods.json root/mods structure is invalid")
    mods = document["mods"]
    static_ids: set[str] = set()
    probe_count = 0
    desired_enabled: list[bool] = []
    for item in mods:
        if not isinstance(item, dict) or not isinstance(item.get("staticID"), str):
            raise ConfigError("mods/mods.json contains a mod without a staticID")
        static_id = item["staticID"]
        folded = static_id.casefold()
        if folded in static_ids:
            raise ConfigError("mods/mods.json contains a duplicate staticID")
        static_ids.add(folded)
        if not isinstance(item.get("enabled"), bool):
            raise ConfigError("mods/mods.json contains a non-boolean enabled field")
        dlc_ids = item.get("enabledForDlc")
        if not isinstance(dlc_ids, list) or not all(isinstance(value, str) for value in dlc_ids):
            # ONI decides loading with enabledForDlc; a missing list would be
            # migrated from "enabled" to the base game only.
            raise ConfigError("mods/mods.json mod entry lacks a string enabledForDlc list")
        enabled = static_id == PROBE_STATIC_ID
        desired_enabled.append(enabled)
        probe_count += int(enabled)
    if probe_count != 1:
        raise ConfigError("mods/mods.json must contain exactly one ass.OniAiProbe entry")

    start = _skip_ws(text, 0)
    root_members, end = _object_members(text, start)
    if _skip_ws(text, end) != len(text):
        raise ConfigError("mods/mods.json has trailing non-whitespace data")
    mods_members = [(begin, finish) for key, begin, finish in root_members if key == "mods"]
    if len(mods_members) != 1:
        raise ConfigError("mods/mods.json must have one top-level mods member")
    element_spans, array_end = _array_elements(text, mods_members[0][0])
    if array_end != mods_members[0][1] or len(element_spans) != len(mods):
        raise ConfigError("mods/mods.json textual and parsed mod counts disagree")
    replacements: list[tuple[int, int, str]] = []
    for index, (begin, finish) in enumerate(element_spans):
        members, object_end = _object_members(text, begin)
        if object_end != finish:
            raise ConfigError("mods/mods.json mod entry is not an object")
        enabled_members = [(left, right) for key, left, right in members if key == "enabled"]
        if len(enabled_members) != 1:
            raise ConfigError("each mod entry must have one top-level enabled field")
        left, right = enabled_members[0]
        if text[left:right] not in {"true", "false"}:
            raise ConfigError("mod enabled token is not a JSON boolean")
        replacements.append((left, right, "true" if desired_enabled[index] else "false"))
        # The game loads a mod only when enabledForDlc contains the active
        # version (KMod.Mod.IsEnabledForActiveDlc); the legacy "enabled" flag
        # alone left every other mod active under Spaced Out (code review
        # 2026-09-26, S2).  Enable only the Probe, for both versions.
        dlc_members = [(left, right) for key, left, right in members if key == "enabledForDlc"]
        if len(dlc_members) != 1:
            raise ConfigError("each mod entry must have one top-level enabledForDlc field")
        left, right = dlc_members[0]
        replacements.append((left, right, PROBE_ENABLED_FOR_DLC if desired_enabled[index] else "[]"))
    changed = text
    for left, right, replacement in sorted(replacements, reverse=True):
        changed = changed[:left] + replacement + changed[right:]
    effective = json.loads(changed)["mods"]
    for version in MOD_ACTIVE_VERSIONS:
        loaded = [item["staticID"] for item in effective if version in item["enabledForDlc"]]
        if loaded != [PROBE_STATIC_ID]:
            raise ConfigError("configured mods.json does not load exactly the Probe")
    desired = bom + changed.encode("utf-8")
    return desired, len(mods)


def build_desired_configs(ctx: RuntimeContext, *, from_backup: bool) -> tuple[dict[str, bytes], int]:
    source = ctx.backup_bytes if from_backup else ctx.current_bytes
    prefs = build_kplayerprefs(source["kplayerprefs.yaml"], ctx.source_save, ctx.loader_save)
    mods, mod_count = build_mods_json(source["mods/mods.json"])
    return {"kplayerprefs.yaml": prefs, "mods/mods.json": mods}, mod_count


def invoke_test_hook(event: str, ctx: RuntimeContext) -> None:
    if _TEST_HOOK is None:
        return
    if ctx.roots.test_root is None:
        raise ConfigError("test hooks are forbidden in live mode")
    _TEST_HOOK(event, ctx)


def consume_test_fault(name: str, ctx: RuntimeContext) -> None:
    if name not in _TEST_FAULTS:
        return
    if ctx.roots.test_root is None:
        raise ConfigError("test faults are forbidden in live mode")
    _TEST_FAULTS.remove(name)
    if name.endswith(("post_link_raw_oserror", "temp_unlink_failure")):
        raise OSError(f"injected TestRoot publication fault: {name}")
    raise ConfigError(f"injected TestRoot publication fault: {name}")


def invoke_publication_hook(event: str, ctx: RuntimeContext, stage: str) -> None:
    if stage == "evidence":
        invoke_test_hook(event, ctx)
    invoke_test_hook(f"{stage}_{event}", ctx)


def consume_publication_fault(name: str, ctx: RuntimeContext, stage: str) -> None:
    consume_test_fault(f"{stage}_{name}", ctx)
    if stage == "evidence":
        consume_test_fault(name, ctx)


def verify_current_snapshots(ctx: RuntimeContext) -> None:
    for relative in TARGET_CONFIG_RELATIVES:
        path = ctx.roots.oni_root / pathlib.Path(*pathlib.PurePosixPath(relative).parts)
        assert_no_reparse(path, ctx.roots.oni_root)
        payload, state = read_snapshot(path)
        metadata = os.lstat(path)
        if (
            metadata.st_nlink != 1
            or metadata_identity(metadata) != ctx.current_identities[relative]
            or payload != ctx.current_bytes[relative]
            or state != ctx.current_states[relative]
        ):
            raise ConfigError("runtime config changed concurrently before commit")


def write_descriptor_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    written = 0
    while written < len(view):
        count = os.write(descriptor, view[written:])
        if count <= 0:
            raise ConfigError("descriptor write made no progress")
        written += count


def open_exclusive_shared_delete(path: pathlib.Path) -> int:
    flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
    if os.name != "nt":
        return os.open(path, os.O_CREAT | os.O_EXCL | flags, 0o600)
    import msvcrt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p
    handle = create_file(
        str(path),
        0x80000000 | 0x40000000,  # GENERIC_READ | GENERIC_WRITE
        0x1 | 0x4,  # share read/delete; deny external writers while descriptor is held
        None,
        1,  # CREATE_NEW
        0x80,  # FILE_ATTRIBUTE_NORMAL
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle == invalid:
        error = ctypes.get_last_error()
        if error in {80, 183}:  # ERROR_FILE_EXISTS / ERROR_ALREADY_EXISTS
            raise FileExistsError(error, "create-only path already exists", str(path))
        raise OSError(error, "CreateFileW create-only open failed", str(path))
    try:
        return msvcrt.open_osfhandle(int(handle), flags)
    except Exception:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        raise


def named_identity(path: pathlib.Path) -> tuple[int, int]:
    metadata = os.lstat(path)
    if is_reparse(path) or not stat.S_ISREG(metadata.st_mode):
        raise ConfigError("named object is not a regular non-reparse file")
    return metadata_identity(metadata)


def open_existing_delete_guard(path: pathlib.Path) -> int:
    if os.name != "nt":
        raise ConfigError("descriptor-bound deletion requires Windows")
    import msvcrt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p
    handle = create_file(
        str(path),
        0x80000000 | 0x00010000,  # GENERIC_READ | DELETE
        0x1 | 0x2,  # share read/write, intentionally deny delete/rename while guarded
        None,
        3,  # OPEN_EXISTING
        0x00200000,  # FILE_FLAG_OPEN_REPARSE_POINT
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle == invalid:
        error = ctypes.get_last_error()
        if error in {2, 3}:
            raise FileNotFoundError(error, "owned path disappeared", str(path))
        raise OSError(error, "descriptor-bound delete guard open failed", str(path))
    try:
        return msvcrt.open_osfhandle(
            int(handle),
            os.O_RDONLY | getattr(os, "O_BINARY", 0),
        )
    except Exception:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        raise


def mark_descriptor_deleted(descriptor: int) -> None:
    import msvcrt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    set_information = kernel32.SetFileInformationByHandle
    set_information.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    set_information.restype = ctypes.c_int
    handle = ctypes.c_void_p(msvcrt.get_osfhandle(descriptor))

    flags = ctypes.c_uint32(0x1 | 0x2 | 0x10)  # DELETE | POSIX_SEMANTICS | IGNORE_READONLY
    if set_information(handle, 21, ctypes.byref(flags), ctypes.sizeof(flags)):
        return
    first_error = ctypes.get_last_error()
    disposition = ctypes.c_int(1)
    if set_information(handle, 4, ctypes.byref(disposition), ctypes.sizeof(disposition)):
        return
    error = ctypes.get_last_error() or first_error
    raise OSError(error, "descriptor-bound deletion failed")


def set_descriptor_mtime(descriptor: int, mtime_ns: int) -> None:
    if os.name != "nt" or mtime_ns < 0:
        raise ConfigError("descriptor-bound mtime restore requires Windows and a valid timestamp")
    import msvcrt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    set_file_time = kernel32.SetFileTime
    set_file_time.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    set_file_time.restype = ctypes.c_int
    windows_ticks = (mtime_ns // 100) + 116_444_736_000_000_000

    class FileTime(ctypes.Structure):
        _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

    value = FileTime(windows_ticks & 0xFFFFFFFF, (windows_ticks >> 32) & 0xFFFFFFFF)
    handle = ctypes.c_void_p(msvcrt.get_osfhandle(descriptor))
    if not set_file_time(handle, None, None, ctypes.byref(value)):
        raise OSError(ctypes.get_last_error(), "descriptor-bound mtime restore failed")


def safe_unlink_owned(
    path: pathlib.Path,
    identity: tuple[int, int],
    *,
    expected_links: int = 1,
    expected_sha256: Optional[str] = None,
    expected_length: Optional[int] = None,
    after_check: Optional[Callable[[], None]] = None,
) -> bool:
    global _TEST_ATOMIC_UNLINK_PATH
    descriptor: Optional[int] = None
    try:
        descriptor = open_existing_delete_guard(path)
        before = os.fstat(descriptor)
        named = os.lstat(path)
        payload = read_descriptor_all(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or metadata_identity(before) != identity
            or before.st_nlink != expected_links
            or is_reparse(path)
            or not stat.S_ISREG(named.st_mode)
            or metadata_identity(named) != identity
            or named.st_nlink != expected_links
            or (expected_sha256 is not None and sha256_bytes(payload) != expected_sha256)
            or (expected_length is not None and len(payload) != expected_length)
        ):
            return False
        if after_check is not None:
            _TEST_ATOMIC_UNLINK_PATH = path
            try:
                after_check()
            finally:
                _TEST_ATOMIC_UNLINK_PATH = None
        after = os.fstat(descriptor)
        named_after = os.lstat(path)
        if (
            metadata_identity(after) != identity
            or after.st_nlink != expected_links
            or read_descriptor_all(descriptor) != payload
            or is_reparse(path)
            or metadata_identity(named_after) != identity
            or named_after.st_nlink != expected_links
        ):
            return False
        mark_descriptor_deleted(descriptor)
    except (OSError, ConfigError):
        return False
    finally:
        _TEST_ATOMIC_UNLINK_PATH = None
        if descriptor is not None:
            os.close(descriptor)
    try:
        return not os.path.lexists(path) or named_identity(path) != identity
    except OSError:
        return True


def write_staged(
    path: pathlib.Path,
    payload: bytes,
    boundary: pathlib.Path,
    *,
    mtime_ns: Optional[int] = None,
    ctx: Optional[RuntimeContext] = None,
    hook_event: Optional[str] = None,
) -> StagedRecord:
    global _TEST_STAGED_PATH
    assert_no_reparse(path.parent, boundary)
    temporary = path.parent / f".g0-{uuid.uuid4().hex[:16]}.tmp"
    assert_no_reparse(temporary, boundary, allow_missing_leaf=True)
    descriptor = open_exclusive_shared_delete(temporary)
    identity = metadata_identity(os.fstat(descriptor))
    success = False
    try:
        write_descriptor_all(descriptor, payload)
        os.fsync(descriptor)
        if mtime_ns is not None:
            set_descriptor_mtime(descriptor, mtime_ns)
        metadata = os.fstat(descriptor)
        if (
            metadata_identity(metadata) != identity
            or metadata.st_nlink != 1
            or metadata.st_size != len(payload)
            or read_descriptor_all(descriptor) != payload
            or named_identity(temporary) != identity
        ):
            raise ConfigError("staged runtime config descriptor/hash/link identity is invalid")
        os.close(descriptor)
        descriptor = -1
        _TEST_STAGED_PATH = temporary
        if ctx is not None and hook_event is not None:
            invoke_test_hook(hook_event, ctx)
        if not named_record_owned(
            temporary,
            identity=identity,
            sha256=sha256_bytes(payload),
            length=len(payload),
        ):
            raise ConfigError("staged runtime config name changed after descriptor close")
        success = True
        return StagedRecord(
            path=temporary,
            identity=identity,
            sha256=sha256_bytes(payload),
            length=len(payload),
        )
    finally:
        _TEST_STAGED_PATH = None
        if descriptor >= 0:
            os.close(descriptor)
        if not success and os.path.lexists(temporary):
            safe_unlink_owned(temporary, identity)


def named_record_owned(
    path: pathlib.Path,
    *,
    identity: tuple[int, int],
    sha256: str,
    length: int,
    links: int = 1,
) -> bool:
    try:
        metadata = os.lstat(path)
        if (
            is_reparse(path)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata_identity(metadata) != identity
            or metadata.st_nlink != links
        ):
            return False
        payload, state_value = read_snapshot(path, expected_links=links)
        after = os.lstat(path)
        return (
            metadata_identity(after) == identity
            and after.st_nlink == links
            and state_value["sha256"] == sha256
            and state_value["length"] == length
            and sha256_bytes(payload) == sha256
        )
    except (OSError, ConfigError):
        return False


def rename_no_replace(source: pathlib.Path, destination: pathlib.Path) -> None:
    if os.name != "nt":
        raise ConfigError("atomic no-replace config claims require Windows")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    move_file = kernel32.MoveFileExW
    move_file.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    move_file.restype = ctypes.c_int
    if not move_file(str(source), str(destination), 0x8):  # MOVEFILE_WRITE_THROUGH
        error = ctypes.get_last_error()
        if error in {80, 183}:  # ERROR_FILE_EXISTS / ERROR_ALREADY_EXISTS
            raise FileExistsError(error, "no-replace claim path already exists", str(destination))
        if error in {2, 3}:  # ERROR_FILE_NOT_FOUND / ERROR_PATH_NOT_FOUND
            raise FileNotFoundError(error, "config target disappeared before claim", str(source))
        raise OSError(error, "atomic no-replace claim failed", str(source))


def republish_claim_create_only(claim: StagedRecord, target: pathlib.Path) -> bool:
    try:
        if not named_record_owned(
            claim.path,
            identity=claim.identity,
            sha256=claim.sha256,
            length=claim.length,
        ):
            return False
        os.link(claim.path, target)
        if not (
            named_record_owned(
                claim.path,
                identity=claim.identity,
                sha256=claim.sha256,
                length=claim.length,
                links=2,
            )
            and named_record_owned(
                target,
                identity=claim.identity,
                sha256=claim.sha256,
                length=claim.length,
                links=2,
            )
        ):
            return False
        if not safe_unlink_owned(
            claim.path,
            claim.identity,
            expected_links=2,
            expected_sha256=claim.sha256,
            expected_length=claim.length,
        ):
            return False
        return named_record_owned(
            target,
            identity=claim.identity,
            sha256=claim.sha256,
            length=claim.length,
        )
    except (OSError, ConfigError):
        return False


def claim_owned_target(
    ctx: RuntimeContext,
    relative: str,
    *,
    expected_identity: tuple[int, int],
    expected_sha256: str,
    expected_length: int,
    expected_mtime_ns: Optional[int],
    before_event: str,
    after_event: str,
) -> StagedRecord:
    target = ctx.roots.oni_root / pathlib.Path(*pathlib.PurePosixPath(relative).parts)
    invoke_test_hook(before_event, ctx)
    claim = target.parent / f".g0-claim-{uuid.uuid4().hex[:16]}.tmp"
    assert_no_reparse(claim, ctx.roots.oni_root, allow_missing_leaf=True)
    try:
        rename_no_replace(target, claim)
    except (OSError, ConfigError) as exc:
        raise MutationOwnershipError(
            "config target could not be atomically reclaimed without overwrite",
            relative=relative,
        ) from exc
    try:
        payload, state_value = read_snapshot(claim)
        metadata = os.lstat(claim)
        record = StagedRecord(
            path=claim,
            identity=metadata_identity(metadata),
            sha256=state_value["sha256"],
            length=state_value["length"],
        )
        exact = (
            metadata.st_nlink == 1
            and record.identity == expected_identity
            and record.sha256 == expected_sha256
            and record.length == expected_length
            and sha256_bytes(payload) == expected_sha256
            and (
                expected_mtime_ns is None
                or state_value["last_write_ns"] == expected_mtime_ns
            )
        )
        if not exact:
            republish_claim_create_only(record, target)
            claims = () if not os.path.lexists(claim) else (claim,)
            raise MutationOwnershipError(
                "atomic config claim captured externally changed state; it was preserved",
                relative=relative,
                recovery_claims=claims,
            )
        invoke_test_hook(after_event, ctx)
        return record
    except MutationOwnershipError:
        raise
    except Exception as exc:
        claims = (claim,) if os.path.lexists(claim) else ()
        raise MutationOwnershipError(
            "atomic config claim outcome is unknown",
            relative=relative,
            recovery_claims=claims,
        ) from exc


def staged_and_target_owned(staged: StagedRecord, target: pathlib.Path, links: int) -> bool:
    return named_record_owned(
        staged.path,
        identity=staged.identity,
        sha256=staged.sha256,
        length=staged.length,
        links=links,
    ) and named_record_owned(
        target,
        identity=staged.identity,
        sha256=staged.sha256,
        length=staged.length,
        links=links,
    )


def install_staged_create_only(
    ctx: RuntimeContext,
    staged: StagedRecord,
    target: pathlib.Path,
) -> CommitRecord:
    published = False
    try:
        if not named_record_owned(
            staged.path,
            identity=staged.identity,
            sha256=staged.sha256,
            length=staged.length,
        ):
            raise ConfigError("staged config identity changed before create-only install")
        os.link(staged.path, target)
        published = True
        if not staged_and_target_owned(staged, target, 2):
            raise ConfigError("create-only config install identity/link count diverged")
        if not safe_unlink_owned(
            staged.path,
            staged.identity,
            expected_links=2,
            expected_sha256=staged.sha256,
            expected_length=staged.length,
            after_check=lambda: invoke_test_hook("after_staged_cleanup_check", ctx),
        ):
            raise ConfigError("create-only config install lost its staging name")
        if not named_record_owned(
            target,
            identity=staged.identity,
            sha256=staged.sha256,
            length=staged.length,
        ):
            raise ConfigError("create-only installed config changed after publication")
        return CommitRecord(
            relative="",
            identity=staged.identity,
            sha256=staged.sha256,
            length=staged.length,
        )
    except FileExistsError as exc:
        raise PublicationError(
            "create-only config install found an external target",
            outcome="not_published",
        ) from exc
    except PublicationError:
        raise
    except Exception as exc:
        outcome = "not_published"
        if published:
            revoked = False
            if staged_and_target_owned(staged, target, 2):
                revoked = safe_unlink_owned(
                    target,
                    staged.identity,
                    expected_links=2,
                    expected_sha256=staged.sha256,
                    expected_length=staged.length,
                ) and named_record_owned(
                    staged.path,
                    identity=staged.identity,
                    sha256=staged.sha256,
                    length=staged.length,
                )
            elif not os.path.lexists(staged.path) and named_record_owned(
                target,
                identity=staged.identity,
                sha256=staged.sha256,
                length=staged.length,
            ):
                revoked = safe_unlink_owned(
                    target,
                    staged.identity,
                    expected_sha256=staged.sha256,
                    expected_length=staged.length,
                )
            outcome = "revoked" if revoked else "outcome_unknown"
        raise PublicationError(
            "create-only config install failed after ownership analysis",
            outcome=outcome,
        ) from exc


def replace_owned_target_create_only(
    ctx: RuntimeContext,
    relative: str,
    staged: StagedRecord,
    *,
    expected_identity: tuple[int, int],
    expected_sha256: str,
    expected_length: int,
    expected_mtime_ns: Optional[int],
    before_event: str,
    after_event: str,
) -> CommitRecord:
    target = ctx.roots.oni_root / pathlib.Path(*pathlib.PurePosixPath(relative).parts)
    claim = claim_owned_target(
        ctx,
        relative,
        expected_identity=expected_identity,
        expected_sha256=expected_sha256,
        expected_length=expected_length,
        expected_mtime_ns=expected_mtime_ns,
        before_event=before_event,
        after_event=after_event,
    )
    try:
        installed = install_staged_create_only(ctx, staged, target)
    except PublicationError as exc:
        if exc.outcome in {"not_published", "revoked"} and not os.path.lexists(target):
            if republish_claim_create_only(claim, target):
                raise ConfigError("create-only config install failed and the owned target was restored") from exc
        claims = () if not os.path.lexists(claim.path) else (claim.path,)
        raise MutationOwnershipError(
            "create-only config install preserved an external target/recovery claim",
            relative=relative,
            recovery_claims=claims,
        ) from exc
    if not safe_unlink_owned(
        claim.path,
        claim.identity,
        expected_sha256=claim.sha256,
        expected_length=claim.length,
        after_check=lambda: invoke_test_hook("after_claim_cleanup_check", ctx),
    ):
        raise MutationOwnershipError(
            "configured target is owned but the original claim could not be safely retired",
            relative=relative,
            recovery_claims=(claim.path,) if os.path.lexists(claim.path) else (),
        )
    return CommitRecord(
        relative=relative,
        identity=installed.identity,
        sha256=installed.sha256,
        length=installed.length,
    )


def commit_is_owned(ctx: RuntimeContext, record: CommitRecord) -> bool:
    path = ctx.roots.oni_root / pathlib.Path(*pathlib.PurePosixPath(record.relative).parts)
    try:
        before = os.lstat(path)
        if (
            is_reparse(path)
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or metadata_identity(before) != record.identity
        ):
            return False
        payload, state_value = read_snapshot(path)
        after = os.lstat(path)
        return (
            metadata_identity(after) == record.identity
            and after.st_nlink == 1
            and state_value["sha256"] == record.sha256
            and state_value["length"] == record.length
            and sha256_bytes(payload) == record.sha256
        )
    except (OSError, ConfigError):
        return False


def restore_owned_configs(ctx: RuntimeContext, committed: Iterable[CommitRecord]) -> RollbackOutcome:
    unrestored: list[str] = []
    recovery_claims: list[pathlib.Path] = []
    for record in reversed(list(committed)):
        relative = record.relative
        if not commit_is_owned(ctx, record):
            unrestored.append(relative)
            continue
        path = ctx.roots.oni_root / pathlib.Path(*pathlib.PurePosixPath(relative).parts)
        staged: Optional[StagedRecord] = None
        try:
            staged = write_staged(
                path,
                ctx.current_bytes[relative],
                ctx.roots.oni_root,
                mtime_ns=int(ctx.current_states[relative]["last_write_ns"]),
                ctx=ctx,
                hook_event="after_rollback_stage_close",
            )
            if not commit_is_owned(ctx, record):
                raise MutationOwnershipError(
                    "runtime config ownership changed before rollback claim",
                    relative=relative,
                )
            replace_owned_target_create_only(
                ctx,
                relative,
                staged,
                expected_identity=record.identity,
                expected_sha256=record.sha256,
                expected_length=record.length,
                expected_mtime_ns=None,
                before_event="before_rollback_claim",
                after_event="after_rollback_claim",
            )
            payload, state = read_snapshot(path)
            if payload != ctx.current_bytes[relative] or state != ctx.current_states[relative]:
                raise ConfigError("rollback verification mismatch")
        except MutationOwnershipError as exc:
            unrestored.append(relative)
            recovery_claims.extend(exc.recovery_claims)
        except Exception:
            unrestored.append(relative)
        finally:
            if staged is not None and os.path.lexists(staged.path):
                safe_unlink_owned(staged.path, staged.identity)
    return RollbackOutcome(
        unrestored=tuple(sorted(set(unrestored))),
        recovery_claims=tuple(recovery_claims),
    )


def verify_self_hashed_document(payload: bytes) -> None:
    document = parse_json_payload(payload, "published JSON evidence")
    claimed = require_hash(document.get("document_hash"), "published JSON document hash")
    unsigned = dict(document)
    del unsigned["document_hash"]
    if claimed != sha256_bytes(canonical_bytes(unsigned)):
        raise ConfigError("published JSON document self-hash is invalid")


def descriptor_publication_owned(
    descriptor: int,
    temporary: pathlib.Path,
    target: pathlib.Path,
    payload: bytes,
    *,
    expected_links: int,
) -> bool:
    try:
        metadata = os.fstat(descriptor)
        identity = metadata_identity(metadata)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != expected_links
            or metadata.st_size != len(payload)
            or read_descriptor_all(descriptor) != payload
        ):
            return False
        for path in (temporary, target):
            named = os.lstat(path)
            if (
                is_reparse(path)
                or not stat.S_ISREG(named.st_mode)
                or metadata_identity(named) != identity
                or named.st_nlink != expected_links
            ):
                return False
        target_payload, target_state = read_snapshot(target, expected_links=expected_links)
        return target_payload == payload and target_state["sha256"] == sha256_bytes(payload)
    except (OSError, ConfigError):
        return False


def descriptor_named_owned(
    descriptor: int,
    path: pathlib.Path,
    payload: bytes,
    *,
    expected_links: int,
) -> bool:
    try:
        metadata = os.fstat(descriptor)
        identity = metadata_identity(metadata)
        named = os.lstat(path)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != expected_links
            or metadata.st_size != len(payload)
            or read_descriptor_all(descriptor) != payload
            or is_reparse(path)
            or not stat.S_ISREG(named.st_mode)
            or metadata_identity(named) != identity
            or named.st_nlink != expected_links
        ):
            return False
        named_payload, named_state = read_snapshot(path, expected_links=expected_links)
        return named_payload == payload and named_state["sha256"] == sha256_bytes(payload)
    except (OSError, ConfigError):
        return False


def revoke_owned_publication(
    ctx: RuntimeContext,
    descriptor: int,
    temporary: pathlib.Path,
    target: pathlib.Path,
    payload: bytes,
    identity: tuple[int, int],
    publication_stage: str,
) -> bool:
    if descriptor_publication_owned(descriptor, temporary, target, payload, expected_links=2):
        if not safe_unlink_owned(
            target,
            identity,
            expected_links=2,
            expected_sha256=sha256_bytes(payload),
            expected_length=len(payload),
            after_check=lambda: invoke_publication_hook(
                "after_publication_revoke_check", ctx, publication_stage
            ),
        ):
            return False
        return descriptor_named_owned(
            descriptor,
            temporary,
            payload,
            expected_links=1,
        )
    if not os.path.lexists(temporary) and descriptor_named_owned(
        descriptor,
        target,
        payload,
        expected_links=1,
    ):
        if not safe_unlink_owned(
            target,
            identity,
            expected_sha256=sha256_bytes(payload),
            expected_length=len(payload),
            after_check=lambda: invoke_publication_hook(
                "after_publication_revoke_check", ctx, publication_stage
            ),
        ):
            return False
        metadata = os.fstat(descriptor)
        return (
            metadata_identity(metadata) == identity
            and metadata.st_nlink == 0
            and metadata.st_size == len(payload)
            and read_descriptor_all(descriptor) == payload
        )
    return False


def create_only_json(
    ctx: RuntimeContext,
    path: pathlib.Path,
    value: Mapping[str, Any],
    boundary: pathlib.Path,
    *,
    allow_test_faults: bool = True,
    publication_stage: str = "recovery",
    pre_publish_check: Optional[Callable[[], None]] = None,
    post_publish_check: Optional[Callable[[], None]] = None,
) -> PublicationRecord:
    global _TEST_PUBLICATION_PATHS
    payload = json_document_bytes(value)
    verify_self_hashed_document(payload)
    assert_no_reparse(path.parent, boundary)
    assert_no_reparse(path, boundary, allow_missing_leaf=True)
    temporary = path.parent / f".pub-{uuid.uuid4().hex[:16]}.tmp"
    assert_no_reparse(temporary, boundary, allow_missing_leaf=True)
    descriptor = open_exclusive_shared_delete(temporary)
    published = False
    descriptor_identity: Optional[tuple[int, int]] = metadata_identity(os.fstat(descriptor))
    try:
        initial = os.fstat(descriptor)
        if (
            not stat.S_ISREG(initial.st_mode)
            or initial.st_nlink != 1
            or initial.st_size != 0
            or named_identity(temporary) != descriptor_identity
        ):
            raise ConfigError("publication staging descriptor was not exclusively created")
        write_descriptor_all(descriptor, payload)
        os.fsync(descriptor)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size != len(payload)
            or read_descriptor_all(descriptor) != payload
            or metadata_identity(metadata) != descriptor_identity
            or named_identity(temporary) != descriptor_identity
        ):
            raise ConfigError("publication staging descriptor identity is invalid")
        if pre_publish_check is not None:
            pre_publish_check()
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise PublicationError(
                f"immutable {publication_stage} publication already exists",
                outcome="not_published",
            ) from exc
        published = True
        _TEST_PUBLICATION_PATHS = (temporary, path)
        if allow_test_faults:
            invoke_publication_hook("after_publication_link", ctx, publication_stage)
            consume_publication_fault("post_link_raw_oserror", ctx, publication_stage)
        if not descriptor_publication_owned(descriptor, temporary, path, payload, expected_links=2):
            raise ConfigError("publication descriptor/path identity, nlink, or bytes diverged")
        if allow_test_faults:
            consume_publication_fault(
                "post_publication_hash_failure", ctx, publication_stage
            )
            invoke_publication_hook(
                "before_publication_temp_unlink", ctx, publication_stage
            )
            consume_publication_fault("temp_unlink_failure", ctx, publication_stage)
        if descriptor_identity is None or not safe_unlink_owned(
            temporary,
            descriptor_identity,
            expected_links=2,
            expected_sha256=sha256_bytes(payload),
            expected_length=len(payload),
            after_check=lambda: invoke_publication_hook(
                "after_publication_temp_unlink_check", ctx, publication_stage
            ),
        ):
            raise ConfigError("publication temporary name lost descriptor ownership")
        metadata = os.fstat(descriptor)
        if metadata.st_nlink != 1 or metadata_identity(metadata) != descriptor_identity:
            raise ConfigError("publication link count/identity changed after temp unlink")
        target_payload, target_state = read_snapshot(path)
        target_metadata = os.lstat(path)
        if (
            metadata_identity(target_metadata) != descriptor_identity
            or target_metadata.st_nlink != 1
            or target_payload != payload
            or target_state["sha256"] != sha256_bytes(payload)
            or read_descriptor_all(descriptor) != payload
        ):
            raise ConfigError("post-publication target identity/bytes/self-hash verification failed")
        verify_self_hashed_document(target_payload)
        if post_publish_check is not None:
            post_publish_check()
        if (
            descriptor_identity is None
            or metadata_identity(os.fstat(descriptor)) != descriptor_identity
            or not descriptor_named_owned(
                descriptor,
                path,
                payload,
                expected_links=1,
            )
        ):
            raise ConfigError(
                "post-callback publication descriptor/name ownership changed"
            )
        return PublicationRecord(
            path=path,
            identity=descriptor_identity,
            sha256=target_state["sha256"],
            length=target_state["length"],
        )
    except PublicationError:
        raise
    except Exception as exc:
        outcome = "not_published"
        if published:
            assert descriptor_identity is not None
            outcome = (
                "revoked"
                if revoke_owned_publication(
                    ctx,
                    descriptor,
                    temporary,
                    path,
                    payload,
                    descriptor_identity,
                    publication_stage,
                )
                else "outcome_unknown"
            )
        raise PublicationError(
            "create-only publication failed closed after explicit ownership analysis",
            outcome=outcome,
        ) from exc
    finally:
        _TEST_PUBLICATION_PATHS = None
        if descriptor_identity is not None and os.path.lexists(temporary):
            try:
                remaining_links = int(os.fstat(descriptor).st_nlink)
            except OSError:
                remaining_links = 1
            safe_unlink_owned(
                temporary,
                descriptor_identity,
                expected_links=remaining_links,
                expected_sha256=sha256_bytes(payload),
                expected_length=len(payload),
            )
        os.close(descriptor)


def publication_record_is_owned(
    record: PublicationRecord,
    *,
    verify_document: bool = True,
) -> bool:
    try:
        payload, state = read_snapshot(record.path)
        metadata = os.lstat(record.path)
        if verify_document:
            verify_self_hashed_document(payload)
        return (
            not is_reparse(record.path)
            and stat.S_ISREG(metadata.st_mode)
            and metadata.st_nlink == 1
            and metadata_identity(metadata) == record.identity
            and metadata.st_size == state["length"]
            and metadata.st_mtime_ns == state["last_write_ns"]
            and state["sha256"] == record.sha256
            and state["length"] == record.length
            and sha256_bytes(payload) == record.sha256
        )
    except (OSError, ConfigError):
        return False


def revoke_publication_record(
    ctx: RuntimeContext,
    record: PublicationRecord,
    *,
    stage: str,
) -> bool:
    if not publication_record_is_owned(record):
        return False
    return safe_unlink_owned(
        record.path,
        record.identity,
        expected_sha256=record.sha256,
        expected_length=record.length,
        after_check=lambda: invoke_publication_hook(
            "after_pair_revoke_check", ctx, stage
        ),
    )


def require_publication_targets_absent(ctx: RuntimeContext) -> None:
    for path, label, boundary in (
        (ctx.evidence_path, "runtime config evidence", ctx.roots.evidence_root),
        (ctx.startup_binding_path, "runtime startup binding", ctx.roots.oni_root),
    ):
        assert_no_reparse(path.parent, boundary)
        assert_no_reparse(
            path,
            boundary,
            allow_missing_leaf=True,
        )
        if os.path.lexists(path):
            raise ConfigError(f"immutable {label} already exists")


@contextmanager
def exclusive_environment_lock(
    ctx: RuntimeContext,
    *,
    final_verifier: Optional[Callable[[], None]] = None,
) -> Iterable[tuple[int, int]]:
    lock = ctx.roots.environment_dir / LOCK_NAME
    assert_no_reparse(lock, ctx.roots.evidence_root, allow_missing_leaf=True)
    token = uuid.uuid4().hex
    payload = canonical_bytes(
        {
            "schema": "oni.live-validation-runtime-config-lock.v1",
            "run_id": ctx.roots.run_id,
            "owner_token": token,
        }
    ) + b"\n"
    try:
        descriptor = open_exclusive_shared_delete(lock)
    except FileExistsError as exc:
        raise ConfigError("environment configure/finalize operation is already locked") from exc
    identity: Optional[tuple[int, int]] = None
    body_completed = False
    try:
        write_descriptor_all(descriptor, payload)
        os.fsync(descriptor)
        metadata = os.fstat(descriptor)
        identity = metadata_identity(metadata)
        if (
            metadata.st_nlink != 1
            or metadata.st_size != len(payload)
            or read_descriptor_all(descriptor) != payload
            or named_identity(lock) != identity
        ):
            raise LockOwnershipError("environment lock identity/token acquisition is ambiguous")
        invoke_test_hook("after_lock_acquire", ctx)
        named = os.lstat(lock)
        if (
            is_reparse(lock)
            or metadata_identity(named) != identity
            or named.st_nlink != 1
            or read_snapshot(lock)[0] != payload
        ):
            raise LockOwnershipError("environment lock was replaced during acquisition")
        ctx.active_config_lock_identity = identity
        yield identity
        body_completed = True
    finally:
        release_error: Optional[Exception] = None

        def remember_release_error(exc: Exception, *, override: bool = False) -> None:
            nonlocal release_error
            if release_error is None or override:
                release_error = exc

        def verify_after_release_event(event: str) -> None:
            invoke_test_hook(event, ctx)
            if body_completed and final_verifier is not None:
                final_verifier()

        try:
            try:
                verify_after_release_event("before_lock_release")
            except Exception as exc:
                remember_release_error(exc)
            metadata = os.fstat(descriptor)
            owned = False
            if identity is not None and metadata_identity(metadata) == identity and metadata.st_nlink == 1:
                try:
                    named = os.lstat(lock)
                    owned = (
                        not is_reparse(lock)
                        and metadata_identity(named) == identity
                        and named.st_nlink == 1
                        and read_descriptor_all(descriptor) == payload
                        and read_snapshot(lock)[0] == payload
                    )
                except (OSError, ConfigError):
                    owned = False
            if owned:
                released = safe_unlink_owned(
                    lock,
                    identity,
                    expected_sha256=sha256_bytes(payload),
                    expected_length=len(payload),
                    after_check=lambda: invoke_test_hook("after_lock_release_check", ctx),
                )
                if not released or os.fstat(descriptor).st_nlink != 0:
                    remember_release_error(
                        LockOwnershipError(
                            "environment lock unlink did not release its sole name"
                        ),
                        override=True,
                    )
                else:
                    try:
                        verify_after_release_event("after_lock_name_release")
                    except Exception as exc:
                        remember_release_error(exc)
            else:
                remember_release_error(
                    LockOwnershipError(
                        "environment lock ownership was lost; replacement was preserved"
                    ),
                    override=True,
                )
        finally:
            try:
                try:
                    verify_after_release_event("before_lock_descriptor_release")
                except Exception as exc:
                    remember_release_error(exc)
            finally:
                try:
                    os.close(descriptor)
                except OSError as exc:
                    remember_release_error(exc, override=True)
            try:
                verify_after_release_event("after_lock_descriptor_release")
            except Exception as exc:
                remember_release_error(exc)
        ctx.active_config_lock_identity = None
        if release_error is not None:
            if body_completed and not isinstance(release_error, LockOwnershipError):
                raise LockFinalVerificationError(
                    "environment binding changed during final lock release verification"
                ) from release_error
            raise release_error


def build_startup_intent(
    ctx: RuntimeContext,
    startup_nonce: str,
) -> dict[str, Any]:
    if STARTUP_NONCE_RE.fullmatch(startup_nonce) is None:
        raise ConfigError("startup nonce is invalid")
    loader_binding = ctx.manifest["bindings"]["loader_save"]
    loader_state = loader_binding["state"]
    loader_hash = require_hash(loader_state.get("sha256"), "loader save content hash")
    loader_length = loader_state.get("length")
    if isinstance(loader_length, bool) or not isinstance(loader_length, int) or loader_length < 0:
        raise ConfigError("loader save content length is invalid")
    expected_probe_hash = require_hash(
        ctx.runtime_bindings["installed_probe"]["dll_sha256"],
        "installed Probe DLL hash",
    )
    runtime_result = ctx.runtime_bindings["runtime_bootstrap_result"]
    return self_hash_document(
        {
            "schema": STARTUP_INTENT_SCHEMA,
            "validation_run_id": ctx.roots.run_id,
            "startup_nonce": startup_nonce,
            "environment_manifest_sha256": ctx.manifest_sha256,
            "requested_loader_path_id_lower": loader_binding["path_id_lower"],
            "requested_loader_path_id_upper": loader_binding["path_id_upper"],
            "requested_loader_save_sha256": loader_hash,
            "requested_loader_save_length": loader_length,
            "expected_game_build": EXPECTED_GAME_BUILD,
            "expected_mod_version": EXPECTED_MOD_VERSION,
            "expected_probe_dll_sha256": expected_probe_hash,
            "runtime_bootstrap_result_path_id_lower": runtime_result[
                "path_id_lower"
            ],
            "runtime_bootstrap_result_sha256": runtime_result["raw_sha256"],
            "runtime_bootstrap_result_hash": runtime_result["result_hash"],
            "runtime_bootstrap_transaction_id": runtime_result["transaction_id"],
            "installed_probe_tree_content_hash": runtime_result[
                "probe_tree_content_hash"
            ],
            "auto_resume_semantics": AUTO_RESUME_STARTUP_SEMANTICS,
        }
    )


def build_startup_binding(
    ctx: RuntimeContext,
    startup_intent: Mapping[str, Any],
    runtime_config_evidence_sha256: str,
    created_at: str,
) -> dict[str, Any]:
    require_canonical_utc_timestamp(created_at, "startup binding timestamp")
    return self_hash_document(
        {
            "schema": STARTUP_BINDING_SCHEMA,
            "validation_run_id": ctx.roots.run_id,
            "startup_nonce": startup_intent["startup_nonce"],
            "startup_intent_hash": startup_intent["document_hash"],
            "runtime_config_evidence_sha256": require_hash(
                runtime_config_evidence_sha256,
                "runtime config evidence raw SHA-256",
            ),
            "environment_manifest_sha256": ctx.manifest_sha256,
            "expected_game_build": startup_intent["expected_game_build"],
            "expected_mod_version": startup_intent["expected_mod_version"],
            "expected_probe_dll_sha256": startup_intent[
                "expected_probe_dll_sha256"
            ],
            "runtime_bootstrap_result_path_id_lower": startup_intent[
                "runtime_bootstrap_result_path_id_lower"
            ],
            "runtime_bootstrap_result_sha256": startup_intent[
                "runtime_bootstrap_result_sha256"
            ],
            "runtime_bootstrap_result_hash": startup_intent[
                "runtime_bootstrap_result_hash"
            ],
            "runtime_bootstrap_transaction_id": startup_intent[
                "runtime_bootstrap_transaction_id"
            ],
            "installed_probe_tree_content_hash": startup_intent[
                "installed_probe_tree_content_hash"
            ],
            "requested_loader_path_id_upper": startup_intent[
                "requested_loader_path_id_upper"
            ],
            "requested_loader_save_sha256": startup_intent[
                "requested_loader_save_sha256"
            ],
            "requested_loader_save_length": startup_intent[
                "requested_loader_save_length"
            ],
            "created_at": created_at,
        }
    )


def build_evidence(
    ctx: RuntimeContext,
    desired: Mapping[str, bytes],
    after_states: Mapping[str, Mapping[str, Any]],
    mod_count: int,
    transaction_id: str,
    configured_at: str,
    startup_intent: Mapping[str, Any],
) -> dict[str, Any]:
    bindings = ctx.manifest["bindings"]
    evidence: dict[str, Any] = {
        "schema": EVIDENCE_SCHEMA,
        "run_id": ctx.roots.run_id,
        "mode": "live" if ctx.roots.live_mode else "test_fixture",
        "configured_at": configured_at,
        "transaction_id": transaction_id,
        "environment_manifest_sha256": ctx.manifest_sha256,
        "bindings": {
            "pre_boundary_sha256": ctx.manifest["bindings"]["pre_boundary"]["sha256"],
            "clone_manifest_sha256": ctx.manifest["bindings"]["clone_manifest"]["sha256"],
            "source_save": {
                "path_id_lower": bindings["source_save"]["path_id_lower"],
                "path_id_upper": bindings["source_save"]["path_id_upper"],
                "state": bindings["source_save"]["state"],
            },
            "loader_save": {
                "path_id_lower": bindings["loader_save"]["path_id_lower"],
                "path_id_upper": bindings["loader_save"]["path_id_upper"],
                "stem_id": bindings["loader_save"]["stem_id"],
                "state": bindings["loader_save"]["state"],
            },
        },
        "runtime": dict(ctx.runtime_bindings),
        "configs": {
            relative: {
                "relative": relative,
                "path_id_lower": ctx.config_records[relative]["path_id_lower"],
                "before": ctx.current_states.get(
                    relative,
                    ctx.config_records[relative]["state"],
                ),
                "after": dict(after_states[relative]),
                "after_bytes_sha256": sha256_bytes(desired[relative]),
            }
            for relative in TARGET_CONFIG_RELATIVES
        },
        "loader_policy": loader_policy_from_kplayerprefs(
            desired["kplayerprefs.yaml"],
            ctx.loader_save,
            ctx.managed_assembly_binding,
        ),
        "startup_intent": dict(startup_intent),
        "mod_policy": {
            "mod_count": mod_count,
            "enabled_static_ids": [PROBE_STATIC_ID],
            "all_other_mods_disabled": True,
        },
        "restoration": {
            "owner": "environment_finalizer",
            "original_config_snapshot_bound": True,
        },
    }
    return self_hash_document(evidence)


def publish_recovery_evidence(
    ctx: RuntimeContext,
    *,
    transaction_id: str,
    reason: str,
    unsafe_relatives: Iterable[str],
    publication_outcome: str,
    recovery_claims: Iterable[pathlib.Path] = (),
) -> PublicationRecord:
    recovery: dict[str, Any] = {
        "schema": RECOVERY_SCHEMA,
        "status": "outcome_unknown",
        "run_id": ctx.roots.run_id,
        "transaction_id": transaction_id,
        "recorded_at": now_iso(),
        "environment_manifest_sha256": ctx.manifest_sha256,
        "reason": reason,
        "publication_outcome": publication_outcome,
        "unsafe_config_path_ids": [
            ctx.config_records[item]["path_id_lower"]
            for item in sorted(set(unsafe_relatives))
            if item in ctx.config_records
        ],
        "recovery_claim_path_ids": [
            path_id(item, contract="lower") for item in sorted(set(recovery_claims), key=str)
        ],
        "automatic_overwrite_performed": False,
        "requires_manual_recovery_review": True,
    }
    recovery["document_hash"] = sha256_bytes(canonical_bytes(recovery))
    path = ctx.roots.environment_dir / f"runtime-config-recovery-{transaction_id}.json"
    try:
        return create_only_json(
            ctx,
            path,
            recovery,
            ctx.roots.evidence_root,
            allow_test_faults=False,
        )
    except PublicationError as exc:
        raise OutcomeUnknownError(
            "runtime config outcome is unknown and recovery evidence publication also failed"
        ) from exc


def configure(args: argparse.Namespace) -> dict[str, Any]:
    roots = resolve_roots(args)
    require_test_controls_mode(args, roots.test_root)
    counts = process_counts(args, roots)
    require_processes_stopped(counts)
    ctx = validate_environment(args, roots, phase="before")
    require_publication_targets_absent(ctx)
    desired, mod_count = build_desired_configs(ctx, from_backup=False)
    loader_policy = loader_policy_from_kplayerprefs(
        desired["kplayerprefs.yaml"],
        ctx.loader_save,
        ctx.managed_assembly_binding,
    )
    predicted = {
        relative: {
            "before_sha256": ctx.current_states[relative]["sha256"],
            "after_sha256": sha256_bytes(desired[relative]),
            "after_length": len(desired[relative]),
        }
        for relative in TARGET_CONFIG_RELATIVES
    }
    if not args.apply:
        return {
            "schema": PLAN_SCHEMA,
            "status": "would_configure",
            "run_id": roots.run_id,
            "write_count": 0,
            "planned_atomic_replacements": 2,
            "planned_create_only_evidence": 2,
            "environment_manifest_sha256": ctx.manifest_sha256,
            "configs": predicted,
            "loader_policy": loader_policy,
            "mod_count": mod_count,
            "enabled_static_ids": [PROBE_STATIC_ID],
            "bridge_policy_disabled": True,
            "expected_game_build": EXPECTED_GAME_BUILD,
            "expected_mod_version": EXPECTED_MOD_VERSION,
            "expected_probe_dll_sha256": ctx.runtime_bindings["installed_probe"][
                "dll_sha256"
            ],
            "runtime_bootstrap_result_path_id_lower": ctx.runtime_bindings[
                "runtime_bootstrap_result"
            ]["path_id_lower"],
            "runtime_bootstrap_result_sha256": ctx.runtime_bindings[
                "runtime_bootstrap_result"
            ]["raw_sha256"],
            "runtime_bootstrap_result_hash": ctx.runtime_bindings[
                "runtime_bootstrap_result"
            ]["result_hash"],
            "runtime_bootstrap_transaction_id": ctx.runtime_bindings[
                "runtime_bootstrap_result"
            ]["transaction_id"],
            "installed_probe_tree_content_hash": ctx.runtime_bindings[
                "runtime_bootstrap_result"
            ]["probe_tree_content_hash"],
            "requested_loader_path_id_lower": ctx.manifest["bindings"]["loader_save"][
                "path_id_lower"
            ],
            "requested_loader_path_id_upper": ctx.manifest["bindings"]["loader_save"][
                "path_id_upper"
            ],
            "requested_loader_save_sha256": ctx.manifest["bindings"]["loader_save"][
                "state"
            ]["sha256"],
            "requested_loader_save_length": ctx.manifest["bindings"]["loader_save"][
                "state"
            ]["length"],
            "expected_release_dll_sha256": roots.expected_release_dll_sha256,
            "bootstrap_raw_sha256": ctx.runtime_bindings["bootstrap"]["raw_sha256"],
            "initial_policy_raw_sha256": ctx.runtime_bindings["initial_disabled_policy"][
                "raw_sha256"
            ],
        }

    transaction_id = "tx-" + uuid.uuid4().hex
    startup_nonce = "startup-" + uuid.uuid4().hex
    evidence_publication: Optional[PublicationRecord] = None
    binding_publication: Optional[PublicationRecord] = None
    startup_intent: Optional[dict[str, Any]] = None
    runtime_config_evidence_sha256: Optional[str] = None
    staged: dict[str, StagedRecord] = {}
    committed: list[CommitRecord] = []

    def require_final_transaction_binding() -> None:
        if evidence_publication is None or binding_publication is None:
            raise ConfigError("final lock verification is missing the startup pair")
        require_processes_stopped(process_counts(args, roots))
        require_current_save_clone_binding(ctx)
        require_current_immutable_environment_binding(ctx)
        require_current_bridge_probe_binding(
            ctx,
            require_disabled_policy=True,
        )
        require_current_managed_assembly_binding(ctx)
        if not publication_record_is_owned(binding_publication):
            raise ConfigError("startup binding changed during final lock verification")
        if not publication_record_is_owned(evidence_publication):
            raise ConfigError("runtime evidence changed during final lock verification")
        if any(not commit_is_owned(ctx, record) for record in committed):
            raise OutcomeUnknownError(
                "configured runtime ownership changed during final lock verification"
            )

    def raise_after_transaction_cleanup(exc: Exception) -> None:
        publication_failure = isinstance(exc, PublicationError)
        published_pair_member = (
            evidence_publication is not None or binding_publication is not None
        )
        if publication_failure or published_pair_member:
            revocation_failed = False
            for stage, record in (
                ("binding", binding_publication),
                ("evidence", evidence_publication),
            ):
                if record is not None and not revoke_publication_record(
                    ctx,
                    record,
                    stage=stage,
                ):
                    revocation_failed = True
            publication_unknown = (
                isinstance(exc, PublicationError)
                and exc.outcome == "outcome_unknown"
            ) or revocation_failed
            if publication_unknown:
                publish_recovery_evidence(
                    ctx,
                    transaction_id=transaction_id,
                    reason="startup_pair_publication_identity_or_result_unknown",
                    unsafe_relatives=[record.relative for record in committed],
                    publication_outcome="outcome_unknown",
                )
                raise OutcomeUnknownError(
                    "runtime startup pair publication outcome is unknown"
                ) from exc
            rollback = restore_owned_configs(ctx, committed)
            if rollback.unrestored:
                publish_recovery_evidence(
                    ctx,
                    transaction_id=transaction_id,
                    reason="startup_pair_revoked_but_config_ownership_lost",
                    unsafe_relatives=rollback.unrestored,
                    publication_outcome="revoked",
                    recovery_claims=rollback.recovery_claims,
                )
                raise OutcomeUnknownError(
                    "startup pair was revoked but external config state was preserved"
                ) from exc
            raise exc

        rollback = restore_owned_configs(ctx, committed)
        attempt_unrestored: tuple[str, ...] = ()
        attempt_claims: tuple[pathlib.Path, ...] = ()
        if isinstance(exc, MutationOwnershipError):
            attempt_unrestored = (exc.relative,)
            attempt_claims = exc.recovery_claims
        unrestored = tuple(sorted(set(rollback.unrestored + attempt_unrestored)))
        claims = rollback.recovery_claims + attempt_claims
        if unrestored:
            publish_recovery_evidence(
                ctx,
                transaction_id=transaction_id,
                reason="conditional_rollback_refused_external_state",
                unsafe_relatives=unrestored,
                publication_outcome="not_published",
                recovery_claims=claims,
            )
            raise OutcomeUnknownError(
                "external config state was preserved; recovery evidence is required"
            ) from exc
        raise exc

    try:
        with exclusive_environment_lock(
            ctx,
            final_verifier=require_final_transaction_binding,
        ) as config_lock_identity:
            require_test_controls_mode(args, roots.test_root)
            # Re-read every binding under the lock.  This also detects a finalize
            # that completed immediately before the lock was acquired.
            ctx = validate_environment(
                args,
                roots,
                phase="before",
                allowed_config_lock_identity=config_lock_identity,
            )
            invoke_test_hook("before_locked_process_recheck", ctx)
            require_processes_stopped(process_counts(args, roots))
            require_current_save_clone_binding(ctx)
            require_current_immutable_environment_binding(ctx)
            require_current_bridge_probe_binding(
                ctx,
                require_disabled_policy=True,
            )
            require_publication_targets_absent(ctx)
            desired, mod_count = build_desired_configs(ctx, from_backup=False)
            invoke_test_hook("before_commit", ctx)
            # The first owned config write is forbidden unless the external
            # manifest anchor still matches under the environment lock.
            require_current_immutable_environment_binding(ctx)
            verify_current_snapshots(ctx)
            try:
                for relative in TARGET_CONFIG_RELATIVES:
                    target = roots.oni_root / pathlib.Path(*pathlib.PurePosixPath(relative).parts)
                    staged[relative] = write_staged(
                        target,
                        desired[relative],
                        roots.oni_root,
                        ctx=ctx,
                        hook_event=f"after_stage_close_{relative}",
                    )
                verify_current_snapshots(ctx)
                require_current_managed_assembly_binding(ctx)
                for index, relative in enumerate(TARGET_CONFIG_RELATIVES, start=1):
                    require_current_environment_manifest_anchor(ctx)
                    target = roots.oni_root / pathlib.Path(*pathlib.PurePosixPath(relative).parts)
                    assert_no_reparse(target, roots.oni_root)
                    record = replace_owned_target_create_only(
                        ctx,
                        relative,
                        staged[relative],
                        expected_identity=ctx.current_identities[relative],
                        expected_sha256=ctx.current_states[relative]["sha256"],
                        expected_length=int(ctx.current_states[relative]["length"]),
                        expected_mtime_ns=int(ctx.current_states[relative]["last_write_ns"]),
                        before_event=f"before_commit_claim_{index}",
                        after_event=f"after_commit_claim_{index}",
                    )
                    committed.append(record)
                    invoke_test_hook(f"after_replace_{index}", ctx)
                    # This check is deliberately inside the replace loop so a
                    # coherent manifest/journal swap cannot bridge two writes.
                    require_current_environment_manifest_anchor(ctx)
                    if not commit_is_owned(ctx, record):
                        raise OutcomeUnknownError(
                            "committed runtime config was externally changed after replace"
                        )
                    if (
                        roots.test_root is not None
                        and getattr(args, "test_fail_after_replace", 0) == index
                    ):
                        raise ConfigError("injected TestRoot partial-commit failure")
                after_states: dict[str, dict[str, Any]] = {}
                for record in committed:
                    if not commit_is_owned(ctx, record):
                        raise OutcomeUnknownError("committed runtime config ownership was lost")
                    target = roots.oni_root / pathlib.Path(*pathlib.PurePosixPath(record.relative).parts)
                    payload, state_value = read_snapshot(target)
                    if payload != desired[record.relative]:
                        raise OutcomeUnknownError("committed runtime config bytes diverged")
                    after_states[record.relative] = state_value
                invoke_test_hook("before_startup_intent_capture", ctx)
                require_processes_stopped(process_counts(args, roots))
                require_current_save_clone_binding(ctx)
                require_current_immutable_environment_binding(ctx)
                require_current_bridge_probe_binding(
                    ctx,
                    require_disabled_policy=True,
                )
                require_current_managed_assembly_binding(ctx)
                configured_at = now_iso()
                require_canonical_utc_timestamp(configured_at, "configuration timestamp")
                startup_intent = build_startup_intent(ctx, startup_nonce)
                evidence = build_evidence(
                    ctx,
                    desired,
                    after_states,
                    mod_count,
                    transaction_id,
                    configured_at,
                    startup_intent,
                )
                evidence_payload = json_document_bytes(evidence)
                runtime_config_evidence_sha256 = sha256_bytes(evidence_payload)
                startup_binding = build_startup_binding(
                    ctx,
                    startup_intent,
                    runtime_config_evidence_sha256,
                    configured_at,
                )
                startup_binding_payload = json_document_bytes(startup_binding)
                invoke_test_hook("before_evidence", ctx)
                require_current_immutable_environment_binding(ctx)
                require_current_managed_assembly_binding(ctx)

                def managed_evidence_pre_publish_check() -> None:
                    invoke_test_hook("before_managed_prepublication_check", ctx)
                    invoke_test_hook("before_evidence_managed_prepublication_check", ctx)
                    consume_test_fault("evidence_managed_prepublication_check_failure", ctx)
                    consume_test_fault("managed_prepublication_check_failure", ctx)
                    require_processes_stopped(process_counts(args, roots))
                    require_current_save_clone_binding(ctx)
                    require_current_immutable_environment_binding(ctx)
                    require_current_bridge_probe_binding(
                        ctx,
                        require_disabled_policy=True,
                    )
                    require_current_managed_assembly_binding(ctx)

                def managed_evidence_post_publish_check() -> None:
                    invoke_test_hook("before_managed_postpublication_check", ctx)
                    invoke_test_hook("before_evidence_managed_postpublication_check", ctx)
                    consume_test_fault("evidence_managed_postpublication_check_failure", ctx)
                    consume_test_fault("managed_postpublication_check_failure", ctx)
                    require_processes_stopped(process_counts(args, roots))
                    require_current_save_clone_binding(ctx)
                    require_current_immutable_environment_binding(ctx)
                    require_current_bridge_probe_binding(
                        ctx,
                        require_disabled_policy=True,
                    )
                    require_current_managed_assembly_binding(ctx)

                evidence_publication = create_only_json(
                    ctx,
                    ctx.evidence_path,
                    evidence,
                    roots.evidence_root,
                    publication_stage="evidence",
                    pre_publish_check=managed_evidence_pre_publish_check,
                    post_publish_check=managed_evidence_post_publish_check,
                )
                if (
                    evidence_publication.sha256 != runtime_config_evidence_sha256
                    or evidence_publication.length != len(evidence_payload)
                ):
                    raise ConfigError("runtime evidence publication diverged from predicted bytes")
                invoke_test_hook("after_evidence_publication", ctx)
                if not publication_record_is_owned(evidence_publication):
                    raise ConfigError("runtime evidence ownership changed before binding publication")
                require_processes_stopped(process_counts(args, roots))
                require_current_save_clone_binding(ctx)
                require_current_immutable_environment_binding(ctx)
                require_current_bridge_probe_binding(
                    ctx,
                    require_disabled_policy=True,
                )
                require_current_managed_assembly_binding(ctx)
                if any(not commit_is_owned(ctx, record) for record in committed):
                    raise OutcomeUnknownError(
                        "configured runtime ownership changed before binding publication"
                    )

                def managed_binding_pre_publish_check() -> None:
                    invoke_test_hook("before_binding_managed_prepublication_check", ctx)
                    consume_test_fault("binding_managed_prepublication_check_failure", ctx)
                    if evidence_publication is None or not publication_record_is_owned(
                        evidence_publication
                    ):
                        raise ConfigError("runtime evidence ownership changed before binding link")
                    require_processes_stopped(process_counts(args, roots))
                    require_current_save_clone_binding(ctx)
                    require_current_immutable_environment_binding(ctx)
                    require_current_bridge_probe_directory_identity(ctx)
                    require_current_managed_assembly_binding(ctx)

                def managed_binding_post_publish_check() -> None:
                    invoke_test_hook("before_binding_managed_postpublication_check", ctx)
                    consume_test_fault("binding_managed_postpublication_check_failure", ctx)
                    if evidence_publication is None or not publication_record_is_owned(
                        evidence_publication
                    ):
                        raise ConfigError("runtime evidence ownership changed after binding link")
                    require_processes_stopped(process_counts(args, roots))
                    require_current_save_clone_binding(ctx)
                    require_current_immutable_environment_binding(ctx)
                    require_current_bridge_probe_directory_identity(ctx)
                    require_current_managed_assembly_binding(ctx)

                binding_publication = create_only_json(
                    ctx,
                    ctx.startup_binding_path,
                    startup_binding,
                    roots.oni_root,
                    publication_stage="binding",
                    pre_publish_check=managed_binding_pre_publish_check,
                    post_publish_check=managed_binding_post_publish_check,
                )
                if (
                    binding_publication.sha256 != sha256_bytes(startup_binding_payload)
                    or binding_publication.length != len(startup_binding_payload)
                ):
                    raise ConfigError("startup binding publication diverged from predicted bytes")
                invoke_test_hook("before_pair_postcheck", ctx)
                consume_test_fault("pair_postcheck_failure", ctx)
                require_processes_stopped(process_counts(args, roots))
                require_current_save_clone_binding(ctx)
                require_current_immutable_environment_binding(ctx)
                require_current_bridge_probe_binding(
                    ctx,
                    require_disabled_policy=True,
                )
                require_current_managed_assembly_binding(ctx)
                if not publication_record_is_owned(binding_publication):
                    raise ConfigError("startup binding ownership changed during pair recapture")
                if not publication_record_is_owned(evidence_publication):
                    raise ConfigError("runtime evidence ownership changed during pair recapture")
                if any(not commit_is_owned(ctx, record) for record in committed):
                    raise OutcomeUnknownError(
                        "configured runtime ownership changed during pair recapture"
                    )
            except Exception as exc:
                raise_after_transaction_cleanup(exc)
            finally:
                for item in staged.values():
                    if os.path.lexists(item.path):
                        safe_unlink_owned(item.path, item.identity)
    except LockOwnershipError:
        # Never unlink or overwrite an unowned replacement lock.  A successful
        # publication, if any, remains paired with the configured bytes.
        raise
    except LockFinalVerificationError as exc:
        # The body committed, but a deterministic release/return checkpoint
        # invalidated the external environment anchor.  Revoke and roll back.
        raise_after_transaction_cleanup(exc)

    try:
        # One final deterministic checkpoint runs after the lock descriptor has
        # been released and immediately before constructing a configured
        # result.  A TestRoot hook can prove this return-gap path fail-closes.
        invoke_test_hook("before_configured_result", ctx)
        require_final_transaction_binding()
    except Exception as exc:
        raise_after_transaction_cleanup(exc)

    if (
        evidence_publication is None
        or binding_publication is None
        or startup_intent is None
        or runtime_config_evidence_sha256 is None
    ):
        raise ConfigError("runtime config transaction completed without an owned startup pair")
    loader_binding = ctx.manifest["bindings"]["loader_save"]
    loader_state = loader_binding["state"]
    return {
        "schema": RESULT_SCHEMA,
        "status": "configured",
        "run_id": roots.run_id,
        "environment_manifest_sha256": ctx.manifest_sha256,
        "runtime_config_evidence_path_id": path_id(ctx.evidence_path, contract="lower"),
        "runtime_config_evidence_sha256": evidence_publication.sha256,
        "runtime_config_evidence_identity_id": identity_id(evidence_publication.identity),
        "startup_binding_path_id": path_id(ctx.startup_binding_path, contract="lower"),
        "startup_binding_sha256": binding_publication.sha256,
        "startup_binding_identity_id": identity_id(binding_publication.identity),
        "startup_nonce": startup_intent["startup_nonce"],
        "startup_intent_hash": startup_intent["document_hash"],
        "requested_loader_path_id_lower": loader_binding["path_id_lower"],
        "requested_loader_path_id_upper": loader_binding["path_id_upper"],
        "requested_loader_save_sha256": loader_state["sha256"],
        "requested_loader_save_length": loader_state["length"],
        "expected_game_build": EXPECTED_GAME_BUILD,
        "expected_mod_version": EXPECTED_MOD_VERSION,
        "expected_probe_dll_sha256": startup_intent["expected_probe_dll_sha256"],
        "runtime_bootstrap_result_path_id_lower": startup_intent[
            "runtime_bootstrap_result_path_id_lower"
        ],
        "runtime_bootstrap_result_sha256": startup_intent[
            "runtime_bootstrap_result_sha256"
        ],
        "runtime_bootstrap_result_hash": startup_intent[
            "runtime_bootstrap_result_hash"
        ],
        "runtime_bootstrap_transaction_id": startup_intent[
            "runtime_bootstrap_transaction_id"
        ],
        "installed_probe_tree_content_hash": startup_intent[
            "installed_probe_tree_content_hash"
        ],
        "transaction_id": transaction_id,
        "config_count": 2,
        "mod_count": mod_count,
        "enabled_static_ids": [PROBE_STATIC_ID],
        "loader_clone_only": True,
        "loader_policy": loader_policy_from_kplayerprefs(
            desired["kplayerprefs.yaml"],
            ctx.loader_save,
            ctx.managed_assembly_binding,
        ),
        "original_configs_owned_by_finalizer": True,
        "expected_release_dll_sha256": roots.expected_release_dll_sha256,
        "bootstrap_raw_sha256": ctx.runtime_bindings["bootstrap"]["raw_sha256"],
        "initial_policy_raw_sha256": ctx.runtime_bindings["initial_disabled_policy"][
            "raw_sha256"
        ],
    }


def decode_frozen_json_record(record: Mapping[str, Any], label: str) -> dict[str, Any]:
    encoded = record.get("raw_utf8_base64")
    length = record.get("raw_byte_length")
    if not isinstance(encoded, str) or isinstance(length, bool) or not isinstance(length, int):
        raise ConfigError(f"{label} exact raw payload fields are invalid")
    try:
        payload = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (UnicodeError, ValueError) as exc:
        raise ConfigError(f"{label} exact raw payload is not canonical base64") from exc
    if base64.b64encode(payload).decode("ascii") != encoded:
        raise ConfigError(f"{label} exact raw payload base64 is not canonical")
    if payload.startswith(b"\xef\xbb\xbf") or len(payload) != length:
        raise ConfigError(f"{label} exact raw UTF-8 byte length/BOM is invalid")
    if require_hash(record.get("raw_sha256"), f"{label} raw SHA-256") != sha256_bytes(payload):
        raise ConfigError(f"{label} exact raw SHA-256 binding is invalid")
    document = parse_json_payload(payload, label)
    embedded = record.get("document")
    if not isinstance(embedded, dict) or not canonical_typed_equal(document, embedded):
        raise ConfigError(f"{label} raw JSON and embedded document diverge")
    if record.get("document_hash") != sha256_bytes(canonical_bytes(document)):
        raise ConfigError(f"{label} canonical document hash is invalid")
    return document


def decode_frozen_runtime_result_record(
    ctx: RuntimeContext,
    record: Mapping[str, Any],
) -> dict[str, Any]:
    if set(record) != RUNTIME_BOOTSTRAP_RESULT_EVIDENCE_KEYS:
        raise ConfigError("frozen runtime bootstrap result record shape is invalid")
    encoded = record.get("raw_utf8_base64")
    length = record.get("raw_byte_length")
    if not isinstance(encoded, str) or type(length) is not int or length < 0:
        raise ConfigError("frozen runtime bootstrap result raw fields are invalid")
    try:
        payload = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (UnicodeError, ValueError) as exc:
        raise ConfigError("frozen runtime bootstrap result base64 is invalid") from exc
    if (
        base64.b64encode(payload).decode("ascii") != encoded
        or payload.startswith(b"\xef\xbb\xbf")
        or len(payload) != length
    ):
        raise ConfigError("frozen runtime bootstrap result raw representation is invalid")
    external_hash = require_hash(
        record.get("external_expected_sha256"),
        "frozen runtime bootstrap result external hash",
    )
    if (
        external_hash != ctx.roots.expected_runtime_bootstrap_result_sha256
        or require_hash(
            record.get("raw_sha256"), "frozen runtime bootstrap result raw hash"
        )
        != external_hash
        or sha256_bytes(payload) != external_hash
        or record.get("path_id_lower")
        != path_id(ctx.roots.runtime_bootstrap_result, contract="lower")
    ):
        raise ConfigError("frozen runtime bootstrap result external binding is invalid")
    document = parse_json_payload(payload, "frozen runtime bootstrap result")
    if payload != json_document_bytes(document):
        raise ConfigError("frozen runtime bootstrap result serialization is not exact")
    embedded = record.get("document")
    if (
        not isinstance(embedded, dict)
        or not canonical_typed_equal(document, embedded)
        or set(document) != RUNTIME_BOOTSTRAP_RESULT_KEYS
    ):
        raise ConfigError("frozen runtime bootstrap result document is not exact")
    claimed = require_hash(document.get("result_hash"), "frozen result semantic hash")
    unsigned = dict(document)
    del unsigned["result_hash"]
    if (
        sha256_bytes(canonical_bytes(unsigned)) != claimed
        or record.get("result_hash") != claimed
        or record.get("transaction_id") != document.get("transaction_id")
        or record.get("probe_tree_content_hash")
        != document.get("probe_tree_content_hash")
    ):
        raise ConfigError("frozen runtime bootstrap result semantic binding is invalid")
    return document


def validate_frozen_runtime_bootstrap_result_semantics(
    roots: Roots,
    manifest: Mapping[str, Any],
    runtime_bootstrap_result_record: Mapping[str, Any],
    bootstrap_record: Mapping[str, Any],
    *,
    active_parent_finalize_lock: Optional[ActiveParentFinalizeLock] = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Revalidate one frozen config-v5 bootstrap certificate without live Probe churn.

    This is an internal historical/recovery API.  It deliberately reuses the
    normal result/stage/lock/candidate/producer validator.  Its sole phase
    exception is that a current canonical restore journal may have an
    append-only suffix after the frozen environment origin even when no parent
    finalize-lock descriptor exists.  The ordinary configure/verify entry
    points never enable that exception.
    """

    if not isinstance(roots, Roots) or not isinstance(manifest, Mapping):
        raise ConfigError("internal frozen runtime bootstrap inputs are invalid")
    if active_parent_finalize_lock is not None:
        require_current_active_parent_finalize_lock(
            roots,
            active_parent_finalize_lock,
        )
    if (
        not isinstance(runtime_bootstrap_result_record, Mapping)
        or set(runtime_bootstrap_result_record)
        != RUNTIME_BOOTSTRAP_RESULT_EVIDENCE_KEYS
        or not isinstance(bootstrap_record, Mapping)
        or set(bootstrap_record) != BOOTSTRAP_EVIDENCE_KEYS
    ):
        raise ConfigError("frozen runtime bootstrap historical record shape is invalid")

    decoded_result = decode_frozen_runtime_result_record(
        argparse.Namespace(roots=roots),
        runtime_bootstrap_result_record,
    )
    bootstrap = decode_frozen_json_record(
        bootstrap_record,
        "historical frozen bootstrap",
    )
    try:
        bootstrap_payload = base64.b64decode(
            str(bootstrap_record["raw_utf8_base64"]).encode("ascii"),
            validate=True,
        )
    except (UnicodeError, ValueError) as exc:
        raise ConfigError("historical frozen bootstrap payload is invalid") from exc
    bootstrap_state = {
        "sha256": require_hash(
            bootstrap_record.get("raw_sha256"),
            "historical frozen bootstrap raw hash",
        )
    }
    bridge = roots.oni_root / pathlib.Path(*BRIDGE_RELATIVE.parts)
    if bootstrap_record.get("path_id_lower") != path_id(
        bridge / "v27-runtime-bootstrap.json",
        contract="lower",
    ):
        raise ConfigError("historical frozen bootstrap path binding is invalid")
    built_hash, installed_hash = validate_bootstrap_document(
        bootstrap,
        roots,
        bridge,
        EXPECTED_MOD_VERSION,
    )
    if (
        built_hash != roots.expected_release_dll_sha256
        or installed_hash != roots.expected_release_dll_sha256
    ):
        raise ConfigError("historical frozen bootstrap lost its Release binding")

    artifacts = decoded_result.get("installed_probe_artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != set(
        RUNTIME_BOOTSTRAP_ARTIFACT_NAMES
    ):
        raise ConfigError("historical frozen Probe artifact set is invalid")
    content_entries: list[dict[str, Any]] = []
    for name in sorted(RUNTIME_BOOTSTRAP_ARTIFACT_NAMES, key=str.casefold):
        artifact = artifacts.get(name)
        if not isinstance(artifact, dict) or set(artifact) != {"length", "sha256"}:
            raise ConfigError("historical frozen Probe artifact record is invalid")
        length = require_exact_nonnegative_int(
            artifact.get("length"),
            f"historical frozen Probe {name} length",
        )
        artifact_hash = require_hash(
            artifact.get("sha256"),
            f"historical frozen Probe {name} hash",
        )
        content_entries.append(
            {
                "kind": "file",
                "relative": name,
                "length": length,
                "sha256": artifact_hash,
            }
        )
    probe_content = {"exists": True, "entries": content_entries}
    probe_identity = decoded_result.get("installed_probe_tree_identity")
    validate_identity_tree(probe_identity, probe_identity if isinstance(probe_identity, dict) else {})
    identity_entries = probe_identity.get("entries") if isinstance(probe_identity, dict) else None
    expected_identity_shapes = [
        (name, "file")
        for name in sorted(RUNTIME_BOOTSTRAP_ARTIFACT_NAMES, key=str.casefold)
    ]
    if not isinstance(identity_entries, list) or [
        (entry.get("relative"), entry.get("kind"))
        for entry in identity_entries
        if isinstance(entry, dict)
    ] != expected_identity_shapes:
        raise ConfigError("historical frozen Probe identity artifact set is invalid")
    probe_tree = {
        "artifacts": copy.deepcopy(artifacts),
        "content": probe_content,
        "content_hash": sha256_bytes(canonical_bytes(probe_content)),
        "identity": copy.deepcopy(probe_identity),
    }

    journal_path = roots.environment_dir / "restore-journal.jsonl"
    journal_payload, journal_state = read_snapshot(journal_path)
    journal_identity = capture_bound_regular_identity(
        journal_path,
        roots.environment_dir,
        journal_state,
        "historical restore journal",
    )
    def validate_historical_result_once() -> tuple[
        dict[str, Any],
        list[ImmutableFileBinding],
        list[ImmutableDirectoryBinding],
    ]:
        return validate_runtime_bootstrap_result(
            roots,
            manifest,
            bootstrap,
            bootstrap_payload,
            bootstrap_state,
            probe_tree,
            {"content_hash": decoded_result.get("bridge_tree_content_hash")},
            phase="evidence",
            allowed_config_lock_identity=None,
            active_parent_finalize_lock=active_parent_finalize_lock,
            require_current_bridge_content=False,
            _allow_historical_journal_append=True,
        )

    reconstructed_record, immutable_files, immutable_directories = (
        validate_historical_result_once()
    )
    if (
        not canonical_typed_equal(
            reconstructed_record,
            runtime_bootstrap_result_record,
        )
        or not canonical_typed_equal(
            reconstructed_record.get("document"),
            decoded_result,
        )
    ):
        raise ConfigError("historical frozen runtime result reconstruction diverged")

    if _TEST_HOOK is not None:
        if roots.test_root is None:
            raise ConfigError("test hooks are forbidden in live mode")
        _TEST_HOOK(
            "before_frozen_runtime_semantics_final_recheck",
            argparse.Namespace(roots=roots),
        )
    final_record, immutable_files, immutable_directories = (
        validate_historical_result_once()
    )
    if not canonical_typed_equal(final_record, reconstructed_record):
        raise ConfigError("historical frozen runtime result changed during revalidation")
    for binding in immutable_directories:
        try:
            metadata = os.lstat(binding.path)
        except OSError as exc:
            raise ConfigError(f"{binding.label} is missing") from exc
        if (
            is_reparse(binding.path)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata_identity(metadata) != binding.identity
        ):
            raise ConfigError(f"{binding.label} identity changed during historical validation")
    for binding in immutable_files:
        current_identity = capture_bound_regular_identity(
            binding.path,
            binding.boundary,
            binding.state,
            binding.label,
        )
        if current_identity != binding.identity:
            raise ConfigError(f"{binding.label} changed during historical validation")
    for binding in immutable_directories:
        metadata = os.lstat(binding.path)
        if (
            is_reparse(binding.path)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata_identity(metadata) != binding.identity
        ):
            raise ConfigError(f"{binding.label} changed during historical recapture")
    if (
        capture_bound_regular_identity(
            journal_path,
            roots.environment_dir,
            journal_state,
            "historical restore journal",
        )
        != journal_identity
        or read_snapshot(journal_path)[0] != journal_payload
    ):
        raise ConfigError("historical restore journal changed during semantic validation")
    if active_parent_finalize_lock is not None:
        require_current_active_parent_finalize_lock(
            roots,
            active_parent_finalize_lock,
        )
    return copy.deepcopy(reconstructed_record), copy.deepcopy(decoded_result)


def validate_frozen_runtime_evidence(
    ctx: RuntimeContext,
    runtime: Mapping[str, Any],
) -> None:
    if set(runtime) != RUNTIME_EVIDENCE_KEYS:
        raise ConfigError("runtime evidence field set is not exact")
    for key, expected in ctx.runtime_bindings.items():
        if not canonical_typed_equal(runtime.get(key), expected):
            raise ConfigError("runtime bootstrap/Release/Probe historical anchor is invalid")
    if (
        runtime.get("mod_version") != EXPECTED_MOD_VERSION
        or runtime.get("probe_static_id") != PROBE_STATIC_ID
        or runtime.get("dll_four_way_identical") is not True
    ):
        raise ConfigError("runtime evidence version/static-id/four-way assertion is invalid")

    bootstrap_record = runtime.get("bootstrap")
    result_record = runtime.get("runtime_bootstrap_result")
    release_record = runtime.get("release_dll")
    installed_record = runtime.get("installed_probe")
    policy_record = runtime.get("initial_disabled_policy")
    if (
        not isinstance(bootstrap_record, dict)
        or set(bootstrap_record) != BOOTSTRAP_EVIDENCE_KEYS
        or not isinstance(result_record, dict)
        or set(result_record) != RUNTIME_BOOTSTRAP_RESULT_EVIDENCE_KEYS
        or not isinstance(release_record, dict)
        or set(release_record) != RELEASE_EVIDENCE_KEYS
        or not isinstance(installed_record, dict)
        or set(installed_record) != INSTALLED_PROBE_EVIDENCE_KEYS
        or not isinstance(policy_record, dict)
        or set(policy_record) != POLICY_EVIDENCE_KEYS
    ):
        raise ConfigError("runtime evidence anchor record shape is invalid")

    bootstrap_document = decode_frozen_json_record(bootstrap_record, "frozen bootstrap")
    result_document = decode_frozen_runtime_result_record(ctx, result_record)
    bridge = ctx.roots.oni_root / pathlib.Path(*BRIDGE_RELATIVE.parts)
    built_hash, installed_hash = validate_bootstrap_document(
        bootstrap_document,
        ctx.roots,
        bridge,
        EXPECTED_MOD_VERSION,
    )
    expected_dll_hash = ctx.roots.expected_release_dll_sha256
    if (
        bootstrap_record.get("path_id_lower")
        != path_id(ctx.bootstrap_path, contract="lower")
        or bootstrap_record.get("raw_sha256")
        != ctx.runtime_bindings["bootstrap"]["raw_sha256"]
        or built_hash != expected_dll_hash
        or installed_hash != expected_dll_hash
        or release_record.get("path_id_lower")
        != path_id(ctx.roots.release_dll, contract="lower")
        or require_hash(
            release_record.get("external_expected_sha256"),
            "frozen external Release DLL anchor",
        )
        != expected_dll_hash
        or require_hash(release_record.get("file_sha256"), "workspace Release DLL hash")
        != expected_dll_hash
        or require_hash(installed_record.get("dll_sha256"), "installed Probe DLL hash")
        != expected_dll_hash
        or result_document.get("release_dll_sha256") != expected_dll_hash
    ):
        raise ConfigError("frozen bootstrap/Release/installed DLL four-way binding is invalid")
    require_hash(installed_record.get("manifest_sha256"), "installed Probe manifest hash")
    require_hash(installed_record.get("info_sha256"), "installed Probe info hash")
    require_hash(installed_record.get("pdb_sha256"), "installed Probe PDB hash")
    if (
        require_hash(
            installed_record.get("tree_content_hash"), "installed Probe tree hash"
        )
        != result_document.get("probe_tree_content_hash")
    ):
        raise ConfigError("frozen installed Probe tree/result binding is invalid")

    policy_document = decode_frozen_json_record(
        policy_record,
        "frozen initial disabled policy",
    )
    validate_disabled_policy_document(policy_document)
    if (
        policy_record.get("path_id_lower") != path_id(ctx.policy_path, contract="lower")
    ):
        raise ConfigError("frozen initial disabled/unbound policy evidence is invalid")


def validate_startup_intent(
    ctx: RuntimeContext,
    raw_intent: Any,
) -> dict[str, Any]:
    if not isinstance(raw_intent, dict) or set(raw_intent) != STARTUP_INTENT_KEYS:
        raise ConfigError("startup intent field set is not exact")
    if raw_intent.get("schema") != STARTUP_INTENT_SCHEMA:
        raise ConfigError("startup intent schema is invalid")
    nonce = raw_intent.get("startup_nonce")
    if not isinstance(nonce, str) or STARTUP_NONCE_RE.fullmatch(nonce) is None:
        raise ConfigError("startup intent nonce is invalid")
    if (
        isinstance(raw_intent.get("requested_loader_save_length"), bool)
        or not isinstance(raw_intent.get("requested_loader_save_length"), int)
        or raw_intent["requested_loader_save_length"] < 0
    ):
        raise ConfigError("startup intent loader length is invalid")
    claimed = require_hash(
        raw_intent.get("document_hash"),
        "startup intent document hash",
    )
    unsigned = dict(raw_intent)
    del unsigned["document_hash"]
    if claimed != sha256_bytes(canonical_bytes(unsigned)):
        raise ConfigError("startup intent document hash is invalid")
    expected = build_startup_intent(ctx, nonce)
    if not canonical_typed_equal(raw_intent, expected):
        raise ConfigError("startup intent does not exactly bind the requested loader/runtime")
    current_semantics = bound_builtin_auto_resume_semantics(ctx.managed_assembly_binding)
    if current_semantics.get("expected_probe_game_build") != EXPECTED_GAME_BUILD:
        raise ConfigError("current Managed binding does not support the expected game build")
    return dict(raw_intent)


def validate_startup_binding(
    ctx: RuntimeContext,
    raw_binding: Any,
    startup_intent: Mapping[str, Any],
    runtime_config_evidence_sha256: str,
    configured_at: str,
) -> dict[str, Any]:
    if not isinstance(raw_binding, dict) or set(raw_binding) != STARTUP_BINDING_KEYS:
        raise ConfigError("startup binding field set is not exact")
    if raw_binding.get("schema") != STARTUP_BINDING_SCHEMA:
        raise ConfigError("startup binding schema is invalid")
    require_canonical_utc_timestamp(raw_binding.get("created_at"), "startup binding timestamp")
    claimed = require_hash(
        raw_binding.get("document_hash"),
        "startup binding document hash",
    )
    unsigned = dict(raw_binding)
    del unsigned["document_hash"]
    if claimed != sha256_bytes(canonical_bytes(unsigned)):
        raise ConfigError("startup binding document hash is invalid")
    expected = build_startup_binding(
        ctx,
        startup_intent,
        runtime_config_evidence_sha256,
        configured_at,
    )
    if not canonical_typed_equal(raw_binding, expected):
        raise ConfigError("startup binding cross-link or runtime binding is invalid")
    return dict(raw_binding)


def verify_evidence(
    args: argparse.Namespace,
    *,
    active_parent_finalize_lock: Optional[ActiveParentFinalizeLock] = None,
) -> dict[str, Any]:
    roots = resolve_roots(args)
    if active_parent_finalize_lock is not None:
        require_current_active_parent_finalize_lock(
            roots,
            active_parent_finalize_lock,
        )
    counts = process_counts(args, roots)
    require_processes_stopped(counts)
    ctx = validate_environment(
        args,
        roots,
        phase="evidence",
        active_parent_finalize_lock=active_parent_finalize_lock,
    )
    assert_no_reparse(ctx.evidence_path, roots.evidence_root)
    assert_no_reparse(ctx.startup_binding_path, roots.oni_root)
    evidence_payload, evidence_state = read_snapshot(ctx.evidence_path)
    binding_payload, binding_state = read_snapshot(ctx.startup_binding_path)
    expected_evidence_hash = require_hash(
        getattr(args, "expected_runtime_config_evidence_sha256", None),
        "external runtime config evidence anchor",
    )
    if evidence_state["sha256"] != expected_evidence_hash:
        raise ConfigError("runtime config evidence does not match its external SHA-256 anchor")
    expected_binding_hash = require_hash(
        getattr(args, "expected_startup_binding_sha256", None),
        "external startup binding anchor",
    )
    if binding_state["sha256"] != expected_binding_hash:
        raise ConfigError("startup binding does not match its external SHA-256 anchor")
    evidence_metadata = os.lstat(ctx.evidence_path)
    if evidence_metadata.st_nlink != 1:
        raise ConfigError("runtime config evidence is not an exclusive create-only file")
    binding_metadata = os.lstat(ctx.startup_binding_path)
    if binding_metadata.st_nlink != 1:
        raise ConfigError("startup binding is not an exclusive create-only file")
    evidence_record = PublicationRecord(
        path=ctx.evidence_path,
        identity=metadata_identity(evidence_metadata),
        sha256=evidence_state["sha256"],
        length=evidence_state["length"],
    )
    binding_record = PublicationRecord(
        path=ctx.startup_binding_path,
        identity=metadata_identity(binding_metadata),
        sha256=binding_state["sha256"],
        length=binding_state["length"],
    )
    if not publication_record_is_owned(evidence_record, verify_document=False):
        raise ConfigError("runtime config evidence changed during initial verification capture")
    if not publication_record_is_owned(binding_record, verify_document=False):
        raise ConfigError("startup binding changed during initial verification capture")
    evidence = parse_json_payload(evidence_payload, "runtime config evidence")
    if evidence_payload != json_document_bytes(evidence):
        raise ConfigError("runtime config evidence raw serialization is not exact")
    if (
        set(evidence) != EVIDENCE_KEYS
        or evidence.get("schema") != EVIDENCE_SCHEMA
        or evidence.get("run_id") != roots.run_id
        or evidence.get("mode") != ("live" if roots.live_mode else "test_fixture")
        or TRANSACTION_ID_RE.fullmatch(str(evidence.get("transaction_id") or "")) is None
    ):
        raise ConfigError("runtime config evidence does not bind this run")
    configured_at = require_canonical_utc_timestamp(
        evidence.get("configured_at"),
        "runtime config evidence timestamp",
    )
    claimed = require_hash(evidence.get("document_hash"), "runtime config evidence document hash")
    unsigned = dict(evidence)
    del unsigned["document_hash"]
    if claimed != sha256_bytes(canonical_bytes(unsigned)):
        raise ConfigError("runtime config evidence document hash is invalid")
    if evidence.get("environment_manifest_sha256") != ctx.manifest_sha256:
        raise ConfigError("runtime config evidence lost its environment binding")
    manifest_bindings = ctx.manifest["bindings"]
    expected_bindings = {
        "pre_boundary_sha256": manifest_bindings["pre_boundary"]["sha256"],
        "clone_manifest_sha256": manifest_bindings["clone_manifest"]["sha256"],
        "source_save": {
            "path_id_lower": manifest_bindings["source_save"]["path_id_lower"],
            "path_id_upper": manifest_bindings["source_save"]["path_id_upper"],
            "state": manifest_bindings["source_save"]["state"],
        },
        "loader_save": {
            "path_id_lower": manifest_bindings["loader_save"]["path_id_lower"],
            "path_id_upper": manifest_bindings["loader_save"]["path_id_upper"],
            "stem_id": manifest_bindings["loader_save"]["stem_id"],
            "state": manifest_bindings["loader_save"]["state"],
        },
    }
    if not canonical_typed_equal(evidence.get("bindings"), expected_bindings):
        raise ConfigError("runtime config evidence source/loader binding is invalid")
    startup_intent = validate_startup_intent(ctx, evidence.get("startup_intent"))
    evidence_runtime = evidence.get("runtime") if isinstance(evidence.get("runtime"), dict) else {}
    validate_frozen_runtime_evidence(ctx, evidence_runtime)
    desired, mod_count = build_desired_configs(ctx, from_backup=True)
    expected_loader_policy = loader_policy_from_kplayerprefs(
        desired["kplayerprefs.yaml"],
        ctx.loader_save,
        ctx.managed_assembly_binding,
    )
    config_evidence = evidence.get("configs") if isinstance(evidence.get("configs"), dict) else {}
    if set(config_evidence) != set(TARGET_CONFIG_RELATIVES):
        raise ConfigError("runtime config evidence path set is invalid")
    verified_after_states: dict[str, dict[str, Any]] = {}
    for relative in TARGET_CONFIG_RELATIVES:
        record = config_evidence.get(relative) if isinstance(config_evidence.get(relative), dict) else {}
        after = record.get("after") if isinstance(record.get("after"), dict) else {}
        expected_hash = sha256_bytes(desired[relative])
        if (
            set(record) != {"relative", "path_id_lower", "before", "after", "after_bytes_sha256"}
            or set(after) != {"length", "last_write_ns", "sha256"}
            or
            record.get("relative") != relative
            or record.get("path_id_lower") != ctx.config_records[relative]["path_id_lower"]
            or not canonical_typed_equal(
                record.get("before"), ctx.config_records[relative]["state"]
            )
            or record.get("after_bytes_sha256") != expected_hash
            or after.get("sha256") != expected_hash
            or isinstance(after.get("length"), bool)
            or not isinstance(after.get("length"), int)
            or after.get("length") != len(desired[relative])
            or isinstance(after.get("last_write_ns"), bool)
            or not isinstance(after.get("last_write_ns"), int)
            or after["last_write_ns"] < 0
        ):
            raise ConfigError("runtime config evidence before/after binding is invalid")
        verified_after_states[relative] = {
            "length": after["length"],
            "last_write_ns": after["last_write_ns"],
            "sha256": after["sha256"],
        }
    if not canonical_typed_equal(evidence.get("loader_policy"), expected_loader_policy):
        raise ConfigError("runtime loader policy evidence is invalid")
    if not canonical_typed_equal(
        evidence.get("mod_policy"),
        {
            "mod_count": mod_count,
            "enabled_static_ids": [PROBE_STATIC_ID],
            "all_other_mods_disabled": True,
        },
    ):
        raise ConfigError("runtime mod policy evidence is invalid")
    if not canonical_typed_equal(
        evidence.get("restoration"),
        {
            "owner": "environment_finalizer",
            "original_config_snapshot_bound": True,
        },
    ):
        raise ConfigError("runtime config evidence restoration ownership is invalid")
    startup_binding = parse_json_payload(binding_payload, "runtime startup binding")
    if binding_payload != json_document_bytes(startup_binding):
        raise ConfigError("startup binding raw serialization is not exact")
    validate_startup_binding(
        ctx,
        startup_binding,
        startup_intent,
        evidence_state["sha256"],
        configured_at,
    )
    reconstructed_evidence = build_evidence(
        ctx,
        desired,
        verified_after_states,
        mod_count,
        evidence["transaction_id"],
        configured_at,
        startup_intent,
    )
    # Historical verification intentionally does not consult the current
    # policy document.  Its frozen runtime block was independently decoded and
    # validated above, so splice those exact typed records into the complete
    # evidence reconstruction before recomputing the outer self-hash.
    reconstructed_unsigned = dict(reconstructed_evidence)
    reconstructed_unsigned.pop("document_hash", None)
    reconstructed_unsigned["runtime"] = copy.deepcopy(evidence_runtime)
    reconstructed_evidence = self_hash_document(reconstructed_unsigned)
    if not canonical_typed_equal(evidence, reconstructed_evidence):
        raise ConfigError("runtime config evidence canonical typed reconstruction diverged")
    invoke_test_hook("before_verify_pair_postcheck", ctx)
    require_processes_stopped(process_counts(args, roots))
    require_current_source_binding(ctx)
    require_current_immutable_environment_binding(ctx)
    require_current_bridge_probe_binding(
        ctx,
        require_disabled_policy=False,
    )
    require_current_managed_assembly_binding(ctx)
    if not publication_record_is_owned(binding_record):
        raise ConfigError("startup binding changed during final verification recapture")
    if not publication_record_is_owned(evidence_record):
        raise ConfigError("runtime config evidence changed during final verification recapture")
    result: dict[str, Any] = {
        "schema": VERIFY_RESULT_SCHEMA,
        "status": "passed",
        "run_id": roots.run_id,
        "environment_manifest_sha256": ctx.manifest_sha256,
        "runtime_config_evidence_path_id": path_id(
            ctx.evidence_path, contract="lower"
        ),
        "runtime_config_evidence_sha256": evidence_state["sha256"],
        "runtime_config_evidence_identity_id": identity_id(
            metadata_identity(evidence_metadata)
        ),
        "startup_binding_sha256": binding_state["sha256"],
        "startup_binding_path_id": path_id(
            ctx.startup_binding_path, contract="lower"
        ),
        "startup_binding_identity_id": identity_id(metadata_identity(binding_metadata)),
        "startup_nonce": startup_intent["startup_nonce"],
        "startup_intent_hash": startup_intent["document_hash"],
        "requested_loader_path_id_lower": startup_intent[
            "requested_loader_path_id_lower"
        ],
        "requested_loader_path_id_upper": startup_intent[
            "requested_loader_path_id_upper"
        ],
        "requested_loader_save_sha256": startup_intent[
            "requested_loader_save_sha256"
        ],
        "requested_loader_save_length": startup_intent[
            "requested_loader_save_length"
        ],
        "expected_game_build": startup_intent["expected_game_build"],
        "expected_mod_version": startup_intent["expected_mod_version"],
        "expected_probe_dll_sha256": startup_intent[
            "expected_probe_dll_sha256"
        ],
        "runtime_bootstrap_result_path_id_lower": startup_intent[
            "runtime_bootstrap_result_path_id_lower"
        ],
        "runtime_bootstrap_result_sha256": startup_intent[
            "runtime_bootstrap_result_sha256"
        ],
        "runtime_bootstrap_result_hash": startup_intent[
            "runtime_bootstrap_result_hash"
        ],
        "runtime_bootstrap_transaction_id": startup_intent[
            "runtime_bootstrap_transaction_id"
        ],
        "installed_probe_tree_content_hash": startup_intent[
            "installed_probe_tree_content_hash"
        ],
        "kplayerprefs_sha256": sha256_bytes(desired["kplayerprefs.yaml"]),
        "mods_json_sha256": sha256_bytes(desired["mods/mods.json"]),
        "loader_clone_only": True,
        "loader_policy": expected_loader_policy,
        "enabled_static_ids": [PROBE_STATIC_ID],
        "all_other_mods_disabled": True,
        "historical_validation": True,
        "current_runtime_configs_required": False,
        "expected_release_dll_sha256": roots.expected_release_dll_sha256,
        "bootstrap_raw_sha256": evidence_runtime["bootstrap"]["raw_sha256"],
        "initial_policy_raw_sha256": evidence_runtime["initial_disabled_policy"][
            "raw_sha256"
        ],
    }
    result["report_hash"] = sha256_bytes(canonical_bytes(result))
    if active_parent_finalize_lock is not None:
        invoke_test_hook("before_parent_finalize_lock_return_check", ctx)
        require_current_active_parent_finalize_lock(
            roots,
            active_parent_finalize_lock,
        )
    return result


def add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--oni-root", required=True)
    parser.add_argument("--evidence-root", required=True)
    parser.add_argument("--boundary-report-root", required=True)
    parser.add_argument("--environment-manifest", required=True)
    parser.add_argument("--expected-environment-manifest-sha256", required=True)
    parser.add_argument("--runtime-bootstrap-result", required=True)
    parser.add_argument("--expected-runtime-bootstrap-result-sha256", required=True)
    parser.add_argument("--pre-boundary", required=True)
    parser.add_argument("--clone-manifest", required=True)
    parser.add_argument("--source-save", required=True)
    parser.add_argument("--loader-save", required=True)
    parser.add_argument("--loader-stem", required=True)
    parser.add_argument("--expected-mod-version", default=EXPECTED_MOD_VERSION)
    parser.add_argument("--release-dll", required=True)
    parser.add_argument("--expected-release-dll-sha256", required=True)
    parser.add_argument("--game-managed-dir")
    parser.add_argument("--test-root")
    parser.add_argument("--process-state-fixture")
    parser.add_argument("--confirm-live-run-id")
    parser.add_argument(
        "--test-managed-assembly-csharp-sha256",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--test-managed-assembly-csharp-firstpass-sha256",
        help=argparse.SUPPRESS,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    add_common_arguments(parser)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--what-if", action="store_true")
    parser.add_argument("--test-fail-after-replace", type=int, choices=(0, 1, 2), default=0, help=argparse.SUPPRESS)
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.test_fail_after_replace and not args.test_root:
            raise ConfigError("fault injection is permitted only with --test-root")
        result = configure(args)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except ConfigError as exc:
        print(
            json.dumps(
                {
                    "schema": ERROR_SCHEMA,
                    "status": "blocked",
                    "classification": "infra_fail",
                    "error": str(exc),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 2
    except (
        OSError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
    ):
        print(
            json.dumps(
                {
                    "schema": ERROR_SCHEMA,
                    "status": "blocked",
                    "classification": "infra_fail",
                    "error": "unexpected local I/O or data-shape failure",
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
