#!/usr/bin/env python3
"""Watch a player agent's ONI session and print one line per problem.

The player only plays; tool, Mod and session problems belong to the
supervisor.  This script is the supervisor's eyes: it follows the Mod's audit
log, asks the game for its status now and then (read-only), and reads the
player's issue file.  Each problem is printed once as "ALERT <kind>: ...",
and a "STATUS ..." line is printed every --status-minutes.

Alerts:
  issue            the player filed a line in <run-dir>/player-issues.jsonl
  failing          the same tool and error code failed 3 times in 5 minutes
  read_only_loop   calls keep coming but none changed the colony for a while
  player_idle      no tool call at all for a while
  stalled          the game clock did not move for a while
  paused           the game sat at speed 0 for a while
  duplicant_lost   fewer living duplicants than before
  api_down         the game did not answer
  tool_code_changed the player touched mod/, tools/, scripts/ or agent/
  session_started  a new run directory appeared under reports/api
  resumed          (--no-pause) the game was paused and the watcher set it
                   running again at the last speed, never below 1
"""

from __future__ import annotations

import argparse
import collections
import datetime
import json
import os
import pathlib
import subprocess
import sys
import time
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_BRIDGE = (pathlib.Path(os.environ["ONI_AI_BRIDGE_DIR"].strip()) if os.environ.get("ONI_AI_BRIDGE_DIR", "").strip()
                  else pathlib.Path.home() / "Documents" / "Klei" / "OxygenNotIncluded" / "ai-bridge")
READ_ONLY_TOOLS = {
    "get_lifecycle", "get_status", "get_map", "inspect", "get_buildable", "get_research",
    "get_events", "list_buildings", "get_resources", "find", "get_progress", "get_skills",
    "get_printables", "wait", "set_speed",
}
TOOL_PATHS = ("mod/", "tools/", "scripts/", "agent/")


class Watcher:
    """Turns audit entries and status samples into alerts.  No I/O here."""

    def __init__(self, stall_seconds: float = 600.0, fail_window: float = 300.0, fail_count: int = 3) -> None:
        self.stall_seconds = stall_seconds
        self.fail_window = fail_window
        self.fail_count = fail_count
        self.failures: Dict[Tuple[str, str], Deque[float]] = collections.defaultdict(collections.deque)
        self.last_call: Optional[float] = None
        self.last_write: Optional[float] = None
        self.calls = 0
        self.writes = 0
        self.last_cycle: Optional[float] = None
        self.cycle_moved_at: Optional[float] = None
        self.paused_since: Optional[float] = None
        self.dupes: Optional[int] = None
        self.raised: Dict[str, float] = {}

    def _once(self, key: str, now: float, quiet: float) -> bool:
        """True if this alert has not been raised within the last quiet seconds."""
        last = self.raised.get(key)
        if last is not None and now - last < quiet:
            return False
        self.raised[key] = now
        return True

    def feed_audit(self, entry: Dict[str, Any], now: float) -> List[str]:
        alerts: List[str] = []
        tool = str(entry.get("tool"))
        self.calls += 1
        self.last_call = now
        if entry.get("ok"):
            if tool not in READ_ONLY_TOOLS:
                self.writes += 1
                self.last_write = now
            return alerts
        error = entry.get("error") or {}
        code = str(error.get("code") if isinstance(error, dict) else error)
        times = self.failures[(tool, code)]
        times.append(now)
        while times and now - times[0] > self.fail_window:
            times.popleft()
        if len(times) >= self.fail_count and self._once(f"failing:{tool}:{code}", now, self.fail_window):
            message = error.get("message", "") if isinstance(error, dict) else ""
            alerts.append(f"failing: {tool} -> {code} x{len(times)} in {int(self.fail_window)}s: {message}"[:300])
        return alerts

    def feed_status(self, status: Optional[Dict[str, Any]], now: float, error: str = "") -> List[str]:
        alerts: List[str] = []
        if status is None:
            if self._once("api_down", now, self.stall_seconds):
                alerts.append(f"api_down: {error}"[:300])
            return alerts
        self.raised.pop("api_down", None)
        clock = status.get("time") or {}
        cycle = clock.get("cycle")
        if isinstance(cycle, (int, float)) and isinstance(clock.get("cycle_fraction"), (int, float)):
            cycle = round(cycle + clock["cycle_fraction"], 3)
        paused = bool(clock.get("paused"))
        if isinstance(cycle, (int, float)):
            if self.last_cycle is None or cycle != self.last_cycle:
                self.last_cycle = float(cycle)
                self.cycle_moved_at = now
                self.raised.pop("stalled", None)
        if paused:
            self.paused_since = self.paused_since or now
            if now - self.paused_since >= self.stall_seconds and self._once("paused", now, self.stall_seconds):
                alerts.append(f"paused: speed 0 for {int(now - self.paused_since)}s at cycle {self.last_cycle}")
        else:
            self.paused_since = None
            self.raised.pop("paused", None)
            if (self.cycle_moved_at is not None and now - self.cycle_moved_at >= self.stall_seconds
                    and self._once("stalled", now, self.stall_seconds)):
                alerts.append(f"stalled: clock at {self.last_cycle} for {int(now - self.cycle_moved_at)}s while unpaused")
        dupes = status.get("duplicants")
        if isinstance(dupes, list):
            count = len(dupes)
            if self.dupes is not None and count < self.dupes:
                alerts.append(f"duplicant_lost: {self.dupes} -> {count} at cycle {self.last_cycle}")
            self.dupes = count
        return alerts

    def check_activity(self, now: float, started: float) -> List[str]:
        alerts: List[str] = []
        since_call = now - (self.last_call if self.last_call is not None else started)
        if since_call >= self.stall_seconds:
            if self._once("player_idle", now, self.stall_seconds):
                alerts.append(f"player_idle: no tool call for {int(since_call)}s")
            return alerts
        self.raised.pop("player_idle", None)
        since_write = now - (self.last_write if self.last_write is not None else started)
        if since_write >= self.stall_seconds and self._once("read_only_loop", now, self.stall_seconds):
            alerts.append(f"read_only_loop: {int(since_write)}s of calls with no colony change")
        elif since_write < self.stall_seconds:
            self.raised.pop("read_only_loop", None)
        return alerts

    def summary(self) -> str:
        return (f"cycle={self.last_cycle} dupes={self.dupes} calls={self.calls} writes={self.writes} "
                f"failing_keys={sum(1 for v in self.failures.values() if v)}")


