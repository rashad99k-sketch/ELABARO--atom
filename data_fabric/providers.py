from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Any, Dict, Iterable
import time

from .models import EvidenceRecord

class DataProvider(ABC):
    """Small provider contract inspired by OpenBB's independent extensions.

    Providers are evidence producers only. They cannot access execution methods.
    """
    name = "UNKNOWN"

    @abstractmethod
    def collect(self, symbol: str, context: dict | None = None) -> Iterable[EvidenceRecord]:
        raise NotImplementedError

class ProviderRegistry:
    def __init__(self):
        self._providers: Dict[str, DataProvider] = {}

    def register(self, provider: DataProvider) -> None:
        name = str(provider.name).upper()
        if not name or name == "UNKNOWN":
            raise ValueError("provider.name must be non-empty")
        self._providers[name] = provider

    def names(self):
        return tuple(sorted(self._providers))

    def collect(self, symbol: str, context: dict | None = None) -> list[EvidenceRecord]:
        out: list[EvidenceRecord] = []
        for provider in tuple(self._providers.values()):
            try:
                rows = provider.collect(symbol, context or {}) or []
                for row in rows:
                    if isinstance(row, EvidenceRecord):
                        out.append(row)
            except Exception as exc:
                out.append(EvidenceRecord(symbol=symbol, family=f"PROVIDER_ERROR:{provider.name}",
                    provider=provider.name, status="ERROR", quality="UNKNOWN",
                    metadata={"error": repr(exc)}))
        return out

class StaticContextProvider(DataProvider):
    """Adapter for existing BARON evidence dictionaries.

    This is deliberately generic so existing scanner/indicator engines can be
    exposed as providers without changing their calculation logic.
    """
    def __init__(self, name: str, family: str, key: str, direction_key: str | None = None):
        self.name = name.upper()
        self.family = family.upper()
        self.key = key
        self.direction_key = direction_key

    def collect(self, symbol: str, context: dict | None = None):
        context = context or {}
        value = context.get(self.key)
        if value is None:
            return []
        direction = str(context.get(self.direction_key, "NEUTRAL")) if self.direction_key else "NEUTRAL"
        return [EvidenceRecord(symbol=symbol, family=self.family, value=value,
            direction=direction.upper(), provider=self.name, status="LIVE", quality="GOOD",
            timestamp=float(context.get("timestamp", time.time())),
            timeframe=str(context.get("timeframe", "15m")),
            metadata={"source_key": self.key})]

class EvidenceBusProvider(DataProvider):
    """Read-only bridge from BARON's existing EvidenceBus into the fabric."""
    name = "BARON_EVIDENCE_BUS"
    def __init__(self, bus):
        self.bus = bus
    def collect(self, symbol: str, context: dict | None = None):
        if self.bus is None:
            return []
        rows = []
        for family, item in self.bus.snapshot(symbol).items():
            if not isinstance(item, dict):
                continue
            rows.append(EvidenceRecord(
                symbol=symbol, family=family, value=item.get("value"),
                direction=item.get("direction", "NEUTRAL"), score=float(item.get("score", 0) or 0),
                confidence=float(item.get("confidence", 0) or 0), provider=self.name,
                timestamp=float(item.get("timestamp", time.time()) or time.time()),
                timeframe=str(item.get("timeframe", "")), quality=str(item.get("quality", "UNKNOWN")),
                status=str(item.get("status", "UNKNOWN")), no_lookahead=bool(item.get("no_lookahead", True)),
                metadata=dict(item.get("details") or {}),
            ))
        return rows
