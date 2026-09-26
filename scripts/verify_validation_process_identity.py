#!/usr/bin/env python3
"""Adversarially verify the read-only Windows process-identity v1 contract.

Only copied, short-lived Windows command fixtures are started.  The verifier
never starts, stops, or controls ONI, Steam, or the Bridge, and it only hashes
the real Klei tree before and after the fixture run to prove that it was not
changed.  Raw fixture executable paths never enter stdout or the report.
"""

from __future__ import annotations

import argparse
import copy
import ctypes
from dataclasses import replace
import hashlib
import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterable, Iterator, Mapping, Optional, Sequence


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.canonical_hash import CanonicalHashError, canonical_hash, loads_strict
from scripts import capture_validation_process_identity as subject
from scripts import create_startup_consumption_evidence as startup_contract


SCHEMA = "oni.validation-process-identity-verification.v1"
DEFAULT_REPORT = PROJECT_ROOT / "reports" / "validation-process-identity-verification.json"
FORBIDDEN_IMAGE_NAMES = (
    "OxygenNotIncluded.exe",
    "oni-ai-bridge.exe",
    "steam.exe",
)
REPORT_KEYS = frozenset(
    {
        "schema",
        "status",
        "classification",
        "verification_mode",
        "scenario_count",
        "assertion_count",
        "results",
        "scripts",
        "boundary",
        "document_hash",
    }
)


class VerificationFailure(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class Audit:
    def __init__(self) -> None:
        self.assertion_count = 0

    def check(self, condition: bool, code: str) -> None:
        self.assertion_count += 1
        if not condition:
            raise VerificationFailure(code)

    def expect_error(
        self,
        expected: str | Sequence[str],
        action: Callable[[], Any],
        code: str,
    ) -> str:
        allowed = {expected} if isinstance(expected, str) else set(expected)
        try:
            action()
        except subject.ProcessIdentityError as exc:
            self.check(exc.code in allowed, code)
            return exc.code
        self.check(False, code)
        raise AssertionError("unreachable")


def sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def file_sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def report_document_hash(document: Mapping[str, Any]) -> str:
    return canonical_hash({key: value for key, value in document.items() if key != "document_hash"})


def strict_object(payload: bytes, code: str) -> Dict[str, Any]:
    try:
        value = loads_strict(payload.decode("utf-8", "strict"))
    except (UnicodeError, CanonicalHashError, TypeError, ValueError) as exc:
        raise VerificationFailure(code) from exc
    if type(value) is not dict:
        raise VerificationFailure(code)
    return dict(value)


def process_counts() -> Dict[str, int]:
    return {
        name: len(subject.enumerate_process_ids_by_image_name(name))
        for name in FORBIDDEN_IMAGE_NAMES
    }


def documents_directory() -> pathlib.Path:
    if os.name != "nt":
        raise VerificationFailure("windows_required")
    buffer = ctypes.create_unicode_buffer(32768)
    result = ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buffer)
    if result != 0 or not buffer.value:
        raise VerificationFailure("documents_directory_unavailable")
    return pathlib.Path(buffer.value)


