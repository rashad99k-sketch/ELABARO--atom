"""Runtime orchestration, exchange reconciliation and portfolio supervision."""
from __future__ import annotations

import os
import threading
import time
import traceback
import requests

import core.engine as E
import scanner.scanner as S
from portfolio.manager import PortfolioManager
from portfolio.allocator import GlobalAssetAllocator
from scanner.deep_scanner import DeepScanner
try:
    from news.reaction import NewsReactionEngine
except Exception:
    NewsReactionEngine = None

# Compatibility exports: legacy modules still call these names through E.
globals().update({k: v for k, v in vars(E).items() if not k.startswith("__")})
globals().update({k: v for k, v in vars(S).items() if not k.startswith("__")})

MAX_OPEN_POSITIONS = max(1, int(os.getenv("MAX_OPEN_POSITIONS", "6")))
PORTFOLIO = PortfolioManager(MAX_OPEN_POSITIONS, E)
ALLOCATOR = GlobalAssetAllocator(PORTFOLIO, E)
DEEP_SCANNER = DeepScanner()


def sync_position_with_exchange(symbol):
    """Compatibility sync that distinguishes PAUSED/ERROR from NOT_FOUND."""
    try:
        if hasattr(E, "fetch_position_status"):
            pos, status = E.fetch_position_status(symbol)
            if status == "PAUSED":
                log_execution(f"[SYNC] {symbol} paused on venue; preserving local state", "WARN", debounce_key=f"runtime_sync_paused_{symbol}", debounce_sec=60)
                return None
            return pos
        if hasattr(ex, "fetch_positions"):
            positions = safe_api_call(ex.fetch_positions)
        elif hasattr(ex, "fetch_open_positions"):
            positions = safe_api_call(ex.fetch_open_positions)
        else:
            return None
        if not positions:
            return None
        for pos in positions:
            pos_sym = pos.get("symbol", pos.get("info", {}).get("symbol", ""))
            if normalize_symbol(symbol) == normalize_symbol(pos_sym) and float(pos.get("contracts", 0)) > 0:
                return pos
        return None
    except Exception as e:
        log_execution(f"[SYNC] error: {e}", "ERROR")
        return None


def get_realized_pnl(symbol, limit=100):
    try:
        trades = safe_api_call(ex.fetch_my_trades, normalize_symbol(symbol), limit=limit)
        if not trades:
            return 0.0, 0.0
        buy_value = sell_value = 0.0
        for trade in trades:
            qty = float(trade.get("amount", 0) or 0)
            price = float(trade.get("price", 0) or 0)
            cost = qty * price
            if str(trade.get("side", "")).lower() == "buy":
                buy_value += cost
            elif str(trade.get("side", "")).lower() == "sell":
                sell_value += cost
        pnl = sell_value - buy_value
        balance = get_balance_safe()
        return pnl, (pnl / balance * 100) if balance > 0 else 0.0
    except Exception as e:
        log_execution(f"[SYNC] error in get_realized_pnl: {e}", "ERROR")
        return 0.0, 0.0


def validate_position_state(local_pos, symbol):
    return local_pos if sync_position_with_exchange(symbol) is not None else None


def sync_all_states():
    """Compatibility sync for single-state callers.

    The production loop uses PORTFOLIO.manage_all(), which isolates state per
    symbol. This function remains for dashboard/manual API compatibility.
    """
    if E.PAPER_MODE:
        MEMORY["position_status"] = "OPEN" if STATE.get("open") else "CLOSED"
        MEMORY["total_pnl"] = PERF.get("total_pnl_usdt", 0.0)
        MEMORY["total_pnl_pct"] = PERF.get("total_pnl_pct", 0.0) * 100
        return

    symbols = PORTFOLIO.symbols()
    if symbols:
        PORTFOLIO.manage_all()
        MEMORY["position_status"] = "OPEN"
        MEMORY["current_positions"] = PORTFOLIO.snapshot()
    else:
        real_pos = sync_position_with_exchange(DEFAULT_SYMBOL)
        MEMORY["position_status"] = "OPEN" if real_pos else "CLOSED"
        MEMORY["current_position"] = real_pos
        real_pnl, real_pnl_pct = get_realized_pnl(DEFAULT_SYMBOL)
        MEMORY["total_pnl"] = real_pnl
        MEMORY["total_pnl_pct"] = real_pnl_pct


def _publish_portfolio_dashboard(dashboard_module):
    positions = PORTFOLIO.snapshot()
    E.DASHBOARD_STATE["positions"] = positions
    perf = E.PERF if isinstance(getattr(E, "PERF", None), dict) else {}
    total_pnl = float(perf.get("total_pnl_usdt", 0.0) or 0.0)
    trades = int(perf.get("trades", 0) or 0)
    wins = int(perf.get("wins", 0) or 0)
    balance = float(E.STATE.get("initial_balance", 0.0) or 0.0)
    if balance <= 0:
        try:
            balance = float(E.get_balance_safe() or 0.0)
        except Exception:
            balance = 0.0
    E.DASHBOARD_STATE["portfolio"] = {
        "open_positions": len(positions),
        "max_positions": PORTFOLIO.max_positions,
        "max_technical_positions": PORTFOLIO.max_technical_positions,
        "max_news_positions": 1,
        "capacity": max(0, PORTFOLIO.max_positions - len(positions)),
        "technical_capacity": max(0, PORTFOLIO.max_technical_positions - sum(1 for _p in positions if str(_p.get("asset_class", "")).upper() != "NEWS")),
        "news_capacity": max(0, 1 - sum(1 for _p in positions if str(_p.get("asset_class", "")).upper() == "NEWS")),
        "asset_classes": {},
        "risk": PORTFOLIO.risk_snapshot(),
        "total_pnl": total_pnl,
        "equity": balance + total_pnl,
        "balance": balance,
        "margin": float(E.STATE.get("margin", 0.0) or 0.0),
        "trades": trades,
        "wins": wins,
        "losses": trades - wins,
        "win_rate": (wins / trades * 100) if trades else 0.0,
        "last_update": time.time(),
    }
    for pos in positions:
        base = str(pos.get("symbol", "")).upper()
        cls = "CRYPTO"
        if any(x in base for x in ("XAU", "GOLD")): cls = "GOLD"
        elif any(x in base for x in ("WTI", "BRENT", "OIL", "CRUDE")): cls = "OIL"
        elif any(x in base for x in ("SPX", "SP500", "NAS", "US30", "DAX", "FTSE", "CAC", "NIKKEI")): cls = "INDEX"
        elif str(pos.get("asset_class", "")).upper() in ("GOLD", "OIL", "INDEX", "CRYPTO", "STOCK"):
            cls = str(pos.get("asset_class", "")).upper()
        E.DASHBOARD_STATE["portfolio"]["asset_classes"][cls] = E.DASHBOARD_STATE["portfolio"]["asset_classes"].get(cls, 0) + 1
    if positions:
        primary = positions[0]
        E.DASHBOARD_STATE["position"] = primary
    else:
        E.DASHBOARD_STATE["position"] = None


