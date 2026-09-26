"""Regression tests for the evaluation-scoped strategy quality cache."""

from __future__ import annotations

import datetime as dt
import json
import unittest

from agent import strategy
from scripts.verify_bridge_reliability import make_state


class StrategyQualityCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = make_state(
            "quality-cache-test",
            1,
            timestamp="2026-09-13T01:00:00+00:00",
        )
        self.now = dt.datetime(2026, 9, 13, tzinfo=dt.timezone.utc)

    def test_cached_evaluation_matches_uncached_and_preserves_input(self) -> None:
        before = json.dumps(self.state, sort_keys=True, separators=(",", ":"))
        token = strategy._QUALITY_STATUS_CACHE.set(None)
        try:
            uncached = strategy._evaluate_strategy_impl(self.state, now=self.now)
        finally:
            strategy._QUALITY_STATUS_CACHE.reset(token)

        cached = strategy.evaluate_strategy(self.state, now=self.now)
        after = json.dumps(self.state, sort_keys=True, separators=(",", ":"))

        self.assertEqual(cached, uncached)
        self.assertEqual(before, after)

    def test_cache_reduces_repeated_quality_reads_within_one_evaluation(self) -> None:
        original = strategy.field_quality

        def count_reads(use_cache: bool) -> int:
            calls = 0

            def counted(state, path):
                nonlocal calls
                calls += 1
                return original(state, path)

            strategy.field_quality = counted
            try:
                if use_cache:
                    strategy.evaluate_strategy(self.state, now=self.now)
                else:
                    token = strategy._QUALITY_STATUS_CACHE.set(None)
                    try:
                        strategy._evaluate_strategy_impl(self.state, now=self.now)
                    finally:
                        strategy._QUALITY_STATUS_CACHE.reset(token)
            finally:
                strategy.field_quality = original
            return calls

        uncached_reads = count_reads(False)
        cached_reads = count_reads(True)
        self.assertLess(cached_reads, uncached_reads)


if __name__ == "__main__":
    unittest.main()
