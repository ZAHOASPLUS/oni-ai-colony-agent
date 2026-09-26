"""Independent offline verifier for the A09A-2 command-custody core.

The verifier deliberately exercises the public custody APIs through fresh
filesystem fixtures.  It never starts ONI/Klei/Steam/Bridge and never reads a
save.  Failure injection is process-local monkeypatching only; every child is
created by this runner and is joined before the report is written.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import multiprocessing as mp
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import traceback
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent import command_custody as cc  # noqa: E402


REPORT_DIR = PROJECT_ROOT / "reports" / "a09a2-command-custody-core"
RECOVERY_DIR_NAME = cc.LIFECYCLE_RECOVERY_DIR_NAME
REPARSE_POINT = 0x0400
FORBIDDEN_PROCESS_NAMES = frozenset(
    name.casefold()
    for name in (
        "ONI.exe",
        "OxygenNotIncluded.exe",
        "OxygenNotIncluded_DX11.exe",
        "OxygenNotIncluded_DX12.exe",
        "Klei.exe",
        "KleiCrashReporter.exe",
        "steam.exe",
        "steamservice.exe",
        "steamwebhelper.exe",
        "bridge.exe",
        "oni-bridge.exe",
        "oni_bridge.exe",
        "run-bridge.exe",
        "verifier-helper.exe",
        "verify-command-custody-core.exe",
        "verify_command_custody_core.exe",
    )
)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def source_hashes() -> Dict[str, str]:
    return {
        "agent/command_custody.py": sha256_file(PROJECT_ROOT / "agent" / "command_custody.py"),
        "agent/command_queue.py": sha256_file(PROJECT_ROOT / "agent" / "command_queue.py"),
    }


def _is_reparse(metadata: os.stat_result) -> bool:
    return bool(int(getattr(metadata, "st_file_attributes", 0)) & REPARSE_POINT)


def _safe_kind(metadata: os.stat_result) -> str:
    if os.path.islink(metadata_path := getattr(metadata, "_path", "")):
        return "symlink"
    if _is_reparse(metadata):
        return "reparse"
    if stat.S_ISDIR(metadata.st_mode):
        return "directory"
    if stat.S_ISREG(metadata.st_mode):
        return "file"
    return "other"


def safe_remove(path: pathlib.Path) -> None:
    """Remove only the runner's own tree without following links/reparse points."""

    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return
    if os.path.islink(path):
        if stat.S_ISDIR(metadata.st_mode) and os.name == "nt":
            path.rmdir()
        else:
            path.unlink()
        return
    if _is_reparse(metadata):
        if stat.S_ISDIR(metadata.st_mode):
            path.rmdir()
        else:
            path.unlink()
        return
    if stat.S_ISDIR(metadata.st_mode):
        for entry in list(os.scandir(path)):
            safe_remove(pathlib.Path(entry.path))
        path.rmdir()
        return
    path.unlink()


def _read_regular(path: pathlib.Path) -> Optional[bytes]:
    try:
        metadata = os.lstat(path)
    except OSError:
        return None
    if os.path.islink(path) or _is_reparse(metadata) or not stat.S_ISREG(metadata.st_mode):
        return None
    if int(metadata.st_nlink) != 1:
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


def inventory(root: pathlib.Path) -> Dict[str, Any]:
    result: Dict[str, Any] = {"stages": {}, "recovery": {}}
    commands = root / "commands"
    stage_names = list(cc.COMMAND_STAGES)
    for stage in stage_names:
        directory = commands / stage
        entries: List[Dict[str, Any]] = []
        try:
            children = sorted(directory.iterdir(), key=lambda item: item.name)
        except OSError:
            result["stages"][stage] = entries
            continue
        for path in children:
            try:
                metadata = os.lstat(path)
                kind = "reparse" if os.path.islink(path) or _is_reparse(metadata) else (
                    "directory" if stat.S_ISDIR(metadata.st_mode) else "file"
                )
                item: Dict[str, Any] = {
                    "name": path.name,
                    "kind": kind,
                    "identity": [int(metadata.st_dev), int(metadata.st_ino)],
                    "nlink": int(metadata.st_nlink),
                }
                raw = _read_regular(path)
                if raw is not None:
                    item["sha256"] = cc.sha256_bytes(raw)
                    try:
                        parsed = json.loads(raw.decode("utf-8"))
                        if isinstance(parsed, dict):
                            item["command_id"] = parsed.get("id")
                            item["status"] = parsed.get("status")
                    except (UnicodeError, json.JSONDecodeError, ValueError):
                        item["json"] = "invalid"
                entries.append(item)
            except OSError as exc:
                entries.append({"name": path.name, "error": type(exc).__name__})
        result["stages"][stage] = entries
    recovery = commands / RECOVERY_DIR_NAME
    try:
        children = sorted(recovery.iterdir(), key=lambda item: item.name)
    except OSError:
        children = []
    for path in children:
        try:
            metadata = os.lstat(path)
            item = {
                "name": path.name,
                "kind": "reparse" if os.path.islink(path) or _is_reparse(metadata) else (
                    "directory" if stat.S_ISDIR(metadata.st_mode) else "file"
                ),
                "identity": [int(metadata.st_dev), int(metadata.st_ino)],
                "nlink": int(metadata.st_nlink),
            }
            raw = _read_regular(path)
            if raw is not None:
                item["sha256"] = cc.sha256_bytes(raw)
            result["recovery"][path.name] = item
        except OSError as exc:
            result["recovery"][path.name] = {"error": type(exc).__name__}
    return result


