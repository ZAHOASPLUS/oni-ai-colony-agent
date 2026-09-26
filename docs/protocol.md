# ONI AI Colony Agent Protocol V2

The mod starts in Observer mode. Copilot advice and Controlled Agent actions are
separate layers: telemetry is always local, rule advice is quality-gated, and
the game reads an action only after a local policy and explicit approval both
allow it.

## Runtime Layout

The default root is
`%USERPROFILE%\Documents\Klei\OxygenNotIncluded\ai-bridge`:

```text
state.jsonl
decision-frame.json
control-packet.json
action-outcomes.json
action-outcomes-commit.json
latest_state.json
state_summary.md
advice.json
incidents.json
plan.json
strategy.json
strategy.md
colony-plan.json
health.json
llm-health.json
action-policy.json
sessions/<session_id>/
  state.jsonl
  decision-frame.json
  control-packet.json
  action-outcomes.json
  latest_state.json
  state_summary.md
  advice.json
  incidents.json
  plan.json
  strategy.json
  strategy.md
  health.json
commands/
  pending/
  approving/
  approved/
  executing/
  confirming/
  done/
  failed/
  rejected/
  quarantine/
colony-plans/
  active.json
  <plan_id>/
    current.json
    revisions/000001.json
    corrupt/
```

Set `ONI_AI_BRIDGE_DIR` before launching ONI and the Bridge to use another
root. Runtime data, secrets, reports, and save backups are not project source.

## Event Envelope

Every complete line of `state.jsonl` is one JSON object. Events use
`oni.event.v2` and carry the active process/save identity:

```json
{
  "schema": "oni.event.v2",
  "event": "session_started",
  "ts": "2026-07-11T01:00:00+08:00",
  "session_id": "session-20260711-010000-a1b2c3d4",
  "seq": 1,
  "game_build": "cl-740622",
  "mod_version": "0.6.0",
  "save_id_hash": "sha256:...",
  "world_id": 0
}
```

Important events include `probe_loaded`, `probe_patch_registered`,
`session_started`, `save_loaded`, `state_snapshot`, `command_ack`,
`save_write_started`, `save_write_completed`, `save_unloaded`, `session_ended`,
`probe_shutdown`, and `probe_error`.
Sequence numbers are strictly increasing within a session. A new save load or
game restart creates a new session; Bridge never substitutes a retired
session's snapshot.

`seq` is the ordinal for every event in the session, including acknowledgements
and save lifecycle events. `state_snapshot` additionally carries a positive
`snapshot_seq` that increments only for snapshots. Intervening non-snapshot
events may therefore make successive snapshot `seq` values non-consecutive,
while their `snapshot_seq` values must remain consecutive. `snapshot_id`
continues to be `<session_id>:<seq>`; it is not based on `snapshot_seq`.

For a live stream, only a `session_started` envelope that is the first record
of that session with `seq=1` establishes a trusted boundary. A structurally
valid snapshot may be retained as a diagnostic baseline after Bridge startup,
but a later `session_started` cannot retroactively certify it; late, repeated,
or `seq>1` starts leave the stream untrusted. A transition introduced by any
other event is ignored. Retired session ids cannot reclaim the active session.

`SaveLoader.Save` is observed before and after its synchronous file write. The
completion event contains only path fingerprints, never a local path. A save is
successful only when the requested/result paths match, a file rewrite is
observed, both file hashes and ONI's own save header are readable, and an updated save pointer matches
when requested. Success rotates `save_id_hash` to the active file and clears
`save_dirty`; every ambiguous or failed write leaves `save_dirty=true`, so
subsequent actions fail closed.

## State V2 And Quality

Snapshots use the additively extended `oni.state.v2`; the schema name did not
change. `cycle` is the UI-facing, one-based integer cycle, while
`game_time_cycles` is the continuous one-based game clock and must satisfy
`cycle <= game_time_cycles < cycle + 1`. Unknown, zero, empty, and normal are
distinct values.

```json
{
  "schema": "oni.state.v2",
  "event": "state_snapshot",
  "session_id": "session-...",
  "seq": 14,
  "snapshot_seq": 4,
  "snapshot_id": "session-...:14",
  "game": {"paused": true},
  "cycle": 5,
  "game_time_cycles": 5.42,
  "duplicants": {"total": 5, "stressed": null, "idle": null},
  "resources": {
    "food_kcal": 18000.0,
    "oxygen_kg_estimate": null,
    "water_kg_estimate": null
  },
  "observation_scope": {
    "active_world_id": 0,
    "populated_world_ids": [0],
    "covered_world_ids": [0],
    "complete_for_populated_worlds": true
  },
  "materials": {
    "active_world_cells": {
      "clean_oxygen_kg": 950.0,
      "polluted_oxygen_kg": 80.0,
      "clean_water_kg": 12000.0,
      "polluted_water_kg": 900.0,
      "salt_water_kg": 0.0,
      "brine_kg": 0.0,
      "valid_cell_count": 92160,
      "candidate_cell_count": 98304,
      "scan_generation": 2,
      "scan_progress_ratio": 0.42,
      "scan_status": "partial",
      "scan_complete": false,
      "aggregate_available": true,
      "aggregate_scan_generation": 1,
      "aggregate_window_started_at": "2026-07-11T01:00:00+08:00",
      "aggregate_window_completed_at": "2026-07-11T01:00:08+08:00",
      "aggregate_age_seconds": 2.0
    }
  },
  "production": {
    "food_kcal_per_cycle": null,
    "oxygen_kg_per_cycle": null,
    "water_kg_per_cycle": null,
    "power_watts": null
  },
  "consumption": {
    "oxygen_kg_per_cycle": null,
    "water_kg_per_cycle": null,
    "power_watts": null
  },
  "power": {
    "stored_joules": 60000.0,
    "capacity_joules": 100000.0,
    "battery_percent": 60.0,
    "active_load_watts": 120.0,
    "active_rated_generation_watts": 400.0,
    "battery_count": 1,
    "consumer_count": 4,
    "generator_count": 1,
    "active_generator_count": 1,
    "critical_load_covered": null,
    "independent_source_count": null
  },
  "temperature": {
    "active_world_material_min_c": -12.0,
    "active_world_material_max_c": 86.0,
    "critical_zones_safe": null,
    "min_c": null,
    "max_c": null
  },
  "rockets": {
    "total": 1,
    "grounded": 1,
    "launching": 0,
    "in_flight": 0,
    "landing": 0
  },
  "alerts": null,
  "research": {"current": "FarmingTech", "complete": null},
  "buildings": {
    "oxygen_diffuser": 1,
    "outhouse": 1,
    "manual_generator": 1,
    "_scanned": 120,
    "_source": "typed_active_world"
  },
  "quality": {
    "game.paused": {"status": "observed", "source": "SpeedControlScreen.IsPaused"},
    "cycle": {"status": "observed", "source": "GameClock.GetCycle", "semantic": "display_1_based"},
    "game_time_cycles": {
      "status": "observed",
      "source": "GameClock.GetTimeInCycles",
      "semantic": "display_1_based_continuous",
      "unit": "cycle",
      "scope": "save",
      "completeness": "complete",
      "captured_at": "snapshot.ts"
    },
    "materials.active_world_cells.clean_water_kg": {
      "status": "estimated",
      "source": "WorldContainer.WorldOffset+WorldSize-HiddenYOffset+Grid.IsVisible+Grid.Element+Grid.Mass+Grid.Temperature",
      "semantic": "rolling_window_gauge",
      "unit": "kg",
      "scope": "active_world_visible_cells",
      "completeness": "complete",
      "captured_at": "materials.active_world_cells.aggregate_window_completed_at"
    },
    "production.oxygen_kg_per_cycle": {
      "status": "unsupported",
      "source": "none",
      "semantic": "window_rate",
      "unit": "kg/cycle",
      "scope": "all_populated_worlds",
      "completeness": "unknown",
      "captured_at": "snapshot.ts",
      "reason": "No gap-free production counter window is available."
    },
    "research.current": {
      "status": "observed",
      "source": "Research.GetActiveResearch",
      "contract_id": "oni.qc.research.current.v1",
      "semantic": "active_selection",
      "unit": "technology_id_or_empty",
      "scope": "save_research",
      "completeness": "complete",
      "captured_at": "snapshot.ts"
    },
    "alerts": {"status": "unsupported", "source": "none"}
  },
  "sample": {"interval_seconds": 5.0, "duration_ms": 2.1},
  "writer": {
    "queue_depth": 0,
    "dropped_snapshot_count": 0,
    "fault_count": 0,
    "non_finite_numeric_count": 0
  }
}
```

Live admission requires all four writer diagnostics to be exact integer zeroes.
`fault_count` and `non_finite_numeric_count` are session-scoped counters reset
by `session_started`; `dropped_snapshot_count` is process-lifetime sticky. A
positive `queue_depth` blocks that snapshot because it was captured while older
records were still waiting to publish.

Quality status is `observed`, `estimated`, `unsupported`, `error`, or `stale`.
Rules and action preconditions may use only explicitly allowed trustworthy
statuses. In particular, `research.current=null` with `unsupported` quality is
not interpreted as no active research. An observed empty string means the game
API positively reported no active research.

The additive survival telemetry deliberately publishes raw measurements and
explicitly unsupported decision fields instead of manufacturing conclusions:

| Payload | Raw meaning and scope | Important non-claim |
| --- | --- | --- |
| `observation_scope` | Active world, live-duplicant-populated worlds, covered worlds, and whether every populated world is covered | The current Probe covers only the active world; a multi-world colony therefore reports incomplete coverage |
| `duplicants.total` | Complete, unique positive-instance count from the typed active-world `LiveMinionIdentities` view, bounded at 1,000 | It is not a save-wide population count and does not include child worlds |
| `resources.food_kcal` | Active-world `WorldInventory` edible registrations excluding `StoredPrivate`, converted from internal calories to kcal | Registration does not prove reachability or ration permission; no registered sum is converted into runway, and an `estimated/bounded` lower bound cannot establish that total food is low |
| `materials.active_world_cells` | A budgeted rolling scan of revealed, non-hidden active-world cells; current progress is published beside the last completed generation's oxygen/water-family masses and material-temperature extrema | The aggregate is `estimated`, cross-time rather than atomic, and excludes storage, pipes, bottles, pickupables, hidden and fogged cells; positive mass never certifies total usable reserves |
| `power.stored_joules`, `capacity_joules`, `battery_percent` | Active-world registered storage batteries with transformer-backed `Battery` components excluded; percent is the energy-weighted ratio | It does not prove critical-load coverage or outage survival |
| `power.active_load_watts` | Instantaneous active registered consumer load | It is not a cycle demand window |
| `power.active_rated_generation_watts` and counts | Rated capacity of currently producing registered non-transformer generators and raw component counts; `PowerTransformer : Generator` instances are explicitly excluded | Observing rated capacity is not observing delivered production or independent failure domains |
| `report_counters.current/previous_closed.oxygen_created` | Save-global raw positive and negative `ReportManager` counters for the open report epoch and latest closed epoch | They cover only report-instrumented events, the open epoch is partial, rollover is not an integer cycle boundary, and neither counter is a gap-free rate |
| `temperature.active_world_material_min_c/max_c` | Extremes over positive-mass cells in the bounded active-world Grid scan | They are not policy-defined critical-zone extrema; `temperature.min_c/max_c` and `critical_zones_safe` remain unsupported |
| `rockets.total/grounded/launching/in_flight/landing` | Cluster-wide raw counts in cluster-space mode; classic mode observes only total | Counts do not prove mission readiness, completion, return readiness, or base reserve safety |
| `production.*` and `consumption.*` | Reserved window-rate fields with explicit units | All remain `null`/`unsupported` until a gap-free all-populated-world counter window exists |
| topology, space, and sustainability conclusions | `power/oxygen/water.independent_source_count`, `resilience.n_minus_one_safe`, `space.*`, and `colony.sustainable` | Remain `null`/`unsupported`; no Probe gauge may stand in for a versioned policy proof |

