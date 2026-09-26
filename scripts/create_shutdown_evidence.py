#!/usr/bin/env python3
"""Create or verify one fail-closed, lineage-bound normal-shutdown proof.

The proof is deliberately offline and create-only.  It binds an immutable
startup certificate and its complete origin-session archive through the
activation/checkpoint save-lineage chain to the exact final session, whose log
must end with ``session_ended(application_quit) -> probe_shutdown``.  Merely
observing that the ONI and Bridge process counts are zero is never evidence.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import stat
import sys
import uuid
from contextlib import contextmanager
from typing import Any, BinaryIO, Callable, Dict, Iterable, Iterator, Mapping, Optional, Sequence


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.canonical_hash import CanonicalHashError, canonical_hash, loads_strict
from agent.state_quality import canonical_state_hash, parse_snapshot_timestamp
from scripts import create_startup_consumption_evidence as startup_contract
from scripts import manage_save_lineage as lineage_contract


EVIDENCE_SCHEMA = "oni.normal-shutdown-evidence.v3"
LINEAGE_SCHEMA = "oni.test-save-lineage.v1"
PAIR_REPORT_SCHEMA = "oni.priority-pair-lineage-finalization.v1"
BUNDLE_SCHEMA = "oni.confirmed-command-approval-bundle.v1"
FREEZE_SCHEMA = "oni.priority-command-lineage-freeze.v1"
ACTION = "set_duplicant_chore_priority"
STARTUP_BUNDLE_DIRECTORY = "startup-consumption-evidence"
HASH_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
COMMAND_PATTERN = re.compile(r"^cmd-[A-Za-z0-9_.-]+$")
STEP_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,79}$")
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
MAX_JSON_BYTES = 32 * 1024 * 1024
MAX_LOG_BYTES = 512 * 1024 * 1024
MAX_LOG_LINE_BYTES = 8 * 1024 * 1024
REPARSE_ATTRIBUTE = 0x400
WINDOWS_RESERVED_NAMES = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{value}" for value in range(1, 10)),
        *(f"lpt{value}" for value in range(1, 10)),
    }
)
FORBIDDEN_EVENTS = frozenset(
    {
        "crash",
        "forced_shutdown",
        "probe_crash",
        "probe_error",
        "process_crash",
        "process_killed",
    }
)
# A final validation lineage is established by the completed loader callback.
# The broader game lifecycle callback can run before the requested save has
# finished loading, so it is not a sufficient start anchor for this gate.
TRUSTED_SESSION_START_REASONS = frozenset({"save_loader_load_complete"})
PAIR_REPORT_KEYS = frozenset(
    {
        "schema",
        "status",
        "classification",
        "verification_mode",
        "fail_closed",
        "manifest",
        "pair",
        "final_tip",
        "shutdown",
        "report_hash",
    }
)
EVIDENCE_KEYS = frozenset(
    {
        "schema",
        "status",
        "classification",
        "verification_mode",
        "fail_closed",
        "lineage",
        "priority_pair_report",
        "startup_consumption_certificate",
        "final_tip",
        "pair",
        "source",
        "termination",
        "evidence_hash",
    }
)
STARTUP_REPLAY_KEYS = frozenset(
    {
        "schema",
        "status",
        "classification",
        "verification_mode",
        "fail_closed",
        "verified_evidence_hash",
        "validation_run_id",
        "session_id",
        "artifact_count",
        "evidence",
    }
)
STARTUP_REPORT_KEYS = frozenset(
    {
        "schema",
        "status",
        "classification",
        "verification_mode",
        "fail_closed",
        "validation_run_id",
        "startup_intent_hash",
        "runtime_config_evidence_sha256",
        "startup_binding_sha256",
        "process",
        "session",
        "loader",
        "consumed_prefs",
        "player_log",
        "source_archive",
        "artifacts",
        "created_at",
        "evidence_hash",
    }
)
STARTUP_SESSION_KEYS = frozenset(
    {"session_id", "initial_sequence", "record_count", "state_snapshot_count", "start_reason"}
)
STARTUP_LOADER_KEYS = frozenset(
    {
        "path_id_upper",
        "initial_save_sha256",
        "initial_save_length",
        "expected_game_build",
        "expected_mod_version",
        "expected_probe_dll_sha256",
    }
)
STARTUP_CONSUMED_PREFS_KEYS = frozenset(
    {"save_filename_key_count", "auto_resume_key_count", "save_filename_line", "value_path_id_upper"}
)
STARTUP_PLAYER_LOG_KEYS = frozenset(
    {
        "loading_line",
        "loaded_line",
        "verified_marker_line",
        "loading_line_sha256",
        "loaded_line_sha256",
        "verified_marker_line_sha256",
    }
)
STARTUP_ARTIFACT_KEYS = frozenset({"file", "sha256", "length", "source_identity_id"})
STARTUP_CERTIFICATE_KEYS = frozenset(
    {
        "schema",
        "status",
        "evidence_hash",
        "bundle",
        "replay",
        "validation_run_id",
        "binding",
        "process",
        "session",
        "source_archive",
        "loader",
    }
)
STARTUP_CERTIFICATE_BUNDLE_KEYS = frozenset(
    {
        "directory_name",
        "path_id_lower",
        "report_file",
        "report_file_sha256",
        "entry_count",
        "artifact_count",
    }
)
STARTUP_CERTIFICATE_REPLAY_KEYS = frozenset(
    {"schema", "status", "verification_mode", "fail_closed"}
)
STARTUP_CERTIFICATE_BINDING_KEYS = frozenset(
    {
        "startup_binding_sha256",
        "startup_intent_hash",
        "runtime_config_evidence_sha256",
        "startup_binding_status",
        "requested_save_path_id",
        "active_save_path_id",
    }
)
STARTUP_CERTIFICATE_SESSION_KEYS = frozenset(
    {
        "session_id",
        "initial_sequence",
        "start_reason",
        "initial_save_sha256",
        "prefix_record_count",
        "prefix_file_sha256",
        "prefix_byte_length",
    }
)
STARTUP_CERTIFICATE_LOADER_KEYS = frozenset(
    {
        "initial_save_length",
        "expected_game_build",
        "expected_mod_version",
        "expected_probe_dll_sha256",
    }
)
LINEAGE_EVENT_EVIDENCE_KEYS = frozenset(
    {
        "session_id",
        "seq",
        "ts",
        "success",
        "is_auto_save",
        "update_save_pointer",
        "file_rewrite_observed",
        "save_file_readable",
        "requested_save_path_id",
        "result_save_path_id",
        "active_save_path_id",
        "before_save_id_hash",
        "written_save_id_hash",
        "active_save_id_hash",
        "save_id_hash",
        "save_dirty",
    }
)
LINEAGE_SNAPSHOT_KEYS = frozenset(
    {
        "session_id",
        "snapshot_id",
        "seq",
        "save_id_hash",
        "world_id",
        "mod_version",
        "snapshot_ts",
    }
)


class ShutdownEvidenceError(RuntimeError):
    """Stable fail-closed shutdown-evidence error."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def require(condition: bool, code: str, detail: str = "") -> None:
    if not condition:
        raise ShutdownEvidenceError(code, detail)


def normalized(path: pathlib.Path | str) -> pathlib.Path:
    return pathlib.Path(os.path.abspath(os.path.normpath(str(pathlib.Path(path).expanduser()))))


def _path_text(path: pathlib.Path | str) -> str:
    return os.path.normcase(str(normalized(path)).rstrip("\\/"))


def same_path(left: pathlib.Path | str, right: pathlib.Path | str) -> bool:
    return _path_text(left) == _path_text(right)


def path_within(path: pathlib.Path | str, root: pathlib.Path | str, *, strict: bool = True) -> bool:
    candidate = _path_text(path)
    boundary = _path_text(root)
    try:
        common = os.path.commonpath((candidate, boundary))
    except ValueError:
        return False
    return common == boundary and (not strict or candidate != boundary)


def _is_reparse(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & REPARSE_ATTRIBUTE
    )


