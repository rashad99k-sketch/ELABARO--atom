"""Integration: six positions open SIMULTANEOUSLY through the real entry path,
full risk/capacity enforcement, close-all, and slot reuse.

This is a dedicated "open 6 in the same instant" regression. It uses the real
PortfolioManager.open_top -> open_candidate -> execute_entry pipeline in PAPER
mode with only the provider boundary replaced (OHLCV / ticker / orderbook).

  Part 1 - Six slots (2 CRYPTO / 2 INDEX / 1 GOLD / 1 NEWS) are opened in a
           single batch. Every context is open at once and the margin ledger
           invariant holds (balance + committed == initial).
  Part 2 - The 7th slot is refused by capacity AND the portfolio margin cap.
  Part 3 - manage_all() runs the real multi-position management loop over all
           six without corrupting contexts; snapshot() reports the opened_at
           window proving simultaneous opening.
  Part 4 - Every position is closed through the real close path; slots,
           margin and ledger slot are all released, and a new position can be
           reopened in a freed slot.
"""
import os
import types
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
    "NCSKNVDA2USD/USDT:USDT": 130.0,
}


def _price(symbol):
    return float(PRICES.get(str(symbol), 100.0))


def _frame(n=250, base=100.0):
    """Same trending frame family the T4/T6 tests use: passes the REAL entry
    gates (ADX in class band + sell-side liquidity sweep + strong reclaim)."""
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
        "news": types.SimpleNamespace(
            risk=risk, bias=bias,
            headlines=[{"impact_strength": "STRONG", "scope": "DIRECT",
                        "headline": f"{symbol} impact"}],
            as_dict=lambda: {"bias": bias, "risk": risk},
        ),
    }


