# A09A6 boundary verifier repair

Date: 2026-09-07 (Asia/Shanghai)

This note records the verifier repair only. It is not an acceptance result. The
pre-repair Spark artifact was copied create-only to:

`C:\Users\user\Documents\ONI\reports\a09a6-boundary-verifier-20260907T045735791Z\preimage\verify_approval_boundary_safety.py`

Its SHA-256 was
`D4B7CC49962032235DD134203BE01680177C4F3F6CED091877C8D8CDF6D3C743`.
Any older report whose `passed` field is true is historical evidence only and
must not be used as acceptance evidence for this repair.

The repaired verifier is deliberately offline-only. It adds a valid approval
baseline before the recovery race, records source and loaded-module identity,
uses create-only report roots, writes the report after temporary-root cleanup,
and makes runner failures fail closed. Case A uses an `ExitStack` so the owned
spawn child receives `release_event.set()` and is joined before its temporary
root can be removed. A non-zero exit, runner termination, residual/unknown
child, setup/status error, zero assertions, cleanup failure, source drift, or
module-path mismatch cannot be reported as passed. Intermediate lock-window
inventories are retained as hashes and small diffs rather than repeated full
inventories.

Case B injects the real second `os.replace` failure and wraps the real recovery
directory creator after creating a foreign root and sentinel. It requires
`ApprovalOutcomeUnknown`, unchanged foreign identity/content, no foreign claim
or ambiguity file, and preservation of this transaction's approving temporary
files. Case C tests the production no-op cleanup policy directly: a replaced
empty directory and its original sentinel are retained, and a newly owned
empty directory is retained. It does not claim an untriggered after-check race.

No fixture, regression, child process, game, real Bridge, queue, save, Mod,
config, or network action was run while preparing this note. The only authored
source change is `scripts/verify_approval_boundary_safety.py`; the preimage
above and this document are the explicitly requested evidence artifacts.

## Future exact commands

Run the verifier only from the project root after the production source freeze:

```powershell
Set-Location -LiteralPath 'C:\Users\user\Documents\ONI'
python .\scripts\verify_approval_boundary_safety.py
```

For the permitted preflight-only syntax check (no imports or fixtures), use an
in-memory compile rather than running the verifier:

```powershell
Set-Location -LiteralPath 'C:\Users\user\Documents\ONI'
python -c "from pathlib import Path; compile(Path(r'.\scripts\verify_approval_boundary_safety.py').read_text(encoding='utf-8'), r'.\scripts\verify_approval_boundary_safety.py', 'exec'); print('in-memory compile: ok')"
```

Acceptance must use the newly emitted create-only report and verify its source
hashes, module paths, UTC timestamps, `offline_only=true`,
`game_launched=false`, cleanup gate, normal-child gate, and all four expected
case records. Do not infer acceptance from the current repair turn because it
has intentionally not executed the future command.
