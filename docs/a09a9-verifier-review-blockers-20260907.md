# A09A9 verifier review — not accepted for execution

Action ONI_A09A9_REVIEW_BLOCKERS_20260907_01, 2026-09-07. Root completed a
read-only review of the entire delivered verifier at SHA256
`236E32229162B089B668D1B92D844993BF22937A32D039C0E1761236E44E586B`.
No verifier, fixture, child or production mutation was run. These are source
findings, not observed runtime failures. Do not run this revision as accepted
safety evidence. Preserve it as the exact preimage for a bounded test-only fix.

The real workspace imports, exact four names, ordinary A control, independent
valid B construction with regenerated receipt/history, one-shot A-scan/B-swap
hook, source hash stability, exclusive report writes and expected fail/exit 1
behavior are useful. Keep these; do not rewrite for line count or change any
production/old verifier file.

## Required local fixes before baseline execution

1. **Capture the protected baseline before go.** `_run_locked_worker` sends
   go around line 722, then `_sample_while_locked` captures the initial tree
   at 590. An unprotected child can mutate between those steps, making the
   first snapshot already contain the bad state. Early-child-exit detection
   does not restore the missing before evidence. Under the held lock, after
   ready but before go, capture the baseline and pass it to sampling. Preserve
   every held-period sample and all errors; do not reduce to final state.

2. **Do not read the held lock byte through the ordinary tree reader.**
   `_protected_snapshot` traverses all of commands (276), so `_tree_records`
   reads `.mutation-lifecycle.lock` with path.read_bytes while parent owns the
   OS byte-range lock. This is a different handle and can conflict with the
   Windows lock protocol; no runtime claim has been made yet. Observe the
   exact known lifecycle lock by metadata/identity while held, with explicit
   regular/single-link/non-reparse checks and header verification outside the
   held interval if necessary. Keep full raw-byte hashing for every command
   stage and journal/certificate. Do not suppress general read errors or
   exclude whole directories. Use the real LIFECYCLE_LOCK_NAME constant.

3. **A failing normal control must prevent attack execution.** `run` currently
   executes all four callbacks in one list comprehension around 1175. Run the
   normal case first; if it fails, represent later cases as not executed and
   failed/blocked, with exact names and reason. Do not call their callbacks.
   B-control failure must likewise keep the stale hook unexecuted, as intended.

4. **Prove unique ownership, not just one done file.** `_command_locations`
   around 316 silently skips malformed/unreadable JSON and missing stage
   directories; `_assert_final_state` around 388 only requires one done and
   no confirming, so an additional failed/rejected/other owner can be missed.
   The fixture inspector must fail on read/parse/enumeration errors and
   explicitly require one total command location, canonical done path,
   status confirmed, and physical absence of the canonical confirming source.
   Apply the same complete final-state checks to B's independent control,
   including root/session certificate equality, confirmed status and no
   blockers. Preserve nonmatching command IDs only if validly inventoried.

5. **Bound and completely collect worker output.** `_pump_worker` keeps an
   unbounded events list even though the reader queue/line length are bounded.
   Add a hard total event/output bound with explicit failure, not silent data
   loss. `_finish_worker` pumps before reader EOF/join and closes stdout/stderr
   while reader threads may still deliver the final result. After normal exit,
   boundedly join/drain readers before final result selection; record EOF,
   reader errors/aliveness, exact PID and cleanup. Ensure no deadlock on a
   malformed/oversized stream. The case/global gates must reject pipe overflow,
   protocol errors, worker_error, nonempty stderr, malformed/duplicate or
   mismatched ready/calling/result events, not merely accept result=[id].
   Record return code and forced-kill/residual state even on failed assertions.

6. **Enforce the worker's Temp-only write boundary before resolving aliases.**
   `_worker_validate_args` around 1237 accepts any absolute directory with a
   self-authored marker, does not require a system Temp child/prefix, and
   resolves before is_symlink, which hides a lexical symlink. Require an
   existing owned-scope `oni-a09a9-...` direct child of the system Temp root,
   inspect lexical path segments for symlink/reparse before resolving, and
   require resolved path equals the validated lexical path. Require marker
   inside that exact root, plain regular single-link, exact token/schema and
   binding. Do not touch a rejected root or launch a helper to test it. Keep
   workspace script path validation; attest actual module paths in child too.

7. **Tighten report entry and aggregate gates.** Require actual nonzero body
   assertions per executed case, strict executed=true, complete per-child
   evidence, no missing/error cleanup, and actual successful positive controls.
   Do not rely only on an aggregate positive assertion sum. Put Temp fixture
   allocation in the case's error-reporting boundary so generation errors
   produce an explicit failed case rather than losing the whole report. No
   exception/skip/missing case or changed source may pass. Preserve expected
   diagnostic failures as failures, not successful defect demonstrations.

## Next single action and verification boundary

After fresh root context and fresh transport preflight, dispatch one new short
Luna Max test-only repair task with this file plus the exact preimage hash and
two allowed paths: scripts/verify_confirmation_custody_integration.py and its
existing explanation note. Do not resume the 107k+ author for another feature.
First retain the exact delivered verifier/doc as hash-checked preimages in a
unique report directory. No production/old tests/C# changes, no fixture or
child execution. Only local edits and in-memory AST/compile, then terminal
hash delivery. No size rewrite or extra scenarios in this step.

Root then independently reviews the minimal diff, uses pure aggregate-gate
negative controls if useful (no production module/fixture import), freezes
sources and runs the four-case baseline once. It should show a real valid
control and explicit production failures, not test setup errors. Only then
dispatch the actual custody implementation. This is a prerequisite evidence
repair, not a substitute for the production or cross-language goal.

Rollback is only the test file/doc to the exact preimages after verifying no
intervening edits. Keep all failed reports; do not revert A09A8/B18, use real
queues, start game/Steam/Bridge, alter Mod/config, or change performance gates.