def owners(root: pathlib.Path, command_id: str) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []
    snapshot = inventory(root)
    for stage, entries in snapshot["stages"].items():
        for item in entries:
            if item.get("command_id") == command_id:
                found.append({"stage": stage, **item})
    return found


def command(command_id: str, status: str = cc.PENDING_STATUS, **extra: Any) -> Dict[str, Any]:
    value: Dict[str, Any] = {
        "schema": cc.COMMAND_SCHEMA,
        "id": command_id,
        "status": status,
        "action": "set_paused",
        "args": {},
    }
    value.update(extra)
    return value


def final_command(command_id: str, operation: str = "rejected") -> Dict[str, Any]:
    return command(
        command_id,
        cc.DISPOSITION_STATUSES[operation],
        rejection_reason="a09a2-verifier",
    )


def setup_pending(root: pathlib.Path, command_id: str) -> Tuple[pathlib.Path, Dict[str, Any]]:
    root.mkdir(parents=True, exist_ok=True)
    value = command(command_id)
    path = cc.publish_pending_command(root, value)
    return path, value


def wait_message(connection: Any, timeout: float = 8.0) -> Dict[str, Any]:
    if not connection.poll(timeout):
        raise AssertionError("child did not send a deterministic synchronization message")
    value = connection.recv()
    if not isinstance(value, dict):
        raise AssertionError(f"child message is not a mapping: {value!r}")
    return value


def join_child(process: mp.Process, label: str, records: List[Dict[str, Any]], timeout: float = 12.0) -> None:
    process.join(timeout)
    if process.is_alive():
        process.terminate()
        process.join(3)
        records.append({"label": label, "pid": process.pid, "exitcode": process.exitcode, "residual": True})
        raise AssertionError(f"child remained alive after bounded join: {label}")
    records.append({"label": label, "pid": process.pid, "exitcode": process.exitcode, "residual": False})


def lock_worker(
    root_text: str,
    mode: str,
    ready: Any,
    go: Any,
    release: Any,
    connection: Any,
    timeout_seconds: float,
) -> None:
    root = pathlib.Path(root_text)
    try:
        if mode == "holder" or mode == "crash":
            with cc.lifecycle_custody(root, timeout_seconds=timeout_seconds):
                connection.send({"event": "entered", "pid": os.getpid()})
                if mode == "crash":
                    os._exit(0)
                release.wait(10)
                connection.send({"event": "released", "pid": os.getpid()})
            return
        ready.set()
        if not go.wait(10):
            connection.send({"event": "error", "error": "go timeout", "pid": os.getpid()})
            return
        started = time.monotonic()
        try:
            with cc.lifecycle_custody(root, timeout_seconds=timeout_seconds):
                connection.send({"event": "entered", "pid": os.getpid()})
                release.wait(10)
                connection.send({"event": "released", "pid": os.getpid()})
        except Exception as exc:
            connection.send(
                {
                    "event": "exception",
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
                    "pid": os.getpid(),
                }
            )
    except BaseException as exc:
        try:
            connection.send({"event": "error", "type": type(exc).__name__, "message": str(exc), "pid": os.getpid()})
        except Exception:
            pass
    finally:
        try:
            connection.close()
        except Exception:
            pass


def publication_worker(root_text: str, value: Mapping[str, Any], barrier: Any, connection: Any) -> None:
    try:
        barrier.wait(10)
        try:
            path = cc.publish_pending_command(pathlib.Path(root_text), dict(value))
            connection.send({"outcome": "success", "path": str(path), "pid": os.getpid()})
        except Exception as exc:
            connection.send({"outcome": "exception", "type": type(exc).__name__, "message": str(exc), "pid": os.getpid()})
    except BaseException as exc:
        try:
            connection.send({"outcome": "error", "type": type(exc).__name__, "message": str(exc), "pid": os.getpid()})
        except Exception:
            pass
    finally:
        connection.close()


def transition_worker(
    root_text: str,
    command_id: str,
    final_payload: Mapping[str, Any],
    barrier: Any,
    connection: Any,
) -> None:
    try:
        barrier.wait(10)
        root = pathlib.Path(root_text)
        source = root / "commands" / "pending" / f"{command_id}.json"
        target = root / "commands" / "rejected" / f"{command_id}.json"
        try:
            path = cc.transition_pending_envelope(
                root,
                source,
                target,
                dict(final_payload),
                operation="rejected",
            )
            connection.send({"outcome": "success", "path": str(path) if path else None, "pid": os.getpid()})
        except Exception as exc:
            connection.send({"outcome": "exception", "type": type(exc).__name__, "message": str(exc), "pid": os.getpid()})
    except BaseException as exc:
        try:
            connection.send({"outcome": "error", "type": type(exc).__name__, "message": str(exc), "pid": os.getpid()})
        except Exception:
            pass
    finally:
        connection.close()


