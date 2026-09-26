#!/usr/bin/env python3
"""Create or replay-verify a fail-closed ONI startup-consumption certificate.

The certificate joins the externally anchored runtime-config evidence and
startup binding to the preferences consumed by ONI, the native Player.log load
lines, and the minimal loader-bound prefix of Bridge's immutable origin-session
archive.  A full archived source may contain the later derivative Save-As
transition; only its path-free custody projection is frozen into the report.
It never authorizes an action; it only proves which save/process actually
consumed one one-shot startup request.

Creation captures a stable Player.log prefix and exactly the origin records
through the first trusted state snapshot, then publishes a fixed create-only
bundle.  The JSON report contains hashes and path identifiers only; raw local
paths remain inside the private captured artifacts.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import datetime as dt
import errno
import hashlib
import json
import ntpath
import os
import pathlib
import re
import stat
import sys
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.canonical_hash import CanonicalHashError, canonical_hash, loads_strict


EVIDENCE_SCHEMA = "oni.startup-consumption-evidence.v3"
REPLAY_VERIFICATION_SCHEMA = "oni.startup-consumption-evidence-replay-verification.v3"
BINDING_SCHEMA = "oni.runtime-startup-binding.v2"
INTENT_SCHEMA = "oni.live-validation-startup-intent.v2"
HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
NONCE_RE = re.compile(r"^startup-[0-9a-f]{32}$")
RUNTIME_BOOTSTRAP_TRANSACTION_RE = re.compile(r"^rtb-[0-9a-f]{24}$")
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
MAX_JSON_BYTES = 32 * 1024 * 1024
MAX_PREFS_BYTES = 8 * 1024 * 1024
MAX_PLAYER_LOG_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_RECORD_BYTES = 4 * 1024 * 1024
MAX_PROCESS_IMAGE_BYTES = 512 * 1024 * 1024
REPARSE_ATTRIBUTE = 0x400

BINDING_KEYS = frozenset(
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

RUNTIME_KEYS = frozenset(
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
RUNTIME_BOOTSTRAP_RESULT_RECORD_KEYS = frozenset(
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
RUNTIME_BOOTSTRAP_ARTIFACT_NAMES = (
    "OniAiProbeMod.dll",
    "OniAiProbeMod.pdb",
    "mod.yaml",
    "mod_info.yaml",
)

RUNTIME_EVIDENCE_KEYS = frozenset(
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

ARTIFACT_FILES = {
    "runtime_config_evidence": "runtime-config-evidence.json",
    "startup_binding": "runtime-startup-binding.json",
    "consumed_prefs": "consumed-kplayerprefs.yaml",
    "player_log_prefix": "Player.log.prefix",
    "session_archive_prefix": "session-state.jsonl",
}
REPORT_FILE = "evidence.json"
PROCESS_EVIDENCE_KEYS = frozenset(
    {
        "process_id",
        "image_name",
        "creation_token",
        "image_path_id_upper",
        "image_file_sha256",
    }
)
SOURCE_ARCHIVE_KEYS = frozenset(
    {
        "session_id",
        "source_identity_id",
        "file_sha256",
        "byte_length",
        "record_count",
        "first_seq",
        "last_seq",
        "record_chain_hash",
        "prefix_record_count",
        "prefix_file_sha256",
        "prefix_byte_length",
    }
)
EVIDENCE_KEYS = frozenset(
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


class StartupEvidenceError(RuntimeError):
    """A stable, non-path-disclosing startup evidence failure."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def require(condition: bool, code: str, detail: str = "") -> None:
    if not condition:
        raise StartupEvidenceError(code, detail)


def sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def require_hash(value: Any, label: str) -> str:
    require(isinstance(value, str) and HASH_RE.fullmatch(value) is not None, "hash_invalid", label)
    return value


def require_nonempty_string(value: Any, label: str) -> str:
    require(isinstance(value, str) and bool(value), "string_invalid", label)
    return value


def require_offset_timestamp(value: Any, label: str) -> str:
    text = require_nonempty_string(value, label)
    require(text.endswith("Z") or re.search(r"[+-][0-9]{2}:[0-9]{2}$", text) is not None, "timestamp_offset_missing", label)
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise StartupEvidenceError("timestamp_invalid", label) from exc
    require(parsed.tzinfo is not None and parsed.utcoffset() is not None, "timestamp_offset_missing", label)
    return text


