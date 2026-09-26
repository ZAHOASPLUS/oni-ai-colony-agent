from pathlib import Path
import shutil
oni=Path(r'C:/Users/user/Documents/Klei/OxygenNotIncluded');old=oni/'ai-bridge';tmp=oni/'ai-bridge-leftover-20260913-08';
try:
 if tmp.exists(): shutil.rmtree(tmp,ignore_errors=True)
 old.rename(tmp); shutil.copytree(Path(r'reports/controller-plan-sequence-20260913/user-live-closed-loop-20260913-01/environment/backup/old-bridge'),old); print('bridge-tree-restored')
except Exception as e: print(type(e).__name__,e)