def _run_discovery():
    """Run the 20-minute global discovery cycle.

    Legacy RF/Smart scanners remain auxiliary evidence providers. The canonical
    market-wide candidate source is DeepScanner, which seeds the dynamic
    50-60-symbol watchlist and never executes orders.
    """
    try:
        run_scanner_v2()
    except Exception as exc:
        log_execution(f"[SCANNER] v2: {exc}", "WARN")

    try:
        cands = scan_market_rf(top_n=40)
        MEMORY["rf_candidates"] = cands
        # Rebuild the RF dashboard from the candidates just scanned (shape-
        # compatible with the legacy build_rf_dashboard output) without a
        # second market scan.
        MEMORY["rf_dashboard"] = [
            {
                "symbol": c.get("symbol"),
                "status": c.get("status", "PROXIMITY"),
                "icon": "🔔" if c.get("rf_triggered") else "📡",
                "score": c.get("score", 0),
                "adx": c.get("adx", 0),
                "rsi": c.get("rsi", 0),
                "atrp": c.get("atrp", 0),
                "signal": c.get("rf_signal") or "N/A",
            }
            for c in (cands or [])
        ]
        MEMORY["rf_last_scan"] = time.time()
    except Exception as exc:
        log_execution(f"[SCANNER] RF auxiliary: {exc}", "WARN")

    try:
        candidates = DEEP_SCANNER.scan(force=True)
        MEMORY["top_candidates"] = candidates[:60]
        MEMORY["scanned_count"] = DEEP_SCANNER.stats.get("radar_scanned", 0)
        MEMORY["last_scan"] = DEEP_SCANNER.last_scan
    except Exception as exc:
        log_execution(f"[SCANNER] dynamic deep discovery: {traceback.format_exc()}", "ERROR")


def _service_watchlist_and_queue():
    """Continuously analyze watchlist, promote mature setups and re-evaluate queue."""
    # Early Institutional Radar: keep the staggered 20-minute discovery cycle
    # advancing one time-boxed batch per service tick (never a blocking pass).
    try:
        DEEP_SCANNER.advance_radar_cycle()
    except Exception as exc:
        log_execution(f"[RADAR] advance error: {exc}", "WARN")

    try:
        updated = DEEP_SCANNER.monitor_watchlist()
        if updated:
            log_execution(
                f"[WATCHLIST] Deep batch analyzed={len(updated)} | "
                f"active={len(MEMORY.get('watchlist', {}))}",
                "INFO",
                debounce_key="watchlist_batch",
                debounce_sec=20,
            )
    except Exception as exc:
        log_execution(f"[WATCHLIST] monitor error: {exc}", "WARN")

    try:
        promoted = promote_to_queue()
        if promoted:
            log_execution(
                f"[QUEUE] Promoted {promoted} mature watchlist candidate(s)",
                "INFO",
                debounce_key="queue_promotions",
                debounce_sec=20,
            )
    except Exception as exc:
        log_execution(f"[QUEUE] promotion error: {exc}", "WARN")

    # Lifecycle cleanup lives here, never in dashboard read routes.
    try:
        cleanup_watchlist(float(os.getenv("WATCHLIST_EXPIRE_AFTER_SEC", "600")))
    except Exception as exc:
        log_execution(f"[WATCHLIST] cleanup error: {exc}", "WARN")

    MEMORY["queue_status"] = queue.get_status() if USE_EXECUTION_QUEUE else {}
    # BARON Moves: rank emerging formations, not already-expanded gainers.
    try:
        wl = MEMORY.get("watchlist", {}) or {}
        early = []
        for _sym, _item in wl.items():
            if not isinstance(_item, dict):
                continue
            _m = str(_item.get("move_maturity", "UNKNOWN"))
            _f = float(_item.get("formation_score", 0.0) or 0.0)
            if _m in ("PRE_EXPANSION", "EARLY_EXPANSION") and _f >= 45:
                early.append({
                    "symbol": _sym, "side": _item.get("side"), "formation_score": _f,
                    "maturity": _m, "formation_verdict": _item.get("formation_verdict", "NEUTRAL"),
                    "evidence": _item.get("early_formation", {}).get("evidence", []),
                    "zone": _item.get("zone"), "location": _item.get("location"),
                    "institutional_stage": _item.get("institutional_stage"),
                    "news_risk": _item.get("news_risk", 0),
                })
        early.sort(key=lambda x: (x["formation_score"], 1 if x["maturity"] == "PRE_EXPANSION" else 0), reverse=True)
        MEMORY["early_moves"] = early[:20]
        E.DASHBOARD_STATE["early_moves"] = early[:20]
    except Exception as exc:
        log_execution(f"[EARLY_MOVES] publication failed: {exc}", "WARN")

    # News -> reaction telemetry. This is causal research context only; it
    # never creates an order or changes an open position by itself.
    if NewsReactionEngine is not None:
        try:
            _nr = NewsReactionEngine()
            _prices = MEMORY.setdefault("news_reaction_prices", {})
            _reactions = []
            _watch = MEMORY.get("watchlist", {}) or {}
            _news_items = []
            for _sym, _item in _watch.items():
                if not isinstance(_item, dict):
                    continue
                _news = _item.get("news") or {}
                _heads = _news.get("headlines", []) if isinstance(_news, dict) else []
                if _heads:
                    _news_items.append((_sym, _item, _heads))
            _max_news_reaction = max(1, int(os.getenv("NEWS_REACTION_MAX_SYMBOLS", "24")))
            for _sym, _item, _heads in _news_items[:_max_news_reaction]:
                _px = _item.get("price")
                try:
                    _px = float(_px or 0.0)
                except Exception:
                    _px = 0.0
                if _px <= 0:
                    _px = get_ticker_safe(_sym)
                if not _px: continue
                _now = time.time()
                _old = _prices.get(_sym)
                _prices[_sym] = {"price":float(_px),"ts":_now}
                if _old and _now-float(_old.get("ts",0)) <= float(os.getenv("NEWS_REACTION_WINDOW_SEC","600")):
                    _reactions.append({"symbol":_sym,"headline":_heads[0].get("title", ""),"reaction":_nr.reaction(_old.get("price"),_px,_item.get("side"))})
            E.DASHBOARD_STATE["news_reaction"] = {"updated_at":time.time(),"items":_reactions[:20]}
        except Exception as exc:
            log_execution(f"[NEWS_REACTION] telemetry degraded: {exc}", "WARN", debounce_key="news_reaction", debounce_sec=120)

    # Continuously evaluate global allocation over the current queue snapshot
    # (read-only publication; the actual gate still only fires on READY).
    try:
        if USE_EXECUTION_QUEUE:
            queue_snapshot = [c.to_dict() for c in queue._candidates.values()]
            report = ALLOCATOR.allocate(queue_snapshot, limit=PORTFOLIO.max_positions)
            MEMORY["portfolio_allocation"] = report.to_dict()
    except Exception as exc:
        log_execution(f"[PORTFOLIO] allocation evaluation failed: {exc}", "WARN")

