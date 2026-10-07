import numpy as np, pandas as pd, json
from policies import v1
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "core" / "src"))
from core.allocation import equity_cap, V1Params  # 운영 코드와 연구 코드의 숫자 일치 확인
from engine import load, run
d=load('tech')
# 백테스트가 실제로 만든 NAV 곡선을 입력으로 사용
m,s,w=run(d,v1(target=0.14,wmin=0.10,wmax=0.80))
px=d.px.values; navs=s.values
# research 정책을 같은 방식으로 직접 호출해 체크포인트별 목표를 기록
pol=v1(target=0.14,wmin=0.10,wmax=0.80); st={}
r=np.r_[0,np.diff(px)/px[:-1]]
ma200=pd.Series(px).rolling(200).mean().values; ma50=pd.Series(px).rolling(50).mean().values
vol=pd.Series(r).ewm(span=30).std().values*np.sqrt(252); hi=pd.Series(px).rolling(252,min_periods=1).max().values
ref={}
for t in range(len(px)):
    if t%5==0 or t<2:
        ref[t]=pol(dict(t=t,px=px[t],ma200=ma200[t],ma50=ma50[t],vol=vol[t],hi252=hi[t],w=0,nav=navs[t],navs=navs[:t+1]),st)
worst=0; samples=[]
for t in list(range(1,len(px),37))+[len(px)-1]:
    last=max(k for k in ref if k<=t)
    got=equity_cap(px[:t+1].tolist(), navs[:t+1].tolist())
    assert got['computed_on_index']==last
    diff=abs(got['cap']-ref[last]); worst=max(worst,diff)
print('points',len(range(0,len(px),37))+1,'max abs diff',worst)
# 골든 값: 앞 400일 합성 입력 (core 테스트용)
t=399; g=equity_cap(px[:t+1].tolist(), navs[:t+1].tolist())
print('golden idx',g['computed_on_index'],'cap',repr(g['cap']))
json.dump(dict(closes=[round(x,6) for x in px[:400]], navs=[round(x,6) for x in navs[:400]]),open('golden_input.json','w'))
g2=equity_cap([round(x,6) for x in px[:400]],[round(x,6) for x in navs[:400]]); print('golden rounded cap',repr(g2['cap']), g2['trend_on'], g2['brake_factor'])
# ref on rounded inputs via research policy
pr=np.array([round(x,6) for x in px[:400]]); nr=np.array([round(x,6) for x in navs[:400]])
r=np.r_[0,np.diff(pr)/pr[:-1]]; m2=pd.Series(pr).rolling(200).mean().values; m5=pd.Series(pr).rolling(50).mean().values
vo=pd.Series(r).ewm(span=30).std().values*np.sqrt(252); h=pd.Series(pr).rolling(252,min_periods=1).max().values
st={}; pol=v1(target=0.14,wmin=0.10,wmax=0.80); out=None
for t in range(400):
    if t%5==0 or t<2: out=pol(dict(t=t,px=pr[t],ma200=m2[t],ma50=m5[t],vol=vo[t],hi252=h[t],w=0,nav=nr[t],navs=nr[:t+1]),st)
print('research on rounded', repr(out))
print('current cap full', equity_cap(px.tolist(), navs.tolist())['cap'])
