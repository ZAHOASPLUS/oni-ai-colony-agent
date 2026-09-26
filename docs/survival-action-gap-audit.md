# Survival-action gap audit

Original audit time: `2026-08-31T05:48:18+08:00`  
Original mode: static, read-only repository audit  
Current update: `2026-09-03`, offline implementation and verification only  
Live game/Klei/Bridge access in this update: none

## 2026-09-03 implementation update

The original gap inventory below remains the long-horizon boundary, but two
control-plane slices have now been implemented offline. A06 removed the guessed
empty-research mutation and enforces at most one new mutation per Decision
Frame while preserving non-mutating notes. A07 adds the non-executing,
post-sync `oni.control-packet.v1`, exact frame/state/policy evaluation context,
deterministic-rule candidate revalidation, pipeline ambiguity latches, and
survival-objective observation priority. Its lifecycle hardening is explicitly
versioned as `oni.decision-frame.v2`; V1 archives fail closed rather than being
silently reinterpreted.

Only the exact post-sync `frame_bound` gate may produce a packet, and every
candidate is a non-executing summary with no approval or execution authority.
Candidate admission requires exact command/source keysets, deterministic rule
re-evaluation, canonical lifecycle and time/TTL bindings, one and only one
mutating pending record, and no hidden unresolved outcome. Source/trust/domain
observations precede objective requests; objective integers order all
unknown/blocked objectives before generic gates. `ACTIONS_DISABLED` has one
narrow exception for a non-executing recommendation only. V1-bound suggested
work is never rebound, while operator/planned nonterminal work remains a
fail-closed latch until disposition or expiry. A fresh-frame suggested attempt
now receives a frame/state/policy-bound 128-bit id, but bounded long-horizon
retention for repeatedly stale unapproved attempts remains an offline release
gate before live admission.

Fresh targeted evidence currently passes Decision Frame (17 cases), Control
Packet (6 cases), command pipeline (16 cases), and Bridge reliability (15
cases/397 assertions), using only system Temp/stdout. This is not formal
pre-live or live-game acceptance: the production bootstrap report has not yet
been refreshed for the current source tree, G0 has not run, and no current
evidence proves game launch or a real mutation. The next offline vertical slice
is A08 survival-emergency arbitration to one reversible `set_paused=true`
containment intent; fresh G0 priority change/restore remains the mandatory live
admission gate before any new survival capability is exercised in game.

## Verdict

The current system is an honest, safety-oriented observer plus an approval-gated narrow mutation substrate; it is not yet an executor that can keep a colony alive indefinitely. Its practical ceiling is to detect a few active-world risks, pause, leave operator notes, select research, change one duplicant chore priority, mark/cancel digs, and place/cancel only `Ladder` and `Wire` blueprints. It cannot create or operate the bootstrap life-support trio (`Outhouse`, `OxygenDiffuser`, `ManualGenerator`), farms, plumbing, batteries, thermal systems, rockets, or renewable/fault-tolerant loops.

The strategy correctly refuses to guess: it never executes actions and treats missing telemetry as unknown (`agent/strategy.py:1-14`, `agent/strategy.py:2744-2786`). With the actual Probe shape, its own verification expects the colony to remain in `bootstrap` with oxygen and sustainability unproven (`scripts/verify_strategy.py:837-880`). A human can still play the colony, but the observer/executor cannot close the survival loop itself.

Four findings control the implementation order:

