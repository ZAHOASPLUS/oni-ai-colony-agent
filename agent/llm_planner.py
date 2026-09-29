from __future__ import annotations

import datetime as dt
import errno
import hashlib
import json
import math
import os
import pathlib
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from agent.rules import AVAILABLE_ACTIONS
from agent.state_quality import canonical_state_hash, field_quality, safe_get


DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4.1-mini"
MAX_PROMPT_TOKENS = 8_000
HARD_PROMPT_TOKEN_LIMIT = 100_000
MIN_CALL_INTERVAL_SECONDS = 60
REQUEST_TIMEOUT_SECONDS = 30
# Optional OPENAI_REQUEST_TIMEOUT_SECONDS in the secrets document (live run
# m1-0930b: the free qwen model needed more than 30 s per answer); bounded so
# a request can never outlive the model loop's 90 s total deadline.
MIN_REQUEST_TIMEOUT_SECONDS = 5.0
MAX_REQUEST_TIMEOUT_SECONDS = 80.0
RANKING_SCHEMA = "oni.llm-ranking.v1"
_ATOMIC_REPLACE_ATTEMPTS = 8
_ATOMIC_REPLACE_INITIAL_DELAY_SECONDS = 0.025
_ATOMIC_REPLACE_MAX_DELAY_SECONDS = 0.2
_RETRYABLE_REPLACE_ERRNOS = {errno.EACCES, errno.EBUSY, errno.EPERM}
_RETRYABLE_REPLACE_WINERRORS = {5, 32, 33}


class ModelRequestError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def local_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).astimezone()


def now_iso() -> str:
    return local_now().isoformat(timespec="seconds")


