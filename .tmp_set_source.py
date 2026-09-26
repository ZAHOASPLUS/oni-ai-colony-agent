from pathlib import Path
p=Path(r'C:/Users/user/Documents/Klei/OxygenNotIncluded/kplayerprefs.yaml')
t=p.read_text(encoding='utf-8')
old=r'C:\Users\user\Documents\Klei\OxygenNotIncluded\cloud_save_files\76561190000000000\气喘吁吁的杂物间\气喘吁吁的杂物间.sav'
new=r'C:\Users\user\Documents\Klei\OxygenNotIncluded\cloud_save_files\76561190000000000\娴雅的太空监牢\oni-v3-observer-20260830-193156.sav'
if old not in t: raise SystemExit('source value not found')
p.write_text(t.replace(old,new,1),encoding='utf-8');print('updated source binding')
