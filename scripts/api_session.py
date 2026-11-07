#!/usr/bin/env python3
"""Start and stop one tool-API game session, without mouse or keyboard.

    python scripts/api_session.py start --manifest M --save S --run-dir D [--resume | --checkpoint N]
    python scripts/api_session.py stop --run-dir D
    python scripts/api_session.py seal --manifest M --save S --run-dir D

``start`` checks the clone against its manifest, keeps a copy of the game's
preferences (loading a save moves the game's "last save" pointer), writes the
autoload request and asks Steam to launch the game.  It returns at once, so the
game is never a child of a long-lived command.  With ``--resume`` it loads the
newest intact link of the clone's checkpoint chain instead of the clone, and
with ``--checkpoint N`` it rolls back to link N of that chain.

``stop`` closes the game the way the window's close button does, puts the
preferences back, moves the run's audit log into the run directory and seals the
checkpoints the run saved: a checkpoint joins the chain only through its
manifest.  ``seal`` does that last step alone, for a run that ended without
``stop``.
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
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, List, Mapping

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.checkpoint_chain import (  # noqa: E402
    CheckpointChainError,
    build_checkpoint_manifest,
    checkpoint_paths,
    publish_checkpoint_manifest,
    select_latest_checkpoint,
)
from agent.clone_launch_guard import (  # noqa: E402
    CloneLaunchGuardError,
    build_process_probe,
    validate_clone_launch,
)

GAME_PROCESS = "OxygenNotIncluded.exe"
STEAM_LAUNCH_URL = "steam://rungameid/457140"
WM_CLOSE = 0x0010
STOP_TIMEOUT_SECONDS = 25.0
AUDIT_FILE = "api-audit.jsonl"
PREEXISTING_AUDIT_FILE = "api-audit.preexisting.jsonl"
CHECKPOINT_STEM = re.compile(r"-ckpt-(?P<index>[0-9]{3})$")


def oni_root() -> pathlib.Path:
    return pathlib.Path.home() / "Documents" / "Klei" / "OxygenNotIncluded"


def bridge_dir() -> pathlib.Path:
    """The same folder the Mod and tools/oni_tools.py use: ONI_AI_BRIDGE_DIR, else ai-bridge."""
    configured = os.environ.get("ONI_AI_BRIDGE_DIR", "").strip()
    return pathlib.Path(configured) if configured else oni_root() / "ai-bridge"


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


def archive_existing_audit(bridge: pathlib.Path, run_dir: pathlib.Path) -> bool:
    """Keep earlier sessions' evidence without letting it become this run's audit."""
    source = bridge / AUDIT_FILE
    if not source.exists():
        return False
    target = run_dir / PREEXISTING_AUDIT_FILE
    if target.exists():
        raise CloneLaunchGuardError(f"Earlier audit evidence already exists: {target}")
    source.rename(target)
    return True


def audit_time(value: Any) -> datetime.datetime:
    stamp = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if stamp.utcoffset() is None:
        raise CheckpointChainError("Audit timestamps must include a timezone.")
    return stamp


def start(args: argparse.Namespace) -> Dict[str, Any]:
    root = oni_root()
    bridge = bridge_dir()
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

    target = save
    if args.resume or args.checkpoint is not None:
        target = select_latest_checkpoint(pathlib.Path(args.manifest), save)["save_path"]
    if args.checkpoint is not None:
        target = checkpoint_paths(save, args.checkpoint)["save"]
        if not target.is_file():
            raise CheckpointChainError(f"Checkpoint {args.checkpoint} is not in the chain.")

    run_dir.mkdir(parents=True, exist_ok=True)
    prefs = root / "kplayerprefs.yaml"
    prefs_backup = run_dir / "kplayerprefs.before.yaml"
    shutil.copy2(prefs, prefs_backup)
    archived_audit = archive_existing_audit(bridge, run_dir)
    session = {
        "schema": "oni.api-session.v1",
        "started_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "save": str(save),
        "loaded": str(target),
        "manifest": str(pathlib.Path(args.manifest).resolve()),
        "preflight": preflight,
        "kplayerprefs_sha256": sha256(prefs),
        "installed_dll_sha256": sha256(root / "mods" / "Dev" / "oni-ai-colony-agent" / "OniAiProbeMod.dll"),
        "preexisting_audit_archived": archived_audit,
    }
    write_json_atomic(run_dir / "session.json", session)
    write_json_atomic(
        bridge / "autoload.json",
        {
            "schema": "oni.api-autoload.v1",
            "save": str(target),
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
    bridge = bridge_dir()
    process_ids = game_process_ids()
    end_path = run_dir / "session-end.json"
    if (run_dir / AUDIT_FILE).exists() and ((bridge / AUDIT_FILE).exists() or process_ids):
        raise CloneLaunchGuardError("This run already has an audit, but another game or audit is present; nothing was changed.")
    if end_path.exists():
        if process_ids:
            raise CloneLaunchGuardError("This session already ended; refusing to close another game.")
        return json.loads(end_path.read_text(encoding="utf-8"))
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
    if (bridge / AUDIT_FILE).exists():
        if (run_dir / AUDIT_FILE).exists():
            raise CloneLaunchGuardError("This run already has an audit; refusing to overwrite its evidence.")
        (bridge / AUDIT_FILE).rename(run_dir / AUDIT_FILE)
    sealed = seal_checkpoints(pathlib.Path(session["manifest"]), pathlib.Path(session["save"]), run_dir)
    report = {
        "schema": "oni.api-session-end.v1",
        "stopped_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "kplayerprefs_restored": sha256(prefs) == session["kplayerprefs_sha256"],
        "save_unchanged": sha256(pathlib.Path(session["save"])) == session["preflight"]["clone_save_sha256"],
        "api_file_left": (bridge / "api.json").exists(),
        "autoload_left": (bridge / "autoload.json").exists(),
        "checkpoints_sealed": sealed,
    }
    write_json_atomic(run_dir / "session-end.json", report)
    return report


def seal_checkpoints(manifest: pathlib.Path, clone_save: pathlib.Path, run_dir: pathlib.Path) -> List[str]:
    """Publish a manifest for each checkpoint this run saved, oldest first."""
    audit = run_dir / AUDIT_FILE
    if not audit.exists():
        return []

    # Older session runners left a global append-only audit behind.  Preserve
    # that file intact, but only seal entries made after this session began.
    session_path = run_dir / "session.json"
    started = None
    if session_path.exists():
        started = audit_time(json.loads(session_path.read_text(encoding="utf-8"))["started_utc"])

    saved: Dict[int, float] = {}
    for line in audit.read_text(encoding="utf-8").splitlines():
        entry = json.loads(line)
        if entry.get("tool") != "save_checkpoint" or not entry.get("ok"):
            continue
        if started is not None and audit_time(entry["utc"]) < started:
            continue
        path = pathlib.Path(str(entry["result"]["path"]))
        match = CHECKPOINT_STEM.search(path.stem)
        if match is None or path.parent != clone_save.parent or not path.stem.startswith(clone_save.stem + "-ckpt-"):
            raise CheckpointChainError(f"The audit log names a foreign checkpoint: {path}")
        saved[int(match["index"])] = float(entry["cycle"])

    sealed: List[str] = []
    for index in sorted(saved):
        paths = checkpoint_paths(clone_save, index)
        if paths["manifest"].exists():
            continue
        publish_checkpoint_manifest(
            paths["manifest"],
            build_checkpoint_manifest(
                clone_manifest=manifest,
                clone_save=clone_save,
                index=index,
                run_id=run_dir.name,
                session_id=run_dir.name,
                game_time_cycles=saved[index],
            ),
        )
        sealed.append(paths["save"].name)
    return sealed


def seal(args: argparse.Namespace) -> Dict[str, Any]:
    sealed = seal_checkpoints(pathlib.Path(args.manifest), pathlib.Path(args.save).resolve(), pathlib.Path(args.run_dir).resolve())
    return {"checkpoints_sealed": sealed}


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    start_parser = commands.add_parser("start")
    start_parser.add_argument("--manifest", required=True)
    start_parser.add_argument("--save", required=True)
    start_parser.add_argument("--run-dir", required=True)
    start_parser.add_argument("--resume", action="store_true")
    start_parser.add_argument("--checkpoint", type=int)
    stop_parser = commands.add_parser("stop")
    stop_parser.add_argument("--run-dir", required=True)
    seal_parser = commands.add_parser("seal")
    seal_parser.add_argument("--manifest", required=True)
    seal_parser.add_argument("--save", required=True)
    seal_parser.add_argument("--run-dir", required=True)
    args = parser.parse_args(argv)
    try:
        result = {"start": start, "stop": stop, "seal": seal}[args.command](args)
    except (CloneLaunchGuardError, CheckpointChainError, OSError, ValueError, KeyError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
