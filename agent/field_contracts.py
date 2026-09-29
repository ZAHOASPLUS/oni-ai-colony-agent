from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

from agent.state_quality import (
    ALERTS_ACTIVE_WORLD_SCOPE,
    ALERTS_ACTIVE_WORLD_SOURCE,
    ALERTS_ALL_WORLDS_SCOPE,
    ALERTS_ALL_WORLDS_SOURCE,
    field_quality,
    parse_snapshot_timestamp,
    quality_contract_blockers,
    safe_get,
)


GAME_PAUSED_CONTRACT_ID = "oni.qc.game.paused.v1"
GAME_BUILD_MODE_CONTRACT_ID = "oni.qc.game.build_mode.v1"
RESEARCH_CURRENT_CONTRACT_ID = "oni.qc.research.current.v1"
RESEARCH_COMPLETE_CONTRACT_ID = "oni.qc.research.complete.v1"
RESEARCH_COMPLETE_SOURCE = "Db.Techs+Research.Get+TechInstance.IsComplete"
REGISTERED_FOOD_CONTRACT_ID = "oni.qc.resources.registered_food.v1"
ALERTS_CANONICAL_CONTRACT_ID = "oni.qc.alerts.canonical.v1"
CANONICAL_ALERT_IDS = (
    "bionic_low_battery",
    "bionic_offline",
    "exhaustion",
    "high_stress",
    "hyperthermia",
    "hypothermia",
    "incapacitated",
    "low_oxygen",
    "major_sickness",
    "sickness",
    "starvation",
    "suffocation",
    "urgent_toilet",
    "wounded",
)
DUPLICANTS_TOTAL_CONTRACT_ID = "oni.qc.duplicants.active_world_count.v1"
DUPLICANTS_IDLE_CONTRACT_ID = "oni.qc.duplicants.idle.v1"
DUPLICANTS_STRESSED_CONTRACT_ID = "oni.qc.duplicants.stressed.v1"
DUPLICANTS_TOTAL_SOURCE = "Components.LiveMinionIdentities.active_world"
BUILDING_COMPLETED_COUNT_CONTRACT_ID = "oni.qc.buildings.completed_prefab_count.v1"
FUNCTIONAL_BUILDINGS_CONTRACT_ID = "oni.qc.buildings.functional_instances.v1"
FUNCTIONAL_BUILDINGS_SOURCE = (
    "Components.BuildingCompletes.GetWorldItems(activeWorldId)+Operational.IsOperational"
)
FUNCTIONAL_BUILDINGS_SCHEMA = "oni.functional_buildings.v1"
# Current installed ONI uses MineralDeoxidizer for the oxygen generator. Keep
# the native id in telemetry while treating it as an explicit alias in rules.
FUNCTIONAL_BUILDING_PREFAB_IDS = (
    "OxygenDiffuser",
    "MineralDeoxidizer",
    "Outhouse",
    "ManualGenerator",
    # Accepted ahead of the Probe reader so a producer that adds flush
    # toilets does not invalidate the whole payload; sanitation counts both.
    "FlushToilet",
)
FUNCTIONAL_BUILDING_GEOMETRY_CONTRACT_ID = "oni.qc.buildings.functional_geometry_instances.v1"
FUNCTIONAL_BUILDING_GEOMETRY_SOURCE = (
    "Components.BuildingCompletes.GetWorldItems(activeWorldId)+Building.Orientation+Building.GetBottomLeftCell+Building.PlacementCells"
)
FUNCTIONAL_BUILDING_GEOMETRY_SCHEMA = "oni.functional_building_geometry.v1"
FUNCTIONAL_BUILDING_PORTS_CONTRACT_ID = "oni.qc.buildings.functional_ports.v1"
FUNCTIONAL_BUILDING_PORTS_SOURCE = (
    "Components.BuildingCompletes.GetWorldItems(activeWorldId)+BuildingDef.RequiresPowerInput/Output+BuildingDef.InputConduitType/OutputConduitType+BuildingDef.UseHighEnergyParticleInputPort/OutputPort+Building.Orientation+Building.GetPowerInputCell/GetPowerOutputCell/GetUtilityInputCell/GetUtilityOutputCell/GetHighEnergyParticleInputCell/GetHighEnergyParticleOutputCell"
)
FUNCTIONAL_BUILDING_PORTS_SCHEMA = "oni.functional_building_ports.v1"
FUNCTIONAL_BUILDING_OCCUPANCY_CONTRACT_ID = "oni.qc.buildings.functional_occupancy_instances.v1"
FUNCTIONAL_BUILDING_OCCUPANCY_SOURCE = (
    "Components.BuildingCompletes.GetWorldItems(activeWorldId)+Structure.IsEntombed+OccupyArea.GetOccupiedGridCells"
)
FUNCTIONAL_BUILDING_OCCUPANCY_SCHEMA = "oni.functional_building_occupancy.v1"
FUNCTIONAL_BUILDING_HEALTH_CONTRACT_ID = "oni.qc.buildings.functional_health_instances.v1"
FUNCTIONAL_BUILDING_HEALTH_SOURCE = (
    "Components.BuildingCompletes.GetWorldItems(activeWorldId)+BuildingEnabledButton.IsEnabled+BuildingHP.HitPoints/MaxHitPoints/IsBroken/NeedsRepairs"
)
FUNCTIONAL_BUILDING_HEALTH_SCHEMA = "oni.functional_building_health.v1"
FUNCTIONAL_BUILDING_FLOODED_CONTRACT_ID = "oni.qc.buildings.functional_flooded_instances.v1"
FUNCTIONAL_BUILDING_FLOODED_SOURCE = (
    "Components.BuildingCompletes.GetWorldItems(activeWorldId)+Floodable.IsFlooded"
)
FUNCTIONAL_BUILDING_FLOODED_SCHEMA = "oni.functional_building_flooded.v1"
FUNCTIONAL_BUILDING_REACHABILITY_CONTRACT_ID = "oni.qc.buildings.functional_reachability_instances.v1"
FUNCTIONAL_BUILDING_REACHABILITY_SOURCE = (
    "Components.BuildingCompletes.GetWorldItems(activeWorldId)+Workable.GetCell/GetOffsets+MinionGroupProber.IsAllReachable"
)
FUNCTIONAL_BUILDING_REACHABILITY_SCHEMA = "oni.functional_building_reachability.v1"
FUNCTIONAL_BUILDING_POWER_NETWORK_CONTRACT_ID = "oni.qc.buildings.functional_power_network_instances.v1"
FUNCTIONAL_BUILDING_POWER_NETWORK_SOURCE = (
    "Components.BuildingCompletes.GetWorldItems(activeWorldId)+Wire.IsConnected/NetworkID"
)
FUNCTIONAL_BUILDING_POWER_NETWORK_SCHEMA = "oni.functional_building_power_network.v1"
FUNCTIONAL_BUILDING_UTILITY_NETWORK_CONTRACT_ID = "oni.qc.buildings.functional_utility_network_instances.v1"
FUNCTIONAL_BUILDING_UTILITY_NETWORK_SOURCE = (
    "Components.BuildingCompletes.GetWorldItems(activeWorldId)+BuildingDef.Input/OutputConduitType+Building.GetUtilityInput/OutputCell+Conduit.GetNetworkManager/Game.solidConduitSystem.GetNetworkForCell"
)
FUNCTIONAL_BUILDING_UTILITY_NETWORK_SCHEMA = "oni.functional_building_utility_network.v1"
FUNCTIONAL_BUILDING_LOGIC_PORTS_CONTRACT_ID = "oni.qc.buildings.functional_logic_ports_instances.v1"
FUNCTIONAL_BUILDING_LOGIC_PORTS_SOURCE = (
    "Components.BuildingCompletes.GetWorldItems(activeWorldId)+LogicPorts.inputPortInfo/outputPortInfo+GetPortCell+IsPortConnected"
)
FUNCTIONAL_BUILDING_LOGIC_PORTS_SCHEMA = "oni.functional_building_logic_ports.v1"
FUNCTIONAL_BUILDING_UTILITY_ENDPOINT_CONTRACT_ID = "oni.qc.buildings.functional_utility_endpoint_instances.v1"
FUNCTIONAL_BUILDING_UTILITY_ENDPOINT_SOURCE = (
    "Components.BuildingCompletes.GetWorldItems(activeWorldId)+Building.GetUtilityInput/OutputCell+IUtilityNetworkMgr.GetEndpoint+FlowUtilityNetwork.IItem"
)
FUNCTIONAL_BUILDING_UTILITY_ENDPOINT_SCHEMA = "oni.functional_building_utility_endpoint.v1"
FUNCTIONAL_BUILDING_UTILITY_TOPOLOGY_CONTRACT_ID = "oni.qc.buildings.functional_utility_topology_instances.v1"
FUNCTIONAL_BUILDING_UTILITY_TOPOLOGY_SOURCE = (
    "Components.BuildingCompletes.GetWorldItems(activeWorldId)+Building.GetUtilityInput/OutputCell+IUtilityNetworkMgr.GetNetworkForCell+FlowUtilityNetwork.sources/sinks/conduitCount"
)
FUNCTIONAL_BUILDING_UTILITY_TOPOLOGY_SCHEMA = "oni.functional_building_utility_topology.v1"
FUNCTIONAL_BUILDING_UTILITY_CONTENTS_CONTRACT_ID = "oni.qc.buildings.functional_utility_contents_instances.v1"
FUNCTIONAL_BUILDING_UTILITY_CONTENTS_SOURCE = "Components.BuildingCompletes.GetWorldItems(activeWorldId)+Building.GetUtilityInput/OutputCell+ConduitFlow.GetContents/SolidConduitFlow.GetContents"
FUNCTIONAL_BUILDING_UTILITY_CONTENTS_SCHEMA = "oni.functional_building_utility_contents.v1"
DUPLICANT_CHORE_PRIORITY_CONTRACT_ID = "oni.qc.duplicants.chore_priority.v1"
DUPLICANT_PRIORITY_CONTRACT_ID = DUPLICANT_CHORE_PRIORITY_CONTRACT_ID
DIG_TOOL_CONTRACT_ID = "oni.qc.designations.dig_tool.v1"
DIG_CELL_CONTRACT_ID = "oni.qc.designations.dig_cell.v1"
BLUEPRINT_CATALOG_CONTRACT_ID = "oni.qc.blueprints.catalog.v1"
BLUEPRINT_CELL_CONTRACT_ID = "oni.qc.blueprints.cell.v1"
BLUEPRINT_INSTANCE_CONTRACT_ID = "oni.qc.blueprints.instance.v1"
BLUEPRINT_UTILITY_PATH_CONTRACT_ID = "oni.qc.blueprints.utility_path.v1"
BLUEPRINT_MATERIAL_LINEAGE_CONTRACT_ID = "oni.qc.blueprints.material_lineage.v1"
UTILITY_PATH_CONTRACT_ID = BLUEPRINT_UTILITY_PATH_CONTRACT_ID

NON_MUTATING_ACTIONS = frozenset({"noop", "focus_note"})
_MAX_CAPTURE_FUTURE_SKEW_SECONDS = 5.0
_CANONICAL_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SHA256_FINGERPRINT_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_INT32_MAX = 2_147_483_647
_CONDITION_OPERATORS = frozenset(
    {"eq", "ne", "lt", "lte", "gt", "gte", "contains"}
)
_PATH_NATIVE_BOOLEAN_FIELDS = frozenset(
    {
        "available",
        "is_utility",
        "cancellable",
        "visible",
        "solid",
        "foundation",
        "empty_for_blueprint",
        "can_place_ladder",
        "can_place_wire",
        "blueprint_present",
        "blueprint_in_progress",
        "blueprint_is_utility",
        "in_progress",
        "active",
        "fully_active",
        "designation_present",
        "designation_in_progress",
        "safe_mark_candidate",
    }
)
_PATH_NATIVE_INTEGER_FIELDS = frozenset(
    {
        "cell",
        "x",
        "y",
        "world_id",
        "object_count",
        "blueprint_instance_id",
        "blueprint_progress_percent",
        "blueprint_worker_instance_id",
        "blueprint_root_cell",
        "instance_id",
        "progress_percent",
        "worker_instance_id",
        "root_cell",
        "designation_instance_id",
        "blueprint_priority_value",
        "priority_value",
    }
)
_PATH_NATIVE_NUMBER_FIELDS = frozenset({"mass_kg", "damage"})
_PATH_NATIVE_FINGERPRINT_FIELDS = frozenset({"fingerprint", "empty_fingerprint"})
_PATH_NATIVE_STRING_LIST_FIELDS = frozenset({"material_tags", "blueprint_material_tags"})
_PATH_NATIVE_INTEGER_LIST_FIELDS = frozenset(
    {
        "cells",
        "blueprint_instance_ids",
        "active_blueprint_instance_ids",
        "blueprint_footprint_cells",
        "footprint_cells",
    }
)
_PATH_NATIVE_STRING_FIELDS = frozenset(
    {
        "element_id",
        "designation_kind",
        "prefab_id",
        "orientation",
        "facade_id",
        "blueprint_prefab_id",
        "blueprint_orientation",
        "path_id",
        "building_def_id",
        "blueprint_priority_class",
        "priority_class",
    }
)
_BLUEPRINT_MATERIAL_LINEAGE_ITEM_KEYS = frozenset(
    {"tag", "required_mass_kg", "delivered_mass_kg", "remaining_mass_kg", "delivery_status"}
)
_BLUEPRINT_MATERIAL_LINEAGE_STATUSES = frozenset({"complete", "pending", "unknown"})


@dataclass(frozen=True)
class FieldContract:
    contract_id: str
    paths: Tuple[str, ...]
    path_patterns: Tuple[str, ...] = ()
    statuses: Tuple[str, ...] = ("observed",)
    semantics: Tuple[str, ...] = ()
    units: Tuple[str, ...] = ()
    scopes: Tuple[str, ...] = ()
    completeness: Tuple[str, ...] = ("complete",)
    sources: Tuple[str, ...] = ()
    captures: Tuple[str, ...] = ("snapshot.ts",)
    value_type: str = "any"
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    maximum_items: Optional[int] = None
    maximum_text_length: Optional[int] = None
    canonical_string_items: bool = False
    sorted_items: bool = False
    allowed_string_items: Tuple[str, ...] = ()
    status_completeness: Tuple[Tuple[str, str], ...] = ()
    # When set, the entry's (scope, source) must be one of these pairs, so a
    # source cannot claim a scope it does not cover.
    scope_sources: Tuple[Tuple[str, str], ...] = ()
    capture_max_age_seconds: Optional[float] = None
    mutating_condition_allowed: bool = True

    def matches_path(self, path: str) -> bool:
        if path in self.paths:
            return True
        return any(re.fullmatch(pattern, path) is not None for pattern in self.path_patterns)


@dataclass(frozen=True)
class ContractRead:
    path: str
    contract_id: str
    value: Any
    status: str
    blockers: Tuple[str, ...]

    @property
    def usable(self) -> bool:
        return not self.blockers


