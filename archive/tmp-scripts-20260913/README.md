# Archived one-off scripts (2026-09-13 live runs)

These files were ad-hoc helpers written during the 2026-09-13/14 live runs and
left in the workspace root. They are kept only as history. Each file now starts
with a line that refuses to run.

Why they must not be run (code review 2026-09-26, finding S3):

- They use paths relative to the current directory; `.tmp_restore_probe.py`
  deletes the installed Probe before copying and leaves nothing behind when the
  source path does not resolve.
- `.tmp_final_cleanup.py`, `.tmp_restore_*` and `.tmp_retry_bridge*.py`
  `rmtree` the live `ai-bridge`/Probe directories and overwrite
  `kplayerprefs.yaml`/`mods.json` from old backups without checking hashes or
  the current state. Re-running them destroys the only "before" snapshot.
- `.tmp_final_cleanup.py` records `processes: {oni: 0, bridge: 0}` without
  checking any process.
- `.tmp_restart_prep10.py` truncates `state.jsonl` and inserts
  `AutoResumeSaveFile` into the user's `kplayerprefs.yaml`.
- `.tmp_stop*.ps1`/`.tmp_close_oni.ps1` do not check process creation time
  (PID reuse); `.tmp_time.ps1` still has the old 28,800 s timezone parsing bug.

Use the maintained tools under `scripts/` instead.
