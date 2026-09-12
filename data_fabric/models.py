from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, Optional
import time

@dataclass(frozen=True)
class EvidenceRecord:
    symbol: str
    family: str
    value: Any = None
    direction: str = "NEUTRAL"
    score: float = 0.0
    confidence: float = 0.0
    provider: str = "UNKNOWN"
    timestamp: float = field(default_factory=time.time)
    timeframe: str = ""
    quality: str = "UNKNOWN"
    status: str = "UNKNOWN"
    no_lookahead: bool = True
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def age_sec(self) -> float:
        return max(0.0, time.time() - float(self.timestamp))

    def to_dict(self) -> dict:
        d = asdict(self)
        d["age_sec"] = round(self.age_sec, 3)
        return d

@dataclass
class EvidenceSnapshot:
    symbol: str
    records: Dict[str, EvidenceRecord] = field(default_factory=dict)
    generated_at: float = field(default_factory=time.time)

    def quality(self) -> str:
        if not self.records:
            return "UNKNOWN"
        if any(r.status in {"ERROR", "STALE", "CONFLICT"} for r in self.records.values()):
            return "DEGRADED"
        if all(r.status == "LIVE" and r.quality in {"GOOD", "HIGH"} for r in self.records.values()):
            return "HIGH"
        return "PARTIAL"

    def to_dict(self) -> dict:
        return {"symbol": self.symbol, "generated_at": self.generated_at,
                "quality": self.quality(),
                "records": {k: v.to_dict() for k, v in self.records.items()}}
