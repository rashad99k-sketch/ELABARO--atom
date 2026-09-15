"""NEWS <-> TECHNICAL separation proof through the REAL runtime authority.

Requirement (reviewer focus): the 5-technical book (2 Crypto + 2 Index +
1 Gold) must NEVER depend on news, and a news event must NEVER produce a
technical entry. The only paths out of the NEWS slot are:
    News Event -> Headline -> Confirmed Market Reaction -> NEWS execution.
No back edge NEWS -> TECHNICAL ENTRY exists.

Proven here end-to-end with BARON_ZONE_JUDGE=1 (production default):
  1. The technical book opens fully with the NEWS slot DISABLED.
  2. A watchlist that contains ONLY a strong-news symbol produces ZERO queue
     READY candidates and ZERO technical executions (news is evidence, never
     an order).
  3. A NEWS trade is classified NEWS in the book and contributes ZERO to the
     technical/class quotas while open.
  4. Without CONFIRMED market reaction (no telemetry, or wrong direction) the
     news slot never opens  -  the blind-headline path stays closed.
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
    "XAUUSD": 2300.0,
    "NCSKNVDA2USD/USDT:USDT": 130.0,
}

CLASS_BY_SYMBOL = {
    "BTC/USDT:USDT": "CRYPTO",
    "ETH/USDT:USDT": "CRYPTO",
    "US500/USDT:USDT": "INDEX",
    "USTECH/USDT:USDT": "INDEX",
    "XAUUSD": "GOLD",
}


class NewsTechnicalSeparationTest(unittest.TestCase):

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
        # Sibling teardowns (os.environ.clear()) can wipe module-import env;
        # re-establish the FULL required environment explicitly (order-safe).
        os.environ["BARON_ZONE_JUDGE"] = "1"
        os.environ["PAPER_MODE"] = "True"
        os.environ["USE_EXECUTION_QUEUE"] = "True"
        os.environ["NEWS_ENABLED"] = "True"
        os.environ["NEWS_SLOT_ENABLED"] = "True"
        os.environ["MAX_TECHNICAL_POSITIONS"] = "5"
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

    def _exec_pipe(self):
        return self.RT.MEMORY.setdefault("pipeline", {}).setdefault("execution", {})

    def _ready_candidate(self, symbol, score=71.4, ready_floor=68.0, side="BUY"):
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
        cand.asset_class = CLASS_BY_SYMBOL[symbol]
        cand.ready_score_required = float(ready_floor)
        self.RT.E.MEMORY.setdefault("watchlist", {})[symbol] = {
            "symbol": symbol, "side": side, "news_risk": 0,
            "asset_class": cand.asset_class,
        }
        self.assertTrue(E.queue.add_candidate(cand), f"admit {symbol}")
        E.queue._record_opportunity_lifecycle(cand)
        return cand

    def _news_watch(self, symbol="NCSKNVDA2USD/USDT:USDT", direction="UP",
                    with_reaction=True):
        E = self.RT.E
        price = PRICES[symbol]
        E.MEMORY.setdefault("watchlist", {})[symbol] = {
            "symbol": symbol, "side": "BUY", "price": price,
            "atr": price * 0.01, "news_risk": 20.0, "asset_class": "NEWS",
            "news": types.SimpleNamespace(
                risk=20.0, bias="BULLISH",
                headlines=[{"impact_strength": "STRONG", "scope": "DIRECT",
                            "headline": f"{symbol} impact"}],
                as_dict=lambda: {"bias": "BULLISH", "risk": 20.0},
            ),
        }
        if with_reaction:
            E.DASHBOARD_STATE["news_reaction"] = {
                "items": [{"symbol": symbol, "reaction": {
                    "causality": "CONFIRMED", "move_pct": 0.20,
                    "direction": direction}}],
            }
        return symbol

    def _technical_count(self):
        return sum(1 for ctx in self.RT.PORTFOLIO.contexts.values()
                   if str(getattr(ctx, "asset_class", "") or "").upper() != "NEWS")

    # ---- 1. Technical book is INDEPENDENT of news ----
    def test_technical_book_opens_with_news_slot_disabled(self):
        os.environ["NEWS_SLOT_ENABLED"] = "false"
        RT = self.RT
        for sym in ["BTC/USDT:USDT", "ETH/USDT:USDT", "US500/USDT:USDT",
                    "USTECH/USDT:USDT", "XAUUSD"]:
            self._ready_candidate(sym)
            self.assertTrue(RT._execute_ready_queue_candidate(),
                            f"technical {sym} must open with NEWS slot OFF")
        self.assertEqual(RT.PORTFOLIO.count(), 5)
        self.assertEqual(self._technical_count(), 5,
                         "all five open positions must be technical")
        # NEWS slot physically cannot open.
        self._news_watch(with_reaction=True)
        self.assertFalse(RT.execute_news_slot())
        self.assertEqual(RT.PORTFOLIO.count(), 5)

    # ---- 2. NO BACK EDGE: news alone creates NO technical entry ----
    def test_news_only_cannot_create_a_technical_entry(self):
        RT = self.RT
        self._news_watch(with_reaction=True)
        # Strong bullish news + confirmed reaction are present, but the queue
        # contains zero technical candidates -> the executor can open nothing.
        self.assertIsNone(RT.E.queue.get_best_candidate())
        self.assertFalse(RT._execute_ready_queue_candidate())
        self.assertEqual(RT.PORTFOLIO.count(), 0,
                         "news must never manufacture a technical entry")
        self.assertEqual("no_ready_candidate", self._exec_pipe().get("last_outcome"))

    # ---- 3. A NEWS trade is classified NEWS, never technical ----
    def test_news_trade_is_never_labeled_technical(self):
        RT = self.RT
        self._news_watch(with_reaction=True)
        self.assertTrue(RT.execute_news_slot())
        self.assertEqual(RT.PORTFOLIO.count(), 1)
        ctx = list(RT.PORTFOLIO.contexts.values())[0]
        self.assertEqual(str(ctx.asset_class).upper(), "NEWS")
        self.assertEqual(self._technical_count(), 0,
                         "a news position contributes ZERO to technical quotas")
        snap = RT.PORTFOLIO.snapshot()
        self.assertTrue(all(str(r["asset_class"]).upper() == "NEWS" for r in snap))
        # While 1 NEWS is open, a technical class slot remains available.
        self.assertTrue(RT.PORTFOLIO.can_open("XAUUSD", "GOLD"),
                        "news must not consume a technical class quota")

    # ---- 4. No blind headlines: reaction telemetry is mandatory ----
    def test_news_without_confirmed_reaction_never_opens(self):
        RT = self.RT
        self._news_watch(with_reaction=False)
        self.assertFalse(RT.execute_news_slot())
        self.assertEqual(RT.PORTFOLIO.count(), 0)
        self.assertEqual("news_waiting_reaction", self._exec_pipe().get("last_outcome"))

    def test_news_reaction_wrong_direction_never_opens(self):
        RT = self.RT
        self._news_watch(with_reaction=True, direction="DOWN")  # BUY wants UP
        self.assertFalse(RT.execute_news_slot())
        self.assertEqual(RT.PORTFOLIO.count(), 0)
        self.assertEqual("news_waiting_reaction", self._exec_pipe().get("last_outcome"))


if __name__ == "__main__":
    unittest.main(verbosity=2)