# A09A9 C2a partial worker ownership contract (conditional, not dispatched)

Action A09A9-C2-CONTRACT-SEAL-01, 2026-09-07T08:45Z. This is a root design
artifact only, not implemented code or permission to start another writer.
C1 remains active in ONI A09A9C1 门禁证据收口, thread
01a07b08-5e2e-7392-8e86-fe0c84d9647c. Do not modify its script/document while
that task is running. Root must accept C1, rehash its final revision, and issue
a fresh exact preimage/dispatch checkpoint before implementing this contract.

## Evidence boundary

The read-only Luna audit and root source review used the immutable preimage
reports/a09a9c1-gate-20260907T081324Z-pid-41960-nonce-/preimage_verify_confirmation_custody_integration.py,
SHA256 F627E61AE937EA7B8815D5138E671AF8BC0D47DB3D8F440E57CA569AA5A96DFB.
Root independently rehashed it at this checkpoint. C1 is changing gate
predicates, not the process creation/cleanup functions reviewed here. Line
numbers below describe that preimage; resolve by function names after C1.

Certain defect: _spawn_worker starts a child and then constructs two
_PipeReader objects before returning the worker dict. A constructor/thread
start exception leaves _run_locked_worker.worker=None. Its finally calls
_finish_worker(None), which cannot discover, close or reap that real child.
Popen failure handling also swallows marker cleanup errors, and validation
failure after marker creation is outside that cleanup try block. These are
local verifier defects, not observed live-game incidents.

Resource locations: _PipeReader 494-506 (constructor starts the thread),
_pump_worker 538-583 (assumes both readers), _worker_marker 636-647,
_spawn_worker 650-705 (Popen 668-680, readers 696-697), _finish_worker 832-967
(None branch 833-841, dual-reader assumptions 858-901), _run_locked_worker
983-1039 (worker=None 989, spawn 996, go sequence 1000-1005, finally 1021-1024).

## Allowed implementation scope after a separate root dispatch

Use the same verifier script, its explanation doc and one new exclusive
reports/a09a9c2a-<UTC>-pid-<pid>-nonce-<nonempty>/ directory. Back up the actual
accepted C1 source/doc first, not the older preimage above. Rehash all six
protected production/old-verifier files; extract expected hashes from the
accepted script's literal dictionary without importing that script.

Patch only partial resource ownership and bounded cleanup in _PipeReader,
_pump_worker, _worker_marker/_spawn_worker, _send_worker_go if necessary to
record uncertainty, _finish_worker and _run_locked_worker, with small local
support helpers. Do not rewrite the script or change lifecycle lock sampling,
fixture semantics, child protocol, inventory, case bodies, report gates,
production APIs, thresholds, CLI authorization or real queues. Inventory and
producer protocol improvements are distinct C2b work, not a surprise addition
to this resource patch. Any field additions must be evidence, not a way to
turn an actual launch/cleanup failure into a passed case.

## Ownership ledger

There must be one outer-reachable mutable ledger BEFORE fallible acquisition
steps. _run_locked_worker and the acquisition functions reference the same
object. Register the marker immediately when it has been successfully created
and register the actual Popen handle immediately on return, before PID
conversion, stream assertions or reader construction. Register each reader
object before its thread can start. Prefer separating _PipeReader construction
from start(); no background thread should become unowned because its
constructor has not returned. Do not rely on finding a process later by name,
PID-only rediscovery, directory glob or broad scan.

Track operation/id, phase, marker path/token/creation ownership, process handle
and PID, stdin and both pipe handles, both reader slots, ready status,
go_attempted and go_sent. Error state must survive exceptions in path/executable
validation, Popen, first reader construction/start, second reader
construction/start, ready wait, baseline capture and sending go. Sending is
attempted at most once: a failed write/flush may already have sent bytes, so
record uncertainty and cleanup; never resend go to make the test proceed.

Marker creation failure/collision does not grant ownership of an existing
path. Never unlink a marker simply because its name was proposed. A partial
marker write needs a known exclusive-create ownership record if it is to be
cleaned; failure evidence may not silently claim successful cleanup. Only
delete a marker whose recorded identity/content binding still establishes
that it belongs to this acquisition. Preserve replacement/unowned material
and report conflict. Missing an expected owned marker is observed as a fault,
not silently treated as successful removal. Root will review the smallest
safe implementation before allowing actual marker operations.

