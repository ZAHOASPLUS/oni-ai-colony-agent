# ONI command recovery checkpoint — 2026-09-07

Action ID: `ONI_COMMAND_RECOVERY_20260907_01`.
Scope: commander recovery and evidence reconciliation only. This packet does
not enable any game action. Read the latest worker completion and rehash the
named files before resuming; a worker may still be sealing its report.

## Goal and authority

Continue implementing the ONI AI long-term survival architecture, progressing
through bootstrap, stabilize, expand, space, and sustain from fresh trusted
observations. Preserve oxygen, accessible food, sanitation, usable water,
delivered power, and critical-zone temperatures before expanding. Sustainable
operation requires renewable supplies, demand and reserve evidence, explicit
failure assumptions, and observed recovery. A finite offline or live run cannot
prove unconditional infinite survival. Report precisely which capabilities and
test windows are actually demonstrated.

Latest user routing: the main commander is Astra Ultra; execution should be
delegated to Luna Max in a new conversation with exact file ownership, input
contracts, stop conditions, verification, and recovery instructions. The user
additionally authorizes changing the execution conversation's model and effort
according to task difficulty when Luna execution is abnormal. That exception
may be used after the present atomic rollback/report operation completes. Do
not switch a model during a live action or send competing writers to the same
files. The commander's local health check at 04:07 UTC confirmed
`gpt-6-astra`, effort `ultra`, 142,387 last input tokens, 55.1% of its reported
258,400 context window. Finish the current recovery checkpoint before starting
another multi-step mutation if the workspace red line is reached.

The active goal remains incomplete. Keep it active; do not call `update_goal`
with complete for a partial subsystem pass. The prior user objective was
replaced by the user's current wording, including the model fallback authority.
No new permission request is needed for the already authorized offline repair.

## Safety invariants

Read `AGENTS.md` completely in a new execution context. Preserve original cloud
saves and normal Mod configuration. Any eventual game load must use a clearly
named test clone with both `.sav` and preview `.png` verified, with original
hash inventory and post-run comparison. This checkpoint did not open original
saves or normal Mod/config files and therefore does not attest their current
bytes. No live load, command, approval, unpause, or game launch is authorized
by any result in this packet.

Before a long delegated turn run `scripts/test-codex-transport-stability.ps1`.
The commander observed stable transport at 2026-09-07T04:03:12Z: zero recent
disconnects, reconnects, or fresh transport helpers. This is a historical
checkpoint, not a substitute for the next fresh check. Do not restart or
reconfigure VPN, Clash, Mihomo, TAG, TUN, or proxy while turns are active.

Publish action ID, fresh preconditions, one intended action, expected evidence,
and rollback before mutation. If action delivery or completion is uncertain,
inspect rollout, child process, queue, cursor, and resulting files before
repeating. Keep logs in evidence files, commentary within 60 seconds, and wait
polls no longer than 30 seconds. A blank app response is not an idle worker.

The workspace has no Git repository. Preserve unrelated changes; use
`apply_patch`, never reset/checkout or broad deletion. Temp cleanup must be
limited to the worker's exact owned directories and must not follow reparse
points. Existing failed Temp trees are evidence and must not be reused or
cleaned. Unknown outcomes, corrupt custody journals, incomplete world scope,
missing freshness, or contradictory incident evidence must remain blocking.

## Source and test hashes observed by the commander

These SHA-256 values were independently read on 2026-09-07. Rehash before a
new write; do not silently adopt a different value.

