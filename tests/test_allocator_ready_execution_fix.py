"""Allocator / READY execution forensic fix: combined INDEX/STOCK bucket.

Production telemetry showed READY candidates dying right after the allocator
with a bare `STOCK_CAP` rejection:
    QUEUE: ready=7 ... EXECUTION: last=allocator_reject
    allocator_reject=2, last_reject_reason=STOCK_CAP

ROOT CAUSE: INDEX and STOCK were two SEPARATE capacity buckets — and STOCK had
cap 0 (`CLASS_CAPS.get("STOCK", 0) == 0`, and `0 >= 0` is always true), so the
FIRST stock candidate was rejected before it ever reached a slot, while the
intended portfolio model gives INDEX/STOCK a COMBINED 2-seat technical bucket
("Index/Stock #1 + Index/Stock #2", never 2 INDEX + 2 STOCK). OIL/GOLD also
share ONE 1-seat commodity seat.

This file drives the SAME production path
(core.runtime._execute_ready_queue_candidate for technical candidates and
core.runtime.execute_news_slot for the independent NEWS slot) with the BARON
judge ON and every other gate real, and proves:

  1. Crypto #1 + Crypto #2 + Stock #1 + Index #1  -> Index accepted (combined).
  2. ... + Gold #1 -> TECHNICAL = 5/5, then News #1 -> TOTAL = 6/6.
  3. Index/Stock #3 -> REJECTED INDEX_STOCK_CAPACITY_FULL (used=2/max=2).
  4. Gold vs Oil on the SAME commodity seat -> 2nd one REJECTED
     COMMODITY_CAPACITY_FULL.
  5. News #2 -> NEWS_SLOT_FULL; 7th position -> TOTAL_PORTFOLIO_CAPACITY_FULL.
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
    sys.modules["ccxt"] = fake_ccxt
    # ccxt is the only dependency this suite needs to fake. The conftest flask
    # boundary (with test_client) is left intact: replacing it with a private
    # stub here poisons core.engine's Flask/app globals, which dashboard/app.py
    # copies into its own namespace and then caches process-wide, breaking every
    # later dashboard test (stub without test_client).

    import core.runtime as RT
    return RT


def _restore_modules(saved):
    sys.modules.clear()
    sys.modules.update(saved)


def _frame(n=250, base=100.0):
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
    "NCSKAAPL2USD/USDT:USDT": 210.0,
    "XAUUSD": 2300.0,
    "OIL/USDT:USDT": 80.0,
    "NCSKNVDA2USD/USDT:USDT": 130.0,
}

CLASS_BY_SYMBOL = {
    "BTC/USDT:USDT": "CRYPTO",
    "ETH/USDT:USDT": "CRYPTO",
    "US500/USDT:USDT": "INDEX",
    "USTECH/USDT:USDT": "INDEX",
    "NCSKAAPL2USD/USDT:USDT": "STOCK",
    "XAUUSD": "GOLD",
    "OIL/USDT:USDT": "OIL",
}

READY_FLOORS = {
    "CRYPTO": 68.0,
    "INDEX": 68.0,
    "STOCK": 67.0,
    "GOLD": 68.0,
    "OIL": 68.0,
}


class AllocatorReadyExecutionFixTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._saved_modules = sys.modules.copy()
        cls.RT = _load_runtime()
        from portfolio.manager import PortfolioManager
        from portfolio.allocator import GlobalAssetAllocator
        cls._pm_type = PortfolioManager
        cls._alloc_type = GlobalAssetAllocator

    @classmethod
    def tearDownClass(cls):
        _restore_modules(cls._saved_modules)

    def setUp(self):
        os.environ["BARON_ZONE_JUDGE"] = "1"
        os.environ["PAPER_MODE"] = "True"
        os.environ["USE_EXECUTION_QUEUE"] = "True"
        os.environ["NEWS_ENABLED"] = "True"
        os.environ["NEWS_SLOT_ENABLED"] = "True"
        os.environ["MAX_TECHNICAL_POSITIONS"] = "5"
        os.environ.pop("MAX_POSITIONS_PER_ASSET_CLASS", None)
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

    def _ready_candidate(self, symbol, score=71.4, side="BUY", asset_class=None):
        E = self.RT.E
        price = PRICES[symbol]
        atr = price * 0.01
        cls = asset_class or CLASS_BY_SYMBOL[symbol]
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
        cand.asset_class = cls
        cand.ready_score_required = READY_FLOORS[cls]
        self._govern_watch(symbol, cls)
        self.assertTrue(E.queue.add_candidate(cand), f"admit {symbol}")
        E.queue._record_opportunity_lifecycle(cand)
        return cand

    def _news_watch(self, symbol="NCSKNVDA2USD/USDT:USDT", bias="BULLISH",
                    risk=20.0):
        E = self.RT.E
        price = PRICES[symbol]
        E.MEMORY.setdefault("watchlist", {})[symbol] = {
            "symbol": symbol, "side": "BUY", "price": price,
            "atr": price * 0.01, "news_risk": risk, "asset_class": "NEWS",
            "news": types.SimpleNamespace(
                risk=risk, bias=bias,
                headlines=[{"impact_strength": "STRONG", "scope": "DIRECT",
                            "headline": f"{symbol} impact"}],
                as_dict=lambda: {"bias": bias, "risk": risk},
            ),
        }
        E.DASHBOARD_STATE["news_reaction"] = {
            "items": [{"symbol": symbol, "reaction": {
                "causality": "CONFIRMED", "move_pct": 0.20, "direction": "UP"}}],
        }

    def _exec_pipe(self):
        return self.RT.MEMORY.setdefault("pipeline", {}).setdefault("execution", {})

    # ---- 1. Stock #1 + Index #1 share the combined bucket; then News; 7th ----
    def test_stock_index_gold_news_full_book_via_runtime(self):
        RT = self.RT
        # Existing telemetry scenario: CRYPTO x2 + STOCK x1 live.
        for sym in ["BTC/USDT:USDT", "ETH/USDT:USDT", "NCSKAAPL2USD/USDT:USDT"]:
            self._ready_candidate(sym)
            self.assertTrue(RT._execute_ready_queue_candidate(),
                            f"runtime must open {sym}")
        # A valid INDEX #1 MUST be accepted (combined INDEX/STOCK bucket 2/2).
        self._ready_candidate("US500/USDT:USDT")
        self.assertTrue(RT._execute_ready_queue_candidate(),
                        "INDEX #1 must be accepted after CRYPTO x2 + STOCK x1")
        self.assertEqual(RT.PORTFOLIO.count(), 4)
        classes = [ctx.asset_class for ctx in RT.PORTFOLIO.contexts.values()]
        self.assertEqual(classes.count("CRYPTO"), 2)
        self.assertEqual(classes.count("INDEX") + classes.count("STOCK"), 2)

        # NEWS #1 opens while technical seats are still free (5 of 5), proving
        # the news slot is INDEPENDENT and the pool is 5 technical + 1 news.
        self._news_watch()
        self.assertTrue(RT.execute_news_slot(), "independent NEWS slot must open")
        self.assertEqual(self._exec_pipe().get("news_executed"), 1)

        # NEWS #2 -> NEWS_SLOT_FULL while a technical slot is still free (news
        # does not consume a technical seat, and the news seat is exactly one).
        self.assertFalse(RT.execute_news_slot())
        self.assertEqual(self._exec_pipe().get("last_outcome"), "news_slot_full")
        self.assertEqual(self._exec_pipe().get("last_reject_reason_user"),
                         "NEWS_SLOT_FULL")
        self.assertEqual(RT.PORTFOLIO.count(), 5)

        # Then GOLD #1 -> TECHNICAL = 5/5, TOTAL = 6/6.
        self._ready_candidate("XAUUSD")
        self.assertTrue(RT._execute_ready_queue_candidate(),
                        "GOLD #1 must be accepted after CRYPTO x2 + INDEX/STOCK x2")
        self.assertEqual(RT.PORTFOLIO.count(), 6)
        classes = [ctx.asset_class for ctx in RT.PORTFOLIO.contexts.values()]
        self.assertEqual(classes.count("GOLD"), 1)
        self.assertEqual(self._exec_pipe().get("executed"), 5)

        # Judge advisory (READY grace), never a hard reject, under judge ON.
        feed = RT.MEMORY.get("gate_feed", [])
        self.assertTrue(any(ev.get("blocker") == "BARON_ADVISORY" for ev in feed))
        self.assertFalse(any(ev.get("blocker") == "BARON_REJECT" for ev in feed))

        # 7th position (any) -> TOTAL_PORTFOLIO_CAPACITY_FULL.
        self._ready_candidate("OIL/USDT:USDT")
        self.assertFalse(RT._execute_ready_queue_candidate(),
                         "7th position must be refused")
        self.assertEqual(self._exec_pipe().get("last_outcome"), "no_slots")
        self.assertEqual(self._exec_pipe().get("last_reject_reason_user"),
                         "TOTAL_PORTFOLIO_CAPACITY_FULL")
        self.assertEqual(RT.PORTFOLIO.count(), 6)

    # ---- 2. Third Index/Stock rejected with the precise reason + used/max ----
    def test_third_index_stock_rejected_index_stock_capacity_full(self):
        RT = self.RT
        for sym in ["BTC/USDT:USDT", "ETH/USDT:USDT",
                    "NCSKAAPL2USD/USDT:USDT", "US500/USDT:USDT"]:
            self._ready_candidate(sym)
            self.assertTrue(RT._execute_ready_queue_candidate())
        self.assertEqual(RT.PORTFOLIO.count(), 4)

        # Third Index/Stock candidate (USTECH) -> combined bucket full.
        self._ready_candidate("USTECH/USDT:USDT")
        self.assertFalse(RT._execute_ready_queue_candidate(),
                         "Index/Stock #3 must be rejected")
        self.assertEqual(RT.PORTFOLIO.count(), 4)
        exec_pipe = self._exec_pipe()
        self.assertEqual(exec_pipe.get("last_outcome"), "allocator_reject")
        self.assertEqual(exec_pipe.get("last_reject_reason"), "INDEX_STOCK_CAP")
        self.assertEqual(exec_pipe.get("last_reject_reason_user"),
                         "INDEX_STOCK_CAPACITY_FULL")
        self.assertEqual(exec_pipe.get("last_reject_bucket"), "INDEX_STOCK")
        self.assertEqual(exec_pipe.get("last_reject_bucket_used"), 2)
        self.assertEqual(exec_pipe.get("last_reject_bucket_max"), 2)
        self.assertEqual(exec_pipe.get("last_reject_technical_used"), 4)
        self.assertNotIn("USTECH/USDT:USDT", RT.PORTFOLIO.symbols())

        # Capacity-rejected candidates leave the queue via maintenance instead of
        # re-picking the same highest-READY candidate forever (queue backoff).
        RT.queue._invalidate("USTECH/USDT:USDT", "capacity-rejected INDEX_STOCK bucket full")
        # The commodity seat is still free -> GOLD opens after the rejection.
        self._ready_candidate("XAUUSD")
        self.assertTrue(RT._execute_ready_queue_candidate(),
                        "GOLD must still open after an Index/Stock rejection")
        self.assertEqual(RT.PORTFOLIO.count(), 5)

    # ---- 3. OIL/GOLD share ONE commodity seat ----
    def test_commodity_single_seat_oil_rejected_when_gold_open(self):
        RT = self.RT
        for sym in ["BTC/USDT:USDT", "ETH/USDT:USDT",
                    "US500/USDT:USDT", "USTECH/USDT:USDT", "XAUUSD"]:
            self._ready_candidate(sym)
            self.assertTrue(RT._execute_ready_queue_candidate())
        self.assertEqual(RT.PORTFOLIO.count(), 5)

        self._ready_candidate("OIL/USDT:USDT")
        self.assertFalse(RT._execute_ready_queue_candidate(),
                         "OIL after GOLD must be refused (one commodity seat)")
        exec_pipe = self._exec_pipe()
        self.assertEqual(exec_pipe.get("last_reject_reason"), "COMMODITY_CAP")
        self.assertEqual(exec_pipe.get("last_reject_reason_user"),
                         "COMMODITY_CAPACITY_FULL")
        self.assertEqual(exec_pipe.get("last_reject_bucket"), "COMMODITY")
        self.assertEqual(exec_pipe.get("last_reject_bucket_used"), 1)
        self.assertEqual(exec_pipe.get("last_reject_bucket_max"), 1)
        self.assertEqual(exec_pipe.get("last_reject_technical_used"), 5)
        self.assertEqual(exec_pipe.get("last_reject_technical_max"), 5)
        self.assertNotIn("OIL/USDT:USDT", RT.PORTFOLIO.symbols())

    # ---- 4. Classifier: allocator capacity reject -> category=capacity ----
    def test_allocator_reject_classified_capacity_not_other(self):
        RT = self.RT
        for sym in ["BTC/USDT:USDT", "ETH/USDT:USDT",
                    "NCSKAAPL2USD/USDT:USDT", "US500/USDT:USDT"]:
            self._ready_candidate(sym)
            self.assertTrue(RT._execute_ready_queue_candidate())
        self.assertEqual(RT.PORTFOLIO.count(), 4)

        # Third Index/Stock candidate (USTECH) -> combined bucket full.
        self._ready_candidate("USTECH/USDT:USDT")
        self.assertFalse(RT._execute_ready_queue_candidate(),
                         "Index/Stock #3 must be rejected")
        exec_pipe = self._exec_pipe()
        self.assertEqual(exec_pipe.get("last_reject_reason"), "INDEX_STOCK_CAP")
        # Requirement: INDEX_STOCK_CAP MUST classify as capacity with the user
        # facing blocker, never other/NONE (the pre-fix behaviour).
        self.assertEqual(exec_pipe.get("last_open_failure_category"), "capacity")
        self.assertEqual(exec_pipe.get("last_open_failure_blocker"),
                         "INDEX_STOCK_CAPACITY_FULL")
        self.assertGreaterEqual(exec_pipe.get("open_failure_category:capacity", 0), 1)
        # skip the old other/NONE buckets
        self.assertNotEqual(exec_pipe.get("last_open_failure_blocker"), "NONE")

        # The rejected candidate is backed off (queue advance), so the queue no
        # longer re-picks the same blocked highest-READY candidate forever.
        cand = RT.queue._candidates.get("USTECH/USDT:USDT")
        self.assertIsNotNone(cand)
        self.assertGreater(cand.allocator_rejected_until, time.time())
        self.assertEqual(cand.last_allocator_reason, "INDEX_STOCK_CAP")

        # Per-attempt deterministic telemetry ring carries the failure fact.
        last_attempt = exec_pipe.get("last_open_attempt")
        self.assertIsNotNone(last_attempt)
        self.assertEqual(last_attempt["symbol"], "USTECH/USDT:USDT")
        self.assertEqual(last_attempt["asset_class"], "INDEX")
        self.assertEqual(last_attempt["bucket"], "INDEX_STOCK")
        self.assertEqual(last_attempt["rejection_reason"], "INDEX_STOCK_CAP")
        self.assertEqual(last_attempt["rejection_category"], "capacity")
        self.assertEqual(last_attempt["next_queue_action"], "backoff+advance_queue")
        attempts = exec_pipe.get("open_attempts")
        self.assertGreaterEqual(len(attempts), 1)

        # Queue advances WITHOUT manual invalidation: the commodity seat is
        # still free -> GOLD opens after the rejected Index/Stock candidate,
        # proving a blocked STOCK no longer starves CRYPTO/OIL/GOLD/NEWS.
        self._ready_candidate("XAUUSD")
        self.assertTrue(RT._execute_ready_queue_candidate(),
                        "GOLD must open after backoff; queue must advance")
        self.assertEqual(RT.PORTFOLIO.count(), 5)