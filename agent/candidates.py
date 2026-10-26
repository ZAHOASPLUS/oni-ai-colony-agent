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

import math
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
    BUILDING_COMPLETED_COUNT_CONTRACT_ID,
    GAME_PAUSED_CONTRACT_ID,
    RESEARCH_COMPLETE_CONTRACT_ID,
    RESEARCH_CURRENT_CONTRACT_ID,
    build_condition,
    read_contract_field,
)
from agent.state_quality import field_status, safe_get


CANDIDATE_SET_SCHEMA = "oni.action-candidates.v1"
WAIT_CANDIDATE_ID = "wait"
# Including the always-present "wait"; the full paused set (two research, two
# priority, one dig, three buildings, resume) never truncates.
MAX_CANDIDATES = 10
# Early-game chore groups offered for a one-step raise, in preference order.
PRIORITY_GROUPS = ("Dig", "Build")
MAX_PRIORITY_CANDIDATES = 2
MAX_DIG_RUN_CELLS = 4
# Tier-1 technologies (no prerequisites) in the order an oxygen-starved early
# colony needs them.  Entries in an observed research.complete set are skipped;
# when that set is unobserved (older Probe) every entry is offered, a finished
# tech is refused by the executor ("already researched") and that refusal
# reaches the model as outcome feedback.
RESEARCH_LADDER = ("ImprovedOxygen", "FarmingTech", "PowerRegulation", "BasicRefinement")
RESEARCH_SUMMARIES = {
    "ImprovedOxygen": "Air Systems: unlocks the Electrolyzer, which makes oxygen from water",
    "FarmingTech": "Basic Farming: unlocks the Algae Terrarium, Planter Box and Compost",
    "PowerRegulation": "Power Regulation: unlocks batteries and power transformers",
    "BasicRefinement": "Basic Refinement: unlocks the Rock Crusher and Kiln",
}
MAX_RESEARCH_CANDIDATES = 2
# Single-building blueprints offered while paused, in preference order: the
# two early oxygen buildings (1x2, on a floor; the Probe lists candidate cells
# only for a researched building with materials), then a Ladder.  Each entry
# is (building_def_id, candidate id prefix, label shown to the model).
BUILDING_CANDIDATES = (
    ("MineralDeoxidizer", "oxygen_diffuser", "Oxygen Diffuser (MineralDeoxidizer, algae to oxygen)"),
    ("AlgaeHabitat", "algae_terrarium", "Algae Terrarium (AlgaeHabitat, algae and water to oxygen)"),
    ("Ladder", "ladder", "Ladder"),
)
# The oxygen buildings carry the observed oxygen balance in their summary
# (see oxygen_balance): the first live colonies breathed about four times what
# they made and the model still waited, because the raw report counters in
# the colony summary carried no interpretation (run m1-0930b).
OXYGEN_BUILDING_DEF_IDS = frozenset({"MineralDeoxidizer", "AlgaeHabitat"})
MAX_SUMMARY_CHARS = 200
MAX_NAME_CHARS = 32
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]+")


def display_text(value: Any, limit: int = MAX_NAME_CHARS) -> str:
    """Bound user-controlled text (duplicant names) before it enters a prompt."""

    return _CONTROL_CHARS.sub(" ", str(value)).strip()[:limit]


def _candidate(candidate_id: str, summary: str, hint: Dict[str, Any], *, risk: str = "low") -> Dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "summary": summary[:MAX_SUMMARY_CHARS],
        "action": hint["action"],
        "risk": risk,
        "hint": hint,
        "contract_hash": canonical_hash(hint),
    }


def _observed(state: Mapping[str, Any], path: str, contract_id: str) -> Any:
    read = read_contract_field(state, path, contract_id, allowed_statuses=("observed",))
    return None if read.blockers else read.value


