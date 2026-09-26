"""Deterministic, telemetry-gated long-term strategy for Oxygen Not Included.

The strategy layer is deliberately separate from the command queue and the
optional language-model planner. It turns a trusted state snapshot into a
small, auditable set of objectives and operator recommendations. It never
executes an action, mutates a save, or invents telemetry that the probe did not
report.

The probe currently exposes only a subset of the information a human player
uses for a full colony plan. Consequently this module treats missing or stale
fields as unknown and asks for observation instead of guessing. Future probe
versions may add fields (power, temperature, rockets, and renewable rates);
the alias table below lets the strategy consume those fields without making
older snapshots invalid.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import math
import re
from contextvars import ContextVar
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from agent.state_quality import (
    canonical_state_hash,
    field_quality,
    parse_snapshot_timestamp,
    quality_contract_blockers,
    quality_contract_matches,
    quality_summary,
    safe_get,
    trusted_sample_interval_seconds,
)
from agent.field_contracts import (
    ALERTS_CANONICAL_CONTRACT_ID,
    CANONICAL_ALERT_IDS,
    DUPLICANTS_IDLE_CONTRACT_ID,
    DUPLICANTS_STRESSED_CONTRACT_ID,
    DUPLICANTS_TOTAL_CONTRACT_ID,
    FUNCTIONAL_BUILDINGS_CONTRACT_ID,
    GAME_PAUSED_CONTRACT_ID,
    REGISTERED_FOOD_CONTRACT_ID,
    RESEARCH_CURRENT_CONTRACT_ID,
    read_contract_field,
)


STRATEGY_SCHEMA = "oni.strategy.v2"
STRATEGY_GOAL = (
    "Keep the colony alive indefinitely: stabilize life support, build resilient "
    "renewable systems, then pursue space milestones without sacrificing safety."
)

PHASE_ORDER = ("bootstrap", "stabilize", "industry", "expand", "space", "sustain")
OBJECTIVE_STATUSES = {"satisfied", "at_risk", "unknown", "blocked", "deferred"}
PHASE_CONFIDENCE = {"high", "medium", "low", "blocked"}
READINESS_STATUSES = {"ready", "not_ready", "unknown", "blocked"}

# A phase is the earliest group of exit gates that has not yet been proven.
# The groups are intentionally sequential: a rocket/victory marker can never
# bypass unresolved life-support, resilience, or thermal gates.
PHASE_EXIT_GATES: Dict[str, Tuple[str, ...]] = {
    "bootstrap": ("oxygen_security", "food_reserve", "sanitation"),
    "stabilize": ("water_resilience", "power_resilience", "morale_and_stress", "research_progress"),
    # Industry is deliberately a separate phase.  A blueprint/building count
    # cannot satisfy any of these predicates: each one requires an observed
    # production/delivery/input/heat/waste result from the same bounded scope.
    "industry": (
        "industry_production",
        "industry_delivery",
        "industry_inputs",
        "industry_thermal_safety",
        "industry_waste_handling",
    ),
    "expand": (
        "thermal_safety",
        "renewable_food",
        "renewable_oxygen",
        "renewable_water",
        "renewable_power",
        "fault_tolerance",
    ),
    "space": ("space_progress",),
    "sustain": ("indefinite_survival",),
}
PHASE_STABILITY_SAMPLES: Dict[str, int] = {
    "bootstrap": 3,
    "stabilize": 12,
    "industry": 24,
    "expand": 24,
    "space": 12,
    "sustain": 120,
}

# Sample counts alone are not duration.  Each forward gate must span both
# monotonic wall-clock time and monotonic in-game time; paused snapshots or a
# burst replay can therefore never manufacture maturity.
PHASE_STABILITY_MIN_SECONDS: Dict[str, float] = {
    phase: float(max(10, (samples - 1) * 5)) for phase, samples in PHASE_STABILITY_SAMPLES.items()
}
PHASE_STABILITY_MIN_GAME_CYCLES: Dict[str, float] = {
    "bootstrap": 0.02,
    "stabilize": 0.25,
    "industry": 1.0,
    "expand": 1.0,
    "space": 0.25,
    "sustain": 10.0,
}

# A certifying evidence window is stricter than snapshot freshness.  Freshness
# may tolerate several intervals for operator diagnostics, but a missing
# observation must break the gap-free proof.  One quarter-interval of scheduler
# jitter is allowed; a two-interval gap is always rejected.
PHASE_EVIDENCE_MAX_JITTER_RATIO = 0.25

_POWER_TELEMETRY_SOURCE = (
    "Components.Batteries(powerTransformer==null)+EnergyConsumers+"
    "Generators(excluding PowerTransformer)"
)
_POWER_MAXIMUM_JOULES = 1_000_000_000_000_000_000.0
_POWER_PERCENT_TOLERANCE = 0.001

# ``oni.state.v2`` remains the transport schema.  These per-field contracts
# define which additive quality metadata is strong enough to certify a gate.
# Observed values with a mismatched semantic/unit/scope are treated as blocked,
# while estimated values may still raise risk but never satisfy a gate.
CERTIFYING_FIELD_CONTRACTS: Dict[str, Dict[str, Any]] = {
    "game.paused": {
        "semantics": ("direct_state",),
        "units": ("boolean",),
        "scopes": ("active_game_session",),
        "sources": ("SpeedControlScreen.IsPaused",),
        "contract_ids": (GAME_PAUSED_CONTRACT_ID,),
    },
    "observation_scope.active_world_id": {
        "semantics": ("raw_identifier",),
        "units": ("world_id",),
        "scopes": ("active_world",),
    },
    "observation_scope.populated_world_ids": {
        "semantics": ("raw_set",),
        "units": ("world_id",),
        "scopes": ("save",),
    },
    "observation_scope.covered_world_ids": {
        "semantics": ("raw_set",),
        "units": ("world_id",),
        "scopes": ("snapshot",),
    },
    "observation_scope.complete_for_populated_worlds": {
        "semantics": ("raw_coverage",),
        "units": ("boolean",),
        "scopes": ("save",),
    },
    "game_time_cycles": {
        "semantics": ("display_1_based_continuous",),
        "units": ("cycle",),
        "scopes": ("save",),
    },
    "duplicants.total": {
        "semantics": ("live_count",),
        "units": ("count",),
        "scopes": ("active_world",),
        "sources": ("Components.LiveMinionIdentities.active_world",),
        "contract_ids": (DUPLICANTS_TOTAL_CONTRACT_ID,),
    },
    "colony.duplicants": {
        "semantics": ("raw_count",),
        "units": ("count",),
        "scopes": ("active_world", "all_populated_worlds"),
    },
    "resources.food_kcal": {
        # No live producer currently proves accessibility/permissions.  A
        # future producer may certify this semantic only through an in-process
        # proof verifier; self-declared observed metadata is insufficient.
        "semantics": ("accessible_inventory_sum",),
        "units": ("kcal",),
        "scopes": ("active_world_accessible_edibles",),
        "authorities": ("oni.accessible-food-evidence.v1",),
        "require_proof_hashes": True,
    },
    "resources.oxygen_kg_estimate": {
        "semantics": ("composite_gauge",),
        "units": ("kg",),
        "scopes": ("active_world_all_storage_domains", "all_populated_worlds_all_storage_domains"),
    },
    "resources.oxygen_kg": {
        "semantics": ("composite_gauge",),
        "units": ("kg",),
        "scopes": ("active_world_all_storage_domains", "all_populated_worlds_all_storage_domains"),
    },
    "resources.oxygen_kg_total": {
        "semantics": ("composite_gauge",),
        "units": ("kg",),
        "scopes": ("active_world_all_storage_domains", "all_populated_worlds_all_storage_domains"),
    },
    "resources.water_kg_estimate": {
        "semantics": ("composite_gauge",),
        "units": ("kg",),
        "scopes": ("active_world_all_storage_domains", "all_populated_worlds_all_storage_domains"),
    },
    "resources.water_kg": {
        "semantics": ("composite_gauge",),
        "units": ("kg",),
        "scopes": ("active_world_all_storage_domains", "all_populated_worlds_all_storage_domains"),
    },
    "resources.water_kg_total": {
        "semantics": ("composite_gauge",),
        "units": ("kg",),
        "scopes": ("active_world_all_storage_domains", "all_populated_worlds_all_storage_domains"),
    },
    "alerts": {
        "semantics": ("canonical_active_alert_ids",),
        "units": ("alert_id_list",),
        "scopes": ("active_world",),
        "completeness": ("complete",),
        "sources": (
            "Components.LiveMinionIdentities.GetWorldItems(activeWorldId,false)+typed_monitor_predicates;catalog=oni.alert.catalog.active_world.v1",
        ),
        "captures": ("snapshot.ts",),
        "contract_ids": (ALERTS_CANONICAL_CONTRACT_ID,),
    },
    "research.current": {
        "semantics": ("active_selection",),
        "units": ("technology_id_or_empty",),
        "scopes": ("save_research",),
        "sources": ("Research.GetActiveResearch",),
        "contract_ids": (RESEARCH_CURRENT_CONTRACT_ID,),
    },
    "duplicants.stressed": {
        "semantics": ("stressed_count",),
        "units": ("count",),
        "scopes": ("active_world",),
        "completeness": ("complete",),
        "sources": (
            "Components.LiveMinionIdentities.GetWorldItems(activeWorldId,false)+StressMonitor",
        ),
        "captures": ("snapshot.ts",),
        "contract_ids": (DUPLICANTS_STRESSED_CONTRACT_ID,),
    },
    "colony.stressed": {
        "semantics": ("raw_count",),
        "units": ("count",),
        "scopes": ("active_world", "all_populated_worlds"),
    },
    "duplicants.idle": {
        "semantics": ("idle_count",),
        "units": ("count",),
        "scopes": ("active_world",),
        "completeness": ("complete",),
        "sources": (
            "Components.LiveMinionIdentities.GetWorldItems(activeWorldId,false)+GameTags.Idle",
        ),
        "captures": ("snapshot.ts",),
        "contract_ids": (DUPLICANTS_IDLE_CONTRACT_ID,),
    },
    "colony.idle": {
        "semantics": ("raw_count",),
        "units": ("count",),
        "scopes": ("active_world", "all_populated_worlds"),
    },
    "buildings.oxygen_diffuser": {
        "semantics": ("raw_count",),
        "units": ("count",),
        "scopes": ("active_world_registered_buildings",),
    },
    "buildings.outhouse": {
        "semantics": ("raw_count",),
        "units": ("count",),
        "scopes": ("active_world_registered_buildings",),
    },
    "buildings.manual_generator": {
        "semantics": ("raw_count",),
        "units": ("count",),
        "scopes": ("active_world_registered_buildings",),
    },
    "buildings.generator": {
        "semantics": ("raw_count",),
        "units": ("count",),
        "scopes": ("active_world_registered_buildings",),
    },
    "power.storage_mode": {
        "semantics": ("storage_capacity_mode",),
        "units": ("enum",),
        "scopes": ("active_world_registered_batteries",),
        "completeness": ("complete",),
        "sources": (_POWER_TELEMETRY_SOURCE,),
        "captures": ("snapshot.ts",),
    },
    "power.stored_joules": {
        "semantics": ("raw_gauge",),
        "units": ("J",),
        "scopes": ("active_world_registered_batteries",),
        "completeness": ("complete",),
        "sources": (_POWER_TELEMETRY_SOURCE,),
        "captures": ("snapshot.ts",),
    },
    "power.capacity_joules": {
        "semantics": ("raw_gauge",),
        "units": ("J",),
        "scopes": ("active_world_registered_batteries",),
        "completeness": ("complete",),
        "sources": (_POWER_TELEMETRY_SOURCE,),
        "captures": ("snapshot.ts",),
    },
    "power.battery_percent": {
        "semantics": ("derived_ratio",),
        "units": ("percent",),
        "scopes": ("active_world_registered_batteries",),
        "completeness": ("complete",),
        "sources": (_POWER_TELEMETRY_SOURCE,),
        "captures": ("snapshot.ts",),
    },
    "power.battery_pct": {
        "semantics": ("derived_ratio",),
        "units": ("percent",),
        "scopes": ("active_world_registered_batteries", "all_populated_worlds_registered_batteries"),
    },
    "resources.battery_percent": {
        "semantics": ("derived_ratio",),
        "units": ("percent",),
        "scopes": ("active_world_registered_batteries", "all_populated_worlds_registered_batteries"),
    },
    "rockets.total": {
        "semantics": ("raw_count",),
        "units": ("count",),
        "scopes": ("cluster",),
    },
    "space.rockets": {
        "semantics": ("raw_count",),
        "units": ("count",),
        "scopes": ("cluster",),
    },
    "space.rocket_count": {
        "semantics": ("raw_count",),
        "units": ("count",),
        "scopes": ("cluster",),
    },
}

# Rolling cell aggregates are diagnostics, not certifying reserves: they omit
# storage, pipes, bottles, debris and hidden cells, and their members were
# sampled across a bounded time window. Exact contracts still matter because
# a malformed estimate must not enter even the conservative risk path.
_CELL_AGGREGATE_CAPTURE = ("materials.active_world_cells.aggregate_window_completed_at",)
for _cell_mass_path in (
    "materials.active_world_cells.clean_oxygen_kg",
    "materials.active_world_cells.polluted_oxygen_kg",
    "materials.active_world_cells.clean_water_kg",
    "materials.active_world_cells.polluted_water_kg",
    "materials.active_world_cells.salt_water_kg",
    "materials.active_world_cells.brine_kg",
):
    CERTIFYING_FIELD_CONTRACTS[_cell_mass_path] = {
        "semantics": ("rolling_window_gauge",),
        "units": ("kg",),
        "scopes": ("active_world_visible_cells",),
        "captures": _CELL_AGGREGATE_CAPTURE,
        "capture_max_age_seconds": 900.0,
    }
for _cell_temperature_path in (
    "temperature.active_world_material_min_c",
    "temperature.active_world_material_max_c",
):
    CERTIFYING_FIELD_CONTRACTS[_cell_temperature_path] = {
        "semantics": ("rolling_window_extreme",),
        "units": ("degC",),
        "scopes": ("active_world_visible_material_cells",),
        "captures": _CELL_AGGREGATE_CAPTURE,
        "capture_max_age_seconds": 900.0,
    }


def _add_window_rate_contract(paths: Sequence[str], unit: str) -> None:
    contract = {
        "semantics": ("window_rate",),
        "units": (unit,),
        "scopes": ("all_populated_worlds",),
        "authorities": ("oni.evidence-window.v1",),
        "require_proof_hashes": True,
    }
    for path in paths:
        CERTIFYING_FIELD_CONTRACTS[path] = dict(contract)


_add_window_rate_contract(
    ("production.food_kcal_per_cycle", "rates.food_kcal_per_cycle", "resources.food_production_kcal_per_cycle"),
    "kcal/cycle",
)
_add_window_rate_contract(
    ("production.oxygen_kg_per_cycle", "rates.oxygen_kg_per_cycle"),
    "kg/cycle",
)
_add_window_rate_contract(
    ("consumption.oxygen_kg_per_cycle", "demand.oxygen_kg_per_cycle", "rates.oxygen_demand_kg_per_cycle"),
    "kg/cycle",
)
_add_window_rate_contract(
    ("production.water_kg_per_cycle", "rates.water_kg_per_cycle", "resources.water_production_kg_per_cycle"),
    "kg/cycle",
)
_add_window_rate_contract(
    ("consumption.water_kg_per_cycle", "demand.water_kg_per_cycle", "rates.water_demand_kg_per_cycle"),
    "kg/cycle",
)
_add_window_rate_contract(
    ("production.power_watts", "power.generation_watts", "rates.power_generation_watts"),
    "W",
)
_add_window_rate_contract(
    ("consumption.power_watts", "power.load_watts", "rates.power_demand_watts"),
    "W",
)


def _add_certified_contract(
    paths: Sequence[str],
    *,
    semantic: str = "certified_assertion",
    unit: str = "boolean",
    scope: str,
) -> None:
    contract = {
        "semantics": (semantic,),
        "units": (unit,),
        "scopes": (scope,),
        "authorities": ("oni.certification.v1",),
        "require_proof_hashes": True,
    }
    for path in paths:
        CERTIFYING_FIELD_CONTRACTS[path] = dict(contract)


_add_certified_contract(
    ("power.critical_load_covered", "power.life_support_covered"),
    scope="all_populated_worlds",
)
_add_certified_contract(
    ("power.independent_source_count", "resilience.power_source_count"),
    semantic="certified_topology_count",
    unit="count",
    scope="all_populated_worlds",
)
_add_certified_contract(
    ("oxygen.independent_source_count", "resilience.oxygen_source_count"),
    semantic="certified_topology_count",
    unit="count",
    scope="all_populated_worlds",
)
_add_certified_contract(
    ("water.independent_source_count", "resilience.water_source_count"),
    semantic="certified_topology_count",
    unit="count",
    scope="all_populated_worlds",
)
_add_certified_contract(
    ("temperature.critical_zones_safe", "environment.critical_zones_safe"),
    scope="all_populated_worlds",
)
_add_certified_contract(
    ("space.mission_ready", "rockets.mission_ready", "rockets.ready"),
    scope="cluster",
)
_add_certified_contract(("space.return_ready", "rockets.return_ready"), scope="cluster")
_add_certified_contract(
    ("space.base_reserve_safe", "colony.space_reserve_safe"),
    scope="save",
)
_add_certified_contract(
    ("resilience.n_minus_one_safe", "colony.n_minus_one_safe"),
    scope="all_populated_worlds",
)
_add_certified_contract(
    ("colony.sustainable", "sustainability.complete", "game.sustainable"),
    scope="save",
)

# Industrial completion is an observed result contract, rather than a
# building/blueprint label.  The canonical paths are nested under ``industry``
# and the aliases keep older probe envelopes readable without weakening the
# observed-only requirement.  Every field must carry a trusted proof hash via
# the in-process verifier before it can satisfy an industry objective.
_INDUSTRY_FIELD_PATHS: Dict[str, Tuple[str, ...]] = {
    "production": (
        "industry.production.completed",
        "industry.production_complete",
        "industry.production_completed",
        "industry.production.produced",
    ),
    "delivery": (
        "industry.delivery.completed",
        "industry.delivery_complete",
        "industry.delivery_completed",
        "industry.delivery.delivered",
    ),
    "inputs": (
        "industry.inputs.supplied",
        "industry.inputs_supplied",
        "industry.input_supply_complete",
        "industry.inputs.supplied_mass_kg",
    ),
    "thermal": (
        "industry.heat.removed",
        "industry.heat_removed",
        "industry.heat_removal_complete",
        "industry.heat.removed_kg",
    ),
    "waste": (
        "industry.waste.handled",
        "industry.waste_handled",
        "industry.waste_processing_complete",
        "industry.waste.processed",
    ),
}
for _industry_domain, _industry_paths in _INDUSTRY_FIELD_PATHS.items():
    _add_certified_contract(
        _industry_paths,
        semantic="industry_deliverable",
        unit="boolean",
        scope="all_populated_worlds",
    )

# Quantity pairs are accepted only when both sides are observed and certified;
# they are used to derive a completion boolean without treating an estimate as
# an actual deliverable.  Their values are exposed as diagnostics as well.
_INDUSTRY_QUANTITY_PATHS: Dict[str, Tuple[Tuple[str, ...], Tuple[str, ...], str]] = {
    "production": (
        ("industry.production.produced_quantity", "industry.production.actual_quantity"),
        ("industry.production.required_quantity", "industry.production.target_quantity"),
        "count",
    ),
    "delivery": (
        ("industry.delivery.delivered_quantity", "industry.delivery.actual_quantity"),
        ("industry.delivery.required_quantity", "industry.delivery.target_quantity"),
        "count",
    ),
    "inputs": (
        ("industry.inputs.supplied_quantity", "industry.inputs.actual_quantity"),
        ("industry.inputs.required_quantity", "industry.inputs.target_quantity"),
        "kg",
    ),
    "thermal": (
        ("industry.heat.removed_watts", "industry.heat.removal_rate_watts"),
        ("industry.heat.generated_watts", "industry.heat.load_watts"),
        "W",
    ),
    "waste": (
        ("industry.waste.handled_quantity", "industry.waste.processed_quantity"),
        ("industry.waste.generated_quantity", "industry.waste.required_quantity"),
        "kg",
    ),
}
for _industry_domain, (_industry_actual_paths, _industry_required_paths, _industry_unit) in _INDUSTRY_QUANTITY_PATHS.items():
    _add_window_rate_contract(_industry_actual_paths + _industry_required_paths, _industry_unit)

for _mission_path in ("space.mission_completed", "rockets.mission_completed"):
    CERTIFYING_FIELD_CONTRACTS[_mission_path] = {
        "semantics": ("raw_event",),
        "units": ("boolean",),
        "scopes": ("cluster",),
        "authorities": ("oni.mission-ledger.v1",),
        "require_proof_hashes": True,
    }

for _temperature_path in (
    "temperature.max_c",
    "environment.max_temperature_c",
    "temperature.max",
    "temperature.min_c",
    "environment.min_temperature_c",
    "temperature.min",
):
    CERTIFYING_FIELD_CONTRACTS[_temperature_path] = {
        "semantics": ("critical_zone_extreme",),
        "units": ("degC",),
        "scopes": ("all_populated_worlds_critical_zones",),
        "authorities": ("oni.evidence-window.v1",),
        "require_proof_hashes": True,
    }

SAFETY_CRITICAL_OBJECTIVES = {
    "oxygen_security",
    "food_reserve",
    "sanitation",
    "water_resilience",
    "power_resilience",
    "thermal_safety",
    # Industrial heat and waste are direct colony safety constraints. Keep
    # them in the top-level critical set so a failed industry gate selects
    # emergency mode instead of being reported as merely degraded.
    "industry_thermal_safety",
    "industry_waste_handling",
}

# The rule layer used by this project models a duplicant as consuming roughly
# 1000 kcal per cycle. Keep the value explicit for future ruleset changes.
KCAL_PER_DUPLICANT_CYCLE = 1000.0
FOOD_RUNWAY_CRITICAL = 2.0
FOOD_RUNWAY_TARGET = 5.0
WATER_PER_DUPLICANT_CRITICAL = 10.0
WATER_PER_DUPLICANT_TARGET = 50.0

_API_SECRET_RE = re.compile(
    r"(?i)(?:sk-[A-Za-z0-9_-]{8,}|(?:api[_-]?key|token|secret)\s*[:=]\s*[A-Za-z0-9._~+/=-]{8,})"
)

# Keep the strategy's hazard projection closed over the same canonical v1
# catalog as the producer and field-contract validator. This duplicate is
# intentional: catalog growth must be reviewed here before it can affect a
# safety objective.
_CANONICAL_ALERT_IDS = frozenset(CANONICAL_ALERT_IDS)
_OXYGEN_ALERT_IDS = frozenset({"low_oxygen", "suffocation"})
_FOOD_ALERT_IDS = frozenset({"starvation"})
_POWER_ALERT_IDS = frozenset({"bionic_low_battery", "bionic_offline"})
_TEMPERATURE_ALERT_IDS = frozenset({"hyperthermia", "hypothermia"})


class StrategyError(ValueError):
    """Raised when a strategy payload cannot satisfy the public contract."""


TrustedProofVerifier = Callable[[Mapping[str, Any], str, Mapping[str, Any]], bool]


# ``collect_metrics`` reads a deliberately broad telemetry surface.  Several
# aliases and contract summaries ask for the same quality path more than once
# during one strategy evaluation.  Keep the cache scoped to that evaluation so
# no snapshot mutation or cross-snapshot state can affect a later decision.
_QUALITY_STATUS_CACHE: ContextVar[Optional[Dict[Tuple[int, str], str]]] = ContextVar(
    "oni_strategy_quality_status_cache", default=None
)


class _ProofBoundState(dict):
    """A process-local proof context that JSON input cannot manufacture."""

    def __init__(self, source: Mapping[str, Any], verifier: TrustedProofVerifier) -> None:
        super().__init__(source)
        self.trusted_proof_verifier = verifier


class PhaseEvidenceTracker:
    """Session-bound rolling proof for sequential phase certification.

    Forward promotion requires consecutive snapshots for the current phase's
    exit gate.  Later-phase gates do not accumulate early.  A safety regression
    is immediate, while duplicate render calls for one snapshot never increase
    evidence.
    """

    def __init__(self) -> None:
        self.reset(None)

    def reset(self, session_id: Optional[str]) -> None:
        self.session_id = session_id
        self.current_phase = "bootstrap"
        self.last_snapshot_id: Optional[str] = None
        self.last_seq: Optional[int] = None
        self.last_snapshot_seq: Optional[int] = None
        self.last_timestamp: Optional[dt.datetime] = None
        self.last_game_time_cycles: Optional[float] = None
        self.last_sample_interval_seconds: Optional[float] = None
        self.last_world_id: Optional[int] = None
        self.last_save_id_hash: Optional[str] = None
        self.last_state_hash: Optional[str] = None
        # ``observe_validated`` may defer the expensive full-payload digest
        # until a duplicate snapshot actually needs comparison.  The producer
        # owns this mapping for the lifetime of one tracker sample.
        self._last_strategy: Optional[Mapping[str, Any]] = None
        self.last_transition: Optional[Dict[str, Any]] = None
        self.integrity_blockers: List[str] = []
        self.critical_incident_count = 0
        self._reset_window()

    def _reset_window(self) -> None:
        self.consecutive_samples = 0
        # The session total is retained separately. This counter describes
        # only the current candidate window (including its invalidating sample).
        self.window_critical_incident_count = 0
        self.window_start_seq: Optional[int] = None
        self.window_end_seq: Optional[int] = None
        self.window_start_snapshot_seq: Optional[int] = None
        self.window_end_snapshot_seq: Optional[int] = None
        self.window_start_timestamp: Optional[dt.datetime] = None
        self.window_end_timestamp: Optional[dt.datetime] = None
        self.window_start_game_time_cycles: Optional[float] = None
        self.window_end_game_time_cycles: Optional[float] = None
        self.window_input_hash: Optional[str] = None

    def update(self, strategy: Mapping[str, Any]) -> Dict[str, Any]:
        validate_strategy(strategy)
        result = copy.deepcopy(dict(strategy))
        self._consume(result)
        return self._render(result)

    def observe(self, strategy: Mapping[str, Any]) -> None:
        """Accumulate one validated sample without rendering a full payload.

        Replay ingestion may process thousands of snapshots before it publishes
        only the final decision generation.  Keeping the rolling proof is
        mandatory, while deep-copying and rendering every discarded strategy
        payload is not.
        """

        validate_strategy(strategy)
        self._consume(strategy)

    def observe_validated(self, strategy: Mapping[str, Any]) -> None:
        """Accumulate a strategy already validated by its producer.

        The Bridge evaluates each trusted snapshot with :func:`evaluate_strategy`,
        which validates the complete public strategy before returning it.  Calling
        :meth:`observe` afterwards would repeat that full schema walk for every
        replayed snapshot.  This narrow seam preserves the public ``observe``
        contract while allowing the trusted producer to avoid duplicate work.
        Callers outside the Bridge should continue to use :meth:`observe`.
        """

        self._consume(strategy, lazy_duplicate_hash=True)

    def _consume(self, strategy: Mapping[str, Any], *, lazy_duplicate_hash: bool = False) -> None:
        session_id = str(strategy.get("session_id") or "") or None
        if self.session_id != session_id:
            self.reset(session_id)

        snapshot_id = str(strategy.get("snapshot_id") or "") or None
        sequence = strategy.get("seq")
        sequence_value = sequence if isinstance(sequence, int) and not isinstance(sequence, bool) else None
        snapshot_sequence = strategy.get("snapshot_seq")
        snapshot_sequence_value = (
            snapshot_sequence
            if isinstance(snapshot_sequence, int) and not isinstance(snapshot_sequence, bool) and snapshot_sequence > 0
            else None
        )
        timestamp = self._parse_tracker_timestamp(strategy.get("ts"))
        metrics = strategy.get("metrics") if isinstance(strategy.get("metrics"), Mapping) else {}
        game_time_cycles = _number(metrics.get("game_time_cycles"))
        cycle = _number(metrics.get("cycle"))
        sample_interval = _number(metrics.get("sample_interval_seconds"))
        world = strategy.get("world_id")
        world_id = world if isinstance(world, int) and not isinstance(world, bool) and world >= 0 else None
        save_id_hash = str(strategy.get("save_id_hash") or "") or None
        duplicate = snapshot_id is not None and snapshot_id == self.last_snapshot_id
        state_hash = None if lazy_duplicate_hash else canonical_state_hash(dict(strategy))

        if duplicate:
            previous_hash = self.last_state_hash
            if previous_hash is None and self._last_strategy is not None:
                previous_hash = canonical_state_hash(dict(self._last_strategy))
            current_hash = state_hash or canonical_state_hash(dict(strategy))
            if current_hash != previous_hash:
                self.integrity_blockers = ["strategy_duplicate_conflict"]
                self._reset_window()
            elif self.last_state_hash is None:
                # Cache the first deferred comparison so repeated duplicate
                # renders do not re-hash both full strategy payloads.
                self.last_state_hash = current_hash
            state_hash = current_hash
        else:
            self.integrity_blockers = []
            if sequence_value is None or (self.last_seq is not None and sequence_value <= self.last_seq):
                self.integrity_blockers.append("strategy_sequence_invalid")
            if snapshot_sequence_value is None or (
                self.last_snapshot_seq is not None
                and snapshot_sequence_value != self.last_snapshot_seq + 1
            ):
                self.integrity_blockers.append("strategy_snapshot_sequence_invalid")
            if timestamp is None or (self.last_timestamp is not None and timestamp <= self.last_timestamp):
                self.integrity_blockers.append("strategy_timestamp_invalid")
            if sample_interval is None or sample_interval <= 0.0:
                self.integrity_blockers.append("strategy_sample_interval_invalid")
            elif self.last_sample_interval_seconds is not None and not math.isclose(
                sample_interval,
                self.last_sample_interval_seconds,
                rel_tol=0.0,
                abs_tol=1e-6,
            ):
                self.integrity_blockers.append("strategy_sample_interval_changed")
            if (
                timestamp is not None
                and self.last_timestamp is not None
                and sample_interval is not None
                and (timestamp - self.last_timestamp).total_seconds()
                > sample_interval * (1.0 + PHASE_EVIDENCE_MAX_JITTER_RATIO)
            ):
                self.integrity_blockers.append("strategy_snapshot_gap")
            if game_time_cycles is None or (
                game_time_cycles < 1.0
                or (
                    self.last_game_time_cycles is not None
                    and game_time_cycles < self.last_game_time_cycles
                )
            ):
                self.integrity_blockers.append("strategy_game_time_invalid")
            if cycle is None or cycle < 1.0 or not cycle.is_integer():
                self.integrity_blockers.append("strategy_cycle_invalid")
            if (
                cycle is not None
                and game_time_cycles is not None
                # Both Probe clocks are display-1-based. Continuous time may
                # be fractional; six-decimal native rounding can reach the
                # next integer while ReadCycle still reports this cycle.
                and not cycle <= game_time_cycles <= cycle + 1.0
            ):
                self.integrity_blockers.append("strategy_cycle_mismatch")
            if world_id is None:
                self.integrity_blockers.append("strategy_world_invalid")
            elif self.last_world_id is not None and world_id != self.last_world_id:
                self.integrity_blockers.append("strategy_world_changed")
            if save_id_hash is None:
                self.integrity_blockers.append("strategy_save_identity_missing")
            elif self.last_save_id_hash is not None and save_id_hash != self.last_save_id_hash:
                self.integrity_blockers.append("strategy_save_identity_changed")

            trust_ready = (strategy.get("telemetry") or {}).get("trust_ready") is True
            detected = str((strategy.get("phase") or {}).get("detected_from_single_snapshot") or "bootstrap")
            if detected not in PHASE_ORDER:
                detected = "bootstrap"
                self.integrity_blockers.append("strategy_detected_phase_invalid")

            if not trust_ready or self.integrity_blockers:
                self._reset_window()
            else:
                detected_index = PHASE_ORDER.index(detected)
                current_index = PHASE_ORDER.index(self.current_phase)
                safety_regression = detected_index < current_index
                if safety_regression:
                    previous = self.current_phase
                    self.current_phase = detected
                    self.last_transition = {
                        "kind": "safety_regression",
                        "from": previous,
                        "to": detected,
                        "snapshot_id": snapshot_id,
                    }

                critical_risk = any(
                    item.get("status") == "at_risk"
                    for item in strategy.get("objectives") or []
                    if isinstance(item, Mapping) and item.get("id") in SAFETY_CRITICAL_OBJECTIVES
                )
                active_gate = self._gate(strategy, self.current_phase)
                if safety_regression or critical_risk:
                    # Count one incident per distinct trusted snapshot, even
                    # when both phase regression and several risks apply. A
                    # different phase's critical risk also breaks this window.
                    self.critical_incident_count += 1
                    self._reset_window()
                    self.window_critical_incident_count = 1
                elif active_gate is not None and active_gate.get("status") == "satisfied":
                    if self.consecutive_samples == 0:
                        # The first clean sample starts a fresh candidate; an
                        # earlier incident remains in the session diagnostic.
                        self.window_critical_incident_count = 0
                        self.window_start_seq = sequence_value
                        self.window_start_snapshot_seq = snapshot_sequence_value
                        self.window_start_timestamp = timestamp
                        self.window_start_game_time_cycles = game_time_cycles
                    self.consecutive_samples += 1
                    self.window_end_seq = sequence_value
                    self.window_end_snapshot_seq = snapshot_sequence_value
                    self.window_end_timestamp = timestamp
                    self.window_end_game_time_cycles = game_time_cycles
                    self._append_window_hash(strategy)
                else:
                    self._reset_window()

                if self.current_phase != "sustain" and self._window_requirements_met(self.current_phase):
                    previous = self.current_phase
                    duration_seconds, duration_cycles = self._window_durations()
                    self.current_phase = PHASE_ORDER[PHASE_ORDER.index(previous) + 1]
                    self.last_transition = {
                        "kind": "trusted_window_completed",
                        "from": previous,
                        "to": self.current_phase,
                        "snapshot_id": snapshot_id,
                        "required_samples": PHASE_STABILITY_SAMPLES[previous],
                        "duration_seconds": round(duration_seconds, 6),
                        "duration_game_cycles": round(duration_cycles, 6),
                        "input_hash": self.window_input_hash,
                    }
                    self._reset_window()

            self.last_snapshot_id = snapshot_id
            self.last_seq = sequence_value
            self.last_snapshot_seq = snapshot_sequence_value
            self.last_timestamp = timestamp
            self.last_game_time_cycles = game_time_cycles
            self.last_sample_interval_seconds = sample_interval
            self.last_world_id = world_id
            self.last_save_id_hash = save_id_hash
            self.last_state_hash = state_hash
            self._last_strategy = strategy

    @staticmethod
    def _parse_tracker_timestamp(value: Any) -> Optional[dt.datetime]:
        if not isinstance(value, str) or not value:
            return None
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed

    def _append_window_hash(self, strategy: Mapping[str, Any]) -> None:
        state_hash = str(strategy.get("state_hash") or "")
        payload = "|".join(
            (
                self.window_input_hash or "window-start",
                state_hash,
                str(strategy.get("snapshot_id") or ""),
                str(strategy.get("snapshot_seq") or ""),
                str(strategy.get("seq") or ""),
            )
        )
        self.window_input_hash = "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _window_durations(self) -> Tuple[float, float]:
        duration_seconds = 0.0
        duration_cycles = 0.0
        if self.window_start_timestamp is not None and self.window_end_timestamp is not None:
            duration_seconds = max(
                0.0,
                (self.window_end_timestamp - self.window_start_timestamp).total_seconds(),
            )
        if (
            self.window_start_game_time_cycles is not None
            and self.window_end_game_time_cycles is not None
        ):
            duration_cycles = max(
                0.0,
                self.window_end_game_time_cycles - self.window_start_game_time_cycles,
            )
        return duration_seconds, duration_cycles

    def _window_requirements_met(self, phase: str) -> bool:
        duration_seconds, duration_cycles = self._window_durations()
        return bool(
            self.consecutive_samples >= PHASE_STABILITY_SAMPLES[phase]
            and duration_seconds >= PHASE_STABILITY_MIN_SECONDS[phase]
            and duration_cycles >= PHASE_STABILITY_MIN_GAME_CYCLES[phase]
            and self.window_input_hash
            and self.window_critical_incident_count == 0
            and not self.integrity_blockers
        )

    @staticmethod
    def _gate(strategy: Mapping[str, Any], phase: str) -> Optional[Dict[str, Any]]:
        for gate in strategy.get("gates") or []:
            if isinstance(gate, dict) and gate.get("phase") == phase:
                return gate
        return None

    def _render(self, strategy: Dict[str, Any]) -> Dict[str, Any]:
        phase = strategy["phase"]
        phase["id"] = self.current_phase
        phase["certified"] = self.current_phase
        phase["order"] = PHASE_ORDER.index(self.current_phase)
        phase["target"] = PHASE_ORDER[min(phase["order"] + 1, len(PHASE_ORDER) - 1)]

        active_gate = self._gate(strategy, self.current_phase)
        active_status = str((active_gate or {}).get("status") or "blocked")
        required = PHASE_STABILITY_SAMPLES[self.current_phase]
        minimum_seconds = PHASE_STABILITY_MIN_SECONDS[self.current_phase]
        minimum_game_cycles = PHASE_STABILITY_MIN_GAME_CYCLES[self.current_phase]
        duration_seconds, duration_game_cycles = self._window_durations()
        stable_verified = (
            self.current_phase == "sustain"
            and active_status == "satisfied"
            and self._window_requirements_met(self.current_phase)
            and (strategy.get("telemetry") or {}).get("trust_ready") is True
        )
        blockers = list((active_gate or {}).get("blockers") or []) + list(self.integrity_blockers)
        blockers.extend(_sustainability_readiness_blockers(strategy.get("metrics") or {}))
        if active_status == "satisfied" and not stable_verified:
            if self.consecutive_samples < required:
                blockers.append("stable_window_sample_count_incomplete")
            if duration_seconds < minimum_seconds:
                blockers.append("stable_window_wall_duration_incomplete")
            if duration_game_cycles < minimum_game_cycles:
                blockers.append("stable_window_game_duration_incomplete")
        readiness_status = (
            "blocked"
            if (strategy.get("telemetry") or {}).get("trust_ready") is not True or self.integrity_blockers
            else "ready"
            if stable_verified
            else "not_ready"
            if active_status in {"satisfied", "at_risk"}
            else "unknown"
        )
        strategy["readiness"] = {
            "status": readiness_status,
            "next_phase": phase["target"],
            "blockers": sorted(set(blockers)),
            "required_consecutive_samples": required,
            "observed_consecutive_samples": self.consecutive_samples,
            "required_duration_seconds": minimum_seconds,
            "observed_duration_seconds": round(duration_seconds, 6),
            "required_game_cycles": minimum_game_cycles,
            "observed_game_cycles": round(duration_game_cycles, 6),
            "stable_window_verified": stable_verified,
        }
        phase["active_gate"] = self.current_phase
        phase["confidence"] = (
            "blocked"
            if readiness_status == "blocked"
            else "high"
            if readiness_status == "ready"
            else "medium"
            if active_status == "satisfied"
            else "low"
        )
        strategy["phase_evidence"] = {
            "schema": "oni.phase-evidence.v1",
            "session_id": self.session_id,
            "last_snapshot_id": self.last_snapshot_id,
            "current_phase": self.current_phase,
            "consecutive_samples": self.consecutive_samples,
            "required_samples": required,
            "required_duration_seconds": minimum_seconds,
            "required_game_cycles": minimum_game_cycles,
            "observed_duration_seconds": round(duration_seconds, 6),
            "observed_game_cycles": round(duration_game_cycles, 6),
            "integrity_blockers": list(self.integrity_blockers),
            "critical_incident_count": self.critical_incident_count,
            "last_transition": copy.deepcopy(self.last_transition),
            "evidence_window": {
                "schema": "oni.evidence-window.v1",
                "phase": self.current_phase,
                "session_id": self.session_id,
                "start_seq": self.window_start_seq,
                "end_seq": self.window_end_seq,
                "start_snapshot_seq": self.window_start_snapshot_seq,
                "end_snapshot_seq": self.window_end_snapshot_seq,
                "start_ts": self.window_start_timestamp.isoformat() if self.window_start_timestamp else None,
                "end_ts": self.window_end_timestamp.isoformat() if self.window_end_timestamp else None,
                "start_game_time_cycles": self.window_start_game_time_cycles,
                "end_game_time_cycles": self.window_end_game_time_cycles,
                "sample_count": self.consecutive_samples,
                "gap_free": not self.integrity_blockers,
                "input_hash": self.window_input_hash,
                "critical_incidents": self.window_critical_incident_count,
            },
        }
        validate_strategy(strategy)
        return strategy


def _safe_text(value: Any, limit: int = 500) -> str:
    """Normalize text and redact obvious credential-shaped values."""

    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    if _API_SECRET_RE.search(text) is not None:
        text = _API_SECRET_RE.sub("[redacted]", text)
    return text[:limit]


def _number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            number = float(value)
        except (OverflowError, ValueError):
            return None
    else:
        return None
    if not math.isfinite(number):
        return None
    return number


def _sustainability_readiness_blockers(metrics: Mapping[str, Any]) -> List[str]:
    """Keep marker uncertainty visible before and after tracker rendering."""

    if metrics.get("qualities", {}).get("sustainable") == "unsupported":
        return ["sustainability_telemetry_unsupported"]
    if metrics.get("sustainable") is None:
        return ["sustainability_telemetry_unverified"]
    if metrics.get("sustainable") is False:
        return ["sustainability_observed_false"]
    return []


def _capture_binding_blockers(
    state: Mapping[str, Any],
    entry: Mapping[str, Any],
    *,
    max_age_seconds: Optional[float] = None,
) -> List[str]:
    """Validate that quality provenance points at a real bounded timestamp."""

    binding = entry.get("captured_at")
    if not isinstance(binding, str) or not binding:
        return ["quality_capture_binding_missing"]
    captured_value = state.get("ts") if binding == "snapshot.ts" else safe_get(dict(state), binding)
    captured_at = parse_snapshot_timestamp(captured_value)
    snapshot_at = parse_snapshot_timestamp(state.get("ts"))
    if captured_at is None:
        return ["quality_capture_timestamp_invalid"]
    if snapshot_at is None:
        return ["snapshot_timestamp_invalid"]
    age_seconds = (snapshot_at - captured_at).total_seconds()
    # Probe sub-read completion can be milliseconds after the envelope time;
    # allow only the same five-second skew budget as snapshot admission.
    if age_seconds < -5.0:
        return ["quality_capture_in_future"]
    if max_age_seconds is not None and age_seconds > max_age_seconds:
        return ["quality_capture_stale"]
    return []


def _quality_status(state: Mapping[str, Any], path: str) -> str:
    state_dict = state if isinstance(state, dict) else dict(state)
    cache = _QUALITY_STATUS_CACHE.get()
    cache_key = (id(state_dict), path)
    if cache is not None:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
    entry = field_quality(state_dict, path)
    status = str(entry.get("status") or "unsupported")
    contract = CERTIFYING_FIELD_CONTRACTS.get(path)
    if status in {"observed", "estimated"} and contract is not None:
        contract_arguments = dict(contract)
        capture_max_age_seconds = contract_arguments.pop("capture_max_age_seconds", None)
        trusted_verifier = getattr(state_dict, "trusted_proof_verifier", None)
        proof_verifier = (
            (lambda candidate: trusted_verifier(state_dict, path, candidate))
            if callable(trusted_verifier)
            else None
        )
        contract_matches = quality_contract_matches(
            entry,
            allowed_statuses=(status,),
            proof_verifier=proof_verifier,
            **contract_arguments,
        )
        if not contract_matches:
            status = "contract_mismatch"
        elif _capture_binding_blockers(
            state_dict,
            entry,
            max_age_seconds=capture_max_age_seconds,
        ):
            status = "contract_mismatch"
    if cache is not None:
        cache[cache_key] = status
    return status


def _field_contract_blockers(state: Optional[Mapping[str, Any]], path: str) -> List[str]:
    if not isinstance(state, Mapping):
        return ["state_missing"]
    contract = CERTIFYING_FIELD_CONTRACTS.get(path)
    if contract is None:
        return []
    state_dict = state if isinstance(state, dict) else dict(state)
    contract_arguments = dict(contract)
    capture_max_age_seconds = contract_arguments.pop("capture_max_age_seconds", None)
    trusted_verifier = getattr(state_dict, "trusted_proof_verifier", None)
    proof_verifier = (
        (lambda candidate: trusted_verifier(state_dict, path, candidate))
        if callable(trusted_verifier)
        else None
    )
    entry = field_quality(state_dict, path)
    blockers = quality_contract_blockers(
        entry,
        proof_verifier=proof_verifier,
        **contract_arguments,
    )
    blockers.extend(
        _capture_binding_blockers(
            state_dict,
            entry,
            max_age_seconds=capture_max_age_seconds,
        )
    )
    return sorted(set(blockers))


def _power_battery_percent_v2_blockers(state: Optional[Mapping[str, Any]]) -> List[str]:
    """Require the bounded power-v2 tuple before consuming battery percent."""

    if not isinstance(state, Mapping):
        return ["power_telemetry_missing"]
    state_dict = state if isinstance(state, dict) else dict(state)
    power = state_dict.get("power")
    if not isinstance(power, Mapping):
        return ["power_telemetry_missing"]

    blockers: List[str] = []
    mode = power.get("storage_mode")
    if mode == "unbounded":
        blockers.append("battery_percent_unavailable:unbounded_storage")
    elif mode != "bounded":
        blockers.append("power_storage_mode_invalid")

    stored = _number(power.get("stored_joules"))
    capacity = _number(power.get("capacity_joules"))
    percent = _number(power.get("battery_percent"))
    if stored is None or not (0.0 <= stored <= _POWER_MAXIMUM_JOULES):
        blockers.append("power_stored_joules_invalid")
    if capacity is None or not (0.0 < capacity <= _POWER_MAXIMUM_JOULES):
        blockers.append("power_capacity_joules_invalid")
    if stored is not None and capacity is not None and stored > capacity:
        blockers.append("power_stored_exceeds_capacity")
    if percent is None or not (0.0 <= percent <= 100.0):
        blockers.append("power_battery_percent_invalid")
    elif stored is not None and capacity is not None and capacity > 0.0:
        expected = stored / capacity * 100.0
        if abs(percent - expected) > _POWER_PERCENT_TOLERANCE:
            blockers.append("power_battery_percent_inconsistent")

    for path in (
        "power.storage_mode",
        "power.stored_joules",
        "power.capacity_joules",
        "power.battery_percent",
    ):
        blockers.extend(
            f"{path}:{blocker}" for blocker in _field_contract_blockers(state_dict, path)
        )
    return sorted(set(blockers))


def _read_power_battery_percent_v2(
    state: Optional[Mapping[str, Any]],
) -> Tuple[Optional[float], Optional[str], str]:
    path = "power.battery_percent"
    blockers = _power_battery_percent_v2_blockers(state)
    if blockers:
        return None, path, "contract_mismatch"
    state_dict = state if isinstance(state, dict) else dict(state or {})
    return _number(safe_get(state_dict, path)), path, "observed"


def _observation_scope_blockers(state: Optional[Mapping[str, Any]]) -> List[str]:
    """Require a complete, internally coherent set of populated worlds."""

    if not isinstance(state, Mapping):
        return ["observation_scope_missing"]
    state_dict = state if isinstance(state, dict) else dict(state)
    path = "observation_scope.complete_for_populated_worlds"
    blockers: List[str] = []
    for scope_path in (
        "observation_scope.active_world_id",
        "observation_scope.populated_world_ids",
        "observation_scope.covered_world_ids",
        path,
    ):
        blockers.extend(
            f"{scope_path}:{blocker}"
            for blocker in _field_contract_blockers(state_dict, scope_path)
        )
    if safe_get(state_dict, path) is not True:
        blockers.append("populated_world_coverage_incomplete")

    scope = state_dict.get("observation_scope")
    populated = scope.get("populated_world_ids") if isinstance(scope, Mapping) else None
    covered = scope.get("covered_world_ids") if isinstance(scope, Mapping) else None
    active_world = scope.get("active_world_id") if isinstance(scope, Mapping) else None

    def exact_world_ids(value: Any) -> Optional[List[int]]:
        if not isinstance(value, list):
            return None
        if any(isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in value):
            return None
        if len(value) != len(set(value)):
            return None
        return value

    populated_ids = exact_world_ids(populated)
    covered_ids = exact_world_ids(covered)
    if not populated_ids:
        blockers.append("populated_world_ids_missing")
    if not covered_ids:
        blockers.append("covered_world_ids_missing")
    if populated_ids is not None and covered_ids is not None and not set(populated_ids).issubset(covered_ids):
        blockers.append("populated_world_not_covered")
    if isinstance(active_world, bool) or not isinstance(active_world, int) or active_world < 0:
        blockers.append("active_world_id_invalid")
    else:
        if covered_ids is not None and active_world not in covered_ids:
            blockers.append("active_world_not_covered")
        if covered_ids != [active_world]:
            blockers.append("covered_world_ids_not_active_world_only")
        if safe_get(state_dict, path) is True and populated_ids != [active_world]:
            blockers.append("complete_populated_world_ids_not_active_world_only")
    top_level_world = state_dict.get("world_id")
    if (
        isinstance(active_world, int)
        and not isinstance(active_world, bool)
        and isinstance(top_level_world, int)
        and not isinstance(top_level_world, bool)
        and active_world != top_level_world
    ):
        blockers.append("active_world_identity_mismatch")
    return sorted(set(blockers))


def _read(
    state: Optional[Mapping[str, Any]],
    paths: Sequence[str],
    *,
    allow_estimated: bool = True,
) -> Tuple[Any, Optional[str], str]:
    """Read the first usable alias and return value, path, and quality."""

    if not isinstance(state, Mapping):
        return None, None, "unsupported"
    state_dict = state if isinstance(state, dict) else dict(state)
    for path in paths:
        status = _quality_status(state_dict, path)
        usable = status == "observed" or (allow_estimated and status == "estimated")
        if not usable:
            continue
        marker = object()
        value = safe_get(state_dict, path, marker)
        if value is marker:
            continue
        return value, path, status
    # Preserve the path/status for diagnostics, but never return a blocked raw
    # value as decision input.  Estimated/unsupported/stale/error values may
    # raise risk elsewhere, but cannot silently satisfy an observed-only gate.
    for path in paths:
        marker = object()
        value = safe_get(state_dict, path, marker)
        if value is not marker:
            return None, path, _quality_status(state_dict, path)
    return None, None, "unsupported"


def _read_number(
    state: Optional[Mapping[str, Any]],
    paths: Sequence[str],
    *,
    allow_estimated: bool = True,
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
) -> Tuple[Optional[float], Optional[str], str]:
    value, path, status = _read(state, paths, allow_estimated=allow_estimated)
    number = _number(value)
    if number is not None and (
        (minimum is not None and number < minimum)
        or (maximum is not None and number > maximum)
    ):
        return None, path, "value_out_of_range"
    return number, path, status


def _read_int(
    state: Optional[Mapping[str, Any]], paths: Sequence[str]
) -> Tuple[Optional[int], Optional[str], str]:
    value, path, status = _read_number(state, paths, allow_estimated=False, minimum=0.0)
    return (int(value) if value is not None and value.is_integer() else None), path, status


def _read_registered_food_diagnostic(
    state: Optional[Mapping[str, Any]],
) -> Tuple[Optional[float], Optional[str], str]:
    """Read Probe's registered/non-private food sum for risk only."""

    path = "resources.food_kcal"
    if not isinstance(state, Mapping):
        return None, None, "unsupported"
    state_dict = state if isinstance(state, dict) else dict(state)
    observed = read_contract_field(
        state_dict,
        path,
        REGISTERED_FOOD_CONTRACT_ID,
        allowed_statuses=("observed", "estimated"),
    )
    if not observed.usable:
        return None, path, "contract_mismatch"
    value = _number(observed.value)
    if value is None or value < 0.0:
        return None, path, "value_out_of_range"
    return value, path, observed.status


