# A09A9 confirmation custody — baseline and bounded handoff

Status at 2026-09-07T07:05Z: legacy behavior baseline passed; the new race
verifier is being authored, not run. No production repair or live admission.

## Current source findings

Root independently reviewed the relevant Python and C# paths after a separate
Luna Max read-only audit. `agent/action_outcomes.py:1367` performs commit without
the shared lifecycle lock. It scans command locations, prepares confirming
with `write_json_atomic` at 1448, directly unlinks at 1425/1455, and uses ordinary
rename at 1461. The source observation is not bound to raw bytes, file identity
and mtime immediately across those writes. The core public transition remains
limited to pending-to-rejected/quarantine; it cannot simply be called for done.

`mod/Source/Commands/ActionExecutor.cs` startup recovery enumerates confirming
at 135, later passes the previously read payload into failed finalization at
282, and non-executing `MoveCommandWithPayload` writes a target then deletes
the current source path at 3063-3067. No use of `.mutation-lifecycle.lock` or
the Python OS byte-range protocol was found in the relevant C# consumers.
Python locks byte [0,1) on that persistent lock file, with an exact header and
identity checks. Cross-runtime interoperability and C# coverage are untested;
no claim is made that a Python-only lock can serialize these C# writes.

The resulting overwrite/loss scenario is still a source-grounded inference,
not an executed race reproduction. Preserve this distinction until the new
fixture actually runs. Existing valid prepared/legacy-duplicate recovery must
not be weakened to make a future test pass.

## Fresh legacy baseline

Action `ONI_A09A9_LEGACY_BASELINE_20260907_01` invoked both unmodified verifiers
with new exclusive report destinations. Both normal Python processes exited 0.

| Existing suite | Result | Report | SHA256 |
| --- | --- | --- | --- |
| Confirmation transactions | 4/4 | `reports/ONI_A09A9_CONFIRMATION_BASELINE_20260907_01.json` | `A8ADA1AE10D838F2C33FBCFDCDDB9FE23D9043E0187939A8E63EBE2E1BE8FF1B` |
| Action outcomes | 9/9 | `reports/ONI_A09A9_OUTCOMES_BASELINE_20260907_01.json` | `6BCC99842026033B84423B9323BC146717C548C9916FE79543244ABECEFDEDB1` |

The protected files below remained unchanged through those runs and the
2026-09-07T07:05:15Z recheck. Neither suite's Temp prefix had remaining roots.
No game/Steam/normal Bridge, original save/config or real queue was involved.

| Source | SHA256 |
| --- | --- |
| `agent/action_outcomes.py` | `C0FAB71E21F1E49CBE76927BF6CF4E16A7C0179BEDB290CC1EA11B4EA56060D7` |
| `agent/command_custody.py` | `6C0A0B995587CB18E243E4A96C56517FFE8A735923BCC5CEEDE750BA19E91763` |
| `agent/command_queue.py` | `4992E0605DCE7821AE8776E38AB30A2E1C805DD15F23C988E25488EFE4E8DEA1` |
| `agent/colony_plan.py` | `0E5B3D174699A1CC54F42CA749838D766176069C4B5F43AD2AD20B0D8D047ADD` |
| `scripts/verify_confirmation_transactions.py` | `157E191A5030BF0D64742AAA5575F7E845EDBAEFA7C5E4F338A3F442A7B09AC9` |
| `scripts/verify_action_outcomes.py` | `E2566D8B169D2815404CD3141A6BE5A5342BD6BF96DB396BEDE2F225B95C3152` |
| `mod/Source/Commands/ActionExecutor.cs` | `F5610424C56003C0C7AD1C458A103086557154C603574A159B3A561B17D4F378` |

## Active authoring task

Exact user-visible task: **ONI A09A9 确认归档竞态验证器**,
`01a07aae-0f94-7531-9451-25a0c8ec3a0b`, host local, Luna Max, ONI local
non-Git project `39050cea-0e20-4986-b951-04dfc6463680`.
Fresh transport preflight at `2026-09-07T07:01:55.7419479Z` was stable with zero
disconnects/new helpers. First real assistant/tool events were verified by
wait_threads; navigation returned true. It is genuinely active, not merely
dispatched. Turn `01a07aae-1099-7522-8b8b-6f0261bc168b`; latest cursor
`945c7ab5-e5ea-442b-bf7f-518bf320b5be:2`.

Authorized files only: new `scripts/verify_confirmation_custody_integration.py`
and `docs/a09a9-confirmation-custody-verifier-20260907.md`. No production or old
test edits. Only in-memory compile/AST allowed during authoring, no imports,
fixture/test/child execution. Do not duplicate or resend this task.

Required cases: normal commit positive control; separate real cross-process
commit and recovery calls while another process holds lifecycle custody;
scan-A-then-replace-source-with-valid-B race. Both normal A and independent
valid B must pass real publication/confirmation controls. Lock checks must
inspect all held-period samples, not just endpoints. Unexpected skip/error,
zero assertions, hash drift, timeout/forced kill or residue cannot pass.
Current defective source is expected to fail the new safety expectations;
that must produce status failed/exit 1, not relabel a reproduced bug as passed.

Next single action: wait for this exact authoring task, review final source and
gates, then freeze and run one baseline. No production repair is yet authorized
to the worker. After observed baseline failure, design a bounded journaled
transition and adapter repair with C# coordination separately accounted for.
Do not weaken existing semantics or change B18/Plan to simplify that repair.
