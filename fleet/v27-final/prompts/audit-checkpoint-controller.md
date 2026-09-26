# V2.7 Checkpoint Controller Audit

Perform a read-only audit of the ONI AI Colony Agent `0.6.8` validation checkpoint path.

Scope:

- `mod/Source/Probe/ValidationCheckpointController.cs`
- `mod/Source/Probe/ProbeSession.cs`
- `mod/Source/Patches/LifecyclePatches.cs`
- `scripts/manage_validation_checkpoint.py`
- `scripts/write-validation-control-policy.ps1`
- `scripts/verify_validation_checkpoint.py`

Review the exact save and reload lifecycle, especially:

- fail-closed policy, session, save hash, and path hash binding;
- action queue quiescence before save or reload;
- `SaveLoader.Instance.Save` completion and disk verification;
- `LoadScreen.ForceStopGame()` plus `SaveLoader.LoadScene()` ordering;
- durable reload intent recovery after the new game session starts;
- timeout, duplicate control, stale control, and crash behavior;
- whether any path can repeat the known `0.6.5` low-level reload failure.

Do not modify files. Do not access token files, save files, logs outside the repository, or the game UI.

Return:

1. Findings ordered by severity with exact file and line references.
2. A direct verdict: `ready_for_runtime_smoke`, `needs_fix`, or `insufficient_evidence`.
3. Missing tests or evidence needed before the first `0.6.8` high-level reload.
4. A short checklist the main thread can execute without mouse automation.
