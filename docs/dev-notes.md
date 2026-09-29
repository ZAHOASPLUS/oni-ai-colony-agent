# ONI AI Colony Agent V2 Dev Notes

## Current Validation Boundary

The current phase is code and offline-contract work only. Do not launch,
control, or use ONI to validate these changes until an explicit notification
opens a game-validation window. Offline fixtures may prove deterministic
schemas, admission gates, replay isolation, and fail-closed behavior; they are
not evidence that the current game build, mod load, save, or colony behavior
has been validated. Keep runtime claims pending while this boundary is active.

## Additive State-V2 Survival Telemetry

This round extends `oni.state.v2` without changing its schema identifier.
`ProbeSession` now maintains two session-bound clocks: `seq` advances for every
event, while `snapshot_seq` advances only for `state_snapshot`. Consequently,
`snapshot_id` remains `<session_id>:<seq>`, and Strategy uses gap-free
`snapshot_seq` rather than assuming that successive snapshot `seq` values are
adjacent. `game_time_cycles` is the continuous UI-one-based value from
`GameClock.GetTimeInCycles() + 1`, separate from integer `cycle`.

The new `observation_scope` explicitly publishes the active world, worlds with
live duplicants, covered worlds, and `complete_for_populated_worlds`. The
current implementation covers only the active world, so a colony populated on
multiple worlds must fail closed for whole-colony certification.

Raw telemetry is intentionally additive and non-concluding:

- `materials.active_world_cells` performs a cross-snapshot rolling scan of
  revealed, non-hidden cells, bounded to 4,096 candidates or 4 ms per call.
  Current-scan progress and the last completed generation are double-buffered;
  aggregate oxygen/water-family masses are always `estimated`, bound to their
  completion timestamp, and stale after 900 seconds. They exclude storages,
  conduits, bottles, pickupables, hidden cells, and fogged cells.
- `power` reports registered active-world storage-battery joules/capacity
  after excluding transformer-backed `Battery` components, the
  energy-weighted battery percentage, instantaneous active consumer watts,
  active rated generator watts, and raw component counts. Rated generation is
  capacity, not delivered production; counts are not redundancy proof.
- `temperature.active_world_material_min_c/max_c` are raw positive-mass-cell
  extrema. They are not critical-zone policy extrema, so
  `temperature.min_c/max_c` and `critical_zones_safe` remain unsupported.
- `rockets` reports cluster-wide raw counts. Cluster-space mode partitions
  total into grounded/launching/in-flight/landing; classic mode supports total
  only. Counts do not prove any `space.*` conclusion.
- `production.*`, `consumption.*`, independent-source counts, N-1 safety,
  mission readiness/completion/return safety, base reserves, and sustainability
  remain explicit `null`/`unsupported` fields until a versioned proof producer
  exists.

The active-world Grid reader refuses more than 500,000 candidate cells, never
publishes partial sums as a completed aggregate, resets both buffers on
session/world/bounds changes or exceptions, and excludes vacuum from material
temperature extrema. A zero contract-valid visible-cell oxygen aggregate may
raise a conservative risk, but no positive rolling aggregate can certify an
oxygen/water reserve or critical-zone temperature safety.

`resources.food_kcal` is now an explicitly registered/non-private active-world
inventory sum in kcal; internal `Edible.Calories` is divided by 1,000. Complete
enumeration is `observed/complete`, the 10,000-item bound is
`estimated/bounded`, and unavailable/error/non-finite paths fail closed. The
unverified `RationMonitor` fallback was removed. Because registry membership
does not prove reachability or ration permission, no registered value is
converted into a food runway. A complete low sum may produce only a local
diagnostic; an `estimated/bounded` lower bound remains unknown and emits no
command candidate because it cannot prove the total is low. High values cannot
certify accessible food.

For certifying fields, quality is a contract rather than a label. In addition
to `status=observed`, Strategy checks `semantic`, `unit`, `scope`,
`completeness`, exact `source`/`contract_id` where a trusted producer exists,
and the exact `captured_at` binding. It
also resolves the binding to a timezone-aware timestamp; rolling aggregates
must be no older than their field-specific limit. Derived
window rates or conclusions also require the expected `authority`, valid
SHA-256 `input_hash` and `policy_hash`, and a non-empty `policy_version`.
Those self-declared fields are shape checks only. `evaluate_strategy` defaults
every proof-required field to unverified unless its caller passes an explicit
in-process verifier that recomputes the input hash and checks a policy
hash/version allowlist. The production Bridge currently passes no verifier;
only Strategy fixtures install the explicit test verifier. Current Probe raw
gauges carry provenance metadata but do not invent proof authority.
No live accessibility-and-permission food producer exists; that hypothetical
certifying field therefore requires explicit proof authority and an in-process
verifier, while the current registered-inventory contract remains diagnostic.

All statements in this section describe implemented code and offline
contracts. Static inspection, deterministic fixtures, the telemetry Mock
scenario, Strategy regression, and Decision Frame regression are not evidence
that the current Probe loads or emits these values in ONI. Game/Probe proof is
still deferred until notification.

## Build And Install

```powershell
.\scripts\build-mod.ps1 -Configuration Release
.\scripts\install-dev-mod.ps1 -Configuration Release
.\scripts\write-build-manifest.ps1
```

The build fails on warning codes outside `docs/build-warning-baseline.json`.
Installation copies the DLL/PDB/manifests to the ONI Dev mod directory and
fails if the installed DLL SHA-256 differs from the build artifact. The local
manifest records ONI changelist/branch, mod version, Managed reference hashes,
build artifacts, and installed artifact.

## Test Save

`prepare-test-save.ps1` and `restore-test-save.ps1` are retired and refuse to
run: their destination check could overwrite an original save (code review
2026-09-26). Final V2.7 acceptance does not restore that baseline between
scenarios. First capture the pre-boundary, create the exact named loader clone,
and run the environment `prepare --apply` below so the original configs, Probe,
and Bridge are frozen. Then complete the separately authorized runtime bootstrap
transaction below through `installed_ready`, freeze its exact `result.json` raw
SHA-256, and reserve a save path that does not exist:

```powershell
py -3 .\scripts\manage_save_lineage.py reserve `
  --run-id v27-final-<timestamp> `
  --save-path "$env:USERPROFILE\Documents\Klei\OxygenNotIncluded\save_files\ONI-AI-V27-<timestamp>.sav" `
  --mod-version 0.7.0
```

Create that exact path with in-game Save As, reload it, then activate the
lineage:

```powershell
py -3 .\scripts\manage_save_lineage.py activate --manifest <manifest.json>
```

After every runtime scenario, disable actions, explicitly save to the same file,
reload it, and record one checkpoint. Do not use autosaves as checkpoints. Keep
the game paused during supervised action windows to avoid unrelated colony
progression.

For the G0 priority change/restore gate, a confirmed command automatically
publishes its create-only approval bundle before `confirming -> done`; failure
to publish or revalidate it leaves the command in `confirming`. After each
command reaches `done/status=confirmed`, run
`finalize_priority_pair_lineage.py freeze-command` before the corresponding
save/reload, then bind the resulting `priority-command-freezes/<step>.json` to
the corresponding checkpoint with `--evidence`. Create `priority_forward`
before opening the inverse policy window. After the inverse freeze and final
save/reload, exit ONI normally, wait for Bridge's adjacent terminal tail and
complete terminal outputs, then stop Bridge cleanly. Only then create the
offline `priority_inverse` checkpoint and run `finalize ... --report
<manifest-dir>\priority-pair-finalization.json` inside the pair-creation
freshness window. The exact commands and failure contract are in
`docs/protocol.md` under **One-Save Validation Lineage**.
Finalization also runs `command_pipeline_readiness(...)` against the exact
current `latest_state.json` and disabled policy. Empty nonterminal directories
are insufficient: any unresolved `failed/` mutation, ambiguity,
duplicate id, stale pending record, invalid active record, or non-ready mutation
proposal rejects the pair. The create-only report binds the resulting pipeline
summary and canonical gate hash.

## Bridge

```powershell
.\scripts\run-bridge.ps1
.\scripts\run-bridge.ps1 -Once
```

Bridge defaults to rule-only planning. `health.json`, session output folders,
and the summary should refresh while a save is loaded. A current session with
no snapshot must show `waiting_current_session_snapshot`, never an old save.

Live trust requires `session_started` as the first record at `seq=1`, followed by strictly
increasing, gap-free snapshots whose envelope, save/world/build/mod identity,
timestamp, interval, quality map, and writer health pass
`snapshot_trust_blockers`. A snapshot may be retained for diagnostics without
that boundary, but `health.status` must remain `untrusted_snapshot` and it must
not create open advice or a pending suggestion. Never weaken live admission to
make a fixture pass; fixture-only baseline relaxation belongs to `--input`
replay mode. A start received after a snapshot, a repeated start, or a first
start with `seq>1` is a sticky ordering failure and cannot retroactively
certify the retained snapshot.

Keep the two sequence domains separate when reading logs: Bridge session
integrity uses the all-event `seq`, `snapshot_id` is still derived from that
value, while Bridge and rolling Strategy evidence both require `snapshot_seq`
to increase by exactly one. Save events or command acknowledgements between
snapshots are not snapshot gaps. A gap-free all-event stream does not hide a
missing snapshot ordinal.

Offline replay is non-commanding by default:

```powershell
py -3 .\bridge\oni_bridge.py --input .\fixture.jsonl --once `
  --bridge-dir "$env:TEMP\oni-ai-replay"
```

