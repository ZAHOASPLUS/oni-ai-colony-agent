# Commander recovery — A09A7, 2026-09-07

## Terminal delivery / review completion — 2026-09-07T05:53Z

The sole verifier writer is now TERMINAL: local last event
2026-09-07T05:52:16.554Z, `active_turn=false`; final delivery received.
Do not continue waiting for it or resend its authoring task. Final script
SHA-256 `E5CA75C76AB0AC6489FF00E669E529E4AB51ABC337C7E405AE0CEAE9C8176A98`;
document SHA-256 `81F573DF1ADB09EC9BD0E380B2EEF30C485252359306E6224B6202CC88C494CD`.
The script is 726 lines. Root read the complete original candidate and the
changed sections, confirming the two requested repairs are present:
all held samples plus the terminal snapshot are counted and checked, and
fresh-B positive quarantine must succeed in its separate owned sub-root
before stale-A is attempted. The immutable/current/pointer plan paths were
checked against the actual production commit function.

Only author in-memory compile/AST/static checks have run. No new verifier
fixture, main, or child has executed. Current next action is its old-source
baseline command, not another authoring task:
`python -B scripts/verify_colony_plan_custody_integration.py`.
Before that command: fresh critical source hashes, all writer-terminal check,
process census, short action_id checkpoint. It allocates a new report itself.
Afterward: verify actual errors/positive control, per-sample counts, loaded
paths, source drift, normal child exit, Temp removal and report hash. Reject
invalid fixture/setup results instead of attributing them to production.

Additional review limitation for execution: `CASE_HARD_TIMEOUT_SECONDS=20`
is a nominal per-lock-case budget, not an external kill supervisor for the
entire verifier. Fixture preparation is outside that deadline and some Event
waits/join cleanup use their own bounded waits. Record actual case/whole-run
durations and owned-process cleanup; do not claim a hard 20-second guarantee
for every possible failure path from the constant alone. This is suitable
for a parent-observed bounded diagnostic, not full timeout-stress acceptance.
Parent input was 198,031 tokens at this terminal check, still RED. Finish
this review/packet atomic step; fresh compact context is needed before new
multi-step implementation or fixture execution. Earlier active-writer notes
below are historical and superseded by this terminal checkpoint.

## Current atomic review update — 2026-09-07T05:49Z

The new verifier is now on disk. Root fully read the candidate at SHA-256
`50808948B159134A745604A871A15021C0AE71C8FC47C3665995F0C7989ACCA1`;
it is NOT yet accepted or executed. The same writer remains active and has
been asked to fix two specific findings before terminal delivery:

1. Count changed/error snapshots across EVERY held-lock sample and require
   both counts zero, in addition to the final snapshot. The candidate only
   asserted the final diff and could miss transient change followed by restore.
2. Within the stale-payload case, first use a separate child fixture root to
   prove the same valid replacement B is accepted by the real quarantine
   helper when expected=B. That positive control must pass before invoking
   stale expected=A. This prevents blanket rejection of pretty-JSON commands
   from masquerading as stale-source protection. Keep the four case names;
   the additional root is inside this case's owned TemporaryDirectory.

The 300–400-line suggestion has explicitly been made advisory: do not rewrite
again merely for line count or compress code into giant single lines. The
writer previously spent time shortening an in-memory 958-line prototype;
there was a roughly four-minute activity gap, but real events resumed and
the file was applied at about 05:47Z. No retry, interruption or model change
was performed. Current parent context is RED (185,751 input tokens); finish
only this atomic review/record step. Do not run new fixtures or begin a new
production mutation until a fresh compact command context is available.

Root verified the plan snapshot paths against `_commit_revision`: root
`colony-plan.json`, `colony-plans/active.json`, per-plan `current.json`, and
immutable revisions are correct. Production colony_plan remains at EE0AA5...
as listed below. Use the final delivered verifier hash, never the intermediate
508089... hash, for future baseline execution. No functional test has run yet.

## Goal and safety invariants

Continue the ACTIVE ONI goal: fresh trustworthy observations, safe control
through bootstrap/stabilize/expand/space/sustain, with oxygen, food, sanitation,
water, power and temperature before expansion and renewable fault-tolerant
loops. No live admission and no perpetual-survival claim exist. Main is Astra
Ultra; user authorized Luna Max or available Spark execution. Do not route
back to an old model merely because AGENTS.md retains earlier Sol/Spark text.

