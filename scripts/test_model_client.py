"""Regression: one explicit, validated LLM configuration source (review 2026-09-26, M6).

Any bare URL line in any secrets file overrode OPENAI_BASE_URL, the last one
winning, while the key came from the first source; any 30+ character
token-looking line (a GitHub token, a hash) could become the API key; and
http:// endpoints were accepted.  A key could therefore be sent to an
unrelated host or in clear text.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import model_client  # noqa: E402

# Every variable the model configuration reads, including the key aliases added
# for the Cline-pass gateway (2026-10-25).  Without them a machine with
# CLINE_API_KEY set leaked its real key and endpoint into these tests, which then
# failed and printed the key in the assertion message.
CONFIG_ENVIRONMENT_NAMES = frozenset(
    model_client._KEY_ENVIRONMENT_NAMES
    + model_client._BASE_URL_ENVIRONMENT_NAMES
    + model_client._MODEL_ENVIRONMENT_NAMES
    + model_client._WIRE_API_ENVIRONMENT_NAMES
    + model_client._PROVIDER_ENVIRONMENT_NAMES
    + model_client._REQUEST_TIMEOUT_ENVIRONMENT_NAMES
)
CLEAN_ENV = {
    name: value
    for name, value in os.environ.items()
    if not name.startswith(("OPENAI_", "ONI_AI_")) and name not in CONFIG_ENVIRONMENT_NAMES
}


class SecretsConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="oni-llm-secrets-")
        self.addCleanup(self.temp.cleanup)
        self.secrets = pathlib.Path(self.temp.name) / "secrets"
        self.secrets.mkdir()
        patcher = mock.patch.dict(os.environ, CLEAN_ENV, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, name: str, text: str) -> None:
        (self.secrets / name).write_text(text, encoding="utf-8")

    def test_environment_key_is_never_paired_with_a_url_from_a_note(self):
        os.environ["OPENAI_API_KEY"] = "sk-environment-key-000000000000000000"
        self.write("notes.md", "relay:\nhttps://relay.third-party.example/v1\nBASE_URL=https://other.example/v1\n")
        config = model_client.load_llm_config(self.secrets)
        self.assertEqual(config["base_url"], model_client.DEFAULT_BASE_URL)
        self.assertEqual(config["config_source"], "environment")
        self.assertIsNone(config["api_key"])

    def test_a_key_without_provider_or_endpoint_is_never_sent_to_the_default_gateway(self):
        for name in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY"):
            with self.subTest(name=name), mock.patch.dict(os.environ, {name: "sk-generic-000000000000000000000000"}):
                config = model_client.load_llm_config(None)
                self.assertEqual(config["config_error"], "provider_unset")
                self.assertIsNone(config["api_key"])
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-generic-000000000000000000000000",
                                          "OPENAI_BASE_URL": "https://relay.example/v1"}):
            config = model_client.load_llm_config(None)
            self.assertIsNone(config.get("config_error"))
            self.assertEqual(config["api_key"], "sk-generic-000000000000000000000000")

    def test_bare_token_lines_are_not_guessed_as_keys(self):
        self.write("github.txt", "ghp_" + "A" * 36 + "\n")
        self.write("openai.env", "OPENAI_API_KEY=sk-explicit-key-0000000000000000000\n"
                                 "OPENAI_BASE_URL=https://relay.example/v1\n")
        config = model_client.load_llm_config(self.secrets)
        self.assertEqual(config["api_key"], "sk-explicit-key-0000000000000000000")
        self.assertEqual(config["config_source"], "secrets_dir")

    def test_two_files_with_keys_are_ambiguous(self):
        self.write("a.env", "OPENAI_API_KEY=sk-first-000000000000000000000000\n")
        self.write("b.env", "OPENAI_API_KEY=sk-second-00000000000000000000000\n")
        config = model_client.load_llm_config(self.secrets)
        self.assertEqual(config["config_error"], "secrets_ambiguous")
        self.assertIsNone(config["api_key"])

    def test_request_timeout_is_read_from_the_secrets_document_and_bounded(self):
        self.write("oni-llm.json", json.dumps({
            "OPENAI_API_KEY": "sk-json-key-000000000000000000000000",
            "OPENAI_BASE_URL": "https://relay.example/v1",
            "OPENAI_MODEL": "m",
            "OPENAI_REQUEST_TIMEOUT_SECONDS": 75,
        }))
        config = model_client.load_llm_config(self.secrets)
        self.assertEqual(model_client.effective_request_timeout(config), 75.0)
        self.assertEqual(model_client.config_health(config)["request_timeout_seconds"], 75.0)
        self.assertEqual(
            model_client.effective_request_timeout({"request_timeout_seconds": "900"}),
            model_client.MAX_REQUEST_TIMEOUT_SECONDS,
        )
        self.assertEqual(
            model_client.effective_request_timeout({"request_timeout_seconds": "nan"}),
            float(model_client.REQUEST_TIMEOUT_SECONDS),
        )
        self.assertEqual(model_client.effective_request_timeout({}), float(model_client.REQUEST_TIMEOUT_SECONDS))

    def test_local_http_gateway_is_allowed(self):
        self.assertIsNone(model_client.base_url_error("http://127.0.0.1:8317/v1"))
        self.assertIsNone(model_client.base_url_error("https://api.openai.com/v1"))
        self.assertEqual(model_client.base_url_error("http://api.openai.com/v1"), "base_url_insecure")


if __name__ == "__main__":
    unittest.main()