def _read_bool(
    state: Optional[Mapping[str, Any]], paths: Sequence[str]
) -> Tuple[Optional[bool], Optional[str], str]:
    value, path, status = _read(state, paths, allow_estimated=False)
    if isinstance(value, bool):
        return value, path, status
    return None, path, status


def _read_industry_deliverable(
    state: Optional[Mapping[str, Any]], domain: str
) -> Tuple[Optional[bool], Optional[str], str, Tuple[str, ...]]:
    """Read one industrial deliverable with an observed, bounded contract.

    A probe may report either an explicit completion boolean or an actual vs
    required quantity pair.  Both forms are accepted only when their quality
    metadata is ``observed`` and the trusted proof verifier accepts it.  A
    present but estimated/unsupported/malformed field is retained as unknown
    and never bypassed by a permissive alias.
    """

    bool_paths = _INDUSTRY_FIELD_PATHS[domain]
    value, path, status = _read_bool(state, bool_paths)
    if value is not None:
        return value, path, status, (path,) if path else ()
    # A present boolean claim that failed its contract must not fall through to
    # a quantity alias from the same snapshot.
    if isinstance(state, Mapping):
        quality = state.get("quality") if isinstance(state.get("quality"), Mapping) else {}
        marker = object()
        for candidate in bool_paths:
            if safe_get(state, candidate, marker) is not marker or candidate in quality:
                return None, candidate, status, (candidate,)

    actual_paths, required_paths, _unit = _INDUSTRY_QUANTITY_PATHS[domain]
    actual, actual_path, actual_status = _read_number(
        state, actual_paths, allow_estimated=False, minimum=0.0
    )
    required, required_path, required_status = _read_number(
        state, required_paths, allow_estimated=False, minimum=0.0
    )
    evidence_paths = tuple(path for path in (actual_path, required_path) if path)
    if actual is None or required is None:
        selected_path = actual_path or required_path
        selected_status = actual_status if actual is None else required_status
        return None, selected_path, selected_status, evidence_paths
    if actual_status != "observed" or required_status != "observed":
        return None, actual_path or required_path, "unknown", evidence_paths
    return actual >= required, actual_path or required_path, "observed", evidence_paths