The active-world Grid scanner refuses more than 500,000 candidate cells and
processes at most 4,096 candidates or 4 ms per snapshot call. It keeps a
current scan and a last-completed double buffer; no aggregate is available
until the first full revealed-cell sweep completes, and a new scan never
publishes its partial sums over the previous generation. Aggregate quality is
always `estimated` with `captured_at` bound to
`materials.active_world_cells.aggregate_window_completed_at`, becomes `stale`
after 900 seconds, and resets on session/world/bounds changes or exceptions.
Material-temperature extrema exclude vacuum (`mass <= 0`). Strategy may use a
contract-valid zero visible-cell oxygen aggregate only as a conservative risk
signal; positive cell mass, water-family mass, and whole-map extrema are
non-certifying diagnostics.

`resources.food_kcal` is wire-level kcal. The typed inventory reader converts
ONI's internal `Edible.Calories` value by dividing by 1,000 before publishing.
Its exact quality contract is `registered_non_private_inventory_sum` over
`active_world_registered_non_private_edibles`; a full enumeration is
`observed/complete`, while the 10,000-item bound is `estimated/bounded`.
Unavailable registries, exceptions, and non-finite calories fail closed. The
unverified `RationMonitor` reflection fallback has been removed. Strategy may
escalate a low complete registry sum as risk. For an `estimated/bounded`
enumeration, a low enumerated lower bound means the total is unknown rather
than proving the total is low. Only a future reachability-and-ration-permission
aggregate may satisfy the food runway gate.

`report_counters` never calls `DailyReport.GetEntry`, because that observer
would create a missing report entry. The reader iterates at most 256 public
entries, requires exactly one `OxygenCreated` entry, finite counters with
`positive_kg >= 0` and `negative_kg <= 0`, a stable repeated read, and a closed
epoch strictly older than the open epoch. Failure clears the affected period
to `null/error`; no missing value is replaced by zero. Future rate derivation
must compare epochs over the actual `game_time_cycles` delta and reject gaps or
rollover ambiguity.

For a field to certify a Strategy gate, `status=observed` is necessary but not
sufficient. Its quality entry must match the field contract's `semantic`,
`unit`, `scope`, `completeness`, non-empty `source`, and
allowed `captured_at` binding. Strategy resolves that binding to a real
timezone-aware timestamp; rolling aggregates additionally enforce their
maximum capture age and future-skew bound. Derived window rates and certified conclusions
also require an approved `authority`, SHA-256 `input_hash` and `policy_hash`,
and a non-empty `policy_version`, but self-declared metadata is still not proof.
The Strategy accepts those fields only when its caller supplies an in-process
verifier that recomputes the input hash and matches the policy hash/version to
an allowlist. The production Bridge currently supplies no such verifier, so
all proof-required rates and conclusions remain unverified even if a fixture
copies the correct metadata shape. Window rates reserve
`oni.evidence-window.v1`, policy assertions/topology reserve
`oni.certification.v1`, mission completion reserves `oni.mission-ledger.v1`,
and policy-critical temperature extrema reserve `oni.evidence-window.v1` for
future trusted producers.

Building diagnostics use the typed active-world registry and refresh every 30
seconds. Their cache is reset whenever a save session starts, while critical
decision fields continue to refresh on every five-second snapshot.

The probe samples at most 32 otherwise-untracked live dig designations, matching
the command target limit. Cells explicitly referenced by commands remain in the
bounded tracked-cell set even when they are outside that sample.

Snapshot capture enqueues detached primitive/container records. The writer
thread streams JSON directly to the append-only log; the game thread never
constructs a full snapshot-sized JSON string. This keeps large snapshots off
the large object heap and prevents serialization-driven full-GC pauses from
appearing inside later state reads.

## Bridge Reliability

Bridge keeps an incomplete final line unread until its newline arrives. It
tracks malformed lines, duplicate/out-of-order/gapped sequences, partial bytes,
retired-session records, rotation, freshness, and current output status in
`health.json`.

Live snapshot admission is fail-closed. A trusted `oni.state.v2`
`state_snapshot` must have a non-empty session id, a positive integer sequence,
`snapshot_id == "<session_id>:<seq>"`, a valid save SHA-256, a non-negative
integer world id, build and mod identifiers, boolean `save_dirty`, a quality
map, a parseable timestamp no more than five seconds in the future, and a
sample interval between 0.1 and 60 seconds. The snapshot becomes stale after
three declared sample intervals. Writer drops, writer faults, invalid JSON,
duplicate/out-of-order/gapped sequence history, or a missing, late, repeated,
or non-`seq=1` `session_started` also prevent `trust_ready` (invalid JSON is session-blocking once a current
session exists). While any blocker is present, health is
`untrusted_snapshot` or `stale`, open incidents are not promoted to advice, and
no incident suggestion is synchronized to the command queue.

The additive telemetry contract additionally requires a positive integer
`snapshot_seq <= seq`, continuous `game_time_cycles`, internally coherent
world coverage, complete bounded material scans, and cross-field invariants
such as stored joules not exceeding capacity and rocket status partitions
summing to total. Base Bridge admission retains the all-event `seq`/
`snapshot_id` identity rule and independently rejects duplicate,
out-of-order, or gapped snapshot ordinals. `PhaseEvidenceTracker` also uses
consecutive `snapshot_seq` values as the no-missing-snapshot proof clock.

- Model-facing files are written by temporary file plus atomic replacement.
- Raw records are partitioned under `sessions/<session_id>/state.jsonl`.
- `state.jsonl` rotates at 20 MiB with three backups.
- A snapshot older than three sample intervals becomes stale and cannot create
  advice commands.
- Startup replay is bounded to the latest 10,000 complete lines.

Before action confirmation, Bridge separately scans unresolved `executing/`
and `confirming/` claims for their exact session, source snapshot, command, and
attempt identities. It then streams the existing session archive, the current
root log, and available rotation backups to recover only the named source and
ACK records that fell outside the startup replay tail. Exact canonical copies
are de-duplicated, conflicting variants remain visible, and missing records are
merged by positive producer sequence through atomic replacement. The scan is
bounded to 256 unresolved claims, 16 canonical variants per requirement, and a
4 MiB record, with at most 32 MiB of matched canonical payload retained across
the scan; existing archives are never loaded wholesale.

`evidence-continuity.json` records the scanned sources, recovery count, limits,
and per-command blockers. Missing source/ACK evidence, conflicting variants,
invalid pipeline identity, target-related corruption or truncation, unsafe
archive ordering, I/O failure, or a recovery bound being exceeded adds
`evidence_continuity_blocked` to `health.json` and clears `trust_ready`. Bridge
therefore cannot publish a snapshot-confirmed outcome from incomplete or
ambiguous recovered history.

Raw input can never establish its own Bridge authority: any input `bridge`
object is overwritten after local admission. Non-actionable `--input` replay
does not evaluate and render a discarded Strategy for every historical line;
it evaluates the final diagnostic generation once, keeping the 10,000-line
bound inexpensive without turning replay history into live evidence.

`--input` is replay mode. Replay may relax live admission only so historical
fixtures can be inspected; it does not make those records live evidence.
Default replay never creates pending suggestions and reports
`replay_suggestions_suppressed=true`. The explicit
`--allow-replay-suggestions` test switch is accepted only with a fresh isolated
output directory containing no live `state.jsonl`, action policy, or command
tree. It may create an unapproved fixture suggestion there, never in a live
Bridge root. A replay decision frame carries the stable `REPLAY_MODE` blocker.
Bounded `observe` and `advise` projections remain available for historical
inspection, but replay artifacts can never become ready for `propose`,
`advance`, or a sustainability claim.

## Strategy V2 And Sequential Phase Proof

`oni.strategy.v2` is deterministic and non-executing. It separates what one
fresh snapshot appears to show (`phase.detected_from_single_snapshot`) from the
session-bound certified phase (`phase.certified`). A single snapshot can cause
an immediate safety regression, but it can never promote the certified phase.
Estimated or unsupported telemetry may escalate risk or leave a gate unknown;
only observed trustworthy telemetry can satisfy a gate.

The order and exit gates are fixed:

| Certified phase | Required satisfied objectives | Samples | Minimum wall time | Minimum game time |
| --- | --- | ---: | ---: | ---: |
| `bootstrap` | oxygen security, food reserve, sanitation | 3 | 10 s | 0.02 cycle |
| `stabilize` | water resilience, power resilience, morale/stress, research progress | 12 | 55 s | 0.25 cycle |
| `expand` | thermal safety, renewable food/oxygen/water/power, N-1 fault tolerance | 24 | 115 s | 1.0 cycle |
| `space` | mission completion with return readiness and safe base reserve | 12 | 55 s | 0.25 cycle |
| `sustain` | explicit indefinite-survival proof after all earlier gates | 120 | 595 s | 10.0 cycles |

`PhaseEvidenceTracker` requires all three thresholds: sample count, wall-clock
duration, and in-game-cycle duration. It accepts only the current session/save/
world, strictly increasing all-event `seq` and timestamps, exactly consecutive
`snapshot_seq`, a stable declared sample interval, monotonic
`game_time_cycles`, trusted telemetry, and a satisfied current gate. A wall
clock burst cannot manufacture game maturity, while paused game snapshots
cannot manufacture in-game duration. A timestamp gap greater than 1.25 times
the declared sample interval invalidates the gap-free proof window.

