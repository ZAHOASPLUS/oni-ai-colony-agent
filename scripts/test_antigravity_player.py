#!/usr/bin/env python3
"""Offline tests for the Antigravity ONI player interface and session bridge."""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tools"))

from antigravity_player import (  # noqa: E402
    AntigravityPlayer,
    AntigravityPlayerError,
    AntigravitySession,
    compact_observation,
    mcp_config_payload,
)


class AntigravityPlayerLoopTests(unittest.TestCase):
    def test_multi_turn_function_call_preserves_thought_parts_and_returns_text(self) -> None:
        payloads = []
        executed = []

        responses = [
            {
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {
                            "role": "model",
                            "parts": [
                                {"thought": True, "text": "Check lifecycle first.", "thoughtSignature": "sig-1"},
                                {"functionCall": {"id": "call-1", "name": "get_lifecycle", "args": {}}},
                            ],
                        },
                    }
                ]
            },
            {
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {
                            "role": "model",
                            "parts": [
                                {"thought": True, "text": "Colony is in_game.", "thoughtSignature": "sig-2"},
                                {"text": "Ready in oni-test clone."},
                            ],
                        },
                    }
                ],
                "usageMetadata": {"promptTokenCount": 120, "candidatesTokenCount": 30},
            },
        ]

        def fake_request(payload: dict) -> dict:
            payloads.append(payload)
            return responses.pop(0)

        def fake_tool(name: str, args: dict | None = None) -> dict:
            executed.append((name, dict(args or {})))
            return {"ok": True, "result": {"state": "in_game", "save": "oni-test"}}

        player = AntigravityPlayer(
            model="gemini-2.5-pro",
            api_key="secret-test-key",
            tool_executor=fake_tool,
            request=fake_request,
        )
        outcome = player.run([{"role": "user", "content": "Inspect lifecycle"}], system_instruction="Follow SKILL.md")

        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["rounds"], 2)
        self.assertEqual(outcome["content"], "Ready in oni-test clone.")
        self.assertEqual(executed, [("get_lifecycle", {})])
        self.assertEqual(payloads[0]["systemInstruction"], {"parts": [{"text": "Follow SKILL.md"}]})
        # Verify thoughtSignature was preserved on the follow-up turn
        second_contents = payloads[1]["contents"]
        self.assertEqual(second_contents[1]["parts"][0]["thoughtSignature"], "sig-1")
        self.assertEqual(
            second_contents[2],
            {
                "role": "user",
                "parts": [
                    {
                        "functionResponse": {
                            "id": "call-1",
                            "name": "get_lifecycle",
                            "response": {"ok": True, "result": {"state": "in_game", "save": "oni-test"}},
                        }
                    }
                ],
            },
        )

    def test_unknown_tool_and_bad_args_become_function_responses(self) -> None:
        responses = [
            {
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {
                            "role": "model",
                            "parts": [
                                {"functionCall": {"name": "invented_tool", "args": {}}},
                                {"functionCall": {"name": "dig", "args": "not-json"}},
                            ],
                        },
                    }
                ]
            },
            {
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {"role": "model", "parts": [{"text": "Handled errors."}]},
                    }
                ]
            },
        ]
        captured = []

        def fake_request(payload: dict) -> dict:
            captured.append(payload)
            return responses.pop(0)

        player = AntigravityPlayer(model="gemini-2.5-pro", api_key="k", request=fake_request)
        outcome = player.run([{"role": "user", "content": "Try tools"}])
        self.assertTrue(outcome["ok"])
        fn_responses = captured[1]["contents"][2]["parts"]
        self.assertEqual(fn_responses[0]["functionResponse"]["response"]["error"]["code"], "unknown_tool")
        self.assertEqual(fn_responses[1]["functionResponse"]["response"]["error"]["code"], "invalid_tool_arguments")

    def test_loop_limit_and_filter_and_max_tokens_errors(self) -> None:
        infinite_call = {
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {"role": "model", "parts": [{"functionCall": {"name": "get_lifecycle", "args": {}}}]},
                }
            ]
        }
        player = AntigravityPlayer(
            model="gemini-2.5-pro",
            api_key="k",
            max_rounds=2,
            tool_executor=lambda n, a: {"ok": True, "result": {}},
            request=lambda p: infinite_call,
        )
        res = player.run([{"role": "user", "content": "Loop"}])
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"]["code"], "tool_loop_limit")

        max_tok = AntigravityPlayer(
            model="gemini-2.5-pro",
            api_key="k",
            request=lambda p: {"candidates": [{"finishReason": "MAX_TOKENS", "content": {"role": "model", "parts": [{"text": "cut"}]}}]},
        ).run([{"role": "user", "content": "Long"}])
        self.assertEqual(max_tok["error"]["code"], "response_incomplete")

        blocked = AntigravityPlayer(
            model="gemini-2.5-pro",
            api_key="k",
            request=lambda p: {"promptFeedback": {"blockReason": "SAFETY"}},
        ).run([{"role": "user", "content": "Blocked"}])
        self.assertEqual(blocked["error"]["code"], "content_filtered")

    def test_missing_api_key_and_error_handling(self) -> None:
        player = AntigravityPlayer(model="gemini-2.5-pro", api_key="", base_url="https://example.invalid/v1beta")
        res = player.run([{"role": "user", "content": "Hi"}])
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"]["code"], "missing_api_key")
        with self.assertRaises(AntigravityPlayerError):
            AntigravityPlayer(model="", api_key="k")