This path reports `replay_suggestions_suppressed=true` and must create no new
pending suggestion. `--allow-replay-suggestions` exists only for command-queue
contract tests and refuses an output directory containing a live state log,
action policy, or command tree. Use a fresh disposable directory if that test
switch is required. `decision-frame.json` remains fail-closed with a
`REPLAY_MODE` blocker in either replay variant.

## Strategy And Canonical Decision Output

`agent/strategy.py` emits `oni.strategy.v2`. Its certified progression is
strictly `bootstrap -> stabilize -> expand -> space -> sustain`; forward
transitions use phase-specific consecutive trusted windows. They require all
three thresholds, not just `PHASE_STABILITY_SAMPLES`:

| Phase | Samples | Wall-clock minimum | In-game minimum |
| --- | ---: | ---: | ---: |
| bootstrap | 3 | 10 s | 0.02 cycle |
| stabilize | 12 | 55 s | 0.25 cycle |
| expand | 24 | 115 s | 1.0 cycle |
| space | 12 | 55 s | 0.25 cycle |
| sustain | 120 | 595 s | 10.0 cycles |

Do not replace the session-bound `PhaseEvidenceTracker` with a single-snapshot
phase label. It requires strictly increasing event sequence/timestamp,
consecutive `snapshot_seq`, stable sample interval, monotonic
`game_time_cycles`, unchanged save/world identity, complete populated-world
coverage, trusted telemetry, and a satisfied current gate. Missing snapshots,
excessive timestamp gaps, interval changes, sequence/time regressions, failed
or unknown gates, or session/save/world changes reset the active window.
Duplicate renders do not add evidence, paused snapshots do not add game-cycle
duration, replay bursts do not bypass wall duration, and safety regression is
immediate. Estimated telemetry may identify risk but cannot satisfy an exit
gate.

`phase_evidence.evidence_window` binds start/end event and snapshot ordinals,
timestamps, game times, sample count, gap-free state, critical incidents, and
a chained SHA-256 `input_hash`. Each accepted sample chains the prior value,
canonical state hash, snapshot id, snapshot ordinal, and event sequence.

`agent/decision.py` defines the strict `oni.decision-frame.v2` canonical
contract. Its builder is pure: pass the copied latest state plus health,
advice, incidents, plan, strategy, and policy; publish the returned dictionary
with one atomic replacement. Every child must carry the same
`source{session_id,snapshot_id,state_hash}` (strategy temporarily also accepts
its existing top-level identity). Missing child identity blocks every readiness
stage; conflicting identity raises `DecisionFrameError` and must leave the last
valid frame intact.

Consumers validate `frame_id` and the complete schema before using
`decision-frame.json`. Do not reconstruct a decision from independently read
`latest_state.json`, `health.json`, `advice.json`, `incidents.json`, `plan.json`,
or `strategy.json`; those are compatibility/diagnostic artifacts and can span
different atomic replacements. There is no loose-file fallback for a missing
or rejected canonical frame. Strategy recommendations remain non-executable
projections and never bypass policy, command approval, or current-state
preconditions. The Bridge writes the frame last, after the compatibility files,
so a valid frame is the generation commit marker.

Decision Frame independently rechecks the bounded Strategy evidence-window
bindings before accepting `ready`: matching phase/session/last snapshot,
`gap_free=true`, zero critical incidents, sample-count agreement, a contiguous
`snapshot_seq` range ending at the Strategy snapshot, a SHA-256-shaped input
hash, exact wall/game duration agreement, and all required thresholds. A
malformed or internally inconsistent change produces `STRATEGY_INVALID` and
blocks advance/sustainability; `frame_id` catches mutation after frame
construction. The frame does not contain all source snapshots, so it cannot
replay the chained input hash independently; that limitation remains part of
the runtime proof boundary.

## Action Policy

Use the lineage-aware wrapper. It refuses dirty/stale state, pending or approved
commands, and manifests that do not match the current Bridge session/hash/world:

```powershell
.\scripts\set-v27-validation-policy.ps1 -Manifest <manifest.json>
.\scripts\set-v27-validation-policy.ps1 -Manifest <manifest.json> `
  -EnableActions -AllowedActions set_paused
```

Only the exact current checkpoint hash is allowed while enabled. Disable the
policy immediately after each scenario and before every save/reload transition;
disabled policy revokes all save hashes.

## Runtime Commands

Inspect current state:

```powershell
py -3 .\scripts\verify_runtime.py latest
```

Create, inspect, and explicitly approve a pause command:

```powershell
py -3 .\scripts\verify_runtime.py prepare --action set_paused --paused auto
.\scripts\approve-command.ps1 -CommandId cmd-runtime-pause-...
```

Research validation requires a known current-build tech ID different from
`research.current`. The approved action replaces the current research queue,
so use a disposable test save and restore the original target with a second
approved command when the scenario requires it:

```powershell
py -3 .\scripts\verify_runtime.py prepare --action set_research --tech FarmingTech
.\scripts\approve-command.ps1 -CommandId cmd-runtime-research-...
```

Batch-A priority validation keeps the game paused and runs a verified change,
the inverse restore, and one wrong-name rejection against the same target:

```powershell
py -3 .\scripts\verify_runtime.py prepare --action set_duplicant_chore_priority `
  --duplicant-instance-id 123 --chore-group Dig --priority 4
.\scripts\approve-command.ps1 -CommandId cmd-runtime-priority-...

py -3 .\scripts\verify_runtime.py prepare --action set_duplicant_chore_priority `
  --duplicant-instance-id 123 --chore-group Dig --priority 2
.\scripts\approve-command.ps1 -CommandId cmd-runtime-priority-...

py -3 .\scripts\verify_runtime.py prepare-invalid-priority-target `
  --duplicant-instance-id 123 --chore-group Dig --priority 4
.\scripts\approve-command.ps1 -CommandId cmd-runtime-priority-invalid-...

py -3 .\scripts\verify_runtime.py collect --phase batch-a --minimum-duration-seconds 0
```

The first target value must differ from the observed value. The restore target
must be the first command's verified `before_state` value. For G0 acceptance,
these commands are not complete as an in-memory pair: each must be frozen and
bound to its own save/reload checkpoint in the strict forward-then-inverse
workflow above.

Batch-B dig validation keeps the game paused and uses one observed safe
candidate. Run the invalid fingerprint case first, then mark and cancel the
same cell. The cancel helper reads and binds the designation instance created
by the verified mark command:

```powershell
py -3 .\scripts\verify_runtime.py latest

py -3 .\scripts\verify_runtime.py prepare-invalid-dig-target --cell 456
.\scripts\approve-command.ps1 -CommandId cmd-runtime-dig-invalid-...

py -3 .\scripts\verify_runtime.py prepare --action mark_dig_cells --cells 456
.\scripts\approve-command.ps1 -CommandId cmd-runtime-dig-mark-...

py -3 .\scripts\verify_runtime.py prepare --action cancel_dig_cells --cells 456
.\scripts\approve-command.ps1 -CommandId cmd-runtime-dig-cancel-...

py -3 .\scripts\verify_runtime.py collect --phase batch-b --minimum-duration-seconds 0
```

The invalid command must be rejected before `accepted` or `executed`. The
verified pair must preserve element, mass, damage, and solidity, hand the exact
designation instance from mark to cancel, and restore the original cell
fingerprint after the asynchronous cancel animation.

Batch-C blueprint validation also keeps the game paused with Sandbox and
Instant Build disabled. Read `blueprint_catalog` and `blueprint_targets` from
`latest`, use the exact material tag selected by the live catalog, and run the
invalid fingerprint case before the positive/restore pairs:

```powershell
py -3 .\scripts\verify_runtime.py latest

py -3 .\scripts\verify_runtime.py prepare-invalid-blueprint-target `
  --cell 500 --material-tags SandStone
.\scripts\approve-command.ps1 -CommandId cmd-runtime-blueprint-invalid-...

py -3 .\scripts\verify_runtime.py prepare --action place_building_blueprint `
  --cells 500 --material-tags SandStone
.\scripts\approve-command.ps1 -CommandId cmd-runtime-blueprint-place-...

py -3 .\scripts\verify_runtime.py prepare --action cancel_building_blueprint --cells 500
.\scripts\approve-command.ps1 -CommandId cmd-runtime-blueprint-cancel-...

py -3 .\scripts\verify_runtime.py prepare --action place_utility_blueprint_path `
  --path-id wirepath1 --cells 600 601 --material-tags Copper
.\scripts\approve-command.ps1 -CommandId cmd-runtime-utility-place-...

py -3 .\scripts\verify_runtime.py prepare --action cancel_utility_blueprint_path `
  --path-id wirepath1
.\scripts\approve-command.ps1 -CommandId cmd-runtime-utility-cancel-...

py -3 .\scripts\verify_runtime.py collect --phase batch-c --minimum-duration-seconds 0
```

The selected utility cells must be ordered four-neighbor cells. Placement may
create only unfinished allowlisted blueprints (`Ladder`, `MineralDeoxidizer`,
`AlgaeHabitat`, `Wire`) through `TryPlace`; each
cancel command binds the positive instance ids observed after placement and
must restore every original empty-cell fingerprint.

