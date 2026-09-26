# Local ONI controller ownership release

Action `m35-release-local-ownership`. Local controller `01a086fb-6b93-7543-803b-25129d2bbfe6` releases local development/integration write ownership to migration controller `01a08768-930a-7b13-ad08-2a82411e0285`, task **评估 ONI 云端代码运行**. State: **OWNERSHIP_RELEASED**. No active local code authors remain. The releasing task will not resume authors, alter code/tests, restore the environment, launch ONI or replay any uncertain action. It will not automatically reclaim ownership.

The original long-term Goal remains unachieved and unchanged. Its local status was set to blocked after three consecutive context-redline turns; it was not marked complete or shortened. The receiving task is responsible for preserving the objective and VPS continuation, without claiming that the old token/time ledger migrated.

## Recovery packet to carry with the snapshot

1. `docs/recovery-controller-m33-20260910.md`, SHA-256 `94b098cccbf3009019781ac75687652182aeec7ebd93f4a1af8e7c781b251110`: complete Goal, boundaries, M28/M29 state, isolated slices, hashes, evidence and rollback.
2. `reports/controller-m28-priority-20260909T1556/m34-scalar-contract-readonly.md`, SHA-256 `10c7c9f00166e22809abc3db68beae330b076e2dd63305a2cca938cda0e1e232`: corrected actual scalar metadata and narrow next repair. Neither cycle scalar has a registered contract_id; do not invent one.
3. `reports/controller-m28-priority-20260909T1556/m35-ownership-release.json`: fresh source/evidence hashes, 16 protected hashes, queues, stage contents, worker states and formal ownership release.

No candidate was integrated. M28 candidate controller SHA-256 remains `6e56e6fa46f53bda1e3bbd449f8a40c44417607be99137117190fd75be1bda51`, test SHA-256 `45346e2e27c87c21782b01dec47156ae33e743ad10e404fc22ab3f25c787f0be`. Root ran 22 tests successfully, but independently reproduced P2 malformed cycle/game_time_cycles acceptance; the scalar fix author failed on capacity before editing. Shared and baseline controller/test files remain their original frozen versions. Former M28 writers were `/root/m28_astra_author` (interrupted after sealed result) and `/root/m28_scalar_guard_fix` (errored before work); both owned only the two candidate files. Final reviewer was read-only and is interrupted.

Strategy and learning slices are terminal isolated artifacts, not shared integrations: `reports/strategy-progression-m33-astra-20260910` (18 tests/50 subtests; one old sealed pyc artifact is explicitly recorded) and `reports/learning-replay-m33-astra-20260910` (24 tests, real sequential tracker fixture replay, no real clone promotion). Original M33 seal and manifest evidence must be preserved. The visible observation fixture pack is likewise synthetic/offline-only. Follow exact hashes in the packet rather than treating test counts as gameplay acceptance.

## Fresh local state and preserved unknown outcome

Read-only process/queue/hash check at `2026-09-09T18:43:21.7509671Z`: 16/16 protected hashes matched, seven queues existed and were empty, ONI/Steam/Bridge/environment-manager processes were absent. M29 stage retains eight files; transaction plan and finalize lock were absent. Installed runtime is still **configured_not_restored**. The no-plan recovery latch remains outcome_unknown/plan_publication_outcome_unknown. Do not delete/rename latch or stage, rerun old Apply, or perform a local restore as part of migration. Preserve original saves, normal configuration backups, all failed evidence and the configured current bytes in the migration snapshot.

## New user migration boundary supersedes old execution location/routing

The receiving task contains the directly verified user authorization: all development, build, offline tests and continuing workers migrate to Alpha VPS. Tests that inherently require this local machine must be marked **SKIPPED**, not passed, until the user actively reconnects and arranges local execution. No local game launch, local test restart or local environment restoration is authorized by this release.

The later user message in that same receiving task sets a five-hour migration/environment-deployment/VPS-restart target; authorizes VPS multi-key/multi-CLI/full-permission execution; routes main controllers to Astra Ultra with Sol Ultra fallback when unavailable, and execution workers to Luna Max. Those latest remote routing instructions supersede the older Astra-Max-worker references in M33 for future VPS work. The releasing task does not edit global credentials, providers, routing, network or AGENTS as part of this handoff.

At 21:00 Asia/Shanghai, only a new candidate meeting real-machine test prerequisites should trigger email or VPS QQ to request user participation. The receiving migration controller owns scheduling, verified recipient binding, notification deduplication and remote deployment; the releasing task creates no duplicate automation or notification channel.

The next product code action after the receiving controller safely snapshots and starts the VPS environment is the isolated M28 scalar/quality repair and meaningful test extension from M34. Preserve one writer per file set and serial acceptance/integration ownership. Linux/offline passes cannot replace Windows path, live Probe/Bridge or actual clone acceptance; keep skipped/unproven gates explicit.