1. **P0 multi-world release blocker.** Probe v1 deliberately covers exactly the active world (`mod/Source/Probe/SurvivalTelemetryReader.cs:123-136`); the telemetry contract permits one covered world (`tools/game_verify/contracts/oni_telemetry.v1.json:24-28`, `tools/game_verify/contracts/oni_telemetry.v1.json:414-444`); Strategy explicitly rejects a covered set other than `[active_world]` (`agent/strategy.py:1070-1131`); changing the active world invalidates the rolling phase window (`agent/strategy.py:631-672`); and colony plans bind one `world_id` (`agent/colony_plan.py:478-487`, `agent/colony_plan.py:821-858`). A save with duplicants on two worlds can therefore never become trusted. This is not a missing reader alone; Probe, contract, Strategy, evidence tracking, plans, and action routing must change together.
2. **P0 rollback-contract mismatch before broader blueprint authority.** Documentation says a postcondition timeout cancels all still-unfinished blueprints (`docs/protocol.md:893-899`). The timeout path actually calls `FinishPendingUnknown` (`mod/Source/Commands/ActionExecutor.cs:2496-2503`), which latches mutations and pauses but does not cancel (`mod/Source/Commands/ActionExecutor.cs:2546-2601`). The only routine containing automatic cancellation is `FinishPendingFailure` (`mod/Source/Commands/ActionExecutor.cs:2508-2544`), and static call-site search finds only its definition. Synchronous partial-placement failures do perform cancellation (`mod/Source/Commands/ActionExecutor.cs:1744-1839`). The implementation and published rollback contract must agree before expanding from Ladder to life-support buildings.
3. **The safe first post-priority implementation is read-only telemetry/catalog, not mutation.** Existing building telemetry is only three completed-prefab counts and explicitly says count does not prove function (`mod/Source/Probe/ProbeStateReader.cs:1856-1951`, `agent/rules.py:382-425`). Current placement/cancellation is hard-coded to Ladder and utility placement to Wire (`agent/advanced_actions.py:12-17`, `agent/advanced_actions.py:526-640`, `mod/Source/Commands/ActionExecutor.cs:1209-1317`, `mod/Source/Commands/ActionExecutor.cs:1494-1504`). There is no trustworthy recipe, footprint, hazard, construction-progress, instance-function, input, or network evidence for the trio.
4. **Self-declared sustainability is neither produced nor trusted.** Probe publishes `colony.sustainable=null/unsupported` (`mod/Source/Probe/SurvivalTelemetryReader.cs:1206-1227`) and explicitly refuses to manufacture critical-load, N-1, or sustainable conclusions (`mod/Source/Probe/SurvivalTelemetryReader.cs:8-16`). Strategy requires all loops plus `sustainable is True` (`agent/strategy.py:2516-2550`) and accepts proof-required fields only through an in-process verifier that the live Bridge does not provide (`agent/strategy.py:1788-1808`, `docs/protocol.md:327-343`, `bridge/oni_bridge.py:354-365`, `bridge/oni_bridge.py:1683-1688`). The happy-path verifier is test-only (`scripts/verify_strategy.py:74-94`), while spoofed markers are rejected (`scripts/verify_strategy.py:710-730`). This is safe behavior, but it makes `sustain` unreachable in production.

## Gap matrix

