# Autonomous protocol implementation agreement

Frozen for the 2026-10-11 parallel implementation. This document defines shared wire decisions; workers must report a required change to the main controller before diverging.

## Compatibility

Keep V1 policies, V3 commands, and V1 receipts as the existing manual-human path. Do not rewrite old artifacts. Add V2 policy/V4 command/V2 receipt support; a mismatched combination is rejected. Existing observations and evidence remain authoritative; never relax quality, source/frame, custody, action-contract, or later-effect checks to obtain autonomous execution.

## Policy V2

All existing required policy fields remain. Add exactly `execution_mode`, `run_id`, `controller_id`, `session_id`, and `world_id`. `execution_mode` is exactly `autonomous`; run/controller identifiers match `^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$` (maximum 80 characters), session is the exact active Probe session, and world is an exact non-boolean integer in 0..2147483647 matching the active world. Require exactly one allowed save identity and `max_pending_commands=1` when autonomous actions are enabled. Preserve existing TTL and action-catalog bounds. The `run_id` must match the trusted snapshot's validation run identity. Disabled policies may retain the same identity for pause/stop. Missing or invalid identity is not inferred from a model response.

## Command V4 and receipt V2

The command retains the existing typed action, conditions, source identity/frame, expiry, and `requires_approval=true` (authorization is still required). Add `run_id` and `controller_id`. V4 supports `mode=model_selected` with existing candidate provenance and `mode=suggested` with existing rule/incident provenance and all survival-certificate checks. Other modes remain legacy. Use `pending_autonomous_authorization` as the pre-authorization status; subsequent custody directories/states remain approving/approved/executing/confirming/done. V4 authority identifiers must exactly match policy and be covered by the command intent hash.

V2 receipt retains all V1 receipt fields and adds `run_id` and `controller_id`. Its `approved_by` is exactly `autonomous_controller`; it binds the exact V4 intent, source/frame, and canonical V2 policy hash. Both implementations use the existing typed canonical hash. Validation must reject autonomous authority on legacy V3/V1 or human authority on the autonomous V4/V2 route.

## Atomic authorizer integration

Python owner exports `authorize_autonomous_command(bridge_dir, command_id, *, latest_state, frame, policy)` from `agent/autonomous_authorization.py`. It returns the approved command document only after an actual durable custody transition; refusal raises a typed error without an execution claim. Reuse the existing lifecycle lock and approval transaction primitives; never directly copy pending into approved or synthesize a human receipt. Re-read protected policy/pending identity under custody, validate the pinned committed frame and latest trustworthy state, check exact run/session/save/world bindings, and refuse a second active mutation or any unresolved outcome. Do not hold the lock across network/model calls.

The model-loop owner constructs V4 only under valid V2 autonomous policy, publishes pending once, then calls this authorizer after the generation's frame is committed. Never auto-authorize a leftover legacy pending command. On a crash/unknown result, reconcile the existing command; do not mint a replacement id and dispatch again.

Deterministic survival-pause arbitration takes precedence over model/network calls. Its V4 suggested command uses the same sole autonomous authorizer, retains genuine rule provenance and certificate validation, and blocks ordinary model actions until resolved. Do not suppress the emergency pause, leave it awaiting a human, or falsely label it model-selected.

Native execution independently validates these contracts against current startup/run/session/world/save identity and live preconditions before using existing durable at-most-once execution and later observation confirmation. Autonomous mode may not bypass OutcomeLatch.

## Cooperative controller control

Require `bridge_dir/autonomous-control.json` for autonomous authorization, with schema `oni.autonomous-control.v1`, exact `run_id`, `controller_id`, `session_id`, `world_id`, `save_id_hash`, the exact nine-field `startup_expectations` object, and `desired_state` equal to `running`, `paused`, or `stopped`. `updated_at` and controller process metadata are optional diagnostics. Missing, unreadable, mismatched, or non-running control blocks new V4 authorization under the same lifecycle lock. It does not redefine the V2/V4/V2 wire shapes.

Pause/stop first publishes revoked desired_state under lifecycle custody. Do not change the policy hash while an approved/executing/confirming intent still needs dispatch/effect reconciliation: native confirmation binds that exact policy. Continue observing/draining existing commands through existing custody/expiry paths, then disable the bound policy when quiescent. Preserve unknown outcomes and report them; never delete queues, kill unverified processes, or report stop completed while an atomic step is unresolved. Distinguish requested pause/stop from actually quiescent/stopped, and controller pause from observed game pause.

## Ownership and checks

- Python protocol owner: `agent/action_policy.py`, `agent/approval_receipt.py`, `agent/command_queue.py`, `agent/command_custody.py` if necessary, `agent/action_outcomes.py`, `scripts/commit_command_approval.py`, new `agent/autonomous_authorization.py`. V4-only intent projection adds run/controller identifiers; V3 projection remains byte-semantically unchanged.
- Native owner: `mod/Source/Commands/ActionExecutor.cs`; request ownership before any other existing file changes.
- Loop/entry owner: `agent/model_loop.py`, `bridge/oni_bridge.py`, `scripts/run-bridge.ps1`, new `scripts/oni_autonomous.py`.
- Main controller: this agreement, goal/docs, any cross-cutting integration outside those files, runtime preparation, compilation/deployment, and the sole real-game test.
- Do not launch game/Bridge, edit Klei data, access credentials, install Mods, mutate real queues, or run tests. Python syntax checking is allowed without importing project modules. Main owns C# compilation once source edits are quiescent.
