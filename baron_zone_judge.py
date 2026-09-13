"""baron_zone_judge — additive location-quality filter for the RORO entry funnel.

Pure, self-contained, deterministic. It never creates candidates, never opens
positions, never sizes an order, and never manages a trade. It only grades the
zone / order-block / location quality of a candidate that RORO already produced
and returns one of:

    ENTER_NOW    location quality verified -> RORO proceeds as it would have.
    WAIT_RETEST  zone is valid but the entry needs a retest / confirmation.
    BLOCK        zone broken, price extended, S/R flipped, or data unusable.

Scope (non-negotiable): this is NOT an entry strategy. RORO remains the sole
entry authority. The judge only answers "does the OB / zone at the entry
represent a strong, valid location?".

Model (mirrors the execution-queue weights so the judge speaks the same
language as the existing queue-quality machinery):

  * The order block is treated as a PRICE ZONE (high / low / mid / width), and
    the current price is graded against the band (inside / near / away).
  * S/R flips around the zone are detected for both sides:
      - BUY  : support broken  -> SUPPORT_INVALIDATED  (BLOCK)
               resistance broken up + retest held      (positive evidence)
      - SELL : resistance broken up -> RESISTANCE_INVALIDATED (BLOCK)
               support broken down + retest held       (positive evidence)
  * OB validity states: FRESH / VALID / WEAK / CONSUMED / BROKEN / INVALIDATED.
  * Volume is EVIDENCE (creation / rejection / retest / relative volume) and is
    never a universal hard gate. Missing/invalid data always degrades to BLOCK.
"""

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

ZONE_WEIGHTS = {
    "order_block_quality": 0.20,
    "zone_strength": 0.18,
    "liquidity_quality": 0.15,
    "institutional_confidence": 0.15,
    "structure_alignment": 0.12,
    "entry_timing": 0.10,
    "trend_alignment": 0.05,
    "risk_score": 0.05,
}

MIN_ROWS = 30
ZONE_TAP_PCT = 0.003
ENTRY_WINDOW_ATR = 1.5
OB_MAX_DIST_ATR = 0.5

BLOCK_OB_STATES = ("BROKEN", "FAKE", "CONSUMED", "INVALIDATED")
NEGATIVE_FLIP_BUY = ("SUPPORT_INVALIDATED", "RESISTANCE_AFTER_FLIP")
NEGATIVE_FLIP_SELL = ("RESISTANCE_INVALIDATED",)
POSITIVE_FLIP_BUY = ("RESISTANCE_TO_SUPPORT",)
POSITIVE_FLIP_SELL = ("SUPPORT_TO_RESISTANCE",)


@dataclass
class JudgeVerdict:
    symbol: str
    side: str
    decision: str
    final_zone_score: float = 0.0
    dimensions: Dict[str, float] = field(default_factory=dict)
    main_blocker: str = ""
    pending_reason: str = ""
    zone: Optional[dict] = None
    ob_quality: str = ""
    volume_state: str = ""
    trigger_state: str = ""
    location_quality: str = ""
    ob_state: str = ""
    sr_flip_state: str = ""
    chase: bool = False
    diagnostics: Dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.decision == "ENTER_NOW"

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "decision": self.decision,
            "final_zone_score": self.final_zone_score,
            "dimensions": self.dimensions,
            "main_blocker": self.main_blocker,
            "pending_reason": self.pending_reason,
            "zone": self.zone,
            "ob_quality": self.ob_quality,
            "volume_state": self.volume_state,
            "trigger_state": self.trigger_state,
            "location_quality": self.location_quality,
            "ob_state": self.ob_state,
            "sr_flip_state": self.sr_flip_state,
            "chase": self.chase,
            "diagnostics": self.diagnostics,
        }

    def render_report(self) -> str:
        """Human-readable quality model, mirroring the diagnostic spec."""
        side = self.side
        g = "\U0001F7E2"  # green
        y = "\U0001F7E1"  # yellow
        r = "\U0001F534"  # red
        blank = "\u25EF"
        diag = self.diagnostics or {}
        ob_zone = diag.get("ob_zone") or {}
        ob_type = ob_zone.get("type") or ("" if not ob_zone.get("high") else "DEMAND" if side == "BUY" else "SUPPLY")
        zoneb = diag.get("zone_band") or {}
        sr = diag.get("sr_flip") or {}
        liq = diag.get("liquidity") or {}
        vol = diag.get("volume_recipe") or {}
        st = diag.get("structure") or {}
        pl = diag.get("price_location") or {}

        def _pct(name, ok):
            return name, g if ok else (y if ok is None else r)

        lines = []
        lines.append("=" * 30)
        lines.append("(BARON) ZONE / OB QUALITY JUDGE")
        lines.append("=" * 30)
        lines.append("RORO SIGNAL: {:>3}".format("BUY" if side == "BUY" else "SELL"))
        lines.append("")
        lines.append("ORDER BLOCK")
        lines.append("  Type:     {}".format(ob_type or "N/A"))
        lines.append("  Status:   {} {}".format(self.ob_state or "N/A",
                                                g if self.ob_state in ("FRESH", "VALID") else y if self.ob_state == "WEAK" else r))
        lines.append("  Strength: {:.0f} / 100".format(float(diag.get("ob_strength") or self.dimensions.get("order_block_quality") or 0)))
        lines.append("")
        lines.append("ZONE")
        lines.append("  Type:     {}".format((zoneb.get("type") or "N/A")))
        lines.append("  Strength: {:.0f} / 100".format(float(self.dimensions.get("zone_strength") or 0)))
        lines.append("  Status:   {} {}".format("VALID" if self.decision != "BLOCK" or self.main_blocker == "" else self.main_blocker, blank))
        lines.append("")
        lines.append("S/R FLIP")
        state = sr.get("state") or ""
        if state and state != "NONE":
            good = state in POSITIVE_FLIP_BUY + POSITIVE_FLIP_SELL
            lines.append("  {}: {:>24} {}".format("BUY" if side == "BUY" else "SELL", state, g if good else r))
        else:
            lines.append("  {}: None".format("BUY" if side == "BUY" else "SELL"))
        lines.append("")
        lines.append("LIQUIDITY")
        if side == "BUY":
            lines.append("  Sell-side sweep: {:<4} {}".format("YES" if liq.get("sell_side_sweep") else "NO",
                                                             g if liq.get("sell_side_sweep") else blank))
        else:
            lines.append("  Buy-side sweep: {:<4} {}".format("YES" if liq.get("buy_side_sweep") else "NO",
                                                             g if liq.get("buy_side_sweep") else blank))
        lines.append("")
        lines.append("VOLUME")
        lines.append("  Confirmation: {:<8} {}".format((vol.get("confirmation") or "N/A").upper(),
                                                       g if vol.get("confirmation") == "GOOD" else y if vol.get("confirmation") == "CONCERN" else r))
        lines.append("")
        lines.append("STRUCTURE")
        lines.append("  MSS/BOS: {:<12} {}".format((st.get("mss") or "WAITING"), g if st.get("mss") and "CONFIRMED" in str(st.get("mss")) else y))
        lines.append("")
        lines.append("PRICE LOCATION")
        lines.append("  Inside zone:   {:<4} {}".format("YES" if pl.get("inside_zone") else "NO",
                                                        g if pl.get("inside_zone") else blank))
        lines.append("  Near edge:     {:<4} {}".format("YES" if pl.get("near_edge") else "NO",
                                                        g if pl.get("near_edge") else blank))
        lines.append("  Chase:         {:<4} {}".format("YES" if self.chase else "NO",
                                                        r if self.chase else (g if not self.chase else blank)))
        lines.append("")
        lines.append("=" * 30)
        if self.decision == "ENTER_NOW":
            lines.append("FINAL: {} ENTER_NOW".format(g))
        elif self.decision == "WAIT_RETEST":
            lines.append("FINAL: {} WAIT_RETEST".format(y))
            lines.append("       {}".format(self.pending_reason or ""))
        else:
            lines.append("FINAL: {} BLOCK".format(r))
            lines.append("       {}".format(self.main_blocker or ""))
        lines.append("=" * 30)
        return "\n".join(lines)


