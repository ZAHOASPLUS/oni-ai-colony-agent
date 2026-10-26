"""Enable the ONI AI Probe mod for the current user, with an exact backup.

Only the ``ass.OniAiProbe`` entry's ``enabled`` flag is changed; every other
mod, DLC list, and crash counter is preserved byte-for-byte in meaning.  A
timestamped backup of the previous file is written beside it so the change is
exactly reversible.

Usage:
    python -B scripts/enable_probe_mod.py [--disable] [--mods-json PATH]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import shutil
import sys

KLEI = pathlib.Path.home() / "Documents" / "Klei" / "OxygenNotIncluded"
DEFAULT_MODS_JSON = KLEI / "mods" / "mods.json"
TARGET_STATIC_ID = "ass.OniAiProbe"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mods-json", type=pathlib.Path, default=DEFAULT_MODS_JSON)
    parser.add_argument("--disable", action="store_true")
    parser.add_argument("--mod-static-id", default=TARGET_STATIC_ID)
    args = parser.parse_args()

    path = args.mods_json
    if not path.is_file():
        print(f"mods.json not found: {path}")
        return 2
    document = json.loads(path.read_text(encoding="utf-8-sig"))
    mods = document.get("mods")
    if not isinstance(mods, list):
        print("mods.json has no mods list")
        return 2

    wanted = not args.disable
    changed = []
    for entry in mods:
        if not isinstance(entry, dict):
            continue
        if entry.get("staticID") != args.mod_static_id:
            continue
        before = entry.get("enabled")
        entry["enabled"] = wanted
        changed.append({"staticID": entry.get("staticID"), "title": (entry.get("label") or {}).get("title"), "before": before, "after": wanted})

    if not changed:
        print(f"no mod entry with staticID {args.mod_static_id!r}")
        return 3

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(path.name + f".pre-dsh-{stamp}.bak")
    shutil.copy2(path, backup)

    payload = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(path)

    print(json.dumps({"mods_json": str(path), "backup": str(backup), "changed": changed}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
