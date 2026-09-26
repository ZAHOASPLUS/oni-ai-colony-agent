# V2.7 Batch C Runtime Readiness Audit

Perform a read-only audit of the `0.6.8` Batch C shortage-blueprint contract. A prior `0.6.7` live snapshot reported `materials_available=false`, zero available mass, and no candidate cells for both Ladder and Wire. `0.6.8` intentionally keeps those inventory fields as diagnostics while allowing `BuildingDef.TryPlace(...)` to create an unbuilt `Constructable` that waits for normal material delivery.

Scope:

- `mod/Source/Probe/ProbeStateReader.cs`
- `mod/Source/Commands/ActionExecutor.cs`
- `scripts/verify_runtime.py`
- `scenarios/game_verify_runtime_blueprint.json`
- `config/advanced-actions.v1.json`
- `docs/advanced-actions-v2.7.md`

Verify that removing current inventory as a placement precondition does not create or consume resources, does not call `BuildingDef.Build(...)`, and does not weaken legal material selection, paused/non-sandbox/non-instant-build checks, empty-cell and placement validation, atomic rollback, or exact-instance cancellation. Determine what exact runtime evidence must prove a zero-inventory Ladder and Wire path can be placed as waiting-delivery blueprints and then cancelled back to the original fingerprints.

Do not modify files. Do not access token files, live save files, or the game UI.

Return:

1. Findings ordered by severity with exact file and line references.
2. A decision tree for `runtime_ready`, `reader_fix_required`, `executor_fix_required`, or `keep_batch_c_blocked`.
3. Exact snapshot fields and command preconditions the main thread must inspect.
4. A direct verdict: `shortage_blueprint_contract_ready`, `needs_fix`, or `insufficient_evidence`.
