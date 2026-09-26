# Power telemetry v2 migration

## Outcome

`oni.power-telemetry-contract.v2` is an additive overlay on the existing
`oni.state.v2` snapshot. It makes a finite stored-energy observation with an
unbounded capacity representable without emitting `Infinity`, inventing a
capacity, or manufacturing a battery percentage.

The immutable `oni.telemetry-contract.v1` remains unchanged. A bounded snapshot
with the additive `power.storage_mode` field still satisfies v1. An unbounded
snapshot intentionally fails v1 because v1 continues to require finite numeric
`capacity_joules` and `battery_percent` values.

## Wire and quality matrix

| Mode | `stored_joules` | `capacity_joules` | `battery_percent` | Percent-dependent actions |
| --- | --- | --- | --- | --- |
| `bounded`, capacity > 0 | finite observed number | finite observed number | finite observed consistent ratio | eligible only after the rest of admission succeeds |
| `bounded`, capacity = 0 | finite observed `0` | finite observed `0` | `null`, `unsupported` quality | blocked |
| `unbounded` | finite observed number | `null`, blocked quality | `null`, blocked quality | blocked |

`quality.power.storage_mode` is required and observed with semantic
`storage_capacity_mode`, unit `enum`, and the same active-world registered
battery scope as the gauges. For a recognized unbounded mode, capacity and
percent quality may be `unsupported/complete`; an `error/unknown` transition
state is also telemetry-valid but never action-eligible. `NaN`, positive or
negative `Infinity`, a finite stand-in capacity, or a finite stand-in percent
are never valid wire representations for unbounded storage.

## Admission audit

- Bridge admission is transport/integrity admission. `agent.state_quality`
  checks the `oni.state.v2` envelope, identity, freshness, writer state, and
  presence of the quality map. It does not turn every observed field into an
  actionable field. The additive mode therefore does not require relaxing
  Bridge admission.
- Strategy field admission requires the complete bounded v2 tuple: observed
  mode/stored/capacity/percent quality with the exact producer metadata,
  positive finite capacity, stored energy no greater than capacity, and a
  `0..100` percent consistent with `stored / capacity` within the v2 tolerance.
  Missing, unbounded, contradictory, or forged tuples are read as unavailable;
  `power_resilience` remains `unknown` and produces no percent-based power
  recommendation.
- The v2 overlay exposes `percent_dependent_actions_eligible` separately from
  telemetry acceptance. Honest unbounded telemetry can be accepted while the
  dependent action capability remains false.

## Producer behavior

The Mod producer now emits `power.storage_mode` and its quality entry without
changing the user's normal Mod configuration. It classifies capacity as:

1. `bounded` when every raw member and the aggregate are finite;
2. `unbounded` when at least one raw member is positive infinity and no invalid
   member is present (finite members may coexist, but their partial total is not
   published); or
3. `invalid` when any raw member is negative, `NaN`, or negative infinity, or
   when finite accumulation overflows. Invalid takes precedence over
   unbounded.

Stored energy is accumulated independently and is published only when every
raw stored value is non-negative and finite and its aggregate remains finite.
In unbounded mode the producer
publishes a finite stored aggregate, keeps capacity and percent `null`, and
marks both unavailable gauges `unsupported/complete`. Invalid capacity keeps
capacity and percent `null` with `error/unknown`; a bounded zero capacity keeps
percent `null` with `unsupported/complete`. It never skips an anomalous member,
publishes a partial capacity, clamps a percentage, or synthesizes a finite
stand-in.

Producer reflection tests cover bounded, zero-capacity, positive-infinite,
mixed finite/unbounded, invalid non-finite, finite-overflow, and independently
invalid stored-value cases.

Consumers must opt into the v2 overlay explicitly. Existing v1 callers keep
their current fail-closed rejection behavior.