Duplicate renders do not increment evidence. A missing snapshot, sequence or
timestamp regression, sample-interval change, save/world change, failed or
unknown gate, or new session resets the active window; safety regression is
immediate, and later-phase evidence cannot accumulate early. Every accepted
sample extends `evidence_window.input_hash` with the previous chain value,
canonical state hash, snapshot id, snapshot ordinal, and all-event sequence.
The rendered `oni.phase-evidence.v1` binds start/end event and snapshot
ordinals, timestamps, game times, sample count, gap-free state, critical
incident count, and the chained input hash. Strategy readiness becomes `ready`
only after the final `sustain` window is complete; until then recommendations
remain operator advice with `auto_execute=false` and
`requires_approval=true`.

## Canonical Decision Frame V2

`oni.decision-frame.v2` is the atomic canonical consumer payload. It binds a
copy of the complete source identity (`session_id`, `save_id_hash`, `world_id`,
`snapshot_id`, `seq`, `snapshot_seq`, `ts`, and canonical `state_hash`) to bounded projections
of health, advice, incidents, plan, strategy, and policy. Every child payload
must declare the same `source.session_id`, `source.snapshot_id`, and
`source.state_hash`; a missing declaration produces
`OUTPUT_IDENTITY_MISSING` and blocks all readiness, while a conflicting value
rejects the frame instead of publishing a mixed-state result.

The frame exposes independent readiness for `observe`, `advise`, `propose`,
`advance`, and `claim_sustainable`. Trusted observations and enabled
suggestions may permit `propose` while actions remain disabled; actual
`advance` additionally requires enabled actions and satisfied strategy gates.
`claim_sustainable` is an observational proof, so action policy does not grant
or revoke it: it requires a fresh trusted source, certified `sustain` phase,
high confidence, every closed-loop gate, and satisfied indefinite-survival
evidence. Strategy recommendations are projected only as non-executable
operator recommendations; command objects, action arguments, credentials,
paths, and unbounded prose are not copied.

V2 freezes the current bounded Strategy objective priority and adds an exact
causal lifecycle binding to every item in a valid projected Advice payload:
`{incident_id,risk,incident_opened_at}`. `incident_opened_at` must carry an
explicit UTC offset and cannot be later than the frame source timestamp. An
invalid Advice input may still produce a fail-closed frame whose projection is
invalid and whose blockers include `ADVICE_INVALID`; it cannot propose. A
pending suggestion must repeat the valid item's exact lifecycle value, so rewriting an incident
episode and recomputing its stable command id cannot produce a current
candidate. This is an exact-key schema change: V1 archives are neither
translated nor accepted by V2 consumers. A V1-bound suggested command cannot
be rebound; normal fresh synchronization rejects it and may derive a new
suggestion only from the current incident and V2 frame. Operator/planned
commands are never silently rewritten or re-hashed and remain proposal
blockers until explicit disposition or expiry. V1 archives remain audit
artifacts and cannot authorize V2 work.

Current Bridge health also carries `command_pipeline` with schema
`oni.command-pipeline-gate.v1`. When present, the Decision Frame strictly
projects the top-level proposal gate, the independent mutation-proposal and
mutation-confirmation gates, confirmation candidate ids, exact per-stage
counts, aggregate diagnostic counts, and the explicit pre-frame binding
handoff. Keys, booleans, non-negative integer counts, bounded sorted blocker/
candidate tokens, and the agreement between top-level and proposal readiness
are all validated. Every confirmation blocker must also block a new mutation
proposal; a confirmation-only blocker is invalid rather than an optimistic
proposal path. Malformed Bridge-present data is replaced by fail-closed
projection defaults and emits `COMMAND_PIPELINE_INVALID`; a valid gate whose
mutation proposal is not ready emits `COMMAND_PIPELINE_MUTATION_BLOCKED`.
Both codes block `propose` and `advance`, but do not contaminate source trust,
observation, advice, or an otherwise valid observational sustainability claim.
A valid proposal blocker also preserves the separately projected confirmation
readiness so an existing mutation can finish or recover. A malformed gate never
claims confirmation readiness. Health documents that completely omit the field
remain accepted only as a legacy compatibility path; once Bridge publishes the
field, malformed or optimistic content cannot fall back to that path.

`frame_id` is the SHA-256 of canonical frame content excluding the id itself;
the strict validator rejects schema drift or post-build mutation. Consumers
must read `decision-frame.json` once and validate it before making a decision.
Bridge atomically writes the compatibility generation first and publishes the
frame last as that generation's commit marker in both the root and session
directory.

The Decision Frame does not trust a Strategy `ready` flag in isolation. Its
bounded Strategy projection revalidates the phase-evidence and evidence-window
schemas; session, phase, and last-snapshot binding; zero critical incidents;
`gap_free=true`; sample count; contiguous `snapshot_seq` range; equality of the
window end and Strategy snapshot ordinal; SHA-256 input-hash shape; and exact
agreement between start/end wall/game durations and the readiness counters.
The counters must also meet their required thresholds. Any malformed or
internally inconsistent proof makes the Strategy projection invalid, adds
`STRATEGY_INVALID`, and prevents advancement or sustainability claims; the
canonical `frame_id` then detects mutation after construction. Because a frame
does not contain the complete raw snapshot history, it validates these bindings
but does not independently replay the input-hash chain.

The separately written `latest_state.json`, `health.json`, `advice.json`,
`incidents.json`, `plan.json`, and `strategy.json` remain compatibility and
diagnostic artifacts only. They must not be independently joined as a control
input, and absence or rejection of the canonical frame has no loose-file
fallback.

## Non-executing Control Packet V1

`oni.control-packet.v1` is an additive post-sync control-plane commit marker.
Its exact top-level keys are `schema`, `packet_id`, `mode`, `source`,
`pipeline`, and `next_step`; `mode` is always `non_executing`. `packet_id` is
the typed canonical SHA-256 of the complete packet payload excluding
`packet_id`. The packet never enqueues, approves, claims, restores, or executes
a command and does not grant authority beyond Command V3 policy and human
approval.

`source` copies the exact Decision Frame source identity plus `frame_id` and a
nested exact `policy={valid,policy_hash}` binding. `pipeline` binds the complete
post-sync gate by canonical `gate_hash`, declares `stage=post_sync`,
`proposal_frame_evaluated=true`, repeats the exact `proposal_frame_id`, carries
proposal/confirmation readiness, and contains exactly the six counts for
`pending`, `approving`, `approved`, `executing`, `confirming`, and `failed`.
The bound gate has exact top-level keys and an exact
`evaluation_context={phase,proposal_frame_id,state_hash,policy_hash}`. Packet
construction requires `phase=frame_bound`, equality with the validated V2
frame and current policy, agreement between top-level and nested proposal
readiness, and no more candidate summaries than pending commands. Its
post-sync nature is established by Bridge calling
`command_pipeline_readiness(...)` with the same admitted state, already-loaded
policy object, and exact committed frame, then binding that result in the
validated packet. A pre-frame or invalid-frame scan cannot build a packet.
The earlier raw Bridge scan also always carries this context: `pre_frame` uses
`proposal_frame_id=null` with the exact available state/policy hashes, while a
supplied but invalid frame reports `invalid_frame`. Health projects only that
deferred handoff; the final Control Packet accepts only `frame_bound`.

`next_step` is either `null` or exactly one strict variant:

- `observe_request` references one existing Decision Frame blocker, pipeline
  blocker, or `unknown`/`blocked` Strategy objective identifier.
- `operator_recommendation` references only an identifier already projected by
  the ordered Plan, Advice, or Strategy recommendation views. Plan priority is
  preserved. It always carries `requires_approval=true` and
  `auto_execute=false`.
- `action_candidate` summarizes exactly one already-materialized current
  suggested mutation with only `command_id`, `action`, canonical `intent_hash`,
  `risk`, `requires_approval=true`, and `auto_execute=false` (plus the `kind`
  discriminator). It contains no args, conditions, effects, path, body,
  evidence, reason, or prose.

An `action_candidate` requires one and only one queue-provided
`pending_candidates` summary whose full command was already matched to the
same current frame/state/policy. Candidate admission re-evaluates the exact
active deterministic rule and requires its complete action hint, risk, reason,
evidence, title, incident id, snapshot/state binding, source sequence/time, V2
incident lifecycle, and policy allowlists to match. `created_at` and
`expires_at` must be canonical offset-aware timestamps, creation cannot be in
the future, `expires_at` must still be later than the candidate scan clock, and
their interval must equal the current policy TTL bounded to 1-300 seconds. The
128-bit stable id is recomputed from the session, incident id/opened-at, action,
complete `{args,preconditions,expected_effects}` proposal, source frame id,
canonical state hash, and source policy hash. This makes one exact context
idempotent while preserving an old rejected attempt under a distinct id when a
fresh frame safely re-derives the same still-open incident. Both the command and
its source object must have their exact allowlisted keysets. Its only proposal
blocker must be its own
`mutation_pending_selection:<id>` latch; replay (including the explicit replay
sandbox), disabled actions or suggestions, untrusted/stale/missing state,
duplicates, any second mutating pending record, stale pending records, invalid reads,
in-flight mutations, and unresolved outcomes never produce an action
candidate. Strategy action prose is never upgraded into this variant. Pipeline
ambiguity takes first observation priority, followed by source/state/trust/
freshness/replay blockers, invalid domain contracts, unknown/blocked Strategy
objectives, and generic gates. Every unknown or blocked objective precedes a
generic gate; larger integer priority orders objectives among themselves, then
phase order is `bootstrap`, `stabilize`, `expand`, `space`, `sustain`, and id is
the final tie-break, so oxygen cannot be displaced by alphabetical ordering.
`ACTIONS_DISABLED` is the sole recommendation exception: when suggestions
remain enabled and it is the only remaining selectable observation, the packet
may expose a strictly non-executing operator recommendation. Pipeline or other
frame blockers and every unknown/blocked objective still win. The
exception never exposes an action candidate or grants approval/execution
authority. A structured gate read failure publishes a bound packet with
`next_step=null`.

Bridge publication order is root Decision Frame commit, session Decision Frame
commit, immutable frame archive, outcome journal/recovery, suggestion sync,
exact post-sync scan, packet build and validation, session packet atomic write,
root packet atomic write, then archive pruning. The root packet is the final
global marker. If the post-sync scan itself raises or publication stops before
the packet writes, the older packet is retained and must fail validation
against the newly committed frame id. This ordering is not a multi-file
transaction and must not be presented as cross-file atomicity.

## Incidents And Advice

Rules produce lifecycle incidents, not one-shot notifications. Each incident
declares required fields and quality, confirmation samples/duration, cooldown,
and recovery behavior. Timeline events include `incident_opened`,
`incident_updated`, `incident_resolved`, and `incident_blocked_unknown`.

Only open incidents become `oni.advice.v2` items. A stable incident identity
allows at most one pending command. Recovery rejects its unapproved pending
command with `source_incident_not_open`.

## Command V3

State-changing commands use `oni.command.v3` and bind both the proposal and
every condition to the exact source state. Legacy `oni.command.v2` is accepted
only for `noop` and `focus_note`; the executor rejects every V2 mutation.

