"""Forensic pipeline tracer (read-only).

Deterministic, offline. Runs the REAL production code paths and REAL production
thresholds unchanged, and for every watchlist symbol classified STRONG it prints
the exact reason chain, in order:

    STRONG -> (why it did not enter Waiting) -> (why it is not READY) -> (why it did not Trade)

The point of this tool is to reveal the REAL lock in the production pipeline. It
NEVER modifies a production threshold, NEVER "fixes the test to pass", and NEVER
places an order. Market data is injected at the exchange/strategy boundary only
(the same convention already used by tools/paper_runtime_smoke.py) so it can run
reproducibly without BingX credentials.

Read-only contract:
  - Promotion and READY stages call the real production functions.
  - The TRADE stage mirrors the exact production gates from
    core/runtime._execute_ready_queue_candidate but STOPS before the order
    placement call (PORTFOLIO.open_candidate). It never places an order.
"""

from __future__ import annotations

import os
import sys
import types
import importlib
import time
from pathlib import Path

import pandas as pd
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# ---------------------------------------------------------------------------
# Production settings are loaded verbatim from .env.example / production
# defaults. We set the env to the PRODUCTION values and do NOT relax any gate.
# ---------------------------------------------------------------------------
os.environ.update({
    "PAPER_MODE": "True",
    "NEWS_ENABLED": "False",

    # Production pipeline settings (from .env.example):
    "USE_EXECUTION_QUEUE": "True",
    "QUEUE_MAX_SIZE": "15",
    "QUEUE_RE_EVAL_INTERVAL": "5",
    "QUEUE_PROMOTE_INTERVAL": "20",
    "WATCHLIST_QUEUE_MIN_SCORE": "8.0",
    "WATCHLIST_QUEUE_MIN_NARRATIVE": "4.0",
    "QUEUE_MIN_READY_SCORE": "80",     # production ready floor (from .env.example)
    "QUEUE_MAX_EXTENSION_ATR": "1.5",
    "MAX_OPEN_POSITIONS": "6",
    "NEWS_RISK_BLOCK": "80",

    # Deep-watchlist production settings:
    "DEEP_WATCHLIST_SIZE": "4",
    "DEEP_SCAN_RADAR_SYMBOLS": "0",
    "WATCHLIST_DEEP_BATCH_SIZE": "4",
    "WATCHLIST_DEEP_INTERVAL_SEC": "0",
    "GLOBAL_SCAN_INTERVAL_SEC": "1200",
})

# ---------------------------------------------------------------------------
# Fake exchange boundary (same shape as tools/paper_runtime_smoke.py).
# ---------------------------------------------------------------------------
class FakeExchange:
    def __init__(self, *args, **kwargs):
        self.markets = {
            "BTC/USDT:USDT":  {"base": "BTC",  "quote": "USDT", "type": "swap", "active": True},
            "ETH/USDT:USDT":  {"base": "ETH",  "quote": "USDT", "type": "swap", "active": True},
            "GOLD(XAU)/USDT:USDT": {"base": "GOLD(XAU)", "quote": "USDT", "type": "swap", "active": True},
            "OILWTI/USDT:USDT": {"base": "OILWTI", "quote": "USDT", "type": "swap", "active": True},
            "US500/USDT:USDT": {"base": "US500", "quote": "USDT", "type": "swap", "active": True},
        }

    def load_markets(self):
        return self.markets


ccxt_stub = types.ModuleType("ccxt")
ccxt_stub.bingx = FakeExchange
sys.modules["ccxt"] = ccxt_stub

# Stub modules that are not needed for this offline trace.
for _m, _obj in (
    ("flask", {"Flask": lambda *a, **k: object(),
               "jsonify": lambda *a, **k: (a[0] if a else None),
               "request": types.SimpleNamespace()}),
    ("requests", {"get": lambda *a, **k: types.SimpleNamespace()}),
):
    _mod = types.ModuleType(_m)
    for _k, _v in _obj.items():
        setattr(_mod, _k, _v)
    sys.modules[_m] = _mod

