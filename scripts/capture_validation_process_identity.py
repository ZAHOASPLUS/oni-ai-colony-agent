#!/usr/bin/env python3
"""Create or verify a fail-closed Windows validation-process identity report.

This tool is deliberately read-only with respect to processes.  It never
launches, stops, signals, injects into, or otherwise controls a process.  A
create operation accepts exactly one native Toolhelp candidate for the
requested image name, binds it to external image path/file hashes, re-queries
the same PID across image hashing, and create-only publishes a path-free JSON
identity report.  Verification requires the external raw report-file SHA-256
and reconstructs the identity while that exact process is still alive.
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import stat
import sys
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Sequence


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.canonical_hash import CanonicalHashError, canonical_hash, loads_strict
from scripts import create_startup_consumption_evidence as startup_contract


SCHEMA = "oni.validation-process-identity.v1"
HASH_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
MAX_REPORT_BYTES = 1024 * 1024
REPARSE_ATTRIBUTE = 0x400
PROCESS_KEYS = startup_contract.PROCESS_EVIDENCE_KEYS
DOCUMENT_KEYS = frozenset(
    {
        "schema",
        "status",
        "classification",
        "verification_mode",
        "fail_closed",
        "expected_image_name",
        "expected_image_path_id_upper",
        "expected_image_file_sha256",
        "process",
        "captured_at",
        "document_hash",
    }
)


class ProcessIdentityError(RuntimeError):
    """A stable error whose code never contains a raw executable path."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def require(condition: bool, code: str) -> None:
    if not condition:
        raise ProcessIdentityError(code)


def normalized(path: pathlib.Path | str) -> pathlib.Path:
    return pathlib.Path(os.path.abspath(os.path.normpath(str(pathlib.Path(path).expanduser()))))


def _is_reparse(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & REPARSE_ATTRIBUTE
    )


def _path_identity(metadata: os.stat_result) -> tuple[int, int]:
    return int(metadata.st_dev), int(metadata.st_ino)


def _file_version(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_size),
        int(getattr(metadata, "st_mtime_ns", int(metadata.st_mtime * 1_000_000_000))),
        int(getattr(metadata, "st_ctime_ns", int(metadata.st_ctime * 1_000_000_000))),
        int(metadata.st_nlink),
    )


def _sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _require_hash(value: Any, code: str) -> str:
    require(isinstance(value, str) and HASH_PATTERN.fullmatch(value) is not None, code)
    return str(value)


def _require_image_name(value: Any) -> str:
    require(
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and "\x00" not in value
        and "\r" not in value
        and "\n" not in value
        and "/" not in value
        and "\\" not in value
        and pathlib.PureWindowsPath(value).name == value,
        "expected_image_name_invalid",
    )
    return str(value)


def _require_offset_timestamp(value: Any) -> str:
    require(isinstance(value, str) and bool(value), "captured_at_invalid")
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProcessIdentityError("captured_at_invalid") from exc
    require(parsed.tzinfo is not None and parsed.utcoffset() is not None, "captured_at_invalid")
    return str(value)


def _document_hash(document: Mapping[str, Any]) -> str:
    payload = {key: value for key, value in document.items() if key != "document_hash"}
    try:
        return canonical_hash(payload)
    except (CanonicalHashError, TypeError, ValueError) as exc:
        raise ProcessIdentityError("document_not_hashable") from exc


def _assert_no_reparse_components(
    path: pathlib.Path,
    *,
    allow_missing_leaf: bool = False,
) -> None:
    candidate = normalized(path)
    chain = list(candidate.parents)
    chain.reverse()
    chain.append(candidate)
    for index, component in enumerate(chain):
        if index == 0 and component == component.parent:
            continue
        try:
            metadata = component.lstat()
        except FileNotFoundError as exc:
            if allow_missing_leaf and index == len(chain) - 1:
                return
            raise ProcessIdentityError("report_path_component_missing") from exc
        except OSError as exc:
            raise ProcessIdentityError("report_path_component_unreadable") from exc
        require(not _is_reparse(metadata), "report_path_contains_reparse")


def _directory_identity(path: pathlib.Path) -> tuple[int, int]:
    _assert_no_reparse_components(path)
    try:
        metadata = normalized(path).lstat()
    except OSError as exc:
        raise ProcessIdentityError("report_parent_unreadable") from exc
    require(stat.S_ISDIR(metadata.st_mode) and not _is_reparse(metadata), "report_parent_invalid")
    return _path_identity(metadata)