_CONTRACTS: Tuple[FieldContract, ...] = (
    FieldContract(
        contract_id=GAME_PAUSED_CONTRACT_ID,
        paths=("game.paused",),
        statuses=("observed",),
        semantics=("direct_state",),
        units=("boolean",),
        scopes=("active_game_session",),
        completeness=("complete",),
        sources=("SpeedControlScreen.IsPaused",),
        captures=("snapshot.ts",),
        value_type="boolean",
    ),
    FieldContract(
        contract_id=GAME_BUILD_MODE_CONTRACT_ID,
        paths=("game.sandbox_mode", "game.instant_build_mode"),
        statuses=("observed",),
        semantics=("direct_state",),
        units=("boolean",),
        scopes=("active_game_session",),
        completeness=("complete",),
        sources=("Game.SandboxModeActive+DebugHandler.InstantBuildMode",),
        captures=("snapshot.ts",),
        value_type="boolean",
    ),
    FieldContract(
        contract_id=RESEARCH_CURRENT_CONTRACT_ID,
        paths=("research.current",),
        statuses=("observed",),
        semantics=("active_selection",),
        units=("technology_id_or_empty",),
        scopes=("save_research",),
        completeness=("complete",),
        sources=("Research.GetActiveResearch",),
        captures=("snapshot.ts",),
        value_type="string",
        maximum_text_length=128,
    ),
    FieldContract(
        # The finished technology ids (ordinal-sorted, unique).  It only
        # informs candidate selection (agent.candidates skips finished techs);
        # a set_research command binds research.current alone.
        contract_id=RESEARCH_COMPLETE_CONTRACT_ID,
        paths=("research.complete",),
        statuses=("observed",),
        semantics=("completed_technology_ids",),
        units=("technology_id_list",),
        scopes=("save_research",),
        completeness=("complete",),
        sources=(RESEARCH_COMPLETE_SOURCE,),
        captures=("snapshot.ts",),
        value_type="string_list",
        maximum_items=512,
        maximum_text_length=128,
        sorted_items=True,
        mutating_condition_allowed=False,
    ),
    FieldContract(
        contract_id=REGISTERED_FOOD_CONTRACT_ID,
        paths=("resources.food_kcal",),
        statuses=("observed", "estimated"),
        semantics=("registered_non_private_inventory_sum",),
        units=("kcal",),
        scopes=("active_world_registered_non_private_edibles",),
        completeness=("complete", "bounded"),
        sources=(
            "ClusterManager.activeWorld.worldInventory.GetPickupables(GameTags.Edible)+!GameTags.StoredPrivate",
        ),
        captures=("snapshot.ts",),
        value_type="number",
        minimum=0.0,
        maximum=1_000_000_000_000.0,
        status_completeness=(("observed", "complete"), ("estimated", "bounded")),
        # This is a registered-inventory diagnostic, not accessible-food proof.
        mutating_condition_allowed=False,
    ),
    FieldContract(
        contract_id=ALERTS_CANONICAL_CONTRACT_ID,
        paths=("alerts",),
        statuses=("observed",),
        semantics=("canonical_active_alert_ids",),
        units=("alert_id_list",),
        # The Probe reads every world with live duplicants (review G3/M8).
        # The active-world form is still read so older recordings replay; the
        # snapshot trust check accepts it only while the camera world is the
        # only populated one.
        scopes=(ALERTS_ALL_WORLDS_SCOPE, ALERTS_ACTIVE_WORLD_SCOPE),
        completeness=("complete",),
        sources=(ALERTS_ALL_WORLDS_SOURCE, ALERTS_ACTIVE_WORLD_SOURCE),
        scope_sources=(
            (ALERTS_ALL_WORLDS_SCOPE, ALERTS_ALL_WORLDS_SOURCE),
            (ALERTS_ACTIVE_WORLD_SCOPE, ALERTS_ACTIVE_WORLD_SOURCE),
        ),
        captures=("snapshot.ts",),
        value_type="string_list",
        maximum_items=128,
        maximum_text_length=64,
        canonical_string_items=True,
        sorted_items=True,
        allowed_string_items=CANONICAL_ALERT_IDS,
    ),
    FieldContract(
        contract_id=DUPLICANTS_TOTAL_CONTRACT_ID,
        paths=("duplicants.total",),
        statuses=("observed",),
        semantics=("live_count",),
        units=("count",),
        scopes=("active_world",),
        completeness=("complete",),
        sources=(DUPLICANTS_TOTAL_SOURCE,),
        captures=("snapshot.ts",),
        value_type="integer",
        minimum=0,
        maximum=1_000,
    ),
    FieldContract(
        contract_id=DUPLICANTS_IDLE_CONTRACT_ID,
        paths=("duplicants.idle",),
        statuses=("observed",),
        semantics=("idle_count",),
        units=("count",),
        scopes=("active_world",),
        completeness=("complete",),
        sources=(
            "Components.LiveMinionIdentities.GetWorldItems(activeWorldId,false)+GameTags.Idle",
        ),
        captures=("snapshot.ts",),
        value_type="integer",
        minimum=0,
        maximum=1_000,
        mutating_condition_allowed=False,
    ),
    FieldContract(
        contract_id=DUPLICANTS_STRESSED_CONTRACT_ID,
        paths=("duplicants.stressed",),
        statuses=("observed",),
        semantics=("stressed_count",),
        units=("count",),
        scopes=("active_world",),
        completeness=("complete",),
        sources=(
            "Components.LiveMinionIdentities.GetWorldItems(activeWorldId,false)+StressMonitor",
        ),
        captures=("snapshot.ts",),
        value_type="integer",
        minimum=0,
        maximum=1_000,
        mutating_condition_allowed=False,
    ),
    FieldContract(
        contract_id=BUILDING_COMPLETED_COUNT_CONTRACT_ID,
        paths=(
            "buildings.oxygen_diffuser",
            "buildings.outhouse",
            "buildings.manual_generator",
        ),
        statuses=("observed",),
        semantics=("completed_prefab_instance_count",),
        units=("count",),
        scopes=("active_world",),
        completeness=("complete",),
        sources=("Components.BuildingCompletes.GetWorldItems(activeWorldId)",),
        captures=("buildings.captured_at",),
        value_type="integer",
        minimum=0,
        maximum=1_000_000,
        capture_max_age_seconds=35.0,
        # A completed-prefab count is diagnostic and does not prove function.
        mutating_condition_allowed=False,
    ),
    FieldContract(
        contract_id=FUNCTIONAL_BUILDINGS_CONTRACT_ID,
        paths=("functional_buildings",),
        statuses=("observed",),
        semantics=("completed_building_operational_instances",),
        units=("instance_records",),
        scopes=("active_world",),
        completeness=("complete",),
        sources=(FUNCTIONAL_BUILDINGS_SOURCE,),
        captures=("functional_buildings.captured_at",),
        value_type="functional_buildings",
        status_completeness=(("observed", "complete"),),
        capture_max_age_seconds=5.0,
        mutating_condition_allowed=False,
    ),
    FieldContract(
        contract_id=FUNCTIONAL_BUILDING_GEOMETRY_CONTRACT_ID,
        paths=("functional_building_geometry",),
        statuses=("observed",),
        semantics=("completed_building_geometry_instances",),
        units=("instance_records",),
        scopes=("active_world",),
        completeness=("complete",),
        sources=(FUNCTIONAL_BUILDING_GEOMETRY_SOURCE,),
        captures=("functional_building_geometry.captured_at",),
        value_type="functional_building_geometry",
        status_completeness=(("observed", "complete"),),
        capture_max_age_seconds=5.0,
        mutating_condition_allowed=False,
    ),
    FieldContract(
        contract_id=FUNCTIONAL_BUILDING_PORTS_CONTRACT_ID,
        paths=("functional_building_ports",),
        statuses=("observed",),
        semantics=("completed_building_declared_ports",),
        units=("port_records",),
        scopes=("active_world",),
        completeness=("complete",),
        sources=(FUNCTIONAL_BUILDING_PORTS_SOURCE,),
        captures=("functional_building_ports.captured_at",),
        value_type="functional_building_ports",
        status_completeness=(("observed", "complete"),),
        capture_max_age_seconds=5.0,
        mutating_condition_allowed=False,
    ),
    FieldContract(
        contract_id=FUNCTIONAL_BUILDING_OCCUPANCY_CONTRACT_ID,
        paths=("functional_building_occupancy",),
        statuses=("observed",),
        semantics=("completed_building_occupancy_instances",),
        units=("instance_records",),
        scopes=("active_world",),
        completeness=("complete",),
        sources=(FUNCTIONAL_BUILDING_OCCUPANCY_SOURCE,),
        captures=("functional_building_occupancy.captured_at",),
        value_type="functional_building_occupancy",
        status_completeness=(("observed", "complete"),),
        capture_max_age_seconds=5.0,
        mutating_condition_allowed=False,
    ),
    FieldContract(
        contract_id=FUNCTIONAL_BUILDING_HEALTH_CONTRACT_ID,
        paths=("functional_building_health",),
        statuses=("observed",),
        semantics=("completed_building_health_instances",),
        units=("instance_records",),
        scopes=("active_world",),
        completeness=("complete",),
        sources=(FUNCTIONAL_BUILDING_HEALTH_SOURCE,),
        captures=("functional_building_health.captured_at",),
        value_type="functional_building_health",
        status_completeness=(("observed", "complete"),),
        capture_max_age_seconds=5.0,
        mutating_condition_allowed=False,
    ),
    FieldContract(
        contract_id=FUNCTIONAL_BUILDING_FLOODED_CONTRACT_ID,
        paths=("functional_building_flooded",),
        statuses=("observed",),
        semantics=("completed_building_flooded_instances",),
        units=("instance_records",),
        scopes=("active_world",),
        completeness=("complete",),
        sources=(FUNCTIONAL_BUILDING_FLOODED_SOURCE,),
        captures=("functional_building_flooded.captured_at",),
        value_type="functional_building_flooded",
        status_completeness=(("observed", "complete"),),
        capture_max_age_seconds=5.0,
        mutating_condition_allowed=False,
    ),
    FieldContract(
        contract_id=FUNCTIONAL_BUILDING_REACHABILITY_CONTRACT_ID,
        paths=("functional_building_reachability",),
        statuses=("observed",),
        semantics=("completed_building_reachability_instances",),
        units=("instance_records",),
        scopes=("active_world",),
        completeness=("complete",),
        sources=(FUNCTIONAL_BUILDING_REACHABILITY_SOURCE,),
        captures=("functional_building_reachability.captured_at",),
        value_type="functional_building_reachability",
        status_completeness=(("observed", "complete"),),
        capture_max_age_seconds=5.0,
        mutating_condition_allowed=False,
    ),
    FieldContract(
        contract_id=FUNCTIONAL_BUILDING_POWER_NETWORK_CONTRACT_ID,
        paths=("functional_building_power_network",),
        statuses=("observed",),
        semantics=("completed_building_power_network_instances",),
        units=("instance_records",),
        scopes=("active_world",),
        completeness=("complete",),
        sources=(FUNCTIONAL_BUILDING_POWER_NETWORK_SOURCE,),
        captures=("functional_building_power_network.captured_at",),
        value_type="functional_building_power_network",
        status_completeness=(("observed", "complete"),),
        capture_max_age_seconds=5.0,
        mutating_condition_allowed=False,
    ),
    FieldContract(
        contract_id=FUNCTIONAL_BUILDING_UTILITY_NETWORK_CONTRACT_ID,
        paths=("functional_building_utility_network",),
        statuses=("observed",),
        semantics=("completed_building_utility_network_instances",),
        units=("instance_records",),
        scopes=("active_world",),
        completeness=("complete",),
        sources=(FUNCTIONAL_BUILDING_UTILITY_NETWORK_SOURCE,),
        captures=("functional_building_utility_network.captured_at",),
        value_type="functional_building_utility_network",
        status_completeness=(("observed", "complete"),),
        capture_max_age_seconds=5.0,
        mutating_condition_allowed=False,
    ),
    FieldContract(
        contract_id=FUNCTIONAL_BUILDING_LOGIC_PORTS_CONTRACT_ID,
        paths=("functional_building_logic_ports",),
        statuses=("observed",),
        semantics=("completed_building_logic_port_instances",),
        units=("instance_records",),
        scopes=("active_world",),
        completeness=("complete",),
        sources=(FUNCTIONAL_BUILDING_LOGIC_PORTS_SOURCE,),
        captures=("functional_building_logic_ports.captured_at",),
        value_type="functional_building_logic_ports",
        status_completeness=(("observed", "complete"),),
        capture_max_age_seconds=5.0,
        mutating_condition_allowed=False,
    ),
    FieldContract(
        contract_id=FUNCTIONAL_BUILDING_UTILITY_ENDPOINT_CONTRACT_ID,
        paths=("functional_building_utility_endpoint",),
        statuses=("observed",),
        semantics=("completed_building_utility_endpoint_instances",),
        units=("instance_records",),
        scopes=("active_world",),
        completeness=("complete",),
        sources=(FUNCTIONAL_BUILDING_UTILITY_ENDPOINT_SOURCE,),
        captures=("functional_building_utility_endpoint.captured_at",),
        value_type="functional_building_utility_endpoint",
        status_completeness=(("observed", "complete"),),
        capture_max_age_seconds=5.0,
        mutating_condition_allowed=False,
    ),
    FieldContract(
        contract_id=FUNCTIONAL_BUILDING_UTILITY_TOPOLOGY_CONTRACT_ID,
        paths=("functional_building_utility_topology",), statuses=("observed",),
        semantics=("completed_building_utility_topology_instances",), units=("instance_records",),
        scopes=("active_world",), completeness=("complete",),
        sources=(FUNCTIONAL_BUILDING_UTILITY_TOPOLOGY_SOURCE,),
        captures=("functional_building_utility_topology.captured_at",),
        value_type="functional_building_utility_topology",
        status_completeness=(("observed", "complete"),), capture_max_age_seconds=5.0,
        mutating_condition_allowed=False,
    ),
    FieldContract(
        contract_id=FUNCTIONAL_BUILDING_UTILITY_CONTENTS_CONTRACT_ID,
        paths=("functional_building_utility_contents",), statuses=("observed",),
        semantics=("completed_building_utility_contents_instances",), units=("instance_records",), scopes=("active_world",), completeness=("complete",),
        sources=(FUNCTIONAL_BUILDING_UTILITY_CONTENTS_SOURCE,), captures=("functional_building_utility_contents.captured_at",), value_type="functional_building_utility_contents", status_completeness=(("observed", "complete"),), capture_max_age_seconds=5.0, mutating_condition_allowed=False,
    ),
    FieldContract(
        contract_id=DUPLICANT_CHORE_PRIORITY_CONTRACT_ID,
        paths=(),
        path_patterns=(
            r"^duplicants\.by_instance_id\.id_[1-9][0-9]*\.chore_priorities\.[A-Za-z0-9_]{1,64}$",
        ),
        statuses=("observed",),
        semantics=("configured_chore_priority",),
        units=("priority_0_to_5",),
        scopes=("active_world_live_duplicant",),
        completeness=("complete",),
        sources=("Components.LiveMinionIdentities+ChoreConsumer",),
        captures=("snapshot.ts",),
        value_type="integer",
        minimum=0,
        maximum=5,
        status_completeness=(("observed", "complete"),),
    ),
    FieldContract(
        contract_id=DIG_TOOL_CONTRACT_ID,
        paths=(
            "designations.dig.tool.tiles_filter_on",
            "designations.dig.tool.natural_backwall_filter_on",
        ),
        statuses=("observed",),
        semantics=("dig_tool_filter_state",),
        units=("boolean",),
        scopes=("active_game_session",),
        completeness=("complete",),
        sources=("Grid+DigTool+Components.Diggables",),
        captures=("snapshot.ts",),
        value_type="boolean",
        status_completeness=(("observed", "complete"),),
    ),
    FieldContract(
        contract_id=DIG_CELL_CONTRACT_ID,
        paths=(),
        path_patterns=(
            r"^designations\.dig\.by_cell\.cell_[0-9]+\."
            r"(cell|world_id|solid|element_id|mass_kg|damage|designation_present|"
            r"designation_instance_id|designation_kind|designation_in_progress|"
            r"safe_mark_candidate|fingerprint)$",
        ),
        statuses=("observed",),
        semantics=("dig_cell_state_field",),
        units=("field_native",),
        scopes=("active_world_tracked_cells",),
        completeness=("complete_for_tracked_cell",),
        sources=("Grid+DigTool+Components.Diggables",),
        captures=("snapshot.ts",),
        value_type="path_native",
        status_completeness=(("observed", "complete_for_tracked_cell"),),
    ),
    FieldContract(
        contract_id=BLUEPRINT_CATALOG_CONTRACT_ID,
        paths=(),
        path_patterns=(
            r"^blueprints\.catalog\.(Ladder|Wire)\."
            r"(available|prefab_id|is_utility|cancellable|orientation|facade_id|material_tags)$",
        ),
        statuses=("observed",),
        semantics=("blueprint_catalog_field",),
        units=("field_native",),
        scopes=("approved_blueprint_catalog",),
        completeness=("complete",),
        sources=("BuildingDef+Grid+Constructable+WorldInventory",),
        captures=("snapshot.ts",),
        value_type="path_native",
        status_completeness=(("observed", "complete"),),
    ),
    FieldContract(
        contract_id=BLUEPRINT_CELL_CONTRACT_ID,
        paths=(),
        path_patterns=(
            r"^cells\.by_id\.cell_[0-9]+\."
            r"(cell|x|y|world_id|visible|solid|foundation|element_id|mass_kg|damage|"
            r"object_count|empty_for_blueprint|can_place_ladder|can_place_wire|"
            r"blueprint_present|blueprint_instance_id|blueprint_prefab_id|"
            r"blueprint_orientation|blueprint_material_tags|blueprint_in_progress|"
            r"blueprint_is_utility|blueprint_progress_percent|"
            r"blueprint_worker_instance_id|blueprint_root_cell|"
            r"blueprint_footprint_cells|blueprint_priority_class|blueprint_priority_value|"
            r"empty_fingerprint|fingerprint)$",
        ),
        statuses=("observed",),
        semantics=("blueprint_cell_state_field",),
        units=("field_native",),
        scopes=("active_world_tracked_cells",),
        completeness=("complete_for_tracked_cell",),
        sources=("BuildingDef+Grid+Constructable+WorldInventory",),
        captures=("snapshot.ts",),
        value_type="path_native",
        status_completeness=(("observed", "complete_for_tracked_cell"),),
    ),
    FieldContract(
        contract_id=BLUEPRINT_INSTANCE_CONTRACT_ID,
        paths=(),
        path_patterns=(
            r"^blueprints\.by_instance_id\.id_[1-9][0-9]*\."
            r"(instance_id|cell|world_id|prefab_id|orientation|material_tags|"
            r"in_progress|is_utility|fingerprint|progress_percent|worker_instance_id|"
            r"root_cell|footprint_cells|priority_class|priority_value)$",
        ),
        statuses=("observed",),
        semantics=("blueprint_instance_state_field",),
        units=("field_native",),
        scopes=("active_world_registered_blueprints",),
        completeness=("complete_for_registered_instance",),
        sources=("BuildingDef+Grid+Constructable+WorldInventory",),
        captures=("snapshot.ts",),
        value_type="path_native",
        status_completeness=(("observed", "complete_for_registered_instance"),),
    ),
    FieldContract(
        contract_id=BLUEPRINT_UTILITY_PATH_CONTRACT_ID,
        paths=(),
        path_patterns=(
            r"^blueprints\.utility_paths\.id_[A-Za-z0-9_]{1,64}\."
            r"(path_id|building_def_id|cells|blueprint_instance_ids|"
            r"active_blueprint_instance_ids|active|fully_active)$",
        ),
        statuses=("observed",),
        semantics=("utility_blueprint_path_state_field",),
        units=("field_native",),
        scopes=("probe_tracked_utility_paths",),
        completeness=("complete_for_tracked_path",),
        sources=("BuildingDef+Grid+Constructable+WorldInventory",),
        captures=("snapshot.ts",),
        value_type="path_native",
        status_completeness=(("observed", "complete_for_tracked_path"),),
    ),
    FieldContract(
        contract_id=BLUEPRINT_MATERIAL_LINEAGE_CONTRACT_ID,
        paths=(),
        path_patterns=(
            r"^(cells\.by_id\.cell_[0-9]+\.blueprint_material_lineage|"
            r"blueprints\.by_instance_id\.id_[1-9][0-9]*\.material_lineage)$",
        ),
        statuses=("observed",),
        semantics=("blueprint_material_lineage",),
        units=("mass_kg_by_recipe_ingredient",),
        scopes=("active_world_tracked_blueprint",),
        completeness=("complete_for_constructable",),
        sources=(
            "Constructable.Recipe+Storage.GetMassAvailable",
            "BuildingDef+Grid+Constructable+WorldInventory",
        ),
        captures=("snapshot.ts",),
        value_type="blueprint_material_lineage",
        status_completeness=(("observed", "complete_for_constructable"),),
        mutating_condition_allowed=False,
    ),
)


