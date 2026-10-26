"""Minimal model-driven closed loop (code review 2026-09-26, M1).

Each Bridge generation derives at most five catalogued candidate commands from
the trusted snapshot (agent.candidates).  A background request asks the
configured model to return exactly one ``candidate_id``; the Bridge then
publishes that candidate's complete local contract as one pending
``model_selected`` command bound to the committed decision frame of the
generation that publishes it.  The model contributes the choice only: it never
supplies actions, arguments or conditions.

Nothing executes without the existing local human approval path
(scripts/approve-command.ps1), the executor re-checks every precondition
against live game state, and the confirmation, rejection or failure of every
published command is fed back into the next prompt.

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
import pathlib
import threading
import time
import urllib.request
from typing import Any, Callable, Deque, Dict, List, Mapping, Optional, Sequence, Tuple

from agent.action_policy import action_policy_hash, action_policy_is_valid
from agent.candidates import WAIT_CANDIDATE_ID, display_text, generate_candidates, oxygen_balance
from agent.colony_plan import TERMINAL_PLAN_STATUSES, load_active_plan
from agent.command_custody import publish_pending_command, read_bytes_share_delete
from agent.command_queue import (
    COMMAND_DIRS,
    COMMAND_SCHEMA,
    MODEL_SELECTED_MODE,
    command_hint_is_current,
    command_pipeline_readiness,
    now,
    trusted_proposal_state,
)
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
from agent.state_quality import canonical_state_hash, field_status, safe_get


MODEL_LOOP_SCHEMA = "oni.model-loop.v1"
MODEL_LOOP_STATE_SCHEMA = "oni.model-loop-state.v1"
CHOICE_SCHEMA = "oni.llm-choice.v1"
CHOICE_INPUT_SCHEMA = "oni.model-choice-input.v1"
MODEL_CHOICE_SOURCE = "model_choice"
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
            "A chosen action still needs local human approval and is verified against the game.",
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
) -> Dict[str, Any]:
    """Project one chosen candidate into a pending ``model_selected`` command."""

    hint = candidate["hint"]
    state_hash = canonical_state_hash(dict(state))
    policy_hash = action_policy_hash(policy)
    created = now()
    expires = created + dt.timedelta(seconds=max(1, int(policy["command_ttl_seconds"])))
    identity = json.dumps(
        {
            "session_id": state.get("session_id"),
            "source_frame_id": frame.get("frame_id"),
            "state_hash": state_hash,
            "request_id": decision["request_id"],
            "contract_hash": candidate["contract_hash"],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "schema": COMMAND_SCHEMA,
        "id": "cmd-model-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32],
        "mode": MODEL_SELECTED_MODE,
        "status": "pending_human_approval",
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
    ) -> None:
        self._secrets_dir = secrets_dir
        self._goal = goal
        self._model_caller = model_caller
        self._config_override = config_override
        self._deadline_seconds = float(deadline_seconds)
        self._min_interval = float(min_interval_seconds)
        self._max_per_hour = int(max_requests_per_hour)
        self._monotonic = monotonic
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
        self._request_counter = 0

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
        published_history = self._load_published(root)
        outcomes = self._recent_outcomes(root, published_history)
        blockers = self._eligibility_blockers(root, latest, frame, policy, advice, outcomes)
        candidate_set: Dict[str, Any] = {"items": [], "candidate_set_hash": None, "blockers": []}
        if not blockers:
            try:
                candidate_set = generate_candidates(latest, allowed_actions=policy.get("allowed_actions") or [])
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
        payload = build_choice_payload(latest or {}, items, outcomes, self._goal, advice)
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
        ):
            # The game moved on while the model answered; ask again.
            self._decision = None
            self._event("choice_stale", candidate_id=chosen["candidate_id"])
            return None
        command = build_model_command(latest or {}, frame, policy, match, decision)
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
        atomic_write_json(
            root / "model-loop-state.json",
            {"schema": MODEL_LOOP_STATE_SCHEMA, "published": history},
        )
        self._event("published", command_id=command["id"], candidate_id=match["candidate_id"])
        return entry

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
        if published is not None or pending is not None:
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
        return {
            "schema": MODEL_LOOP_SCHEMA,
            "updated_at": now().isoformat(timespec="seconds"),
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
            "awaiting_approval": published or pending,
            "approve_with": (
                f"scripts/approve-command.ps1 -CommandId {(published or pending)['command_id']}"
                if (published or pending)
                else None
            ),
            "last_event": self._last_event,
            "last_choice": self._last_choice,
            "recent_outcomes": list(outcomes),
            "llm": dict(self._llm_health),
            "budget": {
                "requests_last_hour": len(self._request_starts),
                "max_requests_per_hour": self._max_per_hour,
                "min_interval_seconds": self._min_interval,
                "deadline_seconds": self._deadline_seconds,
                "next_request_in_seconds": max(0.0, round(self._next_request_at - current, 3)),
            },
        }