Batch-D plan controls run in the Bridge process and must remain absent from the
game executor allowlist. Run the deterministic revision and recovery suite,
then use a current paused ONI session for a no-execution live probe:

```powershell
py -3 .\scripts\verify_colony_plan.py
py -3 .\scripts\verify_colony_plan.py --live
```

The live probe creates one catalogued leaf candidate, then exercises create,
advance, pause, resume, recover, and abort. It passes only when the command
always requires approval, never enters approved/done/failed, and finishes in
quarantine without a game acknowledgement.

Fault injections run only after the game session and policy are verified:

```powershell
py -3 .\scripts\verify_runtime.py inject-stale
py -3 .\scripts\verify_runtime.py inject-replay --command-id cmd-runtime-pause-...
```

The first should enter `quarantine` with `skipped_stale`. The replay must not
add another `accepted`, `executed`, or `verified` stage for that ID.

## Background Checkpoint Controls

Once a source save is manually loaded after a mod restart, create and reload the
reserved validation save without mouse or keyboard automation:

```powershell
py -3 .\scripts\manage_validation_checkpoint.py `
  --operation save_checkpoint `
  --test-run-id <run-id> `
  --save-path <loaded-source.sav> `
  --target-save-path <reserved-new.sav>

py -3 .\scripts\manage_validation_checkpoint.py `
  --operation reload_checkpoint `
  --test-run-id <run-id> `
  --save-path <reserved-new.sav>
```

Run the same save/reload pair after every scenario. Do not leave normal action
commands pending or approved; the checkpoint controller rejects a non-quiescent
queue and requires `action-policy.json` disabled with no save hashes.

## Optional LLM

```powershell
.\scripts\run-bridge.ps1 -Once -EnableLlm -SecretsDir "C:\path\to\local-secrets"
```

The key is read locally and is never printed or persisted. Configure optional
cost rates with `ONI_AI_INPUT_COST_PER_1M` and
`ONI_AI_OUTPUT_COST_PER_1M`. LLM output ranks existing incident IDs only and
never writes `approved` commands.

### Model loop (M1)

`-ModelLoop` (watch mode only) lets the configured model pick one of at most
ten catalogued candidates per decision (`docs/protocol.md`, `model_selected`).
The Bridge publishes the choice as one pending command; nothing runs until a
human approves it:

```powershell
.\scripts\run-bridge.ps1 -ModelLoop -SecretsDir "C:\path\to\local-secrets" <startup expectation arguments>
Get-Content "$HOME\Documents\Klei\OxygenNotIncluded\ai-bridge\model-loop.json"
.\scripts\approve-command.ps1 -CommandId <awaiting_approval.command_id>
```

`model-loop.json` shows the candidates, the model's last choice and reason,
the command awaiting approval, and the recent outcomes fed back to the model.
The policy must enable actions and suggestions for the loaded save and allow the
candidate actions (`set_paused`, `set_research`, `set_duplicant_chore_priority`,
`mark_dig_cells`, `place_building_blueprint`); raise `command_ttl_seconds` (at
most 300) so there is time to read and approve. Priority, dig, and building
blueprint candidates (Oxygen Diffuser, Algae Terrarium, Ladder; each on its
lowest catalogued empty cell) exist only while the game is paused; the research candidates
(`research_<TechId>`, a fixed tier-1 ladder led by `ImprovedOxygen` because the
first live colony consumed about four times the oxygen it produced) are offered
in either pause state. The Probe reports the finished set as `research.complete`
(contract `oni.qc.research.complete.v1`), and the ladder skips those entries;
when that field is unobserved (an older Probe) every entry is offered, a
finished tech is refused by the executor and that refusal reaches the model as
outcome feedback. The loop stands aside while a rule suggests a mutation (the arbitrated
survival pause); other open rule findings, such as idle duplicants or a
non-operational building, are passed to the model as context. `-ModelLoop`
refuses `-EnableLlm`: that older ranker calls the model synchronously inside
each generation. A live clone run of this loop has not happened yet; before
one, `py -3 scripts\check_model_loop_on_recording.py
<bundle>\sessions\<session>\state.jsonl --executor-check` runs it offline on a
recorded session (real Bridge generation, a stand-in model, the real approval
validators and the executor's own command checks).

## Verification Ladder

```powershell
py -3 -m compileall -q .
py -3 .\scripts\test_runtime_compatibility.py
py -3 -m unittest scripts.test_llm_planner -v
py -3 .\scripts\verify_oni_doctor.py
py -3 .\scripts\verify_advanced_actions.py
py -3 .\scripts\verify_colony_plan.py
py -3 .\scripts\verify_automation.py
py -3 .\scripts\verify_bridge_reliability.py
py -3 .\scripts\verify_command_safety.py
py -3 .\scripts\verify_strategy.py
py -3 .\scripts\verify_decision_frame.py
py -3 .\tools\game_verify\verify_telemetry_contract.py `
  --scenario .\tools\game_verify\scenarios\oni_telemetry_contract_mock.json
py -3 .\scripts\verify_live_telemetry_contract.py
py -3 .\scripts\verify_save_lineage.py
py -3 .\scripts\verify_v27_workflow.py
py -3 .\scripts\verify_validation_checkpoint.py
py -3 .\scripts\verify_llm_fallback.py
.\scripts\verify-runtime.ps1 -Phase observer
.\scripts\verify-runtime.ps1 -Phase final -MinimumDurationSeconds 600 `
  -SaveLineage <manifest.json>
```

Then run the project `game_verify_*.json` scenarios. The known behavior failure
must continue to fail as `behavior_fail`; a passing result would mean the
verifier stopped detecting missing effects.

The two runtime commands in this ladder remain deferred while the code-only
validation boundary is active. Run them, and the `game_verify` scenarios, only
after explicit notification opens an in-game validation window.

The scenario form of the telemetry command is Mock-only. For a Probe-backed
generation, pass the loose Bridge files explicitly; the validator reads them
but does not start, control, or inspect the ONI process:

```powershell
py -3 .\tools\game_verify\verify_telemetry_contract.py `
  --snapshot "$env:USERPROFILE\Documents\Klei\OxygenNotIncluded\ai-bridge\latest_state.json" `
  --health "$env:USERPROFILE\Documents\Klei\OxygenNotIncluded\ai-bridge\health.json" `
  --output .\reports\live-telemetry-contract.json `
  --json
```

Live mode does not upgrade fixtures or inject missing fields. Its report binds
the snapshot/session/save/world/sequences/canonical hash and aware validation
time. Snapshot-only acceptance is deliberately not command eligibility:
`command_eligible` becomes true only when the supplied health is `ok`,
trust-ready, blocker-free, and source-identity matched. Use `--now` with an
aware RFC3339 timestamp only for deterministic offline replay. Passing Mock,
live snapshot, Strategy, and Decision Frame validation still does not establish
an action effect, real scan coverage, or evidence duration in-game; those need
scenario-bound runtime evidence.

Completion requires all of the following in the same current-build evidence
set: installed Release hash match, Player.log load marker, one newly reserved
save path, all 14 ordered explicit-save/reload checkpoints, current-only action
policy hashes, A/B/C/D command or control-plane evidence, ten-minute
p95/max/queue/drop budgets bound to the performance checkpoint's pre-save
session, passing runtime marker scenarios, a known `behavior_fail` sample, and
accepted CLI worker results covering at least 40 percent of the delegated work
packages.

## Read-only restart preflight

Before launching a fresh runtime, run the doctor from the project root:

```powershell
py -3 .\scripts\oni_doctor.py
py -3 .\scripts\oni_doctor.py --json
py -3 .\scripts\oni_doctor.py --oni-dir "C:\path\to\OxygenNotIncluded" `
  --bridge-dir "$HOME\Documents\Klei\OxygenNotIncluded\ai-bridge" `
  --secrets-dir "C:\path\to\local-secrets"
```

The command is strictly read-only: it does not start ONI, call the model, move
queue entries, edit a save, or write a report. It checks the Steam appmanifest
`buildid`, current `Assembly-CSharp.dll` SHA-256, the project and Dev Probe DLL
hashes, recent `Player.log` load markers, Bridge freshness/session identity,
queue and policy safety, and LLM key *presence only*. Key material and
`Authorization` values are never printed or persisted.

Exit codes are `0` for healthy, `1` for degraded-but-usable (for example ONI is
not running yet, the Bridge is stale, or no optional LLM key is configured),
and `2` for a blocked or malformed required identity/configuration (for example
an Assembly hash mismatch or an approved command left in the queue). A blocked
result is the signal to rebuild/install against the current game before
starting a new session; it is not evidence of successful gameplay automation.

## Manifest-bound live-validation environment transaction

`scripts/manage_live_validation_environment.py` is the fail-closed environment
bookend for a named-clone action run. Its default and explicit `--what-if` modes
are read-only. A live write requires both `--apply` and an exact
`--confirm-live-run-id`; `--test-root` and the process-state fixture are accepted
only for isolated offline tests. Before either live phase, ONI and
`oni_bridge.py` must be stopped. The current Bridge policy must have actions and
suggestions disabled, an empty save allowlist, empty nonterminal queues, and no
temporary queue files.

Run prepare as a plan first, then repeat the same command with `--apply` and the
live confirmation:

```powershell
python .\scripts\manage_live_validation_environment.py prepare `
  --run-id $ValidationRunId `
  --oni-root $OniUserRoot `
  --evidence-root $EnvironmentEvidenceRoot `
  --boundary-report-root $BoundaryReportRoot `
  --pre-boundary $PreBoundaryJson `
  --clone-manifest $CloneManifestJson `
  --source-save $ImmutableSourceSave `
  --loader-save $NamedLoaderSave `
  --loader-stem $NamedLoaderStem `
  --what-if

