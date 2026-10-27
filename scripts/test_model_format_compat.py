"""Cross-provider format compatibility for the model choice path.

Test runs drive the controller with ChatGPT (OpenAI) and the final validation
with DeepSeek, so the same code must read and write each provider's format:

* OpenAI Responses API: ``output`` items (a ``reasoning`` item before the
  ``message``), ``refusal`` blocks, ``status: incomplete``, usage with
  ``*_tokens_details``;
* OpenAI chat completions: ``message.refusal``, ``finish_reason``;
* DeepSeek: ``reasoning_content`` beside the answer, ``prompt_cache_hit_tokens``,
  JSON mode only (``response_format: json_object``), and deployments that
  inline ``<think>`` in the content;
* request bodies for both wire APIs and all structured-output modes;
* key/endpoint safety when several providers' keys coexist on one machine.

The bodies are hand-written from each provider's documented shape; none is a
recorded live response.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import time
import types
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import llm_planner as L  # noqa: E402
from agent import model_loop as M  # noqa: E402

ANSWER = {"schema": M.CHOICE_SCHEMA, "candidate_id": "wait", "reason": "nothing to do"}
ANSWER_TEXT = json.dumps(ANSWER)

CONFIG_NAMES = frozenset(
    L._KEY_ENVIRONMENT_NAMES + L._BASE_URL_ENVIRONMENT_NAMES + L._MODEL_ENVIRONMENT_NAMES
    + L._WIRE_API_ENVIRONMENT_NAMES + L._PROVIDER_ENVIRONMENT_NAMES + L._REQUEST_TIMEOUT_ENVIRONMENT_NAMES
)
CLEAN_ENV = {
    name: value for name, value in os.environ.items()
    if name not in CONFIG_NAMES and not name.startswith(("OPENAI_", "ONI_AI_"))
}

OPENAI_RESPONSES = {
    "object": "response",
    "status": "completed",
    "output": [
        {"type": "reasoning", "id": "rs_1", "summary": []},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": ANSWER_TEXT}]},
    ],
    "usage": {
        "input_tokens": 1200, "output_tokens": 300, "total_tokens": 1500,
        "input_tokens_details": {"cached_tokens": 1024},
        "output_tokens_details": {"reasoning_tokens": 256},
    },
}
OPENAI_RESPONSES_REFUSAL = {
    "status": "completed",
    "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "I can't help with that."}]}],
}
OPENAI_RESPONSES_INCOMPLETE = {
    "status": "incomplete",
    "incomplete_details": {"reason": "max_output_tokens"},
    "output": [{"type": "message", "content": [{"type": "output_text", "text": '{"schema": "oni.mod'}]}],
}
OPENAI_CHAT = {
    "choices": [{"index": 0, "finish_reason": "stop",
                 "message": {"role": "assistant", "content": ANSWER_TEXT, "refusal": None}}],
    "usage": {"prompt_tokens": 900, "completion_tokens": 80, "total_tokens": 980,
              "prompt_tokens_details": {"cached_tokens": 512},
              "completion_tokens_details": {"reasoning_tokens": 64}},
}
DEEPSEEK_CHAT = {
    "model": "deepseek-reasoner",
    "choices": [{"index": 0, "finish_reason": "stop",
                 "message": {"role": "assistant", "reasoning_content": '{"candidate_id": "not_this"}',
                             "content": ANSWER_TEXT}}],
    "usage": {"prompt_tokens": 800, "completion_tokens": 120, "total_tokens": 920,
              "prompt_cache_hit_tokens": 640, "prompt_cache_miss_tokens": 160,
              "completion_tokens_details": {"reasoning_tokens": 90}},
}


def chat(content=None, *, finish="stop", refusal=None):
    return {"choices": [{"finish_reason": finish, "message": {"content": content, "refusal": refusal}}]}


class ResponseReadingTests(unittest.TestCase):
    def answer(self, response):
        return M.validate_choice_output(L.parse_json_content(L.extract_message_content(response)), ["wait"])

    def test_every_provider_shape_yields_the_answer(self):
        for name, body in (("openai_responses", OPENAI_RESPONSES), ("openai_chat", OPENAI_CHAT),
                           ("deepseek_chat", DEEPSEEK_CHAT)):
            with self.subTest(name):
                choice, error = self.answer(body)
                self.assertIsNone(error)
                self.assertEqual(choice["candidate_id"], "wait")

    def test_usage_is_normalized_and_details_are_kept_apart(self):
        expected = {
            "openai_responses": ({"input_tokens": 1200, "output_tokens": 300, "total_tokens": 1500},
                                 {"reasoning_tokens": 256, "cached_input_tokens": 1024}),
            "openai_chat": ({"input_tokens": 900, "output_tokens": 80, "total_tokens": 980},
                            {"reasoning_tokens": 64, "cached_input_tokens": 512}),
            "deepseek_chat": ({"input_tokens": 800, "output_tokens": 120, "total_tokens": 920},
                              {"reasoning_tokens": 90, "cached_input_tokens": 640}),
        }
        bodies = {"openai_responses": OPENAI_RESPONSES, "openai_chat": OPENAI_CHAT, "deepseek_chat": DEEPSEEK_CHAT}
        for name, (usage, details) in expected.items():
            with self.subTest(name):
                self.assertEqual(L.normalize_usage(L.response_usage(bodies[name])), usage)
                self.assertEqual(L.usage_details(L.response_usage(bodies[name])), details)

    def test_missing_usage_details_are_null(self):
        self.assertEqual(L.usage_details({"prompt_tokens": 1}), {"reasoning_tokens": None, "cached_input_tokens": None})

    def test_failure_reasons(self):
        cases = {
            "responses_refusal": (OPENAI_RESPONSES_REFUSAL, "model_refused"),
            "responses_incomplete": (OPENAI_RESPONSES_INCOMPLETE, "response_incomplete"),
            "chat_refusal": (chat(None, refusal="I can't help with that."), "model_refused"),
            "chat_length": (chat('{"schema":', finish="length"), "response_incomplete"),
            "chat_filter": (chat("", finish="content_filter"), "content_filtered"),
            "ok_responses": (OPENAI_RESPONSES, None),
            "ok_chat": (OPENAI_CHAT, None),
            "ok_deepseek": (DEEPSEEK_CHAT, None),
        }
        for name, (body, reason) in cases.items():
            with self.subTest(name):
                self.assertEqual(L.response_failure_reason(body), reason)

    def test_inline_reasoning_and_prose_are_stripped(self):
        cases = {
            "think": "<think>The answer is {not json}</think>\n" + ANSWER_TEXT,
            "fence_after_prose": "Here is my choice:\n```json\n" + ANSWER_TEXT + "\n```",
            "prose_around": "I pick " + ANSWER_TEXT + " because it is safe.",
        }
        for name, content in cases.items():
            with self.subTest(name):
                self.assertEqual(L.parse_json_content(content), ANSWER)

    def test_non_objects_from_extraction_are_rejected(self):
        self.assertIsNone(L.parse_json_content("no json here"))
        self.assertIsNone(L.parse_json_content("value: [1, 2]"))


class RequestShapeTests(unittest.TestCase):
    def body(self, config, response_schema):
        captured = {}

        def fake_execute(request, timeout):
            captured["url"] = request.full_url
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return {"choices": []}

        with mock.patch.object(M, "execute_request", side_effect=fake_execute):
            M.call_choice_model(config, "prompt", response_schema, timeout=5)
        return captured

    def test_wire_and_mode_matrix(self):
        schema = M.choice_response_schema(["wait"])
        openai = {"provider": "openai", "base_url": "https://api.openai.com/v1", "model": "m", "api_key": "k"}
        deepseek = {"provider": "deepseek", "base_url": "https://api.deepseek.com/v1", "model": "m", "api_key": "k"}
        responses_schema = self.body(openai, schema)
        self.assertTrue(responses_schema["url"].endswith("/responses"))
        self.assertEqual(responses_schema["body"]["text"]["format"]["type"], "json_schema")
        self.assertTrue(responses_schema["body"]["text"]["format"]["strict"])
        self.assertEqual(self.body(openai, M.JSON_OBJECT_FORMAT)["body"]["text"], {"format": {"type": "json_object"}})
        self.assertNotIn("text", self.body(openai, None)["body"])
        chat_object = self.body(deepseek, M.JSON_OBJECT_FORMAT)
        self.assertTrue(chat_object["url"].endswith("/chat/completions"))
        self.assertEqual(chat_object["body"]["response_format"], {"type": "json_object"})
        self.assertEqual(self.body(deepseek, schema)["body"]["response_format"]["type"], "json_schema")
        self.assertNotIn("response_format", self.body(deepseek, None)["body"])
        for config in (openai, deepseek):
            self.assertNotIn("temperature", self.body(config, None)["body"])

    def test_json_mode_prompt_mentions_json(self):
        # OpenAI and DeepSeek both reject JSON mode unless the prompt says JSON.
        self.assertIn("JSON", M.SYSTEM_PROMPT)

    def test_an_equal_mapping_is_still_a_schema(self):
        lookalike = {"type": "json_object"}
        body = self.body({"provider": "deepseek", "base_url": "https://api.deepseek.com/v1", "model": "m",
                          "api_key": "k"}, lookalike)
        self.assertEqual(body["body"]["response_format"]["type"], "json_schema")


class LadderTests(unittest.TestCase):
    def schemas_tried(self, provider):
        tried = []

        def caller(config, prompt_text, response_schema, *, timeout):
            tried.append(response_schema)
            if response_schema is not None:
                raise L.ModelRequestError("http_400")
            return OPENAI_CHAT

        loop = M.ModelLoop(model_caller=caller)
        request = types.SimpleNamespace(deadline=time.monotonic() + 30)
        _, structured = loop._call(request, {"provider": provider}, "prompt", ["wait"])
        return tried, structured

    def test_each_provider_climbs_its_own_ladder(self):
        tried, structured = self.schemas_tried("deepseek")
        self.assertIs(tried[0], M.JSON_OBJECT_FORMAT)
        self.assertEqual(len(tried), 2)
        self.assertIsNone(tried[1])
        self.assertEqual(structured, "plain")
        for provider in ("openai", "cline-pass", "compatible"):
            with self.subTest(provider):
                tried, _ = self.schemas_tried(provider)
                self.assertEqual(len(tried), 2)
                self.assertEqual(tried[0]["type"], "object")
                self.assertIsNone(tried[1])

    def test_a_non_format_failure_is_not_retried(self):
        def caller(config, prompt_text, response_schema, *, timeout):
            raise L.ModelRequestError("http_401")

        loop = M.ModelLoop(model_caller=caller)
        request = types.SimpleNamespace(deadline=time.monotonic() + 30)
        with self.assertRaises(L.ModelRequestError):
            loop._call(request, {"provider": "openai"}, "prompt", ["wait"])


class ProviderSafetyTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, CLEAN_ENV, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ["OPENAI_API_KEY"] = "sk-openai-fixture-000000000000000000"
        os.environ["CLINE_API_KEY"] = "sk_cline_fixture_0000000000000000000"

    def resolved(self):
        return L.resolve_llm_config(None)

    def test_a_named_provider_reads_its_own_key(self):
        os.environ["ONI_AI_PROVIDER"] = "cline-pass"
        self.assertEqual(self.resolved()["api_key"], "sk_cline_fixture_0000000000000000000")
        os.environ["ONI_AI_PROVIDER"] = "openai"
        config = self.resolved()
        self.assertEqual(config["api_key"], "sk-openai-fixture-000000000000000000")
        self.assertEqual(config["base_url"], "https://api.openai.com/v1")
        self.assertIsNone(config["config_error"])

    def test_a_stale_endpoint_never_receives_another_providers_key(self):
        os.environ["ONI_AI_PROVIDER"] = "openai"
        os.environ["OPENAI_BASE_URL"] = "https://api.cline.bot/api/v1"
        config = self.resolved()
        self.assertEqual(config["config_error"], "provider_endpoint_mismatch")
        self.assertIsNone(config["api_key"])

    def test_an_unknown_provider_is_refused(self):
        os.environ["ONI_AI_PROVIDER"] = "chatgpt-web"
        config = self.resolved()
        self.assertEqual(config["config_error"], "provider_unknown")
        self.assertIsNone(config["api_key"])

    def test_compatible_needs_an_explicit_endpoint(self):
        os.environ["ONI_AI_PROVIDER"] = "compatible"
        config = self.resolved()
        self.assertEqual(config["config_error"], "endpoint_unset")
        self.assertIsNone(config["api_key"])
        os.environ["OPENAI_BASE_URL"] = "https://gateway.example/v1"
        os.environ["OPENAI_MODEL"] = "some-model"
        config = self.resolved()
        self.assertIsNone(config["config_error"])
        self.assertEqual(config["base_url"], "https://gateway.example/v1")

    def test_cockpit_local_proxy(self):
        os.environ["COCKPIT_API_KEY"] = "sk-cockpit-fixture-00000000000000000"
        os.environ["ONI_AI_PROVIDER"] = "cockpit"
        unset = self.resolved()
        self.assertEqual(unset["config_error"], "endpoint_unset")
        self.assertIsNone(unset["api_key"])
        os.environ["OPENAI_MODEL"] = "fixture-terra-model"
        config = self.resolved()
        self.assertIsNone(config["config_error"])
        self.assertEqual(config["api_key"], "sk-cockpit-fixture-00000000000000000")
        self.assertEqual(config["base_url"], "http://127.0.0.1:14861/v1")
        self.assertEqual(L.effective_wire_api(config), "chat_completions")
        self.assertEqual(L.structured_output_modes(config), ("json_schema", "plain"))
        os.environ["OPENAI_BASE_URL"] = "http://localhost:15000/v1"
        self.assertIsNone(self.resolved()["config_error"])
        os.environ["OPENAI_BASE_URL"] = "https://api.cline.bot/api/v1"
        remote = self.resolved()
        self.assertEqual(remote["config_error"], "provider_endpoint_mismatch")
        self.assertIsNone(remote["api_key"])

    def test_cockpit_never_borrows_another_providers_key(self):
        # setUp provides OPENAI_API_KEY and CLINE_API_KEY but no Cockpit key.
        os.environ["ONI_AI_PROVIDER"] = "cockpit"
        os.environ["ONI_AI_API_KEY"] = "sk-generic-fixture-0000000000000000000"
        os.environ["OPENAI_MODEL"] = "fixture-terra-model"
        self.assertIsNone(self.resolved()["api_key"])

    def test_without_a_named_provider_the_old_rules_hold(self):
        os.environ["OPENAI_BASE_URL"] = "https://self-hosted.example/v1"
        config = self.resolved()
        self.assertIsNone(config["config_error"])
        self.assertEqual(config["api_key"], "sk-openai-fixture-000000000000000000")


if __name__ == "__main__":
    unittest.main()
