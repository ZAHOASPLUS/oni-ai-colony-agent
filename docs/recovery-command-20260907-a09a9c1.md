# ONI current recovery packet — C1 gate correction in flight

Recorded 2026-09-07T08:21:11Z. Read this packet instead of the accumulated
overrides in recovery-command-20260907-a09a9.md. The full goal is ACTIVE.
This is a recovery handoff, not a safety pass or permission for a live run.

## Goal

Implement AI control of ONI for indefinitely safe colony survival through
startup, stability, expansion, space and renewable sustainability. Prioritize
oxygen, food, sanitation, water, power and thermal control, with renewable
resource loops, reserves, fault isolation and recovery. Base decisions on
fresh trustworthy observations only. Finite offline tests cannot demonstrate
infinite survival, and fixing a verifier is not the final goal. Actual game
survival, complete world telemetry and functional action coverage remain
unproven. Do not narrow the goal to passing C1 or the current four-case test.

User's current routing: Astra Ultra commands and accepts safety decisions;
fresh Luna Max or available Spark executes bounded code tasks. This supersedes
the old Sol/Spark routing in AGENTS. Spark was available and executed C1, but
its final answer went off-task to plugin installation while a positive unit
control still failed. Root verified it terminal before switching that same
short task to Luna Max. Root model/network/game state did not change.

## Safety invariants

Read AGENTS.md completely in fresh worker contexts. Original cloud saves,
their PNG previews and normal Mod/configuration are immutable inputs. Only a
clearly named, hash-verified SAV+PNG test clone may ever be loaded. No live
game/Steam launch, real queue or Mod/config mutation until all current live
admission gates and fresh protected-input hashes are satisfied. An old offline
PASS is not live admission. If dispatch is ambiguous, inspect exact process,
queue and log cursor; never replay a mutation merely because a UI is blank.

Publish a short action_id checkpoint before any mutation: fresh preconditions,
one intended action, expected evidence, bounded rollback. Current C1 permits
only test-file/report changes and pure synthetic report-gate evaluation. It
does not authorize production module import, fixture construction, subprocess
workers, the four-case CLI, Bridge, game or benchmarking. C2 is not dispatched.

Before a long task dispatch, test-codex-transport-stability.ps1 must report
stable with zero recent disconnects/fresh proxy helpers. Do not alter network,
Clash/Mihomo/TAG/VPN/TUN/proxy during turns. Updates at most 60 seconds apart;
waits at most 30 seconds, retain a thread/session/PID or cursor. A successful
dispatch is not execution evidence; verify actual assistant/tool activity.
Never treat polling timeout as termination or resend to a live task.

Context yellow: >100k input or >40%; red: >150k or >60%. Root was 167,297
tokens/64.74% at 08:08:40Z, with 9 compactions, and only performed recovery
handoffs/record sealing rather than running production tests in that context.
Do not propagate the long history to fresh workers. After actual compaction,
do not keep treating this historical root measurement as current; recheck and
continue from the real file/handle state. At red, finish only the safe atomic
step and seal; keep new implementation in short, exact-scope contexts.

Workspace is NOT a Git repository. Use apply_patch for edits; exact preimages
for comparison. git diff --no-index preimage current can compare matching
files and exit 1 just means differences. Never compare the Python script to
its Markdown note. No broad delete, reset, cleanup or recursive move. Keep old
failed reports and partial artifacts. Generated unit data is synthetic, never
an observed worker/queue/game state. Do not install any plugin for this work.

## Source/test hashes and current evidence

Root verified these six protected files unchanged at 08:16Z. Rehash before
any later test; current author may only read their hashes, not import them.

| Protected file | SHA256 |
| --- | --- |
| agent/action_outcomes.py | C0FAB71E21F1E49CBE76927BF6CF4E16A7C0179BEDB290CC1EA11B4EA56060D7 |
| agent/command_custody.py | 6C0A0B995587CB18E243E4A96C56517FFE8A735923BCC5CEEDE750BA19E91763 |
| agent/command_queue.py | 4992E0605DCE7821AE8776E38AB30A2E1C805DD15F23C988E25488EFE4E8DEA1 |
| agent/colony_plan.py | 0E5B3D174699A1CC54F42CA749838D766176069C4B5F43AD2AD20B0D8D047ADD |
| scripts/verify_confirmation_transactions.py | 157E191A5030BF0D64742AAA5575F7E845EDBAEFA7C5E4F338A3F442A7B09AC9 |
| scripts/verify_action_outcomes.py | E2566D8B169D2815404CD3141A6BE5A5342BD6BF96DB396BEDE2F225B95C3152 |