def make_journal_fixture(root: pathlib.Path, command_id: str, transaction: str) -> pathlib.Path:
    """Create one valid staged journal for duplicate-journal testing only."""

    cc._ensure_layout(root)
    source = root / "commands" / "pending" / f"{command_id}.json"
    original = command(command_id)
    original_raw = cc._json_bytes(original)
    if not source.exists():
        cc._atomic_create_bytes(source, original_raw)
    source_meta = os.stat(source)
    recovery = root / "commands" / RECOVERY_DIR_NAME
    original_path, final_path, journal_path = cc._recovery_payload_paths(recovery, command_id, transaction)
    original_identity = cc._atomic_create_bytes(original_path, original_raw)
    final_raw = cc._json_bytes(final_command(command_id))
    final_identity = cc._atomic_create_bytes(final_path, final_raw)
    journal = cc._journal_value(
        bridge_dir=root,
        source=source,
        target=root / "commands" / "rejected" / f"{command_id}.json",
        command_id=command_id,
        operation="rejected",
        source_status=cc.PENDING_STATUS,
        original_hash=cc.sha256_bytes(original_raw),
        final_hash=cc.sha256_bytes(final_raw),
        original_identity=(int(source_meta.st_dev), int(source_meta.st_ino)),
        original_mtime_ns=int(getattr(source_meta, "st_mtime_ns", int(source_meta.st_mtime * 1_000_000_000))),
        original_payload=original_path,
        original_payload_identity=original_identity,
        final_payload=final_path,
        final_payload_identity=final_identity,
        transaction=transaction,
        phase="staged",
        journal_identity=(1, 1),
    )
    journal_identity = cc._atomic_create_bytes(journal_path, cc._journal_bytes(journal))
    journal["journal_identity"] = list(journal_identity)
    cc._rewrite_file_exact(
        journal_path,
        cc._journal_bytes(journal),
        expected_identity=journal_identity,
        label="fixture journal",
    )
    return journal_path


class Harness:
    def __init__(self, report_dir: pathlib.Path) -> None:
        self.report_dir = report_dir
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self.run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        self.temp_root = pathlib.Path(tempfile.mkdtemp(prefix="oni-a09a2-core-"))
        self.cases: List[Dict[str, Any]] = []
        self.child_records: List[Dict[str, Any]] = []
        self._children: Dict[int, Tuple[mp.Process, str]] = {}
        self.events: List[Dict[str, Any]] = []
        self._case: Optional[Dict[str, Any]] = None

    def case_root(self, label: str) -> pathlib.Path:
        root = self.temp_root / label
        root.mkdir(parents=True, exist_ok=True)
        return root

    def check(self, condition: bool, message: str, **details: Any) -> None:
        if self._case is not None:
            self._case["assertions"] += 1
        if not condition:
            raise AssertionError(message + (f" details={details!r}" if details else ""))

    def run_case(self, name: str, function: Callable[[], None]) -> None:
        record: Dict[str, Any] = {"name": name, "started_at": utc_now(), "assertions": 0, "passed": False}
        self._case = record
        try:
            function()
            record["passed"] = True
        except Exception as exc:
            record["error_type"] = type(exc).__name__
            record["error"] = str(exc)
            record["traceback"] = traceback.format_exc(limit=12)
        finally:
            record["finished_at"] = utc_now()
            self.cases.append(record)
            self._case = None

    def record_event(self, name: str, **fields: Any) -> None:
        self.events.append({"event": name, "at": utc_now(), **fields})

    def start_child(self, process: mp.Process, label: str) -> mp.Process:
        process.start()
        self._children[id(process)] = (process, label)
        return process

    def join_child(self, process: mp.Process, label: str, timeout: float = 12.0) -> None:
        try:
            join_child(process, label, self.child_records, timeout=timeout)
        finally:
            self._children.pop(id(process), None)

    def join_all_children(self) -> None:
        for process, label in list(self._children.values()):
            try:
                process.join(12.0)
                if process.is_alive():
                    process.terminate()
                    process.join(3.0)
                self.child_records.append(
                    {
                        "label": label,
                        "pid": process.pid,
                        "exitcode": process.exitcode,
                        "residual": process.is_alive(),
                    }
                )
            except Exception as exc:
                self.child_records.append(
                    {
                        "label": label,
                        "pid": process.pid,
                        "exitcode": process.exitcode,
                        "residual": True,
                        "error": type(exc).__name__,
                    }
                )
            finally:
                self._children.pop(id(process), None)


