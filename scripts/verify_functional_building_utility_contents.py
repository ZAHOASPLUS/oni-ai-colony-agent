#!/usr/bin/env python3
from __future__ import annotations
import copy,datetime as dt,hashlib,json,pathlib,sys
ROOT=pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from agent.field_contracts import FUNCTIONAL_BUILDING_UTILITY_CONTENTS_CONTRACT_ID,FUNCTIONAL_BUILDING_UTILITY_CONTENTS_SCHEMA,contract_by_id,contract_quality_fixture,read_contract_field
CONTRACT_PATH=ROOT/'tools/game_verify/contracts/oni_functional_building_utility_contents.v1.json'; SCENARIO_PATH=ROOT/'tools/game_verify/scenarios/functional_building_utility_contents_contract_v1.json'; BASE=dt.datetime(2026,9,13,tzinfo=dt.timezone.utc)
def state(c):
 t=BASE.isoformat(); w=c['world_id']; return {'schema':'oni.state.v2','session_id':'utility-contents-v1','seq':1,'snapshot_seq':1,'snapshot_id':'utility-contents-v1:1','ts':t,'world_id':w,'observation_scope':{'active_world_id':w,'populated_world_ids':[w],'covered_world_ids':[w],'complete_for_populated_worlds':True},'quality':{'functional_building_utility_contents':contract_quality_fixture(FUNCTIONAL_BUILDING_UTILITY_CONTENTS_CONTRACT_ID)},'functional_building_utility_contents':{'schema':FUNCTIONAL_BUILDING_UTILITY_CONTENTS_SCHEMA,'captured_at':t,'world_id':w,'complete':c['complete'],'instances':copy.deepcopy(c['instances'])}}
def main():
 c=json.loads(CONTRACT_PATH.read_text(encoding='utf-8')); s=json.loads(SCENARIO_PATH.read_text(encoding='utf-8')); assert c['payload_schema']==FUNCTIONAL_BUILDING_UTILITY_CONTENTS_SCHEMA and c['quality_contract_id']==FUNCTIONAL_BUILDING_UTILITY_CONTENTS_CONTRACT_ID and c['mutating_condition_allowed'] is False and s['live_admission'] is False; assert contract_by_id(FUNCTIONAL_BUILDING_UTILITY_CONTENTS_CONTRACT_ID) is not None
 src=ROOT/'mod/Source/Probe/FunctionalBuildingUtilityContentsTelemetryReader.cs'; txt=src.read_text(encoding='utf-8')
 for m in ('ConduitFlow.GetContents','SolidConduitFlow.GetContents'): assert m in txt
 for m in ('SetContents(', 'AddElement(', 'RemoveElement(', 'AddToNetworks(', 'RemoveFromNetworks(', 'ForceRebuildNetworks('): assert m not in txt
 out=[]
 for x in s['cases']:
  r=read_contract_field(state(x),'functional_building_utility_contents',FUNCTIONAL_BUILDING_UTILITY_CONTENTS_CONTRACT_ID); ok=bool(r.usable); assert ok==(x['expected']=='usable'),(x['id'],r.blockers); out.append({'id':x['id'],'usable':ok,'blockers':list(r.blockers)})
 stale=state(s['cases'][0]); stale['functional_building_utility_contents']['captured_at']=(BASE-dt.timedelta(seconds=6)).isoformat(); r=read_contract_field(stale,'functional_building_utility_contents',FUNCTIONAL_BUILDING_UTILITY_CONTENTS_CONTRACT_ID); assert not r.usable; out.append({'id':'stale_capture','usable':bool(r.usable),'blockers':list(r.blockers)})
 report={'schema':'oni.functional-building-utility-contents-verification.v1','status':'passed','live_admission':False,'case_count':len(out),'results':out,'contract_sha256':hashlib.sha256(CONTRACT_PATH.read_bytes()).hexdigest(),'scenario_sha256':hashlib.sha256(SCENARIO_PATH.read_bytes()).hexdigest(),'source':str(src.resolve()),'static_safety':{'read_markers':True,'mutation_markers_absent':True}}
 (ROOT/'reports/functional-building-utility-contents-verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8'); print(json.dumps(report,ensure_ascii=False,sort_keys=True))
if __name__=='__main__': main()