Use AST.literal_eval on AUTHOR_BASELINE_SOURCE_HASHES in the hash-guarded
verifier to avoid hand-copy errors; do not execute the source module. A prior
author mistyped a constant and correctly stopped before backup; no protected
file was changed to satisfy a bad expected hash.

Current C1 target observations, matched by root at 08:16Z and by Luna before
its partial backup (active writer may change these; they are not permanent):

- scripts/verify_confirmation_custody_integration.py:
  3386627DA584FC5E53B0A9D3A7D7500507B3FA5728258A221CF9A214A9B70BFF
- docs/a09a9-confirmation-custody-verifier-20260907.md:
  6766B54B889052144263D0E73CC32BF8A57CA066F2687F1494C4DA6646389685

C1 exact preimages (root independently verified both) are in
reports/a09a9c1-gate-20260907T081324Z-pid-41960-nonce-/:

- preimage_verify_confirmation_custody_integration.py:
  F627E61AE937EA7B8815D5138E671AF8BC0D47DB3D8F440E57CA569AA5A96DFB
- preimage_a09a9-confirmation-custody-verifier-20260907.md:
  7950A381E3FAF6318147DDA4AB2C4785F411713F45B6F16C6D5A1916AD90C984

The literal directory name ends in nonce- with an empty suffix. Do not invent
a nonce or rename the live report directory. Its exact two backups exist and
match; preserve them. Luna is now creating a separate spark-partial-UTC-guid
subdirectory to retain 3386/6766 before any further correction. Discover and
verify its returned path; do not assume backup completion from intent alone.

Spark's actual pure-gate report exists at the same directory's
ONI_A09A9_PURE_GATE_REVIEW_20260907_02.json:
A621C23ED64C5F4A18D3019E26EBA1D25C697D549C3501FDEAB4C9B48676252C.
Its outer object has passed_count=21, failed_count=1, report={...}; inner
report.results fields are name, expected, actual, matched, checks. The FULL
positive synthetic_actual_nested_shape_pass fails. It shows individual commit
and recover bound checks true and evidence_complete true, but combined
lock_case_worker_binding_complete/worker_binding_ok false. Negative controls
passing while the positive fails does NOT prove a correct gate.

Spark created a pure test script outside its intended owned report directory:
C:/Users/user/AppData/Local/Temp/a09a9_pure_gate_review.py,
E979E39532E1ECE61DA2940509EF420DDCAA884D44C0E5119F2499B9F89D5C3A,
created 08:15:16Z. Retain this artifact; do not overwrite/delete/re-execute it
blindly. Luna may read/copy its synthetic inputs, but any new validation script
must be apply_patch-created inside the owned report directory, statically
limited to extracted pure gate functions. Do not reuse a fixed global Temp
filename. Existing reports must remain immutable.

Original root pure gate diagnostic (F627 version) remains:
reports/ONI_A09A9_PURE_GATE_REVIEW_20260907_01.json,
0F66A240BC3F8CAD8A42482EFEADD8FD4B96B5532E35892DE639FFD5B5AE2193.
It confirmed three false acceptances (empty reader proof, empty B final state,
worker on wrong case) and one actual-nesting false rejection. None involved
production imports, command fixtures or child execution. Root's exact review
contract docs/a09a9-repaired-verifier-review-20260907.md has hash
A82C2B74DAFA278D883F1168DD33F774953FDCEB9C11C2F8966B228ED0C02607.

Historical accepted work stays intact: A09A8 custody/Plan suites 93/93;
B18 original Bridge verifier 15/15 and 399 assertions, normal exit. The latter's
startup-bound sample was 7.897s against 8.0s, only 0.103s margin. It is neither
robustness nor game admission. Do not rerun/revert these unchanged stages just
for status. Full current bootstrap two-round verification remains unperformed;
fixture startup reason must be save_loader_load_complete. Production
confirming custody/C# interoperability and the broad survival scope remain
unfinished. Those historical results do not authorize skipping C1/C2 review.

## Current process, Mod, queue and log cursor