def _path_is_owned(
    path: pathlib.Path,
    parent: pathlib.Path,
    parent_identity: tuple[int, int],
    owner_identity: tuple[int, int],
) -> bool:
    try:
        _assert_no_reparse_components(parent)
        parent_metadata = parent.lstat()
        path_metadata = path.lstat()
    except (OSError, ProcessIdentityError):
        return False
    return (
        stat.S_ISDIR(parent_metadata.st_mode)
        and not _is_reparse(parent_metadata)
        and _path_identity(parent_metadata) == parent_identity
        and stat.S_ISREG(path_metadata.st_mode)
        and not _is_reparse(path_metadata)
        and int(path_metadata.st_nlink) == 1
        and _path_identity(path_metadata) == owner_identity
    )


def _revoke_owned_publication(
    path: pathlib.Path,
    parent: pathlib.Path,
    parent_identity: tuple[int, int],
    owner_identity: tuple[int, int],
) -> bool:
    if not _path_is_owned(path, parent, parent_identity, owner_identity):
        return False
    try:
        path.chmod(stat.S_IREAD | stat.S_IWRITE)
        path.unlink()
    except OSError:
        return False
    return not os.path.lexists(path)


def _read_report_snapshot(
    path: pathlib.Path,
) -> tuple[Dict[str, Any], bytes, str, tuple[int, int, int, int, int, int]]:
    path = normalized(path)
    _assert_no_reparse_components(path)
    try:
        before = path.lstat()
    except OSError as exc:
        raise ProcessIdentityError("report_file_unreadable") from exc
    require(
        stat.S_ISREG(before.st_mode)
        and not _is_reparse(before)
        and int(before.st_nlink) == 1,
        "report_file_not_owned_regular",
    )
    require(
        not bool(before.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)),
        "report_file_permissions_invalid",
    )
    require(0 < int(before.st_size) <= MAX_REPORT_BYTES, "report_file_size_invalid")
    try:
        with path.open("rb", buffering=0) as handle:
            opened = os.fstat(handle.fileno())
            require(_path_identity(opened) == _path_identity(before), "report_file_identity_changed")
            payload = handle.read(MAX_REPORT_BYTES + 1)
            require(len(payload) <= MAX_REPORT_BYTES, "report_file_size_invalid")
            handle.seek(0)
            replay = handle.read(len(payload) + 1)
            after = os.fstat(handle.fileno())
    except OSError as exc:
        raise ProcessIdentityError("report_file_read_failed") from exc
    require(payload == replay, "report_file_content_changed")
    require(
        _path_identity(after) == _path_identity(before)
        and int(after.st_size) == len(payload) == int(before.st_size)
        and int(getattr(after, "st_mtime_ns", 0))
        == int(getattr(before, "st_mtime_ns", 0)),
        "report_file_changed_during_read",
    )
    try:
        current = path.lstat()
    except OSError as exc:
        raise ProcessIdentityError("report_file_unreadable") from exc
    require(
        _path_identity(current) == _path_identity(before)
        and int(current.st_nlink) == 1
        and not _is_reparse(current),
        "report_file_path_replaced",
    )
    try:
        value = loads_strict(payload.decode("utf-8", "strict"))
    except (UnicodeError, CanonicalHashError, TypeError, ValueError) as exc:
        raise ProcessIdentityError("report_json_invalid") from exc
    require(type(value) is dict, "report_json_invalid")
    return dict(value), payload, _sha256_bytes(payload), _file_version(before)


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


def enumerate_process_ids_by_image_name(image_name: str) -> list[int]:
    """Return native Toolhelp PIDs whose executable basename matches exactly."""

    expected = _require_image_name(image_name)
    require(os.name == "nt", "windows_required")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_snapshot = kernel32.CreateToolhelp32Snapshot
    create_snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    create_snapshot.restype = wintypes.HANDLE
    process_first = kernel32.Process32FirstW
    process_first.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    process_first.restype = wintypes.BOOL
    process_next = kernel32.Process32NextW
    process_next.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    process_next.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    handle = create_snapshot(0x00000002, 0)
    invalid_handle = ctypes.c_void_p(-1).value
    require(bool(handle) and int(handle) != int(invalid_handle or -1), "process_enumeration_unavailable")
    candidates: list[int] = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if process_first(handle, ctypes.byref(entry)):
            while True:
                pid = int(entry.th32ProcessID)
                if pid > 0 and str(entry.szExeFile).casefold() == expected.casefold():
                    candidates.append(pid)
                entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
                ctypes.set_last_error(0)
                if not process_next(handle, ctypes.byref(entry)):
                    require(ctypes.get_last_error() == 18, "process_enumeration_failed")
                    break
        else:
            require(ctypes.get_last_error() == 18, "process_enumeration_failed")
    finally:
        close_handle(handle)
    return sorted(set(candidates))