def case_lock_concurrency(h: Harness) -> None:
    root = h.case_root("lock-concurrency")
    cc.commands_root_for(root)
    context = mp.get_context("spawn")
    holder_ready = context.Event()
    contender_go = context.Event()
    contender_release = context.Event()
    holder_release = context.Event()
    holder_parent, holder_child = context.Pipe(False)
    contender_parent, contender_child = context.Pipe(False)
    holder = context.Process(target=lock_worker, args=(str(root), "holder", holder_ready, contender_go, holder_release, holder_child, 3.0))
    h.start_child(holder, "lock-holder")
    entered = wait_message(holder_parent)
    h.check(entered.get("event") == "entered", "holder did not acquire lifecycle OS lock", entered=entered)
    contender_ready = context.Event()
    contender = context.Process(
        target=lock_worker,
        args=(str(root), "contender", contender_ready, contender_go, contender_release, contender_child, 0.20),
    )
    h.start_child(contender, "lock-contender")
    h.check(contender_ready.wait(5), "contender did not reach deterministic barrier")
    contender_go.set()
    blocked = wait_message(contender_parent)
    h.check(blocked.get("event") == "exception", "contender entered while holder owned lock", result=blocked)
    h.check(blocked.get("type") == "CommandCustodyTimeout", "finite timeout was not reported", result=blocked)
    h.check(0 < float(blocked.get("elapsed_ms", 0)) < 2000, "timeout was not finite", result=blocked)
    holder_release.set()
    released = wait_message(holder_parent)
    h.check(released.get("event") == "released", "holder did not release normally", result=released)
    h.join_child(contender, "lock-contender")
    h.join_child(holder, "lock-holder")


def case_crash_release(h: Harness) -> None:
    root = h.case_root("crash-release")
    cc.commands_root_for(root)
    context = mp.get_context("spawn")
    holder_parent, holder_child = context.Pipe(False)
    holder = context.Process(target=lock_worker, args=(str(root), "crash", context.Event(), context.Event(), context.Event(), holder_child, 3.0))
    h.start_child(holder, "lock-crash-holder")
    entered = wait_message(holder_parent)
    h.check(entered.get("event") == "entered", "crash holder did not acquire lock", result=entered)
    h.join_child(holder, "lock-crash-holder")
    h.check(holder.exitcode == 0, "crash holder did not exit through os._exit(0)", exitcode=holder.exitcode)
    contender_ready = context.Event()
    contender_go = context.Event()
    contender_release = context.Event()
    contender_parent, contender_child = context.Pipe(False)
    contender = context.Process(
        target=lock_worker,
        args=(str(root), "contender", contender_ready, contender_go, contender_release, contender_child, 1.0),
    )
    h.start_child(contender, "crash-release-contender")
    h.check(contender_ready.wait(5), "crash-release contender did not reach barrier")
    contender_go.set()
    acquired = wait_message(contender_parent)
    h.check(acquired.get("event") == "entered", "OS lock was not released by crashed process", result=acquired)
    contender_release.set()
    h.check(wait_message(contender_parent).get("event") == "released", "crash-release contender did not exit lock")
    h.join_child(contender, "crash-release-contender")


def case_publication_and_disposition(h: Harness) -> None:
    root = h.case_root("interleavings")
    value = command("cmd-interleave")
    root.mkdir(parents=True, exist_ok=True)
    context = mp.get_context("spawn")
    barrier = context.Barrier(2)
    connections: List[Any] = []
    processes: List[mp.Process] = []
    for index in range(2):
        parent, child = context.Pipe(False)
        connections.append(parent)
        process = context.Process(target=publication_worker, args=(str(root), value, barrier, child))
        processes.append(process)
        h.start_child(process, f"publication-{index}")
    publication_results = [wait_message(connection) for connection in connections]
    for index, process in enumerate(processes):
        h.join_child(process, f"publication-{index}")
    outcomes = [item.get("outcome") for item in publication_results]
    h.check(outcomes.count("success") == 1, "two-process publication did not have one winner", results=publication_results)
    h.check(outcomes.count("exception") == 1, "two-process publication did not reject loser", results=publication_results)
    h.check(len(owners(root, "cmd-interleave")) == 1, "publication left duplicate command owners", inventory=inventory(root))

    duplicate = command("cmd-interleave")
    try:
        cc.publish_pending_command(root, duplicate)
    except cc.CommandCustodyConflict:
        duplicate_rejected = True
    else:
        duplicate_rejected = False
    h.check(duplicate_rejected, "direct duplicate publication was accepted")

    different = command("cmd-interleave", reason="different-body")
    try:
        cc.publish_pending_command(root, different)
    except cc.CommandCustodyConflict:
        different_rejected = True
    else:
        different_rejected = False
    h.check(different_rejected, "same id with different body was accepted")
    h.check(len(owners(root, "cmd-interleave")) == 1, "duplicate checks changed the original owner")

    disposition_root = h.case_root("disposition-interleave")
    setup_pending(disposition_root, "cmd-disposition")
    final = final_command("cmd-disposition")
    barrier = context.Barrier(2)
    connections = []
    processes = []
    for index in range(2):
        parent, child = context.Pipe(False)
        connections.append(parent)
        process = context.Process(target=transition_worker, args=(str(disposition_root), "cmd-disposition", final, barrier, child))
        processes.append(process)
        h.start_child(process, f"disposition-{index}")
    transition_results = [wait_message(connection) for connection in connections]
    for index, process in enumerate(processes):
        h.join_child(process, f"disposition-{index}")
    h.check(
        all(item.get("outcome") in {"success", "exception"} for item in transition_results),
        "disposition child returned an invalid result",
        results=transition_results,
    )
    h.check(len(owners(disposition_root, "cmd-disposition")) == 1, "disposition left duplicate command owners")
    h.check(len(owners(disposition_root, "cmd-disposition")[0:1]) == 1, "disposition lost the command owner")
    h.check(not any(item.get("command_id") == "cmd-disposition" for item in inventory(disposition_root)["stages"]["pending"]), "pending replay source survived disposition")
    h.check(not inventory(disposition_root)["recovery"], "disposition left replayable recovery evidence")


