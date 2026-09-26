# A09A9 repaired verifier — root review still blocks execution

2026-09-07T07:59Z. Worker **ONI A09A9 验证器证据边界修复** completed normally
at 07:56:30Z after 1194.113 seconds. Thread
01a07acc-2a81-7e91-8a99-e242dc0f1898, host local, terminal wait cursor
acf8a26f-e253-4842-bc17-2836c3aeed0b:46. Do not resume that long author or
send a duplicate implementation request there.

Root independently matched final hashes:
- scripts/verify_confirmation_custody_integration.py:
  F627E61AE937EA7B8815D5138E671AF8BC0D47DB3D8F440E57CA569AA5A96DFB
- docs/a09a9-confirmation-custody-verifier-20260907.md:
  7950A381E3FAF6318147DDA4AB2C4785F411713F45B6F16C6D5A1916AD90C984

The two exact original preimages remain in
reports/a09a9-verifier-repair-20260907T073942488Z-pid34804-b95fcbab/preimage/,
hashes 236E32229162B089B668D1B92D844993BF22937A32D039C0E1761236E44E586B
and 3344E1123635790217F5EEFDD32F8947B14F2205FB55314C5C009EA8023FE1B2.
Root independently rechecked all six protected production/old-verifier hashes
at 07:56:50Z: unchanged. Worker reports in-memory AST/compile passed,
source UTF-8 size 78362 bytes, no import/test/fixture/child. Root has not run
the verifier CLI or a production fixture. The root pure gate diagnostic below
does not upgrade that boundary.

## Positive progress retained

The patch implements before-go baseline capture, metadata-only lifecycle lock
sampling, stricter final owner/path checks, normal-first short circuit, worker
output collection/protocol evidence, Temp scope validation and per-case body
assertion checking. This is not a blanket rejection or a request to rewrite
the 1790-line script. Preserve the implemented safety work and exact four names.
Root is reviewing the final source, not the invalid Git comparison emitted by
the author in this non-Git workspace.

## Confirmed pure-gate false acceptances

Action ONI_A09A9_PURE_GATE_REVIEW_20260907_01 extracted ONLY the _gate AST
function, hash guarded it, and supplied synthetic report dictionaries. No
agent/module top-level, fixture, command queue or worker ran. Process exit 0
means the diagnostic finished, not safety pass. Persisted results:
reports/ONI_A09A9_PURE_GATE_REVIEW_20260907_01.json.

Three negative inputs were incorrectly accepted by every gate check:
1. A lock worker's reader_errors and reader_alive replaced by empty mappings.
   all(...values()) is vacuously true (around 1505-1506). Require exact stream
   keys and strict values, not merely all values that happen to be present.
2. B positive-control final_state replaced by {}. The current gate checks only
   Mapping type (around 1477-1481), not the actual final-state predicates.
   Require complete real control evidence: no errors, valid equal certificates,
   confirmed outcome/no blockers, one canonical confirmed done owner, no
   canonical/other confirming source, valid certificate commit; also verify
   recorded idempotent control results and successful B cleanup.
3. Move the commit lock worker record to the normal case. Total worker count
   remains two, so the gate passes (1483-1511). Bind each required worker to
   its exact lock case and operation/command_id; no worker may substitute for
   another case's missing proof. Require complete stream/cleanup/protocol
   evidence for each. Do not accept a cached protocol_valid flag in isolation
   if its underlying required proof is missing.

Useful negative results: zero body assertions and executed=1 were correctly
rejected. Keep those checks. The synthetic shape control is NOT a valid real
runtime report and must not be cited as a production/control case pass.

## Remaining source-level inventory gap

_command_locations at 351-376 still continues past non-.json entries, and
_read_json accepts any object; a JSON object with missing/invalid id is silently
treated as a nonmatching command. This does not satisfy the original explicit
contract to fail on protected-stage garbage and inventory valid nonmatching
IDs. Require all entries to be canonical plain command records, validate ID
and canonical filename, and fail on unsupported material. Preserve legitimate
nonmatching IDs after validation; do not simply delete or ignore them. Keep
actual target-ID total-owner counting and the no-confirming checks.

