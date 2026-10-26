# ONI model routing — cline-pass / DeepSeek (2026-10-25)

Owner: ONI main controller. Status: implemented and verified on this host.

This document is the current model-routing decision for ONI. It supersedes the
`gpt-6-astra` / `gpt-6.1-sol` routing recorded in `docs/active-goal.md` and
`AGENTS.md` for the ONI *controller's own* model endpoint: the controller now
calls DeepSeek through the Cline-pass subscription gateway, using the same
admitted credential as the interactive session.

## Decision

| Role | Provider | Model id | Wire API |
| --- | --- | --- | --- |
| ONI controller choice loop (primary) | `cline-pass` | `cline-pass/deepseek-v4.1-flash` | `chat_completions` |
| ONI controller choice loop (escalation) | `cline-pass` | `cline-pass/deepseek-v4-pro` | `chat_completions` |
| Legacy / self-hosted alternative | `openai` | `gpt-4.1-mini` | `responses` |
| Legacy / direct vendor | `deepseek` | `deepseek-chat` | `chat_completions` |

Endpoint: `https://api.cline.bot/api/v1`. Credential: `CLINE_API_KEY`
(already provisioned for this Windows user).

## Why the adapter was required

The gateway is OpenAI-compatible but not OpenAI-shaped. Verified by direct
probe on 2026-10-25 (evidence: `reports/model-integration/relay-probe.json`,
`reports/model-integration/probe-shapes.json`):

1. **The completion body is wrapped.** A successful answer arrives as

   ```json
   {"data": {"choices": [{"message": {"content": "...", "reasoning": "...",
    "reasoning_details": [...]}}], "usage": {...}, "model": "vmc/..."},
    "success": true}
   ```

   A reader that expects top-level `choices`/`usage` sees an empty answer.
   `agent.llm_planner.normalize_model_response` now unwraps the envelope and
   records `envelope_unwrapped`, `provider_model`, `gateway_generation_id`.
2. **Failures can arrive inside HTTP 200.** `{"error": "model not found",
   "success": false}` is returned with status 200. `response_error_code` turns
   it into a `gateway_*` failure so the loop never treats it as a success.
3. **`/responses` is not served.** The gateway serves `chat/completions` only.
   `effective_wire_api` now infers the protocol from the endpoint host (and the
   model name), keeping `responses` for `api.openai.com` and forcing
   `chat_completions` for `api.cline.bot`, `api.deepseek.com`, and other
   compatible gateways.
4. **DeepSeek answers carry reasoning.** `reasoning` / `reasoning_details` are
   never treated as the answer; only `message.content` is parsed.
5. **Structured output works.** Both `response_format: {"type": "json_schema",
   ...}` and `{"type": "json_object"}` return 200 with schema-conforming JSON,
   so the loop's strict `oni.llm-choice.v1` schema path stays enabled. If a
   gateway ever rejects it, the existing single plain retry covers it
   (`structured_output_unsupported`).

## Configuration

```powershell
# Apply the Cline-pass DeepSeek endpoint for this Windows user (reuses the
# already-provisioned CLINE_API_KEY; never prints or stores it in the repo).
scripts\configure-oni-model.ps1 -Provider cline-pass

# Inspect the effective wire configuration.
scripts\configure-oni-model.ps1 -Show

# Legacy providers.
scripts\configure-oni-model.ps1 -Provider openai
scripts\configure-oni-model.ps1 -Provider deepseek
```

Environment contract (per-user):

| Variable | Value |
| --- | --- |
| `OPENAI_API_KEY` | the provider key (ONI's wire field name) |
| `OPENAI_BASE_URL` | `https://api.cline.bot/api/v1` |
| `OPENAI_MODEL` | `cline-pass/deepseek-v4.1-flash` |
| `OPENAI_WIRE_API` | `chat_completions` |
| `ONI_AI_PROVIDER` | `cline-pass` |

Alternatively, a secrets file (`--secrets-dir`) may carry `CLINE_API_KEY`,
`NEWAPI_API_KEY`, `DEEPSEEK_API_KEY`, `provider`, `base_url`, `model`,
`wire_api`. The key and the endpoint always come from the same source, so a key
is never sent to an unrelated host.

Restart the Bridge/controller after changing these values.

## Verification

| Check | Command | Result |
| --- | --- | --- |
| Gateway reachability and model inventory | `python -B reports/model-integration/probe_relay.py` | 472 models; `deepseek/deepseek-v4.1-flash`, `deepseek/deepseek-v4-pro` present |
| Envelope/structured-output shape | `python -B reports/model-integration/probe_shapes.py` | `data` envelope confirmed; `json_schema` and `json_object` both 200 |
| Adapter regression (offline) | `python -B scripts/test_cline_pass_model_contract.py` | 23 passed |
| Existing config regression | `python -B scripts/test_llm_secrets_config.py` | 6 passed |
| Existing loop regression | `python -B scripts/test_model_loop.py` | 26 passed |
| LLM fallback contract | `python -B scripts/verify_llm_fallback.py` | passed |
| Doctor key detection | `python -B scripts/verify_oni_doctor.py` | passed; `CLINE_API_KEY` detected |
| End-to-end live choice | `python -B scripts/verify_oni_model_integration.py` | `status: ok`, candidate `resume_game`, command `cmd-model-…` pending approval |

Live evidence: `reports/model-integration/live-integration-smoke.json`.

## Open items

- Per-token pricing for the Cline-pass models is not configured, so
  `cost_estimate_usd` stays `null` while token accounting is live. Set
  `ONI_AI_INPUT_COST_PER_1M` / `ONI_AI_OUTPUT_COST_PER_1M` when the published
  rates are known.
- `cline-pass/deepseek-v4-pro` is catalogued as the escalation model but the
  loop currently uses one model per run; switching is a configuration change.
- The gateway's own quota/rate limits are outside ONI's control; the loop's
  existing backoff (60/120/300 s) covers `http_429`.