FIELD_CONTRACTS_BY_ID: Dict[str, FieldContract] = {
    contract.contract_id: contract for contract in _CONTRACTS
}


def contract_by_id(contract_id: Any) -> Optional[FieldContract]:
    if not isinstance(contract_id, str):
        return None
    return FIELD_CONTRACTS_BY_ID.get(contract_id)


def contract_for_path(path: str) -> Optional[FieldContract]:
    matches = [contract for contract in FIELD_CONTRACTS_BY_ID.values() if contract.matches_path(path)]
    return matches[0] if len(matches) == 1 else None


def contract_id_for_path(path: str) -> Optional[str]:
    contract = contract_for_path(path)
    return contract.contract_id if contract is not None else None


def _capture_binding_blockers(
    state: Mapping[str, Any],
    entry: Mapping[str, Any],
    contract: FieldContract,
) -> list[str]:
    binding = entry.get("captured_at")
    if not isinstance(binding, str) or binding not in contract.captures:
        return ["quality_capture_binding_missing"]
    captured_value = state.get("ts") if binding == "snapshot.ts" else safe_get(dict(state), binding)
    captured_at = parse_snapshot_timestamp(captured_value)
    snapshot_at = parse_snapshot_timestamp(state.get("ts"))
    if captured_at is None:
        return ["quality_capture_timestamp_invalid"]
    if snapshot_at is None:
        return ["snapshot_timestamp_invalid"]
    age_seconds = (snapshot_at - captured_at).total_seconds()
    blockers: list[str] = []
    if age_seconds < -_MAX_CAPTURE_FUTURE_SKEW_SECONDS:
        blockers.append("quality_capture_in_future")
    if contract.capture_max_age_seconds is not None and age_seconds > contract.capture_max_age_seconds:
        blockers.append("quality_capture_stale")
    return blockers


def _path_native_value_blockers(path: Optional[str], value: Any) -> list[str]:
    if not isinstance(path, str) or "." not in path:
        return ["field_type_mismatch"]
    field = path.rsplit(".", 1)[1]
    if field in _PATH_NATIVE_BOOLEAN_FIELDS:
        return [] if isinstance(value, bool) else ["field_type_mismatch"]
    if field in _PATH_NATIVE_INTEGER_FIELDS:
        if isinstance(value, bool) or not isinstance(value, int):
            return ["field_type_mismatch"]
        if value < 0:
            return ["field_below_minimum"]
        if value > _INT32_MAX:
            return ["field_above_maximum"]
        if field in {"blueprint_progress_percent", "progress_percent"} and value > 100:
            return ["field_above_maximum"]
        if field in {"blueprint_priority_value", "priority_value"} and not 1 <= value <= 9:
            return ["field_below_minimum" if value < 1 else "field_above_maximum"]
        return []
    if field in _PATH_NATIVE_NUMBER_FIELDS:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return ["field_type_mismatch"]
        try:
            number = float(value)
        except (OverflowError, ValueError):
            return ["field_not_finite"]
        if not math.isfinite(number):
            return ["field_not_finite"]
        return [] if number >= 0 else ["field_below_minimum"]
    if field in _PATH_NATIVE_FINGERPRINT_FIELDS:
        return (
            []
            if isinstance(value, str) and _SHA256_FINGERPRINT_RE.fullmatch(value) is not None
            else ["field_type_mismatch"]
        )
    if field in _PATH_NATIVE_STRING_LIST_FIELDS:
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            return ["field_type_mismatch"]
        if len(value) > 8:
            return ["field_list_too_long"]
        if any(len(item) > 64 for item in value):
            return ["field_text_too_long"]
        return []
    if field in _PATH_NATIVE_INTEGER_LIST_FIELDS:
        if not isinstance(value, list):
            return ["field_type_mismatch"]
        if len(value) > 64:
            return ["field_list_too_long"]
        if any(isinstance(item, bool) or not isinstance(item, int) for item in value):
            return ["field_type_mismatch"]
        if any(item < 0 for item in value):
            return ["field_below_minimum"]
        if any(item > _INT32_MAX for item in value):
            return ["field_above_maximum"]
        if field in {"blueprint_footprint_cells", "footprint_cells"}:
            if not value:
                return ["field_list_empty"]
            if value != sorted(value):
                return ["field_list_unsorted"]
            if len(value) != len(set(value)):
                return ["field_list_duplicates"]
        return []
    if field in _PATH_NATIVE_STRING_FIELDS:
        if not isinstance(value, str):
            return ["field_type_mismatch"]
        if field in {"blueprint_priority_class", "priority_class"} and value not in {
            "idle", "basic", "high", "personalNeeds", "topPriority", "compulsory"
        }:
            return ["field_identifier_invalid"]
        return [] if len(value) <= 128 else ["field_text_too_long"]
    return ["field_type_mismatch"]


def _finite_float(value: Any) -> Optional[float]:
    """Return a JSON number as a finite float, or None for anything else.

    A JSON integer beyond binary64 (e.g. ``10**400``) raises OverflowError in
    ``float``; a validator must report it as a blocker, not raise.
    """

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _blueprint_material_lineage_blockers(value: Any) -> list[str]:
    if not isinstance(value, list):
        return ["blueprint_material_lineage_type_mismatch"]
    blockers: list[str] = []
    if not value or len(value) > 8:
        blockers.append("blueprint_material_lineage_count_invalid")
    seen_tags: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping) or set(item) != _BLUEPRINT_MATERIAL_LINEAGE_ITEM_KEYS:
            blockers.append("blueprint_material_lineage_item_shape_invalid")
            continue
        tag = item.get("tag")
        if not isinstance(tag, str) or not tag or len(tag) > 64:
            blockers.append("blueprint_material_lineage_tag_invalid")
        elif tag in seen_tags:
            blockers.append("blueprint_material_lineage_tag_duplicate")
        else:
            seen_tags.add(tag)
        required = item.get("required_mass_kg")
        required_number = _finite_float(required)
        if required_number is None or required_number < 0:
            blockers.append("blueprint_material_lineage_required_invalid")
        delivered = item.get("delivered_mass_kg")
        remaining = item.get("remaining_mass_kg")
        masses: Dict[str, Optional[float]] = {}
        for name, number in (("delivered", delivered), ("remaining", remaining)):
            masses[name] = _finite_float(number)
            if number is not None and (masses[name] is None or masses[name] < 0):
                blockers.append(f"blueprint_material_lineage_{name}_invalid")
        status = item.get("delivery_status")
        if status not in _BLUEPRINT_MATERIAL_LINEAGE_STATUSES:
            blockers.append("blueprint_material_lineage_status_invalid")
        elif status == "unknown":
            if delivered is not None or remaining is not None:
                blockers.append("blueprint_material_lineage_unknown_has_mass")
        elif delivered is None or remaining is None:
            blockers.append("blueprint_material_lineage_observed_mass_missing")
        elif required_number is None or masses["delivered"] is None or masses["remaining"] is None:
            # Already reported as invalid above; there is nothing to compare.
            pass
        else:
            required_value = required_number
            delivered_value = masses["delivered"]
            remaining_value = masses["remaining"]
            if delivered_value > required_value + 0.0001:
                blockers.append("blueprint_material_lineage_delivered_exceeds_required")
            if remaining_value > required_value + 0.0001:
                blockers.append("blueprint_material_lineage_remaining_exceeds_required")
            if abs((delivered_value + remaining_value) - required_value) > 0.0002:
                blockers.append("blueprint_material_lineage_mass_not_conserved")
            if status == "complete" and remaining_value > 0.0001:
                blockers.append("blueprint_material_lineage_complete_remaining")
            if status == "pending" and remaining_value <= 0.0001:
                blockers.append("blueprint_material_lineage_pending_without_remaining")
    return sorted(set(blockers))


_FUNCTIONAL_BUILDING_INSTANCE_KEYS = frozenset(
    {"instance_id", "world_id", "prefab_id", "operational"}
)
_FUNCTIONAL_BUILDING_PAYLOAD_KEYS = frozenset(
    {"schema", "captured_at", "world_id", "complete", "instances"}
)
# Optional: the sorted prefab ids the producer enumerates, so a consumer can
# tell "none exist" from "not tracked" for a prefab without instances.
_FUNCTIONAL_BUILDING_OPTIONAL_PAYLOAD_KEYS = frozenset({"prefab_ids"})
_FUNCTIONAL_BUILDING_MAX_INSTANCES = 10_000


def _functional_buildings_payload_blockers(value: Any) -> list[str]:
    """Validate the producer's bounded, active-world instance envelope."""

    if not isinstance(value, Mapping):
        return ["functional_buildings_type_mismatch"]

    blockers: list[str] = []
    keys = set(value)
    if not _FUNCTIONAL_BUILDING_PAYLOAD_KEYS <= keys or not keys <= (
        _FUNCTIONAL_BUILDING_PAYLOAD_KEYS | _FUNCTIONAL_BUILDING_OPTIONAL_PAYLOAD_KEYS
    ):
        blockers.append("functional_buildings_payload_shape_invalid")
    if "prefab_ids" in value:
        declared = value.get("prefab_ids")
        if (
            not isinstance(declared, list)
            or not declared
            or any(item not in FUNCTIONAL_BUILDING_PREFAB_IDS for item in declared)
            or declared != sorted(set(declared))
        ):
            blockers.append("functional_buildings_prefab_ids_invalid")
    if value.get("schema") != FUNCTIONAL_BUILDINGS_SCHEMA:
        blockers.append("functional_buildings_schema_invalid")
    captured_at = value.get("captured_at")
    if not isinstance(captured_at, str) or parse_snapshot_timestamp(captured_at) is None:
        blockers.append("functional_buildings_capture_invalid")

    world_id = value.get("world_id")
    if (
        isinstance(world_id, bool)
        or not isinstance(world_id, int)
        or world_id < 0
        or world_id > _INT32_MAX
    ):
        blockers.append("functional_buildings_world_id_invalid")
    if not isinstance(value.get("complete"), bool):
        blockers.append("functional_buildings_complete_invalid")
    elif value.get("complete") is not True:
        blockers.append("functional_buildings_incomplete")

    instances = value.get("instances")
    if not isinstance(instances, list):
        blockers.append("functional_buildings_instances_invalid")
        return sorted(set(blockers))
    if len(instances) > _FUNCTIONAL_BUILDING_MAX_INSTANCES:
        blockers.append("functional_buildings_instances_too_many")

    seen_instance_ids: set[int] = set()
    for instance in instances:
        if not isinstance(instance, Mapping) or set(instance) != _FUNCTIONAL_BUILDING_INSTANCE_KEYS:
            blockers.append("functional_building_instance_shape_invalid")
            continue

        instance_id = instance.get("instance_id")
        if (
            isinstance(instance_id, bool)
            or not isinstance(instance_id, int)
            or instance_id <= 0
            or instance_id > _INT32_MAX
        ):
            blockers.append("functional_building_instance_id_invalid")
        elif instance_id in seen_instance_ids:
            blockers.append("functional_building_instance_id_duplicate")
        else:
            seen_instance_ids.add(instance_id)

        instance_world_id = instance.get("world_id")
        if (
            isinstance(instance_world_id, bool)
            or not isinstance(instance_world_id, int)
            or instance_world_id < 0
            or instance_world_id > _INT32_MAX
        ):
            blockers.append("functional_building_instance_world_id_invalid")
        elif isinstance(world_id, int) and not isinstance(world_id, bool) and instance_world_id != world_id:
            blockers.append("functional_building_instance_world_mismatch")

        if instance.get("prefab_id") not in FUNCTIONAL_BUILDING_PREFAB_IDS:
            blockers.append("functional_building_prefab_id_invalid")
        operational = instance.get("operational")
        if operational is not None and not isinstance(operational, bool):
            blockers.append("functional_building_operational_type_invalid")

    return sorted(set(blockers))


def _functional_buildings_state_binding_blockers(
    state: Mapping[str, Any], value: Any
) -> list[str]:
    """Require the payload world to equal the snapshot's bound active world."""

    if not isinstance(value, Mapping):
        return []
    snapshot_world_id = state.get("world_id")
    if (
        isinstance(snapshot_world_id, bool)
        or not isinstance(snapshot_world_id, int)
        or snapshot_world_id < 0
        or snapshot_world_id > _INT32_MAX
    ):
        return ["snapshot_world_id_invalid"]
    blockers: list[str] = []
    if value.get("world_id") != snapshot_world_id:
        blockers.append("functional_buildings_snapshot_world_mismatch")

    observation_scope = state.get("observation_scope")
    if not isinstance(observation_scope, Mapping):
        blockers.append("functional_buildings_active_world_scope_missing")
        return sorted(set(blockers))
    active_world_id = observation_scope.get("active_world_id")
    if (
        isinstance(active_world_id, bool)
        or not isinstance(active_world_id, int)
        or active_world_id < 0
        or active_world_id > _INT32_MAX
    ):
        blockers.append("functional_buildings_active_world_id_invalid")
    elif active_world_id != snapshot_world_id:
        blockers.append("functional_buildings_snapshot_active_world_mismatch")
    elif value.get("world_id") != active_world_id:
        blockers.append("functional_buildings_active_world_mismatch")
    return sorted(set(blockers))


