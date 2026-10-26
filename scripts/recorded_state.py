"""Frozen recorded game state for tests that need every real field contract.

Several regressions build their fixtures from a real Probe snapshot, so every
field-quality contract the candidate builders check stays intact.  They used to
read the live ``ai-bridge/latest_state.json``, which made their result depend on
whatever the last run left behind: with no placed blueprint in it, seven of the
eight cases in test_cancel_stuck_blueprint were skipped (roadmap B9).

The default is now a snapshot frozen in the repository:
``fixtures/phase2-paused-blueprint-state.json.gz`` is the Probe snapshot of
2026-10-26T11:35:01+09:00 (run dsh-20261025-r01, seq 214) with the Bridge's
``bridge`` block attached.  The game is paused, MineralDeoxidizer blueprint
``id_14588`` has never been started, and the onset alerts ``hypothermia`` and
``urgent_toilet`` are open.  ``fixtures/phase2-action-policy.json`` is that run's
action policy.  Set ``ONI_TEST_LIVE_STATE=1`` to use the live bridge files
instead.
"""

from __future__ import annotations

import gzip
import json
import os
import pathlib
from typing import Any, Dict

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
STATE_FIXTURE = FIXTURES / "phase2-paused-blueprint-state.json.gz"
POLICY_FIXTURE = FIXTURES / "phase2-action-policy.json"
LIVE_BRIDGE = pathlib.Path.home() / "Documents" / "Klei" / "OxygenNotIncluded" / "ai-bridge"


def _use_live() -> bool:
    return os.environ.get("ONI_TEST_LIVE_STATE") == "1"


def _read_live(name: str) -> Dict[str, Any]:
    try:
        document = json.loads((LIVE_BRIDGE / name).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return document if isinstance(document, dict) else {}


def load_recorded_state() -> Dict[str, Any]:
    """A fresh copy of the recorded snapshot (or the live one when requested)."""

    if _use_live():
        document = _read_live("latest_state.json")
        return document if isinstance(document.get("quality"), dict) else {}
    with gzip.open(STATE_FIXTURE, "rb") as handle:
        return json.loads(handle.read().decode("utf-8"))


def load_recorded_policy() -> Dict[str, Any]:
    """The recorded action policy (or the live one when requested)."""

    if _use_live():
        return _read_live("action-policy.json")
    return json.loads(POLICY_FIXTURE.read_text(encoding="utf-8"))
