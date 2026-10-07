import pandas as pd
from engine import load, run
from policies import *
pd.set_option('display.width',220); pd.set_option('display.float_format',lambda x:f'{x:.2f}')
V1=lambda: v1(target=0.14,wmin=0.10,wmax=0.80)
cols=['CAGR','MaxDD','Vol','Calmar','DD10pct_time','LongestUW_yrs','Turnover','AvgEq']
tech=load('tech'); spx=load('spx')
out=[]
for dn,d in [('나스닥100 1999-03~2026-09',tech),('나스닥100 2009-06~2026-09',tech.loc['2009-06':]),('S&P500 1928~1998 (규칙 선택 구간)',spx.loc[:'1998']),('S&P500 1999~2025',spx.loc['1999':])]:
    for k,p in [('100% 보유',buyhold),('고정 65%',static(0.65)),('현행 기획 근사',doc_policy),('비중 규칙 v1',V1())]:
        m=run(d,p)[0]; out.append(dict(data=dn,rule=k,**{c:round(m[c],2) for c in cols}))
df=pd.DataFrame(out); df.to_csv('final_table.csv',index=False); print(df.to_string(index=False))
z=tech.copy(); z['rf']=0
print('rf=0 v1', {k:round(v,2) for k,v in run(z,V1())[0].items()}); print('rf=0 static65', round(run(z,static(0.65))[0]['CAGR'],2))
# 차트용 곡선 저장
curves={k:run(tech,p)[1] for k,p in [('B&H',buyhold),('정적65',static(0.65)),('현행근사',doc_policy),('v1',V1())]}
pd.DataFrame(curves).to_csv('curves.csv')
