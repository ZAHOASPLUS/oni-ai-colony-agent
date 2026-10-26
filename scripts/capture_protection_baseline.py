"""Capture a protection baseline before any build, deploy, or game launch.

Records SHA-256 for the files ONI must never mutate silently: the named test
clone pair, the Mod enablement file, and the player preferences.  Also records
the current game build identity so a later comparison can prove nothing changed.

Usage:
    python -B scripts/capture_protection_baseline.py [--label pre-phase2]
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
KLEI = pathlib.Path.home() / "Documents" / "Klei" / "OxygenNotIncluded"
GAME = pathlib.Path(r"C:\Program Files (x86)\Steam\steamapps\common\OxygenNotIncluded")
REPORT_DIR = ROOT / "reports" / "phase2"


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def entry(path: pathlib.Path, *, required: bool = False) -> dict:
    if not path.exists():
        return {"path": str(path), "exists": False, "required": required}
    stat = path.stat()
    return {
        "path": str(path),
        "exists": True,
        "size": stat.st_size,
        "mtime_utc": dt.datetime.fromtimestamp(stat.st_mtime, dt.timezone.utc).isoformat(timespec="seconds"),
        "sha256": sha256_file(path),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", default="baseline")
    args = parser.parse_args()

    clone_dir = KLEI / "test-clones" / "oni-m15-autoplay-cycle3-20260913"
    report: dict = {
        "schema": "oni.phase2.protection-baseline.v1",
        "label": args.label,
        "captured_at": dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds"),
        "protected": {
            "clone_save": entry(clone_dir / "oni-autoplay-20260714145532-7dde11.AUTOPLAY Cycle 3.sav", required=True),
            "clone_preview": entry(clone_dir / "oni-autoplay-20260714145532-7dde11.AUTOPLAY Cycle 3.png", required=True),
            "clone_manifest": entry(clone_dir / "manifest.json", required=True),
            "mods_json": entry(KLEI / "mods" / "mods.json", required=True),
            "kplayerprefs": entry(KLEI / "kplayerprefs.yaml"),
        },
        "game": {
            "install_dir": str(GAME),
            "exists": GAME.is_dir(),
            "exe": entry(GAME / "OxygenNotIncluded.exe"),
            "assembly_csharp": entry(GAME / "OxygenNotIncluded_Data" / "Managed" / "Assembly-CSharp.dll"),
            "dlc": sorted(p.name for p in (GAME / "OxygenNotIncluded_Data" / "StreamingAssets" / "dlc").iterdir())
            if (GAME / "OxygenNotIncluded_Data" / "StreamingAssets" / "dlc").is_dir()
            else [],
        },
        "deployed_mod": {
            "dev_dll": entry(KLEI / "mods" / "Dev" / "oni-ai-colony-agent" / "OniAiProbeMod.dll"),
            "dev_mod_yaml": entry(KLEI / "mods" / "Dev" / "oni-ai-colony-agent" / "mod.yaml"),
        },
        "original_saves_present": sorted(p.name for p in (KLEI / "save_files").glob("*")) if (KLEI / "save_files").is_dir() else [],
        "cloud_saves_present": sorted(p.name for p in (KLEI / "cloud_save_files").glob("*")) if (KLEI / "cloud_save_files").is_dir() else [],
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    target = REPORT_DIR / f"protection-baseline-{args.label}.json"
    target.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"\nbaseline written: {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