def _klei_fingerprint_once() -> Dict[str, Any]:
    script = r"""
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$docs = [Environment]::GetFolderPath([Environment+SpecialFolder]::MyDocuments)
$root = Join-Path $docs 'Klei\OxygenNotIncluded'
if (-not (Test-Path -LiteralPath $root -PathType Container)) {
  [pscustomobject]@{exists=$false;file_count=0;byte_count=0;aggregate_sha256=$null;reparse_count=0} | ConvertTo-Json -Compress
  exit 0
}
$files = @(Get-ChildItem -LiteralPath $root -File -Recurse -Force | Sort-Object FullName)
$sb = [System.Text.StringBuilder]::new()
[long]$bytes = 0
[int]$reparse = 0
foreach ($file in $files) {
  if (($file.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { $reparse += 1 }
  $relative = [System.IO.Path]::GetRelativePath($root, $file.FullName).Replace('\','/')
  $hash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
  [void]$sb.Append($relative).Append([char]0).Append($file.Length).Append([char]0).Append($hash).Append("`n")
  $bytes += $file.Length
}
$digestBytes = [System.Security.Cryptography.SHA256]::HashData([System.Text.Encoding]::UTF8.GetBytes($sb.ToString()))
$digest = [Convert]::ToHexString($digestBytes).ToLowerInvariant()
[pscustomobject]@{exists=$true;file_count=$files.Count;byte_count=$bytes;aggregate_sha256=('sha256:' + $digest);reparse_count=$reparse} | ConvertTo-Json -Compress
"""
    completed = subprocess.run(
        ["pwsh.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
        creationflags=_creation_flags(),
    )
    if completed.returncode != 0 or completed.stderr:
        raise OSError("klei_fingerprint_failed")
    value = strict_object(completed.stdout, "klei_fingerprint_invalid")
    if set(value) != {
        "exists",
        "file_count",
        "byte_count",
        "aggregate_sha256",
        "reparse_count",
    }:
        raise OSError("klei_fingerprint_invalid")
    return value


def klei_fingerprint() -> Dict[str, Any]:
    for attempt in range(5):
        try:
            return _klei_fingerprint_once()
        except (FileNotFoundError, PermissionError, OSError):
            if attempt == 4:
                raise VerificationFailure("klei_fingerprint_unstable")
            time.sleep(0.05)
    raise AssertionError("unreachable")


def _creation_flags() -> int:
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


def stop_fixture(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        try:
            process.terminate()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
    if process.stdin is not None:
        try:
            process.stdin.close()
        except OSError:
            pass


def spawn_fixture(image_path: pathlib.Path) -> subprocess.Popen[bytes]:
    process = subprocess.Popen(
        [str(image_path), "/d", "/q", "/c", "pause"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=_creation_flags(),
    )
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise VerificationFailure("fixture_process_exited_early")
        candidates = subject.enumerate_process_ids_by_image_name(image_path.name)
        if process.pid in candidates:
            return process
        time.sleep(0.05)
    stop_fixture(process)
    raise VerificationFailure("fixture_process_not_enumerated")


@contextmanager
def fixture_process(root: pathlib.Path, label: str) -> Iterator[tuple[subprocess.Popen[bytes], pathlib.Path]]:
    source_value = os.environ.get("COMSPEC")
    if not source_value:
        raise VerificationFailure("fixture_source_unavailable")
    source = pathlib.Path(source_value)
    image_path = root / f"pid_fixture_{label}_{uuid.uuid4().hex[:10]}.exe"
    shutil.copy2(source, image_path)
    process = spawn_fixture(image_path)
    try:
        yield process, image_path
    finally:
        stop_fixture(process)


def run_subject(arguments: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "capture_validation_process_identity.py"), *arguments],
        cwd=PROJECT_ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
        creationflags=_creation_flags(),
    )


def raw_path_needles(raw_path: str) -> list[bytes]:
    variants = {
        raw_path,
        os.path.abspath(raw_path),
        os.path.normpath(raw_path),
    }
    needles: list[bytes] = []
    for value in variants:
        needles.append(value.encode("utf-8", "strict").lower())
        escaped = json.dumps(value, ensure_ascii=False)[1:-1]
        needles.append(escaped.encode("utf-8", "strict").lower())
    return [needle for needle in needles if needle]


def assert_private(audit: Audit, payload: bytes, raw_path: str, code: str) -> None:
    lowered = payload.lower()
    audit.check(all(needle not in lowered for needle in raw_path_needles(raw_path)), code)


def make_read_only(path: pathlib.Path) -> None:
    path.chmod(stat.S_IREAD)


def make_writable(path: pathlib.Path) -> None:
    path.chmod(stat.S_IREAD | stat.S_IWRITE)


def write_document(path: pathlib.Path, document: Mapping[str, Any], *, self_hash: bool = True) -> bytes:
    value = copy.deepcopy(dict(document))
    if self_hash:
        value["document_hash"] = subject._document_hash(value)
    payload = subject.json_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        make_writable(path)
    path.write_bytes(payload)
    make_read_only(path)
    return payload


def copy_report(source: pathlib.Path, destination: pathlib.Path) -> pathlib.Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    make_read_only(destination)
    return destination


def verify_path(path: pathlib.Path) -> Dict[str, Any]:
    return subject.verify_report(
        report_path=path,
        expected_report_file_sha256=file_sha256(path),
    )


class SequenceEnumerator:
    def __init__(self, values: Sequence[Sequence[int]]) -> None:
        self.values = [list(value) for value in values]
        self.index = 0

    def __call__(self, _name: str) -> Sequence[int]:
        if self.index >= len(self.values):
            return list(self.values[-1])
        value = self.values[self.index]
        self.index += 1
        return list(value)


class SequenceQuery:
    def __init__(self, values: Sequence[Any]) -> None:
        self.values = list(values)
        self.index = 0

    def __call__(self, _process_id: int) -> startup_contract.ProcessEvidence:
        if self.index >= len(self.values):
            value = self.values[-1]
        else:
            value = self.values[self.index]
            self.index += 1
        if isinstance(value, BaseException):
            raise value
        return value


def expected_arguments(evidence: startup_contract.ProcessEvidence) -> list[str]:
    return [
        "--expected-image-name",
        evidence.image_name,
        "--expected-image-path-id-upper",
        evidence.image_path_id_upper,
        "--expected-image-file-sha256",
        evidence.image_file_sha256,
    ]


def scenario_static_contract(audit: Audit) -> None:
    source = (PROJECT_ROOT / "scripts" / "capture_validation_process_identity.py").read_text(
        encoding="utf-8"
    )
    startup_source = (
        PROJECT_ROOT / "scripts" / "create_startup_consumption_evidence.py"
    ).read_text(encoding="utf-8")
    for anchor in (
        "CreateToolhelp32Snapshot",
        "Process32FirstW",
        "Process32NextW",
        "process_enumeration_failed",
        "capture_unique_process_identity",
        "publication_validator",
        "expected_report_file_sha256",
    ):
        audit.check(anchor in source, f"static_anchor_missing_{anchor}")
    audit.check(
        "startup_contract._windows_process_evidence(process_id)" in source,
        "frozen_windows_identity_not_reused",
    )
    audit.check(
        "handle = open_process(0x1000, 0, process_id)" in startup_source,
        "windows_query_limited_information_access_missing",
    )
    audit.check(
        all(
            forbidden not in source
            for forbidden in (
                "TerminateProcess",
                "CreateRemoteThread",
                "WriteProcessMemory",
                "DebugActiveProcess",
                "SetPriorityClass",
                "SuspendThread",
                "ResumeThread",
                "subprocess.",
            )
        ),
        "subject_contains_process_control_api",
    )
    audit.check(
        subject.DOCUMENT_KEYS
        == {
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
        },
        "document_key_contract_changed",
    )


def scenario_native_cli_positive(
    audit: Audit,
    root: pathlib.Path,
    process: subprocess.Popen[bytes],
    evidence: startup_contract.ProcessEvidence,
) -> tuple[pathlib.Path, Dict[str, Any]]:
    report = root / "positive" / "identity.json"
    report.parent.mkdir(parents=True)
    created = run_subject(["create", *expected_arguments(evidence), "--output", str(report)])
    audit.check(created.returncode == 0, "cli_create_failed")
    audit.check(created.stderr == b"", "cli_create_stderr_not_empty")
    created_stdout = strict_object(created.stdout, "cli_create_stdout_invalid")
    audit.check(
        set(created_stdout)
        == {
            "schema",
            "status",
            "file",
            "path_id_lower",
            "report_file_sha256",
            "document_hash",
            "process_id",
        },
        "cli_create_stdout_fields_invalid",
    )
    audit.check(created_stdout.get("schema") == subject.SCHEMA, "cli_create_schema_invalid")
    audit.check(created_stdout.get("status") == "published", "cli_create_status_invalid")
    audit.check(created_stdout.get("process_id") == process.pid, "cli_create_pid_invalid")
    audit.check(report.is_file(), "cli_create_report_missing")
    report_payload = report.read_bytes()
    assert_private(audit, created.stdout, evidence.image_path, "cli_create_stdout_disclosed_raw_path")
    assert_private(audit, created.stderr, evidence.image_path, "cli_create_stderr_disclosed_raw_path")
    assert_private(audit, report_payload, evidence.image_path, "report_disclosed_raw_path")
    metadata = report.lstat()
    audit.check(stat.S_ISREG(metadata.st_mode), "report_not_regular")
    audit.check(not subject._is_reparse(metadata), "report_is_reparse")
    audit.check(int(metadata.st_nlink) == 1, "report_not_single_link")
    audit.check(
        not bool(metadata.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)),
        "report_not_read_only",
    )
    document = strict_object(report_payload, "report_json_invalid")
    audit.check(set(document) == subject.DOCUMENT_KEYS, "report_fields_invalid")
    audit.check(subject.validate_document(document) == document, "report_document_invalid")
    audit.check(document["process"]["process_id"] == process.pid, "report_pid_invalid")
    audit.check(
        document["expected_image_name"].casefold() == evidence.image_name.casefold(),
        "report_expected_name_invalid",
    )
    audit.check(
        document["expected_image_path_id_upper"] == evidence.image_path_id_upper,
        "report_expected_path_invalid",
    )
    audit.check(
        document["expected_image_file_sha256"] == evidence.image_file_sha256,
        "report_expected_hash_invalid",
    )
    raw_hash = file_sha256(report)
    audit.check(created_stdout["report_file_sha256"] == raw_hash, "cli_create_raw_hash_invalid")
    audit.check(created_stdout["document_hash"] == document["document_hash"], "cli_create_self_hash_invalid")

    verified = run_subject(
        ["verify", "--report", str(report), "--report-file-sha256", raw_hash]
    )
    audit.check(verified.returncode == 0, "cli_verify_failed")
    audit.check(verified.stderr == b"", "cli_verify_stderr_not_empty")
    verified_stdout = strict_object(verified.stdout, "cli_verify_stdout_invalid")
    audit.check(set(verified_stdout) == set(created_stdout), "cli_verify_stdout_fields_invalid")
    audit.check(verified_stdout.get("status") == "passed", "cli_verify_status_invalid")
    audit.check(verified_stdout.get("process_id") == process.pid, "cli_verify_pid_invalid")
    audit.check(verified_stdout.get("report_file_sha256") == raw_hash, "cli_verify_hash_invalid")
    audit.check(
        verified_stdout.get("document_hash") == document.get("document_hash"),
        "cli_verify_self_hash_invalid",
    )
    assert_private(audit, verified.stdout, evidence.image_path, "cli_verify_stdout_disclosed_raw_path")
    assert_private(audit, verified.stderr, evidence.image_path, "cli_verify_stderr_disclosed_raw_path")

    collision = run_subject(["create", *expected_arguments(evidence), "--output", str(report)])
    audit.check(collision.returncode == 1, "cli_create_collision_not_rejected")
    collision_stdout = strict_object(collision.stdout, "cli_collision_stdout_invalid")
    audit.check(collision_stdout.get("blockers") == ["report_already_exists"], "cli_collision_code_invalid")
    audit.check(file_sha256(report) == raw_hash, "cli_collision_changed_report")
    assert_private(audit, collision.stdout, evidence.image_path, "cli_collision_disclosed_raw_path")
    return report, document


