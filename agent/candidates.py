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
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from agent.advanced_actions import (
    BUILDING_BLUEPRINT_ALLOWLIST,
    UTILITY_BLUEPRINT_ALLOWLIST,
    build_cancel_building_blueprint_hint,
    build_cancel_utility_blueprint_path_hint,
    build_duplicant_priority_hint,
    build_mark_dig_hint,
    build_place_building_blueprint_hint,
    build_place_utility_blueprint_path_hint,
    build_set_research_hint,
)
from agent.canonical_hash import canonical_hash
from agent.field_contracts import (
    ALERTS_CANONICAL_CONTRACT_ID,
    ONSET_ALERT_IDS,
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
# Five chore groups produce ten priority candidates, and the dig, building and
# resume candidates have to fit beside them.  At twelve the resume was pushed past
# the cut and the game could never be unpaused (2026-10-26 round 28).  The
# authoritative prompt size comes from the loop's own llm-health.json, which
# reported 1725 of 8000 tokens with twelve candidates, so the headroom is real.
MAX_AUTONOMOUS_CANDIDATES = 16
# Early-game chore groups offered for a one-step raise, in preference order.
# Chore groups the model may change.
#
# ``LifeSupport`` and ``Hauling`` lead because a built oxygen diffuser only
# produces oxygen once a duplicant supplies it: the three diffusers the loop had
# built all reported ``operational: false`` and the colony stayed at -192 kg of
# oxygen per cycle (2026-10-26 round 28).  Operating life-support buildings and
# supplying them are those two chore groups, and neither was reachable, so the
# loop could build but never make anything run.
#
# ``Farming`` follows: a colony that runs out of food dies faster than any other
# failure, and with only Dig/Build offered sixteen candidates consumed the whole
# budget so Farming was never reachable while food fell to 800 kcal (round 21).
#
# Dig and Build stay in the list: they were the original pair and the recorded
# fixtures only carry those two groups.  With five groups the priority candidates
# can fill the budget, which is why the resume slot is reserved explicitly below.
# The Mod reports these groups as typed chore priorities.
PRIORITY_GROUPS = ("LifeSupport", "Hauling", "Farming", "Dig", "Build")
MAX_PRIORITY_CANDIDATES = 2
MAX_DIG_RUN_CELLS = 4
# Tier-1 technologies (no prerequisites) in the order an oxygen-starved early
# colony needs them.  Entries in an observed research.complete set are skipped;
# when that set is unobserved (older Probe) every entry is offered, a finished
# tech is refused by the executor ("already researched") and that refusal
# reaches the model as outcome feedback.
RESEARCH_LADDER = ("ImprovedOxygen", "LiquidPiping", "FarmingTech", "PowerRegulation", "BasicRefinement")
RESEARCH_SUMMARIES = {
    "ImprovedOxygen": "Air Systems: unlocks the Electrolyzer, which makes oxygen from water",
    # Roadmap B15: the Electrolyzer takes water only through a pipe.
    "LiquidPiping": "Plumbing: unlocks the Liquid Pipe and Liquid Pump that feed an Electrolyzer water",
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
    ("Electrolyzer", "electrolyzer", "Electrolyzer (water to oxygen and hydrogen)"),
    ("FarmTile", "farm_tile", "Farm Tile (FarmTile, a duplicant plants a seed to grow food)"),
    ("MicrobeMusher", "microbe_musher", "Microbe Musher (MicrobeMusher, dirt and water to mush bars)"),
    # Roadmap B15: offered only once an Electrolyzer exists and while none of
    # their own kind does (see SUPPLY_BUILDINGS).
    ("ManualGenerator", "manual_generator", "Manual Generator (ManualGenerator, a duplicant runs it to make power)"),
    ("LiquidPump", "liquid_pump", "Liquid Pump (LiquidPump, pumps the liquid it stands in into a pipe)"),
    ("Ladder", "ladder", "Ladder"),
)
# Supply buildings exist for an Electrolyzer: one of each is enough for the
# first one (a Manual Generator makes 400 W; Electrolyzer 120 W plus Liquid
# Pump 240 W fit), and a second would only be one more idle building.
SUPPLY_BUILDINGS = ("ManualGenerator", "LiquidPump")
SUPPLY_LINK_LABELS = {
    "power": ("Wire", "power"),
    "liquid": ("Liquid Pipe", "water"),
}
MAX_SUPPLY_LINK_CANDIDATES = 2
# The oxygen buildings carry the observed oxygen balance in their summary
# (see oxygen_balance): the first live colonies breathed about four times what
# they made and the model still waited, because the raw report counters in
# the colony summary carried no interpretation (run m1-0930b).
#
# The Electrolyser is included: the visible world holds no algae, so the two
# algae-fed producers are dead ends, while it holds about 5.5 t of water
# (2026-10-26 rounds 33-38).
OXYGEN_BUILDING_DEF_IDS = frozenset({"MineralDeoxidizer", "AlgaeHabitat", "Electrolyzer"})
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


def _building_needs_consumable(building_def_id: str, consumable: str) -> bool:
    """True when a shortage of ``consumable`` blocks this building."""

    needed = CONSUMABLE_BY_BUILDING.get(building_def_id)
    return needed is not None and needed.lower() == str(consumable).strip().lower()


# The completed-prefab count each placeable building is reported under.
# ``buildings.oxygen_diffuser`` also counts the legacy OxygenDiffuser prefab.
BUILDING_COUNT_PATHS = {
    "MineralDeoxidizer": "buildings.oxygen_diffuser",
    "AlgaeHabitat": "buildings.algae_terrarium",
    "Electrolyzer": "buildings.electrolyzer",
    "FarmTile": "buildings.farm_tile",
    "MicrobeMusher": "buildings.microbe_musher",
    "ManualGenerator": "buildings.manual_generator",
    "LiquidPump": "buildings.liquid_pump",
}
# The Electrolyzer takes water only through a liquid pipe input and needs
# power (ElectrolyzerConfig: InputConduitType + ConduitConsumer,
# RequiresPowerInput); duplicants never carry water to it.  Without a pipe,
# a pump and a power source every Electrolyzer the controller places is dead:
# Phase 2 built six and none ever ran (docs/b2-blueprint-lifecycle-20261027.md).
# Roadmap B15 added all three; the links come from blueprints.supply_links.
ELECTROLYZER_SUPPLY_UTILITIES = ("Wire", "LiquidConduit")
ELECTROLYZER_SUPPLY_BUILDINGS = ("ManualGenerator", "LiquidPump")


def electrolyzer_supply_offered() -> bool:
    """True when the action catalog can supply an Electrolyzer with power and water."""

    return all(utility in UTILITY_BLUEPRINT_ALLOWLIST for utility in ELECTROLYZER_SUPPLY_UTILITIES) and all(
        building in BUILDING_BLUEPRINT_ALLOWLIST for building in ELECTROLYZER_SUPPLY_BUILDINGS
    )


MAX_IDLE_STATUS_ITEMS = 8


def building_status(state: Optional[Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Per placeable building: how many are built and how many are working.

    ``built`` is the observed completed-prefab count.  ``operational`` counts the
    life-support instances the game reports as operational, and is only given for
    buildings that reader tracks while its quality is observed.  ``idle_status``
    is the sorted union of the game's status item ids on the instances that do
    not operate (for example a missing power or pipe input), at most
    ``MAX_IDLE_STATUS_ITEMS``.  ``None`` means not observed, which is
    deliberately distinct from a measured zero.
    """

    snapshot = dict(state) if isinstance(state, Mapping) else {}
    result: Dict[str, Dict[str, Optional[int]]] = {}
    for building_def_id, path in BUILDING_COUNT_PATHS.items():
        count = read_contract_field(
            snapshot, path, BUILDING_COMPLETED_COUNT_CONTRACT_ID, allowed_statuses=("observed",)
        )
        built = None
        if not count.blockers and isinstance(count.value, int) and not isinstance(count.value, bool):
            built = count.value
        result[building_def_id] = {"built": built, "operational": None, "idle_status": None}
    if field_status(snapshot, "life_support_buildings") != "observed":
        return result
    tracked = safe_get(snapshot, "life_support_buildings.tracked_prefab_ids")
    instances = safe_get(snapshot, "life_support_buildings.instances")
    if not isinstance(tracked, Sequence) or isinstance(tracked, (str, bytes)):
        return result
    if not isinstance(instances, Sequence) or isinstance(instances, (str, bytes)):
        return result
    for building_def_id in result:
        if building_def_id not in tracked:
            continue
        working = 0
        reasons: set = set()
        for entry in instances:
            if isinstance(entry, Mapping) and entry.get("prefab_id") == building_def_id:
                if entry.get("operational") is True:
                    working += 1
                    continue
                ids = entry.get("status_item_ids")
                if isinstance(ids, Sequence) and not isinstance(ids, (str, bytes)):
                    reasons.update(display_text(item, 48) for item in ids if isinstance(item, str) and item)
        result[building_def_id]["operational"] = working
        result[building_def_id]["idle_status"] = sorted(reasons)[:MAX_IDLE_STATUS_ITEMS]
    return result


def _idle_built_buildings(status: Mapping[str, Mapping[str, Any]]) -> Dict[str, int]:
    """Buildings that exist but of which not a single one is working.

    Placing another of these adds a second idle building rather than output; the
    reason one is idle (missing consumable, power or pipe) has to be fixed first.
    """

    idle: Dict[str, int] = {}
    for building_def_id, item in status.items():
        built = item.get("built")
        operational = item.get("operational")
        if isinstance(built, int) and built > 0 and operational == 0:
            idle[building_def_id] = built
    return idle


def _pause_candidate(snapshot: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """The running-state pause, naming what pausing would actually enable.

    While the game runs, advanced actions are unbuildable, so the candidate set is
    just this and ``wait``.  Rule 2 tells the model to pause whenever a problem is
    open, so it paused, and then often resumed without changing anything: three
    ``set_paused`` commands and no priority or dig command in a whole session
    (2026-10-26 rounds 36-40).  Naming the pending changes lets the model pause
    with an intent it can carry out.
    """

    hint = {
        "action": "set_paused",
        "args": {"paused": True},
        "preconditions": [build_condition("game.paused", "eq", False, GAME_PAUSED_CONTRACT_ID)],
        "expected_effects": [build_condition("game.paused", "eq", True, GAME_PAUSED_CONTRACT_ID)],
    }
    summary = "Pause the game so priorities or digging can be changed"
    if snapshot is not None:
        enabled: List[str] = []
        try:
            runs = _dig_runs(dict(snapshot))
        except (KeyError, TypeError, ValueError):
            runs = []
        if runs:
            longest = max(len(run) for run in runs)
            enabled.append(f"mark up to {longest} cell(s) for digging")
        priorities = safe_get(snapshot, "duplicants.by_instance_id")
        if isinstance(priorities, Mapping) and priorities:
            enabled.append("change duplicant priorities")
        catalog = safe_get(snapshot, "blueprints.catalog")
        if isinstance(catalog, Mapping):
            starved = _oxygen_consumable_starved(snapshot) or ""
            placeable = [
                name
                for name, entry in catalog.items()
                if isinstance(entry, Mapping)
                and (entry.get("candidate_cells") or [])
                and not _building_needs_consumable(str(name), starved)
            ]
            if placeable:
                enabled.append("place " + ", ".join(sorted(placeable)[:4]))
        if enabled:
            summary = "Pause the game to " + "; ".join(enabled)
    return _candidate("pause_game", summary, hint)


# How long (in game cycles, which do not advance while paused) a placement must
# exist before it may count as stuck and be offered for cancelling.  Phase 2
# cancelled an Electrolyzer 66 s after placing it, while the six others took
# 1.5-4.5 minutes to be built once duplicants reached them
# (docs/b2-blueprint-lifecycle-20261027.md, roadmap B3).
STUCK_BLUEPRINT_MIN_AGE_CYCLES = 1.0


def _old_enough(key: Any, blueprint_ages: Optional[Mapping[str, float]]) -> bool:
    """True when the placement has existed long enough to be judged stuck.

    ``blueprint_ages`` maps a ``blueprints.by_instance_id`` key to the game
    cycles since the caller first saw it.  ``None`` means the caller does not
    track ages (offline tools), which keeps the old ungated behaviour; a
    tracked placement without an age is treated as new.
    """

    if blueprint_ages is None:
        return True
    age = blueprint_ages.get(str(key))
    return (
        isinstance(age, (int, float))
        and not isinstance(age, bool)
        and math.isfinite(age)
        and age >= STUCK_BLUEPRINT_MIN_AGE_CYCLES
    )


def _stuck_blueprint_count(
    snapshot: Mapping[str, Any], blueprint_ages: Optional[Mapping[str, float]] = None
) -> int:
    """Placements no duplicant has ever started and nothing has been delivered to.

    One such placement suppresses every building candidate (see
    ``_pending_blueprint_count``), which is the right guard while a duplicant is
    working on it.  A placement that has never been started is different: the
    colony's food and oxygen buildings were each placeable with 32 free cells, and
    a single dead MineralDeoxidizer kept them unofferable for many cycles while the
    model kept resuming to let duplicants "work on" it (2026-10-26 round 44).
    """

    instances = safe_get(snapshot, "blueprints.by_instance_id")
    if not isinstance(instances, Mapping):
        return 0
    stuck = 0
    for key, entry in instances.items():
        if not isinstance(entry, Mapping):
            continue
        if entry.get("in_progress") is True:
            continue
        if not _old_enough(key, blueprint_ages):
            continue
        prefab_id = entry.get("prefab_id")
        if not isinstance(prefab_id, str) or (
            prefab_id not in BUILDING_BLUEPRINT_ALLOWLIST and prefab_id not in UTILITY_BLUEPRINT_ALLOWLIST
        ):
            continue
        progress = entry.get("progress_percent")
        if not isinstance(progress, (int, float)) or isinstance(progress, bool) or progress > 0:
            continue
        worker = entry.get("worker_instance_id")
        if isinstance(worker, int) and not isinstance(worker, bool) and worker > 0:
            continue
        lineage = entry.get("material_lineage")
        delivered = 0.0
        if isinstance(lineage, Sequence) and not isinstance(lineage, (str, bytes)):
            for item in lineage:
                if isinstance(item, Mapping):
                    amount = item.get("delivered_mass_kg")
                    if isinstance(amount, (int, float)) and not isinstance(amount, bool):
                        delivered += float(amount)
        if delivered > 0:
            continue
        stuck += 1
    return stuck


def _non_operational_building_count(state: Mapping[str, Any]) -> int:
    """Completed buildings the game reports as not operational.

    They need a duplicant to supply or operate them, which only happens while the
    game runs, so they are queued work from the resume candidate's point of view
    (2026-10-26 round 28).
    """

    instances = safe_get(state, "functional_buildings.instances")
    if not isinstance(instances, Sequence) or isinstance(instances, (str, bytes)):
        return 0
    return sum(
        1
        for entry in instances
        if isinstance(entry, Mapping) and entry.get("operational") is False
    )


def _marked_dig_count(state: Mapping[str, Any]) -> int:
    """How many cells actually carry a dig designation right now.

    Offered dig candidates mean cells COULD be marked, not that any are.  The
    model conflated the two and resumed the game believing "pending dig orders
    are marked" while nothing was designated, so no digging ever happened
    (2026-10-26 rounds 36-41).
    """

    by_cell = safe_get(state, "designations.dig.by_cell")
    if not isinstance(by_cell, Mapping):
        return 0
    return sum(
        1
        for entry in by_cell.values()
        if isinstance(entry, Mapping) and entry.get("designation_present") is True
    )


def _queued_work_phrases(state: Mapping[str, Any]) -> List[str]:
    """What resuming would actually put the duplicants to work on."""

    phrases: List[str] = []
    placements = _pending_blueprint_count(state)
    if placements:
        phrases.append(f"{placements} unbuilt placement(s)")
    marked = _marked_dig_count(state)
    if marked:
        phrases.append(f"{marked} marked dig cell(s)")
    not_operational = _non_operational_building_count(state)
    if not_operational:
        phrases.append(f"{not_operational} built building(s) not operating")
    return phrases


def _resume_candidate(state: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    # The executor only resumes on an observed canonical alert set that is empty
    # or holds onset alerts only.  An onset alert (low_oxygen, urgent_toilet,
    # exhaustion) means a duplicant's current need is starting to be unmet; it
    # clears by itself once the game runs and the duplicant walks to the toilet
    # or into breathable air.  Treating those as hard blockers deadlocked the
    # autonomous loop — the game stayed paused, the duplicant could not move, the
    # alert never cleared, and every resume was refused (2026-10-25 rounds 9-11).
    # Genuine emergencies (suffocation, incapacitation, starvation, ...) still
    # refuse the resume, and the executor validates the same rule independently.
    alerts = _observed(state, "alerts", ALERTS_CANONICAL_CONTRACT_ID)
    if not isinstance(alerts, list):
        return None
    onset = [alert for alert in alerts if alert in ONSET_ALERT_IDS]
    if len(onset) != len(alerts):
        return None
    hint = {
        "action": "set_paused",
        "args": {"paused": False},
        "preconditions": [
            build_condition("game.paused", "eq", True, GAME_PAUSED_CONTRACT_ID),
            build_condition("alerts", "eq", list(alerts), ALERTS_CANONICAL_CONTRACT_ID),
        ],
        "expected_effects": [build_condition("game.paused", "eq", False, GAME_PAUSED_CONTRACT_ID)],
    }
    summary = "Resume the game so duplicants carry out the current orders"
    queued = _queued_work_phrases(state)
    if queued:
        summary = "Resume the game to work on " + ", ".join(queued)
    else:
        summary = (
            "Resume the game; nothing is queued yet, so also mark digs or place a "
            "building while paused first if the colony needs one"
        )
    if alerts:
        summary += " (only onset alerts are open: " + ",".join(alerts) + "; they clear once duplicants move)"
    return _candidate("resume_game", summary, hint)


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


def _autonomous_priority_candidates(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Offer genuine one-step changes in both directions for a stable target.

    The target order is independent of its current priority, so a later fresh
    snapshot can offer the inverse of a confirmed change. Every candidate is
    still built from current typed telemetry; history alone grants no action.
    """
    duplicants = safe_get(state, "duplicants.by_instance_id")
    if not isinstance(duplicants, dict):
        return []
    result: List[Dict[str, Any]] = []
    for group in PRIORITY_GROUPS:
        options = []
        for entry in duplicants.values():
            if not isinstance(entry, dict):
                continue
            instance_id = entry.get("instance_id")
            priorities = entry.get("chore_priorities")
            current = priorities.get(group) if isinstance(priorities, dict) else None
            if (
                type(instance_id) is int and instance_id > 0
                and type(current) is int and 0 <= current <= 5
            ):
                options.append((instance_id, current, entry))
        for instance_id, current, entry in sorted(options, key=lambda item: item[0]):
            pair = []
            for target, verb in ((current + 1, "Raise"), (current - 1, "Lower")):
                if not 0 <= target <= 5:
                    continue
                try:
                    hint = build_duplicant_priority_hint(
                        state, instance_id=instance_id, chore_group=group, priority=target
                    )
                except ValueError:
                    continue
                pair.append(_candidate(
                    f"priority_{instance_id}_{group}_{target}",
                    f"{verb} {display_text(entry.get('name'))}'s {group} priority from {current} to {target}",
                    hint,
                ))
            if pair:
                result.extend(pair)
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


def _dig_candidate(
    state: Dict[str, Any], exposure: Optional[Mapping[str, Any]] = None
) -> Optional[Dict[str, Any]]:
    # Longest horizontal run of safe candidates (ties: lowest row, then x).
    runs = sorted(_dig_runs(state), key=lambda run: -len(run))
    # Roadmap B6: while a temperature alert is open, digging into the coldest
    # (or hottest) material is what keeps duplicants exposed, so extreme runs
    # are withheld and the mildest run goes first.  A run whose temperature is
    # not observed is kept, after the measured ones: an older Probe publishes none.
    alert = (exposure or {}).get("dig_steering")
    if alert is not None:
        cold = alert == "hypothermia"
        steered = []
        for run in runs:
            temperatures = _dig_run_temperatures(state, run)
            if temperatures is None:
                steered.append((0, 0.0, run))
                continue
            extreme = min(temperatures) if cold else max(temperatures)
            if (extreme < DIG_COLD_LIMIT_C) if cold else (extreme > DIG_HOT_LIMIT_C):
                continue
            # Mildest first: warmest coldest-cell for cold, coolest hottest-cell for heat.
            steered.append((1, -extreme if cold else extreme, run))
        runs = [run for _, _, run in sorted(steered, key=lambda item: (item[0] == 0, item[1]))]
    for run in runs:
        try:
            hint = build_mark_dig_hint(state, cells=run)
        except ValueError:
            continue
        first = safe_get(state, f"designations.dig.by_cell.cell_{run[0]}") or {}
        element = display_text(first.get("element_id"), 40)
        temperatures = _dig_run_temperatures(state, run)
        climate = ""
        if temperatures is not None:
            climate = f" ({min(temperatures):g} to {max(temperatures):g} °C)"
        return _candidate(
            f"dig_{run[0]}_{len(run)}",
            f"Mark {len(run)} {element} cell(s) for digging starting at x={first.get('x')}, y={first.get('y')}{climate}",
            hint,
        )
    return None


# Roadmap B6: temperature observation and the mapping from a temperature alert
# to candidates.  The game raises ``hypothermia`` / ``hyperthermia`` per
# duplicant; the Probe now also publishes which duplicant, their body and cell
# temperatures, and every dig candidate cell's temperature (all diagnostic: no
# command binds them).  The limits are a first heuristic to check live.
TEMPERATURE_ALERT_STATES = {"hypothermia": "hypothermic", "hyperthermia": "hyperthermic"}
DIG_COLD_LIMIT_C = -20.0
DIG_HOT_LIMIT_C = 60.0
# The chore group that walks duplicants into unexplored, untempered material.
TEMPERATURE_RELIEF_GROUP = "Dig"
MAX_TEMPERATURE_RELIEF_CANDIDATES = 2


def _observed_number(
    state: Mapping[str, Any], path: str, statuses: Sequence[str] = ("observed",)
) -> Optional[float]:
    if field_status(state, path) not in statuses:
        return None
    value = safe_get(state, path)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _dig_run_temperatures(state: Mapping[str, Any], run: Sequence[int]) -> Optional[List[float]]:
    values = [_observed_number(state, f"designations.dig.by_cell.cell_{cell}.temperature_c") for cell in run]
    if not values or any(value is None for value in values):
        return None
    return [value for value in values if value is not None]


def temperature_exposure(state: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Which temperature alerts are open and which duplicant has each.

    ``None`` means not observed; an empty list means observed and empty.
    """

    snapshot = dict(state) if isinstance(state, Mapping) else {}
    alerts = _observed(snapshot, "alerts", ALERTS_CANONICAL_CONTRACT_ID)
    open_alerts = (
        sorted(alert for alert in alerts if alert in TEMPERATURE_ALERT_STATES)
        if isinstance(alerts, list) else None
    )
    exposure: Dict[str, Any] = {
        "alerts": open_alerts,
        # The world scan is a rolling window, so an estimated extreme is kept.
        "world_material_min_c": _observed_number(
            snapshot, "temperature.active_world_material_min_c", ("observed", "estimated")
        ),
        "world_material_max_c": _observed_number(
            snapshot, "temperature.active_world_material_max_c", ("observed", "estimated")
        ),
        "duplicants": None,
        "dig_steering": None,
    }
    by_instance = safe_get(snapshot, "duplicants.by_instance_id")
    if isinstance(by_instance, Mapping):
        exposed: List[Dict[str, Any]] = []
        observed_any = False
        for key, entry in sorted(by_instance.items()):
            if not isinstance(entry, Mapping):
                continue
            prefix = f"duplicants.by_instance_id.{key}"
            if field_status(snapshot, f"{prefix}.temperature_state") != "observed":
                continue
            state_value = entry.get("temperature_state")
            if state_value not in ("normal", "hypothermic", "hyperthermic"):
                continue
            observed_any = True
            if state_value == "normal":
                continue
            exposed.append({
                "instance_id": entry.get("instance_id"),
                "name": display_text(entry.get("name")),
                "temperature_state": state_value,
                "body_temperature_c": _observed_number(snapshot, f"{prefix}.body_temperature_c"),
                "cell_temperature_c": _observed_number(snapshot, f"{prefix}.cell_temperature_c"),
            })
        if observed_any:
            exposure["duplicants"] = exposed
    # Cold wins over heat when both are open: the cold-start map is where this
    # was observed (Phase 2: world material down to -86.5 °C).
    if open_alerts:
        exposure["dig_steering"] = "hypothermia" if "hypothermia" in open_alerts else "hyperthermia"
    return exposure


def _temperature_notes(exposure: Mapping[str, Any]) -> List[str]:
    notes: List[str] = []
    for alert in exposure.get("alerts") or ():
        duplicants = exposure.get("duplicants")
        if duplicants is None:
            notes.append(f"temperature_alert:{alert}:exposed_unobserved")
            continue
        wanted = TEMPERATURE_ALERT_STATES[alert]
        count = sum(1 for entry in duplicants if entry.get("temperature_state") == wanted)
        notes.append(f"temperature_alert:{alert}:exposed={count}")
    return notes


def _temperature_relief_candidates(
    state: Dict[str, Any], exposure: Mapping[str, Any]
) -> List[Dict[str, Any]]:
    """Lower the Dig priority of a duplicant who has an open temperature alert.

    The smallest move the action catalog has: less digging keeps the duplicant
    in the tempered base instead of at the cold (or hot) dig face, while the
    alert clears once they recover.  Built by the ordinary priority builder, so
    the command binds the current priority under its own contract.
    """

    result: List[Dict[str, Any]] = []
    open_alerts = exposure.get("alerts") or ()
    for entry in exposure.get("duplicants") or ():
        if len(result) >= MAX_TEMPERATURE_RELIEF_CANDIDATES:
            break
        alert = next(
            (name for name, wanted in TEMPERATURE_ALERT_STATES.items()
             if wanted == entry.get("temperature_state") and name in open_alerts),
            None,
        )
        instance_id = entry.get("instance_id")
        if alert is None or type(instance_id) is not int or instance_id <= 0:
            continue
        current = safe_get(
            state, f"duplicants.by_instance_id.id_{instance_id}.chore_priorities.{TEMPERATURE_RELIEF_GROUP}"
        )
        if type(current) is not int or not 0 < current <= 5:
            continue
        try:
            hint = build_duplicant_priority_hint(
                state, instance_id=instance_id, chore_group=TEMPERATURE_RELIEF_GROUP, priority=current - 1
            )
        except ValueError:
            continue
        detail = [entry["temperature_state"]]
        if entry.get("body_temperature_c") is not None:
            detail.append(f"body {entry['body_temperature_c']:g} °C")
        if entry.get("cell_temperature_c") is not None:
            detail.append(f"surroundings {entry['cell_temperature_c']:g} °C")
        result.append(_candidate(
            f"priority_{instance_id}_{TEMPERATURE_RELIEF_GROUP}_{current - 1}",
            f"Lower {entry['name']}'s {TEMPERATURE_RELIEF_GROUP} priority from {current} to {current - 1}"
            f" to keep them away from the dig face ({', '.join(detail)})",
            hint,
        ))
    return result


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


def _connected_blueprint_cells(
    snapshot: Mapping[str, Any],
    cells: Sequence[Any],
    width: int = 1,
    height: int = 1,
) -> List[int]:
    """Prefer blueprint cells a duplicant could actually walk to.

    A cell with ``nav_floor`` true but no standable neighbour is an isolated
    platform: the game accepts the placement, yet no duplicant can path to it, so
    no construction chore is ever created and the placement sits at progress 0
    forever while every duplicant idles.  That is exactly what the recorded
    MineralDeoxidizer at x=102,y=219 was: standable itself, with all four
    neighbours unstandable (2026-10-26 round 47).

    The check covers the whole footprint, because a multi-cell building is reached
    through any of its cells: a 2x2 Electrolyser kept stalling on a root whose own
    neighbours were fine while its upper cells were not (round 49).

    This is a one-step proxy, not a flood fill: it removes isolated platforms and
    keeps everything else, and it falls back to the unfiltered list whenever the
    navigation reading is unavailable, so a missing field never removes the only
    candidate.
    """

    usable = [cell for cell in cells if isinstance(cell, int) and not isinstance(cell, bool)]
    if not usable:
        return []
    by_id = safe_get(snapshot, "cells.by_id")
    if not isinstance(by_id, Mapping):
        return usable
    standable = set()
    coords = {}
    for cell in usable:
        entry = by_id.get(f"cell_{cell}")
        if not isinstance(entry, Mapping):
            continue
        value = entry.get("nav_floor")
        if value is None:
            continue
        x, y = entry.get("x"), entry.get("y")
        if not isinstance(x, int) or not isinstance(y, int):
            continue
        coords[cell] = (x, y)
        if value is True:
            standable.add(cell)
    if not standable or not coords:
        return usable
    standable_xy = {coords[cell] for cell in standable}
    step = ((1, 0), (-1, 0), (0, 1), (0, -1))

    # One-step adjacency is not enough: a cell can have a standable neighbour and
    # still sit in a region cut off from the colony by a vacuum gap.  The largest
    # connected component of standable cells is the region the duplicants actually
    # live in, so a candidate is only reachable if it touches that component
    # (2026-10-26 round 51).
    main: set = set()
    unseen = set(standable_xy)
    while unseen:
        seed = unseen.pop()
        component = {seed}
        frontier = [seed]
        while frontier:
            cx, cy = frontier.pop()
            for dx, dy in step:
                neighbour = (cx + dx, cy + dy)
                if neighbour in unseen:
                    unseen.discard(neighbour)
                    component.add(neighbour)
                    frontier.append(neighbour)
        if len(component) > len(main):
            main = component
    if not main:
        return usable

    def reachable(cell: int) -> bool:
        x, y = coords[cell]
        footprint = {(x + dx, y + dy) for dx in range(max(1, width)) for dy in range(max(1, height))}
        # The footprint cells are occupied by the building, so the duplicant has
        # to stand OUTSIDE it and beside it.  Requiring a standable footprint cell
        # accepted a 2x2 placement whose own cells were walkable but whose only
        # approach was blocked, and it never got a construction chore
        # (2026-10-26 round 50).
        for fx, fy in footprint:
            for dx, dy in step:
                neighbour = (fx + dx, fy + dy)
                if neighbour in footprint:
                    continue
                if neighbour in main:
                    return True
        return False

    connected = [cell for cell in usable if reachable(cell)]
    return connected or usable


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
    width = catalog.get("width")
    height = catalog.get("height")
    for cell in sorted(
        _connected_blueprint_cells(
            state,
            cells,
            width if isinstance(width, int) and not isinstance(width, bool) else 1,
            height if isinstance(height, int) and not isinstance(height, bool) else 1,
        )
    ):
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


def _supply_building_wanted(
    status: Mapping[str, Mapping[str, Any]], building_def_id: str, electrolyzers: Any
) -> bool:
    """A supply building is offered for a built Electrolyzer, and only the first one."""

    built = status.get(building_def_id, {}).get("built")
    return (
        isinstance(electrolyzers, int)
        and not isinstance(electrolyzers, bool)
        and electrolyzers > 0
        and built == 0
    )


def _supply_link_candidates(state: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Wire and Liquid Pipe paths the Probe computed between built ports.

    A wire or pipe connects a port only by occupying the port's cell, so the
    Probe finds the path from the completed buildings' own port cells
    (``blueprints.supply_links``); the placement contract then binds every one
    of those cells as the executor will check it.
    """

    if field_status(state, "blueprints.supply_links") != "observed":
        return []
    links = safe_get(state, "blueprints.supply_links")
    if not isinstance(links, Mapping):
        return []
    result: List[Dict[str, Any]] = []
    for key in sorted(links):
        link = links[key]
        if not isinstance(link, Mapping):
            continue
        kind = link.get("kind")
        utility = link.get("building_def_id")
        path_id = link.get("path_id")
        cells = link.get("cells")
        if (
            kind not in SUPPLY_LINK_LABELS
            or utility not in UTILITY_BLUEPRINT_ALLOWLIST
            or not isinstance(path_id, str)
            or key != f"id_{path_id}"
            or not isinstance(cells, list)
            or not cells
        ):
            continue
        materials = safe_get(state, f"blueprints.catalog.{utility}.material_tags")
        if not isinstance(materials, list) or not materials:
            continue
        try:
            hint = build_place_utility_blueprint_path_hint(
                dict(state), path_id=path_id, cells=cells, material_tags=materials, building_def_id=utility
            )
        except (ValueError, TypeError):
            continue
        label, purpose = SUPPLY_LINK_LABELS[kind]
        consumers = link.get("consumer_prefab_ids")
        names = ", ".join(display_text(name, 40) for name in consumers if isinstance(name, str)) if isinstance(
            consumers, list
        ) else ""
        source = display_text(link.get("source_prefab_id"), 40)
        result.append(
            _candidate(
                f"{kind}_link_{path_id}",
                f"Lay a {label} over {len(cells)} cells from the {source} to the {names or 'consumer'} "
                f"so it gets {purpose}",
                hint,
                risk="medium",
            )
        )
        if len(result) >= MAX_SUPPLY_LINK_CANDIDATES:
            break
    return result


def _cancel_utility_path_candidate(
    snapshot: Mapping[str, Any], blueprint_ages: Optional[Mapping[str, float]] = None
) -> Optional[Dict[str, Any]]:
    """Offer cancelling one whole utility path no duplicant has started.

    An unbuilt supply link blocks new placements like any other placement, and
    a path is cancelled only as the exact path it was placed as.
    """

    paths = safe_get(snapshot, "blueprints.utility_paths")
    instances = safe_get(snapshot, "blueprints.by_instance_id")
    if not isinstance(paths, Mapping) or not isinstance(instances, Mapping):
        return None
    for key in sorted(paths):
        path = paths[key]
        if not isinstance(path, Mapping) or path.get("active") is not True or path.get("fully_active") is not True:
            continue
        ids = path.get("blueprint_instance_ids")
        if not isinstance(ids, list) or not ids:
            continue
        started = False
        for instance_id in ids:
            entry = instances.get(f"id_{instance_id}")
            if (
                not isinstance(entry, Mapping)
                or entry.get("in_progress") is not False
                or not _old_enough(f"id_{instance_id}", blueprint_ages)
            ):
                started = True
                break
        if started:
            continue
        path_id = path.get("path_id")
        try:
            hint = build_cancel_utility_blueprint_path_hint(dict(snapshot), path_id=path_id)
        except (ValueError, TypeError):
            continue
        utility = display_text(path.get("building_def_id"), 40)
        return _candidate(
            f"cancel_blueprint_path_{path_id}",
            f"Cancel the unbuilt {utility} path {display_text(path_id, 64)} ({len(ids)} cells) no duplicant has started",
            hint,
        )
    return None


def _cancel_blueprint_candidate(
    snapshot: Mapping[str, Any], blueprint_ages: Optional[Mapping[str, float]] = None
) -> Optional[Dict[str, Any]]:
    """Offer cancelling one placed blueprint no duplicant has started.

    A placement whose material delivery never starts is dead weight: it blocks
    new placements (``_pending_blueprint_count``) and the colony then has no
    constructive candidate at all, so the loop only toggles the pause while food
    and oxygen fall (2026-10-25 round 20: one unbuildable MineralDeoxidizer held
    the colony at ``done`` 9 while food dropped 13705 -> 7692 kcal).  Offering the
    cancel lets the model clear the stuck plan and place one that will be built.

    Only a placement the game reports as not in progress is a candidate, so
    cancelling can never interrupt active construction.
    """

    instances = safe_get(snapshot, "blueprints.by_instance_id")
    if not isinstance(instances, Mapping):
        return None
    targets: List[tuple] = []
    for key, entry in instances.items():
        if not isinstance(entry, Mapping):
            continue
        if entry.get("in_progress") is True:
            continue
        if not _old_enough(key, blueprint_ages):
            continue
        prefab_id = entry.get("prefab_id")
        cell = entry.get("cell")
        if not isinstance(prefab_id, str) or prefab_id not in BUILDING_BLUEPRINT_ALLOWLIST:
            continue
        if isinstance(cell, bool) or not isinstance(cell, int) or cell <= 0:
            continue
        targets.append((cell, prefab_id, entry))
    # The summary must describe the placement being cancelled, not whichever
    # entry the scan above happened to visit last.
    for cell, prefab_id, target_entry in sorted(targets, key=lambda target: target[0]):
        try:
            hint = build_cancel_building_blueprint_hint(snapshot, cells=[cell])
        except ValueError:
            continue
        return _candidate(
            f"cancel_blueprint_{cell}",
            _cancel_blueprint_summary(target_entry, prefab_id, cell),
            hint,
            risk="medium",
        )
    return None


def _cancel_blueprint_summary(entry: Mapping[str, Any], prefab_id: str, cell: int) -> str:
    """Name why the placement is stuck and what keeping it costs.

    The model read "unbuilt placements are pending" as work in progress and kept
    resuming to let duplicants do it, while the placement had never been started
    (``worker_instance_id`` 0, ``progress_percent`` 0) and its delivery had never
    arrived.  Because one pending placement suppresses every building candidate,
    keeping it also kept the colony's only food and oxygen buildings unofferable
    (2026-10-26 round 43).
    """

    parts = [f"Cancel the unbuilt {prefab_id} blueprint at cell {cell}"]
    details: List[str] = []
    progress = entry.get("progress_percent")
    if isinstance(progress, (int, float)) and not isinstance(progress, bool) and progress <= 0:
        details.append("no duplicant has started it")
    lineage = entry.get("material_lineage")
    if isinstance(lineage, Sequence) and not isinstance(lineage, (str, bytes)):
        for item in lineage:
            if not isinstance(item, Mapping):
                continue
            delivered = item.get("delivered_mass_kg")
            required = item.get("required_mass_kg")
            tag = item.get("tag")
            if isinstance(delivered, (int, float)) and isinstance(required, (int, float)):
                details.append(f"{delivered:g} of {required:g} kg {tag} delivered")
            break
    if details:
        parts.append(" (" + "; ".join(details) + ")")
    parts.append(" so a plan that will actually be constructed can be placed instead")
    return "".join(parts)


# Which consumable each oxygen producer actually needs, so a shortage suppresses
# only the producers it blocks.  A single shared set suppressed the Electrolyser
# too: the world has no algae, but it does hold about 5.5 t of water, and the
# Electrolyser is the only route that works here (2026-10-26 round 40).
CONSUMABLE_BY_BUILDING = {
    "MineralDeoxidizer": "algae",
    "AlgaeHabitat": "algae",
    "Electrolyzer": "water",
}


def _oxygen_consumable_starved(snapshot: Mapping[str, Any]) -> Optional[str]:
    """Name the consumable that blocks an already-placed oxygen building.

    A building whose ``ManualDeliveryKG`` reports a positive capacity while the
    world's own inventory holds none of the requested item can never be supplied,
    so placing another one only adds another dead building.  The model kept
    pursuing exactly that: it switched research to Basic Farming to unlock the
    Algae Terrarium, which consumes the same missing algae (2026-10-26 round 34).

    The check needs ``inventory_valid`` so an unreadable inventory is never
    mistaken for an empty world, and it only fires for oxygen buildings.
    """

    instances = safe_get(snapshot, "life_support_buildings.instances")
    if not isinstance(instances, Sequence) or isinstance(instances, (str, bytes)):
        return None
    for entry in instances:
        if not isinstance(entry, Mapping):
            continue
        if entry.get("prefab_id") not in OXYGEN_BUILDING_DEF_IDS:
            continue
        if entry.get("inventory_valid") is not True:
            continue
        available = entry.get("requested_item_available_kg")
        capacity = entry.get("delivery_capacity_kg")
        if not isinstance(capacity, (int, float)) or isinstance(capacity, bool) or capacity <= 0:
            continue
        if not isinstance(available, (int, float)) or isinstance(available, bool):
            continue
        if available > 0:
            continue
        item = entry.get("requested_item_tag")
        return display_text(item, 40) if isinstance(item, str) and item else "its consumable"
    return None


def _pending_blueprint_count(snapshot: Mapping[str, Any]) -> int:
    """Count placed blueprints that are not yet constructed.

    ``blueprints.by_instance_id`` lists every placement the game reports; a
    placement whose ``in_progress`` flag is false is queued work that no
    duplicant has started.  Used to stop the model from adding still more
    blueprints while unbuilt ones are already queued.
    """

    instances = safe_get(snapshot, "blueprints.by_instance_id")
    if not isinstance(instances, Mapping):
        return 0
    count = 0
    for entry in instances.values():
        if not isinstance(entry, Mapping):
            continue
        if entry.get("in_progress") is True:
            continue
        if entry.get("prefab_id"):
            count += 1
    return count


def _active_construction_count(snapshot: Mapping[str, Any]) -> int:
    """Count blueprints a duplicant is constructing right now.

    Only active construction justifies withholding the pause.  A placement that
    merely exists is not active work: after a build completes the game keeps
    reporting the placement with ``in_progress`` false and progress 0, so
    treating any placement as "keep running" suppressed the pause forever and
    left ``wait`` as the only candidate (2026-10-25 round 14: one completed
    diffuser plus one lingering placement stalled the loop for minutes).
    """

    instances = safe_get(snapshot, "blueprints.by_instance_id")
    if not isinstance(instances, Mapping):
        return 0
    return sum(
        1
        for entry in instances.values()
        if isinstance(entry, Mapping) and entry.get("in_progress") is True
    )


def generate_candidates(
    state: Optional[Mapping[str, Any]],
    *,
    allowed_actions: Iterable[str] = (),
    autonomous: bool = False,
    blueprint_ages: Optional[Mapping[str, float]] = None,
) -> Dict[str, Any]:
    """Return a bounded candidate set; autonomous mode includes priority inverses.

    ``blueprint_ages`` (game cycles since each placement was first seen) gates
    the stuck-placement logic; see ``STUCK_BLUEPRINT_MIN_AGE_CYCLES``.
    """

    if type(autonomous) is not bool:
        raise ValueError("autonomous must be a boolean")
    allowed = set(allowed_actions)
    items: List[Dict[str, Any]] = []
    blockers: List[str] = []
    # Informational: explains an absent candidate without stopping proposal.
    notes: List[str] = []
    snapshot = dict(state) if isinstance(state, Mapping) else {}
    paused = _observed(snapshot, "game.paused", GAME_PAUSED_CONTRACT_ID)
    exposure = temperature_exposure(snapshot)
    notes.extend(_temperature_notes(exposure))
    if not isinstance(paused, bool):
        blockers.append("game_paused_unobserved")
    elif not paused:
        # Blueprints are only constructed while the game runs, so the pause is
        # withheld while a duplicant is actively constructing one; interrupting
        # that work wasted whole game-minutes and no diffuser was ever built
        # (2026-10-25 rounds 9-12).  A placement that merely exists is not active
        # work, so the pause stays available then; otherwise the loop loses every
        # candidate but ``wait`` and stalls (round 14).
        if _active_construction_count(snapshot) == 0:
            items.append(_pause_candidate(snapshot))
        # Research needs no pause: the executor only binds research.current.
        items.extend(_research_candidates(snapshot))
    else:
        # Advanced actions require a paused game; resume is offered last so a
        # model that wants duplicants to act can still return to play.
        items.extend(_research_candidates(snapshot))
        # Supply links are inserted here once known (see below): the priority
        # candidates alone can fill the budget, and a link is what makes an
        # already-built building work.
        # Roadmap B6: the relief for an open temperature alert goes ahead of
        # the generic priority changes, which can fill the budget on their own.
        relief = _temperature_relief_candidates(snapshot, exposure)
        items.extend(relief)
        link_slot = len(items)
        relief_ids = {item["candidate_id"] for item in relief}
        items.extend(
            item
            for item in (
                _autonomous_priority_candidates(snapshot) if autonomous else _priority_candidates(snapshot)
            )
            if item["candidate_id"] not in relief_ids
        )
        dig = _dig_candidate(snapshot, exposure)
        if dig is not None:
            items.append(dig)
        oxygen_note = _oxygen_note(oxygen_balance(snapshot))
        # A blueprint that is already placed but not yet constructed makes more
        # blueprint placement pointless: nothing is built while the game is
        # paused, and the colony cannot run without resuming.  The model
        # otherwise kept placing Oxygen Diffusers instead of resuming, so the
        # game stayed frozen and no diffuser was ever built (2026-10-25 round 10:
        # game_time_cycles unchanged for 90 s with 11 pending blueprints).
        pending_builds = _pending_blueprint_count(snapshot)
        # Do not offer another oxygen building while an already-placed one is
        # provably unsupplyable: the world holds none of its consumable, so a
        # second one cannot work either.  This is reported as a note rather than
        # a blocker: it explains an absent candidate, it is not a reason to stop
        # proposing (round 34).
        starved = _oxygen_consumable_starved(snapshot)
        if starved is not None:
            notes.append(f"oxygen_consumable_unavailable:{starved}")
        # A placement a duplicant is actually working on suppresses new placements,
        # so plans do not pile up.  One that has never been started does not: it
        # blocked the colony's only food and oxygen buildings for many cycles
        # (2026-10-26 round 44).  The cancel stays offered so the model can still
        # clear it.
        stuck = _stuck_blueprint_count(snapshot, blueprint_ages)
        if stuck:
            notes.append(f"stuck_placement_ignored:{stuck}")
        if pending_builds == 0 or stuck == pending_builds:
            # The cancel goes first: with several buildings now offered, a stuck
            # placement's cancel was crowded out of the candidate budget and the
            # model lost the only way to clear it (2026-10-26 round 44).
            if stuck:
                for cancel in (
                    _cancel_blueprint_candidate(snapshot, blueprint_ages),
                    _cancel_utility_path_candidate(snapshot, blueprint_ages),
                ):
                    if cancel is not None:
                        items.append(cancel)
            # A building that exists but of which none works is not offered again:
            # Phase 2 built six identical Electrolyzers that could never run, because
            # the controller could not see the first one (B14).  The Electrolyzer is
            # withheld outright while no liquid pipe can be laid for it.
            idle = _idle_built_buildings(building_status(snapshot))
            for building_def_id, count in sorted(idle.items()):
                notes.append(f"built_not_operational:{building_def_id}:{count}")
            supply = electrolyzer_supply_offered()
            if not supply:
                notes.append("electrolyzer_needs_power_and_liquid_pipe")
            # Roadmap B15: wire and pipe links between built ports, computed by
            # the Probe.  An idle Electrolyzer with no link to offer is noted,
            # so the model knows a supply building (or a route) is missing.
            links = _supply_link_candidates(snapshot)
            items[link_slot:link_slot] = links
            status = building_status(snapshot)
            electrolyzers = status.get("Electrolyzer", {}).get("built")
            if "Electrolyzer" in idle and not links:
                notes.append("electrolyzer_idle_no_supply_link")
            for building_def_id, prefix, label in BUILDING_CANDIDATES:
                if starved is not None and _building_needs_consumable(building_def_id, starved):
                    continue
                if building_def_id in idle:
                    continue
                if building_def_id == "Electrolyzer" and not supply:
                    continue
                if building_def_id in SUPPLY_BUILDINGS and not _supply_building_wanted(
                    status, building_def_id, electrolyzers
                ):
                    continue
                note = oxygen_note if building_def_id in OXYGEN_BUILDING_DEF_IDS else ""
                building = _building_candidate(snapshot, building_def_id, prefix, label, note)
                if building is not None:
                    items.append(building)
        else:
            # Unbuilt placements are queued, so no new placement is offered; the
            # cancel is what lets the model clear one that will never be built.
            for cancel in (
                _cancel_blueprint_candidate(snapshot, blueprint_ages),
                _cancel_utility_path_candidate(snapshot, blueprint_ages),
            ):
                if cancel is not None:
                    items.append(cancel)
        resume = _resume_candidate(snapshot)
        if resume is not None:
            items.append(resume)
    maximum = MAX_AUTONOMOUS_CANDIDATES if autonomous else MAX_CANDIDATES
    # ``resume_game`` is what makes every other paused-only change take effect, so
    # it must survive the budget.  It is appended after the priority, dig and
    # building candidates, and those alone can fill the list: with five chore
    # groups the nine priority candidates plus a dig and three placements pushed
    # the resume past the cut, so the model never saw it and the game stayed
    # paused forever (2026-10-26 round 28).  Reserve its slot before truncating.
    resume_items = [item for item in items if item.get("candidate_id") == "resume_game"]
    if resume_items:
        items = [
            item for item in items if item.get("candidate_id") != "resume_game"
        ][: max(0, maximum - 1 - len(resume_items))] + resume_items
    items = [item for item in items if item["action"] in allowed][: maximum - 1]
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
        "notes": notes,
        "candidate_set_hash": canonical_hash(
            [{"candidate_id": item["candidate_id"], "contract_hash": item["contract_hash"]} for item in items]
        ),
    }
