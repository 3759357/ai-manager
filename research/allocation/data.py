import pandas as pd, numpy as np
q=pd.read_csv('raw/nateGeorge_simulate_leveraged_ETFs/eod_data/QQQ.csv',parse_dates=['Date']).set_index('Date')['Adj_Close']
n=pd.read_csv('raw/Chief-rich_nasdaq100-data/data/ndx_daily/history.csv',parse_dates=['Date']).set_index('Date')['Close']
m=pd.read_csv('raw/SteelCerberus_us-market-data/data/us_market_data.csv',parse_dates=['Date']).set_index('Date')
ov=pd.concat([q.pct_change(),n.pct_change()],axis=1,keys=['q','n']).dropna()
print('overlap corr',ov.corr().iloc[0,1].round(4),'days',len(ov),'ann diff %',((ov.q-ov.n).mean()*252*100).round(2))
cut=pd.Timestamp('2019-01-02')
nr=n.pct_change()[n.index>cut]+0.004/252   # NDX price -> approx QQQ total return (div ~0.6% - ER 0.2%)
tech=pd.concat([q[q.index<=cut], (1+nr).cumprod()*q[cut]])
tech.name='tech'
rf=m['Risk Free Rate']/100
spx=m['Adjusted Close']
idx=tech.index
rf_t=rf.reindex(idx,method='ffill').fillna(rf.iloc[-1])
pd.DataFrame({'tech':tech,'rf':rf_t}).to_csv('tech.csv')
s=pd.DataFrame({'spx':spx,'rf':rf}).loc['1928':]
s.to_csv('spx.csv')
t=tech
dd=t/t.cummax()-1
print('tech',t.index[0].date(),t.index[-1].date(),len(t),'maxDD',round(dd.min()*100,1),'CAGR',round(((t.iloc[-1]/t.iloc[0])**(365.25/(t.index[-1]-t.index[0]).days)-1)*100,2))
d2=spx.loc['1928':]; print('spx',d2.index[0].date(),d2.index[-1].date(),'maxDD',round((d2/d2.cummax()-1).min()*100,1))
for y0,y1 in [('2000','2003'),('2007','2009'),('2020-01','2020-06'),('2022','2022'),('2025-01','2025-06')]:
    s_=t.loc[y0:y1]; print(y0, round((s_/s_.cummax()-1).min()*100,1))
