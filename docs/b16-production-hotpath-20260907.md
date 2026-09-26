# B16 Production Hotpath SPARK (2026-09-07)

- action_id: ONI_B16_SPARK_HOTPATH_20260907_PRODUCTION_01
- status: dedicated_offline_gate_passed_after_commander_review
- requested_scope: offline hot-path equivalence optimization on two production files only
- allowed_targets: agent/strategy.py, agent/state_quality.py
- protected_unchanged: scripts/verify_b14_strategy_hotpath.py; bridge/oni_bridge.py; scripts/verify_bridge_reliability.py; reports/bridge-reliability.json

## 1) Prechecks
- `AGENTS.md` reviewed.
- `test-codex-transport-stability.ps1` completed and reported `stable: true`.
- `reports/b16-strategy-hotpath-20260907/recovery-after-harness.md` reviewed and consistent with constraints.
- source hashes before edit matched required values (strategy/security):
  - agent/strategy.py: `926F3597A4057E210E619EF203FD2B33ECE82DF3A03DEC3B76734E51C368E87E`
  - agent/state_quality.py: `99C1B0D6F3AF6AEE4D4CC0F309973932AF7F4E99BE5C6C309DBD3D7F5653C14A`

## 2) Production edits applied
### agent/state_quality.py
- `safe_get`: add fast path when path contains no dot for top-level direct lookup.
- `field_quality`: preserve existing quality-entry precedence; after that, non-dot path uses direct top-level state lookup, nested paths still use `safe_get`.

### agent/strategy.py
- `_field_contract_blockers`: call `field_quality(state_dict, path)` once and pass the same entry to both:
  - `quality_contract_blockers`
  - `_capture_binding_blockers`

No other production logic was changed.

## 3) Syntax check
Executed: `python -B -c "import pathlib; ... compile(...); print('compile_ok')"`
Result: `compile_ok` (no syntax errors).

## 4) Post-patch verification command
The second attempt used this command:
`python scripts/verify_b14_strategy_hotpath.py --post-patch --run-id ONI_B16_SPARK_HOTPATH_20260907_POSTPATCH_01 --baseline-run-id ONI_B16_SOL_HOTPATH_20260907_BASELINE_01`

### outcome
- Command exited with code `1`.
- `FileExistsError` from `run_dir` creation because directory already existed:
  `...\reports\b16-strategy-hotpath-20260907\runs\ONI_B16_SPARK_HOTPATH_20260907_POSTPATCH_01`
- directory exists and is currently empty (no valid run artifacts written).
- first attempt used wrong baseline id and created the same run directory, so this constrained run was intentionally not rerun.

## 5) Current state
- `agent/strategy.py` hash: `2D70076900DA0B21CC77A9267888C0CF5C4C69E7143F30B0A0F0B77B063D9AF5`
- `agent/state_quality.py` hash: `49C0A2C6B894E5EB519B8115B8F244EF14A58AA1D7EB912C291B21CA777FDB6D`

## 6) Independent commander acceptance, 2026-09-07

No additional authorization was required for a fresh isolated run. The first Spark attempt selected a nonexistent SPARK baseline instead of the supplied SOL baseline; the second reused the directory allocated by that failure. Both stopped before measurement. Their empty directory is retained as failure evidence.

- action_id: `ONI_B16_ASTRA_REVIEW_20260907_POSTPATCH_02`
- Fresh precheck: 2026-09-07T04:34:01Z; both production hashes and harness hash matched; correct baseline existed; new run directory absent; no relevant game/Bridge/benchmark processes.
- Exact command: `python -B scripts/verify_b14_strategy_hotpath.py --post-patch --run-id ONI_B16_ASTRA_REVIEW_20260907_POSTPATCH_02 --baseline-run-id ONI_B16_SOL_HOTPATH_20260907_BASELINE_01`
- Normal exit 0. All five deterministic inputs and outputs matched the baseline. Two-record startup-admission sanity passed all 15 gates.
- 10,000-record replay: 7.786653399998613 seconds, peak working set 135.08203125 MiB, 383 positive memory samples. All 24 benchmark gates passed, including exact current session/world/save/path, latest sequence 10000, strategy processed latest, complete archive, trusted startup binding, and fresh timestamp.
- No timeout, exception, stderr, child residual, reparse point, or temporary-file residue. Temporary root removal verified after context exit.
- Report: `reports/b16-strategy-hotpath-20260907/runs/ONI_B16_ASTRA_REVIEW_20260907_POSTPATCH_02/post-patch.json`
- Report SHA-256: `DB0CA21F96BD9DB3B806E0822E828972EC8543455CA2198DD0D74D5985F77871`
- Source hashes above remained unchanged after the run. Bridge, official reliability verifier, and canonical reliability report also retained their pre-run hashes.

Acceptance is limited to this dedicated offline run; headroom below the 8-second limit is approximately 0.213 seconds, not evidence of robust timing under all loads. Official integrated reliability regression and formal bootstrap acceptance remain pending. This result does not authorize a game launch or establish colony survival.

Independent follow-up `ONI_B16_STRATEGY_REGRESSION_20260907_01` ran all 14 original pure-code strategy cases and exited 0. After importing the unchanged verifier, only its output-only `ROOT` variable was redirected to a new report root; test functions, fixtures, and semantics were unchanged. Strategy, state-quality, field-contract and verifier hashes were identical before/after. Report: `reports/b16-strategy-hotpath-20260907/runs/ONI_B16_STRATEGY_REGRESSION_20260907_01/reports/strategy-verification.json`, SHA-256 `926298E53787AB12D11D32437B1597ED99BF52CD8BA785FAFD168789A067B21A`. This pure-code run created no child process or game/queue fixture.

Next independent gate: approval lifecycle custody integration. The A09A3 baseline run separately reproduced a cross-process lifecycle-lock bypass and an unexpected `approval-recovery` root entry; its failure report is retained under `reports/a09a3-approval-custody-integration-20260907T043520376420Z-37684-f03f366fd4/report.json`. Repair and regression are required before live admission.
