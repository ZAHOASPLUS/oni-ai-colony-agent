# ONI active architecture and release plan — 2026-09-05

Status: active implementation; no live admission and no indefinite-survival claim.

The current source/evidence capability matrix is maintained at
`reports/s1-current-capability-matrix-20260912T1300Z.md`.

## Current checkpoint — 2026-09-12

The additive `oni.functional_buildings.v1` operational telemetry slice is now
formalized and independently verified offline. `FunctionalBuildingTelemetryReader`
is registered and compiles in an isolated Mod copy; the contract covers only
active-world completed instances of `OxygenDiffuser`, `Outhouse`, and
`ManualGenerator`, with fail-closed identity, completeness, freshness, and
`operational` checks. No action authority or live admission is granted.

The full S1 requirement remains open for functional-building ports/occupancy,
reachability, enabled/broken/flooded state, building-specific inputs/outputs,
and clone evidence. Tracked-blueprint worker identity, root/footprint, and
recipe-versus-delivered material lineage now have bounded offline producer
contracts only. The latest evidence is retained at
`reports/s1-functional-buildings-contract-seal-20260912T1100Z.json`.

The next S1 increment now consumes the reader's observed `operational` value
for `OxygenDiffuser` and `ManualGenerator` in Strategy. Explicit false values
set oxygen or power to `at_risk`; present but unusable values remain
`unknown`; only explicit true values can satisfy the compatibility gate. No
new action authority or live admission is granted. Evidence is retained at
`reports/s1-operational-and-snapshot-binding-seal-20260912T1208Z.json`.

The bounded offline readiness fixture's timestamp ordering was also repaired:
advice lifecycle timestamps are now rebound to the fixture's actual source
sample before frame construction. `verify_live_readiness.py` passes all 13
fixture cases; this remains fixture evidence only and does not grant live
admission. Evidence is retained at
`reports/readiness-fixture-order-seal-20260912T1215Z.json`.

The remaining direct frame consumers were audited and hardened: the
Colony Plan secondary source check and priority-pair finalizer now compare
`snapshot_seq` to the raw state. Approval-receipt and action-outcome records
retain their intentionally narrower five-field command source identity and
therefore do not independently claim snapshot-sequence provenance. Evidence
is retained at
`reports/snapshot-seq-consumer-hardening-seal-20260912T1220Z.json`.

The next additive S1 increment publishes optional construction progress and
worker instance fields for each already tracked blueprint cell and registered
blueprint instance. Progress is emitted as a bounded integer percentage only
when the native Constructable sample is finite and in range; worker identity is
emitted only when the worker's KPrefabID can be read. Empty cells omit both
fields, and static fingerprints remain unchanged. The focused contract suite
passes offline; no game launch, clone observation, or live admission has been
performed.

Evidence is retained at
`reports/s1-blueprint-progress-telemetry-seal-20260912T1230Z.json`.

Blueprint mutation checks now share a finite construction-state guard: the
native `-1` not-started sentinel is accepted, while non-finite values, other
out-of-range values, and getter failures refuse placement verification or
cancellation. This remains source-compiled and synthetic-tested only. Evidence
is retained at
`reports/s1-blueprint-construction-guard-seal-20260912T1245Z.json`.

Tracked blueprint records now also expose a canonical root cell and a bounded,
sorted footprint list from the native `Building` placement data. Any missing,
cross-world, duplicate, empty, or oversized footprint is omitted rather than
treated as a valid placement. The identity projection binds multi-cell records
to the root cell; ports and live placement effects remain unverified.

The same tracked-blueprint projection now carries an independent
`blueprint_material_lineage` envelope. It derives required mass from
`Constructable.Recipe.GetAllIngredients(SelectedElementsTags)` and observed
delivered mass only from that constructable's `Storage.GetMassAvailable`.
Missing storage, invalid mass, duplicate ingredients, object/world races, and
over-delivery omit the envelope; unknown item status never becomes an action
precondition. Related-world inventory and reservation totals remain diagnostic
only, the lineage is excluded from fingerprints, and no mutation or live-game
admission is granted. Evidence is retained at the current material-lineage
seal `reports/s1-blueprint-material-lineage-footprint-seal-20260912T1250Z.json`
and its offline verifier `scripts/verify_blueprint_material_lineage.py`.

