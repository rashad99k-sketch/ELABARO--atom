"""SURGICAL regression: READY/EXECUTION threshold alignment (single source).

Proves, through the REAL runtime executor (_execute_ready_queue_candidate) and
the REAL class-aware floor helper (_class_ready_score_floor), with only the
network/provider boundary replaced:

  1. A genuinely READY candidate scoring 71.4 (CRYPTO class floor 68) executes
     even when QUEUE_MIN_READY_SCORE=75 is present in the environment: the
     legacy global floor is no longer an independent execution gate.
  2. Exact class floors remain the authority (CRYPTO 68, GOLD 70, STOCK 67):
     a candidate at its exact floor is PASS, and nothing below the class floor
     is admitted (defensive ready_score_below_min + visible READY_REJECTED).
  3. No execution gate is weakened: news risk, allocator, non-READY blocking
     and Zone/OB Judge (execute_entry) keep their behaviour exactly as before.
"""
import os
import sys
import types
import time
import unittest

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("PAPER_MODE", "True")
os.environ.setdefault("BINGX_KEY", "")
os.environ.setdefault("BINGX_SECRET", "")
os.environ.setdefault("USE_EXECUTION_QUEUE", "True")
os.environ.setdefault("NEWS_ENABLED", "False")


class _FakeFlask:
    def __init__(self, *args, **kwargs):
        self.routes = {}

    def route(self, path, methods=None, **kwargs):
        return lambda fn: fn

    def add_url_rule(self, *args, **kwargs):
        return None


def _load_runtime():
    for name in list(sys.modules):
        if (name == "core.engine" or name == "core.runtime"
                or name.startswith("scanner.") or name.startswith("portfolio.")
                or name.startswith("strategy.")):
            sys.modules.pop(name, None)
    fake_ccxt = types.ModuleType("ccxt")

    class FakeBingX:
        def __init__(self, *args, **kwargs):
            self.markets = {
                "BTC/USDT:USDT": {"base": "BTC", "quote": "USDT", "type": "swap", "active": True},
                "ETH/USDT:USDT": {"base": "ETH", "quote": "USDT", "type": "swap", "active": True},
                "SOL/USDT:USDT": {"base": "SOL", "quote": "USDT", "type": "swap", "active": True},
            }

    fake_ccxt.bingx = FakeBingX
    fake_flask = types.ModuleType("flask")
    fake_flask.Flask = _FakeFlask
    fake_flask.jsonify = lambda *a, **k: a[0] if a else None
    fake_flask.request = types.SimpleNamespace(headers={}, remote_addr="127.0.0.1", json=None)
    sys.modules["ccxt"] = fake_ccxt
    sys.modules["flask"] = fake_flask

    import core.runtime as RT
    return RT


def _restore_modules(saved):
    sys.modules.clear()
    sys.modules.update(saved)


def _frame(n=250, side="BUY", base=100.0):
    """Synthetic frame the REAL execute_entry gates approve (ADX in class band,
    tail candle sweeps low-side liquidity for BUY -> sell_side_taken)."""
    t = np.arange(n)
    x = base + 3.0 * (1 - np.exp(-t / 900.0)) + 1.5 * np.sin(t / 6.0)
    o = x - 0.2
    c = x
    h = np.maximum(o, c) + 0.4
    l = np.minimum(o, c) - 0.4
    prior_low = l[n - 3]
    prior_hi = h[n - 3]
    if str(side).upper() == "BUY":
        o[n - 2] = prior_low - 0.2
        c[n - 2] = prior_low + 0.3
        h[n - 2] = max(prior_hi - 0.1, prior_low + 0.5)
        l[n - 2] = prior_low - 1.2
        o[n - 1] = prior_low + 0.1
        c[n - 1] = prior_low + 0.9
        h[n - 1] = prior_low + 1.3
        l[n - 1] = prior_low - 0.1
    else:
        o[n - 2] = prior_hi - 0.3
        c[n - 2] = prior_hi - 0.2
        l[n - 2] = max(prior_low + 0.1, prior_hi - 0.5)
        h[n - 2] = prior_hi + 1.2
        o[n - 1] = prior_hi - 0.1
        c[n - 1] = prior_hi - 0.9
        h[n - 1] = prior_hi + 0.1
        l[n - 1] = prior_hi - 1.3
    return pd.DataFrame({
        "timestamp": t, "open": o, "high": h, "low": l, "close": c,
        "volume": np.full(n, 1000.0),
    })


class ReadyExecutionAlignmentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._saved_modules = sys.modules.copy()
        cls.RT = _load_runtime()

    @classmethod
    def tearDownClass(cls):
        _restore_modules(cls._saved_modules)

    def setUp(self):
        RT = self.RT
        E = RT.E
        self._saved = (E.get_ohlcv_safe, E.get_ticker_safe,
                       E.get_orderbook_cached, E.get_balance_safe)
        self._prices = {"BTC/USDT:USDT": 60000.0, "ETH/USDT:USDT": 3000.0,
                        "SOL/USDT:USDT": 150.0, "XAU/USDT:USDT": 2600.0,
                        "AAPL/USDT:USDT": 200.0}
        self._frames = {s: _frame(base=p) for s, p in self._prices.items()}

        def provider(symbol, limit=120, htf=False):
            return self._frames.get(str(symbol))

        E.get_ohlcv_safe = provider
        E.get_ticker_safe = lambda symbol, retries=3: self._prices.get(str(symbol), 100.0)
        E.get_orderbook_cached = lambda *a, **k: {
            "bids": [[self._prices.get(str(a[0]), 100.0) - 1.0, 10.0]],
            "asks": [[self._prices.get(str(a[0]), 100.0) + 1.0, 5.0]]}
        E.get_balance_safe = lambda retries=3: E.paper["balance"]

        E.paper = {"balance": 10000.0, "position": None, "committed_margin": 0.0}
        E.MEMORY.clear()
        E.MEMORY["pipeline"] = {"execution": {}}
        E.STATE.clear()
        E.TRADE_STATE.clear()
        E.STATE["daily_loss_limit_hit"] = False
        E.STATE["last_trade_day"] = time.strftime("%Y-%m-%d")
        E.STATE["daily_peak_balance"] = E.paper["balance"]
        E.queue._candidates.clear()
        E.queue.total_executed = 0
        E.queue.total_rejected = 0

        from portfolio.manager import PortfolioManager
        from portfolio.allocator import GlobalAssetAllocator
        RT.PORTFOLIO = PortfolioManager(6, E)
        RT.ALLOCATOR = GlobalAssetAllocator(RT.PORTFOLIO, E)
        RT.PORTFOLIO.bind(E)
        RT.PORTFOLIO.risk_guard._day = None
        RT.PORTFOLIO.risk_guard._consecutive_losses = 0
        RT.PORTFOLIO.risk_guard._cooldown_until = 0.0

    def tearDown(self):
        RT = self.RT
        E = RT.E
        (E.get_ohlcv_safe, E.get_ticker_safe,
         E.get_orderbook_cached, E.get_balance_safe) = self._saved

    def _govern_watch(self, symbol, asset_class="CRYPTO", news_risk=0):
        RT = self.RT
        RT.E.MEMORY.setdefault("watchlist", {})[symbol] = {
            "symbol": symbol, "side": "BUY", "news_risk": news_risk,
            "asset_class": asset_class,
        }

    def _ready_candidate(self, symbol, score, ready_floor=None, news_risk=0):
        E = self.RT.E
        price = self._prices[symbol]
        atr = price * 0.01
        cand = E.ExecutionCandidate(
            symbol=symbol, side="BUY", price=price,
            entry_price=price - atr * 0.5, stop_loss=price - atr * 1.6,
            take_profit_1=price + atr * 1.5, take_profit_2=price + atr * 2.5,
            atr=atr, df=self._frames[symbol], ob={},
        )
        cand.priority_score = float(score)
        cand.state = E.ExecutionState.READY
        cand.confirmation_count = 2
        cand.confirmation_state = "CONFIRMED_2"
        cand.confirmation_reason = "CONFIRMATION_COMPLETE"
        cand.ready_time = time.time()
        cand.ready_blocker = "NONE"
        cand.institutional_score = 85.0
        cand.pre_institutional_state = "CONFIRMED"
        cand.zone_low = price - atr * 0.6
        cand.zone_high = price + atr * 0.4
        cand.entry_distance_atr = 0.4
        cand.opportunity_type = E.OpportunityType.ACCUMULATION_ENTRY
        cand.asset_class = "CRYPTO"
        if ready_floor is not None:
            cand.ready_score_required = float(ready_floor)
        self._govern_watch(symbol, news_risk=news_risk)
        self.assertTrue(E.queue.add_candidate(cand), f"admit {symbol}")
        E.queue._record_opportunity_lifecycle(cand)
        return cand

    # ---- Test 1: live realistic case 71.4 CRYPTO(68) despite env 75 ----
    def test_ready_71_4_executes_despite_legacy_env_75(self):
        RT = self.RT
        E = RT.E
        os.environ["QUEUE_MIN_READY_SCORE"] = "75"
        try:
            self._ready_candidate("BTC/USDT:USDT", score=71.4, ready_floor=68.0)
            self.assertTrue(RT._execute_ready_queue_candidate())
        finally:
            os.environ.pop("QUEUE_MIN_READY_SCORE", None)

        out = RT.MEMORY.setdefault("pipeline", {}).setdefault("execution", {})
        self.assertEqual(out.get("last_outcome"), "executed")
        self.assertEqual(out.get("ready_score_below_min", 0), 0,
                         "71.4 must not be killed by the legacy 75 floor")
        self.assertEqual(RT.PORTFOLIO.count(), 1)

    # ---- Test 2: exact CRYPTO floor ----
    def test_ready_at_exact_crypto_floor_68_executes(self):
        RT = self.RT
        E = RT.E
        cand = self._ready_candidate("BTC/USDT:USDT", score=68.0, ready_floor=68.0)
        self.assertTrue(RT._execute_ready_queue_candidate())
        out = RT.MEMORY.setdefault("pipeline", {}).setdefault("execution", {})
        self.assertEqual(out.get("last_outcome"), "executed")
        self.assertEqual(cand.state, E.ExecutionState.EXECUTED)

    # ---- Test 3: GOLD 70 passes score gate; the only blocker is NEWS ----
    def test_ready_gold_70_passes_score_gate_then_news_blocks(self):
        RT = self.RT
        E = RT.E
        self._ready_candidate("XAU/USDT:USDT", score=70.0, ready_floor=70.0,
                              news_risk=85)
        rejected = RT._execute_ready_queue_candidate()
        self.assertFalse(rejected)
        out = RT.MEMORY.setdefault("pipeline", {}).setdefault("execution", {})
        self.assertEqual(out.get("last_outcome"), "news_risk_block",
                         "71.4-/70-class floor passes; only NEWS may block now")
        self.assertEqual(out.get("ready_score_below_min", 0), 0)

    # ---- Test 4: STOCK 67 exact floor - no forced execution ----
    def test_stock_67_exact_floor_not_force_executed(self):
        RT = self.RT
        E = RT.E
        self._ready_candidate("AAPL/USDT:USDT", score=67.0, ready_floor=67.0,
                              news_risk=85)
        rejected = RT._execute_ready_queue_candidate()
        self.assertFalse(rejected)
        out = RT.MEMORY.setdefault("pipeline", {}).setdefault("execution", {})
        self.assertEqual(out.get("last_outcome"), "news_risk_block",
                         "exact-class floor is PASS; execution still blocked by a real gate")
        self.assertEqual(out.get("ready_score_below_min", 0), 0)

    # ---- Test 5: below class floor still rejected and VISIBLE ----
    def test_ready_below_class_floor_still_rejected_readable(self):
        RT = self.RT
        E = RT.E
        self._ready_candidate("SOL/USDT:USDT", score=66.5, ready_floor=68.0)
        rejected = RT._execute_ready_queue_candidate()
        self.assertFalse(rejected)
        out = RT.MEMORY.setdefault("pipeline", {}).setdefault("execution", {})
        self.assertEqual(out.get("last_outcome"), "ready_score_below_min")
        self.assertEqual(RT.PORTFOLIO.count(), 0)
        feed = RT.MEMORY.get("gate_feed", [])
        self.assertTrue(any(ev.get("blocker") == "READY_REJECTED"
                            and ev.get("symbol") == "SOL/USDT:USDT"
                            and "required=68.0" in ev.get("detail", "")
                            for ev in feed),
                        "READY_REJECTED must be visible in existing telemetry")

    # ---- Test 6: allocator still gated ----
    def test_allocator_still_gates_third_ready_71_4(self):
        RT = self.RT
        E = RT.E
        self._ready_candidate("BTC/USDT:USDT", score=71.4, ready_floor=68.0)
        self.assertTrue(RT._execute_ready_queue_candidate())
        RT.E.STATE["daily_peak_balance"] = RT.E.paper["balance"]
        self._ready_candidate("ETH/USDT:USDT", score=71.4, ready_floor=68.0)
        self.assertTrue(RT._execute_ready_queue_candidate())
        RT.E.STATE["daily_peak_balance"] = RT.E.paper["balance"]
        self.assertEqual(RT.PORTFOLIO.count(), 2)

        self._ready_candidate("SOL/USDT:USDT", score=71.4, ready_floor=68.0)
        rejected = RT._execute_ready_queue_candidate()
        self.assertFalse(rejected)
        out = RT.MEMORY.setdefault("pipeline", {}).setdefault("execution", {})
        self.assertEqual(out.get("last_outcome"), "allocator_reject",
                         "READY score gate passes at 71.4; ALLOCATOR still decides")
        self.assertEqual(out.get("ready_score_below_min", 0), 0)
        self.assertEqual(RT.PORTFOLIO.count(), 2, "no 7th/third crypto slot")

    # ---- Test 7: non-READY must not be promoted ----
    def test_non_ready_without_confirmed_trigger_not_promoted(self):
        RT = self.RT
        E = RT.E
        cand = self._ready_candidate("BTC/USDT:USDT", score=71.4, ready_floor=68.0)
        cand.state = E.ExecutionState.WAITING_TRIGGER
        cand.ready_time = 0.0
        executed = RT._execute_ready_queue_candidate()
        self.assertFalse(executed)
        out = RT.MEMORY.setdefault("pipeline", {}).setdefault("execution", {})
        self.assertEqual(out.get("last_outcome"), "no_ready_candidate")
        self.assertEqual(RT.PORTFOLIO.count(), 0)

    # ---- Test 8: floor helper = class-ready authority (single source) ----
    def test_floor_helper_single_source_of_truth(self):
        RT = self.RT
        E = RT.E
        prof = E.AssetBehaviorProfile.entry_config
        self.assertEqual(prof("CRYPTO")["ready_score"], 68.0)
        self.assertEqual(prof("GOLD")["ready_score"], 70.0)
        self.assertEqual(prof("STOCK")["ready_score"], 67.0)
        self.assertEqual(prof("OIL")["ready_score"], 70.0)
        self.assertEqual(prof("NEWS")["ready_score"], 70.0)

        btc = self._ready_candidate("BTC/USDT:USDT", score=71.4, ready_floor=68.0)
        self.assertEqual(RT._class_ready_score_floor(btc), 68.0)
        btc.ready_score_required = None
        self.assertEqual(RT._class_ready_score_floor(btc), 68.0, "resolver -> CRYPTO")


if __name__ == "__main__":
    unittest.main(verbosity=2)