def json_document_hash(document: Mapping[str, Any]) -> str:
    material = {key: value for key, value in document.items() if key != "document_hash"}
    try:
        encoded = json.dumps(
            material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8", "strict")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise StartupEvidenceError("document_not_hashable") from exc
    return sha256_bytes(encoded)


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8", "strict")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise StartupEvidenceError("document_not_hashable") from exc


def _document_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        return (
            json.dumps(
                dict(value),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            ).encode("utf-8", "strict")
            + b"\n"
        )
    except (TypeError, ValueError, UnicodeError) as exc:
        raise StartupEvidenceError("document_not_hashable") from exc


def _typed_equal(left: Any, right: Any) -> bool:
    try:
        return _canonical_bytes(left) == _canonical_bytes(right)
    except StartupEvidenceError:
        return False


def _valid_identity(value: Any) -> bool:
    return (
        type(value) is list
        and len(value) == 3
        and all(type(item) is int and item >= 0 for item in value)
    )


def _validate_result_lock_projection(
    document: Mapping[str, Any],
    *,
    prefix: str,
    purpose: str,
) -> None:
    epoch = document.get(f"{prefix}_lock_epoch")
    claim = document.get(f"{prefix}_lock_claim")
    release = document.get(f"{prefix}_lock_release")
    require(type(epoch) is int and epoch >= 0, "runtime_bootstrap_result_lock_epoch_invalid", prefix)
    require(type(claim) is dict, "runtime_bootstrap_result_lock_claim_invalid", prefix)
    require(type(release) is dict, "runtime_bootstrap_result_lock_release_invalid", prefix)
    claim_keys = {
        "schema",
        "transaction_id",
        "plan_hash",
        "epoch",
        "purpose",
        "intent_hash",
        "payload_sha256",
        "lock_identity",
        "claim_hash",
    }
    release_keys = {
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
    }
    require(set(claim) == claim_keys, "runtime_bootstrap_result_lock_claim_invalid", prefix)
    require(set(release) == release_keys, "runtime_bootstrap_result_lock_release_invalid", prefix)
    require(
        claim.get("schema")
        == "oni.live-runtime-bootstrap-environment-lock-claim.v1"
        and release.get("schema")
        == "oni.live-runtime-bootstrap-environment-lock-release.v1",
        "runtime_bootstrap_result_lock_schema_invalid",
        prefix,
    )
    for key in ("plan_hash", "intent_hash", "payload_sha256", "claim_hash"):
        require_hash(claim.get(key), f"{prefix} lock claim {key}")
    for key in ("plan_hash", "intent_hash", "payload_sha256", "claim_hash", "release_hash"):
        require_hash(release.get(key), f"{prefix} lock release {key}")
    require(
        claim.get("transaction_id") == document.get("transaction_id")
        and release.get("transaction_id") == document.get("transaction_id")
        and claim.get("plan_hash") == document.get("plan_hash")
        and release.get("plan_hash") == document.get("plan_hash")
        and type(claim.get("epoch")) is int
        and type(release.get("epoch")) is int
        and claim.get("epoch") == epoch == release.get("epoch")
        and claim.get("purpose") == purpose == release.get("purpose")
        and claim.get("intent_hash") == release.get("intent_hash")
        and claim.get("payload_sha256") == release.get("payload_sha256")
        and claim.get("claim_hash") == release.get("claim_hash")
        and _valid_identity(claim.get("lock_identity"))
        and claim.get("lock_identity") == release.get("released_identity")
        and document.get(f"{prefix}_lock_claim_hash") == claim.get("claim_hash")
        and document.get(f"{prefix}_lock_release_hash") == release.get("release_hash"),
        "runtime_bootstrap_result_lock_cross_binding_invalid",
        prefix,
    )
    claim_material = dict(claim)
    claim_hash = claim_material.pop("claim_hash")
    release_material = dict(release)
    release_hash = release_material.pop("release_hash")
    require(
        claim_hash == sha256_bytes(_canonical_bytes(claim_material))
        and release_hash == sha256_bytes(_canonical_bytes(release_material)),
        "runtime_bootstrap_result_lock_self_hash_invalid",
        prefix,
    )


def validate_runtime_bootstrap_result_record(
    record: Any,
    binding: Mapping[str, Any],
) -> dict[str, Any]:
    require(type(record) is dict, "runtime_bootstrap_result_record_invalid")
    require(
        set(record) == RUNTIME_BOOTSTRAP_RESULT_RECORD_KEYS,
        "runtime_bootstrap_result_record_fields_invalid",
    )
    for key in {
        "path_id_lower",
        "external_expected_sha256",
        "raw_sha256",
        "result_hash",
        "probe_tree_content_hash",
    }:
        require_hash(record.get(key), f"runtime bootstrap result {key}")
    transaction_id = record.get("transaction_id")
    require(
        isinstance(transaction_id, str)
        and RUNTIME_BOOTSTRAP_TRANSACTION_RE.fullmatch(transaction_id) is not None,
        "runtime_bootstrap_result_transaction_invalid",
    )
    raw_length = record.get("raw_byte_length")
    encoded = record.get("raw_utf8_base64")
    require(
        type(raw_length) is int and raw_length >= 0 and isinstance(encoded, str),
        "runtime_bootstrap_result_raw_fields_invalid",
    )
    try:
        payload = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (UnicodeError, ValueError) as exc:
        raise StartupEvidenceError("runtime_bootstrap_result_base64_invalid") from exc
    require(
        base64.b64encode(payload).decode("ascii") == encoded,
        "runtime_bootstrap_result_base64_invalid",
    )
    require(
        len(payload) == raw_length and not payload.startswith(b"\xef\xbb\xbf"),
        "runtime_bootstrap_result_raw_length_invalid",
    )
    require(
        sha256_bytes(payload) == record.get("raw_sha256")
        == record.get("external_expected_sha256"),
        "runtime_bootstrap_result_raw_hash_invalid",
    )
    document = parse_strict_object(payload, "runtime bootstrap result")
    require(payload == _document_bytes(document), "runtime_bootstrap_result_serialization_invalid")
    require(
        type(record.get("document")) is dict
        and _typed_equal(document, record.get("document")),
        "runtime_bootstrap_result_document_mismatch",
    )
    require(set(document) == RUNTIME_BOOTSTRAP_RESULT_KEYS, "runtime_bootstrap_result_fields_invalid")
    require(
        document.get("schema") == "oni.live-runtime-bootstrap-result.v1"
        and document.get("status") == "installed_ready"
        and document.get("classification") == "static_probe"
        and document.get("physical_commit_complete") is True
        and document.get("shared_lock_released") is True
        and document.get("game_launched") is False,
        "runtime_bootstrap_result_status_invalid",
    )
    require(
        isinstance(document.get("expected_mod_version"), str)
        and bool(document["expected_mod_version"])
        and (
            document.get("archive_relative") is None
            or isinstance(document.get("archive_relative"), str)
            and bool(document["archive_relative"])
        )
        and document.get("old_probe_recovery_relative")
        in {None, "recovery/old-probe"},
        "runtime_bootstrap_result_value_types_invalid",
    )
    for key in {
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
        "result_hash",
    }:
        require_hash(document.get(key), f"runtime bootstrap result {key}")
    require(
        isinstance(document.get("run_id"), str)
        and RUN_ID_RE.fullmatch(document["run_id"]) is not None
        and document.get("run_id") == binding.get("validation_run_id")
        and document.get("transaction_id") == transaction_id
        and document.get("expected_mod_version") == binding.get("expected_mod_version")
        and document.get("environment_manifest_sha256")
        == binding.get("environment_manifest_sha256")
        and document.get("release_dll_sha256") == binding.get("expected_probe_dll_sha256"),
        "runtime_bootstrap_result_binding_invalid",
    )
    result_material = dict(document)
    claimed_result_hash = result_material.pop("result_hash")
    require(
        claimed_result_hash == sha256_bytes(_canonical_bytes(result_material))
        == record.get("result_hash"),
        "runtime_bootstrap_result_self_hash_invalid",
    )
    artifacts = document.get("installed_probe_artifacts")
    require(
        type(artifacts) is dict and set(artifacts) == set(RUNTIME_BOOTSTRAP_ARTIFACT_NAMES),
        "runtime_bootstrap_result_artifacts_invalid",
    )
    for name in RUNTIME_BOOTSTRAP_ARTIFACT_NAMES:
        artifact = artifacts.get(name)
        require(
            type(artifact) is dict
            and set(artifact) == {"length", "sha256"}
            and type(artifact.get("length")) is int
            and artifact["length"] >= 0,
            "runtime_bootstrap_result_artifact_invalid",
            name,
        )
        require_hash(artifact.get("sha256"), f"runtime bootstrap artifact {name}")
    require(
        artifacts["OniAiProbeMod.dll"]["sha256"] == document.get("release_dll_sha256"),
        "runtime_bootstrap_result_dll_invalid",
    )
    tree_content = {
        "exists": True,
        "entries": [
            {
                "kind": "file",
                "relative": name,
                "length": artifacts[name]["length"],
                "sha256": artifacts[name]["sha256"],
            }
            for name in sorted(RUNTIME_BOOTSTRAP_ARTIFACT_NAMES, key=str.casefold)
        ],
    }
    require(
        sha256_bytes(_canonical_bytes(tree_content)) == document.get("probe_tree_content_hash")
        == record.get("probe_tree_content_hash"),
        "runtime_bootstrap_result_probe_tree_invalid",
    )
    identity_tree = document.get("installed_probe_tree_identity")
    require(
        type(identity_tree) is dict
        and set(identity_tree) == {"exists", "root_identity", "entries"}
        and identity_tree.get("exists") is True
        and _valid_identity(identity_tree.get("root_identity"))
        and type(identity_tree.get("entries")) is list
        and len(identity_tree["entries"]) == len(RUNTIME_BOOTSTRAP_ARTIFACT_NAMES),
        "runtime_bootstrap_result_probe_identity_invalid",
    )
    expected_identity_shape = [
        (name, "file")
        for name in sorted(RUNTIME_BOOTSTRAP_ARTIFACT_NAMES, key=str.casefold)
    ]
    actual_identity_shape: list[tuple[str, str]] = []
    for entry in identity_tree["entries"]:
        require(
            type(entry) is dict
            and set(entry) == {"relative", "kind", "identity"}
            and isinstance(entry.get("relative"), str)
            and isinstance(entry.get("kind"), str)
            and _valid_identity(entry.get("identity")),
            "runtime_bootstrap_result_probe_identity_invalid",
        )
        actual_identity_shape.append((entry["relative"], entry["kind"]))
    require(
        actual_identity_shape == expected_identity_shape,
        "runtime_bootstrap_result_probe_identity_invalid",
    )
    producer = document.get("producer")
    require(
        type(producer) is dict
        and set(producer) == {"length", "sha256"}
        and type(producer.get("length")) is int
        and producer["length"] >= 0,
        "runtime_bootstrap_result_producer_invalid",
    )
    require_hash(producer.get("sha256"), "runtime bootstrap result producer")
    commit = document.get("install_commit_entry")
    commit_data = commit.get("data") if type(commit) is dict else None
    require(
        type(commit) is dict
        and set(commit)
        == {
            "schema",
            "index",
            "transaction_id",
            "plan_hash",
            "event",
            "data",
            "previous_entry_hash",
        }
        and type(commit.get("index")) is int
        and commit["index"] >= 0
        and commit.get("transaction_id") == transaction_id
        and commit.get("plan_hash") == document.get("plan_hash")
        and commit.get("event") == "commit_verified"
        and type(commit_data) is dict
        and set(commit_data)
        == {
            "probe_tree_content_hash",
            "bridge_tree_content_hash",
            "archive_relative",
            "old_probe_recovery",
        }
        and commit_data.get("probe_tree_content_hash")
        == document.get("probe_tree_content_hash")
        and commit_data.get("bridge_tree_content_hash")
        == document.get("bridge_tree_content_hash")
        and commit_data.get("archive_relative") == document.get("archive_relative")
        and commit_data.get("old_probe_recovery")
        == document.get("old_probe_recovery_relative"),
        "runtime_bootstrap_result_commit_invalid",
    )
    require_hash(commit.get("previous_entry_hash"), "runtime bootstrap commit previous hash")
    require(
        sha256_bytes(_canonical_bytes(commit)) == document.get("install_commit_entry_hash"),
        "runtime_bootstrap_result_commit_hash_invalid",
    )
    _validate_result_lock_projection(document, prefix="install", purpose="install")
    _validate_result_lock_projection(document, prefix="ready", purpose="replay")
    require(
        record.get("path_id_lower") == binding.get("runtime_bootstrap_result_path_id_lower")
        and record.get("external_expected_sha256")
        == binding.get("runtime_bootstrap_result_sha256")
        and record.get("result_hash") == binding.get("runtime_bootstrap_result_hash")
        and transaction_id == binding.get("runtime_bootstrap_transaction_id")
        and record.get("probe_tree_content_hash")
        == binding.get("installed_probe_tree_content_hash"),
        "runtime_bootstrap_result_binding_mismatch",
    )
    return document


def parse_strict_object(payload: bytes, label: str, *, maximum: int = MAX_JSON_BYTES) -> dict[str, Any]:
    require(0 < len(payload) <= maximum, "json_size_invalid", label)
    try:
        text = payload.decode("utf-8", "strict")
        value = loads_strict(text)
    except (UnicodeError, CanonicalHashError) as exc:
        raise StartupEvidenceError("json_invalid", label) from exc
    require(type(value) is dict, "json_root_invalid", label)
    return value


def normalized(path: pathlib.Path | str) -> pathlib.Path:
    return pathlib.Path(os.path.abspath(os.path.normpath(str(pathlib.Path(path).expanduser()))))


def _is_reparse(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & REPARSE_ATTRIBUTE
    )


def _path_identity(metadata: os.stat_result) -> tuple[int, int]:
    return int(metadata.st_dev), int(metadata.st_ino)


def identity_id(metadata: os.stat_result) -> str:
    material = (
        f"{int(metadata.st_dev)}:{int(metadata.st_ino)}:"
        f"{int(getattr(metadata, 'st_ctime_ns', int(metadata.st_ctime * 1_000_000_000)))}"
    ).encode("ascii")
    return sha256_bytes(material)


def assert_no_reparse_components(path: pathlib.Path, label: str, *, allow_missing_leaf: bool = False) -> None:
    candidate = normalized(path)
    chain = list(candidate.parents)
    chain.reverse()
    chain.append(candidate)
    for index, component in enumerate(chain):
        if index == 0 and component == component.parent:
            continue
        try:
            metadata = component.lstat()
        except FileNotFoundError:
            if allow_missing_leaf and index == len(chain) - 1:
                return
            raise StartupEvidenceError("path_component_missing", label)
        except OSError as exc:
            raise StartupEvidenceError("path_component_unreadable", label) from exc
        require(not _is_reparse(metadata), "path_contains_reparse", label)


def require_regular_file(path: pathlib.Path, label: str) -> os.stat_result:
    assert_no_reparse_components(path, label)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise StartupEvidenceError("file_unreadable", label) from exc
    require(stat.S_ISREG(metadata.st_mode), "file_not_regular", label)
    require(not _is_reparse(metadata), "file_reparse", label)
    require(int(metadata.st_nlink) == 1, "file_hardlinked", label)
    return metadata


@dataclass(frozen=True)
class Capture:
    payload: bytes
    sha256: str
    length: int
    source_identity_id: str


def capture_exact(path: pathlib.Path, label: str, maximum: int) -> Capture:
    path = normalized(path)
    before = require_regular_file(path, label)
    require(0 < int(before.st_size) <= maximum, "file_size_invalid", label)
    try:
        with path.open("rb", buffering=0) as handle:
            opened = os.fstat(handle.fileno())
            require(_path_identity(opened) == _path_identity(before), "file_identity_changed", label)
            payload = handle.read(maximum + 1)
            require(len(payload) <= maximum, "file_too_large", label)
            handle.seek(0)
            replay = handle.read(len(payload) + 1)
            after = os.fstat(handle.fileno())
    except OSError as exc:
        raise StartupEvidenceError("file_read_failed", label) from exc
    require(payload == replay, "file_content_changed", label)
    require(_path_identity(after) == _path_identity(before), "file_identity_changed", label)
    require(int(after.st_size) == len(payload) == int(before.st_size), "file_size_changed", label)
    require(
        int(getattr(after, "st_mtime_ns", 0)) == int(getattr(before, "st_mtime_ns", 0)),
        "file_timestamp_changed",
        label,
    )
    current = require_regular_file(path, label)
    require(_path_identity(current) == _path_identity(before), "file_path_replaced", label)
    return Capture(payload, sha256_bytes(payload), len(payload), identity_id(before))


def capture_complete_prefix(path: pathlib.Path, label: str, maximum: int) -> Capture:
    path = normalized(path)
    before = require_regular_file(path, label)
    require(0 < int(before.st_size) <= maximum, "prefix_size_invalid", label)
    try:
        with path.open("rb", buffering=0) as handle:
            opened = os.fstat(handle.fileno())
            require(_path_identity(opened) == _path_identity(before), "prefix_identity_changed", label)
            initial = handle.read(int(before.st_size))
            require(len(initial) == int(before.st_size), "prefix_short_read", label)
            newline = initial.rfind(b"\n")
            require(newline >= 0, "prefix_has_no_complete_line", label)
            payload = initial[: newline + 1]
            handle.seek(0)
            replay = handle.read(len(payload))
            after = os.fstat(handle.fileno())
    except OSError as exc:
        raise StartupEvidenceError("prefix_read_failed", label) from exc
    require(payload == replay, "prefix_content_changed", label)
    require(_path_identity(after) == _path_identity(before), "prefix_identity_changed", label)
    require(int(after.st_size) >= len(payload), "prefix_truncated", label)
    current = require_regular_file(path, label)
    require(_path_identity(current) == _path_identity(before), "prefix_path_replaced", label)
    return Capture(payload, sha256_bytes(payload), len(payload), identity_id(before))


def path_id_upper(raw_path: str) -> str:
    require(isinstance(raw_path, str) and bool(raw_path.strip()), "loader_path_invalid")
    require("\x00" not in raw_path and "\r" not in raw_path and "\n" not in raw_path, "loader_path_invalid")
    require(raw_path == raw_path.strip() and not raw_path.startswith("~"), "loader_path_not_absolute")
    require(not raw_path.startswith(("\\\\", "//")), "loader_path_not_local")
    if os.name == "nt":
        drive, tail = ntpath.splitdrive(raw_path)
        require(
            ntpath.isabs(raw_path)
            and len(drive) == 2
            and drive[1] == ":"
            and tail.startswith(("\\", "/")),
            "loader_path_not_absolute",
        )
    else:
        require(os.path.isabs(raw_path), "loader_path_not_absolute")
    path = normalized(raw_path)
    text = str(path).rstrip("\\/")
    require(bool(text), "loader_path_invalid")
    require(not text.startswith("\\\\"), "loader_path_not_local")
    return sha256_bytes(text.upper().encode("utf-8", "strict"))


def _strip_yaml_comment(line: str) -> str:
    quote: str | None = None
    escaped = False
    for index, character in enumerate(line):
        if quote == '"':
            if escaped:
                escaped = False
                continue
            if character == "\\":
                escaped = True
                continue
            if character == '"':
                quote = None
            continue
        if quote == "'":
            if character == "'":
                if index + 1 < len(line) and line[index + 1] == "'":
                    continue
                quote = None
            continue
        if character in {'"', "'"}:
            quote = character
        elif character == "#" and (index == 0 or line[index - 1].isspace()):
            return line[:index]
    require(quote is None, "prefs_yaml_ambiguous", "unterminated quote")
    return line


_PLAIN_YAML_KEY_RE = re.compile(r"^[A-Za-z0-9_./-]+$")


def _require_plain_yaml_mapping(content: str) -> None:
    """Accept only the unquoted block-mapping subset emitted by KPlayerPrefs."""

    require("'" not in content and '"' not in content, "prefs_yaml_ambiguous", "quoted scalar")
    require(":" in content, "prefs_yaml_ambiguous", "non-mapping line")
    key, value = content.split(":", 1)
    require(_PLAIN_YAML_KEY_RE.fullmatch(key) is not None, "prefs_yaml_ambiguous", "non-plain key")
    require(key != "<<", "prefs_yaml_ambiguous", "merge key")
    stripped_value = value.lstrip()
    if stripped_value:
        require(
            not stripped_value.startswith(("&", "*", "!", "[", "{", "|", ">", "?", "%", "---", "...")),
            "prefs_yaml_ambiguous",
            "non-plain value",
        )


@dataclass(frozen=True)
class PrefsEvidence:
    loader_path: str
    loader_path_id_upper: str
    save_filename_line: int
    auto_resume_count: int
    save_filename_count: int


def validate_consumed_prefs(payload: bytes, expected_path_id: str) -> PrefsEvidence:
    require(0 < len(payload) <= MAX_PREFS_BYTES, "prefs_size_invalid")
    try:
        text = payload.decode("utf-8-sig", "strict")
    except UnicodeError as exc:
        raise StartupEvidenceError("prefs_encoding_invalid") from exc
    require("\x00" not in text, "prefs_encoding_invalid")
    logical: list[tuple[int, int, str]] = []
    for line_number, raw in enumerate(text.splitlines(), start=1):
        require("\t" not in raw, "prefs_yaml_ambiguous", "tab")
        cleaned = _strip_yaml_comment(raw).rstrip()
        if not cleaned.strip():
            continue
        indent = len(cleaned) - len(cleaned.lstrip(" "))
        content = cleaned[indent:]
        _require_plain_yaml_mapping(content)
        logical.append((line_number, indent, content))

    strings_headers = [item for item in logical if item[1] == 0 and item[2] == "strings:"]
    require(len(strings_headers) == 1, "prefs_strings_section_invalid")
    header_line = strings_headers[0][0]
    block = [item for item in logical if item[0] > header_line]
    end_index = next((index for index, item in enumerate(block) if item[1] == 0), len(block))
    block = block[:end_index]
    require(bool(block), "prefs_strings_section_invalid")
    direct_indent = min(item[1] for item in block)
    require(direct_indent > 0, "prefs_strings_section_invalid")

    save_matches: list[tuple[int, str]] = []
    auto_count = 0
    target_tokens = ("SaveFilenameKey/", "AutoResumeSaveFile")
    key_re = re.compile(r"^(?P<key>SaveFilenameKey/|AutoResumeSaveFile)\s*:\s*(?P<value>.*)$")
    for line_number, indent, content in logical:
        contains_target = any(token in content for token in target_tokens)
        match = key_re.fullmatch(content)
        if contains_target and match is None:
            raise StartupEvidenceError("prefs_target_key_ambiguous")
        if match is None:
            continue
        key = match.group("key")
        require(indent == direct_indent and line_number > header_line, "prefs_target_key_not_direct")
        require(any(item[0] == line_number for item in block), "prefs_target_key_not_in_strings")
        if key == "AutoResumeSaveFile":
            auto_count += 1
            continue
        value = match.group("value").strip()
        require(bool(value), "prefs_save_filename_empty")
        require(
            not value.startswith(("'", '"', "&", "*", "!", "[", "{", "|", ">")),
            "prefs_save_filename_ambiguous",
        )
        save_matches.append((line_number, value))

    require(auto_count == 0, "prefs_auto_resume_not_consumed")
    require(len(save_matches) == 1, "prefs_save_filename_count_invalid")
    line_number, loader_path = save_matches[0]
    observed_path_id = path_id_upper(loader_path)
    require(observed_path_id == expected_path_id, "prefs_loader_path_mismatch")
    return PrefsEvidence(loader_path, observed_path_id, line_number, auto_count, len(save_matches))


def validate_binding(
    payload: bytes,
    expected_binding_sha256: str,
    expected_runtime_sha256: str,
) -> dict[str, Any]:
    require(sha256_bytes(payload) == require_hash(expected_binding_sha256, "startup binding"), "binding_hash_mismatch")
    document = parse_strict_object(payload, "startup binding", maximum=64 * 1024)
    require(set(document) == BINDING_KEYS, "binding_fields_invalid")
    require(document.get("schema") == BINDING_SCHEMA, "binding_schema_invalid")
    require(RUN_ID_RE.fullmatch(str(document.get("validation_run_id", ""))) is not None, "binding_run_id_invalid")
    require(NONCE_RE.fullmatch(str(document.get("startup_nonce", ""))) is not None, "binding_nonce_invalid")
    for key in {
        "startup_intent_hash",
        "runtime_config_evidence_sha256",
        "environment_manifest_sha256",
        "expected_probe_dll_sha256",
        "runtime_bootstrap_result_path_id_lower",
        "runtime_bootstrap_result_sha256",
        "runtime_bootstrap_result_hash",
        "installed_probe_tree_content_hash",
        "requested_loader_path_id_upper",
        "requested_loader_save_sha256",
        "document_hash",
    }:
        require_hash(document.get(key), f"binding {key}")
    length = document.get("requested_loader_save_length")
    require(type(length) is int and 0 <= length <= (1 << 63) - 1, "binding_save_length_invalid")
    require(document.get("runtime_config_evidence_sha256") == expected_runtime_sha256, "binding_runtime_hash_mismatch")
    require_offset_timestamp(document.get("created_at"), "binding created_at")
    require_nonempty_string(document.get("expected_game_build"), "binding game build")
    require_nonempty_string(document.get("expected_mod_version"), "binding mod version")
    require(
        isinstance(document.get("runtime_bootstrap_transaction_id"), str)
        and RUNTIME_BOOTSTRAP_TRANSACTION_RE.fullmatch(
            document["runtime_bootstrap_transaction_id"]
        )
        is not None,
        "binding_runtime_bootstrap_transaction_invalid",
    )
    require(document.get("document_hash") == json_document_hash(document), "binding_document_hash_mismatch")
    return document


def _intent_value(intent: Mapping[str, Any], *names: str) -> Any:
    present = [name for name in names if name in intent]
    require(len(present) == 1, "startup_intent_fields_invalid", "/".join(names))
    return intent[present[0]]


def validate_runtime_evidence(
    payload: bytes,
    expected_runtime_sha256: str,
    binding: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    require(sha256_bytes(payload) == require_hash(expected_runtime_sha256, "runtime evidence"), "runtime_evidence_hash_mismatch")
    evidence = parse_strict_object(payload, "runtime evidence")
    require(set(evidence) == RUNTIME_EVIDENCE_KEYS, "runtime_evidence_fields_invalid")
    require(evidence.get("schema") == "oni.live-validation-runtime-config.v5", "runtime_evidence_schema_invalid")
    require_hash(evidence.get("document_hash"), "runtime evidence document_hash")
    require(
        evidence.get("document_hash") == json_document_hash(evidence),
        "runtime_evidence_document_hash_mismatch",
    )
    require(evidence.get("run_id") == binding.get("validation_run_id"), "runtime_evidence_run_mismatch")
    require_nonempty_string(evidence.get("mode"), "runtime evidence mode")
    require_nonempty_string(evidence.get("transaction_id"), "runtime evidence transaction id")
    require_offset_timestamp(evidence.get("configured_at"), "runtime evidence configured_at")
    require(
        evidence.get("configured_at") == binding.get("created_at"),
        "runtime_evidence_created_at_mismatch",
    )
    require(
        evidence.get("environment_manifest_sha256") == binding.get("environment_manifest_sha256"),
        "runtime_evidence_manifest_mismatch",
    )
    runtime = evidence.get("runtime")
    require(type(runtime) is dict and set(runtime) == RUNTIME_KEYS, "runtime_fields_invalid")
    require(
        type(runtime.get("bootstrap")) is dict
        and type(runtime.get("release_dll")) is dict
        and type(runtime.get("installed_probe")) is dict
        and type(runtime.get("initial_disabled_policy")) is dict
        and runtime.get("mod_version") == binding.get("expected_mod_version")
        and isinstance(runtime.get("probe_static_id"), str)
        and bool(runtime.get("probe_static_id"))
        and runtime.get("dll_four_way_identical") is True,
        "runtime_anchor_fields_invalid",
    )
    runtime_result = validate_runtime_bootstrap_result_record(
        runtime.get("runtime_bootstrap_result"), binding
    )
    intent = evidence.get("startup_intent")
    require(type(intent) is dict, "startup_intent_missing")
    require(set(intent) == STARTUP_INTENT_KEYS, "startup_intent_fields_invalid")
    require(intent.get("schema") == INTENT_SCHEMA, "startup_intent_schema_invalid")
    require(NONCE_RE.fullmatch(str(intent.get("startup_nonce", ""))) is not None, "startup_intent_nonce_invalid")
    require(
        intent.get("auto_resume_semantics") == "one_shot_nonempty_exact_loader",
        "startup_intent_auto_resume_semantics_invalid",
    )
    require_hash(intent.get("requested_loader_path_id_lower"), "startup intent lower path id")
    for key in {
        "runtime_bootstrap_result_path_id_lower",
        "runtime_bootstrap_result_sha256",
        "runtime_bootstrap_result_hash",
        "installed_probe_tree_content_hash",
    }:
        require_hash(intent.get(key), f"startup intent {key}")
    require(
        isinstance(intent.get("runtime_bootstrap_transaction_id"), str)
        and RUNTIME_BOOTSTRAP_TRANSACTION_RE.fullmatch(
            intent["runtime_bootstrap_transaction_id"]
        )
        is not None,
        "startup_intent_runtime_bootstrap_transaction_invalid",
    )
    require_hash(intent.get("document_hash"), "startup intent document_hash")
    require(intent.get("document_hash") == json_document_hash(intent), "startup_intent_document_hash_mismatch")
    require(intent.get("document_hash") == binding.get("startup_intent_hash"), "startup_intent_hash_mismatch")
    require(intent.get("validation_run_id") == binding.get("validation_run_id"), "startup_intent_run_mismatch")
    require(intent.get("startup_nonce") == binding.get("startup_nonce"), "startup_intent_nonce_mismatch")
    require(
        intent.get("environment_manifest_sha256") == binding.get("environment_manifest_sha256"),
        "startup_intent_manifest_mismatch",
    )
    require(
        _intent_value(intent, "requested_loader_path_id_upper")
        == binding.get("requested_loader_path_id_upper"),
        "startup_intent_path_mismatch",
    )
    require(
        _intent_value(intent, "requested_loader_save_sha256")
        == binding.get("requested_loader_save_sha256"),
        "startup_intent_save_hash_mismatch",
    )
    require(
        _intent_value(intent, "requested_loader_save_length")
        == binding.get("requested_loader_save_length"),
        "startup_intent_save_length_mismatch",
    )
    require(
        _intent_value(intent, "expected_game_build") == binding.get("expected_game_build"),
        "startup_intent_game_build_mismatch",
    )
    require(
        _intent_value(intent, "expected_mod_version") == binding.get("expected_mod_version"),
        "startup_intent_mod_version_mismatch",
    )
    require(
        _intent_value(intent, "expected_probe_dll_sha256")
        == binding.get("expected_probe_dll_sha256"),
        "startup_intent_probe_hash_mismatch",
    )
    for key in {
        "runtime_bootstrap_result_path_id_lower",
        "runtime_bootstrap_result_sha256",
        "runtime_bootstrap_result_hash",
        "runtime_bootstrap_transaction_id",
        "installed_probe_tree_content_hash",
    }:
        require(
            intent.get(key) == binding.get(key),
            "startup_intent_runtime_bootstrap_binding_mismatch",
            key,
        )
    require(
        runtime_result.get("result_hash") == intent.get("runtime_bootstrap_result_hash")
        and runtime_result.get("transaction_id")
        == intent.get("runtime_bootstrap_transaction_id")
        and runtime_result.get("probe_tree_content_hash")
        == intent.get("installed_probe_tree_content_hash"),
        "startup_intent_runtime_bootstrap_result_mismatch",
    )
    return evidence, intent


@dataclass(frozen=True)
class PlayerEvidence:
    loading_line: int
    loaded_line: int
    marker_line: int
    loading_line_sha256: str
    loaded_line_sha256: str
    marker_line_sha256: str


def _line_hash(line: str) -> str:
    return sha256_bytes(line.encode("utf-8", "strict"))


def _parse_marker(line: str) -> dict[str, str] | None:
    prefix = "[ONI AI Probe] STARTUP_BINDING_VERIFIED "
    if not line.startswith(prefix):
        return None
    result: dict[str, str] = {}
    for token in line[len(prefix) :].split(" "):
        require("=" in token, "player_marker_invalid")
        key, value = token.split("=", 1)
        require(bool(key) and bool(value) and key not in result, "player_marker_invalid")
        result[key] = value
    require(
        set(result)
        == {
            "validation_run_id",
            "startup_binding_hash",
            "startup_intent_hash",
            "validation_process_id",
            "requested_save_path_id",
            "active_save_path_id",
        },
        "player_marker_fields_invalid",
    )
    return result


def validate_player_log(
    payload: bytes,
    binding: Mapping[str, Any],
    expected_binding_sha256: str,
    process_id: int,
    loader_path: str,
) -> PlayerEvidence:
    try:
        text = payload.decode("utf-8-sig", "strict")
    except UnicodeError as exc:
        raise StartupEvidenceError("player_log_encoding_invalid") from exc
    require("\x00" not in text, "player_log_encoding_invalid")
    lines = text.splitlines()
    marker_matches: list[tuple[int, dict[str, str]]] = []
    for index, line in enumerate(lines):
        parsed = _parse_marker(line)
        if parsed is None:
            continue
        if (
            parsed.get("validation_run_id") == binding.get("validation_run_id")
            and parsed.get("startup_binding_hash") == expected_binding_sha256
            and parsed.get("startup_intent_hash") == binding.get("startup_intent_hash")
            and parsed.get("validation_process_id") == str(process_id)
            and parsed.get("requested_save_path_id") == binding.get("requested_loader_path_id_upper")
            and parsed.get("active_save_path_id") == binding.get("requested_loader_path_id_upper")
        ):
            marker_matches.append((index, parsed))
    require(len(marker_matches) == 1, "player_verified_marker_count_invalid")
    marker_index = marker_matches[0][0]

    for line in lines:
        if "[ONI AI Probe] STARTUP_BINDING_FAILED" not in line:
            continue
        if (
            f"validation_run_id={binding.get('validation_run_id')}" in line
            or f"validation_process_id={process_id}" in line
        ):
            raise StartupEvidenceError("player_startup_failure_marker_present")

    expected_loading = f"[INFO] Loading save file: {loader_path}"
    expected_loaded = f"[INFO] Loaded [{loader_path}]"
    native_loads = [index for index, line in enumerate(lines[:marker_index]) if line.startswith("[INFO] Loading save file: ")]
    require(bool(native_loads), "player_loading_line_missing")
    loading_index = native_loads[-1]
    require(lines[loading_index] == expected_loading, "player_loading_path_mismatch")
    native_loaded = [
        index
        for index in range(loading_index + 1, marker_index)
        if lines[index].startswith("[INFO] Loaded [")
    ]
    require(len(native_loaded) == 1, "player_loaded_line_count_invalid")
    loaded_index = native_loaded[0]
    require(lines[loaded_index] == expected_loaded, "player_loaded_path_mismatch")
    require(loading_index < loaded_index < marker_index, "player_load_order_invalid")
    return PlayerEvidence(
        loading_index + 1,
        loaded_index + 1,
        marker_index + 1,
        _line_hash(lines[loading_index]),
        _line_hash(lines[loaded_index]),
        _line_hash(lines[marker_index]),
    )


def safe_session_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value)[:120] or "waiting"


def parse_archive_records(payload: bytes) -> list[dict[str, Any]]:
    require(bool(payload) and payload.endswith(b"\n"), "archive_incomplete_line")
    records: list[dict[str, Any]] = []
    for raw in payload.splitlines():
        require(0 < len(raw) <= MAX_RECORD_BYTES and bool(raw.strip()), "archive_record_size_invalid")
        records.append(parse_strict_object(raw, "session archive record", maximum=MAX_RECORD_BYTES))
    require(bool(records), "archive_empty")
    return records


def _archive_timestamp(value: Any) -> dt.datetime:
    text = require_offset_timestamp(value, "archive record timestamp")
    try:
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise StartupEvidenceError("archive_timestamp_invalid") from exc


def _record_schema_valid(record: Mapping[str, Any]) -> bool:
    event = record.get("event")
    return isinstance(event, str) and bool(event) and record.get("schema") == (
        "oni.state.v2" if event == "state_snapshot" else "oni.event.v2"
    )


def validate_archive_records(
    records: Sequence[Mapping[str, Any]],
    binding: Mapping[str, Any],
    expected_binding_sha256: str,
    process_id: int,
) -> tuple[str, int]:
    first = records[0]
    session_id = require_nonempty_string(first.get("session_id"), "session id")
    require(SESSION_ID_RE.fullmatch(session_id) is not None, "session_id_invalid")
    expected_path_id = binding.get("requested_loader_path_id_upper")
    required_fields = {
        "validation_run_id": binding.get("validation_run_id"),
        "startup_binding_hash": expected_binding_sha256,
        "startup_intent_hash": binding.get("startup_intent_hash"),
        "runtime_config_evidence_sha256": binding.get("runtime_config_evidence_sha256"),
        "validation_process_id": process_id,
        "startup_binding_status": "verified",
        "requested_save_path_id": expected_path_id,
        "active_save_path_id": expected_path_id,
        "game_build": binding.get("expected_game_build"),
        "mod_version": binding.get("expected_mod_version"),
    }
    snapshot_count = 0
    previous_time: dt.datetime | None = None
    for expected_sequence, record in enumerate(records, start=1):
        require(record.get("session_id") == session_id, "archive_session_mismatch")
        require(type(record.get("seq")) is int and record.get("seq") == expected_sequence, "archive_sequence_invalid")
        require(_record_schema_valid(record), "archive_record_schema_invalid")
        current_time = _archive_timestamp(record.get("ts"))
        require(previous_time is None or current_time >= previous_time, "archive_timestamp_order_invalid")
        previous_time = current_time
        for key, expected in required_fields.items():
            require(record.get(key) == expected, "archive_startup_field_mismatch", key)
        require(
            record.get("save_id_hash") == binding.get("requested_loader_save_sha256")
            and record.get("save_dirty") is False,
            "archive_startup_save_identity_mismatch",
        )
        require(record.get("event") != "save_write_completed", "archive_save_transition_before_origin_snapshot")
        if record.get("event") == "state_snapshot" and record.get("schema") == "oni.state.v2":
            snapshot_count += 1
    require(
        first.get("schema") == "oni.event.v2"
        and first.get("event") == "session_started"
        and first.get("reason") == "save_loader_load_complete",
        "archive_session_start_invalid",
    )
    require(first.get("save_id_hash") == binding.get("requested_loader_save_sha256"), "archive_initial_save_hash_mismatch")
    require(
        len(records) >= 2
        and records[1].get("schema") == "oni.event.v2"
        and records[1].get("event") == "save_loaded",
        "archive_save_loaded_missing",
    )
    require(
        snapshot_count == 1
        and records[-1].get("event") == "state_snapshot"
        and records[-1].get("snapshot_id") == f"{session_id}:{records[-1].get('seq')}"
        and type(records[-1].get("snapshot_seq")) is int
        and int(records[-1].get("snapshot_seq")) > 0,
        "archive_minimal_prefix_invalid",
    )
    return session_id, snapshot_count


def validate_source_archive_records(
    records: Sequence[Mapping[str, Any]],
    binding: Mapping[str, Any],
    expected_binding_sha256: str,
    process_id: int,
) -> tuple[str, int, int]:
    """Validate a complete origin archive while allowing its Save-As suffix."""

    require(bool(records), "archive_empty")
    first = records[0]
    session_id = require_nonempty_string(first.get("session_id"), "session id")
    require(SESSION_ID_RE.fullmatch(session_id) is not None, "session_id_invalid")
    loader_path_id = binding.get("requested_loader_path_id_upper")
    sticky_fields = {
        "validation_run_id": binding.get("validation_run_id"),
        "startup_binding_hash": expected_binding_sha256,
        "startup_intent_hash": binding.get("startup_intent_hash"),
        "runtime_config_evidence_sha256": binding.get("runtime_config_evidence_sha256"),
        "validation_process_id": process_id,
        "startup_binding_status": "verified",
        "game_build": binding.get("expected_game_build"),
        "mod_version": binding.get("expected_mod_version"),
    }
    previous_time: dt.datetime | None = None
    first_snapshot_index: int | None = None
    current_active_path_id = loader_path_id
    current_save_hash = binding.get("requested_loader_save_sha256")
    save_transition_count = 0
    save_start_count = 0
    session_end_count = 0
    pending_save_path_id: str | None = None
    pending_before_save_hash: str | None = None
    for expected_sequence, record in enumerate(records, start=1):
        require(record.get("session_id") == session_id, "archive_session_mismatch")
        require(
            type(record.get("seq")) is int and record.get("seq") == expected_sequence,
            "archive_sequence_invalid",
        )
        require(_record_schema_valid(record), "archive_record_schema_invalid")
        current_time = _archive_timestamp(record.get("ts"))
        require(previous_time is None or current_time >= previous_time, "archive_timestamp_order_invalid")
        previous_time = current_time
        for key, expected in sticky_fields.items():
            require(record.get(key) == expected, "archive_startup_field_mismatch", key)
        require_hash(record.get("save_id_hash"), "archive save id")
        require_hash(record.get("active_save_path_id"), "archive active save path id")
        if record.get("event") == "save_write_started":
            save_start_count += 1
            require(first_snapshot_index is not None, "archive_save_transition_before_origin_snapshot")
            require(pending_save_path_id is None, "archive_save_transition_nested")
            requested_path_id = require_hash(
                record.get("requested_save_path_id"), "archive save start requested path"
            )
            require(
                record.get("active_save_path_id") == current_active_path_id
                and record.get("before_save_id_hash") == current_save_hash
                and record.get("save_id_hash") == current_save_hash
                and record.get("save_dirty") is True
                and record.get("is_auto_save") is False
                and record.get("update_save_pointer") is True,
                "archive_save_start_invalid",
            )
            pending_save_path_id = requested_path_id
            pending_before_save_hash = current_save_hash
        elif record.get("event") == "save_write_completed":
            save_transition_count += 1
            for field in {
                "requested_save_path_id",
                "result_save_path_id",
                "active_save_path_id",
                "before_save_id_hash",
                "written_save_id_hash",
                "active_save_id_hash",
            }:
                require_hash(record.get(field), f"archive save transition {field}")
            require(
                record.get("requested_save_path_id") == record.get("result_save_path_id")
                == record.get("active_save_path_id")
                == pending_save_path_id
                and record.get("before_save_id_hash") == current_save_hash
                == pending_before_save_hash
                and record.get("written_save_id_hash") == record.get("active_save_id_hash")
                == record.get("save_id_hash")
                and record.get("save_dirty") is False
                and record.get("success") is True
                and record.get("is_auto_save") is False
                and record.get("update_save_pointer") is True
                and record.get("file_rewrite_observed") is True
                and record.get("save_file_readable") is True,
                "archive_save_transition_invalid",
            )
            require(first_snapshot_index is not None, "archive_save_transition_before_origin_snapshot")
            current_active_path_id = record.get("active_save_path_id")
            current_save_hash = record.get("save_id_hash")
            pending_save_path_id = None
            pending_before_save_hash = None
        else:
            require(record.get("save_dirty") is False, "archive_save_dirty_invalid")
            require(
                record.get("requested_save_path_id") == loader_path_id,
                "archive_requested_loader_path_mismatch",
            )
            require(
                record.get("active_save_path_id") == current_active_path_id
                and record.get("save_id_hash") == current_save_hash,
                "archive_active_save_chain_mismatch",
            )
        if record.get("event") == "session_ended":
            session_end_count += 1
            require(expected_sequence == len(records), "archive_session_end_order_invalid")
        if first_snapshot_index is None and record.get("event") == "state_snapshot":
            first_snapshot_index = expected_sequence - 1
    require(first_snapshot_index is not None, "archive_state_snapshot_missing")
    prefix_count = int(first_snapshot_index) + 1
    prefix_session, snapshot_count = validate_archive_records(
        records[:prefix_count], binding, expected_binding_sha256, process_id
    )
    require(prefix_session == session_id, "archive_session_mismatch")
    require(
        save_start_count == 1
        and save_transition_count == 1
        and pending_save_path_id is None,
        "archive_save_transition_count_invalid",
    )
    require(session_end_count == 1, "archive_session_end_count_invalid")
    return session_id, prefix_count, snapshot_count


def _origin_first_record_matches(
    first: Mapping[str, Any],
    binding: Mapping[str, Any],
    expected_binding_sha256: str,
    process_id: int,
) -> bool:
    loader_path_id = binding.get("requested_loader_path_id_upper")
    return (
        first.get("validation_run_id") == binding.get("validation_run_id")
        and first.get("startup_binding_hash") == expected_binding_sha256
        and first.get("startup_intent_hash") == binding.get("startup_intent_hash")
        and first.get("runtime_config_evidence_sha256")
        == binding.get("runtime_config_evidence_sha256")
        and first.get("validation_process_id") == process_id
        and first.get("startup_binding_status") == "verified"
        and first.get("requested_save_path_id") == loader_path_id
        and first.get("active_save_path_id") == loader_path_id
        and first.get("save_id_hash") == binding.get("requested_loader_save_sha256")
        and first.get("game_build") == binding.get("expected_game_build")
        and first.get("mod_version") == binding.get("expected_mod_version")
    )


def _prefix_capture(
    archive_capture: Capture,
    records: Sequence[Mapping[str, Any]],
    prefix_count: int,
) -> Capture:
    raw_lines = archive_capture.payload.splitlines(keepends=True)
    require(
        len(raw_lines) == len(records)
        and 0 < prefix_count <= len(raw_lines)
        and all(line.endswith(b"\n") and bool(line.strip()) for line in raw_lines),
        "archive_raw_lines_invalid",
    )
    payload = b"".join(raw_lines[:prefix_count])
    return Capture(
        payload,
        sha256_bytes(payload),
        len(payload),
        archive_capture.source_identity_id,
    )


@dataclass(frozen=True)
class ArchiveDiscovery:
    path: pathlib.Path
    prefix_capture: Capture
    prefix_records: list[dict[str, Any]]
    session_id: str
    snapshot_count: int
    source_archive: dict[str, Any]


def discover_session_archive(
    bridge_dir: pathlib.Path,
    binding: Mapping[str, Any],
    expected_binding_sha256: str,
    process_id: int,
) -> ArchiveDiscovery:
    sessions = normalized(bridge_dir) / "sessions"
    assert_no_reparse_components(sessions, "Bridge sessions")
    require(sessions.is_dir(), "bridge_sessions_missing")
    try:
        entries = sorted(sessions.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise StartupEvidenceError("bridge_sessions_unreadable") from exc
    require(len(entries) <= 128, "bridge_session_count_excessive")
    matches: list[ArchiveDiscovery] = []
    for entry in entries:
        metadata = entry.lstat()
        require(stat.S_ISDIR(metadata.st_mode) and not _is_reparse(metadata), "bridge_session_entry_invalid")
        state_path = entry / "state.jsonl"
        if not state_path.exists():
            continue
        archive_capture = capture_complete_prefix(
            state_path, "Bridge session archive", MAX_ARCHIVE_BYTES
        )
        first_raw = archive_capture.payload.splitlines()[0]
        first = parse_strict_object(
            first_raw, "session archive first record", maximum=MAX_RECORD_BYTES
        )
        if (
            first.get("validation_run_id") != binding.get("validation_run_id")
            or first.get("startup_binding_hash") != expected_binding_sha256
            or first.get("validation_process_id") != process_id
        ):
            continue
        session_value = first.get("session_id")
        is_probe_process_archive = (
            isinstance(session_value, str)
            and session_value.startswith("process-")
            and entry.name == safe_session_name(session_value)
            and first.get("schema") == "oni.event.v2"
            and first.get("event") != "session_started"
            and type(first.get("seq")) is int
            and first.get("seq") == 1
        )
        if is_probe_process_archive:
            continue
        if not _origin_first_record_matches(
            first, binding, expected_binding_sha256, process_id
        ):
            continue
        records = parse_archive_records(archive_capture.payload)
        session_id, prefix_count, snapshots = validate_source_archive_records(
            records, binding, expected_binding_sha256, process_id
        )
        require(entry.name == safe_session_name(session_id), "archive_session_path_mismatch")
        prefix_capture = _prefix_capture(archive_capture, records, prefix_count)
        record_hashes = [canonical_hash(dict(record)) for record in records]
        matches.append(
            ArchiveDiscovery(
                state_path,
                prefix_capture,
                [dict(record) for record in records[:prefix_count]],
                session_id,
                snapshots,
                {
                    "session_id": session_id,
                    "source_identity_id": archive_capture.source_identity_id,
                    "file_sha256": archive_capture.sha256,
                    "byte_length": archive_capture.length,
                    "record_count": len(records),
                    "first_seq": 1,
                    "last_seq": len(records),
                    "record_chain_hash": canonical_hash(record_hashes),
                    "prefix_record_count": prefix_count,
                    "prefix_file_sha256": prefix_capture.sha256,
                    "prefix_byte_length": prefix_capture.length,
                },
            )
        )
    require(len(matches) == 1, "bridge_session_archive_count_invalid")
    return matches[0]


@dataclass(frozen=True)
class ProcessEvidence:
    process_id: int
    image_name: str
    creation_token: str
    image_path_id_upper: str
    image_file_sha256: str
    image_path: str = field(repr=False)


def process_image_path_id_upper(raw_path: str) -> str:
    require(isinstance(raw_path, str) and bool(raw_path), "validation_process_image_path_invalid")
    require("\x00" not in raw_path and "\r" not in raw_path and "\n" not in raw_path, "validation_process_image_path_invalid")
    if os.name == "nt":
        drive, tail = ntpath.splitdrive(raw_path)
        require(
            ntpath.isabs(raw_path)
            and len(drive) == 2
            and drive[1] == ":"
            and tail.startswith(("\\", "/")),
            "validation_process_image_path_invalid",
        )
        normalized_path = ntpath.normpath(raw_path)
    else:
        require(os.path.isabs(raw_path), "validation_process_image_path_invalid")
        normalized_path = os.path.normpath(raw_path)
    return sha256_bytes(normalized_path.rstrip("\\/").upper().encode("utf-8", "strict"))


def process_image_file_sha256(raw_path: str) -> str:
    capture = capture_exact(pathlib.Path(raw_path), "validation process image", MAX_PROCESS_IMAGE_BYTES)
    return capture.sha256


def _windows_process_evidence(process_id: int) -> ProcessEvidence:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    open_process.restype = ctypes.c_void_p
    query_image = kernel32.QueryFullProcessImageNameW
    query_image.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint32)]
    query_image.restype = ctypes.c_int
    get_times = kernel32.GetProcessTimes
    get_times.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    get_times.restype = ctypes.c_int
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int

    handle = open_process(0x1000, 0, process_id)
    require(bool(handle), "validation_process_unavailable")
    try:
        size = ctypes.c_uint32(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        require(bool(query_image(handle, 0, buffer, ctypes.byref(size))), "validation_process_image_unavailable")
        filetime = ctypes.c_uint64()
        exit_time = ctypes.c_uint64()
        kernel_time = ctypes.c_uint64()
        user_time = ctypes.c_uint64()
        require(
            bool(
                get_times(
                    handle,
                    ctypes.byref(filetime),
                    ctypes.byref(exit_time),
                    ctypes.byref(kernel_time),
                    ctypes.byref(user_time),
                )
            ),
            "validation_process_time_unavailable",
        )
        image_path = buffer.value
        creation = int(filetime.value)
    finally:
        close_handle(handle)
    name = pathlib.Path(image_path).name
    return ProcessEvidence(
        process_id,
        name,
        sha256_bytes(f"{process_id}:{creation}:{image_path.upper()}".encode("utf-8", "strict")),
        process_image_path_id_upper(image_path),
        process_image_file_sha256(image_path),
        image_path,
    )


def _posix_process_evidence(process_id: int) -> ProcessEvidence:
    proc = pathlib.Path("/proc") / str(process_id)
    try:
        image_path = os.readlink(proc / "exe")
        stat_fields = (proc / "stat").read_text(encoding="ascii").split()
        creation = stat_fields[21]
    except (OSError, IndexError, UnicodeError) as exc:
        raise StartupEvidenceError("validation_process_unavailable") from exc
    name = pathlib.Path(image_path).name
    return ProcessEvidence(
        process_id,
        name,
        sha256_bytes(f"{process_id}:{creation}:{image_path.upper()}".encode("utf-8", "strict")),
        process_image_path_id_upper(image_path),
        process_image_file_sha256(image_path),
        image_path,
    )


def query_process(
    process_id: int,
    expected_image_name: str,
    expected_image_path_id_upper: str,
    expected_image_sha256: str,
) -> ProcessEvidence:
    require(type(process_id) is int and process_id > 0, "validation_process_id_invalid")
    require(isinstance(expected_image_name, str) and bool(expected_image_name), "validation_process_image_invalid")
    expected_path_id = require_hash(expected_image_path_id_upper, "process image path id")
    expected_file_hash = require_hash(expected_image_sha256, "process image")
    value = _windows_process_evidence(process_id) if os.name == "nt" else _posix_process_evidence(process_id)
    require(value.image_name.casefold() == expected_image_name.casefold(), "validation_process_image_mismatch")
    require(value.image_path_id_upper == expected_path_id, "validation_process_image_path_mismatch")
    require(value.image_file_sha256 == expected_file_hash, "validation_process_image_hash_mismatch")
    return value


def process_document(value: ProcessEvidence) -> dict[str, Any]:
    return {
        "process_id": value.process_id,
        "image_name": value.image_name,
        "creation_token": value.creation_token,
        "image_path_id_upper": value.image_path_id_upper,
        "image_file_sha256": value.image_file_sha256,
    }


def artifact_document(capture: Capture, file_name: str) -> dict[str, Any]:
    return {
        "file": file_name,
        "sha256": capture.sha256,
        "length": capture.length,
        "source_identity_id": capture.source_identity_id,
    }


@dataclass(frozen=True)
class ValidatedMaterial:
    binding: dict[str, Any]
    intent: dict[str, Any]
    prefs: PrefsEvidence
    player: PlayerEvidence
    session_id: str
    snapshot_count: int
    record_count: int


def validate_material(
    captures: Mapping[str, Capture],
    *,
    expected_runtime_sha256: str,
    expected_binding_sha256: str,
    process_id: int,
) -> ValidatedMaterial:
    binding = validate_binding(
        captures["startup_binding"].payload,
        expected_binding_sha256,
        expected_runtime_sha256,
    )
    _evidence, intent = validate_runtime_evidence(
        captures["runtime_config_evidence"].payload,
        expected_runtime_sha256,
        binding,
    )
    prefs = validate_consumed_prefs(
        captures["consumed_prefs"].payload,
        binding["requested_loader_path_id_upper"],
    )
    player = validate_player_log(
        captures["player_log_prefix"].payload,
        binding,
        expected_binding_sha256,
        process_id,
        prefs.loader_path,
    )
    records = parse_archive_records(captures["session_archive_prefix"].payload)
    session_id, snapshots = validate_archive_records(records, binding, expected_binding_sha256, process_id)
    return ValidatedMaterial(binding, intent, prefs, player, session_id, snapshots, len(records))


def build_report(
    captures: Mapping[str, Capture],
    material: ValidatedMaterial,
    process: ProcessEvidence,
    source_archive: Mapping[str, Any],
    *,
    expected_runtime_sha256: str,
    expected_binding_sha256: str,
    created_at: str,
) -> dict[str, Any]:
    binding = material.binding
    require(
        type(source_archive) is dict and set(source_archive) == SOURCE_ARCHIVE_KEYS,
        "source_archive_projection_invalid",
    )
    require(source_archive.get("session_id") == material.session_id, "source_archive_session_mismatch")
    require_hash(source_archive.get("source_identity_id"), "source archive identity")
    require_hash(source_archive.get("file_sha256"), "source archive file")
    require_hash(source_archive.get("record_chain_hash"), "source archive record chain")
    require(
        type(source_archive.get("byte_length")) is int
        and source_archive.get("byte_length") >= captures["session_archive_prefix"].length,
        "source_archive_length_invalid",
    )
    require(
        type(source_archive.get("record_count")) is int
        and source_archive.get("record_count") >= material.record_count,
        "source_archive_record_count_invalid",
    )
    require(
        source_archive.get("first_seq") == 1
        and source_archive.get("last_seq") == source_archive.get("record_count"),
        "source_archive_sequence_invalid",
    )
    require(
        source_archive.get("prefix_record_count") == material.record_count
        and source_archive.get("prefix_file_sha256") == captures["session_archive_prefix"].sha256
        and source_archive.get("prefix_byte_length") == captures["session_archive_prefix"].length,
        "source_archive_prefix_mismatch",
    )
    report: dict[str, Any] = {
        "schema": EVIDENCE_SCHEMA,
        "status": "passed",
        "classification": "startup_consumption_verified",
        "verification_mode": "live_create",
        "fail_closed": True,
        "validation_run_id": binding["validation_run_id"],
        "startup_intent_hash": binding["startup_intent_hash"],
        "runtime_config_evidence_sha256": expected_runtime_sha256,
        "startup_binding_sha256": expected_binding_sha256,
        "process": process_document(process),
        "session": {
            "session_id": material.session_id,
            "initial_sequence": 1,
            "record_count": material.record_count,
            "state_snapshot_count": material.snapshot_count,
            "start_reason": "save_loader_load_complete",
        },
        "loader": {
            "path_id_upper": binding["requested_loader_path_id_upper"],
            "initial_save_sha256": binding["requested_loader_save_sha256"],
            "initial_save_length": binding["requested_loader_save_length"],
            "expected_game_build": binding["expected_game_build"],
            "expected_mod_version": binding["expected_mod_version"],
            "expected_probe_dll_sha256": binding["expected_probe_dll_sha256"],
        },
        "consumed_prefs": {
            "save_filename_key_count": material.prefs.save_filename_count,
            "auto_resume_key_count": material.prefs.auto_resume_count,
            "save_filename_line": material.prefs.save_filename_line,
            "value_path_id_upper": material.prefs.loader_path_id_upper,
        },
        "player_log": {
            "loading_line": material.player.loading_line,
            "loaded_line": material.player.loaded_line,
            "verified_marker_line": material.player.marker_line,
            "loading_line_sha256": material.player.loading_line_sha256,
            "loaded_line_sha256": material.player.loaded_line_sha256,
            "verified_marker_line_sha256": material.player.marker_line_sha256,
        },
        "source_archive": dict(source_archive),
        "artifacts": {
            key: artifact_document(captures[key], file_name)
            for key, file_name in ARTIFACT_FILES.items()
        },
        "created_at": created_at,
    }
    report["evidence_hash"] = canonical_hash(report)
    return report


def json_bytes(document: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        + "\n"
    ).encode("utf-8", "strict")


def _unlink_owned_regular_file(path: pathlib.Path, expected_identity: tuple[int, int]) -> None:
    try:
        metadata = path.lstat()
    except OSError:
        return
    if (
        stat.S_ISREG(metadata.st_mode)
        and not _is_reparse(metadata)
        and int(metadata.st_nlink) == 1
        and _path_identity(metadata) == expected_identity
    ):
        try:
            path.chmod(stat.S_IWRITE | stat.S_IREAD)
            path.unlink()
        except OSError:
            return


def _write_create_only(path: pathlib.Path, payload: bytes) -> tuple[int, int]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    descriptor = os.open(path, flags, 0o600)
    opened_identity: tuple[int, int] | None = None
    try:
        opened_identity = _path_identity(os.fstat(descriptor))
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            require(written > 0, "bundle_write_failed")
            view = view[written:]
        os.fsync(descriptor)
        metadata = os.fstat(descriptor)
        require(_path_identity(metadata) == opened_identity, "bundle_file_identity_changed")
    except BaseException:
        try:
            os.close(descriptor)
        finally:
            if opened_identity is not None:
                _unlink_owned_regular_file(path, opened_identity)
        raise
    try:
        os.close(descriptor)
    except OSError as exc:
        if opened_identity is not None:
            _unlink_owned_regular_file(path, opened_identity)
        raise StartupEvidenceError("bundle_file_close_failed") from exc
    require(opened_identity is not None, "bundle_file_identity_changed")
    return opened_identity


def _require_directory_identity(
    directory: pathlib.Path,
    expected_identity: tuple[int, int],
    code: str,
) -> os.stat_result:
    try:
        metadata = directory.lstat()
    except OSError as exc:
        raise StartupEvidenceError(code) from exc
    require(
        stat.S_ISDIR(metadata.st_mode)
        and not _is_reparse(metadata)
        and _path_identity(metadata) == expected_identity,
        code,
    )
    return metadata


def _require_owned_bundle(
    directory: pathlib.Path,
    directory_identity: tuple[int, int],
    owned: Mapping[str, tuple[int, int]],
    code: str,
) -> None:
    _require_directory_identity(directory, directory_identity, code)
    try:
        entries = {entry.name: entry for entry in directory.iterdir()}
    except OSError as exc:
        raise StartupEvidenceError(code) from exc
    require(set(entries) == set(owned), code)
    for name, expected_identity in owned.items():
        try:
            metadata = entries[name].lstat()
        except OSError as exc:
            raise StartupEvidenceError(code) from exc
        require(
            stat.S_ISREG(metadata.st_mode)
            and not _is_reparse(metadata)
            and int(metadata.st_nlink) == 1
            and _path_identity(metadata) == expected_identity,
            code,
        )


def _cleanup_owned_temp(directory: pathlib.Path, directory_identity: tuple[int, int], owned: Mapping[str, tuple[int, int]]) -> None:
    try:
        _require_owned_bundle(directory, directory_identity, owned, "bundle_cleanup_identity_changed")
    except StartupEvidenceError:
        return
    for name in owned:
        target = directory / name
        try:
            target.chmod(stat.S_IWRITE | stat.S_IREAD)
            target.unlink()
        except OSError:
            return
    try:
        directory.rmdir()
    except OSError:
        return


def publish_bundle(output_dir: pathlib.Path, captures: Mapping[str, Capture], report: Mapping[str, Any]) -> None:
    output_dir = normalized(output_dir)
    parent = output_dir.parent
    assert_no_reparse_components(parent, "bundle parent")
    require(parent.is_dir(), "bundle_parent_invalid")
    parent_identity = _path_identity(parent.lstat())
    assert_no_reparse_components(output_dir, "bundle output", allow_missing_leaf=True)
    require(not os.path.lexists(output_dir), "bundle_already_exists")
    temporary = parent / f".{output_dir.name}.tmp-{uuid.uuid4().hex}"
    os.mkdir(temporary, 0o700)
    directory_metadata = temporary.lstat()
    directory_identity = _path_identity(directory_metadata)
    owned: dict[str, tuple[int, int]] = {}
    try:
        for key, file_name in ARTIFACT_FILES.items():
            owned[file_name] = _write_create_only(temporary / file_name, captures[key].payload)
        owned[REPORT_FILE] = _write_create_only(temporary / REPORT_FILE, json_bytes(report))
        for name in (*ARTIFACT_FILES.values(), REPORT_FILE):
            try:
                (temporary / name).chmod(stat.S_IREAD)
            except OSError as exc:
                raise StartupEvidenceError("bundle_readonly_failed") from exc
        _require_directory_identity(parent, parent_identity, "bundle_parent_identity_changed")
        _require_owned_bundle(temporary, directory_identity, owned, "bundle_source_identity_changed")
        require(not os.path.lexists(output_dir), "bundle_already_exists")
        require(os.name == "nt", "bundle_atomic_noreplace_unsupported")
        try:
            # On the Windows target, rename does not replace an existing
            # directory.  The pre-check is advisory; this operation is the
            # create-only collision boundary.
            os.rename(temporary, output_dir)
        except OSError as exc:
            if isinstance(exc, FileExistsError) or exc.errno == errno.EEXIST or getattr(exc, "winerror", None) == 183:
                raise StartupEvidenceError("bundle_already_exists") from exc
            raise
        _require_directory_identity(parent, parent_identity, "bundle_parent_identity_changed")
        require(not os.path.lexists(temporary), "bundle_source_path_reappeared")
        _require_owned_bundle(output_dir, directory_identity, owned, "bundle_destination_identity_changed")
    except Exception:
        _cleanup_owned_temp(temporary, directory_identity, owned)
        _cleanup_owned_temp(output_dir, directory_identity, owned)
        raise


def capture_live_sources(
    args: argparse.Namespace,
) -> tuple[dict[str, Capture], ProcessEvidence, ValidatedMaterial, dict[str, Any]]:
    expected_runtime = require_hash(args.expected_runtime_config_evidence_sha256, "runtime evidence")
    expected_binding = require_hash(args.expected_startup_binding_sha256, "startup binding")
    process_before = query_process(
        args.expected_process_id,
        args.expected_process_image_name,
        args.expected_process_image_path_id_upper,
        args.expected_process_image_sha256,
    )
    captures: dict[str, Capture] = {
        "runtime_config_evidence": capture_exact(
            normalized(args.runtime_config_evidence), "runtime evidence", MAX_JSON_BYTES
        ),
        "startup_binding": capture_exact(normalized(args.startup_binding), "startup binding", 64 * 1024),
        "consumed_prefs": capture_exact(normalized(args.consumed_prefs), "consumed prefs", MAX_PREFS_BYTES),
        "player_log_prefix": capture_complete_prefix(
            normalized(args.player_log), "Player.log", MAX_PLAYER_LOG_BYTES
        ),
    }
    binding = validate_binding(captures["startup_binding"].payload, expected_binding, expected_runtime)
    discovery = discover_session_archive(
        normalized(args.bridge_dir), binding, expected_binding, args.expected_process_id
    )
    captures["session_archive_prefix"] = discovery.prefix_capture
    material = validate_material(
        captures,
        expected_runtime_sha256=expected_runtime,
        expected_binding_sha256=expected_binding,
        process_id=args.expected_process_id,
    )
    process_after = query_process(
        args.expected_process_id,
        args.expected_process_image_name,
        args.expected_process_image_path_id_upper,
        args.expected_process_image_sha256,
    )
    require(process_before == process_after, "validation_process_changed")
    return captures, process_before, material, discovery.source_archive


def create_command(args: argparse.Namespace) -> dict[str, Any]:
    captures, process, material, source_archive = capture_live_sources(args)
    created_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds")
    report = build_report(
        captures,
        material,
        process,
        source_archive,
        expected_runtime_sha256=args.expected_runtime_config_evidence_sha256,
        expected_binding_sha256=args.expected_startup_binding_sha256,
        created_at=created_at,
    )
    publish_bundle(normalized(args.output_dir), captures, report)
    return report


def _capture_bundle_file(bundle: pathlib.Path, name: str, maximum: int) -> Capture:
    return capture_exact(bundle / name, f"bundle {name}", maximum)


def verify_command(args: argparse.Namespace) -> dict[str, Any]:
    bundle = normalized(args.bundle_dir)
    assert_no_reparse_components(bundle, "bundle")
    require(bundle.is_dir(), "bundle_invalid")
    expected_names = set(ARTIFACT_FILES.values()) | {REPORT_FILE}
    try:
        actual_names = {entry.name for entry in bundle.iterdir()}
    except OSError as exc:
        raise StartupEvidenceError("bundle_unreadable") from exc
    require(actual_names == expected_names, "bundle_entries_invalid")
    report_capture = _capture_bundle_file(bundle, REPORT_FILE, MAX_JSON_BYTES)
    report = parse_strict_object(report_capture.payload, "startup evidence report")
    require(set(report) == EVIDENCE_KEYS, "evidence_fields_invalid")
    require(report.get("schema") == EVIDENCE_SCHEMA, "evidence_schema_invalid")
    require(report.get("status") == "passed" and report.get("fail_closed") is True, "evidence_status_invalid")
    claimed_hash = require_hash(report.get("evidence_hash"), "evidence hash")
    unhashed = dict(report)
    del unhashed["evidence_hash"]
    require(claimed_hash == canonical_hash(unhashed), "evidence_hash_mismatch")
    artifacts = report.get("artifacts")
    require(type(artifacts) is dict and set(artifacts) == set(ARTIFACT_FILES), "evidence_artifacts_invalid")
    captures: dict[str, Capture] = {}
    maximums = {
        "runtime_config_evidence": MAX_JSON_BYTES,
        "startup_binding": 64 * 1024,
        "consumed_prefs": MAX_PREFS_BYTES,
        "player_log_prefix": MAX_PLAYER_LOG_BYTES,
        "session_archive_prefix": MAX_ARCHIVE_BYTES,
    }
    for key, file_name in ARTIFACT_FILES.items():
        capture = _capture_bundle_file(bundle, file_name, maximums[key])
        record = artifacts.get(key)
        require(type(record) is dict, "evidence_artifact_record_invalid", key)
        require(record.get("file") == file_name, "evidence_artifact_file_invalid", key)
        require(record.get("sha256") == capture.sha256, "evidence_artifact_hash_mismatch", key)
        require(record.get("length") == capture.length, "evidence_artifact_length_mismatch", key)
        source_identity = require_hash(
            record.get("source_identity_id"), "evidence artifact source identity"
        )
        # The immutable bundle file has a new filesystem identity.  Replay the
        # original capture identity from the self-hashed report only after the
        # bundle payload hash and length have been independently rechecked.
        captures[key] = Capture(
            capture.payload,
            capture.sha256,
            capture.length,
            source_identity,
        )

    process_record = report.get("process")
    require(
        type(process_record) is dict and set(process_record) == PROCESS_EVIDENCE_KEYS,
        "evidence_process_invalid",
    )
    process_id = process_record.get("process_id")
    require(type(process_id) is int and process_id > 0, "evidence_process_invalid")
    expected_runtime = require_hash(report.get("runtime_config_evidence_sha256"), "runtime evidence")
    expected_binding = require_hash(report.get("startup_binding_sha256"), "startup binding")
    material = validate_material(
        captures,
        expected_runtime_sha256=expected_runtime,
        expected_binding_sha256=expected_binding,
        process_id=process_id,
    )
    replay_process = ProcessEvidence(
        process_id,
        require_nonempty_string(process_record.get("image_name"), "process image"),
        require_hash(process_record.get("creation_token"), "process creation"),
        require_hash(process_record.get("image_path_id_upper"), "process image path id"),
        require_hash(process_record.get("image_file_sha256"), "process image"),
        "",
    )
    expected_report = build_report(
        captures,
        material,
        replay_process,
        report.get("source_archive"),
        expected_runtime_sha256=expected_runtime,
        expected_binding_sha256=expected_binding,
        created_at=require_nonempty_string(report.get("created_at"), "created_at"),
    )
    require(expected_report == report, "evidence_semantic_replay_mismatch")
    return {
        "schema": REPLAY_VERIFICATION_SCHEMA,
        "status": "passed",
        "classification": "startup_consumption_verified",
        "verification_mode": "offline_replay",
        "fail_closed": True,
        "verified_evidence_hash": claimed_hash,
        "validation_run_id": report.get("validation_run_id"),
        "session_id": material.session_id,
        "artifact_count": len(ARTIFACT_FILES),
        "evidence": report,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create", help="Capture and publish a live startup-consumption bundle.")
    create.add_argument("--runtime-config-evidence", required=True)
    create.add_argument("--expected-runtime-config-evidence-sha256", required=True)
    create.add_argument("--startup-binding", required=True)
    create.add_argument("--expected-startup-binding-sha256", required=True)
    create.add_argument("--consumed-prefs", required=True)
    create.add_argument("--player-log", required=True)
    create.add_argument("--bridge-dir", required=True)
    create.add_argument("--expected-process-id", required=True, type=int)
    create.add_argument("--expected-process-image-name", default="OxygenNotIncluded.exe")
    create.add_argument("--expected-process-image-path-id-upper", required=True)
    create.add_argument("--expected-process-image-sha256", required=True)
    create.add_argument("--output-dir", required=True)
    create.set_defaults(handler=create_command)

    verify = subparsers.add_parser("verify", help="Replay-verify an immutable evidence bundle offline.")
    verify.add_argument("--bundle-dir", required=True)
    verify.set_defaults(handler=verify_command)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.handler(args)
    except StartupEvidenceError as exc:
        print(
            json.dumps(
                {
                    "schema": EVIDENCE_SCHEMA,
                    "status": "blocked",
                    "classification": exc.code,
                    "detail": exc.detail,
                    "fail_closed": True,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    except Exception:
        print(
            json.dumps(
                {
                    "schema": EVIDENCE_SCHEMA,
                    "status": "blocked",
                    "classification": "internal_error",
                    "fail_closed": True,
                },
                sort_keys=True,
            )
        )
        return 3
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