```json
{
  "schema": "oni.command.v3",
  "id": "cmd-...",
  "status": "pending_human_approval",
  "action": "set_paused",
  "args": {"paused": true},
  "session_id": "session-...",
  "save_id_hash": "sha256:...",
  "world_id": 0,
  "source_incident_id": "low_oxygen",
  "source_snapshot_id": "session-...:14",
  "state_hash": "sha256:...",
  "source_frame_id": "sha256:...",
  "created_at": "2026-07-11T01:00:00+08:00",
  "expires_at": "2026-07-11T01:00:30+08:00",
  "requires_approval": true,
  "preconditions": [
    {
      "path": "alerts",
      "op": "contains",
      "value": "low_oxygen",
      "quality": ["observed"],
      "quality_contract_id": "oni.qc.alerts.canonical.v1"
    },
    {
      "path": "game.paused",
      "op": "eq",
      "value": false,
      "quality": ["observed"],
      "quality_contract_id": "oni.qc.game.paused.v1"
    }
  ],
  "expected_effects": [
    {
      "path": "game.paused",
      "op": "eq",
      "value": true,
      "quality": ["observed"],
      "quality_contract_id": "oni.qc.game.paused.v1"
    }
  ]
}
```

`quality_contract_id` is an identifier, not self-issued authority. Python
Rules, plans, and the C# executor each resolve it through a packaged trusted
path/pattern registry. The registry fixes allowed status, semantic, unit,
scope, completeness, source, capture binding, and wire type/range. Unknown
paths, missing ids, duplicate quality allowances, operator/type mismatches,
out-of-range operands, an estimated condition on a mutation, a diagnostic-only
field on any mutation, metadata tampering, or a stale/unparseable capture all
fail closed. The command cannot redefine those fields. The executor takes a
fresh direct read, binds `snapshot.ts`, and repeats the same contract check
before both execution and verification.

Approval and execution are separate crash-recoverable transactions. A mutating
approval must bind the exact trusted, propose-ready `decision-frame.json`, the
complete immutable command intent, the exact source session/save/world/snapshot
and state hash, and the complete policy hash in an
`oni.approval-receipt.v1`. The receipt and C# verifier use the versioned
`oni.typed-canonical-sha256.v1` tree contract rather than JSON renderer text.
Approval publishes through `pending -> approving -> approved`; the executor
never scans `approving`, so a crash cannot expose both pending and approved
copies. A surviving approving claim is stale-before-dispatch and may be
quarantined without engaging the mutation latch.

Mutating execution order is fixed: validate schema/receipt/policy and fresh
direct preconditions, durably write the attempt and immutable intent into an
`executing` claim, atomically rename `approved -> executing`, assign and record
the `accepted` acknowledgement sequence, and only then call a typed ONI API.
The Probe writer is asynchronous, so an assigned acknowledgement sequence is
explicitly recorded as queue-unconfirmed until it is observed in the archived
session JSONL. Any `executing` claim surviving process restart means the API may
have been called: the executor pauses, reports `outcome_unknown`, and holds the
mutation safety latch; it never replays the command.

After dispatch the executor emits `executed`, polls a fresh direct read for up
to five seconds, emits `local_effect_observed`, and moves a successful mutation
to `confirming` with `status=executed_pending_snapshot`. This is not success.
Bridge requires exactly one archived source snapshot, the exact approval
receipt and intent, strictly ordered archived ACKs (`accepted < executed <
local_effect_observed`), and a later trusted Probe snapshot whose all-event
`seq` is greater than the local-effect ACK and whose quality-bound fields
satisfy every expected effect. Evidence-continuity recovery runs before this
attestation is built; its blocker is itself confirmation-blocking even when a
partial archive happens to contain otherwise plausible records.

Only then does Bridge publish identical root/session `action-outcomes.json`
certificates and stage `action-outcomes-commit.json`. Before a confirmed
`set_duplicant_chore_priority` command may leave `confirming`, Bridge also
create-only publishes
`confirmed-command-evidence/<sha256(command-id)>/approval-bundle.json`. The
bundle contains the exact archived decision frame, final confirmed command and
intent hash, approval receipt, and the exact enabled policy. Its frame policy
projection, command source, receipt, and frozen policy must all name the same
canonical policy hash. A byte-independent canonical match makes recovery
idempotent; a missing/pruned frame, write failure, or existing different bundle
leaves the command in `confirming` and fails closed. This publication precedes
both the prepared command write and `confirming -> done`, so decision-frame
retention cannot erase its approval authority after the command becomes
terminal.

After that durable bundle is re-read and validated, Bridge embeds the immutable
attestation in the command and atomically renames `confirming -> done` with
`status=confirmed`. A prepared `status=confirmed` file left in `confirming` is
held for journal recovery and must not be treated as an unknown mutation merely
because the game process starts first. Post-dispatch read/verification
ambiguity is `outcome_unknown` and latches; a proven pre-dispatch rejection is
`behavior_fail` or `rejected`. `noop` and `focus_note` have no game-state effect,
complete directly in `done/status=verified`, and never enter snapshot
attestation or engage the mutation latch.

The Python queue layer exposes `command_pipeline_readiness(...)` as the unified
offline gate for Bridge and colony-plan callers. It deterministically scans
`pending`, `approving`, `approved`, `executing`, and `confirming`, plus
same-save `failed/result.classification=outcome_unknown` records. Duplicate
command IDs, conflicting immutable intents, malformed active records,
approval/execution claims, prepared confirmation commits, unconfirmed
mutations, and unresolved unknown outcomes are returned as structured blockers.
A valid `confirming/status=confirmed` preparation blocks a new mutation but
remains ready for confirmation-journal recovery. Non-mutating `noop` and
`focus_note` records remain visible diagnostics without engaging either
mutation gate.

Each Bridge generation publishes a deterministic projection of that scan under
`health.command_pipeline` before building the new decision frame. The projection
uses the current admitted state and policy and explicitly reports
`proposal_frame_evaluated=false`; frame binding is deferred because the new
frame does not exist yet. After the frame is durably published,
`sync_pending_commands(...)` reruns the same gate with the exact proposal frame
and retains authoritative frame-specific expiry and creation enforcement.
Bridge supplies the same already-loaded policy through the keyword-only
`policy_override` rather than performing a second policy read. The complete
frame-bound scan also returns bounded `pending_candidates` summaries for
Control Packet construction; these omit paths and command bodies, exclude
duplicate/stale/operator-owned/invalid-mode/in-flight/unknown records, and do
not change any queue transition or execution permission.

The frame-bound scan records its exact evaluation phase, proposal frame id,
canonical state hash, and canonical policy hash. A suggested mutation becomes
a Control Packet candidate only when it is the exact currently active
deterministic rule output and its state timestamp, V2 incident lifecycle, and
canonical creation/expiry interval satisfy the bindings above. This candidate
gate is deliberately stronger than the pending-slot latch: a malformed or
forged pending file still blocks another mutation but is never promoted into
`ControlPacket.next_step`.

Bridge-generated suggested commands bind `source.policy_hash` in addition to
the source snapshot/state hash and `source_frame_id`. Once a newer trusted
state/frame is committed, pending suggestions with any superseded binding are
atomically rejected before new advice is considered. Colony plans consult the
same gate immediately before emitting each mutating leaf.

The executor disables JSON.NET date coercion when loading command files so ISO
8601 offsets remain intact through validation and audit serialization.
Integer equality remains exact. Floating-point `eq` comparisons use a
`1e-6 * max(1, |actual|, |expected|)` tolerance because periodic JSON
serialization shortens ONI's single-precision values while executor direct
reads retain the in-memory value. Cell fingerprints, instance ids, counters,
booleans, and strings remain exact bindings.

Existing files in `approved` at session start are quarantined and never
replayed; surviving `executing` claims are latched unknown. Supported actions
are `noop`, `focus_note`, `set_paused`,
`set_research`, `set_duplicant_chore_priority`, `mark_dig_cells`, and
`cancel_dig_cells`, plus `place_building_blueprint`,
`cancel_building_blueprint`, `place_utility_blueprint_path`, and
`cancel_utility_blueprint_path`; local policy may allow a smaller set.

`set_research` replaces the current research queue with the approved target.
ONI's `SetActiveResearch` API only enqueues its argument when the queue is
empty, so preserving a non-empty queue would acknowledge a no-op instead of
providing the command's exact set-active semantics.
An observed empty `research.current` opens the `no_active_research` advisory
incident but does not synthesize a target or a `set_research` command. A target
must come from an explicit, separately validated operator/plan decision with
the necessary queue and resource evidence; missing feasibility evidence is not
permission to guess `FarmingTech` or any other technology.

The batch-A action uses the exact argument set below:

```json
{
  "duplicant_instance_id": 123,
  "expected_name": "Ada",
  "chore_group": "Dig",
  "from_priority": 2,
  "priority": 4
}
```

Its preconditions must include observed `game.paused=true` and observed
`duplicants.by_instance_id.id_123.chore_priorities.Dig=2`. Its expected effect
must require the same dynamic path to equal `4`. Only current-world live
duplicants, exact instance/name matches, user-prioritizable ChoreGroups, and
values from 0 through 5 are accepted. Rollback is another explicitly approved
command with the confirmed direct before/after values swapped, followed by a
separate trusted-snapshot confirmation of the rollback.

`verify_runtime.py prepare-invalid-priority-target` creates a deliberately
wrong `expected_name` while preserving the live instance and observed current
priority. It must be explicitly approved, rejected as `behavior_fail`, produce
no execution acknowledgement, and leave the observed priority unchanged.

The batch-B mark action accepts `{"cells":[123,124]}` with one through 32
unique current-world cells. Every cell must bind an observed fingerprint,
element, mass, damage, solid state, absence of an existing designation, and
`safe_mark_candidate=true`. The command additionally binds the live DigTool
filters to Tiles on and NaturalBackwall off. Execution uses
`DigTool.PlaceDig`; direct simulation damage or instant digging is forbidden.

The batch-B cancel action accepts exact pairs such as
`{"targets":[{"cell":123,"designation_instance_id":456}]}`. It only cancels
tile-only designations whose stable instance id still matches and whose chore
has not started. Cancellation uses the native cancel event and its
postcondition is polled through the asynchronous `CancellableDig` animation.
Both actions require observed `game.paused=true`; mark must verify designation
creation without changing element, mass, damage, or solidity, and cancel must
verify exact removal while restoring the cell as a safe mark candidate.

Batch-C blueprint placement additionally requires observed `sandbox_mode=false`
and `instant_build_mode=false`. The initial allowlist is exactly `Ladder` for
single-building commands and `Wire` for ordered four-neighbor utility paths.
Orientation is fixed to `Neutral`, facade to `DEFAULT_FACADE`, and material tags
must exactly match the current observed catalog selection. Every target cell
must be visible, empty, non-liquid, in the active world, and valid through both
`IsValidPlaceLocation` and `IsValidBuildLocation`.

