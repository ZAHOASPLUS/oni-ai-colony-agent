"""Deterministic, catalogued action candidates for the model loop.

Code review 2026-09-26, M1: the model must choose between a few complete,
locally built command contracts instead of producing actions itself.  Every
candidate here is built by the same catalogued builders the rules and the
executor contracts use (agent.advanced_actions, agent.field_contracts), from
trusted observed fields only; a builder that refuses the current state simply
yields no candidate.  The set is stable for a stable game state (no sequence
numbers or timestamps enter a contract), so a model answer that arrives a few
snapshots later can still be matched to the identical contract.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Mapping, Optional

from agent.advanced_actions import (
    build_duplicant_priority_hint,
    build_mark_dig_hint,
    build_place_building_blueprint_hint,
    build_set_research_hint,
)
from agent.canonical_hash import canonical_hash
from agent.field_contracts import (
    ALERTS_CANONICAL_CONTRACT_ID,
    GAME_PAUSED_CONTRACT_ID,
    RESEARCH_CURRENT_CONTRACT_ID,
    build_condition,
    read_contract_field,
)
from agent.state_quality import safe_get


CANDIDATE_SET_SCHEMA = "oni.action-candidates.v1"
WAIT_CANDIDATE_ID = "wait"
MAX_CANDIDATES = 8  # including the always-present "wait"; the paused set never truncates
# Early-game chore groups offered for a one-step raise, in preference order.
PRIORITY_GROUPS = ("Dig", "Build")
MAX_PRIORITY_CANDIDATES = 2
MAX_DIG_RUN_CELLS = 4
# Tier-1 technologies (no prerequisites) in the order an oxygen-starved early
# colony needs them.  The Probe does not yet report research.complete, so a
# finished tech is refused by the executor ("already researched") and that
# refusal reaches the model as outcome feedback; the next entry is offered too.
RESEARCH_LADDER = ("ImprovedOxygen", "FarmingTech", "PowerRegulation", "BasicRefinement")
RESEARCH_SUMMARIES = {
    "ImprovedOxygen": "Air Systems: unlocks the Electrolyzer, which makes oxygen from water",
    "FarmingTech": "Basic Farming: unlocks the Algae Terrarium, Planter Box and Compost",
    "PowerRegulation": "Power Regulation: unlocks batteries and power transformers",
    "BasicRefinement": "Basic Refinement: unlocks the Rock Crusher and Kiln",
}
MAX_RESEARCH_CANDIDATES = 2
# The only building the Mod's blueprint allowlist accepts today.
LADDER_BUILDING_ID = "Ladder"
MAX_NAME_CHARS = 32
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]+")


def display_text(value: Any, limit: int = MAX_NAME_CHARS) -> str:
    """Bound user-controlled text (duplicant names) before it enters a prompt."""

    return _CONTROL_CHARS.sub(" ", str(value)).strip()[:limit]


def _candidate(candidate_id: str, summary: str, hint: Dict[str, Any], *, risk: str = "low") -> Dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "summary": summary[:160],
        "action": hint["action"],
        "risk": risk,
        "hint": hint,
        "contract_hash": canonical_hash(hint),
    }


def _observed(state: Mapping[str, Any], path: str, contract_id: str) -> Any:
    read = read_contract_field(state, path, contract_id, allowed_statuses=("observed",))
    return None if read.blockers else read.value


def _pause_candidate() -> Dict[str, Any]:
    hint = {
        "action": "set_paused",
        "args": {"paused": True},
        "preconditions": [build_condition("game.paused", "eq", False, GAME_PAUSED_CONTRACT_ID)],
        "expected_effects": [build_condition("game.paused", "eq", True, GAME_PAUSED_CONTRACT_ID)],
    }
    return _candidate("pause_game", "Pause the game so priorities or digging can be changed", hint)


def _resume_candidate(state: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    # The executor only resumes on a complete observed empty canonical alert
    # set; offering it otherwise would only produce a rejected command.
    if _observed(state, "alerts", ALERTS_CANONICAL_CONTRACT_ID) != []:
        return None
    hint = {
        "action": "set_paused",
        "args": {"paused": False},
        "preconditions": [
            build_condition("game.paused", "eq", True, GAME_PAUSED_CONTRACT_ID),
            build_condition("alerts", "eq", [], ALERTS_CANONICAL_CONTRACT_ID),
        ],
        "expected_effects": [build_condition("game.paused", "eq", False, GAME_PAUSED_CONTRACT_ID)],
    }
    return _candidate("resume_game", "Resume the game so duplicants carry out the current orders", hint)


def _priority_candidates(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    duplicants = safe_get(state, "duplicants.by_instance_id")
    if not isinstance(duplicants, dict):
        return []
    result: List[Dict[str, Any]] = []
    for group in PRIORITY_GROUPS:
        # Offer the least-prioritised duplicant for this group (ties: lowest
        # instance id); the builder re-reads every value under its contract.
        options = []
        for entry in duplicants.values():
            if not isinstance(entry, dict):
                continue
            instance_id = entry.get("instance_id")
            priorities = entry.get("chore_priorities")
            current = priorities.get(group) if isinstance(priorities, dict) else None
            if (
                isinstance(instance_id, int) and not isinstance(instance_id, bool) and instance_id > 0
                and isinstance(current, int) and not isinstance(current, bool) and 0 <= current < 5
            ):
                options.append((current, instance_id, entry))
        for current, instance_id, entry in sorted(options, key=lambda item: (item[0], item[1])):
            try:
                hint = build_duplicant_priority_hint(
                    state, instance_id=instance_id, chore_group=group, priority=current + 1
                )
            except ValueError:
                continue
            name = display_text(entry.get("name"))
            result.append(
                _candidate(
                    f"priority_{instance_id}_{group}_{current + 1}",
                    f"Raise {name}'s {group} priority from {current} to {current + 1}",
                    hint,
                )
            )
            break
        if len(result) >= MAX_PRIORITY_CANDIDATES:
            break
    return result


def _dig_runs(state: Dict[str, Any]) -> List[List[int]]:
    cells = safe_get(state, "designations.dig.candidate_cells")
    by_cell = safe_get(state, "designations.dig.by_cell")
    if not isinstance(cells, list) or not isinstance(by_cell, dict):
        return []
    positions = {}
    for cell in cells:
        details = by_cell.get(f"cell_{cell}") if isinstance(cell, int) and not isinstance(cell, bool) else None
        if isinstance(details, dict) and isinstance(details.get("x"), int) and isinstance(details.get("y"), int):
            positions[cell] = (details["y"], details["x"])
    runs: List[List[int]] = []
    for cell in sorted(positions, key=lambda item: positions[item]):
        y, x = positions[cell]
        last = runs[-1] if runs else None
        if last and len(last) < MAX_DIG_RUN_CELLS and positions[last[-1]] == (y, x - 1):
            last.append(cell)
        else:
            runs.append([cell])
    return runs


def _dig_candidate(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    # Longest horizontal run of safe candidates (ties: lowest row, then x).
    runs = sorted(_dig_runs(state), key=lambda run: -len(run))
    for run in runs:
        try:
            hint = build_mark_dig_hint(state, cells=run)
        except ValueError:
            continue
        first = safe_get(state, f"designations.dig.by_cell.cell_{run[0]}") or {}
        element = display_text(first.get("element_id"), 40)
        return _candidate(
            f"dig_{run[0]}_{len(run)}",
            f"Mark {len(run)} {element} cell(s) for digging starting at x={first.get('x')}, y={first.get('y')}",
            hint,
        )
    return None


def _research_candidates(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    current = _observed(state, "research.current", RESEARCH_CURRENT_CONTRACT_ID)
    if not isinstance(current, str):
        return []
    result: List[Dict[str, Any]] = []
    for tech in RESEARCH_LADDER:
        if tech == current:
            continue
        try:
            hint = build_set_research_hint(state, tech=tech)
        except ValueError:
            continue
        prefix = "Switch research" if current else "Start research"
        result.append(_candidate(f"research_{tech}", f"{prefix} to {RESEARCH_SUMMARIES.get(tech, tech)}", hint))
        if len(result) >= MAX_RESEARCH_CANDIDATES:
            break
    return result


def _ladder_candidate(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """One Ladder blueprint on the lowest catalogued empty cell (deterministic)."""

    catalog = safe_get(state, f"blueprints.catalog.{LADDER_BUILDING_ID}")
    if not isinstance(catalog, dict):
        return None
    cells = catalog.get("candidate_cells")
    materials = catalog.get("material_tags")
    if not isinstance(cells, list) or not isinstance(materials, list) or not materials:
        return None
    for cell in sorted(item for item in cells if isinstance(item, int) and not isinstance(item, bool)):
        try:
            hint = build_place_building_blueprint_hint(
                state, cells=[cell], material_tags=materials, building_def_id=LADDER_BUILDING_ID
            )
        except ValueError:
            continue
        details = safe_get(state, f"cells.by_id.cell_{cell}") or {}
        material = display_text(materials[0], 40)
        return _candidate(
            f"ladder_{cell}",
            f"Place one Ladder blueprint at x={details.get('x')}, y={details.get('y')} built from {material}",
            hint,
            risk="medium",
        )
    return None


def generate_candidates(
    state: Optional[Mapping[str, Any]],
    *,
    allowed_actions: Iterable[str] = (),
) -> Dict[str, Any]:
    """Return up to MAX_CANDIDATES candidates (the last one is always "wait")."""

    allowed = set(allowed_actions)
    items: List[Dict[str, Any]] = []
    blockers: List[str] = []
    snapshot = dict(state) if isinstance(state, Mapping) else {}
    paused = _observed(snapshot, "game.paused", GAME_PAUSED_CONTRACT_ID)
    if not isinstance(paused, bool):
        blockers.append("game_paused_unobserved")
    elif not paused:
        items.append(_pause_candidate())
        # Research needs no pause: the executor only binds research.current.
        items.extend(_research_candidates(snapshot))
    else:
        # Advanced actions require a paused game; resume is offered last so a
        # model that wants duplicants to act can still return to play.
        items.extend(_research_candidates(snapshot))
        items.extend(_priority_candidates(snapshot))
        dig = _dig_candidate(snapshot)
        if dig is not None:
            items.append(dig)
        ladder = _ladder_candidate(snapshot)
        if ladder is not None:
            items.append(ladder)
        resume = _resume_candidate(snapshot)
        if resume is not None:
            items.append(resume)
    items = [item for item in items if item["action"] in allowed][: MAX_CANDIDATES - 1]
    items.append(
        {
            "candidate_id": WAIT_CANDIDATE_ID,
            "summary": "Do nothing now and look again at the next change",
            "action": None,
            "risk": "low",
            "hint": None,
            "contract_hash": None,
        }
    )
    return {
        "schema": CANDIDATE_SET_SCHEMA,
        "items": items,
        "blockers": blockers,
        "candidate_set_hash": canonical_hash(
            [{"candidate_id": item["candidate_id"], "contract_hash": item["contract_hash"]} for item in items]
        ),
    }
