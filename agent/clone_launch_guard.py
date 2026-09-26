"""Fail-closed preflight for a future named-clone ONI launcher.

This module deliberately has no process-launch or filesystem-mutation surface.
It validates the immutable clone manifest and the current runtime boundary so a
launcher can only be called after the named clone is proven to be the selected
input.  The caller supplies process and queue observations; no global discovery
is performed here.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shlex
import shutil
import stat
import subprocess
from typing import Any, Callable, Dict, Iterable, Mapping

from agent.command_custody import CommandCustodyError, read_stable_bytes


class CloneLaunchGuardError(RuntimeError):
    pass


def _sha256(path: pathlib.Path) -> str:
    try:
        payload, _, _ = read_stable_bytes(path, label="clone file")
    except (CommandCustodyError, OSError) as exc:
        raise CloneLaunchGuardError("Clone file changed or is unreadable.") from exc
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _path_id(path: pathlib.Path) -> str:
    normalized = str(path.expanduser().resolve()).lower().encode("utf-8")
    return "sha256:" + hashlib.sha256(normalized).hexdigest()


def _regular_file(path: pathlib.Path, label: str) -> pathlib.Path:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise CloneLaunchGuardError(f"{label} is not readable.") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise CloneLaunchGuardError(f"{label} must be a regular file.")
    if path.is_symlink():
        raise CloneLaunchGuardError(f"{label} must not be a symlink.")
    return path


def _load_manifest(path: pathlib.Path) -> Dict[str, Any]:
    try:
        payload, _, _ = read_stable_bytes(path, label="clone manifest")
        value = json.loads(payload.decode("utf-8-sig"))
    except (CommandCustodyError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CloneLaunchGuardError("The clone manifest is unreadable.") from exc
    if not isinstance(value, dict) or value.get("schema") != "oni.named-test-clone.v1":
        raise CloneLaunchGuardError("The clone manifest schema is invalid.")
    if value.get("created") is not True:
        raise CloneLaunchGuardError("The clone manifest is not marked created.")
    for branch in ("source", "target"):
        if not isinstance(value.get(branch), dict):
            raise CloneLaunchGuardError(f"The clone manifest {branch} record is missing.")
    return value


def validate_clone_launch(
    manifest_path: pathlib.Path,
    clone_save: pathlib.Path,
    clone_preview: pathlib.Path,
    *,
    bridge_dir: pathlib.Path,
    process_names: Iterable[str] = (),
    queue_counts: Mapping[str, int] | None = None,
    process_probe: Callable[[], Mapping[str, int]] | None = None,
    queue_probe: Callable[[], Mapping[str, int]] | None = None,
) -> Dict[str, Any]:
    """Validate a named clone and an empty runtime boundary without mutation."""
    manifest_file = _regular_file(manifest_path.expanduser().resolve(), "The clone manifest")
    manifest = _load_manifest(manifest_file)
    save = _regular_file(clone_save.expanduser().resolve(), "The clone save")
    preview = _regular_file(clone_preview.expanduser().resolve(), "The clone preview")
    if save.parent != preview.parent:
        raise CloneLaunchGuardError("The clone save and preview must share one directory.")
    source = manifest["source"]
    target = manifest["target"]
    expected_save = str(target.get("save_sha256") or "")
    expected_preview = str(target.get("preview_sha256") or "")
    if _sha256(save) != expected_save or _sha256(preview) != expected_preview:
        raise CloneLaunchGuardError("The clone content does not match its manifest.")
    if _path_id(save) == str(source.get("save", {}).get("path_id") or ""):
        raise CloneLaunchGuardError("The selected clone save resolves to the source save path.")
    if _path_id(preview) == str(source.get("preview", {}).get("path_id") or ""):
        raise CloneLaunchGuardError("The selected clone preview resolves to the source preview path.")
    try:
        counts = dict(process_probe() if process_probe is not None else {})
    except Exception as exc:
        raise CloneLaunchGuardError("The process boundary is unreadable.") from exc
    names = tuple(str(name) for name in process_names)
    if any(type(count) is not int or count < 0 for count in counts.values()):
        raise CloneLaunchGuardError("Process counts must be non-negative integers.")
    if any(counts.get(name, 0) != 0 for name in names):
        raise CloneLaunchGuardError("A protected game or Bridge process is already running.")
    if queue_counts is not None and queue_probe is not None:
        raise CloneLaunchGuardError("Specify queue_counts or queue_probe, not both.")
    try:
        queues = dict(queue_probe() if queue_probe is not None else (queue_counts or {}))
    except Exception as exc:
        raise CloneLaunchGuardError("The queue boundary is unreadable.") from exc
    if any(type(count) is not int or count < 0 for count in queues.values()):
        raise CloneLaunchGuardError("Queue counts must be non-negative integers.")
    nonempty = {str(name): count for name, count in queues.items() if count != 0}
    if nonempty:
        raise CloneLaunchGuardError("Protected command or validation queues are not empty.")
    return {
        "schema": "oni.clone-launch-preflight.v1",
        "manifest_sha256": _sha256(manifest_file),
        "clone_save_sha256": _sha256(save),
        "clone_preview_sha256": _sha256(preview),
        "clone_save_path_id": _path_id(save),
        "clone_preview_path_id": _path_id(preview),
        "bridge_dir": str(bridge_dir.expanduser().resolve()),
        "process_counts": {name: int(counts.get(name, 0)) for name in names},
        "queue_counts": queues,
        "launch_allowed": True,
    }


def build_clone_probe(
    manifest_path: pathlib.Path,
    clone_save: pathlib.Path,
    clone_preview: pathlib.Path,
    *,
    bridge_dir: pathlib.Path,
    process_names: Iterable[str] = (),
    process_probe: Callable[[], Mapping[str, int]] | None = None,
    queue_probe: Callable[[], Mapping[str, int]] | None = None,
) -> Callable[[], Dict[str, Any]]:
    """Return a probe that re-reads clone files and runtime boundaries each call.

    The returned callback has no launch or mutation surface.  It is intended
    for a coordinator tick to revalidate the same manifest/content/path IDs,
    rather than treating a caller-supplied preflight mapping as durable proof.
    """
    frozen_names = tuple(str(name) for name in process_names)

    def probe() -> Dict[str, Any]:
        return validate_clone_launch(
            manifest_path,
            clone_save,
            clone_preview,
            bridge_dir=bridge_dir,
            process_names=frozen_names,
            process_probe=process_probe,
            queue_probe=queue_probe,
        )

    return probe


_PYTHON_PROCESS_NAMES = frozenset({"python", "pythonw", "py"})
_BRIDGE_SCRIPT_NAME = "oni_bridge.py"


def _process_basename(value: Any) -> str:
    """Return a case-folded Windows-compatible executable basename."""
    text = str(value or "").strip().replace("/", "\\")
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        text = text[1:-1]
    basename = text.rsplit("\\", 1)[-1]
    return basename.casefold()


def _process_key(value: Any) -> str:
    basename = _process_basename(value)
    return basename[:-4] if basename.endswith(".exe") else basename


def _command_line_argv(command_line: Any) -> list[str]:
    """Parse a native command line without treating data arguments as scripts."""
    if isinstance(command_line, (list, tuple)):
        if not command_line or any(not isinstance(item, str) or not item for item in command_line):
            raise CloneLaunchGuardError("The process command line is unreadable.")
        return list(command_line)
    if not isinstance(command_line, str) or not command_line.strip():
        raise CloneLaunchGuardError("The process command line is unreadable.")
    if os.name != "nt":
        try:
            return shlex.split(command_line, posix=False)
        except ValueError as exc:
            raise CloneLaunchGuardError("The process command line is invalid.") from exc

    try:
        import ctypes

        argc = ctypes.c_int(0)
        parser = ctypes.windll.shell32.CommandLineToArgvW
        parser.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
        parser.restype = ctypes.POINTER(ctypes.c_wchar_p)
        argv_pointer = parser(command_line, ctypes.byref(argc))
        if not argv_pointer:
            raise CloneLaunchGuardError("The process command line is invalid.")
        try:
            return [argv_pointer[index] for index in range(argc.value)]
        finally:
            local_free = ctypes.windll.kernel32.LocalFree
            local_free.argtypes = [ctypes.c_void_p]
            local_free.restype = ctypes.c_void_p
            local_free(ctypes.cast(argv_pointer, ctypes.c_void_p))
    except CloneLaunchGuardError:
        raise
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        raise CloneLaunchGuardError("The process command line could not be parsed.") from exc


def _is_bridge_command(command_line: Any) -> bool:
    """Match only the Python script argv, never a string in ``-c`` or data."""
    argv = _command_line_argv(command_line)
    if not argv:
        return False
    index = 1
    while index < len(argv):
        argument = str(argv[index])
        folded = argument.casefold()
        if folded in {"-c", "-m"} or folded.startswith("-c") or folded.startswith("-m"):
            return False
        if folded in {"-w", "-x", "--check-hash-based-pycs"}:
            index += 2
            continue
        if folded == "--":
            index += 1
            break
        if folded.startswith("-"):
            index += 1
            continue
        break
    if index >= len(argv):
        return False
    return _process_basename(argv[index]) == _BRIDGE_SCRIPT_NAME


def _record_value(record: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in record:
            return record[key]
    folded = {str(key).casefold(): value for key, value in record.items()}
    for key in keys:
        if key.casefold() in folded:
            return folded[key.casefold()]
    return None


def _record_matches(record: Mapping[str, Any], target: str) -> bool:
    process_name = _record_value(record, "name", "Name", "process_name", "ProcessName")
    executable = _record_value(record, "exe", "ExecutablePath", "executable")
    command_line = _record_value(record, "cmdline", "CommandLine", "command_line")
    target_key = _process_key(target)
    if target_key in {"oni_bridge", "oni_bridge.py"}:
        if _process_key(process_name) in _PYTHON_PROCESS_NAMES or _process_key(executable) in _PYTHON_PROCESS_NAMES:
            if not isinstance(command_line, (str, list, tuple)) or not command_line:
                raise CloneLaunchGuardError("The Bridge process command line is unreadable.")
            return _is_bridge_command(command_line)
        return _process_key(process_name) in {"oni_bridge", "oni_bridge.py"} or _process_key(executable) in {"oni_bridge", "oni_bridge.py"}
    return target_key in {_process_key(process_name), _process_key(executable)}


def _windows_process_records() -> list[dict[str, Any]]:
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if not powershell:
        raise CloneLaunchGuardError("The process table is unreadable.")
    command = (
        "$ErrorActionPreference='Stop';"
        "@(Get-CimInstance Win32_Process -ErrorAction Stop | "
        "Select-Object Name,ExecutablePath,CommandLine) | "
        "ConvertTo-Json -Depth 3 -Compress"
    )
    try:
        completed = subprocess.run(
            [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CloneLaunchGuardError("The process table is unreadable.") from exc
    if completed.returncode != 0:
        raise CloneLaunchGuardError("The process table query failed.")
    try:
        payload = json.loads(completed.stdout or "[]")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CloneLaunchGuardError("The process table query returned invalid data.") from exc
    if payload is None:
        return []
    if isinstance(payload, Mapping):
        payload = [payload]
    if not isinstance(payload, list) or any(not isinstance(item, Mapping) for item in payload):
        raise CloneLaunchGuardError("The process table query returned invalid data.")
    return [dict(item) for item in payload]


def build_process_probe(
    process_names: Iterable[str],
    *,
    lister: Callable[[], Iterable[Any]] | None = None,
) -> Callable[[], Dict[str, int]]:
    """Build a fail-closed process-count probe for a coordinator boundary.

    Structured records must include the process name/executable and, for a
    Python candidate, its native command line.  The Bridge is counted only
    when the first Python script argv is exactly ``oni_bridge.py``; a literal
    mention in ``-c``, ``-m`` or a later data argument is ignored.  The
    default uses ``psutil`` or a Windows CIM query and never turns a denied
    process-table read into a zero count.
    """
    names = tuple(str(name) for name in process_names)
    if not names or any(not name.strip() for name in names):
        raise CloneLaunchGuardError("Protected process names are required.")

    def default_lister() -> Iterable[Any]:
        try:
            import psutil  # type: ignore
        except ImportError:
            psutil = None  # type: ignore[assignment]
        if psutil is not None:
            records: list[dict[str, Any]] = []
            try:
                iterator = psutil.process_iter(["name", "exe", "cmdline"])
                for process in iterator:
                    try:
                        info = dict(process.info)
                    except getattr(psutil, "NoSuchProcess", OSError):
                        continue
                    except (OSError, PermissionError) as exc:
                        raise CloneLaunchGuardError("The process table is unreadable.") from exc
                    candidate = _process_key(info.get("name")) in _PYTHON_PROCESS_NAMES or _process_key(info.get("exe")) in _PYTHON_PROCESS_NAMES
                    if candidate and ("cmdline" not in info or not isinstance(info.get("cmdline"), (list, tuple))):
                        raise CloneLaunchGuardError("The Bridge process command line is unreadable.")
                    records.append({
                        "name": info.get("name"), "exe": info.get("exe"),
                        "cmdline": list(info.get("cmdline", ()))
                        if isinstance(info.get("cmdline"), (list, tuple)) else info.get("cmdline"),
                    })
            except CloneLaunchGuardError:
                raise
            except (OSError, PermissionError) as exc:
                raise CloneLaunchGuardError("The process table is unreadable.") from exc
            return records
        if os.name == "nt":
            return _windows_process_records()
        proc = pathlib.Path("/proc")
        if proc.is_dir():
            records = []
            for entry in proc.iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    name = (entry / "comm").read_text(encoding="utf-8", errors="replace").strip()
                    raw = (entry / "cmdline").read_bytes()
                except OSError as exc:
                    raise CloneLaunchGuardError("The process table is unreadable.") from exc
                records.append({"name": name, "cmdline": raw.replace(b"\x00", b" ").decode(errors="replace")})
            return records
        raise CloneLaunchGuardError("No supported process table is available.")

    def probe() -> Dict[str, int]:
        try:
            observed = list((lister or default_lister)())
        except CloneLaunchGuardError:
            raise
        except Exception as exc:
            raise CloneLaunchGuardError("The process table is unreadable.") from exc
        counts = {name: 0 for name in names}
        for raw in observed:
            if isinstance(raw, Mapping):
                for name in names:
                    if _record_matches(raw, name):
                        counts[name] += 1
                continue
            value = str(raw)
            for name in names:
                if _process_key(value) == _process_key(name):
                    counts[name] += 1
        return counts

    return probe


def build_queue_probe(
    bridge_dir: pathlib.Path,
    queue_names: Iterable[str],
) -> Callable[[], Dict[str, int]]:
    """Build a read-only probe that counts existing queue-directory entries."""
    root = pathlib.Path(bridge_dir).expanduser().resolve()
    names = tuple(str(name) for name in queue_names)
    if not names or any(not name.strip() for name in names):
        raise CloneLaunchGuardError("Queue names are required.")

    def probe() -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for name in names:
            path = root / name
            try:
                metadata = path.lstat()
            except OSError as exc:
                raise CloneLaunchGuardError("A protected queue is unreadable.") from exc
            if not stat.S_ISDIR(metadata.st_mode) or path.is_symlink():
                raise CloneLaunchGuardError("A protected queue must be a real directory.")
            try:
                counts[name] = sum(1 for _ in path.iterdir())
            except OSError as exc:
                raise CloneLaunchGuardError("A protected queue is unreadable.") from exc
        return counts

    return probe