def _class_ready_score_floor(candidate):
    """SINGLE SOURCE OF TRUTH for the READY/EXECUTION score floor.

    Returns the exact floor the readiness authority used to grant READY:
    AssetBehaviorProfile.entry_config().ready_score (engine.py:16121 /
    2780-2787), preferring the per-candidate value stored by the engine at
    readiness time. There is deliberately NO independent global floor here: a
    READY candidate (ready_blocker == NONE) must not be rejected by a second,
    duplicated threshold.
    """
    req = getattr(candidate, "ready_score_required", None)
    if req is not None:
        try:
            return float(req)
        except Exception:
            pass
    try:
        _cls = E.AssetBehaviorProfile.resolve_asset_class(candidate.symbol)
        return float(E.AssetBehaviorProfile.entry_config(_cls).get("ready_score", 75))
    except Exception:
        return 75.0


# Classification of open_candidate_failed events. Every failure is split into an
# explicit category (never one generic bucket) so dashboard telemetry can show a
# breakdown instead of a single counter.
OPEN_FAILURE_CATEGORY_ORDER = (
    ("BARON_ERROR", "judge_error"),
    ("BARON_REJECT", "judge_block"),
    ("EXECUTION_ERROR", "execution_error"),
    ("MARGIN", "sizing_margin"),
    ("QTY", "sizing_qty"),
    ("ADX", "adx_reject"),
    ("LIQUIDITY", "liquidity_reject"),
    ("ENTRY_QUALITY", "entry_quality"),
    ("RISK", "risk_reject"),
    ("DUPLICATE", "duplicate"),
    ("NEWS_SLOT_FULL", "news_capacity"),
    ("TOTAL_CAPACITY_FULL", "total_capacity"),
    ("TECHNICAL_CAPACITY_FULL", "technical_capacity"),
    ("_CAPACITY_FULL", "capacity"),
    ("_CAP", "capacity"),
    ("DATA", "data"),
    ("SESSION", "session"),
    ("SPREAD", "spread"),
    ("SYMBOL", "symbol_mapping"),
    ("COOLDOWN", "cooldown"),
    ("UNKNOWN", "unknown"),
)


def _classify_open_failure(symbol, side: str, exec_pipe: dict,
                           explicit_token=None, explicit_blocker=None) -> None:
    """Bucket a PORTFOLIO.open_candidate failure into a precise category using
    the breadcrumbs the path itself left (allocator reason, portfolio manager
    _can_open_blocker, engine _record_exec_blocker -> last_open_outcome,
    EXECUTION_ERROR). Pure accounting; never raises; never re-decides the entry.

    `explicit_token` / `explicit_blocker` let non-portfolio gates (e.g. the
    allocator-reject branch) classify with the allocator's OWN reason token and
    the user-facing label, so a capacity reject like INDEX_STOCK_CAP is never
    shelved as category=other / blocker=NONE. A lifecycle primary_blocker of
    "NONE" is treated as MISSING (it just means "READY-approved"), so the real
    blocker recorded by execute_entry (OPEN_REJECTED / last_exec_blocker)
    wins instead of the 80-event "other" trap.
    """
    try:
        lc = MEMORY.setdefault("opportunity_lifecycle", {}).get(symbol, {})
        if explicit_token is not None:
            token = str(explicit_token)
        else:
            _raw = str(lc.get("primary_blocker") or "")
            token = "" if _raw.upper() in ("", "NONE") else _raw
        if not token:
            _st = STATE if isinstance(STATE, dict) else {}
            _out = str(_st.get("last_open_outcome") or "")
            if _out.startswith("OPEN_REJECTED:"):
                token = _out.split(":", 1)[1].strip()
            else:
                _bl = _st.get("last_exec_blocker") or {}
                if isinstance(_bl, dict):
                    token = str(_bl.get("blocker") or "")
        if not token:
            token = str(lc.get("execution_error") or "UNKNOWN")
        upper = token.upper()
        category = "other"
        for _frag, _cat in OPEN_FAILURE_CATEGORY_ORDER:
            if _frag in upper:
                category = _cat
                break
        blocker = str(explicit_blocker if explicit_blocker is not None else token)
        key = f"open_failure_category:{category}"
        exec_pipe[key] = exec_pipe.get(key, 0) + 1
        exec_pipe["last_open_failure_category"] = category
        exec_pipe["last_open_failure_blocker"] = blocker
    except Exception:
        try:
            exec_pipe["open_failure_category:unknown"] = exec_pipe.get("open_failure_category:unknown", 0) + 1
        except Exception:
            pass


