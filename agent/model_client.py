"""One OpenAI-compatible model endpoint: which one, with which key, and how to call it."""

from __future__ import annotations

import json
import math
import os
import pathlib
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

# --- Model provider catalog -------------------------------------------------
# ONI speaks two OpenAI-compatible wire protocols.  ``responses`` is the native
# OpenAI Responses API; ``chat_completions`` is what every other compatible
# gateway (Cline-pass, DeepSeek's own endpoint, local gateways) serves.
WIRE_API_RESPONSES = "responses"
WIRE_API_CHAT_COMPLETIONS = "chat_completions"
# Default provider for the DeepSeek player: the Cline-pass subscription gateway.
DEFAULT_PROVIDER = "cline-pass"
DEFAULT_BASE_URL = "https://api.cline.bot/api/v1"
DEFAULT_MODEL = "cline-pass/deepseek-v4.1-flash"
# Providers whose endpoint serves only ``chat/completions``; a key must never be
# sent to ``/responses`` there.
CHAT_COMPLETIONS_ONLY_HOSTS = frozenset(
    {
        "api.cline.bot",
        "api.deepseek.com",
        "api.deepseek.ai",
        "openrouter.ai",
        "api.moonshot.cn",
        "api.z.ai",
    }
)
# OpenAI's own hosts keep the Responses API default.
RESPONSES_HOSTS = frozenset({"api.openai.com"})
PROVIDER_PRESETS: Dict[str, Dict[str, Any]] = {
    "cline-pass": {
        "base_url": DEFAULT_BASE_URL,
        "model": DEFAULT_MODEL,
        "wire_api": WIRE_API_CHAT_COMPLETIONS,
        "key_env": ("CLINE_API_KEY", "ONI_AI_API_KEY", "OPENAI_API_KEY"),
        "models": (
            "cline-pass/deepseek-v4.1-flash",
            "cline-pass/deepseek-v4-pro",
        ),
        "hosts": ("api.cline.bot",),
        "structured_output": ("json_schema", "plain"),
        "input_cost_per_million": None,
        "output_cost_per_million": None,
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4.1-mini",
        "wire_api": WIRE_API_RESPONSES,
        "key_env": ("OPENAI_API_KEY", "ONI_AI_API_KEY"),
        "models": ("gpt-4.1-mini",),
        "hosts": ("api.openai.com",),
        "structured_output": ("json_schema", "plain"),
        "input_cost_per_million": None,
        "output_cost_per_million": None,
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "wire_api": WIRE_API_CHAT_COMPLETIONS,
        "key_env": ("DEEPSEEK_API_KEY", "ONI_AI_API_KEY", "OPENAI_API_KEY"),
        "models": ("deepseek-chat", "deepseek-reasoner"),
        "hosts": ("api.deepseek.com",),
        # DeepSeek's own API offers JSON mode (response_format json_object) but
        # not json_schema, so the schema request would only ever cost a 400.
        "structured_output": ("json_object", "plain"),
        "input_cost_per_million": None,
        "output_cost_per_million": None,
    },
    # Any other OpenAI-compatible endpoint: base_url and model must be stated
    # explicitly, and no host check applies.
    "compatible": {
        "base_url": None,
        "model": None,
        "wire_api": WIRE_API_CHAT_COMPLETIONS,
        "key_env": ("ONI_AI_API_KEY", "OPENAI_API_KEY"),
        "models": (),
        "hosts": (),
        "structured_output": ("json_schema", "plain"),
        "input_cost_per_million": None,
        "output_cost_per_million": None,
    },
}
STRUCTURED_OUTPUT_MODES = ("json_schema", "json_object", "plain")
# Gateways that wrap the OpenAI body in ``{"data": {...}, "success": true}``.
# Cline-pass is one; the unwrapping below is provider-agnostic by shape.
ENVELOPE_DATA_KEYS = frozenset({"choices", "output", "output_text", "usage"})
MIN_CALL_INTERVAL_SECONDS = 60
REQUEST_TIMEOUT_SECONDS = 30
# Optional OPENAI_REQUEST_TIMEOUT_SECONDS in the secrets document (live run
# m1-0930b: the free qwen model needed more than 30 s per answer); bounded so
# a request can never outlive the model loop's 90 s total deadline.
MIN_REQUEST_TIMEOUT_SECONDS = 5.0
MAX_REQUEST_TIMEOUT_SECONDS = 80.0


class ModelRequestError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