def _read_functional_building_operational(
    state: Optional[Mapping[str, Any]],
    prefab_id: str | Sequence[str],
) -> Tuple[Optional[bool], Optional[str], str]:
    """Project trusted functional-building telemetry to one prefab tri-state.

    The legacy completed-prefab count is intentionally separate.  A wholly
    absent functional field preserves old snapshot compatibility, while any
    present-but-unusable functional claim fails closed instead of falling back
    to that count.
    """

    path = "functional_buildings"
    if not isinstance(state, Mapping):
        return None, None, "unsupported"
    state_dict = state if isinstance(state, dict) else dict(state)
    marker = object()
    field_present = safe_get(state_dict, path, marker) is not marker
    quality = state_dict.get("quality")
    quality_present = isinstance(quality, Mapping) and path in quality
    if not field_present and not quality_present:
        return None, None, "unsupported"

    # A globally stale snapshot is not a current operational proof even when
    # the nested producer envelope itself still satisfies its contract.
    if _state_is_stale(state_dict):
        return None, path, "stale"

    reading = read_contract_field(
        state_dict,
        path,
        FUNCTIONAL_BUILDINGS_CONTRACT_ID,
        allowed_statuses=("observed",),
    )
    if not reading.usable or reading.status != "observed":
        return None, path, "contract_mismatch"

    payload = reading.value
    instances = payload.get("instances") if isinstance(payload, Mapping) else None
    if not isinstance(instances, list):
        # This is defensive; the shared contract has already validated the
        # payload shape before this value is consumed.
        return None, path, "contract_mismatch"
    prefab_ids = (prefab_id,) if isinstance(prefab_id, str) else tuple(prefab_id)
    values = [
        instance.get("operational")
        for instance in instances
        if isinstance(instance, Mapping) and instance.get("prefab_id") in prefab_ids
    ]
    if not values:
        return None, path, "unknown"
    if any(value is False for value in values):
        return False, path, "observed"
    if all(value is True for value in values):
        return True, path, "observed"
    return None, path, "unknown"