_FUNCTIONAL_BUILDING_GEOMETRY_INSTANCE_KEYS = frozenset(
    {"instance_id", "world_id", "prefab_id", "orientation", "root_cell", "footprint_cells"}
)
_FUNCTIONAL_BUILDING_GEOMETRY_PAYLOAD_KEYS = frozenset(
    {"schema", "captured_at", "world_id", "complete", "instances"}
)
_FUNCTIONAL_BUILDING_GEOMETRY_MAX_INSTANCES = 10_000
_FUNCTIONAL_BUILDING_GEOMETRY_MAX_FOOTPRINT_CELLS = 64


def _functional_building_geometry_payload_blockers(value: Any) -> list[str]:
    """Validate bounded placement geometry from completed active-world buildings."""

    if not isinstance(value, Mapping):
        return ["functional_building_geometry_type_mismatch"]

    blockers: list[str] = []
    if set(value) != _FUNCTIONAL_BUILDING_GEOMETRY_PAYLOAD_KEYS:
        blockers.append("functional_building_geometry_payload_shape_invalid")
    if value.get("schema") != FUNCTIONAL_BUILDING_GEOMETRY_SCHEMA:
        blockers.append("functional_building_geometry_schema_invalid")
    captured_at = value.get("captured_at")
    if not isinstance(captured_at, str) or parse_snapshot_timestamp(captured_at) is None:
        blockers.append("functional_building_geometry_capture_invalid")

    world_id = value.get("world_id")
    if (
        isinstance(world_id, bool)
        or not isinstance(world_id, int)
        or world_id < 0
        or world_id > _INT32_MAX
    ):
        blockers.append("functional_building_geometry_world_id_invalid")
    if not isinstance(value.get("complete"), bool):
        blockers.append("functional_building_geometry_complete_invalid")
    elif value.get("complete") is not True:
        blockers.append("functional_building_geometry_incomplete")

    instances = value.get("instances")
    if not isinstance(instances, list):
        blockers.append("functional_building_geometry_instances_invalid")
        return sorted(set(blockers))
    if len(instances) > _FUNCTIONAL_BUILDING_GEOMETRY_MAX_INSTANCES:
        blockers.append("functional_building_geometry_instances_too_many")

    seen_instance_ids: set[int] = set()
    for instance in instances:
        if not isinstance(instance, Mapping) or set(instance) != _FUNCTIONAL_BUILDING_GEOMETRY_INSTANCE_KEYS:
            blockers.append("functional_building_geometry_instance_shape_invalid")
            continue

        instance_id = instance.get("instance_id")
        if (
            isinstance(instance_id, bool)
            or not isinstance(instance_id, int)
            or instance_id <= 0
            or instance_id > _INT32_MAX
        ):
            blockers.append("functional_building_geometry_instance_id_invalid")
        elif instance_id in seen_instance_ids:
            blockers.append("functional_building_geometry_instance_id_duplicate")
        else:
            seen_instance_ids.add(instance_id)

        instance_world_id = instance.get("world_id")
        if (
            isinstance(instance_world_id, bool)
            or not isinstance(instance_world_id, int)
            or instance_world_id < 0
            or instance_world_id > _INT32_MAX
        ):
            blockers.append("functional_building_geometry_instance_world_id_invalid")
        elif isinstance(world_id, int) and not isinstance(world_id, bool) and instance_world_id != world_id:
            blockers.append("functional_building_geometry_instance_world_mismatch")

        prefab_id = instance.get("prefab_id")
        if not isinstance(prefab_id, str) or not prefab_id or len(prefab_id) > 128:
            blockers.append("functional_building_geometry_prefab_id_invalid")
        orientation = instance.get("orientation")
        if not isinstance(orientation, str) or not orientation or len(orientation) > 128:
            blockers.append("functional_building_geometry_orientation_invalid")

        root_cell = instance.get("root_cell")
        if (
            isinstance(root_cell, bool)
            or not isinstance(root_cell, int)
            or root_cell < 0
            or root_cell > _INT32_MAX
        ):
            blockers.append("functional_building_geometry_root_cell_invalid")

        footprint_cells = instance.get("footprint_cells")
        if not isinstance(footprint_cells, list):
            blockers.append("functional_building_geometry_footprint_invalid")
            continue
        if not footprint_cells:
            blockers.append("functional_building_geometry_footprint_empty")
        if len(footprint_cells) > _FUNCTIONAL_BUILDING_GEOMETRY_MAX_FOOTPRINT_CELLS:
            blockers.append("functional_building_geometry_footprint_too_many")
        previous_cell: Optional[int] = None
        seen_cells: set[int] = set()
        for cell in footprint_cells:
            if (
                isinstance(cell, bool)
                or not isinstance(cell, int)
                or cell < 0
                or cell > _INT32_MAX
            ):
                blockers.append("functional_building_geometry_footprint_cell_invalid")
                continue
            if cell in seen_cells:
                blockers.append("functional_building_geometry_footprint_duplicate")
            seen_cells.add(cell)
            if previous_cell is not None and cell <= previous_cell:
                blockers.append("functional_building_geometry_footprint_unsorted")
            previous_cell = cell
        if (
            isinstance(root_cell, int)
            and not isinstance(root_cell, bool)
            and root_cell >= 0
            and root_cell <= _INT32_MAX
            and isinstance(footprint_cells, list)
            and root_cell not in seen_cells
        ):
            blockers.append("functional_building_geometry_root_cell_not_in_footprint")

    return sorted(set(blockers))


def _functional_building_geometry_state_binding_blockers(
    state: Mapping[str, Any], value: Any
) -> list[str]:
    """Require geometry world identity to equal the snapshot active world."""

    if not isinstance(value, Mapping):
        return []
    snapshot_world_id = state.get("world_id")
    if (
        isinstance(snapshot_world_id, bool)
        or not isinstance(snapshot_world_id, int)
        or snapshot_world_id < 0
        or snapshot_world_id > _INT32_MAX
    ):
        return ["snapshot_world_id_invalid"]
    blockers: list[str] = []
    if value.get("world_id") != snapshot_world_id:
        blockers.append("functional_building_geometry_snapshot_world_mismatch")

    observation_scope = state.get("observation_scope")
    if not isinstance(observation_scope, Mapping):
        blockers.append("functional_building_geometry_active_world_scope_missing")
        return sorted(set(blockers))
    active_world_id = observation_scope.get("active_world_id")
    if (
        isinstance(active_world_id, bool)
        or not isinstance(active_world_id, int)
        or active_world_id < 0
        or active_world_id > _INT32_MAX
    ):
        blockers.append("functional_building_geometry_active_world_id_invalid")
    elif active_world_id != snapshot_world_id:
        blockers.append("functional_building_geometry_snapshot_active_world_mismatch")
    elif value.get("world_id") != active_world_id:
        blockers.append("functional_building_geometry_active_world_mismatch")
    return sorted(set(blockers))


_FUNCTIONAL_BUILDING_PORT_KEYS = frozenset(
    {"kind", "direction", "cell", "type", "connection_status"}
)
_FUNCTIONAL_BUILDING_PORT_INSTANCE_KEYS = frozenset(
    {"instance_id", "world_id", "prefab_id", "orientation", "ports"}
)
_FUNCTIONAL_BUILDING_PORT_PAYLOAD_KEYS = frozenset(
    {"schema", "captured_at", "world_id", "complete", "instances"}
)
_FUNCTIONAL_BUILDING_PORT_KINDS = frozenset({"power", "conduit", "high_energy_particle"})
_FUNCTIONAL_BUILDING_PORT_DIRECTIONS = frozenset({"input", "output"})
_FUNCTIONAL_BUILDING_CONDUIT_TYPES = frozenset({"gas", "liquid", "solid"})


def _functional_building_ports_payload_blockers(value: Any) -> list[str]:
    if not isinstance(value, Mapping):
        return ["functional_building_ports_type_mismatch"]
    blockers: list[str] = []
    if set(value) != _FUNCTIONAL_BUILDING_PORT_PAYLOAD_KEYS:
        blockers.append("functional_building_ports_payload_shape_invalid")
    if value.get("schema") != FUNCTIONAL_BUILDING_PORTS_SCHEMA:
        blockers.append("functional_building_ports_schema_invalid")
    if not isinstance(value.get("captured_at"), str) or parse_snapshot_timestamp(value["captured_at"]) is None:
        blockers.append("functional_building_ports_capture_invalid")
    world_id = value.get("world_id")
    if isinstance(world_id, bool) or not isinstance(world_id, int) or not 0 <= world_id <= _INT32_MAX:
        blockers.append("functional_building_ports_world_id_invalid")
    if value.get("complete") is not True:
        blockers.append("functional_building_ports_incomplete")
    instances = value.get("instances")
    if not isinstance(instances, list):
        return sorted(set(blockers + ["functional_building_ports_instances_invalid"]))
    if len(instances) > _FUNCTIONAL_BUILDING_GEOMETRY_MAX_INSTANCES:
        blockers.append("functional_building_ports_instances_too_many")
    seen_ids: set[int] = set()
    for instance in instances:
        if not isinstance(instance, Mapping) or set(instance) != _FUNCTIONAL_BUILDING_PORT_INSTANCE_KEYS:
            blockers.append("functional_building_ports_instance_shape_invalid")
            continue
        iid = instance.get("instance_id")
        if isinstance(iid, bool) or not isinstance(iid, int) or not 0 < iid <= _INT32_MAX:
            blockers.append("functional_building_ports_instance_id_invalid")
        elif iid in seen_ids:
            blockers.append("functional_building_ports_instance_id_duplicate")
        else:
            seen_ids.add(iid)
        iw = instance.get("world_id")
        if isinstance(iw, bool) or not isinstance(iw, int) or not 0 <= iw <= _INT32_MAX:
            blockers.append("functional_building_ports_instance_world_id_invalid")
        elif isinstance(world_id, int) and not isinstance(world_id, bool) and iw != world_id:
            blockers.append("functional_building_ports_instance_world_mismatch")
        if not isinstance(instance.get("prefab_id"), str) or not instance["prefab_id"] or len(instance["prefab_id"]) > 128:
            blockers.append("functional_building_ports_prefab_id_invalid")
        if not isinstance(instance.get("orientation"), str) or not instance["orientation"] or len(instance["orientation"]) > 128:
            blockers.append("functional_building_ports_orientation_invalid")
        ports = instance.get("ports")
        if not isinstance(ports, list):
            blockers.append("functional_building_ports_list_invalid")
            continue
        if len(ports) > 32:
            blockers.append("functional_building_ports_too_many")
        signatures: set[tuple[Any, ...]] = set()
        for port in ports:
            if not isinstance(port, Mapping) or set(port) != _FUNCTIONAL_BUILDING_PORT_KEYS:
                blockers.append("functional_building_port_shape_invalid")
                continue
            kind, direction, cell, ptype, status = (port.get(k) for k in ("kind", "direction", "cell", "type", "connection_status"))
            if kind not in _FUNCTIONAL_BUILDING_PORT_KINDS:
                blockers.append("functional_building_port_kind_invalid")
            if direction not in _FUNCTIONAL_BUILDING_PORT_DIRECTIONS:
                blockers.append("functional_building_port_direction_invalid")
            if isinstance(cell, bool) or not isinstance(cell, int) or not 0 <= cell <= _INT32_MAX:
                blockers.append("functional_building_port_cell_invalid")
            if status != "unknown":
                blockers.append("functional_building_port_connection_not_unknown")
            if kind == "conduit":
                if ptype not in _FUNCTIONAL_BUILDING_CONDUIT_TYPES:
                    blockers.append("functional_building_port_conduit_type_invalid")
            elif ptype is not None:
                blockers.append("functional_building_port_type_unexpected")
            signature = (kind, direction, cell, ptype)
            if signature in signatures:
                blockers.append("functional_building_port_duplicate")
            signatures.add(signature)
    return sorted(set(blockers))


def _functional_building_ports_state_binding_blockers(state: Mapping[str, Any], value: Any) -> list[str]:
    if not isinstance(value, Mapping):
        return []
    snapshot_world_id = state.get("world_id")
    blockers: list[str] = []
    if isinstance(snapshot_world_id, bool) or not isinstance(snapshot_world_id, int) or snapshot_world_id < 0:
        return ["snapshot_world_id_invalid"]
    if value.get("world_id") != snapshot_world_id:
        blockers.append("functional_building_ports_snapshot_world_mismatch")
    scope = state.get("observation_scope")
    if not isinstance(scope, Mapping):
        blockers.append("functional_building_ports_active_world_scope_missing")
    elif scope.get("active_world_id") != snapshot_world_id:
        blockers.append("functional_building_ports_snapshot_active_world_mismatch")
    return sorted(set(blockers))


_FUNCTIONAL_BUILDING_OCCUPANCY_INSTANCE_KEYS = frozenset(
    {"instance_id", "world_id", "prefab_id", "entombed", "occupied_cells"}
)
_FUNCTIONAL_BUILDING_OCCUPANCY_PAYLOAD_KEYS = frozenset(
    {"schema", "captured_at", "world_id", "complete", "instances"}
)
_FUNCTIONAL_BUILDING_OCCUPANCY_MAX_INSTANCES = 10_000
_FUNCTIONAL_BUILDING_OCCUPANCY_MAX_CELLS = 64


def _functional_building_occupancy_payload_blockers(value: Any) -> list[str]:
    """Validate optional entombment and native occupancy-cell snapshots."""

    if not isinstance(value, Mapping):
        return ["functional_building_occupancy_type_mismatch"]

    blockers: list[str] = []
    if set(value) != _FUNCTIONAL_BUILDING_OCCUPANCY_PAYLOAD_KEYS:
        blockers.append("functional_building_occupancy_payload_shape_invalid")
    if value.get("schema") != FUNCTIONAL_BUILDING_OCCUPANCY_SCHEMA:
        blockers.append("functional_building_occupancy_schema_invalid")
    captured_at = value.get("captured_at")
    if not isinstance(captured_at, str) or parse_snapshot_timestamp(captured_at) is None:
        blockers.append("functional_building_occupancy_capture_invalid")

    world_id = value.get("world_id")
    if (
        isinstance(world_id, bool)
        or not isinstance(world_id, int)
        or world_id < 0
        or world_id > _INT32_MAX
    ):
        blockers.append("functional_building_occupancy_world_id_invalid")
    if value.get("complete") is not True:
        blockers.append("functional_building_occupancy_incomplete")

    instances = value.get("instances")
    if not isinstance(instances, list):
        blockers.append("functional_building_occupancy_instances_invalid")
        return sorted(set(blockers))
    if len(instances) > _FUNCTIONAL_BUILDING_OCCUPANCY_MAX_INSTANCES:
        blockers.append("functional_building_occupancy_instances_too_many")

    seen_instance_ids: set[int] = set()
    for instance in instances:
        if not isinstance(instance, Mapping) or set(instance) != _FUNCTIONAL_BUILDING_OCCUPANCY_INSTANCE_KEYS:
            blockers.append("functional_building_occupancy_instance_shape_invalid")
            continue

        instance_id = instance.get("instance_id")
        if (
            isinstance(instance_id, bool)
            or not isinstance(instance_id, int)
            or instance_id <= 0
            or instance_id > _INT32_MAX
        ):
            blockers.append("functional_building_occupancy_instance_id_invalid")
        elif instance_id in seen_instance_ids:
            blockers.append("functional_building_occupancy_instance_id_duplicate")
        else:
            seen_instance_ids.add(instance_id)

        instance_world_id = instance.get("world_id")
        if (
            isinstance(instance_world_id, bool)
            or not isinstance(instance_world_id, int)
            or instance_world_id < 0
            or instance_world_id > _INT32_MAX
        ):
            blockers.append("functional_building_occupancy_instance_world_id_invalid")
        elif isinstance(world_id, int) and not isinstance(world_id, bool) and instance_world_id != world_id:
            blockers.append("functional_building_occupancy_instance_world_mismatch")

        prefab_id = instance.get("prefab_id")
        if not isinstance(prefab_id, str) or not prefab_id or len(prefab_id) > 128:
            blockers.append("functional_building_occupancy_prefab_id_invalid")

        entombed = instance.get("entombed")
        if entombed is not None and not isinstance(entombed, bool):
            blockers.append("functional_building_occupancy_entombed_type_invalid")

        occupied_cells = instance.get("occupied_cells")
        if occupied_cells is None:
            continue
        if not isinstance(occupied_cells, list):
            blockers.append("functional_building_occupancy_cells_invalid")
            continue
        if len(occupied_cells) > _FUNCTIONAL_BUILDING_OCCUPANCY_MAX_CELLS:
            blockers.append("functional_building_occupancy_cells_too_many")
        previous_cell: Optional[int] = None
        seen_cells: set[int] = set()
        for cell in occupied_cells:
            if (
                isinstance(cell, bool)
                or not isinstance(cell, int)
                or cell < 0
                or cell > _INT32_MAX
            ):
                blockers.append("functional_building_occupancy_cell_invalid")
                continue
            if cell in seen_cells:
                blockers.append("functional_building_occupancy_cell_duplicate")
            seen_cells.add(cell)
            if previous_cell is not None and cell <= previous_cell:
                blockers.append("functional_building_occupancy_cells_unsorted")
            previous_cell = cell

    return sorted(set(blockers))