Placement uses only `BuildingDef.TryPlace`; completed-building creation is
forbidden. Returned objects enter the command rollback set before component
checks. An unambiguous partial placement failure triggers native cancellation
for every still-unfinished object created by that command. A postcondition
timeout has a different, fail-closed result: the outcome is recorded as
`outcome_unknown`, the mutation safety latch is set, the session is paused, and
no automatic blueprint cancellation is claimed because construction may already
have started. Reconciliation must establish the actual state before any
recovery action is considered.
Successful placement proves an exact positive `KPrefabID.InstanceID`, def,
orientation, material set, and unchanged element/mass/damage. Cancellation is
accepted only for the exact cell/instance pair while `Constructable` has no
worker and zero progress, then must restore the observed empty-cell fingerprint.
Utility cancellation additionally binds the session-owned path id, ordered
cells, and full instance-id list.

## Colony Plan V1

Batch-D controls are Bridge operations, not game actions. They never enter
`ALLOWED_ACTIONS`, the game action policy, or `ActionExecutor`. A definition
uses `oni.colony-plan-definition.v1`, contains 1 through 32 DAG-ordered steps,
and may reference only catalogued batch A through C game leaves. Every step
must carry concrete args, preconditions, expected effects, and optional cycle
bounds.

The durable plan uses `oni.colony-plan.v1` and the fixed state machine
`draft -> active <-> paused -> completed|aborted|failed`. Each mutation writes
an immutable numbered revision with `previous_revision_hash`, then atomically
updates `current.json`, `active.json`, and `colony-plan.json`. Recovery validates
the complete hash chain; a corrupt tail is moved under `corrupt/` before a new
revision is committed.

`advance_colony_plan` creates at most one dependency-ready leaf command. The
command remains `pending_human_approval`, binds the current session, save,
world, snapshot, and state hash, and always has `requires_approval=true`.
An executor direct read or a certificate published while the command remains
in `confirming` cannot advance a checkpoint. Only one command in `done/` with
`status=confirmed`, an embedded certificate identical to the published
action-outcome entry, matching direct before/after hashes, and a later trusted
witness advances the checkpoint. Failed leaves fail the plan; rejected or
quarantined leaves pause it; missing or duplicate locations, certificate
mismatch, or a forged legacy `done/result` are ambiguous and also pause it.

`pause_colony_plan` never rewrites pending or approved commands. Resume checks
freshness, session/save/world identity, and the latest snapshot-confirmed checkpoint.
Abort quarantines unapproved pending leaves but refuses while an approved leaf
may still execute. Recover restores control-plane state only and never creates,
approves, retries, or compensates a game command.

```powershell
py -3 .\scripts\manage_colony_plan.py create --definition .\plan-definition.json
py -3 .\scripts\manage_colony_plan.py advance
py -3 .\scripts\manage_colony_plan.py pause
py -3 .\scripts\manage_colony_plan.py resume
py -3 .\scripts\manage_colony_plan.py recover
py -3 .\scripts\manage_colony_plan.py abort
```

## Action Policy And Approval

No policy or an invalid policy means actions are disabled. A valid
`oni.action-policy.v1` object has exactly the required keys `schema`,
`actions_enabled`, `suggestions_enabled`, `allowed_actions`,
`allowed_save_hashes`, `command_ttl_seconds`, and `max_pending_commands`, plus
only the optional offset-aware `generated_at`. Boolean fields must be actual
booleans; the nonempty action list must be unique and drawn from the closed
implemented action set; save hashes must be unique lowercase SHA-256 envelopes
and an enabled policy requires at least one; TTL is an exact integer from 1
through 300 seconds and pending capacity is an exact integer from 1 through
100. Unknown/missing keys, booleans masquerading as integers, duplicates, or
out-of-range values invalidate the complete policy and remove its canonical
authorization hash.

`write-action-policy.ps1` writes the policy atomically. Enabling actions without
an explicit save hash fails. `approve-command.ps1` validates V3, expiry, policy,
action, save whitelist, current canonical decision frame, and a bound approval
receipt before the single-copy `approving` transaction publishes
`status=approved`, `approved_at`, and `approved_by=local_human`. Legacy V2
approval is limited to `noop` and `focus_note`; V2 mutation is rejected.

`verify_runtime.py prepare` creates operator-only acceptance commands with
`mode=runtime_validation`, `source.kind=runtime_validation`, and a null
`source_incident_id` because they do not originate from the incident lifecycle.
They still require an enabled local policy, an exact save hash whitelist, a
fresh session/state binding, and explicit approval before the game can execute
them. Normal suggested commands remain bound to an open incident.

Mutating `mode=suggested` approval always requires the bound frame's
`readiness.propose` to be exactly `ready=true, blockers=[]`. Mutating
`runtime_validation` and `planned` commands use the same rule normally, with one
narrow operator-only exception: a trusted frame may have exactly
`ready=false, blockers=["SUGGESTIONS_DISABLED"]`. Any additional pipeline,
health, policy, replay, plan, identity, freshness, or other blocker rejects the
approval, as does every other mutating mode. The exception does not relax exact
action membership or save-hash membership in the enabled local action policy.
`noop` and `focus_note` retain their non-mutating approval compatibility and do
not require a mutation approval receipt.

A mutating command must be approved against the exact immutable frame named by
its `source_frame_id`; validating an arbitrary historical frame is forbidden.
If only the moving root `decision-frame.json` is available and Bridge publishes
a newer generation after the command enters `pending`, approval must fail on
the frame-id mismatch. Runtime-command preparation therefore cannot be declared
live-ready until it binds `source_frame_id` and `source.policy_hash` and the
approval transaction can load a durably persisted frame snapshot by that exact
digest, validate its canonical hash and source identity, and keep it through the
approval audit lifecycle. This rejection boundary is intentional: it prevents
the queue-induced next-frame rewrite from being "fixed" by accepting any old
frame.

`suggestions_enabled` defaults to true for normal Copilot operation. V2.7
validation policies set it to false so a fresh reload session cannot create an
unapproved incident suggestion while a save-lineage transition is being
recorded. Existing unapproved pending suggestions are retained as rejected with
`suggestions_disabled`; approved commands are never moved by this switch.

## One-Save Validation Lineage

Final runtime acceptance uses one newly named `.sav` and an
`oni.test-save-lineage.v1` manifest under
`ai-bridge/evidence/save-lineage/<test_run_id>/manifest.json`.

1. `reserve` requires the target path to be inside an ONI save root and absent.
2. After one source save is loaded, `manage_validation_checkpoint.py` creates
   the reserved path with `save_checkpoint --target-save-path`; it never
   overwrites an existing Save As target.
3. The same manager runs `reload_checkpoint` without desktop input.
4. `activate` binds the creation save event, on-disk hash, and first reload
   session.
5. After every runtime scenario, the game is explicitly saved and reloaded.
   `checkpoint` requires a successful non-autosave completion event, the exact
   path fingerprint, the next on-disk hash, and a new-session snapshot with the
   same hash. It also requires actions disabled with empty pending/approving/
   approved/executing/confirming queues and no `commands/**/*.tmp` publication
   remnants. A successful mutating checkpoint binds
   exactly one `commands/done/status=confirmed` file, its embedded attestation,
   the matching session `action-outcomes.json`, ordered archived `accepted <
   executed < local_effect_observed` ACKs, and the later trusted witness.
6. Every checkpoint includes the previous checkpoint hash. `verify` rejects a
   broken chain, missing artifact, changed command result, foreign save path,
   missing reload, or live tip that no longer matches the lineage.

Only the current checkpoint hash is placed in `action-policy.json`; historical
hashes are revoked before the next command is approved.

```powershell
py -3 .\scripts\manage_save_lineage.py reserve --run-id <id> --save-path <new.sav> --mod-version 0.7.0
py -3 .\scripts\manage_validation_checkpoint.py --operation save_checkpoint --test-run-id <id> --save-path <source.sav> --target-save-path <new.sav>
py -3 .\scripts\manage_validation_checkpoint.py --operation reload_checkpoint --test-run-id <id> --save-path <new.sav>
py -3 .\scripts\manage_save_lineage.py activate --manifest <manifest.json>
py -3 .\scripts\manage_save_lineage.py checkpoint --manifest <manifest.json> --step-id <step> --scenario <name> --outcome confirmed --expected-mutation persisted_change --command-id <id>
py -3 .\scripts\manage_save_lineage.py verify --manifest <manifest.json> --minimum-checkpoints 1
```

The G0 priority admission gate is stricter than a generic one-command
checkpoint. Use the following order without combining the two policy windows:

Before the first policy window, complete this pre-run admission sequence while
ONI and Bridge are stopped:

1. Capture the pre-boundary, create the exact named loader clone, and run
   `manage_live_validation_environment.py prepare --what-if`, then the same
   command with `--apply --confirm-live-run-id <run-id>`. This freezes the
   original configs, Probe, and Bridge before any runtime install or config
   mutation.
2. Run `prepare-v27-runtime.ps1 -BuildRelease` as a workspace-only phase and
   freeze its exact four-artifact receipt. With one caller-frozen 32-hex nonce,
   run Install `-WhatIf`, require zero writes and a typed-exact match between
   `release_artifacts` and the build receipt, then freeze the returned stage SHA
   and transaction id. Publish that authorization with `-Stage`; use
   `-RecoverStage` only for an uncertain Stage attempt and only with those same
   external values. Run Install `-Apply` as a separate action checkpoint and
   require the fixed 38-key `runtime-bootstrap/result.json` to reach
   `installed_ready` after shared-lock release. Immediately freeze the exact
   result file's raw SHA-256 outside the document. The install does not enable
   the mod. For this workflow `<ONI-user-root>` is not arbitrary: it must resolve
   to `$env:USERPROFILE\Documents\Klei\OxygenNotIncluded`, with the live Bridge
   named exactly `ai-bridge` and the archive root beneath that canonical root.
3. Run the config gate as WhatIf, passing the frozen runtime result path/raw
   SHA. Apply it with exact run confirmation, freeze its returned config-evidence
   and startup-binding SHA-256 values outside both documents, and run the
   independent verifier with all external anchors.
   Only a passed verifier permits launch. The fixed order is: launch ONI via
   Steam, capture and independently verify the exact live ONI process identity,
   then start Bridge with the complete nine-anchor startup set. Bridge must not
   start before the validation PID has been frozen.