_SECRET_KEY_NAMES = frozenset(
    {"OPENAI_API_KEY", "ONI_AI_API_KEY", "API_KEY", "KEY", "CLINE_API_KEY", "NEWAPI_API_KEY", "DEEPSEEK_API_KEY"}
)
_SECRET_FIELD_NAMES = {
    "base_url": frozenset({"OPENAI_BASE_URL", "ONI_AI_BASE_URL", "BASE_URL", "URL", "NEWAPI_BASE_URL"}),
    "model": frozenset({"OPENAI_MODEL", "ONI_AI_MODEL", "MODEL"}),
    "wire_api": frozenset({"OPENAI_WIRE_API", "ONI_AI_WIRE_API", "WIRE_API", "API_WIRE"}),
    "provider": frozenset({"ONI_AI_PROVIDER", "PROVIDER"}),
    "input_cost_per_million": frozenset({"ONI_AI_INPUT_COST_PER_1M", "INPUT_COST_PER_MILLION"}),
    "output_cost_per_million": frozenset({"ONI_AI_OUTPUT_COST_PER_1M", "OUTPUT_COST_PER_MILLION"}),
}
# Environment variables that may carry the key, in precedence order.  The
# Cline-pass gateway key is accepted directly so the ONI controller can use the
# same admitted credential as the interactive session.
_KEY_ENVIRONMENT_NAMES = (
    "OPENAI_API_KEY", "ONI_AI_API_KEY", "CLINE_API_KEY", "NEWAPI_API_KEY", "DEEPSEEK_API_KEY",
)
_BASE_URL_ENVIRONMENT_NAMES = ("OPENAI_BASE_URL", "ONI_AI_BASE_URL")
_MODEL_ENVIRONMENT_NAMES = ("OPENAI_MODEL", "ONI_AI_MODEL")
_WIRE_API_ENVIRONMENT_NAMES = ("OPENAI_WIRE_API", "ONI_AI_WIRE_API")
_PROVIDER_ENVIRONMENT_NAMES = ("ONI_AI_PROVIDER",)
# The environment path must be able to set the per-request timeout too.  It could
# only come from a secrets file before, so a deployment configured entirely by
# environment (which is how this run is configured) silently kept the 30 s
# default; DeepSeek answers took up to 42 s and the loop recorded
# ``request_failed status=timeout`` with a 120 s backoff (2026-10-26 round 29).
_REQUEST_TIMEOUT_ENVIRONMENT_NAMES = ("OPENAI_REQUEST_TIMEOUT_SECONDS", "ONI_AI_REQUEST_TIMEOUT_SECONDS")


def first_environment(names: Iterable[str]) -> Optional[str]:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def provider_preset(provider: Any) -> Dict[str, Any]:
    """Return one provider preset (never shared, so callers may mutate it)."""

    name = str(provider or DEFAULT_PROVIDER).strip().lower()
    preset = PROVIDER_PRESETS.get(name)
    if preset is None:
        preset = PROVIDER_PRESETS[DEFAULT_PROVIDER]
    return dict(preset)