def _regular_file(path: pathlib.Path, label: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ShutdownEvidenceError("evidence_file_unreadable", label) from exc
    require(stat.S_ISREG(metadata.st_mode) and not _is_reparse(metadata), "evidence_file_not_regular", label)
    require(metadata.st_nlink == 1, "evidence_file_hardlinked", label)
    return metadata


def _assert_no_reparse_path(
    path: pathlib.Path,
    boundary: pathlib.Path,
    *,
    allow_missing_leaf: bool = False,
) -> None:
    path = normalized(path)
    boundary = normalized(boundary)
    require(path_within(path, boundary, strict=False), "evidence_path_outside_trust_root")
    try:
        relative = pathlib.Path(os.path.relpath(path, boundary))
    except ValueError as exc:
        raise ShutdownEvidenceError("evidence_path_outside_trust_root") from exc
    current = boundary
    components = (boundary, *(boundary / pathlib.Path(*relative.parts[:index]) for index in range(1, len(relative.parts) + 1)))
    for index, candidate in enumerate(components):
        current = candidate
        if allow_missing_leaf and index == len(components) - 1 and not os.path.lexists(current):
            return
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise ShutdownEvidenceError("evidence_path_component_missing", str(current)) from exc
        require(not _is_reparse(metadata), "evidence_path_contains_reparse", str(current))


def _file_version(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        stat.S_IFMT(metadata.st_mode),
        int(metadata.st_nlink),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
    )


@contextmanager
def _stable_binary_handle(
    path: pathlib.Path,
    label: str,
    *,
    maximum: int,
    minimum: int = 0,
    before_open_hook: Optional[Callable[[], None]] = None,
    after_open_hook: Optional[Callable[[], None]] = None,
) -> Iterator[tuple[BinaryIO, os.stat_result]]:
    """Pin one pathname to one regular, single-link descriptor for a full read."""

    path = normalized(path)
    before = _regular_file(path, label)
    require(minimum <= before.st_size <= maximum, "evidence_file_size_invalid", label)
    if before_open_hook is not None:
        before_open_hook()
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ShutdownEvidenceError("evidence_file_unreadable", label) from exc
    handle: Optional[BinaryIO] = None
    try:
        handle = os.fdopen(descriptor, "rb", closefd=True)
        opened = os.fstat(handle.fileno())
        require(
            stat.S_ISREG(opened.st_mode)
            and opened.st_nlink == 1
            and _file_version(opened) == _file_version(before),
            "evidence_file_name_swapped",
            label,
        )
        if after_open_hook is not None:
            after_open_hook()
        yield handle, before
        after_handle = os.fstat(handle.fileno())
        after_path = _regular_file(path, label)
        require(
            _file_version(after_handle) == _file_version(opened)
            and _file_version(after_path) == _file_version(opened),
            "evidence_file_changed_or_name_swapped",
            label,
        )
    finally:
        if handle is not None:
            handle.close()
        else:
            os.close(descriptor)


def _stable_file_custody_hash(
    path: pathlib.Path,
    label: str,
    *,
    maximum: int = MAX_JSON_BYTES,
    before_open_hook: Optional[Callable[[], None]] = None,
    after_open_hook: Optional[Callable[[], None]] = None,
) -> tuple[str, int, tuple[int, int, int, int, int, int]]:
    digest = hashlib.sha256()
    with _stable_binary_handle(
        path,
        label,
        maximum=maximum,
        before_open_hook=before_open_hook,
        after_open_hook=after_open_hook,
    ) as (handle, metadata):
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest(), metadata.st_size, _file_version(metadata)


def _stable_file_hash(
    path: pathlib.Path,
    label: str,
    *,
    maximum: int = MAX_JSON_BYTES,
    before_open_hook: Optional[Callable[[], None]] = None,
    after_open_hook: Optional[Callable[[], None]] = None,
) -> tuple[str, int]:
    file_hash, byte_length, _version = _stable_file_custody_hash(
        path,
        label,
        maximum=maximum,
        before_open_hook=before_open_hook,
        after_open_hook=after_open_hook,
    )
    return file_hash, byte_length


def sha256_file(path: pathlib.Path, label: str = "evidence") -> str:
    return _stable_file_hash(normalized(path), label)[0]


def _strict_object_snapshot(
    path: pathlib.Path,
    label: str,
    *,
    before_open_hook: Optional[Callable[[], None]] = None,
    after_open_hook: Optional[Callable[[], None]] = None,
) -> tuple[Dict[str, Any], str, int]:
    path = normalized(path)
    try:
        with _stable_binary_handle(
            path,
            label,
            maximum=MAX_JSON_BYTES,
            before_open_hook=before_open_hook,
            after_open_hook=after_open_hook,
        ) as (handle, metadata):
            payload = handle.read()
        value = loads_strict(payload.decode("utf-8-sig", errors="strict"))
    except (OSError, UnicodeError, CanonicalHashError, TypeError, ValueError) as exc:
        raise ShutdownEvidenceError("evidence_json_invalid", label) from exc
    require(isinstance(value, dict), "evidence_json_not_object", label)
    return dict(value), "sha256:" + hashlib.sha256(payload).hexdigest(), metadata.st_size


def _strict_object_custody_snapshot(
    path: pathlib.Path,
    label: str,
) -> tuple[Dict[str, Any], str, int, tuple[int, int, int, int, int, int]]:
    payload, file_hash, byte_length, version = _stable_payload_snapshot(
        normalized(path), label, maximum=MAX_JSON_BYTES
    )
    try:
        value = loads_strict(payload.decode("utf-8-sig", errors="strict"))
    except (UnicodeError, CanonicalHashError, TypeError, ValueError) as exc:
        raise ShutdownEvidenceError("evidence_json_invalid", label) from exc
    require(isinstance(value, dict), "evidence_json_not_object", label)
    return dict(value), file_hash, byte_length, version


def _strict_object(path: pathlib.Path, label: str) -> Dict[str, Any]:
    return _strict_object_snapshot(path, label)[0]


def _require_hash(value: Any, code: str) -> str:
    require(isinstance(value, str) and HASH_PATTERN.fullmatch(value) is not None, code)
    return str(value)


def _hash_without(document: Mapping[str, Any], field: str) -> str:
    value = copy.deepcopy(dict(document))
    value.pop(field, None)
    try:
        return canonical_hash(value)
    except (CanonicalHashError, TypeError, ValueError, OverflowError) as exc:
        raise ShutdownEvidenceError("canonical_document_invalid", field) from exc


def _canonical_equal(left: Any, right: Any) -> bool:
    try:
        return canonical_hash(left) == canonical_hash(right)
    except (CanonicalHashError, TypeError, ValueError, OverflowError):
        return False


def _path_id_lower(path: pathlib.Path | str) -> str:
    text = str(normalized(path)).rstrip("\\/").lower()
    return "sha256:" + hashlib.sha256(text.encode("utf-8", errors="strict")).hexdigest()


def _directory_identity(path: pathlib.Path, label: str) -> tuple[int, int, int]:
    try:
        metadata = normalized(path).lstat()
    except OSError as exc:
        raise ShutdownEvidenceError("evidence_directory_unreadable", label) from exc
    require(
        stat.S_ISDIR(metadata.st_mode) and not _is_reparse(metadata),
        "evidence_directory_invalid",
        label,
    )
    return (int(metadata.st_dev), int(metadata.st_ino), stat.S_IFMT(metadata.st_mode))


def _stable_payload_snapshot(
    path: pathlib.Path,
    label: str,
    *,
    maximum: int,
) -> tuple[bytes, str, int, tuple[int, int, int, int, int, int]]:
    digest = hashlib.sha256()
    chunks: list[bytes] = []
    with _stable_binary_handle(path, label, maximum=maximum, minimum=1) as (handle, metadata):
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            chunks.append(chunk)
    payload = b"".join(chunks)
    return payload, "sha256:" + digest.hexdigest(), len(payload), _file_version(metadata)


def _exact_object(value: Any, keys: frozenset[str], code: str) -> Dict[str, Any]:
    require(isinstance(value, Mapping) and set(value) == keys, code)
    return dict(value)


def _validate_startup_bundle(
    *,
    bundle_dir: pathlib.Path,
    expected_evidence_hash: str,
    expected_process_id: int,
    manifest_path: pathlib.Path,
    bridge_dir: pathlib.Path,
    expected_run_id: str,
    lineage_origin: Mapping[str, Any],
) -> Dict[str, Any]:
    """Replay one immutable startup certificate and return a path-safe projection."""

    bundle_dir = normalized(bundle_dir)
    manifest_path = normalized(manifest_path)
    bridge_dir = normalized(bridge_dir)
    expected_bundle = manifest_path.parent / STARTUP_BUNDLE_DIRECTORY
    require(same_path(bundle_dir, expected_bundle), "startup_bundle_path_not_exact")
    require(path_within(bundle_dir, bridge_dir), "startup_bundle_outside_bridge")
    _assert_no_reparse_path(bundle_dir, bridge_dir)
    require(
        isinstance(expected_process_id, int)
        and not isinstance(expected_process_id, bool)
        and expected_process_id > 0,
        "expected_validation_process_id_invalid",
    )
    expected_hash = _require_hash(
        expected_evidence_hash, "expected_startup_consumption_evidence_hash_invalid"
    )

    directory_identity_before = _directory_identity(bundle_dir, "startup bundle")
    expected_names = set(startup_contract.ARTIFACT_FILES.values()) | {
        startup_contract.REPORT_FILE
    }
    try:
        actual_names = {entry.name for entry in bundle_dir.iterdir()}
    except OSError as exc:
        raise ShutdownEvidenceError("startup_bundle_unreadable") from exc
    require(actual_names == expected_names, "startup_bundle_entries_invalid")

    try:
        replay = startup_contract.verify_command(argparse.Namespace(bundle_dir=str(bundle_dir)))
    except startup_contract.StartupEvidenceError as exc:
        raise ShutdownEvidenceError("startup_bundle_replay_failed", exc.code) from exc
    except Exception as exc:
        raise ShutdownEvidenceError("startup_bundle_replay_failed", "internal_error") from exc

    replay = _exact_object(replay, STARTUP_REPLAY_KEYS, "startup_replay_fields_invalid")
    require(
        replay.get("schema") == startup_contract.REPLAY_VERIFICATION_SCHEMA
        and replay.get("status") == "passed"
        and replay.get("classification") == "startup_consumption_verified"
        and replay.get("verification_mode") == "offline_replay"
        and replay.get("fail_closed") is True,
        "startup_replay_status_invalid",
    )
    report = _exact_object(
        replay.get("evidence"), STARTUP_REPORT_KEYS, "startup_report_fields_invalid"
    )
    evidence_hash = _require_hash(report.get("evidence_hash"), "startup_evidence_hash_invalid")
    require(
        evidence_hash == expected_hash
        and replay.get("verified_evidence_hash") == expected_hash,
        "startup_evidence_expected_hash_mismatch",
    )
    require(
        report.get("schema") == startup_contract.EVIDENCE_SCHEMA
        and report.get("status") == "passed"
        and report.get("classification") == "startup_consumption_verified"
        and report.get("verification_mode") == "live_create"
        and report.get("fail_closed") is True,
        "startup_report_status_invalid",
    )
    require(
        report.get("validation_run_id") == expected_run_id
        and replay.get("validation_run_id") == expected_run_id,
        "startup_validation_run_lineage_mismatch",
    )

    process = _exact_object(
        report.get("process"),
        startup_contract.PROCESS_EVIDENCE_KEYS,
        "startup_process_fields_invalid",
    )
    require(
        process.get("process_id") == expected_process_id,
        "startup_validation_process_id_mismatch",
    )
    for field in {"creation_token", "image_path_id_upper", "image_file_sha256"}:
        _require_hash(process.get(field), f"startup_process_{field}_invalid")
    require(
        isinstance(process.get("image_name"), str) and bool(process.get("image_name")),
        "startup_process_image_name_invalid",
    )

    session = _exact_object(
        report.get("session"), STARTUP_SESSION_KEYS, "startup_session_fields_invalid"
    )
    require(
        session.get("session_id") == lineage_origin.get("session_id")
        and replay.get("session_id") == lineage_origin.get("session_id"),
        "startup_session_lineage_mismatch",
    )
    require(
        session.get("initial_sequence") == 1
        and session.get("start_reason") == "save_loader_load_complete"
        and isinstance(session.get("record_count"), int)
        and not isinstance(session.get("record_count"), bool)
        and session.get("record_count") >= 3
        and isinstance(session.get("state_snapshot_count"), int)
        and not isinstance(session.get("state_snapshot_count"), bool)
        and session.get("state_snapshot_count") >= 1,
        "startup_session_contract_invalid",
    )
    source_archive = _exact_object(
        report.get("source_archive"),
        startup_contract.SOURCE_ARCHIVE_KEYS,
        "startup_source_archive_fields_invalid",
    )
    for field in {
        "source_identity_id",
        "file_sha256",
        "record_chain_hash",
        "prefix_file_sha256",
    }:
        _require_hash(source_archive.get(field), f"startup_source_archive_{field}_invalid")
    require(
        source_archive.get("session_id") == lineage_origin.get("session_id")
        and source_archive.get("first_seq") == 1
        and isinstance(source_archive.get("record_count"), int)
        and not isinstance(source_archive.get("record_count"), bool)
        and source_archive.get("record_count") > session.get("record_count")
        and source_archive.get("last_seq") == source_archive.get("record_count")
        and isinstance(source_archive.get("byte_length"), int)
        and not isinstance(source_archive.get("byte_length"), bool)
        and source_archive.get("byte_length") > 0,
        "startup_source_archive_contract_invalid",
    )

    loader = _exact_object(
        report.get("loader"), STARTUP_LOADER_KEYS, "startup_loader_fields_invalid"
    )
    for field in {"path_id_upper", "initial_save_sha256", "expected_probe_dll_sha256"}:
        _require_hash(loader.get(field), f"startup_loader_{field}_invalid")
    require(
        isinstance(loader.get("initial_save_length"), int)
        and not isinstance(loader.get("initial_save_length"), bool)
        and loader.get("initial_save_length") >= 0
        and isinstance(loader.get("expected_game_build"), str)
        and bool(loader.get("expected_game_build"))
        and isinstance(loader.get("expected_mod_version"), str)
        and bool(loader.get("expected_mod_version")),
        "startup_loader_contract_invalid",
    )
    require(
        loader.get("initial_save_sha256") == lineage_origin.get("save_id_hash"),
        "startup_initial_save_origin_mismatch",
    )
    require(
        loader.get("expected_mod_version") == lineage_origin.get("mod_version"),
        "startup_mod_version_origin_mismatch",
    )

    consumed_prefs = _exact_object(
        report.get("consumed_prefs"),
        STARTUP_CONSUMED_PREFS_KEYS,
        "startup_consumed_prefs_fields_invalid",
    )
    require(
        consumed_prefs.get("save_filename_key_count") == 1
        and consumed_prefs.get("auto_resume_key_count") == 0
        and consumed_prefs.get("value_path_id_upper") == loader.get("path_id_upper"),
        "startup_consumed_prefs_contract_invalid",
    )
    player_log = _exact_object(
        report.get("player_log"), STARTUP_PLAYER_LOG_KEYS, "startup_player_log_fields_invalid"
    )
    for field in {
        "loading_line_sha256",
        "loaded_line_sha256",
        "verified_marker_line_sha256",
    }:
        _require_hash(player_log.get(field), f"startup_player_log_{field}_invalid")

    artifacts = report.get("artifacts")
    require(
        isinstance(artifacts, Mapping)
        and set(artifacts) == set(startup_contract.ARTIFACT_FILES),
        "startup_artifacts_fields_invalid",
    )
    file_versions: Dict[str, tuple[int, int, int, int, int, int]] = {}
    report_path = bundle_dir / startup_contract.REPORT_FILE
    report_document, report_file_sha256, _, report_version = (
        _strict_object_custody_snapshot(report_path, "startup report")
    )
    require(_canonical_equal(report_document, report), "startup_report_snapshot_mismatch")
    file_versions[startup_contract.REPORT_FILE] = report_version

    archive_payload = b""
    archive_file_sha256 = ""
    archive_length = 0
    for artifact_key, file_name in startup_contract.ARTIFACT_FILES.items():
        record = _exact_object(
            artifacts.get(artifact_key),
            STARTUP_ARTIFACT_KEYS,
            "startup_artifact_record_fields_invalid",
        )
        require(record.get("file") == file_name, "startup_artifact_file_invalid", artifact_key)
        maximum = {
            "runtime_config_evidence": startup_contract.MAX_JSON_BYTES,
            "startup_binding": 64 * 1024,
            "consumed_prefs": startup_contract.MAX_PREFS_BYTES,
            "player_log_prefix": startup_contract.MAX_PLAYER_LOG_BYTES,
            "session_archive_prefix": startup_contract.MAX_ARCHIVE_BYTES,
        }[artifact_key]
        payload, file_hash, byte_length, version = _stable_payload_snapshot(
            bundle_dir / file_name,
            f"startup artifact {artifact_key}",
            maximum=maximum,
        )
        require(
            record.get("sha256") == file_hash
            and record.get("length") == byte_length,
            "startup_artifact_snapshot_mismatch",
            artifact_key,
        )
        _require_hash(record.get("source_identity_id"), "startup_artifact_source_identity_invalid")
        file_versions[file_name] = version
        if artifact_key == "session_archive_prefix":
            archive_payload = payload
            archive_file_sha256 = file_hash
            archive_length = byte_length

    try:
        prefix_records = startup_contract.parse_archive_records(archive_payload)
    except startup_contract.StartupEvidenceError as exc:
        raise ShutdownEvidenceError("startup_archive_prefix_invalid", exc.code) from exc
    require(
        len(prefix_records) == session.get("record_count"),
        "startup_archive_prefix_record_count_mismatch",
    )
    require(
        source_archive.get("prefix_record_count") == len(prefix_records)
        and source_archive.get("prefix_file_sha256") == archive_file_sha256
        and source_archive.get("prefix_byte_length") == archive_length
        and source_archive.get("byte_length") > archive_length,
        "startup_source_archive_prefix_mismatch",
    )
    directory_identity_after = _directory_identity(bundle_dir, "startup bundle")
    require(
        directory_identity_after == directory_identity_before,
        "startup_bundle_directory_changed",
    )

    reservation_matches = [
        record
        for record in prefix_records
        if record.get("event") == "state_snapshot"
        and record.get("snapshot_id") == lineage_origin.get("snapshot_id")
        and record.get("seq") == lineage_origin.get("seq")
        and record.get("session_id") == lineage_origin.get("session_id")
    ]
    require(len(reservation_matches) == 1, "startup_origin_snapshot_missing_or_ambiguous")
    reservation_record = reservation_matches[0]
    require(
        reservation_record.get("save_id_hash") == lineage_origin.get("save_id_hash")
        and reservation_record.get("world_id") == lineage_origin.get("world_id")
        and reservation_record.get("mod_version") == lineage_origin.get("mod_version")
        and reservation_record.get("save_dirty") is False,
        "startup_origin_snapshot_identity_mismatch",
    )

    projection: Dict[str, Any] = {
        "schema": startup_contract.EVIDENCE_SCHEMA,
        "status": "passed",
        "evidence_hash": evidence_hash,
        "bundle": {
            "directory_name": STARTUP_BUNDLE_DIRECTORY,
            "path_id_lower": _path_id_lower(bundle_dir),
            "report_file": startup_contract.REPORT_FILE,
            "report_file_sha256": report_file_sha256,
            "entry_count": len(expected_names),
            "artifact_count": len(startup_contract.ARTIFACT_FILES),
        },
        "replay": {
            "schema": startup_contract.REPLAY_VERIFICATION_SCHEMA,
            "status": "passed",
            "verification_mode": "offline_replay",
            "fail_closed": True,
        },
        "validation_run_id": expected_run_id,
        "binding": {
            "startup_binding_sha256": report.get("startup_binding_sha256"),
            "startup_intent_hash": report.get("startup_intent_hash"),
            "runtime_config_evidence_sha256": report.get("runtime_config_evidence_sha256"),
            "startup_binding_status": "verified",
            "requested_save_path_id": loader.get("path_id_upper"),
            "active_save_path_id": loader.get("path_id_upper"),
        },
        "process": process,
        "session": {
            "session_id": lineage_origin.get("session_id"),
            "initial_sequence": 1,
            "start_reason": "save_loader_load_complete",
            "initial_save_sha256": loader.get("initial_save_sha256"),
            "prefix_record_count": len(prefix_records),
            "prefix_file_sha256": archive_file_sha256,
            "prefix_byte_length": archive_length,
        },
        "source_archive": source_archive,
        "loader": {
            "initial_save_length": loader.get("initial_save_length"),
            "expected_game_build": loader.get("expected_game_build"),
            "expected_mod_version": loader.get("expected_mod_version"),
            "expected_probe_dll_sha256": loader.get("expected_probe_dll_sha256"),
        },
    }
    require(set(projection) == STARTUP_CERTIFICATE_KEYS, "startup_certificate_projection_invalid")
    require(
        set(projection["bundle"]) == STARTUP_CERTIFICATE_BUNDLE_KEYS
        and set(projection["replay"]) == STARTUP_CERTIFICATE_REPLAY_KEYS
        and set(projection["binding"]) == STARTUP_CERTIFICATE_BINDING_KEYS
        and set(projection["process"]) == startup_contract.PROCESS_EVIDENCE_KEYS
        and set(projection["session"]) == STARTUP_CERTIFICATE_SESSION_KEYS
        and set(projection["source_archive"]) == startup_contract.SOURCE_ARCHIVE_KEYS
        and set(projection["loader"]) == STARTUP_CERTIFICATE_LOADER_KEYS,
        "startup_certificate_projection_invalid",
    )
    return {
        "projection": projection,
        "custody": {
            "directory_identity": directory_identity_before,
            "file_versions": file_versions,
        },
        "prefix_payload": archive_payload,
        "prefix_record_hashes": [canonical_hash(record) for record in prefix_records],
    }


def _aware_time(value: Any, code: str) -> dt.datetime:
    parsed = parse_snapshot_timestamp(value)
    require(parsed is not None and parsed.utcoffset() is not None, code)
    assert parsed is not None
    return parsed


def safe_session_name(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", value)[:120]
    return text or "waiting"


def resolve_bridge_relative_path(
    value: Any,
    bridge_dir: pathlib.Path,
    label: str,
    *,
    allow_missing_leaf: bool = False,
) -> pathlib.Path:
    """Resolve the production bridge-relative POSIX path contract fail-closed."""

    require(isinstance(value, str) and bool(value), "bridge_relative_path_invalid", label)
    text = str(value)
    windows = pathlib.PureWindowsPath(text)
    relative = pathlib.PurePosixPath(text)
    require(
        "\\" not in text
        and not windows.is_absolute()
        and not windows.drive
        and not relative.is_absolute()
        and relative.as_posix() == text
        and all(part not in {"", ".", ".."} for part in relative.parts),
        "bridge_relative_path_invalid",
        label,
    )
    candidate = normalized(bridge_dir).joinpath(*relative.parts)
    require(path_within(candidate, bridge_dir), "bridge_relative_path_escape", label)
    _assert_no_reparse_path(
        candidate,
        normalized(bridge_dir),
        allow_missing_leaf=allow_missing_leaf,
    )
    return candidate


def _termination_projection(record: Mapping[str, Any], line_number: int) -> Dict[str, Any]:
    return {
        "schema": record.get("schema"),
        "event": record.get("event"),
        "line_number": line_number,
        "seq": record.get("seq"),
        "ts": record.get("ts"),
        "reason": record.get("reason"),
        "session_id": record.get("session_id"),
        "save_id_hash": record.get("save_id_hash"),
        "world_id": record.get("world_id"),
        "save_dirty": record.get("save_dirty"),
        "game_build": record.get("game_build"),
        "mod_version": record.get("mod_version"),
        "record_hash": canonical_hash(record),
    }


def _startup_record_contract(startup_certificate: Mapping[str, Any]) -> Dict[str, Any]:
    """Return process/run anchors that remain sticky across every session."""
    binding = (
        startup_certificate.get("binding")
        if isinstance(startup_certificate.get("binding"), Mapping)
        else {}
    )
    process = (
        startup_certificate.get("process")
        if isinstance(startup_certificate.get("process"), Mapping)
        else {}
    )
    loader = (
        startup_certificate.get("loader")
        if isinstance(startup_certificate.get("loader"), Mapping)
        else {}
    )
    return {
        "validation_run_id": startup_certificate.get("validation_run_id"),
        "startup_binding_hash": binding.get("startup_binding_sha256"),
        "startup_intent_hash": binding.get("startup_intent_hash"),
        "runtime_config_evidence_sha256": binding.get(
            "runtime_config_evidence_sha256"
        ),
        "validation_process_id": process.get("process_id"),
        "startup_binding_status": "verified",
        "game_build": loader.get("expected_game_build"),
        "mod_version": loader.get("expected_mod_version"),
    }


def _read_trusted_startup_session_archive(
    bridge_dir: pathlib.Path,
    origin: Mapping[str, Any],
    activation_save: Mapping[str, Any],
    startup_certificate: Mapping[str, Any],
    startup_prefix_payload: bytes,
    startup_prefix_hashes: Sequence[str],
) -> tuple[Dict[str, Any], tuple[int, int, int, int, int, int]]:
    """Bind the frozen startup prefix to the complete authoritative origin archive."""

    bridge_dir = normalized(bridge_dir)
    session_id = str(origin.get("session_id") or "")
    archive = bridge_dir / "sessions" / safe_session_name(session_id) / "state.jsonl"
    require(path_within(archive, bridge_dir), "startup_origin_archive_outside_bridge")
    _assert_no_reparse_path(archive, bridge_dir)
    digest = hashlib.sha256()
    raw_lines: list[bytes] = []
    records: list[Dict[str, Any]] = []
    with _stable_binary_handle(
        archive,
        "startup origin session archive",
        maximum=MAX_LOG_BYTES,
        minimum=1,
    ) as (handle, before):
        for line_number, raw in enumerate(handle, start=1):
            digest.update(raw)
            require(
                0 < len(raw) <= MAX_LOG_LINE_BYTES and raw.endswith(b"\n") and bool(raw.strip()),
                "startup_origin_archive_line_invalid",
                str(line_number),
            )
            try:
                value = loads_strict(raw.decode("utf-8-sig", errors="strict"))
            except (UnicodeError, CanonicalHashError, TypeError, ValueError) as exc:
                raise ShutdownEvidenceError(
                    "startup_origin_archive_record_invalid", str(line_number)
                ) from exc
            require(isinstance(value, dict), "startup_origin_archive_record_invalid", str(line_number))
            raw_lines.append(raw)
            records.append(dict(value))

    require(bool(records), "startup_origin_archive_empty")
    sequences = [record.get("seq") for record in records]
    require(
        sequences == list(range(1, len(records) + 1)),
        "startup_origin_archive_sequence_invalid",
    )
    record_hashes = [canonical_hash(record) for record in records]
    require(
        len(record_hashes) == len(set(record_hashes)),
        "startup_origin_archive_record_duplicate",
    )
    first = records[0]
    require(
        first.get("schema") == "oni.event.v2"
        and first.get("event") == "session_started"
        and first.get("reason") in TRUSTED_SESSION_START_REASONS
        and first.get("session_id") == session_id,
        "startup_origin_archive_start_invalid",
    )
    contract = _startup_record_contract(startup_certificate)
    binding = (
        startup_certificate.get("binding")
        if isinstance(startup_certificate.get("binding"), Mapping)
        else {}
    )
    loader_path_id = _require_hash(
        binding.get("requested_save_path_id"), "startup_loader_path_id_invalid"
    )
    current_active_path_id = loader_path_id
    current_save_hash = _require_hash(
        origin.get("save_id_hash"), "startup_origin_save_hash_invalid"
    )
    require(
        isinstance(activation_save, Mapping)
        and set(activation_save) == LINEAGE_EVENT_EVIDENCE_KEYS,
        "startup_activation_save_fields_invalid",
    )
    activation_save = dict(activation_save)
    activation_target_path_id = _require_hash(
        activation_save.get("requested_save_path_id"),
        "startup_activation_save_path_invalid",
    )
    require(
        activation_target_path_id != loader_path_id,
        "startup_activation_save_path_not_derivative",
    )
    origin_snapshot_time = _aware_time(
        origin.get("snapshot_ts"), "startup_origin_snapshot_timestamp_invalid"
    )
    previous_time: Optional[dt.datetime] = None
    pending_save_path_id: Optional[str] = None
    pending_before_save_hash: Optional[str] = None
    save_started: list[tuple[int, Dict[str, Any]]] = []
    save_completed: list[tuple[int, Dict[str, Any]]] = []
    session_ended: list[tuple[int, Dict[str, Any]]] = []
    prefix_count = len(startup_prefix_hashes)
    for index, record in enumerate(records):
        event = record.get("event")
        require(isinstance(event, str) and bool(event), "startup_origin_archive_event_invalid")
        require(event not in FORBIDDEN_EVENTS, "startup_origin_archive_forbidden_event", str(event))
        require(record.get("session_id") == session_id, "startup_origin_archive_session_mismatch")
        expected_schema = "oni.state.v2" if event == "state_snapshot" else "oni.event.v2"
        require(record.get("schema") == expected_schema, "startup_origin_archive_schema_invalid", str(event))
        current_time = _aware_time(record.get("ts"), "startup_origin_archive_timestamp_invalid")
        require(
            previous_time is None or current_time >= previous_time,
            "startup_origin_archive_timestamp_order_invalid",
        )
        previous_time = current_time
        for field, expected_value in contract.items():
            require(
                record.get(field) == expected_value,
                "startup_origin_archive_binding_mismatch",
                field,
            )
        require(
            record.get("world_id") == origin.get("world_id"),
            "startup_origin_archive_world_mismatch",
        )

        if index < prefix_count:
            require(
                record.get("requested_save_path_id") == loader_path_id
                and record.get("active_save_path_id") == loader_path_id
                and record.get("save_id_hash") == origin.get("save_id_hash")
                and record.get("save_dirty") is False
                and event not in {"save_write_started", "save_write_completed"},
                "startup_origin_archive_prefix_loader_mismatch",
                str(record.get("seq")),
            )
        elif event == "save_write_started":
            save_started.append((index, record))
            require(
                pending_save_path_id is None
                and record.get("seq") > origin.get("seq")
                and current_time > origin_snapshot_time
                and record.get("requested_save_path_id") == activation_target_path_id
                and record.get("active_save_path_id") == current_active_path_id
                and record.get("before_save_id_hash") == current_save_hash
                and record.get("save_id_hash") == current_save_hash
                and record.get("save_dirty") is True
                and record.get("is_auto_save") is False
                and record.get("update_save_pointer") is True,
                "startup_origin_archive_save_start_invalid",
            )
            pending_save_path_id = activation_target_path_id
            pending_before_save_hash = current_save_hash
        elif event == "save_write_completed":
            save_completed.append((index, record))
            observed_event = lineage_contract.event_evidence(record)
            require(
                pending_save_path_id == activation_target_path_id
                and pending_before_save_hash == current_save_hash
                and record.get("seq") > origin.get("seq")
                and current_time > origin_snapshot_time
                and _canonical_equal(observed_event, activation_save),
                "startup_origin_archive_activation_save_mismatch",
            )
            current_active_path_id = _require_hash(
                record.get("active_save_path_id"),
                "startup_origin_archive_active_path_invalid",
            )
            current_save_hash = _require_hash(
                record.get("save_id_hash"), "startup_origin_archive_save_hash_invalid"
            )
            pending_save_path_id = None
            pending_before_save_hash = None
        else:
            require(
                record.get("requested_save_path_id") == loader_path_id
                and record.get("active_save_path_id") == current_active_path_id
                and record.get("save_id_hash") == current_save_hash
                and record.get("save_dirty") is False,
                "startup_origin_archive_suffix_identity_mismatch",
                str(event),
            )
        if event == "session_ended":
            session_ended.append((index, record))

    require(
        len(startup_prefix_hashes) > 0
        and len(records) >= len(startup_prefix_hashes)
        and record_hashes[: len(startup_prefix_hashes)] == list(startup_prefix_hashes),
        "startup_origin_archive_prefix_mismatch",
    )
    require(
        isinstance(startup_prefix_payload, bytes)
        and bool(startup_prefix_payload)
        and b"".join(raw_lines[: len(startup_prefix_hashes)]) == startup_prefix_payload,
        "startup_origin_archive_prefix_bytes_mismatch",
    )
    origin_matches = [
        record
        for record in records
        if record.get("event") == "state_snapshot"
        and record.get("snapshot_id") == origin.get("snapshot_id")
        and record.get("seq") == origin.get("seq")
    ]
    require(len(origin_matches) == 1, "startup_origin_archive_snapshot_missing_or_ambiguous")
    origin_record = origin_matches[0]
    require(
        origin_record.get("save_id_hash") == origin.get("save_id_hash")
        and origin_record.get("world_id") == origin.get("world_id")
        and origin_record.get("mod_version") == origin.get("mod_version")
        and origin_record.get("save_dirty") is False,
        "startup_origin_archive_snapshot_identity_mismatch",
    )
    require(
        len(save_started) == 1
        and len(save_completed) == 1
        and save_completed[0][0] == save_started[0][0] + 1
        and pending_save_path_id is None,
        "startup_origin_archive_activation_save_count_invalid",
    )
    require(
        len(session_ended) == 1
        and session_ended[0][0] == len(records) - 1
        and session_ended[0][0] > save_completed[0][0]
        and session_ended[0][1].get("reason") == "save_loader_load_start",
        "startup_origin_archive_session_end_invalid",
    )
    source_archive = _exact_object(
        startup_certificate.get("source_archive"),
        startup_contract.SOURCE_ARCHIVE_KEYS,
        "startup_source_archive_certificate_invalid",
    )
    observed_file_hash = "sha256:" + digest.hexdigest()
    observed_record_chain_hash = canonical_hash(record_hashes)
    require(
        source_archive.get("session_id") == session_id
        and source_archive.get("source_identity_id") == startup_contract.identity_id(before)
        and source_archive.get("file_sha256") == observed_file_hash
        and source_archive.get("byte_length") == before.st_size
        and source_archive.get("record_count") == len(records)
        and source_archive.get("first_seq") == sequences[0]
        and source_archive.get("last_seq") == sequences[-1]
        and source_archive.get("record_chain_hash") == observed_record_chain_hash
        and source_archive.get("prefix_record_count") == len(startup_prefix_hashes)
        and source_archive.get("prefix_file_sha256")
        == "sha256:" + hashlib.sha256(startup_prefix_payload).hexdigest()
        and source_archive.get("prefix_byte_length") == len(startup_prefix_payload),
        "startup_origin_archive_custody_mismatch",
    )
    relative = archive.relative_to(bridge_dir).as_posix()
    return (
        {
            "kind": "session_archive",
            "relative_path": relative,
            "path_id_lower": _path_id_lower(archive),
            "file_sha256": observed_file_hash,
            "byte_length": before.st_size,
            "record_count": len(records),
            "first_seq": sequences[0],
            "last_seq": sequences[-1],
            "record_chain_hash": observed_record_chain_hash,
            "prefix_record_count": len(startup_prefix_hashes),
            "prefix_raw_match": True,
            "prefix_canonical_match": True,
            "origin_snapshot_id": origin.get("snapshot_id"),
            "origin_snapshot_seq": origin.get("seq"),
            "activation_save_event": copy.deepcopy(activation_save),
            "activation_save_record_hash": canonical_hash(save_completed[0][1]),
            "session_end_seq": session_ended[0][1].get("seq"),
            "session_end_ts": session_ended[0][1].get("ts"),
            "trust_contract": "origin_startup_prefix_activation_archive_v2",
        },
        _file_version(before),
    )


def _read_trusted_reload_session_prefix(
    bridge_dir: pathlib.Path,
    reload_snapshot: Mapping[str, Any],
    startup_certificate: Mapping[str, Any],
    expected_active_save_path_id: str,
    after_timestamp: Any,
) -> tuple[Dict[str, Any], tuple[int, int, int, int, int, int]]:
    """Prove the next session loaded the derivative after the origin ended."""

    bridge_dir = normalized(bridge_dir)
    session_id = str(reload_snapshot.get("session_id") or "")
    archive = bridge_dir / "sessions" / safe_session_name(session_id) / "state.jsonl"
    require(path_within(archive, bridge_dir), "activation_reload_archive_outside_bridge")
    _assert_no_reparse_path(archive, bridge_dir)
    digest = hashlib.sha256()
    records: list[Dict[str, Any]] = []
    with _stable_binary_handle(
        archive,
        "activation reload session archive",
        maximum=MAX_LOG_BYTES,
        minimum=1,
    ) as (handle, before):
        for line_number, raw in enumerate(handle, start=1):
            digest.update(raw)
            require(
                0 < len(raw) <= MAX_LOG_LINE_BYTES and raw.endswith(b"\n") and bool(raw.strip()),
                "activation_reload_archive_line_invalid",
                str(line_number),
            )
            try:
                value = loads_strict(raw.decode("utf-8-sig", errors="strict"))
            except (UnicodeError, CanonicalHashError, TypeError, ValueError) as exc:
                raise ShutdownEvidenceError(
                    "activation_reload_archive_record_invalid", str(line_number)
                ) from exc
            require(
                isinstance(value, dict),
                "activation_reload_archive_record_invalid",
                str(line_number),
            )
            records.append(dict(value))

    require(len(records) >= 4, "activation_reload_archive_too_short")
    sequences = [record.get("seq") for record in records]
    require(
        sequences == list(range(1, len(records) + 1)),
        "activation_reload_archive_sequence_invalid",
    )
    record_hashes = [canonical_hash(record) for record in records]
    require(
        len(record_hashes) == len(set(record_hashes)),
        "activation_reload_archive_record_duplicate",
    )
    first = records[0]
    require(
        first.get("schema") == "oni.event.v2"
        and first.get("event") == "session_started"
        and first.get("reason") in TRUSTED_SESSION_START_REASONS
        and first.get("session_id") == session_id
        and records[1].get("schema") == "oni.event.v2"
        and records[1].get("event") == "save_loaded",
        "activation_reload_archive_start_invalid",
    )
    sticky_contract = _startup_record_contract(startup_certificate)
    binding = (
        startup_certificate.get("binding")
        if isinstance(startup_certificate.get("binding"), Mapping)
        else {}
    )
    requested_loader_path_id = _require_hash(
        binding.get("requested_save_path_id"), "startup_loader_path_id_invalid"
    )
    expected_active_save_path_id = _require_hash(
        expected_active_save_path_id, "activation_reload_path_id_invalid"
    )
    expected_save_hash = _require_hash(
        reload_snapshot.get("save_id_hash"), "activation_reload_save_hash_invalid"
    )
    after_time = _aware_time(after_timestamp, "origin_session_end_timestamp_invalid")
    previous_time: Optional[dt.datetime] = None
    target_matches: list[tuple[int, Dict[str, Any]]] = []
    ended: list[tuple[int, Dict[str, Any]]] = []
    for index, record in enumerate(records):
        event = record.get("event")
        require(
            isinstance(event, str) and bool(event) and event not in FORBIDDEN_EVENTS,
            "activation_reload_archive_event_invalid",
        )
        require(
            record.get("session_id") == session_id,
            "activation_reload_archive_session_mismatch",
        )
        expected_schema = "oni.state.v2" if event == "state_snapshot" else "oni.event.v2"
        require(
            record.get("schema") == expected_schema,
            "activation_reload_archive_schema_invalid",
            str(event),
        )
        current_time = _aware_time(
            record.get("ts"), "activation_reload_archive_timestamp_invalid"
        )
        require(
            previous_time is None or current_time >= previous_time,
            "activation_reload_archive_timestamp_order_invalid",
        )
        previous_time = current_time
        for field, expected_value in sticky_contract.items():
            require(
                record.get(field) == expected_value,
                "activation_reload_archive_binding_mismatch",
                field,
            )
        for field in {"requested_save_path_id", "active_save_path_id", "save_id_hash"}:
            _require_hash(record.get(field), f"activation_reload_archive_{field}_invalid")
        if (
            event == "state_snapshot"
            and record.get("snapshot_id") == reload_snapshot.get("snapshot_id")
            and record.get("seq") == reload_snapshot.get("seq")
        ):
            target_matches.append((index, record))
        if event == "session_ended":
            ended.append((index, record))

    require(
        _aware_time(first.get("ts"), "activation_reload_start_timestamp_invalid") > after_time,
        "activation_reload_did_not_follow_origin_end",
    )
    require(
        len(target_matches) == 1,
        "activation_reload_snapshot_missing_or_ambiguous",
    )
    target_index, target_record = target_matches[0]
    for record in records[: target_index + 1]:
        require(
            record.get("requested_save_path_id") == requested_loader_path_id
            and record.get("active_save_path_id") == expected_active_save_path_id
            and record.get("save_id_hash") == expected_save_hash
            and record.get("world_id") == reload_snapshot.get("world_id")
            and record.get("mod_version") == reload_snapshot.get("mod_version")
            and record.get("save_dirty") is False,
            "activation_reload_prefix_identity_mismatch",
            str(record.get("event")),
        )
    require(
        target_record.get("snapshot_id") == reload_snapshot.get("snapshot_id")
        and target_record.get("seq") == reload_snapshot.get("seq")
        and target_record.get("ts") == reload_snapshot.get("snapshot_ts"),
        "activation_reload_snapshot_projection_mismatch",
    )
    require(
        len(ended) == 1
        and ended[0][0] == len(records) - 1
        and ended[0][0] > target_index
        and ended[0][1].get("reason") == "save_loader_load_start",
        "activation_reload_archive_session_end_invalid",
    )
    return (
        {
            "kind": "session_archive",
            "relative_path": archive.relative_to(bridge_dir).as_posix(),
            "path_id_lower": _path_id_lower(archive),
            "file_sha256": "sha256:" + digest.hexdigest(),
            "byte_length": before.st_size,
            "record_count": len(records),
            "first_seq": sequences[0],
            "last_seq": sequences[-1],
            "record_chain_hash": canonical_hash(record_hashes),
            "reload_snapshot_id": reload_snapshot.get("snapshot_id"),
            "reload_snapshot_seq": reload_snapshot.get("seq"),
            "origin_end_before_start": True,
            "session_end_seq": ended[0][1].get("seq"),
            "session_end_ts": ended[0][1].get("ts"),
            "trust_contract": "activation_reload_prefix_archive_v1",
        },
        _file_version(before),
    )


def _read_trusted_session_log(
    event_log: pathlib.Path,
    bridge_dir: pathlib.Path,
    identity: Mapping[str, Any],
    startup_certificate: Mapping[str, Any],
    expected_active_save_path_id: str,
) -> tuple[
    Dict[str, Any],
    Dict[str, Any],
    Dict[str, str],
    tuple[int, int, int, int, int, int],
]:
    event_log = normalized(event_log)
    bridge_dir = normalized(bridge_dir)
    session_id = str(identity["session_id"])
    archive = bridge_dir / "sessions" / safe_session_name(session_id) / "state.jsonl"
    root_log = bridge_dir / "state.jsonl"
    if same_path(event_log, archive):
        source_kind = "session_archive"
    elif same_path(event_log, root_log):
        source_kind = "root_log"
    else:
        raise ShutdownEvidenceError("shutdown_log_path_not_authoritative", str(event_log))
    require(path_within(event_log, bridge_dir), "shutdown_log_outside_bridge")
    _assert_no_reparse_path(event_log, bridge_dir)

    digest = hashlib.sha256()
    target: list[tuple[int, Dict[str, Any]]] = []
    target_raw_lines: list[bytes] = []
    all_line_count = 0
    target_started = False
    target_finished = False
    last_nonempty_line = 0
    try:
        with _stable_binary_handle(
            event_log,
            "shutdown event log",
            maximum=MAX_LOG_BYTES,
            minimum=1,
        ) as (handle, before):
            for line_number, raw in enumerate(handle, start=1):
                digest.update(raw)
                all_line_count = line_number
                require(len(raw) <= MAX_LOG_LINE_BYTES, "shutdown_log_record_too_large", str(line_number))
                require(raw.endswith(b"\n"), "shutdown_log_incomplete_line", str(line_number))
                require(raw.strip(), "shutdown_log_blank_line", str(line_number))
                last_nonempty_line = line_number
                try:
                    value = loads_strict(raw.decode("utf-8-sig", errors="strict"))
                except (UnicodeError, CanonicalHashError, TypeError, ValueError) as exc:
                    raise ShutdownEvidenceError("shutdown_log_record_invalid", str(line_number)) from exc
                require(isinstance(value, dict), "shutdown_log_record_not_object", str(line_number))
                record = dict(value)
                is_target = record.get("session_id") == session_id
                if source_kind == "session_archive":
                    require(is_target, "session_archive_contains_wrong_session", str(line_number))
                if is_target:
                    require(not target_finished, "shutdown_record_after_probe_shutdown", str(line_number))
                    target_started = True
                    target.append((line_number, record))
                    target_raw_lines.append(raw)
                    if record.get("event") == "probe_shutdown":
                        target_finished = True
                elif source_kind == "root_log" and target_started:
                    raise ShutdownEvidenceError("root_log_interleaves_or_follows_target_session", str(line_number))
    except ShutdownEvidenceError:
        raise
    except OSError as exc:
        raise ShutdownEvidenceError("shutdown_log_unreadable") from exc
    require(target and target_finished, "shutdown_session_sequence_missing")
    if source_kind == "root_log":
        require(target[-1][0] == last_nonempty_line, "root_log_target_session_not_terminal")

    records = [record for _, record in target]
    line_numbers = [line_number for line_number, _ in target]
    sequences = [record.get("seq") for record in records]
    require(
        all(isinstance(value, int) and not isinstance(value, bool) and value > 0 for value in sequences),
        "shutdown_sequence_invalid",
    )
    require(sequences == list(range(1, len(records) + 1)), "shutdown_sequence_not_gap_free")
    record_hashes = [canonical_hash(record) for record in records]
    require(len(set(record_hashes)) == len(record_hashes), "shutdown_record_duplicate")

    startup_loader = (
        startup_certificate.get("loader")
        if isinstance(startup_certificate.get("loader"), Mapping)
        else {}
    )
    require(
        startup_certificate.get("validation_run_id") == identity.get("validation_run_id"),
        "startup_certificate_lineage_mismatch",
    )
    startup_record_fields = _startup_record_contract(startup_certificate)
    startup_binding = (
        startup_certificate.get("binding")
        if isinstance(startup_certificate.get("binding"), Mapping)
        else {}
    )
    requested_loader_path_id = _require_hash(
        startup_binding.get("requested_save_path_id"),
        "startup_loader_path_id_invalid",
    )
    expected_active_save_path_id = _require_hash(
        expected_active_save_path_id, "final_active_save_path_id_invalid"
    )

    previous_time: Optional[dt.datetime] = None
    for record in records:
        event = record.get("event")
        require(isinstance(event, str) and event, "shutdown_event_name_invalid")
        require(event not in FORBIDDEN_EVENTS, "shutdown_stream_contains_forbidden_event", str(event))
        require(record.get("session_id") == session_id, "shutdown_session_mismatch")
        current_time = _aware_time(record.get("ts"), "shutdown_event_timestamp_invalid")
        require(previous_time is None or current_time >= previous_time, "shutdown_event_timestamp_order_invalid")
        previous_time = current_time

    starts = [(index, item) for index, item in enumerate(records) if item.get("event") == "session_started"]
    ended = [(index, item) for index, item in enumerate(records) if item.get("event") == "session_ended"]
    stopped = [(index, item) for index, item in enumerate(records) if item.get("event") == "probe_shutdown"]
    require(len(starts) == 1 and starts[0][0] == 0 and starts[0][1].get("seq") == 1, "trusted_session_start_invalid")
    require(len(ended) == 1, "session_ended_missing_or_ambiguous")
    require(len(stopped) == 1, "probe_shutdown_missing_or_ambiguous")
    start_index, start_record = starts[0]
    ended_index, ended_record = ended[0]
    stopped_index, stopped_record = stopped[0]
    require(
        ended_index == len(records) - 2 and stopped_index == len(records) - 1,
        "normal_shutdown_tail_order_invalid",
    )
    require(
        int(stopped_record["seq"]) == int(ended_record["seq"]) + 1,
        "normal_shutdown_tail_not_adjacent",
    )
    require(ended_record.get("reason") == "application_quit", "session_ended_reason_not_application_quit")

    save_id_hash = _require_hash(identity.get("save_id_hash"), "final_tip_save_hash_invalid")
    world_id = identity.get("world_id")
    require(isinstance(world_id, int) and not isinstance(world_id, bool) and world_id >= 0, "final_tip_world_id_invalid")
    expected_mod_version = identity.get("mod_version")
    require(isinstance(expected_mod_version, str) and expected_mod_version, "final_tip_mod_version_invalid")
    require(
        expected_mod_version == startup_loader.get("expected_mod_version"),
        "startup_mod_version_lineage_mismatch",
    )

    snapshot_id = identity.get("snapshot_id")
    state_hash = _require_hash(identity.get("state_hash"), "final_tip_state_hash_invalid")
    snapshots = [
        (index, record)
        for index, record in enumerate(records)
        if record.get("event") == "state_snapshot"
    ]
    require(bool(snapshots), "shutdown_snapshot_sequence_missing")
    snapshot_sequences = [record.get("snapshot_seq") for _, record in snapshots]
    require(
        all(
            isinstance(value, int) and not isinstance(value, bool) and value > 0
            for value in snapshot_sequences
        ),
        "shutdown_snapshot_sequence_invalid",
    )
    require(
        snapshot_sequences == list(range(1, len(snapshots) + 1)),
        "shutdown_snapshot_sequence_not_gap_free",
    )
    snapshot_ids = [record.get("snapshot_id") for _, record in snapshots]
    require(
        all(isinstance(value, str) and bool(value) for value in snapshot_ids)
        and len(set(snapshot_ids)) == len(snapshot_ids),
        "shutdown_snapshot_id_duplicate_or_invalid",
    )
    tip_matches = [
        (index, record)
        for index, record in snapshots
        if record.get("snapshot_id") == snapshot_id
        and record.get("session_id") == session_id
    ]
    require(len(tip_matches) == 1, "final_tip_snapshot_missing_or_ambiguous")
    tip_index, tip_record = tip_matches[0]
    require(tip_index < ended_index, "final_tip_snapshot_not_before_shutdown")
    require(
        tip_index == snapshots[-1][0]
        and int(tip_record.get("seq") or 0)
        == max(int(record.get("seq") or 0) for _, record in snapshots),
        "final_tip_snapshot_not_latest",
    )
    game_build = tip_record.get("game_build")
    require(
        tip_record.get("schema") == "oni.state.v2"
        and tip_record.get("session_id") == session_id
        and tip_record.get("save_id_hash") == save_id_hash
        and tip_record.get("world_id") == world_id
        and tip_record.get("save_dirty") is False,
        "final_tip_snapshot_identity_mismatch",
    )
    require(isinstance(game_build, str) and game_build, "final_tip_game_build_invalid")
    require(tip_record.get("mod_version") == expected_mod_version, "final_tip_mod_version_mismatch")
    require(
        game_build == startup_loader.get("expected_game_build"),
        "startup_game_build_lineage_mismatch",
    )
    require(canonical_state_hash(tip_record) == state_hash, "final_tip_snapshot_state_hash_mismatch")

    require(
        start_record.get("schema") == "oni.event.v2"
        and start_record.get("reason") in TRUSTED_SESSION_START_REASONS
        and start_record.get("session_id") == session_id
        and start_record.get("save_id_hash") == save_id_hash
        and start_record.get("world_id") == world_id
        and start_record.get("save_dirty") is False
        and start_record.get("game_build") == game_build
        and start_record.get("mod_version") == expected_mod_version,
        "trusted_session_start_envelope_invalid",
    )
    for label, record in (("session_ended", ended_record), ("probe_shutdown", stopped_record)):
        require(record.get("schema") == "oni.event.v2", "shutdown_event_schema_invalid", label)
        require(record.get("session_id") == session_id, "shutdown_event_session_mismatch", label)
        require(record.get("save_id_hash") == save_id_hash, "shutdown_event_save_mismatch", label)
        require(record.get("world_id") == world_id, "shutdown_event_world_mismatch", label)
        require(record.get("save_dirty") is False, "shutdown_event_save_dirty", label)
        require(record.get("game_build") == game_build, "shutdown_event_build_mismatch", label)
        require(record.get("mod_version") == expected_mod_version, "shutdown_event_mod_mismatch", label)
    require(
        _aware_time(ended_record.get("ts"), "session_ended_timestamp_invalid")
        <= _aware_time(stopped_record.get("ts"), "probe_shutdown_timestamp_invalid"),
        "normal_shutdown_timestamp_order_invalid",
    )
    for record in records:
        expected_schema = "oni.state.v2" if record.get("event") == "state_snapshot" else "oni.event.v2"
        require(record.get("schema") == expected_schema, "shutdown_record_schema_invalid", str(record.get("event")))
        require(
            record.get("save_id_hash") == save_id_hash
            and record.get("world_id") == world_id
            and record.get("game_build") == game_build
            and record.get("mod_version") == expected_mod_version
            and record.get("save_dirty") is False,
            "shutdown_record_runtime_identity_mismatch",
            str(record.get("event")),
        )
    for record in records:
        for field, expected_value in startup_record_fields.items():
            require(
                record.get(field) == expected_value,
                "shutdown_record_startup_binding_mismatch",
                field,
            )
        require(
            record.get("requested_save_path_id") == requested_loader_path_id
            and record.get("active_save_path_id") == expected_active_save_path_id,
            "shutdown_record_save_path_binding_mismatch",
        )
    source = {
        "kind": source_kind,
        "path": str(event_log),
        "file_sha256": "sha256:" + digest.hexdigest(),
        "byte_length": before.st_size,
        "complete_line_count": all_line_count,
        "target_record_count": len(records),
        "first_target_line": line_numbers[0],
        "last_target_line": line_numbers[-1],
        "first_seq": sequences[0],
        "last_seq": sequences[-1],
        "record_chain_hash": canonical_hash(record_hashes),
        "trust_contract": "lineage_final_tip_gap_free_unique_terminal_v3",
    }
    termination = {
        "required_order": ["session_ended", "probe_shutdown"],
        "same_session": True,
        "adjacent_sequences": True,
        "terminal_for_session": True,
        "normal_exit_reason": "application_quit",
        "session_started": _termination_projection(start_record, line_numbers[start_index]),
        "final_tip_snapshot": {
            "line_number": line_numbers[tip_index],
            "seq": tip_record.get("seq"),
            "snapshot_id": snapshot_id,
            "game_build": game_build,
            "mod_version": expected_mod_version,
            "record_hash": canonical_hash(tip_record),
        },
        "session_ended": _termination_projection(ended_record, line_numbers[ended_index]),
        "probe_shutdown": _termination_projection(stopped_record, line_numbers[stopped_index]),
    }
    return (
        source,
        termination,
        {
            "game_build": str(game_build),
            "mod_version": str(expected_mod_version),
        },
        _file_version(before),
    )


def _lineage_snapshot(
    value: Any,
    *,
    expected_mod_version: str,
    code: str,
) -> Dict[str, Any]:
    snapshot = _exact_object(value, LINEAGE_SNAPSHOT_KEYS, code)
    require(
        isinstance(snapshot.get("session_id"), str) and bool(snapshot.get("session_id")),
        code,
        "session_id",
    )
    require(
        isinstance(snapshot.get("snapshot_id"), str)
        and snapshot.get("snapshot_id") == f"{snapshot.get('session_id')}:{snapshot.get('seq')}",
        code,
        "snapshot_id",
    )
    require(
        isinstance(snapshot.get("seq"), int)
        and not isinstance(snapshot.get("seq"), bool)
        and int(snapshot.get("seq")) > 0,
        code,
        "seq",
    )
    _require_hash(snapshot.get("save_id_hash"), code)
    require(
        isinstance(snapshot.get("world_id"), int)
        and not isinstance(snapshot.get("world_id"), bool)
        and int(snapshot.get("world_id")) >= 0,
        code,
        "world_id",
    )
    require(snapshot.get("mod_version") == expected_mod_version, code, "mod_version")
    _aware_time(snapshot.get("snapshot_ts"), code)
    return snapshot


def _disabled_lineage_policy(value: Any, code: str) -> Dict[str, Any]:
    policy = dict(value) if isinstance(value, Mapping) else {}
    require(
        policy.get("actions_enabled") is False
        and policy.get("allowed_save_hash_count") == 0
        and policy.get("pending_count") == 0
        and policy.get("approving_count") == 0
        and policy.get("approved_count") == 0
        and policy.get("executing_count") == 0
        and policy.get("confirming_count") == 0
        and policy.get("temporary_count", 0) == 0,
        code,
    )
    return policy


def _validate_lineage_chain(
    manifest: Mapping[str, Any],
    *,
    expected_run_id: str,
    expected_mod_version: str,
    forward_step: str,
    inverse_step: str,
) -> Dict[str, Any]:
    """Replay the origin -> activation -> checkpoint chain from immutable fields."""

    require(
        manifest.get("schema") == LINEAGE_SCHEMA
        and manifest.get("status") == "active"
        and manifest.get("test_run_id") == expected_run_id
        and manifest.get("expected_mod_version") == expected_mod_version,
        "lineage_manifest_binding_invalid",
    )
    save_path_id = _require_hash(manifest.get("save_path_id"), "lineage_save_path_id_invalid")
    require(manifest.get("save_absent_at_reservation") is True, "lineage_reservation_contract_invalid")
    _disabled_lineage_policy(
        manifest.get("reservation_policy"), "lineage_reservation_policy_invalid"
    )

    reservation_source = _lineage_snapshot(
        manifest.get("reservation_source"),
        expected_mod_version=expected_mod_version,
        code="lineage_reservation_source_invalid",
    )
    initial = manifest.get("initial") if isinstance(manifest.get("initial"), Mapping) else {}
    require(
        initial.get("kind") == "activation"
        and initial.get("previous_checkpoint_hash") is None
        and initial.get("checkpoint_hash")
        == lineage_contract.checkpoint_hash(dict(initial)),
        "lineage_activation_hash_invalid",
    )
    origin = _lineage_snapshot(
        initial.get("source"),
        expected_mod_version=expected_mod_version,
        code="lineage_activation_source_invalid",
    )
    require(
        _canonical_equal(reservation_source, origin),
        "lineage_origin_sources_mismatch",
    )
    activation_save = _exact_object(
        initial.get("save"),
        LINEAGE_EVENT_EVIDENCE_KEYS,
        "lineage_activation_save_fields_invalid",
    )
    activation_reload = _lineage_snapshot(
        initial.get("reload"),
        expected_mod_version=expected_mod_version,
        code="lineage_activation_reload_invalid",
    )
    require(
        activation_save.get("success") is True
        and activation_save.get("is_auto_save") is False
        and activation_save.get("update_save_pointer") is True
        and activation_save.get("file_rewrite_observed") is True
        and activation_save.get("save_file_readable") is True
        and activation_save.get("session_id") == origin.get("session_id")
        and isinstance(activation_save.get("seq"), int)
        and not isinstance(activation_save.get("seq"), bool)
        and activation_save.get("seq") > origin.get("seq")
        and _aware_time(activation_save.get("ts"), "lineage_activation_save_timestamp_invalid")
        > _aware_time(origin.get("snapshot_ts"), "lineage_origin_snapshot_timestamp_invalid")
        and activation_save.get("before_save_id_hash") == origin.get("save_id_hash")
        and activation_save.get("requested_save_path_id") == save_path_id
        and activation_save.get("result_save_path_id") == save_path_id
        and activation_save.get("active_save_path_id") == save_path_id
        and activation_save.get("written_save_id_hash")
        == activation_reload.get("save_id_hash")
        and activation_save.get("active_save_id_hash")
        == activation_reload.get("save_id_hash")
        and activation_save.get("save_id_hash") == activation_reload.get("save_id_hash")
        and activation_save.get("save_dirty") is False
        and activation_reload.get("session_id") != origin.get("session_id")
        and activation_reload.get("save_id_hash") != origin.get("save_id_hash")
        and activation_reload.get("world_id") == origin.get("world_id"),
        "lineage_activation_transition_invalid",
    )
    _disabled_lineage_policy(initial.get("policy_gate"), "lineage_activation_policy_invalid")

    checkpoints = manifest.get("checkpoints") if isinstance(manifest.get("checkpoints"), list) else []
    require(len(checkpoints) >= 2, "lineage_checkpoint_count_invalid")
    previous_hash = initial.get("checkpoint_hash")
    previous_reload = activation_reload
    projections: list[Dict[str, Any]] = []
    observed_steps: list[str] = []
    for expected_index, raw_checkpoint in enumerate(checkpoints, start=1):
        require(isinstance(raw_checkpoint, Mapping), "lineage_checkpoint_invalid", str(expected_index))
        checkpoint = dict(raw_checkpoint)
        step_id = checkpoint.get("step_id")
        require(
            isinstance(step_id, str)
            and STEP_PATTERN.fullmatch(step_id) is not None
            and step_id not in observed_steps,
            "lineage_checkpoint_step_invalid",
            str(expected_index),
        )
        observed_steps.append(step_id)
        require(
            checkpoint.get("index") == expected_index
            and checkpoint.get("previous_checkpoint_hash") == previous_hash
            and checkpoint.get("checkpoint_hash")
            == lineage_contract.checkpoint_hash(checkpoint)
            and _canonical_equal(checkpoint.get("before"), previous_reload),
            "lineage_checkpoint_hash_chain_invalid",
            step_id,
        )
        save = _exact_object(
            checkpoint.get("save"),
            LINEAGE_EVENT_EVIDENCE_KEYS,
            "lineage_checkpoint_save_fields_invalid",
        )
        reload_state = _lineage_snapshot(
            checkpoint.get("reload"),
            expected_mod_version=expected_mod_version,
            code="lineage_checkpoint_reload_invalid",
        )
        require(
            save.get("success") is True
            and save.get("is_auto_save") is False
            and save.get("update_save_pointer") is True
            and save.get("file_rewrite_observed") is True
            and save.get("save_file_readable") is True
            and save.get("session_id") == previous_reload.get("session_id")
            and isinstance(save.get("seq"), int)
            and not isinstance(save.get("seq"), bool)
            and save.get("seq") > previous_reload.get("seq")
            and _aware_time(save.get("ts"), "lineage_checkpoint_save_timestamp_invalid")
            > _aware_time(previous_reload.get("snapshot_ts"), "lineage_checkpoint_before_timestamp_invalid")
            and save.get("requested_save_path_id") == save_path_id
            and save.get("result_save_path_id") == save_path_id
            and save.get("active_save_path_id") == save_path_id
            and save.get("before_save_id_hash") == previous_reload.get("save_id_hash")
            and save.get("written_save_id_hash") == reload_state.get("save_id_hash")
            and save.get("active_save_id_hash") == reload_state.get("save_id_hash")
            and save.get("save_id_hash") == reload_state.get("save_id_hash")
            and save.get("save_dirty") is False
            and reload_state.get("session_id") != previous_reload.get("session_id")
            and reload_state.get("save_id_hash") != previous_reload.get("save_id_hash")
            and reload_state.get("world_id") == origin.get("world_id")
            and isinstance(checkpoint.get("artifacts"), list)
            and bool(checkpoint.get("artifacts")),
            "lineage_checkpoint_transition_invalid",
            step_id,
        )
        _disabled_lineage_policy(
            checkpoint.get("policy_gate"), "lineage_checkpoint_policy_invalid"
        )
        projections.append(
            {
                "index": expected_index,
                "step_id": step_id,
                "checkpoint_hash": checkpoint.get("checkpoint_hash"),
                "before_session_id": previous_reload.get("session_id"),
                "before_save_id_hash": previous_reload.get("save_id_hash"),
                "reload_session_id": reload_state.get("session_id"),
                "reload_save_id_hash": reload_state.get("save_id_hash"),
            }
        )
        previous_hash = checkpoint.get("checkpoint_hash")
        previous_reload = reload_state

    require(
        observed_steps.count(forward_step) == 1
        and observed_steps.count(inverse_step) == 1
        and observed_steps.index(forward_step) < observed_steps.index(inverse_step),
        "priority_checkpoint_order_invalid",
    )
    require(
        manifest.get("latest_checkpoint_hash") == previous_hash
        and _canonical_equal(manifest.get("latest"), previous_reload),
        "lineage_latest_pointer_invalid",
    )
    return {
        "origin": origin,
        "activation": {
            "checkpoint_hash": initial.get("checkpoint_hash"),
            "source_session_id": origin.get("session_id"),
            "source_save_id_hash": origin.get("save_id_hash"),
            "reload_session_id": activation_reload.get("session_id"),
            "reload_save_id_hash": activation_reload.get("save_id_hash"),
        },
        "checkpoints": projections,
        "final": previous_reload,
    }


def _validate_bindings(
    manifest_path: pathlib.Path,
    bridge_dir: pathlib.Path,
    pair_report_path: pathlib.Path,
    expected_pair_report_hash: str,
    expected_run_id: str,
    forward_step: str,
    inverse_step: str,
    command_ids: Sequence[str],
    command_bundle_hashes: Sequence[str],
) -> Dict[str, Any]:
    manifest_path = normalized(manifest_path)
    bridge_dir = normalized(bridge_dir)
    pair_report_path = normalized(pair_report_path)
    require(
        RUN_ID_PATTERN.fullmatch(expected_run_id or "") is not None
        and not expected_run_id.endswith(".")
        and expected_run_id.split(".", 1)[0].casefold() not in WINDOWS_RESERVED_NAMES,
        "run_id_invalid",
    )
    expected_manifest = bridge_dir / "evidence" / "save-lineage" / expected_run_id / "manifest.json"
    require(same_path(manifest_path, expected_manifest), "lineage_manifest_path_not_exact")
    require(same_path(pair_report_path, manifest_path.parent / "priority-pair-finalization.json"), "pair_report_path_not_exact")
    require(path_within(manifest_path, bridge_dir), "lineage_manifest_outside_bridge")
    require(path_within(pair_report_path, bridge_dir), "pair_report_outside_bridge")
    _assert_no_reparse_path(manifest_path, bridge_dir)
    _assert_no_reparse_path(pair_report_path, bridge_dir)
    require(
        STEP_PATTERN.fullmatch(forward_step) is not None
        and STEP_PATTERN.fullmatch(inverse_step) is not None
        and forward_step != inverse_step,
        "priority_step_ids_invalid",
    )
    require(
        len(command_ids) == 2
        and len(set(command_ids)) == 2
        and all(COMMAND_PATTERN.fullmatch(value) is not None for value in command_ids),
        "priority_command_ids_invalid",
    )
    require(len(command_bundle_hashes) == 2, "command_bundle_hash_count_invalid")
    for value in command_bundle_hashes:
        _require_hash(value, "expected_command_bundle_hash_invalid")

    manifest, manifest_file_hash, _ = _strict_object_snapshot(manifest_path, "lineage manifest")
    expected_mod_version = manifest.get("expected_mod_version")
    require(
        isinstance(expected_mod_version, str)
        and bool(expected_mod_version),
        "lineage_mod_version_binding_invalid",
    )
    chain = _validate_lineage_chain(
        manifest,
        expected_run_id=expected_run_id,
        expected_mod_version=expected_mod_version,
        forward_step=forward_step,
        inverse_step=inverse_step,
    )
    latest = chain["final"]
    identity = {
        "validation_run_id": expected_run_id,
        "session_id": latest.get("session_id"),
        "snapshot_id": latest.get("snapshot_id"),
        "save_id_hash": _require_hash(latest.get("save_id_hash"), "final_tip_save_hash_invalid"),
        "world_id": latest.get("world_id"),
        "mod_version": expected_mod_version,
    }
    require(isinstance(identity["snapshot_id"], str) and identity["snapshot_id"], "final_tip_snapshot_id_invalid")
    require(
        isinstance(identity["world_id"], int)
        and not isinstance(identity["world_id"], bool)
        and int(identity["world_id"]) >= 0,
        "final_tip_world_id_invalid",
    )

    report, report_file_hash, _ = _strict_object_snapshot(pair_report_path, "priority pair report")
    require(
        set(report) == PAIR_REPORT_KEYS
        and report.get("schema") == PAIR_REPORT_SCHEMA
        and report.get("status") == "passed"
        and report.get("classification") is None
        and report.get("verification_mode") == "offline_read_only"
        and report.get("fail_closed") is True,
        "priority_pair_report_shape_invalid",
    )
    report_hash = _require_hash(report.get("report_hash"), "priority_pair_report_hash_invalid")
    require(report_hash == _require_hash(expected_pair_report_hash, "expected_pair_report_hash_invalid"), "priority_pair_report_hash_mismatch")
    require(report_hash == _hash_without(report, "report_hash"), "priority_pair_report_self_hash_invalid")
    report_manifest = report.get("manifest") if isinstance(report.get("manifest"), Mapping) else {}
    require(
        same_path(pathlib.Path(str(report_manifest.get("path") or "")), manifest_path)
        and report_manifest.get("file_sha256") == manifest_file_hash
        and report_manifest.get("test_run_id") == expected_run_id
        and report_manifest.get("latest_checkpoint_hash") == manifest.get("latest_checkpoint_hash"),
        "priority_pair_manifest_cross_binding_invalid",
    )
    final_tip = report.get("final_tip") if isinstance(report.get("final_tip"), Mapping) else {}
    require(
        final_tip.get("session_id") == identity["session_id"]
        and final_tip.get("snapshot_id") == identity["snapshot_id"]
        and final_tip.get("save_id_hash") == identity["save_id_hash"]
        and HASH_PATTERN.fullmatch(str(final_tip.get("state_hash") or "")) is not None,
        "priority_pair_final_tip_cross_binding_invalid",
    )
    identity["state_hash"] = str(final_tip["state_hash"])
    pair = report.get("pair") if isinstance(report.get("pair"), Mapping) else {}
    report_commands = pair.get("commands") if isinstance(pair.get("commands"), list) else []
    require(
        pair.get("forward_step") == forward_step
        and pair.get("inverse_step") == inverse_step
        and len(report_commands) == 2
        and [item.get("step_id") if isinstance(item, Mapping) else None for item in report_commands]
        == [forward_step, inverse_step]
        and [item.get("id") if isinstance(item, Mapping) else None for item in report_commands]
        == list(command_ids),
        "priority_pair_commands_cross_binding_invalid",
    )

    raw_checkpoints = (
        manifest.get("checkpoints") if isinstance(manifest.get("checkpoints"), list) else []
    )
    checkpoint_by_step = {
        item.get("step_id"): item
        for item in raw_checkpoints
        if isinstance(item, Mapping) and isinstance(item.get("step_id"), str)
    }
    for step_id, command_id in zip((forward_step, inverse_step), command_ids):
        checkpoint = checkpoint_by_step.get(step_id)
        command = (
            checkpoint.get("command")
            if isinstance(checkpoint, Mapping)
            and isinstance(checkpoint.get("command"), Mapping)
            else {}
        )
        require(
            isinstance(checkpoint, Mapping)
            and command.get("id") == command_id
            and command.get("action") == ACTION,
            "priority_checkpoint_command_cross_binding_invalid",
            step_id,
        )

    projected_commands: list[Dict[str, Any]] = []
    file_snapshots: list[tuple[pathlib.Path, str, str, int]] = [
        (manifest_path, manifest_file_hash, "lineage manifest", MAX_JSON_BYTES),
        (pair_report_path, report_file_hash, "priority pair report", MAX_JSON_BYTES),
    ]
    for step_id, command_id, expected_bundle_hash, report_command in zip(
        (forward_step, inverse_step), command_ids, command_bundle_hashes, report_commands
    ):
        require(isinstance(report_command, Mapping), "priority_pair_command_invalid", step_id)
        require(report_command.get("action") == ACTION, "priority_pair_command_action_invalid", step_id)
        digest = hashlib.sha256(command_id.encode("utf-8", errors="strict")).hexdigest()
        bundle_path = bridge_dir / "confirmed-command-evidence" / digest / "approval-bundle.json"
        _assert_no_reparse_path(bundle_path, bridge_dir)
        reported_bundle = (
            report_command.get("automatic_approval_bundle")
            if isinstance(report_command.get("automatic_approval_bundle"), Mapping)
            else {}
        )
        reported_bundle_path = resolve_bridge_relative_path(
            reported_bundle.get("path"), bridge_dir, f"{step_id} reported approval bundle"
        )
        bundle, bundle_file_hash, _ = _strict_object_snapshot(bundle_path, f"{step_id} approval bundle")
        bundle_hash = _require_hash(bundle.get("bundle_hash"), "command_bundle_hash_invalid")
        require(
            bundle.get("schema") == BUNDLE_SCHEMA
            and bundle.get("command_id") == command_id
            and bundle.get("action") == ACTION
            and bundle_hash == expected_bundle_hash
            and bundle_hash == _hash_without(bundle, "bundle_hash")
            and isinstance(bundle.get("final_command"), Mapping)
            and bundle.get("final_command_hash") == canonical_hash(bundle.get("final_command")),
            "command_bundle_content_invalid",
            step_id,
        )
        require(
            same_path(reported_bundle_path, bundle_path)
            and reported_bundle.get("path") == bundle_path.relative_to(bridge_dir).as_posix()
            and reported_bundle.get("file_sha256") == bundle_file_hash
            and reported_bundle.get("bundle_hash") == bundle_hash,
            "command_bundle_report_binding_invalid",
            step_id,
        )

        freeze_path = manifest_path.parent / "priority-command-freezes" / f"{step_id}.json"
        _assert_no_reparse_path(freeze_path, bridge_dir)
        freeze, freeze_file_hash, _ = _strict_object_snapshot(freeze_path, f"{step_id} lineage freeze")
        freeze_hash = _require_hash(freeze.get("freeze_hash"), "command_freeze_hash_invalid")
        require(
            freeze.get("schema") == FREEZE_SCHEMA
            and freeze.get("step_id") == step_id
            and freeze.get("test_run_id") == expected_run_id
            and freeze_hash == _hash_without(freeze, "freeze_hash"),
            "command_freeze_content_invalid",
            step_id,
        )
        checkpoint = checkpoint_by_step[step_id]
        frozen_manifest = (
            freeze.get("manifest_binding")
            if isinstance(freeze.get("manifest_binding"), Mapping)
            else {}
        )
        require(
            frozen_manifest.get("latest_checkpoint_hash")
            == checkpoint.get("previous_checkpoint_hash")
            and _canonical_equal(frozen_manifest.get("before"), checkpoint.get("before")),
            "command_freeze_manifest_binding_invalid",
            step_id,
        )
        frozen_bundle = freeze.get("approval_bundle") if isinstance(freeze.get("approval_bundle"), Mapping) else {}
        frozen_bundle_path = resolve_bridge_relative_path(
            frozen_bundle.get("path"), bridge_dir, f"{step_id} frozen approval bundle"
        )
        require(
            same_path(frozen_bundle_path, bundle_path)
            and frozen_bundle.get("path") == bundle_path.relative_to(bridge_dir).as_posix()
            and frozen_bundle.get("file_sha256") == bundle_file_hash
            and frozen_bundle.get("bundle_hash") == bundle_hash
            and isinstance(frozen_bundle.get("document"), Mapping)
            and _canonical_equal(frozen_bundle.get("document"), bundle),
            "command_freeze_bundle_binding_invalid",
            step_id,
        )
        reported_freeze = (
            report_command.get("lineage_freeze")
            if isinstance(report_command.get("lineage_freeze"), Mapping)
            else {}
        )
        require(
            same_path(pathlib.Path(str(reported_freeze.get("path") or "")), freeze_path)
            and reported_freeze.get("file_sha256") == freeze_file_hash
            and reported_freeze.get("freeze_hash") == freeze_hash,
            "command_freeze_report_binding_invalid",
            step_id,
        )
        projected_commands.append(
            {
                "step_id": step_id,
                "id": command_id,
                "automatic_approval_bundle": {
                    "path": str(bundle_path),
                    "file_sha256": bundle_file_hash,
                    "bundle_hash": bundle_hash,
                },
                "lineage_freeze": {
                    "path": str(freeze_path),
                    "file_sha256": freeze_file_hash,
                    "freeze_hash": freeze_hash,
                },
            }
        )
        file_snapshots.extend(
            (
                (bundle_path, bundle_file_hash, f"{step_id} approval bundle", MAX_JSON_BYTES),
                (freeze_path, freeze_file_hash, f"{step_id} lineage freeze", MAX_JSON_BYTES),
            )
        )

    return {
        "manifest": manifest,
        "manifest_path": manifest_path,
        "manifest_file_sha256": manifest_file_hash,
        "report": report,
        "report_path": pair_report_path,
        "report_file_sha256": report_file_hash,
        "report_hash": report_hash,
        "lineage_chain": chain,
        "identity": identity,
        "commands": projected_commands,
        "file_snapshots": file_snapshots,
    }


def build_shutdown_evidence(
    *,
    manifest_path: pathlib.Path,
    bridge_dir: pathlib.Path,
    pair_report_path: pathlib.Path,
    expected_pair_report_hash: str,
    expected_run_id: str,
    forward_step: str,
    inverse_step: str,
    command_ids: Sequence[str],
    command_bundle_hashes: Sequence[str],
    event_log: pathlib.Path,
    startup_bundle_dir: pathlib.Path,
    expected_startup_evidence_hash: str,
    expected_validation_process_id: int,
    after_startup_verify_hook: Optional[Callable[[Mapping[str, Any]], None]] = None,
    after_startup_session_log_read_hook: Optional[Callable[[Mapping[str, Any]], None]] = None,
    after_shutdown_log_read_hook: Optional[Callable[[Mapping[str, Any]], None]] = None,
) -> Dict[str, Any]:
    bindings = _validate_bindings(
        manifest_path,
        bridge_dir,
        pair_report_path,
        expected_pair_report_hash,
        expected_run_id,
        forward_step,
        inverse_step,
        command_ids,
        command_bundle_hashes,
    )
    startup = _validate_startup_bundle(
        bundle_dir=startup_bundle_dir,
        expected_evidence_hash=expected_startup_evidence_hash,
        expected_process_id=expected_validation_process_id,
        manifest_path=manifest_path,
        bridge_dir=bridge_dir,
        expected_run_id=expected_run_id,
        lineage_origin=bindings["lineage_chain"]["origin"],
    )
    if after_startup_verify_hook is not None:
        after_startup_verify_hook(startup["projection"])
    origin_archive, origin_archive_version = _read_trusted_startup_session_archive(
        bridge_dir,
        bindings["lineage_chain"]["origin"],
        bindings["manifest"]["initial"]["save"],
        startup["projection"],
        startup["prefix_payload"],
        startup["prefix_record_hashes"],
    )
    if after_startup_session_log_read_hook is not None:
        after_startup_session_log_read_hook(origin_archive)
    activation_reload_archive, activation_reload_archive_version = (
        _read_trusted_reload_session_prefix(
            bridge_dir,
            bindings["manifest"]["initial"]["reload"],
            startup["projection"],
            bindings["manifest"]["save_path_id"],
            origin_archive["session_end_ts"],
        )
    )
    source, termination, runtime_identity, source_version = _read_trusted_session_log(
        event_log,
        bridge_dir,
        bindings["identity"],
        startup["projection"],
        bindings["manifest"]["save_path_id"],
    )
    if after_shutdown_log_read_hook is not None:
        after_shutdown_log_read_hook(source)
    for snapshot_path, expected_hash, label, maximum in bindings["file_snapshots"]:
        current_hash, _ = _stable_file_hash(snapshot_path, label, maximum=maximum)
        require(current_hash == expected_hash, "evidence_file_changed_across_validation", label)
    current_log_hash, current_log_size, current_log_version = _stable_file_custody_hash(
        normalized(event_log), "shutdown event log", maximum=MAX_LOG_BYTES
    )
    require(
        current_log_hash == source["file_sha256"]
        and current_log_size == source["byte_length"]
        and current_log_version == source_version,
        "shutdown_log_changed_across_validation",
    )
    origin_archive_relative = pathlib.PurePosixPath(str(origin_archive["relative_path"]))
    origin_archive_path = normalized(bridge_dir).joinpath(*origin_archive_relative.parts)
    current_origin_hash, current_origin_size, current_origin_version = (
        _stable_file_custody_hash(
            origin_archive_path,
            "startup origin session archive",
            maximum=MAX_LOG_BYTES,
        )
    )
    require(
        current_origin_hash == origin_archive["file_sha256"]
        and current_origin_size == origin_archive["byte_length"]
        and current_origin_version == origin_archive_version,
        "startup_origin_archive_changed_across_validation",
    )
    activation_reload_relative = pathlib.PurePosixPath(
        str(activation_reload_archive["relative_path"])
    )
    activation_reload_path = normalized(bridge_dir).joinpath(
        *activation_reload_relative.parts
    )
    (
        current_activation_reload_hash,
        current_activation_reload_size,
        current_activation_reload_version,
    ) = _stable_file_custody_hash(
        activation_reload_path,
        "activation reload session archive",
        maximum=MAX_LOG_BYTES,
    )
    require(
        current_activation_reload_hash == activation_reload_archive["file_sha256"]
        and current_activation_reload_size == activation_reload_archive["byte_length"]
        and current_activation_reload_version == activation_reload_archive_version,
        "activation_reload_archive_changed_across_validation",
    )
    startup_after = _validate_startup_bundle(
        bundle_dir=startup_bundle_dir,
        expected_evidence_hash=expected_startup_evidence_hash,
        expected_process_id=expected_validation_process_id,
        manifest_path=manifest_path,
        bridge_dir=bridge_dir,
        expected_run_id=expected_run_id,
        lineage_origin=bindings["lineage_chain"]["origin"],
    )
    require(
        _canonical_equal(startup_after["projection"], startup["projection"])
        and startup_after["custody"] == startup["custody"]
        and startup_after["prefix_payload"] == startup["prefix_payload"]
        and startup_after["prefix_record_hashes"] == startup["prefix_record_hashes"],
        "startup_bundle_changed_across_validation",
    )
    document: Dict[str, Any] = {
        "schema": EVIDENCE_SCHEMA,
        "status": "passed",
        "classification": None,
        "verification_mode": "offline_read_only",
        "fail_closed": True,
        "lineage": {
            "path": str(bindings["manifest_path"]),
            "file_sha256": bindings["manifest_file_sha256"],
            "test_run_id": expected_run_id,
            "latest_checkpoint_hash": bindings["manifest"].get("latest_checkpoint_hash"),
            "origin": copy.deepcopy(bindings["lineage_chain"]["origin"]),
            "activation": copy.deepcopy(bindings["lineage_chain"]["activation"]),
            "checkpoints": copy.deepcopy(bindings["lineage_chain"]["checkpoints"]),
            "origin_archive": origin_archive,
            "activation_reload_archive": activation_reload_archive,
            "final_session_id": bindings["identity"]["session_id"],
        },
        "priority_pair_report": {
            "path": str(bindings["report_path"]),
            "file_sha256": bindings["report_file_sha256"],
            "report_hash": bindings["report_hash"],
        },
        "startup_consumption_certificate": startup["projection"],
        "final_tip": {**dict(bindings["identity"]), **runtime_identity},
        "pair": {
            "forward_step": forward_step,
            "inverse_step": inverse_step,
            "commands": bindings["commands"],
        },
        "source": source,
        "termination": termination,
        "evidence_hash": "",
    }
    document["evidence_hash"] = _hash_without(document, "evidence_hash")
    return document


def write_evidence_create_only(
    output_path: pathlib.Path,
    manifest_path: pathlib.Path,
    document: Mapping[str, Any],
    *,
    before_open_hook: Optional[Callable[[pathlib.Path], None]] = None,
    after_open_hook: Optional[Callable[[pathlib.Path], None]] = None,
    after_close_hook: Optional[Callable[[pathlib.Path], None]] = None,
    after_hash_hook: Optional[Callable[[pathlib.Path], None]] = None,
    before_revoke_hook: Optional[Callable[[pathlib.Path], None]] = None,
) -> pathlib.Path:
    output_path = normalized(output_path)
    expected = normalized(manifest_path).parent / "shutdown-evidence.json"
    require(same_path(output_path, expected), "shutdown_evidence_output_path_not_exact")
    trust_boundary = normalized(manifest_path).parents[3]
    _assert_no_reparse_path(output_path, trust_boundary, allow_missing_leaf=True)
    parent_identity = _directory_identity(output_path.parent, "shutdown evidence parent")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")
    expected_payload_hash = "sha256:" + hashlib.sha256(payload).hexdigest()
    require(
        document.get("evidence_hash") == _hash_without(document, "evidence_hash"),
        "shutdown_evidence_document_self_hash_invalid",
    )

    def parent_is_stable() -> bool:
        try:
            _assert_no_reparse_path(output_path.parent, trust_boundary)
            return (
                _directory_identity(output_path.parent, "shutdown evidence parent")
                == parent_identity
            )
        except ShutdownEvidenceError:
            return False

    require(parent_is_stable(), "shutdown_evidence_publication_parent_changed")
    if before_open_hook is not None:
        before_open_hook(output_path)
    try:
        descriptor = os.open(output_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError as exc:
        raise ShutdownEvidenceError("shutdown_evidence_already_exists", str(output_path)) from exc
    owner_identity: Optional[tuple[int, int, int]] = None

    def identity(metadata: os.stat_result) -> tuple[int, int, int]:
        return (int(metadata.st_dev), int(metadata.st_ino), stat.S_IFMT(metadata.st_mode))

    def path_is_owned() -> bool:
        if owner_identity is None:
            return False
        try:
            metadata = output_path.lstat()
        except OSError:
            return False
        return (
            parent_is_stable()
            and
            stat.S_ISREG(metadata.st_mode)
            and not _is_reparse(metadata)
            and metadata.st_nlink == 1
            and identity(metadata) == owner_identity
        )

    def revoke_owned_publication() -> bool:
        if owner_identity is None or not parent_is_stable() or not os.path.lexists(output_path):
            return False
        if before_revoke_hook is not None:
            before_revoke_hook(output_path)
        claim = output_path.parent / f".shutdown-revoke-{uuid.uuid4().hex[:16]}.tmp"
        require(not os.path.lexists(claim), "shutdown_evidence_revoke_claim_exists")
        try:
            os.rename(output_path, claim)
        except OSError as exc:
            raise ShutdownEvidenceError("shutdown_evidence_publication_outcome_unknown", "reclaim") from exc
        try:
            metadata = claim.lstat()
        except OSError as exc:
            raise ShutdownEvidenceError("shutdown_evidence_publication_outcome_unknown", "claim_missing") from exc
        owned = (
            stat.S_ISREG(metadata.st_mode)
            and not _is_reparse(metadata)
            and metadata.st_nlink == 1
            and identity(metadata) == owner_identity
        )
        if not owned:
            if not os.path.lexists(output_path):
                try:
                    os.rename(claim, output_path)
                except OSError:
                    pass
            raise ShutdownEvidenceError(
                "shutdown_evidence_publication_outcome_unknown", "replacement_retained"
            )
        # The random claim is transaction-private.  Recheck immediately before
        # unlink so an unexpected claim replacement is never deleted.
        current = claim.lstat()
        if (
            not stat.S_ISREG(current.st_mode)
            or _is_reparse(current)
            or current.st_nlink != 1
            or identity(current) != owner_identity
        ):
            raise ShutdownEvidenceError(
                "shutdown_evidence_publication_outcome_unknown", "claim_changed"
            )
        claim.unlink()
        return True

    handle: Optional[BinaryIO] = None
    try:
        handle = os.fdopen(descriptor, "w+b", closefd=True)
        created = os.fstat(handle.fileno())
        require(
            stat.S_ISREG(created.st_mode) and created.st_nlink == 1,
            "shutdown_evidence_publication_target_unsafe",
        )
        owner_identity = identity(created)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
        if after_open_hook is not None:
            after_open_hook(output_path)
        written = os.fstat(handle.fileno())
        require(
            identity(written) == owner_identity
            and written.st_nlink == 1
            and written.st_size == len(payload),
            "shutdown_evidence_publication_descriptor_changed",
        )
        handle.seek(0)
        descriptor_payload = handle.read(len(payload) + 1)
        require(
            descriptor_payload == payload
            and "sha256:" + hashlib.sha256(descriptor_payload).hexdigest() == expected_payload_hash,
            "shutdown_evidence_publication_descriptor_hash_mismatch",
        )
        require(path_is_owned(), "shutdown_evidence_publication_path_changed")
        handle.close()
        handle = None
        if after_close_hook is not None:
            after_close_hook(output_path)
        published, published_hash, published_size = _strict_object_snapshot(
            output_path, "published shutdown evidence"
        )
        require(
            path_is_owned()
            and published_size == len(payload)
            and published_hash == expected_payload_hash
            and _canonical_equal(published, document),
            "shutdown_evidence_publication_hash_mismatch",
        )
        if after_hash_hook is not None:
            after_hash_hook(output_path)
        require(path_is_owned(), "shutdown_evidence_publication_path_changed_after_hash")
    except Exception as exc:
        if handle is not None:
            handle.close()
        try:
            removed = revoke_owned_publication()
        except ShutdownEvidenceError:
            raise
        if removed:
            raise
        raise ShutdownEvidenceError(
            "shutdown_evidence_publication_outcome_unknown", type(exc).__name__
        ) from exc
    return output_path


def verify_published_shutdown_evidence(
    *,
    evidence_path: pathlib.Path,
    expected_evidence_hash: str,
    manifest_path: pathlib.Path,
    bridge_dir: pathlib.Path,
    pair_report_path: pathlib.Path,
    expected_pair_report_hash: str,
    expected_run_id: str,
    forward_step: str,
    inverse_step: str,
    command_ids: Sequence[str],
    command_bundle_hashes: Sequence[str],
    startup_bundle_dir: pathlib.Path,
    expected_startup_evidence_hash: str,
    expected_validation_process_id: int,
    after_evidence_snapshot_hook: Optional[Callable[[Mapping[str, Any]], None]] = None,
) -> Dict[str, Any]:
    evidence_path = normalized(evidence_path)
    expected_path = normalized(manifest_path).parent / "shutdown-evidence.json"
    require(same_path(evidence_path, expected_path), "shutdown_evidence_path_not_exact")
    _assert_no_reparse_path(evidence_path, normalized(bridge_dir))
    document, evidence_file_hash, evidence_size, evidence_version = (
        _strict_object_custody_snapshot(evidence_path, "shutdown evidence")
    )
    if after_evidence_snapshot_hook is not None:
        after_evidence_snapshot_hook(document)
    require(
        set(document) == EVIDENCE_KEYS
        and document.get("schema") == EVIDENCE_SCHEMA
        and document.get("status") == "passed"
        and document.get("classification") is None
        and document.get("verification_mode") == "offline_read_only"
        and document.get("fail_closed") is True,
        "shutdown_evidence_shape_invalid",
    )
    evidence_hash = _require_hash(document.get("evidence_hash"), "shutdown_evidence_hash_invalid")
    require(
        evidence_hash == _require_hash(expected_evidence_hash, "expected_shutdown_evidence_hash_invalid"),
        "shutdown_evidence_expected_hash_mismatch",
    )
    require(evidence_hash == _hash_without(document, "evidence_hash"), "shutdown_evidence_self_hash_invalid")
    source = document.get("source") if isinstance(document.get("source"), Mapping) else {}
    event_log = normalized(pathlib.Path(str(source.get("path") or "")))
    expected = build_shutdown_evidence(
        manifest_path=manifest_path,
        bridge_dir=bridge_dir,
        pair_report_path=pair_report_path,
        expected_pair_report_hash=expected_pair_report_hash,
        expected_run_id=expected_run_id,
        forward_step=forward_step,
        inverse_step=inverse_step,
        command_ids=command_ids,
        command_bundle_hashes=command_bundle_hashes,
        event_log=event_log,
        startup_bundle_dir=startup_bundle_dir,
        expected_startup_evidence_hash=expected_startup_evidence_hash,
        expected_validation_process_id=expected_validation_process_id,
    )
    require(_canonical_equal(document, expected), "shutdown_evidence_reconstruction_mismatch")
    _assert_no_reparse_path(evidence_path, normalized(bridge_dir))
    final_document, final_file_hash, final_size, final_version = (
        _strict_object_custody_snapshot(evidence_path, "shutdown evidence")
    )
    require(
        final_file_hash == evidence_file_hash
        and final_size == evidence_size
        and final_version == evidence_version
        and _canonical_equal(final_document, document),
        "shutdown_evidence_changed_across_reconstruction",
    )
    return {
        "document": document,
        "path": evidence_path,
        "file_sha256": evidence_file_hash,
        "evidence_hash": evidence_hash,
        "source_path": event_log,
        "source_file_sha256": source.get("file_sha256"),
    }


def _add_binding_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--manifest", type=pathlib.Path, required=True)
    parser.add_argument("--bridge-dir", type=pathlib.Path, required=True)
    parser.add_argument("--priority-pair-report", type=pathlib.Path, required=True)
    parser.add_argument("--priority-pair-report-hash", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--forward-step", required=True)
    parser.add_argument("--inverse-step", required=True)
    parser.add_argument("--command-id", action="append", required=True)
    parser.add_argument("--command-bundle-hash", action="append", required=True)
    parser.add_argument("--startup-consumption-bundle", type=pathlib.Path, required=True)
    parser.add_argument("--startup-consumption-evidence-hash", required=True)
    parser.add_argument("--validation-process-id", type=int, required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    operations = parser.add_subparsers(dest="operation", required=True)
    create = operations.add_parser("create", help="Build and create-only publish a normal-shutdown proof.")
    _add_binding_arguments(create)
    create.add_argument("--event-log", type=pathlib.Path, required=True)
    create.add_argument("--output", type=pathlib.Path, required=True)
    verify = operations.add_parser("verify", help="Independently rebuild and verify a published proof.")
    _add_binding_arguments(verify)
    verify.add_argument("--shutdown-evidence", type=pathlib.Path, required=True)
    verify.add_argument("--shutdown-evidence-hash", required=True)
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.operation == "create":
            document = build_shutdown_evidence(
                manifest_path=args.manifest,
                bridge_dir=args.bridge_dir,
                pair_report_path=args.priority_pair_report,
                expected_pair_report_hash=args.priority_pair_report_hash,
                expected_run_id=args.run_id,
                forward_step=args.forward_step,
                inverse_step=args.inverse_step,
                command_ids=list(args.command_id or []),
                command_bundle_hashes=list(args.command_bundle_hash or []),
                event_log=args.event_log,
                startup_bundle_dir=args.startup_consumption_bundle,
                expected_startup_evidence_hash=args.startup_consumption_evidence_hash,
                expected_validation_process_id=args.validation_process_id,
            )
            published = write_evidence_create_only(args.output, args.manifest, document)
            result = {
                "schema": EVIDENCE_SCHEMA,
                "status": "published",
                "file": published.name,
                "path_id_lower": _path_id_lower(published),
                "file_sha256": sha256_file(published, "published shutdown evidence"),
                "evidence_hash": document["evidence_hash"],
            }
        else:
            verified = verify_published_shutdown_evidence(
                evidence_path=args.shutdown_evidence,
                expected_evidence_hash=args.shutdown_evidence_hash,
                manifest_path=args.manifest,
                bridge_dir=args.bridge_dir,
                pair_report_path=args.priority_pair_report,
                expected_pair_report_hash=args.priority_pair_report_hash,
                expected_run_id=args.run_id,
                forward_step=args.forward_step,
                inverse_step=args.inverse_step,
                command_ids=list(args.command_id or []),
                command_bundle_hashes=list(args.command_bundle_hash or []),
                startup_bundle_dir=args.startup_consumption_bundle,
                expected_startup_evidence_hash=args.startup_consumption_evidence_hash,
                expected_validation_process_id=args.validation_process_id,
            )
            result = {
                "schema": EVIDENCE_SCHEMA,
                "status": "passed",
                "file": pathlib.Path(verified["path"]).name,
                "path_id_lower": _path_id_lower(verified["path"]),
                "file_sha256": verified["file_sha256"],
                "evidence_hash": verified["evidence_hash"],
            }
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))
        return 0
    except ShutdownEvidenceError as exc:
        print(
            json.dumps(
                {
                    "schema": EVIDENCE_SCHEMA,
                    "status": "incomplete_or_failed",
                    "classification": "behavior_fail",
                    "verification_mode": "offline_read_only",
                    "blockers": [exc.code],
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 1
    except Exception as exc:
        print(
            json.dumps(
                {
                    "schema": EVIDENCE_SCHEMA,
                    "status": "incomplete_or_failed",
                    "classification": "infra_fail",
                    "verification_mode": "offline_read_only",
                    "blockers": ["shutdown_evidence_exception"],
                    "exception_type": type(exc).__name__,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
