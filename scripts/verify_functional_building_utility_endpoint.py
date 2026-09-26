#!/usr/bin/env python3
"""Offline verifier for utility endpoint registration telemetry."""
from __future__ import annotations
import copy, datetime as dt, hashlib, json, pathlib, sys
ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
from agent.field_contracts import FUNCTIONAL_BUILDING_UTILITY_ENDPOINT_CONTRACT_ID, FUNCTIONAL_BUILDING_UTILITY_ENDPOINT_SCHEMA, contract_by_id, contract_quality_fixture, read_contract_field
CONTRACT_PATH = ROOT / 'tools/game_verify/contracts/oni_functional_building_utility_endpoint.v1.json'
SCENARIO_PATH = ROOT / 'tools/game_verify/scenarios/functional_building_utility_endpoint_contract_v1.json'
BASE_TIME = dt.datetime(2026, 9, 12, tzinfo=dt.timezone.utc)
def state(case):
    ts = BASE_TIME.isoformat(); w = case['world_id']
    return {'schema':'oni.state.v2','session_id':'utility-endpoint-v1','seq':1,'snapshot_seq':1,'snapshot_id':'utility-endpoint-v1:1','ts':ts,'world_id':w,'observation_scope':{'active_world_id':w,'populated_world_ids':[w],'covered_world_ids':[w],'complete_for_populated_worlds':True},'quality':{'functional_building_utility_endpoint':contract_quality_fixture(FUNCTIONAL_BUILDING_UTILITY_ENDPOINT_CONTRACT_ID)},'functional_building_utility_endpoint':{'schema':FUNCTIONAL_BUILDING_UTILITY_ENDPOINT_SCHEMA,'captured_at':ts,'world_id':w,'complete':case['complete'],'instances':copy.deepcopy(case['instances'])}}
def main():
    c=json.loads(CONTRACT_PATH.read_text(encoding='utf-8')); s=json.loads(SCENARIO_PATH.read_text(encoding='utf-8'))
    assert c['payload_schema']==FUNCTIONAL_BUILDING_UTILITY_ENDPOINT_SCHEMA
    assert c['quality_contract_id']==FUNCTIONAL_BUILDING_UTILITY_ENDPOINT_CONTRACT_ID
    assert c['safety']['actions']==[] and s['action_types']==[] and s['live_admission'] is False
    assert contract_by_id(FUNCTIONAL_BUILDING_UTILITY_ENDPOINT_CONTRACT_ID) is not None
    source_path = ROOT / 'mod/Source/Probe/FunctionalBuildingUtilityEndpointTelemetryReader.cs'
    source_text = source_path.read_text(encoding='utf-8')
    for marker in ('GetEndpoint(', 'FlowUtilityNetwork.IItem'):
        assert marker in source_text, marker
    for banned in ('AddToNetworks(', 'RemoveFromNetworks(', 'ForceRebuildNetworks(', 'SetConnections('):
        assert banned not in source_text, banned
    out=[]
    for case in s['cases']:
        r=read_contract_field(state(case),'functional_building_utility_endpoint',FUNCTIONAL_BUILDING_UTILITY_ENDPOINT_CONTRACT_ID); usable=bool(r.usable); expected=case['expected']=='usable'; assert usable is expected,(case['id'],r.blockers); out.append({'id':case['id'],'usable':usable,'blockers':list(r.blockers)})
    stale=state(s['cases'][0]); stale['functional_building_utility_endpoint']['captured_at']=(BASE_TIME-dt.timedelta(seconds=6)).isoformat(); r=read_contract_field(stale,'functional_building_utility_endpoint',FUNCTIONAL_BUILDING_UTILITY_ENDPOINT_CONTRACT_ID); assert not r.usable; out.append({'id':'stale_capture','usable':bool(r.usable),'blockers':list(r.blockers)})
    report={'schema':'oni.functional-building-utility-endpoint-verification.v1','status':'passed','live_admission':False,'case_count':len(out),'results':out,'contract_sha256':hashlib.sha256(CONTRACT_PATH.read_bytes()).hexdigest(),'scenario_sha256':hashlib.sha256(SCENARIO_PATH.read_bytes()).hexdigest(),'source':str(source_path.resolve()),'static_safety':{'read_markers':True,'mutation_markers_absent':True}}
    (ROOT/'reports/functional-building-utility-endpoint-verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8'); print(json.dumps(report,ensure_ascii=False,sort_keys=True))
if __name__=='__main__': main()
