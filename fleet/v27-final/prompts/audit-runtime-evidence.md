# V2.7 Runtime Evidence Audit

Perform a read-only audit of the final single-save V2.7 verification workflow.

Scope:

- `scripts/verify_runtime.py`
- `scripts/manage_save_lineage.py`
- `scripts/prepare-v27-runtime.ps1`
- `scripts/set-v27-validation-policy.ps1`
- `scripts/verify_v27_completion.py`
- `scenarios/game_verify_runtime_*.json`
- `docs/protocol.md`
- `docs/dev-notes.md`
- `../oni-ai-colony-agent-local-plan.md` when available from the repository root

Validate that the tooling can prove all 14 ordered checkpoints on one newly created `.sav`, with a real explicit save and a new loaded session after every checkpoint. Check command terminal evidence, save hash rotation, path identity, checkpoint hash chaining, runtime marker freshness, performance-session binding, policy shutdown, and rejection of old evidence.

Do not modify files. Do not access token files, live save files, or the game UI.

Return:

1. A 14-row coverage table: checkpoint, producing command/tool, required evidence, and any gap.
2. Findings ordered by severity with exact file and line references.
3. A direct verdict: `workflow_complete`, `needs_fix`, or `insufficient_evidence`.
4. The minimum main-thread commands needed to freeze and verify each checkpoint.
