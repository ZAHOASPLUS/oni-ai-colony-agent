#!/usr/bin/env python3
"""Create and operate durable, approval-gated ONI colony plans."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any, Dict


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.colony_plan import (
    ColonyPlanError,
    abort_colony_plan,
    advance_colony_plan,
    create_colony_plan,
    load_active_plan,
    pause_colony_plan,
    plan_operation_result,
    recover_colony_plan,
    resume_colony_plan,
)
from agent.llm_planner import colony_plan_definition_from_plan


DEFAULT_BRIDGE = pathlib.Path.home() / "Documents" / "Klei" / "OxygenNotIncluded" / "ai-bridge"


def load_definition(path: pathlib.Path) -> Dict[str, Any]:
    value = json.loads(path.expanduser().resolve().read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ColonyPlanError("Plan definition must be a JSON object.")
    return value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bridge-dir", type=pathlib.Path, default=DEFAULT_BRIDGE)
    subparsers = parser.add_subparsers(dest="operation", required=True)

    create = subparsers.add_parser("create")
    create.add_argument("--definition", type=pathlib.Path, required=True)
    project = subparsers.add_parser("create-from-llm")
    project.add_argument("--planner-plan", type=pathlib.Path, required=True)
    project.add_argument("--advice", type=pathlib.Path, required=True)
    project.add_argument("--title", default="LLM ranked colony response")
    project.add_argument("--goal")

    for name in ("advance", "pause", "resume", "abort", "recover", "show"):
        command = subparsers.add_parser(name)
        command.add_argument("--plan-id")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    bridge_dir = args.bridge_dir.expanduser().resolve()
    try:
        if args.operation == "create":
            plan = create_colony_plan(bridge_dir, load_definition(args.definition))
        elif args.operation == "create-from-llm":
            planner = load_definition(args.planner_plan)
            advice = json.loads(args.advice.expanduser().resolve().read_text(encoding="utf-8-sig"))
            if not isinstance(advice, list):
                raise ColonyPlanError("Advice must be a JSON array.")
            definition = colony_plan_definition_from_plan(
                planner, advice, title=args.title, goal=args.goal
            )
            plan = create_colony_plan(bridge_dir, definition)
        elif args.operation == "advance":
            plan = advance_colony_plan(bridge_dir, args.plan_id)
        elif args.operation == "pause":
            plan = pause_colony_plan(bridge_dir, args.plan_id)
        elif args.operation == "resume":
            plan = resume_colony_plan(bridge_dir, args.plan_id)
        elif args.operation == "abort":
            plan = abort_colony_plan(bridge_dir, args.plan_id)
        elif args.operation == "recover":
            plan = recover_colony_plan(bridge_dir, args.plan_id)
        else:
            plan = load_active_plan(bridge_dir, args.plan_id)
    except (ColonyPlanError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "failed", "classification": "behavior_fail", "error": str(exc)}))
        return 1
    print(json.dumps(plan_operation_result(plan), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
