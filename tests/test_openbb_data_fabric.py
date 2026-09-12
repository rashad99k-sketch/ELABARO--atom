import time
import unittest
from data_fabric import DataFabric, ProviderRegistry, EvidenceRecord
from data_fabric.providers import DataProvider

class P(DataProvider):
    name = "TEST_PROVIDER"
    def collect(self, symbol, context=None):
        return [EvidenceRecord(symbol=symbol, family="FLOW", value=7,
                                direction="BUY", score=80, confidence=.9,
                                provider=self.name, status="LIVE", quality="HIGH")]

class DataFabricTest(unittest.TestCase):
    def test_provider_isolation_and_normalization(self):
        bus = type("Bus", (), {"publish": lambda *a, **k: None})()
        fabric = DataFabric(ProviderRegistry(), bus)
        fabric.register(P())
        snap = fabric.collect("BTC/USDT:USDT", {"timeframe":"15m"})
        self.assertEqual(snap.quality(), "HIGH")
        self.assertEqual(snap.records["FLOW"].direction, "BUY")
        self.assertIn("TEST_PROVIDER", fabric.registry.names())
        self.assertTrue(fabric.snapshot("BTC/USDT:USDT")["records"])

    def test_provider_failure_is_degraded_not_bullish(self):
        class Bad(DataProvider):
            name="BAD"
            def collect(self, symbol, context=None): raise RuntimeError("boom")
        fabric=DataFabric(); fabric.register(Bad())
        snap=fabric.collect("X")
        self.assertEqual(snap.quality(), "DEGRADED")
        self.assertEqual(snap.records["PROVIDER_ERROR:BAD"].status, "ERROR")

if __name__ == '__main__': unittest.main()