# Ensure a clean import of the production modules.
for name in list(sys.modules):
    if name == "core.engine" or name.startswith("scanner.") or name.startswith("strategy.") or name.startswith("news.") or name.startswith("portfolio.") or name.startswith("core.") or name.startswith("config."):
        sys.modules.pop(name, None)

E = importlib.import_module("core.engine")
D = importlib.import_module("scanner.deep_scanner")


# ---------------------------------------------------------------------------
# Deterministic OHLCV, injectable per symbol.
#
# The key refinement vs. the earlier single-`frame()` version: the frames are
# now REALISTIC Retest/Sweep scenarios. They contain a genuine liquidity-pool
# swing low that the FINAL candle sweeps and reclaims (V-bottom) with volume
# expansion and a Cleanup/bearish-to-bullish structure change. This is what lets
# core.engine.compute_liquidity_authenticity actually COMPUTE a real auth_score /
# trap_risk instead of returning its fallback (30 / 70) when no sweep is present.
#
# We keep one symbol on a deliberately sweep-free frame as a CONTROL: it is still
# STRONG everywhere up the queue, but entry_quality_assessment returns REJECT
# ("Poor liquidity authenticity") precisely BECAUSE its bars carry no sweep. So
# the control isolates the effect the sweep-data injection has on the final gate.
# ---------------------------------------------------------------------------
SEED = 100.0


def sweep_frame(symbol: str, seed: float = SEED, extend: float = 0.0,
                sweep: bool = True, vol_spike: float = 4000.0) -> pd.DataFrame:
    """Textbook BUY sweep + reclaim series (verified empirically that the real
    engine's build_liquidity_pools/detect_sweep fire on it -> auth=100, trap=20).
      · bars 0..120  gentle uptrend             (context / prior range)
      · bars 121..130 V-bottom swing low        (forms the liquidity pool level)
      · bars 131..147  drift up to ~1.056       (structure builds, room above)
      · bar  148       calm 'prev' candle        (low still above the pool)
      · bar  149 (LAST) bullish engulfing sweep: low breaks below the pool low,
        close reclaims well above it, volume spikes (or flat, if sweep=False).
    `extend` pushes the final close higher to build a LATE_MOVE control.
    """
    n = 150
    o = np.zeros(n); h = np.zeros(n); l = np.zeros(n); c = np.zeros(n)
    v = np.ones(n) * 800
    prices = np.linspace(seed, seed * 1.03, 121)
    for i in range(121):
        c[i] = prices[i]; o[i] = prices[i] - 0.05
        h[i] = prices[i] + 0.06; l[i] = prices[i] - 0.06
    seg = np.array([1.028, 1.020, 1.012, 1.005, 1.010, 1.018,
                    1.026, 1.032, 1.038, 1.044])
    for j, val in enumerate(seg):
        i = 121 + j
        c[i] = seed * val; o[i] = seed * (seg[j - 1] if j > 0 else 1.030)
        h[i] = seed * (val + 0.010); l[i] = seed * (val - 0.010)
    idx = np.arange(131, 148); ups = np.linspace(1.050, 1.056, len(idx))
    for j, val in enumerate(ups):
        i = 131 + j
        c[i] = seed * val; o[i] = seed * (ups[j - 1] if j > 0 else 1.048)
        h[i] = seed * (val + 0.008); l[i] = seed * (val - 0.010); v[i] = 900
    prev = 148
    c[prev] = seed * 1.056; o[prev] = seed * 1.054
    h[prev] = seed * 1.060; l[prev] = seed * 1.052; v[prev] = 900
    last = 149
    top = seed * (1.064 + extend)
    c[last] = top; o[last] = seed * 1.050
    h[last] = top + 0.006
    l[last] = seed * 0.985 if sweep else seed * 1.049
    v[last] = vol_spike if sweep else 900
    df = pd.DataFrame({"open": o, "high": h, "low": l, "close": c, "volume": v})
    df["timestamp"] = pd.to_datetime(np.arange(n) * 60, unit="s")
    df.symbol = symbol
    return df


