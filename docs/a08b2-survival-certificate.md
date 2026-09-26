# A08B survival-arbitration certificate

This document records the offline semantic stage for the survival-arbitration
certificate. It is not live-game, save, Bridge, bootstrap, or long-term
survival acceptance.

## Contract

`agent.rules.build_survival_arbitration_certificate()` publishes the closed
schema `oni.survival-arbitration.v1` with exactly these fields:

```text
schema
policy_hash
status
blockers
game_paused
active_pause_alert_ids
eligible_incident_ids
selected_incident_id
command_hint_hash
```

The producer arbiter and `survival_certificate_blockers()` use the same pure
reducer for active pause IDs, retained incident history, witness identity,
sample hysteresis, duration, priority, selection, containment, and command
hint derivation. The validator compares the complete reduced result rather
than trusting a certificate's selected or eligible lists.

The fixed pause priority is:

```text
suffocation, incapacitated, starvation, bionic_offline,
hyperthermia, hypothermia, major_sickness, low_oxygen,
bionic_low_battery
```

Observe-only alerts remain informational and cannot acquire pause command
authority. An open pause witness outside the active set, a duplicate incident
ID, or an invalid active witness is a blocker. In particular, an invalid
higher-priority witness blocks the whole result; it cannot be hidden by
removing that ID from `eligible_incident_ids`. A missing higher-priority
hysteresis witness also blocks when a lower-priority witness would otherwise
be selectable. A lone not-yet-open hysteresis alert remains the explicit
`none / pending_hysteresis` observation state.

The producer-side state/trust/contract exception is deliberately narrow. It
may publish a `blocked` observation envelope for
`state_unavailable`, `state_not_trusted_live`,
`alerts_contract_unusable`, or `paused_contract_unusable`, but that envelope
must have no active or eligible IDs, selected incident, or command hint. It
does not grant action authority.

Witness checks require matching session and snapshot identity, current state
and snapshot sequences, positive sequence values, risk, open status, active
telemetry, explicit-offset timestamps, exact sample count continuity, and
current timestamp. Equivalent timezone representations and seven fractional
timestamp digits are accepted after instant-preserving parsing. The 7-sample /
30-second low-oxygen and 2-sample / 5-second battery boundaries are covered by
the offline verifier.

Suggested pause commands additionally bind the selected witness episode,
advice opening time, rule metadata, command hint, and source metadata. Missing
the survival certificate is therefore rejected for a suggested pause. Existing
operator and planned approval modes remain governed by their original policy
gate and are not disabled by this certificate path.

## Evidence

The independent verifier is
[`scripts/verify_survival_certificate.py`](../scripts/verify_survival_certificate.py).
The current run covers 8 cases and 1,282 assertions, including 36 unordered
pause-rank pairs and 180 fixed-oracle higher-witness variants (invalid,
missing, stale, non-open, and wrong-risk). Its output and the three requested
baseline outputs are under
[`reports/a08b2-survival-certificate/`](../reports/a08b2-survival-certificate/).

The verifier is intentionally offline-only. It uses in-memory fixtures,
`IncidentTracker`, and the existing pure frame/approval fixtures; it does not
start ONI, Steam, Klei, Bridge, a formal bootstrap wrapper, or a network/VPN
helper.

## Provenance limitation

Current `oni.decision-frame.v2` producers bind `snapshot_seq` explicitly in
the frame source. Older certificate archives remain readable for audit and
may infer that value only when all represented active witnesses agree; a
disagreement fails closed. This is a consistency check, not source
authentication. A coordinated edit that changes every witness in the same
internally consistent way cannot be proven false by this certificate alone.
Likewise, `command_hint_hash` and the frame digest are integrity checks, not
producer signatures, and the certificate does not prove that each Probe
sample originated from a real game observation. Individual sample provenance
is still open.
