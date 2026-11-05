#!/usr/bin/env python3
"""The model player's loop, with a scripted model and no game."""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from agent import model_client, player_loop  # noqa: E402


def tool_call(call_id: str, name: str, args: dict) -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class TrimTests(unittest.TestCase):
    def test_short_history_is_untouched(self) -> None:
        messages = [{"role": "system"}, {"role": "user"}]
        self.assertIs(player_loop.trimmed(messages), messages)

    def test_a_tool_result_never_loses_its_call(self) -> None:
        messages = [{"role": "system"}, {"role": "user"}, {"role": "assistant"}]
        messages += [{"role": "tool"} for _ in range(5)] + [{"role": "assistant"}, {"role": "user"}]
        kept = player_loop.trimmed(messages, limit=4)
        self.assertEqual([entry["role"] for entry in kept], ["system", "assistant", "user"])


class TurnTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.notes = pathlib.Path(folder.name) / "notes.md"
        self.logged: list = []

    def turn(self, reply: dict) -> list:
        messages = [{"role": "system", "content": "s"}]
        player_loop.play_turn(messages, lambda _, __: reply, self.notes, self.logged.append)
        return messages

    def test_a_reply_without_calls_is_told_to_go_on(self) -> None:
        messages = self.turn({"content": "thinking"})
        self.assertEqual(messages[-1], {"role": "user", "content": player_loop.CONTINUE_PROMPT})

    def test_tool_results_answer_their_calls_in_order(self) -> None:
        with mock.patch.object(player_loop.oni_tools, "call", side_effect=[
            {"ok": True, "result": {"cycle": 3}},
            {"ok": False, "error": {"code": "invalid_placement"}},
        ]) as forwarded:
            messages = self.turn({"tool_calls": [
                tool_call("a", "get_status", {}),
                tool_call("b", "build", {"def_id": "Bed", "x": 1, "y": 2}),
            ]})
        self.assertEqual(forwarded.call_count, 2)
        self.assertEqual([entry.get("tool_call_id") for entry in messages[-2:]], ["a", "b"])
        self.assertIn("invalid_placement", messages[-1]["content"])

    def test_notes_stay_local_to_the_loop(self) -> None:
        with mock.patch.object(player_loop.oni_tools, "call") as forwarded:
            self.turn({"tool_calls": [tool_call("a", "write_notes", {"text": "plan"})]})
            messages = self.turn({"tool_calls": [tool_call("b", "read_notes", {})]})
        forwarded.assert_not_called()
        self.assertEqual(messages[-1]["content"], "plan")

    def test_malformed_arguments_are_reported_to_the_model(self) -> None:
        call = {"id": "a", "type": "function", "function": {"name": "dig", "arguments": "{oops"}}
        with mock.patch.object(player_loop.oni_tools, "call") as forwarded:
            messages = self.turn({"tool_calls": [call]})
        forwarded.assert_not_called()
        self.assertIn("JSON", messages[-1]["content"])

    def test_a_long_result_is_cut(self) -> None:
        with mock.patch.object(player_loop.oni_tools, "call", return_value={"ok": True, "result": "x" * 50000}):
            messages = self.turn({"tool_calls": [tool_call("a", "get_map", {})]})
        self.assertLess(len(messages[-1]["content"]), 13000)


class PaceTests(unittest.TestCase):
    def test_looks_are_counted_back_to_the_last_action(self) -> None:
        messages = [
            {"role": "assistant", "tool_calls": [tool_call("a", "dig", {})]},
            {"role": "assistant", "tool_calls": [tool_call("b", "inspect", {}), tool_call("c", "find", {})]},
            {"role": "assistant", "tool_calls": [tool_call("d", "get_map", {})]},
        ]
        self.assertEqual(player_loop.looks_since_last_act(messages), 3)

    def test_too_many_looks_get_a_reminder(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            messages = [{"role": "system", "content": "s"}]
            messages += [{"role": "assistant", "tool_calls": [tool_call(str(i), "inspect", {})]} for i in range(5)]
            seen: list = []
            player_loop.play_turn(
                messages,
                lambda history, _: seen.extend(history) or {"content": ""},
                pathlib.Path(folder) / "notes.md",
                lambda _: None,
            )
        self.assertTrue(any(entry["role"] == "user" and "连续查了 5 次" in entry["content"] for entry in seen))


class RetryTests(unittest.TestCase):
    def test_a_failed_request_is_retried(self) -> None:
        replies = iter([model_client.ModelRequestError("http_429"), {"content": "ok"}])

        def ask(_: list, __: list) -> dict:
            reply = next(replies)
            if isinstance(reply, Exception):
                raise reply
            return reply

        waits: list = []
        self.assertEqual(player_loop.patient(ask, waits.append)([], []), {"content": "ok"})
        self.assertEqual(waits, [player_loop.RETRY_SECONDS[0]])


class PromptTests(unittest.TestCase):
    def test_the_prompt_carries_the_skill_and_its_knowledge(self) -> None:
        prompt = player_loop.system_prompt()
        self.assertIn("你就是这局游戏的玩家", prompt)
        self.assertIn("二氧化碳下沉", prompt)


if __name__ == "__main__":
    unittest.main()
