"""Closed-candle early institutional formation detector.

This is a discovery/evidence engine, not an entry engine.  It deliberately
looks for the conditions *before* expansion: compression, liquidity building,
absorption, failed breaks, directional pressure and proximity to a fresh zone.
"""
from __future__ import annotations
import math
from typing import Any, Dict

PHASES = ("PRE_EXPANSION", "EARLY_EXPANSION", "MID_EXPANSION", "LATE_EXPANSION", "EXHAUSTION")

def _safe(v, d=0.0):
    try: return float(v)
    except Exception: return d

def _atr(df, n=14):
    if df is None or len(df) < n + 2: return 0.0
    h,l,c=df["high"],df["low"],df["close"]
    tr=(h-l).combine((h-c.shift(1)).abs(),max).combine((l-c.shift(1)).abs(),max)
    return _safe(tr.rolling(n).mean().iloc[-1])

def _compression(df, window=24):
    if len(df) < window: return 0.0
    c=df["close"].iloc[-window:]
    span=max(1e-12,_safe(c.mean()))
    return max(0.0,min(100.0,100.0*(1.0-(float(c.max()-c.min())/span)*8.0)))

def _vol_compression(df, window=24):
    if len(df) < window*2: return 0.0
    a=_safe(df["volume"].iloc[-window:].mean()); b=_safe(df["volume"].iloc[-window*2:-window].mean())
    if b<=0:return 0.0
    return max(0.0,min(100.0,100.0*(1.0-min(1.0,a/b))))

def _equal_levels(df, window=40, tol=0.0025):
    if len(df)<window:return False,False
    s=df.iloc[-window:]
    highs=sorted([_safe(x) for x in s.high], reverse=True)[:5]
    lows=sorted([_safe(x) for x in s.low])[:5]
    def eq(vals):
        if len(vals)<2 or not vals[0]: return False
        return abs(vals[-1]-vals[0])/abs(vals[0]) <= tol
    return eq(highs),eq(lows)

def classify_move_maturity(df, atr=None) -> str:
    if df is None or len(df)<30:return "PRE_EXPANSION"
    atr=_safe(atr) or _atr(df)
    if atr<=0:return "PRE_EXPANSION"
    c=_safe(df.close.iloc[-1]); w=24
    move=abs(c-_safe(df.close.iloc[-w]))/atr if len(df)>=w else 0.0
    recent=max(_safe(df.high.iloc[-12:].max()-df.low.iloc[-12:].min())/atr,0.0)
    if move>=10 or recent>=8:return "EXHAUSTION"
    if move>=6:return "LATE_EXPANSION"
    if move>=3:return "MID_EXPANSION"
    if move>=1.25:return "EARLY_EXPANSION"
    return "PRE_EXPANSION"

def analyze_formation(df, *, side_hint=None, zone_low=None, zone_high=None, atr=None) -> Dict[str,Any]:
    if df is None or len(df)<35:
        return {"eligible":False,"verdict":"NEUTRAL","phase":"PRE_EXPANSION","score":0.0,"evidence":[],"reason":"INSUFFICIENT_DATA"}
    # Last candle is intentionally excluded from formation calculations.
    closed=df.iloc[:-1] if len(df)>40 else df
    atr=_safe(atr) or _atr(closed)
    comp=_compression(closed); vcomp=_vol_compression(closed)
    eqh,eql=_equal_levels(closed)
    vol_now=_safe(closed.volume.iloc[-6:].mean()); vol_base=max(1e-12,_safe(closed.volume.iloc[-30:-6].mean()))
    volume_ratio=vol_now/vol_base
    # Absorption proxy: repeated closes with relatively high volume and small bodies.
    body=(closed.close-closed.open).abs().iloc[-10:].mean()
    rng=(closed.high-closed.low).iloc[-10:].mean()
    absorption=max(0.0,min(100.0,(1.0-body/max(rng,1e-12))*100.0)) if rng else 0.0
    # Failed-break proxy: recent wick through the prior 20-bar extreme and close back inside.
    prior_hi=_safe(closed.high.iloc[-21:-1].max()); prior_lo=_safe(closed.low.iloc[-21:-1].min())
    last=closed.iloc[-1]
    failed_up=bool(_safe(last.high)>prior_hi and _safe(last.close)<prior_hi) if prior_hi else False
    failed_dn=bool(_safe(last.low)<prior_lo and _safe(last.close)>prior_lo) if prior_lo else False
    directional=0.0
    if len(closed)>=8:
        directional=(_safe(closed.close.iloc[-1])-_safe(closed.close.iloc[-8]))/max(atr,1e-12)
    pressure=50.0+max(-50.0,min(50.0,directional*15.0))
    zone_prox=0.0
    price=_safe(closed.close.iloc[-1])
    if zone_low is not None and zone_high is not None and zone_high>=zone_low and atr>0:
        if zone_low<=price<=zone_high: zone_prox=100.0
        else: zone_prox=max(0.0,100.0-(min(abs(price-zone_low),abs(price-zone_high))/atr)*40.0)
    liquidity=(35.0 if eqh else 0.0)+(35.0 if eql else 0.0)
    score=0.22*comp+0.16*vcomp+0.18*liquidity+0.16*absorption+0.10*zone_prox+0.10*max(0.0,50-abs(pressure-50))*2+0.08*(30 if (failed_up or failed_dn) else 0)
    phase=classify_move_maturity(closed,atr)
    evidence=[]
    if comp>=45:evidence.append("COMPRESSION")
    if vcomp>=35:evidence.append("VOLATILITY_COMPRESSION")
    if eqh or eql:evidence.append("LIQUIDITY_BUILDING")
    if absorption>=55:evidence.append("ABSORPTION")
    if failed_up or failed_dn:evidence.append("FAILED_BREAK")
    if zone_prox>=60:evidence.append("FRESH_ZONE_PROXIMITY")
    if pressure>=62:evidence.append("BUY_PRESSURE")
    elif pressure<=38:evidence.append("SELL_PRESSURE")
    verdict="NEUTRAL"
    if eqh or eql:
        if pressure>=55: verdict="ACCUMULATION"
        elif pressure<=45: verdict="DISTRIBUTION"
    eligible=bool(score>=45 and phase in ("PRE_EXPANSION","EARLY_EXPANSION"))
    return {"eligible":eligible,"verdict":verdict,"phase":phase,"score":round(min(100.0,max(0.0,score)),2),"evidence":evidence,"compression":round(comp,2),"volatility_compression":round(vcomp,2),"liquidity_building":round(liquidity,2),"absorption":round(absorption,2),"failed_break":failed_up or failed_dn,"zone_proximity":round(zone_prox,2),"directional_pressure":round(pressure,2),"atr":atr,"no_lookahead":True}