# Only after reviewing the plan, capture the exact Apply result:
$PrepareApply = (& py -3 -B .\scripts\manage_live_validation_environment.py prepare `
  --run-id $ValidationRunId `
  --oni-root $OniUserRoot `
  --evidence-root $EnvironmentEvidenceRoot `
  --boundary-report-root $BoundaryReportRoot `
  --pre-boundary $PreBoundaryJson `
  --clone-manifest $CloneManifestJson `
  --source-save $ImmutableSourceSave `
  --loader-save $NamedLoaderSave `
  --loader-stem $NamedLoaderStem `
  --apply --confirm-live-run-id $ValidationRunId | ConvertFrom-Json)
if ($LASTEXITCODE -ne 0 -or $PrepareApply.status -cne 'prepared') {
  throw 'environment prepare Apply failed'
}
$ExpectedEnvironmentManifestHash = [string]$PrepareApply.environment_manifest_sha256
```

Prepare binds the exact pre-boundary and clone-manifest file hashes; lower- and
upper-case path identities; the immutable source pair; `kplayerprefs.yaml`,
`mods/mods.json`, `mods/mods.json.bak`, and `mods/config.json`; the recursive
installed Probe tree; the recursive pre-existing Bridge; content hashes; and
last-write timestamps. It also records a metadata-only recursive snapshot of
all pre-existing `ai-bridge-archives` paths, sizes, content hashes, and
timestamps; those historical files are verified in place rather than copied.
It publishes the backup and initial hash-chained restore journal by one
same-volume directory rename.

`prepare-v27-runtime.ps1` is a four-phase transaction wrapper, not a direct
installer. `-BuildRelease` is workspace-only and returns the exact four-artifact
receipt without invoking the transaction manager. Freeze that receipt outside
the workspace mutation, run Install `-WhatIf` with one caller-frozen stage
nonce, compare its `release_artifacts` to the build receipt typed-exactly, then
publish the authorization with `-Stage`. Use `-RecoverStage` only after an
uncertain Stage publication and only with the same nonce, stage SHA, and
transaction id. Install `-Apply` consumes that exact authorization and publishes
the fixed `runtime-bootstrap/result.json` first as `candidate_not_ready`, then as
the 38-key `installed_ready` certificate after its shared lock is released.

Treat environment prepare, runtime Stage, and runtime Apply as separate action
checkpoints. Immediately after Apply, freeze the fixed result file's raw SHA-256
outside the document; never recompute an expected value after suspected drift.
The runtime install leaves the action and validation-control policies disabled.
Before starting Bridge or ONI, the separate config gate must consume both the
result path and that external raw hash. Its live mutation atomically points the
one `SaveFilenameKey/` at the named loader clone and enables only
`ass.OniAiProbe`; every other existing mod entry is preserved but disabled. The
current pointer need not equal the clone source (it follows the player's last
load); it must be an absolute local path other than the loader, and the
byte-exact backup restores it. The
environment finalizer owns restoration.

This workflow requires `$OniUserRoot` to resolve to
`$env:USERPROFILE\Documents\Klei\OxygenNotIncluded`, the live Bridge basename
to be exactly `ai-bridge`, and both Bridge/archive roots to stay under that
canonical user root.

```powershell
$GameManagedDir = Join-Path $GameInstallRoot `
  "OxygenNotIncluded_Data\Managed"
$ReleaseDll = Join-Path (Resolve-Path -LiteralPath .\mod).Path `
  'OniAiProbeMod.dll'

# Workspace-only build. Its one stdout object is frozen before any live phase.
$BuildReceipt = (& .\scripts\prepare-v27-runtime.ps1 `
  -BuildRelease -BuildConfiguration Release `
  -ReleaseDll $ReleaseDll -ONIManagedDir $GameManagedDir | ConvertFrom-Json)
if ($LASTEXITCODE -ne 0 -or
    $BuildReceipt.status -cne 'release_built_for_external_freeze' -or
    $BuildReceipt.manager_invoked -ne $false -or
    $BuildReceipt.runtime_write_count -ne 0 -or
    $BuildReceipt.game_launched -ne $false) {
  throw 'workspace-only Release build receipt failed'
}
$ExpectedReleaseDllHash = [string]$BuildReceipt.expected_release_dll_sha256

# The nonce, WhatIf stage SHA, transaction id, and four-artifact receipt are
# caller-owned external authorization. Do not regenerate them on retry.
$RuntimeStageNonce = [guid]::NewGuid().ToString('N')
$RuntimeArguments = @{
  Operation = 'Install'
  RunId = $ValidationRunId
  OniRoot = $OniUserRoot
  EvidenceRoot = $EnvironmentEvidenceRoot
  EnvironmentManifest = $EnvironmentManifestJson
  ExpectedEnvironmentManifestSha256 = $ExpectedEnvironmentManifestHash
  ReleaseDll = $ReleaseDll
  ExpectedReleaseDllSha256 = $ExpectedReleaseDllHash
  ExpectedModVersion = '0.7.0'
  ConfirmLiveRunId = $ValidationRunId
  StageNonce = $RuntimeStageNonce
}
$RuntimeWhatIf = (& .\scripts\prepare-v27-runtime.ps1 `
  @RuntimeArguments -WhatIf | ConvertFrom-Json)
if ($LASTEXITCODE -ne 0 -or $RuntimeWhatIf.status -cne 'would_install' -or
    $RuntimeWhatIf.write_count -ne 0 -or $RuntimeWhatIf.game_launched -ne $false) {
  throw 'runtime bootstrap WhatIf failed'
}

$ArtifactNames = @('OniAiProbeMod.dll', 'OniAiProbeMod.pdb', 'mod.yaml', 'mod_info.yaml')
$FrozenBuildArtifacts = [ordered]@{}
$FrozenWhatIfArtifacts = [ordered]@{}
if (@($BuildReceipt.artifacts.PSObject.Properties).Count -ne $ArtifactNames.Count -or
    @($RuntimeWhatIf.release_artifacts.PSObject.Properties).Count -ne $ArtifactNames.Count) {
  throw 'runtime bootstrap artifact receipt shape is not exact'
}
foreach ($ArtifactName in $ArtifactNames) {
  $BuildArtifact = $BuildReceipt.artifacts.PSObject.Properties[$ArtifactName].Value
  $WhatIfArtifact = $RuntimeWhatIf.release_artifacts.PSObject.Properties[$ArtifactName].Value
  if ($null -eq $BuildArtifact -or $null -eq $WhatIfArtifact -or
      (@($BuildArtifact.PSObject.Properties.Name) | Sort-Object) -join ',' -cne 'length,sha256' -or
      (@($WhatIfArtifact.PSObject.Properties.Name) | Sort-Object) -join ',' -cne 'length,sha256') {
    throw "runtime bootstrap artifact record is not exact: $ArtifactName"
  }
  $FrozenBuildArtifacts[$ArtifactName] = [ordered]@{
    length = [int64]$BuildArtifact.length
    sha256 = [string]$BuildArtifact.sha256
  }
  $FrozenWhatIfArtifacts[$ArtifactName] = [ordered]@{
    length = [int64]$WhatIfArtifact.length
    sha256 = [string]$WhatIfArtifact.sha256
  }
}
if (($FrozenBuildArtifacts | ConvertTo-Json -Compress -Depth 6) -cne
    ($FrozenWhatIfArtifacts | ConvertTo-Json -Compress -Depth 6)) {
  throw 'BuildRelease and runtime WhatIf artifact receipts differ'
}

$ExpectedRuntimeStageHash = [string]$RuntimeWhatIf.expected_stage_sha256
$ExpectedRuntimeTransactionId = [string]$RuntimeWhatIf.transaction_id
$RuntimeStage = (& .\scripts\prepare-v27-runtime.ps1 @RuntimeArguments `
  -Stage -ExpectedStageSha256 $ExpectedRuntimeStageHash `
  -ExpectedTransactionId $ExpectedRuntimeTransactionId | ConvertFrom-Json)
if ($LASTEXITCODE -ne 0 -or $RuntimeStage.status -cne 'staged' -or
    $RuntimeStage.runtime_write_count -ne 0) {
  throw 'runtime bootstrap Stage failed'
}
# If Stage publication returned an uncertain outcome, use -RecoverStage with
# these same three external values; never issue a new nonce or ordinary Stage.

$RuntimeApply = (& .\scripts\prepare-v27-runtime.ps1 @RuntimeArguments `
  -Apply -ExpectedStageSha256 $ExpectedRuntimeStageHash `
  -ExpectedTransactionId $ExpectedRuntimeTransactionId | ConvertFrom-Json)
if ($LASTEXITCODE -ne 0 -or $RuntimeApply.status -cne 'installed_ready' -or
    $RuntimeApply.game_launched -ne $false) {
  throw 'runtime bootstrap Apply failed'
}
$RuntimeBootstrapResultJson = Join-Path $EnvironmentEvidenceRoot `
  "$ValidationRunId\runtime-bootstrap\result.json"
$ExpectedRuntimeBootstrapResultHash = "sha256:" + `
  (Get-FileHash -LiteralPath $RuntimeBootstrapResultJson -Algorithm SHA256).Hash.ToLowerInvariant()

$RuntimeConfigEvidenceJson = Join-Path $EnvironmentEvidenceRoot `
  "$ValidationRunId\environment\runtime-config-evidence.json"
$StartupBindingJson = Join-Path $OniUserRoot "ai-bridge\runtime-startup-binding.json"

python .\scripts\configure_live_validation_runtime.py `
  --run-id $ValidationRunId `
  --oni-root $OniUserRoot `
  --evidence-root $EnvironmentEvidenceRoot `
  --boundary-report-root $BoundaryReportRoot `
  --environment-manifest $EnvironmentManifestJson `
  --expected-environment-manifest-sha256 $ExpectedEnvironmentManifestHash `
  --runtime-bootstrap-result $RuntimeBootstrapResultJson `
  --expected-runtime-bootstrap-result-sha256 $ExpectedRuntimeBootstrapResultHash `
  --pre-boundary $PreBoundaryJson `
  --clone-manifest $CloneManifestJson `
  --source-save $ImmutableSourceSave `
  --loader-save $NamedLoaderSave `
  --loader-stem $NamedLoaderStem `
  --game-managed-dir $GameManagedDir `
  --release-dll $ReleaseDll `
  --expected-release-dll-sha256 $ExpectedReleaseDllHash `
  --what-if

# Only after reviewing the zero-write plan, capture the exact Apply result:
$ConfigApply = (& py -3 -B .\scripts\configure_live_validation_runtime.py `
  --run-id $ValidationRunId `
  --oni-root $OniUserRoot `
  --evidence-root $EnvironmentEvidenceRoot `
  --boundary-report-root $BoundaryReportRoot `
  --environment-manifest $EnvironmentManifestJson `
  --expected-environment-manifest-sha256 $ExpectedEnvironmentManifestHash `
  --runtime-bootstrap-result $RuntimeBootstrapResultJson `
  --expected-runtime-bootstrap-result-sha256 $ExpectedRuntimeBootstrapResultHash `
  --pre-boundary $PreBoundaryJson `
  --clone-manifest $CloneManifestJson `
  --source-save $ImmutableSourceSave `
  --loader-save $NamedLoaderSave `
  --loader-stem $NamedLoaderStem `
  --game-managed-dir $GameManagedDir `
  --release-dll $ReleaseDll `
  --expected-release-dll-sha256 $ExpectedReleaseDllHash `
  --apply --confirm-live-run-id $ValidationRunId | ConvertFrom-Json)