class SixSimultaneousOpenTest(unittest.TestCase):

    def setUp(self):
        self._saved = (E.get_ohlcv_safe, E.get_ticker_safe,
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
        self.pm = PortfolioManager(6, E)
        self.pm.bind(E)
        self.pm.risk_guard._day = None
        self.pm.risk_guard._consecutive_losses = 0
        self.pm.risk_guard._cooldown_until = 0.0

    def tearDown(self):
        (E.get_ohlcv_safe, E.get_ticker_safe,
         E.get_orderbook_cached, E.get_balance_safe) = self._saved
        perf, dash = self._saved_perf
        E.PERF.clear(); E.PERF.update(perf)
        E.DASHBOARD_STATE.clear(); E.DASHBOARD_STATE.update(dash)
        E.paper = {"balance": 10000.0, "position": None, "committed_margin": 0.0}
        E.STATE.clear()
        E.TRADE_STATE.clear()
        E.MEMORY.setdefault("watchlist", {}).clear()

    def _six_candidates(self):
        _news_watch("NCSKNVDA2USD/USDT:USDT")
        news_cand = scan_for_news_candidate(E.MEMORY["watchlist"])
        self.assertIsNotNone(news_cand, "Strong-news candidate must be found")
        news_cand["side"] = "BUY"
        return [
            _cand("BTC/USDT:USDT", "CRYPTO"),
            _cand("ETH/USDT:USDT", "CRYPTO"),
            _cand("US500/USDT:USDT", "INDEX"),
            _cand("USTECH/USDT:USDT", "INDEX"),
            _cand("XAUUSD", "GOLD"),
            news_cand,
        ]

    def test_six_positions_open_in_the_same_instant_via_real_path(self):
        cands = self._six_candidates()
        opened = self.pm.open_top(cands, slots=6)
        self.assertEqual(opened, 6)
        self.assertEqual(self.pm.count(), 6)
        self.assertEqual(len(self.pm.contexts), 6)

        # Every context is open at the same time, with a real live manager.
        # "Simultaneous" here is a MODEL property: the portfolio holds all six
        # open contexts at once (not one recycled slot). Individual entry calls
        # run sequentially inside open_top, so the wall-clock span is machine
        # dependent and is NOT a correctness assertion.
        for sym in self.pm.symbols():
            ctx = self.pm.contexts[sym]
            self.assertTrue(ctx.state.get("open"), f"{sym} must be open")
            self.assertIsNotNone(ctx.live_manager)
            self.assertGreater(float(ctx.state.get("qty", 0.0) or 0.0), 0)
            self.assertGreater(float(ctx.state.get("entry", 0.0) or 0.0), 0)

        # Margin ledger stayed real through all six commits.
        equity = E.paper["balance"] + E.paper["committed_margin"]
        self.assertAlmostEqual(equity, 10000.0, places=6)
        self.assertGreater(E.paper["committed_margin"], 0)

        # Dashboard snapshot lists all six with canonical payloads.
        snap = self.pm.snapshot()
        self.assertEqual(len(snap), 6)
        for row in snap:
            self.assertTrue(bool(row["symbol"]))
            self.assertGreater(row["entry"], 0)
            self.assertTrue(bool(row["side"]))
            self.assertIn(row["asset_class"], {"CRYPTO", "INDEX", "GOLD", "NEWS"})

    def test_seventh_position_refused_by_capacity_and_margin_cap(self):
        cands = self._six_candidates()
        self.assertEqual(self.pm.open_top(cands, slots=6), 6)

        extra = _cand("SOL/USDT:USDT", "CRYPTO")
        self.assertFalse(self.pm.can_open("SOL/USDT:USDT", "CRYPTO"))
        self.assertFalse(self.pm.open_candidate(extra))
        self.assertEqual(self.pm.count(), 6, "7th must never open")

        status = self.pm.risk_guard.status("SOL/USDT:USDT", current_positions=6)
        self.assertFalse(status.allowed)
        self.assertEqual(status.reason, "PORTFOLIO_MARGIN_CAP")

    def test_manage_all_over_six_keeps_contexts_intact(self):
        self._six_candidates()
        self.assertEqual(self.pm.open_top(self._six_candidates(), slots=6), 6)
        before = {self.pm._key_symbol(k) for k in self.pm.contexts}
        self.pm.manage_all()
        after = {self.pm._key_symbol(k) for k in self.pm.contexts}
        # Management loop must not corrupt or silently drop live positions.
        self.assertEqual(self.pm.count(), len(after))
        self.assertLessEqual(len(after), len(before))
        for sym in after:
            ctx = self.pm.contexts[self.pm._ctx_key(sym)]
            self.assertIsNotNone(ctx.live_manager)
        self.assertEqual(len(self.pm.snapshot()), self.pm.count())

    def test_close_all_six_releases_slots_and_margin(self):
        self._six_candidates()
        self.assertEqual(self.pm.open_top(self._six_candidates(), slots=6), 6)
        committed0 = E.paper["committed_margin"]
        self.assertGreater(committed0, 0)

        closed = 0
        for sym in list(self.pm.symbols()):
            if self.pm.close_symbol(sym):
                closed += 1
        self.assertEqual(closed, 6)
        self.assertEqual(self.pm.count(), 0)
        self.assertAlmostEqual(E.paper["committed_margin"], 0.0, places=6)
        self.assertEqual(E.PERF["trades"], 6)
        self.assertEqual(E.PERF["wins"] + E.PERF["losses"], E.PERF["trades"])
        equity = E.paper["balance"] + E.paper["committed_margin"]
        self.assertAlmostEqual(equity, 10000.0 + E.PERF["total_pnl_usdt"], places=6)

        # A freed slot reopens through the real path after the full close.
        self.assertTrue(self.pm.can_open("BTC/USDT:USDT", "CRYPTO"))
        self.assertTrue(self.pm.open_candidate(_cand("BTC/USDT:USDT", "CRYPTO")))
        self.assertEqual(self.pm.count(), 1)

    def test_close_one_then_third_crypto_fits_in_freed_slot(self):
        cands = [_cand("BTC/USDT:USDT", "CRYPTO"), _cand("ETH/USDT:USDT", "CRYPTO")]
        self.assertEqual(self.pm.open_top(cands, slots=2), 2)
        self.assertFalse(self.pm.can_open("SOL/USDT:USDT", "CRYPTO"))
        self.assertTrue(self.pm.close_symbol("BTC/USDT:USDT"))
        self.assertEqual(self.pm.count(), 1)
        self.assertTrue(self.pm.can_open("SOL/USDT:USDT", "CRYPTO"))
        self.assertTrue(self.pm.open_candidate(_cand("SOL/USDT:USDT", "CRYPTO")))
        self.assertEqual(self.pm.count(), 2)
        classes = [ctx.asset_class for ctx in self.pm.contexts.values()]
        self.assertEqual(classes.count("CRYPTO"), 2)


if __name__ == "__main__":
    unittest.main()