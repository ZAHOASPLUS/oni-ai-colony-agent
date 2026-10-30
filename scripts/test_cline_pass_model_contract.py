"""Regression: the cline-pass / DeepSeek gateway contract.

The ONI controller now runs on the Cline-pass subscription gateway, which
differs from the plain OpenAI shape in two ways that would silently break the
loop:

* the completion body is wrapped in ``{"data": {...}, "success": true}``, so a
  top-level ``choices`` reader sees nothing;
* failures can arrive inside an HTTP 200 body (``{"error": "model not found"}``),
  so an HTTP-status-only client would treat a configuration fault as success.

These tests pin the adapter: envelope unwrapping, wire-protocol inference, key
aliases, usage/reasoning handling, and error classification.  No network call is
made; the recorded shapes come from reports/model-integration/relay-probe.json
and probe_shapes.py.
"""

from __future__ import annotations

import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import llm_planner  # noqa: E402
from agent.model_loop import structured_output_unsupported  # noqa: E402

# Every name the planner reads, not just the prefixes: COCKPIT_API_KEY in the
# developer environment overrode the secrets file under test and the failing
# assertion printed the real key (M1 round 1 preflight, 2026-10-30).
CONFIG_ENVIRONMENT_NAMES = frozenset(
    llm_planner._KEY_ENVIRONMENT_NAMES
    + llm_planner._BASE_URL_ENVIRONMENT_NAMES
    + llm_planner._MODEL_ENVIRONMENT_NAMES
    + llm_planner._WIRE_API_ENVIRONMENT_NAMES
    + llm_planner._PROVIDER_ENVIRONMENT_NAMES
    + llm_planner._REQUEST_TIMEOUT_ENVIRONMENT_NAMES
)
CLEAN_ENV = {
    name: value
    for name, value in os.environ.items()
    if not name.startswith(("OPENAI_", "ONI_AI_", "CLINE_", "NEWAPI_", "DEEPSEEK_", "COCKPIT_"))
    and name not in CONFIG_ENVIRONMENT_NAMES
}

# A trimmed but shape-faithful Cline-pass answer (probe_shapes.py, 2026-10-25).
CLINE_PASS_BODY = {
    "data": {
        "choices": [
            {
                "finish_reason": "stop",
                "index": 0,
                "message": {
                    "content": '{"schema":"oni.llm-choice.v1","candidate_id":"resume_game","reason":"Resume."}',
                    "reasoning": "The colony is paused, so resuming is the least invasive move.",
                    "reasoning_details": [{"format": "unknown", "type": "reasoning.text", "text": "..."}],
                    "role": "assistant",
                    "provider_metadata": {"gateway": {"generationId": "gen_test"}},
                },
            }
        ],
        "created": 1792884564,
        "generationId": "gen_test",
        "id": "gen_test",
        "model": "vmc/deepseek-v4p1-flash-contributor-fallbacks",
        "object": "chat.completion",
        "usage": {
            "cache_creation_input_tokens": 0,
            "completion_tokens": 116,
            "completion_tokens_details": {"reasoning_tokens": 83},
            "prompt_tokens": 109,
            "total_tokens": 225,
        },
    },
    "success": True,
}


def fake_urlopen(payload: object, *, status: int = 200) -> mock.Mock:
    """Return a urlopen replacement serving one JSON body."""

    class _Response(io.BytesIO):
        def __enter__(self) -> "_Response":
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

    body = json.dumps(payload).encode("utf-8")
    opener = mock.Mock(return_value=_Response(body))
    opener.status = status
    return opener


def http_error(status: int, payload: object) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="https://api.cline.bot/api/v1/chat/completions",
        code=status,
        msg="error",
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(json.dumps(payload).encode("utf-8")),
    )


def model_request() -> urllib.request.Request:
    return urllib.request.Request(
        "https://api.cline.bot/api/v1/chat/completions",
        data=b"{}",
        headers={"Content-Type": "application/json", "Authorization": "Bearer sk-test"},
        method="POST",
    )