def scenario_native_uniqueness(
    audit: Audit,
    root: pathlib.Path,
    image_path: pathlib.Path,
    evidence: startup_contract.ProcessEvidence,
) -> None:
    second = spawn_fixture(image_path)
    try:
        output = root / "ambiguous" / "identity.json"
        output.parent.mkdir(parents=True)
        result = run_subject(["create", *expected_arguments(evidence), "--output", str(output)])
        audit.check(result.returncode == 1, "ambiguous_cli_not_rejected")
        stdout = strict_object(result.stdout, "ambiguous_cli_stdout_invalid")
        audit.check(stdout.get("blockers") == ["process_candidate_ambiguous"], "ambiguous_cli_code_invalid")
        audit.check(not output.exists(), "ambiguous_cli_published_report")
        assert_private(audit, result.stdout, evidence.image_path, "ambiguous_cli_disclosed_raw_path")
    finally:
        stop_fixture(second)


def scenario_process_races(audit: Audit, evidence: startup_contract.ProcessEvidence) -> None:
    pid = evidence.process_id
    static_enum = lambda _name: [pid]
    audit.expect_error(
        "process_candidate_missing",
        lambda: subject.capture_unique_process_identity(
            expected_image_name=evidence.image_name,
            expected_image_path_id_upper=evidence.image_path_id_upper,
            expected_image_file_sha256=evidence.image_file_sha256,
            enumerator=lambda _name: [],
            process_query=lambda _pid: evidence,
        ),
        "missing_candidate_not_rejected",
    )
    audit.expect_error(
        "process_candidate_ambiguous",
        lambda: subject.capture_unique_process_identity(
            expected_image_name=evidence.image_name,
            expected_image_path_id_upper=evidence.image_path_id_upper,
            expected_image_file_sha256=evidence.image_file_sha256,
            enumerator=lambda _name: [pid, pid + 1],
            process_query=lambda _pid: evidence,
        ),
        "ambiguous_candidate_not_rejected",
    )
    audit.expect_error(
        "process_id_invalid",
        lambda: subject.capture_unique_process_identity(
            expected_image_name=evidence.image_name,
            expected_image_path_id_upper=evidence.image_path_id_upper,
            expected_image_file_sha256=evidence.image_file_sha256,
            enumerator=lambda _name: [True],
            process_query=lambda _pid: evidence,
        ),
        "bool_candidate_pid_not_rejected",
    )
    audit.expect_error(
        "process_candidate_set_changed",
        lambda: subject.capture_unique_process_identity(
            expected_image_name=evidence.image_name,
            expected_image_path_id_upper=evidence.image_path_id_upper,
            expected_image_file_sha256=evidence.image_file_sha256,
            enumerator=SequenceEnumerator([[pid], [pid, pid + 1]]),
            process_query=lambda _pid: evidence,
        ),
        "candidate_set_change_not_rejected",
    )
    reused = replace(evidence, creation_token="sha256:" + "ab" * 32)
    audit.expect_error(
        "process_identity_changed_across_image_hash",
        lambda: subject.capture_unique_process_identity(
            expected_image_name=evidence.image_name,
            expected_image_path_id_upper=evidence.image_path_id_upper,
            expected_image_file_sha256=evidence.image_file_sha256,
            enumerator=static_enum,
            process_query=SequenceQuery([evidence, reused]),
        ),
        "pid_reuse_not_rejected",
    )
    drifted_hash = replace(evidence, image_file_sha256="sha256:" + "cd" * 32)
    audit.expect_error(
        "process_image_hash_mismatch",
        lambda: subject.capture_unique_process_identity(
            expected_image_name=evidence.image_name,
            expected_image_path_id_upper=evidence.image_path_id_upper,
            expected_image_file_sha256=evidence.image_file_sha256,
            enumerator=static_enum,
            process_query=SequenceQuery([evidence, drifted_hash]),
        ),
        "image_hash_drift_not_rejected",
    )
    drifted_path = replace(evidence, image_path_id_upper="sha256:" + "ef" * 32)
    audit.expect_error(
        "process_image_path_mismatch",
        lambda: subject.capture_unique_process_identity(
            expected_image_name=evidence.image_name,
            expected_image_path_id_upper=evidence.image_path_id_upper,
            expected_image_file_sha256=evidence.image_file_sha256,
            enumerator=static_enum,
            process_query=SequenceQuery([evidence, drifted_path]),
        ),
        "image_path_drift_not_rejected",
    )
    audit.expect_error(
        "validation_process_unavailable",
        lambda: subject.capture_unique_process_identity(
            expected_image_name=evidence.image_name,
            expected_image_path_id_upper=evidence.image_path_id_upper,
            expected_image_file_sha256=evidence.image_file_sha256,
            enumerator=static_enum,
            process_query=SequenceQuery(
                [evidence, subject.ProcessIdentityError("validation_process_unavailable")]
            ),
        ),
        "process_exit_between_queries_not_rejected",
    )