if ($LASTEXITCODE -ne 0 -or $ConfigApply.status -cne 'configured') {
  throw 'runtime config Apply failed'
}

$ExpectedRuntimeConfigEvidenceHash = [string]$ConfigApply.runtime_config_evidence_sha256
$ExpectedStartupBindingHash = [string]$ConfigApply.startup_binding_sha256
$ExpectedStartupIntentHash = [string]$ConfigApply.startup_intent_hash
$ExpectedLoaderPathIdUpper = [string]$ConfigApply.requested_loader_path_id_upper
$ExpectedLoaderSaveHash = [string]$ConfigApply.requested_loader_save_sha256
$ExpectedGameBuild = [string]$ConfigApply.expected_game_build
$ExpectedModVersion = [string]$ConfigApply.expected_mod_version
```

Immediately freeze the apply result's `runtime_config_evidence_sha256` and
`startup_binding_sha256` outside both documents as
`$ExpectedRuntimeConfigEvidenceHash` and `$ExpectedStartupBindingHash`, then
independently verify the pair before launch:

```powershell
$ConfigVerify = (& py -3 -B .\scripts\verify_live_validation_runtime_config.py verify `
  --run-id $ValidationRunId `
  --oni-root $OniUserRoot `
  --evidence-root $EnvironmentEvidenceRoot `
  --boundary-report-root $BoundaryReportRoot `
  --environment-manifest $EnvironmentManifestJson `
  --expected-environment-manifest-sha256 $ExpectedEnvironmentManifestHash `
  --runtime-bootstrap-result $RuntimeBootstrapResultJson `
  --expected-runtime-bootstrap-result-sha256 $ExpectedRuntimeBootstrapResultHash `
  --pre-boundary $PreBoundaryJson `
  --clone-manifest $CloneManifestJson `
  --source-save $ImmutableSourceSave `
  --loader-save $NamedLoaderSave `
  --loader-stem $NamedLoaderStem `
  --game-managed-dir $GameManagedDir `
  --release-dll $ReleaseDll `
  --expected-release-dll-sha256 $ExpectedReleaseDllHash `
  --expected-runtime-config-evidence-sha256 $ExpectedRuntimeConfigEvidenceHash `
  --expected-startup-binding-sha256 $ExpectedStartupBindingHash | ConvertFrom-Json)
if ($LASTEXITCODE -ne 0 -or $ConfigVerify.status -cne 'passed' -or
    $ConfigVerify.runtime_config_evidence_sha256 -cne $ExpectedRuntimeConfigEvidenceHash -or
    $ConfigVerify.startup_binding_sha256 -cne $ExpectedStartupBindingHash -or
    $ConfigVerify.startup_intent_hash -cne $ExpectedStartupIntentHash -or
    $ConfigVerify.requested_loader_path_id_upper -cne $ExpectedLoaderPathIdUpper -or
    $ConfigVerify.requested_loader_save_sha256 -cne $ExpectedLoaderSaveHash -or
    $ConfigVerify.expected_game_build -cne $ExpectedGameBuild -or
    $ConfigVerify.expected_mod_version -cne $ExpectedModVersion) {
  throw 'runtime config independent verify failed'
}
```

Only a passed independent verify permits launch. Runtime-config schema v5 and
startup intent/binding v2 also bind the externally frozen 38-key
`installed_ready` result path, raw SHA-256, semantic hash, transaction id, and
installed Probe tree content hash. Their exact startup fields are
`runtime_bootstrap_result_path_id_lower`,
`runtime_bootstrap_result_sha256`, `runtime_bootstrap_result_hash`,
`runtime_bootstrap_transaction_id`, and
`installed_probe_tree_content_hash`. Start ONI via Steam first,
capture and independently verify its exact live process identity, then start
Bridge with the complete nine-anchor startup set. Bridge must not start before
the validation PID has been frozen. Do not recompute any expected hash after a
suspected edit. Runtime-config schema v5
also binds the exact Managed-directory ancestry and the single-link identities
and SHA-256 values of `Assembly-CSharp.dll` and
`Assembly-CSharp-firstpass.dll`. Its full `loader_policy` proves that the
configured `SaveFilenameKey/` points only at the named loader, that the source
continue path is disabled, and that built-in `AutoResumeSaveFile` behavior is
the expected one-shot behavior in those exact assemblies.

The exact schemas are `oni.live-runtime-bootstrap-result.v1` (38 keys),
`oni.live-validation-runtime-config.v5`,
`oni.live-validation-runtime-config-plan.v5`,
`oni.live-validation-runtime-config-result.v5`, and
`oni.live-validation-runtime-config-verification-result.v5`. The embedded
`oni.live-validation-startup-intent.v2` is exactly 18 keys and the sibling
`oni.runtime-startup-binding.v2` is exactly 19 keys, including each document's
self-hash.

The independent pre-launch verifier still requires the restore journal to be
exactly its one prepared-origin record. Later, only the in-process environment
finalizer may verify a canonical append-only journal while it holds the
descriptor-backed parent finalize lock. That custody proof has no CLI or JSON
surface; a same-named lock file alone never authorizes the relaxed phase.

Freeze the live paths before launch. `Player.log` is in LocalLow, not beneath
`$OniUserRoot`:

```powershell
$RuntimeBridgeDir = Join-Path $OniUserRoot 'ai-bridge'
$StartupBindingJson = Join-Path $RuntimeBridgeDir 'runtime-startup-binding.json'
$ConsumedPrefsYaml = Join-Path $OniUserRoot 'kplayerprefs.yaml'
$PlayerLog = Join-Path $env:USERPROFILE `
  'AppData\LocalLow\Klei\Oxygen Not Included\Player.log'
$LineageManifestJson = Join-Path $RuntimeBridgeDir `
  "evidence\save-lineage\$ValidationRunId\manifest.json"
$StartupConsumptionBundle = Join-Path (Split-Path -Parent $LineageManifestJson) `
  'startup-consumption-evidence'
$ValidationProcessReport = Join-Path $EnvironmentEvidenceRoot `
  "$ValidationRunId\validation-process-identity.json"
$GameExe = Join-Path $GameInstallRoot 'OxygenNotIncluded.exe'

$ExpectedGameExePathIdUpper = (& py -3 -B -c `
  "from scripts.create_startup_consumption_evidence import process_image_path_id_upper; import sys; print(process_image_path_id_upper(sys.argv[1]))" `
  $GameExe).Trim()
if ($LASTEXITCODE -ne 0) { throw 'game exe path-id anchor failed' }
$ExpectedGameExeSha256 = 'sha256:' + `
  (Get-FileHash -LiteralPath $GameExe -Algorithm SHA256).Hash.ToLowerInvariant()
```

Launch ONI through Steam only after those executable anchors are frozen. Require
exactly one `OxygenNotIncluded.exe`, and do not start Bridge until this
create-only process report and its independent live verification both pass:

