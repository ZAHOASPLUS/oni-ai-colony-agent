# A09A5 Approval Boundary Safety (2026-09-07)

## Scope

- `scripts/commit_command_approval.py`:
  - directory/path preflight sequencing in `commit()`
  - recovery ownership gating in `_commit_locked` exception path
  - cleanup behavior in `_remove_new_empty_recovery_dir`
- `scripts/verify_approval_boundary_safety.py` (new)

## Run plan

1. Run baseline with current production source.
2. Apply production fix.
3. Re-run same script with a new report root.
4. Compare counts and evidence.

## Baseline (pre-fix) artifacts

- Report directory: `TBD`
- Run status: `TBD`
- Current failures: `TBD`

## Post-fix artifacts

- Report directory: `TBD`
- Run status: `TBD`
- Old fail / new pass delta: `TBD`

## Evidence contract

- report schema: `oni.a09a5.approval-boundary-safety.v1`
- include source hashes before/after
- include queue snapshots and child-process join/normal-exit evidence
- do not write outside this report and preserve failure outputs

## Notes

- `ApprovalRecovery` recovery directory cleanup is now conservative and does not remove directories in fail path.
