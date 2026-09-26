raise SystemExit("ARCHIVED one-off script from the 2026-09-13 live runs. It deletes or overwrites live Bridge/Probe/config files with relative paths and no verification. Do not run it; see archive/tmp-scripts-20260913/README.md.")
from pathlib import Path
import shutil
oni=Path(r'C:/Users/user/Documents/Klei/OxygenNotIncluded');src=Path(r'reports/controller-plan-sequence-20260913/user-live-closed-loop-20260913-01/environment/backup/old-bridge');dst=oni/'ai-bridge'
for name in ['action-policy.json','live-profile-process.json','runtime-startup-binding.json','v27-runtime-bootstrap.json','validation-control-policy.json']:
 shutil.copy2(src/name,dst/name)
print('bridge-policy-files-restored')