```powershell
$ProcessCreate = (& py -3 -B .\scripts\capture_validation_process_identity.py create `
  --expected-image-name OxygenNotIncluded.exe `
  --expected-image-path-id-upper $ExpectedGameExePathIdUpper `
  --expected-image-file-sha256 $ExpectedGameExeSha256 `
  --output $ValidationProcessReport | ConvertFrom-Json)
if ($LASTEXITCODE -ne 0 -or $ProcessCreate.status -cne 'published') {
  throw 'process identity create failed'
}
$ExpectedValidationProcessId = [int]$ProcessCreate.process_id
$ExpectedValidationProcessReportHash = [string]$ProcessCreate.report_file_sha256

$ProcessVerify = (& py -3 -B .\scripts\capture_validation_process_identity.py verify `
  --report $ValidationProcessReport `
  --report-file-sha256 $ExpectedValidationProcessReportHash | ConvertFrom-Json)
if ($LASTEXITCODE -ne 0 -or $ProcessVerify.status -cne 'passed' -or
    [int]$ProcessVerify.process_id -ne $ExpectedValidationProcessId -or
    $ProcessVerify.report_file_sha256 -cne $ExpectedValidationProcessReportHash -or
    $ProcessVerify.document_hash -cne $ProcessCreate.document_hash) {
  throw 'process identity verify failed'
}
```

Start Bridge in a foreground controllable PTY, retain its session id, and pass
the complete nine-anchor set. The seven non-PID values come verbatim from the
configure Apply result and must agree with the independent config verifier:

```powershell
.\scripts\run-bridge.ps1 `
  -BridgeDir $RuntimeBridgeDir `
  -ExpectedValidationRunId $ValidationRunId `
  -ExpectedValidationProcessId $ExpectedValidationProcessId `
  -ExpectedStartupBindingSha256 $ExpectedStartupBindingHash `
  -ExpectedStartupIntentHash $ExpectedStartupIntentHash `
  -ExpectedRuntimeConfigEvidenceSha256 $ExpectedRuntimeConfigEvidenceHash `
  -ExpectedLoaderPathIdUpper $ExpectedLoaderPathIdUpper `
  -ExpectedLoaderSaveSha256 $ExpectedLoaderSaveHash `
  -ExpectedGameBuild $ExpectedGameBuild `
  -ExpectedModVersion $ExpectedModVersion
```

Never use a partial anchor set, `-Once`, `-EnableLlm`, or `Start-Process`. After
the origin trusted snapshot, reserve the absent derivative, perform the unique
Save As/reload, and activate its lineage manifest. With the exact ONI PID and
Bridge still live, create and replay-verify the
`oni.startup-consumption-evidence.v3` bundle from another shell; the output
directory must not already exist:

```powershell
$StartupCreate = (& py -3 -B .\scripts\create_startup_consumption_evidence.py create `
  --runtime-config-evidence $RuntimeConfigEvidenceJson `
  --expected-runtime-config-evidence-sha256 $ExpectedRuntimeConfigEvidenceHash `
  --startup-binding $StartupBindingJson `
  --expected-startup-binding-sha256 $ExpectedStartupBindingHash `
  --consumed-prefs $ConsumedPrefsYaml `
  --player-log $PlayerLog `
  --bridge-dir $RuntimeBridgeDir `
  --expected-process-id $ExpectedValidationProcessId `
  --expected-process-image-name OxygenNotIncluded.exe `
  --expected-process-image-path-id-upper $ExpectedGameExePathIdUpper `
  --expected-process-image-sha256 $ExpectedGameExeSha256 `
  --output-dir $StartupConsumptionBundle | ConvertFrom-Json)
if ($LASTEXITCODE -ne 0 -or $StartupCreate.status -cne 'passed') {
  throw 'startup v3 create failed'
}
$ExpectedStartupConsumptionEvidenceHash = [string]$StartupCreate.evidence_hash

$StartupVerify = (& py -3 -B .\scripts\create_startup_consumption_evidence.py verify `
  --bundle-dir $StartupConsumptionBundle | ConvertFrom-Json)
if ($LASTEXITCODE -ne 0 -or $StartupVerify.status -cne 'passed' -or
    $StartupVerify.verified_evidence_hash -cne $ExpectedStartupConsumptionEvidenceHash) {
  throw 'startup v3 replay verify failed'
}
```

At normal shutdown, wait for the complete terminal output generation and send
Ctrl+C only to the retained PTY session. Do not force-kill Bridge between its
root and companion output writes.

That one-shot avoids only a second user-selected load-screen activation. The
first Main Menu initialization can still perform `InitLoadScreen`, migration,
save-directory probing, mod-report generation, initial load-screen activation,
and colony-list refresh work before `AutoResumeSaveFile` is consumed. Treat a
failed first startup or wrong-loader selection as a potentially side-effecting
startup, even when no trusted save session was established.

If that first startup or loader selection fails before **any** session,
snapshot, command, priority-pair, bundle, freeze, outcome, save-write, control,
or ACK evidence exists, stop ONI and Bridge and use the narrow manifest-bound
abort path. Run it as a zero-write plan first:

```powershell
# Reuse the external value frozen from `$PrepareApply`; never rehash a possibly
# drifted manifest to create a new expected value here.
if ([string]::IsNullOrWhiteSpace($ExpectedEnvironmentManifestHash)) {
  throw 'frozen environment manifest hash is missing'
}

python .\scripts\manage_live_validation_environment.py abort `
  --run-id $ValidationRunId `
  --oni-root $OniUserRoot `
  --evidence-root $EnvironmentEvidenceRoot `
  --boundary-report-root $BoundaryReportRoot `
  --pre-boundary $PreBoundaryJson `
  --clone-manifest $CloneManifestJson `
  --source-save $ImmutableSourceSave `
  --loader-save $NamedLoaderSave `
  --loader-stem $NamedLoaderStem `
  --manifest $EnvironmentManifestJson `
  --expected-environment-manifest-sha256 $ExpectedEnvironmentManifestHash `
  --runtime-bootstrap-result $RuntimeBootstrapResultJson `
  --expected-runtime-bootstrap-result-sha256 $ExpectedRuntimeBootstrapResultHash `
  --runtime-config-evidence $RuntimeConfigEvidenceJson `
  --expected-runtime-config-evidence-sha256 $ExpectedRuntimeConfigEvidenceHash `
  --expected-startup-binding-sha256 $ExpectedStartupBindingHash `
  --release-dll $ReleaseDll `
  --expected-release-dll-sha256 $ExpectedReleaseDllHash `
  --game-managed-dir $GameManagedDir `
  --what-if

# Only after reviewing the plan and confirming ONI/Bridge remain stopped:
# replace --what-if with --apply --confirm-live-run-id $ValidationRunId
```

Abort requires the initial action policy to remain byte-for-byte equal to the
frozen disabled policy, validation control to remain disabled and unbound, and
every command/control/evidence stage named above to remain empty. Process,
waiting, and loader-failure diagnostics are permitted and archived. Current
prefs/mods/Probe/Bridge/loader state is classified and preserved in abort
evidence before the original loader, configs, Probe, Bridge, and archive tree
are restored exactly. The same `.finalize.lock`, ownership-safe rollback, and
create-only non-replay recovery rules apply. Once any session or action
evidence exists, abort is permanently unavailable for that run: use the full
shutdown/finalize path if its evidence is complete, otherwise stop for manual
recovery. Abort must never be used to bypass the shutdown or priority-pair
gates.

### M1 (model-loop) runs: `finalize_m1_environment.py`

An M1 run (the external model chooses `set_paused`/`noop`-class commands
through `--model-loop`, no priority pair, no derivative save because the Mod's
autosave guard is enforced) produces session and command evidence, so abort is
closed, but it never produces the priority-pair report, shutdown certificate,
or startup-consumption bundle the G0 `finalize` requires. Its restoration is
`scripts/finalize_m1_environment.py`. It shares `resolve_roots`, the
environment-manifest and restore-journal anchors, `verify_base_bindings`,
`verify_backups`, `validate_bootstrap`, `FileTransaction`, `.finalize.lock`,
and the post-boundary comparison with the G0 workflow; only the evidence gate
differs. It requires ONI and Bridge stopped, the action policy disabled, every
nonterminal queue and `quarantine`/`lifecycle-recovery` empty, and at least
one `commands/done` record (with `--expect-done-command-id` the done set must
match exactly). The runtime Bridge is preserved as `environment/rb` plus
`rb-snapshot.json`; the runtime Probe, configs, and pre-restore loader pair are
preserved under `environment/finalization-m1/`; the loader pair, configs, Probe,
pre-existing Bridge, and archive-root timestamp are restored exactly from the
manifest. The self-hashed `finalize-m1-result.json` is create-only; a replay
re-verifies the restored state and returns it unchanged, and any other leftover
from an earlier attempt (a rollback, a recovery latch, a moved Bridge without a
result) is a hard refusal that needs manual inspection.

```powershell
python .\scripts\finalize_m1_environment.py `
  --run-id $ValidationRunId `
  --oni-root $OniUserRoot `
  --evidence-root $EnvironmentEvidenceRoot `
  --boundary-report-root $BoundaryReportRoot `
  --pre-boundary $PreBoundaryJson `
  --clone-manifest $CloneManifestJson `
  --source-save $ImmutableSourceSave `
  --loader-save $NamedLoaderSave `
  --loader-stem $NamedLoaderStem `
  --manifest $EnvironmentManifestJson `
  --expected-environment-manifest-sha256 $ExpectedEnvironmentManifestHash `
  --what-if