```powershell
py -3 .\scripts\configure_live_validation_runtime.py `
  --run-id <run-id> --oni-root <ONI-user-root> `
  --evidence-root <environment-evidence-root> `
  --boundary-report-root <boundary-report-root> `
  --environment-manifest <environment-manifest.json> `
  --expected-environment-manifest-sha256 <external-manifest-sha256> `
  --runtime-bootstrap-result <runtime-bootstrap\result.json> `
  --expected-runtime-bootstrap-result-sha256 <external-result-raw-sha256> `
  --pre-boundary <pre-boundary.json> `
  --clone-manifest <test-clone-manifest.json> `
  --source-save <immutable-source.sav> `
  --loader-save <named-loader-clone.sav> --loader-stem <named-loader-stem> `
  --game-managed-dir <game\OxygenNotIncluded_Data\Managed> `
  --release-dll <workspace\mod\OniAiProbeMod.dll> `
  --expected-release-dll-sha256 <external-release-sha256> --what-if

# Review, then repeat the exact command with:
# --apply --confirm-live-run-id <run-id>

py -3 .\scripts\verify_live_validation_runtime_config.py verify `
  --run-id <run-id> --oni-root <ONI-user-root> `
  --evidence-root <environment-evidence-root> `
  --boundary-report-root <boundary-report-root> `
  --environment-manifest <environment-manifest.json> `
  --expected-environment-manifest-sha256 <external-manifest-sha256> `
  --runtime-bootstrap-result <runtime-bootstrap\result.json> `
  --expected-runtime-bootstrap-result-sha256 <external-result-raw-sha256> `
  --pre-boundary <pre-boundary.json> `
  --clone-manifest <test-clone-manifest.json> `
  --source-save <immutable-source.sav> `
  --loader-save <named-loader-clone.sav> --loader-stem <named-loader-stem> `
  --game-managed-dir <game\OxygenNotIncluded_Data\Managed> `
  --release-dll <workspace\mod\OniAiProbeMod.dll> `
  --expected-release-dll-sha256 <external-release-sha256> `
  --expected-runtime-config-evidence-sha256 <external-config-evidence-sha256> `
  --expected-startup-binding-sha256 <external-startup-binding-sha256>
```

Do not discard either JSON result. Capture the Apply object as `$ConfigApply`
and the independent verifier object as `$ConfigVerify`, then freeze and compare
all seven non-PID Bridge anchors exactly:

```powershell
$ExpectedRuntimeConfigEvidenceHash = [string]$ConfigApply.runtime_config_evidence_sha256
$ExpectedStartupBindingHash = [string]$ConfigApply.startup_binding_sha256
$ExpectedStartupIntentHash = [string]$ConfigApply.startup_intent_hash
$ExpectedLoaderPathIdUpper = [string]$ConfigApply.requested_loader_path_id_upper
$ExpectedLoaderSaveHash = [string]$ConfigApply.requested_loader_save_sha256
$ExpectedGameBuild = [string]$ConfigApply.expected_game_build
$ExpectedModVersion = [string]$ConfigApply.expected_mod_version

if ($ConfigApply.status -cne 'configured' -or $ConfigVerify.status -cne 'passed' -or
    $ConfigVerify.runtime_config_evidence_sha256 -cne $ExpectedRuntimeConfigEvidenceHash -or
    $ConfigVerify.startup_binding_sha256 -cne $ExpectedStartupBindingHash -or
    $ConfigVerify.startup_intent_hash -cne $ExpectedStartupIntentHash -or
    $ConfigVerify.requested_loader_path_id_upper -cne $ExpectedLoaderPathIdUpper -or
    $ConfigVerify.requested_loader_save_sha256 -cne $ExpectedLoaderSaveHash -or
    $ConfigVerify.expected_game_build -cne $ExpectedGameBuild -or
    $ConfigVerify.expected_mod_version -cne $ExpectedModVersion) {
  throw 'runtime config Apply/verify anchor mismatch'
}
```

The config gate atomically points the one `SaveFilenameKey/` only at the named
loader clone, enables only `ass.OniAiProbe`, and preserves every other mod entry
while disabling it. It freezes the exact initial disabled/unbound policy and
0.7.0 bootstrap raw JSON, binds workspace/bootstrap/installed DLL hashes to the
external Release anchor, and delegates restoration to the environment
finalizer. Runtime-config schema v5 also freezes the complete loader policy and
binds it to the exact Managed-directory ancestry, identities, and hashes of
`Assembly-CSharp.dll` and `Assembly-CSharp-firstpass.dll`. Continue/original-save
loading is therefore disabled for the run.

Runtime-config schema v5 publishes a second create-only external anchor at
`ai-bridge/runtime-startup-binding.json`. Its raw SHA-256 must be recorded from
the configure result independently of the config-evidence SHA-256. The embedded
startup intent/binding v2 and the sibling binding cross-link the run, nonce,
environment, exact loader path/hash/length, expected game build, mod version,
Probe DLL, one-shot semantics, and the frozen 38-key installed-ready result's
path id, raw SHA-256, semantic hash, transaction id, and Probe-tree content hash.
The exact sibling fields are `runtime_bootstrap_result_path_id_lower`,
`runtime_bootstrap_result_sha256`, `runtime_bootstrap_result_hash`,
`runtime_bootstrap_transaction_id`, and
`installed_probe_tree_content_hash`.
In addition to the environment-manifest anchor, verify, abort, and finalize
require separately frozen raw hashes for the runtime-bootstrap result,
runtime-config evidence, and startup binding.

The exact schemas are `oni.live-runtime-bootstrap-result.v1` (38 keys),
`oni.live-validation-runtime-config.v5`,
`oni.live-validation-runtime-config-plan.v5`,
`oni.live-validation-runtime-config-result.v5`, and
`oni.live-validation-runtime-config-verification-result.v5`. The embedded
`oni.live-validation-startup-intent.v2` is exactly 18 keys and the sibling
`oni.runtime-startup-binding.v2` is exactly 19 keys, including each document's
self-hash.

Before launch, independent config verification requires the restore journal to
remain the exact single prepared-origin record. During abort/finalize only, the
environment manager may pass an internal open-descriptor custody proof so the
config core can verify the current canonical append-only chain against that
frozen origin. The proof is neither a command-line option nor serialized
evidence, and an unproved same-name `.finalize.lock` remains a hard refusal.

The live process/Bridge/startup-certificate sequence is also fixed. Resolve and
hash the game executable from the trusted Steam install root before launch.
Launch ONI through Steam, require exactly one live process, then create and
independently verify its identity while that PID remains alive:

```powershell
$RuntimeBridgeDir = Join-Path $OniUserRoot 'ai-bridge'
$RuntimeConfigEvidenceJson = Join-Path $EnvironmentEvidenceRoot `
  "$ValidationRunId\environment\runtime-config-evidence.json"
$GameExe = Join-Path $GameInstallRoot 'OxygenNotIncluded.exe'
$ValidationProcessReport = Join-Path $EnvironmentEvidenceRoot `
  "$ValidationRunId\validation-process-identity.json"
$ExpectedGameExePathIdUpper = (& py -3 -B -c `
  "from scripts.create_startup_consumption_evidence import process_image_path_id_upper; import sys; print(process_image_path_id_upper(sys.argv[1]))" `
  $GameExe).Trim()
if ($LASTEXITCODE -ne 0) { throw 'game exe path-id anchor failed' }
$ExpectedGameExeSha256 = 'sha256:' + `
  (Get-FileHash -LiteralPath $GameExe -Algorithm SHA256).Hash.ToLowerInvariant()

# Launch ONI through Steam here; do not start Bridge yet.
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

Only after that verification passes, start Bridge in a foreground controllable
PTY and retain its session id. Supply all nine anchors from the configure Apply
result and verified PID; never supply a partial set, `-Once`, `-EnableLlm`, or
`Start-Process`:

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

Wait for the trusted origin snapshot, reserve the absent derivative path, run
the one Save As/reload sequence, and activate the lineage manifest. While that
same verified ONI PID and Bridge PTY remain alive, create and replay-verify the
`oni.startup-consumption-evidence.v3` certificate from a separate shell. The
source archive must already contain the unique origin-to-derivative transition;
the bundle target must not exist. `Player.log` is the LocalLow path, not a file
under the ONI user root:

```powershell
$RuntimeBridgeDir = Join-Path $OniUserRoot 'ai-bridge'
$RuntimeConfigEvidenceJson = Join-Path $EnvironmentEvidenceRoot `
  "$ValidationRunId\environment\runtime-config-evidence.json"
$StartupBindingJson = Join-Path $RuntimeBridgeDir 'runtime-startup-binding.json'
$ConsumedPrefsYaml = Join-Path $OniUserRoot 'kplayerprefs.yaml'
$PlayerLog = Join-Path $env:USERPROFILE `
  'AppData\LocalLow\Klei\Oxygen Not Included\Player.log'
$LineageManifestJson = Join-Path $RuntimeBridgeDir `
  "evidence\save-lineage\$ValidationRunId\manifest.json"
$StartupConsumptionBundle = Join-Path (Split-Path -Parent $LineageManifestJson) `
  'startup-consumption-evidence'

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

Freeze the configure result's seven non-PID Bridge anchors and the two process
report anchors outside their source documents. Do not recompute an expected
value after suspected drift. At shutdown, send Ctrl+C only to the retained PTY
session and only after the complete terminal output generation is visible.

`AutoResumeSaveFile` is exact one-shot state, but it avoids only the second
user-selected load-screen activation. The first Main Menu initialization can
still run `InitLoadScreen`, migration, save-directory probing, mod-report
generation, initial load-screen activation, and colony-list refresh work before
the one-shot value is consumed.

If that first launch or loader selection fails before any session, snapshot,
command, priority-pair, bundle, freeze, outcome, save-write, control, or ACK
evidence exists, stop ONI and Bridge. The prepared/configured run may then use
the narrow abort transaction, first as WhatIf and then with the exact run
confirmation:

```powershell
py -3 .\scripts\manage_live_validation_environment.py abort `
  --run-id <run-id> --oni-root <ONI-user-root> `
  --evidence-root <environment-evidence-root> `
  --boundary-report-root <boundary-report-root> `
  --pre-boundary <pre-boundary.json> `
  --clone-manifest <test-clone-manifest.json> `
  --source-save <immutable-source.sav> `
  --loader-save <named-loader-clone.sav> --loader-stem <named-loader-stem> `
  --manifest <environment-manifest.json> `
  --expected-environment-manifest-sha256 <external-manifest-sha256> `
  --runtime-bootstrap-result <runtime-bootstrap\result.json> `
  --expected-runtime-bootstrap-result-sha256 <external-result-raw-sha256> `
  --runtime-config-evidence <environment-dir\runtime-config-evidence.json> `
  --expected-runtime-config-evidence-sha256 <external-config-evidence-sha256> `
  --expected-startup-binding-sha256 <external-startup-binding-sha256> `
  --release-dll <workspace\mod\OniAiProbeMod.dll> `
  --expected-release-dll-sha256 <external-release-sha256> `
  --game-managed-dir <game\OxygenNotIncluded_Data\Managed> --what-if

# Review, confirm ONI/Bridge remain stopped, then repeat the exact command with:
# --apply --confirm-live-run-id <run-id>
```

