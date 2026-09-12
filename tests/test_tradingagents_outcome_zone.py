import importlib
import os
import sys
import tempfile
import types
import unittest

import pandas as pd

from core.research_coordinator import EvidenceCoordinator
from core.trade_outcome_memory import TradeOutcomeMemory


class ResearchCoordinatorTest(unittest.TestCase):
    def test_structured_bull_bear_review_is_advisory(self):
        packet = EvidenceCoordinator().review(
            symbol="BTC/USDT:USDT", side="BUY",
            context={
                "classification": "BULLISH_OB_RETEST",
                "institutional_stage": "PRE_IGNITION",
                "structure_shift": "bullish_shift",
                "liquidity_event": "sell_side_taken",
                "ob_grade": "A",
                "zone_info": {"type": "DEMAND", "low": 99, "high": 101},
                "vol_state": "EXPANDING",
                "l2": "SUPPORTIVE",
            },
            market={"trend": "BULLISH"},
        )
        self.assertEqual(packet.side, "BUY")
        self.assertGreaterEqual(packet.evidence_count, 5)
        self.assertEqual(packet.entry_readiness, "READY_REVIEW")
        self.assertIsInstance(packet.contradictions, list)
        self.assertTrue(any(x.startswith("ob_") for x in packet.bull_case))


class OutcomeMemoryTest(unittest.TestCase):
    def test_outcome_is_idempotent_by_trade_id(self):
        with tempfile.TemporaryDirectory() as td:
            mem = TradeOutcomeMemory(os.path.join(td, "outcomes.jsonl"))
            record = {"trade_id": "TRD-1", "result": "WIN", "pnl_usdt": 12.5}
            self.assertTrue(mem.record(record))
            self.assertFalse(mem.record(record))
            self.assertTrue(mem.contains("TRD-1"))
            self.assertEqual(len(open(os.path.join(td, "outcomes.jsonl"), encoding="utf-8").read().splitlines()), 1)


class EngineOutcomeZoneWiringTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.saved_ccxt = sys.modules.get("ccxt")
        cls.saved_flask = sys.modules.get("flask")
        fake_ccxt = types.ModuleType("ccxt")

        class FakeBingX:
            def __init__(self, *args, **kwargs):
                self.markets = {"BTC/USDT:USDT": {}}

        fake_ccxt.bingx = FakeBingX
        fake_flask = types.ModuleType("flask")
        class FakeFlask:
            def __init__(self, *a, **k): pass
            def route(self, *a, **k): return lambda fn: fn
            def add_url_rule(self, *a, **k): pass
        fake_flask.Flask = FakeFlask
        fake_flask.jsonify = lambda *a, **k: None
        fake_flask.request = types.SimpleNamespace()
        sys.modules["ccxt"] = fake_ccxt
        sys.modules["flask"] = fake_flask
        sys.modules.pop("core.engine", None)
        cls.E = importlib.import_module("core.engine")

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop("core.engine", None)
        if cls.saved_ccxt is None: sys.modules.pop("ccxt", None)
        else: sys.modules["ccxt"] = cls.saved_ccxt
        if cls.saved_flask is None: sys.modules.pop("flask", None)
        else: sys.modules["flask"] = cls.saved_flask

    def test_entry_setup_snapshot_preserves_causal_zone(self):
        E = self.E
        df = pd.DataFrame({
            "open": [99, 100, 101, 100], "high": [101, 102, 103, 102],
            "low": [98, 99, 100, 99], "close": [100, 101, 102, 101],
            "volume": [100, 110, 150, 180],
        })
        snap = E._build_entry_setup_snapshot(
            "BTC/USDT:USDT", "BUY", df, 101.0, 1.0,
            {"zone_info": {"type": "DEMAND", "low": 99.0, "high": 100.5},
             "zone_low": 99.0, "zone_high": 100.5, "ob_grade": "A"},
        )
        self.assertEqual(snap["zone_type"], "DEMAND")
        self.assertEqual(snap["zone_low"], 99.0)
        self.assertEqual(snap["zone_high"], 100.5)
        self.assertEqual(snap["ob_grade"], "A")
        self.assertTrue(snap["zone_causal"])
        self.assertIn("structure_shift", snap)
        self.assertIn("liquidity_event", snap)

    def test_close_notification_uses_captured_symbol_side_pnl_and_reason(self):
        E = self.E
        sent = []
        E.send_once = lambda msg, key, cooldown=60: sent.append((msg, key))
        E.tg_close("BTC/USDT:USDT", 3.25, 12.0, "BUY", pnl_usdt=32.5, reason="TP2", trade_id="TRD-42")
        self.assertEqual(len(sent), 1)
        msg = sent[0][0]
        self.assertIn("TRADE CLOSED — WIN", msg)
        self.assertIn("BTC/USDT:USDT (BUY)", msg)
        self.assertIn("32.50 USDT", msg)
        self.assertIn("Exit: TP2", msg)
        self.assertIn("TRD-42", msg)

    def test_external_close_sync_routes_through_finalize_before_cleanup(self):
        E = self.E
        calls = []
        old_validate = E.validate_position_state
        old_finalize = E.finalize_trade_with_reality
        old_paper = E.PAPER_MODE
        old_realized = E.get_realized_pnl_for_symbol
        try:
            E.PAPER_MODE = False
            E.STATE.clear(); E.TRADE_STATE.clear()
            E.STATE.update({"open": True, "current_symbol": "BTC/USDT:USDT", "trade_id": "TRD-EXT-1", "side": "BUY", "entry": 100.0, "remaining_qty": 1.0})
            E.TRADE_STATE.update({"in_position": True, "symbol": "BTC/USDT:USDT"})
            E.validate_position_state = lambda local_pos, symbol: None
            E.finalize_trade_with_reality = lambda symbol: calls.append(symbol) or (0.0, 0.0)
            E.get_realized_pnl_for_symbol = lambda symbol, *a, **k: (0.0, 0.0)
            E.sync_all_states()
            self.assertEqual(calls, ["BTC/USDT:USDT"])
        finally:
            E.validate_position_state = old_validate
            E.finalize_trade_with_reality = old_finalize
            E.get_realized_pnl_for_symbol = old_realized
            E.PAPER_MODE = old_paper

    def test_finalize_records_complete_outcome_before_state_reset(self):
        E = self.E
        with tempfile.TemporaryDirectory() as td:
            old_mem = E.GLOBAL_TRADE_OUTCOME_MEMORY
            E.GLOBAL_TRADE_OUTCOME_MEMORY = TradeOutcomeMemory(os.path.join(td, "outcomes.jsonl"))
            E.PAPER_MODE = True
            E.STATE.clear()
            E.TRADE_STATE.clear()
            E.STATE.update({
                "open": True, "trade_id": "TRD-OUTCOME-1", "current_symbol": "BTC/USDT:USDT",
                "side": "BUY", "entry": 100.0, "qty": 10.0, "remaining_qty": 10.0,
                "qty_initial": 10.0, "margin": 100.0, "mark_price": 110.0,
                "entry_time": 1000000.0, "partial_realized": [], "peak_roe": 10.0,
                "drawdown_from_peak": 1.0, "position_setup_snapshot": {"zone_type": "DEMAND", "ob_grade": "A"},
                "research_decision_packet": {"entry_readiness": "READY_REVIEW"},
                "close_reason": "TP2",
            })
            E.paper = {"balance": 0.0, "position": None, "committed_margin": 100.0}
            E.PERF = {"total_pnl_pct": 0.0, "total_pnl_usdt": 0.0, "closed_trade_ids": {},
                      "trades": 0, "wins": 0, "losses": 0, "last_trade": None, "symbols": {}}
            E.sync_position_state = lambda symbol: (110.0, 100.0, 100.0, 10.0)
            E.send_once = lambda *a, **k: None
            try:
                pnl_usdt, pnl_pct = E.finalize_trade_with_reality("BTC/USDT:USDT")
                self.assertAlmostEqual(pnl_usdt, 100.0)
                self.assertAlmostEqual(pnl_pct, 10.0)
                self.assertFalse(E.STATE.get("open"))
                self.assertEqual(E.PERF["wins"], 1)
                self.assertEqual(E.PERF["trades"], 1)
                rows = open(os.path.join(td, "outcomes.jsonl"), encoding="utf-8").read().splitlines()
                self.assertEqual(len(rows), 1)
                self.assertIn('"result": "WIN"', rows[0])
                self.assertIn('"pnl_usdt": 100.0', rows[0])
                self.assertIn('"position_setup_snapshot"', rows[0])
            finally:
                E.GLOBAL_TRADE_OUTCOME_MEMORY = old_mem


if __name__ == "__main__":
    unittest.main()