# After reviewing the plan (steps, session_window.done_commands) and confirming
# ONI/Bridge remain stopped: replace --what-if with
#   --apply --confirm-live-run-id $ValidationRunId [--expect-done-command-id <id> ...]
```

Its offline TestRoot verification (zero-write what-if, exact restore with a
passing post-boundary comparison, identical replay, tampered-result refusal,
eight zero-write refusals, injected mid-transaction fault with exact rollback
and a non-replay latch) is `python .\scripts\verify_finalize_m1_environment.py`;
run it in a worktree like the other verifiers.

After both priority commands are confirmed and the inverse save/reload is
complete, keep policy disabled. Quit ONI through its normal application exit,
wait until Bridge has copied the final lineage session's
`session_ended(reason=application_quit)` and immediately following
`probe_shutdown` records into the session archive and published the complete
terminal decision generation, then stop Bridge cleanly. Create the offline
inverse checkpoint and run `finalize_priority_pair_lineage.py finalize` inside
the pair-creation freshness window. Capture its declared report hash as the new
external anchor and cross-check its two ordered automatic-bundle hashes against
the values frozen earlier from the two command freezes; do not derive a
replacement expected value after a suspected edit. Then create the
deterministic shutdown proof before starting the environment restore:

Before the forward `prepare`, freeze the operator-selected chore group as
`$ExpectedChoreGroup`, and pass it explicitly as
`--chore-group $ExpectedChoreGroup` to both the forward and inverse
`verify_runtime.py prepare` commands; never rely on the CLI's `Dig` default.
Capture `$ForwardCommandId` and `$InverseCommandId` directly from the
respective `prepare` stdout documents, and
capture `$ExpectedForwardBundleHash` and `$ExpectedInverseBundleHash` directly
from the respective `freeze-command` stdout
`approval_bundle_hash` fields. Keep those four values outside the command and
freeze files. The pair report below must agree with these earlier independent
anchors; it must not replace them.

```powershell
$RequiredEarlierAnchors = @(
  $ExpectedChoreGroup,
  $ForwardCommandId,
  $InverseCommandId,
  $ExpectedForwardBundleHash,
  $ExpectedInverseBundleHash
)
if ($RequiredEarlierAnchors.Where({ [string]::IsNullOrWhiteSpace([string]$_) }).Count -ne 0) {
  throw 'earlier priority command anchors are incomplete'
}

$LineageEvidenceDir = Split-Path -Parent $LineageManifestJson
$PriorityPairReportJson = Join-Path $LineageEvidenceDir `
  'priority-pair-finalization.json'
$ShutdownEvidenceJson = Join-Path $LineageEvidenceDir 'shutdown-evidence.json'

$PairFinalize = (& py -3 -B .\scripts\finalize_priority_pair_lineage.py finalize `
  --manifest $LineageManifestJson `
  --bridge-dir $RuntimeBridgeDir `
  --forward-step priority_forward `
  --inverse-step priority_inverse `
  --report $PriorityPairReportJson | ConvertFrom-Json)
if ($LASTEXITCODE -ne 0 -or $PairFinalize.status -cne 'passed') {
  throw 'priority pair finalization failed'
}

$PairCommands = @($PairFinalize.pair.commands)
if ($PairCommands.Count -ne 2 -or
    $PairCommands[0].step_id -cne 'priority_forward' -or
    $PairCommands[1].step_id -cne 'priority_inverse' -or
    $PairFinalize.pair.target.chore_group -cne $ExpectedChoreGroup -or
    $PairCommands[0].id -cne $ForwardCommandId -or
    $PairCommands[1].id -cne $InverseCommandId -or
    $PairCommands[0].automatic_approval_bundle.bundle_hash -cne `
      $ExpectedForwardBundleHash -or
    $PairCommands[1].automatic_approval_bundle.bundle_hash -cne `
      $ExpectedInverseBundleHash) {
  throw 'priority pair command order is invalid'
}

$ExpectedPairReportHash = [string]$PairFinalize.report_hash
$FinalSessionId = [string]$PairFinalize.final_tip.session_id
if ([string]::IsNullOrWhiteSpace($ExpectedPairReportHash) -or
    [string]::IsNullOrWhiteSpace($FinalSessionId)) {
  throw 'priority pair external anchors are incomplete'
}

$FinalSessionEventLog = Join-Path $RuntimeBridgeDir `
  "sessions\$FinalSessionId\state.jsonl"
if (-not (Test-Path -LiteralPath $FinalSessionEventLog -PathType Leaf)) {
  throw 'final session archive is missing'
}

$ShutdownCreate = (& py -3 -B .\scripts\create_shutdown_evidence.py create `
  --manifest $LineageManifestJson `
  --bridge-dir $RuntimeBridgeDir `
  --priority-pair-report $PriorityPairReportJson `
  --priority-pair-report-hash $ExpectedPairReportHash `
  --run-id $ValidationRunId `
  --forward-step priority_forward `
  --inverse-step priority_inverse `
  --command-id $ForwardCommandId `
  --command-id $InverseCommandId `
  --command-bundle-hash $ExpectedForwardBundleHash `
  --command-bundle-hash $ExpectedInverseBundleHash `
  --startup-consumption-bundle $StartupConsumptionBundle `
  --startup-consumption-evidence-hash $ExpectedStartupConsumptionEvidenceHash `
  --validation-process-id $ExpectedValidationProcessId `
  --event-log $FinalSessionEventLog `
  --output $ShutdownEvidenceJson | ConvertFrom-Json)
if ($LASTEXITCODE -ne 0 -or $ShutdownCreate.status -cne 'published') {
  throw 'shutdown evidence create failed'
}
$ExpectedShutdownEvidenceHash = [string]$ShutdownCreate.evidence_hash

$ShutdownVerify = (& py -3 -B .\scripts\create_shutdown_evidence.py verify `
  --manifest $LineageManifestJson `
  --bridge-dir $RuntimeBridgeDir `
  --priority-pair-report $PriorityPairReportJson `
  --priority-pair-report-hash $ExpectedPairReportHash `
  --run-id $ValidationRunId `
  --forward-step priority_forward `
  --inverse-step priority_inverse `
  --command-id $ForwardCommandId `
  --command-id $InverseCommandId `
  --command-bundle-hash $ExpectedForwardBundleHash `
  --command-bundle-hash $ExpectedInverseBundleHash `
  --startup-consumption-bundle $StartupConsumptionBundle `
  --startup-consumption-evidence-hash $ExpectedStartupConsumptionEvidenceHash `
  --validation-process-id $ExpectedValidationProcessId `
  --shutdown-evidence $ShutdownEvidenceJson `
  --shutdown-evidence-hash $ExpectedShutdownEvidenceHash | ConvertFrom-Json)
if ($LASTEXITCODE -ne 0 -or $ShutdownVerify.status -cne 'passed' -or
    $ShutdownVerify.evidence_hash -cne $ExpectedShutdownEvidenceHash -or
    $ShutdownVerify.file_sha256 -cne $ShutdownCreate.file_sha256) {
  throw 'shutdown evidence independent verify failed'
}
```

`$ExpectedStartupConsumptionEvidenceHash` and `$ExpectedValidationProcessId`
must be the values recorded outside the FINAL-STABLE startup capture; do not
derive them again from a suspect bundle. The bundle path is exact and
run-scoped: `<manifest-dir>/startup-consumption-evidence`. Its strict inner
directory contains only the five artifacts defined by the v3 startup-consumption
contract and `evidence.json`; the shutdown
creator performs the real create-only offline replay and requires PASS before
it reads the shutdown tail.

Record the returned typed canonical `evidence_hash` as
`$ExpectedShutdownEvidenceHash`. The creator validates the complete selected
session archive (or terminal target segment of the exact root log), the unique
gap-free producer sequence from
`oni.event.v2 session_started/seq=1/reason=save_loader_load_complete`, the exact
`oni.state.v2` final-tip snapshot, and the adjacent terminal pair. Every record
in the target session must carry the exact same session, save, world, game
build, mod version, and clean-save identity. Every record must also equal the
replayed startup certificate on validation run, startup binding/intent/runtime
hashes, the external validation PID, verified binding status, game build, and
mod version. Every final-session record keeps the certified requested loader
path while its active path equals the final lineage path. The final session's
first record must be seq 1 with the complete-loader reason and final lineage-tip
save hash. Separately, the lineage origin archive must begin with the exact
certified raw-byte and canonical-record startup prefix.
Event/state schemas are checked per record, and the mod version must equal the
active manifest and latest state.
Crash/kill/probe-error markers, missing or duplicated records, wrong
session/save/world/build/mod, dirty save state, malformed or partial lines, and
any record after `probe_shutdown` are refused before the create-only output is
written. Inputs require non-reparse, single-link, descriptor-bound double reads,
so hardlinks, verify-to-open name swaps, and same-size/same-mtime in-place edits
are refused. The output is create-only and is accepted only after descriptor,
pathname, link-count, byte-hash, and canonical-self-hash verification; cleanup
never removes a late replacement. The shutdown v3 document projects the startup
semantic hash, raw `evidence.json` SHA-256, replay status, path ID, run/session,
process, binding, loader, and archive-prefix anchors into its own self-hash;
raw prefs, Player log contents, and loader paths are not copied into JSON or
stdout. The startup bundle and shutdown log are both rechecked across the
validation window, including same-content name-swap custody changes. Verify also
rereads the shutdown evidence file after reconstruction, while publication pins
the ancestor reparse chain and parent-directory identity across the before-open
window.