def _functional_building_occupancy_state_binding_blockers(
    state: Mapping[str, Any], value: Any
) -> list[str]:
    """Require occupancy world identity to equal the snapshot active world."""

    if not isinstance(value, Mapping):
        return []
    snapshot_world_id = state.get("world_id")
    if (
        isinstance(snapshot_world_id, bool)
        or not isinstance(snapshot_world_id, int)
        or snapshot_world_id < 0
        or snapshot_world_id > _INT32_MAX
    ):
        return ["snapshot_world_id_invalid"]

    blockers: list[str] = []
    if value.get("world_id") != snapshot_world_id:
        blockers.append("functional_building_occupancy_snapshot_world_mismatch")
    observation_scope = state.get("observation_scope")
    if not isinstance(observation_scope, Mapping):
        blockers.append("functional_building_occupancy_active_world_scope_missing")
        return sorted(set(blockers))
    active_world_id = observation_scope.get("active_world_id")
    if (
        isinstance(active_world_id, bool)
        or not isinstance(active_world_id, int)
        or active_world_id < 0
        or active_world_id > _INT32_MAX
    ):
        blockers.append("functional_building_occupancy_active_world_id_invalid")
    elif active_world_id != snapshot_world_id:
        blockers.append("functional_building_occupancy_snapshot_active_world_mismatch")
    elif value.get("world_id") != active_world_id:
        blockers.append("functional_building_occupancy_active_world_mismatch")
    return sorted(set(blockers))


_FUNCTIONAL_BUILDING_HEALTH_INSTANCE_KEYS = frozenset(
    {"instance_id", "world_id", "prefab_id", "enabled", "hit_points", "max_hit_points", "is_broken", "needs_repairs"}
)
_FUNCTIONAL_BUILDING_HEALTH_PAYLOAD_KEYS = frozenset(
    {"schema", "captured_at", "world_id", "complete", "instances"}
)
_FUNCTIONAL_BUILDING_HEALTH_MAX_INSTANCES = 10_000


def _functional_building_health_payload_blockers(value: Any) -> list[str]:
    """Validate nullable native enabled/health fields for completed buildings."""

    if not isinstance(value, Mapping):
        return ["functional_building_health_type_mismatch"]

    blockers: list[str] = []
    if set(value) != _FUNCTIONAL_BUILDING_HEALTH_PAYLOAD_KEYS:
        blockers.append("functional_building_health_payload_shape_invalid")
    if value.get("schema") != FUNCTIONAL_BUILDING_HEALTH_SCHEMA:
        blockers.append("functional_building_health_schema_invalid")
    captured_at = value.get("captured_at")
    if not isinstance(captured_at, str) or parse_snapshot_timestamp(captured_at) is None:
        blockers.append("functional_building_health_capture_invalid")

    world_id = value.get("world_id")
    if isinstance(world_id, bool) or not isinstance(world_id, int) or world_id < 0 or world_id > _INT32_MAX:
        blockers.append("functional_building_health_world_id_invalid")
    if value.get("complete") is not True:
        blockers.append("functional_building_health_incomplete")

    instances = value.get("instances")
    if not isinstance(instances, list):
        blockers.append("functional_building_health_instances_invalid")
        return sorted(set(blockers))
    if len(instances) > _FUNCTIONAL_BUILDING_HEALTH_MAX_INSTANCES:
        blockers.append("functional_building_health_instances_too_many")

    seen_instance_ids: set[int] = set()
    for instance in instances:
        if not isinstance(instance, Mapping) or set(instance) != _FUNCTIONAL_BUILDING_HEALTH_INSTANCE_KEYS:
            blockers.append("functional_building_health_instance_shape_invalid")
            continue

        instance_id = instance.get("instance_id")
        if isinstance(instance_id, bool) or not isinstance(instance_id, int) or instance_id <= 0 or instance_id > _INT32_MAX:
            blockers.append("functional_building_health_instance_id_invalid")
        elif instance_id in seen_instance_ids:
            blockers.append("functional_building_health_instance_id_duplicate")
        else:
            seen_instance_ids.add(instance_id)

        instance_world_id = instance.get("world_id")
        if isinstance(instance_world_id, bool) or not isinstance(instance_world_id, int) or instance_world_id < 0 or instance_world_id > _INT32_MAX:
            blockers.append("functional_building_health_instance_world_id_invalid")
        elif isinstance(world_id, int) and not isinstance(world_id, bool) and instance_world_id != world_id:
            blockers.append("functional_building_health_instance_world_mismatch")

        prefab_id = instance.get("prefab_id")
        if not isinstance(prefab_id, str) or not prefab_id or len(prefab_id) > 128:
            blockers.append("functional_building_health_prefab_id_invalid")

        for field in ("enabled", "is_broken", "needs_repairs"):
            field_value = instance.get(field)
            if field_value is not None and not isinstance(field_value, bool):
                blockers.append(f"functional_building_health_{field}_type_invalid")
        for field in ("hit_points", "max_hit_points"):
            field_value = instance.get(field)
            if field_value is not None and (
                isinstance(field_value, bool) or not isinstance(field_value, int) or field_value < 0 or field_value > _INT32_MAX
            ):
                blockers.append(f"functional_building_health_{field}_type_invalid")
        hit_points = instance.get("hit_points")
        max_hit_points = instance.get("max_hit_points")
        if isinstance(hit_points, int) and not isinstance(hit_points, bool) and isinstance(max_hit_points, int) and not isinstance(max_hit_points, bool) and hit_points > max_hit_points:
            blockers.append("functional_building_health_hit_points_exceed_max")

    return sorted(set(blockers))


def _functional_building_health_state_binding_blockers(
    state: Mapping[str, Any], value: Any
) -> list[str]:
    """Require health world identity to equal the snapshot active world."""

    if not isinstance(value, Mapping):
        return []
    snapshot_world_id = state.get("world_id")
    if isinstance(snapshot_world_id, bool) or not isinstance(snapshot_world_id, int) or snapshot_world_id < 0 or snapshot_world_id > _INT32_MAX:
        return ["snapshot_world_id_invalid"]

    blockers: list[str] = []
    if value.get("world_id") != snapshot_world_id:
        blockers.append("functional_building_health_snapshot_world_mismatch")
    observation_scope = state.get("observation_scope")
    if not isinstance(observation_scope, Mapping):
        blockers.append("functional_building_health_active_world_scope_missing")
        return sorted(set(blockers))
    active_world_id = observation_scope.get("active_world_id")
    if isinstance(active_world_id, bool) or not isinstance(active_world_id, int) or active_world_id < 0 or active_world_id > _INT32_MAX:
        blockers.append("functional_building_health_active_world_id_invalid")
    elif active_world_id != snapshot_world_id:
        blockers.append("functional_building_health_snapshot_active_world_mismatch")
    elif value.get("world_id") != active_world_id:
        blockers.append("functional_building_health_active_world_mismatch")
    return sorted(set(blockers))


_FUNCTIONAL_BUILDING_FLOODED_INSTANCE_KEYS = frozenset(
    {"instance_id", "world_id", "prefab_id", "is_flooded"}
)
_FUNCTIONAL_BUILDING_FLOODED_PAYLOAD_KEYS = frozenset(
    {"schema", "captured_at", "world_id", "complete", "instances"}
)
_FUNCTIONAL_BUILDING_FLOODED_MAX_INSTANCES = 10_000


def _functional_building_flooded_payload_blockers(value: Any) -> list[str]:
    """Validate nullable native Floodable.IsFlooded snapshots."""

    if not isinstance(value, Mapping):
        return ["functional_building_flooded_type_mismatch"]

    blockers: list[str] = []
    if set(value) != _FUNCTIONAL_BUILDING_FLOODED_PAYLOAD_KEYS:
        blockers.append("functional_building_flooded_payload_shape_invalid")
    if value.get("schema") != FUNCTIONAL_BUILDING_FLOODED_SCHEMA:
        blockers.append("functional_building_flooded_schema_invalid")
    captured_at = value.get("captured_at")
    if not isinstance(captured_at, str) or parse_snapshot_timestamp(captured_at) is None:
        blockers.append("functional_building_flooded_capture_invalid")

    world_id = value.get("world_id")
    if (
        isinstance(world_id, bool)
        or not isinstance(world_id, int)
        or world_id < 0
        or world_id > _INT32_MAX
    ):
        blockers.append("functional_building_flooded_world_id_invalid")
    if value.get("complete") is not True:
        blockers.append("functional_building_flooded_incomplete")

    instances = value.get("instances")
    if not isinstance(instances, list):
        blockers.append("functional_building_flooded_instances_invalid")
        return sorted(set(blockers))
    if len(instances) > _FUNCTIONAL_BUILDING_FLOODED_MAX_INSTANCES:
        blockers.append("functional_building_flooded_instances_too_many")

    seen_instance_ids: set[int] = set()
    for instance in instances:
        if not isinstance(instance, Mapping) or set(instance) != _FUNCTIONAL_BUILDING_FLOODED_INSTANCE_KEYS:
            blockers.append("functional_building_flooded_instance_shape_invalid")
            continue

        instance_id = instance.get("instance_id")
        if (
            isinstance(instance_id, bool)
            or not isinstance(instance_id, int)
            or instance_id <= 0
            or instance_id > _INT32_MAX
        ):
            blockers.append("functional_building_flooded_instance_id_invalid")
        elif instance_id in seen_instance_ids:
            blockers.append("functional_building_flooded_instance_id_duplicate")
        else:
            seen_instance_ids.add(instance_id)

        instance_world_id = instance.get("world_id")
        if (
            isinstance(instance_world_id, bool)
            or not isinstance(instance_world_id, int)
            or instance_world_id < 0
            or instance_world_id > _INT32_MAX
        ):
            blockers.append("functional_building_flooded_instance_world_id_invalid")
        elif isinstance(world_id, int) and not isinstance(world_id, bool) and instance_world_id != world_id:
            blockers.append("functional_building_flooded_instance_world_mismatch")

        prefab_id = instance.get("prefab_id")
        if not isinstance(prefab_id, str) or not prefab_id or len(prefab_id) > 128:
            blockers.append("functional_building_flooded_prefab_id_invalid")

        is_flooded = instance.get("is_flooded")
        if is_flooded is not None and not isinstance(is_flooded, bool):
            blockers.append("functional_building_flooded_value_type_invalid")

    return sorted(set(blockers))


def _functional_building_flooded_state_binding_blockers(
    state: Mapping[str, Any], value: Any
) -> list[str]:
    """Require flooded world identity to equal the snapshot active world."""

    if not isinstance(value, Mapping):
        return []
    snapshot_world_id = state.get("world_id")
    if (
        isinstance(snapshot_world_id, bool)
        or not isinstance(snapshot_world_id, int)
        or snapshot_world_id < 0
        or snapshot_world_id > _INT32_MAX
    ):
        return ["snapshot_world_id_invalid"]

    blockers: list[str] = []
    if value.get("world_id") != snapshot_world_id:
        blockers.append("functional_building_flooded_snapshot_world_mismatch")
    observation_scope = state.get("observation_scope")
    if not isinstance(observation_scope, Mapping):
        blockers.append("functional_building_flooded_active_world_scope_missing")
        return sorted(set(blockers))
    active_world_id = observation_scope.get("active_world_id")
    if (
        isinstance(active_world_id, bool)
        or not isinstance(active_world_id, int)
        or active_world_id < 0
        or active_world_id > _INT32_MAX
    ):
        blockers.append("functional_building_flooded_active_world_id_invalid")
    elif active_world_id != snapshot_world_id:
        blockers.append("functional_building_flooded_snapshot_active_world_mismatch")
    elif value.get("world_id") != active_world_id:
        blockers.append("functional_building_flooded_active_world_mismatch")
    return sorted(set(blockers))


_FUNCTIONAL_BUILDING_REACHABILITY_INSTANCE_KEYS = frozenset(
    {"instance_id", "world_id", "prefab_id", "is_reachable"}
)
_FUNCTIONAL_BUILDING_REACHABILITY_PAYLOAD_KEYS = frozenset(
    {"schema", "captured_at", "world_id", "complete", "instances"}
)
_FUNCTIONAL_BUILDING_REACHABILITY_MAX_INSTANCES = 10000


def _functional_building_reachability_payload_blockers(value: Any) -> list[str]:
    if not isinstance(value, Mapping):
        return ["functional_building_reachability_type_mismatch"]
    blockers: list[str] = []
    if set(value) != _FUNCTIONAL_BUILDING_REACHABILITY_PAYLOAD_KEYS:
        blockers.append("functional_building_reachability_payload_shape_invalid")
    if value.get("schema") != FUNCTIONAL_BUILDING_REACHABILITY_SCHEMA:
        blockers.append("functional_building_reachability_schema_invalid")
    if not isinstance(value.get("captured_at"), str) or parse_snapshot_timestamp(value["captured_at"]) is None:
        blockers.append("functional_building_reachability_capture_invalid")
    world_id = value.get("world_id")
    if isinstance(world_id, bool) or not isinstance(world_id, int) or world_id < 0 or world_id > _INT32_MAX:
        blockers.append("functional_building_reachability_world_id_invalid")
    if value.get("complete") is not True:
        blockers.append("functional_building_reachability_incomplete")
    instances = value.get("instances")
    if not isinstance(instances, list):
        return sorted(set(blockers + ["functional_building_reachability_instances_invalid"]))
    if len(instances) > _FUNCTIONAL_BUILDING_REACHABILITY_MAX_INSTANCES:
        blockers.append("functional_building_reachability_instances_too_many")
    seen: set[int] = set()
    for instance in instances:
        if not isinstance(instance, Mapping) or set(instance) != _FUNCTIONAL_BUILDING_REACHABILITY_INSTANCE_KEYS:
            blockers.append("functional_building_reachability_instance_shape_invalid")
            continue
        instance_id = instance.get("instance_id")
        if isinstance(instance_id, bool) or not isinstance(instance_id, int) or instance_id <= 0 or instance_id > _INT32_MAX:
            blockers.append("functional_building_reachability_instance_id_invalid")
        elif instance_id in seen:
            blockers.append("functional_building_reachability_instance_id_duplicate")
        else:
            seen.add(instance_id)
        iw = instance.get("world_id")
        if isinstance(iw, bool) or not isinstance(iw, int) or iw < 0 or iw > _INT32_MAX:
            blockers.append("functional_building_reachability_instance_world_id_invalid")
        elif isinstance(world_id, int) and not isinstance(world_id, bool) and iw != world_id:
            blockers.append("functional_building_reachability_instance_world_mismatch")
        prefab = instance.get("prefab_id")
        if not isinstance(prefab, str) or not prefab or len(prefab) > 128:
            blockers.append("functional_building_reachability_prefab_id_invalid")
        reachable = instance.get("is_reachable")
        if reachable is not None and not isinstance(reachable, bool):
            blockers.append("functional_building_reachability_value_type_invalid")
    return sorted(set(blockers))


