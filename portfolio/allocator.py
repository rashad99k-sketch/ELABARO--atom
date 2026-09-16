"""Global portfolio allocation, concentration, and capital-rotation logic.

This module answers "Where is the best institutional opportunity across the
whole market right now?" without fabricating signals. It reads the production
queue/watchlist structures and imposes three portfolio-level gates on top of
the existing institutional gates:

1. Asset-class bucket exposure caps (INDEX/STOCK share one 2-seat technical
   bucket; OIL/GOLD share one 1-seat bucket; CRYPTO 2; NEWS 1 independent).
2. Directional exposure caps (BUY/SELL count).
3. A Global Allocation Rank (queue priority minus concentration penalty).

Portfolio specification (5 technical + 1 NEWS = 6 total):
   CRYPTO bucket 2/2, INDEX/STOCK bucket 2/2, OIL/GOLD bucket 1/1,
   NEWS 1/1 independent, TECHNICAL total 5/5, TOTAL 6/6.

Unused slots are NEVER implicitly filled — the engine deliberately returns a
reason string for each unused slot so the dashboard can explain them.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence
import threading
import time
import os

from portfolio.manager import PortfolioManager


# Class caps are expressed in counts (not score) to prevent a single class
# bucket from consuming all open positions, while remaining intentionally loose.
#
# PORTFOLIO SPECIFICATION (single source of truth for the numbers):
#   TECHNICAL BOOK = 5 seats:
#     1. CRYPTO bucket                 = 2 seats
#     2. INDEX/STOCK bucket (COMBINED) = 2 seats (INDEX and STOCK share this
#        bucket: "Index/Stock #1 + Index/Stock #2" is allowed — two positions
#        drawn from EITHER asset class, never 2 INDEX + 2 STOCK)
#     3. OIL/GOLD bucket (COMBINED)    = 1 seat (OIL or GOLD, not both)
#   NEWS BOOK      = 1 fully independent seat (never consumes a technical seat)
#   TOTAL MAXIMUM  = 6.
# A class with no bucket seat (unrecognised asset class) is discovered and
# analyzed but never opened -> reasoned bucket cap 0 (fail-closed, traceable).
DEFAULT_CLASS_CAPS = {
    "CRYPTO": 2,
    "INDEX": 2,
    "GOLD": 1,
    "OIL": 1,
    "NEWS": 1,
}
# Asset class -> combined capacity bucket. INDEX/STOCK share the same 2-seat
# technical bucket; OIL/GOLD (and the metal/energy aliases) share ONE seat.
ASSET_BUCKETS = {
    "CRYPTO": "CRYPTO",
    "INDEX": "INDEX_STOCK",
    "STOCK": "INDEX_STOCK",
    "ETF": "INDEX_STOCK",
    "OIL": "COMMODITY",
    "GOLD": "COMMODITY",
    "METAL": "COMMODITY",
    "ENERGY": "COMMODITY",
    "METALS": "COMMODITY",
    "COMMODITIES": "COMMODITY",
    # FOREX keeps its own reasoned cap-0 bucket: discovered/analyzed but never
    # opened (capacity spec is fixed at 6 seats; no Forex seat exists), and it
    # can never silently fall into the CRYPTO bucket.
    "FOREX": "FOREX",
    "NEWS": "NEWS",
}
# Bucket representative class -> per-class cap lookup, so DEFAULT_CLASS_CAPS
# above stays the ONE numbers source (bucket cap = cap of its representative).
BUCKET_REPRESENTATIVE = {
    "CRYPTO": "CRYPTO",
    "INDEX_STOCK": "INDEX",
    "COMMODITY": "GOLD",
    "NEWS": "NEWS",
}

TECHNICAL_CLASSES = set(ASSET_BUCKETS.keys())
DEFAULT_MAX_TECHNICAL_POSITIONS = 5


def bucket_of(asset_class: str) -> str:
    """Normalize an asset class to its capacity bucket. Unknown classes keep
    their own name as the bucket so an unrecognised class gets cap 0 and a
    precise f"{CLASS}_CAP" reason instead of a silent default."""
    ac = str(asset_class or "").upper()
    return ASSET_BUCKETS.get(ac) or ac


def bucket_cap(bucket: str) -> int:
    """Capacity of a combined bucket. Numbers come from DEFAULT_CLASS_CAPS via
    the bucket's representative class; unknown buckets have NO seat -> 0."""
    rep = BUCKET_REPRESENTATIVE.get(str(bucket or "").upper())
    if rep is None:
        return 0
    return int(DEFAULT_CLASS_CAPS.get(rep, 0))