def _read_functional_outhouse_operational(
    state: Optional[Mapping[str, Any]],
) -> Tuple[Optional[bool], Optional[str], str]:
    """Compatibility wrapper for the sanitation objective."""

    return _read_functional_building_operational(state, "Outhouse")


def _read_strict_primary(
    state: Optional[Mapping[str, Any]],
    path: str,
    contract_id: str,
    *,
    legacy_paths: Sequence[str] = (),
) -> Tuple[Any, Optional[str], str]:
    """Read a strict field, falling back only when it is wholly absent.

    A present primary value, or primary quality metadata without a value, is a
    producer claim and must validate against the shared field contract. It can
    never be bypassed by a more permissive legacy alias.
    """

    if not isinstance(state, Mapping):
        return None, None, "unsupported"
    state_dict = state if isinstance(state, dict) else dict(state)
    marker = object()
    primary_value = safe_get(state_dict, path, marker)
    quality = state_dict.get("quality")
    primary_quality_present = isinstance(quality, Mapping) and path in quality
    if primary_value is marker and not primary_quality_present:
        if legacy_paths:
            return _read(state_dict, legacy_paths, allow_estimated=False)
        return None, None, "unsupported"

    observed = read_contract_field(
        state_dict,
        path,
        contract_id,
        allowed_statuses=("observed",),
    )
    if not observed.usable:
        return None, path, "contract_mismatch"
    return observed.value, path, observed.status


def _read_strict_int(
    state: Optional[Mapping[str, Any]],
    path: str,
    contract_id: str,
    *,
    legacy_paths: Sequence[str] = (),
) -> Tuple[Optional[int], Optional[str], str]:
    value, resolved_path, status = _read_strict_primary(
        state,
        path,
        contract_id,
        legacy_paths=legacy_paths,
    )
    if resolved_path == path:
        # read_contract_field already rejected bools, fractions, negatives and
        # values above the shared count ceiling.
        return (value if isinstance(value, int) and not isinstance(value, bool) else None), resolved_path, status
    number = _number(value)
    if number is None or number < 0.0 or not number.is_integer():
        return None, resolved_path, status
    return int(number), resolved_path, status


def _as_alerts(value: Any) -> Optional[List[str]]:
    if not isinstance(value, list):
        return None
    if any(not isinstance(item, str) or item not in _CANONICAL_ALERT_IDS for item in value):
        return None
    if len(value) != len(set(value)) or value != sorted(value):
        return None
    return list(value)


def _contains_alert(alerts: Optional[Sequence[str]], alert_ids: Sequence[str]) -> Optional[bool]:
    if alerts is None:
        return None
    exact_ids = frozenset(alert_ids)
    return bool(exact_ids.intersection(alerts))


def _status_from_risk(value: Optional[float], critical: float, target: float) -> str:
    if value is None:
        return "unknown"
    if value < critical or value < target:
        return "at_risk"
    return "satisfied"


def _required_quality(state: Optional[Mapping[str, Any]], paths: Iterable[str]) -> Dict[str, Any]:
    observed = estimated = blocked = 0
    details: Dict[str, str] = {}
    for path in paths:
        status = _quality_status(state or {}, path)
        details[path] = status
        if status == "observed":
            observed += 1
        elif status == "estimated":
            estimated += 1
        else:
            blocked += 1
    return {"observed": observed, "estimated": estimated, "blocked": blocked, "fields": details}


def _state_is_stale(state: Optional[Mapping[str, Any]]) -> bool:
    if not isinstance(state, Mapping):
        return True
    bridge = state.get("bridge")
    return isinstance(bridge, Mapping) and bool(bridge.get("stale"))