| Phase / objective | What is genuinely observed now | What can genuinely be executed now | Decisive gaps | Current attainable result |
| --- | --- | --- | --- | --- |
| Bootstrap: oxygen | Canonical active-world alert; rolling visible-cell oxygen-family mass is estimated and excludes storage, pipes, bottles, pickupables, hidden/fogged cells (`docs/protocol.md:280-304`) | Persistent low-oxygen alert may propose `set_paused=true` (`agent/rules.py:199-255`) | Usable breathable-zone stock, per-dupe exposure, algae/input runway, gap-free production and demand, operational diffuser instances, distribution | Detect/pause only; cannot correct or certify oxygen security |
| Bootstrap: food | Active-world registered, non-private edible sum (`mod/Source/Probe/ProbeStateReader.cs:1737-1826`) | `focus_note` only (`agent/rules.py:99-195`) | Reachability, ration permission, diet compatibility, spoilage, consumption/production window, seed/crop conditions | Conservative inventory diagnostic; no runway or food production action |
| Bootstrap: sanitation | Active-world completed-prefab count for Outhouse | `focus_note` only; generic building blueprint action is Ladder-only | Instance usability, operational/enabled/broken state, queue/access, waste capacity, handwashing/contamination chain | Count may satisfy a weak Strategy gate, but does not prove sanitation works (`agent/strategy.py:1985-2020`) |
| Stabilize: water | Visible-cell water-family mass is a non-certifying estimate; legacy `water_kg_estimate` is null (`mod/Source/Probe/ProbeStateReader.cs:1720-1734`) | No water or plumbing action | Accessible/usable/germ/temperature-aware inventory, tanks/bottles/pipes, source/sink flow and runway | Water resilience remains unknown (`agent/strategy.py:2023-2061`) |
| Stabilize: power | Active-world batteries, instantaneous load, and rated generation of currently producing generators; rated capacity is not delivered production (`docs/protocol.md:284-286`) | Wire blueprints only; no ManualGenerator/battery/control action | Circuit graph, delivered rate window, fuel/dupe-labor runway, critical consumers, overload and reserve coverage | Gauge/diagnose only; cannot establish critical-load coverage (`agent/strategy.py:2063-2121`) |
| Stabilize: stress/research | Active-world stress/idle counts and current research id | Pause and one duplicant priority; explicit `set_research` capability remains, but empty research now produces advice only and never guesses a target (`agent/rules.py`) | Morale cause/cure, skill/bed/toilet schedules; research queue, station/resource feasibility, and an explicitly justified target | Narrow progress is possible only after a separate target decision; `set_research` replaces the queue and the Probe exposes only current research, so full queue rollback is not observable |
| Expand: thermal and renewable loops | Whole active-world material temperature extrema; all production/consumption window fields reserved but null | Dig, Ladder, Wire, priority, research | Critical zones, object/crop/pipe temperatures and trends; renewable food/O2/water/power production and demand windows | Every expansion gate remains unknown/deferred (`agent/strategy.py:2197-2453`) |
| Expand: fault tolerance | No certified independent source counts or N-1 conclusion | No fault-domain or automation action | Dependency/topology graph, shared upstreams, reserve/runway, simulated or observed failure recovery | Cannot distinguish duplicate equipment from independent sources |
| Space | Cluster/classic raw rocket counts only (`docs/protocol.md:289`) | No rocket action | Mission ledger, modules, fuel/oxidizer, destination, crew, launch/return readiness, base reserve | Counts cannot prove a mission; space gate remains unproven (`agent/strategy.py:2456-2513`) |
| Sustain | No trusted sustainable assertion; renewable rates/topology/space conclusions are null/unsupported (`docs/protocol.md:290-291`) | No certification action should exist | Trusted evidence windows, policy hashes/versions, in-process recomputation, 120 samples and 10 game cycles, all-world coverage | Production path cannot reach `sustain` |
| Multi-world, all phases | Populated world ids are enumerated, but only active world is covered | All current mutable targets are active/single-world bound | Per-world observations, stable all-world evidence identity, step-specific target world, cross-world fault domains | Any multi-populated-world save is untrusted and must be mutation-ineligible |

## Current action and verification boundary

`agent/command_queue.py:45-57` allows exactly `focus_note`, pause, research, one-dupe priority, dig mark/cancel, building blueprint place/cancel, utility path place/cancel, and noop. `ActionExecutor` default-rejects unsupported actions and its building/utility branches are hard-coded to `Ladder`/`Wire` (`mod/Source/Commands/ActionExecutor.cs:1209-1317`, `mod/Source/Commands/ActionExecutor.cs:1462-1505`). The advanced catalog's `catalog_complete=true` is therefore complete only for its v2.7 scope, not for survival (`config/advanced-actions.v1.json:1-23`, `config/advanced-actions.v1.json:81-143`, `config/advanced-actions.v1.json:230-238`). Plans may sequence only catalogued A-C leaves (`agent/colony_plan.py:230-342`); they do not create missing capabilities.

The existing substrate has valuable controls: approved schema and identity/freshness checks (`mod/Source/Commands/ActionExecutor.cs:780-952`), observed-only preconditions/effects and paused-state requirements (`mod/Source/Commands/ActionExecutor.cs:1320-1459`), exact blueprint identity and zero-progress cancellation (`mod/Source/Commands/ActionExecutor.cs:1841-2088`), one mutation in flight and an unknown-outcome latch (`mod/Source/Commands/ActionExecutor.cs:406-778`, `mod/Source/Commands/ActionExecutor.cs:2546-2601`). Those controls should be reused, not weakened.