def scenario_document_refusals(
    audit: Audit,
    root: pathlib.Path,
    positive: pathlib.Path,
    base: Mapping[str, Any],
) -> None:
    cases: list[tuple[str, Callable[[Dict[str, Any]], None], str, bool]] = [
        ("pid_bool", lambda value: value["process"].__setitem__("process_id", True), "process_id_invalid", True),
        ("pid_zero", lambda value: value["process"].__setitem__("process_id", 0), "process_id_invalid", True),
        ("pid_negative", lambda value: value["process"].__setitem__("process_id", -1), "process_id_invalid", True),
        ("pid_wrong", lambda value: value["process"].__setitem__("process_id", int(value["process"]["process_id"]) + 100000), "reported_process_identity_mismatch", True),
        ("extra_top", lambda value: value.__setitem__("extra", "x"), "report_fields_invalid", True),
        ("extra_process", lambda value: value["process"].__setitem__("extra", "x"), "process_fields_invalid", True),
        ("creation_token", lambda value: value["process"].__setitem__("creation_token", "SHA256:" + "a" * 64), "process_creation_token_invalid", True),
        ("expected_path", lambda value: value.__setitem__("expected_image_path_id_upper", "sha256:" + "11" * 32), "process_image_path_mismatch", True),
        ("self_hash", lambda value: value.__setitem__("document_hash", "sha256:" + "22" * 32), "document_self_hash_invalid", False),
    ]
    for label, mutate, expected_code, recompute in cases:
        document = copy.deepcopy(dict(base))
        mutate(document)
        path = root / "document-refusals" / f"{label}.json"
        write_document(path, document, self_hash=recompute)
        audit.expect_error(
            expected_code,
            lambda path=path: verify_path(path),
            f"document_refusal_{label}_failed",
        )

    audit.expect_error(
        "report_file_hash_mismatch",
        lambda: subject.verify_report(
            report_path=positive,
            expected_report_file_sha256="sha256:" + "33" * 32,
        ),
        "external_raw_hash_not_enforced",
    )
    nonfinite = root / "document-refusals" / "nonfinite.json"
    payload = subject.json_bytes(dict(base)).replace(b'"process_id": ', b'"process_id": NaN, "unused": ')
    nonfinite.write_bytes(payload)
    make_read_only(nonfinite)
    audit.expect_error(
        "report_json_invalid",
        lambda: subject.verify_report(
            report_path=nonfinite,
            expected_report_file_sha256=file_sha256(nonfinite),
        ),
        "nonfinite_json_not_rejected",
    )