def effective_wire_api(config: Any, base_url: Any = None, model: Any = None) -> str:
    """Resolve the wire protocol for one configuration.

    An explicit ``wire_api`` wins.  Otherwise the endpoint host decides: the
    OpenAI hosts keep the Responses API, while known compatible gateways (and
    anything serving DeepSeek models) must use ``chat/completions``.  A gateway
    that has no ``/responses`` route would otherwise answer every choice request
    with an HTTP error.
    """

    explicit = str((config or {}).get("wire_api") or "").strip().lower() if isinstance(config, dict) else str(config or "").strip().lower()
    if explicit in {"responses", "chat_completions", "chat"}:
        return WIRE_API_CHAT_COMPLETIONS if explicit == "chat" else explicit
    resolved_base = base_url
    resolved_model = model
    if isinstance(config, dict):
        resolved_base = resolved_base or config.get("base_url")
        resolved_model = resolved_model or config.get("model")
    host = (urllib.parse.urlsplit(str(resolved_base or DEFAULT_BASE_URL)).hostname or "").lower()
    if host in RESPONSES_HOSTS:
        return WIRE_API_RESPONSES
    if host in CHAT_COMPLETIONS_ONLY_HOSTS:
        return WIRE_API_CHAT_COMPLETIONS
    if "deepseek" in str(resolved_model or "").lower():
        return WIRE_API_CHAT_COMPLETIONS
    if host:
        # Unknown compatible gateway: the backward-compatible protocol is the
        # safe choice, because /responses is OpenAI-specific.
        return WIRE_API_CHAT_COMPLETIONS
    return WIRE_API_RESPONSES


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
    environment_provider = first_environment(_PROVIDER_ENVIRONMENT_NAMES)
    environment_key = first_environment(_KEY_ENVIRONMENT_NAMES)
    explicit_name = str(environment_provider or "").strip().lower()
    if explicit_name in PROVIDER_PRESETS:
        # A named provider reads its own key variable first.  Several keys can
        # coexist on one machine (CLINE_API_KEY next to an OpenAI key); the
        # generic order would hand OPENAI_API_KEY to
        # whichever endpoint is configured.
        environment_key = first_environment(PROVIDER_PRESETS[explicit_name]["key_env"])
    if environment_key:
        config.update(
            {
                "api_key": environment_key,
                "base_url": first_environment(_BASE_URL_ENVIRONMENT_NAMES),
                "model": first_environment(_MODEL_ENVIRONMENT_NAMES),
                "wire_api": first_environment(_WIRE_API_ENVIRONMENT_NAMES),
                "provider": environment_provider,
                "request_timeout_seconds": first_environment(_REQUEST_TIMEOUT_ENVIRONMENT_NAMES),
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
    if environment_provider and not config.get("provider"):
        config["provider"] = environment_provider
    config["provider_explicit"] = bool(config.get("provider"))
    if config.get("api_key") and not config.get("config_error") and not config["provider_explicit"] and not config.get("base_url"):
        # A key with neither a provider nor an endpoint must not be sent to the
        # default gateway: a generic OPENAI_API_KEY or DEEPSEEK_API_KEY belongs
        # to someone else's service.  The operator has to choose one.
        config["config_error"] = "provider_unset"
    apply_provider_preset(config)
    if not config.get("config_error") and (not config.get("base_url") or not config.get("model")):
        if config["provider"] in PROVIDER_PRESETS:
            # A provider whose preset has no endpoint (``compatible``) or no
            # model must never fall back to the default gateway's
            # values with this key.
            config["config_error"] = "endpoint_unset"
    config["base_url"] = str(config.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
    config["model"] = config.get("model") or DEFAULT_MODEL
    if config.get("config_error"):
        config["api_key"] = None
    return config


def apply_provider_preset(config: Dict[str, Any]) -> Dict[str, Any]:
    """Fill unset endpoint/model fields from the provider preset.

    Explicit configuration always wins; the preset only supplies what the
    operator did not state, so a key can never be sent to a host the operator
    did not choose together with the key's own provider.
    """

    preset = provider_preset(config.get("provider"))
    for field in ("base_url", "model", "wire_api"):
        if not config.get(field):
            value = preset.get(field)
            if value:
                config[field] = value
    if config.get("input_cost_per_million") in (None, "") and preset.get("input_cost_per_million") is not None:
        config["input_cost_per_million"] = preset["input_cost_per_million"]
    if config.get("output_cost_per_million") in (None, "") and preset.get("output_cost_per_million") is not None:
        config["output_cost_per_million"] = preset["output_cost_per_million"]
    config["provider"] = str(config.get("provider") or DEFAULT_PROVIDER).strip().lower()
    return config


def structured_output_modes(config: Mapping[str, Any]) -> Tuple[str, ...]:
    """The structured-output ladder to try for one configuration, best first."""

    modes = tuple(provider_preset(config.get("provider")).get("structured_output") or ())
    modes = tuple(mode for mode in modes if mode in STRUCTURED_OUTPUT_MODES)
    if not modes or modes[-1] != "plain":
        modes = tuple(mode for mode in modes if mode != "plain") + ("plain",)
    return modes


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
            values["provider"] = first_present(document, "ONI_AI_PROVIDER", "provider")
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


_LOCAL_HTTP_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def base_url_error(base_url: str) -> Optional[str]:
    """Refuse endpoints that would send the key in clear text."""

    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme == "https" and parsed.hostname:
        return None
    if parsed.scheme == "http" and parsed.hostname in _LOCAL_HTTP_HOSTS:
        return None
    return "base_url_insecure"


def config_health(config: Dict[str, Any]) -> Dict[str, Any]:
    host = urllib.parse.urlsplit(str(config.get("base_url") or DEFAULT_BASE_URL)).hostname
    return {
        "api_key_present": bool(config.get("api_key")),
        # Endpoint host (never the key) for auditing where requests go.
        "base_url_host": str(host or "")[:200],
        "provider": str(config.get("provider") or DEFAULT_PROVIDER)[:80],
        "model": str(config.get("model") or DEFAULT_MODEL)[:200],
        "wire_api": effective_wire_api(config),
        "request_timeout_seconds": effective_request_timeout(config),
        "minimum_call_interval_seconds": MIN_CALL_INTERVAL_SECONDS,
        "pricing_configured": parse_rate(config.get("input_cost_per_million")) is not None
        and parse_rate(config.get("output_cost_per_million")) is not None,
        # Why no request is sent (provider_endpoint_mismatch, endpoint_unset, ...).
        "config_error": config.get("config_error"),
        "structured_output_modes": list(structured_output_modes(config)),
    }


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
            value = normalize_model_response(value)
            failure = response_error_code(value)
            if failure:
                # Some compatible gateways report a failure inside an HTTP 200
                # body (Cline-pass: ``{"error": "model not found"}``).
                raise ModelRequestError(failure)
            return value
    except urllib.error.HTTPError as exc:
        raise ModelRequestError(describe_http_error(exc)) from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise ModelRequestError("timeout") from exc
        raise ModelRequestError("network_error") from exc
    except TimeoutError as exc:
        raise ModelRequestError("timeout") from exc
    except json.JSONDecodeError as exc:
        raise ModelRequestError("invalid_response") from exc


def describe_http_error(exc: urllib.error.HTTPError) -> str:
    """Classify one HTTP failure, keeping the gateway's own error code.

    Cline-pass answers an unknown model with ``404 {"error": "model not found"}``
    and a malformed model id with ``400 {"error": "invalid model format..."}``;
    both are configuration faults, not transient transport faults, and the
    model loop reports the resulting code verbatim.
    """

    code = f"http_{exc.code}"
    detail = ""
    try:
        body = exc.read().decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - the status code alone is still useful
        body = ""
    try:
        document = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        document = None
    if isinstance(document, dict):
        raw = document.get("error") if "error" in document else document.get("message")
        if isinstance(raw, dict):
            raw = raw.get("code") or raw.get("message") or raw.get("type")
        detail = str(raw or "").strip().lower()
    if detail:
        detail = re.sub(r"[^a-z0-9]+", "_", detail)[:60].strip("_")
        return f"{code}_{detail}"
    return code


def response_error_code(response: Dict[str, Any]) -> Optional[str]:
    """Return a failure code when a 200 body actually carries an error."""

    success = response.get("success")
    error = response.get("error")
    if isinstance(error, dict):
        error = error.get("code") or error.get("message") or error.get("type")
    text = str(error or "").strip()
    if success is False and not text:
        text = "gateway_rejected"
    if not text:
        return None
    if str(success).lower() in {"true"} and not text:
        return None
    return "gateway_" + re.sub(r"[^a-z0-9]+", "_", text.lower())[:60].strip("_")


def unwrap_model_envelope(response: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
    """Unwrap a gateway's ``{"data": {...}}`` completion envelope.

    Cline-pass returns the OpenAI body nested one level deep
    (``{"data": {"choices": [...], "usage": {...}}, "success": true}``), so the
    unwrapped inner document is what every downstream parser expects.  A body
    that already carries completion fields is returned unchanged.
    """

    if ENVELOPE_DATA_KEYS & set(response):
        return response, False
    data = response.get("data")
    if isinstance(data, dict) and ENVELOPE_DATA_KEYS & set(data):
        return data, True
    return response, False


def normalize_model_response(response: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize one model response and record the envelope decision.

    The returned document is the unwrapped completion body plus the two
    observational fields ``envelope_unwrapped`` and ``provider_model``.  The
    provider model is what the gateway reports it actually served, which can
    differ from the requested alias.
    """

    inner, unwrapped = unwrap_model_envelope(response)
    normalized = dict(inner)
    normalized["envelope_unwrapped"] = unwrapped
    if unwrapped:
        normalized.setdefault("provider_model", str(inner.get("model") or ""))
        normalized.setdefault("gateway_generation_id", str(inner.get("generationId") or ""))
        normalized.setdefault("gateway_success", response.get("success"))
        if isinstance(inner.get("usage"), dict):
            normalized["usage"] = inner["usage"]
    return normalized


def parse_rate(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None