Existing Python verification is mostly contract/static/mock evidence. For example, Batch C checks source fragments and fixture state transitions (`scripts/verify_advanced_actions.py:930-1055`), and the runtime priority game-verify scenario reads pre-existing marker files rather than dispatching a game action (`scenarios/game_verify_runtime_priority.json:13-45`). These are useful regression checks but are not proof that a current build can operate a survival loop. The known negative scenario correctly classifies a missing effect as `behavior_fail` (`scenarios/game_verify_known_behavior_failure.json:13-27`).

## Ranked next implementation sequence

The sequence below treats fresh priority change/restore as an admission gate, not as implementation. Multi-world and truthful rollback remain P0 release gates. Read-only catalog work may proceed under an explicit single-world test-only feature gate, but no new life-support mutation should be released against saves with more than one populated world.

### G0 — Fresh live priority change and restore (admission gate)

- **Observations:** exact clone/session/save/world/snapshot/state hashes; one live duplicant stable instance/name; current chore group priority; later trusted witness sequence; queue and latch state.
- **Action types:** existing `set_duplicant_chore_priority` only, first `from -> to`, then a separately approved `to -> from`; one deliberately wrong `expected_name` negative.
- **Preconditions/effects:** verified test clone; paused; fresh observed field; exact instance/name/from value; one mutation in flight. Each success requires accepted, executed, local-effect-observed, and a later trusted snapshot with the exact target value.
- **Immutable evidence:** before either confirmed priority command can enter `done`, Bridge must create-only publish and revalidate its command-local approval bundle containing the exact source frame, final intent/command, receipt, and enabled policy with one common policy hash. After each confirmation, freeze its certificate and ordered ACK/witness records, disable policy, save/reload, and bind the freeze to the unique `priority_forward` or `priority_inverse` checkpoint. Decision-frame retention is not evidence storage.
- **Rollback:** the inverse command is explicit and separately approved; verify the original priority is restored. Ambiguous dispatch is never replayed; inspect queue/log/result and honor the mutation latch.
- **Finalization:** the create-only priority-pair report must bind the active manifest's latest checkpoint hash, exactly two distinct command bundles/freezes in forward-then-inverse order, the exact same instance/name/group, mathematical `from -> to -> from`, forward witness before inverse source/acceptance, continuous before/save/reload/session/hash links, restored final observed priority, disabled policy, and empty nonterminal/temp queues. It must additionally bind the canonical `command_pipeline_readiness(...)` hash/summary produced from the exact final `latest_state.json` and disabled policy; mutation proposal must be ready and relevant commands, ambiguous outcomes, duplicate ids, stale pending records, invalid records, and in-flight mutations must be empty.
- **Scenarios:** positive change/restore; wrong-name `behavior_fail` with no execution ACK; stale snapshot rejection; restart/witness continuity; missing/conflicting automatic bundle; same-round source-frame pruning after confirmed commit; tampered/missing/swapped freeze; different target; non-inverse values; old frame; duplicate command id; nonempty queue/temp file; same-save `failed/outcome_unknown` with otherwise empty nonterminal queues; wrong final value; original save and normal Mod/config hashes unchanged; queues empty and no helper processes afterward.

### P0-A — All-populated-world observation and target binding