def create_directory_reparse(target: pathlib.Path, link: pathlib.Path) -> None:
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except (NotImplementedError, OSError):
        pass
    command = subprocess.run(
        [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/q", "/c", "mklink", "/J", str(link), str(target)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
        creationflags=_creation_flags(),
    )
    if command.returncode != 0 or not os.path.lexists(link):
        raise VerificationFailure("reparse_fixture_unavailable")


def scenario_report_custody(
    audit: Audit,
    root: pathlib.Path,
    positive: pathlib.Path,
) -> None:
    writable = copy_report(positive, root / "custody" / "writable.json")
    make_writable(writable)
    audit.expect_error(
        "report_file_permissions_invalid",
        lambda: subject.verify_report(
            report_path=writable,
            expected_report_file_sha256=file_sha256(writable),
        ),
        "writable_report_not_rejected",
    )

    hardlink_source = copy_report(positive, root / "custody" / "hardlink-source.json")
    hardlink_alias = root / "custody" / "hardlink-alias.json"
    os.link(hardlink_source, hardlink_alias)
    audit.expect_error(
        "report_file_not_owned_regular",
        lambda: subject.verify_report(
            report_path=hardlink_source,
            expected_report_file_sha256=file_sha256(hardlink_source),
        ),
        "hardlinked_report_not_rejected",
    )

    directory_report = root / "custody" / "directory-report"
    directory_report.mkdir()
    audit.expect_error(
        "report_file_not_owned_regular",
        lambda: subject.verify_report(
            report_path=directory_report,
            expected_report_file_sha256="sha256:" + "44" * 32,
        ),
        "directory_report_not_rejected",
    )

    reparse_target = root / "custody" / "reparse-target"
    reparse_target.mkdir()
    copy_report(positive, reparse_target / "identity.json")
    reparse_link = root / "custody" / "reparse-link"
    create_directory_reparse(reparse_target, reparse_link)
    try:
        audit.expect_error(
            "report_path_contains_reparse",
            lambda: subject.verify_report(
                report_path=reparse_link / "identity.json",
                expected_report_file_sha256=file_sha256(reparse_target / "identity.json"),
            ),
            "reparse_report_not_rejected",
        )
    finally:
        if os.path.lexists(reparse_link):
            os.rmdir(reparse_link)


def scenario_report_toctou(
    audit: Audit,
    root: pathlib.Path,
    positive: pathlib.Path,
) -> None:
    replacement_path = copy_report(positive, root / "toctou" / "replacement.json")
    held_path = replacement_path.with_name("replacement-held.json")

    def replace_after_snapshot(_document: Mapping[str, Any]) -> None:
        candidate = replacement_path.with_name("replacement-new.json")
        candidate.write_bytes(replacement_path.read_bytes())
        make_read_only(candidate)
        os.replace(replacement_path, held_path)
        os.replace(candidate, replacement_path)

    audit.expect_error(
        "report_changed_across_validation",
        lambda: subject.verify_report(
            report_path=replacement_path,
            expected_report_file_sha256=file_sha256(replacement_path),
            after_report_snapshot_hook=replace_after_snapshot,
        ),
        "same_content_name_replacement_not_rejected",
    )

    tamper_path = copy_report(positive, root / "toctou" / "same-file-tamper.json")

    def tamper_after_process(_process: startup_contract.ProcessEvidence) -> None:
        make_writable(tamper_path)
        with tamper_path.open("ab") as handle:
            handle.write(b" ")
            handle.flush()
            os.fsync(handle.fileno())
        make_read_only(tamper_path)

    audit.expect_error(
        "report_changed_across_validation",
        lambda: subject.verify_report(
            report_path=tamper_path,
            expected_report_file_sha256=file_sha256(tamper_path),
            after_process_capture_hook=tamper_after_process,
        ),
        "same_file_toctou_tamper_not_rejected",
    )


def scenario_create_publication(
    audit: Audit,
    root: pathlib.Path,
    document: Mapping[str, Any],
) -> None:
    existing = root / "publication" / "existing.json"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"attacker\n")
    original = existing.read_bytes()
    audit.expect_error(
        "report_already_exists",
        lambda: subject.write_report_create_only(existing, document),
        "create_only_collision_not_rejected",
    )
    audit.check(existing.read_bytes() == original, "create_only_collision_changed_existing")

    tamper = root / "publication" / "same-file-tamper.json"

    def tamper_after_hash(path: pathlib.Path) -> None:
        make_writable(path)
        with path.open("ab") as handle:
            handle.write(b" ")
            handle.flush()
            os.fsync(handle.fileno())
        make_read_only(path)

    audit.expect_error(
        "report_publication_changed_after_hash",
        lambda: subject.write_report_create_only(tamper, document, after_hash_hook=tamper_after_hash),
        "publication_same_file_tamper_not_rejected",
    )
    audit.check(not tamper.exists(), "owned_failed_publication_not_revoked")

    swapped = root / "publication" / "late-swap.json"
    held = root / "publication" / "late-swap-held.json"
    attacker_payload = b'{"attacker":true}\n'

    def swap_after_hash(path: pathlib.Path) -> None:
        os.replace(path, held)
        path.write_bytes(attacker_payload)
        make_read_only(path)

    audit.expect_error(
        "report_publication_outcome_unknown",
        lambda: subject.write_report_create_only(swapped, document, after_hash_hook=swap_after_hash),
        "publication_late_name_swap_not_rejected",
    )
    audit.check(swapped.read_bytes() == attacker_payload, "attacker_replacement_not_preserved")

    linked = root / "publication" / "linked.json"
    linked_alias = root / "publication" / "linked-alias.json"

    def link_after_open(path: pathlib.Path) -> None:
        os.link(path, linked_alias)

    audit.expect_error(
        "report_publication_outcome_unknown",
        lambda: subject.write_report_create_only(linked, document, after_open_hook=link_after_open),
        "publication_hardlink_race_not_rejected",
    )
    audit.check(linked.exists() and linked_alias.exists(), "hardlink_race_ownership_confused")

    parent = root / "publication-parent"
    parent.mkdir()
    output = parent / "identity.json"
    held_parent = root / "publication-parent-held"

    def swap_parent(_path: pathlib.Path) -> None:
        os.replace(parent, held_parent)
        parent.mkdir()

    audit.expect_error(
        "report_parent_identity_changed",
        lambda: subject.write_report_create_only(output, document, before_open_hook=swap_parent),
        "publication_parent_swap_not_rejected",
    )
    audit.check(not output.exists(), "parent_swap_published_report")