AGENTS.md applies: commentary action_id before mutation/launch, only verified
named save+PNG clone for live, immutable original save/preview/normal config
hashes, no uncertain action replay, transport stable preflight before long
dispatch, no VPN/Mihomo/TUN changes. Updates <=60s, waits <=30s. At context red
(>150k input or >60%) finish only current atomic step and use this packet;
fresh compact context may continue meaningful work. Do not repeatedly report
the old red condition after compaction. No Git repository; no git HEAD/reset.
Use apply_patch for source/docs; no broad deletion; preserve failed evidence.

## Current process, Mod, queue and log cursor

No live game/Steam/Bridge was launched by the current round. Real queues,
original saves, previews and normal Mod/config were not read or mutated.
Therefore current original hashes and real queue emptiness are NOT verified.
After approval tests, targeted process census had no game/Steam/approval/
Bridge/multiprocessing-spawn residual. After official Bridge test, no Bridge
or verifier child and no `oni-bridge-v2-*` root remained. After original Colony
Plan baseline, no `oni-colony-plan-*` temporary root remained.

Seven intentional approval staging directories remain under
`C:/Users/user/AppData/Local/Temp/`; exact names are in
`a09a6-commander-validation-20260907.md`. They were created during 05:18:57Z to
05:19:08Z by the legacy run. No deletion attempted. Old Spark diagnostic
residue also remains at
`C:/Users/user/AppData/Local/Temp/oni-a09a5-pre-/f4d9a62f-2f63-469c-afcc-85bc95309e64`
(empty shadow commit module plus pyc). It is not a valid baseline/preimage.
Never claim all temporary evidence cleaned; exact-target cleanup requires
separate identity/containment checks, not deletion of parent Temp directories.

One current worker owns ONLY a new verifier and its document:

- collaboration `/root/a09a7_plan_custody_verifier`, Luna Max.
- Real thread `01a07a59-7ef7-7d33-a8d2-9148a8c85d72`.
- Rollout `C:/Users/user/.codex/sessions/2026/09/07/rollout-2026-09-07T13-31-20-01a07a59-7ef7-7d33-a8d2-9148a8c85d72.jsonl`.
- Turn `01a07a59-7f50-7141-95a7-4be6ad7ba649`.
- Last root health sample about 05:33:18Z: real activity 05:33:07.446Z,
  grew during sample, 84,482 input tokens / 258,400, green, active turn.
- Allowed writes: `scripts/verify_colony_plan_custody_integration.py` and
  `docs/a09a7-plan-custody-verifier-20260907.md`. No production changes.
- NO fixture, main, subprocess, benchmark, game, queue or network execution
  authority. Only in-memory compile/static checks before final delivery.
- Do not redelegate or restore a file while this writer is live. App dispatch
  was subsequently verified by the local real-event health evidence above.

Both A09A6 implementation writers are terminal. The A09A7 read-only consumer
audit (`01a07a4c-1d8e-73d2-8ce7-66d01d1bc48b`) and B17 read-only performance
audit (`01a07a51-2399-7f63-9f46-9d3293cdac67`) are also terminal. Do not resume
them as competing writers. Their findings are below. No live action log
cursor is advanced because this round was offline only.

## Current source/test hashes

- `agent/colony_plan.py`: `EE0AA5C25F7CB2DC0A42C2C7FBE52A19581DBB39DE5972A98BE6BD99A9E4D369`
- `agent/command_custody.py`: `FAACF7C429D3711E4BFCC33FF64E5247FF7725B61570BC72A66D4D02FD413DD1`
- `agent/command_queue.py`: `4992E0605DCE7821AE8776E38AB30A2E1C805DD15F23C988E25488EFE4E8DEA1`
- `scripts/commit_command_approval.py`: `FC69BFE53956B981FF5ADB2CF2109B19D7BE564947F5E7EA32C5DA6DA68E3582`
- `scripts/approve-command.ps1`: `8C045BAE06230A3E2EBEC1E1DD6DF56DDB771E9D053BFB8C7EEB0059FE29DBB6`
- `scripts/verify_approval_boundary_safety.py`: `2B6EA95E7802FF3DF1AC35EE810C9A8D5B998076825C9378DEAB6FBB0D4DD132`
- `scripts/verify_approval_custody_integration.py`: `127419F22441B184F8D654F3F6E619AC7CE01342FC8ADED6A6E7DF149EE113D1`
- `scripts/verify_approval_adapter_regressions.py`: `A88E677191A745284D3B073CD8ACEA600534156F173B49AB83F83F60DFE21C3A`
- `scripts/verify_colony_plan.py`: `8A4A41C85009EB3EDB5291A79EEC721466D4B9026601059920F7958415BC2C8C`
- `scripts/verify_command_custody_core.py`: `307DD76BF049B4C211A289B7351F2DF73B7DB97EF8DB75EF99420826F99380FD`
- `scripts/verify_command_pipeline_gate.py`: `FA93B21ADBC2912144AC3130AB9D327162885DB77F1D186FB7D0EE490CD5270F`
- `agent/strategy.py`: `2D70076900DA0B21CC77A9267888C0CF5C4C69E7143F30B0A0F0B77B063D9AF5`
- `agent/state_quality.py`: `49C0A2C6B894E5EB519B8115B8F244EF14A58AA1D7EB912C291B21CA777FDB6D`
- `bridge/oni_bridge.py`: `0299F1F1C541367841A58771A26EB695D73EA38790A488FC9068451D2F58A03C`
- `scripts/verify_bridge_reliability.py`: `FC29C2A48B11D33B69E8EFE1F26F296C4520CBD0ED9F7F3AFF493A1E21395337`

