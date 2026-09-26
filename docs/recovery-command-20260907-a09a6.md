# ONI commander recovery packet — A09A6, 2026-09-07

## Delivery / verified-wait checkpoint — 2026-09-07T05:10Z

Entry/exception worker `01a07a39-65fa-7b80-99dd-06b9b169d0ae` is now terminal,
confirmed both by its local rollout (active_turn=false, last event 05:08:24Z)
and its delivered final answer. Final production hashes remain FC69BFE…E3582
and 8C045BAE…DBB6, exactly the full values below. The commander read its final
document `docs/a09a6-entry-fix-20260907.md` and independently reviewed both
diffs. It reports only compile/parser/static checks, not functional passes.

Boundary worker `01a07a39-f924-7750-b6b3-58ff3371f74d` remains an active turn.
At the 05:09:52Z sample its last real event was 05:07:47Z, input 96,223 tokens
/ 37.2%, green; it is generating its atomic replacement. Its old script has
been deleted as part of that replacement and the new Add File is not yet
available. Its exact old preimage exists under
`reports/a09a6-boundary-verifier-20260907T045735791Z/preimage/`.
This is NOT a startup-stall candidate: the worker has real prior events and
has not been reported terminal. Do not duplicate its writer or restore a file
while that handle remains active. Wait for the same worker, then review its
actual replacement and returned hashes. No new verifier/test was run during
this goal turn. The root remains in inherited red context (210,474 tokens,
81.5%, sampled 05:09:48Z), doing only this pending delivery/wait checkpoint.

## Follow-up review checkpoint — approximately 2026-09-07T05:07Z

The next goal turn inherited the long context (187,892 input tokens, 72.7%
at its first health check), so it continued only the pending read-only diff
review and this recovery-record step. It did NOT start tests or duplicate
workers. The prior turn was substantive progress; this turn also identified
and removed an unsafe proposed cleanup path through review feedback.

The exact local thread identities of the two existing collaboration workers
are now verified from their session metadata:

- Entry/exception worker `/root/a09a6_entry_and_exception_fix`:
  `01a07a39-65fa-7b80-99dd-06b9b169d0ae`, nickname Confucius,
  rollout `C:\Users\user\.codex\sessions\2026\09\07\rollout-2026-09-07T12-56-17-01a07a39-65fa-7b80-99dd-06b9b169d0ae.jsonl`.
  At 05:05:47Z it had fresh tool activity, 89,572 input tokens / 34.7%, green.
- Boundary verifier worker `/root/a09a6_boundary_verifier_repair`:
  `01a07a39-f924-7750-b6b3-58ff3371f74d`, nickname Dalton,
  rollout `C:\Users\user\.codex\sessions\2026\09\07\rollout-2026-09-07T12-56-54-01a07a39-f924-7750-b6b3-58ff3371f74d.jsonl`.
  Its latest tool event at the sample was 05:05:02Z, 72,077 tokens / 27.9%,
  green. It confirmed it was generating its single patch, with no blocker
  and no fixture/subprocess execution. Do not classify this as startup stall.

The entry worker has now applied and memory-compiled its production changes
and parsed PowerShell successfully. Its latest observed (not yet accepted by
functional tests) hashes are:

- `scripts/commit_command_approval.py`:
  `FC69BFE53956B981FF5ADB2CF2109B19D7BE564947F5E7EA32C5DA6DA68E3582`
- `scripts/approve-command.ps1`:
  `8C045BAE06230A3E2EBEC1E1DD6DF56DDB771E9D053BFB8C7EEB0059FE29DBB6`

The commander independently read both current diffs against that worker's
preimages. Python now preserves all referenced recovery/stage files on every
unknown outcome, proves safe pre-claim pending by single ownership plus exact
stable hash, rechecks recovery-directory ownership immediately before writing
ambiguity evidence, and rejects a cross-queue/different filename target before
directory creation. Its recovery-directory cleanup remains deliberately no-op.