def _functional_building_reachability_state_binding_blockers(state: Mapping[str, Any], value: Any) -> list[str]:
    if not isinstance(value, Mapping):
        return []
    snapshot_world_id = state.get("world_id")
    if isinstance(snapshot_world_id, bool) or not isinstance(snapshot_world_id, int) or snapshot_world_id < 0 or snapshot_world_id > _INT32_MAX:
        return ["snapshot_world_id_invalid"]
    blockers: list[str] = []
    if value.get("world_id") != snapshot_world_id:
        blockers.append("functional_building_reachability_snapshot_world_mismatch")
    scope = state.get("observation_scope")
    if not isinstance(scope, Mapping):
        return sorted(set(blockers + ["functional_building_reachability_active_world_scope_missing"]))
    active = scope.get("active_world_id")
    if isinstance(active, bool) or not isinstance(active, int) or active < 0 or active > _INT32_MAX:
        blockers.append("functional_building_reachability_active_world_id_invalid")
    elif active != snapshot_world_id:
        blockers.append("functional_building_reachability_snapshot_active_world_mismatch")
    elif value.get("world_id") != active:
        blockers.append("functional_building_reachability_active_world_mismatch")
    return sorted(set(blockers))


_FUNCTIONAL_BUILDING_POWER_NETWORK_INSTANCE_KEYS = frozenset(
    {"instance_id", "world_id", "prefab_id", "is_connected", "network_id"}
)
_FUNCTIONAL_BUILDING_POWER_NETWORK_PAYLOAD_KEYS = frozenset(
    {"schema", "captured_at", "world_id", "complete", "instances"}
)
_FUNCTIONAL_BUILDING_POWER_NETWORK_MAX_INSTANCES = 10000


def _functional_building_power_network_payload_blockers(value: Any) -> list[str]:
    if not isinstance(value, Mapping):
        return ["functional_building_power_network_type_mismatch"]
    blockers: list[str] = []
    if set(value) != _FUNCTIONAL_BUILDING_POWER_NETWORK_PAYLOAD_KEYS:
        blockers.append("functional_building_power_network_payload_shape_invalid")
    if value.get("schema") != FUNCTIONAL_BUILDING_POWER_NETWORK_SCHEMA:
        blockers.append("functional_building_power_network_schema_invalid")
    if not isinstance(value.get("captured_at"), str) or parse_snapshot_timestamp(value["captured_at"]) is None:
        blockers.append("functional_building_power_network_capture_invalid")
    world_id = value.get("world_id")
    if isinstance(world_id, bool) or not isinstance(world_id, int) or world_id < 0 or world_id > _INT32_MAX:
        blockers.append("functional_building_power_network_world_id_invalid")
    if value.get("complete") is not True:
        blockers.append("functional_building_power_network_incomplete")
    instances = value.get("instances")
    if not isinstance(instances, list):
        return sorted(set(blockers + ["functional_building_power_network_instances_invalid"]))
    if len(instances) > _FUNCTIONAL_BUILDING_POWER_NETWORK_MAX_INSTANCES:
        blockers.append("functional_building_power_network_instances_too_many")
    seen: set[int] = set()
    for instance in instances:
        if not isinstance(instance, Mapping) or set(instance) != _FUNCTIONAL_BUILDING_POWER_NETWORK_INSTANCE_KEYS:
            blockers.append("functional_building_power_network_instance_shape_invalid")
            continue
        instance_id = instance.get("instance_id")
        if isinstance(instance_id, bool) or not isinstance(instance_id, int) or instance_id <= 0 or instance_id > _INT32_MAX:
            blockers.append("functional_building_power_network_instance_id_invalid")
        elif instance_id in seen:
            blockers.append("functional_building_power_network_instance_id_duplicate")
        else:
            seen.add(instance_id)
        iw = instance.get("world_id")
        if isinstance(iw, bool) or not isinstance(iw, int) or iw < 0 or iw > _INT32_MAX:
            blockers.append("functional_building_power_network_instance_world_id_invalid")
        elif isinstance(world_id, int) and not isinstance(world_id, bool) and iw != world_id:
            blockers.append("functional_building_power_network_instance_world_mismatch")
        prefab = instance.get("prefab_id")
        if not isinstance(prefab, str) or not prefab or len(prefab) > 128:
            blockers.append("functional_building_power_network_prefab_id_invalid")
        connected = instance.get("is_connected")
        if connected is not None and not isinstance(connected, bool):
            blockers.append("functional_building_power_network_connected_type_invalid")
        network_id = instance.get("network_id")
        if network_id is not None and (isinstance(network_id, bool) or not isinstance(network_id, int) or network_id < 0 or network_id > 65535):
            blockers.append("functional_building_power_network_id_invalid")
        if connected is not True and network_id is not None:
            blockers.append("functional_building_power_network_id_without_connection")
    return sorted(set(blockers))


def _functional_building_power_network_state_binding_blockers(state: Mapping[str, Any], value: Any) -> list[str]:
    if not isinstance(value, Mapping):
        return []
    snapshot_world_id = state.get("world_id")
    if isinstance(snapshot_world_id, bool) or not isinstance(snapshot_world_id, int) or snapshot_world_id < 0 or snapshot_world_id > _INT32_MAX:
        return ["snapshot_world_id_invalid"]
    blockers: list[str] = []
    if value.get("world_id") != snapshot_world_id:
        blockers.append("functional_building_power_network_snapshot_world_mismatch")
    scope = state.get("observation_scope")
    if not isinstance(scope, Mapping):
        return sorted(set(blockers + ["functional_building_power_network_active_world_scope_missing"]))
    active = scope.get("active_world_id")
    if isinstance(active, bool) or not isinstance(active, int) or active < 0 or active > _INT32_MAX:
        blockers.append("functional_building_power_network_active_world_id_invalid")
    elif active != snapshot_world_id:
        blockers.append("functional_building_power_network_snapshot_active_world_mismatch")
    elif value.get("world_id") != active:
        blockers.append("functional_building_power_network_active_world_mismatch")
    return sorted(set(blockers))


_FUNCTIONAL_BUILDING_UTILITY_NETWORK_INSTANCE_KEYS = frozenset(
    {"instance_id", "world_id", "prefab_id", "input_declared", "input_network_id", "output_declared", "output_network_id"}
)
_FUNCTIONAL_BUILDING_UTILITY_NETWORK_PAYLOAD_KEYS = frozenset(
    {"schema", "captured_at", "world_id", "complete", "instances"}
)


def _functional_building_utility_network_payload_blockers(value: Any) -> list[str]:
    if not isinstance(value, Mapping):
        return ["functional_building_utility_network_type_mismatch"]
    blockers: list[str] = []
    if set(value) != _FUNCTIONAL_BUILDING_UTILITY_NETWORK_PAYLOAD_KEYS:
        blockers.append("functional_building_utility_network_payload_shape_invalid")
    if value.get("schema") != FUNCTIONAL_BUILDING_UTILITY_NETWORK_SCHEMA:
        blockers.append("functional_building_utility_network_schema_invalid")
    if not isinstance(value.get("captured_at"), str) or parse_snapshot_timestamp(value["captured_at"]) is None:
        blockers.append("functional_building_utility_network_capture_invalid")
    world_id = value.get("world_id")
    if isinstance(world_id, bool) or not isinstance(world_id, int) or world_id < 0 or world_id > _INT32_MAX:
        blockers.append("functional_building_utility_network_world_id_invalid")
    if value.get("complete") is not True:
        blockers.append("functional_building_utility_network_incomplete")
    instances = value.get("instances")
    if not isinstance(instances, list):
        return sorted(set(blockers + ["functional_building_utility_network_instances_invalid"]))
    if len(instances) > 10000:
        blockers.append("functional_building_utility_network_instances_too_many")
    seen: set[int] = set()
    for instance in instances:
        if not isinstance(instance, Mapping) or set(instance) != _FUNCTIONAL_BUILDING_UTILITY_NETWORK_INSTANCE_KEYS:
            blockers.append("functional_building_utility_network_instance_shape_invalid")
            continue
        instance_id = instance.get("instance_id")
        if isinstance(instance_id, bool) or not isinstance(instance_id, int) or instance_id <= 0 or instance_id > _INT32_MAX:
            blockers.append("functional_building_utility_network_instance_id_invalid")
        elif instance_id in seen:
            blockers.append("functional_building_utility_network_instance_id_duplicate")
        else:
            seen.add(instance_id)
        iw = instance.get("world_id")
        if isinstance(iw, bool) or not isinstance(iw, int) or iw < 0 or iw > _INT32_MAX:
            blockers.append("functional_building_utility_network_instance_world_id_invalid")
        elif isinstance(world_id, int) and not isinstance(world_id, bool) and iw != world_id:
            blockers.append("functional_building_utility_network_instance_world_mismatch")
        prefab = instance.get("prefab_id")
        if not isinstance(prefab, str) or not prefab or len(prefab) > 128:
            blockers.append("functional_building_utility_network_prefab_id_invalid")
        for direction in ("input", "output"):
            declared = instance.get(direction + "_declared")
            network_id = instance.get(direction + "_network_id")
            if not isinstance(declared, bool):
                blockers.append("functional_building_utility_network_declared_type_invalid")
            if network_id is not None and (isinstance(network_id, bool) or not isinstance(network_id, int) or network_id < 0 or network_id > _INT32_MAX):
                blockers.append("functional_building_utility_network_id_invalid")
            if declared is not True and network_id is not None:
                blockers.append("functional_building_utility_network_id_without_port")
    return sorted(set(blockers))


def _functional_building_utility_network_state_binding_blockers(state: Mapping[str, Any], value: Any) -> list[str]:
    if not isinstance(value, Mapping):
        return []
    snapshot_world_id = state.get("world_id")
    if isinstance(snapshot_world_id, bool) or not isinstance(snapshot_world_id, int) or snapshot_world_id < 0 or snapshot_world_id > _INT32_MAX:
        return ["snapshot_world_id_invalid"]
    blockers: list[str] = []
    if value.get("world_id") != snapshot_world_id:
        blockers.append("functional_building_utility_network_snapshot_world_mismatch")
    scope = state.get("observation_scope")
    if not isinstance(scope, Mapping):
        return sorted(set(blockers + ["functional_building_utility_network_active_world_scope_missing"]))
    active = scope.get("active_world_id")
    if isinstance(active, bool) or not isinstance(active, int) or active < 0 or active > _INT32_MAX:
        blockers.append("functional_building_utility_network_active_world_id_invalid")
    elif active != snapshot_world_id:
        blockers.append("functional_building_utility_network_snapshot_active_world_mismatch")
    elif value.get("world_id") != active:
        blockers.append("functional_building_utility_network_active_world_mismatch")
    return sorted(set(blockers))


_FUNCTIONAL_BUILDING_UTILITY_ENDPOINT_INSTANCE_KEYS = frozenset(
    {"instance_id", "world_id", "prefab_id", "input", "output"}
)
_FUNCTIONAL_BUILDING_UTILITY_ENDPOINT_SIDE_KEYS = frozenset(
    {"declared", "cell", "registered", "endpoint_type", "conduit_type"}
)


def _functional_building_utility_endpoint_payload_blockers(value: Any) -> list[str]:
    if not isinstance(value, Mapping):
        return ["functional_building_utility_endpoint_type_mismatch"]
    blockers: list[str] = []
    expected = frozenset({"schema", "captured_at", "world_id", "complete", "instances"})
    if set(value) != expected:
        blockers.append("functional_building_utility_endpoint_payload_shape_invalid")
    if value.get("schema") != FUNCTIONAL_BUILDING_UTILITY_ENDPOINT_SCHEMA:
        blockers.append("functional_building_utility_endpoint_schema_invalid")
    if not isinstance(value.get("captured_at"), str) or parse_snapshot_timestamp(value["captured_at"]) is None:
        blockers.append("functional_building_utility_endpoint_capture_invalid")
    world_id = value.get("world_id")
    if isinstance(world_id, bool) or not isinstance(world_id, int) or world_id < 0 or world_id > _INT32_MAX:
        blockers.append("functional_building_utility_endpoint_world_id_invalid")
    if value.get("complete") is not True:
        blockers.append("functional_building_utility_endpoint_incomplete")
    instances = value.get("instances")
    if not isinstance(instances, list):
        return sorted(set(blockers + ["functional_building_utility_endpoint_instances_invalid"]))
    if len(instances) > 10000:
        blockers.append("functional_building_utility_endpoint_instances_too_many")
    seen: set[int] = set()
    for instance in instances:
        if not isinstance(instance, Mapping) or set(instance) != _FUNCTIONAL_BUILDING_UTILITY_ENDPOINT_INSTANCE_KEYS:
            blockers.append("functional_building_utility_endpoint_instance_shape_invalid")
            continue
        iid = instance.get("instance_id")
        if isinstance(iid, bool) or not isinstance(iid, int) or iid <= 0 or iid > _INT32_MAX:
            blockers.append("functional_building_utility_endpoint_instance_id_invalid")
        elif iid in seen:
            blockers.append("functional_building_utility_endpoint_instance_id_duplicate")
        else:
            seen.add(iid)
        iw = instance.get("world_id")
        if isinstance(iw, bool) or not isinstance(iw, int) or iw < 0 or iw > _INT32_MAX:
            blockers.append("functional_building_utility_endpoint_instance_world_id_invalid")
        elif isinstance(world_id, int) and iw != world_id:
            blockers.append("functional_building_utility_endpoint_instance_world_mismatch")
        if not isinstance(instance.get("prefab_id"), str) or not instance["prefab_id"] or len(instance["prefab_id"]) > 128:
            blockers.append("functional_building_utility_endpoint_prefab_id_invalid")
        for side_name in ("input", "output"):
            side = instance.get(side_name)
            if not isinstance(side, Mapping) or set(side) != _FUNCTIONAL_BUILDING_UTILITY_ENDPOINT_SIDE_KEYS:
                blockers.append("functional_building_utility_endpoint_side_shape_invalid")
                continue
            declared = side.get("declared")
            if not isinstance(declared, bool):
                blockers.append("functional_building_utility_endpoint_declared_invalid")
            cell = side.get("cell")
            if cell is not None and (isinstance(cell, bool) or not isinstance(cell, int) or cell < 0 or cell > _INT32_MAX):
                blockers.append("functional_building_utility_endpoint_cell_invalid")
            if declared is True and cell is None:
                blockers.append("functional_building_utility_endpoint_declared_cell_missing")
            if declared is False and any(side.get(k) is not None for k in ("cell", "registered", "endpoint_type", "conduit_type")):
                blockers.append("functional_building_utility_endpoint_undeclared_fields_present")
            registered = side.get("registered")
            if registered is not None and not isinstance(registered, bool):
                blockers.append("functional_building_utility_endpoint_registered_invalid")
            for key in ("endpoint_type", "conduit_type"):
                if side.get(key) is not None and (not isinstance(side.get(key), str) or not side.get(key)):
                    blockers.append("functional_building_utility_endpoint_enum_invalid")
    return sorted(set(blockers))


def _functional_building_utility_endpoint_state_binding_blockers(state: Mapping[str, Any], value: Any) -> list[str]:
    if not isinstance(value, Mapping):
        return []
    snapshot_world_id = state.get("world_id")
    if isinstance(snapshot_world_id, bool) or not isinstance(snapshot_world_id, int) or snapshot_world_id < 0 or snapshot_world_id > _INT32_MAX:
        return ["snapshot_world_id_invalid"]
    blockers: list[str] = []
    if value.get("world_id") != snapshot_world_id:
        blockers.append("functional_building_utility_endpoint_snapshot_world_mismatch")
    scope = state.get("observation_scope")
    if not isinstance(scope, Mapping):
        return sorted(set(blockers + ["functional_building_utility_endpoint_active_world_scope_missing"]))
    active = scope.get("active_world_id")
    if isinstance(active, bool) or not isinstance(active, int) or active < 0 or active > _INT32_MAX:
        blockers.append("functional_building_utility_endpoint_active_world_id_invalid")
    elif active != snapshot_world_id:
        blockers.append("functional_building_utility_endpoint_snapshot_active_world_mismatch")
    elif value.get("world_id") != active:
        blockers.append("functional_building_utility_endpoint_active_world_mismatch")
    return sorted(set(blockers))

_FUNCTIONAL_BUILDING_UTILITY_TOPOLOGY_INSTANCE_KEYS = frozenset({"instance_id", "world_id", "prefab_id", "input", "output"})
_FUNCTIONAL_BUILDING_UTILITY_TOPOLOGY_SIDE_KEYS = frozenset({"declared", "cell", "network_id", "source_count", "sink_count", "conduit_count", "has_sinks"})