@dataclass
class AllocationDecision:
    symbol: str
    side: str
    asset_class: str
    score: float
    allowed: bool
    reason: str
    penalty: float
    bucket: str = ""
    bucket_used: int = 0
    bucket_max: int = 0
    technical_used: int = 0
    technical_max: int = 0
    news_used: int = 0
    news_max: int = 0
    total_used: int = 0
    total_max: int = 0
    class_counts: Optional[Dict[str, int]] = None
    candidate_key: str = ""

    def trace_dict(self) -> dict:
        """Exact capacity snapshot at decision time (used = pre-decision state,
        so a rejection never shows itself in its own counter). This is what the
        dashboard/telemetry renders so an operator sees WHERE a candidate died."""
        return {
            "bucket": self.bucket,
            "bucket_used": self.bucket_used,
            "bucket_max": self.bucket_max,
            "technical_used": self.technical_used,
            "technical_max": self.technical_max,
            "news_used": self.news_used,
            "news_max": self.news_max,
            "total_used": self.total_used,
            "total_max": self.total_max,
            "class_counts": dict(sorted((self.class_counts or {}).items())),
        }


@dataclass
class PortfolioAllocationReport:
    decisions: List[AllocationDecision]
    class_bias: Dict[str, int]
    direction_bias: Dict[str, int]
    concentration: str
    unused_slots: int
    slot_reason: str
    rotation_regime: str

    def to_dict(self) -> dict:
        return {
            "decisions": [
                {
                    "symbol": d.symbol,
                    "side": d.side,
                    "asset_class": d.asset_class,
                    "score": round(d.score, 3),
                    "allowed": d.allowed,
                    "reason": d.reason,
                    "penalty": round(d.penalty, 3),
                    **d.trace_dict(),
                } for d in self.decisions
            ],
            "class_bias": self.class_bias,
            "direction_bias": self.direction_bias,
            "concentration": self.concentration,
            "unused_slots": self.unused_slots,
            "slot_reason": self.slot_reason,
            "rotation_regime": self.rotation_regime,
        }


