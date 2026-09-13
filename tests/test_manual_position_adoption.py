"""Adoption of MANUAL / existing exchange positions into portfolio management.

Regression that proves the root cause fix: a position that positively EXISTS on
the venue must enter the managed portfolio even when the ENTRY gate (can_open /
class caps / opening capacity) would refuse it as a NEW entry.  Management
coverage is adopted through the SAME LiveTradeManager and SAME manage_all path.

Scenarios covered:
  1. 2 manual positions -> 2 adopted contexts -> both managed (startup recover).
  2. 5 Technical + 1 News = 6/6; a 7th symbol is entry-BLOCKED (can_open False)
     yet, being a real venue position, is ADOPTED and MANAGED.
  3. A position whose class cap is already full (2nd GOLD, cap=1) is adopted.
  4. Periodic account-wide adoption discovers a position that appeared AFTER
     startup, without a restart, and is throttled.
  5. Hedge mode: LONG + SHORT on the SAME symbol -> two contexts keyed by
     (symbol, side); both are adopted and managed.
"""
import os
import unittest

import numpy as np
import pandas as pd

os.environ.setdefault("PAPER_MODE", "True")
os.environ.setdefault("BINGX_KEY", "")
os.environ.setdefault("BINGX_SECRET", "")
os.environ.setdefault("NEWS_ENABLED", "True")

import core.engine as E  # noqa: E402
from portfolio.manager import PortfolioManager  # noqa: E402
from portfolio.news_slot import scan_for_news_candidate  # noqa: E402


PRICES = {
    "BTC/USDT:USDT": 60000.0,
    "ETH/USDT:USDT": 3000.0,
    "US500/USDT:USDT": 5000.0,
    "USTECH/USDT:USDT": 17000.0,
    "XAUUSD": 2300.0,
    "XAUUSD2": 2350.0,
    "SOL/USDT:USDT": 150.0,
    "NCSKNVDA2USD/USDT:USDT": 130.0,
}


def _price(symbol):
    return float(PRICES.get(str(symbol), 100.0))


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


def _cand(sym, cls, side="BUY", score=88.0):
    price = _price(sym)
    atr = price * 0.01
    sl, tp1, tp2 = price - atr * 1.6, price + atr * 1.5, price + atr * 2.5
    return {"symbol": sym, "side": side, "price": price, "sl": sl, "tp1": tp1,
            "tp2": tp2, "score": score, "atr": atr, "asset_class": cls,
            "trade_id": sym}


def _news_watch(symbol, bias="BULLISH", risk=20.0):
    E.MEMORY["watchlist"][symbol] = {
        "price": _price(symbol),
        "atr": _price(symbol) * 0.01,
        "news_risk": risk,
        "news": __import__("types").SimpleNamespace(
            risk=risk, bias=bias,
            headlines=[{"impact_strength": "STRONG", "scope": "DIRECT",
                        "headline": f"{symbol} impact"}],
            as_dict=lambda: {"bias": bias, "risk": risk},
        ),
    }


def _manual(symbol, side="BUY", contracts=1.0, entry=None):
    entry = float(entry if entry is not None else _price(symbol))
    return {"symbol": symbol, "side": side, "contracts": float(contracts),
            "entryPrice": entry, "markPrice": entry,
            "stopLossPrice": 0.0, "takeProfitPrice": 0.0, "takeProfit2Price": 0.0}


class FakeSync:
    """Venue stand-in: fetch_all_open_positions returns whatever the account
    would report.  It holds a REFERENCE to the caller's list so that appending
    positions mid-test simulates a manual position appearing while the process
    is running."""

    def __init__(self, positions):
        self.positions = positions

    def fetch_all_open_positions(self):
        return [dict(p) for p in self.positions]


class RecordingManager:
    """Drops in for engine.LiveTradeManager so adoption can be observed without
    any real order/protection API call.  start_trade/manage_live_trade/dispose
    record the SAME calls the real manager performs."""

    started = []
    managed = 0
    disposed = []

    def __init__(self, *a, **k):
        pass

    @classmethod
    def reset(cls):
        cls.started = []
        cls.managed = 0
        cls.disposed = []

    def start_trade(self, symbol, side, entry_price, qty, sl, tp1, tp2, trade_id=None):
        self.__class__.started.append({"symbol": symbol, "side": side,
                                       "entry_price": entry_price, "qty": qty,
                                       "trade_id": trade_id})

    def manage_live_trade(self):
        self.__class__.managed += 1

    def dispose(self):
        self.__class__.disposed.append(1)


