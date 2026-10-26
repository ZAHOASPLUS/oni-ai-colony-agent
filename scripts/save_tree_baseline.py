"""Hash every ONI save and the player preferences, read-only, for a before/after check.

Usage:
    python -B scripts/save_tree_baseline.py capture OUT.json
    python -B scripts/save_tree_baseline.py compare BEFORE.json AFTER.json

``capture`` records size and SHA-256 of every file under the Klei
``save_files`` and ``cloud_save_files`` trees plus ``kplayerprefs.yaml`` and
``mods/mods.json``. ``compare`` lists changed, removed and added files; it exits
1 when any file that existed before changed or disappeared, so new files (a
newly created colony) are reported but do not fail the check. Nothing under the
Klei folder is ever written.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
import sys
from typing import Dict

KLEI = pathlib.Path.home() / "Documents" / "Klei" / "OxygenNotIncluded"
TREES = ("save_files", "cloud_save_files")
SINGLE_FILES = ("kplayerprefs.yaml", "mods/mods.json")


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def capture() -> Dict[str, object]:
    files: Dict[str, Dict[str, object]] = {}
    for tree in TREES:
        root = KLEI / tree
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file() and not path.is_symlink():
                files[path.relative_to(KLEI).as_posix()] = {"size": path.stat().st_size, "sha256": sha256_file(path)}
    for name in SINGLE_FILES:
        path = KLEI / name
        if path.is_file():
            files[name] = {"size": path.stat().st_size, "sha256": sha256_file(path)}
    return {
        "schema": "oni.save-tree-baseline.v1",
        "captured_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "klei_root": str(KLEI),
        "file_count": len(files),
        "files": files,
    }


def compare(before: Dict[str, object], after: Dict[str, object]) -> Dict[str, object]:
    old, new = before["files"], after["files"]
    changed = sorted(name for name in old if name in new and old[name]["sha256"] != new[name]["sha256"])
    removed = sorted(name for name in old if name not in new)
    added = sorted(name for name in new if name not in old)
    return {"changed": changed, "removed": removed, "added": added, "unchanged": len(old) - len(changed) - len(removed)}


def main(argv) -> int:
    if len(argv) == 2 and argv[0] == "capture":
        out = pathlib.Path(argv[1])
        if out.exists():
            print(f"refusing to overwrite {out}")
            return 2
        out.parent.mkdir(parents=True, exist_ok=True)
        document = capture()
        out.write_text(json.dumps(document, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(json.dumps({"written": str(out), "file_count": document["file_count"]}, ensure_ascii=False))
        return 0
    if len(argv) == 3 and argv[0] == "compare":
        before, after = (json.loads(pathlib.Path(item).read_text(encoding="utf-8")) for item in argv[1:])
        result = compare(before, after)
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return 1 if result["changed"] or result["removed"] else 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