## Bounded cleanup and failure evidence

_finish_worker must accept a partial ledger and handle absent process/readers
without KeyError or joining a never-started thread. Cleanup should not itself
escape before dealing with the remaining owned resources. Preserve primary
launch failure and ordered cleanup errors; do not overwrite one with the
other. Live process/stream/thread objects stay internal. Report only JSON
scalars, mappings and lists; never deepcopy live resources into CaseFailure.

Order of cleanup:

1. Close owned stdin first. This lets a child still waiting for go observe EOF.
   Record close success/error; a pre-go failure is not a normal completed run.
2. Bounded pump/drain while waiting on the actual owned process. Pump only
   readers that exist; do not synthesize two healthy readers from absent slots.
3. If that process is still alive at the existing timeout, kill only the same
   owned Popen handle and wait with a bound. Record forced_kill, returncode and
   residual process; any forced kill remains a gate failure.
4. Drain/join existing readers with a bound. Check whether a thread was
   actually started before join. Preserve EOF, error, alive and dropped/output
   counts separately for each real stream. A missing reader is incomplete
   evidence, not EOF=True. No real sleeps or threads in author-side fake tests.
5. Never close a pipe still being read by a live reader. Close inactive owned
   streams individually and record remaining live-reader/pipe resources as
   cleanup errors. A stream close exception must not skip the other stream.
6. Validate and remove only the owned marker, preserving disappearance,
   replacement, deletion error and final presence evidence. Return a bounded,
   serializable failure result even if cleanup was incomplete.

Retain normal run_info and cleanup field names. In particular the EXISTING
producer uses reader_alive keys stdout_reader/stderr_reader; reader_eof,
reader_errors, reader_lines, reader_bytes and dropped_lines use stdout/stderr.
C1 is being corrected to honor that distinction. Do not accidentally revert
the fix by normalizing one side only. Partial evidence may use null/error
values that the gate rejects; it must not invent successful EOF or PID data.
Normal successful runs still require exactly one ready/calling/result sequence,
valid module evidence, zero exit, both readers done and no cleanup error.

## Author-side fake-resource tests only

AST-extract only the reviewed functions/classes and inject fake Popen, thread,
pipe, marker/path and clock objects; do not import verifier top-level or agent
modules. Every dependency capable of starting a process/thread, sleeping or
writing a filesystem path must be replaced or proven unused in this harness.
Run the actual extracted implementation under those fakes, not a separately
reimplemented ownership algorithm. A callback-only model of desired behavior
does not test the patch. JSON reports use exclusive open('x') under the owned
report directory. No helper may overwrite an old report.

At minimum cover normal complete ownership; Popen failure after marker;
first reader constructor and start failure; second reader constructor and
start failure with the first reader already registered; baseline/pre-go
failure; write/flush failure with go_attempted=True and no replay; timeout
kill and bounded wait failure; reader remains alive; pipe/marker cleanup
error. Assertions must verify exact resource registration before each failure,
outer reachability, cleanup order, per-stream state, no unowned deletion/kill,
no close of a live reader pipe, no second go, serializable evidence and failure
classification. Fake clock must prove loops terminate at their declared bound.

Use one clean positive control before fault cases, and retain all failed
attempt reports. Report actual names/counts/assertions and final source/doc/
six protected hashes before/after. Pure tests prove branch and ledger logic
only. They do not prove Windows EOF, process exit, cross-process locking,
real stream behavior or real game safety.

## Root-only next acceptance stage (not authorized yet)

After patch review and fake tests, root will define exact isolated real-child
failure cases in a direct unique system Temp directory, using only owned
marker/child PIDs and unchanged production files. Root will publish action_id,
fresh hashes/process preconditions, command/timeout, evidence and rollback
before starting. Each real-child report must retain original exception,
actual exit code/PID disappearance, EOF/join state, marker state and no residual
helper. Do not infer those facts from fake tests or app task completion.

The four-case confirmation-custody production baseline is still blocked until
resource safety and inventory/protocol evidence issues are accepted. Expected
production lock/identity failures remain diagnostic failures. No game, Steam,
Bridge, Mod/config mutation, save loading, original save/hash claim or live
admission follows from this document. Rollback is limited to the actual next
writer's two verified preimages; do not touch accepted A09A8/B18 or old reports.