def _functional_building_utility_topology_payload_blockers(value: Any) -> list[str]:
    if not isinstance(value, Mapping): return ["functional_building_utility_topology_type_mismatch"]
    b: list[str] = []; expected = frozenset({"schema", "captured_at", "world_id", "complete", "instances"})
    if set(value) != expected: b.append("functional_building_utility_topology_payload_shape_invalid")
    if value.get("schema") != FUNCTIONAL_BUILDING_UTILITY_TOPOLOGY_SCHEMA: b.append("functional_building_utility_topology_schema_invalid")
    if not isinstance(value.get("captured_at"), str) or parse_snapshot_timestamp(value["captured_at"]) is None: b.append("functional_building_utility_topology_capture_invalid")
    w=value.get("world_id")
    if isinstance(w,bool) or not isinstance(w,int) or w<0 or w>_INT32_MAX: b.append("functional_building_utility_topology_world_id_invalid")
    if value.get("complete") is not True: b.append("functional_building_utility_topology_incomplete")
    xs=value.get("instances")
    if not isinstance(xs,list): return sorted(set(b+["functional_building_utility_topology_instances_invalid"]))
    if len(xs)>10000: b.append("functional_building_utility_topology_instances_too_many")
    seen:set[int]=set()
    for x in xs:
        if not isinstance(x,Mapping) or set(x)!=_FUNCTIONAL_BUILDING_UTILITY_TOPOLOGY_INSTANCE_KEYS: b.append("functional_building_utility_topology_instance_shape_invalid"); continue
        iid=x.get("instance_id")
        if isinstance(iid,bool) or not isinstance(iid,int) or iid<=0 or iid>_INT32_MAX: b.append("functional_building_utility_topology_instance_id_invalid")
        elif iid in seen: b.append("functional_building_utility_topology_instance_id_duplicate")
        else: seen.add(iid)
        iw=x.get("world_id")
        if isinstance(iw,bool) or not isinstance(iw,int) or iw<0 or iw>_INT32_MAX: b.append("functional_building_utility_topology_instance_world_id_invalid")
        elif isinstance(w,int) and iw!=w: b.append("functional_building_utility_topology_instance_world_mismatch")
        if not isinstance(x.get("prefab_id"),str) or not x["prefab_id"] or len(x["prefab_id"])>128: b.append("functional_building_utility_topology_prefab_id_invalid")
        for name in ("input","output"):
            s=x.get(name)
            if not isinstance(s,Mapping) or set(s)!=_FUNCTIONAL_BUILDING_UTILITY_TOPOLOGY_SIDE_KEYS: b.append("functional_building_utility_topology_side_shape_invalid"); continue
            d=s.get("declared")
            if not isinstance(d,bool): b.append("functional_building_utility_topology_declared_invalid")
            for key in ("network_id","source_count","sink_count","conduit_count"):
                z=s.get(key)
                if z is not None and (isinstance(z,bool) or not isinstance(z,int) or z<0 or z>_INT32_MAX): b.append("functional_building_utility_topology_count_invalid")
            if not isinstance(s.get("has_sinks"),bool) and s.get("has_sinks") is not None: b.append("functional_building_utility_topology_has_sinks_invalid")
            cell=s.get("cell")
            if cell is not None and (isinstance(cell,bool) or not isinstance(cell,int) or cell<0 or cell>_INT32_MAX): b.append("functional_building_utility_topology_cell_invalid")
            if d is True and cell is None: b.append("functional_building_utility_topology_declared_cell_missing")
            if d is False and any(s.get(k) is not None for k in _FUNCTIONAL_BUILDING_UTILITY_TOPOLOGY_SIDE_KEYS-{"declared"}): b.append("functional_building_utility_topology_undeclared_fields_present")
    return sorted(set(b))

def _functional_building_utility_topology_state_binding_blockers(state: Mapping[str, Any], value: Any) -> list[str]:
    if not isinstance(value,Mapping): return []
    sw=state.get("world_id")
    if isinstance(sw,bool) or not isinstance(sw,int) or sw<0 or sw>_INT32_MAX: return ["snapshot_world_id_invalid"]
    b=[]
    if value.get("world_id")!=sw: b.append("functional_building_utility_topology_snapshot_world_mismatch")
    scope=state.get("observation_scope")
    if not isinstance(scope,Mapping): return sorted(set(b+["functional_building_utility_topology_active_world_scope_missing"]))
    a=scope.get("active_world_id")
    if isinstance(a,bool) or not isinstance(a,int) or a<0 or a>_INT32_MAX: b.append("functional_building_utility_topology_active_world_id_invalid")
    elif a!=sw: b.append("functional_building_utility_topology_snapshot_active_world_mismatch")
    elif value.get("world_id")!=a: b.append("functional_building_utility_topology_active_world_mismatch")
    return sorted(set(b))

_FUNCTIONAL_BUILDING_UTILITY_CONTENTS_INSTANCE_KEYS = frozenset({"instance_id", "world_id", "prefab_id", "input", "output"})
_FUNCTIONAL_BUILDING_UTILITY_CONTENTS_SIDE_KEYS = frozenset({"declared", "cell", "content_kind", "element", "mass", "temperature", "disease_count", "solid_present"})
def _functional_building_utility_contents_payload_blockers(value: Any) -> list[str]:
    if not isinstance(value, Mapping): return ["functional_building_utility_contents_type_mismatch"]
    b=[]; expected=frozenset({"schema","captured_at","world_id","complete","instances"})
    if set(value)!=expected: b.append("functional_building_utility_contents_payload_shape_invalid")
    if value.get("schema")!=FUNCTIONAL_BUILDING_UTILITY_CONTENTS_SCHEMA: b.append("functional_building_utility_contents_schema_invalid")
    if not isinstance(value.get("captured_at"),str) or parse_snapshot_timestamp(value["captured_at"]) is None: b.append("functional_building_utility_contents_capture_invalid")
    w=value.get("world_id")
    if isinstance(w,bool) or not isinstance(w,int) or w<0 or w>_INT32_MAX: b.append("functional_building_utility_contents_world_id_invalid")
    if value.get("complete") is not True: b.append("functional_building_utility_contents_incomplete")
    xs=value.get("instances")
    if not isinstance(xs,list): return sorted(set(b+["functional_building_utility_contents_instances_invalid"]))
    if len(xs)>10000: b.append("functional_building_utility_contents_instances_too_many")
    seen=set()
    for x in xs:
        if not isinstance(x,Mapping) or set(x)!=_FUNCTIONAL_BUILDING_UTILITY_CONTENTS_INSTANCE_KEYS: b.append("functional_building_utility_contents_instance_shape_invalid"); continue
        iid=x.get("instance_id")
        if isinstance(iid,bool) or not isinstance(iid,int) or iid<=0 or iid>_INT32_MAX: b.append("functional_building_utility_contents_instance_id_invalid")
        elif iid in seen: b.append("functional_building_utility_contents_instance_id_duplicate")
        else: seen.add(iid)
        iw=x.get("world_id")
        if isinstance(iw,bool) or not isinstance(iw,int) or iw<0 or iw>_INT32_MAX: b.append("functional_building_utility_contents_instance_world_id_invalid")
        elif isinstance(w,int) and iw!=w: b.append("functional_building_utility_contents_instance_world_mismatch")
        if not isinstance(x.get("prefab_id"),str) or not x["prefab_id"]: b.append("functional_building_utility_contents_prefab_id_invalid")
        for name in ("input","output"):
            s=x.get(name)
            if not isinstance(s,Mapping) or set(s)!=_FUNCTIONAL_BUILDING_UTILITY_CONTENTS_SIDE_KEYS: b.append("functional_building_utility_contents_side_shape_invalid"); continue
            d=s.get("declared"); kind=s.get("content_kind")
            if not isinstance(d,bool): b.append("functional_building_utility_contents_declared_invalid")
            if kind is not None and kind not in ("gas","liquid","solid"): b.append("functional_building_utility_contents_kind_invalid")
            cell=s.get("cell")
            if cell is not None and (isinstance(cell,bool) or not isinstance(cell,int) or cell<0 or cell>_INT32_MAX): b.append("functional_building_utility_contents_cell_invalid")
            if d is True and cell is None: b.append("functional_building_utility_contents_declared_cell_missing")
            for key in ("mass","temperature"):
                z=s.get(key)
                if z is not None and (_finite_float(z) is None or (key=="mass" and z<0)): b.append("functional_building_utility_contents_numeric_invalid")
            dc=s.get("disease_count")
            if dc is not None and (isinstance(dc,bool) or not isinstance(dc,int) or dc<0): b.append("functional_building_utility_contents_disease_invalid")
            sp=s.get("solid_present")
            if sp is not None and not isinstance(sp,bool): b.append("functional_building_utility_contents_solid_invalid")
    return sorted(set(b))
def _functional_building_utility_contents_state_binding_blockers(state: Mapping[str, Any], value: Any) -> list[str]:
    if not isinstance(value,Mapping): return []
    sw=state.get("world_id")
    if isinstance(sw,bool) or not isinstance(sw,int) or sw<0 or sw>_INT32_MAX: return ["snapshot_world_id_invalid"]
    b=[]
    if value.get("world_id")!=sw: b.append("functional_building_utility_contents_snapshot_world_mismatch")
    scope=state.get("observation_scope")
    if not isinstance(scope,Mapping): return sorted(set(b+["functional_building_utility_contents_active_world_scope_missing"]))
    a=scope.get("active_world_id")
    if isinstance(a,bool) or not isinstance(a,int) or a<0 or a>_INT32_MAX: b.append("functional_building_utility_contents_active_world_id_invalid")
    elif a!=sw: b.append("functional_building_utility_contents_snapshot_active_world_mismatch")
    elif value.get("world_id")!=a: b.append("functional_building_utility_contents_active_world_mismatch")
    return sorted(set(b))


_FUNCTIONAL_BUILDING_LOGIC_PORTS_INSTANCE_KEYS = frozenset(
    {"instance_id", "world_id", "prefab_id", "component_present", "ports"}
)
_FUNCTIONAL_BUILDING_LOGIC_PORT_KEYS = frozenset(
    {"id", "direction", "cell", "requires_connection", "is_connected"}
)
_FUNCTIONAL_BUILDING_LOGIC_PORTS_PAYLOAD_KEYS = frozenset(
    {"schema", "captured_at", "world_id", "complete", "instances"}
)


def _functional_building_logic_ports_payload_blockers(value: Any) -> list[str]:
    if not isinstance(value, Mapping):
        return ["functional_building_logic_ports_type_mismatch"]
    blockers: list[str] = []
    if set(value) != _FUNCTIONAL_BUILDING_LOGIC_PORTS_PAYLOAD_KEYS:
        blockers.append("functional_building_logic_ports_payload_shape_invalid")
    if value.get("schema") != FUNCTIONAL_BUILDING_LOGIC_PORTS_SCHEMA:
        blockers.append("functional_building_logic_ports_schema_invalid")
    if not isinstance(value.get("captured_at"), str) or parse_snapshot_timestamp(value["captured_at"]) is None:
        blockers.append("functional_building_logic_ports_capture_invalid")
    world_id = value.get("world_id")
    if isinstance(world_id, bool) or not isinstance(world_id, int) or world_id < 0 or world_id > _INT32_MAX:
        blockers.append("functional_building_logic_ports_world_id_invalid")
    if value.get("complete") is not True:
        blockers.append("functional_building_logic_ports_incomplete")
    instances = value.get("instances")
    if not isinstance(instances, list):
        return sorted(set(blockers + ["functional_building_logic_ports_instances_invalid"]))
    if len(instances) > 10000:
        blockers.append("functional_building_logic_ports_instances_too_many")
    seen: set[int] = set()
    for instance in instances:
        if not isinstance(instance, Mapping) or set(instance) != _FUNCTIONAL_BUILDING_LOGIC_PORTS_INSTANCE_KEYS:
            blockers.append("functional_building_logic_ports_instance_shape_invalid")
            continue
        iid = instance.get("instance_id")
        if isinstance(iid, bool) or not isinstance(iid, int) or iid <= 0 or iid > _INT32_MAX:
            blockers.append("functional_building_logic_ports_instance_id_invalid")
        elif iid in seen:
            blockers.append("functional_building_logic_ports_instance_id_duplicate")
        else:
            seen.add(iid)
        iw = instance.get("world_id")
        if isinstance(iw, bool) or not isinstance(iw, int) or iw < 0 or iw > _INT32_MAX:
            blockers.append("functional_building_logic_ports_instance_world_id_invalid")
        elif isinstance(world_id, int) and not isinstance(world_id, bool) and iw != world_id:
            blockers.append("functional_building_logic_ports_instance_world_mismatch")
        if not isinstance(instance.get("prefab_id"), str) or not instance["prefab_id"] or len(instance["prefab_id"]) > 128:
            blockers.append("functional_building_logic_ports_prefab_id_invalid")
        if not isinstance(instance.get("component_present"), bool):
            blockers.append("functional_building_logic_ports_component_present_invalid")
        ports = instance.get("ports")
        if not isinstance(ports, list) or len(ports) > 256:
            blockers.append("functional_building_logic_ports_ports_invalid")
            continue
        seen_ports: set[str] = set()
        for port in ports:
            if not isinstance(port, Mapping) or set(port) != _FUNCTIONAL_BUILDING_LOGIC_PORT_KEYS:
                blockers.append("functional_building_logic_ports_port_shape_invalid")
                continue
            pid = port.get("id")
            direction = port.get("direction")
            if not isinstance(pid, str) or not pid or len(pid) > 128:
                blockers.append("functional_building_logic_ports_port_id_invalid")
            key = str(direction) + "|" + str(pid)
            if key in seen_ports:
                blockers.append("functional_building_logic_ports_port_duplicate")
            seen_ports.add(key)
            if direction not in ("input", "output"):
                blockers.append("functional_building_logic_ports_direction_invalid")
            cell = port.get("cell")
            if isinstance(cell, bool) or not isinstance(cell, int) or cell < 0 or cell > _INT32_MAX:
                blockers.append("functional_building_logic_ports_cell_invalid")
            if not isinstance(port.get("requires_connection"), bool):
                blockers.append("functional_building_logic_ports_requires_connection_invalid")
            connected = port.get("is_connected")
            if connected is not None and not isinstance(connected, bool):
                blockers.append("functional_building_logic_ports_connected_invalid")
    return sorted(set(blockers))


def _functional_building_logic_ports_state_binding_blockers(state: Mapping[str, Any], value: Any) -> list[str]:
    if not isinstance(value, Mapping):
        return []
    snapshot_world_id = state.get("world_id")
    if isinstance(snapshot_world_id, bool) or not isinstance(snapshot_world_id, int) or snapshot_world_id < 0 or snapshot_world_id > _INT32_MAX:
        return ["snapshot_world_id_invalid"]
    blockers: list[str] = []
    if value.get("world_id") != snapshot_world_id:
        blockers.append("functional_building_logic_ports_snapshot_world_mismatch")
    scope = state.get("observation_scope")
    if not isinstance(scope, Mapping):
        return sorted(set(blockers + ["functional_building_logic_ports_active_world_scope_missing"]))
    active = scope.get("active_world_id")
    if isinstance(active, bool) or not isinstance(active, int) or active < 0 or active > _INT32_MAX:
        blockers.append("functional_building_logic_ports_active_world_id_invalid")
    elif active != snapshot_world_id:
        blockers.append("functional_building_logic_ports_snapshot_active_world_mismatch")
    elif value.get("world_id") != active:
        blockers.append("functional_building_logic_ports_active_world_mismatch")
    return sorted(set(blockers))


