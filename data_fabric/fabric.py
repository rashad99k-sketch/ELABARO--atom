from __future__ import annotations
from typing import Dict
from .models import EvidenceRecord, EvidenceSnapshot
from .providers import DataProvider, ProviderRegistry

class DataFabric:
    """Provider-agnostic evidence layer.

    It normalizes external/internal data into records and can mirror the
    existing BARON EvidenceBus. It never decides BUY/SELL and never executes.
    """
    def __init__(self, registry: ProviderRegistry | None = None, evidence_bus=None):
        self.registry = registry or ProviderRegistry()
        self.evidence_bus = evidence_bus
        self._latest: Dict[str, EvidenceSnapshot] = {}

    def register(self, provider: DataProvider):
        self.registry.register(provider)
        return provider

    def collect(self, symbol: str, context: dict | None = None) -> EvidenceSnapshot:
        rows = self.registry.collect(symbol, context)
        snap = EvidenceSnapshot(symbol=symbol, records={r.family: r for r in rows})
        self._latest[symbol] = snap
        if self.evidence_bus is not None:
            for r in rows:
                self.evidence_bus.publish(symbol, r.family, value=r.value,
                    direction=r.direction, score=r.score, confidence=r.confidence,
                    source=r.provider, timestamp=r.timestamp, timeframe=r.timeframe,
                    quality=r.quality, status=r.status, no_lookahead=r.no_lookahead,
                    details=r.metadata)
        return snap

    def snapshot(self, symbol: str) -> dict:
        snap = self._latest.get(symbol)
        return snap.to_dict() if snap else {"symbol": symbol, "records": {}, "quality": "UNKNOWN"}
