"""Runtime portfolio orchestration for multiple independent positions."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, Optional, Any, List
import copy
import os
import threading
import time

from portfolio.risk import PortfolioRiskGuard
from portfolio.trade_registry import TradeRegistry, TradeRecord
from core.trade_lifecycle import TradeLifecycleJournal


@dataclass
class PositionContext:
    symbol: str
    state: dict
    trade_state: dict
    live_manager: Any
    paper_position: Any = None
    opened_at: float = 0.0
    client_order_id: Optional[str] = None
    asset_class: Optional[str] = None
    ctx_key: Any = None


class PortfolioManager:
    def __init__(self, max_positions: int = 6, engine=None):
        self.max_positions = max(1, int(max_positions))
        self.max_technical_positions = max(1, min(self.max_positions, int(os.getenv("MAX_TECHNICAL_POSITIONS", "5"))))
        self.engine = engine
        self.contexts: Dict[str, PositionContext] = {}
        self.active_symbol: Optional[str] = None
        self._lock = threading.RLock()
        self._base_state = None
        self._base_trade_state = None
        self.risk_guard = PortfolioRiskGuard(engine)
        self.trade_registry = TradeRegistry()
        self._last_perf_trade_count = 0
        self.hedge_mode = False
        if engine is not None:
            self.bind(engine)

    def bind(self, engine):
        self.engine = engine
        self.risk_guard.engine = engine
        if self._base_state is None:
            self._base_state = copy.deepcopy(engine.STATE)
        if self._base_trade_state is None:
            self._base_trade_state = copy.deepcopy(engine.TRADE_STATE)

    def count(self) -> int:
        return len(self.contexts)

    def symbols(self):
        return list(self.contexts.keys())

    def _key_symbol(self, key) -> str:
        """Underlying exchange symbol of a context key (tuple keys are hedge
        (symbol, side) pairs; plain keys are symbol strings)."""
        return key[0] if isinstance(key, tuple) else key

    def _ctx_key(self, symbol: str, side: Optional[str] = None):
        """Context dict key. In hedge mode the same symbol may hold both a LONG
        and a SHORT position, so those must be distinct contexts keyed by
        (symbol, side). Without hedge mode the key stays the plain symbol."""
        if self.hedge_mode and side in ("BUY", "SELL"):
            return (symbol, side)
        return symbol

    def _has_context(self, symbol: str) -> bool:
        return any(self._key_symbol(k) == symbol for k in self.contexts)

    def _update_hedge_mode(self, positions: List[dict]) -> None:
        """Detect hedge usage from the venue truth: if the exchange reports both
        a LONG and a SHORT position for the same symbol, contexts for that
        account must be keyed by (symbol, side). Detection is per-account and
        done against the live position list, never guessed."""
        if self.hedge_mode or not positions:
            return
        seen = {}
        for p in positions or []:
            sym = str(p.get("symbol") or "")
            if not sym:
                continue
            side = str(p.get("side") or "").upper()
            if side not in ("BUY", "SELL"):
                continue
            seen.setdefault(sym, set()).add(side)
            if set(("BUY", "SELL")).issubset(seen[sym]):
                self.hedge_mode = True
                if self.engine is not None:
                    self.engine.log_execution(
                        f"[HEDGE] LONG+SHORT present on {sym}; managing contexts keyed by (symbol, side)", "WARN")
                return

    @staticmethod
    def _asset_class(symbol: str, explicit: str | None = None) -> str:
        if explicit:
            return str(explicit).upper()
        text = str(symbol or "").upper()
        # Canonical tradfi prefix classification (single source of truth:
        # scanner.universe.classify): NCSK->STOCK, NCSI->INDEX, NCCO->
        # GOLD/OIL/METAL/ENERGY. Only metadata-confidence prefix hits are routed
        # through the canonical classifier; every other symbol keeps the legacy
        # hint behaviour below so CRYPTO remains the safe fallback.
        if text.split("/")[0].startswith(("NCSI", "NCSK", "NCCO")):
            try:
                from scanner.universe import classify
                _ac, _src, _conf = classify(str(symbol or ""), {})
                if _src == "metadata" and _ac:
                    return _ac
            except Exception:
                pass
        if any(x in text for x in ("XAU", "GOLD")):
            return "GOLD"
        if any(x in text for x in ("WTI", "BRENT", "OIL", "CRUDE")):
            return "OIL"
        if any(x in text for x in ("SP500", "US500", "NASDAQ", "USTECH", "US30", "DAX", "FTSE", "CAC", "NIKKEI", "INDEX")):
            return "INDEX"
        stock_hints = {"AAPL", "AMZN", "GOOGL", "MSFT", "NVDA", "META", "TSLA", "JPM", "ARM", "INTC", "CRCL", "COIN", "PLTR"}
        base = text.replace(":USDT", "").replace("/USDT", "").replace("-USDT", "")
        if base in stock_hints:
            return "STOCK"
        return "CRYPTO"

    @staticmethod
    def _class_cap(cls: str) -> int:
        """Capacity of the technical bucket a class belongs to.

        Source of truth is portfolio.allocator.DEFAULT_CLASS_CAPS via the
        bucket mapping, so the allocator report and the open authority always
        agree:
        CRYPTO:2 / INDEX+STOCK combined bucket:2 / OIL+GOLD combined bucket:1,
        plus the independent NEWS:1 slot.
        The env master override MAX_POSITIONS_PER_ASSET_CLASS applies to EVERY
        class when explicitly set only (no 999 fake default).
        """
        # NEWS is a dedicated singleton slot by contract. It must remain capped
        # at one even when MAX_POSITIONS_PER_ASSET_CLASS is raised for technical
        # classes; otherwise a second headline trade could consume another slot.
        if str(cls).upper() == "NEWS":
            return 1
        env = os.getenv("MAX_POSITIONS_PER_ASSET_CLASS", "").strip()
        if env:
            return max(1, int(env))
        from portfolio.allocator import bucket_cap, bucket_of
        # Combined buckets are the single capacity unit (INDEX and STOCK share
        # one 2-seat bucket; OIL and GOLD share one 1-seat bucket). Classes
        # outside that model keep bucket cap 0 -- discovered but never opened.
        return bucket_cap(bucket_of(str(cls).upper()))

    def _ctx_class(self, pos) -> str:
        """Class of an open context: prefer the EXPLICIT class stored at OPEN
        (e.g. the NEWS slot on a symbol whose name would classify as CRYPTO),
        falling back to symbol derivation for legacy contexts."""
        stored = getattr(pos, "asset_class", None)
        return stored if stored else self._asset_class(pos.symbol)

    def can_open(self, symbol: str, asset_class: str | None = None) -> bool:
        with self._lock:
            if self._has_context(symbol) or len(self.contexts) >= self.max_positions:
                return False
            if not self.risk_guard.can_open(symbol, len(self.contexts)):
                return False
            cls = self._asset_class(symbol, asset_class)
            if cls == "NEWS":
                current_news = sum(1 for pos in self.contexts.values() if self._ctx_class(pos) == "NEWS")
                return current_news < 1
            technical_open = sum(1 for pos in self.contexts.values() if self._ctx_class(pos) != "NEWS")
            if technical_open >= self.max_technical_positions:
                return False
            # Capacity is counted per COMBINED bucket (INDEX+STOCK share a
            # 2-seat seat; OIL/GOLD share one seat) — exactly the bucket the
            # allocator enforces, so the two authorities never disagree.
            from portfolio.allocator import bucket_of
            bucket = bucket_of(cls)
            current = sum(1 for pos in self.contexts.values() if bucket_of(self._ctx_class(pos)) == bucket)
            return current < self._class_cap(cls)

    def _can_open_blocker(self, symbol: str, asset_class: str | None = None) -> str:
        """WHY can_open would refuse this candidate today. Mirrors the exact
        gate order of can_open and returns a traceable token (DUPLICATE /
        TOTAL_CAPACITY_FULL / RISK_REJECT / TECHNICAL_CAPACITY_FULL /
        NEWS_SLOT_FULL / <bucket>_CAPACITY_FULL). Never raises."""
        try:
            if self._has_context(symbol):
                return "DUPLICATE"
            if len(self.contexts) >= self.max_positions:
                return "TOTAL_CAPACITY_FULL"
            if not self.risk_guard.can_open(symbol, len(self.contexts)):
                return "RISK_REJECT"
            cls = self._asset_class(symbol, asset_class)
            if cls == "NEWS":
                current_news = sum(1 for pos in self.contexts.values() if self._ctx_class(pos) == "NEWS")
                return "NEWS_SLOT_FULL" if current_news >= 1 else ""
            technical_open = sum(1 for pos in self.contexts.values() if self._ctx_class(pos) != "NEWS")
            if technical_open >= self.max_technical_positions:
                return "TECHNICAL_CAPACITY_FULL"
            from portfolio.allocator import bucket_of
            bucket = bucket_of(cls)
            current = sum(1 for pos in self.contexts.values() if bucket_of(self._ctx_class(pos)) == bucket)
            if current >= self._class_cap(cls):
                return {
                    "CRYPTO": "CRYPTO_SLOT_FULL",
                    "INDEX_STOCK": "INDEX_STOCK_CAPACITY_FULL",
                    "COMMODITY": "COMMODITY_CAPACITY_FULL",
                    "NEWS": "NEWS_SLOT_FULL",
                }.get(bucket, f"{bucket}_CAPACITY_FULL")
            return ""
        except Exception:
            return "UNKNOWN"

    def _capture(self):
        if not self.engine or not self.active_symbol:
            return
        ctx = self.contexts.get(self.active_symbol)
        if ctx is None:
            return
        ctx.state = copy.deepcopy(self.engine.STATE)
        ctx.trade_state = copy.deepcopy(self.engine.TRADE_STATE)
        ctx.live_manager = self.engine._live_manager
        paper = getattr(self.engine, "paper", None)
        if isinstance(paper, dict):
            ctx.paper_position = copy.deepcopy(paper.get("position"))
        # Materialize the current management state so a restart does not lose
        # TP1/runner/peak/profit-lock information. This is persistence only;
        # the live manager remains the sole decision/execution authority.
        tid = str(ctx.state.get("trade_id") or "")
        if tid:
            try:
                self.trade_registry.update(
                    tid, status="OPEN" if ctx.state.get("open") else "CLOSED",
                    sl=float(ctx.state.get("synthetic_sl", 0.0) or 0.0),
                    tp1=float(ctx.state.get("synthetic_tp1", ctx.state.get("tp1_price", 0.0)) or 0.0),
                    tp2=float(ctx.state.get("synthetic_tp2", ctx.state.get("tp2_price", 0.0)) or 0.0),
                    tp1_done=bool(ctx.state.get("tp1_hit", False)),
                    runner=bool(ctx.state.get("runner_enabled", ctx.state.get("runner_on", False))),
                    peak_roe=float(ctx.state.get("peak_roe", 0.0) or 0.0),
                    peak_price=float(ctx.state.get("peak_price", 0.0) or 0.0),
                    profit_lock_roe=float(ctx.state.get("profit_lock_roe", 0.0) or 0.0),
                    realized_pnl=float(ctx.state.get("realized_pnl_usdt", 0.0) or 0.0),
                )
            except Exception as exc:
                self.engine.log_execution(f"[TRADE_REGISTRY] state sync failed: {exc}", "WARN")

    def _blank(self):
        if self._base_state is None:
            self._base_state = copy.deepcopy(getattr(self.engine, "STATE", {}) or {})
        if self._base_trade_state is None:
            self._base_trade_state = copy.deepcopy(getattr(self.engine, "TRADE_STATE", {}) or {})
        self.engine.STATE.clear()
        self.engine.STATE.update(copy.deepcopy(self._base_state or {}))
        self.engine.TRADE_STATE.clear()
        self.engine.TRADE_STATE.update(copy.deepcopy(self._base_trade_state or {}))
        paper = getattr(self.engine, "paper", None)
        if isinstance(paper, dict):
            paper["position"] = None

    def activate(self, symbol: Optional[str]):
        if not self.engine:
            raise RuntimeError("PortfolioManager is not bound to core.engine")
        with self._lock:
            self._capture()
            self.active_symbol = symbol
            if symbol is None:
                self._blank()
                return
            ctx = self.contexts.get(symbol)
            if ctx is None:
                self._blank()
                ctx_manager = self.engine.LiveTradeManager(
                    self.engine._event_bus,
                    self.engine._exchange_sync,
                    self.engine._recovery_guard,
                )
                self.engine._live_manager = ctx_manager
                return
            self.engine.STATE.clear()
            self.engine.STATE.update(copy.deepcopy(ctx.state))
            self.engine.TRADE_STATE.clear()
            self.engine.TRADE_STATE.update(copy.deepcopy(ctx.trade_state))
            self.engine._live_manager = ctx.live_manager
            paper = getattr(self.engine, "paper", None)
            if isinstance(paper, dict):
                paper["position"] = copy.deepcopy(ctx.paper_position)

    def deactivate(self):
        with self._lock:
            self._capture()
            self.active_symbol = None
            self._blank()

    def _store_after_open(self, symbol: str, manager, asset_class: Optional[str] = None, key=None):
        if asset_class is None:
            asset_class = self._asset_class(symbol)
        if key is None:
            key = symbol
        self.contexts[key] = PositionContext(
            symbol=symbol,
            state=copy.deepcopy(self.engine.STATE),
            trade_state=copy.deepcopy(self.engine.TRADE_STATE),
            live_manager=manager,
            paper_position=copy.deepcopy(
                self.engine.paper.get("position")
            ) if isinstance(getattr(self.engine, "paper", None), dict) else None,
            opened_at=time.time(),
            asset_class=asset_class,
            ctx_key=key,
        )

    def open_candidate(self, candidate: dict) -> bool:
        symbol = candidate["symbol"]
        if not candidate.get("trade_id"):
            candidate["trade_id"] = TradeLifecycleJournal.new_trade_id(symbol)
        trade_id = str(candidate["trade_id"])
        client_order_id = TradeRegistry.client_order_id(trade_id, "OPEN", "MAIN")
        if not self.can_open(symbol, candidate.get("asset_class")):
            # Do not materialize rejected capacity/risk intents as active trade
            # records. This prevents restart recovery from mistaking a rejected
            # candidate for a live position.
            if self.engine is not None:
                try:
                    blocker = self._can_open_blocker(symbol, candidate.get("asset_class"))
                    if blocker:
                        _lc = self.engine.MEMORY.setdefault("opportunity_lifecycle", {}).setdefault(symbol, {})
                        _lc["primary_blocker"] = blocker
                except Exception:
                    pass
            return False
        self.trade_registry.upsert(TradeRecord(
            trade_id=trade_id, symbol=symbol, side=str(candidate.get("side", "BUY")).upper(),
            client_order_id=client_order_id, status="INTENT",
            qty=float(candidate.get("qty", 0.0) or 0.0), entry=float(candidate.get("price", 0.0) or 0.0),
            sl=float(candidate.get("sl", 0.0) or 0.0), tp1=float(candidate.get("tp1", 0.0) or 0.0),
            tp2=float(candidate.get("tp2", 0.0) or 0.0), metadata={"asset_class": candidate.get("asset_class"), "source": candidate.get("reason", [])}
        ))
        self.activate(symbol)
        try:
            # Trade type / classification come from the source that produced the
            # candidate (e.g. the NEWS slot -> trade_type="NEWS"). Technical
            # candidates do not set these, so they keep the legacy defaults
            # (INSTITUTIONAL / SNIPER) exactly as before. This is the point that
            # preserves trade_type=NEWS from creation through management.
            cand_trade_type = candidate.get("trade_type") or "INSTITUTIONAL"
            cand_classification = candidate.get("classification") or "SNIPER"
            _exec_args = (
                candidate["side"], symbol, candidate["price"],
                candidate["sl"], candidate["tp1"], candidate["tp2"],
                candidate["score"],
                f"PORTFOLIO:{candidate.get('trade_id','UNKNOWN')}",
                candidate["atr"], cand_trade_type, "PORTFOLIO_MANAGER", cand_classification
            )
            _context = {
                "trade_id": candidate.get("trade_id"),
                "location": candidate.get("location"),
                "zone_info": candidate.get("zone_info") or candidate.get("zone"),
                "narrative_classification": candidate.get("narrative_classification"),
                "narrative_confidence": candidate.get("narrative_confidence", 0.0),
                "confidence_level": candidate.get("confidence_level"),
                "institutional_stage": candidate.get("institutional_stage"),
                "move_maturity": candidate.get("move_maturity", "UNKNOWN"),
                "early_formation": candidate.get("early_formation", {}),
                "zone": candidate.get("zone", {}),
                "zone_low": float(candidate.get("zone_low", 0.0) or 0.0),
                "zone_high": float(candidate.get("zone_high", 0.0) or 0.0),
                "ob_grade": candidate.get("ob_grade", "NONE"),
                "vpa": candidate.get("vpa", (candidate.get("evidence", {}) or {}).get("vpa", {})),
                "execution_context": candidate.get("execution_context") or {},
                "news": candidate.get("news", {}),
                "news_reaction": candidate.get("news_reaction", {}),
                "reason": candidate.get("reason", []),
            }
            try:
                import inspect
                params = inspect.signature(self.engine.execute_entry).parameters
                if "context" in params:
                    ok = bool(self.engine.execute_entry(*_exec_args, context=_context))
                else:
                    ok = bool(self.engine.execute_entry(*_exec_args))
            except (TypeError, ValueError):
                # Test/legacy engine seams may expose the pre-context signature.
                ok = bool(self.engine.execute_entry(*_exec_args))
            if ok and self.engine.STATE.get("open"):
                self._store_after_open(symbol, self.engine._live_manager, candidate.get("asset_class"))
                self.trade_registry.update(
                    trade_id, status="OPEN", qty=float(self.engine.STATE.get("qty_initial", self.engine.STATE.get("qty", 0.0)) or 0.0),
                    entry=float(self.engine.STATE.get("entry", candidate.get("price", 0.0)) or 0.0),
                    sl=float(self.engine.STATE.get("synthetic_sl", candidate.get("sl", 0.0)) or 0.0),
                    tp1=float(self.engine.STATE.get("synthetic_tp1", candidate.get("tp1", 0.0)) or 0.0),
                    tp2=float(self.engine.STATE.get("synthetic_tp2", candidate.get("tp2", 0.0)) or 0.0),
                    opened_at=time.time(),
                )
                if cand_trade_type == "NEWS":
                    self._log_news_open(symbol, candidate)
                else:
                    self.engine.log_execution(
                        f"[PORTFOLIO] Opened {symbol} {candidate['side']} | "
                        f"slot {len(self.contexts)}/{self.max_positions}",
                        "SUCCESS",
                    )
                return True
            self.trade_registry.update(trade_id, status="REJECTED", closed_at=time.time())
            return False
        finally:
            self.deactivate()

    def _log_news_open(self, symbol: str, candidate: dict) -> None:
        """Emit the dedicated, unambiguous NEWS open block so the runtime log
        shows the trade was created as NEWS (trade_type + independent slot +
        impact + direction) from the very first moment, before management."""
        side = str(candidate.get("side", "BUY"))
        try:
            # Keep both the machine-readable uppercase contract and the
            # human-readable NEWS block.  This is intentionally redundant:
            # downstream log parsers use the uppercase fields while operators
            # can still scan the compact NEWS line.
            self.engine.log_execution(
                f"[NEWS] {symbol} TRADE_TYPE=NEWS REGIME=NEWS_DRIVEN "
                f"SLOT=NEWS IMPACT={candidate.get('impact', 'MEDIUM')} "
                f"DIRECTION={'LONG' if side == 'BUY' else 'SHORT'} "
                f"trade_type=NEWS slot=NEWS "
                f"impact={candidate.get('impact', 'MEDIUM')} "
                f"direction={'LONG' if side == 'BUY' else 'SHORT'}",
                "SUCCESS",
            )
        except Exception as exc:
            self.engine.log_execution(f"[NEWS] {symbol} open log error: {exc}", "WARN")

    def open_top(self, candidates: List[dict], slots: Optional[int] = None) -> int:
        opened = 0
        target = self.max_positions - self.count() if slots is None else min(
            int(slots), self.max_positions - self.count()
        )
        if target <= 0:
            return 0
        for candidate in candidates:
            if opened >= target:
                break
            if not self.can_open(candidate["symbol"], candidate.get("asset_class")):
                continue
            if self.open_candidate(candidate):
                opened += 1
        return opened

    def recover_from_exchange(self) -> int:
        """Adopt exchange positions after process restart without inventing history.

        Only positions positively returned by the exchange are adopted.  API
        errors never become a synthetic CLOSED state.  Conservative defaults are
        used for missing historical TP/SL metadata; native/synthetic protection
        is then re-established by the management layer.

        This is management coverage only: adoption deliberately bypasses the
        entry gate.  A position that positively exists on the venue is managed,
        regardless of class caps / risk firewall / opening capacity - those
        limits still gate NEW entries via can_open() on the open_candidate path.
        """
        if not self.engine or self.engine.PAPER_MODE:
            return 0
        try:
            raw = self.engine._exchange_sync.fetch_all_open_positions()
        except Exception as exc:
            self.engine.log_execution(f"[RECOVERY] exchange position enumeration failed: {exc}", "ERROR")
            return 0
        self._update_hedge_mode(raw or [])
        recovered = 0
        for item in raw or []:
            symbol = str(item.get("symbol") or "")
            qty = float(item.get("contracts", 0) or 0)
            entry = float(item.get("entryPrice", 0) or 0)
            if not symbol or qty <= 0 or entry <= 0:
                continue
            side = str(item.get("side", "BUY") or "BUY").upper()
            if self._ctx_key(symbol, side) in self.contexts:
                continue
            persisted = None
            try:
                matches = [r for r in self.trade_registry.active() if str(r.symbol) == str(symbol)]
                if matches:
                    persisted = max(matches, key=lambda r: float(r.updated_at if hasattr(r, "updated_at") else r.opened_at or 0.0))
            except Exception:
                persisted = None
            if self._adopt_position(item, persisted):
                recovered += 1
        return recovered

    def adopt_unmanaged_from_exchange(self, throttle_sec: float = 30.0) -> int:
        """Periodic account-wide adoption inside the live portfolio loop.

        Any position that exists on the venue but is not yet a managed context
        is adopted into the SAME LiveTradeManager and SAME management path -
        never a second/parallel execution authority.  Enables manual positions
        placed while the process is running to be discovered and managed without
        a restart, and without any entry strategy involvement.
        """
        if not self.engine or self.engine.PAPER_MODE:
            return 0
        now = time.time()
        if now - getattr(self, "_last_adopt_scan", 0.0) < throttle_sec:
            return 0
        self._last_adopt_scan = now
        try:
            raw = self.engine._exchange_sync.fetch_all_open_positions()
        except Exception as exc:
            self.engine.log_execution(f"[PORTFOLIO] adoption scan failed: {exc}", "WARN")
            return 0
        self._update_hedge_mode(raw or [])
        adopted = 0
        for item in raw or []:
            symbol = str(item.get("symbol") or "")
            qty = float(item.get("contracts", 0) or 0)
            if not symbol or qty <= 0:
                continue
            side = str(item.get("side", "BUY") or "BUY").upper()
            if self._ctx_key(symbol, side) in self.contexts:
                continue
            if self._adopt_position(item):
                adopted += 1
        if adopted:
            self.engine.log_execution(
                f"[PORTFOLIO] Adopted {adopted} unmanaged exchange position(s) into managed portfolio", "SUCCESS")
        return adopted

    def _adopt_position(self, item: dict, persisted=None) -> bool:
        """Adopt ONE existing exchange position into portfolio management.

        Management coverage, not entry: no class cap / risk firewall / opening
        capacity decision happens here.  Equivalent exchange truth (contracts,
        entry, side, symbol) becomes the context; SL/TP fall back to conservative
        defaults only when neither the venue nor a durable trade record supplies
        them.  Returns True only when a NEW context was stored.
        """
        symbol = str(item.get("symbol") or "")
        qty = float(item.get("contracts", 0) or 0)
        entry = float(item.get("entryPrice", 0) or 0)
        side = str(item.get("side", "BUY") or "BUY").upper()
        if side not in ("BUY", "SELL"):
            side = "BUY"
        if not symbol or qty <= 0 or entry <= 0:
            return False
        key = self._ctx_key(symbol, side)
        if key in self.contexts:
            return False
        self.activate(key)
        try:
            # Restart recovery must restore the durable trade thesis first.
            # Recomputing SL/TP from *current* ATR can silently mutate a live
            # trade after restart and erase TP1/runner/profit-lock history.
            # Only synthesize conservative levels when no durable record exists.
            atr = entry * 0.01
            if persisted is not None and float(persisted.entry or 0) > 0:
                sl = float(persisted.sl or 0.0)
                tp1 = float(persisted.tp1 or 0.0)
                tp2 = float(persisted.tp2 or 0.0)
                trade_id = str(persisted.trade_id)
                if sl <= 0 or tp1 <= 0 or tp2 <= 0:
                    # Durable record exists but protection is incomplete: obtain
                    # venue protection if possible; do not invent from live ATR.
                    sl = float(item.get("stopLossPrice", 0) or item.get("stopLoss", 0) or 0)
                    tp1 = float(item.get("takeProfitPrice", 0) or item.get("takeProfit", 0) or 0)
                    tp2 = 0.0
                if sl <= 0 or tp1 <= 0:
                    self.engine.log_execution(
                        f"[RECOVERY] {symbol} durable trade {trade_id} missing protection; refusing ATR recompute",
                        "WARN")
            else:
                sl = float(item.get("stopLossPrice", 0) or item.get("stopLoss", 0) or 0)
                tp1 = float(item.get("takeProfitPrice", 0) or item.get("takeProfit", 0) or 0)
                tp2 = float(item.get("takeProfit2Price", 0) or 0)
                if sl <= 0 or tp1 <= 0:
                    sl = entry - atr * 1.6 if side == "BUY" else entry + atr * 1.6
                    tp1 = entry + atr * 1.5 if side == "BUY" else entry - atr * 1.5
                    tp2 = entry + atr * 2.5 if side == "BUY" else entry - atr * 2.5
                    self.engine.log_execution(
                        f"[RECOVERY] {symbol} no durable trade record; using conservative fallback protection",
                        "WARN")
                trade_id = f"REC-{str(symbol).replace('/','_').replace(':','_')}-{side}-{int(time.time()*1000)}"
            self.engine.STATE.update({
                "open": True, "current_symbol": symbol, "symbol": symbol, "side": side,
                "entry": entry, "qty": qty, "remaining_qty": qty, "qty_initial": qty,
                "mark_price": float(item.get("markPrice", entry) or entry),
                "entry_time": float(getattr(persisted, "opened_at", 0.0) or time.time()) if persisted is not None else time.time(),
                "recovered": True, "atr": atr, "entry_atr": atr,
                "sl": sl, "dynamic_tp1": tp1, "dynamic_tp2": tp2, "synthetic_sl": sl,
                "synthetic_tp1": tp1, "synthetic_tp2": tp2, "tp1_price": tp1, "tp2_price": tp2,
                "trade_id": trade_id,
                "tp1_hit": bool(getattr(persisted, "tp1_done", False)) if persisted is not None else False,
                "runner_enabled": bool(getattr(persisted, "runner", False)) if persisted is not None else False,
                "peak_roe": float(getattr(persisted, "peak_roe", 0.0) or 0.0) if persisted is not None else 0.0,
                "peak_price": float(getattr(persisted, "peak_price", 0.0) or 0.0) if persisted is not None else 0.0,
                "profit_lock_roe": float(getattr(persisted, "profit_lock_roe", 0.0) or 0.0) if persisted is not None else 0.0,
                "realized_pnl_usdt": float(getattr(persisted, "realized_pnl", 0.0) or 0.0) if persisted is not None else 0.0,
            })
            self.engine.TRADE_STATE.update({"in_position": True, "symbol": symbol, "side": side, "entry": entry, "qty": qty, "last_update_ts": time.time()})
            self.engine._live_manager.start_trade(symbol, side, entry, qty, sl, tp1, tp2, self.engine.STATE["trade_id"])
            self._store_after_open(symbol, self.engine._live_manager, None, key=key)
            self.engine.log_execution(f"[RECOVERY] Adopted {symbol} {side} qty={qty:.6f} entry={entry:.6f} trade_id={self.engine.STATE['trade_id']}", "SUCCESS")
            return True
        finally:
            self.deactivate()

    def manage_all(self):
        if not self.engine:
            return
        for key in list(self.contexts.keys()):
            symbol = self._key_symbol(key)
            self.activate(key)
            try:
                if not self.engine.STATE.get("open"):
                    ctx = self.contexts.pop(key, None)
                    if ctx is not None and callable(getattr(ctx.live_manager, "dispose", None)):
                        ctx.live_manager.dispose()
                    continue
                self.engine.sync_position_state(symbol)
                if self.engine.STATE.get("open"):
                    self.engine._live_manager.manage_live_trade()
                # Exit decisions belong exclusively to LiveTradeManager.
                # council_exit is a legacy signal provider and must not be
                # invoked here as a second execution authority; doing so can
                # race the Brain/PPE path and produce duplicate or unverified
                # closes. Emergency/SL handling is already enforced inside the
                # manager's verified execution path.
                if not self.engine.STATE.get("open"):
                    ctx = self.contexts.pop(key, None)
                    if ctx is not None and callable(getattr(ctx.live_manager, "dispose", None)):
                        ctx.live_manager.dispose()
                else:
                    self._capture()
            except Exception as exc:
                self.engine.log_execution(f"[PORTFOLIO] manage {symbol}: {exc}", "ERROR")
            finally:
                self.risk_guard.sync_closed_trades()
                self.engine.MEMORY["portfolio_risk"] = self.risk_guard.snapshot(self.count())
                self.deactivate()

        # Closed-trade materialization is deliberately batched AFTER the live
        # management loop. This keeps persistence I/O completely out of the
        # per-symbol execution path and cannot delay/alter another symbol's
        # management tick.
        try:
            ledger = getattr(self.engine, "PERF", {}).get("closed_trades", [])
            for item in ledger[-64:] if isinstance(ledger, list) else []:
                tid = str(item.get("trade_id") or "")
                if tid:
                    self.trade_registry.update(
                        tid, status="CLOSED", closed_at=float(item.get("closed_at", time.time()) or time.time()),
                        realized_pnl=float(item.get("pnl_usdt", 0.0) or 0.0)
                    )
        except Exception as exc:
            self.engine.log_execution(f"[TRADE_REGISTRY] closed-ledger sync failed: {exc}", "WARN")

    def restore_from_exchange(self):
        if not self.engine:
            return
        try:
            positions = self.engine._exchange_sync.fetch_all_open_positions()
            for pos in positions:
                symbol = pos.get('symbol')
                if not symbol or self._has_context(symbol):
                    continue
                # Create a new context from the position data
                self.activate(symbol)
                # The engine state is blank; set it from position data
                self.engine.STATE["open"] = True
                self.engine.STATE["side"] = pos.get('side', 'BUY')
                self.engine.STATE["entry"] = float(pos.get('entryPrice', 0))
                self.engine.STATE["qty"] = float(pos.get('contracts', 0))
                self.engine.STATE["remaining_qty"] = float(pos.get('contracts', 0))
                self.engine.STATE["mark_price"] = float(pos.get('markPrice', 0))
                self.engine.STATE["current_symbol"] = symbol
                self.engine.STATE["entry_time"] = time.time()
                tid = str(self.engine.STATE.get("trade_id") or "")
                rec = self.trade_registry.get(tid) if tid else None
                if rec is not None:
                    # Restart recovery preserves the last known management state.
                    self.engine.STATE["synthetic_sl"] = rec.sl
                    self.engine.STATE["synthetic_tp1"] = rec.tp1
                    self.engine.STATE["synthetic_tp2"] = rec.tp2
                    self.engine.STATE["tp1_price"] = rec.tp1
                    self.engine.STATE["tp2_price"] = rec.tp2
                    self.engine.STATE["tp1_hit"] = rec.tp1_done
                    self.engine.STATE["peak_roe"] = rec.peak_roe
                    self.engine.STATE["peak_price"] = rec.peak_price
                    self.engine.STATE["profit_lock_roe"] = rec.profit_lock_roe
                    self.engine.STATE["recovery_source"] = "TRADE_REGISTRY"
                    atr = float(self.engine.STATE.get("entry_atr", 0.0) or 0.0)
                    df = self.engine.get_ohlcv_safe(symbol, 50)
                else:
                    # Legacy exchange-only recovery: keep the fallback explicit.
                    df = self.engine.get_ohlcv_safe(symbol, 50)
                    if df is not None and len(df) > 14:
                        atr = self.engine.compute_atr(df).iloc[-1]
                    else:
                        atr = self.engine.STATE["entry"] * 0.02
                    self.engine.STATE["entry_atr"] = atr
                    sl, tp1, tp2 = self.engine.compute_sl_tp(
                        self.engine.STATE["entry"], self.engine.STATE["side"], "REVERSAL", atr, df
                    )
                    self.engine.STATE["synthetic_sl"] = sl
                    self.engine.STATE["synthetic_tp1"] = tp1
                    self.engine.STATE["tp2_price"] = tp2
                    self.engine.STATE["recovery_source"] = "EXCHANGE_FALLBACK_MISSING_REGISTRY"
                self.engine._live_manager = self.engine.LiveTradeManager(
                    self.engine._event_bus,
                    self.engine._exchange_sync,
                    self.engine._recovery_guard
                )
                self.engine._live_manager.set_entry_atr(atr)
                self._store_after_open(symbol, self.engine._live_manager)
                self.engine.log_execution(f"[RECOVERY] Restored position for {symbol}", "INFO")
                self.deactivate()
        except Exception as e:
            self.engine.log_execution(f"[RECOVERY] Error: {e}", "ERROR")

    def risk_snapshot(self):
        # Integration dependency: core/runtime health payload and the security
        # controls test consume the portfolio risk view through this method.
        return self.risk_guard.snapshot(self.count())

    def close_symbol(self, symbol: str, side: Optional[str] = None) -> bool:
        keys = [k for k in self.contexts if self._key_symbol(k) == symbol]
        if self.hedge_mode and side in ("BUY", "SELL"):
            wanted = (symbol, side)
            keys = [k for k in keys if k == wanted]
        if not keys:
            return False
        closed = False
        for key in keys:
            self.activate(key)
            try:
                if self.engine.STATE.get("open"):
                    self.engine.close_position_full()
                done = not self.engine.STATE.get("open")
                if done:
                    ctx = self.contexts.pop(key, None)
                    if ctx is not None and callable(getattr(ctx.live_manager, "dispose", None)):
                        ctx.live_manager.dispose()
                else:
                    self._capture()
                closed = closed or done
            finally:
                self.deactivate()
        return closed

    def shutdown(self) -> None:
        """Detach all per-position managers from the shared engine event bus.

        Useful for deterministic teardown/restart and prevents stale managers
        from acting on a later position that happens to use the same symbol.
        """
        for ctx in list(self.contexts.values()):
            manager = getattr(ctx, "live_manager", None)
            dispose = getattr(manager, "dispose", None)
            if callable(dispose):
                dispose()
        self.contexts.clear()
        self.active_symbol = None
        if self.engine is not None:
            try:
                self._blank()
            except Exception:
                pass

    def snapshot(self):
        out = []
        for key in list(self.contexts.keys()):
            ctx = self.contexts[key]
            symbol = self._key_symbol(key)
            payload = canonical_position_payload(symbol, ctx.state, self._ctx_class(ctx))
            out.append(payload)
        return out


def canonical_position_payload(symbol: str, s: dict, asset_class: Optional[str] = None):
    """P1 canonical portfolio-position payload. Every documented key is always
    present; genuinely unknown values are None/0.0/False -- never the string
    'undefined' and never fake placeholders."""
    entry = float(s.get("entry", 0.0) or 0.0)
    tp1 = float(s.get("tp1_price", 0.0) or 0.0)
    if tp1 <= 0:
        tp1 = float(s.get("synthetic_tp1", 0.0) or 0.0)
    if tp1 <= 0:
        tp1 = float(s.get("dynamic_tp1", 0.0) or 0.0)
    tp2 = float(s.get("tp2_price", 0.0) or 0.0)
    if tp2 <= 0:
        tp2 = float(s.get("synthetic_tp2", 0.0) or 0.0)
    intel = s.get("trade_intelligence") if isinstance(s.get("trade_intelligence"), dict) else {}
    narrative = intel.get("narrative") or s.get("narrative_classification") or None
    session = s.get("market_session")
    if session is None:
        session = {}
    if isinstance(session, dict):
        label = s.get("session_label") or session.get("label") or session.get("current")
    else:
        label = s.get("session_label")
    mark = float(s.get("mark_price", 0.0) or 0.0)
    side = str(s.get("side") or "BUY").upper()
    pnl_usdt = float(s.get("unrealized_pnl_usdt", 0.0) or 0.0)
    margin = float(s.get("margin", 0.0) or 0.0)
    roi_pct = (pnl_usdt / margin * 100.0) if margin > 0 else float(s.get("roe_pct", 0.0) or 0.0)
    remaining = float(s.get("remaining_qty", 0.0) or 0.0)
    qty = float(s.get("qty_initial", s.get("qty", 0.0)) or 0.0)
    price_move_pct = (((mark-entry)/entry*100.0) if side == "BUY" else ((entry-mark)/entry*100.0)) if entry > 0 and mark > 0 else 0.0
    def _progress(target):
        target = float(target or 0.0)
        denom = abs(target-entry)
        if entry <= 0 or target <= 0 or denom <= 0:
            return 0.0
        return max(0.0, min(100.0, abs(mark-entry)/denom*100.0))
    tp1_hit = bool(s.get("tp1_hit", False))
    tp2_hit = bool(s.get("tp2_hit", False))
    if tp2_hit or remaining <= 0:
        profit_stage = "TP2_COMPLETE"
    elif tp1_hit:
        profit_stage = "RUNNER"
    else:
        profit_stage = "PRE_TP1"
    mom = s.get("momentum_flow") if isinstance(s.get("momentum_flow"), dict) else {}
    sm = s.get("smart_money") if isinstance(s.get("smart_money"), dict) else {}
    cont = float(s.get("continuation_probability", 0.5) or 0.5)
    mom_health = float(mom.get("momentum_health", 50.0) or 50.0)
    dist = float(sm.get("distribution_risk", 0.0) or 0.0)
    runner_health = max(0.0, min(100.0, cont*100.0*0.45 + mom_health*0.35 + (100.0-dist)*0.20))
    action = s.get("position_action") or s.get("management_action") or ("RUNNER" if tp1_hit else "WAIT_TP1")
    reason = s.get("management_reason") or s.get("position_reason") or s.get("profit_protection_reason")
    news_ctx = s.get("advisory_news_ctx") if isinstance(s.get("advisory_news_ctx"), dict) else {}
    return {
        "symbol": symbol,
        "asset_class": asset_class or PortfolioManager._asset_class(symbol),
        "side": s.get("side"),
        "entry": round(entry, 6),
        "mark_price": float(s.get("mark_price", 0.0) or 0.0),
        "current_price": float(s.get("mark_price", 0.0) or 0.0),
        "qty": float(s.get("qty", 0.0) or 0.0),
        "remaining_qty": float(s.get("remaining_qty", 0.0) or 0.0),
        "pnl": float(s.get("unrealized_pnl_usdt", 0.0) or 0.0),
        "roe": float(s.get("roe_pct", 0.0) or 0.0),
        "roe_pct": float(s.get("roe_pct", 0.0) or 0.0),
        "pnl_usdt": pnl_usdt,
        "roi_pct": roi_pct,
        "price_move_pct": price_move_pct,
        "initial_margin": margin,
        "profit_stage": profit_stage,
        "tp1_progress_pct": _progress(tp1),
        "tp2_progress_pct": _progress(tp2),
        "tp1_close_pct": 50.0,
        "tp2_close_pct": 50.0,
        "remaining_pct": (remaining / qty * 100.0) if qty > 0 else 0.0,
        "runner_active": bool(s.get("runner_mode", False)),
        "runner_health": runner_health,
        "management_action": action,
        "management_reason": reason,
        "profit_protection": bool(s.get("profit_lock_activated", False) or s.get("be_ratchet_active", False)),
        "news_context": news_ctx,
        "profit_execution": copy.deepcopy(s.get("profit_execution")) if isinstance(s.get("profit_execution"), dict) else None,
        "sl": float(s.get("synthetic_sl", s.get("sl", 0.0)) or 0.0),
        "tp1": tp1,
        "tp2": tp2,
        "tp1_done": bool(s.get("tp1_hit", False)),
        "tp1_hit": bool(s.get("tp1_hit", False)),
        "tp2_hit": bool(s.get("tp2_hit", False)),
        "trailing_active": bool(s.get("trail_activated", False)),
        "trail_stop": float(s.get("trail_stop", 0.0) or 0.0),
        "trail_multiplier": float(s.get("smart_trail_mult", 1.5) or 1.5),
        "delay_tp1": bool(s.get("delay_tp1", False)),
        "location": s.get("location"),
        "zone": s.get("zone_info"),
        "zone_behaviour": s.get("zone_behaviour"),
        "narrative": narrative,
        "narrative_classification": s.get("narrative_classification"),
        "narrative_confidence": float(s.get("narrative_confidence", 0.0) or 0.0),
        "confidence": s.get("current_confidence"),
        "confidence_level": s.get("confidence_level"),
        "current_confidence": float(s.get("current_confidence", 0.0) or 0.0),
        "regime": s.get("market_regime"),
        "market_regime": s.get("market_regime"),
        "trade_state": s.get("trade_state"),
        "state": None,
        "market_phase": s.get("market_phase"),
        "entry_timing": s.get("entry_timing"),
        "classification": s.get("classification"),
        "trade_type": s.get("trade_type"),
        "entry_type": s.get("entry_type"),
        "trade_style": s.get("trade_style"),
        "score": s.get("trade_score", 0),
        "continuation_pressure": s.get("continuation_pressure", 50),
        "board": s.get("trade_board") if isinstance(s.get("trade_board"), dict) else None,
        "trade_board": s.get("trade_board") if isinstance(s.get("trade_board"), dict) else None,
        "market_session": session if isinstance(session, dict) else {},
        "session_label": label,
        "dynamic_tp1": float(s.get("dynamic_tp1", 0.0) or 0.0),
        "dynamic_tp2": float(s.get("dynamic_tp2", 0.0) or 0.0),
        "entry_atr": float(s.get("entry_atr", 0.0) or 0.0),
        "last_update_ts": s.get("last_update_ts") or s.get("entry_time"),
    }