- **Missing observations:** `worlds.by_id.<world_id>` containing per-world duplicants, alerts, accessible resources, buildings, cells, networks, rates, and quality/provenance; sorted `covered_world_ids` equal to every populated world; stable populated-world-set identity in every evidence window.
- **Action types:** add mandatory `target_world_id` to every mutable leaf and resolve targets through that world, not incidental UI-active state. Prefer direct world-scoped APIs. If an API truly requires a UI switch, isolate a separate `select_active_world` control action with exact prior/target ids; do not hide a switch inside another action.
- **Preconditions/effects:** paused, fresh all-world snapshot, target in populated/covered set, exact object/cell belongs to target world, no other mutation. Effect must remain in the same world; a UI-switch action must prove only `active_world_id` changed and no game object changed.
- **Rollback:** normal leaves use their own inverse in the same target world. A UI switch, if unavoidable, restores the exact prior world only after proving no mutation is in flight. Cross-world ambiguity pauses and latches globally.
- **Scenarios:** two-populated-world complete coverage accepted; one missing world blocks trust and every mutation; wrong-world cell/instance rejected; active UI switch does not reset all-world evidence; per-step multi-world plan resumes correctly; shared cross-world dependency included in N-1; world removal/addition resets only the affected evidence generation.
- **Required coordinated changes:** remove the active-world-only Strategy blockers; revise the telemetry contract's one-item maximum; key `PhaseEvidenceTracker` by a stable world set rather than `last_world_id`; make plans bind save plus per-step world; retain explicit single-world compatibility fixtures (`mod/Source/Probe/SurvivalTelemetryReader.cs:130-135`, `agent/strategy.py:1116-1121`, `agent/strategy.py:669-672`).

### P0-B — Make asynchronous blueprint rollback semantics true and testable

- **Missing observations:** pending command's exact created instance ids; per-instance worker/progress/completed state at timeout; cancellation result and later trusted restoration witness.
- **Action types:** no new game authority. Route timeout either to (a) exact native cancellation only when every created identity is directly proven unfinished/unworked, or (b) `outcome_unknown` + pause/latch when that proof is absent.
- **Preconditions/effects:** never cancel by cell/def alone; require exact command-created instance id, same world, worker null, progress zero, no `BuildingComplete`. Cancellation effect is exact disappearance plus restored footprint fingerprint.
- **Rollback:** synchronous partial placement may cancel all command-created objects. After dispatch, automatic cancellation is permitted only under the exact unstarted proof above. Otherwise do not claim rollback; latch and require reconciliation. Once work starts, automatic rollback is forbidden.
- **Scenarios:** postcondition timeout before any worker cancels and later proves restoration; timeout after worker/progress latches without cancel/deconstruct; partial multi-object placement restores all; malformed/missing witness yields `outcome_unknown`; docs/catalog/test report match the selected behavior.

### S1 — Read-only functional-building catalog and instance telemetry

This is the smallest safe implementation after G0 because S2 cannot bind a trustworthy command without it.

- **Missing observations:** for exact defs `Outhouse`, `OxygenDiffuser`, and `ManualGenerator`: allowed orientations; root and full footprint cells; foundation/clearance/port cells; recipe material tags and mass; selected material and available mass in the target world; `IsValidPlaceLocation`/`IsValidBuildLocation`; visibility, occupancy, liquid, gas, temperature and access hazards; construction instance id, worker, delivered material, progress, priority; completed instance id; enabled/operational/broken/entombed/flooded/reachable state; Outhouse uses/waste/access, diffuser algae/power/oxygen output, generator operable/power output/circuit connection.
- **Action types:** none.
- **Preconditions/effects:** each observation carries observed quality, exact source, target-world scope, snapshot timestamp, completeness and stable identities. Catalog hash binds def recipe/footprint/ports for the current game build.
- **Rollback:** none; read-only reader failures publish `null/error/unsupported`, never zero or inferred safety.
- **Scenarios:** static contract and mock fixtures for all three defs; valid and invalid footprints/orientations/material selections; insufficient material; liquid/hazard/access blocks; completed-but-disabled/broken/flooded instances do not count as functional; two-world isolation; catalog drift/hash mismatch fails closed; bounded read budget and active-world-change race.

### S2 — Exact bootstrap-trio blueprint place/cancel loop

Do not silently broaden the already-verified Ladder verb. Add a distinct typed pair such as `place_survival_building_blueprint` and `cancel_survival_building_blueprint`, allowlisting exactly `Outhouse`, `OxygenDiffuser`, and `ManualGenerator`, with one root/footprint per approval.

