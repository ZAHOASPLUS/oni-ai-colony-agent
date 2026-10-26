"""Point ONI's remembered save at an exact .sav, with an exact backup.

ONI stores the save it will load on the next launch in
``kplayerprefs.yaml`` under ``strings.SaveFilenameKey/``.  This script rewrites
only that one scalar (preserving every other line byte-for-byte), after writing
a timestamped backup, so the pointer change is exactly reversible.

Usage:
    python -B scripts/set_oni_load_save.py --save "<path to .sav>"
    python -B scripts/set_oni_load_save.py --restore "<backup path>"
    python -B scripts/set_oni_load_save.py --show
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import pathlib
import shutil
import sys

KLEI = pathlib.Path.home() / "Documents" / "Klei" / "OxygenNotIncluded"
PREFS = KLEI / "kplayerprefs.yaml"
KEY = "SaveFilenameKey/"


def read_prefs() -> tuple[list[str], str]:
    raw = PREFS.read_bytes()
    text = raw.decode("utf-8-sig")
    return text.splitlines(keepends=True), text


def current_value(lines: list[str]) -> str | None:
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(KEY + ":") or stripped.startswith(KEY + " "):
            _, _, value = stripped.partition(":")
            return value.strip()
    return None


def show() -> int:
    lines, _ = read_prefs()
    print(json.dumps({"prefs": str(PREFS), KEY: current_value(lines)}, indent=2, ensure_ascii=False))
    return 0


def write_prefs(lines: list[str]) -> None:
    payload = "".join(lines).encode("utf-8-sig")
    temporary = PREFS.with_name(PREFS.name + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(PREFS)


def set_value(save: pathlib.Path) -> int:
    if not save.is_file() or save.suffix.lower() != ".sav":
        print(f"save must be an existing .sav file: {save}")
        return 2
    lines, _ = read_prefs()
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = PREFS.with_name(PREFS.name + f".pre-dsh-{stamp}.bak")
    shutil.copy2(PREFS, backup)
    before = current_value(lines)
    replaced = False
    output: list[str] = []
    for line in lines:
        if line.strip().startswith(KEY + ":"):
            indent = line[: len(line) - len(line.lstrip())]
            output.append(f"{indent}{KEY}: {save}\n")
            replaced = True
        else:
            output.append(line)
    if not replaced:
        print(f"prefs has no {KEY} entry")
        return 3
    write_prefs(output)
    lines_after, _ = read_prefs()
    print(json.dumps({
        "prefs": str(PREFS),
        "backup": str(backup),
        "before": before,
        "after": current_value(lines_after),
        "save_sha256": "sha256:" + hashlib.sha256(save.read_bytes()).hexdigest(),
    }, indent=2, ensure_ascii=False))
    return 0


def restore(backup: pathlib.Path) -> int:
    if not backup.is_file():
        print(f"backup not found: {backup}")
        return 2
    shutil.copy2(backup, PREFS)
    lines, _ = read_prefs()
    print(json.dumps({"prefs": str(PREFS), "restored_from": str(backup), KEY: current_value(lines)}, indent=2, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--save")
    group.add_argument("--restore")
    group.add_argument("--show", action="store_true")
    args = parser.parse_args()
    if args.show:
        return show()
    if args.restore:
        return restore(pathlib.Path(args.restore))
    return set_value(pathlib.Path(args.save))


if __name__ == "__main__":
    sys.exit(main())
