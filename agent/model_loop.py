"""Minimal model-driven closed loop (code review 2026-09-26, M1).

Each Bridge generation derives at most five catalogued candidate commands from
the trusted snapshot (agent.candidates).  A background request asks the
configured model to return exactly one ``candidate_id``; the Bridge then
publishes that candidate's complete local contract as one pending
``model_selected`` command bound to the committed decision frame of the
generation that publishes it.  The model contributes the choice only: it never
supplies actions, arguments or conditions.

Manual mode uses the existing local human approval path
(scripts/approve-command.ps1). Explicit autonomous mode uses a bound V2 policy,
V4 intent and the durable autonomous authorizer. The executor re-checks every
precondition against live game state, and every outcome feeds the next prompt.

The loop is explicit opt-in (``oni_bridge.py --model-loop``) and never runs in
replay.  It yields to a rule's mutating suggestion (the arbitrated survival
pause), any other command in the pipeline, and an active Colony Plan; other
open rule findings are passed to the model as context.
"""

from __future__ import annotations

import collections
import dataclasses
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import tempfile
import threading
import time
import urllib.request
from typing import Any, Callable, Deque, Dict, List, Mapping, Optional, Sequence, Tuple

from agent.action_policy import action_policy_hash, action_policy_is_valid
from agent.action_outcomes import command_intent_hash
from agent.canonical_hash import canonical_bytes, loads_strict
from agent.candidates import WAIT_CANDIDATE_ID, display_text, generate_candidates, oxygen_balance
from agent.colony_plan import TERMINAL_PLAN_STATUSES, load_active_plan
from agent.command_custody import lifecycle_custody, publish_pending_command, read_bytes_share_delete, read_stable_bytes
from agent.command_queue import (
    COMMAND_DIRS,
    COMMAND_SCHEMA,
    MODEL_SELECTED_MODE,
    command_hint_is_current,
    command_pipeline_readiness,
    now,
    trusted_proposal_state,
)
from agent.decision_frame_archive import load_archived_decision_frame
from agent.field_contracts import NON_MUTATING_ACTIONS
from agent.llm_planner import (
    effective_request_timeout,
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    MAX_PROMPT_TOKENS,
    ModelRequestError,
    atomic_write_json,
    classify_request_failure,
    compact_value,
    config_health,
    estimate_cost_usd,
    estimate_tokens,
    execute_request,
    extract_message_content,
    normalize_usage,
    parse_json_content,
    resolve_llm_config,
)
from agent.state_quality import canonical_state_hash, field_status, parse_snapshot_timestamp, safe_get


MODEL_LOOP_SCHEMA = "oni.model-loop.v1"
MODEL_LOOP_STATE_SCHEMA = "oni.model-loop-state.v1"
CHOICE_SCHEMA = "oni.llm-choice.v1"
CHOICE_INPUT_SCHEMA = "oni.model-choice-input.v1"
MODEL_CHOICE_SOURCE = "model_choice"
AUTONOMOUS_CONTROL_SCHEMA = "oni.autonomous-control.v1"
AUTONOMOUS_CONTROL_NAME = "autonomous-control.json"
AUTONOMOUS_POLICY_SCHEMA = "oni.action-policy.v2"
AUTONOMOUS_COMMAND_SCHEMA = "oni.command.v4"
AUTONOMOUS_INTENT_STATES = frozenset({"reserved", "pending", "authorized", "uncertain", "terminal"})
_AUTHORITY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_AUTHORITY_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
AUTONOMOUS_STARTUP_FIELDS = frozenset({
    "validation_run_id", "validation_process_id", "startup_binding_hash",
    "startup_intent_hash", "runtime_config_evidence_sha256", "loader_path_id_upper",
    "loader_save_sha256", "game_build", "mod_version",
})
DEFAULT_GOAL = "Keep the colony alive with conservative, reversible actions."
DEFAULT_DEADLINE_SECONDS = 90.0
PER_OPERATION_TIMEOUT_SECONDS = 30.0
MIN_REQUEST_INTERVAL_SECONDS = 20.0
MAX_REQUESTS_PER_HOUR = 30
SAME_SET_RECHECK_SECONDS = 300.0
DECISION_TTL_SECONDS = 120.0
FAILURE_BACKOFF_SECONDS = (60.0, 120.0, 300.0)
FEEDBACK_LIMIT = 5
PUBLISHED_HISTORY_LIMIT = 20
MAX_REASON_CHARS = 200
NONTERMINAL_STAGES = frozenset({"pending", "approving", "approved", "executing", "confirming"})
SYSTEM_PROMPT = "Return only the JSON object that selects exactly one listed candidate_id."

ModelCaller = Callable[..., Dict[str, Any]]


