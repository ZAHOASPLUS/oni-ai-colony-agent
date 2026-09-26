# A09A7 Colony Plan custody baseline — 2026-09-07

Offline diagnostic only. No game, Steam, Bridge, real command queue, original
save, or Mod configuration was used or changed. No live admission.

## Frozen-source execution

Action: `ONI_A09A7_CUSTODY_BASELINE_20260907_01`.
Command: `python -B scripts/verify_colony_plan_custody_integration.py`.
Normal runner exit: **1**, expected diagnostic failure, not a test pass.
Window: 2026-09-07 14:01:45.626–14:01:47.727 +08:00 (2.101 seconds).

Report:
`../reports/a09a7-plan-custody-verifier-20260907T060145626335Z-9576-52b67c33c709/report.json`

Report SHA-256:
`7C4865F95B275356919C8F45C833D0D260D455758F4C12F4327BC329966A96FE`.

| Case | Result | Evidence |
| --- | --- | --- |
| Valid lifecycle | PASS, 10/10 assertions | Draft → active → aborted; one approval-required pending command, then one matching quarantine owner. |
| Lifecycle lock excludes advance | FAIL, 9/11 assertions | All 17 actual held-lock samples observed mutation; child completed while parent held the shared lock. |
| Lifecycle lock excludes abort | FAIL, 10/12 assertions | All 17 actual held-lock samples observed mutation; child completed while parent held the shared lock. |
| Stale quarantine payload rejected | FAIL, 5/9 assertions | Fresh B positive control passed; stale A was accepted and B was not preserved. |

Total: 1/4 cases, 34/42 assertions passed. Both child processes exited 0,
without runner termination or residual children. All four temporary roots were
removed. Actual import paths matched; recorded source hashes did not drift.
Post-run targeted process census found no matching verifier/spawn process.
The lock-window transcript contains pipe-closure errors after the incorrectly
early child completion; these do not replace the independent mutation evidence.
The assertion named `child_succeeds_after_lock_release` is not sufficient alone
to prove timing: acceptance requires every held sample unchanged and no held
outcome, both of which failed here.

Key source hashes at this run:

- Verifier: `E5CA75C76AB0AC6489FF00E669E529E4AB51ABC337C7E405AE0CEAE9C8176A98`.
- Colony Plan: `EE0AA5C25F7CB2DC0A42C2C7FBE52A19581DBB39DE5972A98BE6BD99A9E4D369`.
- Custody core: `FAACF7C429D3711E4BFCC33FF64E5247FF7725B61570BC72A66D4D02FD413DD1`.
- Command queue: `4992E0605DCE7821AE8776E38AB30A2E1C805DD15F23C988E25488EFE4E8DEA1`.

## Verifier gate check

Root separately extracted only `_finish` with AST and executed it in memory,
without importing production modules or constructing fixtures. One valid
positive control and 14 negative controls passed: zero/false assertions,
explicit failure, exception, missing/failed cleanup, missing/unstarted child,
nonzero exit, forced termination, unknown residual state, shutdown/release/
close errors. The verifier bytes remained unchanged. This is not an additional
functional integration run and did not generate a separate report file.

## Bounded A09A8 repair contract

Two fresh Luna Max workers have disjoint write scopes. The root remains Astra
and owns final acceptance. Functional runs wait until both writers are terminal
and their source hashes have been independently frozen.

1. **Observed-envelope core:** additive explicit stable observation (path,
   exact bytes, file identity, mtime) supports legacy pretty JSON while matching
   the complete expected envelope. Existing calls without an observation retain
   their strict canonical-byte behavior. No source rewrite, omitted expected
   value, or relaxed identity check is an acceptable compatibility workaround.
2. **Plan adapter:** serialize plan mutations under the shared OS lifecycle
   lock; reject, never stale-delete, an existing legacy plan lock. Publish only
   with create-only custody; quarantine with journaled custody and observation.
   Reject abort for approving/approved/executing/confirming leaves.
3. **Publication context loss:** keep both decision-context checks. Replace
   raw deletion and silent same-ID retry with an auditable paused/blocked plan,
   retained command identity, and exact quarantine. If quarantine is uncertain,
   preserve the paused plan and evidence and require recovery; do not claim the
   entire plan/queue revision transaction is already crash-atomic.

The original four-case custody verifier and five-case Colony Plan verifier are
not to be modified to accept the repair. New tests cover the additive interface
and explicit publication-recovery semantics. Existing raw plan inventory,
remaining outcome consumers, uncertain approval recovery, official Bridge
replay performance failure, formal bootstrap acceptance, and live admission
remain separate unfinished work.
