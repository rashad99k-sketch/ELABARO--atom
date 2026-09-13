"""Phase 2 (approved decisions) verification for Trade Management gaps.

Decision 1: asset class (GOLD/OIL) or trade type (REVERSAL/RETEST) is NO
longer a standalone early-profit authority. Early profit protection fires only
on correction EVIDENCE (faded continuation + structure not aligned).

Decision 2: partial closes are internal-only (bookkeeping + logs, NO Telegram).
The ONLY notification is the professional full-close message, sent after
verification, carrying the REAL close_reason derived from STATE.
"""
import copy
import os
import tempfile
import unittest

os.environ.setdefault("PAPER_MODE", "True")
os.environ.setdefault("BINGX_KEY", "")
os.environ.setdefault("BINGX_SECRET", "")
os.environ.setdefault("NEWS_ENABLED", "True")

import core.engine as E  # noqa: E402
from core.trade_outcome_memory import TradeOutcomeMemory  # noqa: E402


class Decision1EvidenceOnlyProfitProtectionTest(unittest.TestCase):
    """Rule 3 must NOT fire on asset class alone."""

    def setUp(self):
        self._manager = E.LiveTradeManager(E._event_bus, E._exchange_sync, E._recovery_guard)
        E.STATE["open"] = False
        E.STATE["tp1_hit"] = False
        E.STATE["runner_mode"] = False
        E.STATE["synthetic_sl"] = 0.0
        E.STATE["trail_stop"] = 0.0
        E.STATE["dynamic_reversal_exit_done"] = False
        E.STATE["dynamic_exhaustion_protect_done"] = False
        E.STATE["dynamic_partial_done"] = False
        E.STATE["position_health"] = 60.0
        E.STATE["position_action"] = "HOLD"
        E.STATE["position_confidence"] = 0.7
        E.STATE["market_regime"] = "EXPANSION"
        self._broker = E.close_position_full
        self._partial = E.close_partial
        self._dec = E.log_position_decision
        E.close_position_full = lambda: True
        E.close_partial = lambda ratio: None
        E.log_position_decision = lambda *a, **k: None

    def tearDown(self):
        E.close_position_full = self._broker
        E.close_partial = self._partial
        E.log_position_decision = self._dec
        E.STATE["open"] = False

    def _profile(self, symbol="BTC/USDT:USDT", classification="TREND", trade_type="TREND",
                 asset_class="CRYPTO"):
        self._manager.position_profile = E.DynamicPositionProfile(
            symbol, "BUY", 100.0, 1.0, classification=classification,
            trade_type=trade_type, asset_class=asset_class)

    def _run(self, *, trade_state, cont, structure_aligned, mom_decay=False,
             dist_risk=10, exh_risk=20, roe=3.0):
        continuation = E.ContinuationEvaluation(
            continuation_probability=cont, trend_strength=cont,
            exhaustion_probability=0.1, reclaim_risk=0.2, counter_pressure=0.1,
            confidence=0.85, reasons=["x"], should_hold=cont >= 0.62,
            hold_quality="GOOD")
        return self._manager._apply_dynamic_profit_and_exit_rules(
            symbol="BTC/USDT:USDT", mark_price=103.0, atr=1.0, side="BUY", entry=100.0,
            roe=roe, trade_state=trade_state,
            smart_money={"distribution_risk": dist_risk, "banker_pressure": 50},
            momentum={"momentum_health": 80, "exhaustion_risk": exh_risk,
                      "momentum_decay": mom_decay, "continuation_strength": 60},
            continuation_eval=continuation, structure_aligned=structure_aligned)

    def test_asset_class_alone_never_triggers_early_protection(self):
        # XAUUSD REVERSAL with aligned structure and still-healthy continuation
        # (0.55 <= cont < 0.62): the OLD asset-class factor alone fired here;
        # under Decision 1 evidence says hold, so nothing may fire.
        self._profile(symbol="XAUUSD", classification="REVERSAL",
                      trade_type="REVERSAL", asset_class="GOLD")
        closed = self._run(trade_state="TREND_RIDE", cont=0.58, structure_aligned=True)
        self.assertFalse(closed)
        self.assertFalse(E.STATE["dynamic_partial_done"])
        self.assertFalse(E.STATE["tp1_hit"])
        self.assertIsNone(E.STATE.get("close_reason"))

    def test_correction_evidence_still_protects_early(self):
        # Evidence is what matters: faded continuation + structure not aligned
        # -> early profit protection is still allowed.
        self._profile(symbol="XAUUSD", classification="REVERSAL",
                      trade_type="REVERSAL", asset_class="GOLD")
        closed = self._run(trade_state="HEALTHY_PULLBACK", cont=0.5,
                           structure_aligned=False, mom_decay=True,
                           dist_risk=20, exh_risk=25)
        self.assertFalse(closed)
        self.assertTrue(E.STATE["dynamic_partial_done"])
        self.assertEqual(E.STATE["synthetic_sl"], 100.0)
        self.assertFalse(E.STATE["tp1_hit"])

    def test_micro_pullback_and_healthy_trend_do_not_close(self):
        # pullback != exit. A shallow dip or a riding trend never triggers a
        # partial or a reversal exit.
        for kwargs in (
            dict(trade_state="HEALTHY_PULLBACK", cont=0.7, structure_aligned=True),
            dict(trade_state="TREND_RIDE", cont=0.85, structure_aligned=True),
        ):
            E.STATE["dynamic_partial_done"] = False
            E.STATE["dynamic_reversal_exit_done"] = False
            E.STATE["dynamic_exhaustion_protect_done"] = False
            self._profile(trade_type="TREND")
            closed = self._run(**kwargs)
            self.assertFalse(closed)
            self.assertFalse(E.STATE["dynamic_partial_done"])
            self.assertFalse(E.STATE["dynamic_reversal_exit_done"])

    def test_liquidity_reach_break_does_not_close(self):
        # Reaching/breaking liquidity is NOT an exit signal: a liquidity move
        # with intact continuation rides to the next liquidity.
        E.STATE["dynamic_partial_done"] = False
        E.STATE["dynamic_reversal_exit_done"] = False
        E.STATE["dynamic_exhaustion_protect_done"] = False
        self._profile(trade_type="TREND")
        closed = self._run(trade_state="TREND_RIDE", cont=0.8, structure_aligned=True)
        self.assertFalse(closed)
        self.assertFalse(E.STATE["dynamic_partial_done"])
        self.assertFalse(E.STATE["dynamic_reversal_exit_done"])


