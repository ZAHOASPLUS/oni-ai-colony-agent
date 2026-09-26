# A09B C# confirming custody audit — read-only delivery

Luna Max `/root/a09b_csharp_lock_boundary_audit` delivered at 2026-09-07T07:22Z.
No source edits, import, compilation, test, fixture or game execution. Its four
protected hashes matched the values in recovery-command-20260907-a09a9.md.
The following are source findings and a proposed test boundary, not proof of
cross-runtime correctness. Root received this at its red-context seal; full
acceptance of a future implementation remains pending.

## Exact local surfaces

- `ActionExecutor.BeginSession` lines 70-80 calls `QuarantineStartupCommands`
  at 108. Confirming enumeration starts at 135, read at 144. It can finalize
  to done at 191-200, recognize a prepared transaction at 226-244, finalize
  to failed at 284-292, or move malformed input to failed at 296-297.
  A lock only around deletion leaves the earlier stale read unprotected.
- `VerifyPendingCommands` at 2382 enters confirming through `FinalizeCommand`
  at 2476-2484; mutating commands target Confirming, others Done. Source path
  originates in the executing claim at 448/466 and PendingVerification.Path
  at 715-732. `MoveCommandWithPayload` at 3037-3077 delegates executing to
  ExecutionClaimStore at 3048-3060. Other sources write a target at 3063-3067
  then test/delete current source at 3068-3071.
- `PollWitnessConfirmation` 2311-2379 reads done and scans confirming at
  2321-2359. `HasProcessedId` 3119-3159 scans confirming at 3126-3149 and is
  called from command validation at 842. These reads affect mutation decisions
  and must not be silently treated as coherent snapshots.
- `PreparedConfirmationRecovery.TryRecognize` 22-141 validates journal,
  certificate, hashes and claim. `WasRenamedToDone` 148-176 observes path
  presence. Neither makes ordinary ReadAllText/Exists atomic or provides
  lifecycle mutual exclusion. FileShare.None on a temporary payload is not
  the Python OS byte-range lifecycle protocol.

## Minimal candidate and test facilities

Candidate: a Unity-independent filesystem custody helper. Hold the same OS
lock across startup confirming enumeration/read/recognition/movement, across
the executing-to-confirming transition with revalidated source ownership,
and around coherent confirmation reads. Do not lock Unity CaptureCurrentState,
ExecuteCommand, or the entire executor. Exactly which read scopes can use a
short bounded snapshot still needs implementation review.

Existing harness: `tools/ExecutionClaimHarness/ExecutionClaimHarness.csproj`
lines 3-17 targets net8.0, references `$(ONIManagedDir)\Newtonsoft.Json.dll`,
and links ExecutionClaimStore, PreparedConfirmationRecovery and
CanonicalHashContract. Program.cs lines 20-33 is the case entrypoint and
317-350 creates layout fixtures. It currently does not exercise lifecycle
locking or cross-language interaction.

Existing Python references: verify_command_custody_core.py lock-concurrency
and crash-release cases, and verify_approval_custody_integration.py spawn,
Pipe and held-lock samples. Reuse their safety patterns, not their reports
as evidence for new C# code.

Required future interoperability test: fresh isolated Temp with exact lock
header and valid command A; actual C# holder blocks Python contender, then
actual Python holder blocks C# contender. Sample command bytes/native identity
throughout each hold. Verify no mutation during hold, normal success after
release, bounded timeout failure, and owned-process crash release in both
directions. Forced termination is an intentional crash test only, never a
normal-success substitute. Preserve hashes, return codes, exact child PIDs,
cleanup and no duplicate owners. This has not been run.

## Remaining choices and restrictions

ProbeRunner.Update lines 95-107 calls ActionExecutor on the Unity main thread;
session lifecycle is synchronous too. A five-second lock wait could stall
the UI. Prefer a short bounded try with fail-closed latch, source/claim
preservation, no further mutation and no replay on conflict. An independent
pure filesystem worker is a separate design option, not yet selected.

Three unresolved points: FileStream.Lock versus Python CRT byte-range lock
interoperability; native Windows identity mapping to Python st_dev/st_ino;
main-thread short timeout versus pure file worker. Local source inspection
alone does not prove any of these. No production repair has been dispatched.

## Root documentation check, 2026-09-07T07:40Z

This is a source/documentation constraint, not an executed interop result.
Fresh local inspection reports Python 3.12.10 (MSC v.1943, AMD64), the Mod
project targets netstandard2.1, and ExecutionClaimHarness targets net8.0.
A net8 harness alone cannot establish behavior inside ONI's Unity runtime.

Microsoft states that an exclusive byte-range lock also rejects access through
a second handle opened by the same process. This supports the verifier repair
contract's metadata-only observation of the exact held lifecycle lock; command,
journal and certificate files still require full bytes and identity checks.
See [Windows byte-range locking](https://learn.microsoft.com/en-us/windows/win32/fileio/locking-and-unlocking-byte-ranges-in-files).
The CRT locks from the current file offset; `_LK_NBLCK` returns an error when
unavailable. Do not substitute the roughly ten-retry `_LK_LOCK` behavior for
the explicitly bounded protocol. See [CRT locking](https://learn.microsoft.com/en-us/cpp/c-runtime-library/reference/locking?view=msvc-170).

Python 3.12 Windows st_ino may be 128 bits, and must not be truncated to a
64-bit C# integer or floating-point JSON number during cross-runtime identity
comparison. See [Python stat_result](https://docs.python.org/3.12/library/os.html#os.stat_result).
FILE_ID_INFO supplies a volume serial and a 128-bit identifier; whether these
map exactly to the installed Python runtime remains a future empirical check,
not an assumption. See [FILE_ID_INFO](https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_id_info).

The current Python lifecycle acquisition already retries open/header
PermissionError/EACCES/EBUSY/EPERM within its deadline (command_custody.py
lines 499-513). Therefore a potential second-handle header read conflict is
not by itself a newly confirmed production defect. No core change is justified
from that observation alone. Preserve the current accepted source until a
specific isolated regression demonstrates a need. Cross-language blocking,
crash release, native identity mapping and Unity latency remain unverified.