def _record_open_attempt(exec_pipe: dict, *, symbol="", asset_class="", bucket="",
                         lifecycle_state="READY", portfolio_capacity_result="",
                         rejection_reason="", rejection_category="",
                         next_queue_action="", raw_asset_class="") -> None:
    """Deterministic per-OPEN_REQUESTED-attempt telemetry ring (bounded).

    Every READY candidate offered to the allocation/execution path appends one
    structured record so the dashboard can show, per symbol, exactly where it
    died and what the queue did next — instead of only aggregate counters.
    Never raises; pure accounting.

    Classification is recorded at BOTH layers when they legitimately differ:
    `raw_asset_class` is the watch/scanner-seeded class (e.g. deep_scanner
    writes FOREX for NCFX symbols) and `asset_class` is the class the layer
    actually used (allocator decision class / candidate class), so a
    split-brain (raw FOREX vs used CRYPTO) is never silently hidden.
    """
    try:
        _attempts = exec_pipe.setdefault("open_attempts", [])
        _attempts.append({
            "symbol": str(symbol or ""),
            "asset_class": str(asset_class or ""),
            "raw_asset_class": str(raw_asset_class or ""),
            "bucket": str(bucket or ""),
            "lifecycle_state": str(lifecycle_state or "READY"),
            "portfolio_capacity_result": str(portfolio_capacity_result or ""),
            "rejection_reason": str(rejection_reason or ""),
            "rejection_category": str(rejection_category or ""),
            "next_queue_action": str(next_queue_action or ""),
            "ts": time.time(),
        })
        if len(_attempts) > 200:
            del _attempts[:-200]
        exec_pipe["last_open_attempt"] = _attempts[-1]
    except Exception:
        pass


