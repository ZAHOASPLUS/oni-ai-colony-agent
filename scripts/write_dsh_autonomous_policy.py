"""Write the V2 autonomous action policy and control document for a run.

Both documents bind to one exact run/session/world/save identity.  The policy is
``oni.action-policy.v2`` with ``execution_mode=autonomous``; the control
document is ``oni.autonomous-control.v1`` with ``desired_state`` running,
paused or stopped.

Usage:
    python -B scripts/write_dsh_autonomous_policy.py --session SESSION [--world 0] \
        [--desired running|paused|stopped] [--run-id ID] [--controller-id ID]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.action_policy import (  # noqa: E402
    AUTONOMOUS_ACTION_POLICY_SCHEMA,
    AUTONOMOUS_EXECUTION_MODE,
    action_policy_hash,
    action_policy_is_valid,
)

KLEI = pathlib.Path.home() / "Documents" / "Klei" / "OxygenNotIncluded"
BRIDGE = KLEI / "ai-bridge"
CONTROL_SCHEMA = "oni.autonomous-control.v1"
SAVE_HASH = "sha256:504869c1fca4733169ee479a0df71efa32f9959a26040d59909d5f220e85ccee"

# Every action the ONI controller may ever take, most conservative first.
ALLOWED_ACTIONS = [
    "noop",
    "focus_note",
    "set_paused",
    "set_research",
    "set_duplicant_chore_priority",
    "mark_dig_cells",
    "cancel_dig_cells",
    "place_building_blueprint",
    "cancel_building_blueprint",
    "place_utility_blueprint_path",
    "cancel_utility_blueprint_path",
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", default="dsh-20261025-r01")
    parser.add_argument("--controller-id", default="dsh-deepseek-controller")
    parser.add_argument("--session", required=True)
    parser.add_argument("--world", type=int, default=0)
    parser.add_argument("--desired", choices=["running", "paused", "stopped"], default="running")
    parser.add_argument("--save-hash", default=SAVE_HASH)
    # The command TTL must outlast the publish -> authorize round trip.  That
    # round trip includes the model loop's 60 s minimum call interval plus the
    # watch poll, so a 120 s TTL expired commands before they could be approved
    # and every one of them was rejected with ``expired_before_approval``
    # (2026-10-25 round 22).  300 s is the maximum the custody validator accepts.
    parser.add_argument("--ttl", type=int, default=300)
    parser.add_argument("--config", default="", help="controller config supplying the nine startup anchors")
    args = parser.parse_args()

    policy = {
        "schema": AUTONOMOUS_ACTION_POLICY_SCHEMA,
        "execution_mode": AUTONOMOUS_EXECUTION_MODE,
        "run_id": args.run_id,
        "controller_id": args.controller_id,
        "session_id": args.session,
        "world_id": args.world,
        "actions_enabled": True,
        "suggestions_enabled": True,
        "allowed_actions": ALLOWED_ACTIONS,
        "allowed_save_hashes": [args.save_hash],
        "command_ttl_seconds": args.ttl,
        "max_pending_commands": 1,
        "generated_at": dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds"),
    }
    valid = action_policy_is_valid(policy)
    policy_hash = action_policy_hash(policy)
    if not valid or policy_hash is None:
        from agent.action_policy import action_policy_validation_blockers

        print(json.dumps({"error": "policy_invalid", "blockers": action_policy_validation_blockers(policy)}, indent=2))
        return 2

    anchors: dict = {}
    if args.config:
        config_path = pathlib.Path(args.config)
        document = json.loads(config_path.read_text(encoding="utf-8-sig"))
        candidate = document.get("startup_expectations")
        if not isinstance(candidate, dict):
            print(json.dumps({"error": "config_missing_startup_expectations", "config": str(config_path)}, indent=2))
            return 3
        anchors = dict(candidate)
    if not anchors:
        print(json.dumps({"error": "startup_expectations_required", "hint": "pass --config <controller config>"}, indent=2))
        return 3

    control = {
        "schema": CONTROL_SCHEMA,
        "run_id": args.run_id,
        "controller_id": args.controller_id,
        "session_id": args.session,
        "world_id": args.world,
        "save_id_hash": args.save_hash,
        "startup_expectations": anchors,
        "desired_state": args.desired,
        "updated_at": dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds"),
    }

    policy_path = BRIDGE / "action-policy.json"
    control_path = BRIDGE / "autonomous-control.json"
    policy_path.write_text(json.dumps(policy, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    control_path.write_text(json.dumps(control, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(json.dumps({
        "policy": str(policy_path),
        "policy_valid": valid,
        "policy_hash": policy_hash,
        "control": str(control_path),
        "desired_state": args.desired,
        "session_id": args.session,
        "world_id": args.world,
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