- **Observations:** all S1 catalog/candidate fields plus exact empty footprint fingerprints and selected-material availability.
- **Action types:** `place_survival_building_blueprint`; `cancel_survival_building_blueprint`.
- **Preconditions/effects:** explicit target world; verified clone; paused; fresh/trusted observation; no alerts requiring emergency handling, unknown command, or mutation latch; sandbox/instant-build false; exact catalog hash, def, orientation, recipe, material mass, root/footprint and ports; every cell visible, empty, reachable, non-liquid and within hazard policy; no conflicting blueprint; one target. Immediate effect is one exact positive Constructable instance with matching world/def/root/orientation/material/footprint, worker null and progress zero, no `BuildingComplete`, and unchanged element/mass/damage fingerprints.
- **Rollback:** native cancel the exact created instance only while worker is null, progress is zero, and no completion exists; prove the full footprint restored. Synchronous partial failure follows P0-B. Once construction starts, the action has crossed an irreversible boundary: do not deconstruct (currently permanently forbidden); pause and use forward-only recovery.
- **Scenarios:** place then cancel each of the three defs with exact restoration; wrong world/def/material/catalog hash/root/footprint/orientation; occupied/liquid/hot/inaccessible candidate; insufficient exact material; construction-race cancellation rejected; partial placement rollback; timeout branches from P0-B; reload/restart witness; original assets and normal config unchanged.
- **Acceptance limit:** this loop proves safe reversible designation only. It must not be reported as improved oxygen, sanitation, or power.

### S3 — Construction commitment and functional bootstrap recovery

- **Missing observations:** delivered materials, assigned worker, progress transition, construction errors, completed instance identity lineage, enabled/operational/input/output/connectivity state, and domain signal deltas.
- **Action types:** `set_constructable_priority` for an exact Constructable instance; `set_building_enabled` for an allowlisted completed instance. Existing `set_paused` may create an explicit run window; no direct/instant completion action.
- **Preconditions/effects:** fresh exact instance; sufficient survival reserve; reachable builder/material; no competing emergency; explicit acknowledgement that progress loses cancellation rollback. Completion must preserve lineage from blueprint id to completed id. Functional effects are building-specific: usable Outhouse with capacity/access; powered/algae-fed diffuser producing oxygen and improving the trusted O2 window; ManualGenerator connected and producing delivered power/recovering reserve.
- **Rollback:** priority and enabled state can be inverses only while their exact from-state remains current and disabling does not remove the sole critical source. Construction itself has no rollback; after progress starts use forward-only repair/configuration.
- **Scenarios:** one clone per building completes through native work and becomes functional; missing material/worker/path stalls without false success; sole-life-support disable blocked; instance replacement rejected; completion without operational input is not success; expected domain delta over a bounded window; save/reload identity continuity.

### S4 — Oxygen control loop

- **Missing observations:** per-world breathable zones and per-dupe exposure, stored/piped gas, diffuser/electrolyzer inputs and runway, delivered power, gap-free oxygen production/consumption, distribution and low-O2 alert trend.
- **Action types:** exact-instance `set_life_support_enabled`; S2 placement for an additional allowlisted source; later typed gas utility path/configuration actions.
- **Preconditions/effects:** all-world coverage; input and power runway; location will not overpressure/overheat/contaminate; critical consumers remain powered. Success requires the same instance operational plus production at/above demand and alert/exposure recovery over a trusted window, not merely completed count.
- **Rollback:** restore prior enabled state only if another proven source covers demand; blueprint inverse only before progress; otherwise forward repair.
- **Scenarios:** low-O2 recovery; insufficient algae/power; overpressure; isolated pocket; report rollover/gap; multi-world demand; shared-power failure; rate improvement without alert recovery remains at risk.

### S5 — Accessible food and renewable food loop

- **Missing observations:** reachable and ration-permitted kcal per duplicant/diet, spoilage, consumption; seeds, crop instances, harvest state, farm tiles/planters, irrigation/fertilization, light/gas/temperature, and gap-free harvest production.
- **Action types:** `place_food_building_blueprint`/cancel for a small explicit allowlist; exact-instance `plant_crop` and `set_crop`; exact storage/ration configuration only after its own contract.
- **Preconditions/effects:** seed/material/access/temperature/gas policy satisfied; no food reserve regression below emergency margin. Success is accessible reserve plus production at/above consumption over the phase window.
- **Rollback:** cancel only unstarted blueprints; restore reversible crop/config choice only before destructive uproot/harvest boundaries; never destroy the sole food source automatically.
- **Scenarios:** no seed, wrong atmosphere/temperature, unreachable/ration-forbidden food, spoilage, harvest lag, duplicate crop counts, multi-world diets, sustained production balance and reserve recovery.