Tracked Constructable records now also expose optional read-only priority
telemetry from `Prioritizable.GetMasterPriority()`: the enum class is emitted
as a native string and the value is bounded to 1..9. Missing components,
throwing getters, invalid enum values, and out-of-range values are omitted;
the reader never calls `SetMasterPriority`, and the existing fingerprint is
unchanged. This prepares an exact current-priority precondition for a future
construction commitment action without authorizing that action. Evidence is
retained at `reports/blueprint-priority-telemetry-verification.json`.

The read-only priority layer now has a separate pre-authority builder,
`build_constructable_priority_hint`. It requires the exact registered instance,
active world, prefab, canonical root, complete footprint, materials, fingerprint,
paused mode, current priority pair, and observed-zero-progress/no-worker boundary,
then emits a typed `set_constructable_priority`
shape without placing that action in the executor allowlist. The native writer,
later witness, rollback path, and live construction effect remain open; this
hint is therefore an offline contract milestone only. Missing progress is blocked
because telemetry omission cannot distinguish the native -1 sentinel from invalid
raw progress; future native verification must resolve that boundary. Its 31-case evidence
is retained at `reports/constructable-priority-hint-verification.json`.

The Alpha VPS source at commit `a3fb2c565f3b0612aed97fcedf7417d8ab7f659c`
has been downloaded as an immutable local snapshot under
`reports/cloud-reconcile-20260913`. Reconciliation restores the remote food
deficit/depletion forecasts, oxygen/water/power deficits, duplicate-conflict
tracking, native display-clock guards, incident accounting and explicit
sustainability blockers while retaining the local industry phase and operational
building gates. Forecasts remain conditional diagnostics, not runtime safety
certificates. The merged strategy retains all prior local and remote cases;
36 strategy cases and 7 industry tests pass in the isolated merge evidence.
The incident window now separates session lifetime incidents from the current
candidate: 195 safe samples, one incident and 195 further safe samples can
recover decision readiness while preserving the lifetime incident count. Any
current safety-critical risk invalidates the candidate even when the active
phase's local gate is satisfied.

The missing remote `ControllerSequence` API is also restored around the local
at-most-once `ControllerCore`: named selection, whole-sequence prevalidation,
unique intents, step budget, cancellation and shared durable storage precede
adapter access. Only confirmed effects or an explicitly already-satisfied target
advance the sequence. Unknown/terminal outcomes stop it; a timeout or trusted
mismatch can only reobserve the same intent, never redispatch. The merged
controller passes 48 tests, retaining all 24 prior priority tests. This remains
offline adapter evidence with no new game-action permission.

Completed active-world buildings now also have a separate
`functional_building_geometry` sibling payload. It binds each instance to its
world, prefab, orientation, canonical root, and sorted placement cells, with
strict duplicate, overflow, invalid-cell, and read-time world/geometry race
checks. The payload is read-only and does not alter the existing operational
building contract or authorize placement; ports, occupancy, connectivity, and
live functional effects remain open. Evidence is retained at
`reports/functional-building-geometry-verification.json` and the subsequent
geometry seal `reports/s1-functional-building-geometry-seal-20260912T1308Z.json`.

The next sibling payload, `functional_building_ports`, exposes only declared
power, conduit, and high-energy particle port cells gated by the native
BuildingDef flags and rotated Building cell getters. Every record is bound to
the active world and re-read for race detection; connection status is always
`unknown`, while occupancy, reachability, and effects are intentionally
outside this slice. Its contract is read-only and
cannot authorize an action. Evidence is retained at
`reports/functional-building-ports-verification.json` and the ports seal.
The seal is `reports/s1-functional-building-ports-seal-20260912T1325Z.json`.

Completed active-world buildings now also expose an independent
`functional_building_occupancy` payload. `Structure.IsEntombed()` is reported
as nullable state, while `OccupyArea.GetOccupiedGridCells()` supplies actual
occupied cells when the component is initialized; placement offsets are never
used as a substitute. Missing optional components remain null, invalid or
mixed-time cells fail closed, and the payload is read-only with no reachability
or live-effect claim. Evidence is retained at
`reports/functional-building-occupancy-verification.json` and the occupancy
seal `reports/s1-functional-building-occupancy-seal-20260912T1345Z.json`.

The health sibling now adds nullable `BuildingEnabledButton.IsEnabled` and
`BuildingHP` hit-point, broken, and repair-needed state for completed
active-world buildings. Missing components remain null, hit points are checked
against their maximum, and identity/world re-reads reject mixed-time results.
The slice does not claim flooded or reachable state, does not repeat
operational telemetry, and cannot authorize mutation. Evidence is retained at
`reports/functional-building-health-verification.json` and the health seal.
The seal is `reports/s1-functional-building-health-seal-20260912T1455Z.json`.

