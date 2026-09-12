import os
import unittest

from core.orderbook_heatmap import L2HeatmapEngine
from portfolio.manager import PortfolioManager
from portfolio.allocator import DEFAULT_CLASS_CAPS
from core.engine import UnifiedTradeManagementBrain


class ReleaseBlockerRepairsTest(unittest.TestCase):
    def test_l2_persistence_and_spoof_proxy(self):
        e = L2HeatmapEngine(max_snapshots=8, wall_multiple=2.0)
        ob1 = {"bids": [[100, 100], [99, 10], [98, 10]], "asks": [[101, 10], [102, 10], [103, 10]]}
        ob2 = {"bids": [[100.01, 100], [99, 10], [98, 10]], "asks": [[101, 10], [102, 10], [103, 10]]}
        r1 = e.update("BTC/USDT:USDT", ob1)
        r2 = e.update("BTC/USDT:USDT", ob2)
        self.assertEqual(r1["snapshots"], 1)
        self.assertGreaterEqual(r2["bid_persistence"], 1)
        self.assertEqual(r2["wall_evidence"], "PERSISTENT")

    def test_expanded_classes_are_executable_but_news_is_singleton(self):
        self.assertEqual(DEFAULT_CLASS_CAPS["STOCK"], 1)
        self.assertEqual(DEFAULT_CLASS_CAPS["ENERGY"], 1)
        self.assertEqual(DEFAULT_CLASS_CAPS["METALS"], 1)
        self.assertEqual(PortfolioManager._class_cap("NEWS"), 1)

    def test_global_capacity_is_five_technical_plus_one_news(self):
        pm = PortfolioManager(6, None)
        self.assertEqual(pm.max_positions, 6)
        self.assertEqual(pm.max_technical_positions, 5)

    def test_scanner_contract_defaults_are_20m_and_40(self):
        from scanner.deep_scanner import DeepScanner
        d = DeepScanner(max_symbols=60, market_loader=lambda: {})
        self.assertEqual(d.discovery_interval, 1200.0)
        self.assertEqual(d.watchlist_limit, 40)
        self.assertEqual(d.radar_target, 40)

    def test_unified_management_brain_vetoes_weak_close_on_healthy_trend(self):
        b = UnifiedTradeManagementBrain()
        d = b.decide(proposed="CLOSE", reason="weak_provider",
                     continuation_probability=0.85, distribution_risk=20,
                     roe=12, structure_aligned=True)
        self.assertEqual(d["action"], "PROTECT")

    def test_unified_management_brain_authorizes_hard_failure(self):
        b = UnifiedTradeManagementBrain()
        d = b.decide(proposed="CLOSE", reason="hard_failure",
                     continuation_probability=0.20, distribution_risk=90,
                     roe=5, structure_aligned=False, hard_failure=True)
        self.assertEqual(d["action"], "CLOSE")


if __name__ == "__main__":
    unittest.main()
