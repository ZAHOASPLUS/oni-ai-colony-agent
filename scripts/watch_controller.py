"""Read-only, bounded watch of a running autonomous controller.

Usage: python scripts/watch_controller.py [--bridge-dir DIR] [--seconds N] [--interval S]

Prints one compact JSON line per sample from autonomous-controller.json,
health.json and model-loop.json, plus whether the recorded controller process
is alive. It never signals, stops or starts any process and never writes to the
bridge directory, so cancelling or timing out this command cannot take the
controller with it (AGENTS.md, "Process lifetime"). Start the controller in its
own short command with scripts/start-controller.ps1 and run this separately.
A single run lasts at most 30 seconds; run it again to keep watching.
"""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import json
import pathlib
import sys
import time
from typing import Any, Dict, Optional

DEFAULT_BRIDGE = pathlib.Path.home() / "Documents" / "Klei" / "OxygenNotIncluded" / "ai-bridge"
MAX_SECONDS = 30.0
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259


def process_alive(pid: Any) -> Optional[bool]:
    """Query a process without touching it; None when it cannot be determined.

    ``os.kill(pid, 0)`` is not a probe on Windows: it terminates the process.
    """

    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    if sys.platform != "win32":
        return None
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return None
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def read_json(path: pathlib.Path) -> Optional[Dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def age_seconds(path: pathlib.Path) -> Optional[float]:
    try:
        return round(time.time() - path.stat().st_mtime, 1)
    except OSError:
        return None


def sample(bridge: pathlib.Path) -> Dict[str, Any]:
    controller = read_json(bridge / "autonomous-controller.json") or {}
    health = read_json(bridge / "health.json") or {}
    loop = read_json(bridge / "model-loop.json") or {}
    last = controller.get("last_action") if isinstance(controller.get("last_action"), dict) else {}
    pid = controller.get("controller_pid")
    return {
        "at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "controller_pid": pid,
        "controller_alive": process_alive(pid),
        "desired_state": controller.get("desired_state"),
        "controller_doc_age_s": age_seconds(bridge / "autonomous-controller.json"),
        "health": health.get("status"),
        "health_age_s": age_seconds(bridge / "health.json"),
        "loop_phase": loop.get("phase"),
        "loop_blockers": (loop.get("blockers") or [])[:4],
        "controller_blockers": (controller.get("blockers") or [])[:4],
        "last_action": {key: last.get(key) for key in ("action", "candidate_id", "stage", "effect_confirmed")},
    }


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bridge-dir", type=pathlib.Path, default=DEFAULT_BRIDGE)
    parser.add_argument("--seconds", type=float, default=25.0, help=f"total watch time, at most {MAX_SECONDS:g}")
    parser.add_argument("--interval", type=float, default=5.0)
    args = parser.parse_args(argv)
    seconds = min(max(args.seconds, 0.0), MAX_SECONDS)
    interval = max(args.interval, 1.0)
    deadline = time.monotonic() + seconds
    while True:
        print(json.dumps(sample(args.bridge_dir), ensure_ascii=False), flush=True)
        if time.monotonic() + interval > deadline:
            return 0
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main())
