"""Reset the append-only Probe log and bring the DSH run back up.

The Probe appends one large snapshot record every few seconds for as long as the
game runs, so ``state.jsonl`` grows without bound (118-124 MB within a session).
Once the Bridge's per-iteration processing can no longer keep up with that rate
it reports ``sequence_gap`` and the controller refuses to start, which silently
stalls the run.  Rotating the live file is not safe (the Probe holds an append
handle), so the recovery is: stop the Bridge, archive the log, restart the game
to begin a fresh session, rebuild the binding, and start Bridge + controller.

Usage:
    python -B scripts/reset_dsh_run.py [--run-id ID] [--keep-game]
"""

from __future__ import annotations

import argparse
import gzip
import json
import pathlib
import shutil
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
KLEI = pathlib.Path.home() / "Documents" / "Klei" / "OxygenNotIncluded"
BRIDGE = KLEI / "ai-bridge"
GAME_EXE = pathlib.Path(
    r"C:\Program Files (x86)\Steam\steamapps\common\OxygenNotIncluded\OxygenNotIncluded.exe"
)
PYTHON = sys.executable

SESSION_SCOPED = (
    "model-loop.json",
    "model-loop-state.json",
    "autonomous-controller.json",
    "autonomous-controller.lock",
    "autonomous-action-checkpoint.json",
    "load-result.json",
    "health.json",
    "latest_state.json",
    "decision-frame.json",
    "plan.json",
    "incidents.json",
    "strategy.json",
    "action-outcomes.json",
    "control-packet.json",
    "advice.json",
    "evidence-continuity.json",
)


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True, errors="replace", **kwargs)


def python_processes(pattern: str) -> list[int]:
    script = (
        "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and "
        f"$_.CommandLine -match '{pattern}' }} | ForEach-Object {{ $_.ProcessId }}"
    )
    result = run(["powershell", "-NoProfile", "-Command", script])
    return [int(line) for line in (result.stdout or "").split() if line.strip().isdigit()]


def kill(pattern: str, label: str) -> None:
    for pid in python_processes(pattern):
        run(["powershell", "-NoProfile", "-Command", f"Stop-Process -Id {pid} -Force"])
        print(f"stopped {label} pid {pid}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", default="dsh-20261025-r01")
    parser.add_argument("--stem", default="oni-dsh-20261025")
    parser.add_argument("--keep-game", action="store_true", help="do not restart the game")
    parser.add_argument(
        "--keep-commands",
        action="store_true",
        help="keep the command queue, decision frames and autonomous intents",
    )
    args = parser.parse_args()

    report: dict = {"schema": "oni.phase2.reset-run.v1", "run_id": args.run_id}
    log = BRIDGE / "state.jsonl"

    kill("oni_bridge|oni_autonomous", "bridge/controller")
    time.sleep(4)

    if log.is_file():
        size = log.stat().st_size
        archive_dir = ROOT / "reports" / "phase2" / "runs" / args.run_id / "logs"
        archive_dir.mkdir(parents=True, exist_ok=True)
        target = archive_dir / f"state-{int(size/1e6)}mb-{time.strftime('%H%M%S')}.jsonl.gz"
        with log.open("rb") as source, gzip.open(target, "wb", compresslevel=6) as sink:
            shutil.copyfileobj(source, sink)
        report["archived_log"] = {"source_bytes": size, "archive": str(target), "archive_bytes": target.stat().st_size}
        print(f"archived {size} bytes -> {target} ({target.stat().st_size} bytes)")

    shutil.rmtree(BRIDGE / "sessions", ignore_errors=True)
    for name in list(SESSION_SCOPED):
        (BRIDGE / name).unlink(missing_ok=True)
    for pattern in ("state.jsonl*",):
        for path in BRIDGE.glob(pattern):
            path.unlink(missing_ok=True)
    log.write_bytes(b"")

    # Commands and the decision frames they were authorized against must be
    # reset together.  Confirming a command re-loads its ``source_frame_id`` from
    # the archive, so a command left behind without its frame makes the next
    # confirmation fail with "confirmed command source frame could not be frozen
    # before done" and the Bridge exits (2026-10-25 round 13).  Both sets are
    # archived as evidence before removal.  ``--keep-commands`` preserves the
    # whole custody set so a reconciliation applied to a latched command can be
    # exercised without clearing it away.
    if args.keep_commands:
        report["kept_commands"] = True
        report["cleared"] = True
        return _relaunch(args, report, PYTHON, ROOT, KLEI, BRIDGE, GAME_EXE, SESSION_SCOPED)

    evidence_dir = ROOT / "reports" / "phase2" / "runs" / args.run_id / f"pre-reset-{time.strftime('%H%M%S')}"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    archived_commands = 0
    for stage_dir in sorted((BRIDGE / "commands").iterdir()) if (BRIDGE / "commands").is_dir() else []:
        if not stage_dir.is_dir():
            continue
        for item in stage_dir.glob("*.json"):
            shutil.copy2(item, evidence_dir / f"{stage_dir.name}-{item.name}")
            item.unlink(missing_ok=True)
            archived_commands += 1
    archived_frames = 0
    for frame in (BRIDGE / "decision-frames").glob("*.json") if (BRIDGE / "decision-frames").is_dir() else []:
        frame.unlink(missing_ok=True)
        archived_frames += 1
    report["archived_commands"] = archived_commands
    report["cleared_decision_frames"] = archived_frames
    report["evidence_dir"] = str(evidence_dir)
    report["cleared"] = True

    return _relaunch(args, report, PYTHON, ROOT, KLEI, BRIDGE, GAME_EXE, SESSION_SCOPED)