| File | SHA-256 |
| --- | --- |
| `agent/strategy.py` | `926F3597A4057E210E619EF203FD2B33ECE82DF3A03DEC3B76734E51C368E87E` |
| `agent/state_quality.py` | `99C1B0D6F3AF6AEE4D4CC0F309973932AF7F4E99BE5C6C309DBD3D7F5653C14A` |
| `agent/command_custody.py` | `FAACF7C429D3711E4BFCC33FF64E5247FF7725B61570BC72A66D4D02FD413DD1` |
| `bridge/oni_bridge.py` | `0299F1F1C541367841A58771A26EB695D73EA38790A488FC9068451D2F58A03C` |
| `scripts/verify_bridge_reliability.py` | `FC29C2A48B11D33B69E8EFE1F26F296C4520CBD0ED9F7F3AFF493A1E21395337` |
| `reports/bridge-reliability.json` | `11D4669E2BBD0316D6E079C39228BED22CD6DD3079B4B1C6C8BC2C6993DA350D` |
| `scripts/verify_b14_strategy_hotpath.py` after B15 rollback | `69CDE918F295BB754CF27624B42C1F2194FA8E98611389E5BA6CF39C0831BD4E` |
| `reports/b15-strategy-hotpath-20260906/post-patch.json` | `966BD8C0B1510CCBE74EED951202C9E28EAF495B8ACDBE6D11A451C60A90D725` |
| `reports/a09a2-command-custody-core/command-custody-core.json` | `CB2C800D7FFCBE07CB160DFC8263D32032CE917D64CC1533F288C52E4B759052` |
| `reports/b13-cpu-profile-20260906/verify.json` | `DFBDD6A2A36D58646959FD7E5E385015F6698B39AADC0F69F90C4CE3D249E90C` |

The Bridge hash has exactly 64 hex characters and ends in `A03C`. An earlier
delegation accidentally omitted the final `C`. The actual file consistently
matched the complete hash; that 63-character text was not evidence of drift.

The B15 harness is NOT back to its original B14 hash
`BF95EFDD3390FE65D894EA7221D848EE3A2C5EE12095EBA7A4D21CBBBDE3024F`.
It retains the corrected `session_started.reason=save_loader_load_complete`.
Its `REPORT_ROOT` has reverted to the B14 directory, so DO NOT invoke its main
entrypoint until output isolation is repaired. The two production agent
modules are back to their original baselines. Read the worker's final diff
and verify bytes; do not describe all three files as completely restored.

## Last completed implementation and verification boundaries

A09A custody core passed 8 cases, 77 assertions, exit 0, in run
`20260906T002551.415174Z`. The report records Temp cleanup and zero child
residuals. This covers the core, not every consumer. Queue/approval/Bridge
integration is still a separate acceptance slice. Do not use the core pass to
claim one-owner behavior throughout the production lifecycle.

B11 import-attestation test evidence has status passed. Main rechecked:
`reports/b11-bootstrap-import-attestation/test-evidence.json` SHA
`922AB6097C7365CAF4237F8FBD5056D1C53BDF82ECA4DF3A1FC9D9A1DBA0DFA3`,
and `runner-regression-20260906.json` SHA
`54324D349C3144CE57CAF5A48E99AB8DCD21F7288E4A5F389D2A3E3D89C4432A`.
These are not a fresh formal two-round production-wrapper acceptance. The
historical moved-Bridge abort failure cannot be overridden by an older pass.
Complete formal bootstrap only after all relevant source writers freeze.

B13 measured three startup-bound controls around 8.18–8.32 seconds, above
the unchanged 8.0-second gate. Its classification is diagnostic completion,
gate not passed. Peak working set was not measured in that profile. The
canonical Bridge report remains the protected failure hash shown above.

B14's first synthetic benchmark returned 0 and archived 10,000 records in
0.778 seconds, but `latest_seq=null`, health was waiting for a current
snapshot, and startup binding was false. That time did not measure admitted
strategy work and is not a performance success. The fixture used the wrong
reason, `trusted_load_complete`, while Bridge requires
`save_loader_load_complete`. All tentative B14 production changes were
rolled back; historical reports remain under its own directory.

B15 action ID is `A09A2_LUNA_B15_FIXTURE_HOTPATH_20260906_01`. The resumed
worker executed one post-patch command on 2026-09-07 at about 04:06:51 UTC.
Five complete strategy-output comparisons matched. Before any 10,000-record
benchmark, its new two-record admission check observed:

