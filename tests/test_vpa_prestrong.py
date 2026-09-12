import os
import numpy as np
import pandas as pd

from core.vpa import analyze_vpa


def _frame(n=40):
    o=np.full(n,100.0); c=np.full(n,100.0); h=np.full(n,100.5); l=np.full(n,99.5); v=np.full(n,1000.0)
    # Historical bullish OB + displacement with volume expansion.
    o[20],c[20],h[20],l[20]=100.4,99.8,100.7,99.5
    o[21],c[21],h[21],l[21]=99.8,102.0,102.2,99.7
    v[21]=2600
    for i in range(22,35):
        o[i]=c[i]=102.0; h[i]=102.3; l[i]=101.7
    # Current bullish continuation candle.
    o[39],c[39],h[39],l[39]=101.8,102.5,102.7,101.7
    v[39]=1900
    return pd.DataFrame({'timestamp':np.arange(n),'open':o,'high':h,'low':l,'close':c,'volume':v})


def test_vpa_confirms_directional_ob_effort_result():
    df=_frame()
    r=analyze_vpa(df,'BUY',zone_low=99.5,zone_high=100.4,origin_idx=20,atr=0.7)
    assert r['available']
    assert r['no_lookahead']
    assert r['displacement_volume_ratio'] > 1.5
    assert r['displacement_result_atr'] > 0.8
    assert r['confirmation']
    assert not r['adverse']


def test_vpa_flags_high_effort_opposing_attack_inside_zone():
    df=_frame()
    df.loc[39,['open','close','high','low','volume']]=[100.2,98.9,100.3,98.7,3000]
    r=analyze_vpa(df,'BUY',zone_low=99.5,zone_high=100.4,origin_idx=20,atr=0.7)
    assert r['available']
    assert r['adverse']
    assert r['classification']=='ADVERSE_ATTACK'


def test_prestrong_threshold_is_early_preparation_not_public_strength():
    # Contract: raw score 6-8 remains MEDIUM publicly, while the deep scanner
    # exposes pre_strong and the institutional registry is activated early.
    os.environ['PRE_STRONG_SCORE']='6.0'
    import scanner.deep_scanner as ds
    radar=__import__('core.engine', fromlist=['InstitutionalRadar']).InstitutionalRadar()
    entry={
        'deep_analyzed':True,'strength':'MEDIUM','score':6.5,'watch_score':6.5,
        'pre_expansion_evidence':['DISPLACEMENT'],'pre_expansion':{'phase':'EARLY_EXPANSION'},
        'pre_expansion_state':'PRE_EXPANSION_LONG','state':'DETECTED',
        'institutional':{'score':72},'analysis':{'composite_score':6.5},
        'a_grade_ready':False,'institutional_prepared':False,
        'watchlist_entry_time':1,'institutional_analysis_time':2,
    }
    import core.engine as E
    E.MEMORY['watchlist']={'BTC/USDT':entry}
    E.MEMORY['institutional_zone_analysis']={}
    radar._sync_institutional_zone_registry('BTC/USDT',entry)
    reg=E.MEMORY['institutional_zone_analysis']['BTC/USDT']
    assert reg['watch_score']==6.5
    assert entry['institutional_zone_active'] is True
    assert reg['state']=='INSTITUTIONAL_WATCH'
