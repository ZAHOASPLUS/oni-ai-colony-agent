from pathlib import Path
import shutil,hashlib,json,datetime,os
oni=Path(r'C:/Users/user/Documents/Klei/OxygenNotIncluded'); run=Path(r'reports/controller-plan-sequence-20260913/user-live-developed-20260913-06'); env=run/'environment'; out=run/'cleanup-recovery'; out.mkdir(exist_ok=True)
bridge=oni/'ai-bridge'; probe=oni/'mods/Dev/oni-ai-colony-agent'; cfg=[oni/'kplayerprefs.yaml',oni/'mods/mods.json']; backups=[env/'backup/old-bridge',env/'backup/probe',env/'backup/config/kplayerprefs.yaml',env/'backup/config/mods/mods.json']
if any((Path(x).exists() for x in [out/'post.json'])): raise SystemExit('cleanup already completed')
if any(Path(oni/x).exists() for x in ['ai-bridge/bridge.pid']): raise SystemExit('bridge marker exists')
# require no live processes
import subprocess
ps=subprocess.check_output(['powershell','-NoProfile','-Command','@(Get-Process -Name OxygenNotIncluded -ErrorAction SilentlyContinue).Count']).decode().strip()
if ps!='0': raise SystemExit('ONI still running')
pre={'action_id':'run06-cleanup-20260913-01','created':datetime.datetime.now(datetime.timezone.utc).isoformat(),'sources':[str(x) for x in backups]}
(out/'pre.json').write_text(json.dumps(pre,indent=2),encoding='utf-8')
shutil.copytree(bridge,out/'current-bridge'); shutil.copytree(probe,out/'current-probe'); shutil.copy2(cfg[0],out/'current-kplayerprefs.yaml'); shutil.copy2(cfg[1],out/'current-mods.json')
shutil.rmtree(bridge); shutil.copytree(env/'backup/old-bridge',bridge)
shutil.rmtree(probe); shutil.copytree(env/'backup/probe',probe)
shutil.copy2(env/'backup/config/kplayerprefs.yaml',cfg[0]); shutil.copy2(env/'backup/config/mods/mods.json',cfg[1])
def tree(p):
 h=hashlib.sha256(); n=0
 for f in sorted([x for x in p.rglob('*') if x.is_file()] if p.is_dir() else [p],key=lambda x:str(x).lower()): h.update(str(f.relative_to(p)).replace('\\','/').encode());h.update(f.read_bytes());n+=1
 return {'files':n,'sha256':'sha256:'+h.hexdigest()}
post={'action_id':'run06-cleanup-20260913-01','completed':datetime.datetime.now(datetime.timezone.utc).isoformat(),'bridge':tree(bridge),'probe':tree(probe),'config':{'kplayerprefs.yaml':hashlib.sha256(cfg[0].read_bytes()).hexdigest(),'mods.json':hashlib.sha256(cfg[1].read_bytes()).hexdigest()},'old_transaction_touched':False}
(out/'post.json').write_text(json.dumps(post,indent=2),encoding='utf-8'); print(json.dumps(post,indent=2))