### S6 — Water and sanitation closed loop

- **Missing observations:** reachable usable water by germ/temperature/material/location; bottles/tanks/pipes; source/sink flow windows; toilet/outhouse uses, queues and accessibility; handwashing path; polluted-water/output capacity; spills/floods/blockages.
- **Action types:** typed `place_liquid_utility_blueprint_path`/cancel; explicit pump, sink/lavatory and storage blueprint verbs; exact-instance enabled/direction/filter controls.
- **Preconditions/effects:** exact world, port topology, material, footprint, source quality and sink capacity; no cross-connection or flood hazard. Effects require exact topology plus sustained input/output flow and successful sanitation use.
- **Rollback:** blueprint cancellation only pre-progress; reversible control values swap exact before/after; constructed plumbing uses forward isolation/repair, never broad deconstruction.
- **Scenarios:** wrong port/direction, full output, germy/hot water, blocked pipe, flood, insufficient power, handwash bypass, two-world separation, sustained use and waste evacuation.

### S7 — Critical power and thermal resilience

- **Missing observations:** circuit graph, wires/bridges/transformers, delivered generation/load windows, overload, batteries/reserve, fuel and duplicant-labor runway, critical-consumer classification; critical habitat/farm/equipment zones, object/pipe temperatures, trend and cooling capacity.
- **Action types:** allowlisted generator/battery/wire and insulation/cooling blueprints; exact-instance enabled/automation/temperature-setpoint controls.
- **Preconditions/effects:** graph/topology and material/fuel/heat budget known; modification cannot disconnect or overload critical loads. Power succeeds only with critical coverage and reserve recovery; thermal succeeds only when every defined critical zone remains in policy over a trusted window.
- **Rollback:** pre-progress blueprint inverse; exact reversible settings only if sole-source/overshoot guards pass; construction/heat injection is forward-recovery after commitment.
- **Scenarios:** brownout, overload, fuel exhaustion, dupe unavailable, shared transformer false independence, battery recovery, cooling power feedback, hot/cold overshoot, sensor/automation failure, per-world critical zones.

### S8 — Renewable/fault certification and production trust path

- **Missing observations:** gap-free per-world production/consumption counters with actual game-time deltas; source/sink/resource runway; dependency graph and independent failure domains; incident history; policy-defined critical zones; all-world reserve and recovery evidence.
- **Action types:** no `claim_sustainable` mutation. Provide a Bridge-local/in-process evidence-window verifier that recomputes input hashes and allowlists policy hash/version; Strategy should derive sustainability from trusted loop proofs or accept only the recomputed versioned certification.
- **Preconditions/effects:** complete all-world coverage; monotonic sequence/time; no gaps/rollover ambiguity; source data hashes and approved policy version; at least the Strategy-required sample/duration/game-cycle windows (`agent/strategy.py:76-102`). Effect is a verifiable derived record, never a Probe-authored self-assertion.
- **Rollback:** certification is revoked immediately on gap, topology/world-set change, incident or policy/hash mismatch. It never changes game state.
- **Scenarios:** self-declared marker spoof rejected; correct recomputation accepted; one missing sample, restart discontinuity, report rollover ambiguity, shared upstream and N-1 fault each fail closed; sustained 120-sample/10-cycle case passes; regression revokes on the next sample.

### S9 — Space mission and return loop