def scenario_failure_stdout_privacy(
    audit: Audit,
    root: pathlib.Path,
    evidence: startup_contract.ProcessEvidence,
    positive: pathlib.Path,
) -> None:
    wrong_hash_output = root / "failure-stdout" / "wrong-hash.json"
    wrong_hash_output.parent.mkdir(parents=True)
    wrong_hash_args = expected_arguments(evidence)
    wrong_hash_args[-1] = "sha256:" + "55" * 32
    failure = run_subject(["create", *wrong_hash_args, "--output", str(wrong_hash_output)])
    audit.check(failure.returncode == 1, "wrong_external_hash_not_rejected")
    failure_stdout = strict_object(failure.stdout, "wrong_hash_stdout_invalid")
    audit.check(failure_stdout.get("blockers") == ["process_image_hash_mismatch"], "wrong_hash_code_invalid")
    audit.check(failure.stderr == b"", "wrong_hash_stderr_not_empty")
    audit.check(not wrong_hash_output.exists(), "wrong_hash_published_report")
    assert_private(audit, failure.stdout, evidence.image_path, "wrong_hash_stdout_disclosed_raw_path")
    assert_private(audit, failure.stderr, evidence.image_path, "wrong_hash_stderr_disclosed_raw_path")

    invalid_name_output = root / "failure-stdout" / "invalid-name.json"
    invalid_name = run_subject(
        [
            "create",
            "--expected-image-name",
            evidence.image_path,
            "--expected-image-path-id-upper",
            evidence.image_path_id_upper,
            "--expected-image-file-sha256",
            evidence.image_file_sha256,
            "--output",
            str(invalid_name_output),
        ]
    )
    audit.check(invalid_name.returncode == 1, "raw_path_image_name_not_rejected")
    invalid_stdout = strict_object(invalid_name.stdout, "invalid_name_stdout_invalid")
    audit.check(invalid_stdout.get("blockers") == ["expected_image_name_invalid"], "invalid_name_code_invalid")
    audit.check(invalid_name.stderr == b"", "invalid_name_stderr_not_empty")
    assert_private(audit, invalid_name.stdout, evidence.image_path, "invalid_name_stdout_disclosed_raw_path")
    assert_private(audit, invalid_name.stderr, evidence.image_path, "invalid_name_stderr_disclosed_raw_path")

    wrong_raw = run_subject(
        [
            "verify",
            "--report",
            str(positive),
            "--report-file-sha256",
            "sha256:" + "66" * 32,
        ]
    )
    audit.check(wrong_raw.returncode == 1, "wrong_report_raw_hash_not_rejected")
    wrong_raw_stdout = strict_object(wrong_raw.stdout, "wrong_report_hash_stdout_invalid")
    audit.check(wrong_raw_stdout.get("blockers") == ["report_file_hash_mismatch"], "wrong_report_hash_code_invalid")
    audit.check(wrong_raw.stderr == b"", "wrong_report_hash_stderr_not_empty")
    assert_private(audit, wrong_raw.stdout, evidence.image_path, "wrong_report_hash_disclosed_raw_path")