class ManualAdoptionTest(unittest.TestCase):

    def setUp(self):
        self._saved = (E.PAPER_MODE, E.LiveTradeManager,
                       E.sync_position_state, E._exchange_sync)
        self._saved_stubs = (E.get_ohlcv_safe, E.get_ticker_safe,
                             E.get_orderbook_cached, E.get_balance_safe)
        self._saved_perf = (dict(E.PERF), dict(E.DASHBOARD_STATE))
        E.get_ohlcv_safe = lambda symbol, limit=120, htf=False: _frame(base=_price(symbol))
        E.get_ticker_safe = lambda symbol, retries=3: _price(symbol)
        E.get_orderbook_cached = lambda *a, **k: {
            "bids": [[_price(a[0]) - 1.0, 10.0]], "asks": [[_price(a[0]) + 1.0, 5.0]]}
        E.get_balance_safe = lambda retries=3: E.paper["balance"]
        E.paper = {"balance": 10000.0, "position": None, "committed_margin": 0.0}
        E.STATE.clear()
        E.TRADE_STATE.clear()
        E.MEMORY.setdefault("watchlist", {}).clear()
        E.PERF.update({"total_pnl_pct": 0.0, "total_pnl_usdt": 0.0, "trades": 0,
                       "wins": 0, "losses": 0})
        self._positions = []
        E.PAPER_MODE = False
        E._exchange_sync = FakeSync(self._positions)
        E.LiveTradeManager = RecordingManager
        E.sync_position_state = lambda symbol: None
        RecordingManager.reset()
        self.pm = PortfolioManager(6, E)
        self.pm.bind(E)
        self.pm.risk_guard._day = None
        self.pm.risk_guard._consecutive_losses = 0
        self.pm.risk_guard._cooldown_until = 0.0

    def tearDown(self):
        (E.PAPER_MODE, E.LiveTradeManager,
         E.sync_position_state, E._exchange_sync) = self._saved
        (E.get_ohlcv_safe, E.get_ticker_safe,
         E.get_orderbook_cached, E.get_balance_safe) = self._saved_stubs
        perf, dash = self._saved_perf
        E.PERF.clear(); E.PERF.update(perf)
        E.DASHBOARD_STATE.clear(); E.DASHBOARD_STATE.update(dash)
        E.paper = {"balance": 10000.0, "position": None, "committed_margin": 0.0}
        E.STATE.clear()
        E.TRADE_STATE.clear()
        E.MEMORY.setdefault("watchlist", {}).clear()
        RecordingManager.reset()

    def test_two_manual_positions_adopted_and_managed(self):
        # 2 manual positions, one LONG one SHORT, different symbols.
        self._positions[:] = [_manual("BTC/USDT:USDT", "BUY", 1.0),
                              _manual("ETH/USDT:USDT", "SELL", 2.0)]
        recovered = self.pm.recover_from_exchange()
        self.assertEqual(recovered, 2)
        self.assertEqual(self.pm.count(), 2)
        self.assertEqual(len(self.pm.snapshot()), 2)
        # Startup recovery opened the SAME LiveTradeManager path (start_trade).
        self.assertEqual(len(RecordingManager.started), 2)
        started_syms = {s["symbol"] for s in RecordingManager.started}
        self.assertEqual(started_syms, {"BTC/USDT:USDT", "ETH/USDT:USDT"})
        # Every context is live-managed by manage_all, in the SAME loop.
        self.pm.manage_all()
        self.assertEqual(RecordingManager.managed, 2)
        self.assertEqual(self.pm.count(), 2)
        for key, ctx in self.pm.contexts.items():
            self.assertTrue(ctx.state.get("open"))
            self.assertGreater(ctx.state.get("qty", 0), 0)
            self.assertTrue(ctx.state.get("recovered"))
            self.assertEqual(ctx.ctx_key, key)
        # Final accounting: exchange positions == portfolio contexts == managed.
        self.assertEqual(len(self._positions), self.pm.count())
        self.assertEqual(RecordingManager.managed, len(self._positions))

    def test_six_open_with_news_and_seventh_adopted_despite_entry_block(self):
        # Phase 1 - the real entry path opens 5 Technical + 1 News = 6/6,
        # using the REAL LiveTradeManager exactly like the paper path does.
        real_lm = self._saved[1]  # original engine.LiveTradeManager class
        E.PAPER_MODE = True
        E.LiveTradeManager = real_lm
        try:
            _news_watch("NCSKNVDA2USD/USDT:USDT")
            news_cand = scan_for_news_candidate(E.MEMORY["watchlist"])
            self.assertIsNotNone(news_cand, "Strong-news candidate must be found")
            cands = [
                _cand("BTC/USDT:USDT", "CRYPTO"),
                _cand("ETH/USDT:USDT", "CRYPTO"),
                _cand("US500/USDT:USDT", "INDEX"),
                _cand("USTECH/USDT:USDT", "INDEX"),
                _cand("XAUUSD", "GOLD"),
            ]
            news_cand["side"] = "BUY"
            cands.append(news_cand)
            opened = self.pm.open_top(cands, slots=6)
            self.assertEqual(opened, 6)
            self.assertEqual(self.pm.count(), 6)
        finally:
            E.PAPER_MODE = False
            E.LiveTradeManager = RecordingManager
        # 7th NEW symbol is blocked by the entry gate (opening capacity).
        self.assertFalse(self.pm.can_open("SOL/USDT:USDT", "CRYPTO"))
        self.assertFalse(self.pm.open_candidate(_cand("SOL/USDT:USDT", "CRYPTO")))
        self.assertEqual(self.pm.count(), 6)

        # Phase 2 - that same symbol ALREADY EXISTS on the venue as a manual
        # position: the management-coverage gate (NOT can_open) adopts it.
        self._positions[:] = [_manual("SOL/USDT:USDT", "BUY", 0.1)]
        adopted = self.pm.recover_from_exchange()
        self.assertEqual(adopted, 1)
        self.assertEqual(self.pm.count(), 7)
        self.assertIn("SOL/USDT:USDT", self.pm.contexts)
        ctx = self.pm.contexts["SOL/USDT:USDT"]
        self.assertTrue(ctx.state.get("open"))
        self.assertEqual(len(self.pm.snapshot()), 7)

        # The manual 7th must NOT grant BARON an 8th opening slot: opening
        # capacity stayed 6 (5 Technical + 1 News), so with 7 positions the
        # manager refuses every NEW entry.
        self.assertFalse(self.pm.can_open("XRP/USDT:USDT", "CRYPTO"))
        self.assertFalse(self.pm.open_candidate(_cand("XRP/USDT:USDT", "CRYPTO")))
        self.assertEqual(self.pm.count(), 7)

        # Phase 3 - the adopted 7th is managed by the SAME manage_all path
        # as every other context (recording managers stand in non-paper live
        # managers so no real venue calls happen).
        self.assertEqual(len(RecordingManager.started), 1)
        for key in list(self.pm.contexts.keys()):
            if not isinstance(self.pm.contexts[key].live_manager, RecordingManager):
                self.pm.contexts[key].live_manager = RecordingManager()
        self.assertEqual(len(RecordingManager.started), 1)
        self.assertIsInstance(self.pm.contexts["SOL/USDT:USDT"].live_manager, RecordingManager)
        RecordingManager.reset()
        self.pm.manage_all()
        self.assertEqual(RecordingManager.managed, self.pm.count())

    def test_second_gold_adopted_despite_class_cap(self):
        # GOLD class cap = 1 (DEFAULT_CLASS_CAPS).  Two GOLD positions on the
        # venue must BOTH be adopted as management coverage: the cap is an
        # ENTRY rule, never a management rule.
        self._positions[:] = [_manual("XAUUSD", "BUY", 1.0),
                              _manual("XAUUSD2", "BUY", 0.5)]
        recovered = self.pm.recover_from_exchange()
        self.assertEqual(recovered, 2)
        self.assertEqual(self.pm.count(), 2)
        classes = [self.pm.contexts[key].asset_class for key in self.pm.contexts]
        self.assertEqual(classes, ["GOLD", "GOLD"])
        self.assertEqual([self._ctxcls(k) for k in self.pm.contexts],
                         ["GOLD", "GOLD"])
        # A NEW GOLD third entry is still refused by the entry gate.
        self.assertFalse(self.pm.can_open("XAUUSD2", "GOLD"))
        self.pm.manage_all()
        self.assertEqual(RecordingManager.managed, 2)

    def _ctxcls(self, key):
        return self.pm._ctx_class(self.pm.contexts[key])

    def test_periodic_adoption_discovers_mid_run_position_and_throttles(self):
        self._positions[:] = [_manual("BTC/USDT:USDT", "BUY", 1.0)]
        self.assertEqual(self.pm.recover_from_exchange(), 1)
        # A manual position appears at the venue AFTER startup.
        self._positions.append(_manual("SOL/USDT:USDT", "BUY", 0.5))
        first = self.pm.adopt_unmanaged_from_exchange(throttle_sec=0.0)
        self.assertEqual(first, 1)
        self.assertEqual(self.pm.count(), 2)
        self.assertIn("SOL/USDT:USDT", self.pm.contexts)
        # Same scan is throttled when invoked again within the window.
        self._positions.append(_manual("ETH/USDT:USDT", "SELL", 1.0))
        self.assertEqual(self.pm.adopt_unmanaged_from_exchange(throttle_sec=3600.0), 0)
        self.assertEqual(self.pm.count(), 2)
        self.pm.manage_all()
        self.assertEqual(RecordingManager.managed, 2)

    def test_adopted_manual_positions_consume_opening_budget(self):
        # Adopted manual positions share the SAME 6-position opening budget
        # and class caps: they never extend BARON's headroom.
        self._positions[:] = [_manual("BTC/USDT:USDT", "BUY", 1.0),
                              _manual("ETH/USDT:USDT", "BUY", 1.0)]
        self.assertEqual(self.pm.recover_from_exchange(), 2)
        self.assertEqual(self.pm.count(), 2)
        # CRYPTO class cap (2) is consumed by the two adopted positions, so
        # BARON cannot open a 3rd CRYPTO.
        self.assertFalse(self.pm.can_open("SOL/USDT:USDT", "CRYPTO"))
        self.assertFalse(self.pm.open_candidate(_cand("SOL/USDT:USDT", "CRYPTO")))
        # Room still exists in other classes within the shared ceiling.
        self.assertTrue(self.pm.can_open("XAUUSD", "GOLD"))
        # Fill the account to the shared ceiling (2+4=6) with manual positions.
        self._positions.extend([
            _manual("XAUUSD", "BUY", 1.0),
            _manual("US500/USDT:USDT", "BUY", 1.0),
            _manual("USTECH/USDT:USDT", "BUY", 1.0),
            _manual("NCSKNVDA2USD/USDT:USDT", "BUY", 1.0),
        ])
        self.assertEqual(self.pm.adopt_unmanaged_from_exchange(throttle_sec=0.0), 4)
        self.assertEqual(self.pm.count(), 6)
        # 6/6 reached -> a NEW technical entry is blocked (no 7th from BARON).
        self.assertFalse(self.pm.can_open("DOGE/USDT:USDT", "CRYPTO"))
        self.assertFalse(self.pm.open_candidate(_cand("DOGE/USDT:USDT", "CRYPTO")))
        self.assertEqual(self.pm.count(), 6)
        # A 7th MANUAL position is still ADOPTED+MANAGED (management coverage)
        # but that still must not grant BARON an 8th opening slot.
        self._positions.append(_manual("DOGE/USDT:USDT", "BUY", 0.5))
        self.assertEqual(self.pm.adopt_unmanaged_from_exchange(throttle_sec=0.0), 1)
        self.assertEqual(self.pm.count(), 7)
        self.assertFalse(self.pm.can_open("XRP/USDT:USDT", "CRYPTO"))
        self.assertFalse(self.pm.open_candidate(_cand("XRP/USDT:USDT", "CRYPTO")))
        self.assertEqual(self.pm.count(), 7)

    def test_hedge_long_short_same_symbol_composite_keys(self):
        # The account reports BOTH a LONG and a SHORT on the same symbol.
        self._positions[:] = [
            _manual("BTC/USDT:USDT", "BUY", 1.0, 60000.0),
            _manual("BTC/USDT:USDT", "SELL", 0.5, 59000.0),
        ]
        recovered = self.pm.recover_from_exchange()
        self.assertEqual(recovered, 2)
        self.assertTrue(self.pm.hedge_mode)
        keys = set(self.pm.symbols())
        self.assertEqual(keys, {("BTC/USDT:USDT", "BUY"),
                                ("BTC/USDT:USDT", "SELL")})
        self.assertEqual(self.pm.count(), 2)
        self.assertEqual(len(self.pm.snapshot()), 2)
        # New entries on that symbol remain blocked (the symbol already lives
        # in the portfolio, in either direction).
        self.assertTrue(self.pm._has_context("BTC/USDT:USDT"))
        self.assertFalse(self.pm.can_open("BTC/USDT:USDT", "CRYPTO"))
        # Both hedge legs run through the SAME manage_all loop.
        self.pm.manage_all()
        self.assertEqual(RecordingManager.managed, 2)
        for key in self.pm.contexts:
            self.assertIsInstance(key, tuple)
            self.assertIn(key[1], ("BUY", "SELL"))
        # Per-leg close removes exactly the requested side.  The venue-close
        # itself is simulated (real close_position_full needs live markets).
        real_close = E.close_position_full
        E.close_position_full = lambda: E.STATE.__setitem__("open", False)
        try:
            self.assertTrue(self.pm.close_symbol("BTC/USDT:USDT", "BUY"))
        finally:
            E.close_position_full = real_close
        self.assertEqual(self.pm.count(), 1)
        self.assertIn(("BTC/USDT:USDT", "SELL"), self.pm.contexts)


if __name__ == "__main__":
    unittest.main()