def generate_plan(
    bridge_dir: pathlib.Path,
    latest: Optional[Dict[str, Any]],
    events: Iterable[Dict[str, Any]],
    advice: List[Dict[str, Any]],
    *,
    enable_llm: bool = False,
    secrets_dir: Optional[pathlib.Path] = None,
    goal: str = "Keep the colony alive with conservative, reversible actions.",
    model_caller: Optional[Callable[[Dict[str, Any], str], Dict[str, Any]]] = None,
    config_override: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    event_list = list(events)[-30:]
    payload = build_prompt_payload(latest, event_list, advice, goal)
    prompt_text = build_prompt_text(payload)
    prompt_bytes = len(prompt_text.encode("utf-8"))
    estimated_input_tokens = estimate_tokens(prompt_text)
    prompt_hash = "sha256:" + hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()
    incident_fingerprint = build_incident_fingerprint(advice)
    source_state = latest if isinstance(latest, dict) else {}

    plan: Dict[str, Any] = {
        "schema": "oni.plan.v2",
        "created_at": now_iso(),
        "mode": "rule_only",
        "prompt_hash": prompt_hash,
        "incident_fingerprint": incident_fingerprint,
        "source": {
            "session_id": compact_scalar(source_state.get("session_id"), 160),
            "save_id_hash": compact_scalar(source_state.get("save_id_hash"), 160),
            "world_id": compact_scalar(source_state.get("world_id"), 160),
            "snapshot_id": compact_scalar(source_state.get("snapshot_id"), 160),
            "state_hash": canonical_state_hash(source_state) if source_state else None,
        },
        "goal": str(goal)[:500],
        "plan": rule_plan(advice),
        "llm": {
            "enabled": bool(enable_llm),
            "called": False,
            "status": "disabled" if not enable_llm else "checking",
            "input_bytes": prompt_bytes,
            "estimated_input_tokens": estimated_input_tokens,
            "input_token_budget": MAX_PROMPT_TOKENS,
        },
    }

    if not enable_llm:
        write_planner_outputs(bridge_dir, plan)
        return plan

    config = resolve_llm_config(secrets_dir, config_override)
    plan["llm"].update(config_health(config))

    if not advice:
        plan["llm"]["status"] = "no_open_incidents"
        write_planner_outputs(bridge_dir, plan)
        return plan

    if config.get("config_error"):
        plan["llm"]["status"] = str(config["config_error"])
        write_planner_outputs(bridge_dir, plan)
        return plan

    if not config.get("api_key"):
        plan["llm"]["status"] = "missing_api_key"
        write_planner_outputs(bridge_dir, plan)
        return plan

    if estimated_input_tokens > HARD_PROMPT_TOKEN_LIMIT:
        plan["llm"]["status"] = "hard_prompt_limit_exceeded"
        write_planner_outputs(bridge_dir, plan)
        return plan

    if estimated_input_tokens > MAX_PROMPT_TOKENS:
        plan["llm"]["status"] = "prompt_budget_exceeded"
        write_planner_outputs(bridge_dir, plan)
        return plan

    runtime_state = load_runtime_state(bridge_dir)
    if runtime_state.get("last_incident_fingerprint") == incident_fingerprint:
        plan["llm"]["status"] = "unchanged_incidents"
        write_planner_outputs(bridge_dir, plan)
        return plan

    last_called_at = parse_timestamp(runtime_state.get("last_called_at"))
    current_time = local_now()
    if last_called_at is not None:
        elapsed = max(0.0, (current_time - last_called_at).total_seconds())
        if elapsed < MIN_CALL_INTERVAL_SECONDS:
            plan["llm"]["status"] = "rate_limited"
            plan["llm"]["retry_after_seconds"] = int(math.ceil(MIN_CALL_INTERVAL_SECONDS - elapsed))
            write_planner_outputs(bridge_dir, plan)
            return plan

    caller = model_caller or call_model
    runtime_state = {
        "schema": "oni.llm-runtime-state.v1",
        "last_called_at": current_time.isoformat(timespec="seconds"),
        "last_incident_fingerprint": incident_fingerprint,
        "last_prompt_hash": prompt_hash,
        "last_status": "request_started",
    }
    write_runtime_state(bridge_dir, runtime_state)
    try:
        response = caller(config, prompt_text)
    except Exception as exc:
        status = classify_request_failure(exc)
        plan["llm"].update({"called": True, "status": status})
        runtime_state["last_status"] = status
        write_runtime_state(bridge_dir, runtime_state)
        write_planner_outputs(bridge_dir, plan)
        return plan

    content = extract_message_content(response)
    # Keep a tamper-evident, credential-free record of the model response.  The
    # response itself is never persisted because it may contain provider data.
    response_hash = "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
    parsed = parse_json_content(content)
    ranked_ids, validation_error = validate_ranking_output(parsed, advice)
    usage = normalize_usage(response.get("usage"))
    plan["llm"].update(
        {
            "called": True,
            "status": "ok" if validation_error is None else validation_error,
            "usage": usage,
            "cost_estimate_usd": estimate_cost_usd(usage, config),
            "response_hash": response_hash,
        }
    )
    if validation_error is None and ranked_ids is not None:
        plan["mode"] = "llm_ranked"
        plan["plan"] = ranked_rule_plan(advice, ranked_ids)

    runtime_state["last_status"] = plan["llm"]["status"]
    write_runtime_state(bridge_dir, runtime_state)
    write_planner_outputs(bridge_dir, plan)
    return plan


def colony_plan_definition_from_plan(
    plan: Dict[str, Any],
    advice: Iterable[Dict[str, Any]],
    *,
    title: str = "LLM ranked colony response",
    goal: Optional[str] = None,
) -> Dict[str, Any]:
    """Project a validated planner result into the strict Colony Plan schema.

    The model contributes ordering only.  Every leaf is copied from the local
    advice command, including its catalogued action, arguments, preconditions,
    and expected effects.  Missing or malformed contracts fail closed.
    """
    if not isinstance(plan, dict) or plan.get("schema") != "oni.plan.v2":
        raise ValueError("a validated oni.plan.v2 result is required")
    mode = str(plan.get("mode") or "rule_only")
    llm = plan.get("llm") if isinstance(plan.get("llm"), dict) else {}
    if mode == "llm_ranked" and llm.get("status") != "ok":
        raise ValueError("llm_ranked projection requires a successful model ranking")
    if mode not in {"rule_only", "llm_ranked"}:
        raise ValueError("unsupported planner mode")
    by_id = {
        str(item.get("incident_id")): item
        for item in advice
        if isinstance(item, dict) and item.get("incident_id")
    }
    leaves = plan.get("plan") if isinstance(plan.get("plan"), list) else []
    if not leaves:
        raise ValueError("planner result contains no trusted leaves")
    steps: List[Dict[str, Any]] = []
    for index, leaf in enumerate(leaves, start=1):
        if not isinstance(leaf, dict):
            raise ValueError("planner leaf is malformed")
        incident_id = str(leaf.get("incident_id") or "")
        item = by_id.get(incident_id)
        command = item.get("command") if isinstance(item, dict) and isinstance(item.get("command"), dict) else {}
        required = ("action", "args", "preconditions", "expected_effects")
        if item is None or any(key not in command for key in required):
            raise ValueError("planner leaf is not backed by a complete local command contract")
        steps.append(
            {
                "id": f"incident-{index}",
                "action": command["action"],
                "args": command["args"],
                "preconditions": command["preconditions"],
                "expected_effects": command["expected_effects"],
                "dependencies": [f"incident-{index - 1}"] if index > 1 else [],
                "reason": str(item.get("reason") or "")[:500],
            }
        )
    source = plan.get("source") if isinstance(plan.get("source"), dict) else {}
    source_provenance = {
        key: source.get(key)
        for key in ("session_id", "save_id_hash", "world_id", "snapshot_id", "state_hash")
        if source.get(key) is not None
    }
    provenance = {
        "schema": "oni.llm-decision-provenance.v1",
        "mode": mode,
        "prompt_hash": plan.get("prompt_hash"),
        "incident_fingerprint": plan.get("incident_fingerprint"),
        "response_hash": llm.get("response_hash"),
        "model": llm.get("model"),
        "status": llm.get("status"),
        "source": source_provenance,
    }
    return {
        "schema": "oni.colony-plan-definition.v1",
        "title": str(title)[:160],
        "goal": str(goal or plan.get("goal") or "")[:1000],
        "steps": steps,
        "decision_provenance": provenance,
    }


def build_prompt_payload(
    latest: Optional[Dict[str, Any]],
    events: List[Dict[str, Any]],
    advice: List[Dict[str, Any]],
    goal: str,
) -> Dict[str, Any]:
    return {
        "schema": "oni.llm-ranker-input.v1",
        "goal": str(goal).replace("\r", " ").replace("\n", " ")[:500],
        "state_summary": compact_state(latest),
        "recent_events": [compact_event(event) for event in events[-30:]],
        "open_incidents": [compact_advice(item) for item in advice[:10]],
        "available_actions": [
            {
                "action": item.get("action"),
                "risk": item.get("risk"),
                "description": str(item.get("description") or "")[:200],
            }
            for item in AVAILABLE_ACTIONS
        ],
        "constraints": [
            "Return only the ranking schema and existing incident ids.",
            "Do not add actions, arguments, commands, incidents, or state claims.",
            "The local rule plan remains authoritative and requires human approval.",
        ],
    }


def compact_state(latest: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    state = latest if isinstance(latest, dict) else {}
    paths = (
        "game.paused",
        "cycle",
        "duplicants.total",
        "duplicants.stressed",
        "duplicants.idle",
        "resources.food_kcal",
        "resources.oxygen_kg_estimate",
        "resources.water_kg_estimate",
        "alerts",
        "research.current",
        "buildings.oxygen_diffuser",
        "buildings.outhouse",
        "buildings.manual_generator",
    )
    fields: Dict[str, Any] = {}
    for path in paths:
        value = safe_get(state, path)
        fields[path] = {
            "value": compact_value(value, depth=0),
            "quality": str(field_quality(state, path).get("status") or "unsupported"),
        }
    return {
        "session_id": compact_scalar(state.get("session_id"), 120),
        "snapshot_id": compact_scalar(state.get("snapshot_id"), 160),
        "seq": state.get("seq"),
        "snapshot_age_seconds": safe_get(state, "bridge.snapshot_age_seconds"),
        "stale": bool(safe_get(state, "bridge.stale", False)),
        "fields": fields,
    }


def compact_event(event: Dict[str, Any]) -> Dict[str, Any]:
    allowed = ("ts", "event", "schema", "session_id", "seq", "stage", "status", "action", "id", "incident_id")
    return {
        key: compact_scalar(event.get(key), 200)
        for key in allowed
        if event.get(key) is not None
    }


def compact_advice(item: Dict[str, Any]) -> Dict[str, Any]:
    command = item.get("command") if isinstance(item.get("command"), dict) else {}
    return {
        "incident_id": compact_scalar(item.get("incident_id"), 160),
        "title": compact_scalar(item.get("title"), 200),
        "risk": compact_scalar(item.get("risk"), 30),
        "reason": compact_scalar(item.get("reason"), 500),
        "evidence": compact_value(item.get("evidence"), depth=0),
        "allowed_action": compact_scalar(command.get("action"), 80),
    }


def compact_scalar(value: Any, limit: int) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = str(value).replace("\r", " ").replace("\n", " ")
    return text[:limit]


def compact_value(value: Any, *, depth: int) -> Any:
    if depth >= 3:
        return "[truncated]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return compact_scalar(value, 300)
    if isinstance(value, list):
        return [compact_value(item, depth=depth + 1) for item in value[:10]]
    if isinstance(value, dict):
        result: Dict[str, Any] = {}
        for key in sorted(value)[:20]:
            result[str(key)[:100]] = compact_value(value[key], depth=depth + 1)
        return result
    return compact_scalar(value, 200)


def build_prompt_text(payload: Dict[str, Any]) -> str:
    return (
        "You rank already validated Oxygen Not Included rule incidents. "
        "Return JSON only with schema oni.llm-ranking.v1 and ranked_incident_ids. "
        "Every id must already exist in open_incidents. Do not output actions, arguments, commands, or new advice.\n\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    )


def build_incident_fingerprint(advice: List[Dict[str, Any]]) -> str:
    compact = [compact_advice(item) for item in advice[:10]]
    encoded = json.dumps(compact, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def estimate_tokens(text: str) -> int:
    return int(math.ceil(len(text.encode("utf-8")) / 4.0))


def rule_plan(advice: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [trusted_plan_item(item, index) for index, item in enumerate(advice[:8], start=1)]


def ranked_rule_plan(advice: List[Dict[str, Any]], ranked_ids: List[str]) -> List[Dict[str, Any]]:
    by_id = {str(item.get("incident_id")): item for item in advice if item.get("incident_id")}
    ordered: List[Dict[str, Any]] = []
    seen = set()
    for incident_id in ranked_ids:
        if incident_id in by_id and incident_id not in seen:
            ordered.append(by_id[incident_id])
            seen.add(incident_id)
    for item in advice:
        incident_id = str(item.get("incident_id") or "")
        if incident_id and incident_id not in seen:
            ordered.append(item)
            seen.add(incident_id)
    return [trusted_plan_item(item, index) for index, item in enumerate(ordered[:8], start=1)]


def trusted_plan_item(item: Dict[str, Any], priority: int) -> Dict[str, Any]:
    command = item.get("command") if isinstance(item.get("command"), dict) else {}
    action = command.get("action")
    allowed = {str(entry.get("action")) for entry in AVAILABLE_ACTIONS}
    return {
        "priority": priority,
        "incident_id": item.get("incident_id"),
        "title": item.get("title"),
        "risk": item.get("risk"),
        "rationale": item.get("reason"),
        "evidence": item.get("evidence"),
        "allowed_action": action if action in allowed else None,
    }


def validate_ranking_output(
    parsed: Optional[Any], advice: List[Dict[str, Any]]
) -> Tuple[Optional[List[str]], Optional[str]]:
    if not isinstance(parsed, dict):
        return None, "invalid_json"
    if set(parsed) != {"schema", "ranked_incident_ids"}:
        return None, "invalid_output_schema"
    if parsed.get("schema") != RANKING_SCHEMA:
        return None, "invalid_output_schema"
    ranked = parsed.get("ranked_incident_ids")
    if not isinstance(ranked, list) or not ranked or not all(isinstance(value, str) for value in ranked):
        return None, "invalid_output_schema"
    if len(ranked) != len(set(ranked)) or len(ranked) > len(advice):
        return None, "invalid_output_schema"

    current_ids = {str(item.get("incident_id")) for item in advice if item.get("incident_id")}
    if any(incident_id not in current_ids for incident_id in ranked):
        return None, "invalid_incident_reference"

    allowed_actions = {str(entry.get("action")) for entry in AVAILABLE_ACTIONS}
    for item in advice:
        command = item.get("command") if isinstance(item.get("command"), dict) else {}
        action = command.get("action")
        if action is not None and action not in allowed_actions:
            return None, "invalid_rule_action"
    return list(ranked), None


_SECRET_KEY_NAMES = frozenset({"OPENAI_API_KEY", "ONI_AI_API_KEY", "API_KEY", "KEY"})
_SECRET_FIELD_NAMES = {
    "base_url": frozenset({"OPENAI_BASE_URL", "ONI_AI_BASE_URL", "BASE_URL", "URL"}),
    "model": frozenset({"OPENAI_MODEL", "ONI_AI_MODEL", "MODEL"}),
    "wire_api": frozenset({"OPENAI_WIRE_API", "ONI_AI_WIRE_API", "WIRE_API"}),
    "input_cost_per_million": frozenset({"ONI_AI_INPUT_COST_PER_1M", "INPUT_COST_PER_MILLION"}),
    "output_cost_per_million": frozenset({"ONI_AI_OUTPUT_COST_PER_1M", "OUTPUT_COST_PER_MILLION"}),
}
_LOCAL_HTTP_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def load_llm_config(secrets_dir: Optional[pathlib.Path]) -> Dict[str, Any]:
    """Load one self-consistent model configuration.

    The API key and the endpoint always come from the same source, so a key
    can never be sent to a URL found in an unrelated file: the environment
    wins when it holds a key; otherwise exactly one secrets file may provide
    a key, together with its own endpoint/model.  Only explicit ``NAME=value``
    lines or JSON fields are read; bare URL or token-looking lines are not
    guessed (code review 2026-09-26, M6).
    """

    config: Dict[str, Any] = {
        "input_cost_per_million": os.environ.get("ONI_AI_INPUT_COST_PER_1M"),
        "output_cost_per_million": os.environ.get("ONI_AI_OUTPUT_COST_PER_1M"),
    }
    environment_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("ONI_AI_API_KEY")
    if environment_key:
        config.update(
            {
                "api_key": environment_key,
                "base_url": os.environ.get("OPENAI_BASE_URL") or os.environ.get("ONI_AI_BASE_URL"),
                "model": os.environ.get("OPENAI_MODEL") or os.environ.get("ONI_AI_MODEL"),
                "wire_api": os.environ.get("OPENAI_WIRE_API") or os.environ.get("ONI_AI_WIRE_API"),
                "config_source": "environment",
            }
        )
    elif secrets_dir and secrets_dir.exists():
        candidates = []
        for path in sorted(secrets_dir.glob("*")):
            if path.is_file() and path.suffix.lower() in {".md", ".txt", ".env", ".json"}:
                values = read_secret_file(path)
                if values.get("api_key"):
                    candidates.append(values)
        if len(candidates) > 1:
            config["config_error"] = "secrets_ambiguous"
        elif candidates:
            values = candidates[0]
            config.update({key: value for key, value in values.items() if value})
            config["config_source"] = "secrets_dir"
    config["base_url"] = str(config.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
    config["model"] = config.get("model") or DEFAULT_MODEL
    if config.get("config_error"):
        config["api_key"] = None
    return config


def resolve_llm_config(
    secrets_dir: Optional[pathlib.Path],
    config_override: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Load (or copy an override of) one configuration and refuse unsafe ones."""

    config = dict(config_override) if config_override is not None else load_llm_config(secrets_dir)
    config.setdefault("base_url", DEFAULT_BASE_URL)
    config.setdefault("model", DEFAULT_MODEL)
    if not config.get("config_error"):
        config["config_error"] = base_url_error(str(config["base_url"]))
    if config.get("config_error"):
        config["api_key"] = None  # never send a key under an unsafe config
    return config


def read_secret_file(path: pathlib.Path) -> Dict[str, Optional[str]]:
    """Read explicit configuration fields from one secrets file."""

    values: Dict[str, Optional[str]] = {}
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    if path.suffix.lower() == ".json":
        try:
            document = json.loads(text)
        except json.JSONDecodeError:
            document = None
        if isinstance(document, dict):
            values["api_key"] = first_present(document, "OPENAI_API_KEY", "api_key", "key")
            values["base_url"] = first_present(document, "OPENAI_BASE_URL", "base_url", "url")
            values["model"] = first_present(document, "OPENAI_MODEL", "model")
            values["wire_api"] = first_present(document, "OPENAI_WIRE_API", "wire_api")
            values["request_timeout_seconds"] = first_present(
                document, "OPENAI_REQUEST_TIMEOUT_SECONDS", "request_timeout_seconds"
            )
            values["input_cost_per_million"] = first_present(
                document, "ONI_AI_INPUT_COST_PER_1M", "input_cost_per_million"
            )
            values["output_cost_per_million"] = first_present(
                document, "ONI_AI_OUTPUT_COST_PER_1M", "output_cost_per_million"
            )
            return values

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = re.match(r"\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?\s*[:=]\s*(.+?)\s*$", line)
        if not match:
            continue
        name = match.group(1).upper()
        value = match.group(2).strip().strip("'\"`")
        if name in _SECRET_KEY_NAMES:
            values.setdefault("api_key", value)
            continue
        for field, names in _SECRET_FIELD_NAMES.items():
            if name in names:
                values.setdefault(field, value)
    return values


def base_url_error(base_url: str) -> Optional[str]:
    """Refuse endpoints that would send the key in clear text."""

    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme == "https" and parsed.hostname:
        return None
    if parsed.scheme == "http" and parsed.hostname in _LOCAL_HTTP_HOSTS:
        return None
    return "base_url_insecure"


def first_present(mapping: Dict[str, Any], *keys: str) -> Optional[str]:
    for key in keys:
        value = mapping.get(key)
        if value is not None and value != "":
            return str(value)
    return None


def effective_request_timeout(config: Any) -> float:
    """Per-request timeout: the optional bounded secrets value, else the default."""

    raw = config.get("request_timeout_seconds") if isinstance(config, dict) else None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return float(REQUEST_TIMEOUT_SECONDS)
    if not math.isfinite(value):
        return float(REQUEST_TIMEOUT_SECONDS)
    return min(MAX_REQUEST_TIMEOUT_SECONDS, max(MIN_REQUEST_TIMEOUT_SECONDS, value))


def config_health(config: Dict[str, Any]) -> Dict[str, Any]:
    host = urllib.parse.urlsplit(str(config.get("base_url") or DEFAULT_BASE_URL)).hostname
    return {
        "api_key_present": bool(config.get("api_key")),
        # Endpoint host (never the key) for auditing where requests go.
        "base_url_host": str(host or "")[:200],
        "model": str(config.get("model") or DEFAULT_MODEL)[:200],
        "wire_api": "responses" if str(config.get("wire_api") or "").lower().startswith("responses") else "chat_completions",
        "request_timeout_seconds": effective_request_timeout(config),
        "minimum_call_interval_seconds": MIN_CALL_INTERVAL_SECONDS,
        "pricing_configured": parse_rate(config.get("input_cost_per_million")) is not None
        and parse_rate(config.get("output_cost_per_million")) is not None,
    }


def call_model(config: Dict[str, Any], prompt_text: str) -> Dict[str, Any]:
    if str(config.get("wire_api") or "").lower().startswith("responses"):
        return call_responses(config, prompt_text)
    return call_chat_completions(config, prompt_text)


def call_chat_completions(config: Dict[str, Any], prompt_text: str) -> Dict[str, Any]:
    base_url = str(config.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
    request = urllib.request.Request(
        base_url + "/chat/completions",
        data=json.dumps(
            {
                "model": config.get("model") or DEFAULT_MODEL,
                "messages": [
                    {"role": "system", "content": "Return only the strict incident ranking JSON object."},
                    {"role": "user", "content": prompt_text},
                ],
                "temperature": 0,
            }
        ).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + str(config["api_key"])},
        method="POST",
    )
    return execute_request(request)


def call_responses(config: Dict[str, Any], prompt_text: str) -> Dict[str, Any]:
    base_url = str(config.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
    request = urllib.request.Request(
        base_url + "/responses",
        data=json.dumps(
            {
                "model": config.get("model") or DEFAULT_MODEL,
                "input": [
                    {"role": "system", "content": "Return only the strict incident ranking JSON object."},
                    {"role": "user", "content": prompt_text},
                ],
            }
        ).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + str(config["api_key"])},
        method="POST",
    )
    return execute_request(request)


def execute_request(
    request: urllib.request.Request,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    # ``timeout`` bounds each socket operation, not the whole request; callers
    # that need a total deadline must enforce it themselves (review M5).
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            value = json.loads(response.read().decode("utf-8"))
            if not isinstance(value, dict):
                raise ModelRequestError("invalid_response")
            return value
    except urllib.error.HTTPError as exc:
        raise ModelRequestError(f"http_{exc.code}") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise ModelRequestError("timeout") from exc
        raise ModelRequestError("network_error") from exc
    except TimeoutError as exc:
        raise ModelRequestError("timeout") from exc
    except json.JSONDecodeError as exc:
        raise ModelRequestError("invalid_response") from exc


def classify_request_failure(exc: Exception) -> str:
    if isinstance(exc, ModelRequestError):
        return exc.code
    if isinstance(exc, TimeoutError):
        return "timeout"
    text = str(exc).lower()
    if "403" in text:
        return "http_403"
    if "timed out" in text or "timeout" in text:
        return "timeout"
    return "request_failed"


def extract_message_content(response: Dict[str, Any]) -> str:
    output_text = response.get("output_text")
    if isinstance(output_text, str):
        return output_text
    output = response.get("output")
    if isinstance(output, list):
        parts: List[str] = []
        for item in output:
            if not isinstance(item, dict) or not isinstance(item.get("content"), list):
                continue
            for block in item["content"]:
                if not isinstance(block, dict):
                    continue
                text = block.get("text") or block.get("output_text")
                if isinstance(text, str):
                    parts.append(text)
        if parts:
            return "\n".join(parts)
    choices = response.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            return message["content"]
    return ""


def parse_json_content(content: str) -> Optional[Any]:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def normalize_usage(value: Any) -> Dict[str, Optional[int]]:
    usage = value if isinstance(value, dict) else {}
    input_tokens = first_integer(usage, "input_tokens", "prompt_tokens")
    output_tokens = first_integer(usage, "output_tokens", "completion_tokens")
    total_tokens = first_integer(usage, "total_tokens")
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    return {"input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": total_tokens}


def first_integer(mapping: Dict[str, Any], *keys: str) -> Optional[int]:
    for key in keys:
        try:
            value = int(mapping[key])
        except (KeyError, TypeError, ValueError):
            continue
        return max(0, value)
    return None


def parse_rate(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def estimate_cost_usd(usage: Dict[str, Optional[int]], config: Dict[str, Any]) -> Optional[float]:
    input_rate = parse_rate(config.get("input_cost_per_million"))
    output_rate = parse_rate(config.get("output_cost_per_million"))
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    if input_rate is None or output_rate is None or input_tokens is None or output_tokens is None:
        return None
    cost = input_tokens * input_rate / 1_000_000 + output_tokens * output_rate / 1_000_000
    return round(cost, 8)


def parse_timestamp(value: Any) -> Optional[dt.datetime]:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone()


def load_runtime_state(bridge_dir: pathlib.Path) -> Dict[str, Any]:
    path = bridge_dir / "llm-state.json"
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def write_runtime_state(bridge_dir: pathlib.Path, state: Dict[str, Any]) -> None:
    atomic_write_json(bridge_dir / "llm-state.json", state)


def write_planner_outputs(bridge_dir: pathlib.Path, plan: Dict[str, Any]) -> None:
    atomic_write_json(bridge_dir / "plan.json", plan)
    llm = plan.get("llm") if isinstance(plan.get("llm"), dict) else {}
    health = {
        "schema": "oni.llm-health.v1",
        "updated_at": now_iso(),
        "mode": plan.get("mode"),
        **llm,
    }
    atomic_write_json(bridge_dir / "llm-health.json", health)


def write_plan(bridge_dir: pathlib.Path, plan: Dict[str, Any]) -> None:
    write_planner_outputs(bridge_dir, plan)


def atomic_write_json(path: pathlib.Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Optional[pathlib.Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", delete=False, dir=str(path.parent), suffix=".tmp"
        ) as handle:
            temporary = pathlib.Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        _replace_with_retry(temporary, path)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _replace_with_retry(temporary: pathlib.Path, path: pathlib.Path) -> None:
    """Replace ``path`` while tolerating short-lived sharing locks."""

    delay = _ATOMIC_REPLACE_INITIAL_DELAY_SECONDS
    for attempt in range(_ATOMIC_REPLACE_ATTEMPTS):
        try:
            temporary.replace(path)
            return
        except OSError as exc:
            if attempt + 1 >= _ATOMIC_REPLACE_ATTEMPTS or not _is_retryable_replace_error(exc):
                raise
            time.sleep(delay)
            delay = min(delay * 2, _ATOMIC_REPLACE_MAX_DELAY_SECONDS)


def _is_retryable_replace_error(exc: OSError) -> bool:
    return (
        isinstance(exc, PermissionError)
        or exc.errno in _RETRYABLE_REPLACE_ERRNOS
        or getattr(exc, "winerror", None) in _RETRYABLE_REPLACE_WINERRORS
    )