The flooded sibling now adds nullable native `Floodable.IsFlooded` state for
each completed active-world building. Missing `Floodable` components and
getter failures remain `null`; world/identity re-reads reject mixed-time
snapshots. This is a read-only observation: it does not infer liquid quantity,
hazard safety, reachability, or functional status and cannot authorize an
action. Evidence is retained at
`reports/functional-building-flooded-verification.json` and the flooded seal
`reports/s1-functional-building-flooded-seal-20260912T2200Z.json`.
The installed-assembly member audit is recorded in
`docs/functional-building-flooded-api-audit-20260912.md`.

Reachability API triage is recorded in
`docs/reachability-api-audit-20260912.md`. The native surface is sufficient
for the optional `Workable` sibling; any runtime value remains unadmitted
until its contract, race checks, and clone-only runtime evidence are verified.

The optional `functional_building_reachability` sibling is now implemented.
It reads `Workable.GetCell/GetOffsets` with
`MinionGroupProber.IsAllReachable`, keeps missing Workable/prober state
nullable, and never calls `RefreshReachability()`. Contract evidence is in
`reports/functional-building-reachability-verification.json`; the slice is
still offline-only and does not certify food access, hazard safety, network
connectivity, or live effects.

Power network API triage is recorded in
`docs/power-network-api-audit-20260912.md`. The installed assembly exposes
read-only `Wire.IsConnected`, `Wire.NetworkID`, and utility-manager network
queries. The bounded power-membership sibling is now implemented below;
mutation and network-rebuild methods remain explicitly excluded.

The `functional_building_power_network` sibling is now implemented for
optional `Wire` components on completed active-world buildings. It reports
nullable connection and network identity, rejects disconnected records that
carry an id, and never calls network mutation/rebuild methods. It does not
claim circuit sufficiency, power production, load coverage, conduit endpoint
binding, or live effects. Evidence is retained at
`reports/functional-building-power-network-verification.json` and the seal
`reports/s1-functional-building-power-network-seal-20260912T2300Z.json`.

The `functional_building_utility_network` sibling now reads network identity
at declared conduit input/output cells. Gas/liquid use the native conduit
manager; Solid uses `Game.solidConduitSystem`. Invalid or unknown cell/manager
reads stay nullable, and no flow or operational conclusion is emitted.
Evidence is retained at `reports/functional-building-utility-network-verification.json`
and `reports/s1-functional-building-utility-network-seal-20260912T2330Z.json`.

The `functional_building_logic_ports` sibling now reads `LogicPorts`
input/output declarations, rotated port cells, connection requirements, and
nullable `IsPortConnected` state for completed active-world buildings. Missing
logic components remain explicitly unknown. The reader never sends signals,
refreshes visualizers, or mutates network membership, and it makes no claim
about logic values, control semantics, causality, or functional effects.
Evidence is retained at
`reports/functional-building-logic-ports-verification.json` and the seal
`reports/s1-functional-building-logic-ports-seal-20260912T2350Z.json`.

The `functional_building_utility_endpoint` sibling now checks each declared
utility cell with the native manager's `GetEndpoint` and compares an
`FlowUtilityNetwork.IItem` owner to the active building. It reports registered,
unregistered, and unknown ownership while preserving input/output direction
and conduit type. It never rebuilds networks or changes endpoint membership;
registration does not imply flow, capacity, or functional output. Evidence is
retained at `reports/functional-building-utility-endpoint-verification.json`
and `reports/s1-functional-building-utility-endpoint-seal-20260912T1630Z.json`.

The `functional_building_utility_topology` sibling now reads the native
`FlowUtilityNetwork` source, sink, and conduit counts for each declared utility
network. Missing networks and unavailable topology stay unknown. These counts
describe membership topology only; they do not prove flow, capacity, delivery,
or building output, and the reader never mutates or rebuilds a network.
Evidence is retained at `reports/functional-building-utility-topology-verification.json`
and `reports/s1-functional-building-utility-topology-seal-20260912T1700Z.json`.

The `functional_building_utility_contents` sibling now reads instantaneous
contents at declared utility cells. Gas and liquid records expose element,
mass, temperature, and disease count; solid records expose pickupable
presence only. This is a point-in-time observation and does not claim flow,
capacity, delivery, or functional output. The reader is strictly read-only.
Evidence is retained at `reports/functional-building-utility-contents-verification.json`
and `reports/s1-functional-building-utility-contents-seal-20260913T0030Z.json`.