def run_fault(root: pathlib.Path, fault: str) -> Tuple[Optional[BaseException], Dict[str, Any]]:
    command_id = "cmd-fault-" + fault.replace("_", "-")
    source, _original = setup_pending(root, command_id)
    target = root / "commands" / "rejected" / source.name
    final = final_command(command_id)
    original_create = cc._atomic_create_bytes
    original_rename = cc._atomic_rename_noreplace
    original_fsync = cc._fsync_directory
    original_read = cc.read_stable_bytes
    original_phase = cc._rewrite_journal_phase
    original_unlink = cc._unlink_owned
    create_calls = 0

    def injected_create(path: pathlib.Path, payload: bytes) -> Tuple[int, int]:
        nonlocal create_calls
        create_calls += 1
        cutoff = {"preclaim": 1, "claimed": 2}.get(fault)
        if cutoff is not None and create_calls == cutoff:
            raise RuntimeError("A09A injected atomic create boundary")
        return original_create(path, payload)

    def injected_rename(source_path: pathlib.Path, target_path: pathlib.Path, **kwargs: Any) -> Tuple[int, int]:
        result = original_rename(source_path, target_path, **kwargs)
        if fault == "published" and target_path.parent.name == "rejected":
            raise RuntimeError("A09A injected after terminal rename")
        return result

    def injected_fsync(path: pathlib.Path) -> bool:
        result = original_fsync(path)
        if fault == "fsync" and path.name == "rejected":
            raise RuntimeError("A09A injected after terminal directory fsync")
        return result

    def injected_read(path: pathlib.Path, *, label: str = "queue file") -> Tuple[bytes, Tuple[int, int], int]:
        if fault == "revalidation" and label == "completed pending disposition":
            raise RuntimeError("A09A injected target revalidation failure")
        return original_read(path, label=label)

    def injected_phase(record: Any, phase: str) -> Any:
        result = original_phase(record, phase)
        if fault == "published-phase" and phase == "published":
            raise RuntimeError("A09A injected after journal published phase")
        return result

    def injected_unlink(path: pathlib.Path, *, expected_identity: Tuple[int, int], expected_bytes: bytes, label: str) -> None:
        if fault == "cleanup-before" and label == "pending source":
            raise RuntimeError("A09A injected before source cleanup")
        result = original_unlink(path, expected_identity=expected_identity, expected_bytes=expected_bytes, label=label)
        if fault == "cleanup-after" and label == "pending source":
            raise RuntimeError("A09A injected after source cleanup")
        if fault == "cleanup-journal" and label.startswith("lifecycle journal"):
            raise RuntimeError("A09A injected after journal cleanup")
        return result

    try:
        if fault in {"preclaim", "claimed"}:
            cc._atomic_create_bytes = injected_create  # type: ignore[assignment]
        elif fault == "published":
            cc._atomic_rename_noreplace = injected_rename  # type: ignore[assignment]
        elif fault == "fsync":
            cc._fsync_directory = injected_fsync  # type: ignore[assignment]
        elif fault == "revalidation":
            cc.read_stable_bytes = injected_read  # type: ignore[assignment]
        elif fault == "published-phase":
            cc._rewrite_journal_phase = injected_phase  # type: ignore[assignment]
        elif fault in {"cleanup-before", "cleanup-after", "cleanup-journal"}:
            cc._unlink_owned = injected_unlink  # type: ignore[assignment]
        try:
            cc.transition_pending_envelope(root, source, target, final, operation="rejected")
            error: Optional[BaseException] = None
        except BaseException as exc:
            error = exc
    finally:
        cc._atomic_create_bytes = original_create  # type: ignore[assignment]
        cc._atomic_rename_noreplace = original_rename  # type: ignore[assignment]
        cc._fsync_directory = original_fsync  # type: ignore[assignment]
        cc.read_stable_bytes = original_read  # type: ignore[assignment]
        cc._rewrite_journal_phase = original_phase  # type: ignore[assignment]
        cc._unlink_owned = original_unlink  # type: ignore[assignment]
    return error, inventory(root)