def _execute_ready_queue_candidate():
    """Execute only a READY queue candidate through PortfolioManager.

    This preserves the intended pipeline:
    Discovery -> Watchlist -> Institutional Queue -> READY -> Portfolio execution.
    The scanner itself never places an order.
    """
    if not USE_EXECUTION_QUEUE:
        return False

    # User-facing capacity labels mapped from the allocator's internal reasons.
    # Kept additive: internal reason strings (TECHNICAL_CAP, *_CAP, SLOT_CAP -
    # pinned by existing tests) are never rewritten, only surfaced alongside.
    # Combined-bucket reasons (INDEX_STOCK_CAP, COMMODITY_CAP) map to explicit
    # "capacity used=x/max=y" labels; legacy tokens (INDEX_CAP, GOLD_CAP,
    # OIL_CAP) stay mapped for any historical trace.
    PORTFOLIO_REASON_USER = {
        "SLOT_CAP": "TOTAL_PORTFOLIO_CAPACITY_FULL",
        "TECHNICAL_CAP": "TECHNICAL_CAPACITY_FULL",
        "CRYPTO_CAP": "CRYPTO_SLOT_FULL",
        "INDEX_CAP": "INDEX_SLOT_FULL",
        "GOLD_CAP": "GOLD_SLOT_FULL",
        "OIL_CAP": "OIL_SLOT_FULL",
        "NEWS_CAP": "NEWS_SLOT_FULL",
        "INDEX_STOCK_CAP": "INDEX_STOCK_CAPACITY_FULL",
        "COMMODITY_CAP": "COMMODITY_CAPACITY_FULL",
        # NCFX forex instruments classify as FOREX (universe pattern) and FOREX
        # has a permanently-0 capacity bucket (discovered, never opened), so the
        # allocator's FOREX_CAP maps to the same explicit user-facing blocker
        # the portfolio layer reports (FOREX_CAPACITY_FULL).
        "FOREX_CAP": "FOREX_CAPACITY_FULL",
    }

    exec_pipe = MEMORY.setdefault("pipeline", {}).setdefault("execution", {})
    def _exec_gate(outcome):
        exec_pipe[outcome] = exec_pipe.get(outcome, 0) + 1
        exec_pipe["last_outcome"] = outcome
        exec_pipe["ts"] = time.time()

    # Safety barrier: emergency kill switch (daily drawdown / loss limit) must
    # actually gate new entries, not merely exist as dead code.
    try:
        if emergency_kill_switch_active():
            log_execution(
                "[SAFETY] Kill switch active — new entries blocked",
                "WARN",
                debounce_key="kill_switch_active",
                debounce_sec=300,
            )
            _exec_gate("kill_switch")
            return False
    except Exception as exc:
        log_execution(f"[SAFETY] kill-switch check failed: {exc}", "WARN")

    slots = PORTFOLIO.max_positions - PORTFOLIO.count()
    if slots <= 0:
        _exec_gate("no_slots")
        exec_pipe["last_reject_reason"] = "SLOT_CAP"
        exec_pipe["last_reject_reason_user"] = "TOTAL_PORTFOLIO_CAPACITY_FULL"
        # Total-portfolio capacity refusals must ALSO enter the failure
        # taxonomy (category=capacity, blocker=TOTAL_PORTFOLIO_CAPACITY_FULL)
        # instead of leaving last_open_failure_category stale.
        _classify_open_failure("", "", exec_pipe,
                               explicit_token="SLOT_CAP",
                               explicit_blocker="TOTAL_PORTFOLIO_CAPACITY_FULL")
        _record_open_attempt(
            exec_pipe,
            symbol="",
            asset_class="",
            bucket="TOTAL",
            lifecycle_state="READY",
            portfolio_capacity_result="SLOT_CAP (TOTAL_PORTFOLIO_CAPACITY_FULL)",
            rejection_reason="SLOT_CAP",
            rejection_category="capacity",
            next_queue_action="no_slot_wait",
        )
        return False

    try:
        best = queue.get_best_candidate()
        # SINGLE SOURCE OF TRUTH for the READY/EXECUTION score floor.
        # READY is granted by the class-aware readiness authority
        # (AssetBehaviorProfile.entry_config().ready_score, engine.py:16121 /
        # 2780-2787). The execution path re-checks ONLY that same floor, so a
        # candidate that is genuinely READY (ready_blocker == NONE) is never
        # killed by a second, duplicated threshold (the legacy global
        # QUEUE_MIN_READY_SCORE=75 was exactly that duplicate). A candidate
        # offered through the confirmed-trigger fallback (Waiting) is NOT
        # hard-blocked by any score floor: the authoritative final decision is
        # the Entry Quality / quality assessment inside
        # PORTFOLIO.open_candidate -> execute_entry. All other execution gates
        # (kill switch, slots, news, allocator, portfolio, execute_entry,
        # Zone/OB Judge, sizing, exchange validation) are unchanged below.
        is_ready = best is not None and best.state == ExecutionState.READY
        if best is None:
            _exec_gate("no_ready_candidate")
            return False
        if is_ready:
            ready_floor = _class_ready_score_floor(best)
            if best.priority_score + 1e-9 < ready_floor:
                _exec_gate("ready_score_below_min")
                E.record_gate_event(
                    best.symbol, "EXECUTION", "READY_REJECTED",
                    f"score={best.priority_score:.2f} required={ready_floor:.1f} "
                    f"candidate_id={getattr(best, 'candidate_id', '') or getattr(best, 'trade_id', '')} "
                    f"gate=READY_FLOOR reason=below class ready score",
                    best.side)
                return False

        # News is contextual evidence/catalyst and execution-risk input; it must
        # not independently qualify or create an entry. Technical and
        # institutional confirmation remain the primary decision authority.
        news_risk = 0.0
        watch = MEMORY.get("watchlist", {}).get(best.symbol, {})
        if isinstance(watch, dict):
            news_risk = float(watch.get("news_risk", 0) or 0)
        if news_risk >= float(os.getenv("NEWS_RISK_BLOCK", "80")):
            _exec_gate("news_risk_block")
            E.record_gate_event(best.symbol, "EXECUTION", "NEWS_RISK_BLOCK",
                                f"news_risk={news_risk:.0f}", best.side)
            return False

        # CLASSIFICATION COHERENCE: prefer the watch-stored explicit class when
        # present (deep_scanner writes FOREX for NCFX symbols); otherwise derive
        # from the symbol via PortfolioManager (which routes the NCFX forex
        # prefix to FOREX), NEVER a silent hardcoded "CRYPTO" fallback — that
        # default let an NCFX instrument (asset_class=FOREX) be classified as
        # CRYPTO by the allocator while the portfolio layer rejected it with
        # FOREX_CAPACITY_FULL (the split-brain this file reproduces).
        _cand_ac = ""
        if isinstance(watch, dict) and str(watch.get("asset_class") or ""):
            _cand_ac = str(watch.get("asset_class")).upper()
        else:
            _cand_ac = PORTFOLIO._asset_class(best.symbol)
        candidate = {
            "symbol": best.symbol,
            "side": best.side,
            "price": best.price,
            "sl": best.stop_loss,
            "tp1": best.take_profit_1,
            "tp2": best.take_profit_2,
            "score": best.priority_score,
            "atr": best.atr,
            "scenario": best.opportunity_type.value,
            "asset_class": _cand_ac,
            "news": watch.get("news", {}) if isinstance(watch, dict) else {},
            "trade_id": getattr(best, "trade_id", "") or getattr(best, "candidate_id", ""),
            "trade_type": getattr(best, "trade_type", "TREND"),
            "classification": getattr(best, "classification", "INSTITUTIONAL_SNIPER"),
            "location": watch.get("location") if isinstance(watch, dict) else None,
            "zone_info": watch.get("zone") if isinstance(watch, dict) else None,
            "narrative_classification": watch.get("narrative_classification") if isinstance(watch, dict) else None,
            "narrative_confidence": watch.get("narrative_confidence", 0.0) if isinstance(watch, dict) else 0.0,
            "confidence_level": watch.get("confidence_level") if isinstance(watch, dict) else None,
            "institutional_stage": watch.get("institutional_phase") if isinstance(watch, dict) else None,
            "move_maturity": watch.get("move_maturity", getattr(best, "move_maturity", "UNKNOWN")) if isinstance(watch, dict) else getattr(best, "move_maturity", "UNKNOWN"),
            "early_formation": watch.get("early_formation", {}) if isinstance(watch, dict) else {},
            "zone": watch.get("zone") if isinstance(watch, dict) else None,
            "execution_context": {
                "is_ready_validated": is_ready,
                "ready_ts": float(getattr(best, "ready_time", 0) or 0),
                "ready_adx": float(getattr(best, "latest_adx", 0) or 0),
            },
        }

        # Global portfolio-allocator gate: rejects when class/direction caps
        # would be violated, and records the reason for dashboard explain.
        # EXECUTED candidates are already counted as open positions via
        # PORTFOLIO.contexts; forwarding them into the allocator snapshot would
        # double-count capacity and wrongly cap new entries of the same class.
        queue_snapshot = [c.to_dict() for c in queue._candidates.values()
                          if c.state != ExecutionState.EXECUTED]
        alloc_report = ALLOCATOR.allocate(queue_snapshot + [candidate], limit=PORTFOLIO.max_positions)
        for decision in alloc_report.decisions:
            if decision.symbol == candidate["symbol"]:
                if not decision.allowed:
                    MEMORY["portfolio_allocation"] = alloc_report.to_dict()
                    _exec_gate("allocator_reject")
                    _reason_user = PORTFOLIO_REASON_USER.get(decision.reason, decision.reason)
                    # FAILURE CLASSIFICATION: the allocator-reject branch must
                    # flow through the SAME taxonomy so INDEX_STOCK_CAP ->
                    # category=capacity, blocker=INDEX_STOCK_CAPACITY_FULL
                    # (never "other"/NONE). This is what makes allocator_reject
                    # count reconcile with open_failure_category:capacity and
                    # last_open_failure_* instead of leaving stale values.
                    _classify_open_failure(candidate["symbol"], candidate["side"], exec_pipe,
                                           explicit_token=decision.reason,
                                           explicit_blocker=_reason_user)
                    # QUEUE ADVANCE: capacity-rejected candidates are backed
                    # off (not re-picked as the highest-READY forever) so the
                    # next eligible candidate — a free CRYPTO/OIL/GOLD/NEWS seat
                    # — is evaluated on the following cycle. The allocator
                    # reason is preserved for the operator.
                    try:
                        _bsoff = float(os.getenv("QUEUE_ALLOCATOR_BACKOFF_SEC", "300"))
                        _best_cand = queue._candidates.get(candidate["symbol"])
                        if _best_cand is not None:
                            _best_cand.allocator_rejected_until = time.time() + _bsoff
                            _best_cand.last_allocator_reason = decision.reason
                            _best_cand.last_allocator_reason_user = _reason_user
                    except Exception:
                        pass
                    exec_pipe["last_reject_reason"] = decision.reason
                    exec_pipe["last_reject_reason_user"] = _reason_user
                    # Full capacity trace: symbol / class / bucket used+max /
                    # technical used+max / news used+max / total used+max, so
                    # the dashboard shows exactly where the candidate died
                    # instead of a bare token like STOCK_CAP.
                    exec_pipe["last_reject_symbol"] = decision.symbol
                    exec_pipe["last_reject_asset_class"] = decision.asset_class
                    for _k, _v in decision.trace_dict().items():
                        exec_pipe[f"last_reject_{_k}"] = _v
                    E.record_gate_event(candidate["symbol"], "RISK", "ALLOCATOR_REJECT",
                                        f"{decision.reason} ({_reason_user}) "
                                        f"bucket={decision.bucket} {decision.bucket_used}/{decision.bucket_max}",
                                        candidate["side"])
                    log_execution(
                        f"[PORTFOLIO] {candidate['symbol']} rejected: {decision.reason} "
                        f"[{decision.bucket} {decision.bucket_used}/{decision.bucket_max} "
                        f"tech {decision.technical_used}/{decision.technical_max} "
                        f"total {decision.total_used}/{decision.total_max}]",
                        "INFO",
                        debounce_key=f"alloc_reject_{candidate['symbol']}",
                        debounce_sec=60,
                    )
                    _record_open_attempt(
                        exec_pipe,
                        symbol=candidate["symbol"],
                        asset_class=decision.asset_class,
                        raw_asset_class=candidate.get("asset_class", ""),
                        bucket=decision.bucket,
                        lifecycle_state=getattr(best, "state", None).value if getattr(best, "state", None) is not None else "READY",
                        portfolio_capacity_result=f"{decision.reason} ({_reason_user}) "
                                                  f"bucket={decision.bucket} {decision.bucket_used}/{decision.bucket_max}",
                        rejection_reason=decision.reason,
                        rejection_category=exec_pipe.get("last_open_failure_category", "other"),
                        next_queue_action="backoff+advance_queue",
                    )
                    return False
                break

        MEMORY["portfolio_allocation"] = alloc_report.to_dict()
        # Lifecycle observability: allocator stage committed for this candidate.
        try:
            _lc = MEMORY.setdefault("opportunity_lifecycle", {}).setdefault(best.symbol, {})
            _lc["allocator_time"] = time.time()
            _lc["state"] = best.state.value
            _lc["primary_blocker"] = best.ready_blocker
        except Exception:
            pass
        if PORTFOLIO.open_candidate(candidate):
            with queue._lock:
                if best.symbol in queue._candidates:
                    queue._candidates[best.symbol].state = ExecutionState.EXECUTED
                    queue.total_executed += 1
            # Lifecycle observability: execution + opened timestamps.
            try:
                _lc = MEMORY.setdefault("opportunity_lifecycle", {}).setdefault(best.symbol, {})
                _lc["execution_time"] = time.time()
                _lc["opened_time"] = time.time()
                _lc["state"] = "EXECUTED"
                _lc["primary_blocker"] = "NONE"
            except Exception:
                pass
            _exec_gate("executed")
            E.record_gate_event(best.symbol, "EXECUTION", "EXECUTED",
                                f"priority={best.priority_score:.1f}", best.side)
            log_execution(
                f"[QUEUE] READY -> EXECUTED {best.symbol} {best.side} "
                f"priority={best.priority_score:.1f}",
                "SUCCESS",
            )
            _record_open_attempt(
                exec_pipe,
                symbol=best.symbol,
                asset_class=best.asset_class,
                raw_asset_class=_cand_ac,
                bucket="",
                lifecycle_state="EXECUTED",
                portfolio_capacity_result="ALLOWED (executed)",
                rejection_reason="",
                rejection_category="",
                next_queue_action="position_open",
            )
            return True
        _exec_gate("open_candidate_failed")
        _classify_open_failure(best.symbol, best.side, exec_pipe)
        # QUEUE ADVANCE for open_candidate_failed capacity rejections. A
        # deterministic capacity blocker (FOREX_CAPACITY_FULL, TOTAL,
        # TECHNICAL, NEWS_SLOT, <bucket>_CAPACITY_FULL) must NOT re-pick the
        # same highest-READY candidate every cycle forever. Mirror the
        # allocator-reject branch: back off so the queue advances to the next
        # eligible candidate (a free CRYPTO/OIL/GOLD/NEWS seat). Non-capacity
        # failures (RISK, ADX, liquidity, execution) keep retry_next_cycle —
        # they are transient and must not be masked.
        _oa_cat = exec_pipe.get("last_open_failure_category", "other")
        _oa_is_capacity = _oa_cat in ("capacity", "news_capacity",
                                      "total_capacity", "technical_capacity")
        if _oa_is_capacity:
            try:
                _bsoff = float(os.getenv("QUEUE_ALLOCATOR_BACKOFF_SEC", "300"))
                _kand = queue._candidates.get(best.symbol)
                if _kand is not None:
                    _kand.allocator_rejected_until = time.time() + _bsoff
                    _kand.last_allocator_reason = str(exec_pipe.get("last_open_failure_blocker", "CAPACITY"))
                    _kand.last_allocator_reason_user = str(exec_pipe.get("last_open_failure_blocker", "CAPACITY"))
            except Exception:
                pass
        _oa_next = "backoff+advance_queue" if _oa_is_capacity else "retry_next_cycle"
        _watch_ac = watch.get("asset_class") if isinstance(watch, dict) else ""
        _record_open_attempt(
            exec_pipe,
            symbol=best.symbol,
            asset_class=getattr(best, "asset_class", ""),
            raw_asset_class=str(_watch_ac or ""),
            bucket="",
            lifecycle_state=getattr(best, "state", None).value if getattr(best, "state", None) is not None else "READY",
            portfolio_capacity_result="PORTFOLIO/open_candidate rejected",
            rejection_reason=exec_pipe.get("last_open_failure_blocker", "UNKNOWN"),
            rejection_category=_oa_cat,
            next_queue_action=_oa_next,
        )
    except Exception as exc:
        log_execution(f"[QUEUE] ready execution error: {exc}", "ERROR")
        # A swallowed exception here used to leave the candidate READY and emit
        # a fresh OPEN_REQUESTED every re-eval tick with NO visible outcome.
        # Surface the failure in the gate feed + lifecycle so it can never
        # silently disappear; the queue clean-up still governs retry cadence.
        _best = locals().get("best")
        if _best is not None:
            try:
                E.record_gate_event(_best.symbol, "EXECUTION", "EXECUTION_ERROR",
                                    f"ready execution raised: {str(exc)[:180]}", _best.side)
                _lc = MEMORY.setdefault("opportunity_lifecycle", {}).setdefault(_best.symbol, {})
                _lc["primary_blocker"] = "EXECUTION_ERROR"
                _lc["execution_error"] = str(exc)[:220]
            except Exception:
                pass
        _exec_gate("execution_error")
    return False


