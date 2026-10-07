import pandas as pd, numpy as np
COST=0.001          # 수수료+스프레드 편도 0.10%
TAX=0.22; DEDUCT=1800  # 해외주식 양도세 22%, 연 250만원(≈$1,800) 공제
def load(name='tech'):
    df=pd.read_csv(f'{name}.csv',parse_dates=[0],index_col=0)
    df.columns=['px','rf']; return df
def run(df, policy, start_nav=20000.0, tax=True, band=0.05, check_every=5):
    px=df.px.values; rf=df.rf.values; idx=df.index; n=len(px)
    r=np.r_[0,np.diff(px)/px[:-1]]
    ma200=pd.Series(px).rolling(200).mean().values
    ma50=pd.Series(px).rolling(50).mean().values
    vol=pd.Series(r).ewm(span=30).std().values*np.sqrt(252)
    hi252=pd.Series(px).rolling(252,min_periods=1).max().values
    cash=start_nav; sh=0.0; basis=0.0; realized=0.0
    nav=np.empty(n); w_hist=np.empty(n); traded=0.0
    st=dict(hwm=start_nav, stage=0, stage_day=0, pending=None)
    year=idx[0].year
    for t in range(n):
        if t>0: cash*=1+rf[t-1]/252   # 현금은 단기국채 금리로 운용(SGOV 등) 가정
        # 전일 신호를 오늘 종가에 체결 (미래 데이터 미사용)
        if st['pending'] is not None:
            tgt=st['pending']; st['pending']=None
            v=cash+sh*px[t]; cur=sh*px[t]/v
            if abs(tgt-cur)>band or (tgt==0 and sh>0):
                dv=(tgt-cur)*v; dsh=dv/px[t]
                if dsh<0:  # 매도: 평균단가 기준 실현손익
                    avg=basis/sh; realized+=(-dsh)*(px[t]-avg); basis-=(-dsh)*avg
                else: basis+=dv
                sh+=dsh; cash-=dv+abs(dv)*COST; traded+=abs(dv)
        if idx[t].year!=year:
            if tax and realized>DEDUCT: cash-=TAX*(realized-DEDUCT)
            realized=0.0; year=idx[t].year
        v=cash+sh*px[t]; nav[t]=v; w_hist[t]=sh*px[t]/v
        st['hwm']=max(st['hwm'],v)
        if t%check_every==0 or t<2:
            f=dict(t=t,px=px[t],ma200=ma200[t],ma50=ma50[t],vol=vol[t],hi252=hi252[t],w=w_hist[t],nav=v,
                   navs=nav[:t+1])
            st['pending']=policy(f,st)
    s=pd.Series(nav,idx)
    yrs=(idx[-1]-idx[0]).days/365.25
    dd=s/s.cummax()-1
    # 최장 고점 회복 기간
    under=(dd<0).astype(int); longest=0; cur=0
    for u in under.values:
        cur=cur+1 if u else 0; longest=max(longest,cur)
    daily=s.pct_change().dropna()
    return dict(CAGR=((s.iloc[-1]/s.iloc[0])**(1/yrs)-1)*100, MaxDD=dd.min()*100,
                Vol=daily.std()*np.sqrt(252)*100,
                Calmar=((s.iloc[-1]/s.iloc[0])**(1/yrs)-1)/abs(dd.min()),
                DD10pct_time=(dd<-0.10).mean()*100, LongestUW_yrs=longest/252,
                Turnover=traded/s.mean()/yrs, AvgEq=w_hist.mean()*100), s, pd.Series(w_hist,idx)
