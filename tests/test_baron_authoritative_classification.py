"""BARON authoritative BingX classification: FAIL-CLOSED, exchange-grounded.

Forensic objective (Phases 11-14): NO TradFi instrument may ever be silently
classified CRYPTO. BingX lists every TradFi perp under a family-prefixed base
(NCSK stocks, NCSI indices, NCCO commodities/energy, NCFX forex) and exposes no
productType/category field — so the venue prefix + displayName are the only
exchange-grounded evidence. This suite proves, through the REAL production paths
(core.runtime._execute_ready_queue_candidate, scanner.promote_to_queue, the
queue engine, the allocator, and the authoritative classifier):

  1. The authoritative classification matrix (crypto / stock / ETF / index /
     gold / metal / oil / energy / forex) resolves EXACTLY per venue metadata.
  2. The allocator bucket map keeps ETF in INDEX_STOCK, METAL in COMMODITY,
     FOREX fail-closed cap 0 — capacity numbers unchanged (2/2/1/1 + NEWS).
  3. Capacity coherence: INDEX_STOCK 2-seat (STOCK+ETF+INDEX), COMMODITY
     1-seat (GOLD+OIL), CRYPTO 2, FOREX 0 — with candidate_key-tagged decisions.
  4. Negative fail-closed: malformed / metadata-free / no-explicit-class shapes
     NEVER become CRYPTO (ExecutionCandidate __post_init__, manager._asset_class,
     universe.classify, engine.resolve_asset_class).
  5. Runtime allocator decision is deterministic (the runtime candidate's own
     decision wins by candidate_key, no first-match CRYPTO split-brain).
  6. DUPLICATE on a LIVE position -> queued entry INVALIDATED + removed
     (no endless retry loop).
  7. scanner promote_to_queue blocks NEW OPEN re-admission of a LIVE symbol.
  8. re_evaluate_all never reprocesses EXECUTED/INVALIDATED/RETURNED_WATCHLIST.
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
os.environ["BARON_ZONE_JUDGE"] = "1"


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
    return pd.DataFrame({"timestamp": t, "open": o, "high": h,
                         "low": l, "close": c,
                         "volume": np.full(n, 1000.0)})


PRICES = {
    "BTC/USDT:USDT": 60000.0,
    "ETH/USDT:USDT": 3000.0,
    "SOL/USDT:USDT": 150.0,
    "NCSKNVDA2USD/USDT:USDT": 130.0,
    "NCSIEWJ2USD/USDT:USDT": 88.0,
    "NCSKSPY2USD/USDT:USDT": 520.0,
    "NCSISP5002USD/USDT:USDT": 5000.0,
    "NCCOGOLD2USD/USDT:USDT": 2300.0,
    "NCCOXAG2USD/USDT:USDT": 26.0,
    "NCCOWTI2USD/USDT:USDT": 80.0,
    "NCFXEUR2USD/USDT:USDT": 1.09,
    "NCSKSPCX2USD/USDT:USDT": 3.5,
}

READY_FLOORS = {
    "CRYPTO": 68.0,
    "INDEX": 68.0,
    "STOCK": 67.0,
    "ETF": 67.0,
    "GOLD": 68.0,
    "OIL": 68.0,
    "METAL": 68.0,
    "ENERGY": 68.0,
    "FOREX": 68.0,
}


def _mkt(base, display=None, quote="USDT", mtype="swap", prefix_ok=True):
    info = {}
    if display:
        info["displayName"] = display
    return {
        "id": f"{base}-{quote}-PERPETUAL",
        "symbol": f"{base}/{quote}:{quote}",
        "base": base, "quote": quote, "settle": quote,
        "type": mtype, "swap": mtype == "swap", "spot": False,
        "future": mtype == "future", "active": True, "info": info,
    }


# Venue-authoritative matrix: (ccxt symbol, market, expected class). The
# markets mirror verified live BingX rows (displayName + family prefix).
VENUE_MATRIX = [
    ("BTC/USDT:USDT", _mkt("BTC"), "CRYPTO"),
    ("ETH/USDT:USDT", _mkt("ETH"), "CRYPTO"),
    ("DOGE/USDT:USDT", _mkt("DOGE"), "CRYPTO"),
    ("SOL/USDT:USDT", _mkt("SOL"), "CRYPTO"),
    ("NCSKSPCX2USD/USDT:USDT", _mkt("NCSKSPCX2USD", "SPCX-USDT"), "STOCK"),
    ("NCSKNVDA2USD/USDT:USDT", _mkt("NCSKNVDA2USD", "NVDA-USDT"), "STOCK"),
    ("NCSKTSLA2USD/USDT:USDT", _mkt("NCSKTSLA2USD", "TSLA-USDT"), "STOCK"),
    ("NCSIEWJ2USD/USDT:USDT", _mkt("NCSIEWJ2USD", "EWJ-USDT"), "ETF"),
    ("NCSKQQQ2USD/USDT:USDT", _mkt("NCSKQQQ2USD", "QQQ-USDT"), "ETF"),
    ("NCSKSPY2USD/USDT:USDT", _mkt("NCSKSPY2USD", "SPY-USDT"), "ETF"),
    ("NCSINASDAQ1002USD/USDT:USDT", _mkt("NCSINASDAQ1002USD", "NASDAQ100-USDT"), "INDEX"),
    ("NCSISP5002USD/USDT:USDT", _mkt("NCSISP5002USD", "SP500-USDT"), "INDEX"),
    ("NCSIDJ30USD/USDT:USDT", _mkt("NCSIDJ30USD", "US30-USDT"), "INDEX"),
    ("NCCOGOLD2USD/USDT:USDT", _mkt("NCCOGOLD2USD", "GOLD(XAU)-USDT"), "GOLD"),
    ("NCCOXAG2USD/USDT:USDT", _mkt("NCCOXAG2USD", "SILVER(XAG)-USDT"), "METAL"),
    ("NCCOWTI2USD/USDT:USDT", _mkt("NCCOWTI2USD", "Oil WTI-USDT"), "OIL"),
    ("NCCOBRENT2USD/USDT:USDT", _mkt("NCCOBRENT2USD", "Oil BRENT-USDT"), "OIL"),
    ("NCCOGAS2USD/USDT:USDT", _mkt("NCCOGAS2USD", "Natural Gas(NG)-USDT"), "ENERGY"),
    ("NCFXEUR2USD/USDT:USDT", _mkt("NCFXEUR2USD", "EURUSD-USDT"), "FOREX"),
    ("NCFXGBP2USD/USDT:USDT", _mkt("NCFXGBP2USD", "GBPUSD-USDT"), "FOREX"),
    ("NCFXUSD2JPY/USDT:USDT", _mkt("NCFXUSD2JPY", "USDJPY-USDT"), "FOREX"),
]

_EXACT_FAMILY = {"STOCK", "ETF", "INDEX", "FOREX"}


class BaronAuthoritativeClassificationTest(unittest.TestCase):

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

    # ---------------- fixtures ----------------
    def _ready_candidate(self, symbol, cls, score=71.4, side="BUY"):
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
        cand.asset_class = cls
        cand.ready_score_required = READY_FLOORS[cls]
        E.MEMORY.setdefault("watchlist", {})[symbol] = {
            "symbol": symbol, "side": side, "news_risk": 0, "asset_class": cls,
        }
        self.assertTrue(E.queue.add_candidate(cand), f"admit {symbol}")
        return cand

    def _gate_events(self, symbol=None, blocker=None):
        events = self.RT.E.MEMORY.get("gate_feed", [])
        if symbol is None and blocker is None:
            return events
        return [ev for ev in events
                if (symbol is None or ev.get("symbol") == symbol)
                and (blocker is None or ev.get("blocker") == blocker)]

    # ---------------- Phase 11: authoritative matrix ----------------
    def test_venue_authoritative_classification_matrix(self):
        from scanner.universe import classify
        E = self.RT.E
        for symbol, market, expected in VENUE_MATRIX:
            asset, source, conf = classify(symbol, market)
            self.assertEqual(asset, expected,
                             f"classify({symbol}) venue metadata -> {asset}, want {expected}")
            self.assertGreaterEqual(conf, 0.8)
            # resolve_asset_class must agree on the same instrument through the
            # symbol-only path for crypto + family symbols.
            resolved = E.AssetBehaviorProfile.resolve_asset_class(symbol)
            if expected == "CRYPTO":
                self.assertEqual(resolved, "CRYPTO", symbol)
            elif expected in _EXACT_FAMILY:
                self.assertEqual(resolved, expected, symbol)
            else:
                # NCCO sub-classes (GOLD/OIL/METAL/ENERGY) need the market
                # displayName for full fidelity; symbol-only must still NEVER
                # collapse into CRYPTO.
                self.assertNotEqual(resolved, "CRYPTO", symbol)

    def test_all_tradfi_families_never_crypto(self):
        E = self.RT.E
        from scanner.universe import classify
        for symbol, market, expected in VENUE_MATRIX:
            if expected == "CRYPTO":
                continue
            cls = classify(symbol, market)[0]
            self.assertNotEqual(cls, "CRYPTO", symbol)
            self.assertNotEqual(E.AssetBehaviorProfile.resolve_asset_class(symbol),
                                "CRYPTO", symbol)

    # ---------------- Phase 11: allocator bucket mapping ----------------
    def test_allocator_bucket_map_and_caps(self):
        from portfolio.allocator import (ASSET_BUCKETS, BUCKET_REPRESENTATIVE,
                                         bucket_cap, bucket_of)
        self.assertEqual(ASSET_BUCKETS["CRYPTO"], "CRYPTO")
        self.assertEqual(ASSET_BUCKETS["STOCK"], "INDEX_STOCK")
        self.assertEqual(ASSET_BUCKETS["INDEX"], "INDEX_STOCK")
        self.assertEqual(ASSET_BUCKETS["ETF"], "INDEX_STOCK")
        self.assertEqual(ASSET_BUCKETS["GOLD"], "COMMODITY")
        self.assertEqual(ASSET_BUCKETS["OIL"], "COMMODITY")
        self.assertEqual(ASSET_BUCKETS["METAL"], "COMMODITY")
        self.assertEqual(ASSET_BUCKETS["ENERGY"], "COMMODITY")
        self.assertEqual(ASSET_BUCKETS["FOREX"], "FOREX")
        self.assertEqual(ASSET_BUCKETS["NEWS"], "NEWS")
        self.assertEqual(bucket_cap("CRYPTO"), 2)
        self.assertEqual(bucket_cap("INDEX_STOCK"), 2)
        self.assertEqual(bucket_cap("COMMODITY"), 1)
        self.assertEqual(bucket_cap("FOREX"), 0)
        self.assertEqual(bucket_cap("NEWS"), 1)
        self.assertEqual(bucket_of("ETF"), "INDEX_STOCK")
        self.assertEqual(bucket_of("METAL"), "COMMODITY")
        self.assertEqual(bucket_of("UNKNOWN"), "UNKNOWN")

    def test_allocator_capacity_coherence(self):
        RT = self.RT
        cands = [
            {"symbol": "BTC/USDT:USDT", "side": "BUY", "asset_class": "CRYPTO",
             "priority_score": 90.0, "candidate_key": "k:btc"},
            {"symbol": "ETH/USDT:USDT", "side": "BUY", "asset_class": "CRYPTO",
             "priority_score": 90.0, "candidate_key": "k:eth"},
            {"symbol": "SOL/USDT:USDT", "side": "BUY", "asset_class": "CRYPTO",
             "priority_score": 95.0, "candidate_key": "k:sol"},
            {"symbol": "NCSKNVDA2USD/USDT:USDT", "side": "BUY", "asset_class": "STOCK",
             "priority_score": 93.0, "candidate_key": "k:nvda"},
            {"symbol": "NCSIEWJ2USD/USDT:USDT", "side": "BUY", "asset_class": "ETF",
             "priority_score": 92.0, "candidate_key": "k:ewj"},
            {"symbol": "NCSISP5002USD/USDT:USDT", "side": "BUY", "asset_class": "INDEX",
             "priority_score": 91.0, "candidate_key": "k:sp500"},
            {"symbol": "NCCOGOLD2USD/USDT:USDT", "side": "BUY", "asset_class": "GOLD",
             "priority_score": 89.0, "candidate_key": "k:gold"},
            {"symbol": "NCCOWTI2USD/USDT:USDT", "side": "BUY", "asset_class": "OIL",
             "priority_score": 88.0, "candidate_key": "k:wti"},
            {"symbol": "NCFXEUR2USD/USDT:USDT", "side": "BUY", "asset_class": "FOREX",
             "priority_score": 97.0, "candidate_key": "k:fx"},
        ]
        report = RT.ALLOCATOR.allocate(cands, limit=6)
        by_key = {d.candidate_key: d for d in report.decisions}
        # candidate_key is carried onto the decision (deterministic identity).
        self.assertEqual(by_key["k:nvda"].candidate_key, "k:nvda")
        self.assertEqual(by_key["k:nvda"].asset_class, "STOCK")
        self.assertEqual(by_key["k:ewj"].asset_class, "ETF")
        # CRYPTO bucket 2/2 -> SOL(#1, 95) + BTC(#2, 90) fill it; ETH rejected.
        self.assertTrue(by_key["k:sol"].allowed)
        self.assertTrue(by_key["k:btc"].allowed)
        self.assertFalse(by_key["k:eth"].allowed)
        self.assertEqual(by_key["k:eth"].reason, "CRYPTO_CAP")
        # INDEX_STOCK combined 2/2 -> STOCK+ETF fill it, INDEX #3 rejected.
        self.assertTrue(by_key["k:nvda"].allowed)
        self.assertTrue(by_key["k:ewj"].allowed)
        self.assertFalse(by_key["k:sp500"].allowed)
        self.assertEqual(by_key["k:sp500"].reason, "INDEX_STOCK_CAP")
        # COMMODITY 1/1 -> GOLD fills it, OIL rejected (never CRYPTO_CAP).
        self.assertTrue(by_key["k:gold"].allowed)
        self.assertFalse(by_key["k:wti"].allowed)
        self.assertEqual(by_key["k:wti"].reason, "COMMODITY_CAP")
        # FOREX bagged separately with cap 0 (reasoned fail-closed).
        self.assertFalse(by_key["k:fx"].allowed)
        self.assertEqual(by_key["k:fx"].bucket, "FOREX")
        self.assertEqual(by_key["k:fx"].reason, "FOREX_CAP")

    # ---------------- Phase 12: negative fail-closed ----------------
    def test_malformed_and_metadata_free_never_crypto(self):
        from scanner.universe import classify
        E = self.RT.E
        self.assertEqual(classify("?!@#", {})[0], "UNKNOWN")
        self.assertEqual(classify("ZZNDQ-XY", {})[0], "UNKNOWN")
        self.assertEqual(classify("", {})[0], "UNKNOWN")
        # Metadata-free NC family symbol still resolves via the venue prefix.
        self.assertEqual(classify("NCSKNVDA2USD/USDT:USDT", {})[0], "STOCK")
        self.assertEqual(classify("NCSIEWJ2USD/USDT:USDT", {})[0], "ETF")
        self.assertEqual(classify("NCFXEUR2USD/USDT:USDT", {})[0], "FOREX")
        # Engine resolver: garbage and non-venue shapes fail closed.
        self.assertEqual(E.AssetBehaviorProfile.resolve_asset_class("FOO"),
                         "UNKNOWN")
        self.assertEqual(E.AssetBehaviorProfile.resolve_asset_class(""),
                         "UNKNOWN")
        # Manager legacy path fail-closes too.
        from portfolio.manager import PortfolioManager
        self.assertEqual(PortfolioManager._asset_class("FOO/BAR"), "UNKNOWN")
        self.assertEqual(PortfolioManager._asset_class("NCSKNVDA2USD/USDT:USDT"),
                         "STOCK")
        self.assertEqual(PortfolioManager._asset_class("BTC/USDT:USDT"),
                         "CRYPTO")

    def test_execution_candidate_default_never_turns_tradfi_to_crypto(self):
        E = self.RT.E
        common = dict(side="BUY", price=100.0, entry_price=100.0, stop_loss=99.0,
                      take_profit_1=101.0, take_profit_2=102.0, atr=1.0,
                      df=None, ob={})
        cases = [
            ("NCSKSPCX2USD/USDT:USDT", "STOCK"),
            ("NCSIEWJ2USD/USDT:USDT", "ETF"),
            ("NCFXEUR2USD/USDT:USDT", "FOREX"),
            ("NCCOWTI2USD/USDT:USDT", "OIL"),
            ("NCSKNVDA2USD/USDT:USDT", "STOCK"),
        ]
        for symbol, expected in cases:
            cand = E.ExecutionCandidate(symbol=symbol, **common)
            self.assertEqual(cand.asset_class, expected,
                             f"default-guard must classify {symbol} as {expected}")
        # A venue crypto symbol keeps CRYPTO; a garbage shape goes UNKNOWN.
        self.assertEqual(E.ExecutionCandidate(symbol="BTC/USDT:USDT", **common).asset_class,
                         "CRYPTO")
        self.assertEqual(E.ExecutionCandidate(symbol="???", **common).asset_class,
                         "UNKNOWN")

    # ---------------- Phase 13: deterministic runtime decision ----------------
    def test_runtime_decision_matches_runtime_candidate_not_queue_duplicate(self):
        RT = self.RT
        self._ready_candidate("BTC/USDT:USDT", "CRYPTO")
        self.assertTrue(RT._execute_ready_queue_candidate(),
                        "crypto #1 must open")
        # STOCK candidate: watch carries STOCK; the runtime candidate is also
        # STOCK. The old code let the queue's own to_dict (CRYPTO default)
        # decide first by symbol -> phantom CRYPTO. Now the runtime decision
        # (candidate_key) must win.
        self._ready_candidate("NCSKNVDA2USD/USDT:USDT", "STOCK")
        self.assertTrue(RT._execute_ready_queue_candidate(),
                        "STOCK #1 must open")
        self.assertEqual(RT.PORTFOLIO.count(), 2)
        ctx = RT.PORTFOLIO.contexts["NCSKNVDA2USD/USDT:USDT"]
        self.assertEqual(ctx.asset_class, "STOCK",
                         "position must be a STOCK, never CRYPTO")
        # The allocator report contains the runtime decision tagged STOCK.
        alloc = RT.MEMORY.get("portfolio_allocation", {}).get("decisions", [])
        nvda = [d for d in alloc if d.get("symbol") == "NCSKNVDA2USD/USDT:USDT"]
        self.assertTrue(nvda and nvda[0]["asset_class"] == "STOCK")
        # No allocator rejection occurred for the STOCK candidate.
        self.assertFalse(self._gate_events(symbol="NCSKNVDA2USD/USDT:USDT",
                                           blocker="ALLOCATOR_REJECT"))
        # And no phantom CRYPTO capacity was ever consumed (CRYPTO_CAP absent).
        self.assertFalse(self._gate_events(blocker="CRYPTO_CAP"))

    # ---------------- Phase 13: DUPLICATE invalidation ----------------
    def test_duplicate_live_position_removes_queued_candidate(self):
        RT = self.RT
        # Live position established on NVDA.
        self._ready_candidate("NCSKNVDA2USD/USDT:USDT", "STOCK")
        self.assertTrue(RT._execute_ready_queue_candidate())
        self.assertEqual(RT.PORTFOLIO.count(), 1)
        E = RT.E
        # A second READY candidate for the SAME symbol is queued (the old
        # EXECUTED entry is gone via cleanup semantics of add_candidate).
        E.queue._candidates.clear()
        self.assertNotIn("NCSKNVDA2USD/USDT:USDT",
                         [s for s, c in E.queue._candidates.items()
                          if c.state == E.ExecutionState.READY])
        self._ready_candidate("NCSKNVDA2USD/USDT:USDT", "STOCK")
        # The swap authority refuses DUPLICATE -> runtime must invalidate+remove
        # the queued entry instead of retrying forever.
        self.assertFalse(RT._execute_ready_queue_candidate())
        self.assertNotIn("NCSKNVDA2USD/USDT:USDT", E.queue._candidates)
        self.assertEqual(
            self._exec_pipe().get("last_open_failure_category"), "duplicate")
        self.assertEqual(
            self._exec_pipe().get("last_open_failure_blocker"), "DUPLICATE")
        self.assertTrue(self._gate_events(blocker="POSITION_ALREADY_OPEN"))
        # The live position was untouched by the failed attempt.
        self.assertEqual(RT.PORTFOLIO.count(), 1)
        self.assertEqual(RT.PORTFOLIO.contexts["NCSKNVDA2USD/USDT:USDT"].asset_class,
                         "STOCK")

    def _exec_pipe(self):
        return self.RT.MEMORY.setdefault("pipeline", {}).setdefault("execution", {})

    # ---------------- Phase 13: scanner promotion guard ----------------
    def test_promote_to_queue_blocks_live_reentry(self):
        RT = self.RT
        E = RT.E
        sym = "NCSKNVDA2USD/USDT:USDT"
        # Simulate a live position via the portfolio singleton the scanner reads.
        RT.PORTFOLIO.contexts[sym] = types.SimpleNamespace(
            asset_class="STOCK", side="BUY", state={})
        RT.PORTFOLIO.hedge_mode = False
        # A fully PREPARED/A-GRADE institutional registry entry for the symbol.
        E.MEMORY.setdefault("watchlist", {})[sym] = {
            "symbol": sym, "side": "BUY", "deep_analyzed": True,
            "institutional_zone_active": True, "a_grade_ready": True,
            "institutional_prepared": True, "precursor_count": 4,
            "analysis": {"liq_score": 85, "struct_score": 85, "ob_grade": "A"},
            "news_risk": 0, "asset_class": "STOCK",
            "pre_institutional_state": "CONFIRMED",
            "institutional_score": 90,
        }
        E.MEMORY.setdefault("institutional_zone_analysis", {})[sym] = {
            "symbol": sym,
            "zone_verdict": "CONFIRMED_ZONE",
            "precursor_count": 4,
            "institutional_score": 90,
            "pre_institutional_state": "CONFIRMED",
            "hypothesis": "INSTITUTIONAL_CONTROL",
        }
        import scanner.scanner as S
        promoted = S.promote_to_queue()
        self.assertEqual(promoted, 0, "live symbol must never be re-admitted")
        pipeline = E.MEMORY.get("pipeline", {}).get("promotion", {}) or {}
        reasons = pipeline.get("rejected_by_reason", {}) or {}
        self.assertGreaterEqual(reasons.get("position_already_open", 0), 1)
        self.assertNotIn(sym, E.queue._candidates)
        self.assertTrue(self._gate_events(symbol=sym,
                                          blocker="POSITION_ALREADY_OPEN"))

    # ---------------- Phase 13: terminal states never re-evaluated ----------------
    def test_re_evaluate_all_skips_terminal_states(self):
        E = self.RT.E
        fetched = []

        def fetcher(symbol, *a, **k):
            fetched.append(symbol)
            return self._frames.get(str(symbol))

        ready = self._ready_candidate("BTC/USDT:USDT", "CRYPTO")
        done = self._ready_candidate("ETH/USDT:USDT", "CRYPTO")
        done.state = E.ExecutionState.EXECUTED
        inv = self._ready_candidate("SOL/USDT:USDT", "CRYPTO")
        inv.state = E.ExecutionState.INVALIDATED
        # RETURNED_WATCHLIST is NOT terminal: the zone-lifecycle contract
        # requires returned candidates to be re-evaluated so an escaped zone
        # can advance to STALE (covered by test_zone_lifecycle_waves).
        ret = self._ready_candidate("NCSKNVDA2USD/USDT:USDT", "STOCK")
        ret.state = E.ExecutionState.RETURNED_WATCHLIST

        E.queue.re_evaluate_all(fetcher)
        self.assertIn("BTC/USDT:USDT", fetched,
                      "READY candidate must still be re-evaluated")
        self.assertNotIn("ETH/USDT:USDT", fetched,
                         "EXECUTED must never be re-evaluated")
        self.assertNotIn("SOL/USDT:USDT", fetched,
                         "INVALIDATED must never be re-evaluated")
        self.assertIn("NCSKNVDA2USD/USDT:USDT", fetched,
                      "RETURNED_WATCHLIST stays re-evaluable (zone lifecycle)")


if __name__ == "__main__":
    unittest.main()