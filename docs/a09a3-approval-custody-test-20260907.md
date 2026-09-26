# A09A3 approval/lifecycle custody integration regression

Status for this hardening revision: implementation plus an in-memory syntax
check only.  The parent task ran the preceding revision after B16 completed;
that earlier report is not evidence for this revision.  The current A09A4
production source change must reach the unified source-freeze checkpoint before
this revision is executed.  Until then, only `compile(source, filename,
'exec')` is authorized.

## Scope and safety boundary

`scripts/verify_approval_custody_integration.py` uses a fresh
`TemporaryDirectory` for every case.  It does not read the real Bridge, queue,
save, Mod, or game process, and it never launches ONI/Klei/Steam/Bridge.  The
child process is created with multiprocessing `spawn`, is communicated with a
`Pipe` plus `Event` handshake, and is joined through its own `Process` handle.
If a bounded join ever expires, only that handle is terminated and then joined
again; there is no global process kill.

The fixture comes from `scripts.verify_approval_transactions.prepare(root)`,
which in turn uses the real `verify_approval_frame_binding.fixture()` and
`prepare_bridge()` paths.  The child calls the production
`scripts.commit_command_approval.commit(args)` API directly.  No validator,
receipt check, `os.replace`, lock, or existing verifier `main` is mocked or
replaced.

The verifier records the actual queue bytes read through
`agent.command_custody.read_stable_bytes`, their SHA-256, file identity, mtime,
stage inventory, and command owners.  It hashes the protected source files
before and after the run and exits failed if they drift during the run.

## Cases

The cases execute in this order:

1. `valid_approval_baseline` commits one valid v3 approval and validates the
   committed receipt against the archived frame.  It asserts the target is
   `approved`, the pending source is gone, the command has exactly one owner,
   and no pending/later duplicate exists.  This is the fixture gate for the
   race case.
2. `lifecycle_lock_exclusion_race` starts a spawned child, receives `ready`,
   acquires the parent process's real `lifecycle_custody(root)` OS lock, then
   sends `go`.  A second pipe message, `commit_started`, is explicitly a
   pre-call handshake sent immediately before the child invokes the production
   function; it does not prove that execution entered the `commit()` body.
   During a bounded observation window the parent repeatedly reads inventory
   and waits on the pipe; it does not use a fixed sleep as the ordering oracle.
   Every collected hold snapshot is compared with the pre-lock stage inventory,
   commands-root inventory, and pending bytes/hash/identity, including snapshots
   containing a read error.  No outcome may arrive until the parent releases
   the lifecycle lock.  After release, the child must complete successfully
   with exit code 0, without runner termination or residual process, and leave
   exactly one approved owner.
3. `post_approval_lifecycle_reentry` performs a valid approval, then exercises
   a fresh `lifecycle_custody` acquisition and the public
   `recover_lifecycle_transactions(root)` inventory/recovery read.  Any
   `approval-recovery` or private-lock residue is recorded, not hidden.

On the current baseline, the race is expected to observe approval mutation
while the parent holds the lifecycle lock because
`commit_command_approval.py` owns only `.approval-<id>.lock`.  The post-approval
inventory read is also expected to expose the consumer-created
`commands/approval-recovery` root entry to the strict lifecycle layout check.
These are real failed assertions, not synthetic passes.

## Report and exit semantics

The default command creates a new, collision-resistant,
`reports/a09a3-approval-custody-integration-*` directory and writes
`report.json` only after every temporary root has exited and cleanup has been
checked.  It never overwrites an existing report root.  The report contains
`status`, `exit_code`, `case_counts`, per-case real assertions, source hashes,
timestamps, handshake/outcome transcripts, child shutdown/residual state,
temporary-root cleanup state, `offline_only: true`, and `game_launched: false`.

Default execution is the acceptance gate: it exits `1` while the known defect
is present.  The optional diagnostic command
`--expect-known-defect` exits `0` only when the baseline passes and both known
defect signatures are reproduced, but the report remains
`status: "known_defect_reproduced"` and `passed: false`; it never turns a
vulnerability reproduction into a pass.

If the baseline approval case fails, the race case is recorded as `blocked`
with no fixture commit or child process started.  This prevents an invalid
fixture from being misreported as a race result.

## Exact command to run after the B16 gate

From `C:\Users\user\Documents\ONI` in PowerShell:

```powershell
python .\scripts\verify_approval_custody_integration.py
```

For an explicitly named unique report root, use a new path that does not
overlap B16 evidence:

```powershell
$a09a3Report = Join-Path (Get-Location) ("reports\a09a3-approval-custody-integration-20260907-" + [guid]::NewGuid().ToString("N"))
python .\scripts\verify_approval_custody_integration.py --report-root $a09a3Report
```

Diagnostic-only mode is:

```powershell
python .\scripts\verify_approval_custody_integration.py --expect-known-defect
```

Before executing either command, confirm the parent task has announced the
unified source-freeze checkpoint for A09A4.  No command above was executed for
this hardening revision.
