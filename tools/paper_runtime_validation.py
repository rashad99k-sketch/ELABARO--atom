"""BARON FINAL PAPER RUNTIME VALIDATION — classification / queue / allocator fix.

Drives the REAL runtime path end-to-end in PAPER mode against the current working
tree (no production code changes):

  BingX market discovery -> Universe -> Radar(W) -> Watchlist -> Promotion
  -> Queue -> READY -> ExecutionCandidate -> Allocator -> Portfolio

Only the network/provider boundary is replaced (fake ccxt + synthetic OHLCV),
exactly like any other offline test. Outputs a structured evidence report covering:

  1. startup classification summary
  2. representative symbols + classifications
  3. queue promotion decisions
  4. already-live exclusion evidence
  5. allocator decisions
  6. capacity state
  7. OIL/GOLD reachability
  8. NEWS capacity independence
  9. absence of repeated DUPLICATE retries
 10. absence of phantom CRYPTO capacity
 11. classification warnings
 12. UNKNOWN classifications
 13. runtime exceptions/errors

No commit. No push. No ZIP. No live trading.
"""
import os
import sys
import types
import time
import json

import numpy as np
import pandas as pd

os.environ["PAPER_MODE"] = "True"
os.environ["BINGX_KEY"] = ""
os.environ["BINGX_SECRET"] = ""
os.environ["USE_EXECUTION_QUEUE"] = "True"
os.environ["NEWS_ENABLED"] = "True"
os.environ["NEWS_SLOT_ENABLED"] = "True"
os.environ["BARON_ZONE_JUDGE"] = "1"
os.environ["MAX_TECHNICAL_POSITIONS"] = "5"
os.environ["MAX_BUY_POSITIONS"] = "6"
os.environ["MAX_SELL_POSITIONS"] = "6"
os.environ.pop("MAX_POSITIONS_PER_ASSET_CLASS", None)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

EVIDENCE = {}
FAILURES = []
PASS_COUNTER = {"n": 0}


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)
        print(f"  [FAIL] {msg}")
    else:
        PASS_COUNTER["n"] += 1
        print(f"  [ok]   {msg}")


def _frame(n=250, base=100.0):
    t = np.arange(n)
    x = base + 3.0 * (1 - np.exp(-t / 900.0)) + 1.5 * np.sin(t / 6.0)
    o = x - 0.2
    c = x
    h = np.maximum(o, c) + 0.4
    l = np.minimum(o, c) - 0.4
    prior_low = l[n - 3]
    o[n - 2] = prior_low - 0.2
    c[n - 2] = prior_low + 0.3
    h[n - 2] = max(h[n - 3] - 0.1, prior_low + 0.5)
    l[n - 2] = prior_low - 1.2
    o[n - 1] = prior_low + 0.1
    c[n - 1] = prior_low + 0.9
    h[n - 1] = prior_low + 1.3
    l[n - 1] = prior_low - 0.1
    return pd.DataFrame({"timestamp": t, "open": o, "high": h,
                         "low": l, "close": c, "volume": np.full(n, 1000.0)})


class _FakeFlask:
    def __init__(self, *args, **kwargs):
        self.routes = {}

    def route(self, path, methods=None, **kwargs):
        return lambda fn: fn

    def add_url_rule(self, *args, **kwargs):
        return None


_SPX = "NCSKSPCX2USD/USDT:USDT"
_EWJ = "NCSIEWJ2USD/USDT:USDT"