def scenario_process_exit_windows(
    audit: Audit,
    root: pathlib.Path,
) -> None:
    with fixture_process(root, "publish_exit") as (process, _image_path):
        evidence = subject._query_process(process.pid)
        output = root / "publish-exit" / "identity.json"
        output.parent.mkdir(parents=True)

        def stop_after_publication(_identity: startup_contract.ProcessEvidence) -> None:
            stop_fixture(process)

        audit.expect_error(
            ("process_candidate_missing", "validation_process_unavailable"),
            lambda: subject.create_report(
                expected_image_name=evidence.image_name,
                expected_image_path_id_upper=evidence.image_path_id_upper,
                expected_image_file_sha256=evidence.image_file_sha256,
                output_path=output,
                after_publication_hook=stop_after_publication,
            ),
            "process_exit_during_publication_not_rejected",
        )
        audit.check(not output.exists(), "dead_process_report_not_revoked")

    with fixture_process(root, "verify_exit") as (process, _image_path):
        evidence = subject._query_process(process.pid)
        output = root / "verify-exit" / "identity.json"
        output.parent.mkdir(parents=True)
        _document, raw_hash = subject.create_report(
            expected_image_name=evidence.image_name,
            expected_image_path_id_upper=evidence.image_path_id_upper,
            expected_image_file_sha256=evidence.image_file_sha256,
            output_path=output,
        )

        def stop_after_capture(_identity: startup_contract.ProcessEvidence) -> None:
            stop_fixture(process)

        audit.expect_error(
            ("process_candidate_missing", "validation_process_unavailable"),
            lambda: subject.verify_report(
                report_path=output,
                expected_report_file_sha256=raw_hash,
                after_process_capture_hook=stop_after_capture,
            ),
            "process_exit_during_verify_not_rejected",
        )


def verification_report(
    audit: Audit,
    results: Mapping[str, str],
    before_klei: Mapping[str, Any],
    after_klei: Mapping[str, Any],
    before_processes: Mapping[str, int],
    after_processes: Mapping[str, int],
) -> Dict[str, Any]:
    document: Dict[str, Any] = {
        "schema": SCHEMA,
        "status": "passed",
        "classification": "offline_contract_and_adversarial_windows_fixture",
        "verification_mode": "windows_native_read_only_process_identity",
        "scenario_count": len(results),
        "assertion_count": audit.assertion_count,
        "results": dict(results),
        "scripts": {
            "capture_validation_process_identity.py": file_sha256(
                PROJECT_ROOT / "scripts" / "capture_validation_process_identity.py"
            ),
            "verify_validation_process_identity.py": file_sha256(pathlib.Path(__file__).resolve()),
        },
        "boundary": {
            "fixture_process": "copied_windows_command_waiter",
            "fixture_processes_controlled_only": True,
            "oni_steam_bridge_started_or_controlled": False,
            "raw_image_path_disclosed": False,
            "klei_tree_read_only": True,
            "klei_before": dict(before_klei),
            "klei_after": dict(after_klei),
            "klei_unchanged": dict(before_klei) == dict(after_klei),
            "process_counts_before": dict(before_processes),
            "process_counts_after": dict(after_processes),
            "forbidden_process_counts_unchanged": dict(before_processes)
            == dict(after_processes),
        },
        "document_hash": "",
    }
    document["document_hash"] = report_document_hash(document)
    if set(document) != REPORT_KEYS or document["document_hash"] != report_document_hash(document):
        raise VerificationFailure("verification_report_invalid")
    return document