class GlobalAssetAllocator:
    """Deterministic allocator for the queue candidates + current positions."""

    CLASS_CAPS = dict(DEFAULT_CLASS_CAPS)
    SIDE_CAPS = {"BUY": 4, "SELL": 4}

    def __init__(self, manager, engine):
        self.manager = manager
        self.engine = engine
        self._lock = threading.RLock()

    # ---------- helpers ----------
    @staticmethod
    def _classify(symbol: str, explicit: Optional[str] = None) -> str:
        return PortfolioManager._asset_class(symbol, explicit)

    # ---------- allocation ----------
    def allocate(self, candidates: Sequence[dict], limit: int = 6) -> PortfolioAllocationReport:
        """Filter candidates the risk manager/gates accept and rank them."""
        with self._lock:
            current = self.manager.count()
            list_cands = [c for c in candidates if isinstance(c, dict)]
            list_cands.sort(key=lambda c: float(c.get("priority_score", c.get("zone_score", 0.0))), reverse=True)
            decisions: List[AllocationDecision] = []
            class_bias: Dict[str, int] = {}
            bucket_bias: Dict[str, int] = {}
            direction_bias: Dict[str, int] = {}
            # Existing open positions occupy their own bias counts. Capacity is
            # enforced per COMBINED bucket (INDEX+STOCK share one seat, OIL/GOLD
            # share one seat) so buckets always agree with portfolio.can_open.
            for pos in self.manager.contexts.values():
                stored = getattr(pos, "asset_class", None)
                cls = self._classify(pos.symbol, stored)
                side = (pos.state.get("side") or "BUY").upper()
                class_bias[cls] = class_bias.get(cls, 0) + 1
                bucket_bias[bucket_of(cls)] = bucket_bias.get(bucket_of(cls), 0) + 1
                direction_bias[side] = direction_bias.get(side, 0) + 1

            max_technical = max(1, min(limit, int(os.getenv(
                "MAX_TECHNICAL_POSITIONS", str(DEFAULT_MAX_TECHNICAL_POSITIONS)))))
            chosen = 0
            for cand in list_cands:
                sym = str(cand.get("symbol", ""))
                side = str(cand.get("side", "")).upper()
                cls = self._classify(sym, cand.get("asset_class"))
                bucket = bucket_of(cls)
                score = float(cand.get("priority_score", cand.get("zone_score", 0.0)))
                allowed = True
                reason = "OK"
                # DISPLAY/TRACE snapshot BEFORE this candidate is counted, so a
                # rejection never reports its own occupation as used capacity.
                hash_bias = dict(class_bias)
                bucket_used = bucket_bias.get(bucket, 0)
                bucket_max = bucket_cap(bucket)
                technical_used = sum(v for k, v in bucket_bias.items() if k != "NEWS")
                news_used = bucket_bias.get("NEWS", 0)
                total_used = current + chosen
                if chosen + current >= limit:
                    allowed = False
                    reason = "SLOT_CAP"
                # Per-combined-bucket cap. Precise reason: CRYPTO_CAP,
                # INDEX_STOCK_CAP ("Index/Stock #3 rejected"), COMMODITY_CAP,
                # NEWS_CAP, or f"{bucket}_CAP" for an unrecognised class.
                if allowed and bucket_used >= bucket_max:
                    allowed = False
                    reason = f"{bucket}_CAP"
                # Technical budget (not reported for NEWS — news is independent).
                if allowed and cls != "NEWS" and technical_used >= max_technical:
                    allowed = False
                    reason = "TECHNICAL_CAP"
                # Per-direction cap. Env-overridable (MAX_BUY_POSITIONS /
                # MAX_SELL_POSITIONS) exactly like MAX_TECHNICAL_POSITIONS, so an
                # operator may raise the symmetric 4/4 default (e.g. a pure
                # one-direction day) without a code change. Default stays 4/4.
                if allowed:
                    default_side = self.SIDE_CAPS.get(side, 0) or 4
                    side_cap = max(1, min(limit, int(os.getenv(
                        f"MAX_{side}_POSITIONS", str(default_side)))))
                    if direction_bias.get(side, 0) >= side_cap:
                        allowed = False
                        reason = f"{side}_CAP"
                penalty = 0.0
                if allowed:
                    class_bias[cls] = class_bias.get(cls, 0) + 1
                    bucket_bias[bucket] = bucket_bias.get(bucket, 0) + 1
                    direction_bias[side] = direction_bias.get(side, 0) + 1
                    chosen += 1
                else:
                    penalty = 1.0
                decisions.append(
                    AllocationDecision(
                        sym, side, cls, score, allowed, reason, penalty,
                        bucket=bucket, bucket_used=bucket_used, bucket_max=bucket_max,
                        technical_used=technical_used, technical_max=max_technical,
                        news_used=news_used, news_max=bucket_cap("NEWS"),
                        total_used=total_used, total_max=limit,
                        class_counts=hash_bias,
                        candidate_key=cand.get("candidate_key", ""),
                    ))
            # If any slot is unused, explain why.
            total = len([d for d in decisions if d.allowed])
            unused = limit - (current + total)
            if unused > 0:
                # Find the first non-OK reason among rejected candidates
                reasons = [d.reason for d in decisions if not d.allowed]
                slot_reason = (reasons[0] if reasons else
                              "no additional candidates passed institutional + liquidity + portfolio-risk gates.")
            else:
                slot_reason = "OK"
            max_class = max(class_bias.values()) if class_bias else 0
            concentration = (
                "HIGH" if class_bias and max_class >= max(self.CLASS_CAPS.get(c, 6) for c in class_bias)
                else ("MEDIUM" if max_class >= 2 else "LOW"))
            return PortfolioAllocationReport(
                decisions=decisions, class_bias=class_bias, direction_bias=direction_bias,
                concentration=concentration, unused_slots=unused, slot_reason=slot_reason,
                rotation_regime=self._rotation(decisions))

    # ---------- rotation ----------
    def _rotation(self, decisions: List[AllocationDecision]) -> str:
        """Evidence-based rotation label from the classes actually chosen."""
        accepted = {d.asset_class for d in decisions if d.allowed}
        if not accepted:
            return "NO_FLOW"
        safe_haven = {"GOLD", "OIL", "ENERGY"}
        risk_on = {"CRYPTO"}
        if accepted & safe_haven and not (accepted & risk_on):
            return "RISK_OFF"
        if accepted & risk_on and not (accepted & safe_haven):
            return "RISK_ON"
        if len(accepted) > 1:
            return "MIXED"
        return "NEUTRAL"
