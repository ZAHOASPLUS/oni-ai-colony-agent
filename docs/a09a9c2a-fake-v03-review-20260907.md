# C2a fake helper v03 independent rejection and repair boundary

Action A09A9-C2A-HELPER-V03-REVIEW-SEAL-01, 2026-09-07T12:44Z.
Root 01a07bd2-da7d-7170-bfae-85bc87f7c69c, actual Astra Ultra.
Independent fake-resource audit /root/c2a_retry_readonly is terminal.
This is a static review plus independently read failed execution evidence.
No fake acceptance, Windows/production/live admission or complete-goal claim.

## Frozen reviewed inputs

Directory: reports/a09a9c2a-fresh-fake-20260907T122715Z-pid-45864-nonce-34bcda8fcacd4bb2/.
Archived helper: helper-version-03-A3F1AF564ADF51290129D45C95EA5902158C3676DA3F541651B02F3F4A1905CD.py.
Actual bytes 66050, SHA256 A3F1AF564ADF51290129D45C95EA5902158C3676DA3F541651B02F3F4A1905CD.
Target source remains 801DE95402039CACFCB40EB0E1761ACC7E2ECECE0488469D636D1DBE204724A4.
Doc remains 6FAB6C0E8E143E31B7034EE738C5BDC2EE63A697A3C69857288A00B46726031C.
The immutable full contract, supplement and six-source hashes remain in
recovery-command-20260907-c2a-801de9-fake-next.md (3F5DB64068F7813DBA268B96F7BE97879FF9E614C98152B95671204D36AFF0D8).

## Target extraction and resource blockers

Actual AST functions/classes are selected, including _spawn_worker,
_PipeReader, _worker_protocol, C1 _worker_evidence_valid and new provenance
dependencies. However globals are incomplete: ns lacks hashlib, and
FakeSubprocess lacks PIPE (v03 828-854 and 689-700). The first actual
acquisition fails in _sha256_bytes before any child; a subsequent PIPE
AttributeError would follow if only hashlib were fixed. Audit globals and
fake API dependencies before another run; unrelated errors are harness bugs.

FakeProcess.poll (497-506) changes None to exit 0 without an exit event;
kill_effect=False is also overridden by poll after killed=True. Poll must
observe state only. Explicit transitions must distinguish child exit,
kill effect and reaping, with operation effects before/after exceptions.

default_output preloads ready/calling/result; FakeThread.start synchronously
runs the entire real reader and an empty list becomes EOF (432-438, 540-556,
750-754). Thus result/EOF precede GO. Use an explicit fake scheduler with
ready after start, calling/result after GO, and EOF/inactive after writer-end
closure/drain. Every synthetic transition must be traced. Actual _PipeReader
start must run. If actual _read cannot run in that integration scheduling
model, document its exclusion and separately test it with finite synchronous
inputs. Never claim fake scheduling establishes real Windows behavior.

FakeHandle seek does not retain a cursor, read always starts at zero, modes
are not enforced, and fileno/fstat accept closed handles (371-412, 272-288).
Keep per-handle cursor/mode/closed checks. Preserve the existing good design
where fstat observes the held state object while path replacement binds a
different object. The oversize_handle case actually changes path size and
fails before reaching the intended handle-specific check.

Configured ident failures are never injected: FakeThread.ident is a plain
attribute (533, 546, 551); is_alive failure is not evidence of ident failure.
Exercise each designated getter and retain original and secondary errors.

The actual 20-second timeout is retained and sleep advances fake time, but
iteration counters are only recorded. Add independent finite iteration
limits/assertions and exact timeout-vs-early-exit distinctions. Do not shorten
source thresholds. Default exec builtins leave open/import capabilities in
the namespace; static inspection found fake bindings on intended paths but
does not prove a sealed I/O boundary. Restrict or explicitly guard real I/O.

## Root integration, oracles and report blockers

run_case manually invokes spawn/ready/go/finish and manufactures baseline and
pre-go exceptions instead of executing _run_locked_worker. Use the actual
integration function with declared inert lifecycle/session/snapshot/sample/
lock-header boundaries. The actual acquisition and cleanup remain unchanged.
An actual_protocol_function_ran assertion contains `or True` and is tautological.

verifier_passed depends on the positive case name; negative assertions demand
C1 false even though C1 checks only its narrow protocol contract. Compute
the same complete conjunction for every case: actual protocol plus actual C1,
complete reader evidence, zero exit, no primary/cleanup/reader errors,
no forced kill/overflow/stderr or residuals. Keep fault matching, verifier
evidence and physical cleanup independent. No case-name forcing.

Match exact fault/stage/effect/ownership/order, not broad words or any reader
error. ready_timeout currently passes with ready merely unset; retry marker
merely matching FileNotFoundError does not make the source defect accepted.
two_cleanup_missing_lifecycle_keys is omitted from the retry set and runs only
once. Missing final-getter cases, forced-kill retry and separate observation
faults must remain unexecuted/unaccepted until implemented to the full contract.

Every execution must first freeze actual helper bytes/hash, measure actual
source/doc/helper/six protected hashes before AND after, use exclusive unique
attempt/extraction/case/summary paths, and preserve all failures. Do not label
after-case measurements as before. Serialize explicit state rather than
live objects through default=str. Counter/assertion counts and UTC must be
actual observations. Distinguish the external process exit from JSON fields.

## Actual failed runs and current stop boundary

Root independently extracted only tool-result metadata from this new author's
rollout; it did not read old task histories. Four external process exits are 1:
- 12:38:20.130Z, chunk 7f5834: literal BinOp evaluation ValueError.
- 12:38:57.031Z, chunk 0c39b9: Trace.add operation argument TypeError.
- 12:39:28.233Z, chunk 200e6a: ast-extraction.json FileExistsError.
- 12:40:27.733Z, chunk 8314db: report-producing run, outer PID 8684.

Last run files use tag 20260907T124027441076Z-pid-8684. Its summary stopped at
one case; positive_complete records NameError hashlib, fault_match=False,
verifier=False, actual C1=False, cleanup_complete=True. That cleanup value
does not prove successful acquisition; no retry or full matrix ran. Original
two failed helper versions were not retained and cannot be claimed reproducible.
The v03 snapshot and all existing reports must remain immutable.

The Luna author 01a07bd4-b319-7140-ab9d-2a3b826de4d2 was instructed to finish
only any already-sent atomic tool, seal hashes/failures and return terminal.
No replacement writer is admitted before terminal/no-pending confirmation.
After that, the user-authorized abnormal-complexity routing permits a new
short Astra Ultra helper author. It receives a new exclusive directory and
may repair/copy only helper evidence, never source/doc/protected/historical
files, and may not delegate. This document itself is not a dispatch.

Run a complete positive first, then prioritize the precise marker retry
counterexample from a09a9c2a-801de9-retry-static-review-20260907.md (E3ACE923890BEBAEA505A3618E61324A1D69CE4ACA4B1A3A78921F02E7CB0B32).
On a real source defect stop, retain evidence and return to root for a
separate hash-guarded source action. Mark all remaining cases unexecuted.
This ordering does not reduce the eventual 25 exact labels, A-D, cleanup and
retry matrix. The complete survival objective and all later gates stay open.