PowerShell no longer creates `approved` outside custody or stages payloads
there. It uses the locally supported `Directory.CreateTempSubdirectory`, checks
the new directory is non-reparse, and writes marker/payload with FileMode.CreateNew
and an exclusive stream. The original proposed cleanup helpers were REJECTED:
they checked path/marker but not deletion-object ownership, so they could delete
a replaced file/directory. The worker removed those helpers and all automatic
Temp deletion. It now prints the retained Temp evidence directory explicitly.
This is intentionally incomplete automatic-cleanup functionality, not a pass
for arbitrary hostile parent-directory replacement. Do not reintroduce the
same check-then-delete routine or describe its removal as a full I/O security
proof.

The boundary verifier was still at its old D4B7 hash at the last root read;
its worker is generating the replacement. Therefore current production code
must NOT yet be called source-frozen for the planned combined test run. Next
single action remains obtaining both terminal deliveries and reviewing the
verifier's actual diff/exception/source/cleanup gates. Retained PowerShell
staging files are outside each legacy case's fixture Temp root: a future
30-case `temporary_cleanup.all_removed` must not be promoted to “all system
temporary evidence removed.” Capture and report retained staging separately,
and preserve it until a bounded ownership-proven cleanup step is designed.

This is the current compact continuation packet. The commander reached the
workspace red line (175,975 input tokens, 68.1% of a 258,400-token window) and
is closing its evidence-recording step. The overall goal remains active and
incomplete. Do not restart completed measurements or duplicate the two active
workers. This packet supersedes older execution-status notes, not safety gates.

## Goal and safety invariants

Use fresh trustworthy observations to control ONI through bootstrap,
stabilize, expand, space, and sustain, prioritizing oxygen, accessible food,
sanitation, usable water, delivered power, and temperature. Renewable supply,
reserve margins, independent fault recovery, and revocable evidence remain
required. Finite offline passes do not establish indefinite survival or a
perfect controller. The latest user routing is Astra Ultra command/acceptance
with Luna Max or available Spark execution. Main root is Astra Ultra.

Original cloud saves, previews, and normal Mod/configuration are immutable.
Only an explicitly named, verified test clone may eventually be loaded. No
live action is authorized by the offline passes below. Before mutations,
publish action_id, fresh preconditions, one action, expected evidence, and
rollback. If an action's dispatch is uncertain, inspect actual state before
any replay. No network/VPN/TUN/helper reconfiguration while turns run.

Run `scripts/test-codex-transport-stability.ps1` before long dispatches; the
latest root preflight at 2026-09-07T04:54:32Z was stable, with zero recent
disconnects and zero new transport helpers. That is time-specific, not a
future authorization. Keep commentary within 60 seconds and waits within
30 seconds. Read local rollout health when app summaries lag. Use apply_patch
for file edits. This workspace has NO Git repository and NO usable Git HEAD.
Use exact preimages and `git diff --no-index`, never `git show HEAD`.

## Current process, worker, Mod, queue, and log-cursor state

At the last fresh census during this packet, the narrow actual-process filter
found no ONI/OxygenNotIncluded/Klei/Steam process and no matching Python
approval verifier, multiprocessing child, or oni_bridge process. The 30-case
runner session 75958 exited normally with code 1; all 30 temporary roots were
removed. No real Bridge/queue/save/Mod/config contents were used or changed by
these root tests. Their current live hashes and current real queue emptiness
were NOT established. Do not turn that into a global live-cleanliness claim.

Two collaboration workers are currently authorized and own disjoint files:

- `/root/a09a6_entry_and_exception_fix`: Luna Max, fresh context. Only
  `scripts/commit_command_approval.py`, `scripts/approve-command.ps1`, its own
  preimages/evidence, and `docs/a09a6-entry-fix-20260907.md`. It may implement,
  memory-compile Python, and parse PowerShell; NO fixture/test execution.
- `/root/a09a6_boundary_verifier_repair`: Luna Max, fresh context. Only
  `scripts/verify_approval_boundary_safety.py`, its own exact preimage, and
  `docs/a09a6-boundary-verifier-20260907.md`. It may implement and memory-compile;
  NO fixture/test execution. It must not edit either production file.

