from pathlib import Path
p=Path(r'C:/Users/user/Documents/Klei/OxygenNotIncluded/ai-bridge/state.jsonl'); b=Path(r'reports/controller-plan-sequence-20260913/user-live-closed-loop-20260913-01/environment/backup/old-bridge/state.jsonl').read_bytes()
try:
 p.write_bytes(b); print('state-overwritten',len(b))
except Exception as e: print(type(e).__name__,e)