class Tail:
    """New complete lines of a file, from where the last read stopped."""

    def __init__(self, path: pathlib.Path, from_end: bool = True) -> None:
        self.path = path
        self.offset = path.stat().st_size if from_end and path.exists() else 0
        self.partial = b""

    def lines(self) -> List[str]:
        if not self.path.exists():
            self.offset = 0
            return []
        size = self.path.stat().st_size
        if size < self.offset:  # moved away and recreated
            self.offset = 0
            self.partial = b""
        if size == self.offset:
            return []
        with self.path.open("rb") as handle:
            handle.seek(self.offset)
            data = handle.read()
        self.offset += len(data)
        data = self.partial + data
        parts = data.split(b"\n")
        self.partial = parts.pop()
        return [part.decode("utf-8", errors="replace") for part in parts if part.strip()]


def read_status() -> Tuple[Optional[Dict[str, Any]], str]:
    try:
        done = subprocess.run(
            [sys.executable, str(REPO_ROOT / "tools" / "oni_tools.py"), "call", "get_status", "{}"],
            capture_output=True, timeout=60, cwd=REPO_ROOT,
        )
        reply = json.loads(done.stdout.decode("utf-8", errors="replace") or "{}")
    except (subprocess.TimeoutExpired, ValueError, OSError) as ex:
        return None, type(ex).__name__
    if not reply.get("ok"):
        error = reply.get("error") or {}
        return None, str(error.get("code") if isinstance(error, dict) else error)
    return reply.get("result") or {}, ""


def resume_speed(speed: int) -> str:
    """Unpause at the given speed; returns "" or the error code."""
    try:
        done = subprocess.run(
            [sys.executable, str(REPO_ROOT / "tools" / "oni_tools.py"), "call", "set_speed",
             json.dumps({"speed": max(1, speed)})],
            capture_output=True, timeout=60, cwd=REPO_ROOT,
        )
        reply = json.loads(done.stdout.decode("utf-8", errors="replace") or "{}")
    except (subprocess.TimeoutExpired, ValueError, OSError) as ex:
        return type(ex).__name__
    if reply.get("ok"):
        return ""
    error = reply.get("error") or {}
    return str(error.get("code") if isinstance(error, dict) else error)


def audit_epoch(utc: Any) -> Optional[float]:
    """The Mod writes "2026-11-05T22:16:18.6178237+00:00"; Python wants 6 digits."""
    if not isinstance(utc, str):
        return None
    try:
        head, _, rest = utc.partition(".")
        digits = "".join(ch for ch in rest if ch.isdigit())[:6]
        zone = rest[len("".join(ch for ch in rest if ch.isdigit())):] or "+00:00"
        text = f"{head}.{digits or '0'}{zone}" if rest else utc
        return datetime.datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


class OwnCalls:
    """The watcher's own get_status / set_speed calls, so they never count as the player's."""

    def __init__(self, keep: int = 64) -> None:
        self.spans: Deque[Tuple[str, float, float]] = collections.deque(maxlen=keep)

    def add(self, tool: str, start: float, end: float) -> None:
        self.spans.append((tool, start, end))

    def owns(self, entry: Dict[str, Any], slack: float = 1.5) -> bool:
        when = audit_epoch(entry.get("utc"))
        if when is None:
            return False
        tool = entry.get("tool")
        return any(tool == own and start - slack <= when <= end + slack for own, start, end in self.spans)