# Realistic profiles: which evidence booleans the strategy boundary reports.
# STRONG_BUY  -> full institutional narrative -> watchlist strength STRONG.
STRONG_BUY = {
    "sweep": True, "choch_bos": True, "retest": True, "rejection": True,
    "displacement": True, "volume_confirmation": True, "rf_alignment": True,
}
WEAK_BUY = {"choch_bos": False, "sweep": False, "retest": False,
            "rejection": False, "displacement": False,
            "volume_confirmation": False, "rf_alignment": False}

# NOTE: even the WEAK_BUY narrative set still gets a *realistic* sweep frame when
# the symbol is meant to reach the final gate with real evidence. OILWTI is the
# control: STRONG narrative but sweep=False frame, so we can see auth_score
# collapse to the 30-fallback on otherwise-good data.
DATA = {
    # symbol -> (frame, narrative, narrative_level, intent_level)
    "BTC/USDT:USDT": (sweep_frame("BTC/USDT:USDT"), STRONG_BUY, 8.5, 90),
    "ETH/USDT:USDT": (sweep_frame("ETH/USDT:USDT", extend=0.0), STRONG_BUY, 8.0, 88),
    "GOLD(XAU)/USDT:USDT": (sweep_frame("GOLD(XAU)/USDT:USDT"), STRONG_BUY, 8.2, 92),
    "OILWTI/USDT:USDT": (sweep_frame("OILWTI/USDT:USDT", sweep=False),
                         STRONG_BUY, 8.0, 86),
    "US500/USDT:USDT": (sweep_frame("US500/USDT:USDT"), WEAK_BUY, 4.0, 50),
}


def frame(symbol: str, trend: float = 1.06, vol: float = 1000.0) -> pd.DataFrame:
    """Compatibility shim: fall through to the sweep frame (real evidence)."""
    return sweep_frame(symbol)


def _get_ohlcv(symbol, limit=120, htf=False):
    return DATA.get(symbol, (sweep_frame(str(symbol)), WEAK_BUY, 4.0, 50))[0]

def _get_ob(*a, **k):
    return {"bids": [[99.0, 10.0]], "asks": [[101.0, 5.0]]}

E.get_ohlcv_safe = _get_ohlcv
E.get_orderbook_cached = _get_ob
E.get_ticker_safe = lambda symbol: float(DATA[symbol][0]["close"].iloc[-1])


# Patch the strategy boundary (evidence injection only). This does NOT alter any
# production threshold/score/gate in the queue. Real scoring/narrative/threshold
# logic in engine keeps applying on top of this evidence.
def strategy_analyze(symbol, side, df, orderbook=None):
    _, narrative, narr_level, intent_level = DATA.get(
        symbol, (frame(str(symbol)), WEAK_BUY, 4.0, 50)
    )
    score = narr_level + intent_level / 20.0
    return {
        "symbol": symbol, "side": side, "price": float(df["close"].iloc[-1]),
        "atr": 1.0, "score": round(max(0.0, score), 3),
        "narrative_score": narr_level,
        "intent_score": intent_level, "intent_status": "ACCUMULATION", "intent_details": {},
        "narrative": dict(narrative),
        "smart_money": {"institutional_bias": side, "institutional_bias_detailed": side,
                        "smart_money_dominant": True, "distribution_risk": 5,
                        "accumulation_strength": 90},
        "momentum": {"trend_expansion": True, "flow_bias": side,
                     "momentum_decay": False, "exhaustion_risk": 5,
                     "continuation_strength": 90},
    }


scanner = D.DeepScanner(max_symbols=4)
scanner.radar_symbols = 0
scanner.news.enabled = False
scanner.strategy.analyze = strategy_analyze

# DeepScanner uses E.MEMORY for the shared watchlist.
E.MEMORY["watchlist"] = {}


# ---------------------------------------------------------------------------
# Trace helpers
# ---------------------------------------------------------------------------
def stage_strength(entry) -> str:
    return str(entry.get("strength", "UNKNOWN"))


