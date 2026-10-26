"""Restore the host to its pre-phase2 state after an ONI autonomous run.

The phase-2 contract requires that the original saves and the normal Mod/config
state are exactly restored when a run ends.  This script performs that
restoration deterministically and verifies it, so the end of a run is a
repeatable procedure rather than a list of remembered steps.

What it does, in order:

1. Stop the controller (which is also the Bridge) and the game, normally.
2. Verify the protection baseline: the user's original saves still exist and the
   clone's ``.sav``/``.png``/manifest are unchanged.
3. Rebuild the Probe mod **without** the development-only ``LoadCoordinator``
   hook and deploy it, then confirm the DLL hash is back to the recorded
   provenance target.
4. Restore ``kplayerprefs.yaml`` from the earliest pre-change backup.
5. Empty the command queues and report what was left.

``--dry-run`` performs every check and reports what it would change without
touching anything.  Run that first.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import shutil
import subprocess
import sys
import time
from typing import Any, Optional

ROOT = pathlib.Path(__file__).resolve().parents[1]
KLEI = pathlib.Path.home() / "Documents" / "Klei" / "OxygenNotIncluded"
BRIDGE = KLEI / "ai-bridge"
MOD_SOURCE = ROOT / "mod" / "Source"
MOD_DEPLOY = KLEI / "mods" / "Dev" / "oni-ai-colony-agent"
BASELINE = ROOT / "reports" / "phase2" / "protection-baseline-pre-phase2.json"
PROVENANCE_DLL_SHA256 = "452916a55914c31d71ccf7a32ac135397a41a50e162a842d3308811a5043860a"
KPLAYERPREFS_GLOB = "kplayerprefs.yaml.pre-dsh-*.bak"
MODS_JSON_BACKUP_GLOB = "mods/mods.json.pre-dsh-*.bak"
LOAD_COORDINATOR = MOD_SOURCE / "Probe" / "LoadCoordinator.cs"


def sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalize_hash(value: Any) -> Optional[str]:
    """Compare hashes regardless of the recorded ``sha256:`` envelope."""

    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if text.startswith("sha256:"):
        text = text[len("sha256:") :]
    return text or None


def powershell(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True, text=True, errors="replace",
    )


def controller_pids() -> list[int]:
    script = (
        "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and "
        "$_.CommandLine -match 'oni_bridge|oni_autonomous' } | ForEach-Object { $_.ProcessId }"
    )
    result = powershell(script)
    return [int(line) for line in (result.stdout or "").split() if line.strip().isdigit()]


def game_running() -> bool:
    result = powershell(
        "if (Get-Process -Name OxygenNotIncluded -ErrorAction SilentlyContinue) { 'up' } else { 'down' }"
    )
    return "up" in (result.stdout or "")


def stop_processes(dry_run: bool, report: dict) -> None:
    pids = controller_pids()
    report["controller_pids"] = pids
    if dry_run:
        return
    for pid in pids:
        powershell(f"Stop-Process -Id {pid} -Force")
    time.sleep(3)
    if game_running():
        powershell(
            "Get-Process -Name OxygenNotIncluded -ErrorAction SilentlyContinue | "
            "ForEach-Object { $_.CloseMainWindow() | Out-Null }"
        )
        for _ in range(30):
            if not game_running():
                break
            time.sleep(2)
    report["stopped"] = True


def verify_baseline(report: dict) -> None:
    if not BASELINE.is_file():
        report["baseline"] = {"status": "missing", "path": str(BASELINE)}
        return
    recorded = json.loads(BASELINE.read_text(encoding="utf-8"))
    comparison = {}
    for key, entry in (recorded.get("protected") or {}).items():
        if not entry.get("exists"):
            continue
        path = pathlib.Path(entry.get("path") or "")
        if not path.is_file():
            comparison[key] = {"status": "missing_now"}
            continue
        current = sha256(path)
        recorded_hash = normalize_hash(entry.get("sha256"))
        comparison[key] = {
            "status": "unchanged" if recorded_hash == current else "changed",
            "recorded": recorded_hash,
            "current": current,
        }
    report["baseline"] = comparison


def rebuild_without_load_coordinator(dry_run: bool, report: dict) -> None:
    """Rebuild the mod with the development-only load hook removed."""

    mod_cs = MOD_SOURCE / "Mod.cs"
    text = mod_cs.read_text(encoding="utf-8")
    call = "            LoadCoordinator.Start(ProbeWriter.BridgeDirectory);"
    report["load_coordinator"] = {
        "source_exists": LOAD_COORDINATOR.is_file(),
        "call_present": call in text,
    }
    if dry_run:
        return

    if call in text:
        # Keep the surrounding comment with the call so the removal is complete.
        marker = (
            "            // Development-only: perform an explicit, request-driven save load so\n"
            "            // an unattended controller can reach a named test clone.  Inert when\n"
            "            // no load-request.json exists.\n"
            + call + "\n\n"
        )
        if marker in text:
            text = text.replace(marker, "")
        else:
            text = text.replace(call + "\n", "")
        mod_cs.write_text(text, encoding="utf-8")
        report["load_coordinator"]["call_removed"] = True
    if LOAD_COORDINATOR.is_file():
        LOAD_COORDINATOR.unlink()
        report["load_coordinator"]["file_removed"] = True

    build = subprocess.run(
        ["dotnet", "build", str(ROOT / "mod" / "OniAiProbeMod.csproj"), "-c", "Release",
         "--no-incremental", "--nologo", "-v", "q", "/p:UseSharedCompilation=false"],
        capture_output=True, text=True, errors="replace", cwd=str(ROOT),
    )
    report["build"] = {"returncode": build.returncode}
    if build.returncode != 0:
        report["build"]["tail"] = (build.stdout or "")[-800:]
        return

    source_dll = ROOT / "mod" / "OniAiProbeMod.dll"
    if source_dll.is_file():
        shutil.copy2(source_dll, MOD_DEPLOY / "OniAiProbeMod.dll")
        report["deployed_dll_sha256"] = sha256(MOD_DEPLOY / "OniAiProbeMod.dll")
        report["provenance_target"] = PROVENANCE_DLL_SHA256
        report["provenance_restored"] = (
            report["deployed_dll_sha256"] == PROVENANCE_DLL_SHA256
        )


def restore_configs(dry_run: bool, report: dict) -> None:
    report["provenance_target"] = PROVENANCE_DLL_SHA256
    mods_backups = sorted(KLEI.glob(MODS_JSON_BACKUP_GLOB))
    report["mods_json_backups"] = [p.name for p in mods_backups]
    if MOD_DEPLOY.joinpath("OniAiProbeMod.dll").is_file():
        report["deployed_dll_sha256"] = sha256(MOD_DEPLOY / "OniAiProbeMod.dll")

    backups = sorted(KLEI.glob(KPLAYERPREFS_GLOB))
    report["kplayerprefs_backups"] = [p.name for p in backups]
    if not backups:
        report["kplayerprefs"] = {"status": "no_backup"}
        return
    earliest = backups[0]
    target = KLEI / "kplayerprefs.yaml"
    report["kplayerprefs"] = {
        "restore_from": earliest.name,
        "backup_sha256": sha256(earliest),
        "current_sha256": sha256(target) if target.is_file() else None,
    }
    if dry_run:
        return
    shutil.copy2(earliest, target)
    report["kplayerprefs"]["restored_sha256"] = sha256(target)
    report["kplayerprefs"]["restored"] = (
        report["kplayerprefs"]["restored_sha256"] == report["kplayerprefs"]["backup_sha256"]
    )


def empty_queues(dry_run: bool, report: dict) -> None:
    commands = BRIDGE / "commands"
    if not commands.is_dir():
        report["queues"] = {"status": "absent"}
        return
    counts = {}
    for stage in sorted(commands.iterdir()):
        if stage.is_dir():
            counts[stage.name] = len(list(stage.glob("*.json")))
    report["queues"] = counts
    if dry_run:
        return
    for stage in sorted(commands.iterdir()):
        if stage.is_dir():
            for item in stage.glob("*.json"):
                item.unlink(missing_ok=True)
    report["queues_emptied"] = True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="check and report only")
    parser.add_argument("--keep-game", action="store_true", help="do not stop the game")
    parser.add_argument("--report", default="", help="write the report to this path")
    args = parser.parse_args()

    report: dict = {
        "schema": "oni.phase2.restore-environment.v1",
        "dry_run": bool(args.dry_run),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    if not args.keep_game:
        stop_processes(args.dry_run, report)
    verify_baseline(report)
    rebuild_without_load_coordinator(args.dry_run, report)
    restore_configs(args.dry_run, report)
    empty_queues(args.dry_run, report)
    report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")

    text = json.dumps(report, indent=2, ensure_ascii=False)
    print(text)
    if args.report:
        path = pathlib.Path(args.report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
