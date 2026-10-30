#!/usr/bin/env python3
"""Start and stop one tool-API game session, without mouse or keyboard.

    python scripts/api_session.py start --manifest M --save S --run-dir D
    python scripts/api_session.py stop --run-dir D

``start`` checks the clone against its manifest, keeps a copy of the game's
preferences (loading a save moves the game's "last save" pointer), writes the
autoload request and asks Steam to launch the game.  It returns at once, so the
game is never a child of a long-lived command.  ``stop`` closes the game the way
the window's close button does and puts the preferences back.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes
import csv
import datetime
import hashlib
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, List, Mapping

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.clone_launch_guard import (  # noqa: E402
    CloneLaunchGuardError,
    build_process_probe,
    validate_clone_launch,
)

GAME_PROCESS = "OxygenNotIncluded.exe"
STEAM_LAUNCH_URL = "steam://rungameid/457140"
WM_CLOSE = 0x0010
STOP_TIMEOUT_SECONDS = 25.0


def oni_root() -> pathlib.Path:
    return pathlib.Path.home() / "Documents" / "Klei" / "OxygenNotIncluded"


def sha256(path: pathlib.Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def write_json_atomic(path: pathlib.Path, payload: Mapping[str, Any]) -> None:
    pending = path.with_name(path.name + ".tmp")
    pending.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(pending, path)


def game_process_ids() -> List[int]:
    listing = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {GAME_PROCESS}", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        errors="replace",  # tasklist answers in the console's code page
        check=True,
    ).stdout
    return [int(row[1]) for row in csv.reader(io.StringIO(listing)) if len(row) > 1 and row[0] == GAME_PROCESS]


def start(args: argparse.Namespace) -> Dict[str, Any]:
    root = oni_root()
    bridge = root / "ai-bridge"
    save = pathlib.Path(args.save).resolve()
    preview = save.with_suffix(".png")
    run_dir = pathlib.Path(args.run_dir).resolve()
    if (run_dir / "session.json").exists():
        raise CloneLaunchGuardError("This run directory already holds a session; evidence is never overwritten.")

    preflight = validate_clone_launch(
        pathlib.Path(args.manifest),
        save,
        preview,
        bridge_dir=bridge,
        process_names=(GAME_PROCESS,),
        process_probe=build_process_probe((GAME_PROCESS,)),
    )

    run_dir.mkdir(parents=True, exist_ok=True)
    prefs = root / "kplayerprefs.yaml"
    prefs_backup = run_dir / "kplayerprefs.before.yaml"
    shutil.copy2(prefs, prefs_backup)
    session = {
        "schema": "oni.api-session.v1",
        "started_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "save": str(save),
        "preflight": preflight,
        "kplayerprefs_sha256": sha256(prefs),
        "installed_dll_sha256": sha256(root / "mods" / "Dev" / "oni-ai-colony-agent" / "OniAiProbeMod.dll"),
    }
    write_json_atomic(run_dir / "session.json", session)
    write_json_atomic(
        bridge / "autoload.json",
        {
            "schema": "oni.api-autoload.v1",
            "save": str(save),
            "created_utc": session["started_utc"],
        },
    )
    os.startfile(STEAM_LAUNCH_URL)  # type: ignore[attr-defined]
    return session


def _close_game_windows(process_ids: List[int]) -> int:
    """Post WM_CLOSE to the game's Unity window, like its close button."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    hwnd = ctypes.wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [hwnd, ctypes.POINTER(ctypes.wintypes.DWORD)]
    user32.GetClassNameW.argtypes = [hwnd, ctypes.wintypes.LPWSTR, ctypes.c_int]
    user32.PostMessageW.argtypes = [hwnd, ctypes.wintypes.UINT, ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM]
    wanted = set(process_ids)
    closed = 0

    @ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL, hwnd, ctypes.wintypes.LPARAM)
    def visit(window: Any, _: int) -> bool:
        nonlocal closed
        owner = ctypes.wintypes.DWORD()
        user32.GetWindowThreadProcessId(window, ctypes.byref(owner))
        name = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(window, name, 64)
        if owner.value in wanted and name.value == "UnityWndClass":
            user32.PostMessageW(window, WM_CLOSE, 0, 0)
            closed += 1
        return True

    user32.EnumWindows(visit, 0)
    return closed


def stop(args: argparse.Namespace) -> Dict[str, Any]:
    root = oni_root()
    run_dir = pathlib.Path(args.run_dir).resolve()
    session = json.loads((run_dir / "session.json").read_text(encoding="utf-8"))

    process_ids = game_process_ids()
    if process_ids:
        _close_game_windows(process_ids)
        deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
        while game_process_ids() and time.monotonic() < deadline:
            time.sleep(0.5)
    remaining = game_process_ids()
    if remaining:
        # Preferences are written at exit, so restoring now would be undone.
        raise CloneLaunchGuardError(f"The game is still running: {remaining}. Nothing was restored.")

    prefs = root / "kplayerprefs.yaml"
    shutil.copy2(run_dir / "kplayerprefs.before.yaml", prefs)
    bridge = root / "ai-bridge"
    report = {
        "schema": "oni.api-session-end.v1",
        "stopped_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "kplayerprefs_restored": sha256(prefs) == session["kplayerprefs_sha256"],
        "save_unchanged": sha256(pathlib.Path(session["save"])) == session["preflight"]["clone_save_sha256"],
        "api_file_left": (bridge / "api.json").exists(),
        "autoload_left": (bridge / "autoload.json").exists(),
    }
    write_json_atomic(run_dir / "session-end.json", report)
    return report


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    start_parser = commands.add_parser("start")
    start_parser.add_argument("--manifest", required=True)
    start_parser.add_argument("--save", required=True)
    start_parser.add_argument("--run-dir", required=True)
    stop_parser = commands.add_parser("stop")
    stop_parser.add_argument("--run-dir", required=True)
    args = parser.parse_args(argv)
    try:
        result = start(args) if args.command == "start" else stop(args)
    except (CloneLaunchGuardError, OSError, ValueError, KeyError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
