# A09A4 legacy approval regression runner

Status: implementation plus in-memory syntax compilation only.  No fixture,
runner `main`, approval subprocess, game, Steam, Bridge, real queue, Mod, or
configuration was executed for this revision.  Execution waits for the parent
task's unified source-freeze checkpoint after the shared-lock adapter change.
The in-memory check completed with `memory_compile=passed` for the new runner.

## Scope

`verify_approval_adapter_regressions.py` calls the existing verifier functions
directly.  It does not call either verifier's `main`, rebind either module's
`PROJECT_ROOT` or `APPROVE_SCRIPT`, or add mocks.  The explicit case list is
the four `inject` boundaries, seven post-publish faults, and the unowned-lock
case from `verify_approval_transactions.py`, followed by the exact 18-case
tuple from `verify_approval_frame_binding.py` (30 cases total).

Every case receives its own `TemporaryDirectory`; failures are recorded and do
not stop later cases.  The report directory is allocated first with an
exclusive directory create.  `report.json` is written only after every
temporary directory has exited and its removal has been recorded, using
exclusive file creation so canonical or prior reports cannot be overwritten.

The source manifest hashes this runner, commit helper, command-custody core,
approval receipt, decision-frame archive, action policy, both original
verifiers, and the actual `APPROVE_SCRIPT` exported by the frame verifier.
The Python helper paths are parsed from that PowerShell script and hashed as
the helpers it launches.  A missing hash, parse error, or before/after source
drift forces `status: "failed"`, `passed: false`, and exit code `1`.

The report records UTC start/end, all case names/statuses/details/errors,
`offline_only: true`, `game_launched: false`, source hashes before/after,
source drift, and temporary-root cleanup.  Existing frame cases own the
lifecycle of any `subprocess.run(timeout=30)` child they start; this runner
does not claim a global process census or residual-process result.  The parent
task performs that separate census.

## Deferred command

After source freeze, from `C:\Users\user\Documents\ONI` run:

```powershell
python -B scripts/verify_approval_adapter_regressions.py
```

The command must produce a new
`reports/a09a4-approval-regressions-UTC-pid-random/report.json` directory and
must return `0` only when all 30 cases pass, cleanup is complete, and source
hashes are unchanged.  No report from that deferred execution is included in
this implementation-only revision.