```powershell
python .\scripts\manage_live_validation_environment.py finalize `
  --run-id $ValidationRunId `
  --oni-root $OniUserRoot `
  --evidence-root $EnvironmentEvidenceRoot `
  --boundary-report-root $BoundaryReportRoot `
  --pre-boundary $PreBoundaryJson `
  --clone-manifest $CloneManifestJson `
  --source-save $ImmutableSourceSave `
  --loader-save $NamedLoaderSave `
  --loader-stem $NamedLoaderStem `
  --manifest $EnvironmentManifestJson `
  --lineage-manifest $LineageManifestJson `
  --priority-pair-report $PriorityPairReportJson `
  --priority-pair-report-hash $ExpectedPairReportHash `
  --shutdown-evidence $ShutdownEvidenceJson `
  --shutdown-evidence-hash $ExpectedShutdownEvidenceHash `
  --startup-consumption-bundle $StartupConsumptionBundle `
  --startup-consumption-evidence-hash $ExpectedStartupConsumptionEvidenceHash `
  --validation-process-id $ExpectedValidationProcessId `
  --runtime-config-evidence $RuntimeConfigEvidenceJson `
  --expected-runtime-config-evidence-sha256 $ExpectedRuntimeConfigEvidenceHash `
  --expected-startup-binding-sha256 $ExpectedStartupBindingHash `
  --runtime-bootstrap-result $RuntimeBootstrapResultJson `
  --expected-runtime-bootstrap-result-sha256 $ExpectedRuntimeBootstrapResultHash `
  --release-dll $ReleaseDll `
  --expected-release-dll-sha256 $ExpectedReleaseDllHash `
  --game-managed-dir $GameManagedDir `
  --forward-step-id priority_forward `
  --inverse-step-id priority_inverse `
  --command-id $ForwardCommandId `
  --command-id $InverseCommandId `
  --command-bundle-hash $ExpectedForwardBundleHash `
  --command-bundle-hash $ExpectedInverseBundleHash `
  --what-if

# Only after reviewing the plan:
# replace --what-if with --apply --confirm-live-run-id $ValidationRunId
```

Finalize requires exactly the two ordered steps and bundles. It validates their
embedded source frames, policies, receipts, per-step freezes, and the exact
manifest-bound pair report. Production command/bundle/frame/certificate fields
are strict Bridge-relative paths, while the lineage-freeze path is the exact
absolute manifest evidence path. If a rolling frame or current certificate is
reported present, its file must match; if reported evicted, the path must be
absent and the bundle/freeze copy is authoritative. The same production frozen
confirmation assessor reconstructs command status, outcome, attestation,
witness, ACK sequences, and records.
It also requires the two shutdown-evidence CLI anchors, the external startup
bundle/hash/PID anchors, the runtime-bootstrap result path/raw-SHA anchor, both
external runtime-config/startup-binding anchors, and the exact Release/Managed
arguments.
The config evidence path must be exactly
`<evidence-root>/<run-id>/environment/runtime-config-evidence.json`; any sibling
`runtime-config-recovery-*.json` blocks both modes. The finalizer invokes the
independent historical verifier, requires `configured_at` to be no later than
the trusted shutdown session's `session_started.ts`, and create-only freezes
the externally SHA-anchored bytes as `finalization/pair/config.json` before the
restore transaction. Its compact runtime binding preserves the config evidence
raw/document hashes and transaction id, bootstrap and initial disabled-policy
raw/document hashes, source/loader identities, 0.7.0 Probe identity, external
Release hash, desired prefs/mods hashes, the complete v5-bound loader policy, and the
exact Managed-directory/assembly binding. Current prefs, mods, policy, and
loader bytes may legitimately have changed during the run; the finalizer still
revalidates the current assemblies before restoration, while runtime drift is
archived separately and is never substituted for the launch-time evidence.

It independently
rehashes the evidence document, source log, pair report, bundles, and freezes,
then reconstructs the canonical document from the active lineage and requires
exact equality. A rehashed edit is therefore rejected even if the supplied
expected evidence hash was changed with it, and process counts of zero alone
are never accepted as proof of normal exit. Reconstruction is repeated after
the runtime Bridge snapshot and immediately before the transaction; the source
log and all present command evidence must be exact members of that snapshot.
It also requires the pair report to have frozen a valid, non-stale
`oni.state.v2` `latest_state.json` no more than 120 seconds old at pair creation,
then recomputes `command_pipeline_readiness` against those exact frozen bytes
and the disabled policy. This is a cleanup-only window after both ONI and Bridge
process counts are zero; it does not authorize another command. Finalize checks
the frozen pair timestamp rather than applying a new wall-clock age deadline
after a normal exit. Mutation
proposal/confirmation readiness must
be true; ambiguous outcomes, duplicate IDs, stale pending commands, invalid
records, and all blockers must be empty. In particular, a relevant mutating
`commands/failed` record classified `outcome_unknown` is a hard stop even when
the five nonterminal directories and all `*.tmp` checks are otherwise empty.
The transaction recoverably moves the derivative pair and pre-restore loader
pair into evidence, restores the loader from the immutable source, restores all
bound config/Probe bytes and timestamps, preserves the completed runtime Bridge
under the compact `environment/rb` evidence path, restores the archived old
Bridge, and verifies every bound artifact before committing. Before the move it
requires the current archive tree, after subtracting the one exact
`archived_previous_bridge` subtree, to equal the recursive prepare snapshot.
After restoration the archive tree and its root timestamp must equal that
snapshot exactly. The final boundary comparison admits only the named loader
stem pair.

The live process guard first limits CIM candidates to `python.exe`,
`pythonw.exe`, and `py.exe`, then parses Windows argv and treats a process as the
Bridge only when its first `.py` script argument has the exact basename
`oni_bridge.py`. The probing PowerShell command, `python -c` text, later data
arguments, and names such as `oni_bridge.py.bak` do not self-match.

A concurrent apply is blocked by `.finalize.lock`. If the process or machine
stops before the lock is released, do not delete it blindly: inspect the
hash-chained `restore-journal.jsonl` and the recoverable evidence paths first.
The lock carries a random token plus its acquired descriptor identity. Release
atomically reclaims and verifies that exact file; a replacement is never
deleted. The same ownership rule applies to JSON/file/tree staging cleanup and
immutable publications. Journal verification and append share stable
single-link descriptor evidence for the prior identity, length, and bytes; a
missing journal uses exclusive creation, so a late foreign empty file is not
modified.

Transaction moves and staged copies publish without overwrite. Each completed
step retains the owner identity and exact file/tree state obtained before the
race boundary. Undo atomically moves that owner into a compact, unique claim,
validates it, then restores without overwriting a newly created source or
target. Compact claims are necessary because production Bridge evidence has
mandatory 64-hex directory components and rollback must remain usable with
legacy Win32 path limits. A late replacement, injected staging child, or
after-check swap is preserved and marks rollback outcome-unknown. Journal
intent/completed/rollback/commit write or fsync faults cannot stop the remaining
best-effort in-memory undos; explicit recovery evidence distinguishes prepared,
physical-committed, journal-confirmed, and outcome-unknown states. Finalize
result is create-only after the physical commit. If its publication or journal
acknowledgement fails, a non-replay candidate is retained; later invocation may
only verify and converge that same result, never repeat the physical restore.
That convergence path rechecks the externally anchored config evidence and
startup binding, the frozen config copy, the compact runtime-config binding,
the Release SHA, immutable
source/loader bindings, and backups; an older recovery/result without those
fields cannot bypass the gate. Finalize power-loss recovery remains a manual
live-only operation. Abort is narrower: its create-only transaction plan binds
every ordered owner/path plus source, Release, Managed, config, and startup
anchors. Before physical commit, a later invocation may reverse only those
exact owners to the pre-abort state (including archive-root mtime) and then
require a new guarded invocation; after physical commit it may only converge
the authenticated result and must never replay the restore.

The full offline TestRoot verification, including rehashed report/bundle/freeze
tampering, production relative-path and eviction fallback contracts, frozen
confirmation reconstruction, stable-descriptor hardlink/name-swap/in-place
drift, path escape, concurrent source mutation, process guards, ownership-safe
exclusive locking, strict process argv classification, the 30-second/130-second
cleanup freshness boundary, recursive pre-existing archive preservation,
unexpected archive changes, unresolved `outcome_unknown`, publication/journal
fault injection, external-replacement retention, runtime-config/Release anchor
tamper, configured/shutdown ordering, current-config drift, full v3 loader
policy and Managed-assembly rebinding, pre-session abort success and partial
config classification, session/action zero-write refusal, wrong anchors,
hardlink/name/parent swaps, every physical abort-step rollback, external
replacement preservation, lock/process races, post-commit non-replay
convergence, cleanup failure, long-path boundaries, frozen-copy races,
exact `oni-user` rollback, and non-replay result convergence is:

```powershell
python .\scripts\verify_live_validation_environment.py
```