Sole active writer: **ONI A09A9C1 报告门禁局部修复**, same short app task:
- thread 01a07aed-7bca-7d70-b5a6-6f5eea2a21be, host local;
- active Luna Max turn 01a07af2-edbf-7740-bf48-16f5c6c788dc,
  dispatched after stable transport at 08:18:55Z;
- latest verified cursor 7facf0dc-7a9c-4d37-9d32-a291b9fc4d82:12;
- actual assistant/tool activity verified, app navigation already succeeded;
- latest event is pre-backup checkpoint plus completed read command, not yet
  a terminal C1 delivery. Do not duplicate or restart this worker.

Previous Spark turn 01a07aed-7ce2-7730-8555-719a5a0d6a44 completed normally
at 08:15:26Z after 147.284s, but off-task final and failed positive control make
it an incomplete handoff. Earlier long Luna repair 01a07acc-2a81-7e91-8a99-
e242dc0f1898 is terminal too, as are all read-only audit subagents. Do not resume
either long author or confuse a terminal prior turn with this new active turn.

No ONI/Steam/dotnet/Bridge/test-worker process was found in the final root
census of the prior review. Pythonw PID10324 was freshly identified as existing
MihomoMonitor windows_agent.py and was not touched. Do not treat the letters
ONI inside Monitor as a game process match. This turn only used diagnostic
Python for pure AST gates, no actual worker PID. No live command was sent;
there is no live game log cursor or action queue admission. Original SAV/PNG,
normal Mod/config and real queue were not accessed/mutated here: current
hashes, restored state and empty queues remain UNVERIFIED, not implicitly PASS.

## Last completed action and next single action

Last completed root action: ONI_A09A9C1_GATE_HANDOFF_20260907_01 plus verified
terminal-Spark to Luna Max recovery at 08:18:55Z. The task was dispatched and
has real activity; C1 itself is NOT complete. Root has only independently
verified backups/current hashes and the 21/22 failed report, not accepted the
new implementation. Root must not claim that all C1 pure checks pass yet.

Next SINGLE action: poll the exact active C1 handle/cursor, then review its
terminal evidence when it actually ends. Use wait_threads <=30 seconds; if
UI/progress uncertain, run check_codex_thread_health.py for that thread,
sample 3 seconds. No extra production writer, C2 task, fixture or CLI yet.

C1 allowed edits are only _gate, small no-I/O evidence predicates, _lock_case's
two strict reader-map checks and its existing note. It must use REAL nested
run-info.cleanup, exact per-case operation/id/result binding, complete normal
and B final-state/idempotence/cleanup evidence, exact stdout/stderr maps and
strict types, no missing/error/skip/zero-assertion/hash-drift false PASS. It
must not invent fields absent from the actual producer or weaken a gate to
make synthetic input pass. Diagnose the current combined binding failure
against real producer fields before altering code or the unit fixture.

## Expected evidence and rollback

Required terminal C1 evidence: two original C1 preimage hashes plus Spark
partial backup hashes; both final target hashes; actual in-memory AST/compile;
complete realistic pure positive control PASS; each recorded single-point
negative FAIL; all names/expected/actual/counts and an exclusive new report
hash; six protected hashes unchanged. Check that report source/doc hashes
match the actual final files. Verify before/after and normal exit explicitly;
normal process exit alone cannot override a failed control. Retain _02 failed
report and write a unique later report, never overwrite to hide the failure.

After C1 terminal verification, C2 is still needed: strict protected-stage
inventory and exception-safe partial Popen/reader ownership cleanup, plus
narrow marker/protocol/sample evidence checks if justified. C2 is not yet
authorized to a worker. Only after both corrections and independent review
should root freeze sources and run the four-case real-import baseline once.
Expect a real normal control and explicit production custody failures, not
test setup errors or a converted defect PASS. Then production repair may be
planned; no transition from C1 directly to a real save or gameplay.

Rollback only the two verifier files from the correct, hash-verified preimage
after excluding intervening edits. Never restore over an active writer.
F627/7950 are C1 entry; 3386/6766 are Spark partial; the older author originals
236E3222/3344E112 live in the older repair directory and must not be mixed up.
Do not revert accepted A09A8/B18 or delete report/Temp evidence. There is no
game/Mod rollback action in this offline-only checkpoint. Leave the full goal
active; do not mark complete or blocked merely for context length, slow work,
or remaining tests.