- **Missing observations:** versioned mission ledger; rocket/modules/destination; fuel/oxidizer; crew health/suits/food/O2; launch/landing state; mission completion and return; all-world base reserves while crew/assets are away.
- **Action types:** separately approved exact rocket/module configuration, fueling, crew assignment and launch. Launch must be its own irreversible high-risk leaf with a short-lived readiness token.
- **Preconditions/effects:** S8 sustainability and fault gates remain satisfied; mission/return/base-reserve certifications trusted; exact rocket/destination/crew; no emergency or in-flight mutation. Success requires ledger-confirmed mission completion and safe return without base-reserve regression.
- **Rollback:** configuration/assignment may revert before launch from exact before-state. After launch there is no rollback; use mission-specific forward abort/return only if explicitly supported and observed.
- **Scenarios:** abort before launch; wrong rocket/crew/destination; insufficient fuel/O2/base reserve; launch-time stale token; safe launch/mission/return; stranded/failure classification; base regression; multi-world crew and reserve accounting.

## Recommended file boundaries for the first slices

Keep survival authority separate from the already-verified Ladder/Wire catalog so prior acceptance evidence is not silently reinterpreted:

- **Multi-world P0:** evolve telemetry in `mod/Source/Probe/SurvivalTelemetryReader.cs` and `mod/Source/Probe/ProbeStateReader.cs`; add a versioned `tools/game_verify/contracts/oni_telemetry.v2.json`; change scope/tracker logic in `agent/strategy.py`; add per-step world binding in `agent/colony_plan.py` and contracts in `agent/field_contracts.py`; enforce `target_world_id` in `mod/Source/Commands/ActionExecutor.cs`; extend `scripts/verify_strategy.py`, `scripts/verify_colony_plan.py`, and `scripts/verify_advanced_actions.py`.
- **Rollback P0:** constrain changes to `mod/Source/Commands/ActionExecutor.cs`, the exact pending-blueprint observations in `mod/Source/Probe/ProbeStateReader.cs`, `config/advanced-actions.v1.json`, `docs/protocol.md`, `scripts/verify_advanced_actions.py`, and the runtime blueprint scenario. One selected timeout behavior must appear consistently in all six.
- **S1 telemetry:** add `mod/Source/Probe/FunctionalBuildingTelemetryReader.cs` rather than enlarging unrelated resource readers; register it in `ProbeStateReader.cs`; add `tools/game_verify/contracts/oni_functional_buildings.v1.json`, field contracts, a dedicated `scripts/verify_functional_building_telemetry.py`, and one read-only scenario.
- **S2/S3 actions:** add `agent/survival_actions.py` and `config/survival-actions.v1.json`; route the two new verbs through `agent/command_queue.py` and plans only after verification; isolate game APIs in `mod/Source/Commands/SurvivalBuildingActionHandler.cs` with `ActionExecutor.cs` retaining common approval/claim/witness/latch control; add `scripts/verify_survival_actions.py` and `scenarios/game_verify_runtime_survival_blueprint.json`.
- **Trusted sustain:** keep raw Probe telemetry separate from derivation. Add Bridge-side `agent/evidence_window.py` and `agent/sustainability_certifier.py`; wire the callable in `bridge/oni_bridge.py`; update `agent/strategy.py` and a new versioned certification contract. Never make Probe write a self-authoritative `sustainable=true` gauge.

## Release acceptance standard

Every slice should retain the existing command approval, identity, one-mutation, later-witness and unknown-outcome controls. Scenario evidence should classify `behavior_fail`, `runtime_fail`, `infra_fail`, `visual_fail`, or `flaky_fail` as applicable; state/event evidence is mandatory and screenshots are supplementary. Each live scenario must use a named verified clone, capture original save and normal Mod/config hashes before/after, prove normal exit, restore any reversible setting, leave queues empty, and leave no helper process. No slice may infer zero/safety from missing telemetry or replay an ambiguously dispatched command.

The first safe survival-improving path is therefore **A08 offline emergency arbitration -> fresh production bootstrap -> G0 live admission -> S1 -> S2 reversible blueprint proof -> explicit S3 construction commitment -> functional-domain recovery**, with **P0-A multi-world** and **P0-B rollback truth** blocking release. A08 may be implemented before G0 but must not be treated as live-proven by offline evidence. Starting directly at Outhouse/OxygenDiffuser/ManualGenerator placement would copy Ladder's shape without the observations needed to prove correct footprint, material, instance, function, or rollback.