Both were told to complete their current atomic edits and return final hashes,
not to redelegate or interpret the commander's context checkpoint as a need to
restart. Use collaboration status/mailbox first. Do not create duplicate writers.

Completed app workers, NOT to resume:

- `01a07a28-66c0-76b0-ae0a-0e3a03a85972`, “A09A4 approval lifecycle custody
  adapter”, Luna Max. Completed a 174-addition/24-deletion adapter and memory
  compile, then stopped at yellow. It initially tried to redelegate; both
  creation attempts failed, and it corrected its role. No duplicate writer.
- `01a07a34-1aa0-7c12-b4ab-33817478e971`, “A09A5 approval boundary safety
  repair”, Spark High. Finished after an explicit stop. Its tests/reports are
  NOT acceptance evidence; details below. No remaining active child was found.

Local rollout cursors for limited reconstruction only:

- Root: `C:\Users\user\.codex\sessions\2026\09\06\rollout-2026-09-06T05-59-36-01a07395-9037-7ad3-b943-90846568a715.jsonl`,
  around line 4537 at 2026-09-07T04:57:10Z. Do not load its full history.
- A09A4 Luna: `C:\Users\user\.codex\sessions\2026\09\07\rollout-2026-09-07T12-37-43-01a07a28-66c0-76b0-ae0a-0e3a03a85972.jsonl`,
  terminal turn `01a07a28-67c1-78d2-a770-aa930df98629`, completed epoch 1788756529.
- A09A5 Spark: `C:\Users\user\.codex\sessions\2026\09\07\rollout-2026-09-07T12-50-30-01a07a34-1aa0-7c12-b4ab-33817478e971.jsonl`,
  terminal turn `01a07a34-1b82-7ca3-b6b8-f5d83e285fcf`, completed epoch 1788756794.

One known Spark diagnostic residue remains, deliberately not deleted during
this context checkpoint:
`C:\Users\user\AppData\Local\Temp\oni-a09a5-pre-\f4d9a62f-2f63-469c-afcc-85bc95309e64`.
It contains an empty `scripts/commit_command_approval.py` and a 215-byte cached
module. This came from the failed Git-HEAD experiment, not a valid preimage.
Before any eventual deletion, revalidate that exact directory is the owned
Temp artifact, inspect reparse/identity and containment, and delete only that
named target using one PowerShell shell. Do not delete the broad Temp parent.
Do not claim all diagnostic artifacts cleaned until this is handled.

## Frozen evidence and hashes

The following were freshly read before A09A6 workers' edits. Those workers may
legitimately change their owned files; rehash their outputs before testing.

- `scripts/commit_command_approval.py`:
  `25EA93AB1C88AB7818E7B9A18FC3944EDB360EAC35901FFB6137D3D591F899C3`
- `scripts/approve-command.ps1`:
  `D680E41736332F0BF2F59FA270B99F3CB866CEB88BB55467B61DA418A96B5676`
- Unaccepted `scripts/verify_approval_boundary_safety.py`:
  `D4B7CC49962032235DD134203BE01680177C4F3F6CED091877C8D8CDF6D3C743`
- Reviewed 30-case runner `scripts/verify_approval_adapter_regressions.py`:
  `A88E677191A745284D3B073CD8ACEA600534156F173B49AB83F83F60DFE21C3A`
- Hardened `scripts/verify_approval_custody_integration.py`:
  `127419F22441B184F8D654F3F6E619AC7CE01342FC8ADED6A6E7DF149EE113D1`
- `agent/command_custody.py`:
  `FAACF7C429D3711E4BFCC33FF64E5247FF7725B61570BC72A66D4D02FD413DD1`
- `agent/strategy.py`:
  `2D70076900DA0B21CC77A9267888C0CF5C4C69E7143F30B0A0F0B77B063D9AF5`
- `agent/state_quality.py`:
  `49C0A2C6B894E5EB519B8115B8F244EF14A58AA1D7EB912C291B21CA777FDB6D`