## Completed action_ids and exact evidence

Full approval acceptance detail is in `a09a6-commander-validation-20260907.md`
(initial hash `3CBA0BF960D4141D808616D4196F0BA6397D47149E769244C7CD544783AD4925`).

1. `ONI_A09A6_BOUNDARY_20260907_01`: normal exit0, 4/4 cases, 34/34 assertions.
   Ten actual held-lock samples unchanged; child PID44864 normal0, no forced
   termination/residual; owned Temp cleanup true. Report
   `reports/a09a6-boundary-verifier-20260907T051758504268Z-44916-e81ef49e63/report.json`
   hash `C0CE79FD79F2A4B053E1321BE6A1254515BAFD1456125F4BA0B2D9915FE94A55`.
2. `ONI_A09A6_CUSTODY_20260907_01`: exit0, 3/3 cases, 80/80 assertions
   (8+65+7, count depends on actual samples). Child PID46480 normal0, no
   forced termination/residual, source drift false, Temp clean. Report
   `reports/a09a3-approval-custody-integration-20260907T051821006571Z-37660-177cf56915/report.json`
   hash `B8754140DCBEB42A5F90822EFC496A1289E49C3269CA7AB17C47A1C1CA9078B4`.
3. `ONI_A09A6_LEGACY_20260907_01`: exit0, original 12 transaction +18 frame
   cases all pass; previous six frame failures resolved. Source stable,
   30 fixture roots removed, seven OS staging roots intentionally retained.
   Report `reports/a09a4-approval-regressions-20260907T051855.810532Z-45732-bc252b3e5639cd6d/report.json`
   hash `1BD1DA7EC95276AD3E377851095677CC1F37596421F1C50268D902CE17C3D82F`.
4. Fourteen pure verifier controls passed, zero fixtures: twelve failure
   variants rejected, positive control passed, module-path mismatch detected.
   No independent shadow-import experiment is claimed.
5. `ONI_A09A6_BRIDGE_INTEGRATION_20260907_01`: official 15case suite exited1,
   14 pass, 377 assertions reached. `large_replay_budget` failed:
   `10,000-line startup-bound live initial replay took 8.082s`.
   Report `reports/ONI_A09A6_BRIDGE_INTEGRATION_20260907_01.json`, hash
   `7E18752204DDCFCEA1466BBCC641B668E9C6EC55201FE6ACD224F7F26F27D5AB`.
   The full case's 14.519s is not the replay duration. Later memory/output
   assertions were not reached; do not infer their success. All 110 Python/
   PowerShell sources were unchanged. Manifests have same prefix plus
   `-sources-before.json` (hash `455457BC99729E33F3E31111AC34E0BE2ABBDA438C74EED329F6F29C951702A3`)
   and `-sources-after.json` (hash `6168019D1265D22EB57B52608ACE7DDF18CACD302FB292EB01B68BE691B7AB85`).
   Canonical Bridge failure unchanged `11D4669E2BBD0316D6E079C39228BED22CD6DD3079B4B1C6C8BC2C6993DA350D`.
6. LAST actual fixture run `ONI_A09A7_PLAN_BASELINE_20260907_01`:
   `python -B scripts/verify_colony_plan.py --report reports/ONI_A09A7_PLAN_BASELINE_20260907_01.json`
   (explicitly NO --live), exit0, all original5cases pass, source stable,
   no plan Temp residue. Report hash
   `46665A75A8CFF232EAA3E8F4B9D5E7DE3FC8F79CB4E54643FDC165F9F7C562A0`.