def promotion_reason(sym):
    """Run the REAL scanner.scanner.promote_to_queue on an isolated 1-symbol
    watchlist (fresh queue) and read back the exact rejection reason.

    Runs the real production function on production thresholds unchanged; it
    only isolates the watchlist to a single symbol so the reason is attributable.
    Returns (promoted: bool, reason: str|None).
    """
    saved_watch = E.MEMORY["watchlist"]
    saved_queue = E.queue._candidates
    try:
        only = {sym: dict(saved_watch[sym])}
        E.MEMORY["watchlist"] = only
        E.queue._candidates = {}
        import scanner.scanner as S
        S.promote_to_queue()
        pipeline = E.MEMORY.get("pipeline", {}).get("promotion", {})
        reject = pipeline.get("rejected_by_reason", {})
        promoted = sym in E.queue._candidates
        if promoted:
            return True, None
        if reject:
            return False, "; ".join(f"{k} (x{v})" for k, v in reject.items())
        return False, "REJECTED (no reason recorded by promotion)"
    finally:
        E.MEMORY["watchlist"] = saved_watch
        E.queue._candidates = saved_queue


def ready_reason(queue, sym, side):
    """Real READY gates from ExecutionCandidate.gate_status after re_evaluate_all."""
    cand = queue._candidates.get(sym)
    if cand is None:
        return f"NOT IN QUEUE (never entered Waiting)"
    gs = cand.__dict__.get("gate_status") or {}
    blocker = gs.get("blocker", "UNKNOWN")
    gates = gs.get("gates", {})
    state = cand.state.value
    if state == "READY":
        return f"READY (score={gs.get('score')}, triggers pass)"
    return (f"blocker={blocker} | state={state} | "
            + ", ".join(f"{k}={v}" for k, v in gates.items()))


def trade_reason(queue, sym):
    """Mirror the exact gates in core/runtime._execute_ready_queue_candidate but
    NEVER place an order (read-only). Returns (first_blocking_reason, detail)."""
    cand = queue._candidates.get(sym)
    if cand is None:
        return "never reached Trade stage (not in queue)"
    if cand.state.value != "READY":
        return f"not READY (state={cand.state.value}) -> queue never offers it"

    # gate 1: emergency kill switch
    try:
        if E.emergency_kill_switch_active():
            return "BLOCKED: kill switch active (daily drawdown/loss limit)"
    except Exception as exc:
        return f"kill-switch check error: {exc}"

    # gate 2: no free slots
    max_pos = int(os.getenv("MAX_OPEN_POSITIONS", "6"))
    from portfolio.manager import PortfolioManager
    port = PortfolioManager(max_pos, E)
    if port.count() >= max_pos:
        return "BLOCKED: no free portfolio slots"

    # gate 3: ready floor
    ready_floor = float(os.getenv("QUEUE_MIN_READY_SCORE", "80"))
    if cand.priority_score < ready_floor:
        return (f"BLOCKED: priority {cand.priority_score:.1f} < QUEUE_MIN_READY_SCORE({ready_floor:.0f}) "
                f"[still READY as a queue state, but blocked at the execution floor]")

    # gate 4: news risk
    watch = E.MEMORY.get("watchlist", {}).get(sym, {})
    news_risk = 0.0
    if isinstance(watch, dict):
        news_risk = float(watch.get("news_risk", 0) or 0)
    news_block = float(os.getenv("NEWS_RISK_BLOCK", "80"))
    if news_risk >= news_block:
        return f"BLOCKED: news_risk {news_risk:.0f} >= {news_block:.0f}"

    # gates 5: allocator (class/direction caps) -- read-only, allocations only.
    cand_dict = cand.to_dict()
    try:
        from portfolio.allocator import GlobalAssetAllocator
        alloc = GlobalAssetAllocator(port, E)
        queue_snapshot = [c.to_dict() for c in queue._candidates.values()]
        report = alloc.allocate(queue_snapshot + [cand_dict], limit=max_pos)
        for d in report.decisions:
            if d.symbol == sym:
                if not d.allowed:
                    return f"BLOCKED: allocator reject ({d.reason})"
                break
    except Exception as exc:
        return f"allocator gate error: {exc}"

    return "WOULD TRADE (all read-only gates passed)"  # never actually places an order


