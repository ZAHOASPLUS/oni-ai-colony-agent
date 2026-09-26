# Codex long-run visibility incident — 2026-08-31

## Outcome

The user-visible “thinking” state combined two different failures:

1. A real startup stall: turn `01a05396-835d-7752-8d07-79c63e1d0306` started at `16:52:48Z` and produced no reasoning, assistant, or tool item before it was interrupted at `17:05:49Z`.
2. A visibility mismatch after recovery: turn `01a053a2-a3a5-71c2-bc6d-da963f11648f` produced commentary, reasoning, commands, and file-change events, while the task observer still reported no latest assistant/tool marker. The task was running in the background and the app had not been navigated to it.

The second condition must never be treated as permission to replay a live-game action.

## Evidence summary

- Source task: `01a051c2-7ea1-71e0-83ad-b4bfe0a62381`
- Codex CLI: `0.151.0-alpha.7.2`
- Model/effort: `gpt-5.6-sol` / `ultra`
- Rollout size during diagnosis: approximately 52.4 MB and 6,400+ events
- Historical compactions: 8
- Pre-compaction request input later reached 207,216 of a 258,400-token context window (80.2%), well above the 60% red line
- Long-running goal before recovery: approximately 7.75 million tokens and 8 hours
- Recovery request transport: two `error sending request` events before the first successful HTTP 200; a later stream disconnect was automatically retried
- No observed 401, 403, 429, quota failure, database corruption, or persistent process hang

## Confirmed transport root cause

The real startup stall was caused by host network churn, not by ONI or a deadlocked model:

- At `16:54:06Z`, Windows NetworkProfile Event `10001` recorded a disconnect for the redacted `mihomo` profile.
- Clash Verge started at `16:54:05Z`, and Verge Mihomo started at `16:54:06Z`, rebuilding the TUN/profile at the same moment.
- Codex then logged five `responses_retry` stream-disconnect warnings from `16:54:06Z` through `16:58:28Z`, exhausting retries `1/5` through `5/5`.
- At `16:59:52Z`, Codex fell back to HTTP/SSE, but that request did not complete before the turn was interrupted at `17:05:49Z`.
- Other Codex tasks recorded the same disconnect pattern in that host-wide window, ruling out an ONI-only or single-thread-only cause.
- During recovery, another Mihomo disconnect at `17:06:51Z` matched the first send failure to the second; a TAG Mihomo helper start at `17:07:11Z` matched the second. HTTP 200 resumed at `17:08:04Z`.

The large history and Ultra reasoning amplified recovery latency after transport returned. They were contributing factors, not the trigger. The observer database was healthy and fully projected; the blank view after local events existed was caused by background-task navigation/active-turn visibility behavior, not database corruption.

## Operational mistakes

- The continuation was initially reported as started before the first real progress event was observed.
- The destination task was not opened for the user, and this control task did not mirror its progress.
- The first diagnosis relied on the task observer alone and incorrectly called the recovery turn stuck before checking the local rollout.

## Controls added

- `AGENTS.md` now requires a first commentary within 30 seconds and a visible heartbeat at least every 60 seconds.
- `scripts/check_codex_thread_health.py` provides a redacted, read-only second source of truth.
- `scripts/test_codex_thread_health.py` locks the detector's active, true-startup-stall, visibility-risk, and terminal classifications with synthetic rollouts.
- `scripts/test-codex-transport-stability.ps1` blocks dispatch during recent network/TUN churn.
- Cross-task continuation is not called started until a real execution event appears; the destination must be opened for the user or its verified progress mirrored in the control task.
- Blank app state plus growing rollout is classified as `visibility_desync`; interruption and action replay are forbidden.
- A true startup stall requires both sources to remain empty for 180 seconds. Only one compact retry is allowed.
- Context yellow/red lines are 100k/40% and 150k/60%. At red, the task must stop starting multi-step mutations and prepare a compact recovery packet.
- Live actions are transactionally identified with preconditions, postconditions, and rollback.

## Residual limitation

Workspace rules cannot eliminate upstream transport failures or patch the Codex desktop renderer. They can eliminate the dangerous consequences: silent waiting without an external heartbeat, false “stuck” diagnoses, duplicate game actions, and continuing an unbounded long context after a safe checkpoint.

The Windows event evidence is in the standard `Microsoft-Windows-NetworkProfile/Operational` channel. Diagnostics record only event IDs, timestamps, counts, and process names; they do not retain network messages, request contents, URLs, tokens, or cookies.

Official OpenAI model guidance recommends tracking context as a session grows, keeping prompts lean, and reserving the highest reasoning settings for workloads where measured quality gains justify added latency: https://developers.openai.com/api/docs/guides/latest-model
