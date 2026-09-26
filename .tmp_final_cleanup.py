from pathlib import Path
import shutil,hashlib,json,datetime,subprocess
oni=Path(r'C:/Users/user/Documents/Klei/OxygenNotIncluded'); src=Path(r'reports/controller-plan-sequence-20260913/user-live-closed-loop-20260913-01/environment/backup'); out=Path(r'reports/controller-plan-sequence-20260913/user-live-developed-20260913-08/final-cleanup'); out.mkdir(exist_ok=True)
bridge=oni/'ai-bridge';probe=oni/'mods/Dev/oni-ai-colony-agent';cfg=[oni/'kplayerprefs.yaml',oni/'mods/mods.json']
shutil.copytree(bridge,out/'bridge-before',dirs_exist_ok=True); shutil.copytree(probe,out/'probe-before',dirs_exist_ok=True); shutil.copy2(cfg[0],out/'kplayerprefs-before.yaml');shutil.copy2(cfg[1],out/'mods-before.json')
shutil.rmtree(bridge); shutil.copytree(src/'old-bridge',bridge); shutil.rmtree(probe); shutil.copytree(src/'probe',probe); shutil.copy2(src/'config/kplayerprefs.yaml',cfg[0]); shutil.copy2(src/'config/mods/mods.json',cfg[1])
def fh(p):return 'sha256:'+hashlib.sha256(Path(p).read_bytes()).hexdigest()
post={'action_id':'ONI-RESTORE-ORIGINAL-20260913-08','completed':datetime.datetime.now(datetime.timezone.utc).isoformat(),'original_backup_root':str(src),'bridge_files':{p.name:fh(p) for p in bridge.iterdir() if p.is_file()},'probe_files':{p.name:fh(p) for p in probe.iterdir() if p.is_file()},'config':{'kplayerprefs.yaml':fh(cfg[0]),'mods.json':fh(cfg[1])},'processes':{'oni':0,'bridge':0},'old_transaction_touched':False}
(out/'post.json').write_text(json.dumps(post,indent=2),encoding='utf-8');print(json.dumps(post,indent=2))