Abort requires the action policy to remain byte-for-byte equal to the frozen
initial disabled policy, validation control to remain disabled and unbound, and
all command/control/evidence stages to remain empty. Only process, waiting, and
loader-failure diagnostics are admitted, and they are archived. It restores
the original loader pair, configs, Probe, Bridge, and archive tree under the
same ownership-safe lock/rollback and create-only non-replay rules as finalize.
Any session or action evidence permanently disables abort for that run. Use the
complete shutdown/finalize workflow when its evidence exists; otherwise stop
for manual recovery rather than using abort to bypass those gates.

After those three pre-run gates pass, use this runtime sequence without
combining the two policy windows:

1. Enable only the forward policy window, approve one priority change, and wait
   for its unique `done/status=confirmed` command. The pre-done approval bundle
   above is automatic.
2. Immediately freeze its session certificate plus exact ACK/witness records,
   disable policy, explicitly save/reload, and create the
   `priority_forward` checkpoint with that freeze as evidence.
3. Only then enable the inverse window. Approve the exact same target with the
   first command's `to -> from`, wait for confirmation, freeze it, disable
   policy, and explicitly save/reload into the final lineage session. Do not
   create `priority_inverse` or the pair report yet.
4. Exit ONI normally. Wait for Bridge to archive the adjacent
   `session_ended(reason=application_quit)` and `probe_shutdown` tail and finish
   the corresponding terminal outputs, then stop Bridge cleanly. Only after
   that terminal publication is frozen, create the offline `priority_inverse`
   checkpoint from the already completed save/reload evidence.
5. Within the pair-creation freshness window, publish the create-only offline
   report. It accepts exactly these two unique
   checkpoints and bundles, in order, and requires the final lineage-tip
   priority to equal the forward command's original value. It also reruns
   `command_pipeline_readiness(...)` with the exact current `latest_state.json`
   lineage tip and disabled `action-policy.json`. The pipeline schema and
   mutation-proposal gate must be valid/ready, and relevant commands,
   `ambiguous_outcomes`, `duplicate_ids`, `stale_pending`, invalid records, and
   in-flight mutations must all be empty. A same-save
   `commands/failed/*/outcome_unknown` therefore blocks finalization even though
    every nonterminal directory is empty.
6. Publish the separate create-only `shutdown-evidence.json`. Its mandatory
   external startup anchor is the
   FINAL-STABLE `oni.startup-consumption-evidence.v3` bundle at the exact
   run-scoped `<manifest-dir>/startup-consumption-evidence` directory, together
   with the externally recorded startup `evidence_hash` and validation PID;
   process absence alone is not an exit proof.

```powershell
py -3 .\scripts\finalize_priority_pair_lineage.py freeze-command `
  --manifest <manifest.json> --bridge-dir <ai-bridge> `
  --step-id priority_forward --command-id <forward-command-id>
# Disable policy, then explicit save/reload.
py -3 .\scripts\manage_save_lineage.py checkpoint `
  --manifest <manifest.json> --step-id priority_forward `
  --scenario priority-forward-persisted --outcome confirmed `
  --expected-mutation persisted_change --command-id <forward-command-id> `
  --evidence <manifest-dir>\priority-command-freezes\priority_forward.json

py -3 .\scripts\finalize_priority_pair_lineage.py freeze-command `
  --manifest <manifest.json> --bridge-dir <ai-bridge> `
  --step-id priority_inverse --command-id <inverse-command-id>
# Disable policy, explicitly save/reload, exit ONI normally, wait for the
# adjacent terminal archive and complete terminal outputs, then stop Bridge
# cleanly before continuing with these offline operations.
py -3 .\scripts\manage_save_lineage.py checkpoint `
  --manifest <manifest.json> --step-id priority_inverse `
  --scenario priority-inverse-persisted --outcome confirmed `
  --expected-mutation persisted_change --command-id <inverse-command-id> `
  --evidence <manifest-dir>\priority-command-freezes\priority_inverse.json

py -3 .\scripts\finalize_priority_pair_lineage.py finalize `
  --manifest <manifest.json> --bridge-dir <ai-bridge> `
  --report <manifest-dir>\priority-pair-finalization.json

# The normal-exit terminal tail and stopped Bridge are already prerequisites;
# now publish the immutable shutdown proof.
py -3 .\scripts\create_shutdown_evidence.py create `
  --manifest <manifest.json> --bridge-dir <ai-bridge> `
  --priority-pair-report <manifest-dir>\priority-pair-finalization.json `
  --priority-pair-report-hash <externally-recorded-report-hash> `
  --run-id <run-id> `
  --forward-step priority_forward --inverse-step priority_inverse `
  --command-id <forward-command-id> --command-id <inverse-command-id> `
  --command-bundle-hash <forward-bundle-hash> `
  --command-bundle-hash <inverse-bundle-hash> `
  --startup-consumption-bundle `
    <manifest-dir>\startup-consumption-evidence `
  --startup-consumption-evidence-hash `
    <external-startup-consumption-evidence-hash> `
  --validation-process-id <external-validation-process-id> `
  --event-log <ai-bridge>\sessions\<final-session-id>\state.jsonl `
  --output <manifest-dir>\shutdown-evidence.json

# Independent replay uses the same manifest/pair/command/startup/PID arguments.
# Replace --event-log/--output with these two externally anchored values:
py -3 .\scripts\create_shutdown_evidence.py verify `
  <same-manifest-pair-command-startup-and-PID-arguments> `
  --shutdown-evidence <manifest-dir>\shutdown-evidence.json `
  --shutdown-evidence-hash <external-shutdown-evidence-hash>

py -3 .\scripts\manage_live_validation_environment.py finalize `
  --run-id <run-id> --oni-root <ONI-user-root> `
  --evidence-root <environment-evidence-root> `
  --boundary-report-root <boundary-report-root> `
  --pre-boundary <pre-boundary.json> `
  --clone-manifest <test-clone-manifest.json> `
  --source-save <immutable-source.sav> `
  --loader-save <named-loader-clone.sav> --loader-stem <named-loader-stem> `
  --manifest <environment-manifest.json> --lineage-manifest <manifest.json> `
  --priority-pair-report <manifest-dir>\priority-pair-finalization.json `
  --priority-pair-report-hash <external-pair-report-hash> `
  --shutdown-evidence <manifest-dir>\shutdown-evidence.json `
  --shutdown-evidence-hash <external-shutdown-evidence-hash> `
  --startup-consumption-bundle `
    <manifest-dir>\startup-consumption-evidence `
  --startup-consumption-evidence-hash `
    <external-startup-consumption-evidence-hash> `
  --validation-process-id <external-validation-process-id> `
  --runtime-config-evidence `
    <environment-dir>\runtime-config-evidence.json `
  --expected-runtime-config-evidence-sha256 `
    <external-config-evidence-sha256> `
  --expected-startup-binding-sha256 <external-startup-binding-sha256> `
  --runtime-bootstrap-result <runtime-bootstrap\result.json> `
  --expected-runtime-bootstrap-result-sha256 <external-result-raw-sha256> `
  --release-dll <workspace\mod\OniAiProbeMod.dll> `
  --expected-release-dll-sha256 <external-release-sha256> `
  --game-managed-dir <game\OxygenNotIncluded_Data\Managed> `
  --forward-step-id priority_forward --inverse-step-id priority_inverse `
  --command-id <forward-command-id> --command-id <inverse-command-id> `
  --command-bundle-hash <forward-bundle-hash> `
  --command-bundle-hash <inverse-bundle-hash> --what-if

