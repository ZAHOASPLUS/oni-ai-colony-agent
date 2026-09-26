from pathlib import Path
import shutil
oni=Path(r'C:/Users/user/Documents/Klei/OxygenNotIncluded');src=Path(r'reports/controller-plan-sequence-20260913/user-live-closed-loop-20260913-01/environment/backup/old-bridge');dst=oni/'ai-bridge'
for name in ['action-policy.json','live-profile-process.json','runtime-startup-binding.json','v27-runtime-bootstrap.json','validation-control-policy.json']:
 shutil.copy2(src/name,dst/name)
print('bridge-policy-files-restored')
