# C2a independent fake acceptance contract

Action A09A9-C2A-FAKE-CONTRACT-SEAL-01, 2026-09-07T11:29Z.
Conditional authoring contract, not a dispatch or run. Freeze and independently
review the sole Luna source writer's terminal output first. Root must supply
actual final source/doc hashes and a fresh transport/ownership checkpoint.
Source changes and full fake authoring deliberately use separate contexts.

## Immutable inputs and bounded authoring

Read AGENTS.md and the latest root packet. No old task/rollout or full source
dump. AST-extract required nodes into an inspectable namespace; do not import
the verifier top-level, production modules or fixtures. Do not launch real
children, threads, markers, queues, Bridge, Steam, ONI or deployment actions.
Only read/hash frozen inputs and write new exclusive helper/report evidence.

Old helper/report directory is immutable:
reports/a09a9c2a-20260907T104119Z-pid-38432-nonce-0400b7b0ecfa427e9afe5d9cfaaa9048/
Its helper was modified by Spark and is no longer669E711A...; root froze the
current5D003FD1... helper in the root-quarantine directory. It is reference
material only, not accepted coverage. All attempts01-05 remain preserved.
Create a new direct reports directory with UTC, actual PID and nonempty nonce;
use exclusive writes. Never overwrite attempts, including your own failures.

## Implementation extraction and fake boundary

The old19 resource-node selection was _identity, _is_reparse, _sha256_bytes,
_new_worker_ledger, _ensure_worker_ledger, _worker_error_text,
_remember_worker_error, _capture_marker_binding, _write_marker_create_only,
_reader_thread_started, _reader_thread_alive, _reader_state, _PipeReader,
_pump_worker, _worker_marker, _spawn_worker, _send_worker_go, _finish_worker,
_run_locked_worker. This is a starting inventory, not the final dependency
closure: statically resolve all new helpers and _mtime_ns from the frozen AST.
Use actual _worker_protocol and its pure dependencies; do not replace it with
constant valid=True. Actual resource functions, especially _spawn_worker,
must not be replaced by a callback model or deep-copied prebuilt ledger.

Read actual MAX_CHILD_LINE, MAX_CHILD_EVENTS, MAX_CHILD_OUTPUT_BYTES,
MAX_CHILD_STDERR_BYTES, _REPARSE_POINT and CHILD_POST_RELEASE_TIMEOUT_SECONDS
from literal source AST. Never shorten the20-second source threshold to0.20.
PROJECT_ROOT/SCRIPT_PATH and sys.executable may use explicit fake identities.
Document every injected non-target boundary, including inert lifecycle
context, session path, snapshots, sampling and lock-header verification.
If _wait_for_worker_stage is replaced, exclude its implementation coverage and
still exercise actual resource acquisition/cleanup through _run_locked_worker.

Fake dependencies cover paths/stores, file handles, OS metadata, processes,
stdin/stdout/stderr, Thread, Queue, clocks and token generation. No fake helper
may reach the filesystem except controlled source/hash reads and report writes.
Each case owns a fresh namespace, ledger, resources, debug/error list and clock.
Keep handle/path identities independently mutable. Reads honor closed state,
mode, cursor and limits; fstat observes the actual held object and real fake
size/type/mtime. A fault must specify whether the operation took effect before
raising. Unknown close state is not automatically closed. Queue bounds and
Empty/Full behavior must be faithful to the tested branch.

Fake scheduling and synthetic EOF/aliveness inputs must be explicit in the
trace. Do not claim they establish blocking read behavior or real thread
scheduling. Test the actual _PipeReader body separately with finite synchronous
fake input if claiming its parsing/count/error branches. No silent settle()
routine may manufacture successful cleanup and then call it observed behavior.
Actual Windows EOF, blocking reads and joins belong to the later real stage.

## Required case matrix

Run a complete positive first; if it fails, preserve the report and stop that
attempt's subsequent cases. It must traverse actual acquisition and cleanup,
one go, valid protocol data, both registered fake readers inactive/EOF,
zero exit, no forced kill/errors/residuals, and owned marker removal.

Preserve these original25 exact coverage labels (first positive, rest faults):

```
positive_complete
popen_failure_after_marker
executable_validation_failure
partial_marker_write
marker_collision
marker_replacement
marker_missing
marker_oversize_preserved
marker_handle_close_failure
first_reader_construct_failure
second_reader_construct_failure
first_reader_start_failure
second_reader_start_after_alive
go_write_failure
go_flush_failure
kill_wait_failure
reader_alive
independent_pipe_close_failure
marker_cleanup_failure
pid_property_failure
missing_stdout_pipe
ready_timeout
ready_exception
baseline_exception
pre_go_go_failure
```

Add precise A-D counterexamples: read-close failure retained/retried; residual
read handle never overwritten; repeated successful observation uses a valid
read lifecycle; throwing reader thread/start/EOF/state/is_alive access retains
its original error and other cleanup continues; alive or unknown readers keep
their streams open; same-identity partial content change cannot rebind/delete;
path/open-handle identity mismatch; before/after mtime change; nonregular,
reparse and oversize handle/path states rejected before read; read plus later
fstat failure preserves both errors in order. Exact case granularity is the
author's implementation choice; report actual names/counts/assertions.

Assert outer ownership registration before each failure, exact injected error
and stage, cleanup order, independent resource continuation, at most one go,
no unowned kill/delete, serializability and expected remaining resources.
NameError or any unrelated exception is a case/harness failure, never a
correct negative. A missing reader remains incomplete evidence, not EOF=True.

Record every sleep/wait/join call and requested timeout. Fake time progresses
according to those operations, not arbitrarily on monotonic reads. Count loop
iterations, drain/wait/join attempts and consumed phase/total budgets against
unchanged source bounds. Finite virtual time alone is not boundedness proof.

## Report and root acceptance

Every report binds unique attempt/case identities, UTC start/end, actual
source/doc/helper and six protected file before/after hashes, per-case initial
and final resource snapshots, counters/budgets, exceptions and exclusions.
Expected dictionaries do not substitute for measured hashes. Record separately:
fault_expectation_matched, verifier_evidence_passed, cleanup_complete. Expected
launch/cleanup/forced-kill faults stay failed verifier evidence even if their
fault expectation matches. Assertions and case counts are actual measurements.

Run the new inspectable helper under python -B with a bounded process/session
handle and <=30-second polls. Preserve actual outer tool exit independently of
the report. On any source drift stop. A failed attempt may be repaired only
within its authorized helper scope and rerun to a new report. Source defects
return to root for a new hash-guarded source action, not surprise source edits.
Root independently reviews extraction/fakes, complete positive, every required
fault, actual hashes and normal exit before accepting fake-only evidence.

Fake success permits no game. Remaining stages: separately reviewed inert real
Windows resource matrix, C2b inventory/protocol, actual four-case fixture
baseline, production confirmation custody, C#, integration, bounded offer/history,
canonical bootstrap, verified original/clone/config/queue inventory, G0 and the
complete ongoing survival roadmap. The full goal remains active throughout.
