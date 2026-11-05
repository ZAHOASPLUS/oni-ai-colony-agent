#!/usr/bin/env python3
"""Offline tests for the supervisor's player watcher."""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from watch_player import Tail, Watcher, wants_pause  # noqa: E402


def fail(tool: str, code: str) -> dict:
    return {"tool": tool, "ok": False, "error": {"code": code, "message": "no"}}


def status(cycle: int, fraction: float, dupes: int, paused: bool = False) -> dict:
    return {"time": {"cycle": cycle, "cycle_fraction": fraction, "paused": paused, "speed": 3},
            "duplicants": [{"id": i} for i in range(dupes)]}


class WatcherTests(unittest.TestCase):
    def test_repeated_failure_alerts_once_per_window(self) -> None:
        w = Watcher(stall_seconds=600, fail_window=300, fail_count=3)
        self.assertEqual(w.feed_audit(fail("build", "invalid_placement"), 0), [])
        self.assertEqual(w.feed_audit(fail("build", "invalid_placement"), 10), [])
        alerts = w.feed_audit(fail("build", "invalid_placement"), 20)
        self.assertEqual(len(alerts), 1)
        self.assertTrue(alerts[0].startswith("failing: build -> invalid_placement x3"))
        self.assertEqual(w.feed_audit(fail("build", "invalid_placement"), 30), [])
        # Different codes and spread-out failures do not add up.
        self.assertEqual(w.feed_audit(fail("dig", "a"), 0), [])
        self.assertEqual(w.feed_audit(fail("dig", "a"), 400), [])
        self.assertEqual(w.feed_audit(fail("dig", "a"), 800), [])

    def test_read_only_loop_and_idle(self) -> None:
        w = Watcher(stall_seconds=600)
        for t in range(0, 700, 30):
            w.feed_audit({"tool": "get_status", "ok": True}, t)
            w.feed_audit({"tool": "set_speed", "ok": True}, t)
        alerts = w.check_activity(700, started=0)
        self.assertEqual(len(alerts), 1)
        self.assertTrue(alerts[0].startswith("read_only_loop"))
        self.assertEqual(w.check_activity(710, started=0), [])
        w.feed_audit({"tool": "dig", "ok": True}, 720)
        self.assertEqual(w.check_activity(730, started=0), [])
        self.assertTrue(w.check_activity(2000, started=0)[0].startswith("player_idle"))

    def test_status_finds_lost_duplicants_stalls_and_long_pauses(self) -> None:
        w = Watcher(stall_seconds=600)
        self.assertEqual(w.feed_status(status(1, 0.1, 3), 0), [])
        self.assertEqual(w.feed_status(status(1, 0.2, 3), 120), [])
        self.assertEqual(w.feed_status(status(1, 0.2, 3), 600), [])
        self.assertTrue(w.feed_status(status(1, 0.2, 3), 721)[0].startswith("stalled"))
        lost = w.feed_status(status(1, 0.3, 2), 800)
        self.assertEqual(lost, ["duplicant_lost: 3 -> 2 at cycle 1.3"])
        self.assertEqual(w.feed_status(status(1, 0.3, 2, paused=True), 900), [])
        self.assertTrue(w.feed_status(status(1, 0.3, 2, paused=True), 1500)[0].startswith("paused"))
        self.assertTrue(w.feed_status(None, 1600, "connection_refused")[0].startswith("api_down"))
        self.assertEqual(w.feed_status(None, 1700, "connection_refused"), [])


class PauseTests(unittest.TestCase):
    def test_only_a_successful_speed_zero_is_a_pause(self) -> None:
        self.assertTrue(wants_pause({"tool": "set_speed", "ok": True, "args": {"speed": 0}}))
        self.assertFalse(wants_pause({"tool": "set_speed", "ok": True, "args": {"speed": 1}}))
        self.assertFalse(wants_pause({"tool": "set_speed", "ok": False, "args": {"speed": 0}}))
        self.assertFalse(wants_pause({"tool": "dig", "ok": True, "args": {"speed": 0}}))


class TailTests(unittest.TestCase):
    def test_tail_returns_only_new_complete_lines_and_survives_recreation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "a.jsonl"
            path.write_bytes(b"old\n")
            tail = Tail(path)
            self.assertEqual(tail.lines(), [])
            with path.open("ab") as handle:
                handle.write(b"one\ntw")
            self.assertEqual(tail.lines(), ["one"])
            with path.open("ab") as handle:
                handle.write(b"o\n")
            self.assertEqual(tail.lines(), ["two"])
            path.unlink()
            self.assertEqual(tail.lines(), [])
            path.write_bytes(b"new\n")
            self.assertEqual(tail.lines(), ["new"])


if __name__ == "__main__":
    unittest.main()