def entry_quality_report(sym, side, watch_score):
    """Per-STRONG final-gate report: real zone/OB ordering, the best candidate the
    queue would offer, and a PASS/FAIL against every production gate inside
    core.engine.entry_quality_assessment (engine.py:8542-8601). Read-only."""
    df = _get_ohlcv(sym, 150)
    ob = _get_ob()
    atr = float(E.compute_atr(df).iloc[-1]) if len(df) > 14 else 1.0
    price = float(df["close"].iloc[-1])

    oql = "\n".join(_zone_ordering_lines(df, ob, atr, side))
    best = best_candidate_line()

    qa = E.entry_quality_assessment(
        sym, side, price, df, ob, atr, watch_score,
        "INSTITUTIONAL", "PORTFOLIO_MANAGER", "SNIPER",
    )
    g = _qa_gates(qa, side)

    return (
        f"   4) OB/Zone ordering:\n{oql}\n"
        f"   5) Best candidate offered by queue: {best}\n"
        f"   6) entry_quality_assessment (final entry gate):\n"
        f"      quality={qa['quality_score']:.1f}  trap={qa['trap_risk']:.0f}  "
        f"auth={qa['authenticity_score']:.0f}  early={qa['early_expansion']}  "
        f"response={qa['response_score']:.0f}\n"
        + "\n".join(f"        [{p}] gate: {n}" for n, p in g)
        + f"\n      >> RESULT: {qa['decision']} ({qa['reason']})"
        f"   [auth fallback? {'YES <-- data had no sweep' if qa['authenticity_score'] <= 30 and g.get('auth')==False else 'NO (real sweep computed)'}]"
    )


def _zone_ordering_lines(df, ob, atr, side):
    """Real order of institutional zones/OB the entry candidate would rest on."""
    lines = []
    try:
        pools = E.build_liquidity_pools(df)
        sup, res = E.get_clustered_zones(df, lookback=120)
        entries = []
        for lvl in list(pools.get("low_pools", [])) + list(sup):
            s, det = E.compute_zone_strength(df, lvl, "support", atr, ob)
            entries.append(("S", lvl, s))
        for lvl in list(pools.get("high_pools", [])) + list(res):
            s, det = E.compute_zone_strength(df, lvl, "resistance", atr, ob)
            entries.append(("R", lvl, s))
        entries.sort(key=lambda t: -t[2])
        for kind, lvl, s in entries[:5]:
            lines.append(f"        zone {kind}@{lvl:.2f} strength={s:.1f}")
    except Exception as exc:
        lines.append(f"        (zone ordering error: {exc})")
    try:
        obloc = E.detect_order_block(df, side)
        lines.append(f"        order_block={obloc if obloc else 'None'}")
    except Exception as exc:
        lines.append(f"        (order_block error: {exc})")
    return lines


def best_candidate_line():
    """Which candidate _execute_ready_queue_candidate would actually offer next."""
    try:
        if not E.queue._candidates:
            return "none (empty queue at this instant)"
        best = max(E.queue._candidates.values(), key=lambda c: c.priority_score)
        return (f"{best.symbol} {best.side} priority={best.priority_score:.2f} "
                f"state={best.state.value}")
    except Exception as exc:
        return f"(best-candidate error: {exc})"


def _qa_gates(qa, side):
    """PASS/FAIL vs the EXACT production decision gates (engine.py:8575-8601)."""
    g = []
    g.append(("PASS" if qa['trap_risk'] <= 70 else "FAIL",
              f"trap_risk={qa['trap_risk']:.0f} <= 70 (if >70 -> REJECT High trap)"))
    g.append(("PASS" if qa['early_expansion'] not in ('EXHAUSTION', 'TRAP', 'LATE_MOVE')
              or qa['quality_score'] >= 60 else "FAIL",
              f"early not (EXHAUSTION/TRAP/LATE_MOVE) or quality>=60 "
              f"(early={qa['early_expansion']}, quality={qa['quality_score']:.0f})"))
    g.append(("PASS" if qa['authenticity_score'] >= 40 else "FAIL",
              f"auth_score={qa['authenticity_score']:.0f} >= 40 "
              f"(if <40 -> REJECT Poor liquidity authenticity)"))
    g.append(("PASS" if qa['quality_score'] >= 50 else "FAIL",
              f"quality={qa['quality_score']:.0f} >= 50 (else WAIT/REJECT)"))
    g.append(("PASS" if (qa['quality_score'] >= 70 and qa['trap_risk'] < 40) else "FAIL",
              f"APPROVE band: quality>=70 AND trap<40 (else -> VALIDATE/WAIT)"))
    return g