def case_failure_boundaries(h: Harness) -> None:
    recoverable = {"staged", "published", "fsync", "revalidation", "published-phase", "cleanup-before", "cleanup-after", "cleanup-journal"}
    for fault in ("preclaim", "claimed", "staged", "published", "fsync", "revalidation", "published-phase", "cleanup-before", "cleanup-after", "cleanup-journal"):
        root = h.case_root("fault-" + fault)
        if fault == "staged":
            original_rename = cc._atomic_rename_noreplace
            def before_terminal(source_path: pathlib.Path, target_path: pathlib.Path, **kwargs: Any) -> Tuple[int, int]:
                if target_path.parent.name == "rejected":
                    raise RuntimeError("A09A injected before terminal rename")
                return original_rename(source_path, target_path, **kwargs)
            cc._atomic_rename_noreplace = before_terminal  # type: ignore[assignment]
            try:
                error, snapshot = run_fault(root, "staged")
            finally:
                cc._atomic_rename_noreplace = original_rename  # type: ignore[assignment]
        else:
            error, snapshot = run_fault(root, fault)
        h.check(isinstance(error, cc.CommandCustodyUnknown), f"fault {fault} was not reported unknown", error=repr(error))
        command_id = "cmd-fault-" + fault.replace("_", "-")
        try:
            recovery = cc.recover_lifecycle_transactions(root)
            recovery_error: Optional[BaseException] = None
        except BaseException as exc:
            recovery = None
            recovery_error = exc
        h.record_event("fault-boundary", fault=fault, error=type(error).__name__ if error else None, recovery_error=type(recovery_error).__name__ if recovery_error else None, snapshot=snapshot)
        found = owners(root, command_id)
        if fault in {"preclaim"}:
            h.check(len(found) == 1 and found[0]["stage"] == "pending", f"fault {fault} lost original owner", owners=found)
            h.check(recovery_error is None, f"fault {fault} left unexpected recovery blocker", error=repr(recovery_error))
        elif fault == "claimed":
            h.check(recovery_error is not None, "claimed fault did not retain orphan evidence")
            h.check(any(name.endswith(".original.bytes") for name in inventory(root)["recovery"]), "claimed fault lost original payload evidence")
        elif fault in recoverable:
            h.check(recovery_error is None, f"fault {fault} did not recover a provable terminal/source state", error=repr(recovery_error))
            found = owners(root, command_id)
            if fault == "staged":
                h.check(len(found) == 1 and found[0]["stage"] == "pending", f"staged fault did not restore pending owner", owners=found)
            else:
                h.check(len(found) == 1 and found[0]["stage"] == "rejected", f"fault {fault} did not preserve one terminal owner", owners=found)
                h.check(not inventory(root)["recovery"], f"fault {fault} left replayable recovery evidence", recovery=inventory(root)["recovery"])
        else:
            h.check(False, f"unclassified fault {fault}")


def case_journal_fail_closed(h: Harness) -> None:
    malformed = h.case_root("journal-malformed")
    cc.commands_root_for(malformed)
    recovery_dir = malformed / "commands" / RECOVERY_DIR_NAME
    recovery_dir.mkdir(parents=True, exist_ok=True)
    (recovery_dir / "bad.lifecycle.json").write_text("{}\n", encoding="utf-8")
    try:
        cc.recover_lifecycle_transactions(malformed)
    except cc.CommandCustodyUnknown:
        malformed_blocked = True
    else:
        malformed_blocked = False
    h.check(malformed_blocked, "malformed journal was treated as nonblocking")
    h.check("bad.lifecycle.json" in cc.lifecycle_recovery_blockers(malformed), "malformed journal was not visible as evidence")

    unreadable = h.case_root("journal-unreadable")
    cc.commands_root_for(unreadable)
    unreadable_recovery = unreadable / "commands" / RECOVERY_DIR_NAME
    unreadable_recovery.mkdir(parents=True, exist_ok=True)
    (unreadable_recovery / "unreadable.lifecycle.json").mkdir()
    try:
        cc.recover_lifecycle_transactions(unreadable)
    except cc.CommandCustodyUnknown:
        unreadable_blocked = True
    else:
        unreadable_blocked = False
    h.check(unreadable_blocked, "unreadable journal was treated as nonblocking")

    orphan = h.case_root("journal-orphan-payload")
    cc.commands_root_for(orphan)
    orphan_recovery = orphan / "commands" / RECOVERY_DIR_NAME
    orphan_recovery.mkdir(parents=True, exist_ok=True)
    (orphan_recovery / "orphan.original.bytes").write_bytes(b"orphan")
    try:
        cc.recover_lifecycle_transactions(orphan)
    except cc.CommandCustodyUnknown:
        orphan_blocked = True
    else:
        orphan_blocked = False
    h.check(orphan_blocked, "orphan payload was silently removed or ignored")

    duplicate = h.case_root("journal-duplicate")
    make_journal_fixture(duplicate, "cmd-duplicate-journal", "tx-" + "1" * 32)
    make_journal_fixture(duplicate, "cmd-duplicate-journal", "tx-" + "2" * 32)
    try:
        cc.recover_lifecycle_transactions(duplicate)
    except cc.CommandCustodyUnknown:
        duplicate_blocked = True
    else:
        duplicate_blocked = False
    h.check(duplicate_blocked, "duplicate journals claiming one command were not blocked")


