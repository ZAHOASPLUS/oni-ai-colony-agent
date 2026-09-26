import pathlib,tempfile,json,time
from scripts import verify_bridge_reliability as v
from bridge import oni_bridge as b
root=pathlib.Path(tempfile.mkdtemp(prefix='inspect-')); src=root/'in.jsonl'; exp=v.startup_expectations(); st=v.startup_snapshot(exp,'session-performance',2,1,active_path_id=exp['loader_path_id_upper'],save_id_hash=exp['loader_save_sha256'])
with src.open('wb') as h:
 h.write(v.json_line(v.startup_event(exp,'session-performance',1,'session_started',active_path_id=exp['loader_path_id_upper'],save_id_hash=exp['loader_save_sha256'],reason='save_loader_load_complete'))+b'\n')
 for seq in range(2,v.PERF_LINE_COUNT+1): st['seq']=seq;st['snapshot_seq']=seq-1;st['snapshot_id']=f'session-performance:{seq}';h.write(v.json_line(st)+b'\n')
orig=b.evaluate_strategy; n=0
# ensure one reason tally by monkey patch? count only

def wrap(*args,**kw):
 global n; n+=1; return orig(*args,**kw)
b.evaluate_strategy=wrap
rt,pos=b.replay_initial_records(src,root/'out',startup_expectations=exp,require_startup_binding=True)
print('count',n,'latest',rt.latest and rt.latest.get('seq'),'blockers',rt.session_integrity_blockers,'warnings',list(rt.warnings)[-10:],'strategy last',rt.strategy_tracker.last_snapshot_id)