def publish_report(path: pathlib.Path, document: Mapping[str, Any]) -> tuple[str, str]:
    path = pathlib.Path(os.path.abspath(path))
    expected_parent = (PROJECT_ROOT / "reports").resolve()
    if path.parent.resolve() != expected_parent:
        raise VerificationFailure("verification_report_path_invalid")
    path.parent.mkdir(parents=True, exist_ok=True)
    subject._assert_no_reparse_components(path.parent)
    if os.path.lexists(path):
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or subject._is_reparse(metadata)
            or int(metadata.st_nlink) != 1
        ):
            raise VerificationFailure("verification_report_existing_path_invalid")
    payload = (
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8", "strict")
    temporary = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise VerificationFailure("verification_report_write_failed")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    make_read_only(temporary)
    try:
        if path.exists():
            make_writable(path)
        os.replace(temporary, path)
        make_read_only(path)
    finally:
        if temporary.exists():
            make_writable(temporary)
            temporary.unlink()
    raw_hash = file_sha256(path)
    stored = strict_object(path.read_bytes(), "verification_report_json_invalid")
    if stored != dict(document):
        raise VerificationFailure("verification_report_roundtrip_invalid")
    return raw_hash, str(document["document_hash"])


def run(report_path: pathlib.Path) -> Dict[str, Any]:
    if os.name != "nt":
        raise VerificationFailure("windows_required")
    audit = Audit()
    results: Dict[str, str] = {}
    before_processes = process_counts()
    audit.check(before_processes["OxygenNotIncluded.exe"] == 0, "oni_process_running")
    audit.check(before_processes["oni-ai-bridge.exe"] == 0, "bridge_process_running")
    before_klei = klei_fingerprint()

    with tempfile.TemporaryDirectory(prefix="oni-pid-identity-v1-") as temporary:
        root = pathlib.Path(temporary)
        scenario_static_contract(audit)
        results["static_read_only_windows_contract"] = "passed"
        with fixture_process(root, "positive") as (process, image_path):
            evidence = subject._query_process(process.pid)
            audit.check(evidence.process_id == process.pid, "native_fixture_pid_invalid")
            audit.check(evidence.image_name.casefold() == image_path.name.casefold(), "native_fixture_name_invalid")
            audit.check(evidence.image_path_id_upper == startup_contract.process_image_path_id_upper(evidence.image_path), "native_fixture_path_id_invalid")
            audit.check(evidence.image_file_sha256 == startup_contract.process_image_file_sha256(evidence.image_path), "native_fixture_file_hash_invalid")
            positive, document = scenario_native_cli_positive(audit, root, process, evidence)
            results["native_cli_create_verify_and_privacy"] = "passed"
            scenario_native_uniqueness(audit, root, image_path, evidence)
            results["native_unique_image_candidate"] = "passed"
            scenario_process_races(audit, evidence)
            results["process_exit_pid_reuse_and_image_drift"] = "passed"
            scenario_document_refusals(audit, root, positive, document)
            results["exact_document_and_external_hash_refusals"] = "passed"
            scenario_report_custody(audit, root, positive)
            results["regular_single_link_permissions_and_reparse"] = "passed"
            scenario_report_toctou(audit, root, positive)
            results["verify_report_replace_and_toctou"] = "passed"
            scenario_create_publication(audit, root, document)
            results["create_only_parent_link_swap_and_late_tamper"] = "passed"
            scenario_failure_stdout_privacy(audit, root, evidence, positive)
            results["failure_stdout_privacy"] = "passed"

        scenario_process_exit_windows(audit, root)
        results["published_and_verify_window_process_exit"] = "passed"

    after_klei = klei_fingerprint()
    after_processes = process_counts()
    audit.check(after_klei == before_klei, "klei_tree_changed")
    audit.check(after_processes == before_processes, "forbidden_process_counts_changed")
    audit.check(after_processes["OxygenNotIncluded.exe"] == 0, "oni_process_started")
    audit.check(after_processes["oni-ai-bridge.exe"] == 0, "bridge_process_started")
    results["live_process_and_klei_pollution_boundary"] = "passed"

    document = verification_report(
        audit,
        results,
        before_klei,
        after_klei,
        before_processes,
        after_processes,
    )
    raw_hash, self_hash = publish_report(report_path, document)
    return {
        "schema": SCHEMA,
        "status": "passed",
        "scenario_count": len(results),
        "assertion_count": audit.assertion_count,
        "report_file": report_path.name,
        "report_file_sha256": raw_hash,
        "document_hash": self_hash,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=pathlib.Path, default=DEFAULT_REPORT)
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run(args.report)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))
        return 0
    except BaseException as exc:
        blocker = exc.code if isinstance(exc, VerificationFailure) else "verification_exception"
        print(
            json.dumps(
                {
                    "schema": SCHEMA,
                    "status": "failed",
                    "blockers": [blocker],
                    "exception_type": type(exc).__name__,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
