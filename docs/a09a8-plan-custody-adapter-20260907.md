# A09A8 Colony Plan custody adapter

Date: 2026-09-07 (UTC)

## Scope

`agent/colony_plan.py` now serializes create, advance, pause, resume, abort, and
recover through the shared OS lifecycle custody lock.  The retired
`colony-plans/.mutation.lock` is inspected with `lstat` (including a dangling
symlink) and causes a fail-closed manual-review error; it is never aged,
removed, or replaced.

Leaf publication uses the public `publish_pending_command` API.  Pending
quarantine observes the complete source envelope and passes its bytes,
identity, mtime, and canonical path observation to
`transition_pending_envelope`.  A missing observation/result is treated as a
custody failure, not as a successful quarantine.

If the decision context changes after publication, the plan first commits a
paused/blocked revision retaining the real step and command id.  Only then is
the command isolated with reason
`colony_plan_decision_context_changed` (without `aborted_plan_id`).  On
successful isolation a second revision clears `open_command_id` and
`current_step` while retaining the blocked step and quarantine history.  An
isolation failure or unknown outcome leaves the first paused revision and all
source evidence in place, then raises `PlanRecoveryRequired`.

Abort rejects every plan command in `approving`, `approved`, `executing`, or
`confirming` before reconciliation and rechecks all four stages after pending
claims.  Normal pending-abort quarantine retains the legacy
`colony_plan_aborted` reason and `aborted_plan_id` fields.

## Evidence and verification boundary

Preimage backup (captured before edits):

- [preimage/colony_plan.py](../reports/a09a8-plan-custody-adapter-20260907T060546138Z-2712-e868d401441f/preimage/colony_plan.py)
- SHA-256: `EE0AA5C25F7CB2DC0A42C2C7FBE52A19581DBB39DE5972A98BE6BD99A9E4D369`

The new bounded verifier is
`scripts/verify_colony_plan_publication_recovery.py`.  It creates unique
UTC/pid/nonce reports and isolated temporary fixtures, records source
before/after hashes and import paths, fails on unexpected exceptions or
skipped cases, and verifies temporary-root cleanup.  It covers normal
publish/abort, post-publication context change, unknown isolation, pre-
publication change, duplicate command-id publication, all four abort-blocking
stages, and retired-lock refusal.

Per the A09A8 execution boundary, this turn did not run the verifier, fixtures,
pytest, game/Steam/Bridge, or real command queues.  Root should run the new
verifier only after all core/worker writers have stopped and the core public
observation API is frozen.  Malformed-JSON raw-inventory handling remains
outside this change and is not claimed as fully consumer-safe here.
