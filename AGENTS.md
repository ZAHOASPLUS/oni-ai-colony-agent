# ONI workspace agent rules

These rules apply to every Codex task working in this workspace.

## Live-game safety

- Treat the user's original cloud saves and normal Mod configuration as immutable inputs. Record hashes before and after a run.
- Load only a clearly named test clone whose `.sav` and preview `.png` have both been verified.
- Before any mutation or game launch, publish a short commentary checkpoint with an `action_id`, fresh preconditions, the one intended action, expected evidence, and rollback.
- If it is unclear whether an action was sent, do not replay it. Read the live process, queue, log cursor, and resulting state first.

## Visible-progress protocol

- Before dispatching a long Codex turn, run `scripts/test-codex-transport-stability.ps1`. Do not start while the recent window contains a network disconnect or a fresh Clash Verge/Mihomo/TAG helper process.
- Do not report a delegated or continued task as "started" until its rollout contains the first real assistant, reasoning, or tool event. A successful message-dispatch response alone is not execution evidence.
- When continuing work in another Codex task, open that destination task for the user or mirror its verified progress in the current task. Always name the destination task and keep the control task visibly updated until navigation is confirmed.
- Do not restart, switch, or reconfigure Clash Verge, Verge Mihomo, TAG Mihomo, a TUN adapter, VPN, or the active proxy while a Codex turn is running. If a network change is required, stop at a verified safe checkpoint, tell the user, make the change, then wait for a stable window before starting a new turn.
- Publish the first commentary within 30 seconds of a turn starting, before deep reasoning.
- Do not leave the user without a visible update for more than 60 seconds while work is active.
- Split long waits into polls of at most 30 seconds. Keep a process/session handle or log cursor; never hide a long operation behind an unbounded sleep.
- A blank task view is not proof that execution stopped. From another task, run:

  `python scripts/check_codex_thread_health.py --thread-id <thread-id> --sample-seconds 3`

- If the app view is blank but the rollout is growing or contains new reasoning/tool events, classify it as `visibility_desync`. Do not interrupt, resend, or duplicate a live-game action.
- Classify a fresh turn as a startup-stall candidate only when both the app view and local rollout contain no assistant, reasoning, or tool event for 180 seconds. Retry at most once, using a compact recovery packet.
- On a startup-stall candidate, check the Windows NetworkProfile log and Codex transport-retry signals before retrying. Five exhausted stream retries or recent network churn means transport failure, not a reasoning failure.
- If the compact retry also stalls, do not issue a third identical retry. Move to a clean task/context at a verified safe checkpoint or ask the user before changing the main model/reasoning setting.

## Automatic context compaction

- User override (2026-09-16): do not impose manual token limits, context-percentage thresholds, yellow/red lines, or fixed recovery-packet lengths. Use Codex's normal automatic context compaction.
- Do not pause work, end a run, restore the environment, or refuse the next authorized step solely because the input token count or context-window usage is high.
- Across automatic compaction, preserve the full objective, user corrections, safety invariants, source/test hashes, current process/Mod/queue state, log cursor, completed actions, outstanding work, expected evidence, and recovery procedure. Compaction does not end or restart the task.
- Give workers the task-relevant context and exact file/test boundaries. Keep handoffs focused without imposing an artificial token budget or length threshold.

## Model routing

- User override (2026-10-11): the ONI main controller uses `gpt-6-astra` with `max` reasoning; all delegated workers (code authors, strategy, learning/replay, reviewers, runtime/environment owners) use `gpt-6.1-sol` with `max` reasoning. This replaces all earlier model routing, including the former main-controller `ultra` requirement.
- Set the model and reasoning effort explicitly when creating or resuming a task; verify the actual model/effort in its execution metadata before reporting the switch as complete. Keep worker context short and use exact file/test boundaries.
- Existing workers must finish or reach a verified safe checkpoint for their current atomic step before switching; do not start a duplicate writer or replay an uncertain action. Subsequent worker work must use `gpt-6.1-sol` / `max`; the main controller remains `gpt-6-astra` / `max`.
- If the requested model/reasoning combination is unavailable, report the blocker immediately. Do not silently substitute another model or reasoning effort, or wait silently for capacity.
- Do not change model, reasoning effort, or service tier in the middle of a live action. End the atomic step first.
- Test routing (2026-10-27): every test during development is executed by the ChatGPT client, and the controller then calls Cockpit Tools' local API (bitibit account proxy service, model 6.1 terra or 6.1 sol; provider `cockpit`, `scripts/configure-oni-model.ps1 -Provider cockpit -Model <id>`). The final validation uses the cline-pass endpoint below. See `docs/test-handoff-chatgpt-20261027.md`.
- ONI's own controller model endpoint (2026-10-25): DeepSeek through the Cline-pass gateway, `cline-pass/deepseek-v4.1-flash` at `https://api.cline.bot/api/v1`, wire API `chat_completions`, credential `CLINE_API_KEY`. This is a separate concern from the Codex task routing above; see `docs/model-routing-20261025.md` and use `scripts/configure-oni-model.ps1`.

## Current autonomous-controller goal

- User-approved goal and current execution contract: `docs/active-goal.md`.
- Execution roadmap (milestones, open problem list B1-B16, pending user decisions): `docs/roadmap-20261027.md`.
- User override (2026-10-11): implement fully autonomous decisions and execution on verified test clones; routine commands do not require per-command human approval once the versioned autonomous authorization path is implemented and admitted.
- Each implementation round has one minimal increment and at most one minimal real-game test attempt, followed by a brief result and automatic continuation. Do not add routine full regression, Mock/replay, A/B, long stress, or fixed test-count gates. Necessary compilation/syntax checks remain allowed.
- A failed real-game attempt is not repeated in the same round. Diagnose from its evidence and make a material correction before another round; unsafe preconditions mean no test is launched. Preserve all live-game safety and restoration requirements above.
- Parallel source work uses explicit, non-overlapping file ownership. Only the main controller owns live runtime, saves, Mods, installation, game launch, and the single real-game test.

## Completion evidence

- Keep large logs in evidence files. Return paths, hashes, counts, timestamps, and small relevant excerpts rather than injecting raw logs into the conversation.
- Before declaring a run complete, verify normal exit, original save hashes, restored Mod/config state, empty command queues, and the absence of leftover helper processes.

## Process lifetime

- Start the game in its own short command with `python scripts/api_session.py start`, and never in a command that also runs a polling loop. A polling command that exceeds its timeout becomes a background job, and cancelling that job terminates its whole process tree.
- On Windows `os.kill(pid, 0)` terminates the process; it is not a liveness probe.
