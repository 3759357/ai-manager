import pandas as pd
q=pd.read_csv('raw/nateGeorge_simulate_leveraged_ETFs/eod_data/QQQ.csv',parse_dates=['Date']).set_index('Date')
n=pd.read_csv('raw/Chief-rich_nasdaq100-data/data/ndx_daily/history.csv',parse_dates=['Date']).set_index('Date')
cut=pd.Timestamp('2019-01-02')
a=q.loc[:cut,['Adj_Open','Adj_Low','Adj_Close']]; a.columns=['o','l','c']
b=n.loc[n.index>cut,['Open','Low','Close']].copy(); b.columns=['o','l','c']
# NDX 가격을 QQQ 총수익 수준으로 이어붙임
tr=(1+b.c.pct_change().fillna(b.c.iloc[0]/n.loc[cut,'Close']-1)+0.004/252).cumprod()*a.c.iloc[-1]
scale=tr/b.c; b=b.mul(scale,axis=0)
t=pd.concat([a,b]); rf=pd.read_csv('tech.csv',index_col=0,parse_dates=True).rf
t['rf']=rf.reindex(t.index).ffill()
assert (t.l<=t[['o','c']].min(axis=1)+1e-9).mean()>0.99
t.to_csv('tech_ohlc.csv'); print(t.tail(2));
r=(t.l/t.c.shift()-1)
for k in [0.03,0.04,0.05,0.07]: print(k, 'days intraday<=-k:', int((r<=-k).sum()), ' of which close recovered above -k/2:', int(((r<=-k)&(t.c/t.c.shift()-1>-k/2)).sum()))