```text
current_session_id = session-performance
latest_seq = 2
health_status = ok
startup_binding_verified = true
temporary_directory_cleaned = false
admission passed = false
benchmark status = skipped
benchmark reason = admission_sanity_failed
```

The cleanup predicate was computed inside the `TemporaryDirectory` context,
while the directory necessarily still existed. Thus the harness failed its
own cleanup-order check. No B15 10,000-record benchmark ran. Do not invent
elapsed/peak measurements or count the command as a successful benchmark.
The worker was instructed to finish only rollback and sealing. It reported
rollback applied at 04:08:22 UTC. Commander hashes corroborate the production
rollback and reason-only harness change above. The eventual worker completion
must still be inspected before dispatching another writer.

## Process, Mod, queue, cursor, and ownership state

Active B15 executor: `01a0742e-acf9-7be2-aa11-52255be84cc1`, title
`B15 strategy hotpath offline repair`, Luna Max. Its older coordinator
`01a0742d-cbb2-7932-8743-1e07395bf3d7` was interrupted and should not be
resumed or used to dispatch duplicate work. A nested coordinator was created
accidentally in the prior turn; the actual executor has been explicitly told
never to redelegate. Only that executor owns B15 edits.

B15's active resumed turn is `01a07a08-c55b-7a51-aaa4-2049faf4ec39`.
Its local rollout is:
`C:\Users\user\.codex\sessions\2026\09\06\rollout-2026-09-06T08-46-51-01a0742e-acf9-7be2-aa11-52255be84cc1.jsonl`.
The app's `read_thread`/wait snapshots did not show resumed messages, while
the local rollout grew with real assistant and tool events. Treat this as
visibility desynchronization, not a reason to interrupt or replay. Use
`python -B scripts/check_codex_thread_health.py --thread-id <id>
--sample-seconds 3` and bounded tail parsing for current status. Do not dump
the whole rollout. Observe final completion plus files before changing model.

The commander did not launch ONI, Steam, Klei, a normal Bridge, or any live
helper. A broad process command-line filter matched its own PowerShell
inspection command; that is not evidence of a real Bridge process. The B15
worker's exact-name census was clear before its offline test. Read current
process identities before further work; do not infer present global absence
from an old census or kill unrelated Python/PowerShell processes.

Normal Mod/config and actual game queues were not read or changed by this
resumed offline checkpoint. Their current contents are unverified. There is
no live game log cursor or command receipt to replay. Offline temporary output
directories are separate from actual game queue paths. Required `py_compile`
can produce `.pyc` despite `-B`; known cache files are not helper processes.
Do not delete them broadly or treat them as a completed live cleanup proof.

One independent read-only Luna Max audit is active as collaboration agent
`/root/architecture_next_gate_audit`. Its scope is the A09A consumer integration
gap and live G0 prerequisites; it may not write files or run tests. Retrieve
its final concise findings before any consumer implementation. It was asked
to finish its current step because commander context was near the red line.

## Next single action, evidence, and rollback

After fresh context recovery, first confirm B15 has completed rollback/report
sealing and collect the audit result. Then the next IMPLEMENTATION action is
one bounded repair of the dedicated performance harness. The user's fallback
authority permits continuing the execution task with a more capable model
(for example Sol High or Ultra) after the old turn finishes. Use a compact
prompt and no further subdelegation. Recheck transport and exact hashes first.

Allowed repair scope: `scripts/verify_b14_strategy_hotpath.py`, a new isolated
report directory such as `reports/b16-strategy-hotpath-20260907/`, and its
specific documentation. Initially leave production modules frozen. Required
fixes: isolated output root; correct trusted start reason; fresh benchmark
timestamp while preserving deterministic differential timestamps; compute
cleanup evidence only after leaving the Temp context; positive memory-sample
evidence, not zero treated as a successful measurement; and stop before the
large benchmark if semantic or small admission checks fail. Preserve all
existing hard thresholds and Bridge source. Retain failed evidence.