def _relaunch(args, report, python, root, klei, bridge, game_exe, session_scoped):
    """Stop the game, relaunch it on the clone, and rebind the controller config."""

    log = bridge / "state.jsonl"
    if not args.keep_game:
        run(["powershell", "-NoProfile", "-Command",
             "Get-Process -Name OxygenNotIncluded -ErrorAction SilentlyContinue | ForEach-Object { $_.CloseMainWindow() | Out-Null }"])
        for _ in range(30):
            probe = run(["powershell", "-NoProfile", "-Command",
                         "if (Get-Process -Name OxygenNotIncluded -ErrorAction SilentlyContinue) { 'up' } else { 'down' }"])
            if "down" in (probe.stdout or ""):
                break
            time.sleep(2)
        report["game_stopped"] = True

        prepare = run([python, "-B", str(root / "scripts" / "prepare_dsh_autonomous_run.py")])
        print((prepare.stdout or "").strip()[:400])

        save = klei / "cloud_save_files" / "76561190000000000" / args.stem / f"{args.stem}.sav"
        (bridge / "load-request.json").write_text(
            json.dumps({
                "schema": "oni.load-request.v1",
                "save": str(save),
                "requested_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }, indent=2),
            encoding="utf-8",
        )
        import os

        child_env = dict(os.environ)
        child_env.update({"SteamAppId": "457140", "SteamGameId": "457140"})
        subprocess.Popen([str(game_exe)], cwd=str(game_exe.parent), env=child_env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(40)
        probe = run(["powershell", "-NoProfile", "-Command",
                     "(Get-CimInstance Win32_Process -Filter \"Name='OxygenNotIncluded.exe'\").ProcessId"])
        game_pid = int((probe.stdout or "0").strip() or 0)
        report["game_pid"] = game_pid

        session = None
        for line in reversed(log.read_text(encoding="utf-8-sig", errors="replace").splitlines()):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("event") == "session_started" and record.get("validation_process_id") == game_pid:
                session = record.get("session_id")
                break
        report["session_id"] = session

        config_path = root / "reports" / "phase2" / "runs" / args.run_id / "autonomous-controller-config.v1.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["session_id"] = session
        config["startup_expectations"]["validation_process_id"] = game_pid
        config["startup_expectations"]["startup_binding_hash"] = (
            "sha256:" + __import__("hashlib").sha256((bridge / "runtime-startup-binding.json").read_bytes()).hexdigest()
        )
        config_path.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        run([python, "-B", str(root / "scripts" / "write_dsh_autonomous_policy.py"),
             "--session", str(session), "--world", "0", "--desired", "running", "--config", str(config_path)])

    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
