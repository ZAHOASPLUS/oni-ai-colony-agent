"""Regression: one explicit, validated LLM configuration source (review 2026-09-26, M6).

Any bare URL line in any secrets file overrode OPENAI_BASE_URL, the last one
winning, while the key came from the first source; any 30+ character
token-looking line (a GitHub token, a hash) could become the API key; and
http:// endpoints were accepted.  A key could therefore be sent to an
unrelated host or in clear text.
"""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import llm_planner  # noqa: E402
from scripts import verify_llm_fallback as fixtures  # noqa: E402

CLEAN_ENV = {
    name: value
    for name, value in os.environ.items()
    if not name.startswith(("OPENAI_", "ONI_AI_"))
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
        config = llm_planner.load_llm_config(self.secrets)
        self.assertEqual(config["base_url"], llm_planner.DEFAULT_BASE_URL)
        self.assertEqual(config["config_source"], "environment")

    def test_bare_token_lines_are_not_guessed_as_keys(self):
        self.write("github.txt", "ghp_" + "A" * 36 + "\n")
        self.write("openai.env", "OPENAI_API_KEY=sk-explicit-key-0000000000000000000\n")
        config = llm_planner.load_llm_config(self.secrets)
        self.assertEqual(config["api_key"], "sk-explicit-key-0000000000000000000")
        self.assertEqual(config["config_source"], "secrets_dir")

    def test_two_files_with_keys_are_ambiguous(self):
        self.write("a.env", "OPENAI_API_KEY=sk-first-000000000000000000000000\n")
        self.write("b.env", "OPENAI_API_KEY=sk-second-00000000000000000000000\n")
        config = llm_planner.load_llm_config(self.secrets)
        self.assertEqual(config["config_error"], "secrets_ambiguous")
        self.assertIsNone(config["api_key"])

    def test_clear_text_endpoint_is_refused_before_any_request(self):
        self.write("relay.env", "OPENAI_API_KEY=sk-relay-000000000000000000000000\n"
                                "OPENAI_BASE_URL=http://relay.example/v1\n")
        called = []

        def model_caller(config, prompt):
            called.append(config)
            raise AssertionError("model must not be called")

        with tempfile.TemporaryDirectory(prefix="oni-llm-plan-") as bridge:
            plan = llm_planner.generate_plan(
                pathlib.Path(bridge), fixtures.state(), [], fixtures.advice(),
                enable_llm=True, secrets_dir=self.secrets, model_caller=model_caller,
            )
        self.assertEqual(plan["llm"]["status"], "base_url_insecure")
        self.assertFalse(plan["llm"]["api_key_present"])
        self.assertEqual(plan["llm"]["base_url_host"], "relay.example")
        self.assertEqual(called, [])

    def test_local_http_gateway_is_allowed(self):
        self.assertIsNone(llm_planner.base_url_error("http://127.0.0.1:8317/v1"))
        self.assertIsNone(llm_planner.base_url_error("https://api.openai.com/v1"))
        self.assertEqual(llm_planner.base_url_error("http://api.openai.com/v1"), "base_url_insecure")


if __name__ == "__main__":
    unittest.main()
