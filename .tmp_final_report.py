from pathlib import Path
import shutil,hashlib,json,datetime,subprocess
oni=Path(r'C:/Users/user/Documents/Klei/OxygenNotIncluded');tmp=oni/'ai-bridge-leftover-20260913-08';
if tmp.exists(): shutil.rmtree(tmp,ignore_errors=True)
src=Path(r'reports/controller-plan-sequence-20260913/user-live-closed-loop-20260913-01/environment/backup');
def sh(p):return 'sha256:'+hashlib.sha256(Path(p).read_bytes()).hexdigest()
post={'action_id':'ONI-RESTORE-ORIGINAL-20260913-08','completed':datetime.datetime.now(datetime.timezone.utc).isoformat(),'bridge_files':{p.name:sh(p) for p in (oni/'ai-bridge').iterdir() if p.is_file()},'probe_files':{p.name:sh(p) for p in (oni/'mods/Dev/oni-ai-colony-agent').iterdir() if p.is_file()},'config':{'kplayerprefs.yaml':sh(oni/'kplayerprefs.yaml'),'mods.json':sh(oni/'mods/mods.json')},'expected_original':{'kplayerprefs.yaml':sh(src/'config/kplayerprefs.yaml'),'mods.json':sh(src/'config/mods/mods.json'),'probe_dll':sh(src/'probe/OniAiProbeMod.dll'),'bridge_state':sh(src/'old-bridge/state.jsonl')},'old_transaction_touched':False};out=Path(r'reports/controller-plan-sequence-20260913/user-live-developed-20260913-08/final-cleanup/post.json');out.write_text(json.dumps(post,indent=2));print(json.dumps(post,indent=2))