def execute_news_slot():
    """Independent, news-driven slot (OPT-IN, env NEWS_SLOT_ENABLED=True).

    Completely separate from the technical OB/Zone ranking:
      * qualifies ONLY via strong news (direction from the news bias),
      * may open on ANY asset class but never consumes that class's cap
        (the candidate is classified as NEWS),
      * keeps total open positions <= MAX_OPEN_POSITIONS (default 6),
      * reserves exactly 1 NEWS position (slot cap), and
      * still routes through the unified safety path (kill switch,
        extreme news-risk block, sizing / SL / TP / position management) via
        PORTFOLIO.open_candidate -> engine.execute_entry, which remains the
        authority before any real order.
    """
    if str(os.getenv("NEWS_SLOT_ENABLED", "False")).lower() not in ("1", "true", "yes"):
        return False
    try:
        import portfolio.news_slot as news_slot
    except Exception as exc:
        log_execution(f"[NEWS_SLOT] init error: {exc}", "WARN")
        return False

    exec_pipe = MEMORY.setdefault("pipeline", {}).setdefault("execution", {})
    def _gate(outcome):
        exec_pipe[outcome] = exec_pipe.get(outcome, 0) + 1
        exec_pipe["last_outcome"] = outcome

    try:
        if emergency_kill_switch_active():
            _gate("news_kill_switch")
            return False
    except Exception:
        pass

    # Global slot cap: total open positions must stay <= MAX_OPEN_POSITIONS.
    if PORTFOLIO.max_positions - PORTFOLIO.count() <= 0:
        _gate("news_no_slots")
        exec_pipe["last_reject_reason"] = "SLOT_CAP"
        exec_pipe["last_reject_reason_user"] = "TOTAL_PORTFOLIO_CAPACITY_FULL"
        return False

    # NEWS slot is capped at 1 open news position (independent of class caps).
    if news_slot.count_open_news(PORTFOLIO) >= 1:
        _gate("news_slot_full")
        exec_pipe["last_reject_reason"] = "NEWS_CAP"
        exec_pipe["last_reject_reason_user"] = "NEWS_SLOT_FULL"
        return False

    watchlist = MEMORY.get("watchlist", {}) or {}
    cand = news_slot.scan_for_news_candidate(watchlist)
    if cand is None:
        _gate("news_no_candidate")
        return False

    # A headline is only the trigger for investigation. Before execution the
    # NEWS slot requires a measured post-headline price reaction in the same
    # direction; this closes the blind-headline trading path.
    reaction = news_slot._confirmed_reaction(
        cand["symbol"], cand["side"], E.DASHBOARD_STATE.get("news_reaction")
    )
    if reaction is None:
        cand["news_reaction"] = {"status": "WAITING_REACTION", "reaction": "NOT_CONFIRMED"}
        E.record_gate_event(
            cand["symbol"], "NEWS", "NEWS_REACTION_WAIT",
            "headline qualified; waiting for directional market reaction", cand["side"]
        )
        _gate("news_waiting_reaction")
        return False
    cand["news_reaction"] = {"status": "CONFIRMED", **reaction}

    # The NEWS slot is the qualification authority for its candidate: headline
    # bias + confirmed directional market reaction + news-risk chain. Mark the
    # candidate READY-validated so the BARON judge's soft WAIT_RETEST verdict
    # is advisory here too (BLOCK verdicts stay fail-closed); otherwise the
    # independent news slot would be silently killed by the same hidden
    # secondary threshold that blocked technical READY candidates.
    try:
        cand.setdefault("execution_context", {})
        if not cand.get("execution_context"):
            cand["execution_context"] = {}
        cand["execution_context"].setdefault("is_ready_validated", True)
        cand["execution_context"].setdefault("ready_ts", time.time())
    except Exception:
        pass

    try:
        if PORTFOLIO.open_candidate(cand):
            _gate("news_executed")
            E.record_gate_event(cand["symbol"], "EXECUTION", "NEWS_SLOT_EXECUTED",
                                f"side={cand['side']}", None)
            log_execution(
                f"[NEWS_SLOT] Opened independent news trade {cand['symbol']} {cand['side']}",
                "SUCCESS",
            )
            return True
        _gate("news_open_candidate_failed")
    except Exception as exc:
        log_execution(f"[NEWS_SLOT] execution error: {exc}", "WARN")
        _gate("news_error")
    return False


