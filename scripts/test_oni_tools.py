#!/usr/bin/env python3
"""The tool forwarder: schema, MCP framing, and failure without a game."""

from __future__ import annotations

import io
import json
import os
import pathlib
import re
import sys
import tempfile
import unittest
from unittest import mock

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tools"))

import oni_tools  # noqa: E402


def mcp_exchange(*messages: dict) -> list:
    output = io.StringIO()
    oni_tools.serve_mcp(io.StringIO("".join(json.dumps(item) + "\n" for item in messages)), output)
    return [json.loads(line) for line in output.getvalue().splitlines()]


class SchemaTests(unittest.TestCase):
    def test_schema_and_mod_name_the_same_tools(self) -> None:
        source = (REPO_ROOT / "mod" / "Source" / "Api" / "ApiTools.cs").read_text(encoding="utf-8")
        in_mod = set(re.findall(r'\{ "(\w+)", new Tool\(', source))
        names = {tool["name"] for tool in oni_tools.load_tools()}
        self.assertEqual(names - {"wait"}, in_mod)

    def test_openai_form_carries_every_tool(self) -> None:
        tools = oni_tools.openai_tools()
        self.assertEqual(len(tools), len(oni_tools.load_tools()))
        self.assertTrue(all(item["type"] == "function" for item in tools))
        self.assertTrue(all(item["function"]["parameters"]["type"] == "object" for item in tools))


class WaitTests(unittest.TestCase):
    def test_returns_early_when_a_duplicant_is_lost(self) -> None:
        replies = [
            {"ok": True, "result": {"events": [{"seq": 5, "type": "cycle_started"}], "next_since": 5}},
            {"ok": True, "result": {"events": [{"seq": 6, "type": "duplicant_lost"}], "next_since": 6}},
        ]
        with mock.patch.object(oni_tools, "call_mod", side_effect=replies), mock.patch.object(oni_tools.time, "sleep"):
            outcome = oni_tools.call("wait", {"seconds": 30, "since": 4})
        self.assertEqual(outcome["result"]["next_since"], 6)
        self.assertEqual(len(outcome["result"]["events"]), 2)

    def test_passes_on_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as bridge, mock.patch.dict(os.environ, {"ONI_AI_BRIDGE_DIR": bridge}):
            self.assertEqual(oni_tools.call("wait", {"seconds": 1})["error"]["code"], "game_unavailable")


class McpTests(unittest.TestCase):
    def test_handshake_and_listing(self) -> None:
        replies = mcp_exchange(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        )
        self.assertEqual([reply["id"] for reply in replies], [1, 2])
        self.assertEqual(replies[0]["result"]["protocolVersion"], "2025-06-18")
        self.assertIn("inputSchema", replies[1]["result"]["tools"][0])

    def test_refused_call_is_a_tool_error_not_a_protocol_error(self) -> None:
        with tempfile.TemporaryDirectory() as bridge, mock.patch.dict(os.environ, {"ONI_AI_BRIDGE_DIR": bridge}):
            reply = mcp_exchange({
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "get_status", "arguments": {}},
            })[0]
        self.assertTrue(reply["result"]["isError"])
        self.assertEqual(json.loads(reply["result"]["content"][0]["text"])["code"], "game_unavailable")

    def test_unknown_method_and_bad_json(self) -> None:
        output = io.StringIO()
        oni_tools.serve_mcp(io.StringIO('{"jsonrpc":"2.0","id":4,"method":"nope"}\nnot json\n'), output)
        codes = [json.loads(line)["error"]["code"] for line in output.getvalue().splitlines()]
        self.assertEqual(codes, [-32601, -32700])


if __name__ == "__main__":
    unittest.main()
