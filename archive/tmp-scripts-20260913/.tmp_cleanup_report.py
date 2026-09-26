raise SystemExit("ARCHIVED one-off script from the 2026-09-13 live runs. It deletes or overwrites live Bridge/Probe/config files with relative paths and no verification. Do not run it; see archive/tmp-scripts-20260913/README.md.")
from pathlib import Path
import json,datetime,hashlib
run=Path(r'reports/controller-plan-sequence-20260913/user-live-developed-20260913-07');oni=Path(r'C:/Users/user/Documents/Klei/OxygenNotIncluded');out=run/'cleanup-recovery';out.mkdir(exist_ok=True)
def sh(p):return 'sha256:'+hashlib.sha256(Path(p).read_bytes()).hexdigest()
post={'action_id':'run07-cleanup-20260913-01','completed':datetime.datetime.now(datetime.timezone.utc).isoformat(),'bridge_key_hashes':{p.name:sh(p) for p in (oni/'ai-bridge').iterdir() if p.is_file()},'probe_key_hashes':{p.name:sh(p) for p in (oni/'mods/Dev/oni-ai-colony-agent').iterdir() if p.is_file()},'config':{'kplayerprefs':sh(oni/'kplayerprefs.yaml'),'mods':sh(oni/'mods/mods.json')},'old_transaction_touched':False};(out/'post.json').write_text(json.dumps(post,indent=2));print(json.dumps(post,indent=2))
