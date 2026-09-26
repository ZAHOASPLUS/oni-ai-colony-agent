#!/usr/bin/env python3
"""Regression checks for the redacted Codex rollout health probe."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path


SCRIPT = Path(__file__).with_name("check_codex_thread_health.py")


def timestamp(seconds_ago: int) -> str:
    value = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    return value.isoformat().replace("+00:00", "Z")


def event(seconds_ago: int, payload_type: str) -> dict[str, object]:
    return {
        "timestamp": timestamp(seconds_ago),
        "type": "event_msg",
        "payload": {"type": payload_type},
    }


def reasoning(seconds_ago: int) -> dict[str, object]:
    return {
        "timestamp": timestamp(seconds_ago),
        "type": "response_item",
        "payload": {"type": "reasoning"},
    }


def commentary(seconds_ago: int) -> dict[str, object]:
    return {
        "timestamp": timestamp(seconds_ago),
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "assistant",
            "phase": "commentary",
            "content": [{"type": "output_text", "text": "fixture"}],
        },
    }


class ThreadHealthProbeTests(unittest.TestCase):
    def run_probe(self, records: list[dict[str, object]]) -> tuple[int, dict[str, object]]:
        thread_id = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as temporary_directory:
            codex_home = Path(temporary_directory)
            rollout_directory = codex_home / "sessions" / "fixture"
            rollout_directory.mkdir(parents=True)
            rollout = rollout_directory / f"rollout-{thread_id}.jsonl"
            rollout.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--thread-id",
                    thread_id,
                    "--codex-home",
                    str(codex_home),
                    "--active-seconds",
                    "90",
                    "--stalled-seconds",
                    "180",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
        return result.returncode, json.loads(result.stdout)

    def test_recent_reasoning_is_active(self) -> None:
        return_code, result = self.run_probe([event(10, "task_started"), reasoning(5)])
        self.assertEqual(return_code, 0)
        self.assertEqual(result["classification"], "active")

    def test_blank_turn_requires_full_stall_window(self) -> None:
        return_code, result = self.run_probe([event(200, "task_started")])
        self.assertEqual(return_code, 2)
        self.assertEqual(result["classification"], "startup_stall_candidate")

    def test_recent_work_with_old_commentary_warns_about_visibility(self) -> None:
        return_code, result = self.run_probe(
            [event(80, "task_started"), commentary(70), reasoning(5)]
        )
        self.assertEqual(return_code, 0)
        self.assertEqual(result["classification"], "active")
        self.assertTrue(result["visibility_desync_risk"])

    def test_terminal_turn_is_not_active(self) -> None:
        return_code, result = self.run_probe(
            [event(20, "task_started"), reasoning(15), event(5, "task_complete")]
        )
        self.assertEqual(return_code, 0)
        self.assertEqual(result["classification"], "idle_or_terminal")


if __name__ == "__main__":
    unittest.main()