class AntigravitySessionTests(unittest.TestCase):
    def test_cursor_persistence_wait_cap_and_audit_log(self) -> None:
        calls = []

        def fake_tool(name: str, args: dict | None = None) -> dict:
            calls.append((name, dict(args or {})))
            if name == "get_events":
                return {"ok": True, "result": {"events": [], "next_since": 42, "missed": 0}}
            if name == "wait":
                return {
                    "ok": True,
                    "result": {
                        "events": [
                            {"seq": 43, "type": "building_completed", "def": "Wire"},
                            {"seq": 44, "type": "building_completed", "def": "Wire"},
                            {"seq": 45, "type": "alert_raised", "severity": "bad", "title": "Starvation"},
                        ],
                        "next_since": 45,
                        "waited_seconds": args["seconds"],
                        "stopped_early": True,
                    },
                }
            return {
                "ok": True,
                "result": {
                    "time": {"cycle": 122, "paused": False},
                    "duplicants": [{"name": "Ada", "health": 100, "kcal": 3200, "extra": "dropped"}],
                    "colony": {"food_kcal": 12000},
                    "alerts": [],
                    "research": {},
                    "work": {},
                    "printables_waiting": True,
                },
            }

        with tempfile.TemporaryDirectory() as tmp:
            session = AntigravitySession(pathlib.Path(tmp), tool_executor=fake_tool)
            session.call("get_events", {})
            self.assertEqual(session.load_cursor(), 42)

            batch = session.wait_and_check(seconds=90.0)
            # Wait must automatically pick up since=42 and cap seconds to 30.0
            self.assertEqual(calls[1], ("wait", {"seconds": 30.0, "since": 42}))
            self.assertEqual(session.load_cursor(), 45)
            # Compact observation collapses Wire completions into construction_counts
            wait_result = batch[0]["outcome"]["result"]
            self.assertEqual(wait_result["construction_counts"], {"building_completed:Wire": 2})
            self.assertEqual(len(wait_result["events"]), 1)
            self.assertNotIn("extra", batch[1]["outcome"]["result"]["duplicants"][0])

            lines = (pathlib.Path(tmp) / "steps.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 3)
            self.assertEqual(json.loads(lines[1])["tool"], "wait")

    def test_compact_inspect_keeps_only_queued_recipes(self) -> None:
        raw = {
            "ok": True,
            "result": {
                "buildings": [
                    {
                        "def": "MicrobeMusher",
                        "recipes": [
                            {"id": "MushBar", "queued": 5},
                            {"id": "BasicPlantBar", "queued": 0},
                        ],
                    }
                ]
            },
        }
        compacted = compact_observation("inspect", raw)
        self.assertEqual(compacted["result"]["buildings"][0]["recipes"], [{"id": "MushBar", "queued": 5}])
        self.assertIn("oni", mcp_config_payload()["mcpServers"])

    def test_compact_status_keeps_duplicant_ids_and_the_food_report(self) -> None:
        food = {"today_made": 2400.0, "today_used": -3000.0, "yesterday_net": -1500.0}
        raw = {
            "ok": True,
            "result": {
                "time": {"cycle": 197, "paused": True},
                "duplicants": [
                    {"x": 40, "y": 120, "id": 101, "name": "Marie", "status": ["Hungry"]},
                    {"x": 88, "y": 131, "id": 105, "name": "Marie", "status": ["Starving"]},
                ],
                "colony": {"food_kcal": 800.0, "food_days": 0.1, "food_report_kcal": food},
            },
        }
        result = compact_observation("get_status", raw)["result"]
        self.assertEqual([(dupe["id"], dupe["name"]) for dupe in result["duplicants"]], [(101, "Marie"), (105, "Marie")])
        self.assertEqual(result["colony"]["food_report_kcal"], food)

    def test_shared_name_refusal_reaches_the_player_whole(self) -> None:
        refusal = {
            "ok": False,
            "error": {
                "code": "ambiguous_duplicant",
                "message": "2 living duplicants match Mae: ids 103, 104.",
                "candidate_ids": [103, 104],
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            session = AntigravitySession(pathlib.Path(tmp), tool_executor=lambda name, args=None: refusal)
            item = session.run_batch([["learn_skill", {"duplicant": "Mae", "skill": "Cooking1"}]])[0]
        self.assertEqual(item["outcome"], refusal)


if __name__ == "__main__":
    unittest.main()