The paired `agent.utility_flow` analyzer computes a gas/liquid mass change rate
only when two complete snapshots share the same world, instance, endpoint cell,
content kind, and element with a positive bounded interval. Solid content,
identity changes, world changes, element changes, and unknown mass remain
unknown. The rate is observational and does not establish production,
delivery, capacity, or causality. Evidence is retained at
`reports/utility-contents-rate-verification.json`.

The sustained `agent.utility_flow` analyzer requires at least three complete
snapshots, evaluates every adjacent interval, and reports per-endpoint rate
arrays plus a bounded stability flag. Any unknown interval fails the complete
summary closed; stability still does not establish production, delivery,
capacity, or causality. Evidence is retained at
`reports/utility-contents-sustained-verification.json`.

The functional-building operational contract now accepts the exact installed
oxygen prefab `MineralDeoxidizer` alongside the legacy `OxygenDiffuser` id.
The Python rule layer groups those two native ids under the oxygen diagnostic;
the raw native id remains in telemetry. This is an installed-build identity
alias backed by the current Assembly-CSharp audit, and does not certify oxygen
production or recovery effects. Evidence is retained at
`reports/functional-buildings-verification.json` and
`reports/s1-functional-building-operational-alias-seal-20260912T1600Z.json`.

The functional-building reader now cross-checks each `BuildingDef.PrefabID`
against `KPrefabID.PrefabID()`. A missing, unreadable, or conflicting native
tag fails the complete observation closed; it is never silently treated as an
alias. This protects the raw identity surface while leaving production and
functional-effect semantics unclaimed. Evidence is retained at
`reports/functional-buildings-verification.json` and
`reports/s1-functional-building-identity-crosscheck-seal-20260913T0100Z.json`.

## Current commander checkpoint — 2026-09-07