def oxygen_balance(state: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """The colony's oxygen picture from observed fields only.

    ``*_net_kg`` is the game's own report counter for oxygen made plus oxygen
    breathed (the latter is negative) over the closed previous report epoch
    and the open current one; ``oxygen_diffusers_built`` is the trustworthy
    completed-prefab count.  Whatever the Probe did not observe is None, and
    the assessment stays ``unobserved`` until at least one net value exists.
    """

    snapshot = dict(state) if isinstance(state, Mapping) else {}
    previous_net = _observed_net_oxygen(snapshot, "previous_closed")
    current_net = _observed_net_oxygen(snapshot, "current")
    count = read_contract_field(
        snapshot, "buildings.oxygen_diffuser", BUILDING_COMPLETED_COUNT_CONTRACT_ID, allowed_statuses=("observed",)
    )
    diffusers = None
    if not count.blockers and isinstance(count.value, int) and not isinstance(count.value, bool):
        diffusers = count.value
    reference = previous_net if previous_net is not None else current_net
    if reference is None:
        assessment = "unobserved"
    elif reference < 0:
        assessment = "deficit"
    elif reference > 0:
        assessment = "surplus"
    else:
        assessment = "balanced"
    return {
        "previous_cycle_net_kg": previous_net,
        "current_cycle_net_kg": current_net,
        "oxygen_diffusers_built": diffusers,
        "assessment": assessment,
    }


def _observed_net_oxygen(snapshot: Dict[str, Any], epoch: str) -> Optional[float]:
    total = 0.0
    for field in ("positive_kg", "negative_kg"):
        path = f"report_counters.{epoch}.oxygen_created.{field}"
        value = safe_get(snapshot, path)
        if field_status(snapshot, path) != "observed" or isinstance(value, bool):
            return None
        if not isinstance(value, (int, float)):
            return None
        try:
            number = float(value)
        except OverflowError:
            return None
        if not math.isfinite(number):
            return None
        total += number
        if not math.isfinite(total):
            return None
    return round(total, 1)


def _oxygen_note(balance: Mapping[str, Any]) -> str:
    """Short suffix for an oxygen-building summary; empty when nothing is observed."""

    parts: List[str] = []
    if balance.get("previous_cycle_net_kg") is not None:
        parts.append(f"oxygen net {balance['previous_cycle_net_kg']:+.1f} kg last cycle")
    elif balance.get("current_cycle_net_kg") is not None:
        parts.append(f"oxygen net {balance['current_cycle_net_kg']:+.1f} kg so far this cycle")
    built = balance.get("oxygen_diffusers_built")
    if built is not None:
        parts.append(f"{built} oxygen diffuser{'' if built == 1 else 's'} built")
    return "; " + ", ".join(parts) if parts else ""


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
    # A finished set that fails its contract (unsorted, oversized, wrong
    # source) is ignored rather than trusted: the fallback only offers more
    # candidates, each still checked by the executor.
    complete = _observed(state, "research.complete", RESEARCH_COMPLETE_CONTRACT_ID)
    finished = set(complete) if isinstance(complete, list) else set()
    result: List[Dict[str, Any]] = []
    for tech in RESEARCH_LADDER:
        if tech == current or tech in finished:
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


def _building_candidate(
    state: Dict[str, Any], building_def_id: str, prefix: str, label: str, note: str = ""
) -> Optional[Dict[str, Any]]:
    """One blueprint of ``building_def_id`` on the lowest catalogued empty cell (deterministic)."""

    catalog = safe_get(state, f"blueprints.catalog.{building_def_id}")
    if not isinstance(catalog, dict):
        return None
    cells = catalog.get("candidate_cells")
    materials = catalog.get("material_tags")
    if not isinstance(cells, list) or not isinstance(materials, list) or not materials:
        return None
    for cell in sorted(item for item in cells if isinstance(item, int) and not isinstance(item, bool)):
        try:
            hint = build_place_building_blueprint_hint(
                state, cells=[cell], material_tags=materials, building_def_id=building_def_id
            )
        except ValueError:
            continue
        details = safe_get(state, f"cells.by_id.cell_{cell}") or {}
        material = display_text(materials[0], 40)
        return _candidate(
            f"{prefix}_{cell}",
            f"Place one {label} blueprint at x={details.get('x')}, y={details.get('y')} built from {material}{note}",
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
        oxygen_note = _oxygen_note(oxygen_balance(snapshot))
        for building_def_id, prefix, label in BUILDING_CANDIDATES:
            note = oxygen_note if building_def_id in OXYGEN_BUILDING_DEF_IDS else ""
            building = _building_candidate(snapshot, building_def_id, prefix, label, note)
            if building is not None:
                items.append(building)
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