## Pending independent narrow review and next boundary

UPDATE 2026-09-07T08:05Z: the narrow audit is now complete; no active code or
audit worker remains. Root received its findings and reread the affected
protocol/spawn/run-info/case sections. Root also ran one additional pure AST
gate shape control, still with no production imports/fixture/child.

**Certain normal-path blocker:** _run_locked_worker returns operation,
command_id, sample and nested cleanup (1029-1039). PID/returncode/readers/
protocol fields live in that cleanup. _gate's worker_evidence_complete and
protocol/output counters instead read them from the outer run-info mapping.
The actual-nesting synthetic shape control fails worker_evidence_complete
and no_worker_protocol_failure even with successful cleanup fields. The
earlier flat synthetic control therefore tests a shape the runtime does not
emit; keep that diagnostic honestly labeled and add a positive control using
the REAL nested shape before claiming the gate repaired. Read each exact
lock case's run-info and then its cleanup, consistently in all counters and
predicates. Do not flatten the runtime ad hoc just to keep a wrong gate.

**Certain failure-cleanup hole:** _spawn_worker protects only Popen itself.
After Popen returns (668-686), construction of stdout/stderr _PipeReader at
696-697 occurs before the worker dictionary is returned. If construction
raises, _run_locked_worker still has worker=None and _finish_worker(None)
cannot close/wait/kill or report that actual child. Retain ownership/PID and
partial resources immediately after spawn; a constructor failure must run
bounded cleanup, retain exact exit/reader/marker evidence and fail explicitly.
Do not start a real child in the next authoring task; root will exercise this
once the corrected helper boundary is independently reviewed.

The audit confirms before-go held-lock sequencing, bounded normal shutdown,
no close of a live reader, and post-release header reading. It additionally
flags missing stream mappings in _lock_case itself (1083-1084), not just
_gate: use exact stdout/stderr maps in BOTH consumers. It recommends explicit
marker disappearance evidence (cleanup currently silently accepts absent
marker at 902-910), complete op/id binding in all three protocol events, and
nonempty/structured baseline validation. These are static evidence-hardening
points, not observed runtime attacks. Child _module_attestation genuinely
executes before emitting ready; the unused parent _module loop variable alone
is NOT proof of a shadow import or an independent blocker. No new production
defect was inferred from it.

The next correction scope should be small and explicit: actual nested
per-case gate/positive evidence predicates, strict stage inventory, and
exception-safe partial worker initialization/cleanup. Align protocol/marker/
sample evidence checks locally where needed; preserve all existing cases,
timeouts and source/import rules. Require pure AST positive shape control
plus each recorded negative to be evaluated, but no production fixture/import
or worker in authoring. Do not repeat the old seven-group rewrite or ask the
132k+ previous author to keep implementing. Use a fresh available Spark or
Luna Max task after a fresh root context and transport check; first preserve
F627E61A... / 7950A381... as the NEW preimage rather than overwriting the
earlier 236E3222... backup. Root reached 154,680 input tokens during this
review, above the AGENTS 150k red threshold despite the health script's
yellow label, so no new multistep correction/test was launched in this turn.

Read-only Luna agent a09a9_repaired_worker_audit completed its sealed worker
reader/protocol/held-lock and worker_main review. It made no edits/execution.
Its terminal report has now been collected, as recorded above.
Root has not accepted worker cleanup paths yet. Do not start baseline/production
repair while these findings remain unresolved. The next implementation must
be a fresh short task, preserve this exact repaired revision as its new preimage,
and patch only concrete remaining functions, not repeat all seven repair groups.

Future production compatibility constraints are separately captured in
docs/a09a9-confirmation-compatibility-contract-20260907.md. C# documentation
constraints are in docs/a09b-csharp-lock-boundary-audit-20260907.md. They are
not authorization to expand this verifier fix. No real saves/Mod/config/queue
were inspected or mutated, so their current hashes/emptiness remain unverified.

Rollback is bounded to the two verifier files from the applicable verified
preimage after checking no intervening edits. Do not revert accepted A09A8/B18,
delete any old reports, start game/Bridge, or change performance/safety gates.
