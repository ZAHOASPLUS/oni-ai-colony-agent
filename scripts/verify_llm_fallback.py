#!/usr/bin/env python3
"""Offline validation of optional LLM ranking and rule-only fallback behavior."""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys
import tempfile
import time
from typing import Any, Callable, Dict, List


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.llm_planner import ModelRequestError, generate_plan


TEST_KEY = "test-secret-that-must-never-appear-in-output"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def config(**overrides: Any) -> Dict[str, Any]:
    value: Dict[str, Any] = {
        "api_key": TEST_KEY,
        "base_url": "https://example.invalid/v1",
        "model": "offline-test-model",
        "wire_api": "chat_completions",
        "input_cost_per_million": "1.0",
        "output_cost_per_million": "2.0",
    }
    value.update(overrides)
    return value


def state() -> Dict[str, Any]:
    return {
        "schema": "oni.state.v2",
        "session_id": "session-llm-test",
        "snapshot_id": "session-llm-test:10",
        "seq": 10,
        "game": {"paused": False},
        "cycle": 12,
        "duplicants": {"total": 3, "stressed": 0, "idle": 0},
        "resources": {"food_kcal": 20_000, "oxygen_kg_estimate": 50, "water_kg_estimate": 500},
        "alerts": ["low_oxygen"],
        "research": {"current": "FarmingTech"},
        "buildings": {"oxygen_diffuser": 1, "outhouse": 1, "manual_generator": 1},
        "quality": {
            key: {"status": "observed", "source": "fixture"}
            for key in (
                "game.paused",
                "cycle",
                "duplicants.total",
                "duplicants.stressed",
                "duplicants.idle",
                "resources.food_kcal",
                "resources.oxygen_kg_estimate",
                "resources.water_kg_estimate",
                "alerts",
                "research.current",
                "buildings.oxygen_diffuser",
                "buildings.outhouse",
                "buildings.manual_generator",
            )
        },
        "bridge": {"stale": False, "snapshot_age_seconds": 1.0},
    }


def advice() -> List[Dict[str, Any]]:
    return [
        {
            "incident_id": "low_oxygen",
            "title": "Oxygen alert observed",
            "risk": "high",
            "reason": "Observed oxygen alert persisted.",
            "evidence": {"alerts": ["low_oxygen"]},
            "command": {"action": "set_paused", "args": {"paused": True}},
        },
        {
            "incident_id": "low_food",
            "title": "Registered food is below the risk threshold",
            "risk": "medium",
            "reason": "The complete registered-food sum is below the conservative inventory risk threshold.",
            "evidence": {"resources.food_kcal": 20_000},
            "command": {"action": "focus_note", "args": {"tag": "food"}},
        },
    ]


def response(payload: Any, *, usage: Dict[str, Any] | None = None) -> Dict[str, Any]:
    return {
        "choices": [{"message": {"content": payload if isinstance(payload, str) else json.dumps(payload)}}],
        "usage": usage or {},
    }


def assert_rule_fallback(plan: Dict[str, Any], status: str) -> None:
    require(plan["mode"] == "rule_only", f"{status} did not retain rule_only mode")
    require(plan["llm"]["status"] == status, f"expected {status}, got {plan['llm']['status']}")
    require([item["incident_id"] for item in plan["plan"]] == ["low_oxygen", "low_food"], "rule fallback changed incident order")


def assert_no_secret(directory: pathlib.Path) -> None:
    for path in directory.glob("*.json"):
        require(TEST_KEY not in path.read_text(encoding="utf-8"), f"secret leaked into {path.name}")


def case_missing_key(root: pathlib.Path) -> Dict[str, Any]:
    plan = generate_plan(root, state(), [], advice(), enable_llm=True, config_override=config(api_key=None))
    assert_rule_fallback(plan, "missing_api_key")
    require(plan["llm"]["called"] is False and plan["llm"]["api_key_present"] is False, "missing-key health is wrong")
    assert_no_secret(root)
    return {"status": plan["llm"]["status"], "called": plan["llm"]["called"]}


def case_request_failures(root: pathlib.Path) -> Dict[str, Any]:
    def forbidden(_config: Dict[str, Any], _prompt: str) -> Dict[str, Any]:
        raise ModelRequestError("http_403")

    plan_403 = generate_plan(root / "403", state(), [], advice(), enable_llm=True, config_override=config(), model_caller=forbidden)
    assert_rule_fallback(plan_403, "http_403")
    assert_no_secret(root / "403")

    def timeout(_config: Dict[str, Any], _prompt: str) -> Dict[str, Any]:
        raise TimeoutError("synthetic timeout")

    plan_timeout = generate_plan(
        root / "timeout", state(), [], advice(), enable_llm=True, config_override=config(), model_caller=timeout
    )
    assert_rule_fallback(plan_timeout, "timeout")
    assert_no_secret(root / "timeout")
    return {"http_status": plan_403["llm"]["status"], "timeout_status": plan_timeout["llm"]["status"]}


