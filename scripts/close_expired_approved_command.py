"""Terminate one never-dispatched expired command so the loop can advance.

The autonomous single-in-flight gate counts a command in ``approved`` as an
active intent.  A command that expired before dispatch can never be executed, so
it must be moved to a terminal stage or the controller is blocked forever: the
game must run for the command to execute, but resuming the game needs a new
command, which the in-flight gate refuses to publish (2026-10-25 round 4).

This uses the repository's own custody-checked ``move_to_rejected`` so the
envelope, receipt and lifecycle lock semantics are the designed ones.

Usage:
    python -B scripts/close_expired_approved_command.py --command-id ID [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.command_queue import move_to_rejected  # noqa: E402

KLEI = pathlib.Path.home() / "Documents" / "Klei" / "OxygenNotIncluded"
BRIDGE = KLEI / "ai-bridge"
REASON = "expired_before_dispatch"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--command-id", required=True)
    parser.add_argument("--bridge-dir", type=pathlib.Path, default=BRIDGE)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    approved = args.bridge_dir / "commands" / "approved" / f"{args.command_id}.json"
    if not approved.is_file():
        print(json.dumps({"status": "not_found", "path": str(approved)}, indent=2))
        return 2
    command = json.loads(approved.read_text(encoding="utf-8-sig"))
    summary = {
        "command_id": command.get("id"),
        "action": command.get("action"),
        "status": command.get("status"),
        "created_at": command.get("created_at"),
        "expires_at": command.get("expires_at"),
        "attempt_id": command.get("attempt_id"),
        "execution_started_at": command.get("execution_started_at"),
        "executed_ack_seq": command.get("executed_ack_seq"),
        "reason": REASON,
    }
    if args.dry_run:
        summary["dry_run"] = True
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 0

    if command.get("attempt_id") or command.get("execution_started_at") or command.get("executed_ack_seq"):
        summary["status"] = "refused_dispatched"
        summary["detail"] = "command carries dispatch evidence; reconcile instead of rejecting"
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 3

    destination = move_to_rejected(
        approved,
        args.bridge_dir / "commands" / "rejected",
        command,
        REASON,
        source_stage="approved",
    )
    summary["status"] = "rejected" if destination is not None else "rejected_no_path"
    summary["destination"] = str(destination) if destination is not None else None
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