# Review the zero-write plan, then repeat the exact command with:
# --apply --confirm-live-run-id <run-id>
```

`freeze-command` and `finalize` are offline filesystem operations; they neither
launch nor control ONI. The finalizer does not depend on the rolling decision
frame archive after the automatic bundle exists. If the original archive is
still present, its bytes and document must match the frozen copy; a conflicting
archive is rejected. The report's `shutdown.command_pipeline` freezes the
pipeline schema, exact gate hash, final state/policy input hashes, readiness
summary, stage counts, and zero-valued ambiguity/duplicate/stale counters; the
outer `report_hash` binds that summary create-only.

`create_shutdown_evidence.py` is a separate normal-exit gate. It independently
revalidates the active lineage manifest, pair-report file and canonical hash,
the final lineage tip, both ordered command IDs, automatic approval bundles,
and per-step freezes. Before accepting a shutdown record it invokes the startup
bundle's create-only offline replay verifier. The bundle directory must be
exactly `<manifest-dir>/startup-consumption-evidence`, contain only the five
artifacts defined by the v3 startup-consumption contract plus `evidence.json`,
and match the externally supplied typed
canonical `evidence_hash`. Replay must pass; its `validation_run_id` must equal
the lineage run and its `session_id` must equal the lineage origin session. The
final shutdown session is validated separately from the terminal archive. Its PID
must equal the separate external `--validation-process-id` anchor. Pair-report
command, bundle, source-frame, and
confirmation-certificate paths are resolved as strict Bridge-relative paths;
the reported lineage-freeze path remains the exact absolute manifest evidence
path. Absolute values in relative fields, `..` escapes, wrong roots, and
forward/inverse path swaps are refused. A source frame or current confirmation
certificate may have been evicted only when the production pair report says it
is absent and the path is actually absent. In that case the immutable bundle or
freeze copy remains authoritative; if the current file exists its bytes must
match. The frozen confirmation is reconstructed with the same production
command assessor, including command id, accepted outcome, attestation, witness,
ACK sequences, and source records. The selected shutdown source must be either the exact
`sessions/<final-session-id>/state.jsonl` archive or the current root
`state.jsonl`; a root log may contain an earlier session, but the final lineage
session must be its terminal segment. Every source line must be complete,
strict JSON. The target session must begin with the unique
`oni.event.v2 session_started/seq=1` whose reason is
`save_loader_load_complete`, have unique gap-free positive producer sequences
and nondecreasing timestamps, and contain the unique `oni.state.v2` final-tip
snapshot with the exact session/save/world/state hash. Every target-session
record, not only the endpoints, must bind the same save hash, world, game build,
mod version, and `save_dirty=false`. Every record must also equal the replayed
certificate's `validation_run_id`, startup-binding hash, startup-intent hash,
runtime-config evidence SHA-256, externally anchored validation PID,
`startup_binding_status=verified`, game build, and mod version. Every final
session record must retain the certified requested loader path while its active
path equals the final lineage path. The final `session_started` record must be
sequence 1 with `save_loader_load_complete` and the final lineage-tip save hash.
Separately, the lineage origin archive must begin with the exact raw-byte and
canonical-record prefix frozen in the startup certificate. The mod version must also equal both the manifest's
expected value and `latest_state.json`. Event envelopes use
`oni.event.v2`, while state snapshots use `oni.state.v2`. The log must end with
adjacent unique records in this order:

1. `session_ended`, `reason=application_quit`, `save_dirty=false`;
2. `probe_shutdown`, `save_dirty=false`.

Both terminal envelopes must have `schema=oni.event.v2` and identical final
session, save hash, world, game build, and mod version. Missing, repeated,
out-of-order, wrong-session/save/world, dirty, crash/forced-kill/probe-error,
partial, malformed, or post-shutdown records fail closed and publish nothing.
The v3 document explicitly projects the startup certificate `evidence_hash`,
raw `evidence.json` file SHA-256, path ID, replay result, run/session, binding,
process, loader, initial-save, and archive-prefix anchors. It never embeds raw
startup prefs, Player log text, or loader paths. Its outer typed canonical
`evidence_hash` is rebuilt from all of those fields. The document also freezes
the authoritative shutdown source path, byte hash, record-chain hash,
line/sequence positions, terminal record hashes, and all lineage/pair/
bundle/freeze bindings. Publication is
create-only at `<manifest-dir>/shutdown-evidence.json`; an existing file is
never overwritten. All trusted inputs are read through path-bound regular-file
descriptors, require one link and no reparse point, and are read twice through
the same handle so same-name/same-size/same-mtime in-place drift is detected.
Publication verifies the owned descriptor, link count, path identity, length,
payload hash, and canonical self-hash. On a late hardlink, name swap, or cleanup
fault it fails closed and never deletes an external replacement; an ambiguous
owned publication is retained as outcome-unknown evidence. The complete startup
bundle (directory/file identities, exact entries, bytes, and semantics) and the
shutdown log are both rechecked across the validation window; drift, hardlinks,
reparse points, relative escapes, same-content name swaps, and TOCTOU changes
fail closed before publication. Independent verification also pins and rereads
the published shutdown file across full reconstruction. Create-only publication
pins the complete ancestor chain and parent-directory identity before opening
the leaf, so a before-open junction or parent swap cannot redirect publication.

The environment finalizer requires the externally recorded canonical hash via
`--shutdown-evidence` and `--shutdown-evidence-hash`. It does not trust the path
or self-hash alone: it rehashes the evidence and every referenced file,
rebuilds the expected document from the still-present log and trust anchors,
and requires exact canonical equality before restoration begins. It repeats
that reconstruction across runtime-Bridge snapshot capture and immediately
before the restore transaction, and cross-binds the shutdown source log plus
each command, bundle, optional current frame/certificate, and freeze into the
captured tree. Therefore an
attacker cannot make a changed world/session/source acceptable merely by
recomputing the document hash, and ONI/Bridge process counts of zero never
substitute for the terminal producer sequence.

The same call must receive the exact run-scoped startup-consumption bundle,
its separately frozen v3 `evidence_hash`, and the separately verified ONI PID
through `--startup-consumption-bundle`,
`--startup-consumption-evidence-hash`, and `--validation-process-id`.
Finalization replays and cross-binds those external anchors; values rediscovered
from a suspect pair or shutdown document are not substitutes.

The same finalizer also requires the exact
`<evidence-root>/<run-id>/environment/runtime-config-evidence.json`, its
externally recorded whole-file SHA-256, the separately anchored create-only
startup binding (at its live path or its single transaction-owned archived
custody path), the exact runtime-bootstrap result path and externally recorded
raw SHA-256, the exact workspace Release DLL, and the externally recorded
Release SHA-256. A sibling
`runtime-config-recovery-*.json` is an unconditional stop. WhatIf performs the
same historical read-only reconstruction with zero writes; Apply repeats it
inside the shared `.finalize.lock`, requires config `configured_at` to be no
later than the trusted shutdown `termination.session_started.ts`, and freezes
the exact bytes create-only at `finalization/pair/config.json` before any
destructive restore. The compact runtime binding preserves evidence raw and
document hashes, transaction id, source/loader identity, embedded bootstrap
and initial disabled-policy raw/document hashes, 0.7.0 Probe identity, the
external Release hash, both desired config hashes, the complete v5-bound loader
policy, and the exact Managed-directory/assembly binding. Current assemblies
are revalidated before restoration. Post-run prefs, mods, policy, and loader
drift is expected and archived independently; it is never used as a substitute
for the launch-time evidence. Finalize result and non-replay recovery must
carry the same binding, so an old or partial recovery cannot bypass the external
anchors.

The restore journal, finalize lock, staging files, immutable evidence copies,
and final result are ownership-bound. Trusted files require stable single-link
descriptor reads. Create-only publications and cleanup first atomically reclaim
the transaction's exact inode and never overwrite or unlink a late external
replacement. Restore moves/copies likewise publish without overwrite and retain
a captured identity plus file/tree snapshot for reverse-order rollback. Compact
unique rollback claims keep the mandatory 64-hex Bridge paths below legacy
Win32 limits. A mismatched replacement, injected tree child, journal write/fsync
fault, or ambiguous cleanup is preserved with recovery-required evidence. Once
the physical restore is committed, a result-publication fault becomes a
non-replay recovery state: a retry may compare and publish the same candidate
result, but may not execute the restore transaction again.

`validation-control-policy.json` defaults to disabled. The manager enables one
operation for one current save hash and one current path id for at most 120
seconds, writes the approved validation control, waits for a terminal record,
and disables the policy in `finally`. The mod also requires a paused game, a
clean loaded save, an exact source session/snapshot, the normal action policy
disabled with no save hashes, and empty action pending/approving/approved/
executing/confirming queues.

The validation controller's terminal `status=verified` is control-plane input,
not game-action success. The manager additionally requires the exact disk hash
and a fresh state snapshot captured after the terminal record (and a new
session for reload), and reports `classification=control_plane_verified` with
`game_action_confirmation=false`. It never upgrades that result into a
`commands/done` mutation confirmation.

Historical mutating `commands/done/status=verified` files remain readable only
as `legacy_executor_verified_read_only`. Offline lineage audit may return
`status=legacy_read_only`; live runtime/lineage acceptance and marker creation
remain fail-closed. `noop` and `focus_note` retain direct
`done/status=verified` compatibility because they do not mutate game state.

`save_checkpoint` calls `SaveLoader.Instance.Save` on the game thread and
requires the existing save lifecycle patch to report a newer write generation,
a readable rewritten file, and matching requested/result/active path ids.
`reload_checkpoint` persists `reload-intent.json`, follows the same high-level
flow as the load screen (`LoadScreen.ForceStopGame`, set active save path,
`SaveLoader.LoadScene`), and lets the new scene's `SaveLoader.OnSpawn` perform
deserialization. Only a new session with the same disk hash, path id, and a
readable probe state completes it. Calling the low-level instance `Load` method
inside an active Game scene is forbidden because it bypasses scene teardown.
This control plane is validation-only and is not part of the colony action
catalog or LLM planner.

## Optional LLM Ranker

LLM is off by default and is not required for Observer or rule Copilot. It sees
only compact trusted state, up to 30 compact events, and current open incidents.
It may return only:

```json
{
  "schema": "oni.llm-ranking.v1",
  "ranked_incident_ids": ["low_oxygen", "low_food"]
}
```

The local planner rebuilds every plan item from trusted rule advice. Unknown
incident IDs, extra fields, actions, arguments, invalid JSON, missing key, 403,
timeout, or request failure retain a valid `rule_only` plan. The request target
is at most 8,000 estimated input tokens, timeout is 30 seconds, and unchanged
incidents or calls within 60 seconds do not call the model. Health output never
contains an API key, authorization header, raw response body, save name, or
local path.

## Verification

```powershell
py -3 .\scripts\verify_advanced_actions.py
py -3 .\scripts\verify_colony_plan.py
py -3 .\scripts\verify_automation.py
py -3 .\scripts\verify_bridge_reliability.py
py -3 .\scripts\verify_command_safety.py
py -3 .\scripts\verify_command_pipeline_gate.py
py -3 .\scripts\verify_control_packet.py
py -3 .\scripts\verify_rule_quality_contracts.py
py -3 .\scripts\verify_strategy.py
py -3 .\scripts\verify_decision_frame.py
py -3 .\tools\game_verify\verify_telemetry_contract.py `
  --scenario .\tools\game_verify\scenarios\oni_telemetry_contract_mock.json
py -3 .\scripts\verify_live_telemetry_contract.py
py -3 .\scripts\verify_save_lineage.py
py -3 .\scripts\verify_shutdown_evidence.py
py -3 .\scripts\verify_live_validation_environment.py
py -3 .\scripts\verify_v27_workflow.py
py -3 .\scripts\verify_validation_checkpoint.py
py -3 .\scripts\verify_llm_fallback.py
.\scripts\verify-runtime.ps1 -Phase final -SaveLineage <manifest.json>
```

When a current Bridge generation is available, validate the unmodified Probe
snapshot and bind it to the health generation explicitly:

```powershell
py -3 .\tools\game_verify\verify_telemetry_contract.py `
  --snapshot "$env:USERPROFILE\Documents\Klei\OxygenNotIncluded\ai-bridge\latest_state.json" `
  --health "$env:USERPROFILE\Documents\Klei\OxygenNotIncluded\ai-bridge\health.json" `
  --output .\reports\live-telemetry-contract.json `
  --json
```

`--now <RFC3339-with-offset>` can freeze the validation clock for reproducible
offline evidence; otherwise the validator uses the current timezone-aware local
time. Live mode reads the snapshot directly and never applies scenario
mutations, fixture upgrades, or missing-field defaults. `accepted` means the
snapshot contract passed; its `blockers` are snapshot-only.
`command_eligible` additionally requires a supplied `oni.bridge.health.v1` with `status=ok`,
`trust_ready=true`, empty blocker lists, and exact session/snapshot/state-hash
identity. Omitting `--health` therefore leaves snapshot `blockers` empty when
the snapshot passes but adds `health_not_provided` to `command_blockers` and
keeps `command_eligible=false`. The report records session, snapshot, save,
world, sequence, canonical state hash, snapshot time, validation time, age, and
freshness threshold. Exit codes are `0` for an accepted snapshot when no health
was requested, or for a fully command-eligible snapshot/health pair; `1` means
a well-formed rejected snapshot or supplied health binding, and `2` means a
validator/input error.

Runtime success additionally requires `game_verify` marker scenarios plus the
retained known failure scenario. Mock results prove protocol contracts. Live
snapshot acceptance proves only that the supplied Probe/Bridge generation
meets this telemetry and freshness contract; only a current ONI session with
JSONL, command result, state delta, Player.log, and performance evidence proves
game behavior.

The additive state-v2 fields, quality metadata checks, rolling evidence
thresholds, and Decision Frame evidence-window tamper cases currently have
static source, deterministic fixture, Mock, and offline regression coverage
only. No current Probe JSONL captured from ONI has yet demonstrated these new
fields or their timing/coverage semantics, and no in-game result may be inferred
from an `observed` example in this document. Probe load and live game proof stay
pending until an explicit notification opens the game-validation window.