No production code has changed since these accepted A09A6 hashes. The small
performance candidates below were evaluated only as in-process test functions.

## Next single action and expected evidence

Receive terminal delivery from the sole active A09A7 verifier writer, inspect
its exact source/diff/hash, then authorize one OLD-SOURCE baseline run of the
new script. Do not test while it is being written. Expected four cases:

1. Real create->advance->abort valid lifecycle baseline.
2. Parent holds real lifecycle lock, spawn child calls real advance; queue
   and plan bytes/hash/identity/mtime stay unchanged through >=2 held samples;
   no outcome before unlock; after unlock active/single pending.
3. Same held-lock test for abort; after unlock aborted/single quarantine.
4. Test setup replaces pending A with valid same-id/different-body B; calling
   the real quarantine helper with stale A must reject and preserve B exactly.

Current production is expected to fail 2/3/4. That is a diagnostic baseline,
not justification to weaken assertions. Baseline1 failure must block all later
fixtures. Source-loaded module paths must match actual workspace imports;
before/after source hashes must compare, unique reports must be create-only,
zero assertions/any exceptions/cleanup issues/child nonzero/termination or
residual must fail. ExitStack must shut down owned child before Temp removal.
No actual game inputs or queues are authorized. Child handshake proves only
pre-call, not entry into the API. Reports retain compact hashes/diffs, not raw
queues. If validation fails due verifier defect, fix only its owned file in a
new bounded repair step; do not infer a production failure from invalid setup.

## Design findings that constrain the subsequent implementation

Colony Plan audit verified direct bypasses at advance lines972-982 and helper
1035-1057/abort1061+. Public custody APIs are `publish_pending_command`1082+
and `transition_pending_envelope`1245+. Recommended eventual outer order is
lifecycle -> plan, preserving whole publication/abort check-transition window.
Plan lock itself is O_EXCL, non-reentrant, and has unsafe stale-mtime deletion;
it remains a distinct unresolved concern. No reverse runtime lock order was
found by the bounded audit; recheck before implementation.

IMPORTANT compatibility finding: core `_json_bytes` is compact canonical JSON
and compares it byte-for-byte with the original raw pending payload when
`expected_source` is supplied. Current plan's `write_json_atomic` uses pretty
JSON. A naive abort-only API replacement will therefore fail even with a
matching legacy command. Do not solve this by silently omitting expected_source,
rewriting pending in place, or weakening core byte/identity checks. Choose an
explicit migration/new publication design in a separate bounded step.

Advance's second decision-context check currently unlinks the pending path.
Conservative quarantine instead would retain the same stable command ID and
change retry behavior (later duplicate detection pauses). Do not silently
remove the check or preserve raw unlink; make recovery/plan-state semantics
explicit and add tests before broadening implementation. Also inspect whether
approving/executing states are guarded during abort; current explicit checks
name approved and confirming only. No production patch is yet authorized to
the current verifier worker.

Approval retains unknown recovery evidence; empty approval-recovery after a
recovered error can still block later approvals. Full five-consumer journal
integration and automatic recovery availability remain open; action_outcomes
still has direct write/unlink/rename paths. Overall gates still include formal
pipeline/control-packet/Bridge/strategy/bootstrap, bounded offers/history,
named clone/original hashes, reversible G0 native effect+trusted later witness,
save/reload and normal shutdown, all-world observations and sustainable loops.

## Performance diagnostic limits / rollback

B17 proposed `_safe_text`: regex search first, sub only if matched. Root pure
differential:132 combinations equal, 350k no-match calls ~0.105-0.107s old vs
~0.0856-0.0873s candidate. NOT applied; benefit does not prove replay headroom.
Another root pure control: typing.Mapping vs collections.abc.Mapping yielded
equal results on14types (dict, OrderedDict, UserDict, proxy/custom Mapping,
nonmapping values);700k checks ~0.134-0.137s vs~0.071-0.073s. NOT applied;
annotation/introspection and whole-output parity are not yet tested. Neither
microbenchmark permits a timing gate pass or an unchanged full-suite rerun.
For greater benefit, first inspect duplicated canonical_state_hash/deepcopy
call paths rather than assuming safe cross-snapshot caching; no such reuse
design is yet verified. Keep 8s/150MiB/20s and fresh startup reason unchanged.

Rollback now is limited to removing/inversely patching only a verified new
test/doc artifact if rejected, after its writer is terminal. Preserve all
reports and source preimages. No new production rollback is needed because
these latest audits/microcontrols did not edit production. Goal stays ACTIVE;
do not mark complete or blocked at this checkpoint.
