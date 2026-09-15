"""ROOT-CAUSE regression: BARON ZONE/OB judge vs the queue READY authority.

Forensic finding (zero-trade funnel): execute_entry emits OPEN_REQUESTED at the
top, then the BARON ZONE/OB judge gate (core/engine.py) fails-closed on ANY
verdict other than ENTER_NOW. The judge scores with its own weight set and a
materially higher ENTER_NOW bar (final_zone_score >= 72 + struct_ok +
rejection/displacement/sweep) than the queue's class-aware READY floor
(68-70). For the SAME frame the queue granted READY=88, the judge returned
WAIT_RETEST (score 58.7) — every READY candidate was rejected after
OPEN_REQUESTED, producing READY > 0 / EXECUTED = 0 on the dashboard.

The judge's own contract (baron_zone_judge.py) defines WAIT_RETEST as "zone is
VALID but entry needs a retest/confirmation" — the queue's READY authority has
already answered that question (in-entry-window + trigger + confirmed +
not-extended, re-verified every QUEUE_RE_EVAL_INTERVAL). The fix applies the
existing _ready_execution_grace contract to the judge: BLOCK verdicts (broken
zone, consumed OB, S/R flip, data unusable) remain hard fail-closed for EVERY
candidate; WAIT_RETEST becomes advisory ONLY for a fresh READY-validated grant
(execution_context.is_ready_validated + ready_ts within grace), and stays
hard-blocked for non-READY / fallback / stale grants.

These tests drive the REAL runtime executor / execute_entry with the
production default BARON_ZONE_JUDGE=1 (conftest disables it for the offline
suite; this file opts back in explicitly).
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
# NOTE: no module-level NEWS_ENABLED/other-toxic env sets here — module-level
# env mutation during pytest collection leaks into every later-imported module
# (see the documented sibling-pollution note in test_runtime_repairs.py:273).
os.environ["BARON_ZONE_JUDGE"] = "1"  # production default: fail-closed judge


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
                "AAPL/USDT:USDT": {"base": "AAPL", "quote": "USDT", "type": "swap", "active": True},
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


class BaronGateReadyGraceTest(unittest.TestCase):
    """Production BARON gate semantics with BARON_ZONE_JUDGE=1."""

    @classmethod
    def setUpClass(cls):
        cls._saved_modules = sys.modules.copy()
        cls.RT = _load_runtime()

    @classmethod
    def tearDownClass(cls):
        _restore_modules(cls._saved_modules)

    def setUp(self):
        os.environ["BARON_ZONE_JUDGE"] = "1"
        RT = self.RT
        E = RT.E
        self._saved = (E.get_ohlcv_safe, E.get_ticker_safe,
                       E.get_orderbook_cached, E.get_balance_safe)
        self._prices = {"BTC/USDT:USDT": 60000.0, "ETH/USDT:USDT": 3000.0,
                        "SOL/USDT:USDT": 150.0, "AAPL/USDT:USDT": 200.0}
        self._frames = {s: _frame(base=p) for s, p in self._prices.items()}
        # A 15-row frame is valid OHLCV for the entry boundary (>=10 rows) but is
        # BLOCKed by the judge as INSUFFICIENT_DATA (<30 rows, MIN_ROWS).
        self._short = {s: _frame(n=15, base=p) for s, p in self._prices.items()}

        def provider(symbol, limit=120, htf=False):
            return self._frames.get(str(symbol), self._short.get(str(symbol)))

        E.get_ohlcv_safe = provider
        E.get_ticker_safe = lambda symbol, retries=3: self._prices.get(str(symbol), 100.0)
        E.get_orderbook_cached = lambda *a, **k: {
            "bids": [[self._prices.get(str(a[0]), 100.0) - 1.0, 10.0]],
            "asks": [[self._prices.get(str(a[0]), 100.0) + 1.0, 5.0]]}
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

    def _ready_candidate(self, symbol, score=71.4, ready_floor=68.0,
                         side="BUY", asset_class="CRYPTO"):
        E = self.RT.E
        price = self._prices[symbol]
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
        cand.asset_class = asset_class
        cand.ready_score_required = float(ready_floor)
        self._govern_watch(symbol, asset_class=asset_class)
        self.assertTrue(E.queue.add_candidate(cand), f"admit {symbol}")
        E.queue._record_opportunity_lifecycle(cand)
        return cand

    def _lifecycle_events(self):
        return [str(e.get("event") or e.get("state") or "")
                for e in self.RT.E.DASHBOARD_STATE.get("trade_lifecycle", [])]

    # ---- Root-cause regression: judge ON, READY candidate MUST now execute ----
    def test_production_judge_on_ready_candidate_executes(self):
        RT = self.RT
        E = RT.E
        cand = self._ready_candidate("BTC/USDT:USDT", score=71.4, ready_floor=68.0)
        self.assertTrue(RT._execute_ready_queue_candidate(),
                        "fresh READY grant must execute under BARON_ZONE_JUDGE=1")
        out = RT.MEMORY.setdefault("pipeline", {}).setdefault("execution", {})
        self.assertEqual(out.get("last_outcome"), "executed")
        self.assertEqual(RT.PORTFOLIO.count(), 1)
        self.assertEqual(cand.state, E.ExecutionState.EXECUTED)
        # The judge verdict was advisory, and that decision is VISIBLE.
        feed = RT.MEMORY.get("gate_feed", [])
        self.assertTrue(any(ev.get("blocker") == "BARON_ADVISORY"
                            for ev in feed),
                        "WAIT_RETEST under READY grace must be recorded as advisory")
        self.assertFalse(any(ev.get("blocker") == "BARON_REJECT" for ev in feed),
                         "no hard BARON reject on a fresh READY grant")

    # ---- Root-cause mechanism preserved: non-READY/fallback stays blocked ----
    def test_judge_wait_retest_blocks_non_ready_fallback(self):
        RT = self.RT
        E = RT.E
        price = self._prices["BTC/USDT:USDT"]
        atr = price * 0.01
        plain = {
            "symbol": "BTC/USDT:USDT", "side": "BUY", "price": price,
            "sl": price - atr * 1.6, "tp1": price + atr * 1.5,
            "tp2": price + atr * 2.5, "score": 71.4, "atr": atr,
            "asset_class": "CRYPTO", "trade_id": "FALLBACK-1",
        }
        # No execution_context => not READY-validated => judge stays fail-closed.
        self.assertFalse(RT.PORTFOLIO.open_candidate(plain))
        self.assertEqual(RT.PORTFOLIO.count(), 0)
        blockers = RT.MEMORY.get("execution_blockers", [])
        self.assertTrue(any(b["blocker"] == "BARON_REJECT" and "WAIT_RETEST" in b["reason"]
                            for b in blockers),
                        "non-READY fallback must still be hard-blocked by the judge")
        events = self._lifecycle_events()
        self.assertIn("OPEN_REQUESTED", events)
        self.assertIn("OPEN_REJECTED", events,
                      "OPEN_REQUESTED must resolve to OPEN_REJECTED when the gate rejects")

    # ---- Objective BLOCK (broken/invalidated location) is NEVER bypassed ----
    def test_judge_block_never_bypassed_even_for_ready(self):
        RT = self.RT
        E = RT.E
        # 15-row frame: passes the OHLCV boundary but the judge BLOCKs it as
        # INSUFFICIENT_DATA (< MIN_ROWS). A READY-validated candidate must NOT
        # override an objective BLOCK.
        self._frames["BTC/USDT:USDT"] = self._short["BTC/USDT:USDT"]
        cand = self._ready_candidate("BTC/USDT:USDT", score=71.4, ready_floor=68.0)
        executed = RT._execute_ready_queue_candidate()
        self.assertFalse(executed)
        self.assertEqual(RT.PORTFOLIO.count(), 0)
        blockers = RT.MEMORY.get("execution_blockers", [])
        self.assertTrue(any(b["blocker"] == "BARON_REJECT" and "BLOCK" in b.get("reason", "")
                            for b in blockers),
                        "objective BLOCK must fail-closed even for a READY grant")

    # ---- Telemetry: OPEN_REQUESTED always reaches a terminal outcome ----
    def test_open_requested_resolves_on_success_and_failure(self):
        RT = self.RT
        E = RT.E
        cand = self._ready_candidate("BTC/USDT:USDT", score=71.4, ready_floor=68.0)
        self.assertTrue(RT._execute_ready_queue_candidate())
        events = self._lifecycle_events()
        self.assertIn("OPEN_REQUESTED", events)
        self.assertIn("OPEN_CONFIRMED", events,
                      "success must terminate the open request with OPEN_CONFIRMED")
        # Now force a rejection and confirm the OPEN_REJECTED terminal event.
        self._frames["ETH/USDT:USDT"] = self._short["ETH/USDT:USDT"]
        self._ready_candidate("ETH/USDT:USDT", score=71.4, ready_floor=68.0)
        self.assertFalse(RT._execute_ready_queue_candidate())
        events2 = self._lifecycle_events()
        self.assertIn("OPEN_REQUESTED", events2)
        self.assertIn("OPEN_REJECTED", events2,
                      "a rejected open request must never dangle")

    # ---- No duplicate execution on a repeated READY grant ----
    def test_no_duplicate_execution_for_same_symbol(self):
        RT = self.RT
        E = RT.E
        self._ready_candidate("BTC/USDT:USDT", score=71.4, ready_floor=68.0)
        self.assertTrue(RT._execute_ready_queue_candidate())
        self.assertEqual(RT.PORTFOLIO.count(), 1)
        # (a) Queue-level: the executed candidate remains in the queue as EXECUTED,
        # so the executor can never re-select the same symbol as READY.
        self.assertEqual(E.queue._candidates["BTC/USDT:USDT"].state,
                         E.ExecutionState.EXECUTED)
        self.assertIsNone(E.queue.get_best_candidate(),
                          "no READY grant may remain for an already-executed symbol")
        # (b) Manager-level: even a direct open attempt on the open symbol fails.
        price = self._prices["BTC/USDT:USDT"]
        atr = price * 0.01
        fresh = {
            "symbol": "BTC/USDT:USDT", "side": "BUY", "price": price,
            "sl": price - atr * 1.6, "tp1": price + atr * 1.5,
            "tp2": price + atr * 2.5, "score": 71.4, "atr": atr,
            "asset_class": "CRYPTO",
            "execution_context": {"is_ready_validated": True, "ready_ts": time.time()},
        }
        self.assertFalse(RT.PORTFOLIO.open_candidate(fresh))
        self.assertEqual(RT.PORTFOLIO.count(), 1,
                         "a symbol already open must never open a second time")


if __name__ == "__main__":
    unittest.main(verbosity=2)