Latest recovery/next-action packet: `recovery-command-20260907-a09a9.md`.
Latest bounded acceptance: `b18-commander-validation-20260907.md` and
`a09a8-commander-validation-20260907.md`.
Both A09A6 repair owners have delivered. Fresh boundary 4/4 (34 assertions),
custody 3/3 (80 assertions), and original legacy 30/30 passed at frozen hashes.
The subsequent official Bridge matrix FAILED: 14/15 cases passed, while the
10,000-line startup-bound live replay took 8.082 seconds against the unchanged
8.0-second gate. All 110 source hashes were unchanged in that earlier run.
That checkpoint retained seven approval staging directories; A09A8's updated
total is recorded below. No live admission is granted.
Both follow-up read-only audits are complete. Original Colony Plan 5/5 static
baseline passed unchanged. Its new cross-process custody verifier has now
been delivered and reviewed, including full-sample and valid-replacement
positive-control repairs. The frozen old-source run now reproduced all three
targeted defects: both plan APIs mutated while lifecycle custody was held, and
stale quarantine payload A overwrote replacement B. The valid lifecycle and
fresh B positive control passed. Source hashes stayed fixed, children exited
normally, and fixture cleanup passed. Exact report and current repair contract:
`a09a7-plan-custody-baseline-20260907.md`.
Both A09A8 Luna Max writers are now terminal. The additive exact-source
observation API and Plan custody adapter passed root review and nine frozen
offline suites: 93/93 cases. All 113 Python/PowerShell source files were
unchanged during validation. Original fixtures cleaned up; seven new approval
staging evidence directories remain intentionally retained (fourteen total).
No game or real queue was used. B18's user-visible Luna Max task, `ONI B18
安全文本性能优化` (`01a07a93-894b-7521-a739-708a2328611b`), is now terminal.
The exact safe-text guard passed pure differential 24/24 and original strategy
14/14. Root's dedicated replay passed all gates at 7.7361039 seconds and
135.171875 MiB. The unmodified official Bridge suite then passed 15/15,
399 assertions, with startup-bound initial replay 7.897 seconds / 137.590 MiB.
All 114 source hashes stayed fixed; helper/Temp cleanup passed. The prior
8.082-second failure above remains preserved, not overwritten. New exact
evidence is in `b18-commander-validation-20260907.md`. This one pass has only
0.103 seconds of headroom and is not a robustness guarantee. The A09A9 read-only
audit is complete and original confirmation 4/4 plus outcomes 9/9 passed.
A fresh Luna Max task, `ONI A09A9 确认归档竞态验证器`
(`01a07aae-0f94-7531-9451-25a0c8ec3a0b`), delivered a new four-case safety
verifier with memory compile only and is now terminal. Root's full read-only
review found evidence/containment blockers; it must be locally repaired before
execution, with no production edits. See `a09a9-verifier-review-blockers-20260907.md` and
`a09a9-confirmation-custody-baseline-20260907.md`. Remaining consumer/bootstrap/live gates are still open;
these offline passes do not grant live admission.
The older `recovery-command-20260907-a09a6.md` preserves the pre-repair failure
history and recovery constraints; its active-worker statuses are superseded.

This checkpoint supersedes the historical running-task and gate statuses below,
not the overall release requirements. Original saves, Mod/configuration, and
real queues were not used or mutated by these offline tests; no current live
hash inventory or live acceptance is claimed.

- B16 dedicated hot-path run passed after independent source-hash checks:
  `ONI_B16_ASTRA_REVIEW_20260907_POSTPATCH_02`, five exact input/output classes,
  15/15 startup-sanity gates, 24/24 benchmark gates, 10,000 records in
  7.7866534 seconds at 135.08203125 MiB peak working set. This has only about
  0.213 seconds of timing headroom; it is not the official integrated Bridge
  reliability gate. See `b16-production-hotpath-20260907.md` for exact hashes.
- Original 14-case pure strategy regression passed at the same frozen
  strategy/state-quality/field-contract hashes. Report:
  `../reports/b16-strategy-hotpath-20260907/runs/ONI_B16_STRATEGY_REGRESSION_20260907_01/reports/strategy-verification.json`;
  SHA-256 `926298E53787AB12D11D32437B1597ED99BF52CD8BA785FAFD168789A067B21A`.
- A09A3 real cross-process approval baseline reproduced two integration defects:
  approval completed in 31 ms while another process held lifecycle custody;
  normal approval left `approval-recovery`, which strict inventory rejected.
  Valid-approval fixture passed 8/8; race and post-approval inventory failed.
  Source hashes were stable, child exited 0, and all temporary roots were
  removed. Failure report retained at
  `../reports/a09a3-approval-custody-integration-20260907T043520376420Z-37684-f03f366fd4/report.json`.
- A09A3 verifier was subsequently hardened to check every held-lock sample,
  require normal child exit without forced termination, stop a race after an
  invalid baseline, and label handshake evidence as pre-call. That revision
  has only an in-memory compile, not an execution pass.
- Current bounded implementation is approval entry-point custody integration,
  not a five-consumer rewrite. Approval must hold the shared lock through
  validation, claim, publication, recovery, and cleanup; old unknown recovery
  evidence must block further approvals. Other queue/plan/outcome consumers
  and a unified lifecycle journal remain separate, unaccepted gates.

Next after this single-entry adapter: independently review its diff, freeze
sources, run the hardened cross-process verifier and all 12 transaction plus
18 frame-binding cases without overwriting historical reports, then continue
remaining consumer integration. Formal full-matrix, bounded offer/history,
bootstrap wrapper, and live clone/G0 gates remain pending. A09A core tests and
B11 targeted import-attestation tests are not substitutes for those gates.

## Historical checkpoints

Execution reassessment at 2026-09-06 05:48–05:50 +08: the user reported quota
reset; live usage showed 7 percent used in the returned weekly window and no
limit-reached classification. Actual Luna Max assistant/tool events were then
verified in two new, disjoint-scope tasks: `01a0738a-b1f5-74d2-984d-13705999be2c`
(“ONI A08B3：混合事件证书回归修复”) and
`01a0738c-df79-7b82-8fa8-3df960840954` (“ONI B11：bootstrap依赖锁定补全”).
They are running, not accepted repairs. No reset credit was consumed and no
model fallback was used. This supersedes the execution-unavailability note
below, not any failed safety gate. Formal bootstrap must wait for all source
writers to finish; custody completion and whole-objective gates remain open.
Latest review: `offline-review-20260906.md` supersedes the task-status and
acceptance observations below. Certificate-only checks pass, but the actual
Bridge mixed-incident candidate test fails; formal bootstrap's held-source
list also omits three current imports. A09A-2 Luna execution terminated for
workspace credits without a repair. No live gate is passed by these results.
Scope: the original complete survival objective, not merely the currently
implemented pause/approval substrate. This document supplements the detailed
S1–S9 inventory in `survival-action-gap-audit.md`; it does not replace its gates.

## Objective and authority

Use fresh, trustworthy observations to control a colony through bootstrap,
stabilize, expand, space, and sustain. Oxygen, accessible food, sanitation,
usable water, delivered power, and critical-zone temperatures take precedence
over expansion. Final capability requires renewable supply, verified demand
coverage, reserve margins, and recovery from specified independent failures.

“Perfect operation” is a design target, not a result established by current
tests. Every accepted action needs stated assumptions and a checked safety
envelope. Missing observations produce an observe request, not a guessed
action. No finite test establishes unconditional survival for infinite time;
the operational requirement is continuous safe control with revocable,
window-bound evidence, explicit fault assumptions, and ongoing monitoring.

The latest user routing is Astra command/review with Luna Max execution in
separate tasks. This supersedes the older Sol/Spark routing for these tasks.
An app dispatch is not execution evidence: inspect the destination rollout.
Main review owns release acceptance; a worker's pass is only a review input.

## Fresh restart evidence

At 2026-09-05 18:34 +08:00 the read-only process census found no ONI game,
normal Bridge, or old `verify_automation.py` process. The old execution handle
could therefore not establish a successful exit. No source save, normal Mod
configuration, or real command queue was mutated in this restart audit.

Transport checks at 18:34, 18:37, 18:46, and 18:56 +08:00 reported stable
120-second windows, no network disconnects, and no fresh transport helpers.
They authorize starting bounded work, not changing the network during a run.

The fresh `python -u scripts/verify_automation.py` run completed normally with
exit 0 at 18:41:58 +08:00. Its 13 result groups passed: static Probe checks,
362,880 survival-alert permutations, and 11 offline scenarios. The stderr file
was empty. Artifacts:

- `reports/a08-automation-20260905-1840.stdout.json`, 21,629 bytes,
  SHA256 `2DB44466DB1B8B832607FD2E717E5DE7F777808A8782A1E8344C4F39B98D7B62`.
- `reports/a08-automation-20260905-1840.stderr.log`, zero bytes.

Inputs recorded before the independent fixes:

| Input | SHA256 |
| --- | --- |
| `agent/rules.py` | `78F6EE866AE9E366573FC7295EEC9BF2291BAB11969B63E41E58B39AE32922E9` |
| `agent/decision.py` | `1BBDDBFAEA6C0F05D18C04A5673DFB129E5C6CDA3BB96EC7042A4D69A4CC8CBF` |
| `agent/approval_receipt.py` | `27266A6FFF338E45B8C4497FC22AE21A0A6BE62457AA3A26D3C9134ADE27CFE3` |
| `scripts/verify_automation.py` | `8432813218083EB7932374A94F0D794B73A26D2BA1A2D796D4C50D27A6B14656` |
| `scenarios/scenario_manifest.json` | `90152F6516FC247D314D7A3B68D5910F7CD09111D03F1C201C991F43A90A1841` |

This passing run does **not** cover the certificate inconsistency below and
must be rerun against the final integrated source hashes.

## New negative evidence: certificate parity

The main review reproduced an offline authorizing disagreement with genuine
seven-sample low-oxygen history plus a same-frame suffocation incident:

1. The projected suffocation witness was changed to inactive telemetry.
2. The certificate's eligible/selected payload was changed to low oxygen,
   with empty blockers and the corresponding command-hint hash.
3. The ordinary DecisionFrame payload digest was recomputed.
4. `validate_decision_frame` and `validate_approval_inputs` both accepted.
5. The actual `arbitrate_survival_emergency` returned `blocked` for that state
   and those incident records.

Observed result was `frame_accepted=true`, `approval_inputs_accepted=true`,
`fresh_arbiter=blocked`. No game command was approved or executed. The candidate
pipeline's additional full-state re-arbitration blocks this variant; direct
approval's certificate-only validation was inconsistent. Fixing only the
single inactive string is insufficient: missing, invalid, contradictory,
duplicate, or stale higher-priority evidence must not authorize a lower choice.

## Bootstrap evidence precedence

`reports/live-runtime-bootstrap-verification.json` is an older 2026-09-01
14:08 +08:00 report: two rounds, 466 scenarios, 3,728 assertions, passed.
A later worker report at 20:01 +08:00 failed at zero complete rounds with
`abort moved-Bridge attack did not preserve both trees` (10 static scenarios,
80 assertions). Both explicitly say `game_launched=false` and
`real_klei_writes=false`.

The failed worker evidence remains at:
`C:\Users\user\AppData\Local\Temp\rm7kxv1h0ff4nw5tj2hd2d0r8v0\worker-live-runtime-bootstrap-verification.json`.
Its preserved test root is evidence, not a directory to reuse or clean.
The older canonical pass does not override the later failed run. A current
production-wrapper receipt with unchanged attested sources and successful
cleanup is required before treating this gate as passed.

## Control-plane architecture

Separate five kinds of authority rather than giving one generated JSON file
all of them:

1. **Observation admission.** Pin producer/session/build/save/world identity;
   validate sequence, capture time, quality contracts, complete required scope,
   and writer health. Preserve enough source evidence to reproduce a decision.
2. **Domain evidence and phase gates.** Derive usable inventories, rates,
   demand, reserves, topology, and failure-domain claims from admitted samples.
   Unknown or incomplete scope cannot satisfy a phase gate.
3. **Single deterministic next step.** Emergency containment arbitration and
   domain repair planning generate at most one mutating proposal per decision.
   Strategy cannot bypass the emergency arbiter or silently unpause.
4. **Offer, human claim, and immutable command custody.** An unclaimed proposal
   has no execution authority. Claim freezes exact observed intent and policy.
   Queue transitions preserve one owner; unknown outcomes block new mutations.
5. **Effect and recovery evidence.** Native acknowledgement is not sufficient.
   A later trusted observation must establish the exact intended effect and
   invariants. Reversible actions need an exact inverse witness; irreversible
   construction and launch require explicit forward-recovery plans.

A `ControlPacket` is a bounded summary, never executable authority. Hashes
bind content but are not signatures and do not prove its producer was honest.
The trust model covers malformed/stale/torn input, contradictory projections,
restarts, ownership races, and unintended local changes. A fully compromised
host cannot be made trustworthy merely by adding another self-authored hash.

## Active implementation ownership

| Slice | Task | File ownership | Exit evidence |
| --- | --- | --- | --- |
| A09A custody | `01a07125-2adc-7533-9317-b303fcba460e` — ONI A09A：命令归属竞态与恢复验证 | shared custody module; queue, approval commit, necessary publishers/Bridge/plan integration; dedicated tests | deterministic cross-process interleavings, crash recovery, unique owner, old regression preservation |
| A08B certificate parity | `01a07130-379d-7540-83f4-51554bb02b09` — ONI A08B：生存仲裁证书一致性修复 | rules, decision validation, approval receipt; dedicated certificate tests | exact negative case rejected; valid selected/none/blocked frames preserved; both paths semantically consistent |
| B10 bootstrap | `01a0713a-63fa-7b61-8287-968073fbe240` — ONI B10：隔离 bootstrap 故障修复与两轮验收 | bootstrap manager/verifier/worker/wrapper and their dedicated tests | current full two-round wrapper, unchanged source hashes, no real game/Klei access, normal exit and cleanup |

All are local Luna Max tasks with explicit file and safety boundaries. This
table records assignment, not completion. Integration must recheck hashes
because independent test runs can import concurrently changing dependencies.

## A09B: bounded offer refresh, not an audit deletion job

After custody is reviewed, remove per-frame materialization of unclaimed
mutating commands. A proposal lineage binds session, save, target world,
incident identifier, and episode opening identity. Each fresh revision binds
the exact source frame/state/policy. Repeated evaluation of an identical revision
must be a no-op, including bytes and mtime.

The sole overwriteable object is a non-authorizing active offer. A human claim
under lifecycle custody freezes the exact revision into the existing command
lifecycle. Refresh must not invalidate or rewrite a claimed command. Any
claim ambiguity blocks further proposal/claim rather than recreating an offer.
Frame pruning must keep every active offer/claim/journal/nonterminal reference.

The command-pipeline readiness scan now uses the shared stable file reader for
all active and unresolved-failure stages. Hardlinks, symlinks/reparse points,
non-regular files, and read-time identity or size changes become explicit
unreadable-record blockers instead of executable candidates. This is still an
offline ingestion safeguard; the readiness bundle and state-log readers need
the same physical-file treatment, and no command was dispatched by this work.

Acceptance must include at least 10,000 fresh frames in one episode: one active
slot, zero newly generated rejected records, unchanged protected records,
deterministic crash recovery, and both refresh-versus-claim orderings. Also test
many resolved/reopened episodes; moving each old offer to a permanent retired
directory would simply move the unbounded-growth defect elsewhere.

Existing user audit records and old rejected files remain untouched. New
unclaimed offers may be retired only according to their explicitly
non-authorizing lifecycle. Terminal action evidence retention is a separate
policy: keeping every action's full evidence forever consumes unbounded
storage. No component may advertise globally bounded disk use while retaining
an unbounded terminal archive. Storage budgets, evidence pinning, diagnostics
rotation, and a safe disk-pressure state are required; deleting historical
audit evidence is not silently included in offer refresh.

## Observation history and evidence provenance

Episode counters and a self-consistent certificate do not prove that all
required snapshots were received. The next evidence slice must root window
claims in admitted per-sample records, not projected counts alone.

The live-readiness bundle reader now applies physical-file checks to each
generation member: it rejects links/reparse points, non-regular files,
hardlinks, and path/open-handle identity, size, or mtime changes while reading.
The check is read-only and only strengthens admission safety; Bridge-root
ancestor links and the streaming `state.jsonl` reader remain separate gaps.

Required design properties:

- A rolling admitted-sample window records producer/session/save/world
  identity, monotonic event and snapshot sequence, timestamp, state hash, and
  exact required contract fields. Gaps, untrusted samples, scope changes,
  producer faults, or clock discontinuities revoke eligibility.
- The Bridge, not an incoming advice/frame payload, constructs the window.
  Candidate construction and approval revalidate against immutable pinned
  evidence owned by the Bridge ingestion boundary. A missing sample is a
  missing proof, not an invitation to interpolate.
- Claim freezes only the evidence needed by that command into create-only
  storage. Unclaimed rolling history is bounded; all nonterminal commands pin
  their evidence. A restarted process restores authority only after validating
  its source chain and fresh producer/session continuity.
- Compact projections need a well-defined source reconstruction path. Shared
  pure semantic reducers prevent divergence, while independent negative tests
  check that a shared implementation is not consistently wrong.
- Hash/identity continuity provides tamper evidence relative to the trusted
  ingestion boundary. It is not protection against an administrator who can
  replace the producer, every archive, and all pinned roots.

## Release order and whole-objective acceptance

The next single action after all three active slices finish is independent
integrated review at a stable source snapshot, not a game launch. Run the
complete approval/transaction/frame/pipeline/ControlPacket/Bridge/strategy
matrix against those same hashes, then complete the offer/history gates.

Before live admission, require current bootstrap wrapper evidence, no
ambiguous queues or helper processes, verified original `.sav`/preview/config
hash inventory, and an explicitly named verified clone. Publish a fresh
checkpoint before each mutation. G0 is an exact duplicant-priority
`from -> to -> from` pair with fresh source, native outcome and later trusted
effect witnesses, save/reload continuity, disabled final policy, empty queues,
normal exit, unchanged originals/config, and no leftover helpers.

After G0, the full roadmap remains:

1. All-populated-world observation and explicit target-world binding.
2. True, tested asynchronous blueprint timeout/rollback semantics.
3. Read-only functional-building catalog and instance telemetry for Outhouse,
   OxygenDiffuser, and ManualGenerator, before expanding mutation authority.
4. Exact reversible placement/cancellation, followed by native construction
   commitment and measured functional recovery.
5. Oxygen, accessible renewable food, usable water/sanitation, delivered
   critical power, and critical-zone temperature control loops.
6. Resource/dependency/fault-domain evidence and a production sustainability
   certifier whose proofs are revoked on the next contradictory sample.
7. Mission readiness, launch, return, and simultaneous base-reserve protection.
8. Long-duration, multi-failure recovery scenarios and continuous regression
   detection across all populated worlds.

Current source inspection still finds Ladder-only building and Wire-only
utility allowlists, active-world observation limitations, and no production
sustainability certifier module. The functional-building reader now exists as
an operational-only, fail-closed slice for Outhouse, OxygenDiffuser, and
ManualGenerator, with a strict offline contract and clean compile evidence;
it now provides bounded construction progress, worker instance telemetry,
canonical root/footprint, and recipe-versus-delivered material lineage for
tracked blueprints. Functional-building ports/occupancy, reachability,
enabled/broken/flooded state, or live clone and multi-world acceptance remain
unverified. Passing control-plane tests does not satisfy any
of the missing resource-loop steps.

The decision-frame source now binds `snapshot_seq` explicitly and rejects a
missing or future ordinal; control-packet, current-readiness, archive, colony
plan, and command-queue currentness checks carry that binding through. Legacy
survival certificate archives retain a narrow witness-agreement fallback for
audit only. This improves consistency evidence but still cannot authenticate
that every sample originated from a real Probe observation.

No action should be called “completed” solely because a command was queued,
approved, acknowledged, or a green fixture was produced. Completion evidence
must match the real requirement's scope and current build/save lineage.