def _query_process(process_id: int) -> startup_contract.ProcessEvidence:
    require(type(process_id) is int and process_id > 0, "process_id_invalid")
    try:
        return startup_contract._windows_process_evidence(process_id)
    except startup_contract.StartupEvidenceError as exc:
        raise ProcessIdentityError(exc.code) from exc
    except OSError as exc:
        raise ProcessIdentityError("validation_process_unavailable") from exc


def _validate_external_expectations(
    value: startup_contract.ProcessEvidence,
    *,
    expected_image_name: str,
    expected_image_path_id_upper: str,
    expected_image_file_sha256: str,
) -> None:
    require(
        value.image_name.casefold() == expected_image_name.casefold(),
        "process_image_name_mismatch",
    )
    require(value.image_path_id_upper == expected_image_path_id_upper, "process_image_path_mismatch")
    require(value.image_file_sha256 == expected_image_file_sha256, "process_image_hash_mismatch")


def capture_unique_process_identity(
    *,
    expected_image_name: str,
    expected_image_path_id_upper: str,
    expected_image_file_sha256: str,
    enumerator: Callable[[str], Sequence[int]] = enumerate_process_ids_by_image_name,
    process_query: Callable[[int], startup_contract.ProcessEvidence] = _query_process,
    after_enumeration_hook: Optional[Callable[[int], None]] = None,
    after_first_query_hook: Optional[Callable[[startup_contract.ProcessEvidence], None]] = None,
) -> startup_contract.ProcessEvidence:
    """Capture one stable identity with native uniqueness before and after hashing."""

    image_name = _require_image_name(expected_image_name)
    path_id = _require_hash(expected_image_path_id_upper, "expected_image_path_id_invalid")
    image_hash = _require_hash(expected_image_file_sha256, "expected_image_file_hash_invalid")
    before_candidates = list(enumerator(image_name))
    require(len(before_candidates) > 0, "process_candidate_missing")
    require(len(before_candidates) == 1, "process_candidate_ambiguous")
    process_id = before_candidates[0]
    require(type(process_id) is int and process_id > 0, "process_id_invalid")
    if after_enumeration_hook is not None:
        after_enumeration_hook(process_id)

    before = process_query(process_id)
    _validate_external_expectations(
        before,
        expected_image_name=image_name,
        expected_image_path_id_upper=path_id,
        expected_image_file_sha256=image_hash,
    )
    if after_first_query_hook is not None:
        after_first_query_hook(before)
    after = process_query(process_id)
    _validate_external_expectations(
        after,
        expected_image_name=image_name,
        expected_image_path_id_upper=path_id,
        expected_image_file_sha256=image_hash,
    )
    require(before == after, "process_identity_changed_across_image_hash")
    after_candidates = list(enumerator(image_name))
    require(after_candidates == [process_id], "process_candidate_set_changed")
    return before


def process_document(value: startup_contract.ProcessEvidence) -> Dict[str, Any]:
    return startup_contract.process_document(value)


def build_document(
    process: startup_contract.ProcessEvidence,
    *,
    expected_image_name: str,
    expected_image_path_id_upper: str,
    expected_image_file_sha256: str,
    captured_at: Optional[str] = None,
) -> Dict[str, Any]:
    document: Dict[str, Any] = {
        "schema": SCHEMA,
        "status": "passed",
        "classification": "validation_process_identity_verified",
        "verification_mode": "windows_native_read_only",
        "fail_closed": True,
        "expected_image_name": _require_image_name(expected_image_name),
        "expected_image_path_id_upper": _require_hash(
            expected_image_path_id_upper, "expected_image_path_id_invalid"
        ),
        "expected_image_file_sha256": _require_hash(
            expected_image_file_sha256, "expected_image_file_hash_invalid"
        ),
        "process": process_document(process),
        "captured_at": captured_at
        or dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="microseconds"),
        "document_hash": "",
    }
    document["document_hash"] = _document_hash(document)
    return validate_document(document)


