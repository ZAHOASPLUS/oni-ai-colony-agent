#!/usr/bin/env python3
"""Verify the finite two-leaf Colony Plan completion contract offline.

The verifier uses the existing schema-complete temporary Bridge fixture.  It
proves that a confirmed first leaf is written back without publishing a
successor, and that a second leaf whose effect is already satisfied completes
without creating a command.  It never launches ONI/Steam/Bridge, calls a
model, or touches the real save/queue tree.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.controller_bridge import ColonyPlanControllerSequence
from agent.colony_plan import load_active_plan
from agent.controller_loop import EffectStatus
from scripts.test_controller_bridge import BridgeIntegrationTests


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    fixture = BridgeIntegrationTests()
    fixture.plan_targets = (4, 4)
    fixture.setUp()
    try:
        sequence = ColonyPlanControllerSequence(
            fixture.root,
            plan_id=fixture.plan["plan_id"],
            step_ids=["priority_1", "priority_2"],
            expected_identity=fixture.identity,
            intent_directory=fixture.store.directory,
            allow_pending_commands=True,
            max_calls=6,
            final_apply_guard=lambda _proposal: True,
        )
        first = sequence.step()
        require(first.effect is not None and first.effect.status is EffectStatus.PENDING,
                "first leaf did not stop at external confirmation")
        require(len(list((fixture.root / "commands" / "pending").glob("*.json"))) == 1,
                "first leaf did not publish exactly one pending command")
        plan_after_publish = load_active_plan(fixture.root, fixture.plan["plan_id"])
        require(plan_after_publish["steps"][0]["status"] == "awaiting_approval",
                "first leaf was not awaiting approval")
        require(plan_after_publish["steps"][1]["status"] == "waiting",
                "successor changed before confirmation")

        fixture.confirm()
        confirmed = sequence.step()
        require(confirmed.reconciliation is not None and confirmed.reconciliation.confirmed,
                "first leaf confirmation was not reconciled")
        plan_after_confirm = load_active_plan(fixture.root, fixture.plan["plan_id"])
        require(plan_after_confirm["steps"][0]["status"] == "completed",
                "first leaf was not completed")
        require(plan_after_confirm["steps"][1]["status"] == "waiting",
                "confirmation advanced successor implicitly")
        require(not list((fixture.root / "commands" / "pending").glob("*.json")),
                "confirmation left a successor pending command")
        require(sequence.step_index == 1, "sequence did not move to exactly one successor")

        satisfied = sequence.step()
        require(satisfied.decision.reason in {
            "target_already_at_priority", "expected_effects_already_satisfied",
        }, "satisfied successor did not take the no-command path")
        require(satisfied.effect is None, "already-satisfied leaf produced an effect")
        require(sequence.completed, "finite sequence did not complete")
        final_plan = load_active_plan(fixture.root, fixture.plan["plan_id"])
        require(final_plan["status"] == "completed", "plan status is not completed")
        require([step["status"] for step in final_plan["steps"]] == ["completed", "completed"],
                "finite plan leaves are not both completed")
        require(final_plan["steps"][1]["completion_mode"] == "already_satisfied_without_command",
                "successor completion mode was not persisted")
        require(not list((fixture.root / "commands" / "pending").glob("*.json")),
                "finite completion left a pending command")
        report: dict[str, Any] = {
            "schema": "oni.finite-completion-contract-verification.v1",
            "status": "passed",
            "scope": "temporary Bridge fixture; no real runtime",
            "live_admission": False,
            "game_launched": False,
            "first_leaf": {
                "initial_effect_status": first.effect.status.value,
                "confirmed": bool(confirmed.reconciliation.confirmed),
                "successor_status_after_confirmation": plan_after_confirm["steps"][1]["status"],
                "pending_after_confirmation": 0,
            },
            "successor": {
                "decision_reason": satisfied.decision.reason,
                "effect_status": None,
                "completion_mode": final_plan["steps"][1]["completion_mode"],
            },
            "final": {
                "plan_status": final_plan["status"],
                "step_statuses": [step["status"] for step in final_plan["steps"]],
                "pending_count": 0,
                "sequence_completed": sequence.completed,
            },
            "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "rollback": "temporary fixture cleaned up; no real mutation",
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    finally:
        fixture.doCleanups()


if __name__ == "__main__":
    raise SystemExit(main())
