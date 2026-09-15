"""Six-position paper test through the REAL runtime authority.

The forensic root cause was that the queue granted READY but the BARON ZONE/OB
judge (core/engine.py) silently killed every candidate with WAIT_RETEST under
the production default BARON_ZONE_JUDGE=1, so EXECUTED stayed 0. This file
drives the ACTUAL production loop path — core.runtime._execute_ready_queue_candidate
for technical candidates and core.runtime.execute_news_slot for the independent
NEWS slot — with the judge forced ON, while every other gate stays real
(allocator / class caps / portfolio cap / risk guard / sizing / lifecycle).

Covered:
  1. Six positions open at once through the runtime: 2 CRYPTO + 2 INDEX +
     1 GOLD (technical) + 1 NEWS, judge assertions that WAIT_RETEST was
     advisory and no BARON_REJECT fired.
  2. 6th technical refused -> user-facing TECHNICAL_CAPACITY_FULL.
  3. 2nd NEWS attempt refused -> user-facing NEWS_SLOT_FULL (news independence:
     a NEWS trade never consumes a technical class quota).
  4. 7th position refused -> user-facing TOTAL_PORTFOLIO_CAPACITY_FULL.
"""
import os
import sys
import types
import time
import unittest

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ.setdefault("PAPER_MODE", "True")
os.environ.setdefault("BINGX_KEY", "")
os.environ.setdefault("BINGX_SECRET", "")
os.environ.setdefault("USE_EXECUTION_QUEUE", "True")
os.environ.setdefault("NEWS_ENABLED", "True")
os.environ.setdefault("NEWS_SLOT_ENABLED", "True")
os.environ["BARON_ZONE_JUDGE"] = "1"  # production default


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
            self.markets = {}

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


def _frame(n=250, base=100.0, side="BUY"):
    t = np.arange(n)
    x = base + 3.0 * (1 - np.exp(-t / 900.0)) + 1.5 * np.sin(t / 6.0)
    o = x - 0.2
    c = x
    h = np.maximum(o, c) + 0.4
    l = np.minimum(o, c) - 0.4
    prior_low = l[n - 3]
    prior_hi = h[n - 3]
    o[n - 2] = prior_low - 0.2
    c[n - 2] = prior_low + 0.3
    h[n - 2] = max(prior_hi - 0.1, prior_low + 0.5)
    l[n - 2] = prior_low - 1.2
    o[n - 1] = prior_low + 0.1
    c[n - 1] = prior_low + 0.9
    h[n - 1] = prior_low + 1.3
    l[n - 1] = prior_low - 0.1
    return pd.DataFrame({"timestamp": t, "open": o, "high": h,
                         "low": l, "close": c, "volume": np.full(n, 1000.0)})


PRICES = {
    "BTC/USDT:USDT": 60000.0,
    "ETH/USDT:USDT": 3000.0,
    "US500/USDT:USDT": 5000.0,
    "USTECH/USDT:USDT": 17000.0,
    "XAUUSD": 2300.0,
    "OIL/USDT:USDT": 80.0,
    "SOL/USDT:USDT": 150.0,
    "NCSKNVDA2USD/USDT:USDT": 130.0,
}

CLASS_BY_SYMBOL = {
    "BTC/USDT:USDT": "CRYPTO",
    "ETH/USDT:USDT": "CRYPTO",
    "US500/USDT:USDT": "INDEX",
    "USTECH/USDT:USDT": "INDEX",
    "XAUUSD": "GOLD",
    "OIL/USDT:USDT": "OIL",
    "SOL/USDT:USDT": "CRYPTO",
}


class SixSlotRuntimeBaronTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._saved_modules = sys.modules.copy()
        cls.RT = _load_runtime()
        # Connect the runtime numerator to the SAME portfolio under test.
        from portfolio.manager import PortfolioManager
        from portfolio.allocator import GlobalAssetAllocator
        cls._pm_type = PortfolioManager
        cls._alloc_type = GlobalAssetAllocator

    @classmethod
    def tearDownClass(cls):
        _restore_modules(cls._saved_modules)

    def setUp(self):
        # Sibling teardowns (e.g. test_portfolio_isolation) call os.environ.clear(),
        # wiping module-import-time setdefaults; re-establish the FULL required
        # environment explicitly so this file is order-independent.
        os.environ["BARON_ZONE_JUDGE"] = "1"
        os.environ["PAPER_MODE"] = "True"
        os.environ["USE_EXECUTION_QUEUE"] = "True"
        os.environ["NEWS_ENABLED"] = "True"
        os.environ["NEWS_SLOT_ENABLED"] = "True"
        os.environ["MAX_TECHNICAL_POSITIONS"] = "5"
        # Per-direction cap is env-configurable (default 4/4). The six-slot
        # scenario is a pure one-direction day; raise BUY to the full book.
        os.environ["MAX_BUY_POSITIONS"] = "6"
        os.environ["MAX_SELL_POSITIONS"] = "6"
        RT = self.RT
        E = RT.E
        self._saved = (E.get_ohlcv_safe, E.get_ticker_safe,
                       E.get_orderbook_cached, E.get_balance_safe)
        self._frames = {s: _frame(base=p) for s, p in PRICES.items()}

        def provider(symbol, limit=120, htf=False):
            return self._frames.get(str(symbol))

        E.get_ohlcv_safe = provider
        E.get_ticker_safe = lambda symbol, retries=3: PRICES.get(str(symbol), 100.0)
        E.get_orderbook_cached = lambda *a, **k: {
            "bids": [[PRICES.get(str(a[0]), 100.0) - 1.0, 10.0]],
            "asks": [[PRICES.get(str(a[0]), 100.0) + 1.0, 5.0]]}
        E.get_balance_safe = lambda retries=3: E.paper["balance"] + E.paper["committed_margin"]

        E.paper = {"balance": 10000.0, "position": None, "committed_margin": 0.0}
        E.MEMORY.clear()
        E.MEMORY["pipeline"] = {"execution": {}}
        E.STATE.clear()
        E.TRADE_STATE.clear()
        E.DASHBOARD_STATE.clear()
        E.DASHBOARD_STATE["logs"] = []
        E.DASHBOARD_STATE["trade_lifecycle"] = []
        E.STATE["daily_loss_limit_hit"] = False
        E.STATE["last_trade_day"] = time.strftime("%Y-%m-%d")
        E.STATE["daily_peak_balance"] = E.paper["balance"]
        E.queue._candidates.clear()
        E.queue.total_executed = 0
        E.queue.total_rejected = 0

        RT.PORTFOLIO = self._pm_type(6, E)
        RT.ALLOCATOR = self._alloc_type(RT.PORTFOLIO, E)
        RT.PORTFOLIO.bind(E)
        RT.PORTFOLIO.risk_guard._day = None
        RT.PORTFOLIO.risk_guard._consecutive_losses = 0
        RT.PORTFOLIO.risk_guard._cooldown_until = 0.0

    def tearDown(self):
        RT = self.RT
        E = RT.E
        (E.get_ohlcv_safe, E.get_ticker_safe,
         E.get_orderbook_cached, E.get_balance_safe) = self._saved

    # ---- Fixture helpers ----
    def _govern_watch(self, symbol, asset_class, news_risk=0):
        self.RT.E.MEMORY.setdefault("watchlist", {})[symbol] = {
            "symbol": symbol, "side": "BUY", "news_risk": news_risk,
            "asset_class": asset_class,
        }

    def _ready_candidate(self, symbol, score=71.4, ready_floor=68.0,
                         side="BUY", asset_class=None):
        E = self.RT.E
        price = PRICES[symbol]
        atr = price * 0.01
        cand = E.ExecutionCandidate(
            symbol=symbol, side=side, price=price,
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
        cand.asset_class = asset_class or CLASS_BY_SYMBOL[symbol]
        cand.ready_score_required = float(ready_floor)
        self._govern_watch(symbol, cand.asset_class)
        self.assertTrue(E.queue.add_candidate(cand), f"admit {symbol}")
        E.queue._record_opportunity_lifecycle(cand)
        return cand

    def _news_watch(self, symbol="NCSKNVDA2USD/USDT:USDT", bias="BULLISH",
                    risk=20.0):
        E = self.RT.E
        sym = symbol
        price = PRICES[sym]
        E.MEMORY.setdefault("watchlist", {})[sym] = {
            "symbol": sym, "side": "BUY", "price": price,
            "atr": price * 0.01, "news_risk": risk, "asset_class": "NEWS",
            "news": types.SimpleNamespace(
                risk=risk, bias=bias,
                headlines=[{"impact_strength": "STRONG", "scope": "DIRECT",
                            "headline": f"{sym} impact"}],
                as_dict=lambda: {"bias": bias, "risk": risk},
            ),
        }
        E.DASHBOARD_STATE["news_reaction"] = {
            "items": [{"symbol": sym, "reaction": {
                "causality": "CONFIRMED", "move_pct": 0.20, "direction": "UP"}}],
        }

    def _exec_pipe(self):
        return self.RT.MEMORY.setdefault("pipeline", {}).setdefault("execution", {})

    # ---- 1. Six positions open through the REAL runtime under judge ON ----
    def test_six_positions_open_via_runtime_under_judge_on(self):
        RT = self.RT
        for sym in ["BTC/USDT:USDT", "ETH/USDT:USDT", "US500/USDT:USDT",
                    "USTECH/USDT:USDT", "XAUUSD"]:
            self._ready_candidate(sym)
            self.assertTrue(RT._execute_ready_queue_candidate(),
                            f"runtime must open {sym} under BARON_ZONE_JUDGE=1")
        self.assertEqual(RT.PORTFOLIO.count(), 5)

        self._news_watch()
        self.assertTrue(RT.execute_news_slot(),
                        "independent NEWS slot must open under BARON_ZONE_JUDGE=1")
        self.assertEqual(RT.PORTFOLIO.count(), 6)

        classes = [ctx.asset_class for ctx in RT.PORTFOLIO.contexts.values()]
        self.assertEqual(classes.count("CRYPTO"), 2)
        self.assertEqual(classes.count("INDEX"), 2)
        self.assertEqual(classes.count("GOLD"), 1)
        self.assertEqual(classes.count("NEWS"), 1)

        exec_pipe = self._exec_pipe()
        self.assertEqual(exec_pipe.get("executed"), 5)
        self.assertEqual(exec_pipe.get("news_executed"), 1)

        # The judge must have been advisory (WAIT_RETEST under READY grace), never
        # the silent killer: no hard BARON_REJECT anywhere in the gate feed.
        feed = RT.MEMORY.get("gate_feed", [])
        self.assertTrue(any(ev.get("blocker") == "BARON_ADVISORY" for ev in feed),
                        "advisory judge verdicts must be visible")
        self.assertFalse(any(ev.get("blocker") == "BARON_REJECT" for ev in feed),
                         "no hard judge reject for fresh READY grants")

        for sym in RT.PORTFOLIO.symbols():
            ctx = RT.PORTFOLIO.contexts[sym]
            self.assertTrue(ctx.state.get("open"), f"{sym} must be open")
            self.assertIsNotNone(ctx.live_manager)
            self.assertGreater(float(ctx.state.get("entry", 0.0) or 0.0), 0)

        equity = RT.E.paper["balance"] + RT.E.paper["committed_margin"]
        self.assertAlmostEqual(equity, 10000.0, places=6)

    # ---- 2. 6th technical refused with a user-facing reason ----
    def test_sixth_technical_refused_technical_capacity_full(self):
        RT = self.RT
        for sym in ["BTC/USDT:USDT", "ETH/USDT:USDT", "US500/USDT:USDT",
                    "USTECH/USDT:USDT", "XAUUSD"]:
            self._ready_candidate(sym)
            self.assertTrue(RT._execute_ready_queue_candidate())
        self.assertEqual(RT.PORTFOLIO.count(), 5)

        self._ready_candidate("OIL/USDT:USDT")
        self.assertFalse(RT._execute_ready_queue_candidate(),
                         "6th technical must be refused")
        self.assertEqual(RT.PORTFOLIO.count(), 5)
        exec_pipe = self._exec_pipe()
        self.assertEqual(exec_pipe.get("last_outcome"), "allocator_reject")
        self.assertEqual(exec_pipe.get("last_reject_reason"), "TECHNICAL_CAP")
        self.assertEqual(exec_pipe.get("last_reject_reason_user"),
                         "TECHNICAL_CAPACITY_FULL")
        self.assertNotIn("OIL/USDT:USDT", RT.PORTFOLIO.symbols())

    # ---- 3. NEWS independence: 2nd news attempt refused as NEWS_SLOT_FULL ----
    def test_second_news_attempt_refused_news_slot_full(self):
        RT = self.RT
        for sym in ["BTC/USDT:USDT", "ETH/USDT:USDT", "US500/USDT:USDT",
                    "USTECH/USDT:USDT"]:
            self._ready_candidate(sym)
            self.assertTrue(RT._execute_ready_queue_candidate())
        self._news_watch()
        self.assertTrue(RT.execute_news_slot())
        self.assertEqual(RT.PORTFOLIO.count(), 5)

        # NEWS must NOT have consumed any technical class quota: with 4 technical
        # + 1 NEWS, one technical (GOLD) still fits.
        self.assertTrue(RT.PORTFOLIO.can_open("XAUUSD", "GOLD"))

        self.assertFalse(RT.execute_news_slot(),
                         "a second NEWS trade must never open")
        self.assertEqual(RT.PORTFOLIO.count(), 5)
        exec_pipe = self._exec_pipe()
        self.assertEqual(exec_pipe.get("last_outcome"), "news_slot_full")
        self.assertEqual(exec_pipe.get("last_reject_reason_user"), "NEWS_SLOT_FULL")
        classes = [ctx.asset_class for ctx in RT.PORTFOLIO.contexts.values()]
        self.assertEqual(classes.count("NEWS"), 1)

    # ---- 4. 7th position refused as TOTAL_PORTFOLIO_CAPACITY_FULL ----
    def test_seventh_total_refused_total_portfolio_capacity_full(self):
        RT = self.RT
        for sym in ["BTC/USDT:USDT", "ETH/USDT:USDT", "US500/USDT:USDT",
                    "USTECH/USDT:USDT", "XAUUSD"]:
            self._ready_candidate(sym)
            self.assertTrue(RT._execute_ready_queue_candidate())
        self._news_watch()
        self.assertTrue(RT.execute_news_slot())
        self.assertEqual(RT.PORTFOLIO.count(), 6)

        self._ready_candidate("SOL/USDT:USDT")
        self.assertFalse(RT._execute_ready_queue_candidate(),
                         "7th position must be refused")
        exec_pipe = self._exec_pipe()
        self.assertEqual(exec_pipe.get("last_outcome"), "no_slots")
        self.assertEqual(exec_pipe.get("last_reject_reason"), "SLOT_CAP")
        self.assertEqual(exec_pipe.get("last_reject_reason_user"),
                         "TOTAL_PORTFOLIO_CAPACITY_FULL")
        self.assertNotIn("SOL/USDT:USDT", RT.PORTFOLIO.symbols())

        # The NEWS slot is subject to the same global total cap.
        self.assertFalse(RT.execute_news_slot())
        self.assertEqual(self._exec_pipe().get("last_outcome"), "news_no_slots")
        self.assertEqual(self._exec_pipe().get("last_reject_reason_user"),
                         "TOTAL_PORTFOLIO_CAPACITY_FULL")
        self.assertEqual(RT.PORTFOLIO.count(), 6)


if __name__ == "__main__":
    unittest.main(verbosity=2)