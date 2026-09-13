"""BARON Data Fabric: OpenBB-inspired provider/evidence normalization."""
from .models import EvidenceRecord, EvidenceSnapshot
from .providers import DataProvider, ProviderRegistry
from .fabric import DataFabric

__all__ = ["EvidenceRecord", "EvidenceSnapshot", "DataProvider", "ProviderRegistry", "DataFabric"]