def _state_identity(state: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    value = state if isinstance(state, Mapping) else {}
    return {
        "session_id": _safe_text(value.get("session_id"), 160) or None,
        "save_id_hash": _safe_text(value.get("save_id_hash"), 160) or None,
        "save_dirty": value.get("save_dirty") if isinstance(value.get("save_dirty"), bool) else None,
        "world_id": value.get("world_id"),
        "snapshot_id": _safe_text(value.get("snapshot_id"), 160) or None,
        "snapshot_seq": value.get("snapshot_seq"),
        "seq": value.get("seq"),
        "ts": _safe_text(value.get("ts"), 160) or None,
        "game_build": _safe_text(value.get("game_build"), 120) or None,
        "mod_version": _safe_text(value.get("mod_version"), 120) or None,
        "cycle": value.get("cycle"),
        "state_hash": canonical_state_hash(dict(value)),
    }


def _objective(
    objective_id: str,
    title: str,
    *,
    phase: str,
    priority: int,
    status: str,
    reason: str,
    evidence: Iterable[str],
    completion: Iterable[str],
    recommended: Iterable[Dict[str, Any]] = (),
    telemetry: Iterable[str] = (),
) -> Dict[str, Any]:
    if status not in OBJECTIVE_STATUSES:
        raise StrategyError(f"Unknown objective status: {status}")
    return {
        "id": objective_id,
        "title": title,
        "phase": phase,
        "priority": int(priority),
        "status": status,
        "reason": _safe_text(reason, 600),
        "evidence": sorted({_safe_text(item, 200) for item in evidence if item}),
        "completion_criteria": [_safe_text(item, 300) for item in completion],
        "required_telemetry": sorted({_safe_text(item, 200) for item in telemetry if item}),
        "recommended_actions": [dict(item) for item in recommended],
    }


def _recommendation(
    recommendation_id: str,
    action: str,
    reason: str,
    *,
    risk: str = "low",
    args: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Create a non-executable operator recommendation."""

    return {
        "id": recommendation_id,
        "kind": "operator_recommendation",
        "action": action,
        "args": dict(args or {}),
        "risk": risk,
        "requires_approval": True,
        "auto_execute": False,
        "reason": _safe_text(reason, 500),
    }


def collect_metrics(state: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Collect a bounded, JSON-safe metric view from one state snapshot."""

    cycle, cycle_path, cycle_quality = _read_number(
        state, ("cycle",), allow_estimated=False, minimum=1.0
    )
    game_time_cycles, game_time_path, game_time_quality = _read_number(
        state, ("game_time_cycles",), allow_estimated=False, minimum=1.0
    )
    sample_interval = trusted_sample_interval_seconds(
        state if isinstance(state, dict) else dict(state) if isinstance(state, Mapping) else None
    )
    dupes, dupes_path, dupes_quality = _read_number(
        state, ("duplicants.total", "colony.duplicants"), allow_estimated=True, minimum=0.0
    )
    food, food_path, food_quality = _read_number(
        state, ("resources.food_kcal",), allow_estimated=True, minimum=0.0
    )
    food_accessible_contract = food is not None
    registered_food = None
    registered_food_path = None
    registered_food_quality = "unsupported"
    if food is None:
        registered_food, registered_food_path, registered_food_quality = _read_registered_food_diagnostic(state)
    oxygen, oxygen_path, oxygen_quality = _read_number(
        state,
        ("resources.oxygen_kg_estimate", "resources.oxygen_kg", "resources.oxygen_kg_total"),
        allow_estimated=True,
        minimum=0.0,
    )
    water, water_path, water_quality = _read_number(
        state,
        ("resources.water_kg_estimate", "resources.water_kg", "resources.water_kg_total"),
        allow_estimated=True,
        minimum=0.0,
    )
    cell_clean_oxygen, cell_clean_oxygen_path, cell_clean_oxygen_quality = _read_number(
        state, ("materials.active_world_cells.clean_oxygen_kg",), allow_estimated=True, minimum=0.0
    )
    cell_polluted_oxygen, cell_polluted_oxygen_path, cell_polluted_oxygen_quality = _read_number(
        state, ("materials.active_world_cells.polluted_oxygen_kg",), allow_estimated=True, minimum=0.0
    )
    cell_water_components = [
        _read_number(state, (path,), allow_estimated=True, minimum=0.0)
        for path in (
            "materials.active_world_cells.clean_water_kg",
            "materials.active_world_cells.polluted_water_kg",
            "materials.active_world_cells.salt_water_kg",
            "materials.active_world_cells.brine_kg",
        )
    ]
    visible_cell_oxygen = (
        cell_clean_oxygen + cell_polluted_oxygen
        if cell_clean_oxygen is not None and cell_polluted_oxygen is not None
        else None
    )
    visible_cell_oxygen_quality = (
        "observed"
        if visible_cell_oxygen is not None
        and cell_clean_oxygen_quality == cell_polluted_oxygen_quality == "observed"
        else "estimated"
        if visible_cell_oxygen is not None
        and {cell_clean_oxygen_quality, cell_polluted_oxygen_quality}.issubset({"observed", "estimated"})
        else "unsupported"
    )
    visible_cell_water = (
        sum(component[0] for component in cell_water_components if component[0] is not None)
        if all(component[0] is not None for component in cell_water_components)
        else None
    )
    visible_cell_water_quality = (
        "observed"
        if visible_cell_water is not None and all(component[2] == "observed" for component in cell_water_components)
        else "estimated"
        if visible_cell_water is not None
        and all(component[2] in {"observed", "estimated"} for component in cell_water_components)
        else "unsupported"
    )
    cell_min_temp, cell_min_temp_path, cell_min_temp_quality = _read_number(
        state, ("temperature.active_world_material_min_c",), allow_estimated=True
    )
    cell_max_temp, cell_max_temp_path, cell_max_temp_quality = _read_number(
        state, ("temperature.active_world_material_max_c",), allow_estimated=True
    )
    stressed, stressed_path, stressed_quality = _read_strict_int(
        state,
        "duplicants.stressed",
        DUPLICANTS_STRESSED_CONTRACT_ID,
        legacy_paths=("colony.stressed",),
    )
    idle, idle_path, idle_quality = _read_strict_int(
        state,
        "duplicants.idle",
        DUPLICANTS_IDLE_CONTRACT_ID,
        legacy_paths=("colony.idle",),
    )
    diffuser, diffuser_path, diffuser_quality = _read_int(state, ("buildings.oxygen_diffuser",))
    outhouse, outhouse_path, outhouse_quality = _read_int(state, ("buildings.outhouse",))
    functional_outhouse_operational, functional_outhouse_path, functional_outhouse_quality = (
        _read_functional_outhouse_operational(state)
    )
    functional_diffuser_operational, functional_diffuser_path, functional_diffuser_quality = (
        _read_functional_building_operational(state, ("OxygenDiffuser", "MineralDeoxidizer"))
    )
    functional_generator_operational, functional_generator_path, functional_generator_quality = (
        _read_functional_building_operational(state, "ManualGenerator")
    )
    generator, generator_path, generator_quality = _read_int(
        state, ("buildings.manual_generator", "buildings.generator")
    )
    research, research_path, research_quality = _read(state, ("research.current",), allow_estimated=False)
    complete, complete_path, complete_quality = _read(state, ("research.complete",), allow_estimated=False)
    alerts_value, alerts_path, alerts_quality = _read_strict_primary(
        state,
        "alerts",
        ALERTS_CANONICAL_CONTRACT_ID,
    )
    victory, victory_path, victory_quality = _read_bool(state, ("game.victory", "colony.victory", "victory"))
    sustainable, sustainable_path, sustainable_quality = _read_bool(
        state, ("colony.sustainable", "sustainability.complete", "game.sustainable")
    )
    industry_production, industry_production_path, industry_production_quality, industry_production_evidence = _read_industry_deliverable(
        state, "production"
    )
    industry_delivery, industry_delivery_path, industry_delivery_quality, industry_delivery_evidence = _read_industry_deliverable(
        state, "delivery"
    )
    industry_inputs, industry_inputs_path, industry_inputs_quality, industry_inputs_evidence = _read_industry_deliverable(
        state, "inputs"
    )
    industry_thermal, industry_thermal_path, industry_thermal_quality, industry_thermal_evidence = _read_industry_deliverable(
        state, "thermal"
    )
    industry_waste, industry_waste_path, industry_waste_quality, industry_waste_evidence = _read_industry_deliverable(
        state, "waste"
    )
    rocket_count, rocket_path, rocket_quality = _read_int(
        state, ("rockets.total", "space.rockets", "space.rocket_count")
    )
    mission_ready, mission_path, mission_quality = _read_bool(
        state, ("space.mission_ready", "rockets.mission_ready", "rockets.ready")
    )
    renewable_food, renewable_food_path, renewable_food_quality = _read_number(
        state,
        (
            "production.food_kcal_per_cycle",
            "rates.food_kcal_per_cycle",
            "resources.food_production_kcal_per_cycle",
        ),
        allow_estimated=True,
        minimum=0.0,
    )
    renewable_oxygen, renewable_oxygen_path, renewable_oxygen_quality = _read_number(
        state,
        ("production.oxygen_kg_per_cycle", "rates.oxygen_kg_per_cycle"),
        allow_estimated=True,
        minimum=0.0,
    )
    oxygen_demand, oxygen_demand_path, oxygen_demand_quality = _read_number(
        state,
        ("consumption.oxygen_kg_per_cycle", "demand.oxygen_kg_per_cycle", "rates.oxygen_demand_kg_per_cycle"),
        allow_estimated=True,
        minimum=0.0,
    )
    renewable_water, renewable_water_path, renewable_water_quality = _read_number(
        state,
        ("production.water_kg_per_cycle", "rates.water_kg_per_cycle", "resources.water_production_kg_per_cycle"),
        allow_estimated=True,
        minimum=0.0,
    )
    water_demand, water_demand_path, water_demand_quality = _read_number(
        state,
        ("consumption.water_kg_per_cycle", "demand.water_kg_per_cycle", "rates.water_demand_kg_per_cycle"),
        allow_estimated=True,
        minimum=0.0,
    )
    renewable_power, renewable_power_path, renewable_power_quality = _read_number(
        state,
        ("production.power_watts", "power.generation_watts", "rates.power_generation_watts"),
        allow_estimated=True,
        minimum=0.0,
    )
    power_demand, power_demand_path, power_demand_quality = _read_number(
        state,
        ("consumption.power_watts", "power.load_watts", "rates.power_demand_watts"),
        allow_estimated=True,
        minimum=0.0,
    )
    critical_power_covered, critical_power_path, critical_power_quality = _read_bool(
        state, ("power.critical_load_covered", "power.life_support_covered")
    )
    independent_power_sources, independent_power_path, independent_power_quality = _read_int(
        state, ("power.independent_source_count", "resilience.power_source_count")
    )
    independent_oxygen_sources, independent_oxygen_path, independent_oxygen_quality = _read_int(
        state, ("oxygen.independent_source_count", "resilience.oxygen_source_count")
    )
    independent_water_sources, independent_water_path, independent_water_quality = _read_int(
        state, ("water.independent_source_count", "resilience.water_source_count")
    )
    critical_temperature_safe, critical_temperature_path, critical_temperature_quality = _read_bool(
        state, ("temperature.critical_zones_safe", "environment.critical_zones_safe")
    )
    max_temp, max_temp_path, max_temp_quality = _read_number(
        state, ("temperature.max_c", "environment.max_temperature_c", "temperature.max"), allow_estimated=True
    )
    min_temp, min_temp_path, min_temp_quality = _read_number(
        state, ("temperature.min_c", "environment.min_temperature_c", "temperature.min"), allow_estimated=True
    )
    battery, battery_path, battery_quality = _read_power_battery_percent_v2(state)
    mission_completed, mission_completed_path, mission_completed_quality = _read_bool(
        state, ("space.mission_completed", "rockets.mission_completed")
    )
    return_ready, return_ready_path, return_ready_quality = _read_bool(
        state, ("space.return_ready", "rockets.return_ready")
    )
    base_reserve_safe, base_reserve_path, base_reserve_quality = _read_bool(
        state, ("space.base_reserve_safe", "colony.space_reserve_safe")
    )
    n_minus_one_safe, n_minus_one_path, n_minus_one_quality = _read_bool(
        state, ("resilience.n_minus_one_safe", "colony.n_minus_one_safe")
    )

    dup_count = int(dupes) if dupes is not None and dupes >= 0 and dupes.is_integer() else None
    food_runway = None
    food_demand = (dup_count * KCAL_PER_DUPLICANT_CYCLE) if dup_count and dup_count > 0 else None
    if food_demand is not None and math.isfinite(food_demand) and food_accessible_contract and food is not None:
        food_runway = max(0.0, food / food_demand)
    food_deficit = None
    food_depletion = None
    if (
        dup_count and dup_count > 0
        and dupes_quality == "observed"
        and renewable_food is not None
        and renewable_food_quality == "observed"
    ):
        if food_demand is not None and math.isfinite(food_demand):
            food_deficit = max(0.0, food_demand - renewable_food)
        if (
            food_deficit is not None
            and food_deficit > 0
            and food_accessible_contract
            and food_quality == "observed"
            and food is not None
        ):
            depletion = max(0.0, food / food_deficit)
            if math.isfinite(depletion):
                food_depletion = depletion
    oxygen_deficit = None
    if (
        renewable_oxygen is not None
        and oxygen_demand is not None
        and renewable_oxygen_quality == "observed"
        and oxygen_demand_quality == "observed"
    ):
        oxygen_deficit = max(0.0, oxygen_demand - renewable_oxygen)
    water_deficit = None
    if (
        renewable_water is not None
        and water_demand is not None
        and renewable_water_quality == "observed"
        and water_demand_quality == "observed"
    ):
        water_deficit = max(0.0, water_demand - renewable_water)
    power_deficit = None
    if (
        renewable_power is not None
        and power_demand is not None
        and renewable_power_quality == "observed"
        and power_demand_quality == "observed"
    ):
        power_deficit = max(0.0, power_demand - renewable_power)
    water_per_dupe = None
    if water is not None and dup_count and dup_count > 0:
        water_per_dupe = max(0.0, water / dup_count)
    alerts = _as_alerts(alerts_value)
    complete_list = complete if isinstance(complete, list) else None
    if complete_list is not None:
        complete_list = [_safe_text(item, 120) for item in complete_list[:100]]

    paths = {
        "cycle": cycle_path,
        "game_time_cycles": game_time_path,
        "duplicants": dupes_path,
        "food": food_path,
        "registered_food": registered_food_path,
        "oxygen": oxygen_path,
        "water": water_path,
        "visible_cell_clean_oxygen": cell_clean_oxygen_path,
        "visible_cell_polluted_oxygen": cell_polluted_oxygen_path,
        "visible_cell_clean_water": cell_water_components[0][1],
        "visible_cell_polluted_water": cell_water_components[1][1],
        "visible_cell_salt_water": cell_water_components[2][1],
        "visible_cell_brine": cell_water_components[3][1],
        "visible_cell_min_temperature": cell_min_temp_path,
        "visible_cell_max_temperature": cell_max_temp_path,
        "stressed": stressed_path,
        "idle": idle_path,
        "diffuser": diffuser_path,
        "outhouse": outhouse_path,
        "functional_outhouse_operational": functional_outhouse_path,
        "functional_diffuser_operational": functional_diffuser_path,
        "functional_generator_operational": functional_generator_path,
        "generator": generator_path,
        "research": research_path,
        "research_complete": complete_path,
        "alerts": alerts_path,
        "victory": victory_path,
        "sustainable": sustainable_path,
        "industry_production": industry_production_path,
        "industry_delivery": industry_delivery_path,
        "industry_inputs": industry_inputs_path,
        "industry_thermal": industry_thermal_path,
        "industry_waste": industry_waste_path,
        "rocket_count": rocket_path,
        "mission_ready": mission_path,
        "renewable_food": renewable_food_path,
        "renewable_oxygen": renewable_oxygen_path,
        "oxygen_demand": oxygen_demand_path,
        "renewable_water": renewable_water_path,
        "water_demand": water_demand_path,
        "renewable_power": renewable_power_path,
        "power_demand": power_demand_path,
        "critical_power_covered": critical_power_path,
        "independent_power_sources": independent_power_path,
        "independent_oxygen_sources": independent_oxygen_path,
        "independent_water_sources": independent_water_path,
        "critical_temperature_safe": critical_temperature_path,
        "max_temperature": max_temp_path,
        "min_temperature": min_temp_path,
        "battery": battery_path,
        "mission_completed": mission_completed_path,
        "return_ready": return_ready_path,
        "base_reserve_safe": base_reserve_path,
        "n_minus_one_safe": n_minus_one_path,
    }
    qualities = {
        "cycle": cycle_quality,
        "game_time_cycles": game_time_quality,
        "duplicants": dupes_quality,
        "food": food_quality,
        "registered_food": registered_food_quality,
        "oxygen": oxygen_quality,
        "water": water_quality,
        "visible_cell_oxygen": visible_cell_oxygen_quality,
        "visible_cell_water": visible_cell_water_quality,
        "visible_cell_min_temperature": cell_min_temp_quality,
        "visible_cell_max_temperature": cell_max_temp_quality,
        "stressed": stressed_quality,
        "idle": idle_quality,
        "diffuser": diffuser_quality,
        "outhouse": outhouse_quality,
        "functional_outhouse_operational": functional_outhouse_quality,
        "functional_diffuser_operational": functional_diffuser_quality,
        "functional_generator_operational": functional_generator_quality,
        "generator": generator_quality,
        "research": research_quality,
        "research_complete": complete_quality,
        "alerts": alerts_quality,
        "victory": victory_quality,
        "sustainable": sustainable_quality,
        "industry_production": industry_production_quality,
        "industry_delivery": industry_delivery_quality,
        "industry_inputs": industry_inputs_quality,
        "industry_thermal": industry_thermal_quality,
        "industry_waste": industry_waste_quality,
        "rocket_count": rocket_quality,
        "mission_ready": mission_quality,
        "renewable_food": renewable_food_quality,
        "renewable_oxygen": renewable_oxygen_quality,
        "oxygen_demand": oxygen_demand_quality,
        "renewable_water": renewable_water_quality,
        "water_demand": water_demand_quality,
        "renewable_power": renewable_power_quality,
        "power_demand": power_demand_quality,
        "critical_power_covered": critical_power_quality,
        "independent_power_sources": independent_power_quality,
        "independent_oxygen_sources": independent_oxygen_quality,
        "independent_water_sources": independent_water_quality,
        "critical_temperature_safe": critical_temperature_quality,
        "max_temperature": max_temp_quality,
        "min_temperature": min_temp_quality,
        "battery": battery_quality,
        "mission_completed": mission_completed_quality,
        "return_ready": return_ready_quality,
        "base_reserve_safe": base_reserve_quality,
        "n_minus_one_safe": n_minus_one_quality,
    }
    return {
        "cycle": int(cycle) if cycle is not None and cycle.is_integer() else cycle,
        "game_time_cycles": game_time_cycles,
        "sample_interval_seconds": sample_interval,
        "duplicants": dup_count,
        "food_kcal": food,
        "food_accessible_contract": food_accessible_contract,
        "food_runway_cycles": round(food_runway, 3) if food_runway is not None else None,
        "food_deficit_kcal_per_cycle": food_deficit,
        "food_depletion_cycles": round(food_depletion, 3) if food_depletion is not None else None,
        "oxygen_deficit_kg_per_cycle": oxygen_deficit,
        "water_deficit_kg_per_cycle": water_deficit,
        "power_deficit_watts": power_deficit,
        "registered_food_kcal": registered_food,
        "oxygen_kg": oxygen,
        "water_kg": water,
        "visible_cell_oxygen_kg": visible_cell_oxygen,
        "visible_cell_water_family_kg": visible_cell_water,
        "visible_cell_material_min_c": cell_min_temp,
        "visible_cell_material_max_c": cell_max_temp,
        "water_kg_per_duplicant": round(water_per_dupe, 3) if water_per_dupe is not None else None,
        "stressed": stressed,
        "idle": idle,
        "oxygen_diffusers": diffuser,
        "outhouses": outhouse,
        "functional_outhouse_operational": functional_outhouse_operational,
        "functional_diffuser_operational": functional_diffuser_operational,
        "functional_generator_operational": functional_generator_operational,
        "manual_generators": generator,
        "research_current": _safe_text(research, 160) if isinstance(research, str) else None,
        "research_complete": complete_list,
        "alerts": alerts,
        "low_oxygen_alert": _contains_alert(alerts, _OXYGEN_ALERT_IDS),
        "food_alert": _contains_alert(alerts, _FOOD_ALERT_IDS),
        "power_alert": _contains_alert(alerts, _POWER_ALERT_IDS),
        "temperature_alert": _contains_alert(alerts, _TEMPERATURE_ALERT_IDS),
        "victory": victory,
        "sustainable": sustainable,
        "industry_production_complete": industry_production,
        "industry_delivery_complete": industry_delivery,
        "industry_inputs_supplied": industry_inputs,
        "industry_heat_removed": industry_thermal,
        "industry_waste_handled": industry_waste,
        "industry_production_evidence": industry_production_evidence,
        "industry_delivery_evidence": industry_delivery_evidence,
        "industry_inputs_evidence": industry_inputs_evidence,
        "industry_thermal_evidence": industry_thermal_evidence,
        "industry_waste_evidence": industry_waste_evidence,
        "rocket_count": rocket_count,
        "mission_ready": mission_ready,
        "renewable_food_kcal_per_cycle": renewable_food,
        "renewable_oxygen_kg_per_cycle": renewable_oxygen,
        "oxygen_demand_kg_per_cycle": oxygen_demand,
        "renewable_water_kg_per_cycle": renewable_water,
        "water_demand_kg_per_cycle": water_demand,
        "renewable_power_watts": renewable_power,
        "power_demand_watts": power_demand,
        "critical_power_covered": critical_power_covered,
        "independent_power_sources": independent_power_sources,
        "independent_oxygen_sources": independent_oxygen_sources,
        "independent_water_sources": independent_water_sources,
        "critical_temperature_safe": critical_temperature_safe,
        "max_temperature_c": max_temp,
        "min_temperature_c": min_temp,
        "battery_percent": battery,
        "mission_completed": mission_completed,
        "return_ready": return_ready,
        "base_reserve_safe": base_reserve_safe,
        "n_minus_one_safe": n_minus_one_safe,
        "paths": paths,
        "qualities": qualities,
    }


def _action_for_objective(
    objective_id: str,
    status: str,
    reason: str,
) -> List[Dict[str, Any]]:
    if status != "at_risk":
        return []
    return [
        _recommendation(
            f"{objective_id}.note",
            "focus_note",
            reason,
            risk="low",
            args={"tag": objective_id},
        )
    ]


def _evaluate_strategy_impl(
    state: Optional[Mapping[str, Any]],
    *,
    goal: str = STRATEGY_GOAL,
    now: Optional[dt.datetime] = None,
    previous_phase: Optional[str] = None,
    trusted_proof_verifier: Optional[TrustedProofVerifier] = None,
) -> Dict[str, Any]:
    """Evaluate one snapshot, defaulting all self-declared proofs to untrusted.

    ``trusted_proof_verifier`` is intentionally an in-process callable rather
    than state metadata.  The live Bridge does not provide one yet; future
    derivation code must recompute input hashes and allowlist policy hashes
    before any window rate or certified assertion can satisfy a gate.
    """

    if trusted_proof_verifier is not None and not callable(trusted_proof_verifier):
        raise StrategyError("trusted_proof_verifier must be callable")
    value = (
        _ProofBoundState(state, trusted_proof_verifier)
        if isinstance(state, Mapping) and trusted_proof_verifier is not None
        else state
        if isinstance(state, Mapping)
        else None
    )
    identity = _state_identity(value)
    metrics = collect_metrics(value)
    stale = _state_is_stale(value)
    quality = quality_summary(dict(value) if isinstance(value, Mapping) else None)

    dupes = metrics["duplicants"]
    expected_outhouses = int(math.ceil(dupes / 4.0)) if isinstance(dupes, int) and dupes > 0 else None
    core_paths = [
        "cycle",
        "duplicants.total",
        "resources.food_kcal",
        "resources.oxygen_kg_estimate",
        "resources.water_kg_estimate",
        "alerts",
        "research.current",
        "buildings.outhouse",
        "power.battery_percent",
        "power.critical_load_covered",
        "temperature.critical_zones_safe",
    ]
    objectives: List[Dict[str, Any]] = []

    # Oxygen certification needs observed stock, observed alert state, and an
    # observed production-versus-demand balance.  A building count or a
    # non-empty estimate is diagnostic evidence, never proof of breathable air.
    oxygen_risk = bool(metrics["low_oxygen_alert"])
    if metrics["oxygen_kg"] is not None and metrics["oxygen_kg"] <= 0:
        oxygen_risk = True
    # A zero result from a complete rolling visible-cell aggregate is a
    # conservative risk signal only. A positive result can never satisfy the
    # reserve gate because storage/pipes/debris/hidden cells are out of scope
    # and the visible cells were not captured atomically.
    visible_cell_oxygen_risk = (
        metrics["visible_cell_oxygen_kg"] is not None
        and metrics["visible_cell_oxygen_kg"] <= 0
        and metrics["qualities"].get("visible_cell_oxygen") in {"observed", "estimated"}
    )
    if visible_cell_oxygen_risk:
        oxygen_risk = True
    oxygen_production = metrics["renewable_oxygen_kg_per_cycle"]
    oxygen_demand = metrics["oxygen_demand_kg_per_cycle"]
    functional_diffuser_path = metrics["paths"].get("functional_diffuser_operational")
    functional_diffuser_quality = metrics["qualities"].get("functional_diffuser_operational")
    functional_diffuser_present = functional_diffuser_path is not None
    functional_diffuser_failure = (
        functional_diffuser_present
        and functional_diffuser_quality == "observed"
        and metrics["functional_diffuser_operational"] is False
    )
    functional_diffuser_unknown = functional_diffuser_present and functional_diffuser_quality != "observed"
    if functional_diffuser_failure:
        oxygen_risk = True
    if oxygen_production is not None and oxygen_demand is not None and oxygen_production < oxygen_demand:
        oxygen_risk = True
    oxygen_proof_observed = all(
        metrics["qualities"].get(key) == "observed"
        for key in ("oxygen", "alerts", "renewable_oxygen", "oxygen_demand")
    )
    oxygen_safe = bool(
        oxygen_proof_observed
        and metrics["oxygen_kg"] is not None
        and metrics["oxygen_kg"] > 0
        and oxygen_production is not None
        and oxygen_demand is not None
        and oxygen_production >= oxygen_demand
        and not metrics["low_oxygen_alert"]
        and (
            not functional_diffuser_present
            or (
                functional_diffuser_quality == "observed"
                and metrics["functional_diffuser_operational"] is True
            )
        )
    )
    oxygen_status = (
        "blocked"
        if stale
        else "at_risk"
        if oxygen_risk
        else "satisfied"
        if oxygen_safe
        else "unknown"
    )
    oxygen_reason = (
        "Snapshot is stale; do not make life-support changes until a fresh sample arrives."
        if stale
        else "Observed functional OxygenDiffuser telemetry reports operational=false; oxygen production cannot be treated as safe."
        if functional_diffuser_failure
        else "Observed oxygen stock/rate margin or the conservative visible-cell oxygen diagnostic is unsafe, or a breathing alert is active."
        if oxygen_risk
        else "Functional OxygenDiffuser telemetry is present but unknown or contract-invalid; oxygen remains uncertified until a fresh complete observation is available."
        if functional_diffuser_unknown
        else "Observed oxygen production covers demand with positive stock and no breathing alert."
        if oxygen_safe
        else "Observed oxygen stock, demand, production, and alert telemetry are all required to certify safety."
    )
    objectives.append(
        _objective(
            "oxygen_security",
            "Secure breathable oxygen",
            phase="bootstrap",
            priority=100,
            status=oxygen_status,
            reason=oxygen_reason,
            evidence=[
                p
                for p in (
                    metrics["paths"]["oxygen"],
                    metrics["paths"]["visible_cell_clean_oxygen"],
                    metrics["paths"]["visible_cell_polluted_oxygen"],
                    metrics["paths"]["renewable_oxygen"],
                    metrics["paths"]["oxygen_demand"],
                    metrics["paths"]["diffuser"],
                    metrics["paths"]["alerts"],
                )
                if p
            ],
            completion=(
                "No active oxygen/suffocation alert",
                "Observed oxygen stock is positive",
                "Observed oxygen production >= observed demand",
            ),
            recommended=_action_for_objective(
                "oxygen_security", oxygen_status, oxygen_reason
            ),
            telemetry=(
                "alerts",
                "resources.oxygen_kg_estimate",
                "production.oxygen_kg_per_cycle",
                "consumption.oxygen_kg_per_cycle",
                "materials.active_world_cells.clean_oxygen_kg",
                "materials.active_world_cells.polluted_oxygen_kg",
            ),
        )
    )

    runway = metrics["food_runway_cycles"]
    food_inputs_observed = all(
        metrics["qualities"].get(key) == "observed" for key in ("food", "duplicants")
    ) and metrics["food_accessible_contract"] is True
    food_status = (
        "blocked"
        if stale
        else "unknown"
        if runway is None
        else "at_risk"
        if runway < FOOD_RUNWAY_TARGET
        else "satisfied"
        if food_inputs_observed
        else "unknown"
    )
    registered_food = metrics.get("registered_food_kcal")
    registered_food_quality = metrics["qualities"].get("registered_food")
    if runway is None and registered_food is not None and registered_food_quality == "estimated":
        food_reason = (
            f"The enumerated registered-food lower bound is {registered_food:.1f} kcal; total, "
            "accessibility, permissions, and usable runway remain unknown."
        )
    elif runway is None and registered_food is not None and registered_food_quality == "observed":
        food_reason = (
            f"The complete registered non-private inventory sum is {registered_food:.1f} kcal; "
            "accessibility, permissions, and usable runway remain unproven."
        )
    elif runway is None:
        food_reason = "Food inventory or duplicant count is unavailable; obtain a fresh observed sample."
    elif runway < FOOD_RUNWAY_CRITICAL:
        food_reason = f"Food runway is approximately {runway:.1f} cycles, below the emergency floor."
    elif runway < FOOD_RUNWAY_TARGET:
        food_reason = f"Food runway is approximately {runway:.1f} cycles; build reserve before expansion."
    else:
        food_reason = f"Food runway is approximately {runway:.1f} cycles, meeting the reserve target."
    objectives.append(
        _objective(
            "food_reserve",
            "Maintain a food reserve",
            phase="bootstrap",
            priority=95,
            status=food_status,
            reason=food_reason,
            evidence=[
                p
                for p in (
                    metrics["paths"]["food"],
                    metrics["paths"]["registered_food"],
                    metrics["paths"]["duplicants"],
                )
                if p
            ],
            completion=(f"Food runway >= {FOOD_RUNWAY_TARGET:g} duplicant cycles",),
            recommended=_action_for_objective("food_reserve", food_status, food_reason),
            telemetry=("resources.food_kcal", "duplicants.total"),
        )
    )

    sanitation_inputs_observed = all(
        metrics["qualities"].get(key) == "observed" for key in ("outhouse", "duplicants")
    )
    functional_outhouse_path = metrics["paths"].get("functional_outhouse_operational")
    functional_outhouse_quality = metrics["qualities"].get("functional_outhouse_operational")
    functional_field_present = functional_outhouse_path is not None
    functional_outhouse_unknown = functional_field_present and functional_outhouse_quality != "observed"
    functional_outhouse_failure = (
        functional_field_present
        and functional_outhouse_quality == "observed"
        and metrics["functional_outhouse_operational"] is False
    )
    functional_outhouse_proof_observed = (
        not functional_field_present or functional_outhouse_quality == "observed"
    )
    sanitation_inputs_observed = sanitation_inputs_observed and functional_outhouse_proof_observed
    sanitation_count_unknown = metrics["outhouses"] is None or expected_outhouses is None
    sanitation_unknown = functional_outhouse_unknown or sanitation_count_unknown
    sanitation_coverage_risk = (
        not sanitation_count_unknown and metrics["outhouses"] < expected_outhouses
    )
    sanitation_status = (
        "blocked"
        if stale
        else "at_risk"
        if functional_outhouse_failure
        else "unknown"
        if sanitation_unknown
        else "at_risk"
        if sanitation_coverage_risk
        else "satisfied"
        if sanitation_inputs_observed
        else "unknown"
    )
    sanitation_reason = (
        "Observed functional Outhouse telemetry reports operational=false for at least one Outhouse; "
        "sanitation cannot be treated as satisfied."
        if functional_outhouse_failure
        else "Functional Outhouse telemetry is present but unknown or contract-invalid; sanitation remains unknown "
        "until a fresh, complete active-world observation is available."
        if functional_outhouse_unknown
        else f"At least {expected_outhouses} outhouse(s) are required for {dupes} duplicants; observed {metrics['outhouses']}."
        if sanitation_coverage_risk
        else (
            "Outhouse coverage meets the conservative early-colony target; observed functional "
            "Outhouse telemetry reports operational=true."
            if functional_field_present and metrics["functional_outhouse_operational"] is True
            else "Outhouse coverage meets the conservative early-colony target."
        )
        if not sanitation_unknown
        else "Duplicant count or outhouse count is unavailable."
    )
    objectives.append(
        _objective(
            "sanitation",
            "Keep sanitation ahead of population",
            phase="bootstrap",
            priority=90,
            status=sanitation_status,
            reason=sanitation_reason,
            evidence=[
                p
                for p in (
                    metrics["paths"]["duplicants"],
                    metrics["paths"]["outhouse"],
                    functional_outhouse_path,
                )
                if p
            ],
            completion=("Outhouse coverage >= one per four duplicants",),
            recommended=_action_for_objective("sanitation", sanitation_status, sanitation_reason),
            telemetry=("duplicants.total", "buildings.outhouse", "functional_buildings"),
        )
    )

    water_per_dupe = metrics["water_kg_per_duplicant"]
    water_inputs_observed = all(
        metrics["qualities"].get(key) == "observed" for key in ("water", "duplicants")
    )
    water_status = (
        "blocked"
        if stale
        else "unknown"
        if water_per_dupe is None
        else "at_risk"
        if water_per_dupe < WATER_PER_DUPLICANT_TARGET
        else "satisfied"
        if water_inputs_observed
        else "unknown"
    )
    if water_per_dupe is None:
        water_reason = "Water inventory telemetry is unavailable; avoid claiming a sustainable colony."
    elif water_per_dupe < WATER_PER_DUPLICANT_CRITICAL:
        water_reason = f"Observed water is approximately {water_per_dupe:.1f} kg per duplicant, below the emergency floor."
    elif water_per_dupe < WATER_PER_DUPLICANT_TARGET:
        water_reason = f"Observed water is approximately {water_per_dupe:.1f} kg per duplicant; establish a renewable source."
    else:
        water_reason = f"Observed water is approximately {water_per_dupe:.1f} kg per duplicant."
    objectives.append(
        _objective(
            "water_resilience",
            "Maintain a trustworthy water reserve",
            phase="stabilize",
            priority=85,
            status=water_status,
            reason=water_reason,
            evidence=[p for p in (metrics["paths"]["water"], metrics["paths"]["duplicants"]) if p],
            completion=(
                f"Water reserve >= {WATER_PER_DUPLICANT_TARGET:g} kg per duplicant",
            ),
            recommended=_action_for_objective("water_resilience", water_status, water_reason),
            telemetry=("resources.water_kg_estimate", "duplicants.total"),
        )
    )

    # A generator count alone is not proof of a functioning grid.
    power_known = metrics["battery_percent"] is not None or metrics["critical_power_covered"] is not None
    functional_generator_path = metrics["paths"].get("functional_generator_operational")
    functional_generator_quality = metrics["qualities"].get("functional_generator_operational")
    functional_generator_present = functional_generator_path is not None
    functional_generator_failure = (
        functional_generator_present
        and functional_generator_quality == "observed"
        and metrics["functional_generator_operational"] is False
    )
    functional_generator_unknown = functional_generator_present and functional_generator_quality != "observed"
    power_risk = bool(metrics["power_alert"]) or (
        metrics["battery_percent"] is not None and metrics["battery_percent"] < 20.0
    ) or metrics["critical_power_covered"] is False or functional_generator_failure
    power_proof_observed = all(
        metrics["qualities"].get(key) == "observed"
        for key in ("alerts", "battery", "critical_power_covered")
    )
    power_safe = bool(
        power_proof_observed
        and metrics["battery_percent"] is not None
        and metrics["battery_percent"] >= 20.0
        and metrics["critical_power_covered"] is True
        and metrics["power_alert"] is False
        and (
            not functional_generator_present
            or (
                functional_generator_quality == "observed"
                and metrics["functional_generator_operational"] is True
            )
        )
    )
    power_status = (
        "blocked"
        if stale
        else "at_risk"
        if power_risk
        else "satisfied"
        if power_safe
        else "unknown"
    )
    power_reason = (
        "Observed functional ManualGenerator telemetry reports operational=false; generator output cannot be treated as safe."
        if functional_generator_failure
        else "Power/battery alert or critically low battery was observed."
        if power_risk
        else "Functional ManualGenerator telemetry is present but unknown or contract-invalid; power remains uncertified until a fresh complete observation is available."
        if functional_generator_unknown
        else "Power telemetry is unavailable; generator count does not prove a stable grid."
        if not power_known
        else "Observed battery reserve and critical-load coverage are inside the safety band."
        if power_safe
        else "Power evidence is incomplete."
    )
    objectives.append(
        _objective(
            "power_resilience",
            "Make power generation resilient",
            phase="stabilize",
            priority=80,
            status=power_status,
            reason=power_reason,
            evidence=[
                p
                for p in (
                    metrics["paths"]["battery"],
                    metrics["paths"]["critical_power_covered"],
                    metrics["paths"]["generator"],
                    metrics["paths"]["alerts"],
                )
                if p
            ],
            completion=(
                "Battery stays above the emergency floor",
                "Critical life-support loads are observed as covered",
            ),
            recommended=_action_for_objective("power_resilience", power_status, power_reason),
            telemetry=("power.battery_percent", "power.critical_load_covered", "alerts"),
        )
    )

    stress_status = (
        "blocked"
        if stale
        else "at_risk"
        if metrics["stressed"] is not None and metrics["stressed"] > 0
        else "satisfied"
        if metrics["stressed"] is not None
        else "unknown"
    )
    stress_reason = (
        f"{metrics['stressed']} stressed duplicant(s) were observed; protect downtime and morale."
        if stress_status == "at_risk"
        else "No stressed duplicants were observed."
        if stress_status == "satisfied"
        else "Stress telemetry is unavailable."
    )
    objectives.append(
        _objective(
            "morale_and_stress",
            "Keep stress and morale sustainable",
            phase="stabilize",
            priority=75,
            status=stress_status,
            reason=stress_reason,
            evidence=[p for p in (metrics["paths"]["stressed"], metrics["paths"]["idle"]) if p],
            completion=(
                "No duplicant is in a stress response",
                "Downtime and morale needs remain covered",
            ),
            recommended=_action_for_objective("morale_and_stress", stress_status, stress_reason),
            telemetry=("duplicants.stressed", "duplicants.idle"),
        )
    )

    research_value = metrics["research_current"]
    research_known = metrics["qualities"]["research"] == "observed"
    research_status = (
        "blocked"
        if stale
        else "at_risk"
        if research_known and not research_value
        else "satisfied"
        if research_known
        else "unknown"
    )
    research_reason = (
        "No active research was observed; choose the next technology that removes the current survival bottleneck."
        if research_status == "at_risk"
        else "An active research target is present."
        if research_status == "satisfied"
        else "Research telemetry is unavailable."
    )
    research_actions: List[Dict[str, Any]] = []
    if research_status == "at_risk":
        # Do not invent a tech id; a build-specific tech-tree adapter must select it.
        research_actions = [
            _recommendation(
                "research_progress.select",
                "set_research",
                research_reason,
                risk="medium",
                args={
                    "selection": "adapter_required",
                    "technology_id": None,
                },
            )
        ]
    objectives.append(
        _objective(
            "research_progress",
            "Keep research advancing",
            phase="stabilize",
            priority=70,
            status=research_status,
            reason=research_reason,
            evidence=[p for p in (metrics["paths"]["research"], metrics["paths"]["research_complete"]) if p],
            completion=("A survival-relevant research target is active",),
            recommended=research_actions,
            telemetry=("research.current", "research.complete"),
        )
    )

    # Industry is intentionally explicit and evidence-heavy.  A completed
    # blueprint or a count of buildings is not a production result; each
    # deliverable below needs an observed completion assertion (or an observed
    # actual/required quantity pair) from the same bounded populated-world
    # scope.  Missing, estimated, or contract-invalid inputs remain unknown.
    industry_objective_specs = (
        (
            "industry_production",
            "Complete the finite production deliverables",
            "industry_production_complete",
            "Production evidence is observed for the frozen industrial deliverables.",
            "Production has not reached the frozen deliverable requirement.",
            "Observed production completion is required; a recipe or building count is insufficient.",
            "production",
        ),
        (
            "industry_delivery",
            "Deliver industrial outputs to their consumers",
            "industry_delivery_complete",
            "Observed delivery records show the required outputs reached their consumers.",
            "One or more required industrial outputs remain undelivered.",
            "Observed delivery records are required; produced stock alone is not delivery evidence.",
            "delivery",
        ),
        (
            "industry_inputs",
            "Keep industrial inputs supplied",
            "industry_inputs_supplied",
            "Observed input supply covers the frozen industrial recipes.",
            "Industrial input supply is below the frozen recipe requirement.",
            "Observed input quantities or a certified supply assertion are required.",
            "inputs",
        ),
        (
            "industry_thermal_safety",
            "Remove heat generated by industry",
            "industry_heat_removed",
            "Observed heat removal covers the industrial heat load and remains inside the safety contract.",
            "Industrial heat generation exceeds observed removal capacity.",
            "Observed heat generation/removal evidence is required; room temperature alone is insufficient.",
            "thermal",
        ),
        (
            "industry_waste_handling",
            "Process and contain industrial waste",
            "industry_waste_handled",
            "Observed waste handling covers the industrial waste stream.",
            "Industrial waste is accumulating beyond observed handling capacity.",
            "Observed waste processing or containment evidence is required.",
            "waste",
        ),
    )
    industry_statuses: List[str] = []
    for objective_id, title, metric_key, ok_reason, risk_reason, unknown_reason, domain in industry_objective_specs:
        metric_value = metrics.get(metric_key)
        metric_quality = metrics["qualities"].get(f"industry_{domain}")
        deliverable_status = (
            "blocked"
            if stale
            else "at_risk"
            if metric_value is False
            else "satisfied"
            if metric_value is True and metric_quality == "observed"
            else "unknown"
        )
        industry_statuses.append(deliverable_status)
        reason = (
            ok_reason
            if deliverable_status == "satisfied"
            else risk_reason
            if deliverable_status == "at_risk"
            else "Snapshot is stale; do not certify industrial effects until a fresh sample arrives."
            if deliverable_status == "blocked"
            else unknown_reason
        )
        evidence_keys = {
            "production": "industry_production_evidence",
            "delivery": "industry_delivery_evidence",
            "inputs": "industry_inputs_evidence",
            "thermal": "industry_thermal_evidence",
            "waste": "industry_waste_evidence",
        }
        required_telemetry = tuple(_INDUSTRY_FIELD_PATHS[domain]) + tuple(
            path for path in (
                "industry.production.produced_quantity",
                "industry.production.required_quantity",
                "industry.delivery.delivered_quantity",
                "industry.delivery.required_quantity",
                "industry.inputs.supplied_quantity",
                "industry.inputs.required_quantity",
                "industry.heat.removed_watts",
                "industry.heat.generated_watts",
                "industry.waste.handled_quantity",
                "industry.waste.generated_quantity",
            )
            if path in metrics.get("paths", {}).values()
        )
        objectives.append(
            _objective(
                objective_id,
                title,
                phase="industry",
                priority={
                    "industry_production": 68,
                    "industry_delivery": 67,
                    "industry_inputs": 66,
                    "industry_thermal_safety": 64,
                    "industry_waste_handling": 63,
                }[objective_id],
                status=deliverable_status,
                reason=reason,
                evidence=list(metrics.get(evidence_keys[domain]) or ()),
                completion=(
                    "The frozen industrial deliverable has an observed completion result",
                    "Inputs, heat removal, and waste handling remain reconciled",
                ),
                recommended=_action_for_objective(objective_id, deliverable_status, reason),
                telemetry=required_telemetry,
            )
        )

    temp_known = metrics["critical_temperature_safe"] is not None
    temp_risk = bool(metrics["temperature_alert"]) or (
        metrics["max_temperature_c"] is not None and metrics["max_temperature_c"] > 40.0
    ) or (
        metrics["min_temperature_c"] is not None and metrics["min_temperature_c"] < 5.0
    ) or metrics["critical_temperature_safe"] is False
    temp_safe = bool(
        metrics["qualities"].get("critical_temperature_safe") == "observed"
        and metrics["qualities"].get("alerts") == "observed"
        and metrics["critical_temperature_safe"] is True
        and metrics["temperature_alert"] is False
    )
    temp_status = (
        "blocked"
        if stale
        else "at_risk"
        if temp_risk
        else "satisfied"
        if temp_safe
        else "unknown"
    )
    temp_reason = (
        "Temperature alert or unsafe hot/cold critical zone was observed."
        if temp_risk
        else "Critical-zone temperature safety telemetry is unavailable; do not claim thermal stability."
        if not temp_known
        else "All observed critical zones are inside their configured safety bands."
    )
    objectives.append(
        _objective(
            "thermal_safety",
            "Control temperature before scaling industry",
            phase="expand",
            priority=65,
            status=temp_status,
            reason=temp_reason,
            evidence=[
                p
                for p in (
                    metrics["paths"]["max_temperature"],
                    metrics["paths"]["min_temperature"],
                    metrics["paths"]["critical_temperature_safe"],
                    metrics["paths"]["alerts"],
                )
                if p
            ],
            completion=("Critical rooms remain within duplicant-safe temperatures",),
            recommended=_action_for_objective("thermal_safety", temp_status, temp_reason),
            telemetry=("temperature.critical_zones_safe", "temperature.max_c", "temperature.min_c", "alerts"),
        )
    )

    food_demand = None
    if isinstance(dupes, int) and dupes > 0:
        candidate_food_demand = dupes * KCAL_PER_DUPLICANT_CYCLE
        if math.isfinite(candidate_food_demand):
            food_demand = candidate_food_demand
    renewable_food_rate = metrics["renewable_food_kcal_per_cycle"]
    renewable_food_observed = all(
        metrics["qualities"].get(key) == "observed" for key in ("renewable_food", "duplicants")
    )
    renewable_food_status = (
        "blocked"
        if stale
        else "unknown"
        if renewable_food_rate is None or food_demand is None
        else "at_risk"
        if renewable_food_rate < food_demand
        else "satisfied"
        if renewable_food_observed
        else "unknown"
    )
    renewable_food_reason = (
        "Observed food production meets the conservative duplicant consumption estimate."
        if renewable_food_status == "satisfied"
        else (
            f"Food production is below current consumption by "
            f"{food_demand - renewable_food_rate:.1f} kcal/cycle "
            f"(production {renewable_food_rate:.1f}, demand {food_demand:.1f})."
        )
        if renewable_food_status == "at_risk"
        else "Observed food production and population evidence are incomplete."
    )
    if renewable_food_status == "at_risk" and metrics["food_depletion_cycles"] is not None:
        renewable_food_reason += (
            f" Accessible food would deplete in approximately {metrics['food_depletion_cycles']:.3f} cycles"
            " if current production and demand persist; this is a forecast, not a safety certificate."
        )
    objectives.append(
        _objective(
            "renewable_food",
            "Verify renewable food production",
            phase="expand",
            priority=60,
            status=renewable_food_status,
            reason=renewable_food_reason,
            evidence=[p for p in (metrics["paths"]["renewable_food"], metrics["paths"]["duplicants"]) if p],
            completion=("Food production >= estimated consumption for the current population",),
            telemetry=("production.food_kcal_per_cycle", "duplicants.total"),
        )
    )

    renewable_oxygen_rate = metrics["renewable_oxygen_kg_per_cycle"]
    renewable_oxygen_observed = all(
        metrics["qualities"].get(key) == "observed" for key in ("renewable_oxygen", "oxygen_demand")
    )
    renewable_oxygen_status = (
        "blocked"
        if stale
        else "unknown"
        if renewable_oxygen_rate is None or oxygen_demand is None
        else "at_risk"
        if renewable_oxygen_rate < oxygen_demand
        else "satisfied"
        if renewable_oxygen_observed
        else "unknown"
    )
    renewable_oxygen_reason = (
        "Observed oxygen production meets or exceeds observed demand."
        if renewable_oxygen_status == "satisfied"
        else "Observed oxygen production is below demand."
        if renewable_oxygen_status == "at_risk"
        else "Observed oxygen production and demand telemetry are both required."
    )
    if renewable_oxygen_status == "at_risk" and metrics["oxygen_deficit_kg_per_cycle"] is not None:
        renewable_oxygen_reason = (
            f"Observed oxygen production is below demand by {metrics['oxygen_deficit_kg_per_cycle']:.1f} kg/cycle "
            f"(production {renewable_oxygen_rate:.1f}, demand {oxygen_demand:.1f}). "
            "This rate shortfall does not establish breathable reserve or time to suffocation."
        )
    objectives.append(
        _objective(
            "renewable_oxygen",
            "Verify renewable oxygen production",
            phase="expand",
            priority=55,
            status=renewable_oxygen_status,
            reason=renewable_oxygen_reason,
            evidence=[p for p in (metrics["paths"]["renewable_oxygen"], metrics["paths"]["oxygen_demand"]) if p],
            completion=("Oxygen production is measured above current demand",),
            telemetry=("production.oxygen_kg_per_cycle", "consumption.oxygen_kg_per_cycle"),
        )
    )

    renewable_water_rate = metrics["renewable_water_kg_per_cycle"]
    water_demand = metrics["water_demand_kg_per_cycle"]
    renewable_water_observed = all(
        metrics["qualities"].get(key) == "observed" for key in ("renewable_water", "water_demand")
    )
    renewable_water_status = (
        "blocked"
        if stale
        else "unknown"
        if renewable_water_rate is None or water_demand is None
        else "at_risk"
        if renewable_water_rate < water_demand
        else "satisfied"
        if renewable_water_observed
        else "unknown"
    )
    renewable_water_reason = (
        "Observed renewable water production meets or exceeds observed demand."
        if renewable_water_status == "satisfied"
        else "Observed renewable water production is below demand."
        if renewable_water_status == "at_risk"
        else "Observed renewable water production and demand telemetry are both required."
    )
    if renewable_water_status == "at_risk" and metrics["water_deficit_kg_per_cycle"] is not None:
        renewable_water_reason = (
            f"Observed renewable water production is below demand by {metrics['water_deficit_kg_per_cycle']:.1f} kg/cycle "
            f"(production {renewable_water_rate:.1f}, demand {water_demand:.1f}). "
            "This rate shortfall does not establish usable water reserve or time to depletion."
        )
    objectives.append(
        _objective(
            "renewable_water",
            "Verify renewable water balance",
            phase="expand",
            priority=54,
            status=renewable_water_status,
            reason=renewable_water_reason,
            evidence=[p for p in (metrics["paths"]["renewable_water"], metrics["paths"]["water_demand"]) if p],
            completion=("Renewable water production >= observed demand",),
            telemetry=("production.water_kg_per_cycle", "consumption.water_kg_per_cycle"),
        )
    )

    renewable_power_rate = metrics["renewable_power_watts"]
    power_demand = metrics["power_demand_watts"]
    renewable_power_observed = all(
        metrics["qualities"].get(key) == "observed" for key in ("renewable_power", "power_demand")
    )
    renewable_power_status = (
        "blocked"
        if stale
        else "unknown"
        if renewable_power_rate is None or power_demand is None
        else "at_risk"
        if renewable_power_rate < power_demand
        else "satisfied"
        if renewable_power_observed
        else "unknown"
    )
    renewable_power_reason = (
        "Observed renewable power generation meets or exceeds observed load."
        if renewable_power_status == "satisfied"
        else "Observed renewable power generation is below load."
        if renewable_power_status == "at_risk"
        else "Observed renewable power generation and load telemetry are both required."
    )
    if renewable_power_status == "at_risk" and metrics["power_deficit_watts"] is not None:
        renewable_power_reason = (
            f"Observed renewable power generation is below load by {metrics['power_deficit_watts']:.1f} W "
            f"(generation {renewable_power_rate:.1f}, load {power_demand:.1f}). "
            "Rate evidence alone does not establish usable battery reserve or time to depletion."
        )
    objectives.append(
        _objective(
            "renewable_power",
            "Verify renewable power balance",
            phase="expand",
            priority=53,
            status=renewable_power_status,
            reason=renewable_power_reason,
            evidence=[p for p in (metrics["paths"]["renewable_power"], metrics["paths"]["power_demand"]) if p],
            completion=("Renewable power generation >= observed load",),
            telemetry=("production.power_watts", "consumption.power_watts"),
        )
    )

    redundancy_values = (
        metrics["independent_oxygen_sources"],
        metrics["independent_water_sources"],
        metrics["independent_power_sources"],
    )
    redundancy_known = all(value is not None for value in redundancy_values) and metrics["n_minus_one_safe"] is not None
    redundancy_risk = any(value is not None and value < 2 for value in redundancy_values) or metrics["n_minus_one_safe"] is False
    redundancy_observed = all(
        metrics["qualities"].get(key) == "observed"
        for key in (
            "independent_oxygen_sources",
            "independent_water_sources",
            "independent_power_sources",
            "n_minus_one_safe",
        )
    )
    fault_tolerance_status = (
        "blocked"
        if stale
        else "at_risk"
        if redundancy_risk
        else "satisfied"
        if redundancy_known and redundancy_observed
        else "unknown"
    )
    fault_tolerance_reason = (
        "A critical life-support domain lacks two independent paths or fails the N-1 check."
        if fault_tolerance_status == "at_risk"
        else "Independent oxygen, water, and power paths pass the observed N-1 check."
        if fault_tolerance_status == "satisfied"
        else "Independent-source counts and an observed N-1 result are required."
    )
    objectives.append(
        _objective(
            "fault_tolerance",
            "Prove N-1 life-support resilience",
            phase="expand",
            priority=52,
            status=fault_tolerance_status,
            reason=fault_tolerance_reason,
            evidence=[
                p
                for p in (
                    metrics["paths"]["independent_oxygen_sources"],
                    metrics["paths"]["independent_water_sources"],
                    metrics["paths"]["independent_power_sources"],
                    metrics["paths"]["n_minus_one_safe"],
                )
                if p
            ],
            completion=("Each critical resource has at least two independent paths", "Observed N-1 analysis passes"),
            telemetry=(
                "oxygen.independent_source_count",
                "water.independent_source_count",
                "power.independent_source_count",
                "resilience.n_minus_one_safe",
            ),
        )
    )

    space_known = any(
        metrics[key] is not None
        for key in ("rocket_count", "mission_ready", "mission_completed", "return_ready", "base_reserve_safe")
    )
    space_progress = bool(
        metrics["mission_completed"] is True
        and metrics["return_ready"] is True
        and metrics["base_reserve_safe"] is True
        and all(
            metrics["qualities"].get(key) == "observed"
            for key in ("mission_completed", "return_ready", "base_reserve_safe")
        )
    )
    space_risk = any(
        metrics[key] is False for key in ("mission_completed", "return_ready", "base_reserve_safe")
    )
    space_status = (
        "blocked"
        if stale
        else "satisfied"
        if space_progress
        else "at_risk"
        if space_risk
        else "unknown"
        if not space_known
        else "unknown"
    )
    space_reason = (
        "A completed mission, return capability, and safe base reserve were all observed."
        if space_progress
        else "Observed space readiness or return/base reserve check is unsafe."
        if space_risk
        else "Rocket presence/readiness alone cannot certify a completed safe space milestone."
        if space_known
        else "Rocket/space telemetry is unavailable; space milestones remain unverified."
    )
    objectives.append(
        _objective(
            "space_progress",
            "Advance space milestones safely",
            phase="space",
            priority=40,
            status=space_status,
            reason=space_reason,
            evidence=[
                p
                for p in (
                    metrics["paths"]["rocket_count"],
                    metrics["paths"]["mission_ready"],
                    metrics["paths"]["mission_completed"],
                    metrics["paths"]["return_ready"],
                    metrics["paths"]["base_reserve_safe"],
                )
                if p
            ],
            completion=("A mission is observed complete", "Return capability and base reserve are verified"),
            telemetry=("space.mission_completed", "space.return_ready", "space.base_reserve_safe"),
        )
    )

    loop_statuses = (
        oxygen_status,
        food_status,
        sanitation_status,
        water_status,
        power_status,
        stress_status,
        research_status,
        *industry_statuses,
        renewable_food_status,
        renewable_oxygen_status,
        renewable_water_status,
        renewable_power_status,
        fault_tolerance_status,
        temp_status,
        space_status,
    )
    loops_proven = all(status == "satisfied" for status in loop_statuses)
    immortal = metrics["sustainable"] is True and loops_proven
    immortal_known = metrics["victory"] is not None or metrics["sustainable"] is not None
    immortal_status = (
        "blocked"
        if stale
        else "satisfied"
        if immortal
        else "at_risk"
        if immortal_known
        else "deferred"
    )
    immortal_reason = (
        "The sustainability marker is backed by observed renewable, thermal, space, and N-1 gates."
        if immortal
        else "Sustainability telemetry is unsupported; indefinite survival cannot be certified from this observation."
        if metrics["qualities"].get("sustainable") == "unsupported"
        else "The sustainability marker is observed false; indefinite survival cannot be certified from this observation."
        if metrics["sustainable"] is False
        else "Victory/sustainability markers cannot replace renewable-loop, thermal, space, and N-1 proof."
        if immortal_known
        else "Indefinite survival remains unproven until every renewable and fault-tolerance gate is observed."
    )
    objectives.append(
        _objective(
            "indefinite_survival",
            "Prove an indefinitely sustainable colony",
            phase="sustain",
            priority=30,
            status=immortal_status,
            reason=immortal_reason,
            evidence=[
                p
                for p in (
                    metrics["paths"]["victory"],
                    metrics["paths"]["sustainable"],
                    metrics["paths"]["renewable_food"],
                    metrics["paths"]["renewable_oxygen"],
                    metrics["paths"]["renewable_water"],
                    metrics["paths"]["renewable_power"],
                    metrics["paths"]["n_minus_one_safe"],
                )
                if p
            ],
            completion=(
                "Renewable food, oxygen, water, power, and thermal loops are observed",
                "No critical incidents over a sustained window",
            ),
            telemetry=(
                "colony.sustainable",
                "game.victory",
                "production.food_kcal_per_cycle",
                "production.oxygen_kg_per_cycle",
                "production.water_kg_per_cycle",
                "production.power_watts",
                "resilience.n_minus_one_safe",
            ),
        )
    )

    by_id = {item["id"]: item for item in objectives}
    bridge_meta = value.get("bridge") if isinstance(value, Mapping) and isinstance(value.get("bridge"), Mapping) else {}
    bridge_trust_ready = bridge_meta.get("trust_ready") is True
    raw_bridge_blockers = bridge_meta.get("trust_blockers")
    bridge_blockers_valid = bool(
        isinstance(raw_bridge_blockers, list)
        and all(isinstance(item, str) and item.strip() for item in raw_bridge_blockers)
    )
    trust_blockers = list(raw_bridge_blockers) if bridge_blockers_valid else ["bridge_trust_blockers_invalid"]
    scope_blockers = _observation_scope_blockers(value)
    trust_blockers.extend(scope_blockers)
    state_trusted = bool(
        value is not None
        and not stale
        and bridge_trust_ready
        and bridge_blockers_valid
        and not raw_bridge_blockers
        and not scope_blockers
    )

    gates: List[Dict[str, Any]] = []
    detected_phase = "sustain"
    for gate_phase in PHASE_ORDER:
        objective_ids = PHASE_EXIT_GATES[gate_phase]
        predicates = [
            {
                "objective_id": objective_id,
                "status": by_id[objective_id]["status"],
                "satisfied": by_id[objective_id]["status"] == "satisfied",
            }
            for objective_id in objective_ids
        ]
        statuses = [item["status"] for item in predicates]
        gate_status = (
            "blocked"
            if not state_trusted or any(status == "blocked" for status in statuses)
            else "at_risk"
            if any(status == "at_risk" for status in statuses)
            else "unknown"
            if any(status in {"unknown", "deferred"} for status in statuses)
            else "satisfied"
        )
        blockers = [
            f"objective_{item['objective_id']}_{item['status']}"
            for item in predicates
            if item["status"] != "satisfied"
        ]
        if not state_trusted:
            blockers.extend(trust_blockers or ["observation_trust_failed"])
        gates.append(
            {
                "phase": gate_phase,
                "status": gate_status,
                "objective_ids": list(objective_ids),
                "predicates": predicates,
                "blockers": sorted(set(blockers)),
                "single_snapshot_only": True,
                "stable_window_verified": False,
            }
        )
        if gate_status != "satisfied":
            detected_phase = gate_phase
            break

    certified_phase = previous_phase if previous_phase in PHASE_ORDER else "bootstrap"
    detected_index = PHASE_ORDER.index(detected_phase)
    certified_index = PHASE_ORDER.index(certified_phase)
    if detected_index < certified_index:
        # Safety regressions are immediate; forward promotion remains held for
        # a session-bound rolling evidence tracker.
        certified_phase = detected_phase
        certified_index = detected_index
    phase = certified_phase
    next_phase = PHASE_ORDER[min(certified_index + 1, len(PHASE_ORDER) - 1)]
    active_gate = next((item for item in gates if item["phase"] == phase), gates[-1])
    readiness_blockers = list(active_gate.get("blockers") or [])
    readiness_blockers.extend(_sustainability_readiness_blockers(metrics))
    if active_gate.get("status") == "satisfied":
        readiness_blockers.append("stable_window_unverified")
    readiness_status = (
        "blocked"
        if not state_trusted
        else "not_ready"
        if active_gate.get("status") in {"at_risk", "satisfied"}
        else "unknown"
    )
    phase_reason = (
        "Observation trust failed; remain in recovery/observation mode."
        if not state_trusted
        else f"{active_gate['phase']} gate is {active_gate['status']}; forward promotion requires a continuous trusted window."
    )
    confidence = (
        "blocked"
        if not state_trusted
        else "medium"
        if active_gate.get("status") == "satisfied"
        else "low"
    )

    critical = [by_id[item] for item in sorted(SAFETY_CRITICAL_OBJECTIVES)]
    critical_risk = any(item["status"] == "at_risk" for item in critical)
    critical_unknown = any(item["status"] in {"unknown", "blocked", "deferred"} for item in critical)

    for item in objectives:
        urgency_bonus = {"blocked": 20, "at_risk": 15, "unknown": 5, "deferred": 0, "satisfied": -10}[item["status"]]
        item["effective_priority"] = item["priority"] + urgency_bonus
    objectives.sort(key=lambda item: (-int(item["effective_priority"]), item["id"]))
    for item in objectives:
        item.pop("effective_priority", None)

    recommendations: List[Dict[str, Any]] = []
    for item in objectives:
        recommendations.extend(item.get("recommended_actions") or [])
    recommendations.sort(key=lambda item: (item.get("risk") != "high", item.get("id", "")))

    status = (
        "blocked"
        if not state_trusted
        else "at_risk"
        if critical_risk
        else "degraded"
        if critical_unknown
        else "ok"
    )
    operating_mode = (
        "recover"
        if not state_trusted
        else "emergency"
        if critical_risk
        else "observe"
        if critical_unknown
        else "observe"
    )
    # Contract mismatch details are useful on a live trusted snapshot, but a
    # rejected replay/state already has trust blockers.  Avoid a second full
    # quality-contract pass over every rejected historical record.
    selected_contract_blockers = (
        {
            path: blockers
            for path in sorted({path for path in metrics.get("paths", {}).values() if isinstance(path, str)})
            if (blockers := _field_contract_blockers(value, path))
        }
        if state_trusted
        else {}
    )
    if state_trusted and metrics.get("paths", {}).get("battery") == "power.battery_percent":
        power_blockers = _power_battery_percent_v2_blockers(value)
        if power_blockers:
            selected_contract_blockers["power.battery_percent"] = power_blockers
    telemetry = {
        "quality_summary": {"observed": quality[0], "estimated": quality[1], "blocked": quality[2]},
        "required": _required_quality(value, core_paths),
        "stale": stale,
        "trust_ready": state_trusted,
        "trust_blockers": sorted(set(trust_blockers)),
        "contract_blockers": selected_contract_blockers,
    }
    result: Dict[str, Any] = {
        "schema": STRATEGY_SCHEMA,
        "generated_at": (now or dt.datetime.now(dt.timezone.utc).astimezone()).isoformat(timespec="seconds"),
        "mode": "deterministic",
        "auto_execute": False,
        "goal": _safe_text(goal or STRATEGY_GOAL, 500),
        **identity,
        "status": status,
        "operating_mode": operating_mode,
        "phase": {
            "id": phase,
            "certified": phase,
            "target": next_phase,
            "detected_from_single_snapshot": detected_phase,
            "order": PHASE_ORDER.index(phase),
            "confidence": confidence,
            "reason": _safe_text(phase_reason, 600),
            "active_gate": active_gate.get("phase"),
        },
        "readiness": {
            "status": readiness_status,
            "next_phase": next_phase,
            "blockers": sorted(set(readiness_blockers)),
            "required_consecutive_samples": PHASE_STABILITY_SAMPLES[phase],
            "observed_consecutive_samples": 0,
            "required_duration_seconds": PHASE_STABILITY_MIN_SECONDS[phase],
            "observed_duration_seconds": 0.0,
            "required_game_cycles": PHASE_STABILITY_MIN_GAME_CYCLES[phase],
            "observed_game_cycles": 0.0,
            "stable_window_verified": False,
        },
        "gates": gates,
        "metrics": metrics,
        "telemetry": telemetry,
        "objectives": objectives,
        "next_actions": recommendations[:8],
        "guardrails": [
            "Observe fresh telemetry before acting on an unknown or blocked objective.",
            "Estimated values may escalate risk but never certify a phase gate.",
            "A single snapshot may trigger regression, never forward promotion.",
            "Every game action remains explicitly approval-gated by the command queue.",
            "Bind any future command to this session, save hash, world, snapshot, and state hash.",
            "Never mutate save files or invent resource/tech/rocket state from absent fields.",
        ],
    }
    validate_strategy(result)
    return result


def evaluate_strategy(
    state: Optional[Mapping[str, Any]],
    *,
    goal: str = STRATEGY_GOAL,
    now: Optional[dt.datetime] = None,
    previous_phase: Optional[str] = None,
    trusted_proof_verifier: Optional[TrustedProofVerifier] = None,
) -> Dict[str, Any]:
    """Evaluate one snapshot with an evaluation-scoped quality-status cache."""

    cache_token = _QUALITY_STATUS_CACHE.set({})
    try:
        return _evaluate_strategy_impl(
            state,
            goal=goal,
            now=now,
            previous_phase=previous_phase,
            trusted_proof_verifier=trusted_proof_verifier,
        )
    finally:
        _QUALITY_STATUS_CACHE.reset(cache_token)


def validate_strategy(strategy: Mapping[str, Any]) -> None:
    """Validate the stable public strategy schema."""

    if not isinstance(strategy, Mapping) or strategy.get("schema") != STRATEGY_SCHEMA:
        raise StrategyError(f"strategy.schema must be {STRATEGY_SCHEMA}")
    if strategy.get("mode") != "deterministic" or strategy.get("auto_execute") is not False:
        raise StrategyError("strategy must be deterministic and non-executing")
    if strategy.get("status") not in {"ok", "degraded", "at_risk", "blocked"}:
        raise StrategyError("strategy.status is invalid")
    if strategy.get("operating_mode") not in {"observe", "execute", "recover", "emergency"}:
        raise StrategyError("strategy.operating_mode is invalid")
    telemetry = strategy.get("telemetry")
    if not isinstance(telemetry, Mapping) or not isinstance(telemetry.get("trust_ready"), bool):
        raise StrategyError("strategy.telemetry trust state is invalid")
    telemetry_blockers = telemetry.get("trust_blockers")
    if not isinstance(telemetry_blockers, list) or any(
        not isinstance(item, str) or not item for item in telemetry_blockers
    ):
        raise StrategyError("strategy.telemetry trust blockers are invalid")
    if telemetry.get("trust_ready") is True and telemetry_blockers:
        raise StrategyError("strategy.telemetry cannot be trust-ready with blockers")
    phase = strategy.get("phase")
    if not isinstance(phase, Mapping) or phase.get("id") not in PHASE_ORDER:
        raise StrategyError("strategy.phase.id is invalid")
    if phase.get("confidence") not in PHASE_CONFIDENCE:
        raise StrategyError("strategy.phase.confidence is invalid")
    if phase.get("certified") not in PHASE_ORDER or phase.get("target") not in PHASE_ORDER:
        raise StrategyError("strategy.phase certified/target is invalid")
    readiness = strategy.get("readiness")
    if not isinstance(readiness, Mapping) or readiness.get("status") not in READINESS_STATUSES:
        raise StrategyError("strategy.readiness is invalid")
    if not isinstance(readiness.get("blockers"), list) or not isinstance(
        readiness.get("stable_window_verified"), bool
    ):
        raise StrategyError("strategy.readiness evidence fields are invalid")
    if readiness.get("status") == "ready" and readiness.get("stable_window_verified") is not True:
        raise StrategyError("strategy.readiness cannot be ready without a verified stable window")
    for key in ("required_consecutive_samples", "observed_consecutive_samples"):
        value = readiness.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise StrategyError(f"strategy.readiness.{key} is invalid")
    for key in (
        "required_duration_seconds",
        "observed_duration_seconds",
        "required_game_cycles",
        "observed_game_cycles",
    ):
        value = readiness.get(key)
        number = _number(value)
        if number is None or number < 0:
            raise StrategyError(f"strategy.readiness.{key} is invalid")
    if readiness.get("status") == "ready" and (
        readiness.get("observed_consecutive_samples") < readiness.get("required_consecutive_samples")
        or readiness.get("observed_duration_seconds") < readiness.get("required_duration_seconds")
        or readiness.get("observed_game_cycles") < readiness.get("required_game_cycles")
    ):
        raise StrategyError("strategy.readiness ready evidence is below its declared thresholds")
    gates = strategy.get("gates")
    if not isinstance(gates, list) or not gates:
        raise StrategyError("strategy.gates must be a non-empty list")
    seen_gate_phases: set[str] = set()
    for gate in gates:
        if not isinstance(gate, Mapping) or gate.get("phase") not in PHASE_ORDER:
            raise StrategyError("strategy gate phase is invalid")
        gate_phase = str(gate.get("phase"))
        if gate_phase in seen_gate_phases or gate.get("status") not in {
            "satisfied",
            "at_risk",
            "unknown",
            "blocked",
        }:
            raise StrategyError("strategy gate status/order is invalid")
        seen_gate_phases.add(gate_phase)
        if gate.get("objective_ids") != list(PHASE_EXIT_GATES[gate_phase]):
            raise StrategyError(f"strategy gate {gate_phase} objective set is invalid")
        if not isinstance(gate.get("predicates"), list) or not isinstance(gate.get("blockers"), list):
            raise StrategyError(f"strategy gate {gate_phase} details are invalid")
    objectives = strategy.get("objectives")
    if not isinstance(objectives, list) or not objectives:
        raise StrategyError("strategy.objectives must be a non-empty list")
    seen: set[str] = set()
    for item in objectives:
        if not isinstance(item, Mapping):
            raise StrategyError("strategy objective must be an object")
        identifier = str(item.get("id") or "")
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", identifier) or identifier in seen:
            raise StrategyError("objective ids must be unique safe ids")
        seen.add(identifier)
        if item.get("phase") not in PHASE_ORDER or item.get("status") not in OBJECTIVE_STATUSES:
            raise StrategyError(f"objective {identifier} phase/status is invalid")
        if not isinstance(item.get("priority"), int):
            raise StrategyError(f"objective {identifier} priority is invalid")
        for key in ("evidence", "completion_criteria", "required_telemetry", "recommended_actions"):
            if not isinstance(item.get(key), list):
                raise StrategyError(f"objective {identifier}.{key} must be a list")
        for action in item.get("recommended_actions") or []:
            if (
                not isinstance(action, Mapping)
                or action.get("auto_execute") is not False
                or action.get("requires_approval") is not True
            ):
                raise StrategyError(f"objective {identifier} contains an executable recommendation")
            if action.get("action") == "set_paused":
                raise StrategyError(
                    f"objective {identifier} bypasses survival emergency arbitration"
                )
    expected_objectives = {identifier for identifiers in PHASE_EXIT_GATES.values() for identifier in identifiers}
    if not expected_objectives.issubset(seen):
        raise StrategyError("strategy is missing one or more phase-gate objectives")
    next_actions = strategy.get("next_actions")
    if not isinstance(next_actions, list) or len(next_actions) > 8:
        raise StrategyError("strategy.next_actions is invalid")
    if any(
        not isinstance(action, Mapping) or action.get("action") == "set_paused"
        for action in next_actions
    ):
        raise StrategyError("strategy.next_actions bypasses survival emergency arbitration")


def build_strategy_markdown(strategy: Mapping[str, Any]) -> str:
    """Render a bounded human-readable strategy report."""

    validate_strategy(strategy)
    phase = strategy["phase"]
    readiness = strategy.get("readiness") if isinstance(strategy.get("readiness"), Mapping) else {}
    metrics = strategy.get("metrics") if isinstance(strategy.get("metrics"), Mapping) else {}
    lines = [
        "# ONI AI Deterministic Strategy",
        "",
        f"Generated: {strategy.get('generated_at')}",
        f"Status: {strategy.get('status')}",
        f"Operating mode: {strategy.get('operating_mode')}",
        f"Certified phase: {phase.get('certified')} ({phase.get('confidence')} confidence)",
        f"Target phase: {phase.get('target')}",
        f"Single-snapshot detection: {phase.get('detected_from_single_snapshot')}",
        f"Readiness: {readiness.get('status')}",
        f"Readiness blockers: {', '.join(readiness.get('blockers') or []) or 'none'}",
        f"Reason: {_safe_text(phase.get('reason'), 500)}",
        f"Session: {strategy.get('session_id') or 'unknown'}",
        f"Snapshot: {strategy.get('snapshot_id') or 'unknown'}",
        "",
        "## Metrics",
        "",
        f"- Cycle: {metrics.get('cycle') if metrics.get('cycle') is not None else 'unknown'}",
        f"- Duplicants: {metrics.get('duplicants') if metrics.get('duplicants') is not None else 'unknown'}",
        f"- Food runway: {metrics.get('food_runway_cycles') if metrics.get('food_runway_cycles') is not None else 'unknown'} cycles",
        f"- Water per duplicant: {metrics.get('water_kg_per_duplicant') if metrics.get('water_kg_per_duplicant') is not None else 'unknown'} kg",
        f"- Rolling visible-cell oxygen diagnostic: {metrics.get('visible_cell_oxygen_kg') if metrics.get('visible_cell_oxygen_kg') is not None else 'unknown'} kg (risk-only)",
        f"- Rolling visible-cell water-family diagnostic: {metrics.get('visible_cell_water_family_kg') if metrics.get('visible_cell_water_family_kg') is not None else 'unknown'} kg (non-certifying)",
        f"- Oxygen production/demand: {metrics.get('renewable_oxygen_kg_per_cycle')} / {metrics.get('oxygen_demand_kg_per_cycle')} kg/cycle",
        f"- Water production/demand: {metrics.get('renewable_water_kg_per_cycle')} / {metrics.get('water_demand_kg_per_cycle')} kg/cycle",
        f"- Power generation/load: {metrics.get('renewable_power_watts')} / {metrics.get('power_demand_watts')} W",
        "",
        "## Phase Gates",
        "",
    ]
    for gate in strategy.get("gates") or []:
        lines.append(
            f"- {gate.get('phase')}: {gate.get('status')}"
            + (f" — {', '.join(gate.get('blockers') or [])}" if gate.get("blockers") else "")
        )
    lines.extend([
        "",
        "## Objectives",
        "",
    ])
    for index, item in enumerate(strategy.get("objectives") or [], start=1):
        lines.extend(
            [
                f"### {index}. {item.get('title')}",
                "",
                f"- ID: {item.get('id')}",
                f"- Phase: {item.get('phase')}",
                f"- Status: {item.get('status')}",
                f"- Reason: {_safe_text(item.get('reason'), 500)}",
                f"- Evidence: {', '.join(item.get('evidence') or []) or 'none'}",
            ]
        )
        actions = item.get("recommended_actions") or []
        if actions:
            lines.append("- Recommendation: " + ", ".join(str(action.get("action")) for action in actions))
        lines.append("")
    lines.extend(["## Guardrails", ""])
    lines.extend(f"- {item}" for item in strategy.get("guardrails") or [])
    text = "\n".join(lines).rstrip() + "\n"
    encoded = text.encode("utf-8")
    if len(encoded) > 16 * 1024:
        text = encoded[: 16 * 1024 - 32].decode("utf-8", errors="ignore").rstrip() + "\n[truncated]\n"
    return text


__all__ = [
    "STRATEGY_SCHEMA",
    "STRATEGY_GOAL",
    "PHASE_EXIT_GATES",
    "PHASE_ORDER",
    "PHASE_STABILITY_SAMPLES",
    "PhaseEvidenceTracker",
    "StrategyError",
    "build_strategy_markdown",
    "collect_metrics",
    "evaluate_strategy",
    "validate_strategy",
]
