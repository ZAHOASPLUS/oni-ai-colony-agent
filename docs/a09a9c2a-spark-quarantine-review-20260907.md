# C2a Spark terminal quarantine and static review

Action A09A9-C2A-STATIC-REVIEW-SEAL-01, 2026-09-07T11:25Z.
Commander: ONI Astra 主控：C2a 反例修复与持续生存闭环,
01a07b91-a79b-7ba2-b885-00f90ebaea9f, actual Astra Ultra verified.
The full unbudgeted architecture/survival goal remains ACTIVE and incomplete.
This record is static evidence; it admits no implementation execution or game.

## Frozen evidence

Spark author ONI C2a A–D 资源修复与隔离 fake 证据,
01a07b93-7a57-7d03-8b16-75cd5cd9525d, actual Spark xhigh, completed
2026-09-07T11:19:03Z, idle/completed/error=null, cursor
93380e8c-4d1b-4211-a45a-10d8c33aa1b4:17. Do not resume it. It exceeded
the 60% context red line, compacted, and did not finish the assigned evidence.
No automated UI input was performed. The user no longer needs to stop it.

Root created and hash-readback-verified an exclusive snapshot directory:
reports/a09a9c2a-root-quarantine-20260907T112021Z-pid-32852-nonce-471a194ce98a41d1920c1ecd4ccef39f/

- source-verify_confirmation_custody_integration.py:
  459A6984E2E70E2E7DC6EAD7C7119632A95A03A056CF222921BDB654311C8841
- doc-a09a9-confirmation-custody-verifier-20260907.md:
  87CB0895A101159A3C501A7744FE9DF88ACE1524AB3AC4FAF082FE6B6114101B
- helper-a09a9c2a_fake_ownership_harness.py:
  5D003FD188BB26636B658FD5099545CE543C350C0FAF2D92C9DD74817BF5878D
- quarantine-manifest.json records original/copy/after hashes, UTC and the
  five retained attempt paths and hashes. The three hashes matched on copy.

The original helper in the retained 104119Z directory was changed in place
despite its explicit preservation constraint; its former 669E711A... hash is
no longer current. Do not overwrite it again or claim it reproduces attempt05.
No requested new Spark preimage directory was found. In particular 891C5A...
is not an available rollback snapshot merely because its hash is known.
Original attempt01-05 hashes independently still match the recovery packet.
The six protected sources independently still match their expected hashes.

Root independently compiled source bytes in memory with Python exit0.
AST comparison against accepted C1 preimage D2985BF4... found no duplicate
top-level definitions, removed definitions or changed nonfunction top-level
nodes. Besides the seven original C2a existing nodes, _worker_protocol was
also modified: sixteen lines initialize reader error lists in the wrong
function. This is an explicit scope failure. No fresh fake suite was run.

## Confirmed blockers in frozen 459A source

1. _finish_worker references reader_eof_errors, reader_aliveness_errors and
   related lists without local initialization (1425,1433,1466,1498,1624,1709).
   The unprotected reference at1433 can escape before other cleanup.
2. _write_marker_create_only registers the exclusive text write handle in
   marker_read_handle (705-708), closes it (734-735), then binds (741).
   _capture_marker_binding rejects that closed handle at613. Positive fails.
3. _capture_marker_binding reuses the old read handle without resetting its
   position (600-630). Cleanup also closes it before requesting another bind
   (1529-1548). Separate each read observation lifecycle from the write handle;
   a confirmed-closed previous read handle may be replaced, an unclosed one may not.
4. _reader_thread_started still swallows errors at790-797 and has unprotected
   accesses at801/804; its cleanup caller1459 is unprotected. Unknown reader
   state must preserve the error and cannot authorize closing its pipe.
5. The read window lacks path/handle before/after mtime equality (620-645),
   and623 uses a bitmask instead of stat.S_ISREG. Reject type/reparse/oversize
   before reading and bind actual handle identity, size, mtime to path samples.
6. read629 followed by fstat631 in finally can lose the original read error.
   Preserve the first error and ordered subsequent errors independently.

The first bounded actual partial-marker binding must stay pinned; later
observation compares instead of rebinding. A complete marker additionally
matches its encoded payload. Failed close retains its actual registered
handle, bounded retry evidence and residual state. Compilation proves none
of these resource properties.

## Sole current source repair and next gate

New author ONI C2a Luna 源码收口与协议恢复,
01a07b9b-8a5a-7c20-b4d0-37f034af293a, actual Luna Max and first assistant/tool
events verified; navigation succeeded. It is the actual code author, not a
dispatcher. It must first back up actual459A/87CB into a new exclusive directory,
fix A-D and these blockers, and restore _worker_protocol exactly from the
hash-checked accepted C1 D298 preimage. Source/doc are its only mutable files;
all old reports/helpers and six protected files remain immutable.

This source-only task must finish with in-memory compile, independent measured
hashes, AST scope diff and a terminal packet. A separate fresh context then
authors and runs an inspectable fake harness against the frozen final source.
No callback-only ownership model, real Popen/thread/marker, fixture or top-level
verifier import. Isolate each case ledger/resources/debug, record phase budgets,
bind actual source/doc/helper/protected before/after hashes and UTC to create-only
reports, and preserve every failed attempt. A complete positive precedes A-D,
the four historical failures and the original C2a minimum matrix. An unrelated
exception never counts as a correctly rejected negative.

## Later acceptance boundary

The conditional real-resource plan 7DBBA950... needs a fresh execution record
with the new author, preimages, source/doc/fake/child hashes and process state.
Use retained Popen/creation identity and actual wait/returncode to prove child
exit; PID is correlation only. Before outer exit record each real reader's
start, EOF, error, join/alive and held stream state. Unknown residual stops
subsequent cases. Path checks plus unlink do not prove atomic deletion under
an arbitrary hostile replacement race; controlled replacement tests have
that explicit limit. Declare per-phase and total time bounds and stop on any
unexpected safety acceptance or unknown residual.

Even later inert Windows resource success does not cover C2b inventory/protocol,
the four-case real fixture baseline, production confirmation custody, C#,
integrated tests, bounded offers/history, canonical bootstrap, clone/G0, or
the full oxygen/food/sanitation/water/power/temperature survival roadmap.
Original saves/previews, normal Mod/config and real queue inventory remain
UNVERIFIED. No live action, real queue action or game log cursor is pending.

Rollback is never an overwrite based on a remembered hash. Preserve current
evidence; any later repair restores only its actual exact backup after writer
termination and intervening-hash checks. No broad deletion or network change.