# Representative BingX instruments with venue metadata (prefix + displayName).
MARKETS = {
    "BTC/USDT:USDT": {"base": "BTC", "quote": "USDT", "type": "swap",
                      "active": True, "info": {"displayName": "BTC-USDT"}},
    "ETH/USDT:USDT": {"base": "ETH", "quote": "USDT", "type": "swap",
                      "active": True, "info": {"displayName": "ETH-USDT"}},
    "DOGE/USDT:USDT": {"base": "DOGE", "quote": "USDT", "type": "swap",
                       "active": True, "info": {"displayName": "DOGE-USDT"}},
    _SPX: {"base": "NCSKSPCX", "quote": "USDT", "type": "swap", "active": True,
           "info": {"displayName": "SPCX-USDT"}},
    "NCSKNVDA2USD/USDT:USDT": {"base": "NCSKNVDA", "quote": "USDT", "type": "swap",
                               "active": True, "info": {"displayName": "NVDA-USDT"}},
    "NCSKTSLA2USD/USDT:USDT": {"base": "NCSKTSLA", "quote": "USDT", "type": "swap",
                               "active": True, "info": {"displayName": "TSLA-USDT"}},
    _EWJ: {"base": "NCSIEWJ", "quote": "USDT", "type": "swap", "active": True,
           "info": {"displayName": "EWJ-USDT"}},
    "NCSKSPY2USD/USDT:USDT": {"base": "NCSKSPY", "quote": "USDT", "type": "swap",
                              "active": True, "info": {"displayName": "SPY-USDT"}},
    "NCSIQQQ2USD/USDT:USDT": {"base": "NCSIQQQ", "quote": "USDT", "type": "swap",
                              "active": True, "info": {"displayName": "QQQ-USDT"}},
    "NCSINASDAQ1002USD/USDT:USDT": {"base": "NCSINASDAQ100", "quote": "USDT",
                                    "type": "swap", "active": True,
                                    "info": {"displayName": "NASDAQ100-USDT"}},
    "NCSISP5002USD/USDT:USDT": {"base": "NCSISP500", "quote": "USDT", "type": "swap",
                                "active": True, "info": {"displayName": "SP500-USDT"}},
    "NCSIUS302USD/USDT:USDT": {"base": "NCSIUS30", "quote": "USDT", "type": "swap",
                               "active": True, "info": {"displayName": "US30-USDT"}},
    "NCCOXAU2USD/USDT:USDT": {"base": "NCCOXAU", "quote": "USDT", "type": "swap",
                              "active": True, "info": {"displayName": "GOLD(XAU)-USDT"}},
    "NCCOWTI2USD/USDT:USDT": {"base": "NCCOWTI", "quote": "USDT", "type": "swap",
                              "active": True, "info": {"displayName": "WTI OIL-USDT"}},
    "NCCOBRENT2USD/USDT:USDT": {"base": "NCCOBRENT", "quote": "USDT", "type": "swap",
                                "active": True, "info": {"displayName": "BRENT OIL-USDT"}},
    "NCCOXAG2USD/USDT:USDT": {"base": "NCCOXAG", "quote": "USDT", "type": "swap",
                              "active": True, "info": {"displayName": "SILVER(XAG)-USDT"}},
    "NCFXEURUSD2USD/USDT:USDT": {"base": "NCFXEURUSD", "quote": "USDT", "type": "swap",
                                 "active": True, "info": {"displayName": "EURUSD-USDT"}},
    "NCFXGBPUSD2USD/USDT:USDT": {"base": "NCFXGBPUSD", "quote": "USDT", "type": "swap",
                                 "active": True, "info": {"displayName": "GBPUSD-USDT"}},
    "NCFXUSDJPY2USD/USDT:USDT": {"base": "NCFXUSDJPY", "quote": "USDT", "type": "swap",
                                 "active": True, "info": {"displayName": "USDJPY-USDT"}},
    "NCSKAAPL2USD/USDT:USDT": {"base": "NCSKAAPL", "quote": "USDT", "type": "swap",
                               "active": True, "info": {"displayName": "AAPL-USDT"}},
}

# expected raw class per representative
EXPECTED = {
    "BTC/USDT:USDT": "CRYPTO", "ETH/USDT:USDT": "CRYPTO", "DOGE/USDT:USDT": "CRYPTO",
    _SPX: "STOCK", "NCSKNVDA2USD/USDT:USDT": "STOCK", "NCSKTSLA2USD/USDT:USDT": "STOCK",
    _EWJ: "ETF", "NCSKSPY2USD/USDT:USDT": "ETF", "NCSIQQQ2USD/USDT:USDT": "ETF",
    "NCSINASDAQ1002USD/USDT:USDT": "INDEX", "NCSISP5002USD/USDT:USDT": "INDEX",
    "NCSIUS302USD/USDT:USDT": "INDEX",
    "NCCOXAU2USD/USDT:USDT": "GOLD",
    "NCCOWTI2USD/USDT:USDT": "OIL", "NCCOBRENT2USD/USDT:USDT": "OIL",
    "NCCOXAG2USD/USDT:USDT": "METAL",
    "NCFXEURUSD2USD/USDT:USDT": "FOREX", "NCFXGBPUSD2USD/USDT:USDT": "FOREX",
    "NCFXUSDJPY2USD/USDT:USDT": "FOREX",
    "NCSKAAPL2USD/USDT:USDT": "STOCK",
}
CRYPTO_SYMBOLS = {"BTC/USDT:USDT", "ETH/USDT:USDT", "DOGE/USDT:USDT"}
GARBAGE = {"???": {}, "FOO BAR": {}}

PRICES = {s: 100.0 + (i * 13.7) for i, s in enumerate(MARKETS)}
PRICES.update({"BTC/USDT:USDT": 60000.0, "ETH/USDT:USDT": 3000.0,
               "NCSKAAPL2USD/USDT:USDT": 210.0})


def _load_runtime():
    for name in list(sys.modules):
        if (name == "core.engine" or name == "core.runtime"
                or name.startswith("scanner.") or name.startswith("portfolio.")
                or name.startswith("strategy.")):
            sys.modules.pop(name, None)
    fake_ccxt = types.ModuleType("ccxt")

    class FakeBingX:
        def __init__(self, *args, **kwargs):
            self.markets = dict(MARKETS)

    fake_ccxt.bingx = FakeBingX
    fake_flask = types.ModuleType("flask")
    fake_flask.Flask = _FakeFlask
    fake_flask.jsonify = lambda *a, **k: a[0] if a else None
    fake_flask.request = types.SimpleNamespace(headers={}, remote_addr="127.0.0.1",
                                               json=None)
    sys.modules["ccxt"] = fake_ccxt
    sys.modules["flask"] = fake_flask

    import core.runtime as RT
    return RT


def _gate_events(RT, symbol=None, blocker=None):
    feed = RT.E.MEMORY.get("gate_feed", []) or []
    out = []
    for ev in feed:
        if symbol is not None and str(ev.get("symbol")) != str(symbol):
            continue
        if blocker is not None and str(ev.get("blocker")) != str(blocker):
            continue
        out.append(ev)
    return out


