#!/usr/bin/env python3
"""Offline verifier for paired utility contents rate analysis."""
from __future__ import annotations
import copy,datetime as dt,json,pathlib,sys
ROOT=pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from agent.utility_flow import paired_contents_rates
BASE=dt.datetime(2026,9,13,tzinfo=dt.timezone.utc)
def snap(t,mass=1.0,world=0,kind='gas',element='Oxygen',complete=True):
 side={'declared':True,'cell':10,'content_kind':kind,'element':element,'mass':mass if kind!='solid' else None,'temperature':293.15 if kind!='solid' else None,'disease_count':0 if kind!='solid' else None,'solid_present':None if kind!='solid' else True}
 empty={'declared':False,'cell':None,'content_kind':None,'element':None,'mass':None,'temperature':None,'disease_count':None,'solid_present':None}
 return {'world_id':world,'functional_building_utility_contents':{'schema':'oni.functional_building_utility_contents.v1','captured_at':t.isoformat(),'world_id':world,'complete':complete,'instances':[{'instance_id':1,'world_id':world,'prefab_id':'Pump','input':side,'output':empty}]}}
def main():
 a=snap(BASE,1.0); b=snap(BASE+dt.timedelta(seconds=10),3.0); r=paired_contents_rates(a,b); assert r['status']=='observed' and r['samples'][0]['mass_rate_per_second']==0.2
 c=snap(BASE+dt.timedelta(seconds=10),2.0,kind='solid'); r=paired_contents_rates(a,c); assert r['status']=='unknown' and r['reason']=='endpoint_identity_changed'
 d=snap(BASE+dt.timedelta(seconds=10),2.0,world=7); r=paired_contents_rates(a,d); assert r['status']=='unknown' and r['reason']=='world_mismatch'
 e=snap(BASE+dt.timedelta(seconds=10),2.0,element='Hydrogen'); r=paired_contents_rates(a,e); assert r['status']=='unknown' and r['reason']=='element_changed'
 f=snap(BASE+dt.timedelta(seconds=10),2.0); f['functional_building_utility_contents']['instances'][0]['input']['declared']=False; a2=copy.deepcopy(a); a2['functional_building_utility_contents']['instances'][0]['input']['declared']=False; r=paired_contents_rates(a2,f); assert r['status']=='unknown' and r['reason']=='endpoint_undeclared'
 g=copy.deepcopy(a); g['functional_building_utility_contents']['captured_at']=(BASE+dt.timedelta(seconds=10)).isoformat(); g['functional_building_utility_contents']['instances']='bad'; r=paired_contents_rates(a,g); assert r['status']=='unknown' and r['reason']=='instances_invalid'
 report={'schema':'oni.utility-contents-rate-verification.v1','status':'passed','case_count':6,'live_admission':False,'results':['positive_rate','solid_unknown','world_mismatch','element_change','undeclared_endpoint','instances_invalid'],'source':str((ROOT/'agent/utility_flow.py').resolve())}
 (ROOT/'reports/utility-contents-rate-verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
 print(json.dumps(report,ensure_ascii=False))
if __name__=='__main__': main()
