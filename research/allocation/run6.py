import pandas as pd
from engine2 import run2
from policies import v1, static, buyhold
pd.set_option('display.width',220); pd.set_option('display.float_format',lambda x:f'{x:.2f}')
d=pd.read_csv('tech_ohlc.csv',index_col=0,parse_dates=True)
V=lambda: v1(target=0.14,wmin=0.10,wmax=0.80)
rows=[]
for per,dd in [('1999-2026',d),('2009-2026',d.loc['2009-06':])]:
    for lab,pol,kw in [('v1 (주간만)',V,{}),
        ('v1 + 장중 -3% → 절반',V,dict(trigger=0.03)),('v1 + 장중 -4% → 절반',V,dict(trigger=0.04)),
        ('v1 + 장중 -5% → 절반',V,dict(trigger=0.05)),('v1 + 장중 -7% → 절반',V,dict(trigger=0.07)),
        ('v1 + 장중 -5% → 전량',V,dict(trigger=0.05,cut=0.0)),('v1 + 장중 -5% → 절반, 20일 잠금',V,dict(trigger=0.05,lock=20)),
        ('고정65 (비교)',lambda: static(0.65),{}),('고정65 + 장중 -5% → 절반',lambda: static(0.65),dict(trigger=0.05))]:
        m,_=run2(dd,pol(),**kw); rows.append(dict(기간=per,규칙=lab,**m))
print(pd.DataFrame(rows).to_string(index=False))
