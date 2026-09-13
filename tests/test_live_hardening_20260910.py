import os
import time
import types
import unittest
from unittest.mock import patch

import pandas as pd

import core.engine as E
from portfolio.manager import PortfolioManager, PositionContext
from portfolio.allocator import GlobalAssetAllocator


class _DummyEngine:
    pass


def _ctx(symbol, asset_class):
    return PositionContext(symbol=symbol, state={"open": True, "side": "BUY"},
                           trade_state={"in_position": True}, live_manager=None,
                           asset_class=asset_class, opened_at=time.time())


class OrderBookHardeningTest(unittest.TestCase):
    def setUp(self):
        self.old_fetch = E.fetch_orderbook
        self.old_cache = dict(E._ORDERBOOK_CACHE)
        self.old_quality = dict(E._ORDERBOOK_QUALITY)
        E._ORDERBOOK_CACHE.clear()
        E._ORDERBOOK_QUALITY.clear()

    def tearDown(self):
        E.fetch_orderbook = self.old_fetch
        E._ORDERBOOK_CACHE.clear(); E._ORDERBOOK_CACHE.update(self.old_cache)
        E._ORDERBOOK_QUALITY.clear(); E._ORDERBOOK_QUALITY.update(self.old_quality)

    def test_position_open_cache_miss_still_refreshes(self):
        calls = []
        E.STATE["open"] = True
        E.TRADE_STATE["in_position"] = True
        def fetch(symbol, limit=20):
            calls.append((symbol, limit))
            return {"bids": [[100.0, 10.0]], "asks": [[101.0, 5.0]]}
        E.fetch_orderbook = fetch
        ob = E.get_orderbook_cached("BTC/USDT:USDT", 10)
        self.assertIsNotNone(ob)
        self.assertEqual(len(calls), 1)
        self.assertEqual(E.get_orderbook_quality("BTC/USDT:USDT")["status"], "ORDERBOOK_OK")

    def test_cache_is_per_symbol(self):
        calls = []
        def fetch(symbol, limit=20):
            calls.append(symbol)
            return {"bids": [[100.0, 10.0]], "asks": [[101.0, 5.0]]}
        E.fetch_orderbook = fetch
        E.get_orderbook_cached("BTC/USDT:USDT", 10)
        E.get_orderbook_cached("ETH/USDT:USDT", 10)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], "BTC/USDT:USDT")
        self.assertEqual(calls[1], "ETH/USDT:USDT")

    def test_stale_is_explicit_not_ok(self):
        E._ORDERBOOK_CACHE["BTC/USDT:USDT_10"] = {
            "value": {"bids": [[100, 10]], "asks": [[101, 5]]},
            "ts": time.time() - E.ORDERBOOK_CACHE_TTL_SEC - 1,
        }
        E.fetch_orderbook = lambda *a, **k: None
        ob = E.get_orderbook_cached("BTC/USDT:USDT", 10)
        self.assertIsNotNone(ob)
        self.assertEqual(E.get_orderbook_quality("BTC/USDT:USDT")["status"], "ORDERBOOK_STALE")

    def test_no_orderbook_is_not_zero_imbalance(self):
        from scanner.deep_scanner import DeepScanner
        self.assertIsNone(DeepScanner._orderbook_imbalance(None))
        self.assertEqual(DeepScanner._orderbook_imbalance({"bids": [[1, 5]], "asks": [[2, 5]]}), 0.0)


class PortfolioCapacityHardeningTest(unittest.TestCase):
    def setUp(self):
        self.old = os.environ.pop("MAX_POSITIONS_PER_ASSET_CLASS", None)
        self.pm = PortfolioManager(6, None)
        self.pm.risk_guard.can_open = lambda *a, **k: True
        self.pm.contexts = {
            "C1": _ctx("C1", "CRYPTO"),
            "C2": _ctx("C2", "CRYPTO"),
            "I1": _ctx("I1", "INDEX"),
            "I2": _ctx("I2", "INDEX"),
            "G1": _ctx("G1", "GOLD"),
        }

    def tearDown(self):
        if self.old is not None:
            os.environ["MAX_POSITIONS_PER_ASSET_CLASS"] = self.old

    def test_five_technical_plus_one_news(self):
        self.assertFalse(self.pm.can_open("OIL2", "OIL"))
        self.assertTrue(self.pm.can_open("NEWS_ASSET", "NEWS"))
        self.pm.contexts["N1"] = _ctx("NEWS_ASSET", "NEWS")
        self.assertFalse(self.pm.can_open("NEWS2", "NEWS"))
        self.assertFalse(self.pm.can_open("TECH6", "GOLD"))

    def test_news_does_not_consume_technical_slot(self):
        self.pm.contexts["N1"] = _ctx("NEWS_ASSET", "NEWS")
        self.assertFalse(self.pm.can_open("TECH6", "GOLD"))
        self.assertFalse(self.pm.can_open("TECH6", "OIL"))


class AllocatorCapacityHardeningTest(unittest.TestCase):
    def test_rejected_candidates_do_not_consume_total_slot(self):
        pm = PortfolioManager(6, None)
        pm.risk_guard.can_open = lambda *a, **k: True
        pm.contexts = {"C1": _ctx("C1", "CRYPTO")}
        alloc = GlobalAssetAllocator(pm, None)
        candidates = [
            {"symbol": "BAD1", "side": "BUY", "asset_class": "STOCK", "priority_score": 100},
            {"symbol": "GOOD", "side": "BUY", "asset_class": "CRYPTO", "priority_score": 90},
        ]
        report = alloc.allocate(candidates, limit=6)
        good = next(d for d in report.decisions if d.symbol == "GOOD")
        self.assertTrue(good.allowed)


class PositionSyncSafetyTest(unittest.TestCase):
    def test_unknown_snapshot_does_not_force_local_close(self):
        old = E._exchange_sync.fetch_live_snapshot
        old_paper = E.PAPER_MODE
        try:
            E.PAPER_MODE = False
            E.STATE.update({"open": True, "current_symbol": "BTC/USDT:USDT"})
            E._exchange_sync.fetch_live_snapshot = lambda symbol: None
            result = E.sync_position_state("BTC/USDT:USDT")
            self.assertIsNone(result[0])
            self.assertTrue(E.STATE.get("open"))
            self.assertEqual(E.STATE.get("position_sync_status"), "UNKNOWN")
        finally:
            E._exchange_sync.fetch_live_snapshot = old
            E.PAPER_MODE = old_paper


if __name__ == "__main__":
    unittest.main()