def _value_blockers(
    value: Any,
    contract: FieldContract,
    path: Optional[str] = None,
) -> list[str]:
    blockers: list[str] = []
    value_type = contract.value_type
    numeric: Optional[float] = None
    if value_type == "boolean":
        if not isinstance(value, bool):
            blockers.append("field_type_mismatch")
    elif value_type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            blockers.append("field_type_mismatch")
        else:
            # Keep exact JSON integers exact. Converting a valid but oversized
            # integer to binary64 can raise OverflowError before the declared
            # range check has a chance to reject it.
            numeric = value
    elif value_type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            blockers.append("field_type_mismatch")
        else:
            try:
                numeric = float(value)
            except (OverflowError, ValueError):
                numeric = None
                blockers.append("field_not_finite")
            if numeric is not None and not math.isfinite(numeric):
                blockers.append("field_not_finite")
    elif value_type == "string":
        if not isinstance(value, str):
            blockers.append("field_type_mismatch")
        elif contract.maximum_text_length is not None and len(value) > contract.maximum_text_length:
            blockers.append("field_text_too_long")
    elif value_type == "string_list":
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            blockers.append("field_type_mismatch")
        else:
            if contract.maximum_items is not None and len(value) > contract.maximum_items:
                blockers.append("field_list_too_long")
            if len(value) != len(set(value)):
                blockers.append("field_list_duplicates")
            if contract.sorted_items and value != sorted(value):
                blockers.append("field_list_unsorted")
            for item in value:
                if contract.maximum_text_length is not None and len(item) > contract.maximum_text_length:
                    blockers.append("field_text_too_long")
                    break
                if contract.canonical_string_items and _CANONICAL_ID_RE.fullmatch(item) is None:
                    blockers.append("field_identifier_invalid")
                if contract.allowed_string_items and item not in contract.allowed_string_items:
                    blockers.append("field_list_item_unknown")
                    break
    elif value_type == "path_native":
        blockers.extend(_path_native_value_blockers(path, value))
    elif value_type == "functional_buildings":
        blockers.extend(_functional_buildings_payload_blockers(value))
    elif value_type == "functional_building_geometry":
        blockers.extend(_functional_building_geometry_payload_blockers(value))
    elif value_type == "functional_building_ports":
        blockers.extend(_functional_building_ports_payload_blockers(value))
    elif value_type == "functional_building_occupancy":
        blockers.extend(_functional_building_occupancy_payload_blockers(value))
    elif value_type == "functional_building_health":
        blockers.extend(_functional_building_health_payload_blockers(value))
    elif value_type == "functional_building_flooded":
        blockers.extend(_functional_building_flooded_payload_blockers(value))
    elif value_type == "functional_building_reachability":
        blockers.extend(_functional_building_reachability_payload_blockers(value))
    elif value_type == "functional_building_power_network":
        blockers.extend(_functional_building_power_network_payload_blockers(value))
    elif value_type == "functional_building_utility_network":
        blockers.extend(_functional_building_utility_network_payload_blockers(value))
    elif value_type == "functional_building_logic_ports":
        blockers.extend(_functional_building_logic_ports_payload_blockers(value))
    elif value_type == "functional_building_utility_endpoint":
        blockers.extend(_functional_building_utility_endpoint_payload_blockers(value))
    elif value_type == "functional_building_utility_topology":
        blockers.extend(_functional_building_utility_topology_payload_blockers(value))
    elif value_type == "functional_building_utility_contents":
        blockers.extend(_functional_building_utility_contents_payload_blockers(value))
    elif value_type == "blueprint_material_lineage":
        blockers.extend(_blueprint_material_lineage_blockers(value))

    if numeric is not None:
        if contract.minimum is not None and numeric < contract.minimum:
            blockers.append("field_below_minimum")
        if contract.maximum is not None and numeric > contract.maximum:
            blockers.append("field_above_maximum")
    return blockers


ALERTS_BY_WORLD_SEMANTIC = "canonical_active_alert_ids_by_world"
ALERTS_BY_WORLD_UNIT = "world_alert_groups"
_ALERTS_BY_WORLD_ENTRY_KEYS = frozenset({"world_id", "duplicant_count", "alerts"})


def _alerts_by_world_blockers(state: Mapping[str, Any], alerts: Any, entry: Mapping[str, Any]) -> list[str]:
    """Check the optional per-world alert groups against ``alerts``.

    ``alerts_by_world`` is additive: an absent or null field changes
    nothing.  When present it must be a list ordered by world id (a null
    world, e.g. a duplicant in transit, first), carry its own quality entry
    with the same source and scope as ``alerts``, and its groups' union must
    equal ``alerts`` exactly; any disagreement makes ``alerts`` unusable.
    """

    groups = state.get("alerts_by_world")
    if groups is None:
        return []
    blockers: list[str] = []
    group_entry = field_quality(state if isinstance(state, dict) else dict(state), "alerts_by_world")
    if (
        group_entry.get("status") != "observed"
        or group_entry.get("semantic") != ALERTS_BY_WORLD_SEMANTIC
        or group_entry.get("unit") != ALERTS_BY_WORLD_UNIT
        or group_entry.get("scope") != entry.get("scope")
        or group_entry.get("source") != entry.get("source")
    ):
        blockers.append("alerts_by_world_quality_mismatch")
    if not isinstance(groups, list) or len(groups) > 1000:
        return blockers + ["alerts_by_world_invalid"]
    union: set[str] = set()
    previous_key: Optional[int] = None
    for group in groups:
        if not isinstance(group, Mapping) or set(group) != _ALERTS_BY_WORLD_ENTRY_KEYS:
            return blockers + ["alerts_by_world_invalid"]
        world_id = group.get("world_id")
        count = group.get("duplicant_count")
        group_alerts = group.get("alerts")
        if world_id is not None and (
            isinstance(world_id, bool) or not isinstance(world_id, int) or not 0 <= world_id <= _INT32_MAX
        ):
            return blockers + ["alerts_by_world_invalid"]
        if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= _INT32_MAX:
            return blockers + ["alerts_by_world_invalid"]
        if (
            not isinstance(group_alerts, list)
            or any(not isinstance(item, str) or item not in CANONICAL_ALERT_IDS for item in group_alerts)
            or group_alerts != sorted(set(group_alerts))
        ):
            return blockers + ["alerts_by_world_invalid"]
        key = -1 if world_id is None else world_id
        if previous_key is not None and key <= previous_key:
            return blockers + ["alerts_by_world_order_invalid"]
        previous_key = key
        union.update(group_alerts)
    if not isinstance(alerts, list) or union != set(item for item in alerts if isinstance(item, str)):
        blockers.append("alerts_by_world_union_mismatch")
    return blockers


def field_contract_blockers(
    state: Optional[Mapping[str, Any]],
    path: str,
    contract_id: Optional[str] = None,
    *,
    allowed_statuses: Optional[Sequence[str]] = None,
) -> list[str]:
    if not isinstance(state, Mapping):
        return ["state_missing"]
    contract = contract_by_id(contract_id) if contract_id is not None else contract_for_path(path)
    if contract is None:
        return ["quality_contract_unknown"]
    if not contract.matches_path(path):
        return ["quality_contract_path_mismatch"]
    statuses = tuple(allowed_statuses) if allowed_statuses is not None else contract.statuses
    if not statuses or any(status not in contract.statuses for status in statuses):
        return ["quality_contract_status_allowance_invalid"]

    state_dict = state if isinstance(state, dict) else dict(state)
    entry = field_quality(state_dict, path)
    blockers = quality_contract_blockers(
        entry,
        allowed_statuses=statuses,
        semantics=contract.semantics,
        units=contract.units,
        scopes=contract.scopes,
        completeness=contract.completeness,
        captures=contract.captures,
    )
    if not isinstance(entry, Mapping):
        return sorted(set(blockers))
    if entry.get("source") not in contract.sources:
        blockers.append("quality_source_mismatch")
    if entry.get("contract_id") != contract.contract_id:
        blockers.append("quality_contract_id_mismatch")
    if contract.status_completeness and (
        entry.get("status"), entry.get("completeness")
    ) not in contract.status_completeness:
        blockers.append("quality_status_completeness_mismatch")
    if contract.scope_sources and (
        entry.get("scope"), entry.get("source")
    ) not in contract.scope_sources:
        blockers.append("quality_scope_source_mismatch")
    blockers.extend(_capture_binding_blockers(state, entry, contract))

    marker = object()
    value = safe_get(state_dict, path, marker)
    if value is marker:
        blockers.append("field_missing")
    else:
        blockers.extend(_value_blockers(value, contract, path))
        if contract.contract_id == ALERTS_CANONICAL_CONTRACT_ID:
            blockers.extend(_alerts_by_world_blockers(state_dict, value, entry))
        if contract.value_type == "functional_buildings":
            blockers.extend(_functional_buildings_state_binding_blockers(state_dict, value))
        elif contract.value_type == "functional_building_geometry":
            blockers.extend(_functional_building_geometry_state_binding_blockers(state_dict, value))
        elif contract.value_type == "functional_building_ports":
            blockers.extend(_functional_building_ports_state_binding_blockers(state_dict, value))
        elif contract.value_type == "functional_building_occupancy":
            blockers.extend(_functional_building_occupancy_state_binding_blockers(state_dict, value))
        elif contract.value_type == "functional_building_health":
            blockers.extend(_functional_building_health_state_binding_blockers(state_dict, value))
        elif contract.value_type == "functional_building_flooded":
            blockers.extend(_functional_building_flooded_state_binding_blockers(state_dict, value))
        elif contract.value_type == "functional_building_reachability":
            blockers.extend(_functional_building_reachability_state_binding_blockers(state_dict, value))
        elif contract.value_type == "functional_building_power_network":
            blockers.extend(_functional_building_power_network_state_binding_blockers(state_dict, value))
        elif contract.value_type == "functional_building_utility_network":
            blockers.extend(_functional_building_utility_network_state_binding_blockers(state_dict, value))
        elif contract.value_type == "functional_building_logic_ports":
            blockers.extend(_functional_building_logic_ports_state_binding_blockers(state_dict, value))
        elif contract.value_type == "functional_building_utility_endpoint":
            blockers.extend(_functional_building_utility_endpoint_state_binding_blockers(state_dict, value))
        elif contract.value_type == "functional_building_utility_topology":
            blockers.extend(_functional_building_utility_topology_state_binding_blockers(state_dict, value))
        elif contract.value_type == "functional_building_utility_contents":
            blockers.extend(_functional_building_utility_contents_state_binding_blockers(state_dict, value))
    return sorted(set(blockers))


def read_contract_field(
    state: Optional[Mapping[str, Any]],
    path: str,
    contract_id: Optional[str] = None,
    *,
    allowed_statuses: Optional[Sequence[str]] = None,
) -> ContractRead:
    resolved = contract_id or contract_id_for_path(path) or ""
    blockers = field_contract_blockers(
        state,
        path,
        resolved or None,
        allowed_statuses=allowed_statuses,
    )
    state_dict = state if isinstance(state, dict) else dict(state) if isinstance(state, Mapping) else {}
    value = safe_get(state_dict, path)
    status = str(field_quality(state_dict, path).get("status") or "unsupported")
    return ContractRead(path, resolved, value, status, tuple(blockers))


def _condition_expected_value_blockers(condition: Mapping[str, Any], contract: FieldContract) -> list[str]:
    op = condition.get("op")
    path = condition.get("path") if isinstance(condition.get("path"), str) else None
    expected = condition.get("value")
    if op == "contains" and contract.value_type == "string_list":
        if not isinstance(expected, str):
            return ["condition_value_type_mismatch"]
        if contract.canonical_string_items and _CANONICAL_ID_RE.fullmatch(expected) is None:
            return ["condition_value_identifier_invalid"]
        if contract.allowed_string_items and expected not in contract.allowed_string_items:
            return ["condition_value_item_unknown"]
        return []
    if op == "contains" and contract.value_type == "path_native" and isinstance(path, str):
        field = path.rsplit(".", 1)[-1]
        if field in _PATH_NATIVE_STRING_LIST_FIELDS:
            if not isinstance(expected, str):
                return ["condition_value_type_mismatch"]
            return [] if len(expected) <= 64 else ["condition_value_text_too_long"]
        if field in _PATH_NATIVE_INTEGER_LIST_FIELDS:
            if isinstance(expected, bool) or not isinstance(expected, int):
                return ["condition_value_type_mismatch"]
            if expected < 0:
                return ["condition_value_below_minimum"]
            if expected > _INT32_MAX:
                return ["condition_value_above_maximum"]
            return []
        return ["condition_value_type_mismatch"]
    return ["condition_" + item for item in _value_blockers(expected, contract, path)]


def _condition_operator_type_blockers(
    path: str,
    op: Any,
    contract: FieldContract,
) -> list[str]:
    if op in {"lt", "lte", "gt", "gte"}:
        if contract.value_type in {"integer", "number"}:
            return []
        if contract.value_type == "path_native":
            field = path.rsplit(".", 1)[-1]
            if field in _PATH_NATIVE_INTEGER_FIELDS or field in _PATH_NATIVE_NUMBER_FIELDS:
                return []
        return ["condition_operator_type_mismatch"]
    if op == "contains":
        if contract.value_type == "string_list":
            return []
        if contract.value_type == "path_native":
            field = path.rsplit(".", 1)[-1]
            if field in _PATH_NATIVE_STRING_LIST_FIELDS or field in _PATH_NATIVE_INTEGER_LIST_FIELDS:
                return []
        return ["condition_operator_type_mismatch"]
    return []


def condition_declaration_blockers(
    condition: Any,
    *,
    action: Optional[str] = None,
) -> list[str]:
    """Validate a condition's trusted declaration without requiring a current field."""

    if not isinstance(condition, Mapping):
        return ["condition_invalid"]
    path = condition.get("path")
    contract_id = condition.get("quality_contract_id")
    if not isinstance(path, str) or not path:
        return ["condition_path_invalid"]
    contract = contract_by_id(contract_id)
    if contract is None:
        return ["quality_contract_unknown"]

    blockers: list[str] = []
    if not contract.matches_path(path):
        blockers.append("quality_contract_path_mismatch")
    op = condition.get("op")
    if op not in _CONDITION_OPERATORS:
        blockers.append("condition_operator_invalid")
    else:
        blockers.extend(_condition_operator_type_blockers(path, op, contract))
    qualities = condition.get("quality")
    if (
        not isinstance(qualities, list)
        or not qualities
        or any(not isinstance(item, str) for item in qualities)
    ):
        blockers.append("condition_quality_invalid")
    elif len(qualities) != len(set(qualities)):
        blockers.append("condition_quality_duplicates")
    elif any(status not in contract.statuses for status in qualities):
        blockers.append("quality_contract_status_allowance_invalid")
    if action is not None and action not in NON_MUTATING_ACTIONS and not contract.mutating_condition_allowed:
        blockers.append("quality_contract_not_mutation_eligible")
    if "value" not in condition:
        blockers.append("condition_value_missing")
    elif op in _CONDITION_OPERATORS and contract.matches_path(path):
        blockers.extend(_condition_expected_value_blockers(condition, contract))
    return sorted(set(blockers))


def condition_contract_blockers(
    state: Optional[Mapping[str, Any]],
    condition: Any,
    *,
    action: Optional[str] = None,
) -> list[str]:
    blockers = condition_declaration_blockers(condition, action=action)
    if not isinstance(condition, Mapping):
        return blockers
    path = condition.get("path")
    contract_id = condition.get("quality_contract_id")
    if not isinstance(path, str) or not path:
        return blockers
    contract = contract_by_id(contract_id)
    if contract is None:
        return blockers
    qualities = condition.get("quality")
    if (
        not isinstance(qualities, list)
        or not qualities
        or any(not isinstance(item, str) for item in qualities)
    ):
        blockers.append("condition_quality_invalid")
        allowed_statuses: Optional[Sequence[str]] = ()
    else:
        allowed_statuses = qualities
    blockers.extend(
        field_contract_blockers(
            state,
            path,
            str(contract_id),
            allowed_statuses=allowed_statuses,
        )
    )
    return sorted(set(blockers))


def build_condition(
    path: str,
    op: str,
    value: Any,
    contract_id: str,
    *,
    quality: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    contract = contract_by_id(contract_id)
    if contract is None or not contract.matches_path(path):
        raise ValueError("Unknown or path-incompatible quality contract")
    statuses = list(quality) if quality is not None else list(contract.statuses)
    if not statuses or any(status not in contract.statuses for status in statuses):
        raise ValueError("Condition quality allowance is outside its contract")
    condition = {
        "path": path,
        "op": op,
        "value": value,
        "quality": statuses,
        "quality_contract_id": contract_id,
    }
    blockers = condition_declaration_blockers(condition)
    if blockers:
        raise ValueError("Invalid condition declaration: " + ", ".join(blockers))
    return condition


def contract_quality_fixture(contract_id: str, status: str = "observed") -> Dict[str, Any]:
    """Build deterministic metadata for offline Mock fixtures, never live admission."""

    contract = contract_by_id(contract_id)
    if contract is None or status not in contract.statuses:
        raise ValueError("Unknown contract or unsupported fixture status")
    completeness = next(
        (item[1] for item in contract.status_completeness if item[0] == status),
        contract.completeness[0],
    )
    return {
        "status": status,
        "source": contract.sources[0],
        "contract_id": contract.contract_id,
        "semantic": contract.semantics[0],
        "unit": contract.units[0],
        "scope": contract.scopes[0],
        "completeness": completeness,
        "captured_at": contract.captures[0],
    }