def _rma(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(alpha=1.0 / period, adjust=False).mean()


def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _compute_atr(df: pd.DataFrame, period: int = 14, fallback: float = 0.01) -> Optional[float]:
    if df is None or df.empty or len(df) < period + 1:
        return None
    try:
        high = df["high"]
        low = df["low"]
        close = df["close"]
        tr1 = high - low
        tr2 = (high - close.shift(1)).abs()
        tr3 = (low - close.shift(1)).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = _rma(tr, period).bfill().ffill().fillna(tr.mean()).clip(lower=1e-8)
        val = float(atr.iloc[-1])
        return max(val, fallback * float(close.iloc[-1]))
    except Exception:
        return None


def _candle_metrics(candle) -> Tuple[float, float, float, float]:
    body = abs(float(candle["close"]) - float(candle["open"]))
    rng = float(candle["high"]) - float(candle["low"])
    upper_wick = float(candle["high"]) - max(float(candle["open"]), float(candle["close"]))
    lower_wick = min(float(candle["open"]), float(candle["close"])) - float(candle["low"])
    return body, rng, upper_wick, lower_wick


def _classify_volume(df: pd.DataFrame, period: int = 20) -> str:
    if df is None or len(df) < period + 1 or "volume" not in df.columns:
        return "neutral"
    vol = df["volume"]
    avg = vol.rolling(period).mean().iloc[-1]
    if avg == 0:
        return "neutral"
    ratio = vol.iloc[-1] / avg
    if ratio > 1.8:
        return "expansion"
    if ratio > 1.3:
        return "normal"
    if ratio < 0.7:
        return "exhaustion"
    return "neutral"


def _detect_rejection(df: pd.DataFrame, side: str) -> bool:
    if df is None or df.empty:
        return False
    last = df.iloc[-1]
    body, rng, upper_wick, lower_wick = _candle_metrics(last)
    if rng == 0:
        return False
    if side == "BUY":
        return (lower_wick > 1.5 * body) or (last["close"] > last["open"] and body / rng > 0.5)
    return (upper_wick > 1.5 * body) or (last["close"] < last["open"] and body / rng > 0.5)


def _detect_displacement(df: pd.DataFrame, side: str, atr: float, volume_state: str) -> bool:
    if df is None or len(df) < 2 or atr <= 0:
        return False
    last = df.iloc[-1]
    body, _, _, _ = _candle_metrics(last)
    if body / atr < 0.8:
        return False
    if side == "BUY" and last["close"] <= last["open"]:
        return False
    if side == "SELL" and last["close"] >= last["open"]:
        return False
    return True


def _detect_structure_shift(df: pd.DataFrame) -> Optional[str]:
    if df is None or len(df) < 10:
        return None
    if df["high"].iloc[-3] > df["high"].iloc[-6] and df["low"].iloc[-3] > df["low"].iloc[-6]:
        return "bullish_shift"
    if df["high"].iloc[-3] < df["high"].iloc[-6] and df["low"].iloc[-3] < df["low"].iloc[-6]:
        return "bearish_shift"
    return None


def _detect_bos(df: pd.DataFrame, lookback: int = 5) -> Tuple[bool, bool]:
    if df is None or len(df) < lookback + 2:
        return False, False
    recent_high = df["high"].iloc[-lookback - 1:-1].max()
    recent_low = df["low"].iloc[-lookback - 1:-1].min()
    close = df["close"].iloc[-1]
    return close > recent_high, close < recent_low


def _detect_ob(df: pd.DataFrame, side: str, lookback: int = 4) -> Optional[Tuple[float, float, int]]:
    if df is None or len(df) < lookback + 2:
        return None
    atr = _compute_atr(df)
    if atr is None:
        return None
    closes = df["close"].values
    for j in range(len(df) - 1, max(0, len(df) - 8), -1):
        move = closes[j] - closes[j - 1]
        if side == "BUY" and move < atr * 1.2:
            continue
        if side == "SELL" and -move < atr * 1.2:
            continue
        for k in range(1, lookback + 2):
            idx = j - 1 - k
            if idx < 0:
                break
            candle = df.iloc[idx]
            if side == "BUY" and candle["close"] < candle["open"]:
                return float(candle["low"]), float(candle["high"]), int(idx)
            if side == "SELL" and candle["close"] > candle["open"]:
                return float(candle["low"]), float(candle["high"]), int(idx)
        break
    return None


def _detect_fvg(df: pd.DataFrame, threshold: float = 0.001) -> Optional[Tuple[str, float, float]]:
    if df is None or len(df) < 2:
        return None
    prev = df.iloc[-2]
    curr = df.iloc[-1]
    if curr["low"] > prev["high"] * (1 + threshold):
        return ("bullish", float(prev["high"]), float(curr["low"]))
    if curr["high"] < prev["low"] * (1 - threshold):
        return ("bearish", float(curr["high"]), float(prev["low"]))
    return None


def _swing_levels(df: pd.DataFrame, lookback: int = 120) -> Tuple[List[float], List[float]]:
    if df is None or df.empty:
        return [], []
    n = min(len(df), lookback)
    highs = df["high"].values[-n:]
    lows = df["low"].values[-n:]
    swing_highs: List[float] = []
    swing_lows: List[float] = []
    for i in range(2, len(highs) - 2):
        if highs[i] == max(highs[i - 2:i + 3]):
            swing_highs.append(float(highs[i]))
        if lows[i] == min(lows[i - 2:i + 3]):
            swing_lows.append(float(lows[i]))
    return swing_highs, swing_lows


def _cluster_levels(points: List[float], pct: float = 0.002) -> List[float]:
    if not points:
        return []
    sorted_p = sorted(points)
    clusters: List[List[float]] = []
    for p in sorted_p:
        if clusters and abs(p - clusters[-1][0]) / p < pct:
            clusters[-1].append(p)
        else:
            clusters.append([p])
    return [sum(c) / len(c) for c in clusters]


def _detect_sweep_side(df: pd.DataFrame, lookback: int = 10) -> Optional[str]:
    if df is None or len(df) < lookback + 2:
        return None
    for i in range(-lookback, -1):
        prev_low = df["low"].iloc[i - 1]
        curr_low = df["low"].iloc[i]
        lower_wick = min(df["open"].iloc[i], df["close"].iloc[i]) - curr_low
        if curr_low < prev_low and lower_wick > 0.0001:
            return "sell_side_taken"
        prev_high = df["high"].iloc[i - 1]
        curr_high = df["high"].iloc[i]
        upper_wick = curr_high - max(df["open"].iloc[i], df["close"].iloc[i])
        if curr_high > prev_high and upper_wick > 0.0001:
            return "buy_side_taken"
    return None


def _retest_held(df: pd.DataFrame, level: float, side: str, atr: float, lookback: int = 8) -> bool:
    n = len(df)
    lo = max(0, n - lookback)
    for i in range(lo, n - 1):
        if side == "BUY":
            if abs(float(df["low"].iloc[i]) - level) <= atr * 0.6 and float(df["close"].iloc[i + 1]) > level:
                return True
        else:
            if abs(float(df["high"].iloc[i]) - level) <= atr * 0.6 and float(df["close"].iloc[i + 1]) < level:
                return True
    return False


def _detect_sr_flip_state(df: pd.DataFrame, side: str, atr: float, price: float,
                          zone_edge: Optional[float], near_pct: float = 0.04) -> dict:
    """Detect S/R flip conditions relative to the candidate location.

    Negative flips are tied to the ZONE's own entry edge (the support/resistance
    being traded): a decisive CLOSE through the edge followed by NO reclaim of
    the edge means the level flipped and can no longer be treated as the original
    side. A liquidity sweep (wick through a base floor that is reclaimed) is
    mitigation, NOT a flip, so wick-only breaks never qualify.

    Positive flips (a broken opposing level that retested and held) are detected
    from swing structure and are evidence of location quality only.

      BUY  : RESISTANCE_TO_SUPPORT  (positive)   SUPPORT_INVALIDATED (negative)
      SELL : SUPPORT_TO_RESISTANCE  (positive)   RESISTANCE_INVALIDATED (negative)
    """
    if df is None or len(df) < 20 or atr is None or atr <= 0:
        return {"state": "NONE"}
    closes = df["close"]
    lookback = min(len(df), 20)
    closes_win = closes.values[-lookback:]

    # --- NEGATIVE: the traded edge itself flipped away from the candidate side ---
    if zone_edge is not None:
        if side == "BUY":
            zone_edge = float(zone_edge)
            if zone_edge - atr * 2.0 <= price <= zone_edge + atr * 0.5:
                break_idx = [i for i, c in enumerate(closes_win) if c <= zone_edge - atr * 0.3]
                if break_idx and not any(
                        closes_win[i] > zone_edge for i in range(break_idx[-1] + 1, len(closes_win))):
                    return {"state": "SUPPORT_INVALIDATED", "level": round(zone_edge, 6),
                            "detail": "traded support broke by close, never reclaimed -> overhead resistance"}
        else:
            zone_edge = float(zone_edge)
            if zone_edge - atr * 0.5 <= price <= zone_edge + atr * 2.0:
                break_idx = [i for i, c in enumerate(closes_win) if c >= zone_edge + atr * 0.3]
                if break_idx and not any(
                        closes_win[i] < zone_edge for i in range(break_idx[-1] + 1, len(closes_win))):
                    return {"state": "RESISTANCE_INVALIDATED", "level": round(zone_edge, 6),
                            "detail": "traded resistance broke by close, never reclaimed -> support underneath"}

    # --- POSITIVE: a broken opposing level retested and held (location evidence) ---
    swing_highs, swing_lows = _swing_levels(df, 60)
    res = _cluster_levels(swing_highs)
    sup = _cluster_levels(swing_lows)
    min_close_win = float(closes_win.min())
    max_close_win = float(closes_win.max())

    if side == "BUY":
        for lvl in res:
            if abs(lvl - price) / price >= near_pct:
                continue
            if price >= lvl - atr * 0.6 and max_close_win >= lvl + atr * 0.5 \
                    and _retest_held(df, lvl, "BUY", atr):
                return {"state": "RESISTANCE_TO_SUPPORT", "level": round(lvl, 6),
                        "detail": "resistance broken, retest held -> support"}
    else:
        for lvl in sup:
            if abs(lvl - price) / price >= near_pct:
                continue
            if price <= lvl + atr * 0.6 and min_close_win <= lvl - atr * 0.5 \
                    and _retest_held(df, lvl, "SELL", atr):
                return {"state": "SUPPORT_TO_RESISTANCE", "level": round(lvl, 6),
                        "detail": "support broken, retest held -> resistance"}
    return {"state": "NONE"}


def _ob_zone_spec(ob, side: str, atr: float, price: float) -> dict:
    """Order block as a price BAND (high / low / mid / width) + price relation."""
    if ob is None:
        return {"type": "", "high": None, "low": None, "mid": None, "width_atr": 0.0,
                "inside": False, "entry_distance_atr": 0.0}
    o_low, o_high, o_idx = ob
    mid = (o_high + o_low) / 2.0
    width_atr = (o_high - o_low) / atr if atr and atr > 0 else 0.0
    inside = o_low <= price <= o_high
    edge = o_low if side == "BUY" else o_high
    dist_edge = abs(price - edge) / atr if atr and atr > 0 else 0.0
    return {
        "type": "DEMAND" if side == "BUY" else "SUPPLY",
        "high": round(o_high, 6),
        "low": round(o_low, 6),
        "mid": round(mid, 6),
        "width_atr": round(width_atr, 4),
        "inside": inside,
        "entry_distance_atr": round(dist_edge, 4),
    }


def _volume_recipe(df: pd.DataFrame, ob, side: str) -> dict:
    """Volume around the OB: creation / rejection / retest / relative + confirmation."""
    if df is None or len(df) == 0 or "volume" not in df.columns:
        return {"creation_ratio": None, "rejection_ratio": None, "retest_ratio": None,
                "relative_ratio": None, "state": "neutral", "confirmation": "BAD"}
    vol = df["volume"].astype(float)
    window = min(len(df), 20)
    avg = float(vol.iloc[-window:].mean()) if window else 0.0
    if not avg or avg <= 0:
        avg = 1.0
    creation = None
    if ob is not None:
        creation = float(vol.iloc[ob[2]]) / avg
    rej_vols = []
    start = 0 if ob is None else ob[2] + 1
    for i in range(start, len(df)):
        body, rng, uw, lw = _candle_metrics(df.iloc[i])
        if rng == 0:
            continue
        if side == "BUY":
            if lw > 1.5 * body or (df["close"].iloc[i] > df["open"].iloc[i] and body / rng > 0.5):
                rej_vols.append(float(vol.iloc[i]))
        else:
            if uw > 1.5 * body or (df["close"].iloc[i] < df["open"].iloc[i] and body / rng > 0.5):
                rej_vols.append(float(vol.iloc[i]))
    rejection = max(rej_vols) / avg if rej_vols else None
    retest = float(vol.iloc[-1]) / avg
    state = _classify_volume(df)
    confirmation = "BAD"
    if ((creation is not None and creation > 1.3) or (rejection is not None and rejection > 1.3)) \
            and state != "exhaustion":
        confirmation = "GOOD"
    elif state == "exhaustion":
        confirmation = "BAD"
    elif retest > 1.3:
        confirmation = "CONCERN"
    else:
        confirmation = "CONCERN"
    return {
        "creation_ratio": round(creation, 3) if creation is not None else None,
        "rejection_ratio": round(rejection, 3) if rejection is not None else None,
        "retest_ratio": round(retest, 3),
        "relative_ratio": round(retest, 3),
        "state": state,
        "confirmation": confirmation,
    }


def _equal_extremes(df: pd.DataFrame, lookback: int = 50, tol: float = 0.002) -> Tuple[bool, bool]:
    if df is None or len(df) < lookback:
        return False, False
    sub = df.iloc[-lookback:]
    highs = sub["high"].values
    lows = sub["low"].values
    sh = [highs[i] for i in range(2, len(sub) - 2) if highs[i] == max(highs[i - 2:i + 3])]
    sl = [lows[i] for i in range(2, len(sub) - 2) if lows[i] == min(lows[i - 2:i + 3])]

    def _eq(points):
        if len(points) < 2:
            return False
        avg = sum(points) / len(points)
        return all(abs(p - avg) / avg < tol for p in points)

    return _eq(sh[-3:]) if len(sh) >= 3 else False, _eq(sl[-3:]) if len(sl) >= 3 else False


def assess(
    symbol: str,
    side: str,
    df: Optional[pd.DataFrame],
    atr: Optional[float] = None,
    price: Optional[float] = None,
    ob: Any = None,
    ctx: Optional[dict] = None,
) -> JudgeVerdict:
    """Grade the zone/OB/location quality of a RORO-produced candidate.

    Returns a JudgeVerdict with decision ENTER_NOW / WAIT_RETEST / BLOCK.
    This function has zero side effects: it never opens, sizes, or mutates.
    """
    side = (side or "BUY").upper()
    ctx = ctx or {}

    def _block(blocker: str, score: float, dims: Optional[Dict[str, float]] = None) -> JudgeVerdict:
        return JudgeVerdict(
            symbol=symbol, side=side, decision="BLOCK", final_zone_score=score,
            dimensions=dims or {}, main_blocker=blocker,
            volume_state=_classify_volume(df) if df is not None else "neutral",
        )

    if df is None or df.empty:
        return _block("INSUFFICIENT_DATA", 0.0)
    for col in ("open", "high", "low", "close", "volume"):
        if col not in df.columns:
            return _block("INSUFFICIENT_DATA:%s" % col, 0.0)
    if len(df) < MIN_ROWS:
        return _block("INSUFFICIENT_DATA", 0.0)

    last_close = float(df["close"].iloc[-1])
    price = float(price) if price else last_close
    atr = atr if atr and atr > 0 else _compute_atr(df)
    if atr is None or atr <= 0:
        return _block("INVALID_CONTEXT:atr", 0.0)
    if price <= 0:
        return _block("INVALID_CONTEXT:price", 0.0)

    swing_highs, swing_lows = _swing_levels(df)
    zones = {
        "supports": _cluster_levels(swing_lows),
        "resistances": _cluster_levels(swing_highs),
    }

    ob = _detect_ob(df, side)
    fvg = _detect_fvg(df)

    zone_level: Optional[float] = None
    zone_type = ""
    zone_high = None
    zone_low = None

    if ob is not None:
        ob_low, ob_high, ob_idx = ob
        if side == "BUY":
            zone_level, zone_type, zone_high, zone_low = ob_low, "BUY_OB", ob_high, ob_low
        else:
            zone_level, zone_type, zone_high, zone_low = ob_high, "SELL_OB", ob_high, ob_low
    elif fvg is not None:
        f_dir, f_a, f_b = fvg
        if side == "BUY" and f_dir == "bullish":
            zone_level, zone_type, zone_high, zone_low = price, "BULLISH_FVG", f_b, f_a
        elif side == "SELL" and f_dir == "bearish":
            zone_level, zone_type, zone_high, zone_low = price, "BEARISH_FVG", f_a, f_b
    if zone_level is None:
        supports = zones["supports"]
        resistances = zones["resistances"]
        if side == "BUY" and supports:
            best = min(supports, key=lambda s: abs(price - s))
            zone_level, zone_type = best, "SUPPORT"
            zone_low = zone_high = best
        elif side == "SELL" and resistances:
            best = min(resistances, key=lambda s: abs(price - s))
            zone_level, zone_type = best, "RESISTANCE"
            zone_low = zone_high = best

    if zone_level is None:
        return _block("NO_VALID_ZONE", 0.0)

    zone_pct = abs(price - zone_level) / price if price else 1.0
    dist_atr = abs(price - zone_level) / atr

    zone_broken = False
    if (zone_high is not None) and (zone_low is not None):
        if side == "BUY" and price < zone_low - atr * 0.8:
            zone_broken = True
        if side == "SELL" and price > zone_high + atr * 0.8:
            zone_broken = True

    extended = dist_atr > ENTRY_WINDOW_ATR
    tapped = zone_pct < ZONE_TAP_PCT or dist_atr <= OB_MAX_DIST_ATR
    chase = not tapped and dist_atr <= ENTRY_WINDOW_ATR

    sr_conflict = False
    if side == "BUY":
        near_res = [r for r in zones["resistances"] if abs(price - r) / price < ZONE_TAP_PCT]
        struct = _detect_structure_shift(df)
        if near_res and not tapped and struct != "bullish_shift":
            sr_conflict = True
    else:
        near_sup = [s for s in zones["supports"] if abs(price - s) / price < ZONE_TAP_PCT]
        struct = _detect_structure_shift(df)
        if near_sup and not tapped and struct != "bearish_shift":
            sr_conflict = True

    vol_state = _classify_volume(df)
    rejection = _detect_rejection(df, side)
    displacement = _detect_displacement(df, side, atr, vol_state)
    struct_shift = _detect_structure_shift(df)
    bos_up, bos_down = _detect_bos(df)
    struct_ok = (side == "BUY" and (struct_shift == "bullish_shift" or bos_up)) or \
                (side == "SELL" and (struct_shift == "bearish_shift" or bos_down))
    sweep_side = _detect_sweep_side(df)
    sweep_ok = (side == "BUY" and sweep_side == "sell_side_taken") or \
               (side == "SELL" and sweep_side == "buy_side_taken")

    ob_zone = _ob_zone_spec(ob, side, atr, price)
    zone_edge = zone_low if side == "BUY" else zone_high
    sr_flip = _detect_sr_flip_state(df, side, atr, price, zone_edge)
    vol_recipe = _volume_recipe(df, ob, side)
    eq_high, eq_low = _equal_extremes(df)

    order_block_quality, ob_quality_label = _grade_order_block(df, side, atr, ob, struct_shift)
    zone_strength = _grade_zone_strength(df, side, atr, zone_level)
    liquidity_quality = _grade_liquidity(df, side, sweep_ok, ob)
    inst_confidence = _grade_institutional(df, side, ob)
    structure_score = _grade_structure(df, side, struct_shift, bos_up, bos_down,
                                       sr_flip.get("state"))
    timing_score = _grade_timing(df, side, atr, price, zone_level, tapped, dist_atr)
    trend_score = _grade_trend(df, side)
    risk_score = _grade_risk(df, atr, price, dist_atr)

    dims = {
        "order_block_quality": round(order_block_quality, 1),
        "zone_strength": round(zone_strength, 1),
        "liquidity_quality": round(liquidity_quality, 1),
        "institutional_confidence": round(inst_confidence, 1),
        "structure_alignment": round(structure_score, 1),
        "entry_timing": round(timing_score, 1),
        "trend_alignment": round(trend_score, 1),
        "risk_score": round(risk_score, 1),
    }
    final_score = sum(max(0.0, min(100.0, dims[k])) * w for k, w in ZONE_WEIGHTS.items())
    final_score = round(final_score, 2)

    inside_zone = bool(ob_zone.get("inside")) if ob is not None else False
    location = "TRADE_ZONE"
    if ob is not None and inside_zone:
        location = "INSIDE_OB"
    elif tapped:
        location = "TRADE_ZONE"
    elif dist_atr <= 1.5:
        location = "NEAR_ZONE"
    else:
        location = "EXTENDED"

    trigger_state = "MITIGATION" if sweep_ok else "WAITING_TRIGGER"
    if sweep_ok and struct_ok and rejection:
        trigger_state = "MSS_CONFIRMED"
    elif sweep_ok and tapped and rejection:
        trigger_state = "LIQUIDITY_SWEEP"
    elif struct_ok and displacement:
        trigger_state = "BOS_CONFIRMED" if not (sweep_ok or rejection) else "CHOCH_CONFIRMED"

    zone_desc = {
        "type": zone_type,
        "level": round(zone_level, 6),
        "high": round(zone_high, 6) if zone_high is not None else None,
        "low": round(zone_low, 6) if zone_low is not None else None,
        "mid": round(ob_zone["mid"], 6) if ob_zone.get("mid") is not None else None,
        "width_atr": ob_zone.get("width_atr", 0.0),
        "distance_atr": round(dist_atr, 3),
        "tapped": tapped,
        "broken": zone_broken,
        "inside": inside_zone,
        "strength": zone_strength,
    }

    mss_state = "WAITING"
    if sweep_ok and struct_ok:
        mss_state = "MSS_CONFIRMED"
    elif (bos_up or bos_down) and struct_ok:
        mss_state = "BOS_CONFIRMED"

    verdict = JudgeVerdict(
        symbol=symbol, side=side, decision="WAIT_RETEST",
        final_zone_score=final_score, dimensions=dims,
        zone=zone_desc, ob_quality=ob_quality_label, volume_state=vol_state,
        trigger_state=trigger_state, location_quality=location,
        ob_state=ob_quality_label, sr_flip_state=sr_flip.get("state", "NONE"),
        chase=chase, pending_reason="Zone valid, waiting for better retest",
    )

    if zone_broken:
        verdict.decision = "BLOCK"
        verdict.main_blocker = "ZONE_BROKEN"
        verdict.pending_reason = ""
    elif extended:
        verdict.decision = "BLOCK"
        verdict.main_blocker = "PRICE_EXTENDED"
        verdict.pending_reason = ""
    elif ob_quality_label in BLOCK_OB_STATES:
        verdict.decision = "BLOCK"
        verdict.main_blocker = "OB_QUALITY:" + ob_quality_label
        verdict.pending_reason = ""
    elif sr_conflict:
        verdict.decision = "BLOCK"
        verdict.main_blocker = "SR_FLIP"
        verdict.pending_reason = ""
    elif sr_flip.get("state") in NEGATIVE_FLIP_BUY + NEGATIVE_FLIP_SELL:
        verdict.decision = "BLOCK"
        verdict.main_blocker = sr_flip["state"]
        verdict.pending_reason = ""
    elif not tapped:
        verdict.decision = "WAIT_RETEST"
        verdict.pending_reason = "Do not chase. Wait for pullback / retest." if chase else \
            "Price not yet at zone"
    else:
        if vol_state == "exhaustion":
            verdict.decision = "WAIT_RETEST"
            verdict.pending_reason = "Volume exhaustion, waiting for confirmation"
        elif final_score >= 72 and struct_ok and (rejection or displacement or sweep_ok):
            verdict.decision = "ENTER_NOW"
            verdict.pending_reason = ""
        else:
            verdict.decision = "WAIT_RETEST"
            verdict.pending_reason = "Waiting for structure / trigger confirmation"

    verdict.diagnostics = {
        "ob_detected": ob is not None,
        "fvg_detected": fvg is not None,
        "ob_zone": ob_zone,
        "ob_strength": order_block_quality,
        "zone_band": {
            "type": zone_type,
            "high": zone_desc["high"],
            "low": zone_desc["low"],
            "mid": zone_desc["mid"],
            "width_atr": zone_desc["width_atr"],
        },
        "price_location": {
            "inside_zone": inside_zone,
            "near_edge": tapped,
            "distance_atr": round(dist_atr, 3),
            "chase": chase,
        },
        "sr_flip": {
            "state": sr_flip.get("state", "NONE"),
            "level": sr_flip.get("level"),
            "detail": sr_flip.get("detail", ""),
        },
        "liquidity": {
            "sell_side_sweep": sweep_side == "sell_side_taken",
            "buy_side_sweep": sweep_side == "buy_side_taken",
            "equal_lows": eq_low,
            "equal_highs": eq_high,
        },
        "volume_recipe": vol_recipe,
        "structure": {
            "shift": struct_shift,
            "bos": "BOS_UP" if bos_up else ("BOS_DOWN" if bos_down else "NONE"),
            "mss": mss_state,
        },
        "ob_validity": ob_quality_label,
        "trigger": trigger_state,
        "rejection": rejection,
        "displacement": displacement,
        "structure_aligned": struct_ok,
        "sweep_aligned": sweep_ok,
        "zone_pct": round(zone_pct, 6),
        "zone_level": zone_level,
    }
    verdict.dimensions = dims
    return verdict


def _grade_order_block(df: pd.DataFrame, side: str, atr: float, ob,
                       struct_shift: Optional[str] = None) -> Tuple[float, str]:
    if ob is None:
        if df is not None and len(df):
            last = df.iloc[-1]
            body, rng, upper_wick, lower_wick = _candle_metrics(last)
            if rng == 0:
                return 30.0, "WEAK"
            if side == "BUY":
                ratio = lower_wick / rng
            else:
                ratio = upper_wick / rng
            if ratio > 0.6:
                return 60.0, "VALID"
            if ratio > 0.4:
                return 50.0, "WEAK"
            return 40.0, "WEAK"
        return 30.0, "WEAK"
    ob_low, ob_high, ob_idx = ob
    if df is None or len(df) < 2 or ob_idx < 0:
        return 30.0, "WEAK"
    if ob_idx + 1 >= len(df):
        return 30.0, "WEAK"
    base = float(df["low"].iloc[ob_idx])
    if side == "SELL":
        base = float(df["high"].iloc[ob_idx])
    last_price = float(df["close"].iloc[-1])
    min_low_after = float(df["low"].iloc[max(ob_idx + 1, len(df) - 8):].min()) if ob_idx + 1 < len(df) else last_price
    max_high_after = float(df["high"].iloc[max(ob_idx + 1, len(df) - 8):].max()) if ob_idx + 1 < len(df) else last_price

    last = df.iloc[-1]
    body, rng, upper_wick, lower_wick = _candle_metrics(last)
    if rng == 0:
        return 30.0, "WEAK"

    if side == "BUY":
        broken = min_low_after < base - atr * 0.8
        if last_price < ob_low - atr * 0.8:
            return 20.0, ("INVALIDATED" if struct_shift == "bearish_shift" else "BROKEN")
        ratio = lower_wick / rng
    else:
        broken = max_high_after > base + atr * 0.8
        if last_price > ob_high + atr * 0.8:
            return (20.0, "INVALIDATED" if struct_shift == "bullish_shift" else "BROKEN")
        ratio = upper_wick / rng

    if broken and ratio <= 0.4:
        return 20.0, "BROKEN"

    touches = 0
    rejections = 0
    for i in range(ob_idx + 1, len(df) - 1):
        if side == "BUY":
            low = float(df["low"].iloc[i])
            if low <= ob_high and low >= ob_low - atr * 0.3:
                touches += 1
                if float(df["close"].iloc[i]) > ob_low - atr * 0.3 \
                        and float(df["close"].iloc[i + 1]) > float(df["close"].iloc[i]):
                    rejections += 1
        else:
            high = float(df["high"].iloc[i])
            if high >= ob_low and high <= ob_high + atr * 0.3:
                touches += 1
                if float(df["close"].iloc[i]) < ob_high + atr * 0.3 \
                        and float(df["close"].iloc[i + 1]) < float(df["close"].iloc[i]):
                    rejections += 1

    if touches >= 4 and rejections < 2 and ratio <= 0.4:
        return 35.0, "CONSUMED"

    if touches == 0 and ratio > 0.6:
        if side == "BUY" and last["close"] > last["open"]:
            return 90.0, "FRESH"
        if side == "SELL" and last["close"] < last["open"]:
            return 90.0, "FRESH"
        return 60.0, "VALID"

    if 0 < touches <= 3 and rejections >= 1:
        return 70.0, "VALID"

    if ratio > 0.4:
        return 60.0, "VALID"

    return 45.0, "WEAK"


def _grade_zone_strength(df: pd.DataFrame, side: str, atr: float, zone_level: float) -> float:
    if df is None or len(df) < 30:
        return 50.0
    touches = 0
    rejections = 0
    vol_sum = 0.0
    for i in range(max(0, len(df) - 30), len(df) - 1):
        candle = df.iloc[i]
        if side == "BUY":
            if abs(candle["low"] - zone_level) < atr * 0.5:
                touches += 1
                if df["close"].iloc[i + 1] > candle["close"]:
                    rejections += 1
                    vol_sum += float(candle["volume"])
        else:
            if abs(candle["high"] - zone_level) < atr * 0.5:
                touches += 1
                if df["close"].iloc[i + 1] < candle["close"]:
                    rejections += 1
                    vol_sum += float(candle["volume"])
    score = 50.0
    if touches >= 4:
        score += 25
    elif touches >= 2:
        score += 12
    elif touches >= 1:
        score += 5
    if rejections >= 3:
        score += 20
    elif rejections >= 2:
        score += 10
    avg_vol = float(df["volume"].iloc[-30:].mean())
    if touches > 0 and avg_vol > 0:
        avg_touch = vol_sum / touches
        if avg_touch > 2 * avg_vol:
            score += 15
        elif avg_touch > 1.5 * avg_vol:
            score += 8
    return max(0.0, min(100.0, score))


def _grade_liquidity(df: pd.DataFrame, side: str, sweep_ok: bool, ob) -> float:
    score = 40.0
    if sweep_ok:
        score += 30
    eq_high, eq_low = _equal_extremes(df)
    if side == "BUY" and eq_low:
        score += 10
    if side == "SELL" and eq_high:
        score += 10
    if ob:
        bids = list(ob.get("bids", [])) if isinstance(ob, dict) else []
        asks = list(ob.get("asks", [])) if isinstance(ob, dict) else []
        if bids and asks:
            b_sum = sum(b[1] for b in bids[:10])
            a_sum = sum(a[1] for a in asks[:10])
            total = b_sum + a_sum
            if total > 0:
                imb = (b_sum - a_sum) / total
                if side == "BUY" and imb > 0.1:
                    score += 15
                elif side == "SELL" and imb < -0.1:
                    score += 15
                elif abs(imb) > 0.05:
                    score += 5
    return max(0.0, min(100.0, score))


def _grade_institutional(df: pd.DataFrame, side: str, ob) -> float:
    score = 50.0
    up_idx = []
    down_idx = []
    if df is not None and len(df) >= 10:
        for i in range(max(0, len(df) - 40), len(df)):
            if df["close"].iloc[i] > df["open"].iloc[i]:
                up_idx.append(i)
            elif df["close"].iloc[i] < df["open"].iloc[i]:
                down_idx.append(i)
    if up_idx and down_idx:
        up_vol = sum(float(df["volume"].iloc[i]) for i in up_idx)
        down_vol = sum(float(df["volume"].iloc[i]) for i in down_idx)
        if side == "BUY" and down_vol > 0:
            buy_ratio = up_vol / (up_vol + down_vol)
            if buy_ratio > 0.6:
                score += 15
            elif buy_ratio < 0.4:
                score -= 10
        elif side == "SELL" and down_vol > 0:
            sell_ratio = down_vol / (up_vol + down_vol)
            if sell_ratio > 0.6:
                score += 15
            elif sell_ratio < 0.4:
                score -= 10
    trend_col = "close"
    if df is not None:
        ema20 = _ema(df[trend_col], 20).iloc[-1]
        ema50 = _ema(df[trend_col], 50).iloc[-1]
        last = float(df[trend_col].iloc[-1])
        if side == "BUY" and last > ema20 > ema50:
            score += 10
        elif side == "SELL" and last < ema20 < ema50:
            score += 10
    return max(0.0, min(100.0, score))


def _grade_structure(df: pd.DataFrame, side: str, struct_shift, bos_up: bool, bos_down: bool,
                     sr_flip_state: Optional[str] = None) -> float:
    score = 50.0
    if side == "BUY":
        if struct_shift == "bullish_shift":
            score = 90.0
        elif bos_up:
            score = 70.0
        elif struct_shift == "bearish_shift":
            score = 30.0
    else:
        if struct_shift == "bearish_shift":
            score = 90.0
        elif bos_down:
            score = 70.0
        elif struct_shift == "bullish_shift":
            score = 30.0
    if sr_flip_state in POSITIVE_FLIP_BUY + POSITIVE_FLIP_SELL:
        score = max(score, 85.0)
    return max(0.0, min(100.0, score))


def _grade_timing(df: pd.DataFrame, side: str, atr: float, price: float, zone_level: float,
                  tapped: bool, dist_atr: float) -> float:
    score = 40.0
    if tapped:
        score += 30
    elif dist_atr < 1.0:
        score += 12
    elif dist_atr > 2.0:
        score -= 30
    if df is not None and not df.empty:
        last = df.iloc[-1]
        body, rng, upper_wick, lower_wick = _candle_metrics(last)
        if rng > 0:
            if side == "BUY" and lower_wick / rng > 0.5 and last["close"] > last["open"]:
                score += 20
            elif side == "SELL" and upper_wick / rng > 0.5 and last["close"] < last["open"]:
                score += 20
        avg = df["volume"].iloc[-10:].mean()
        if avg > 0 and df["volume"].iloc[-1] > 1.5 * avg:
            score += 10
    return max(0.0, min(100.0, score))


def _grade_trend(df: pd.DataFrame, side: str) -> float:
    if df is None or len(df) < 20:
        return 50.0
    close = df["close"]
    ema20 = _ema(close, 20).iloc[-1]
    ema50 = _ema(close, 50).iloc[-1]
    price = float(close.iloc[-1])
    score = 50.0
    if side == "BUY":
        if price > ema20 > ema50:
            score += 25
        elif price > ema20:
            score += 10
        else:
            score -= 20
    else:
        if price < ema20 < ema50:
            score += 25
        elif price < ema20:
            score += 10
        else:
            score -= 20
    return max(0.0, min(100.0, score))


def _grade_risk(df: pd.DataFrame, atr: float, price: float, dist_atr: float) -> float:
    score = 50.0
    atr_pct = atr / price * 100 if price > 0 else 0
    if 0.5 < atr_pct < 2.5:
        score += 10
    elif atr_pct > 4:
        score -= 20
    if dist_atr < 0.5:
        score += 10
    return max(0.0, min(100.0, score))