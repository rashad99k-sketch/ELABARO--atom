"""Volume-Price Analysis evidence for BARON.

VPA is an evidence layer, not an entry authority.  It evaluates effort (volume)
against result (candle body/range and directional displacement), with special
attention to an Order Block and its retest.  Calculations are closed-candle
only and never mutate trading state.
"""
from __future__ import annotations
from typing import Any, Dict, Optional


def _f(v: Any, default: float = 0.0) -> float:
    try:
        x = float(v)
        return x if x == x else default
    except Exception:
        return default


def analyze_vpa(df, side: str, *, zone_low: Optional[float] = None,
                zone_high: Optional[float] = None, origin_idx: Optional[int] = None,
                atr: Optional[float] = None) -> Dict[str, Any]:
    """Return directional VPA evidence for an OB.

    The score is deliberately bounded and interpretable; it is not a probability.
    "confirmed" means effort/result supports the requested side without a major
    opposing-volume signature.  "adverse" means the recent price/volume behavior
    is attacking the zone rather than defending it.
    """
    out = {"available": False, "score": 0.0, "classification": "UNAVAILABLE",
           "confirmation": False, "adverse": False, "effort_result": 0.0,
           "volume_ratio": 0.0, "displacement_volume_ratio": 0.0,
           "retest_volume_ratio": 0.0, "absorption": 0.0, "reason": "INSUFFICIENT_DATA"}
    if df is None or len(df) < 20 or not all(c in df.columns for c in ("open","high","low","close","volume")):
        return out
    side = str(side).upper()
    if side not in ("BUY", "SELL"):
        return out
    # The caller owns candle finality: scanner paths pass closed OHLCV while the
    # live manager may pass a hybrid mark candle. No look-ahead is introduced.
    x = df.copy()
    if len(x) < 15:
        return out
    ranges = (x["high"] - x["low"]).clip(lower=1e-12)
    bodies = (x["close"] - x["open"]).abs()
    vol_base = _f(x["volume"].iloc[-11:-1].mean(), 0.0)
    if vol_base <= 0:
        vol_base = _f(x["volume"].iloc[-20:].mean(), 0.0)
    if vol_base <= 0:
        return out
    last = x.iloc[-1]
    last_vr = _f(last["volume"]) / vol_base
    result_atr = _f(atr, 0.0)
    if result_atr <= 0:
        tr = ranges.rolling(14).mean().iloc[-1]
        result_atr = _f(tr, 0.0)
    body_result = _f(bodies.iloc[-1]) / max(result_atr, 1e-12)
    efficiency = _f(bodies.iloc[-1]) / max(_f(ranges.iloc[-1]), 1e-12)
    directional = (side == "BUY" and _f(last["close"]) > _f(last["open"])) or (side == "SELL" and _f(last["close"]) < _f(last["open"]))

    # Historical displacement around the selected OB: use bars after origin,
    # never future bars beyond the available historical frame.
    disp_vr = 1.0
    disp_result = 0.0
    if origin_idx is not None and int(origin_idx) >= 0 and int(origin_idx) < len(x) - 1:
        i = int(origin_idx)
        fut = x.iloc[i + 1:min(len(x), i + 4)]
        if len(fut):
            fv = _f(fut["volume"].max())
            disp_vr = fv / vol_base if vol_base > 0 else 1.0
            if side == "BUY":
                disp_result = (_f(fut["close"].max()) - _f(x.iloc[i]["high"])) / max(result_atr, 1e-12)
            else:
                disp_result = (_f(x.iloc[i]["low"]) - _f(fut["close"].min())) / max(result_atr, 1e-12)

    # Retest behavior: volume should generally contract into a healthy retest;
    # expansion + poor directional result is an adverse signature.
    retest = x.iloc[-5:]
    retest_vr = _f(retest["volume"].mean()) / vol_base if vol_base > 0 else 1.0
    retest_body = _f(retest.apply(lambda r: abs(r["close"]-r["open"]), axis=1).mean())
    retest_range = _f(retest.apply(lambda r: r["high"]-r["low"], axis=1).mean())
    retest_eff = retest_body / max(retest_range, 1e-12)

    in_zone = False
    if zone_low is not None and zone_high is not None:
        zl, zh = sorted((_f(zone_low), _f(zone_high)))
        in_zone = _f(last["low"]) <= zh and _f(last["high"]) >= zl

    # Absorption proxy: high effort with low result near the zone.
    high_effort = max(last_vr, retest_vr) >= 1.6
    low_result = efficiency < 0.45 or retest_eff < 0.45
    absorption = 70.0 if high_effort and low_result else 20.0 if high_effort else 0.0

    score = 50.0
    if directional:
        score += 10.0
    if last_vr >= 1.5 and directional and efficiency >= 0.55:
        score += 15.0
    elif last_vr < 0.8 and in_zone:
        score += 5.0
    if disp_vr >= 1.5 and disp_result >= 0.8:
        score += 15.0
    if in_zone and retest_vr <= 1.15 and retest_eff >= 0.35:
        score += 8.0
    if absorption >= 60:
        score -= 10.0
    # Strong opposite effort/result at the zone is a direct warning.
    opposite = (side == "BUY" and _f(last["close"]) < _f(last["open"])) or (side == "SELL" and _f(last["close"]) > _f(last["open"]))
    adverse = bool(in_zone and opposite and last_vr >= 1.6 and efficiency >= 0.55)
    if adverse:
        score -= 28.0
    score = max(0.0, min(100.0, score))
    confirmation = score >= 65.0 and not adverse and (disp_result >= 0.5 or (directional and last_vr >= 1.25))
    if adverse:
        cls = "ADVERSE_ATTACK"
    elif confirmation and score >= 78:
        cls = "STRONG_CONFIRMATION"
    elif confirmation:
        cls = "CONFIRMED"
    elif absorption >= 60:
        cls = "ABSORPTION_WATCH"
    else:
        cls = "NEUTRAL"
    out.update({
        "available": True, "score": round(score, 2), "classification": cls,
        "confirmation": bool(confirmation), "adverse": adverse,
        "effort_result": round(efficiency * 100.0, 2),
        "volume_ratio": round(last_vr, 3),
        "displacement_volume_ratio": round(disp_vr, 3),
        "displacement_result_atr": round(disp_result, 3),
        "retest_volume_ratio": round(retest_vr, 3),
        "retest_efficiency": round(retest_eff, 3),
        "absorption": round(absorption, 2), "in_zone": in_zone,
        "reason": "VPA effort/result validated" if confirmation else cls,
        "no_lookahead": True,
    })
    return out