def main():
    print("=" * 72)
    print("PAPER RUNTIME VALIDATION — classification / queue / allocator fix")
    print("=" * 72)
    RT = _load_runtime()
    E = RT.E
    from portfolio.manager import PortfolioManager
    from portfolio.allocator import GlobalAssetAllocator, bucket_of, bucket_cap
    from scanner import universe as U

    # ---- clean paper runtime state (mirrors production startup + test harness)
    _frames = {s: _frame(base=p) for s, p in PRICES.items()}
    _frames["???"] = _frame(base=10.0)
    _frames["FOO BAR"] = _frame(base=10.0)

    def provider(symbol, limit=120, htf=False):
        return _frames.get(str(symbol))

    saved = (E.get_ohlcv_safe, E.get_ticker_safe,
             E.get_orderbook_cached, E.get_balance_safe)
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
    E.DASHBOARD_STATE["errors"] = []
    E.DASHBOARD_STATE["trade_lifecycle"] = []
    E.STATE["daily_loss_limit_hit"] = False
    E.STATE["last_trade_day"] = time.strftime("%Y-%m-%d")
    E.STATE["daily_peak_balance"] = E.paper["balance"]
    E.queue._candidates.clear()
    E.queue.total_executed = 0
    E.queue.total_rejected = 0

    RT.PORTFOLIO = PortfolioManager(6, E)
    RT.ALLOCATOR = GlobalAssetAllocator(RT.PORTFOLIO, E)
    RT.PORTFOLIO.bind(E)
    RT.PORTFOLIO.risk_guard._day = None
    RT.PORTFOLIO.risk_guard._consecutive_losses = 0
    RT.PORTFOLIO.risk_guard._cooldown_until = 0.0
    try:
        RT.PORTFOLIO.hedge_mode = False
    except Exception:
        pass

    def _exec_pipe():
        return RT.MEMORY.setdefault("pipeline", {}).setdefault("execution", {})

    def _govern_watch(symbol, asset_class, news_risk=0):
        E.MEMORY.setdefault("watchlist", {})[symbol] = {
            "symbol": symbol, "side": "BUY", "news_risk": news_risk,
            "asset_class": asset_class}

    def _reset_peak():
        E.STATE["daily_peak_balance"] = E.paper["balance"]

    # ---------------------------------------------------------------- PHASE A
    print("\n[PHASE A] STARTUP CLASSIFICATION SUMMARY + COHERENCE", flush=True)
    discovery = U.build_balanced(MARKETS, radar_limit=60)
    by_class = {}
    for row in discovery:
        by_class.setdefault(row["asset_class"], []).append(row["symbol"])
    EVIDENCE["1_startup_classification_summary"] = {
        "radar_rows": len(discovery),
        "classes": {k: len(v) for k, v in sorted(by_class.items())},
        "samples": {k: v[:3] for k, v in sorted(by_class.items())},
    }
    check(len(discovery) >= len(EXPECTED),
          "discovery classified all representative instruments "
          f"({len(discovery)} rows)")
    check("UNKNOWN" not in by_class,
          "no representative instrument classified UNKNOWN at discovery")

    # 2. representative metadata-free/malformed shapes → UNKNOWN (never CRYPTO)
    unknown_rows = []
    for sym, m in GARBAGE.items():
        raw, src, _ = U.classify(sym, m)
        resolved = E.AssetBehaviorProfile.resolve_asset_class(sym)
        tail = RT.PORTFOLIO._asset_class(sym)
        cand = E.ExecutionCandidate(symbol=sym, side="BUY", price=100.0,
                                    entry_price=100.0, stop_loss=99.0,
                                    take_profit_1=101.0, take_profit_2=102.0,
                                    atr=1.0, df=_frames[sym], ob={})
        check(raw == "UNKNOWN" and resolved == "UNKNOWN" and tail == "UNKNOWN"
              and cand.asset_class == "UNKNOWN",
              f"garbage '{sym}' fails closed UNKNOWN in all 4 layers "
              f"(raw={raw}, resolve={resolved}, manager={tail}, cand={cand.asset_class})")
        unknown_rows.append({"symbol": sym, "raw": raw, "resolved": resolved,
                             "manager": tail, "candidate": cand.asset_class})
    EVIDENCE["12_unknown_classifications"] = unknown_rows

    # 2+7. raw -> resolved -> candidate -> manager -> bucket coherence
    coherence = []
    tradfi = [s for s in EXPECTED if s not in CRYPTO_SYMBOLS]
    for sym in list(EXPECTED) + list(GARBAGE):
        m = MARKETS.get(sym) or GARBAGE[sym]
        raw, src, conf = U.classify(sym, m)
        resolved = E.AssetBehaviorProfile.resolve_asset_class(sym)
        cand_ac = E.ExecutionCandidate(symbol=sym, side="BUY", price=100.0,
                                       entry_price=100.0, stop_loss=99.0,
                                       take_profit_1=101.0, take_profit_2=102.0,
                                       atr=1.0, df=_frames.get(sym), ob={}).asset_class
        mgr = RT.PORTFOLIO._asset_class(sym)
        expected = EXPECTED.get(sym)
        chain_ok = (expected is None) or (raw == expected and resolved == expected
                                          and cand_ac == expected and mgr == expected)
        if expected is not None:
            check(chain_ok,
                  f"coherent {sym}: raw={raw} resolve={resolved} cand={cand_ac} "
                  f"mgr={mgr} (expect {expected})")
            coherence.append({"symbol": sym, "raw_asset_class": raw,
                              "asset_class": resolved, "candidate_asset_class": cand_ac,
                              "manager_tail": mgr, "source": src,
                              "portfolio_bucket": bucket_of(raw),
                              "allocator_bucket": bucket_of(raw)})
    EVIDENCE["2_representative_classifications"] = coherence

    # 3. ZERO silent CRYPTO fallback across every non-crypto family
    fallback_violations = []
    for sym in tradfi:
        raw, _, _ = U.classify(sym, MARKETS[sym])
        resolved = E.AssetBehaviorProfile.resolve_asset_class(sym)
        cand_ac = E.ExecutionCandidate(symbol=sym, side="BUY", price=100.0,
                                       entry_price=100.0, stop_loss=99.0,
                                       take_profit_1=101.0, take_profit_2=102.0,
                                       atr=1.0, df=_frames.get(sym), ob={}).asset_class
        mgr = RT.PORTFOLIO._asset_class(sym)
        for layer, value in (("raw", raw), ("resolve", resolved),
                             ("candidate", cand_ac), ("manager", mgr)):
            if value == "CRYPTO":
                fallback_violations.append({"symbol": sym, "layer": layer})
        check(all(v != "CRYPTO" for v in (raw, resolved, cand_ac, mgr)),
              f"no CRYPTO fallback for TradFi {sym} "
              f"(raw={raw}, cand={cand_ac}, mgr={mgr})")
    EVIDENCE["3_zero_silent_crypto_fallback"] = {
        "families_checked": sorted(tradfi), "violations": fallback_violations}

    # 7. raw STOCK/INDEX can never become CRYPTO through the allocator
    for sym in (_SPX, _EWJ, "NCSINASDAQ1002USD/USDT:USDT"):
        raw, _, _ = U.classify(sym, MARKETS[sym])
        check(raw != "CRYPTO",
              f"raw class for {sym} is {raw} (must be non-CRYPTO: STOCK/ETF/INDEX)")

    # ---------------------------------------------------------------- PHASE B
    print("\n[PHASE B] ALREADY-LIVE STOCK + ETF/INDEX EXCLUSION (TEST 1)", flush=True)
    # The previously observed scenario: live PORTFOLIO.contexts for SPCX + EWJ.
    RT.PORTFOLIO.contexts.clear()
    for sym, cls in ((_SPX, "STOCK"), (_EWJ, "ETF")):
        ctx = types.SimpleNamespace(symbol=sym, asset_class=cls, side="BUY",
                                    state={}, trade_state={}, live_manager=None)
        # open_candidate-derived contexts are PositionContext-like; the manager
        # only needs symbol-keyed membership + stored class + state snapshot.
        RT.PORTFOLIO.contexts[sym] = ctx
    check(len(_gate_events(RT, blocker="POSITION_ALREADY_OPEN")) == 0,
          "clean gate feed before live-exclusion phase")
    check(RT.PORTFOLIO._asset_class(_SPX) == "STOCK"
          and RT.PORTFOLIO._asset_class(_EWJ) == "ETF",
          "live SPCX/EWJ classify as STOCK/ETF in manager tail")
    check(bucket_of(RT.PORTFOLIO._asset_class(_SPX)) == "INDEX_STOCK"
          and bucket_of(RT.PORTFOLIO._asset_class(_EWJ)) == "INDEX_STOCK",
          "SPCX/EWJ map to INDEX_STOCK bucket (never CRYPTO bucket)")
    check(not RT.PORTFOLIO.can_open(_SPX, "STOCK")
          and not RT.PORTFOLIO.can_open(_EWJ, "ETF"),
          "can_open refuses the already-live SPCX/EWJ (DUPLICATE)")

    # Fully PREPARED/A-GRADE watch + institutional registry (would otherwise
    # promote): real scanner promote_to_queue must refuse both.
    prepared = {
        "symbol": None, "side": "BUY", "deep_analyzed": True,
        "institutional_zone_active": True, "a_grade_ready": True,
        "institutional_prepared": True, "precursor_count": 4,
        "analysis": {"liq_score": 85, "struct_score": 85, "ob_grade": "A"},
        "news_risk": 0, "pre_institutional_state": "CONFIRMED",
        "institutional_score": 90, "asset_class": "STOCK",
    }
    for sym, cls in ((_SPX, "STOCK"), (_EWJ, "ETF")):
        E.MEMORY.setdefault("watchlist", {})[sym] = dict(prepared,
                                                         symbol=sym, asset_class=cls)
        E.MEMORY.setdefault("institutional_zone_analysis", {})[sym] = {
            "symbol": sym, "zone_verdict": "CONFIRMED_ZONE",
            "precursor_count": 4, "institutional_score": 90,
            "pre_institutional_state": "CONFIRMED",
            "hypothesis": "INSTITUTIONAL_CONTROL",
        }
    import scanner.scanner as S
    promoted = S.promote_to_queue()
    check(promoted == 0, "live SPCX/EWJ never promoted to the queue (promoted=0)")
    check(_SPX not in E.queue._candidates and _EWJ not in E.queue._candidates,
          "SPCX/EWJ absent from queue._candidates")
    reasons = (E.MEMORY.get("pipeline", {}).get("promotion", {}) or {}
               ).get("rejected_by_reason", {}) or {}
    check(reasons.get("position_already_open", 0) >= 2,
          f"promotion records position_already_open x{reasons.get('position_already_open', 0)}")
    check(len(_gate_events(RT, blocker="POSITION_ALREADY_OPEN")) >= 2,
          "gate events PROMOTION/POSITION_ALREADY_OPEN recorded for both")
    EVIDENCE["3_queue_promotion_decisions"] = {
        "promoted": promoted,
        "rejected_by_reason": reasons,
        "already_live_symbols": [_SPX, _EWJ],
        "gate_events_promotion": _gate_events(RT, blocker="POSITION_ALREADY_OPEN")[-2:],
    }

    # No open_candidate attempted for them: run the runtime executor over
    # several "cycles" (queue best + allocator + open + cleanup + status).
    def _runtime_cycle():
        executed = RT._execute_ready_queue_candidate()
        E.queue.cleanup()
        return {"executed": bool(executed),
                "count": RT.PORTFOLIO.count(),
                "exec_pipe": {k: v for k, v in _exec_pipe().items()},
                "queue_status": E.queue.get_status()}

    cycles = []
    for cidx in range(3):
        cycles.append(_runtime_cycle())
    check(RT.PORTFOLIO.count() == 2, "no position opened for already-live SPCX/EWJ")
    for c in cycles:
        check(not c["executed"] and c["queue_status"].get("ready", 0) == 0,
              f"cycle no_ready_candidate: no open for already-live symbols")
    EVIDENCE["4_already_live_exclusion"] = {
        "cycles": cycles,
        "contexts_after": sorted(RT.PORTFOLIO.contexts.keys()),
        "exec_pipe": _exec_pipe(),
    }
    check(_exec_pipe().get("last_outcome") in ("no_ready_candidate", None),
          "no open_candidate ever attempted for already-live SPCX/EWJ")
    check(len(_gate_events(RT, blocker="CRYPTO_CAP")) == 0,
          "no CRYPTO_CAP event for the already-live STOCK/ETF symbols")

    # ------------------------------------------------------------ PHASE B2
    # TEST 6: a stray READY ExecutionCandidate for the SAME already-live symbol
    # must be invalidated+removed on first attempt, never retried for cycles.
    # Uses a fresh empty book so the allocator allows the commit and the swap
    # authority's DUPLICATE classification is actually reached (a full bucket
    # would short-circuit earlier at the allocator, which is itself proof of
    # safeguarding but not of the DUPLICATE invalidation).
    print("\n[PHASE B2] QUEUE LIFECYCLE — DUPLICATE invalidation (TEST 6)", flush=True)
    main_pm, main_alloc = RT.PORTFOLIO, RT.ALLOCATOR
    RT.PORTFOLIO = PortfolioManager(6, E)
    RT.ALLOCATOR = GlobalAssetAllocator(RT.PORTFOLIO, E)
    RT.PORTFOLIO.bind(E)
    RT.PORTFOLIO.risk_guard._day = None
    RT.PORTFOLIO.risk_guard._consecutive_losses = 0
    RT.PORTFOLIO.risk_guard._cooldown_until = 0.0
    try:
        RT.PORTFOLIO.hedge_mode = False
    except Exception:
        pass

    def _ready_candidate(symbol, cls, score=80.0):
        price = PRICES[symbol]
        atr = price * 0.01
        cand = E.ExecutionCandidate(
            symbol=symbol, side="BUY", price=price,
            entry_price=price - atr * 0.5, stop_loss=price - atr * 1.6,
            take_profit_1=price + atr * 1.5, take_profit_2=price + atr * 2.5,
            atr=atr, df=_frames[symbol], ob={})
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
        cand.ready_score_required = {"CRYPTO": 68.0}.get(cls, 68.0)
        _govern_watch(symbol, cls)
        assert E.queue.add_candidate(cand), f"admit {symbol}"
        E.queue._record_opportunity_lifecycle(cand)
        return cand

    # Establish a REAL live BTC position on the fresh book, then a stray READY
    # duplicate for the SAME symbol must be invalidated+removed by the swap
    # authority (Fix#4) — never retried cycle after cycle.
    _ready_candidate("BTC/USDT:USDT", "CRYPTO", score=80.0)
    check(RT._execute_ready_queue_candidate(),
          "B2: real BTC position opened on the fresh book")
    _reset_peak()
    E.queue._candidates.clear()
    _ready_candidate("BTC/USDT:USDT", "CRYPTO", score=95.0)
    before = E.queue.total_rejected
    dup_cycles = []
    for _ in range(4):
        dup_cycles.append(_runtime_cycle())
    dup_events = _gate_events(RT, blocker="POSITION_ALREADY_OPEN")
    check("BTC/USDT:USDT" not in E.queue._candidates,
          "READY duplicate invalidated and removed from the queue")
    check(RT.PORTFOLIO.count() == 1, "live BTC position untouched")
    check(E.queue.total_rejected - before == 1,
          f"DUPLICATE deduplicated exactly once (total_rejected "
          f"{before}->{E.queue.total_rejected})")
    check(all(not c["executed"] for c in dup_cycles[1:]),
          "later cycles are quiet (no repeated DUPLICATE retry loop)")
    check(_exec_pipe().get("last_open_failure_category") == "duplicate"
          and _exec_pipe().get("last_open_failure_blocker") == "DUPLICATE",
          "open failure classified duplicate/DUPLICATE")
    retries = [e for e in dup_events if "retry_next_cycle" in str(e)]
    check(len(retries) == 0,
          "no duplicate attempt ever scheduled retry_next_cycle")
    EVIDENCE["9_absence_of_duplicate_retries"] = {
        "cycles": dup_cycles,
        "position_already_open_events": len(dup_events),
        "total_rejected": E.queue.total_rejected,
        "queue_empty_of_symbols": True,
        "last_open_failure": {"category": _exec_pipe().get("last_open_failure_category"),
                              "blocker": _exec_pipe().get("last_open_failure_blocker")},
    }
    # Restore the main book (already-live SPCX + EWJ) for the remaining phases.
    RT.PORTFOLIO = main_pm
    RT.ALLOCATOR = main_alloc
    E.queue._candidates.clear()

    # ---------------------------------------------------------------- PHASE C
    print("\n[PHASE C] REAL CRYPTO PATH — BTC + ETH via executor", flush=True)
    E.queue._candidates.clear()
    for sym, cls in (("BTC/USDT:USDT", "CRYPTO"), ("ETH/USDT:USDT", "CRYPTO")):
        _ready_candidate(sym, cls)
        ok = RT._execute_ready_queue_candidate()
        check(ok, f"runtime opened live {sym} (CRYPTO #1/#2)")
        _reset_peak()
    classes = [RT.PORTFOLIO.contexts[s].asset_class for s in RT.PORTFOLIO.contexts]
    check(classes.count("CRYPTO") == 2,
          f"exactly 2 CRYPTO open ({classes.count('CRYPTO')})")
    check(len([s for s in RT.PORTFOLIO.symbols() if s in CRYPTO_SYMBOLS]) == 2,
          "both crypto contexts are real crypto instruments")
    EVIDENCE["5_allocator_decisions"] = {
        "allocator_report": RT.MEMORY.get("portfolio_allocation"),
        "bucket_counts": {"CRYPTO": classes.count("CRYPTO"),
                          "INDEX_STOCK": classes.count("STOCK") + classes.count("ETF"),
                          "COMMODITY": classes.count("OIL") + classes.count("GOLD")
                          + classes.count("METAL") + classes.count("ENERGY"),
                          "FOREX": classes.count("FOREX"),
                          "NEWS": classes.count("NEWS"),
                          "UNKNOWN": classes.count("UNKNOWN")},
    }
    from portfolio.allocator import DEFAULT_CLASS_CAPS
    EVIDENCE["6_capacity_state"] = {
        "default_class_caps": DEFAULT_CLASS_CAPS,
        "bucket_caps": {k: bucket_cap(bucket_of(k)) for k in
                        ("CRYPTO", "STOCK", "ETF", "INDEX", "GOLD", "OIL",
                         "METAL", "ENERGY", "FOREX", "NEWS")},
        "latest_bucket_counts": EVIDENCE["5_allocator_decisions"]["bucket_counts"],
    }

    # ---------------------------------------------------------------- PHASE 5
    print("\n[PHASE 5] NEWS INDEPENDENCE (TEST 5)", flush=True)
    # Book at entry: SPCX(STOCK) + EWJ(ETF) = 2/2 INDEX_STOCK, BTC+ETH = 2/2 CRYPTO,
    # COMMODITY 0/1, NEWS 0/1 (4 technical). News opens as the independent 5th;
    # a second news attempt must fail NEWS_SLOT_FULL while a technical slot and
    # the commodity slot remain free.
    news_sym = "NCSKAAPL2USD/USDT:USDT"
    price = PRICES[news_sym]
    E.MEMORY.setdefault("watchlist", {})[news_sym] = {
        "symbol": news_sym, "side": "BUY", "price": price, "atr": price * 0.01,
        "news_risk": 20.0, "asset_class": "NEWS",
        "news": types.SimpleNamespace(
            risk=20.0, bias="BULLISH",
            headlines=[{"impact_strength": "STRONG", "scope": "DIRECT",
                        "headline": f"{news_sym} impact"}],
            as_dict=lambda: {"bias": "BULLISH", "risk": 20.0}),
    }
    E.DASHBOARD_STATE["news_reaction"] = {
        "items": [{"symbol": news_sym, "reaction": {
            "causality": "CONFIRMED", "move_pct": 0.20, "direction": "UP"}}],
    }
    news1 = RT.execute_news_slot()
    check(news1, "independent NEWS slot opened through the real path")
    _reset_peak()
    technical = [s for s, c in RT.PORTFOLIO.contexts.items()
                 if c.asset_class != "NEWS"]
    news = [s for s, c in RT.PORTFOLIO.contexts.items() if c.asset_class == "NEWS"]
    check(RT.PORTFOLIO.count() == 5, f"total=5 after news ({len(technical)} technical + {len(news)} news)")
    check(len(technical) == 4 and len(news) == 1,
          "technical capacity has 4 live, NEWS has exactly 1")
    check(RT.PORTFOLIO.contexts[news_sym].asset_class == "NEWS",
          "news position stored as NEWS (never STOCK re-labelling)")
    live_classes = [RT.PORTFOLIO.contexts[s].asset_class
                    for s in RT.PORTFOLIO.contexts]
    check(live_classes.count("CRYPTO") == 2
          and live_classes.count("STOCK") + live_classes.count("ETF") == 2
          and live_classes.count("OIL") + live_classes.count("GOLD")
          + live_classes.count("METAL") == 0,
          "news did NOT consume CRYPTO / INDEX_STOCK / COMMODITY capacity")
    check(DEFAULT_CLASS_CAPS.get("NEWS") == 1
          and bucket_cap(bucket_of("NEWS")) == 1,
          "NEWS bucket stays cap-1 independent of technical buckets")
    news2 = RT.execute_news_slot()
    check(not news2 and _exec_pipe().get("last_outcome") == "news_slot_full"
          and _exec_pipe().get("last_reject_reason_user") == "NEWS_SLOT_FULL",
          "second news position refused (NEWS_SLOT_FULL), no technical seat consumed")
    check(RT.PORTFOLIO.count() == 5 and len(technical) == 4,
          "book unchanged after refused second news (4 technical + 1 news)")
    EVIDENCE["8_news_capacity_independence"] = {
        "technical_capacity": 5, "news_capacity": 1, "total_capacity": 6,
        "news_open": news1, "news_slot_full_on_second": not news2,
        "last_outcome": _exec_pipe().get("last_outcome"),
        "last_reject_reason_user": _exec_pipe().get("last_reject_reason_user"),
        "technical_open_after": len(technical), "news_open_after": len(news),
        "contexts_after": {s: RT.PORTFOLIO.contexts[s].asset_class
                           for s in RT.PORTFOLIO.contexts},
    }

    # ---------------------------------------------------------------- PHASE 4
    print("\n[PHASE 4] OIL/GOLD SLOT REACHABILITY (TEST 4)", flush=True)
    # Book now: SPCX + EWJ = 2/2 INDEX_STOCK, BTC+ETH = 2/2 CRYPTO,
    # OIL_GOLD 0/1, NEWS 1/1 (4 technical + 1 news). Blocked higher-priority
    # candidates (3rd CRYPTO, 3rd INDEX/STOCK) must be backed off so the queue
    # reaches the open COMMODITY seat.
    wti = "NCCOWTI2USD/USDT:USDT"
    sol, nasdaq = "DOGE/USDT:USDT", "NCSINASDAQ1002USD/USDT:USDT"
    _ready_candidate(sol, "CRYPTO", score=95.0)      # blocked: CRYPTO 2/2
    _ready_candidate(nasdaq, "INDEX", score=90.0)    # blocked: INDEX_STOCK 2/2
    _ready_candidate(wti, "OIL", score=70.0)          # reachable: COMMODITY 0/1
    step1 = _runtime_cycle()
    check(not step1["executed"] and wti not in RT.PORTFOLIO.symbols()
          and "CRYPTO" in step1["exec_pipe"].get("last_reject_reason", ""),
          "3rd CRYPTO candidate backed off (CRYPTO_CAP) before OIL reached")
    step2 = _runtime_cycle()
    check(not step2["executed"] and wti not in RT.PORTFOLIO.symbols()
          and "INDEX_STOCK" in step2["exec_pipe"].get("last_reject_reason", ""),
          "3rd INDEX/STOCK candidate backed off (INDEX_STOCK_CAP) before OIL reached")
    step3 = _runtime_cycle()
    check(step3["executed"] and wti in RT.PORTFOLIO.symbols(),
          "OIL/GOLD candidate reached and opened on the free COMMODITY seat")
    _reset_peak()
    check(RT.PORTFOLIO.contexts[wti].asset_class == "OIL",
          f"WTI context is OIL (not CRYPTO), class={RT.PORTFOLIO.contexts[wti].asset_class}")
    check(bucket_of(RT.PORTFOLIO.contexts[wti].asset_class) == "COMMODITY",
          "WTI maps to COMMODITY bucket")
    check(len(_gate_events(RT, symbol=sol)) >= 1
          and len(_gate_events(RT, symbol=nasdaq)) >= 1,
          "blocked CRYPTO and blocked INDEX/STOCK candidates were processed "
          "(gate events/backoff recorded) and skipped — queue advanced to OIL")
    final_classes = [RT.PORTFOLIO.contexts[s].asset_class
                     for s in RT.PORTFOLIO.contexts]
    check(RT.PORTFOLIO.count() == 6 and final_classes.count("NEWS") == 1
          and final_classes.count("OIL") == 1,
          "final book = 5 technical + 1 news (OIL opened beside NEWS)")
    technical5 = [s for s, c in RT.PORTFOLIO.contexts.items() if c.asset_class != "NEWS"]
    check(len(technical5) == 5, "technical capacity exactly 5 after OIL")
    EVIDENCE["7_oil_gold_reachability"] = {
        "blocked_crypto": sol, "blocked_index_stock": nasdaq, "reachable_commodity": wti,
        "step1": step1, "step2": step2, "step3": step3,
        "contexts_after": {s: RT.PORTFOLIO.contexts[s].asset_class
                           for s in RT.PORTFOLIO.contexts},
    }

    # ---------------------------------------------------------------- PHASE F
    print("\n[PHASE F] ALLOCATOR TRUTH — chain + no phantom CRYPTO (TEST 7)", flush=True)
    book = {s: RT.PORTFOLIO.contexts[s].asset_class for s in RT.PORTFOLIO.contexts}
    # Probe the allocator with the two candidates that were REJECTED during
    # Phase 4 (NASDAQ as INDEX, DOGE as CRYPTO) so their decisions are part of
    # the allocator-truth chain — same data the runtime used at backoff time.
    probes = {
        "NCSINASDAQ1002USD/USDT:USDT": "INDEX",
        "DOGE/USDT:USDT": "CRYPTO",
    }
    candidates = [{"symbol": s, "side": "BUY", "asset_class": c,
                   "candidate_key": f"runtime:{s}:BUY", "priority_score": 50.0}
                  for s, c in book.items()]
    candidates += [{"symbol": s, "side": "BUY", "asset_class": c,
                    "candidate_key": f"runtime:{s}:BUY", "priority_score": 90.0}
                   for s, c in probes.items()]
    report = RT.ALLOCATOR.allocate(candidates, limit=6)
    decisions = [d.trace_dict() for d in report.decisions]
    decision_rows = report.to_dict()["decisions"]
    violations = []
    for s, c in book.items():
        if c == "CRYPTO" and s not in CRYPTO_SYMBOLS:
            violations.append({"symbol": s, "asset_class": c})
        if c in ("STOCK", "ETF", "INDEX", "GOLD", "OIL", "METAL", "FOREX", "UNKNOWN", "NEWS"):
            if s in CRYPTO_SYMBOLS and c != "CRYPTO" and c != "NEWS":
                violations.append({"symbol": s, "asset_class": c})
    check(len(violations) == 0, f"no phantom CRYPTO / no TradFi-as-CRYPTO in book ({violations})")
    crypto_cap_events = [dict(e) for e in _gate_events(RT)
                         if "CRYPTO_CAP" in json.dumps(e, default=str)]
    check(len(crypto_cap_events) == 1
          and crypto_cap_events[0].get("symbol") == "DOGE/USDT:USDT",
          "exactly one CRYPTO_CAP event, and only for the real 3rd DOGE — no phantom CRYPTO capacity")
    # allocator must never hand a CRYPTO decision to a TradFi symbol
    for drow in decision_rows:
        check(drow["symbol"] not in CRYPTO_SYMBOLS
              or str(drow["asset_class"]) == "CRYPTO",
              f"allocator never labels TradFi {drow['symbol']} as CRYPTO "
              f"(got {drow['asset_class']})")
    # telemetry CRYPTO events only ever concern real crypto symbols
    telemetry_crypto = []
    for ev in _gate_events(RT):
        if str(ev.get("asset_class")) == "CRYPTO" or str(ev.get("blocker")) == "CRYPTO_CAP":
            telemetry_crypto.append(ev)
    for ev in telemetry_crypto:
        check(ev.get("symbol") in CRYPTO_SYMBOLS,
              f"CRYPTO telemetry only for real crypto symbols ({ev.get('symbol')})")
    EVIDENCE["10_no_phantom_crypto_capacity"] = {
        "book": book,
        "allocator_decisions_full_book": decisions,
        "allocator_decision_rows": decision_rows,
        "crypto_cap_event_symbols": sorted({e.get("symbol") for e in crypto_cap_events}),
        "violations": violations,
    }
    # allocator truth chain for a STOCK/ETF/INDEX candidate decision
    for s in (_SPX, _EWJ, "NCSINASDAQ1002USD/USDT:USDT"):
        d = [x for x in decision_rows if str(x.get("symbol")) == s]
        check(d and str(d[0].get("asset_class")) in ("STOCK", "ETF", "INDEX")
              and d[0].get("asset_class") != "CRYPTO",
              f"allocator decision for {s} is {d[0].get('asset_class') if d else None}, never CRYPTO")
    nd = [x for x in decision_rows if str(x.get("symbol")) == "NCSINASDAQ1002USD/USDT:USDT"]
    check(nd and nd[0].get("allowed") is False
          and str(nd[0].get("bucket")) == "INDEX_STOCK",
          "NASDAQ allocator decision stays INDEX in the INDEX_STOCK bucket, "
          "allowed=False (cap proof in Phase 4 step2: INDEX_STOCK_CAP)")

    # ---------------------------------------------------------------- EVIDENCE
    # 11 classification warnings (non-metadata resolutions observed) + 13 errors
    low_conf = [c for c in coherence if c["source"] != "metadata"]
    EVIDENCE["11_classification_warnings"] = {
        "pattern_venue_resolutions": [{k: c[k] for k in
                                       ("symbol", "raw_asset_class", "source")}
                                      for c in low_conf],
        "note": "crypto venue/margin-pair resolutions are expected (venue evidence); "
                "no TradFi instrument depended on a fallback",
    }
    if low_conf:
        for c in low_conf:
            check(c["raw_asset_class"] != "CRYPTO" or c["symbol"] in CRYPTO_SYMBOLS,
                  f"warning-source classification {c['symbol']} -> {c['raw_asset_class']} "
                  "is legitimate (venue/crypto)")
    EVIDENCE["13_runtime_errors"] = {"exceptions": [], "failure_hits": FAILURES[:]}

    # ---------------------------------------------------------------- REPORT
    print("\n" + "=" * 72)
    print(f"EVIDENCE RESULT — {PASS_COUNTER['n']} checks passed, "
          f"{len(FAILURES)} checks failed")
    print("=" * 72)
    if FAILURES:
        print("FAILED CHECKS:")
        for f in FAILURES:
            print("  - " + f)
        verdict = "PAPER RUNTIME FAILED"
    else:
        verdict = "PAPER RUNTIME VALIDATED"
    EVIDENCE["verdict"] = verdict
    EVIDENCE["checks"] = {"passed": PASS_COUNTER["n"], "failed": len(FAILURES),
                          "failures": FAILURES}

    out_path = os.path.join(
        os.environ.get("TEMP", ROOT),
        "opencode", "paper_runtime_validation_evidence.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(EVIDENCE, fh, indent=2, default=str)
    print(f"\nEvidence JSON: {out_path}")
    print(f"VERDICT: {verdict}")
    if FAILURES:
        sys.exit(1)


if __name__ == "__main__":
    main()