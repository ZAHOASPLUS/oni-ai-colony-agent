#!/usr/bin/env python3
"""Bounded offline B16 strategy hot-path differential and replay check.

This harness imports fixture constructors from the protected reliability module,
but never invokes its verifier entry point.  It runs only in TemporaryDirectory
trees and has no game, save, Mod, Bridge, network, or command-custody inputs.
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
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, Iterable, Optional


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
REPORT_ROOT = PROJECT_ROOT / "reports" / "b16-strategy-hotpath-20260907"
RUNS_ROOT = REPORT_ROOT / "runs"
FIXED_NOW = dt.datetime(2026, 9, 6, 0, 0, 0, tzinfo=dt.timezone.utc)
PERF_LINE_COUNT = 10_000
HARD_TIMEOUT_SECONDS = 20.0
LIVE_BUDGET_SECONDS = 8.0
PEAK_WORKING_SET_LIMIT = 150 * 1024 * 1024
SESSION_ID = "session-performance"
WORLD_ID = 0
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_REPARSE_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
SOURCE_PATHS = (
    PROJECT_ROOT / "agent" / "strategy.py",
    PROJECT_ROOT / "agent" / "state_quality.py",
    pathlib.Path(__file__).resolve(),
)
PROTECTED_PATHS = (
    PROJECT_ROOT / "bridge" / "oni_bridge.py",
    PROJECT_ROOT / "scripts" / "verify_bridge_reliability.py",
    PROJECT_ROOT / "reports" / "bridge-reliability.json",
)

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent import state_quality, strategy  # noqa: E402
from bridge import oni_bridge  # noqa: E402
from scripts.verify_bridge_reliability import (  # noqa: E402
    attach_startup_contract,
    make_state,
    startup_expectations,
)


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def sha256_file(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def file_hash_inventory(paths: Iterable[pathlib.Path]) -> Dict[str, str]:
    return {
        path.relative_to(PROJECT_ROOT).as_posix(): sha256_file(path)
        for path in paths
    }


def is_reparse_point(path: pathlib.Path) -> bool:
    return bool(getattr(path.lstat(), "st_file_attributes", 0) & _REPARSE_ATTRIBUTE)


def validate_run_id(run_id: str) -> str:
    if _RUN_ID_RE.fullmatch(run_id) is None:
        raise ValueError("run id is invalid")
    return run_id


def create_run_dir(run_id: str) -> pathlib.Path:
    validate_run_id(run_id)
    reports_root = (PROJECT_ROOT / "reports").resolve(strict=True)
    REPORT_ROOT.mkdir(parents=True, exist_ok=True)
    resolved_report = REPORT_ROOT.resolve(strict=True)
    if resolved_report.parent != reports_root or is_reparse_point(REPORT_ROOT):
        raise RuntimeError("B16 report root failed path validation")
    RUNS_ROOT.mkdir(exist_ok=True)
    if is_reparse_point(RUNS_ROOT):
        raise RuntimeError("B16 runs root is a reparse point")
    run_dir = RUNS_ROOT / run_id
    run_dir.mkdir(exist_ok=False)
    if is_reparse_point(run_dir):
        raise RuntimeError("B16 run directory is a reparse point")
    return run_dir


def fixed_state(seq: int = 2) -> Dict[str, Any]:
    state = make_state(
        "b14-differential",
        seq,
        timestamp=(FIXED_NOW - dt.timedelta(seconds=1)).isoformat(timespec="milliseconds"),
    )
    state["bridge"] = {"stale": False, "trust_ready": True, "trust_blockers": []}
    return state


def differential_cases() -> Dict[str, Dict[str, Any]]:
    normal = fixed_state()

    missing_invalid = fixed_state()
    missing_invalid["resources"].pop("water_kg_estimate")
    missing_invalid["duplicants"]["total"] = True

    stale = fixed_state()
    stale["bridge"]["stale"] = True

    multi_world_contract = fixed_state()
    multi_world_contract["observation_scope"]["populated_world_ids"] = [0, 1]
    multi_world_contract["observation_scope"]["covered_world_ids"] = [0, 1]
    multi_world_contract["quality"]["alerts"]["source"] = "untrusted-test-source"

    emergency = fixed_state()
    emergency["alerts"] = ["suffocation"]

    return {
        "normal": normal,
        "missing_invalid": missing_invalid,
        "stale": stale,
        "multi_world_contract": multi_world_contract,
        "emergency_incident": emergency,
    }


def run_differential() -> Dict[str, Any]:
    inputs = differential_cases()
    outputs: Dict[str, Any] = {}
    timings: Dict[str, float] = {}
    for name, state in inputs.items():
        started = time.perf_counter()
        result = strategy.evaluate_strategy(
            state,
            now=FIXED_NOW,
            previous_phase="stabilize",
        )
        timings[name] = time.perf_counter() - started
        outputs[name] = result
    return {
        "schema": "oni.b16-strategy-hotpath-differential.v1",
        "generated_at": FIXED_NOW.isoformat(),
        "case_names": list(outputs),
        "input_sha256": {name: sha256_json(value) for name, value in inputs.items()},
        "output_sha256": {name: sha256_json(value) for name, value in outputs.items()},
        "inputs": inputs,
        "outputs": outputs,
        "selected": {
            name: {
                "status": value.get("status"),
                "operating_mode": value.get("operating_mode"),
                "phase": value.get("phase"),
                "trust_blockers": value.get("telemetry", {}).get("trust_blockers"),
                "contract_blockers": value.get("telemetry", {}).get("contract_blockers"),
                "gate_blockers": [gate.get("blockers") for gate in value.get("gates", [])],
            }
            for name, value in outputs.items()
        },
        "elapsed_seconds": timings,
        "total_elapsed_seconds": sum(timings.values()),
    }


def write_json_create(path: pathlib.Path, value: Any) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n")


def safe_get_contract_self_check() -> Dict[str, Any]:
    marker = object()
    checks = {
        "empty_path_present": state_quality.safe_get({"": "empty"}, "", marker) == "empty",
        "empty_path_default": state_quality.safe_get({}, "", marker) is marker,
        "top_level_present": state_quality.safe_get({"value": 7}, "value", marker) == 7,
        "top_level_default": state_quality.safe_get({}, "missing", marker) is marker,
        "nested_present": state_quality.safe_get({"a": {"b": 9}}, "a.b", marker) == 9,
        "nested_non_dict_default": state_quality.safe_get({"a": 1}, "a.b", marker) is marker,
        "non_dict_default": state_quality.safe_get(None, "a", marker) is marker,
    }
    return {"passed": all(checks.values()), "checks": checks}


def differential_summary(differential: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "schema": differential["schema"],
        "generated_at": differential["generated_at"],
        "case_names": differential["case_names"],
        "input_sha256": differential["input_sha256"],
        "output_sha256": differential["output_sha256"],
        "selected": differential["selected"],
        "elapsed_seconds": differential["elapsed_seconds"],
        "total_elapsed_seconds": differential["total_elapsed_seconds"],
    }


def run_baseline(run_id: str) -> int:
    run_dir = create_run_dir(run_id)
    self_check = safe_get_contract_self_check()
    differential = run_differential()
    source_hashes = file_hash_inventory(SOURCE_PATHS)
    protected_hashes = file_hash_inventory(PROTECTED_PATHS)
    summary = {
        "schema": "oni.b16-strategy-hotpath-baseline.v1",
        "run_id": run_id,
        "status": "baseline_captured" if self_check["passed"] else "failed",
        "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "scope": "offline_only",
        "game_launched": False,
        "original_cloud_saves_read": False,
        "normal_mod_configuration_changed": False,
        "network_proxy_changed": False,
        "safe_get_contract": self_check,
        "source_sha256": source_hashes,
        "protected_sha256": protected_hashes,
        "differential": differential_summary(differential),
    }
    write_json_create(run_dir / "inputs.json", differential["inputs"])
    write_json_create(run_dir / "outputs.json", differential["outputs"])
    write_json_create(run_dir / "differential.json", differential_summary(differential))
    write_json_create(run_dir / "summary.json", summary)
    print(json.dumps({"run_id": run_id, "status": summary["status"], "safe_get_contract": self_check, "differential": differential_summary(differential)}, ensure_ascii=False, sort_keys=True))
    return 0 if summary["status"] == "baseline_captured" else 1


def _memory_api() -> tuple[Any, Any, Any, Any, Any]:
    if os.name != "nt":
        raise RuntimeError("B16 memory measurement requires Windows")
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    get_memory = psapi.GetProcessMemoryInfo
    get_memory.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    get_memory.restype = wintypes.BOOL
    return ctypes, Counters, open_process, close_handle, get_memory


def open_process_memory_handle(pid: int, api: tuple[Any, Any, Any, Any, Any]) -> Any:
    ctypes, _, open_process, _, _ = api
    handle = open_process(0x1000, False, pid)
    if not handle:
        raise OSError(ctypes.get_last_error(), "OpenProcess failed for benchmark child")
    return handle


def process_working_set(handle: Any, api: tuple[Any, Any, Any, Any, Any]) -> int:
    ctypes, counters_type, _, _, get_memory = api
    counters = counters_type()
    counters.cb = ctypes.sizeof(counters)
    if not get_memory(handle, ctypes.byref(counters), counters.cb):
        raise OSError(ctypes.get_last_error(), "GetProcessMemoryInfo failed")
    value = max(int(counters.WorkingSetSize), int(counters.PeakWorkingSetSize))
    if value <= 0:
        raise RuntimeError("working-set measurement was not positive")
    return value


def close_process_memory_handle(handle: Any, api: tuple[Any, Any, Any, Any, Any]) -> None:
    ctypes, _, _, close_handle, _ = api
    if handle and not close_handle(handle):
        raise OSError(ctypes.get_last_error(), "CloseHandle failed")


def fixture_timestamp(fixture_now: dt.datetime) -> str:
    if fixture_now.tzinfo is None or fixture_now.utcoffset() is None:
        raise ValueError("fixture clock must be timezone-aware")
    return (fixture_now.astimezone(dt.timezone.utc) - dt.timedelta(seconds=1)).isoformat(timespec="milliseconds")


def fixture_state(expected: Dict[str, Any], seq: int, timestamp: str) -> Dict[str, Any]:
    state = make_state(SESSION_ID, seq, timestamp=timestamp, snapshot_seq=seq - 1)
    state = attach_startup_contract(state, expected)
    state["active_save_path_id"] = expected["loader_path_id_upper"]
    state["save_id_hash"] = expected["loader_save_sha256"]
    return state


def build_replay_fixture(path: pathlib.Path, line_count: int, fixture_now: dt.datetime) -> int:
    if line_count < 2:
        raise ValueError("fixture requires at least two records")
    expected = startup_expectations()
    start = startup_event_for_fixture(expected, fixture_now)
    timestamp = fixture_timestamp(fixture_now)
    with path.open("wb") as handle:
        handle.write(canonical_bytes(start) + b"\n")
        for seq in range(2, line_count + 1):
            state = fixture_state(expected, seq, timestamp)
            handle.write(canonical_bytes(state) + b"\n")
    return path.stat().st_size


def startup_event_for_fixture(expected: Dict[str, Any], fixture_now: dt.datetime) -> Dict[str, Any]:
    return attach_startup_contract(
        {
            "schema": "oni.event.v2",
            "event": "session_started",
            "reason": "save_loader_load_complete",
            "ts": fixture_timestamp(fixture_now),
            "session_id": SESSION_ID,
            "seq": 1,
            "game_build": expected["game_build"],
            "mod_version": expected["mod_version"],
            "save_id_hash": expected["loader_save_sha256"],
        },
        expected,
    )


def temp_path_evidence(root: pathlib.Path) -> Dict[str, Any]:
    temp_root = pathlib.Path(tempfile.gettempdir()).resolve(strict=True)
    resolved = root.resolve(strict=True)
    common = pathlib.Path(os.path.commonpath((str(temp_root), str(resolved))))
    within = os.path.normcase(str(common)) == os.path.normcase(str(temp_root)) and resolved != temp_root
    return {
        "temporary_root": str(resolved),
        "system_temp_root": str(temp_root),
        "within_system_temp": within,
        "root_reparse_point": is_reparse_point(root),
    }


def tree_reparse_points(root: pathlib.Path) -> list[str]:
    found: list[str] = []
    for current, directories, files in os.walk(root, followlinks=False):
        base = pathlib.Path(current)
        for name in directories + files:
            candidate = base / name
            if is_reparse_point(candidate):
                found.append(candidate.relative_to(root).as_posix())
    return sorted(found)


def run_admission_sanity() -> Dict[str, Any]:
    fixture_now = dt.datetime.now(dt.timezone.utc)
    root_path: Optional[pathlib.Path] = None
    expected = startup_expectations()
    result: Dict[str, Any] = {
        "schema": "oni.b16-strategy-hotpath-sanity.v1",
        "status": "failed",
        "records": 2,
        "fixture_started_at": fixture_now.isoformat(),
    }
    try:
        with tempfile.TemporaryDirectory(prefix="oni-b16-sanity-") as temporary:
            root_path = pathlib.Path(temporary)
            path_evidence = temp_path_evidence(root_path)
            if not path_evidence["within_system_temp"] or path_evidence["root_reparse_point"]:
                raise RuntimeError("sanity temporary root failed path validation")
            input_path = root_path / "state.jsonl"
            output_dir = root_path / "bridge"
            input_bytes = build_replay_fixture(input_path, 2, fixture_now)
            runtime, position = oni_bridge.replay_initial_records(
                input_path,
                output_dir,
                startup_expectations=expected,
                require_startup_binding=True,
            )
            oni_bridge.write_outputs(output_dir, runtime, replay_mode=True)
            health = json.loads((output_dir / "health.json").read_text(encoding="utf-8"))
            latest = json.loads((output_dir / "latest_state.json").read_text(encoding="utf-8"))
            archive = output_dir / "sessions" / SESSION_ID / "state.jsonl"
            result.update(
                {
                    "position": position,
                    "input_bytes": input_bytes,
                    "input_sha256": sha256_file(input_path),
                    "current_session_id": runtime.current_session_id,
                    "health_current_session_id": health.get("current_session_id"),
                    "latest_session_id": latest.get("session_id"),
                    "latest_seq": latest.get("seq"),
                    "latest_world_id": latest.get("world_id"),
                    "latest_save_id_hash": latest.get("save_id_hash"),
                    "latest_active_save_path_id": latest.get("active_save_path_id"),
                    "health_status": health.get("status"),
                    "health_trust_ready": health.get("trust_ready"),
                    "snapshot_age_seconds": health.get("snapshot_age_seconds"),
                    "startup_binding_verified": bool((health.get("startup_binding") or {}).get("verified")),
                    "archive_line_count": archive.read_bytes().count(b"\n") if archive.exists() else -1,
                    "strategy_last_seq": runtime.strategy_tracker.last_seq,
                    "strategy_last_snapshot_id": runtime.strategy_tracker.last_snapshot_id,
                    "temporary_files": sorted(path.relative_to(root_path).as_posix() for path in root_path.rglob("*.tmp")),
                    "reparse_points": tree_reparse_points(root_path),
                    "path_evidence": path_evidence,
                }
            )
    except Exception as exc:
        result["error_type"] = type(exc).__name__
        result["error"] = str(exc)[:1000]
    result["temporary_directory_cleaned"] = root_path is not None and not root_path.exists()
    age = result.get("snapshot_age_seconds")
    gate = {
        "current_session_exact": result.get("current_session_id") == SESSION_ID == result.get("health_current_session_id") == result.get("latest_session_id"),
        "latest_seq_2": result.get("latest_seq") == 2,
        "world_anchor_exact": result.get("latest_world_id") == WORLD_ID,
        "save_anchor_exact": result.get("latest_save_id_hash") == expected["loader_save_sha256"],
        "active_path_anchor_exact": result.get("latest_active_save_path_id") == expected["loader_path_id_upper"],
        "health_ok": result.get("health_status") == "ok" and result.get("health_trust_ready") is True,
        "startup_binding_verified": result.get("startup_binding_verified") is True,
        "archive_2": result.get("archive_line_count") == 2,
        "strategy_processed_latest": result.get("strategy_last_seq") == 2 and result.get("strategy_last_snapshot_id") == f"{SESSION_ID}:2",
        "fresh_timestamp": isinstance(age, (int, float)) and 0.0 <= float(age) <= 15.0,
        "no_temporary_files": result.get("temporary_files") == [],
        "no_reparse_points": result.get("reparse_points") == [],
        "temp_root_valid": bool((result.get("path_evidence") or {}).get("within_system_temp")) and not bool((result.get("path_evidence") or {}).get("root_reparse_point")),
        "temporary_directory_cleaned": result["temporary_directory_cleaned"] is True,
        "no_exception": "error_type" not in result,
    }
    result["gate"] = gate
    result["passed"] = all(gate.values())
    result["status"] = "passed" if result["passed"] else "failed"
    return result


def run_sanity_command(run_id: str) -> int:
    run_dir = create_run_dir(run_id)
    sanity = run_admission_sanity()
    write_json_create(run_dir / "sanity.json", sanity)
    print(json.dumps({"run_id": run_id, "status": sanity["status"], "gate": sanity["gate"]}, ensure_ascii=False, sort_keys=True))
    return 0 if sanity["passed"] else 1


def worker(input_path: pathlib.Path, output_dir: pathlib.Path) -> int:
    expected = startup_expectations()
    runtime, position = oni_bridge.replay_initial_records(
        input_path,
        output_dir,
        startup_expectations=expected,
        require_startup_binding=True,
    )
    oni_bridge.write_outputs(output_dir, runtime, replay_mode=True)
    health = json.loads((output_dir / "health.json").read_text(encoding="utf-8"))
    latest = json.loads((output_dir / "latest_state.json").read_text(encoding="utf-8"))
    archive = output_dir / "sessions" / SESSION_ID / "state.jsonl"
    archive_lines = archive.read_bytes().count(b"\n") if archive.exists() else -1
    temporary_files = [str(path) for path in output_dir.rglob("*.tmp")]
    result = {
        "status": "worker_completed",
        "position": position,
        "current_session_id": runtime.current_session_id,
        "health_current_session_id": health.get("current_session_id"),
        "latest_session_id": latest.get("session_id"),
        "latest_seq": latest.get("seq"),
        "latest_world_id": latest.get("world_id"),
        "latest_save_id_hash": latest.get("save_id_hash"),
        "latest_active_save_path_id": latest.get("active_save_path_id"),
        "health_status": health.get("status"),
        "health_trust_ready": health.get("trust_ready"),
        "snapshot_age_seconds": health.get("snapshot_age_seconds"),
        "startup_binding_verified": bool((health.get("startup_binding") or {}).get("verified")),
        "archive_line_count": archive_lines,
        "strategy_last_seq": runtime.strategy_tracker.last_seq,
        "strategy_last_snapshot_id": runtime.strategy_tracker.last_snapshot_id,
        "sequence_gap_count": runtime.seq_gap_count,
        "snapshot_sequence_gap_count": runtime.snapshot_seq_gap_count,
        "temporary_files": temporary_files,
        "reparse_points": tree_reparse_points(output_dir),
        "output_files": sorted(str(path.relative_to(output_dir)) for path in output_dir.rglob("*") if path.is_file()),
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


def terminate_child(child: subprocess.Popen[str]) -> None:
    if child.poll() is None:
        child.kill()
        child.wait(timeout=5.0)


def run_benchmark() -> Dict[str, Any]:
    started_at = dt.datetime.now(dt.timezone.utc)
    fixture_now = started_at
    root_path: Optional[pathlib.Path] = None
    child: Optional[subprocess.Popen[str]] = None
    memory_api: Optional[tuple[Any, Any, Any, Any, Any]] = None
    memory_handle: Any = None
    memory_handle_opened = False
    memory_handle_closed = False
    result: Dict[str, Any] = {
        "status": "failed",
        "started_at": started_at.isoformat(),
        "fixture_started_at": fixture_now.isoformat(),
        "records": PERF_LINE_COUNT,
        "hard_timeout_seconds": HARD_TIMEOUT_SECONDS,
        "live_budget_seconds": LIVE_BUDGET_SECONDS,
        "peak_working_set_limit_bytes": PEAK_WORKING_SET_LIMIT,
    }
    try:
        with tempfile.TemporaryDirectory(prefix="oni-b16-benchmark-") as temporary:
            root_path = pathlib.Path(temporary)
            path_evidence = temp_path_evidence(root_path)
            if not path_evidence["within_system_temp"] or path_evidence["root_reparse_point"]:
                raise RuntimeError("benchmark temporary root failed path validation")
            input_path = root_path / "state.jsonl"
            output_dir = root_path / "bridge"
            input_bytes = build_replay_fixture(input_path, PERF_LINE_COUNT, fixture_now)
            command = [sys.executable, "-B", str(pathlib.Path(__file__).resolve()), "--worker", str(input_path), str(output_dir)]
            child_started = time.perf_counter()
            creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            child = subprocess.Popen(
                command,
                cwd=str(PROJECT_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                creationflags=creation_flags,
            )
            peak_working_set = 0
            memory_samples = 0
            timed_out = False
            memory_sampling_error: Optional[str] = None
            stdout = ""
            stderr = ""
            try:
                memory_api = _memory_api()
                memory_handle = open_process_memory_handle(child.pid, memory_api)
                memory_handle_opened = True
                while child.poll() is None:
                    if time.perf_counter() - child_started > HARD_TIMEOUT_SECONDS:
                        timed_out = True
                        terminate_child(child)
                        break
                    try:
                        peak_working_set = max(peak_working_set, process_working_set(memory_handle, memory_api))
                        memory_samples += 1
                    except Exception as exc:
                        memory_sampling_error = f"{type(exc).__name__}: {str(exc)[:500]}"
                        terminate_child(child)
                        break
                    time.sleep(0.02)
                if child.poll() is None:
                    terminate_child(child)
                stdout, stderr = child.communicate(timeout=5.0)
            finally:
                if memory_handle_opened and not memory_handle_closed and memory_api is not None:
                    close_process_memory_handle(memory_handle, memory_api)
                    memory_handle_closed = True
            elapsed = time.perf_counter() - child_started
            worker_payload: Dict[str, Any] = {}
            if stdout.strip():
                try:
                    worker_payload = json.loads(stdout.strip().splitlines()[-1])
                except json.JSONDecodeError:
                    worker_payload = {"stdout_tail": stdout[-1000:]}
            result.update(
                {
                    "status": "benchmark_timeout" if timed_out else ("memory_measurement_failed" if memory_sampling_error else "benchmark_completed"),
                    "command": command,
                    "create_no_window": os.name == "nt" and creation_flags == subprocess.CREATE_NO_WINDOW,
                    "input_bytes": input_bytes,
                    "input_sha256": sha256_file(input_path),
                    "return_code": child.returncode,
                    "stderr": stderr,
                    "stdout": stdout[-2000:],
                    "worker": worker_payload,
                    "elapsed_seconds": elapsed,
                    "peak_working_set_bytes": peak_working_set,
                    "peak_working_set_mib": peak_working_set / (1024 * 1024) if peak_working_set > 0 else None,
                    "memory_sample_count": memory_samples,
                    "memory_sampling_error": memory_sampling_error,
                    "memory_handle_opened": memory_handle_opened,
                    "memory_handle_closed": memory_handle_closed,
                    "timed_out": timed_out,
                    "child_residual": child.poll() is None,
                    "reparse_points": tree_reparse_points(root_path),
                    "path_evidence": path_evidence,
                }
            )
    except Exception as exc:
        result["error_type"] = type(exc).__name__
        result["error"] = str(exc)[:1000]
        if child is not None:
            try:
                terminate_child(child)
            except Exception as terminate_exc:
                result["termination_error"] = f"{type(terminate_exc).__name__}: {str(terminate_exc)[:500]}"
        if memory_handle_opened and not memory_handle_closed and memory_api is not None:
            try:
                close_process_memory_handle(memory_handle, memory_api)
                memory_handle_closed = True
            except Exception as close_exc:
                result["memory_handle_close_error"] = f"{type(close_exc).__name__}: {str(close_exc)[:500]}"
        result["memory_handle_opened"] = memory_handle_opened
        result["memory_handle_closed"] = memory_handle_closed
        result["child_residual"] = child is not None and child.poll() is None
    result["temporary_directory_cleaned"] = root_path is not None and not root_path.exists()
    worker_payload = result.get("worker") if isinstance(result.get("worker"), dict) else {}
    expected = startup_expectations()
    age = worker_payload.get("snapshot_age_seconds")
    peak_bytes = result.get("peak_working_set_bytes")
    result["gate"] = {
        "return_code_zero": result.get("return_code") == 0,
        "stderr_empty": result.get("stderr") == "",
        "elapsed_le_8_seconds": isinstance(result.get("elapsed_seconds"), (int, float)) and result["elapsed_seconds"] <= LIVE_BUDGET_SECONDS,
        "positive_reliable_peak": isinstance(peak_bytes, int) and peak_bytes > 0 and result.get("memory_sample_count", 0) > 0 and result.get("memory_sampling_error") is None and result.get("memory_handle_opened") is True and result.get("memory_handle_closed") is True,
        "peak_le_150_mib": isinstance(peak_bytes, int) and peak_bytes <= PEAK_WORKING_SET_LIMIT,
        "hard_timeout_not_reached": result.get("timed_out") is False and result.get("status") != "benchmark_timeout",
        "latest_seq_10000": worker_payload.get("latest_seq") == PERF_LINE_COUNT,
        "current_session_exact": worker_payload.get("current_session_id") == SESSION_ID == worker_payload.get("health_current_session_id") == worker_payload.get("latest_session_id"),
        "world_anchor_exact": worker_payload.get("latest_world_id") == WORLD_ID,
        "save_anchor_exact": worker_payload.get("latest_save_id_hash") == expected["loader_save_sha256"],
        "active_path_anchor_exact": worker_payload.get("latest_active_save_path_id") == expected["loader_path_id_upper"],
        "startup_binding_verified": worker_payload.get("startup_binding_verified") is True,
        "health_ok": worker_payload.get("health_status") == "ok" and worker_payload.get("health_trust_ready") is True,
        "archive_complete": worker_payload.get("archive_line_count") == PERF_LINE_COUNT,
        "strategy_processed_latest": worker_payload.get("strategy_last_seq") == PERF_LINE_COUNT and worker_payload.get("strategy_last_snapshot_id") == f"{SESSION_ID}:{PERF_LINE_COUNT}",
        "sequence_complete": worker_payload.get("sequence_gap_count") == 0 and worker_payload.get("snapshot_sequence_gap_count") == 0,
        "fresh_timestamp": isinstance(age, (int, float)) and 0.0 <= float(age) <= 15.0,
        "no_temporary_files": worker_payload.get("temporary_files") == [],
        "no_reparse_points": worker_payload.get("reparse_points") == [] and result.get("reparse_points") == [],
        "temp_root_valid": bool((result.get("path_evidence") or {}).get("within_system_temp")) and not bool((result.get("path_evidence") or {}).get("root_reparse_point")),
        "no_child_residual": result.get("child_residual") is False,
        "temporary_directory_cleaned": result["temporary_directory_cleaned"] is True,
        "create_no_window": result.get("create_no_window") is True,
        "no_exception": "error_type" not in result,
    }
    result["passed"] = all(result["gate"].values())
    return result


def baseline_run_dir(run_id: str) -> pathlib.Path:
    validate_run_id(run_id)
    candidate = RUNS_ROOT / run_id
    resolved_runs = RUNS_ROOT.resolve(strict=True)
    resolved_candidate = candidate.resolve(strict=True)
    if resolved_candidate.parent != resolved_runs or is_reparse_point(candidate):
        raise RuntimeError("baseline run path failed validation")
    return candidate


def run_post_patch(run_id: str, baseline_run_id: str) -> int:
    run_dir = create_run_dir(run_id)
    baseline_dir = baseline_run_dir(baseline_run_id)
    baseline = json.loads((baseline_dir / "outputs.json").read_text(encoding="utf-8"))
    baseline_inputs = json.loads((baseline_dir / "inputs.json").read_text(encoding="utf-8"))
    baseline_summary = json.loads((baseline_dir / "summary.json").read_text(encoding="utf-8"))
    self_check = safe_get_contract_self_check()
    differential = run_differential()
    differential_match = differential["outputs"] == baseline
    input_match = differential["inputs"] == baseline_inputs
    if self_check["passed"] and differential_match and input_match:
        sanity = run_admission_sanity()
    else:
        sanity = {"status": "skipped", "passed": False, "reason": "precondition_failed_before_sanity"}
    if sanity.get("passed") is True:
        benchmark = run_benchmark()
    else:
        benchmark = {"status": "skipped", "passed": False, "reason": "precondition_failed_before_benchmark"}
    passed = self_check["passed"] and differential_match and input_match and sanity.get("passed") is True and benchmark.get("passed") is True
    report = {
        "schema": "oni.b16-strategy-hotpath-20260907.v1",
        "run_id": run_id,
        "baseline_run_id": baseline_run_id,
        "status": "passed" if passed else "failed",
        "completed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "scope": "offline_only",
        "game_launched": False,
        "original_cloud_saves_read": False,
        "normal_mod_configuration_changed": False,
        "network_proxy_changed": False,
        "safe_get_contract": self_check,
        "source_sha256": file_hash_inventory(SOURCE_PATHS),
        "protected_sha256": file_hash_inventory(PROTECTED_PATHS),
        "baseline_source_sha256": baseline_summary.get("source_sha256"),
        "differential": {
            "match": differential_match,
            "input_match": input_match,
            "baseline_input_sha256": {name: sha256_json(value) for name, value in baseline_inputs.items()},
            "baseline_sha256": {
                name: sha256_json(value) for name, value in baseline.items()
            },
            "post_input_sha256": differential["input_sha256"],
            "post_sha256": differential["output_sha256"],
            "selected": differential["selected"],
            "elapsed_seconds": differential["elapsed_seconds"],
        },
        "sanity": sanity,
        "benchmark": benchmark,
    }
    write_json_create(run_dir / "inputs.json", differential["inputs"])
    write_json_create(run_dir / "outputs.json", differential["outputs"])
    write_json_create(run_dir / "differential.json", differential_summary(differential))
    write_json_create(run_dir / "sanity.json", sanity)
    write_json_create(run_dir / "benchmark.json", benchmark)
    write_json_create(run_dir / "post-patch.json", report)
    print(json.dumps({"run_id": run_id, "status": report["status"], "differential_match": differential_match, "input_match": input_match, "sanity": {"status": sanity.get("status"), "passed": sanity.get("passed")}, "benchmark": {"status": benchmark.get("status"), "passed": benchmark.get("passed"), "elapsed_seconds": benchmark.get("elapsed_seconds"), "peak_working_set_mib": benchmark.get("peak_working_set_mib")}}, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] == "passed" else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--baseline", action="store_true")
    mode.add_argument("--sanity", action="store_true")
    mode.add_argument("--post-patch", action="store_true")
    mode.add_argument("--worker", nargs=2, metavar=("INPUT", "OUTPUT"))
    parser.add_argument("--run-id")
    parser.add_argument("--baseline-run-id")
    args = parser.parse_args()
    if args.worker:
        return worker(pathlib.Path(args.worker[0]), pathlib.Path(args.worker[1]))
    if not args.run_id:
        parser.error("--run-id is required")
    if args.baseline:
        return run_baseline(args.run_id)
    if args.sanity:
        return run_sanity_command(args.run_id)
    if not args.baseline_run_id:
        parser.error("--baseline-run-id is required with --post-patch")
    return run_post_patch(args.run_id, args.baseline_run_id)


if __name__ == "__main__":
    raise SystemExit(main())