class EnvelopeTests(unittest.TestCase):
    def test_wrapped_body_is_unwrapped_and_the_outer_success_is_kept(self):
        response = llm_planner.normalize_model_response(CLINE_PASS_BODY)
        self.assertTrue(response["envelope_unwrapped"])
        self.assertEqual(response["provider_model"], "vmc/deepseek-v4p1-flash-contributor-fallbacks")
        self.assertEqual(response["gateway_generation_id"], "gen_test")
        self.assertIs(response["gateway_success"], True)
        self.assertNotIn("data", response)
        content = llm_planner.extract_message_content(response)
        self.assertEqual(json.loads(content)["candidate_id"], "resume_game")

    def test_usage_is_read_from_the_envelope_and_normalized(self):
        response = llm_planner.normalize_model_response(CLINE_PASS_BODY)
        usage = llm_planner.normalize_usage(llm_planner.response_usage(response))
        self.assertEqual(usage, {"input_tokens": 109, "output_tokens": 116, "total_tokens": 225})

    def test_a_plain_openai_body_is_left_alone(self):
        plain = {"choices": [{"message": {"content": "{}"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 2}}
        response = llm_planner.normalize_model_response(plain)
        self.assertFalse(response["envelope_unwrapped"])
        self.assertEqual(llm_planner.response_usage(response), plain["usage"])

    def test_provider_reasoning_is_never_the_answer(self):
        response = llm_planner.normalize_model_response(CLINE_PASS_BODY)
        content = llm_planner.extract_message_content(response)
        self.assertNotIn("least invasive", content)

    def test_content_block_lists_are_read(self):
        response = {"choices": [{"message": {"content": [{"type": "text", "text": "{\"a\":1}"}]}}]}
        self.assertEqual(llm_planner.extract_message_content(response), '{"a":1}')

    def test_execute_request_unwraps_a_live_style_body(self):
        with mock.patch.object(urllib.request, "urlopen", fake_urlopen(CLINE_PASS_BODY)):
            response = llm_planner.execute_request(model_request())
        self.assertTrue(response["envelope_unwrapped"])
        self.assertEqual(llm_planner.extract_message_content(response).count("candidate_id"), 1)


class ErrorClassificationTests(unittest.TestCase):
    def test_a_200_body_carrying_an_error_is_a_failure(self):
        body = {"error": "model not found", "success": False}
        with mock.patch.object(urllib.request, "urlopen", fake_urlopen(body)):
            with self.assertRaises(llm_planner.ModelRequestError) as caught:
                llm_planner.execute_request(model_request())
        self.assertEqual(caught.exception.code, "gateway_model_not_found")

    def test_http_error_keeps_the_gateway_error_text(self):
        with mock.patch.object(
            urllib.request, "urlopen", mock.Mock(side_effect=http_error(400, {"error": "invalid model format. Expected format: modelType/model"}))
        ):
            with self.assertRaises(llm_planner.ModelRequestError) as caught:
                llm_planner.execute_request(model_request())
        self.assertEqual(caught.exception.code, "http_400_invalid_model_format_expected_format_modeltype_model")

    def test_unknown_model_404_is_reported_as_http_404(self):
        with mock.patch.object(
            urllib.request, "urlopen", mock.Mock(side_effect=http_error(404, {"error": "model not found", "success": False}))
        ):
            with self.assertRaises(llm_planner.ModelRequestError) as caught:
                llm_planner.execute_request(model_request())
        self.assertEqual(caught.exception.code, "http_404_model_not_found")

    def test_a_structured_output_rejection_triggers_one_plain_retry(self):
        self.assertTrue(structured_output_unsupported("http_400"))
        self.assertTrue(structured_output_unsupported("gateway_invalid_request_error"))
        self.assertFalse(structured_output_unsupported("gateway_model_not_found"))
        self.assertFalse(structured_output_unsupported("timeout"))


class WireApiTests(unittest.TestCase):
    def test_the_configured_provider_is_cline_pass_deepseek(self):
        self.assertEqual(llm_planner.DEFAULT_PROVIDER, "cline-pass")
        self.assertEqual(llm_planner.DEFAULT_BASE_URL, "https://api.cline.bot/api/v1")
        self.assertEqual(llm_planner.DEFAULT_MODEL, "cline-pass/deepseek-v4.1-flash")

    def test_the_gateway_never_receives_a_responses_request(self):
        config = llm_planner.resolve_llm_config(None, {"api_key": "sk-x", "base_url": "https://api.cline.bot/api/v1", "model": "cline-pass/deepseek-v4.1-flash"})
        self.assertEqual(llm_planner.effective_wire_api(config), llm_planner.WIRE_API_CHAT_COMPLETIONS)
        self.assertEqual(llm_planner.config_health(config)["wire_api"], "chat_completions")

    def test_openai_hosts_keep_the_responses_api(self):
        self.assertEqual(
            llm_planner.effective_wire_api({"base_url": "https://api.openai.com/v1", "model": "gpt-4.1-mini"}),
            llm_planner.WIRE_API_RESPONSES,
        )

    def test_an_explicit_wire_api_wins(self):
        self.assertEqual(
            llm_planner.effective_wire_api({"wire_api": "responses", "base_url": "https://api.cline.bot/api/v1"}),
            llm_planner.WIRE_API_RESPONSES,
        )

    def test_a_deepseek_model_name_forces_chat_completions_on_an_unknown_host(self):
        self.assertEqual(
            llm_planner.effective_wire_api({"base_url": "https://relay.example/v1", "model": "deepseek-chat"}),
            llm_planner.WIRE_API_CHAT_COMPLETIONS,
        )

    def test_the_provider_preset_fills_the_endpoint_and_model(self):
        config = llm_planner.resolve_llm_config(None, {"api_key": "sk-x", "provider": "cline-pass"})
        self.assertEqual(config["base_url"], "https://api.cline.bot/api/v1")
        self.assertEqual(config["model"], "cline-pass/deepseek-v4.1-flash")
        self.assertEqual(config["provider"], "cline-pass")

    def test_an_explicit_endpoint_beats_the_preset(self):
        config = llm_planner.resolve_llm_config(
            None, {"api_key": "sk-x", "provider": "cline-pass", "base_url": "https://local.example/v1", "model": "custom"}
        )
        self.assertEqual(config["base_url"], "https://local.example/v1")
        self.assertEqual(config["model"], "custom")

    def test_the_config_health_names_the_provider(self):
        health = llm_planner.config_health(llm_planner.resolve_llm_config(None, {"api_key": "sk-x"}))
        self.assertEqual(health["provider"], "cline-pass")
        self.assertEqual(health["base_url_host"], "api.cline.bot")


class KeyAliasTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, CLEAN_ENV, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_cline_pass_key_is_accepted_directly(self):
        os.environ["CLINE_API_KEY"] = "sk_cline_pass_key_000000000000000000"
        config = llm_planner.load_llm_config(None)
        self.assertEqual(config["api_key"], "sk_cline_pass_key_000000000000000000")
        self.assertEqual(config["base_url"], "https://api.cline.bot/api/v1")
        self.assertEqual(config["model"], "cline-pass/deepseek-v4.1-flash")
        self.assertEqual(config["config_source"], "environment")

    def test_openai_aliases_still_win_over_the_cline_key(self):
        os.environ["OPENAI_API_KEY"] = "sk-openai-000000000000000000000000"
        os.environ["CLINE_API_KEY"] = "sk_cline_pass_key_000000000000000000"
        config = llm_planner.load_llm_config(None)
        self.assertEqual(config["api_key"], "sk-openai-000000000000000000000000")

    def test_an_explicit_endpoint_is_never_overridden_by_the_preset(self):
        os.environ["CLINE_API_KEY"] = "sk_cline_pass_key_000000000000000000"
        os.environ["OPENAI_BASE_URL"] = "https://self-hosted.example/v1"
        os.environ["OPENAI_MODEL"] = "deepseek-v4.1-flash"
        config = llm_planner.load_llm_config(None)
        self.assertEqual(config["base_url"], "https://self-hosted.example/v1")
        self.assertEqual(config["model"], "deepseek-v4.1-flash")

    def test_a_secrets_file_may_use_the_cline_key_name(self):
        with tempfile.TemporaryDirectory(prefix="oni-llm-cline-") as temp:
            secrets = pathlib.Path(temp)
            (secrets / "cline.env").write_text(
                "CLINE_API_KEY=sk-from-file-0000000000000000000000\n"
                "OPENAI_MODEL=cline-pass/deepseek-v4-pro\n",
                encoding="utf-8",
            )
            config = llm_planner.load_llm_config(secrets)
        self.assertEqual(config["api_key"], "sk-from-file-0000000000000000000000")
        self.assertEqual(config["model"], "cline-pass/deepseek-v4-pro")
        self.assertEqual(config["config_source"], "secrets_dir")
        self.assertEqual(config["base_url"], "https://api.cline.bot/api/v1")

    def test_a_provider_field_in_a_secrets_file_is_honoured(self):
        with tempfile.TemporaryDirectory(prefix="oni-llm-provider-") as temp:
            secrets = pathlib.Path(temp)
            (secrets / "oni-llm.json").write_text(
                json.dumps({"OPENAI_API_KEY": "sk-json-000000000000000000000000", "provider": "deepseek"}),
                encoding="utf-8",
            )
            config = llm_planner.load_llm_config(secrets)
        self.assertEqual(config["provider"], "deepseek")
        self.assertEqual(config["base_url"], "https://api.deepseek.com/v1")
        self.assertEqual(config["model"], "deepseek-chat")


if __name__ == "__main__":
    unittest.main()
