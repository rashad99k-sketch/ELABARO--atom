"""Portfolio-level risk protections with per-symbol cooldown and global kill."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
import time
from collections import deque
from portfolio.protection_firewall import ProtectionFirewall


@dataclass
class RiskStatus:
    allowed: bool
    reason: str
    daily_drawdown_pct: float
    consecutive_losses: int
    cooldown_until: float
    projected_margin_pct: float


class PortfolioRiskGuard:
    def __init__(self, engine=None):
        self.engine = engine
        self.max_daily_loss_pct = float(os.getenv("MAX_DAILY_LOSS_PCT", "5.0"))
        self.max_consecutive_losses = max(1, int(os.getenv("MAX_CONSECUTIVE_LOSSES", "3")))
        self.cooldown_loss_sec = max(0, int(os.getenv("COOLDOWN_MINUTES_LOSS", "10"))) * 60
        self.cooldown_drawdown_sec = max(0, int(os.getenv("COOLDOWN_MINUTES_DRAWDOWN", "20"))) * 60
        self.position_margin_pct = float(os.getenv("POSITION_MARGIN_PCT", "0.10"))
        self.portfolio_margin_cap_pct = float(os.getenv("PORTFOLIO_MARGIN_CAP_PCT", "0.60"))
        self._day = None
        self._day_start_equity = None
        self._consecutive_losses = 0
        self._cooldown_until = 0.0
        self._last_seen_trade_count = 0
        # Per-symbol cooldown
        self._symbol_cooldown_until = {}
        self._ledger_pair_cooldown = os.getenv("ENABLE_PAIR_COOLDOWN_LEDGER", "0").strip().lower() in {"1", "true", "yes", "on"}
        # Store recent trade results for sync
        self._trade_results = deque(maxlen=20)
        self.protection = ProtectionFirewall(
            stoploss_limit=max(1, int(os.getenv("PROTECTION_STOPLOSS_LIMIT", "3"))),
            window_sec=max(60, int(os.getenv("PROTECTION_WINDOW_SEC", "3600"))),
            cooldown_sec=max(0, int(os.getenv("PROTECTION_COOLDOWN_SEC", "0"))),
            max_drawdown_pct=self.max_daily_loss_pct,
        )

    def _equity(self) -> float:
        try:
            if self.engine is not None:
                bal = max(0.0, float(self.engine.get_balance_safe()))
                # Committed margin is part of total equity, NOT a loss. Account
                # for it so that opening positions (which moves free balance
                # into committed margin) does not register as a daily drawdown.
                paper = getattr(self.engine, "paper", None)
                if isinstance(paper, dict):
                    bal += max(0.0, float(paper.get("committed_margin", 0.0)))
                return bal
        except Exception:
            pass
        return 0.0

    def _roll_day(self, equity: float) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._day:
            self._day = today
            self._day_start_equity = equity if equity > 0 else self._day_start_equity
            self._consecutive_losses = 0
            self._cooldown_until = 0.0

    def sync_trade_result(self, symbol: str, result: str, pnl_pct: float):
        """Called when a trade closes to update risk state."""
        self._trade_results.append({"symbol": symbol, "result": result, "pnl": pnl_pct})
        self.sync_closed_trades()

    def sync_closed_trades(self) -> None:
        """Process every unseen closed-trade ledger item exactly once.

        BARON now keeps an immutable PERF["closed_trades"] ledger.  This avoids
        the classic "last_trade" race where several positions close between
        risk polls and only the newest result is observed.
        """
        perf = getattr(self.engine, "PERF", {}) if self.engine is not None else {}
        ledger = perf.get("closed_trades") if isinstance(perf, dict) else None
        if isinstance(ledger, list):
            # Test/restart boundaries may reset the in-memory PERF counters
            # while leaving an older diagnostic ledger attached. Do not replay
            # historical trades into a fresh risk session.
            if int(perf.get("trades", 0) or 0) == 0 and ledger:
                self._last_seen_trade_count = len(ledger)
                return
            unseen = ledger[self._last_seen_trade_count:]
            if not unseen:
                return
            for item in unseen:
                result = str(item.get("result", "")).upper()
                symbol = item.get("symbol")
                pnl_pct = float(item.get("pnl_pct", 0.0) or 0.0)
                self.protection.record_close(symbol or "UNKNOWN", pnl_pct)
                if result == "LOSS":
                    self._consecutive_losses += 1
                    if symbol and self._ledger_pair_cooldown:
                        self._symbol_cooldown_until[symbol] = time.time() + self.cooldown_loss_sec
                    if self._consecutive_losses >= self.max_consecutive_losses:
                        self._cooldown_until = max(self._cooldown_until, time.time() + self.cooldown_drawdown_sec)
                elif result == "WIN":
                    self._consecutive_losses = 0
                    self._cooldown_until = 0.0
            self._last_seen_trade_count = len(ledger)
            return

        # Legacy fallback for old runtime state without a ledger.
        count = int(perf.get("trades", 0) or 0)
        if count <= self._last_seen_trade_count:
            return
        last = perf.get("last_trade") or {}
        result = str(last.get("result", "")).upper()
        pnl_pct = float(last.get("pnl_pct", 0.0) or 0.0)
        symbol = last.get("symbol")
        self.protection.record_close(symbol or "UNKNOWN", pnl_pct)
        if result == "LOSS":
            self._consecutive_losses += 1
            if symbol:
                self._symbol_cooldown_until[symbol] = time.time() + self.cooldown_loss_sec
            if self._consecutive_losses >= self.max_consecutive_losses:
                self._cooldown_until = max(self._cooldown_until, time.time() + self.cooldown_drawdown_sec)
        elif result == "WIN":
            self._consecutive_losses = 0
            self._cooldown_until = 0.0
        self._last_seen_trade_count = count

    def status(self, symbol: str | None = None, current_positions: int = 0, requested_margin_pct: float | None = None) -> RiskStatus:
        self.sync_closed_trades()
        equity = self._equity()
        self._roll_day(equity)
        start = self._day_start_equity or equity
        drawdown = max(0.0, ((start - equity) / start) * 100.0) if start > 0 else 0.0
        margin_pct = self.position_margin_pct if requested_margin_pct is None else float(requested_margin_pct)
        projected = (max(0, int(current_positions)) + 1) * margin_pct

        # Preserve BARON's established risk-reason precedence. The new
        # Freqtrade-inspired firewall is an additional gate, never a replacement
        # for the existing portfolio contract.
        if projected > self.portfolio_margin_cap_pct + 1e-9:
            return RiskStatus(False, "PORTFOLIO_MARGIN_CAP", drawdown, self._consecutive_losses, self._cooldown_until, projected)
        if drawdown >= self.max_daily_loss_pct:
            self.protection.set_global_drawdown(drawdown)
            return RiskStatus(False, "DAILY_DRAWDOWN_LIMIT", drawdown, self._consecutive_losses, self._cooldown_until, projected)
        if time.time() < self._cooldown_until:
            return RiskStatus(False, "GLOBAL_LOSS_COOLDOWN", drawdown, self._consecutive_losses, self._cooldown_until, projected)
        if symbol and symbol in self._symbol_cooldown_until:
            if time.time() < self._symbol_cooldown_until[symbol]:
                return RiskStatus(False, f"SYMBOL_COOLDOWN_{symbol}", drawdown, self._consecutive_losses, self._symbol_cooldown_until[symbol], projected)
        self.protection.set_global_drawdown(drawdown)
        protection = self.protection.check(symbol or "GLOBAL")
        if not protection.allowed:
            return RiskStatus(False, protection.reason, drawdown, self._consecutive_losses, protection.lock_until, projected)
        return RiskStatus(True, "OK", drawdown, self._consecutive_losses, self._cooldown_until, projected)

    def can_open(self, symbol: str | None = None, current_positions: int = 0, requested_margin_pct: float | None = None) -> bool:
        return self.status(symbol, current_positions, requested_margin_pct).allowed

    def snapshot(self, current_positions: int) -> dict:
        s = self.status(None, current_positions)
        return {
            "allowed": s.allowed,
            "reason": s.reason,
            "daily_drawdown_pct": round(s.daily_drawdown_pct, 3),
            "consecutive_losses": s.consecutive_losses,
            "cooldown_until": s.cooldown_until,
            "projected_margin_pct": round(s.projected_margin_pct, 4),
            "max_daily_loss_pct": self.max_daily_loss_pct,
            "portfolio_margin_cap_pct": self.portfolio_margin_cap_pct,
            "position_margin_pct": self.position_margin_pct,
        }