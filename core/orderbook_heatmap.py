"""L2 persistence / anti-spoof evidence engine.

This module is deliberately advisory: it never places or closes an order.  It
turns repeated order-book snapshots into persistent wall, absorption, vacuum,
and spoof-risk evidence.  The exchange remains the source of truth for orders.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
import math
import time
from typing import Deque, Dict, List, Tuple


@dataclass
class L2Snapshot:
    ts: float
    bids: List[Tuple[float, float]]
    asks: List[Tuple[float, float]]


class L2HeatmapEngine:
    def __init__(self, max_snapshots: int = 24, wall_multiple: float = 3.0,
                 level_tolerance_bps: float = 8.0, stale_after_sec: float = 15.0):
        self.max_snapshots = max(4, int(max_snapshots))
        self.wall_multiple = max(1.5, float(wall_multiple))
        self.level_tolerance_bps = max(1.0, float(level_tolerance_bps))
        self.stale_after_sec = max(1.0, float(stale_after_sec))
        self.history: Dict[str, Deque[L2Snapshot]] = defaultdict(lambda: deque(maxlen=self.max_snapshots))

    @staticmethod
    def _clean(rows, limit=20):
        out = []
        for row in list(rows or [])[:limit]:
            try:
                p, q = float(row[0]), float(row[1])
                if p > 0 and q > 0 and math.isfinite(p) and math.isfinite(q):
                    out.append((p, q))
            except Exception:
                continue
        return out

    def _same_level(self, p1: float, p2: float) -> bool:
        mid = max((p1 + p2) / 2.0, 1e-12)
        return abs(p1 - p2) / mid * 10000.0 <= self.level_tolerance_bps

    def update(self, symbol: str, orderbook: dict, *, limit: int = 20, now: float | None = None) -> dict:
        now = time.time() if now is None else float(now)
        bids = self._clean(orderbook.get("bids"), limit)
        asks = self._clean(orderbook.get("asks"), limit)
        if not bids or not asks:
            return self.snapshot(symbol)
        snap = L2Snapshot(now, bids, asks)
        hist = self.history[str(symbol)]
        hist.append(snap)
        return self._analyze(symbol, now)

    def _side_stats(self, rows: List[Tuple[float, float]], previous: List[Tuple[float, float]]):
        sizes = [q for _, q in rows]
        median = sorted(sizes)[len(sizes) // 2] if sizes else 0.0
        threshold = max(median * self.wall_multiple, 1e-12)
        walls = [(p, q) for p, q in rows if q >= threshold]
        persistent = []
        disappeared = []
        for p, q in walls:
            seen = 0
            for oldp, oldq in previous:
                if self._same_level(p, oldp):
                    seen += 1
            if seen:
                persistent.append((p, q, seen))
        for oldp, oldq in previous:
            if oldq >= threshold and not any(self._same_level(oldp, p) for p, _ in rows):
                disappeared.append((oldp, oldq))
        return median, walls, persistent, disappeared

    def _analyze(self, symbol: str, now: float) -> dict:
        hist = self.history[str(symbol)]
        current = hist[-1]
        previous = hist[-2] if len(hist) >= 2 else None
        bid_med, bid_walls, bid_persistent, bid_gone = self._side_stats(current.bids, previous.bids if previous else [])
        ask_med, ask_walls, ask_persistent, ask_gone = self._side_stats(current.asks, previous.asks if previous else [])
        bid_persist = max((x[2] for x in bid_persistent), default=0)
        ask_persist = max((x[2] for x in ask_persistent), default=0)
        # A wall that vanishes immediately after being identified is a spoof-risk
        # proxy. It is evidence, not proof, because public L2 cannot reveal trader intent.
        bid_spoof = min(1.0, len(bid_gone) / max(1.0, len(bid_walls) + len(bid_gone)))
        ask_spoof = min(1.0, len(ask_gone) / max(1.0, len(ask_walls) + len(ask_gone)))
        bid_qty = sum(q for _, q in current.bids)
        ask_qty = sum(q for _, q in current.asks)
        total = bid_qty + ask_qty
        imbalance = (bid_qty - ask_qty) / total if total else 0.0
        absorption = bool(bid_persistent or ask_persistent) and abs(imbalance) < 0.20
        vacuum = (not bid_walls and not ask_walls) and total > 0 and len(hist) >= 2
        score = 50.0 + imbalance * 30.0
        score += min(15.0, bid_persist * 1.5) - min(15.0, ask_persist * 1.5)
        score -= bid_spoof * 10.0
        score += ask_spoof * 10.0
        quality = "FRESH" if now - current.ts <= self.stale_after_sec else "STALE"
        return {
            "symbol": symbol,
            "quality": quality,
            "snapshots": len(hist),
            "imbalance": imbalance,
            "bid_walls": [{"price": p, "size": q, "persistence": n} for p, q, n in bid_persistent],
            "ask_walls": [{"price": p, "size": q, "persistence": n} for p, q, n in ask_persistent],
            "bid_wall_count": len(bid_walls),
            "ask_wall_count": len(ask_walls),
            "bid_persistence": bid_persist,
            "ask_persistence": ask_persist,
            "bid_spoof_risk": round(bid_spoof, 3),
            "ask_spoof_risk": round(ask_spoof, 3),
            "absorption": absorption,
            "liquidity_vacuum": vacuum,
            "heatmap_score": round(max(0.0, min(100.0, score)), 2),
            "wall_evidence": "PERSISTENT" if max(bid_persist, ask_persist) >= 1 else ("TRANSIENT" if bid_walls or ask_walls else "NONE"),
        }

    def snapshot(self, symbol: str) -> dict:
        hist = self.history.get(str(symbol))
        if not hist:
            return {"symbol": symbol, "quality": "NO_DATA", "snapshots": 0, "heatmap_score": 50.0}
        return self._analyze(str(symbol), time.time())

    def clear(self):
        self.history.clear()