def case_path_integrity(h: Harness) -> None:
    hardlink_root = h.case_root("hardlink")
    source, _ = setup_pending(hardlink_root, "cmd-hardlink")
    hardlink_target = hardlink_root / "adversarial-hardlink-target.json"
    hardlink_target.write_bytes(source.read_bytes())
    os.link(hardlink_target, hardlink_root / "commands" / "rejected" / source.name)
    try:
        cc.recover_lifecycle_transactions(hardlink_root)
    except cc.CommandCustodyUnknown:
        hardlink_blocked = True
    else:
        hardlink_blocked = False
    h.check(hardlink_blocked, "hard-linked protected command was accepted")

    unsafe = h.case_root("unsafe-ancestor")
    (unsafe / "commands").write_text("not a directory", encoding="utf-8")
    try:
        cc.commands_root_for(unsafe)
    except cc.CommandCustodyConflict:
        unsafe_blocked = True
    else:
        unsafe_blocked = False
    h.check(unsafe_blocked, "unsafe non-directory ancestor was followed or replaced")

    reparse = h.case_root("reparse-ancestor")
    target = h.temp_root / "adversarial-reparse-target"
    target.mkdir(parents=True, exist_ok=True)
    link = reparse / "commands"
    link_created = False
    link_kind = "unavailable"
    try:
        link.symlink_to(target, target_is_directory=True)
        link_created = True
        link_kind = "symlink"
    except (OSError, NotImplementedError):
        if os.name == "nt":
            result = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
                capture_output=True,
                text=True,
                check=False,
            )
            link_created = result.returncode == 0
            link_kind = "junction" if link_created else "unavailable"
    if link_created:
        try:
            cc.commands_root_for(reparse)
        except cc.CommandCustodyConflict:
            reparse_blocked = True
        else:
            reparse_blocked = False
        h.check(reparse_blocked, "reparse/junction ancestor was followed", link_kind=link_kind)
    else:
        h.check(False, "could not create a reparse or junction adversarial fixture", link_kind=link_kind)

    moved = h.case_root("directory-rename")
    setup_root = moved / "original"
    setup_root.mkdir(parents=True, exist_ok=True)
    cc.commands_root_for(setup_root)
    moved_root = moved / "moved-away"
    setup_root.rename(moved_root)
    try:
        cc.publish_pending_command(setup_root, command("cmd-after-directory-rename"))
    except cc.CommandCustodyError:
        rename_blocked = True
    else:
        rename_blocked = False
    h.check(rename_blocked, "renamed Bridge directory was silently recreated")

    tampered = h.case_root("tampered-stage")
    source, _ = setup_pending(tampered, "cmd-tampered-stage")
    tampered_target = tampered / "commands" / "rejected" / source.name
    source.rename(tampered_target)
    try:
        cc.publish_pending_command(tampered, command("cmd-other"))
    except cc.CommandCustodyError:
        stage_blocked = True
    else:
        stage_blocked = False
    h.check(stage_blocked, "protected stage/status mismatch was accepted")


def case_cleanup_replacement(h: Harness) -> None:
    root = h.case_root("cleanup-replacement")
    command_id = "cmd-cleanup-replacement"
    source, _ = setup_pending(root, command_id)
    target = root / "commands" / "rejected" / source.name
    final = final_command(command_id)
    original_read = cc.read_stable_bytes
    replaced = False

    def replacing_read(path: pathlib.Path, *, label: str = "queue file") -> Tuple[bytes, Tuple[int, int], int]:
        nonlocal replaced
        result = original_read(path, label=label)
        if label == "pending source" and not replaced:
            replacement = path.with_name(path.name + ".replacement")
            replacement.write_bytes(cc._json_bytes(command("cmd-unrelated-replacement")))
            os.replace(replacement, path)
            replaced = True
        return result

    cc.read_stable_bytes = replacing_read  # type: ignore[assignment]
    try:
        try:
            cc.transition_pending_envelope(root, source, target, final, operation="rejected")
        except cc.CommandCustodyUnknown:
            cleanup_blocked = True
        else:
            cleanup_blocked = False
    finally:
        cc.read_stable_bytes = original_read  # type: ignore[assignment]
    h.check(cleanup_blocked, "unrelated replacement was deleted or reported as success")
    h.check((root / "commands" / "pending" / source.name).exists(), "unrelated replacement did not survive")
    h.check(len(owners(root, command_id)) == 1, "terminal owner was not retained after cleanup uncertainty")
    h.check(any(item.get("command_id") == "cmd-unrelated-replacement" for item in inventory(root)["stages"]["pending"]), "replacement identity evidence disappeared")