def wants_pause(entry: Dict[str, Any]) -> bool:
    args = entry.get("args") or {}
    return entry.get("tool") == "set_speed" and bool(entry.get("ok")) and args.get("speed") == 0


def tool_changes() -> str:
    done = subprocess.run(
        ["git", "status", "--porcelain", "--", *TOOL_PATHS], capture_output=True, cwd=REPO_ROOT,
    )
    head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, cwd=REPO_ROOT)
    return head.stdout.decode().strip() + "\n" + done.stdout.decode("utf-8", errors="replace")


def run_dirs() -> set:
    root = REPO_ROOT / "reports" / "api"
    return {p.name for p in root.iterdir() if p.is_dir()} if root.exists() else set()


def main(argv: Optional[List[str]] = None, emit: Callable[[str], None] = print) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-dir", required=True, type=pathlib.Path)
    parser.add_argument("--bridge", type=pathlib.Path, default=DEFAULT_BRIDGE)
    parser.add_argument("--poll-seconds", type=float, default=15.0)
    parser.add_argument("--status-seconds", type=float, default=120.0)
    parser.add_argument("--stall-minutes", type=float, default=10.0)
    parser.add_argument("--status-minutes", type=float, default=30.0)
    parser.add_argument("--no-pause", action="store_true",
                        help="unpause the game whenever it is paused (user rule 2026-11-06: no pausing, speed >= 1)")
    parser.add_argument("--digest-minutes", type=float, default=0.0,
                        help="hold every alert but the player's issues and print them as one DIGEST line this often")
    args = parser.parse_args(argv)

    run_dir = args.run_dir if args.run_dir.is_absolute() else REPO_ROOT / args.run_dir
    watcher = Watcher(stall_seconds=args.stall_minutes * 60.0)
    audit = Tail(args.bridge / "api-audit.jsonl")
    issues = Tail(run_dir / "player-issues.jsonl")
    started = time.time()
    last_status = 0.0
    last_summary = started
    baseline_code = tool_changes()
    baseline_dirs = run_dirs()

    pending: "collections.Counter[str]" = collections.Counter()

    def say(line: str) -> None:
        # With --digest-minutes only the player's own issues come through at
        # once; every other alert waits for the next DIGEST line.
        if args.digest_minutes > 0 and line.startswith("ALERT ") and not line.startswith("ALERT issue:"):
            pending[line[len("ALERT "):]] += 1
            return
        emit(line)
        sys.stdout.flush()

    last_speed = 1
    own = OwnCalls()

    def resume(reason: str) -> None:
        start = time.time()
        error = resume_speed(last_speed)
        own.add("set_speed", start, time.time())
        say(f"ALERT resumed: {reason}; set_speed {max(1, last_speed)}" + (f" failed: {error}" if error else ""))

    say(f"WATCH run_dir={run_dir} bridge={args.bridge} no_pause={args.no_pause}")
    while True:
        now = time.time()
        pause_seen = False
        for line in audit.lines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if own.owns(entry):
                continue
            requested =(entry.get("args") or {}).get("speed") if entry.get("tool") == "set_speed" else None
            if isinstance(requested, int) and requested >= 1 and entry.get("ok"):
                last_speed = requested
            pause_seen = pause_seen or wants_pause(entry)
            for alert in watcher.feed_audit(entry, now):
                say("ALERT " + alert)
        if args.no_pause and pause_seen:
            resume("player called set_speed 0")
        for line in issues.lines():
            say("ALERT issue: " + line[:400])
        if now - last_status >= args.status_seconds:
            last_status = now
            start = time.time()
            status, error = read_status()
            own.add("get_status", start, time.time())
            for alert in watcher.feed_status(status, now, error):
                say("ALERT " + alert)
            clock = (status or {}).get("time") or {}
            if args.no_pause and clock.get("paused"):
                if isinstance(clock.get("speed"), int) and clock["speed"] >= 1:
                    last_speed = clock["speed"]
                resume(f"game was paused at cycle {watcher.last_cycle}")
            code = tool_changes()
            if code != baseline_code:
                baseline_code = code
                say("ALERT tool_code_changed: " + code.replace("\n", " | ")[:300])
            dirs = run_dirs()
            for name in sorted(dirs - baseline_dirs):
                say(f"ALERT session_started: reports/api/{name}")
            baseline_dirs = dirs
        for alert in watcher.check_activity(now, started):
            say("ALERT " + alert)
        if args.digest_minutes > 0:
            if now - last_summary >= args.digest_minutes * 60.0:
                last_summary = now
                alerts = "; ".join(f"{text} (x{count})" if count > 1 else text for text, count in pending.items())
                pending.clear()
                emit(f"DIGEST {watcher.summary()} | " + (alerts[:1500] if alerts else "no alerts"))
                sys.stdout.flush()
        elif now - last_summary >= args.status_minutes * 60.0:
            last_summary = now
            say("STATUS " + watcher.summary())
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    sys.exit(main())
