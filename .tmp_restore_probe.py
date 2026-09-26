from pathlib import Path
import shutil
oni=Path(r'C:/Users/user/Documents/Klei/OxygenNotIncluded');src=Path(r'reports/controller-plan-sequence-20260913/user-live-closed-loop-20260913-01/environment/backup');probe=oni/'mods/Dev/oni-ai-colony-agent';
shutil.rmtree(probe);shutil.copytree(src/'probe',probe);shutil.copy2(src/'config/kplayerprefs.yaml',oni/'kplayerprefs.yaml');shutil.copy2(src/'config/mods/mods.json',oni/'mods/mods.json');print('probe-config-restored')