def case_public_surface(h: Harness) -> None:
    public_mutators = {name for name in cc.__all__ if name in {"atomic_create_bytes", "atomic_rename_noreplace", "publish_pending_command", "transition_pending_envelope", "recover_lifecycle_transactions"}}
    h.check("publish_pending_command" in public_mutators, "pending publication is not in public surface")
    h.check("transition_pending_envelope" in public_mutators, "pending disposition is not in public surface")
    h.check("recover_lifecycle_transactions" in public_mutators, "recovery is not in public surface")
    h.check("atomic_create_bytes" not in public_mutators and "atomic_rename_noreplace" not in public_mutators, "raw write helpers are exported as public mutators")


def forbidden_process_census() -> Dict[str, Any]:
    try:
        output = subprocess.check_output(["tasklist", "/FO", "CSV", "/NH"], text=True, errors="replace")
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "unavailable", "error": type(exc).__name__}
    names: List[str] = []
    for line in output.splitlines():
        if not line.startswith('"'):
            continue
        name = line.split('",', 1)[0].strip('"')
        if name.casefold() in FORBIDDEN_PROCESS_NAMES:
            names.append(name)
    return {
        "status": "ok",
        "matching_rule": "exact executable basename, case-insensitive",
        "forbidden_processes": sorted(set(names)),
    }


def main() -> int:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    started = utc_now()
    before = source_hashes()
    h = Harness(REPORT_DIR)
    h.record_event("run-start", temp_root=str(h.temp_root), source_hashes=before)
    try:
        h.run_case("lock-concurrency", lambda: case_lock_concurrency(h))
        h.run_case("crash-release", lambda: case_crash_release(h))
        h.run_case("publication-and-disposition-interleavings", lambda: case_publication_and_disposition(h))
        h.run_case("failure-injection-boundaries", lambda: case_failure_boundaries(h))
        h.run_case("journal-fail-closed", lambda: case_journal_fail_closed(h))
        h.run_case("path-integrity", lambda: case_path_integrity(h))
        h.run_case("cleanup-replacement", lambda: case_cleanup_replacement(h))
        h.run_case("public-surface", lambda: case_public_surface(h))
    finally:
        h.join_all_children()
        before_cleanup = str(h.temp_root)
        try:
            safe_remove(h.temp_root)
        except Exception as exc:
            h.events.append({"event": "temp-cleanup-error", "at": utc_now(), "error": repr(exc)})
        h.events.append({"event": "temp-cleanup", "at": utc_now(), "path": before_cleanup, "exists_after": h.temp_root.exists()})
    after = source_hashes()
    cases_passed = all(bool(case.get("passed")) for case in h.cases)
    report: Dict[str, Any] = {
        "schema": "oni.a09a2-command-custody-core.v1",
        "run_id": h.run_id,
        "started_at": started,
        "finished_at": utc_now(),
        "passed": cases_passed,
        "exit_code": 0 if cases_passed else 1,
        "case_count": len(h.cases),
        "passed_case_count": sum(1 for case in h.cases if case.get("passed")),
        "assertion_count": sum(int(case.get("assertions", 0)) for case in h.cases),
        "failed_cases": [case["name"] for case in h.cases if not case.get("passed")],
        "cases": h.cases,
        "events": h.events,
        "source_hashes": {"at_start": before, "at_finish": after},
        "range_scope": {
            "allowed_source": "agent/command_custody.py",
            "allowed_new_verifier": "scripts/verify_command_custody_core.py",
            "allowed_new_document": "docs/a09a2-command-custody-core.md",
            "allowed_evidence_dir": "reports/a09a2-command-custody-core/",
            "forbidden_source_hash_unchanged": before.get("agent/command_queue.py") == after.get("agent/command_queue.py"),
        },
        "child_processes": h.child_records,
        "child_residual_count": sum(1 for item in h.child_records if item.get("residual")),
        "forbidden_process_census": forbidden_process_census(),
        "temp_root_removed": not h.temp_root.exists(),
        "limitations": [
            "Offline filesystem/process evidence only; no Bridge, game, save, Mod, approval, or live admission evidence.",
            "Trust is local filesystem custody, not protection from an administrator who can replace the process or root.",
        ],
    }
    report_path = REPORT_DIR / "command-custody-core.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log_path = REPORT_DIR / "command-custody-core.events.jsonl"
    log_path.write_text("".join(json.dumps(event, ensure_ascii=False) + "\n" for event in h.events), encoding="utf-8")
    print(json.dumps({"passed": cases_passed, "case_count": len(h.cases), "assertions": report["assertion_count"], "report": str(report_path)}, ensure_ascii=False))
    return 0 if cases_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
