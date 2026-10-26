# ONI fully autonomous controller — active user goal

Updated: 2026-10-11 Asia/Shanghai. Owner: current ONI main controller.

## Outcome

Build on the existing repository and preserve pre-existing uncommitted work. Deliver a controller that observes the real ONI game through Probe/Bridge, chooses objectives and plans, autonomously authorizes and executes controlled game actions, confirms effects from later trustworthy observations, recovers from failures, saves progress, and resumes safely.

Cover survival, a stable colony, expansion, industry, space, every official main objective actually applicable to the bound game version/DLC/test scenario, and at least one demonstrably useful strategy improvement from real feedback. Determine applicability from the current build, game objectives, and official sources; missing implementation or difficulty is not grounds to drop an applicable objective.

## Latest user decisions

- Main controller: `gpt-6-astra / max`.
- Delegated implementation/review/runtime tasks: `gpt-6.1-sol / max`.
- These settings supersede the former `ultra` main setting and Astra worker setting. Verify actual execution metadata; do not substitute another combination silently.
- Fully autonomous game decisions and execution; routine commands do not wait for individual human approval.
- Each round: one minimal useful increment, required compilation/syntax checks only, at most one minimal real-game test attempt, a short factual report, then automatic continuation.
- No routine unit/Mock/replay/A-B/full-regression/long-stress/fixed-count testing gates. Failure is retained; do not repeat the test in the same round or rename an unchanged retry as a new round.
- If safe preconditions are missing, report the test as not executed and resolve the blocker autonomously when possible.
- User submission authorizes real validation in verified test clones after basic admission, overriding older code-only-phase notices. It does not authorize original-save mutation or fabricated game progress.

## Implementation

1. Reconcile current source, controller ownership, transport, processes, save/preview identity, Mods/configuration, command queues, log cursor, and uncertain actions. Use historical reports only as clues.
2. Establish current game/Probe/Bridge identity and trustworthy advancing observations.
3. Implement `oni.command.v4`, `oni.action-policy.v2`, and `oni.approval-receipt.v2` consistently in Python and C#. Record autonomous authority explicitly. Bind it to the current run, controller, clone, session, world, precise intent, source observation, and policy. Retain legacy human authorization semantics and at-most-once dispatch.
4. Start autonomous live validation with a low-impact duplicant-priority change and required inverse in one minimal case. Extend oxygen, reachable food, water/sanitation, power, temperature, work priorities, research, building, production, logistics, expansion, and space according to the actual colony's needs.
5. Confirm each action's real effect; ACKs, building counts, blueprint creation, or process presence alone do not prove functional success. Unknown effects trigger observation/reconciliation, never replay.
6. Persist objective/plan/confirmed results/unresolved intents/clone progress. Use real feedback for a strategy change and verify its intended improvement in a later round's sole real test.
7. Provide one usable entry point for start, status, pause, and safe stop, plus current objective, last action, result, blocker, concise usage instructions, actual coverage, and known limits.

## Safety and ownership

Use account `user`. Prefer Steam/game operations for save and Mod handling; file changes require exact backups and restoration. Original cloud saves and previews, normal Mod configuration, and historical recovery evidence are protected. Only a clearly named clone with verified `.sav` and `.png` may be loaded. Keep clone progress separate from originals.

Before each mutation/launch, publish `action_id`, fresh preconditions, the one action, expected evidence, and recovery. For an uncertain send, inspect live process/queue/log/state before doing anything else. Never remove a recovery latch to bypass a refusal. Use controlled game interfaces; do not rewrite resource/achievement/completion fields to manufacture success.

The main controller alone owns game/runtime/installation/queues and real testing. Worker source ownership must not overlap. Freeze source/artifact hashes before building/deploying; do not build or start a live Bridge against concurrently edited inputs.

Each real session ends with normal game/Bridge shutdown, protected save/preview hash checks, exact normal Mod/configuration restoration, empty nonterminal queues, and no leftover helpers. Resolve an incomplete restore before another real test.

Run the transport-stability script before long dispatches. Do not change proxies, VPN, TUN, or network while a Codex turn is running. Publish first progress within 30 seconds; no active gap over 60 seconds; individual waits at most 30 seconds. Use automatic context compaction without manual token/context thresholds and retain full objective, overrides, hashes, process/queue state, log cursor, completed work, and next steps.

## Reports and completion

Each round reports only the increment, sole real test, observed result (`passed`, `failed`, or `not executed`), evidence paths/essential identities, and next round. Keep large logs in evidence files.

The whole goal is complete only with actual evidence for autonomous observe/plan/execute/confirm/recover, applicable official main objectives, required real survival/industry/space effects, at least one useful feedback-driven improvement, and safe final restoration. Preserve the full goal across rounds. Short tests establish only their observed scope and do not certify indefinite survival.

## Current handoff

- Main thread: `01a129e8-20a1-71d0-849c-eeef582fb3f6`.
- Existing source changes were backed up under `reports/auto/pre-dispatch-20261011` before delegation.
- First observation round: `auto-1011-r01`; the sole launch attempt was canceled by the user's physical Escape key. No current game process or session evidence appeared, and Player.log remained the 2026-10-03 file. No real game acceptance or autonomous action is claimed.
- Source pair: verified migrated copy `oni-oldhost-runtime-20261001-03`; new loader clone: `oni-auto-1011-r01`.
- Fresh boundary: `reports/auto-boundaries/auto-1011-r01/pre-boundary.json`.
- Clone manifest: `reports/auto-clones/auto-1011-r01/test-clone-manifest.json`.
- R01 environment was safely aborted/restored: `reports/auto-1011-r01/environment/abort/abort-result.json`. The final comparison `reports/auto-boundaries/auto-1011-r01/post-comparison.json` passed with 208 original save/preview entries unchanged, exact Mod/configuration/Probe/policy restoration, empty queues, and no ONI/Bridge processes. The only additions are the approved clone pair.
- V4/V2/V2 source implementation, autonomous entry point and candidate inverse support are present. Syntax/import checks and the necessary Release build passed; a focused cross-language source review found no actionable mismatch. These are code/build findings, not live-game acceptance. The current entry operates an already admitted run; preparing/loading the game, saving/rebinding checkpoints and complete colony progression remain unfinished.
- On 2026-10-11 the user requested migration of the execution environment to another Windows host and explicitly paused the Goal until the device switch is complete. The Goal tool confirmed `paused`. Do not resume game actions automatically during migration.
- The app Goal's stored text still contains older model routing; the latest direct user override and this document govern: main `gpt-6-astra/max`, workers `gpt-6.1-sol/max`. Do not reintroduce the resolved model blocker or mark the unfinished goal complete to replace its record.
- Old execution host: `oldhost`, user `user`. The new host must freshly establish installation/build compatibility, local user-data paths, protected-save baselines, clone lineage, process identity and startup anchors. Old machine paths, PIDs, volume/file identities, startup receipts and authority records are historical evidence only.
