#!/usr/bin/env python3
"""Fail when the repository drifts from the single canonical version.

Run before every commit (the pre-commit hook does this):

    python scripts/check_canonical.py

It checks two things:

1. No removed system has come back: the old rule-based controller, the Bridge,
   the ChatGPT player adapters, Codex routing scripts and their documents
   (see AGENTS.md, "已删除、不得恢复").
2. ``tools/oni-tools.json`` and the tools the Mod registers in
   ``mod/Source/Api/ApiTools.cs`` name the same tools, so the schema can never
   describe a Mod that exists only outside git.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys
from typing import List

ROOT = pathlib.Path(__file__).resolve().parent.parent

REMOVED = (
    "bridge/",
    "config/",
    "fleet/",
    "archive/",
    "scenarios/",
    "agent/action_policy.py",
    "agent/approval_receipt.py",
    "agent/autonomous_authorization.py",
    "agent/candidates.py",
    "agent/command_queue.py",
    "agent/controller_loop.py",
    "agent/decision.py",
    "agent/llm_planner.py",
    "agent/model_loop.py",
    "agent/rules.py",
    "agent/strategy.py",
    "scripts/start-controller.ps1",
    "scripts/oni_autonomous.py",
    "scripts/prepare_autonomous_run.py",
    "scripts/write_dsh_autonomous_policy.py",
    "scripts/watch_controller.py",
    "scripts/configure-oni-model.ps1",
    "scripts/configure-openai.ps1",
    "scripts/test-codex-transport-stability.ps1",
    "scripts/check_codex_thread_health.py",
    "scripts/verify_codex_transport_stability.py",
    "tools/openai_player.py",
    "tools/openai_responses_player.py",
    "docs/model-routing-20261025.md",
    "docs/openai-tool-interface-20261102.md",
    "docs/test-handoff-chatgpt-20261027.md",
    "docs/roadmap-20261027.md",
    "docs/protocol.md",
)

# Mod tools answered by tools/oni_tools.py itself rather than by the Mod.
LOCAL_TOOLS = {"wait"}


def tracked_files() -> List[str]:
    output = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [line for line in output.splitlines() if line]


def main() -> int:
    problems: List[str] = []

    files = tracked_files()
    for entry in REMOVED:
        hits = [path for path in files if path == entry or (entry.endswith("/") and path.startswith(entry))]
        problems.extend(f"removed path is back: {path}" for path in hits)

    schema = json.loads((ROOT / "tools" / "oni-tools.json").read_text(encoding="utf-8"))
    schema_names = {tool["name"] for tool in schema["tools"]} - LOCAL_TOOLS
    registry = (ROOT / "mod" / "Source" / "Api" / "ApiTools.cs").read_text(encoding="utf-8")
    mod_names = set(re.findall(r'\{\s*"([a-z_]+)",\s*new Tool\(', registry))
    for name in sorted(schema_names - mod_names):
        problems.append(f"tools/oni-tools.json has {name}, the Mod does not register it")
    for name in sorted(mod_names - schema_names):
        problems.append(f"the Mod registers {name}, tools/oni-tools.json does not describe it")

    if problems:
        print("check_canonical: FAILED")
        for problem in problems:
            print("  " + problem)
        print("See AGENTS.md. The single version is main in this repository.")
        return 1
    print(f"check_canonical: ok ({len(mod_names)} Mod tools, {len(files)} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