def portfolio_loop(dashboard_module=None):
    last_discovery = 0.0
    last_watch_service = 0.0
    last_snapshot = 0.0
    last_queue_eval = 0.0
    discovery_interval = float(os.getenv("GLOBAL_SCAN_INTERVAL_SEC", "900"))
    watch_interval = float(os.getenv("WATCHLIST_SERVICE_INTERVAL_SEC", "20"))
    snapshot_interval = float(os.getenv("SNAPSHOT_INTERVAL", "60"))

    try:
        ex.load_markets()
        log_execution("Markets loaded", "INFO")
    except Exception as exc:
        log_execution(f"Failed to load markets: {exc}", "ERROR")

    # P0 restart recovery: adopt exchange truth once before new discovery.
    # Paper mode is a no-op; API failures leave the account state untouched.
    if MODE_LIVE:
        try:
            recovered = PORTFOLIO.recover_from_exchange()
            log_execution(f"[RECOVERY] startup adoption complete: {recovered} position(s)", "SUCCESS" if recovered else "INFO")
        except Exception as exc:
            log_execution(f"[RECOVERY] startup recovery failed: {exc}", "ERROR")

    tg_start(get_balance_safe(), "LIVE" if MODE_LIVE else "PAPER")

    while not E.shutdown_requested():
        try:
            now = time.time()

            # Existing positions are serviced first.
            if PORTFOLIO.count():
                PORTFOLIO.manage_all()

            # Account-wide adoption: manual/external positions that appeared on
            # the venue while the process is running are discovered and managed
            # through the SAME LiveTradeManager.  This is management coverage
            # only (never an entry/gate decision), and is throttled to avoid
            # hammering the exchange API every loop tick.
            try:
                PORTFOLIO.adopt_unmanaged_from_exchange(
                    throttle_sec=float(os.getenv("ADOPTION_SCAN_INTERVAL_SEC", "30") or 30.0))
            except Exception as exc:
                log_execution(f"[PORTFOLIO] adoption scan error: {exc}", "WARN")

            # Scanner/queue work runs outside any active portfolio context.
            PORTFOLIO.activate(None)
            try:
                if now - last_discovery >= discovery_interval:
                    _run_discovery()
                    last_discovery = now

                if now - last_watch_service >= watch_interval:
                    _service_watchlist_and_queue()
                    last_watch_service = now

                # Queue is re-evaluated frequently, but not on every main-loop
                # tick, to avoid hammering the exchange API.
                if USE_EXECUTION_QUEUE and now - last_queue_eval >= float(os.getenv("QUEUE_RE_EVAL_INTERVAL", "5")):
                    try:
                        queue.re_evaluate_all(lambda sym: get_ohlcv_safe(sym, 100))
                        MEMORY["queue_last_evaluation"] = time.time()
                        last_queue_eval = now
                    except Exception as exc:
                        log_execution(f"[QUEUE] fast re-evaluation error: {exc}", "WARN")

                _execute_ready_queue_candidate()
                # Independent, opt-in news slot. Runs after the technical path
                # and never consumes a technical class cap (NEWS asset class).
                execute_news_slot()
                if USE_EXECUTION_QUEUE:
                    queue.cleanup()
                    MEMORY["queue_status"] = queue.get_status()

                # Keep the institutional flow panel alive independently.
                if now - MEMORY.get("last_flow_update", 0) >= 60:
                    try:
                        update_institutional_flow_scanner()
                    except Exception as exc:
                        log_execution(f"[SCANNER] flow updater: {exc}", "WARN")
                    MEMORY["last_flow_update"] = now
            finally:
                PORTFOLIO.deactivate()

            if dashboard_module:
                _publish_portfolio_dashboard(dashboard_module)

            if time.time() - last_snapshot >= snapshot_interval:
                try:
                    if dashboard_module and hasattr(dashboard_module, "print_snapshot"):
                        dashboard_module.print_snapshot()
                except Exception as exc:
                    log_execution(f"[SNAPSHOT] {exc}", "WARN")
                last_snapshot = time.time()

            try:
                if dashboard_module and hasattr(dashboard_module, "hourly_cleanup"):
                    dashboard_module.hourly_cleanup()
            except Exception:
                pass

            E._RUNTIME_SHUTDOWN.wait(max(1, BASE_SLEEP))

        except Exception:
            if E.shutdown_requested():
                break
            log_execution(f"Portfolio loop error: {traceback.format_exc()}", "ERROR")
            E._RUNTIME_SHUTDOWN.wait(max(2, BASE_SLEEP))


def safe_main_loop(dashboard_module=None):
    while not E.shutdown_requested():
        try:
            portfolio_loop(dashboard_module)
        except Exception:
            tb = traceback.format_exc()
            print(f"CRITICAL EXCEPTION: {tb}")
            try:
                log_execution(f"CRITICAL EXCEPTION: {tb}", "ERROR")
            except Exception:
                pass
            E._RUNTIME_SHUTDOWN.wait(5)


def keep_alive():
    interval = int(os.environ.get("KEEPALIVE_INTERVAL", "50"))
    url = os.environ.get("KEEPALIVE_URL", "").strip()
    while not E.shutdown_requested():
        try:
            if url:
                requests.get(url, timeout=5)
        except Exception as exc:
            try:
                log_execution(f"[KEEPALIVE] {exc}", "WARN")
            except Exception:
                pass
        E._RUNTIME_SHUTDOWN.wait(max(10, interval))


RUNTIME_EXPORTS = [
    "sync_position_with_exchange",
    "get_realized_pnl",
    "validate_position_state",
    "sync_all_states",
    "safe_main_loop",
    "keep_alive",
]