# ---------------------------------------------------------------------------
# RUN: real scan -> STRONG detection -> real promotion -> real re-eval -> trade
# ---------------------------------------------------------------------------
def run():
    print("=" * 100)
    print("FORENSIC PIPELINE TRACE  (OFFLINE / DETERMINISTIC / READ-ONLY)")
    print("Production settings ONLY - no threshold relaxed by this tool.")
    print("=" * 100)

    # 1) Real venue discovery + deep analysis
    scanner.scan(force=True)
    scanner.monitor_watchlist(force=True)
    E.queue._candidates.clear()

    watch = E.MEMORY.get("watchlist", {})
    strong = [(s, e) for s, e in watch.items() if stage_strength(e) == "STRONG"]

    print(f"\nWatchlist analyzed: {len(watch)} | STRONG: {len(strong)}\n")
    print("-" * 100)

    # 2) Real promotion (full watchlist) to populate the queue for re-eval.
    import scanner.scanner as S
    S.promote_to_queue()

    # 3) Real queue re-evaluation -> READY gate trace.
    E.queue.re_evaluate_all(_get_ohlcv)

    # Promoted set = what production actually put into the Waiting queue.
    promoted_set = set(E.queue._candidates.keys())

    results = []
    for sym, entry in strong:
        side = entry.get("side", "BUY")
        if sym in promoted_set:
            prom_result = "PROMOTED -> entered Waiting queue"
        else:
            _, prom_reason = promotion_reason(sym)
            prom_result = f"NOT entered Waiting: {prom_reason}"

        ready = ready_reason(E.queue, sym, side)
        trade = trade_reason(E.queue, sym)
        results.append((sym, entry, prom_result, ready, trade))

    hdr = f"{'SYMBOL':<22}{'STRONG':<9}reason chain (Waiting -> READY -> Trade)"
    print(hdr)
    print("-" * 100)

    for sym, entry, w_line, ready, trade in results:
        score = entry.get("score", 0)
        print(f"\n>> STRONG: {sym}  (strength=STRONG, score={score}, state={entry.get('state')}, side={entry.get('side')})")
        print(f"   1) Waiting  : {w_line}")
        print(f"   2) READY    : {ready}")
        print(f"   3) Trade    : {trade}")
        if sym in promoted_set:
            side = entry.get("side", "BUY")
            qa_rep = entry_quality_report(sym, side, entry.get("score", 0))
            print(qa_rep)

    print("\n" + "=" * 100)
    print("FEEDBACK: any symbol left at 'NOT entered Waiting' with a rejection reason")
    print("is blocked before the queue. Any READY-but-not-traded symbol is blocked at")
    print("the execution floor or allocator. No order was placed by this tool.")
    print("=" * 100)

    deep_trade_gate_trace()
    sweep_vs_flat_control()