Prove the two-record admission and actual post-context Temp removal first.
If that fails, fix the bounded harness defect from observed evidence rather
than repeatedly measuring an invalid workload. Only after this sanity check
is valid may a separate explicit checkpoint reapply the small production
optimization and perform one 10,000-record benchmark. Baseline production
outputs must be freshly captured before the optimization, or a reused
baseline must have exact input/source hashes and an independently checked
equivalence justification. Do not silently overwrite historical baselines.

The proposed optimization remains limited to equivalent top-level lookup in
`safe_get`/`field_quality` and reusing one `field_quality` entry inside
`_field_contract_blockers`. Preserve nested/default/type behavior, all
freshness/world/sequence/field-contract checks, and every blocker. No global
or cross-session/world cache. Keep the canonical verifier and report frozen.
Require exact five-case output equality, normal exit, empty stderr, positive
peak <=150 MiB, wall <=8 seconds, <=20-second hard timeout, seq=10000,
startup binding and health, complete archive, child exit and Temp cleanup.

On any production-semantic failure restore only the exact own production
patch and rehash, preserving the corrected harness and all evidence where
appropriate. A dedicated pass is only preparation for an integrated matrix
against frozen sources; it does not pass formal bootstrap or G0. Consumer
custody integration, bounded offers/history, exact live G0 priority
from/to/from with later trusted effect and save/reload evidence, all-world
observation, functional life-support telemetry, construction recovery,
resource loops, sustainability proof production, and space/base reserve
protection remain separate requirements. Follow the existing architectural
release order rather than starting the game after a timing improvement.

## Final checkpoint reconciliation — 04:12 UTC

The app subsequently confirmed B15 completed normally as a Codex turn at
`1788754196` (04:09:56 UTC) and is idle. This is completion of rollback/report
sealing, not successful verification. The final worker recovery report is
`reports/b15-strategy-hotpath-20260906/recovery-packet.md`, SHA
`CD1156BE588FAF5B2153A5689F50DAC207712A94B6A5EA9C7E89635B290043A8`;
its documentation SHA is
`A775A5EC612C5511BAD66D8619EB736041E1B42FAA13BDD97A64B6C57DBAB40F`.
Its wording about needing separate authorization does not revoke the user's
standing authority to continue offline implementation and use model fallback.

Historical evidence correction: the B14 failure `post-patch.json` remains
unchanged at `8C88D37C74837C8D3964B2DFA40933EDFF4FE5DC10193AEA6B02C96FFB5A798C`.
However B14 `baseline/differential.json` changed from the previously observed
`F0074F71315A8F4487B5E884ACA62FDCFFBE4C2941FB85B0DDFB14B3C1D84879`
to `7B4A87D4266F6D36026EBF55596755131140636C5AB0D23D7D82286B02C0FA77`.
The worker ran baseline before changing the report root. Do not claim all B14
evidence stayed byte-identical. Preserve the current file and recorded prior
hash; do not reconstruct or replace historical evidence to conceal the change.
The exact difference has not been characterized by the commander. The next
harness repair must set an isolated report root BEFORE any writing entrypoint.

The independent read-only audit completed. Commander source reads confirmed
approval's private lock and `approval-recovery` plus direct `os.replace`
(`scripts/commit_command_approval.py:496`), a commands-root allowlist that
does not include that recovery directory (`agent/command_custody.py:287`),
and direct plan-leaf publication/unlink (`agent/colony_plan.py:962`). The audit
also located direct confirmation transitions in `agent/action_outcomes.py:1383`.
Core custody success must therefore not be promoted to integrated safety.
After the bounded harness repair, scope consumer integration into explicit
adapters and shared-lock transactions, with deterministic approval-versus-
expiry/publication races and crash-recovery evidence. Do not combine all five
consumer modules into an unbounded patch merely because their gaps are related.

The commander finally observed 153,952 input tokens at 04:11:43 UTC. Although
the health helper labeled the ratio band yellow, `AGENTS.md` defines red at
more than 150,000 input tokens OR 60% of context. The absolute-token rule is
already met. This packet is the final current atomic step; resume the next
implementation in fresh context, keeping the goal active and the existing
standing authorization. No live action was issued.
