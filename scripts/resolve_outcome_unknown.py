#!/usr/bin/env python3
"""Explicitly resolve one latched failed mutation after reconciling the game.

A failed mutating command whose outcome is unknown latches every further
mutation proposal for the Bridge directory (agent.command_queue).  A later
save or reload no longer clears that latch.  After the operator has inspected
the live game and knows whether the intended effect is present, this tool
records that decision inside the failed command while holding the lifecycle
custody lock.

It never deletes or moves the record, never resends the command, and never
touches game state.  The original bytes stay identified by their SHA-256.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import pathlib
import re
import sys
from typing import Any, Dict, Optional

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.command_custody import (  # noqa: E402
    CommandCustodyError,
    lifecycle_custody,
    read_stable_bytes,
)
from agent.command_queue import (  # noqa: E402
    OPERATOR_RESOLUTION_OUTCOMES,
    OPERATOR_RESOLUTION_SCHEMA,
    _is_unresolved_outcome_unknown,
    write_json_atomic,
)

_COMMAND_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
MAX_REASON_LENGTH = 500


class ResolutionError(ValueError):
    """The requested resolution is not allowed."""


def _reject_constant(token: str) -> Any:
    raise ResolutionError(f"failed command contains a non-finite JSON number: {token}")


def resolve(
    bridge_dir: pathlib.Path,
    command_id: str,
    observed_outcome: str,
    reason: str,
    *,
    now: Optional[dt.datetime] = None,
    what_if: bool = False,
) -> Dict[str, Any]:
    if not isinstance(command_id, str) or not _COMMAND_ID_RE.fullmatch(command_id):
        raise ResolutionError("command id is invalid")
    if observed_outcome not in OPERATOR_RESOLUTION_OUTCOMES:
        raise ResolutionError(f"observed outcome must be one of {sorted(OPERATOR_RESOLUTION_OUTCOMES)}")
    reason = (reason or "").strip()
    if not reason or len(reason) > MAX_REASON_LENGTH:
        raise ResolutionError(f"reason must be 1..{MAX_REASON_LENGTH} characters")
    resolved_at = (now or dt.datetime.now(dt.timezone.utc)).astimezone()

    bridge_dir = bridge_dir.expanduser().resolve()
    path = bridge_dir / "commands" / "failed" / f"{command_id}.json"
    with lifecycle_custody(bridge_dir):
        raw, _identity, _mtime = read_stable_bytes(path, label=f"commands/failed/{path.name}")
        command = json.loads(raw.decode("utf-8-sig"), parse_constant=_reject_constant)
        if not isinstance(command, dict) or command.get("id") != command_id:
            raise ResolutionError("failed command id does not match its file name")
        if not _is_unresolved_outcome_unknown(command):
            raise ResolutionError("failed command is not an unresolved mutation latch")
        resolution = {
            "schema": OPERATOR_RESOLUTION_SCHEMA,
            "command_id": command_id,
            "resolved_by": "local_human",
            "resolved_at": resolved_at.isoformat(timespec="seconds"),
            "observed_outcome": observed_outcome,
            "reason": reason,
            "original_status": command.get("status"),
            "original_sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
        }
        resolved = dict(command)
        # A raw executing claim moved to failed/ keeps status "executing";
        # the resolved record is a canonical failed command.
        resolved["status"] = "failed"
        resolved["operator_resolution"] = resolution
        if not what_if:
            write_json_atomic(path, resolved)
    return {
        "schema": "oni.operator-resolution-result.v1",
        "status": "what_if" if what_if else "resolved",
        "path": str(path),
        "resolution": resolution,
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bridge-dir", type=pathlib.Path, required=True)
    parser.add_argument("--command-id", required=True)
    parser.add_argument("--observed-outcome", required=True, choices=sorted(OPERATOR_RESOLUTION_OUTCOMES))
    parser.add_argument("--reason", required=True, help="What was checked in the live game.")
    parser.add_argument("--what-if", action="store_true", help="Validate and print without writing.")
    args = parser.parse_args(argv)
    try:
        result = resolve(
            args.bridge_dir,
            args.command_id,
            args.observed_outcome,
            args.reason,
            what_if=args.what_if,
        )
    except (ResolutionError, CommandCustodyError, OSError, ValueError) as exc:
        print(json.dumps({"status": "refused", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