class Decision2CloseNotificationTest(unittest.TestCase):
    """The professional close message carries the REAL close_reason, rich
    evidence, and VERIFIED status built from actual STATE."""

    def setUp(self):
        self._old_mem = E.GLOBAL_TRADE_OUTCOME_MEMORY
        self._old_perf = E.PERF
        self._old_send = E.send_once
        self._old_sync = E.sync_position_state
        self._old_paper = E.PAPER_MODE
        self._old_state = copy.deepcopy(E.STATE)
        self._old_trade_state = copy.deepcopy(E.TRADE_STATE)

    def tearDown(self):
        E.GLOBAL_TRADE_OUTCOME_MEMORY = self._old_mem
        E.PERF = self._old_perf
        E.send_once = self._old_send
        E.sync_position_state = self._old_sync
        E.PAPER_MODE = self._old_paper
        E.STATE.clear()
        E.STATE.update(copy.deepcopy(self._old_state))
        E.TRADE_STATE.clear()
        E.TRADE_STATE.update(copy.deepcopy(self._old_trade_state))
        E.STATE["open"] = False

    def _reset_state(self):
        E.STATE.clear()
        E.TRADE_STATE.clear()
        E.PAPER_MODE = True
        E.STATE.update({
            "open": True, "trade_id": "TRD-P2-1", "current_symbol": "BTC/USDT:USDT",
            "side": "BUY", "entry": 100.0, "qty": 10.0, "remaining_qty": 10.0,
            "qty_initial": 10.0, "margin": 100.0, "mark_price": 104.0,
            "close_execution_price": 104.0, "entry_time": 1000000.0,
            "partial_realized": [], "peak_roe": 4.0, "tp1_hit": False,
            "runner_mode": False,
            "position_vol_state": "EXHAUSTION",
            "advisory_struct_shift": "bearish_shift",
            "smart_money": {"institutional_bias": "DISTRIBUTION", "institutional_bias_detailed": "STRONG_DISTRIBUTION"},
            "thesis_failure_score": 70,
        })
        E.paper = {"balance": 0.0, "position": None, "committed_margin": 100.0}
        E.PERF = {"total_pnl_pct": 0.0, "total_pnl_usdt": 0.0, "closed_trade_ids": {},
                  "trades": 0, "wins": 0, "losses": 0, "last_trade": None, "symbols": {},
                  "closed_trades": []}

    def test_close_message_carries_real_reason_and_verified_flag(self):
        self._reset_state()
        E.STATE["close_reason"] = "THESIS_FAILURE"
        E.GLOBAL_TRADE_OUTCOME_MEMORY = TradeOutcomeMemory(os.path.join(
            tempfile.mkdtemp(), "outcomes.jsonl"))
        E.sync_position_state = lambda symbol: (104.0, 40.0, 100.0, 4.0)
        sent = []
        E.send_once = lambda msg, key, cooldown=60: sent.append((msg, key))
        E.finalize_trade_with_reality("BTC/USDT:USDT")
        self.assertEqual(len(sent), 1)
        msg = sent[0][0]
        self.assertIn("BARON — TRADE CLOSED", msg)
        self.assertIn("THESIS_FAILURE", msg)
        self.assertIn("VERIFIED CLOSED", msg)
        self.assertIn("BTC/USDT:USDT", msg)
        self.assertIn("EXHAUSTION", msg)
        self.assertIn("BEARISH", msg)
        self.assertIn("FAILED", msg)
        self.assertIn("+4.00%", msg)
        self.assertIn("TRD-P2-1", msg)

    def test_full_close_defaults_close_reason_when_not_preset(self):
        # A full close that reaches close_position_full without an explicit
        # reason still gets a stage-derived reason (Decision 2 safety net).
        self._reset_state()
        E.GLOBAL_TRADE_OUTCOME_MEMORY = TradeOutcomeMemory(os.path.join(
            tempfile.mkdtemp(), "outcomes.jsonl"))
        E.sync_position_state = lambda symbol: (104.0, 40.0, 100.0, 4.0)
        E.send_once = lambda msg, key, cooldown=60: None
        E.close_position_full(close_price=104.0, stage="TP2")
        self.assertEqual(E.STATE.get("close_reason"), "TP2")
        self.assertFalse(E.STATE["open"])


