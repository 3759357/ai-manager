import pandas as pd, matplotlib
matplotlib.use('Agg'); import matplotlib.pyplot as plt
from matplotlib import font_manager as fm
import os
_f='/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'
if os.path.exists(_f): fm.fontManager.addfont(_f); plt.rcParams['font.family']='Noto Sans CJK JP'
else: plt.rcParams['font.family']='AppleGothic'  # macOS
c=pd.read_csv('curves.csv',index_col=0,parse_dates=True)
names={'B&H':'나스닥100 100% 보유','정적65':'고정 65% (현금 35%)','현행근사':'현행 기획 근사','v1':'비중 규칙 v1'}
cols={'B&H':'#2a78d6','정적65':'#eda100','현행근사':'#1baf7a','v1':'#eb6834'}
ink='#0b0b0b'; ink2='#52514e'; grid='#e4e3de'
fig,(a,b)=plt.subplots(2,1,figsize=(11,7.6),sharex=True,gridspec_kw=dict(height_ratios=[3,2],hspace=0.12))
fig.patch.set_facecolor('#fcfcfb')
for ax in (a,b):
    ax.set_facecolor('#fcfcfb'); ax.grid(axis='y',color=grid,lw=0.8); ax.tick_params(colors=ink2,labelsize=9)
    for s in ax.spines.values(): s.set_visible(False)
g=c/c.iloc[0]
for k in ['B&H','정적65','현행근사','v1']:
    a.plot(g.index,g[k],color=cols[k],lw=2.4 if k=='v1' else 1.6,zorder=3 if k=='v1' else 2)
    a.annotate(f"{names[k]}  {g[k].iloc[-1]:.1f}배",(g.index[-1],g[k].iloc[-1]),xytext=(6,0),textcoords='offset points',va='center',fontsize=9,color=ink)
    dd=(c[k]/c[k].cummax()-1)*100
    b.plot(dd.index,dd,color=cols[k],lw=2.2 if k=='v1' else 1.3,zorder=3 if k=='v1' else 2)
a.set_yscale('log'); from matplotlib.ticker import FixedLocator,FuncFormatter,NullLocator; a.yaxis.set_major_locator(FixedLocator([0.5,1,2,5,10,20])); a.yaxis.set_minor_locator(NullLocator()); a.yaxis.set_major_formatter(FuncFormatter(lambda v,_: f'{v:g}배')); a.set_ylabel('1달러의 성장 (로그)',color=ink2,fontsize=10)
a.set_title('1999-03 ~ 2026-09 · 세금 22%·거래비용 0.1% 반영 · 현금은 단기국채 금리',loc='left',color=ink2,fontsize=10)
fig.suptitle('나스닥100 기반 비중 규칙 비교',x=0.06,ha='left',fontsize=15,color=ink,y=0.97)
b.axhline(-10,color=ink2,lw=0.8,ls='--'); b.text(c.index[-1],-10,'  관리 목표 −10%',color=ink2,fontsize=8.5,va='center')
b.set_ylabel('고점 대비 낙폭 (%)',color=ink2,fontsize=10); b.set_ylim(-90,2)
a.legend([names[k] for k in cols],loc='upper left',frameon=False,fontsize=9,labelcolor=ink)
for line,k in zip(a.get_legend().get_lines(),cols): line.set_color(cols[k]); line.set_linewidth(2.4)
plt.subplots_adjust(left=0.07,right=0.80,top=0.90,bottom=0.06)
plt.savefig('../../docs/allocation-backtest.png',dpi=150,facecolor=fig.get_facecolor())