def write_autonomous_document(path: pathlib.Path, payload: Mapping[str, Any]) -> None:
    """Durably publish an identity/control record before a queue side effect."""

    raw = (json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
    temporary: Optional[pathlib.Path] = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=str(path.parent), delete=False, suffix=".tmp") as handle:
            temporary = pathlib.Path(handle.name)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            descriptor = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        observed, _, _ = read_stable_bytes(path, label="autonomous durable record")
        if observed != raw:
            raise ValueError("autonomous_document_commit_mismatch")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def validate_autonomous_control(
    value: Any,
    *,
    policy: Mapping[str, Any],
    latest_state: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Validate explicit local authority; no identity is inferred or discovered."""

    required = {
        "schema", "run_id", "controller_id", "session_id", "world_id", "save_id_hash",
        "startup_expectations", "desired_state",
    }
    optional = {"updated_at", "controller_pid"}
    if not isinstance(value, Mapping) or not required <= set(value) or set(value) - required - optional:
        raise ValueError("autonomous_control_shape_invalid")
    control = dict(value)
    if control.get("schema") != AUTONOMOUS_CONTROL_SCHEMA:
        raise ValueError("autonomous_control_schema_invalid")
    for field in ("run_id", "controller_id", "session_id"):
        item = control.get(field)
        if not isinstance(item, str) or _AUTHORITY_ID_RE.fullmatch(item) is None:
            raise ValueError(f"autonomous_control_{field}_invalid")
    world = control.get("world_id")
    if isinstance(world, bool) or not isinstance(world, int) or not 0 <= world <= 2147483647:
        raise ValueError("autonomous_control_world_id_invalid")
    if not isinstance(control.get("save_id_hash"), str) or _AUTHORITY_HASH_RE.fullmatch(control["save_id_hash"]) is None:
        raise ValueError("autonomous_control_save_id_hash_invalid")
    if control.get("desired_state") not in {"running", "paused", "stopped"}:
        raise ValueError("autonomous_control_desired_state_invalid")
    if "updated_at" in control and parse_snapshot_timestamp(control["updated_at"]) is None:
        raise ValueError("autonomous_control_updated_at_invalid")
    if "controller_pid" in control and (type(control["controller_pid"]) is not int or control["controller_pid"] <= 0):
        raise ValueError("autonomous_control_controller_pid_invalid")
    anchors = control.get("startup_expectations")
    if not isinstance(anchors, Mapping) or set(anchors) != AUTONOMOUS_STARTUP_FIELDS:
        raise ValueError("autonomous_startup_expectations_invalid")
    pid = anchors.get("validation_process_id")
    if isinstance(pid, bool) or not isinstance(pid, int) or not 0 < pid <= 2147483647:
        raise ValueError("autonomous_startup_process_id_invalid")
    if anchors.get("validation_run_id") != control["run_id"]:
        raise ValueError("autonomous_startup_run_mismatch")
    for field in ("startup_binding_hash", "startup_intent_hash", "runtime_config_evidence_sha256", "loader_path_id_upper", "loader_save_sha256"):
        if not isinstance(anchors.get(field), str) or _AUTHORITY_HASH_RE.fullmatch(anchors[field]) is None:
            raise ValueError(f"autonomous_startup_{field}_invalid")
    for field in ("game_build", "mod_version"):
        if not isinstance(anchors.get(field), str) or not anchors[field].strip() or len(anchors[field]) > 80:
            raise ValueError(f"autonomous_startup_{field}_invalid")
    if anchors["loader_save_sha256"] != control["save_id_hash"]:
        raise ValueError("autonomous_startup_clone_hash_mismatch")
    if not action_policy_is_valid(policy) or policy.get("schema") != AUTONOMOUS_POLICY_SCHEMA or policy.get("execution_mode") != "autonomous":
        raise ValueError("autonomous_policy_invalid")
    if any(canonical_bytes(policy.get(field)) != canonical_bytes(control[field]) for field in ("run_id", "controller_id", "session_id", "world_id")):
        raise ValueError("autonomous_control_policy_identity_mismatch")
    if policy.get("allowed_save_hashes") != [control["save_id_hash"]] or policy.get("max_pending_commands") != 1:
        raise ValueError("autonomous_control_policy_scope_mismatch")
    if latest_state is not None:
        if any(canonical_bytes(latest_state.get(field)) != canonical_bytes(control[field]) for field in ("session_id", "world_id", "save_id_hash")):
            raise ValueError("autonomous_control_state_identity_mismatch")
        if latest_state.get("validation_run_id") != control["run_id"] or latest_state.get("startup_binding_status") != "verified":
            raise ValueError("autonomous_control_state_run_mismatch")
        for field in ("validation_run_id", "validation_process_id", "startup_binding_hash", "startup_intent_hash", "runtime_config_evidence_sha256", "game_build", "mod_version"):
            if latest_state.get(field) != anchors[field]:
                raise ValueError(f"autonomous_control_state_{field}_mismatch")
        if latest_state.get("requested_save_path_id") != anchors["loader_path_id_upper"]:
            raise ValueError("autonomous_control_state_loader_path_mismatch")
        if latest_state.get("active_save_path_id") != anchors["loader_path_id_upper"]:
            raise ValueError("autonomous_control_state_active_path_mismatch")
    return control


def load_autonomous_control(
    bridge_dir: pathlib.Path,
    *,
    policy: Mapping[str, Any],
    latest_state: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    raw, _, _ = read_stable_bytes(pathlib.Path(bridge_dir) / AUTONOMOUS_CONTROL_NAME, label="autonomous control")
    return validate_autonomous_control(loads_strict(raw.decode("utf-8-sig")), policy=policy, latest_state=latest_state)


def _assert_committed_frame(root: pathlib.Path, frame: Mapping[str, Any]) -> None:
    """Require the exact root/session/archive commit before creating authority."""

    archived, _, _ = load_archived_decision_frame(root, frame.get("frame_id"))
    session_id = safe_get(dict(frame), "source.session_id")
    if not isinstance(session_id, str) or _AUTHORITY_ID_RE.fullmatch(session_id) is None:
        raise ValueError("autonomous_frame_session_invalid")
    session_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", session_id)
    expected = canonical_bytes(dict(frame))
    if canonical_bytes(archived) != expected:
        raise ValueError("autonomous_frame_archive_mismatch")
    for path in (root / "decision-frame.json", root / "sessions" / session_name / "decision-frame.json"):
        raw, _, _ = read_stable_bytes(path, label="committed autonomous frame")
        if canonical_bytes(loads_strict(raw.decode("utf-8-sig"))) != expected:
            raise ValueError("autonomous_frame_commit_mismatch")


@dataclasses.dataclass
class _Request:
    request_id: str
    candidate_set_hash: str
    candidates: Dict[str, Dict[str, Any]]
    prompt_hash: str
    model: str
    submitted_at: str
    started: float
    deadline: float
    done: threading.Event = dataclasses.field(default_factory=threading.Event)
    result: Optional[Dict[str, Any]] = None
    abandoned: bool = False


def choice_response_schema(candidate_ids: Sequence[str]) -> Dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["schema", "candidate_id", "reason"],
        "properties": {
            "schema": {"type": "string", "enum": [CHOICE_SCHEMA]},
            "candidate_id": {"type": "string", "enum": list(candidate_ids)},
            "reason": {"type": "string"},
        },
    }


def validate_choice_output(
    parsed: Any, candidate_ids: Sequence[str]
) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
    if not isinstance(parsed, dict):
        return None, "invalid_json"
    if not set(parsed) <= {"schema", "candidate_id", "reason"} or parsed.get("schema") != CHOICE_SCHEMA:
        return None, "invalid_output_schema"
    candidate_id = parsed.get("candidate_id")
    reason = parsed.get("reason", "")
    if not isinstance(candidate_id, str) or not isinstance(reason, str):
        return None, "invalid_output_schema"
    if candidate_id not in candidate_ids:
        return None, "invalid_candidate_reference"
    return {"candidate_id": candidate_id, "reason": display_text(reason, MAX_REASON_CHARS)}, None


SUMMARY_FIELDS = (
    "cycle",
    "game.paused",
    "duplicants.total",
    "duplicants.idle",
    "duplicants.stressed",
    "resources.food_kcal",
    "resources.oxygen_kg_estimate",
    "resources.water_kg_estimate",
    "alerts",
    "research.current",
    # Oxygen balance from the game's daily report (negative_kg is
    # consumption) and the power picture, so the model can see a colony
    # losing air before an alert fires.
    "report_counters.previous_closed.report_day",
    "report_counters.previous_closed.oxygen_created.positive_kg",
    "report_counters.previous_closed.oxygen_created.negative_kg",
    "report_counters.current.report_day",
    "report_counters.current.oxygen_created.positive_kg",
    "report_counters.current.oxygen_created.negative_kg",
    # Completed Oxygen Diffusers (MineralDeoxidizer) in the active world; the
    # interpreted balance is colony.oxygen_balance (agent.candidates).
    "buildings.oxygen_diffuser",
    "power.generator_count",
    "power.battery_count",
    "power.active_load_watts",
    "power.active_rated_generation_watts",
)


def colony_summary(state: Mapping[str, Any]) -> Dict[str, Any]:
    snapshot = dict(state)
    # Only values the Probe marks observed reach the model: an unsupported,
    # stale or failed reader shows as null rather than as a number.
    fields = {
        path: compact_value(safe_get(snapshot, path), depth=0) if field_status(snapshot, path) == "observed" else None
        for path in SUMMARY_FIELDS
    }
    by_instance = safe_get(snapshot, "duplicants.by_instance_id")
    duplicants = []
    if isinstance(by_instance, dict):
        entries = [entry for entry in by_instance.values() if isinstance(entry, dict)]
        for entry in sorted(entries, key=_instance_order)[:10]:
            priorities = entry.get("chore_priorities") if isinstance(entry.get("chore_priorities"), dict) else {}
            duplicants.append(
                {
                    "name": display_text(entry.get("name")),
                    "chore_priorities": {
                        str(group)[:64]: value
                        for group, value in sorted(priorities.items())
                        if isinstance(value, int) and not isinstance(value, bool)
                    },
                }
            )
    return {"fields": fields, "duplicants": duplicants, "oxygen_balance": oxygen_balance(snapshot)}


def _instance_order(entry: Mapping[str, Any]) -> int:
    value = entry.get("instance_id")
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def advice_suggests_mutation(item: Mapping[str, Any]) -> bool:
    """True when a rule advice item carries a mutating (or malformed) command."""

    command = item.get("command")
    return isinstance(command, Mapping) and command.get("action") not in NON_MUTATING_ACTIONS


def build_choice_payload(
    state: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    outcomes: Sequence[Mapping[str, Any]],
    goal: str,
    advice: Sequence[Mapping[str, Any]] = (),
    *,
    autonomous: bool = False,
) -> Dict[str, Any]:
    return {
        "schema": CHOICE_INPUT_SCHEMA,
        "goal": display_text(goal, 500),
        "colony": colony_summary(state),
        "rule_findings": [
            {"title": display_text(item.get("title"), 160), "risk": display_text(item.get("risk"), 16)}
            for item in list(advice)[:FEEDBACK_LIMIT]
            if isinstance(item, Mapping)
        ],
        "candidates": [
            {key: item[key] for key in ("candidate_id", "summary", "action", "risk")}
            for item in candidates
        ],
        "recent_outcomes": [
            {key: item.get(key) for key in ("summary", "action", "stage", "status", "detail", "effect_confirmed")}
            for item in outcomes
        ],
        "rules": [
            "Choose exactly one candidate_id from candidates; never invent actions or arguments.",
            "Choose wait when no candidate helps the goal right now.",
            (
                "A chosen action needs the bound autonomous authorization and live game verification."
                if autonomous else "A chosen action still needs local human approval and is verified against the game."
            ),
            "recent_outcomes lists earlier choices and whether the game confirmed their effect.",
            "colony.fields holds only values the game observed; null means not observed.",
            "colony.oxygen_balance.*_net_kg is oxygen made minus oxygen breathed per cycle from the game's report; "
            "a negative value means the colony is losing breathable air, and a candidate that places an oxygen "
            "producer (Oxygen Diffuser, Algae Terrarium) addresses it before an alert fires.",
            "rule_findings lists open findings of the local safety rules (informational).",
        ],
    }


def build_choice_prompt_text(payload: Mapping[str, Any]) -> str:
    return (
        "You choose the next action for an Oxygen Not Included colony. Return JSON only: "
        '{"schema":"oni.llm-choice.v1","candidate_id":"<one id from candidates>",'
        '"reason":"<one short sentence>"}.\n\n'
        + json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )


def call_choice_model(
    config: Mapping[str, Any],
    prompt_text: str,
    response_schema: Optional[Mapping[str, Any]],
    *,
    timeout: float,
) -> Dict[str, Any]:
    """POST one choice request; ``response_schema`` None omits structured output."""

    base_url = str(config.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
    model = config.get("model") or DEFAULT_MODEL
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt_text},
    ]
    # No temperature: several reasoning models reject it (review M5).
    if str(config.get("wire_api") or "").lower().startswith("responses"):
        url = base_url + "/responses"
        body: Dict[str, Any] = {"model": model, "input": messages}
        if response_schema is not None:
            body["text"] = {
                "format": {"type": "json_schema", "name": "oni_choice", "strict": True, "schema": response_schema}
            }
    else:
        url = base_url + "/chat/completions"
        body = {"model": model, "messages": messages}
        if response_schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "oni_choice", "strict": True, "schema": response_schema},
            }
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + str(config["api_key"])},
        method="POST",
    )
    return execute_request(request, timeout=timeout)


def _sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _locate_command(bridge_dir: pathlib.Path, command_id: str) -> Tuple[Optional[str], Dict[str, Any]]:
    for stage in COMMAND_DIRS:
        path = bridge_dir / "commands" / stage / f"{command_id}.json"
        if not path.is_file():
            continue
        try:
            value = json.loads(read_bytes_share_delete(path).decode("utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return stage, {}
        return stage, value if isinstance(value, dict) else {}
    return None, {}


def build_model_command(
    state: Mapping[str, Any],
    frame: Mapping[str, Any],
    policy: Mapping[str, Any],
    candidate: Mapping[str, Any],
    decision: Mapping[str, Any],
    *,
    autonomous: bool = False,
) -> Dict[str, Any]:
    """Project one chosen candidate into a pending ``model_selected`` command."""

    if autonomous and (not action_policy_is_valid(policy) or policy.get("schema") != AUTONOMOUS_POLICY_SCHEMA or policy.get("execution_mode") != "autonomous"):
        raise ValueError("autonomous_policy_invalid")
    hint = candidate["hint"]
    state_hash = canonical_state_hash(dict(state))
    policy_hash = action_policy_hash(policy)
    created = now()
    expires = created + dt.timedelta(seconds=max(1, int(policy["command_ttl_seconds"])))
    identity_fields = {
            "session_id": state.get("session_id"),
            "source_frame_id": frame.get("frame_id"),
            "state_hash": state_hash,
            "request_id": decision["request_id"],
            "contract_hash": candidate["contract_hash"],
        }
    if autonomous:
        identity_fields.update({"run_id": policy["run_id"], "controller_id": policy["controller_id"]})
    identity = json.dumps(
        identity_fields,
        sort_keys=True,
        separators=(",", ":"),
    )
    command = {
        "schema": AUTONOMOUS_COMMAND_SCHEMA if autonomous else COMMAND_SCHEMA,
        "id": "cmd-model-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32],
        "mode": MODEL_SELECTED_MODE,
        "status": "pending_autonomous_authorization" if autonomous else "pending_human_approval",
        "action": hint["action"],
        "args": hint["args"],
        "session_id": state.get("session_id"),
        "save_id_hash": state.get("save_id_hash"),
        "world_id": state.get("world_id"),
        "source_incident_id": None,
        "source_snapshot_id": state.get("snapshot_id"),
        "state_hash": state_hash,
        "source_frame_id": frame.get("frame_id"),
        "created_at": created.isoformat(timespec="seconds"),
        "expires_at": expires.isoformat(timespec="seconds"),
        "risk": candidate.get("risk") or "low",
        "reason": ("Model choice: " + str(candidate["summary"]))[:500],
        "evidence": {
            "candidate_id": candidate["candidate_id"],
            "candidate_set_hash": decision["candidate_set_hash"],
            "model_reason": decision.get("reason") or "",
            "model": decision.get("model"),
            "prompt_hash": decision.get("prompt_hash"),
            "response_hash": decision.get("response_hash"),
            "request_id": decision["request_id"],
        },
        "preconditions": hint["preconditions"],
        "expected_effects": hint["expected_effects"],
        "requires_approval": True,
        "source": {
            "kind": MODEL_CHOICE_SOURCE,
            "candidate_id": candidate["candidate_id"],
            "candidate_set_hash": decision["candidate_set_hash"],
            "request_id": decision["request_id"],
            "state_seq": state.get("seq"),
            "state_ts": state.get("ts"),
            "policy_hash": policy_hash,
        },
    }
    if autonomous:
        command.update({"run_id": policy["run_id"], "controller_id": policy["controller_id"]})
    return command


class ModelLoop:
    """One Bridge-owned model decision loop; call ``step`` once per generation."""

    def __init__(
        self,
        *,
        secrets_dir: Optional[pathlib.Path] = None,
        goal: str = DEFAULT_GOAL,
        model_caller: Optional[ModelCaller] = None,
        config_override: Optional[Dict[str, Any]] = None,
        deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
        min_interval_seconds: float = MIN_REQUEST_INTERVAL_SECONDS,
        max_requests_per_hour: int = MAX_REQUESTS_PER_HOUR,
        monotonic: Callable[[], float] = time.monotonic,
        autonomous: bool = False,
        autonomous_identity: Optional[Mapping[str, Any]] = None,
        round_command_limit: Optional[int] = None,
    ) -> None:
        self._secrets_dir = secrets_dir
        self._goal = goal
        self._model_caller = model_caller
        self._config_override = config_override
        self._deadline_seconds = float(deadline_seconds)
        self._min_interval = float(min_interval_seconds)
        self._max_per_hour = int(max_requests_per_hour)
        self._monotonic = monotonic
        self.autonomous = bool(autonomous)
        self._autonomous_identity = dict(autonomous_identity or {})
        if round_command_limit is not None and (not self.autonomous or type(round_command_limit) is not int or round_command_limit <= 0):
            raise ValueError("round_command_limit must be a positive integer in autonomous mode")
        self._round_command_limit = round_command_limit
        self._autonomous_limits: Dict[str, int] = {}
        if self.autonomous and set(self._autonomous_identity) != {"run_id", "controller_id", "session_id", "world_id", "save_id_hash", "startup_expectations"}:
            raise ValueError("autonomous loop requires explicit bound controller identity and startup anchors")
        self._request: Optional[_Request] = None
        self._decision: Optional[Dict[str, Any]] = None
        self._answers: Dict[str, Tuple[str, float]] = {}
        self._request_starts: Deque[float] = collections.deque()
        self._next_request_at = 0.0
        self._failures = 0
        self._last_event: Optional[Dict[str, Any]] = None
        self._last_choice: Optional[Dict[str, Any]] = None
        self._llm_health: Dict[str, Any] = {}
        self._published: Optional[List[Dict[str, Any]]] = None
        self._preserved_autonomous_intents: Optional[List[Any]] = None
        self._preserved_autonomous_limits: Optional[Dict[str, Any]] = None
        self._request_counter = 0
        self._autonomous_intents: Optional[List[Dict[str, Any]]] = None
        self._autonomous_load_error: Optional[str] = None
        self._desired_state: Optional[str] = None

    # -- public -----------------------------------------------------------

    def join_request(self, timeout: float) -> bool:
        """Wait for the in-flight request (tests and diagnostics only)."""

        request = self._request
        return True if request is None else request.done.wait(timeout)

    def step(
        self,
        bridge_dir: pathlib.Path,
        latest: Optional[Dict[str, Any]],
        frame: Mapping[str, Any],
        policy: Mapping[str, Any],
        advice: Sequence[Mapping[str, Any]],
    ) -> Dict[str, Any]:
        root = pathlib.Path(bridge_dir)
        current = self._monotonic()
        reconciliation_blockers = self._reconcile_autonomous_intents(root) if self.autonomous else []
        published_history = self._load_published(root)
        outcomes = self._recent_outcomes(root, published_history)
        blockers = self._eligibility_blockers(root, latest, frame, policy, advice, outcomes)
        blockers.extend(reconciliation_blockers)
        candidate_set: Dict[str, Any] = {"items": [], "candidate_set_hash": None, "blockers": []}
        if not blockers:
            try:
                candidate_set = generate_candidates(latest, allowed_actions=policy.get("allowed_actions") or [], autonomous=self.autonomous)
            except Exception:  # noqa: BLE001 - candidate generation fails closed
                blockers.append("candidate_generation_error")
            else:
                blockers.extend(candidate_set.get("blockers") or [])

        self._harvest(current)
        published: Optional[Dict[str, Any]] = None
        if self._decision is not None:
            if current - self._decision["decided_monotonic"] > DECISION_TTL_SECONDS:
                self._event("decision_expired", candidate_id=self._decision["candidate"]["candidate_id"])
                self._decision = None
            elif not blockers:
                published = self._publish(root, latest, frame, policy, candidate_set)
        if not blockers and published is None and self._decision is None:
            self._submit(latest, candidate_set, outcomes, advice, current)

        status = self._status(root, latest, frame, candidate_set, blockers, outcomes, published, current)
        atomic_write_json(root / "model-loop.json", status)
        return status

    # -- eligibility --------------------------------------------------------

    def _eligibility_blockers(
        self,
        root: pathlib.Path,
        latest: Optional[Dict[str, Any]],
        frame: Mapping[str, Any],
        policy: Mapping[str, Any],
        advice: Sequence[Mapping[str, Any]],
        outcomes: Sequence[Mapping[str, Any]],
    ) -> List[str]:
        blockers: List[str] = []
        if not trusted_proposal_state(latest):
            return ["state_untrusted"]
        trust = frame.get("trust") if isinstance(frame.get("trust"), Mapping) else {}
        propose = safe_get(dict(frame), "readiness.propose", {})
        if trust.get("status") != "trusted":
            blockers.append("frame_untrusted")
        if not (isinstance(propose, dict) and propose.get("ready") is True and propose.get("blockers") == []):
            blockers.append("frame_proposal_not_ready")
        source = frame.get("source") if isinstance(frame.get("source"), Mapping) else {}
        if (
            source.get("snapshot_id") != latest.get("snapshot_id")
            or source.get("state_hash") != canonical_state_hash(latest)
        ):
            blockers.append("frame_source_mismatch")
        if not action_policy_is_valid(policy):
            blockers.append("policy_invalid")
        elif policy.get("actions_enabled") is not True:
            blockers.append("actions_disabled")
        elif policy.get("suggestions_enabled") is not True:
            blockers.append("suggestions_disabled")
        elif latest.get("save_id_hash") not in (policy.get("allowed_save_hashes") or []):
            blockers.append("save_not_allowed")
        if self.autonomous:
            blockers.extend(self.autonomous_publication_blockers(root))
            try:
                control = load_autonomous_control(root, policy=policy, latest_state=latest)
                self._desired_state = control["desired_state"]
                if any(control.get(field) != value for field, value in self._autonomous_identity.items()):
                    blockers.append("autonomous_controller_identity_changed")
                if control["desired_state"] != "running":
                    blockers.append("autonomous_issuance_revoked")
                _assert_committed_frame(root, frame)
            except Exception:  # noqa: BLE001 - authority inputs fail closed
                blockers.append("autonomous_authority_unavailable")
        elif policy.get("schema") == AUTONOMOUS_POLICY_SCHEMA:
            blockers.append("autonomous_policy_requires_explicit_mode")
        elif self._preserved_autonomous_intents is not None and any(not isinstance(entry, Mapping) or entry.get("state") != "terminal" for entry in self._preserved_autonomous_intents):
            blockers.append("autonomous_intent_requires_reconciliation")
        if any(isinstance(item, Mapping) and advice_suggests_mutation(item) for item in advice):
            # A rule's mutating suggestion (the arbitrated survival pause)
            # owns the pipeline.  Informational and focus-note findings (idle
            # duplicants, a missing or non-operational building) can stay open
            # for a whole session, so they go into the prompt instead.
            blockers.append("rules_mutation_suggested")
        if (root / "colony-plans" / "active.json").exists():
            try:
                plan_status = load_active_plan(root).get("status")
            except Exception:  # noqa: BLE001 - an unreadable plan fails closed
                plan_status = None
            if plan_status not in TERMINAL_PLAN_STATUSES:
                blockers.append("colony_plan_active")
        if any(item.get("stage") in NONTERMINAL_STAGES for item in outcomes):
            blockers.append("model_command_in_flight")
        if not blockers:
            gate = command_pipeline_readiness(root, latest_state=latest, proposal_frame=frame, policy=policy)
            if not safe_get(gate, "readiness.mutation_proposal.ready", False):
                blockers.append("command_pipeline_blocked")
        return blockers

    # -- request lifecycle --------------------------------------------------

    def _harvest(self, current: float) -> None:
        request = self._request
        if request is None:
            return
        if request.done.is_set():
            self._request = None
            if not request.abandoned:
                self._record_result(request, request.result or {"status": "request_failed"}, current)
            return
        if current >= request.deadline and not request.abandoned:
            # The worker keeps running until its socket times out; only one
            # request may be alive, so no new one starts before it drains.
            request.abandoned = True
            self._record_failure(request, "deadline_exceeded", current)

    def _record_result(self, request: _Request, result: Mapping[str, Any], current: float) -> None:
        status = str(result.get("status") or "request_failed")
        choice = result.get("choice") if isinstance(result.get("choice"), Mapping) else None
        details = {
            "request_id": request.request_id,
            "usage": result.get("usage"),
            "cost_estimate_usd": result.get("cost_estimate_usd"),
            "response_hash": result.get("response_hash"),
            "structured_output": result.get("structured_output"),
            "elapsed_seconds": round(current - request.started, 3),
        }
        if status != "ok" or choice is None:
            self._record_failure(request, status, current, **details)
            return
        self._failures = 0
        candidate_id = str(choice["candidate_id"])
        self._answers[request.candidate_set_hash] = (candidate_id, current)
        self._event("model_choice", candidate_id=candidate_id, reason=choice.get("reason"), **details)
        self._last_choice = self._last_event
        if candidate_id == WAIT_CANDIDATE_ID:
            return
        self._decision = {
            "request_id": request.request_id,
            "candidate_set_hash": request.candidate_set_hash,
            "candidate": request.candidates[candidate_id],
            "reason": choice.get("reason") or "",
            "model": request.model,
            "prompt_hash": request.prompt_hash,
            "response_hash": result.get("response_hash"),
            "decided_monotonic": current,
        }

    def _record_failure(self, request: _Request, status: str, current: float, **details: Any) -> None:
        self._failures += 1
        backoff = FAILURE_BACKOFF_SECONDS[min(self._failures, len(FAILURE_BACKOFF_SECONDS)) - 1]
        self._next_request_at = max(self._next_request_at, current + backoff)
        details.setdefault("request_id", request.request_id)
        self._event("request_failed", status=status, retry_after_seconds=backoff, **details)

    def _submit(
        self,
        latest: Optional[Dict[str, Any]],
        candidate_set: Mapping[str, Any],
        outcomes: Sequence[Mapping[str, Any]],
        advice: Sequence[Mapping[str, Any]],
        current: float,
    ) -> None:
        if self._request is not None:
            return
        items = list(candidate_set.get("items") or [])
        if not any(item["candidate_id"] != WAIT_CANDIDATE_ID for item in items):
            return
        set_hash = str(candidate_set.get("candidate_set_hash"))
        # Only recent answers suppress a repeat question; drop the rest.
        self._answers = {
            key: value for key, value in self._answers.items() if current - value[1] < SAME_SET_RECHECK_SECONDS
        }
        answered = self._answers.get(set_hash)
        if answered is not None and current - answered[1] < SAME_SET_RECHECK_SECONDS:
            return
        while self._request_starts and current - self._request_starts[0] >= 3600.0:
            self._request_starts.popleft()
        if current < self._next_request_at or len(self._request_starts) >= self._max_per_hour:
            return
        config = resolve_llm_config(self._secrets_dir, self._config_override)
        self._llm_health = config_health(config)
        if config.get("config_error") or not config.get("api_key"):
            self._next_request_at = current + FAILURE_BACKOFF_SECONDS[0]
            self._event("config_unavailable", status=str(config.get("config_error") or "missing_api_key"))
            return
        payload = build_choice_payload(latest or {}, items, outcomes, self._goal, advice, autonomous=self.autonomous)
        prompt_text = build_choice_prompt_text(payload)
        if estimate_tokens(prompt_text) > MAX_PROMPT_TOKENS:
            self._next_request_at = current + FAILURE_BACKOFF_SECONDS[-1]
            self._event("prompt_budget_exceeded", estimated_tokens=estimate_tokens(prompt_text))
            return
        self._request_counter += 1
        request = _Request(
            request_id=f"req-{int(time.time())}-{self._request_counter}",
            candidate_set_hash=set_hash,
            candidates={item["candidate_id"]: item for item in items},
            prompt_hash=_sha256_text(prompt_text),
            model=str(config.get("model") or DEFAULT_MODEL),
            submitted_at=now().isoformat(timespec="seconds"),
            started=current,
            deadline=current + self._deadline_seconds,
        )
        self._request = request
        self._request_starts.append(current)
        self._next_request_at = current + self._min_interval
        worker = threading.Thread(
            target=self._run_request,
            args=(request, config, prompt_text),
            name="oni-model-choice",
            daemon=True,
        )
        worker.start()

    def _run_request(self, request: _Request, config: Dict[str, Any], prompt_text: str) -> None:
        try:
            ids = list(request.candidates)
            response, structured = self._call(request, config, prompt_text, ids)
            content = extract_message_content(response)
            choice, error = validate_choice_output(parse_json_content(content), ids)
            usage = normalize_usage(response.get("usage"))
            request.result = {
                "status": "ok" if error is None else error,
                "choice": choice,
                "usage": usage,
                "cost_estimate_usd": estimate_cost_usd(usage, config),
                # Only a hash of the provider response is kept, as in the planner.
                "response_hash": _sha256_text(content),
                "structured_output": structured,
            }
        except Exception as exc:  # noqa: BLE001 - every failure becomes a status
            request.result = {"status": classify_request_failure(exc)}
        finally:
            request.done.set()

    def _call(
        self, request: _Request, config: Dict[str, Any], prompt_text: str, ids: List[str]
    ) -> Tuple[Dict[str, Any], str]:
        caller = self._model_caller or call_choice_model
        schema = choice_response_schema(ids)
        for structured, response_schema in (("json_schema", schema), ("plain", None)):
            remaining = request.deadline - self._monotonic()
            if remaining <= 1.0:
                raise ModelRequestError("deadline_exceeded")
            try:
                response = caller(
                    config,
                    prompt_text,
                    response_schema,
                    timeout=min(max(PER_OPERATION_TIMEOUT_SECONDS, effective_request_timeout(config)), remaining),
                )
            except ModelRequestError as exc:
                # Gateways without structured output answer 400; retry once
                # without it (the local validation stays authoritative).
                if structured == "json_schema" and exc.code == "http_400":
                    continue
                raise
            if not isinstance(response, dict):
                raise ModelRequestError("invalid_response")
            return response, structured
        raise ModelRequestError("http_400")

    # -- publication and feedback -------------------------------------------

    def _publish(
        self,
        root: pathlib.Path,
        latest: Optional[Dict[str, Any]],
        frame: Mapping[str, Any],
        policy: Mapping[str, Any],
        candidate_set: Mapping[str, Any],
    ) -> Optional[Dict[str, Any]]:
        decision = self._decision
        assert decision is not None
        chosen = decision["candidate"]
        current = {item["candidate_id"]: item for item in candidate_set.get("items") or []}
        match = current.get(chosen["candidate_id"])
        if (
            match is None
            or match.get("contract_hash") != chosen.get("contract_hash")
            or not command_hint_is_current(latest, match.get("hint"))
            or self.autonomous and decision.get("candidate_set_hash") != candidate_set.get("candidate_set_hash")
        ):
            # The game moved on while the model answered; ask again.
            self._decision = None
            self._event("choice_stale", candidate_id=chosen["candidate_id"])
            return None
        command = build_model_command(latest or {}, frame, policy, match, decision, autonomous=self.autonomous)
        if self.autonomous:
            # From this point the exact id is the only reconciliable intent.
            # A failed/uncertain write must never let this choice generate a
            # replacement against another frame.
            self._decision = None
            with lifecycle_custody(root):
                control = load_autonomous_control(root, policy=policy, latest_state=latest)
                if control["desired_state"] != "running":
                    self._event("autonomous_issuance_revoked", command_id=command["id"])
                    return None
                _assert_committed_frame(root, frame)
                gate = command_pipeline_readiness(root, latest_state=latest, proposal_frame=frame, policy=policy)
                if not safe_get(gate, "readiness.mutation_proposal.ready", False):
                    self._event("autonomous_pipeline_blocked", command_id=command["id"])
                    return None
                self.reserve_autonomous_command(root, command)
                try:
                    publish_pending_command(root, command)
                except Exception:  # noqa: BLE001 - no publish replay on uncertain custody
                    self._set_autonomous_intent(root, str(command["id"]), "uncertain", "publication_outcome_uncertain")
                    self._event("autonomous_publication_uncertain", command_id=command["id"])
                    return self._entry_for_command(command)
                self._set_autonomous_intent(root, str(command["id"]), "pending")
                self.authorize_published_commands(root, [command], latest=latest or {}, frame=frame, policy=policy)
            return self._entry_for_command(command)
        publish_pending_command(root, command)
        self._decision = None
        entry = {
            "command_id": command["id"],
            "candidate_id": match["candidate_id"],
            "summary": match["summary"],
            "action": match["action"],
            "session_id": command["session_id"],
            "published_at": command["created_at"],
            "expires_at": command["expires_at"],
        }
        history = self._load_published(root)
        history.append(entry)
        del history[:-PUBLISHED_HISTORY_LIMIT]
        state_payload = {"schema": MODEL_LOOP_STATE_SCHEMA, "published": history}
        if self._preserved_autonomous_intents is not None:
            state_payload["autonomous_intents"] = self._preserved_autonomous_intents
        if self._preserved_autonomous_limits is not None:
            state_payload["autonomous_limits"] = self._preserved_autonomous_limits
        atomic_write_json(root / "model-loop-state.json", state_payload)
        self._event("published", command_id=command["id"], candidate_id=match["candidate_id"])
        return entry

    @staticmethod
    def _entry_for_command(command: Mapping[str, Any]) -> Dict[str, Any]:
        evidence = command.get("evidence") if isinstance(command.get("evidence"), Mapping) else {}
        return {
            "command_id": command["id"], "candidate_id": evidence.get("candidate_id"),
            "summary": command.get("reason"), "action": command.get("action"),
            "session_id": command.get("session_id"), "published_at": command.get("created_at"),
            "expires_at": command.get("expires_at"), "authorization": "autonomous_controller",
        }

    def _load_autonomous_intents(self, root: pathlib.Path) -> List[Dict[str, Any]]:
        if self._autonomous_intents is None:
            self._autonomous_intents = []
            path = root / "model-loop-state.json"
            try:
                if path.exists():
                    raw, _, _ = read_stable_bytes(path, label="autonomous intent ledger")
                    value = loads_strict(raw.decode("utf-8-sig"))
                    if not isinstance(value, dict) or value.get("schema") != MODEL_LOOP_STATE_SCHEMA:
                        raise ValueError("intent_ledger_shape_invalid")
                    entries = value.get("autonomous_intents", [])
                    limits = value.get("autonomous_limits", {})
                    if not isinstance(limits, dict) or any(not isinstance(key, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}:[A-Za-z0-9][A-Za-z0-9._-]{0,79}", key) is None or type(limit) is not int or limit <= 0 for key, limit in limits.items()):
                        raise ValueError("autonomous_round_limits_invalid")
                    self._autonomous_limits = dict(limits)
                    key = self._autonomous_budget_key()
                    stored_limit = limits.get(key)
                    if stored_limit is not None:
                        if self._round_command_limit is not None and self._round_command_limit > stored_limit:
                            raise ValueError("autonomous_round_limit_cannot_be_widened")
                        self._round_command_limit = stored_limit if self._round_command_limit is None else min(self._round_command_limit, stored_limit)
                    if not isinstance(entries, list):
                        raise ValueError("intent_ledger_entries_invalid")
                    ids = set()
                    for entry in entries:
                        if not isinstance(entry, dict) or not isinstance(entry.get("command_id"), str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", entry["command_id"]) is None or entry["command_id"] in ids:
                            raise ValueError("intent_ledger_identity_invalid")
                        if not isinstance(entry.get("intent_hash"), str) or _AUTHORITY_HASH_RE.fullmatch(entry["intent_hash"]) is None:
                            raise ValueError("intent_ledger_hash_invalid")
                        if entry.get("state") not in AUTONOMOUS_INTENT_STATES:
                            raise ValueError("intent_ledger_state_invalid")
                        if not isinstance(entry.get("run_id"), str) or _AUTHORITY_ID_RE.fullmatch(entry["run_id"]) is None or not isinstance(entry.get("controller_id"), str) or _AUTHORITY_ID_RE.fullmatch(entry["controller_id"]) is None:
                            raise ValueError("intent_ledger_authority_invalid")
                        ids.add(entry["command_id"])
                    self._autonomous_intents = entries
            except Exception:  # noqa: BLE001 - a damaged ledger cannot mint another intent
                self._autonomous_load_error = "autonomous_intent_ledger_unreadable"
        return self._autonomous_intents

    def _persist_autonomous_intents(self, root: pathlib.Path) -> None:
        if self._autonomous_load_error is not None:
            raise ValueError(self._autonomous_load_error)
        write_autonomous_document(root / "model-loop-state.json", {
            "schema": MODEL_LOOP_STATE_SCHEMA, "published": self._load_published(root),
            "autonomous_intents": self._load_autonomous_intents(root),
            "autonomous_limits": dict(self._autonomous_limits),
        })

    def _autonomous_budget_key(self) -> str:
        return str(self._autonomous_identity.get("run_id")) + ":" + str(self._autonomous_identity.get("controller_id"))

    def _autonomous_reserved_count(self, root: pathlib.Path) -> int:
        return sum(1 for entry in self._load_autonomous_intents(root) if entry.get("run_id") == self._autonomous_identity.get("run_id") and entry.get("controller_id") == self._autonomous_identity.get("controller_id"))

    def bind_autonomous_budget(self, root: pathlib.Path) -> None:
        """Persist the optional round bound before any rule/model can publish."""

        if not self.autonomous:
            raise ValueError("autonomous budget requires explicit mode")
        with lifecycle_custody(root):
            self._load_autonomous_intents(root)
            if self._autonomous_load_error:
                raise ValueError(self._autonomous_load_error)
            if self._round_command_limit is not None:
                self._autonomous_limits[self._autonomous_budget_key()] = self._round_command_limit
                self._persist_autonomous_intents(root)

    def autonomous_publication_blockers(self, root: pathlib.Path) -> List[str]:
        self._load_autonomous_intents(root)
        if self._autonomous_load_error:
            return [self._autonomous_load_error]
        if self._round_command_limit is not None and self._autonomous_reserved_count(root) >= self._round_command_limit:
            return ["autonomous_round_command_limit_exhausted"]
        return []

    def reserve_autonomous_command(self, root: pathlib.Path, command: Mapping[str, Any]) -> None:
        """Called under custody before model or rule pending publication."""

        if not self.autonomous or command.get("schema") != AUTONOMOUS_COMMAND_SCHEMA or command.get("status") != "pending_autonomous_authorization":
            raise ValueError("autonomous_reservation_route_invalid")
        if any(command.get(field) != self._autonomous_identity[field] for field in ("run_id", "controller_id", "session_id", "world_id", "save_id_hash")):
            raise ValueError("autonomous_reservation_identity_mismatch")
        # The rule callback executes under sync's custody lock. A paused
        # controller cannot reserve an emergency or an ordinary intent.
        raw, _, _ = read_stable_bytes(root / AUTONOMOUS_CONTROL_NAME, label="autonomous control")
        control = loads_strict(raw.decode("utf-8-sig"))
        if not isinstance(control, Mapping) or control.get("desired_state") != "running" or any(control.get(field) != value for field, value in self._autonomous_identity.items()):
            raise ValueError("autonomous_issuance_revoked")
        entries = self._load_autonomous_intents(root)
        if self._autonomous_load_error:
            raise ValueError(self._autonomous_load_error)
        if self.autonomous_publication_blockers(root):
            raise ValueError("autonomous_round_command_limit_exhausted")
        if self._reconcile_autonomous_intents(root):
            raise ValueError("autonomous_owned_intent_unresolved")
        if any(entry.get("state") != "terminal" for entry in entries):
            raise ValueError("autonomous_owned_intent_unresolved")
        command_id = str(command["id"])
        if any(entry["command_id"] == command_id for entry in entries):
            raise ValueError("autonomous_intent_already_reserved")
        entry = self._entry_for_command(command)
        entry.update({"intent_hash": command_intent_hash(command), "state": "reserved", "source_frame_id": command.get("source_frame_id"), "run_id": command["run_id"], "controller_id": command["controller_id"]})
        checkpoint = {
            "schema": "oni.autonomous-action-checkpoint.v1", "action_id": command_id,
            "run_id": command["run_id"], "controller_id": command["controller_id"], "created_at": command["created_at"],
            "source": {field: command.get(field) for field in ("session_id", "world_id", "save_id_hash", "source_snapshot_id", "state_hash", "source_frame_id")},
            "fresh_preconditions": command["preconditions"],
            "intended_action": {"action": command["action"], "args": command["args"]},
            "expected_evidence": command["expected_effects"],
            "rollback": "reobserve; no inverse authority inferred",
        }
        args = command.get("args")
        if command.get("action") == "set_duplicant_chore_priority" and isinstance(args, Mapping) and type(args.get("from_priority")) is int and type(args.get("priority")) is int:
            inverse_args = dict(args)
            inverse_args.update({"from_priority": args["priority"], "priority": args["from_priority"]})
            checkpoint["rollback"] = {
                "mode": "reobserve_then_request_inverse", "authority_granted": False,
                "requires_fresh_snapshot": True, "requires_new_authorization": True,
                "proposed_inverse": {"action": command["action"], "args": inverse_args},
            }
        write_autonomous_document(root / "autonomous-action-checkpoint.json", checkpoint)
        print(json.dumps({
            "schema": checkpoint["schema"], "action_id": command_id, "run_id": command["run_id"],
            "source_frame_id": command["source_frame_id"], "action": command["action"],
            "precondition_count": len(command["preconditions"]), "expected_effect_count": len(command["expected_effects"]),
            "checkpoint_file": "autonomous-action-checkpoint.json", "rollback": checkpoint["rollback"],
        }, ensure_ascii=False, allow_nan=False), flush=True)
        if self._round_command_limit is not None:
            self._autonomous_limits[self._autonomous_budget_key()] = self._round_command_limit
        entries.append(entry)
        self._load_published(root).append(self._entry_for_command(command))
        del self._published[:-PUBLISHED_HISTORY_LIMIT]
        # A successful durable reservation precedes any pending side effect.
        self._persist_autonomous_intents(root)

    def _set_autonomous_intent(self, root: pathlib.Path, command_id: str, state: str, detail: Optional[str] = None) -> None:
        for entry in self._load_autonomous_intents(root):
            if entry["command_id"] == command_id:
                entry.update({"state": state, "detail": detail, "updated_at": now().isoformat(timespec="seconds")})
                self._persist_autonomous_intents(root)
                return
        raise ValueError("autonomous_owned_intent_missing")

    def authorize_published_commands(
        self, root: pathlib.Path, commands: Sequence[Mapping[str, Any]], *,
        latest: Mapping[str, Any], frame: Mapping[str, Any], policy: Mapping[str, Any],
    ) -> None:
        """Authorize only this generation's newly published, reserved V4 intent."""

        if not self.autonomous:
            raise ValueError("autonomous_authorizer_requires_explicit_mode")
        from agent.autonomous_authorization import AutonomousAuthorizationError, authorize_autonomous_command

        for command in commands:
            if command.get("schema") != AUTONOMOUS_COMMAND_SCHEMA:
                raise ValueError("legacy_pending_has_no_autonomous_authority")
            command_id = str(command["id"])
            owned = next((entry for entry in self._load_autonomous_intents(root) if entry["command_id"] == command_id), None)
            if owned is None or owned["intent_hash"] != command_intent_hash(command):
                raise ValueError("autonomous_published_intent_mismatch")
            _assert_committed_frame(root, frame)
            self._set_autonomous_intent(root, command_id, "pending")
            try:
                approved = authorize_autonomous_command(root, command_id, latest_state=latest, frame=frame, policy=policy)
            except AutonomousAuthorizationError as exc:
                uncertain = bool(getattr(exc, "outcome_unknown", False))
                self._set_autonomous_intent(root, command_id, "uncertain" if uncertain else "pending", "authorization_outcome_unknown" if uncertain else "authorization_refused")
                self._event("autonomous_authorization_unknown" if uncertain else "autonomous_authorization_refused", command_id=command_id)
            except Exception:  # noqa: BLE001 - reconcile the same custody id after every unknown result
                self._set_autonomous_intent(root, command_id, "uncertain", "authorization_outcome_uncertain")
                self._event("autonomous_authorization_unknown", command_id=command_id)
            else:
                if not isinstance(approved, Mapping) or approved.get("id") != command_id or command_intent_hash(approved) != owned["intent_hash"]:
                    self._set_autonomous_intent(root, command_id, "uncertain", "authorization_return_identity_mismatch")
                    self._event("autonomous_authorization_unknown", command_id=command_id)
                else:
                    self._set_autonomous_intent(root, command_id, "authorized")
                    self._event("autonomous_authorized", command_id=command_id)

    def _reconcile_autonomous_intents(self, root: pathlib.Path) -> List[str]:
        """Read actual custody; never recreate or re-authorize a previous intent."""

        entries = self._load_autonomous_intents(root)
        if self._autonomous_load_error is not None:
            return [self._autonomous_load_error]
        blockers: List[str] = []
        changed = False
        with lifecycle_custody(root):
            for entry in entries:
                if entry.get("state") == "terminal":
                    continue
                command_id = entry["command_id"]
                locations = [stage for stage in COMMAND_DIRS if (root / "commands" / stage / f"{command_id}.json").exists()]
                stage, command = _locate_command(root, command_id)
                if len(locations) != 1 or not command or command.get("id") != command_id or command_intent_hash(command) != entry["intent_hash"]:
                    blockers.append(f"autonomous_intent_outcome_unresolved:{command_id}")
                    continue
                if stage in NONTERMINAL_STAGES:
                    blockers.append(f"autonomous_intent_in_flight:{command_id}")
                else:
                    gate = command_pipeline_readiness(root)
                    if any(command_id in str(item) for item in safe_get(gate, "readiness.mutation_proposal.blockers", [])):
                        blockers.append(f"autonomous_intent_outcome_unresolved:{command_id}")
                    else:
                        entry.update({"state": "terminal", "stage": stage, "status": command.get("status")})
                        changed = True
            if changed:
                self._persist_autonomous_intents(root)
        return blockers

    def _load_published(self, root: pathlib.Path) -> List[Dict[str, Any]]:
        if self._published is None:
            self._published = []
            path = root / "model-loop-state.json"
            try:
                value = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
            except (OSError, UnicodeError, json.JSONDecodeError):
                value = {}
            items = value.get("published") if isinstance(value, dict) else None
            if isinstance(items, list):
                self._published = [item for item in items if isinstance(item, dict) and item.get("command_id")]
            if isinstance(value, dict) and isinstance(value.get("autonomous_intents"), list):
                self._preserved_autonomous_intents = value["autonomous_intents"]
            if isinstance(value, dict) and isinstance(value.get("autonomous_limits"), dict):
                self._preserved_autonomous_limits = value["autonomous_limits"]
        return self._published

    def _recent_outcomes(self, root: pathlib.Path, history: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
        outcomes = []
        for entry in reversed(history[-FEEDBACK_LIMIT:]):
            stage, command = _locate_command(root, str(entry["command_id"]))
            result = command.get("result") if isinstance(command.get("result"), dict) else {}
            detail = (
                command.get("rejection_reason")
                or command.get("quarantine_reason")
                or result.get("classification")
                or command.get("classification")
            )
            outcomes.append(
                {
                    "command_id": entry["command_id"],
                    "candidate_id": entry.get("candidate_id"),
                    "summary": entry.get("summary"),
                    "action": entry.get("action"),
                    "published_at": entry.get("published_at"),
                    "stage": stage or "missing",
                    "status": command.get("status"),
                    "detail": display_text(detail, 120) if detail else None,
                    "effect_confirmed": stage == "done" and command.get("status") == "confirmed",
                }
            )
        return outcomes

    # -- status ---------------------------------------------------------------

    def _event(self, kind: str, **details: Any) -> None:
        self._last_event = {"kind": kind, "at": now().isoformat(timespec="seconds"), **details}

    def _status(
        self,
        root: pathlib.Path,
        latest: Optional[Dict[str, Any]],
        frame: Mapping[str, Any],
        candidate_set: Mapping[str, Any],
        blockers: Sequence[str],
        outcomes: Sequence[Mapping[str, Any]],
        published: Optional[Mapping[str, Any]],
        current: float,
    ) -> Dict[str, Any]:
        request = self._request
        pending = next((item for item in outcomes if item.get("stage") == "pending"), None)
        if self.autonomous:
            current_outcomes = self._recent_outcomes(root, self._load_published(root))
            pending = next((item for item in current_outcomes if item.get("stage") == "pending"), None)
            active = next((item for item in current_outcomes if item.get("stage") in NONTERMINAL_STAGES), None)
            if self._desired_state in {"paused", "stopped"}:
                phase = "draining" if active is not None else "issuance_revoked"
            elif pending is not None:
                phase = "awaiting_autonomous_authorization"
            elif active is not None:
                phase = "awaiting_game_effect"
            elif blockers:
                phase = "blocked"
            elif request is not None:
                phase = "draining_abandoned_request" if request.abandoned else "awaiting_model"
            elif self._decision is not None:
                phase = "choice_awaiting_eligibility"
            else:
                phase = "idle"
            outcomes = current_outcomes
        elif published is not None or pending is not None:
            phase = "awaiting_human_approval"
        elif request is not None:
            phase = "draining_abandoned_request" if request.abandoned else "awaiting_model"
        elif self._decision is not None:
            phase = "choice_awaiting_eligibility"
        elif blockers:
            phase = "blocked"
        else:
            phase = "idle"
        state = latest if isinstance(latest, dict) else {}
        reserved_count = self._autonomous_reserved_count(root) if self.autonomous and self._autonomous_load_error is None else None
        return {
            "schema": MODEL_LOOP_SCHEMA,
            "updated_at": now().isoformat(timespec="seconds"),
            "authorization_mode": "autonomous_controller" if self.autonomous else "human",
            "goal": self._goal,
            "desired_state": self._desired_state if self.autonomous else None,
            "controller_identity": dict(self._autonomous_identity) if self.autonomous else None,
            "autonomous_intents": list(self._load_autonomous_intents(root)) if self.autonomous else [],
            "phase": phase,
            "blockers": list(blockers),
            "source": {
                "session_id": state.get("session_id"),
                "snapshot_id": state.get("snapshot_id"),
                "frame_id": frame.get("frame_id"),
            },
            "candidate_set_hash": candidate_set.get("candidate_set_hash"),
            "candidates": [
                {key: item.get(key) for key in ("candidate_id", "summary", "action", "risk", "contract_hash")}
                for item in candidate_set.get("items") or []
            ],
            "request": None
            if request is None
            else {
                "request_id": request.request_id,
                "submitted_at": request.submitted_at,
                "elapsed_seconds": round(current - request.started, 3),
                "deadline_seconds": self._deadline_seconds,
                "abandoned": request.abandoned,
            },
            "awaiting_approval": pending if self.autonomous else published or pending,
            "approve_with": (
                f"scripts/approve-command.ps1 -CommandId {(published or pending)['command_id']}"
                if not self.autonomous and (published or pending)
                else None
            ),
            "last_event": self._last_event,
            "last_choice": self._last_choice,
            "recent_outcomes": list(outcomes),
            "llm": dict(self._llm_health),
            "budget": {
                "round_command_limit": self._round_command_limit if self.autonomous else None,
                "reserved_intent_count": reserved_count,
                "round_command_limit_exhausted": (
                    reserved_count >= self._round_command_limit
                    if reserved_count is not None and self._round_command_limit is not None else False
                ),
                "requests_last_hour": len(self._request_starts),
                "max_requests_per_hour": self._max_per_hour,
                "min_interval_seconds": self._min_interval,
                "deadline_seconds": self._deadline_seconds,
                "next_request_in_seconds": max(0.0, round(self._next_request_at - current, 3)),
            },
        }