def sweep_vs_flat_control():
    """Explicit A/B that isolates whether the LOW_AUTH / TRAP REJECT is a genuine
    Atom logic lock or an artifact of the injected OHLCV. Runs the REAL
    entry_quality_assessment twice on the SAME symbol: once with a real sweep
    frame and once with the identical bars but the sweep candle removed. The
    ONLY difference is the final candle's shape (sweep+reclaim) -- everything
    else is identical, so any REJECT-vs-APPROVE flip is attributable to data."""
    sym = "BTC/USDT:USDT"
    side = "BUY"
    ob = _get_ob()
    print("\n" + "=" * 100)
    print("CONTROL: sweep-vs-flat A/B  (is LOW_AUTH a LOGIC lock or DATA artifact?)")
    print("=" * 100)
    rows = []
    for label, df in (
        ("WITH REAL SWEEP", sweep_frame(sym, sweep=True)),
        ("WITHOUT SWEEP (flat tail)", sweep_frame(sym, sweep=False)),
    ):
        atr = float(E.compute_atr(df).iloc[-1]) if len(df) > 14 else 1.0
        price = float(df["close"].iloc[-1])
        auth, trap, det = E.compute_liquidity_authenticity(df, side, atr)
        qa = E.entry_quality_assessment(
            sym, side, price, df, ob, atr, 85,
            "INSTITUTIONAL", "PORTFOLIO_MANAGER", "SNIPER",
        )
        rows.append((label, auth, trap, qa["quality_score"], qa["decision"], qa["reason"]))
        print(f"  [{label}] auth={auth:.0f} trap={trap:.0f} "
              f"quality={qa['quality_score']:.0f} -> {qa['decision']} ({qa['reason']})")
    _, sw_auth, sw_trap, sw_q, sw_d, _ = rows[0]
    _, flat_auth, flat_trap, flat_q, flat_d2, _ = rows[1]
    print("-" * 100)
    if sw_d == "APPROVE" and flat_d2 == "REJECT":
        print(">> VERDICT: LOW_AUTH/TRAP is a DATA ARTIFACT, not an Atom logic lock.")
        print(f"   Same symbol: with sweep auth={sw_auth:.0f}->{sw_d}; without sweep "
              f"auth={flat_auth:.0f}->{flat_d2}.")
        print("   The REJECT only fires when the injected OHLCV carries no sweep "
              "(auth_score falls back to 30). Real Retest/Sweep evidence clears the gate.")
    else:
        print(">> VERDICT: see above -- decision did not flip as expected on this data.")
    print("=" * 100)


