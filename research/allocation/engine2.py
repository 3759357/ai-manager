import pandas as pd, numpy as np
from engine import COST, TAX, DEDUCT
SLIP=0.003   # 급락 중 체결 불리함 0.3% 추가
def run2(df, policy, trigger=None, cut=0.5, lock=5, gap_only=False, start_nav=20000.0, band=0.05, check_every=5):
    """policy: v1 계열 주간 정책. trigger: 전일 종가 대비 장중 하락률(예 0.05)에서 주식 비중을 cut배로 즉시 축소, lock일 동안 매수 금지."""
    o=df.o.values; l=df.l.values; c=df.c.values; rf=df.rf.values; idx=df.index; n=len(c)
    r=np.r_[0,np.diff(c)/c[:-1]]
    ma200=pd.Series(c).rolling(200).mean().values; ma50=pd.Series(c).rolling(50).mean().values
    vol=pd.Series(r).ewm(span=30).std().values*np.sqrt(252); hi252=pd.Series(c).rolling(252,min_periods=1).max().values
    cash=start_nav; sh=0.0; basis=0.0; realized=0.0; nav=np.empty(n); wh=np.empty(n); traded=0; fires=0
    st=dict(hwm=start_nav,pending=None); year=idx[0].year; lock_until=-1
    def trade(tgt_w, price, v):
        nonlocal cash, sh, basis, realized, traded
        cur=sh*price/v; dv=(tgt_w-cur)*v; dsh=dv/price
        if dsh<0: avg=basis/sh; realized+=(-dsh)*(price-avg); basis-=(-dsh)*avg
        else: basis+=dv
        sh+=dsh; cash-=dv+abs(dv)*COST; traded+=abs(dv)
    for t in range(n):
        if t>0: cash*=1+rf[t-1]/252
        # 1) 장중 급락 차단기 (전일 종가 기준, 갭이면 시가에 체결)
        if trigger and t>0 and sh>0:
            lvl=c[t-1]*(1-trigger)
            hit = o[t]<=lvl if gap_only else l[t]<=lvl
            if hit:
                px=min(o[t],lvl)*(1-SLIP); v=cash+sh*px
                trade(sh*px/v*cut, px, v); fires+=1; lock_until=t+lock; st['pending']=None
        # 2) 주간 정책 체결 (종가)
        if st['pending'] is not None:
            tgt=st['pending']; st['pending']=None
            v=cash+sh*c[t]; cur=sh*c[t]/v
            if t<=lock_until: tgt=min(tgt,cur)   # 잠금 중 매수 금지
            if abs(tgt-cur)>band: trade(tgt,c[t],v)
        if idx[t].year!=year:
            if realized>DEDUCT: cash-=TAX*(realized-DEDUCT)
            realized=0; year=idx[t].year
        v=cash+sh*c[t]; nav[t]=v; wh[t]=sh*c[t]/v; st['hwm']=max(st['hwm'],v)
        if t%check_every==0 or t<2:
            st['pending']=policy(dict(t=t,px=c[t],ma200=ma200[t],ma50=ma50[t],vol=vol[t],hi252=hi252[t],w=wh[t],nav=v,navs=nav[:t+1]),st)
    s=pd.Series(nav,idx); yrs=(idx[-1]-idx[0]).days/365.25; dd=s/s.cummax()-1
    worst_day=(s.pct_change().min())*100
    return dict(CAGR=((s.iloc[-1]/s.iloc[0])**(1/yrs)-1)*100, MaxDD=dd.min()*100, WorstDay=worst_day,
                Worst5d=(s.pct_change(5).min())*100, Fires=fires, FiresPerYr=fires/yrs, Turnover=traded/s.mean()/yrs), s