class Decision2PartialIsInternalOnlyTest(unittest.TestCase):
    """TP1 partial banks 50% internally and sends NO Telegram message."""

    def setUp(self):
        self._old_mem = E.GLOBAL_TRADE_OUTCOME_MEMORY
        self._old_perf = E.PERF
        self._old_send = E.send_once
        self._old_paper = E.PAPER_MODE
        self._old_state = copy.deepcopy(E.STATE)
        self._old_trade_state = copy.deepcopy(E.TRADE_STATE)

    def tearDown(self):
        E.GLOBAL_TRADE_OUTCOME_MEMORY = self._old_mem
        E.PERF = self._old_perf
        E.send_once = self._old_send
        E.PAPER_MODE = self._old_paper
        E.STATE.clear()
        E.STATE.update(copy.deepcopy(self._old_state))
        E.TRADE_STATE.clear()
        E.TRADE_STATE.update(copy.deepcopy(self._old_trade_state))
        E.STATE["open"] = False

    def test_tp1_partial_is_silent_and_booked_internally(self):
        E.PAPER_MODE = True
        E.STATE.clear()
        E.TRADE_STATE.clear()
        E.STATE.update({
            "open": True, "current_symbol": "BTC/USDT:USDT", "side": "BUY",
            "entry": 100.0, "qty": 2.0, "remaining_qty": 2.0, "qty_initial": 2.0,
            "margin": 100.0, "mark_price": 105.0, "tp1_price": 105.0,
            "tp2_price": 0.0, "tp1_hit": False, "tp2_hit": False,
        })

        E.paper = {"balance": 0.0,
                   "position": {"side": "BUY", "entry": 100.0, "qty": 2.0, "remaining_qty": 2.0},
                   "committed_margin": 100.0}
        E.PERF = {"total_pnl_pct": 0.0, "total_pnl_usdt": 0.0, "closed_trade_ids": {},
                  "trades": 0, "wins": 0, "losses": 0, "last_trade": None, "symbols": {}}
        E.GLOBAL_TRADE_OUTCOME_MEMORY = TradeOutcomeMemory(os.path.join(
            tempfile.mkdtemp(), "outcomes.jsonl"))
        sent = []
        E.send_once = lambda msg, key, cooldown=60: sent.append((msg, key))

        action = E.apply_profit_engine("BTC/USDT:USDT", 105.0, None, 1, E.STATE)
        self.assertEqual(action, "TP1")
        self.assertTrue(E.STATE["tp1_hit"])
        self.assertAlmostEqual(E.STATE["remaining_qty"], 1.0, places=6)
        # internal bookkeeping happened...
        self.assertEqual(len(E.STATE.get("partial_realized", [])), 1)
        self.assertEqual(E.STATE["partial_realized"][0]["pnl_pct"], 5.0)
        # ...and NO close/TP telegram was sent for the partial
        self.assertEqual(len(sent), 0)


if __name__ == "__main__":
    unittest.main()