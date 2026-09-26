# A08B3 mixed incident certificate boundary

Status at recovery cut: **implementation and targeted mixed verifier passed;
the ordered regression tail is pending continuation in the main task**. This is
offline evidence only. No ONI process, Klei save, normal Mod, live Bridge, or
bootstrap admission was used.

## Repair

`agent/rules.py` now has a closed immutable ordinary-rule catalog containing
exactly these seven IDs:

`idle_duplicants`, `low_food`, `missing_manual_generator`, `missing_outhouse`,
`missing_oxygen_diffuser`, `no_active_research`, `stressed_duplicants`.

Incident normalization accepts only `CANONICAL_ALERT_IDS` plus that catalog.
Ordinary records remain retained history and never enter the pause set. Unknown
IDs, wrong ID types, non-mapping items, duplicate IDs, and malformed containers
remain fail-closed. The nine pause IDs, fixed rank order, witness checks,
hysteresis, certificate schema, and shared reducer were not changed.

## Evidence completed

| Run | Result | stderr |
| --- | --- | --- |
| `python -B scripts/verify_survival_mixed_incidents.py` | exit 0; 5 cases; 160 assertions | 0 bytes |
| `python -B scripts/verify_survival_certificate.py` | exit 0; 8 cases; 1,282 assertions; 36 pairs / 180 variants | 0 bytes observed |
| `python -B scripts/verify_decision_frame.py` | exit 0; 17 cases | 0 bytes; stdout 4,069 bytes |

The independent verifier report is
`reports/a08b3-mixed-incidents/verify.json`. It records catalog=21, all six
ordinary tracker statuses (`inactive`, `resolved`, `pending`, `open`,
`blocked_unknown`, `cooldown`), each coexisting with a selected low-oxygen
witness, all nine pause alerts, 6-sample and 29.999-second low-oxygen
non-candidates, malformed-input blockers, missing/invalid higher-priority
witness blockers, valid DecisionFrame plus approval-input validation, and an
explicit `--bridge-dir`/`--input` subprocess replay (exit 0, frame valid, no
pending mutation).

The pre-change Bridge reproduction is in
`reports/a08b3-mixed-incidents/00-existing-bridge-repro-20260906.md`; it
records the 21-item frame blocked by `open_incident_alert_invalid` and the
`observe_request` control packet.

## Recovery packet

- Goal: complete A08B3 mixed incident boundary and ordered offline regression.
- Safety invariants: original saves and normal Mod configuration immutable; no
  live game launch; subprocess Bridge runs must pass explicit temporary
  `--bridge-dir` and `--input`; no edits to decision, approval receipt, Bridge,
  queue, custody, bootstrap, policy, or real Klei assets.
- Current rules SHA-256:
  `73DE05D7EE9C80B999B1C6BA4976A09E27B98955ED9D9193C9C988BA8FC19C00`.
- Current mixed verifier SHA-256:
  `4878B3947B1F5676D8DCE0D2FA4CC16DD5B9CD53E7C449B8EB205861E79E8902`.
- Unchanged baselines: `verify_survival_certificate.py` =
  `DF4009B0DA2C44EC8B5D14ADCF7A4B83ADFFCCA096E522960C0735CA9EB01370`;
  `verify_bridge_reliability.py` =
  `FC29C2A48B11D33B69E8EFE1F26F296C4520CBD0ED9F7F3AFF493A1E21395337`.
- Last completed action: `A08B3_RUN_MIXED_VERIFIER_20260906`, followed by the
  decision-frame regression.
- Next single action: run `python -B scripts/verify_approval_frame_binding.py`
  with `PYTHONDONTWRITEBYTECODE=1`, record exit/stderr and its report hash;
  then continue the already specified pipeline-gate and Bridge-reliability
  order in a fresh short context.
- Rollback: the implementation is one catalog-membership patch plus new
  verifier/docs/reports. Revert only the catalog patch and remove new artifacts
  if an independent tail regression exposes a contract issue; do not touch the
  preserved historical reproduction root.

## Not claimed

The ordered tail (`verify_approval_frame_binding.py`,
`verify_command_pipeline_gate.py`, `verify_bridge_reliability.py`) has not yet
been run in this context. No live-game, real-source provenance, bootstrap, or
long-term survival acceptance is established.
