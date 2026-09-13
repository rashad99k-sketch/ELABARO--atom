"""Deterministic evidence/provenance registry for BARON.

Evidence is observational only.  It never places orders and never turns an
UNKNOWN value into a bullish/bearish value.  The registry is intentionally
small so it can sit beside the preserved engine without becoming a second
trading brain.
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
import time
from typing import Any, Dict, Optional

@dataclass
class Evidence:
    family: str
    value: Any = None
    direction: str = "NEUTRAL"
    score: float = 0.0
    confidence: float = 0.0
    source: str = "UNKNOWN"
    timestamp: float = 0.0
    timeframe: str = ""
    freshness_sec: float = 0.0
    quality: str = "UNKNOWN"
    status: str = "UNKNOWN"
    no_lookahead: bool = True
    details: Dict[str, Any] | None = None

    def to_dict(self):
        d = asdict(self)
        d["details"] = dict(self.details or {})
        return d

class EvidenceBus:
    def __init__(self, max_per_symbol: int = 128):
        self.max_per_symbol = max(16, int(max_per_symbol))
        self._data: Dict[str, Dict[str, Evidence]] = {}

    def publish(self, symbol: str, family: str, *, value=None, direction="NEUTRAL",
                score=0.0, confidence=0.0, source="UNKNOWN", timestamp=None,
                timeframe="", freshness_sec=None, quality="UNKNOWN", status="UNKNOWN",
                no_lookahead=True, details=None) -> Evidence:
        now = time.time()
        ts = float(timestamp if timestamp is not None else now)
        age = max(0.0, now - ts) if freshness_sec is None else max(0.0, float(freshness_sec))
        ev = Evidence(str(family).upper(), value, str(direction).upper(), float(score or 0),
                      max(0.0, min(1.0, float(confidence or 0))), str(source), ts,
                      str(timeframe), age, str(quality).upper(), str(status).upper(),
                      bool(no_lookahead), dict(details or {}))
        bucket = self._data.setdefault(str(symbol), {})
        bucket[ev.family] = ev
        if len(bucket) > self.max_per_symbol:
            oldest = sorted(bucket.items(), key=lambda kv: kv[1].timestamp)[:-self.max_per_symbol]
            for key, _ in oldest:
                bucket.pop(key, None)
        return ev

    def snapshot(self, symbol: str) -> Dict[str, dict]:
        return {k: v.to_dict() for k, v in self._data.get(str(symbol), {}).items()}

    def latest(self, symbol: str, family: str) -> Optional[Evidence]:
        return self._data.get(str(symbol), {}).get(str(family).upper())

    def quality(self, symbol: str) -> str:
        vals = list(self._data.get(str(symbol), {}).values())
        if not vals:
            return "UNKNOWN"
        if any(v.status in {"STALE", "ERROR", "CONFLICT"} for v in vals):
            return "DEGRADED"
        if any(v.status == "LIVE" and v.quality in {"GOOD", "HIGH"} for v in vals):
            return "GOOD"
        return "PARTIAL"