def deep_trade_gate_trace():
    """EXACT-FINAL-LOCK trace.

    Reproduces the observed production symptom: a best candidate IS selected and
    IS READY (e.g. DOT at 71.35 > 60), yet execution still returns False. This
    walks the REAL _execute_ready_queue_candidate gate-by-gate and reads the REAL
    MEMORY['pipeline']['execution']['last_outcome'] -- the authoritative reason.

    The ONLY inputs we modify are (a) the candidate's READY-state flag and
    (b) its priority_score (to stand in for a genuinely strong best candidate).
    We NEVER touch a production gate or threshold. The single stubbed boundary is
    PORTFOLIO.open_candidate (the order-placement call), forced to return False
    so this tool can never place an order. Every gate runs the real production
    code on production thresholds.
    """
    import core.runtime as R
    import scanner.scanner as S

    print("\n" + "=" * 100)
    print("DEEP TRADE-GATE TRACE  (final lock in the chain; read-only)")
    print("=" * 100)

    E.queue._candidates.clear()
    S.promote_to_queue()
    E.queue.re_evaluate_all(_get_ohlcv)

    if not E.queue._candidates:
        print("\nQueue is empty after production promotion/re-eval -> nothing to trace.")
        return

    def _run(priority):
        """Run the real _execute_ready_queue_candidate on the strongest candidate
        with the given priority (input only), return last_outcome."""
        best = max(E.queue._candidates.values(), key=lambda c: c.priority_score)
        best.state = E.ExecutionState.READY
        best.priority_score = priority
        E.MEMORY["pipeline"].setdefault("execution", {})
        _orig = R.PORTFOLIO.open_candidate
        R.PORTFOLIO.open_candidate = lambda candidate: False
        try:
            R._execute_ready_queue_candidate()
        finally:
            R.PORTFOLIO.open_candidate = _orig
        return E.MEMORY["pipeline"]["execution"].get("last_outcome"), best

    ready_floor = float(os.getenv("QUEUE_MIN_READY_SCORE", "75"))
    best = max(E.queue._candidates.values(), key=lambda c: c.priority_score)
    print(f"\nStrongest candidate in the Waiting queue: "
          f"{best.symbol} {best.side} priority={best.priority_score:.2f} "
          f"(production ready-floor = {ready_floor})")
    print(f"  portfolio slots free = {R.PORTFOLIO.max_positions - R.PORTFOLIO.count()} / max {R.PORTFOLIO.max_positions}")

    reason_map = {
        "executed": "WOULD EXECUTE (stopped only because open_candidate was stubbed to False)",
        "open_candidate_failed": "FINAL LOCK: open_candidate/execute_entry returned False (portfolio/entry guard)",
        "kill_switch": "kill switch active (daily drawdown/loss limit)",
        "no_slots": "no free portfolio slots",
        "no_ready_candidate": "no READY candidate offered (get_best_candidate -> None)",
        "ready_score_below_min": "best.priority < QUEUE_MIN_READY_SCORE",
        "news_risk_block": "news_risk >= NEWS_RISK_BLOCK",
        "allocator_reject": "GlobalAssetAllocator refused (class/direction/margin caps)",
    }

    print("\nWalking the REAL gate chain from the ready-floor downward (each line = one")
    print("run of the real function with only priority raised as an input):")
    print("-" * 100)
    seen = set()
    priority = best.priority_score
    for step in range(8):
        outcome, cand = _run(priority)
        mark = f"{('PASS ' if outcome == 'open_candidate_failed' else '')}".rstrip()
        print(f"  priority={priority:7.2f} -> last_outcome={outcome}")
        seen.add(outcome)
        if outcome in ("allocator_reject", "open_candidate_failed", "executed", "no_slots", "news_risk_block", "kill_switch"):
            print("-" * 100)
            print(f">>> TERMINAL GATE: {outcome}  ({reason_map[outcome]})")
            if outcome == "allocator_reject":
                _, c2 = _run(priority)
                watch = E.MEMORY.get("watchlist", {}).get(c2.symbol, {})
                print(f"     candidate {c2.symbol} priority={priority:.2f} "
                      f"asset_class={(watch.get('asset_class') if isinstance(watch, dict) else '?')}")
            if outcome == "open_candidate_failed":
                # Dig into WHY open_candidate/execute_entry returned False: the
                # REAL final gate is entry_quality_assessment (engine.py:8641).
                entry_quality_probe(cand)
            break
        # else: candidate was below the ready floor -> raise priority to pass it,
        # revealing the NEXT gate.
        priority = max(priority, ready_floor + 1.0) + 15.0
    else:
        print("  (gate chain exhausted without a terminal gate seen)")


def entry_quality_probe(cand):
    """Read-only probe of the real entry_quality_assessment decision that
    execute_entry_enhanced uses (engine.py:8641). No order is placed."""
    print(f"\n  -- why open_candidate returned False: entry_quality_assessment --")
    try:
        df = _get_ohlcv(cand.symbol, 100)
        ob = _get_ob()
        atr = float(E.compute_atr(df).iloc[-1]) if len(df) > 14 else cand.price * 0.01
        qa = E.entry_quality_assessment(
            cand.symbol, cand.side, cand.price, df, ob, atr,
            cand.priority_score, "INSTITUTIONAL", "PORTFOLIO_MANAGER", "SNIPER",
        )
        print(f"  decision     = {qa['decision']}")
        print(f"  reason       = {qa['reason']}")
        print(f"  quality_score= {qa['quality_score']:.1f}   (>=70 + trap<40 -> APPROVE)")
        print(f"  trap_risk    = {qa['trap_risk']:.1f}")
        print(f"  auth_score   = {qa['authenticity_score']:.1f}")
        print(f"  ob_score     = {qa['order_block_score']:.1f}")
        print(f"  early_class  = {qa['early_expansion']}")
        print(f"  response     = {qa['response_score']:.1f}")
        if qa['decision'] in ('REJECT', 'WAIT'):
            print(f"\n  >>> This is the THIRD, INDEPENDENT score the candidate must pass")
            print(f"      (separate from watchlist STRONG score and queue final_zone_score).")
    except Exception as exc:
        print(f"  entry_quality_probe error: {exc}")


if __name__ == "__main__":
    run()
