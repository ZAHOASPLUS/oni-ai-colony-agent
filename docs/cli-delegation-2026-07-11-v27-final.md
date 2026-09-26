# V2.7 Final CLI Delegation Record

Date: 2026-07-11

## Target

The final phase is split into seven work packages. Three independent read-only
audits are delegated to local Codex CLI workers, so accepted CLI coverage will
be `3/7 = 42.86%`:

1. Audit the `0.6.8` high-level save/reload checkpoint controller.
2. Audit evidence coverage for all 14 ordered single-save checkpoints.
3. Audit `0.6.8` shortage-blueprint safety and candidate-cell readiness.

The remaining four packages stay on the main thread: runtime smoke and lineage
creation, checkpoints 1-7, checkpoints 8-14 plus performance, and final
integration/audit/reporting.

Authoritative task inputs:

- `fleet/v27-final/tasks.json`
- `fleet/v27-final/prompts/audit-checkpoint-controller.md`
- `fleet/v27-final/prompts/audit-runtime-evidence.md`
- `fleet/v27-final/prompts/audit-blueprint-readiness.md`

## Current Result

Status: **completed and reviewed**. `completed_by_cli=3/3`; final-stage coverage
is `3/7 = 42.86%`.

- The project-specific Fleet Doctor and the low-cost Dali ProviderCheck passed.
- Run `oni-v27-final-cli-audit-20260711` completed all three read-only tasks.
- Every task used the cheap pool, changed zero files, produced an empty diff,
  and was individually reviewed and approved by the main thread.
- The expensive fallback was not started.
- The protected credential helper remained the only credential access path;
  no token value was read, printed, copied, or stored in task evidence.

## Accepted Findings

| Task | Main finding | Disposition |
| --- | --- | --- |
| checkpoint controller | High-level ONI reload ordering needed runtime evidence; corrupt reload intent is not terminally quarantined | Final save/reload smoke passed; corrupt-intent handling remains a low-severity next-iteration item |
| runtime evidence | Order, artifact presence, per-checkpoint session binding, disabled-policy hash revocation, save-hash rotation, and marker freshness were not all mandatory | Verifiers and completion audit were strengthened; the existing 14-step lineage replayed successfully |
| shortage blueprints | Runtime acceptance did not explicitly require zero inventory | Added observed `materials_available=false` and zero-mass assertions for Ladder and Wire, plus stocked-fixture rejection |

All three workers exceeded their configured effective input-token limits:
`36,337/30,000`, `95,413/35,000`, and `94,616/30,000`. This is recorded as a
scheduler budget variance, not hidden as a pass. The reports remained useful
after direct source and stderr review, so the tasks were accepted without retry
or expensive escalation.

The hash-bound review package is under `docs/cli-v27-20260711/`; its
`delegation.json` is the machine-readable completion source used by the final
auditor.
