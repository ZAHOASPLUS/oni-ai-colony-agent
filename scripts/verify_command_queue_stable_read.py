#!/usr/bin/env python3
"""Offline verifier that protected active queue stages reject hardlinks."""
from __future__ import annotations
import json, os, pathlib, tempfile
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.command_queue import command_pipeline_readiness, ensure_command_dirs


def main():
    with tempfile.TemporaryDirectory(prefix="oni-queue-stable-") as raw:
        bridge = pathlib.Path(raw)
        dirs = ensure_command_dirs(bridge)
        payload = {"id": "cmd-hardlink-fixture", "action": "set_paused", "mode": "planned"}
        external = bridge / "external-payload.json"
        external.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        target = dirs["pending"] / "cmd-hardlink-fixture.json"
        try:
            os.link(external, target)
        except (OSError, NotImplementedError) as exc:
            raise AssertionError(f"hardlink fixture unavailable: {exc}") from exc
        result = command_pipeline_readiness(bridge)
        invalid = result.get("invalid_records", [])
        assert any(item.get("reason") == "command_file_unreadable" for item in invalid)
        assert any(str(item).startswith("command_file_unreadable:pending:") for item in result.get("blockers", []))
        report = {"schema": "oni.command-queue-stable-read-verification.v1", "status": "passed",
                  "case_count": 1, "live_admission": False, "hardlink_rejected": True,
                  "classification": "command_file_unreadable"}
    out = ROOT / "reports/command-queue-stable-read-verification.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
