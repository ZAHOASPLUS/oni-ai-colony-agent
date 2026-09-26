raise SystemExit("ARCHIVED one-off script from the 2026-09-13 live runs. It deletes or overwrites live Bridge/Probe/config files with relative paths and no verification. Do not run it; see archive/tmp-scripts-20260913/README.md.")
from pathlib import Path
import shutil
oni=Path(r'C:/Users/user/Documents/Klei/OxygenNotIncluded');b=oni/'ai-bridge';state=b/'state.jsonl';
# Preserve current run evidence and start a fresh live Bridge log.
shutil.copy2(state,Path(r'reports/controller-plan-sequence-20260913/user-live-developed-20260913-10/state-before-restart.jsonl'));state.write_bytes(b'')
profile=b/'live-profile-process.json';shutil.move(profile,Path(r'reports/controller-plan-sequence-20260913/user-live-developed-20260913-10/live-profile-stopped-before-restart.json'))
p=oni/'kplayerprefs.yaml';t=p.read_text(encoding='utf-8');loader=r'C:\Users\user\Documents\Klei\OxygenNotIncluded\cloud_save_files\76561190000000000\娴雅的太空监牢\user-developed-loader-20260913-10.sav';
if 'AutoResumeSaveFile:' in t: raise SystemExit('AutoResume already present')
needle='  SaveFilenameKey/: '+loader+'\n';
if needle not in t: raise SystemExit('loader key not found')
t=t.replace(needle,needle+'  AutoResumeSaveFile: '+loader+'\n',1);p.write_text(t,encoding='utf-8');print('fresh state and auto-resume prepared')
