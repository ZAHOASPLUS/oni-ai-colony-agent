raise SystemExit("ARCHIVED one-off script from the 2026-09-13 live runs. It deletes or overwrites live Bridge/Probe/config files with relative paths and no verification. Do not run it; see archive/tmp-scripts-20260913/README.md.")
import pathlib,tempfile,json,cProfile,pstats,io
from scripts import verify_bridge_reliability as v
from bridge import oni_bridge as b
root=pathlib.Path(tempfile.mkdtemp(prefix='prof2-')); source=root/'in.jsonl'; expected=v.startup_expectations(); st=v.startup_snapshot(expected,'session-performance',2,1,active_path_id=expected['loader_path_id_upper'],save_id_hash=expected['loader_save_sha256'])
with source.open('wb') as h:
 h.write(v.json_line(v.startup_event(expected,'session-performance',1,'session_started',active_path_id=expected['loader_path_id_upper'],save_id_hash=expected['loader_save_sha256'],reason='save_loader_load_complete'))+b'\n')
 for seq in range(2,v.PERF_LINE_COUNT+1): st['seq']=seq; st['snapshot_seq']=seq-1; st['snapshot_id']=f'session-performance:{seq}'; h.write(v.json_line(st)+b'\n')
p=cProfile.Profile();p.enable();runtime,_=b.replay_initial_records(source,root/'out',startup_expectations=expected,require_startup_binding=True);p.disable();s=io.StringIO();pstats.Stats(p,stream=s).sort_stats('cumtime').print_stats(35);print(s.getvalue());print(root)