def validate_document(value: Any) -> Dict[str, Any]:
    require(type(value) is dict and set(value) == DOCUMENT_KEYS, "report_fields_invalid")
    document = dict(value)
    require(
        document.get("schema") == SCHEMA
        and document.get("status") == "passed"
        and document.get("classification") == "validation_process_identity_verified"
        and document.get("verification_mode") == "windows_native_read_only"
        and document.get("fail_closed") is True,
        "report_status_invalid",
    )
    expected_name = _require_image_name(document.get("expected_image_name"))
    expected_path_id = _require_hash(
        document.get("expected_image_path_id_upper"), "expected_image_path_id_invalid"
    )
    expected_file_hash = _require_hash(
        document.get("expected_image_file_sha256"), "expected_image_file_hash_invalid"
    )
    _require_offset_timestamp(document.get("captured_at"))
    process = document.get("process")
    require(type(process) is dict and set(process) == PROCESS_KEYS, "process_fields_invalid")
    assert isinstance(process, dict)
    require(type(process.get("process_id")) is int and process["process_id"] > 0, "process_id_invalid")
    actual_name = _require_image_name(process.get("image_name"))
    creation_token = _require_hash(process.get("creation_token"), "process_creation_token_invalid")
    actual_path_id = _require_hash(process.get("image_path_id_upper"), "process_image_path_id_invalid")
    actual_file_hash = _require_hash(
        process.get("image_file_sha256"), "process_image_file_hash_invalid"
    )
    require(actual_name.casefold() == expected_name.casefold(), "process_image_name_mismatch")
    require(actual_path_id == expected_path_id, "process_image_path_mismatch")
    require(actual_file_hash == expected_file_hash, "process_image_hash_mismatch")
    del creation_token
    expected_document_hash = _require_hash(document.get("document_hash"), "document_hash_invalid")
    require(expected_document_hash == _document_hash(document), "document_self_hash_invalid")
    return document


