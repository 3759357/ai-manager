import numpy as np
def buyhold(f,st): return 1.0
def static(w):
    return lambda f,st: w
def doc_policy(f,st):
    """현행 기획 초안 근사: 기본 65%, 계좌 DD -5% 매수중단, -7% 축소, -10% 대폭 축소. 복귀는 DD>-3%."""
    dd=f['nav']/st['hwm']-1
    if dd<=-0.10: st['stage']=3
    elif dd<=-0.07: st['stage']=max(st['stage'],2)
    elif dd<=-0.05: st['stage']=max(st['stage'],1)
    elif dd>-0.03: st['stage']=0
    return {0:0.65,1:min(f['w'],0.65),2:0.40,3:0.20}[st['stage']]
def trend(w_on=0.65,w_off=0.20):
    def p(f,st):
        if np.isnan(f['ma200']): return w_on
        on=st.get('on',True)
        if on and f['px']<f['ma200']*0.98: on=False
        elif not on and f['px']>f['ma200']*1.02: on=True
        st['on']=on; return w_on if on else w_off
    return p
def voltarget(target=0.15,wmax=0.80,wmin=0.20):
    return lambda f,st: float(np.clip(target/max(f['vol'],1e-6),wmin,wmax))
def v1(wmax=0.80,target=0.18,wmin=0.15,trend_cut=0.5,brake=((-0.06,0.75),(-0.09,0.5)),
       dip=True,dip_level=-0.20,hwm_window=252):
    """제안: 변동성 조절 x 추세 x 계좌 낙폭 브레이크(1년 롤링 고점, 히스테리시스) + 폭락 후 분할 재진입 트랜치"""
    def p(f,st):
        base=min(wmax,target/max(f['vol'],1e-6))
        # 추세 (±2% 히스테리시스)
        on=st.get('on',True)
        if not np.isnan(f['ma200']):
            if on and f['px']<f['ma200']*0.98: on=False
            elif not on and f['px']>f['ma200']*1.02: on=True
        st['on']=on
        tr=1.0 if on else trend_cut
        # 폭락 재진입: 시장이 1년 고점 대비 dip_level 이하 & 50일선 회복 → 추세 감산 해제
        mdd=f['px']/f['hi252']-1
        if dip and not on and mdd<=dip_level and not np.isnan(f['ma50']) and f['px']>f['ma50']:
            tr=1.0
        # 계좌 낙폭: 1년 롤링 고점 기준 (영구 잠김 방지)
        navs=f['navs'][-hwm_window:]; dd=f['nav']/navs.max()-1
        k=1.0
        for lvl,mult in brake:
            if dd<=lvl: k=mult
        # 히스테리시스: 브레이크 완화는 20거래일 유지 후
        prev=st.get('k',1.0)
        if k<prev: st['k']=k; st['k_day']=f['t']
        elif k>prev and f['t']-st.get('k_day',0)>=20: st['k']=k; st['k_day']=f['t']
        return float(np.clip(base*tr*st.get('k',1.0),wmin,wmax))
    return p