- Protected `bridge/oni_bridge.py`:
  `0299F1F1C541367841A58771A26EB695D73EA38790A488FC9068451D2F58A03C`
- Protected official `scripts/verify_bridge_reliability.py`:
  `FC29C2A48B11D33B69E8EFE1F26F296C4520CBD0ED9F7F3AFF493A1E21395337`
- Protected canonical `reports/bridge-reliability.json`:
  `11D4669E2BBD0316D6E079C39228BED22CD6DD3079B4B1C6C8BC2C6993DA350D`
- Protected canonical `reports/approval-transaction-verification.json`:
  `C682C2BDA5E71F8294BCA73800B0E51217BD969C42DE5613DDFBAF5167071D6C`
- Protected canonical `reports/approval-frame-binding.json`:
  `6D18EF8B9303617A554CF248AC075057EDD97CC1ACEDFF83D407F6875C7CD0A0`

### Completed root action_ids and their exact scope

`ONI_B16_ASTRA_REVIEW_20260907_POSTPATCH_02`: first semantically valid B16
10,000-record measurement. Passed all 24 benchmark gates, 15 startup-sanity
gates, and five deterministic input/output classes. Runtime 7.7866534 seconds,
peak 135.08203125 MiB, latest/strategy sequence 10000, complete archive,
verified startup session/world/save/path and freshness, exit 0, no child or
Temp residue. Headroom is only about 0.213 seconds below the fixed 8-second
limit. This is NOT official integrated Bridge acceptance. Report:
`reports/b16-strategy-hotpath-20260907/runs/ONI_B16_ASTRA_REVIEW_20260907_POSTPATCH_02/post-patch.json`,
SHA `DB0CA21F96BD9DB3B806E0822E828972EC8543455CA2198DD0D74D5985F77871`.

`ONI_B16_STRATEGY_REGRESSION_20260907_01`: all 14 original pure strategy cases
passed, exit 0, strategy/state-quality/field-contract/verifier hashes stable.
Only the verifier's output-only ROOT variable was redirected after import.
Report `reports/b16-strategy-hotpath-20260907/runs/ONI_B16_STRATEGY_REGRESSION_20260907_01/reports/strategy-verification.json`,
SHA `926298E53787AB12D11D32437B1597ED99BF52CD8BA785FAFD168789A067B21A`.

`ONI_A09A3_BASELINE_20260907_01`: original approval code valid baseline 8/8,
but cross-process commit succeeded in 31 ms while lifecycle lock was held,
and post-approval strict inventory rejected `approval-recovery`. Real fixture
and API, no semantic mocks. Failure evidence retained at
`reports/a09a3-approval-custody-integration-20260907T043520376420Z-37684-f03f366fd4/report.json`.

`ONI_A09A4_ADAPTER_DIAGNOSTIC_20260907_01`: hardened A09A3 against Luna adapter
hash `8A35FE1DB3A5DA84688A4DC8ECDB824E4CDE77878B65D8FD1DA05F75F6544E83`
passed 3 cases/77 assertions, including 16 held-lock snapshots and normal
child exit 0 without termination. Report
`reports/a09a3-approval-custody-integration-20260907T044918373490Z-42436-1b194aab07/report.json`,
SHA `A2ABA1F32360A5C33E2738BB4D0918DFBEB5BDFD750E21BD0658415437B3C02F`.
This did not cover newly discovered directory-ownership boundaries and is
not acceptance for subsequently changed production hashes.

Last completed test action: `ONI_A09A5_LEGACY_DIAGNOSTIC_20260907_01`.
The reviewed 30-case runner exited 1: all 12 transaction cases passed; six
of 18 frame cases failed because the actual PowerShell caller writes an
approved payload `.tmp` inside `commands/approved` before strict preflight.
Thus valid approval, operator approval, nonmutating compatibility and three
postapproval-tamper setup cases were rejected. Source hashes were stable;
all 30 temporary roots were removed. Report:
`reports/a09a4-approval-regressions-20260907T045406.909144Z-46400-ccbb92b80894a150/report.json`,
SHA `4934169F4F2A58562F33B120DD170DF0BB92C75632F9472DA33CB78574FF8D7C`.