def json_bytes(document: Mapping[str, Any]) -> bytes:
    try:
        return (
            json.dumps(
                document,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8", "strict")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ProcessIdentityError("report_not_serializable") from exc


def write_report_create_only(
    output_path: pathlib.Path,
    document: Mapping[str, Any],
    *,
    before_open_hook: Optional[Callable[[pathlib.Path], None]] = None,
    after_open_hook: Optional[Callable[[pathlib.Path], None]] = None,
    after_close_hook: Optional[Callable[[pathlib.Path], None]] = None,
    after_hash_hook: Optional[Callable[[pathlib.Path], None]] = None,
    publication_validator: Optional[Callable[[], None]] = None,
) -> pathlib.Path:
    output_path = normalized(output_path)
    parent = output_path.parent
    parent_identity = _directory_identity(parent)
    _assert_no_reparse_components(output_path, allow_missing_leaf=True)
    require(not os.path.lexists(output_path), "report_already_exists")
    payload = json_bytes(validate_document(dict(document)))
    payload_hash = _sha256_bytes(payload)
    if before_open_hook is not None:
        before_open_hook(output_path)
    require(_directory_identity(parent) == parent_identity, "report_parent_identity_changed")
    _assert_no_reparse_components(output_path, allow_missing_leaf=True)

    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    try:
        descriptor = os.open(output_path, flags, 0o600)
    except FileExistsError as exc:
        raise ProcessIdentityError("report_already_exists") from exc
    except OSError as exc:
        raise ProcessIdentityError("report_create_failed") from exc

    owner_identity: tuple[int, int] | None = None
    try:
        opened = os.fstat(descriptor)
        owner_identity = _path_identity(opened)
        require(
            stat.S_ISREG(opened.st_mode)
            and not _is_reparse(opened)
            and int(opened.st_nlink) == 1,
            "report_descriptor_invalid",
        )
        if after_open_hook is not None:
            after_open_hook(output_path)
        require(
            _path_is_owned(output_path, parent, parent_identity, owner_identity),
            "report_publication_path_changed",
        )
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            require(written > 0, "report_write_failed")
            view = view[written:]
        os.fsync(descriptor)
        written_metadata = os.fstat(descriptor)
        require(
            _path_identity(written_metadata) == owner_identity
            and int(written_metadata.st_nlink) == 1
            and int(written_metadata.st_size) == len(payload),
            "report_descriptor_changed",
        )
        os.lseek(descriptor, 0, os.SEEK_SET)
        descriptor_payload = os.read(descriptor, len(payload) + 1)
        require(
            descriptor_payload == payload and _sha256_bytes(descriptor_payload) == payload_hash,
            "report_descriptor_hash_mismatch",
        )
        require(
            _path_is_owned(output_path, parent, parent_identity, owner_identity),
            "report_publication_path_changed",
        )
        output_path.chmod(stat.S_IREAD)
        os.close(descriptor)
        descriptor = -1
        if after_close_hook is not None:
            after_close_hook(output_path)
        published, published_payload, published_hash, _version = _read_report_snapshot(output_path)
        require(
            _path_is_owned(output_path, parent, parent_identity, owner_identity)
            and published_payload == payload
            and published_hash == payload_hash
            and published == dict(document),
            "report_publication_hash_mismatch",
        )
        if after_hash_hook is not None:
            after_hash_hook(output_path)
        require(
            _path_is_owned(output_path, parent, parent_identity, owner_identity),
            "report_publication_path_changed_after_hash",
        )
        final_document, final_payload, final_hash, _final_version = _read_report_snapshot(
            output_path
        )
        require(
            final_document == dict(document)
            and final_payload == payload
            and final_hash == payload_hash,
            "report_publication_changed_after_hash",
        )
        if publication_validator is not None:
            publication_validator()
        require(
            _path_is_owned(output_path, parent, parent_identity, owner_identity),
            "report_publication_path_changed_during_validation",
        )
        validated_document, validated_payload, validated_hash, _validated_version = (
            _read_report_snapshot(output_path)
        )
        require(
            validated_document == dict(document)
            and validated_payload == payload
            and validated_hash == payload_hash,
            "report_publication_changed_during_validation",
        )
    except BaseException as exc:
        if descriptor != -1:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if owner_identity is not None and _revoke_owned_publication(
            output_path, parent, parent_identity, owner_identity
        ):
            raise
        raise ProcessIdentityError("report_publication_outcome_unknown") from exc
    return output_path


def create_report(
    *,
    expected_image_name: str,
    expected_image_path_id_upper: str,
    expected_image_file_sha256: str,
    output_path: pathlib.Path,
    enumerator: Callable[[str], Sequence[int]] = enumerate_process_ids_by_image_name,
    process_query: Callable[[int], startup_contract.ProcessEvidence] = _query_process,
    after_publication_hook: Optional[Callable[[startup_contract.ProcessEvidence], None]] = None,
) -> tuple[Dict[str, Any], str]:
    process = capture_unique_process_identity(
        expected_image_name=expected_image_name,
        expected_image_path_id_upper=expected_image_path_id_upper,
        expected_image_file_sha256=expected_image_file_sha256,
        enumerator=enumerator,
        process_query=process_query,
    )
    document = build_document(
        process,
        expected_image_name=expected_image_name,
        expected_image_path_id_upper=expected_image_path_id_upper,
        expected_image_file_sha256=expected_image_file_sha256,
    )
    def validate_published_process() -> None:
        if after_publication_hook is not None:
            after_publication_hook(process)
        published_process = capture_unique_process_identity(
            expected_image_name=expected_image_name,
            expected_image_path_id_upper=expected_image_path_id_upper,
            expected_image_file_sha256=expected_image_file_sha256,
            enumerator=enumerator,
            process_query=process_query,
        )
        require(published_process == process, "process_identity_changed_during_publication")

    write_report_create_only(
        output_path,
        document,
        publication_validator=validate_published_process,
    )
    return document, _sha256_bytes(json_bytes(document))


def verify_report(
    *,
    report_path: pathlib.Path,
    expected_report_file_sha256: str,
    enumerator: Callable[[str], Sequence[int]] = enumerate_process_ids_by_image_name,
    process_query: Callable[[int], startup_contract.ProcessEvidence] = _query_process,
    after_report_snapshot_hook: Optional[Callable[[Mapping[str, Any]], None]] = None,
    after_process_capture_hook: Optional[Callable[[startup_contract.ProcessEvidence], None]] = None,
) -> Dict[str, Any]:
    expected_file_hash = _require_hash(
        expected_report_file_sha256, "expected_report_file_hash_invalid"
    )
    report_path = normalized(report_path)
    document, _payload, file_hash, version = _read_report_snapshot(report_path)
    require(file_hash == expected_file_hash, "report_file_hash_mismatch")
    validated = validate_document(document)
    if after_report_snapshot_hook is not None:
        after_report_snapshot_hook(validated)
    process_record = validated["process"]
    assert isinstance(process_record, dict)
    process = capture_unique_process_identity(
        expected_image_name=str(validated["expected_image_name"]),
        expected_image_path_id_upper=str(validated["expected_image_path_id_upper"]),
        expected_image_file_sha256=str(validated["expected_image_file_sha256"]),
        enumerator=enumerator,
        process_query=process_query,
    )
    require(process_document(process) == process_record, "reported_process_identity_mismatch")
    if after_process_capture_hook is not None:
        after_process_capture_hook(process)
    middle_document, _middle_payload, middle_hash, middle_version = _read_report_snapshot(
        report_path
    )
    require(
        middle_hash == file_hash
        and middle_version == version
        and middle_document == document,
        "report_changed_across_validation",
    )
    final_process = capture_unique_process_identity(
        expected_image_name=str(validated["expected_image_name"]),
        expected_image_path_id_upper=str(validated["expected_image_path_id_upper"]),
        expected_image_file_sha256=str(validated["expected_image_file_sha256"]),
        enumerator=enumerator,
        process_query=process_query,
    )
    require(final_process == process, "process_identity_changed_across_validation")
    final_document, _final_payload, final_hash, final_version = _read_report_snapshot(report_path)
    require(
        final_hash == file_hash
        and final_version == version
        and final_document == document,
        "report_changed_across_validation",
    )
    return {
        "document": validated,
        "report_file_sha256": file_hash,
        "process": process_document(final_process),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    operations = parser.add_subparsers(dest="operation", required=True)
    create = operations.add_parser("create", help="Capture and create-only publish one identity report.")
    create.add_argument("--expected-image-name", required=True)
    create.add_argument("--expected-image-path-id-upper", required=True)
    create.add_argument("--expected-image-file-sha256", required=True)
    create.add_argument("--output", type=pathlib.Path, required=True)
    verify = operations.add_parser("verify", help="Rebuild a live identity from one immutable report.")
    verify.add_argument("--report", type=pathlib.Path, required=True)
    verify.add_argument("--report-file-sha256", required=True)
    return parser


def _path_id_lower(path: pathlib.Path) -> str:
    return _sha256_bytes(str(normalized(path)).rstrip("\\/").lower().encode("utf-8", "strict"))


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.operation == "create":
            document, file_hash = create_report(
                expected_image_name=args.expected_image_name,
                expected_image_path_id_upper=args.expected_image_path_id_upper,
                expected_image_file_sha256=args.expected_image_file_sha256,
                output_path=args.output,
            )
            result = {
                "schema": SCHEMA,
                "status": "published",
                "file": normalized(args.output).name,
                "path_id_lower": _path_id_lower(args.output),
                "report_file_sha256": file_hash,
                "document_hash": document["document_hash"],
                "process_id": document["process"]["process_id"],
            }
        else:
            verified = verify_report(
                report_path=args.report,
                expected_report_file_sha256=args.report_file_sha256,
            )
            result = {
                "schema": SCHEMA,
                "status": "passed",
                "file": normalized(args.report).name,
                "path_id_lower": _path_id_lower(args.report),
                "report_file_sha256": verified["report_file_sha256"],
                "document_hash": verified["document"]["document_hash"],
                "process_id": verified["process"]["process_id"],
            }
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))
        return 0
    except ProcessIdentityError as exc:
        print(
            json.dumps(
                {
                    "schema": SCHEMA,
                    "status": "incomplete_or_failed",
                    "classification": "behavior_fail",
                    "verification_mode": "windows_native_read_only",
                    "blockers": [exc.code],
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 1
    except BaseException as exc:
        print(
            json.dumps(
                {
                    "schema": SCHEMA,
                    "status": "incomplete_or_failed",
                    "classification": "infra_fail",
                    "verification_mode": "windows_native_read_only",
                    "blockers": ["process_identity_exception"],
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