def case_invalid_outputs(root: pathlib.Path) -> Dict[str, Any]:
    invalid_json = generate_plan(
        root / "invalid-json",
        state(),
        [],
        advice(),
        enable_llm=True,
        config_override=config(),
        model_caller=lambda _config, _prompt: response("not-json"),
    )
    assert_rule_fallback(invalid_json, "invalid_json")

    unauthorized = generate_plan(
        root / "unauthorized",
        state(),
        [],
        advice(),
        enable_llm=True,
        config_override=config(),
        model_caller=lambda _config, _prompt: response(
            {
                "schema": "oni.llm-ranking.v1",
                "ranked_incident_ids": ["low_oxygen"],
                "action": "set_research",
            }
        ),
    )
    assert_rule_fallback(unauthorized, "invalid_output_schema")

    unknown_incident = generate_plan(
        root / "unknown-incident",
        state(),
        [],
        advice(),
        enable_llm=True,
        config_override=config(),
        model_caller=lambda _config, _prompt: response(
            {"schema": "oni.llm-ranking.v1", "ranked_incident_ids": ["invented_incident"]}
        ),
    )
    assert_rule_fallback(unknown_incident, "invalid_incident_reference")
    assert_no_secret(root / "invalid-json")
    assert_no_secret(root / "unauthorized")
    assert_no_secret(root / "unknown-incident")
    return {
        "invalid_json": invalid_json["llm"]["status"],
        "unauthorized_action": unauthorized["llm"]["status"],
        "unknown_incident": unknown_incident["llm"]["status"],
    }


def case_valid_ranking_and_budgets(root: pathlib.Path) -> Dict[str, Any]:
    calls = 0

    def valid(_config: Dict[str, Any], _prompt: str) -> Dict[str, Any]:
        nonlocal calls
        calls += 1
        return response(
            {"schema": "oni.llm-ranking.v1", "ranked_incident_ids": ["low_food", "low_oxygen"]},
            usage={"prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100},
        )

    plan = generate_plan(root, state(), [], advice(), enable_llm=True, config_override=config(), model_caller=valid)
    require(plan["mode"] == "llm_ranked" and plan["llm"]["status"] == "ok", "valid ranking was not accepted")
    require([item["incident_id"] for item in plan["plan"]] == ["low_food", "low_oxygen"], "ranking was not applied")
    require(all("args" not in item and "command" not in item for item in plan["plan"]), "LLM plan contains executable parameters")
    require(plan["llm"]["estimated_input_tokens"] <= plan["llm"]["input_token_budget"], "prompt exceeded 8K budget")
    require(plan["llm"]["request_timeout_seconds"] == 30, "network timeout is not 30 seconds")
    require(plan["llm"]["minimum_call_interval_seconds"] == 60, "minimum call interval is not 60 seconds")
    require(plan["llm"]["cost_estimate_usd"] == 0.0012, "cost estimate is wrong")

    repeated = generate_plan(root, state(), [], advice(), enable_llm=True, config_override=config(), model_caller=valid)
    require(repeated["llm"]["status"] == "unchanged_incidents" and calls == 1, "unchanged incidents called the model twice")

    changed = advice()
    changed[0] = dict(changed[0])
    changed[0]["reason"] = "Changed incident evidence."
    rate_limited = generate_plan(root, state(), [], changed, enable_llm=True, config_override=config(), model_caller=valid)
    require(rate_limited["llm"]["status"] == "rate_limited" and calls == 1, "global 60-second interval was bypassed")
    assert_no_secret(root)
    return {
        "mode": plan["mode"],
        "estimated_input_tokens": plan["llm"]["estimated_input_tokens"],
        "cost_estimate_usd": plan["llm"]["cost_estimate_usd"],
        "repeat_status": repeated["llm"]["status"],
        "changed_status": rate_limited["llm"]["status"],
        "model_calls": calls,
    }


def run_case(name: str, function: Callable[[pathlib.Path], Dict[str, Any]], root: pathlib.Path) -> Dict[str, Any]:
    started = time.perf_counter()
    try:
        details = function(root / name)
        return {
            "name": name,
            "status": "passed",
            "duration_seconds": round(time.perf_counter() - started, 3),
            "details": details,
        }
    except Exception as exc:
        return {
            "name": name,
            "status": "failed",
            "classification": "behavior_fail",
            "duration_seconds": round(time.perf_counter() - started, 3),
            "error": str(exc),
        }


def main() -> int:
    cases: List[tuple[str, Callable[[pathlib.Path], Dict[str, Any]]]] = [
        ("missing_key", case_missing_key),
        ("request_failures", case_request_failures),
        ("invalid_outputs", case_invalid_outputs),
        ("valid_ranking_budgets", case_valid_ranking_and_budgets),
    ]
    with tempfile.TemporaryDirectory(prefix="oni-llm-v2-") as temporary:
        root = pathlib.Path(temporary)
        results = [run_case(name, function, root) for name, function in cases]

    passed = all(result["status"] == "passed" for result in results)
    report = {
        "schema": "oni.llm-fallback-report.v1",
        "generated_at": dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds"),
        "status": "passed" if passed else "failed",
        "classification": None if passed else "behavior_fail",
        "cases": results,
    }
    report_path = PROJECT_ROOT / "reports" / "llm-fallback.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for result in results:
        detail = result.get("error") or json.dumps(result.get("details"), ensure_ascii=False, sort_keys=True)
        print(f"[{result['status'].upper()}] {result['name']}: {detail}")
    print(f"Report: {report_path}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