## Current defects and the two bounded repairs

A09A4 integrated full commit/validation/recovery under the lifecycle lock,
but created stage directories before acquisition and could continue recovery
after failing to establish ownership of its recovery directory. Spark moved
stage creation inside the lock and introduced a directory-ownership guard,
but its exception rewrite also dropped preservation of some recovery payloads.
It left pre-claim failures always unknown and used a stale ownership boolean
before writing ambiguity evidence. A09A6 must repair those exact exception
paths while preserving receipt/frame/policy/hash/expiry checks.

The PowerShell caller must stage its generated approved payload outside all
command queues, in a unique create-only owned Temp location, and leave stage
directory creation to locked Python. Cleanup must not delete replaced/foreign
files. If narrow safe cleanup cannot be proven, retain evidence explicitly
rather than invent successful cleanup. Do not loosen the queue allowlist.

The current recovery-directory cleanup function is deliberately no-op.
Normal success creates no recovery directory. After a real failed commit,
an empty recovery directory can remain and block subsequent approvals. This
is an acknowledged automatic-recovery availability gap, not a solved long-term
resilience loop. Do not reintroduce check-then-rmdir replacement deletion.

Spark's two A09A5 reports are invalid as acceptance evidence. Its first run
was already postpatch, not a baseline. Its failed Git-HEAD experiment loaded
an empty shadow module; the runner nevertheless marked AttributeError cases
passed because it counted zero assertions and ignored status_error. It also
hardcoded no source drift, checked some cleanup inside Temp scopes, and had
inadequate child shutdown ordering. The verifier worker must fix all of these,
require actual imported module paths, preserve real tests, and avoid raw repeated
inventory dumps. Neither historical report is to be overwritten or relabeled.

## Next single action, expected evidence, and rollback

Next single action: obtain the two already-running A09A6 workers' terminal
deliveries and independently inspect only their exact diffs and hashes.
Do not start any test while either writer is active. Verify the production
change is still limited to commit and its PowerShell caller; the test worker
may change only its verifier. Confirm no helper process was launched by them.

After that review reaches a stable source checkpoint, the next separate
actions are the repaired boundary verifier, hardened A09A3, and the unchanged
30-case legacy runner, each with a fresh action_id and create-only report:
`python -B scripts/verify_approval_boundary_safety.py`,
`python -B scripts/verify_approval_custody_integration.py`, and
`python -B scripts/verify_approval_adapter_regressions.py`.
Require exact case counts, no ignored exceptions, real source stability,
normal child exits, no forced-termination pass, Temp cleanup after exit,
single command ownership, and unchanged canonical reports. If any fails,
preserve the report, identify source versus fixture defects, and use a fresh
short-context bounded worker. Do not pretend to recover a nonexistent Git
baseline. A direct approved-function test is insufficient; the actual
PowerShell caller must also pass.

Rollback: preserve all reports and exact preimages. The A09A4 original is
`reports/a09a4-approval-adapter-20260907/preimage/commit_command_approval.py`,
SHA `E275B0D6CFF1D5A39ECBA45EB424B39B2E3FF8A462BC04DF25C1937895F60171`.
A09A6 workers must create their own 25EA/D680 and D4B7 preimages before edits.
Use inverse patches against the exact verified current hash, never wholesale
checkout/reset, and never undo another worker's unrelated file. Test failures
do not authorize touching live queues or weakening safety rules.

Remaining release order is unchanged: all queue/plan/outcome consumers and
unified journal integration; full stable-source matrix and bounded offer/history;
formal bootstrap wrapper (targeted B11 tests are not its acceptance); verified
clone/original config-save inventory; reversible G0 with native outcomes and
later trusted witnesses, save/reload, final disabled policy, empty queues,
normal exit and restored immutable inputs. Resource-loop, all-world telemetry,
functional building, fault-domain, sustainability, and space capability gates
still follow. The full survival goal is not complete and not currently blocked
on user permission; meaningful offline work is continuing.
