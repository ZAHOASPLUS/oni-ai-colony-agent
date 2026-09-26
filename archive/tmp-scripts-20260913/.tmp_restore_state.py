raise SystemExit("ARCHIVED one-off script from the 2026-09-13 live runs. It deletes or overwrites live Bridge/Probe/config files with relative paths and no verification. Do not run it; see archive/tmp-scripts-20260913/README.md.")
from pathlib import Path
p=Path(r'C:/Users/user/Documents/Klei/OxygenNotIncluded/ai-bridge/state.jsonl'); b=Path(r'reports/controller-plan-sequence-20260913/user-live-closed-loop-20260913-01/environment/backup/old-bridge/state.jsonl').read_bytes()
try:
 p.write_bytes(b); print('state-overwritten',len(b))
except Exception as e: print(type(e).__name__,e)
