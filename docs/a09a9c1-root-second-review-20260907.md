# A09A9 C1 root second review: not yet accepted

Action A09A9-C1-ROOT-REVIEW-02, 2026-09-07T08:39Z. Root observed the only C1
writer terminal/idle at cursor 7facf0dc-7a9c-4d37-9d32-a291b9fc4d82:18.
Task ONI A09A9C1 报告门禁局部修复, thread 01a07aed-7bca-7d70-b5a6-6f5eea2a21be.
Luna turn 01a07af2-edbf-7740-bf48-16f5c6c788dc completed in 926.132 seconds.
Do not resume that accumulating context or duplicate it.

## Fresh source and evidence

- scripts/verify_confirmation_custody_integration.py:
  45AADC264E6E99489611E45AE8866155FE3443AFC95405BDD79540D7DF24CC32
- docs/a09a9-confirmation-custody-verifier-20260907.md:
  DE5101A212465AF3580A9C1D133742901D8FE320B91295D8C6746DDE8AA9CFF1
- All six protected files match the literal AUTHOR_BASELINE_SOURCE_HASHES
  dictionary in this hash-guarded script. Root independently rehashed them.
- Report directory (literal name, including empty nonce suffix):
  reports/a09a9c1-gate-20260907T081324Z-pid-41960-nonce-/
- _03 JSON: 20556BB2DB4FA336F1007AC054478A5CE2A7F56F5B20C292CDB762F6AE360BB1
  binds source 4F107A9C67D5035AE255B7B4A584551561EFAE4A92DE6187D5BBCA294C513606;
  1 synthetic positive and 21 negatives matched. Immutable historical subset.
- _05 JSON: C891C974E7E96CA418BB52CAFFD8FD0D0D7F96D297EC564E0C34829266E565BE
  module_sha256_before/after both 45AADC...; 4/4 matched, positive and exit codes
  1, -1, False. This proves the narrow exact-zero fix, not complete C1 coverage.
- _05 prior_coverage_cases names six tests absent from _03 results. Therefore
  those references and the author's final claim are incorrect. Do not edit
  either immutable report to hide this mistake; supersede with actual tests.
- _02 remains an explicitly failed positive-control report; 21 negative
  rejections there cannot establish correctness of an always-rejecting gate.

## Independent root pure-AST diagnostic (exit 0, no fixture or child)

Root hash-guarded source 45AADC..., extracted only _gate, _exact_stream_map,
_is_positive_integer and _is_canonical_done_state. Five pure dictionary-builder
functions were AST-extracted from the retained _03 helper; no module top-level,
production import, main(), report writer, real fixture or child was executed.
The source bytes were unchanged afterward. Fifteen inputs were evaluated;
9 matched expected status, 6 false acceptances remain. This diagnostic used
synthetic PIDs and reports, not actual runtime success or live admission.

Correct: nested-shape positive passes; changing only forced_kill=True,
residual_process=True, stderr_lines=['sentinel'], reader_alive.stdout=True,
reader_errors.stdout='sentinel', reader_eof.stdout=False, pid=True or
returncode=1 rejects. These six reader/cleanup faults had not actually been
present in _03 despite the author's references.

Incorrectly accepted (all gate checks true):

1. One worker cleanup.events=[] while cached protocol_valid=True remains.
2. normal body_assertion_count=True.
3. normal body_assertion_count='1'.
4. normal final_state.canonical_done_path and locations[0].path both absent.
5. normal final_state.total_count=True.
6. One worker cleanup_error key absent, mistaken for explicitly observed None.

## Bounded remaining C1 contract

Keep real nested run_info.cleanup, corrected strict-int exact-zero exit,
exact four names/order/executed, final-state predicates and strict stream maps.
Change only _gate and small no-I/O predicate helpers, plus the C1 explanation.
Do not change _spawn_worker, _finish_worker, _worker_main, _worker_protocol,
inventory, fixtures, production, thresholds or test execution logic here.

Require strictly integer/non-bool positive body and owner counts; do not cast
strings or booleans into evidence. Require nonempty string path evidence before
comparing paths. Required nullable safety fields must exist as keys; absence
is not successful cleanup. Verify protocol proof from ACTUAL producer schema
rather than protocol_valid alone: exact three mapping events ready/calling/
result, calling.operation, result.command_id/result, structured ready module
attestation and matching protocol summary. Do not invent operation/id fields
for events that _worker_main does not emit. Future producer improvements are C2.

The actual producer emits ready.module_attestation, calling.operation,
result.command_id/result. _finish_worker emits protocol {valid,stages,errors,
event_count}, events, worker_result/error and nullable cleanup fields.
_inspect_final_state emits canonical_done_path as root/commands/done/id.json;
the old _03 helper's commands/id.done.json is simplified synthetic data. Make
a NEW realistic positive builder, not weakened predicates. Module attestation
entries have expected_path, actual_path, sha256; author baseline hashes are
informational, not a permanent future-production acceptance whitelist.

New pure evidence must bind FINAL source/doc and independently measured six
protected hashes before/after. Preserve pre-patch failures and old reports.
Use exclusive report creation; never rerun helpers that write_text old _03/_05.
Require one full realistic positive, each of six reproduced counterexamples,
actual _03 21 negatives, three exit-code negatives, six omitted faults,
protocol sequence/op/id/result/attestation/summary faults, and missing required
nullable fields. Names/counts must come from actual results, not assertions
that another report supposedly contains them.

## Safety and next boundary

Root transport 08:38:25Z: stable, no recent disconnect or fresh proxy helper.
No network changes or live ONI/Steam/Bridge/command action occurred. Original
save/PNG and normal Mod/config/real queues have not been inspected or mutated;
current hashes/emptiness are unverified, not assumed safe.

C2 partial-spawn ownership review is complete but code is NOT implemented.
It requires immediate mutable ownership registration after marker/Popen and
before reader starts, partial-resource bounded cleanup, no live reader pipe
close and no uncertain go replay. Root will scope inventory/protocol/marker/
sample work and actual isolated-child validation separately after C1 acceptance.
Do not run the four-case verifier or touch production before those resource
safety blockers are repaired and accepted. Production confirmation commit/
recover still lack lifecycle custody repair; future diagnostic baseline
failure must remain a real failure, not be forced to pass.

Rollback only the next task's two exact source/doc preimages after rechecking
ownership/hashes. Do not reset accepted A09A8/B18 work, delete reports, remove
unrelated Temp directories, launch game, or change safety thresholds.
