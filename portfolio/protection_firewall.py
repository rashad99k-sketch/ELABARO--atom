"""Freqtrade-inspired portfolio protections, adapted for BARON.

Protections are risk gates only. They cannot create entries or close trades.
They support global and per-symbol locks and are deterministic/testable.
"""
from __future__ import annotations
from dataclasses import dataclass
import time
from collections import deque

@dataclass(frozen=True)
class ProtectionDecision:
    allowed: bool
    reason: str
    lock_until: float = 0.0
    scope: str = "NONE"

class ProtectionFirewall:
    def __init__(self, *, stoploss_limit: int = 3, window_sec: int = 3600,
                 cooldown_sec: int = 600, max_drawdown_pct: float = 5.0):
        self.stoploss_limit = max(1, int(stoploss_limit))
        self.window_sec = max(60, int(window_sec))
        self.cooldown_sec = max(0, int(cooldown_sec))
        self.max_drawdown_pct = max(0.0, float(max_drawdown_pct))
        self._losses = deque(maxlen=256)
        self._locks = {}
        self._global_lock = 0.0

    def record_close(self, symbol: str, pnl_pct: float, *, now: float | None = None):
        now = float(now if now is not None else time.time())
        if float(pnl_pct) < 0:
            self._losses.append((now, str(symbol)))
            if self.cooldown_sec:
                self._locks[str(symbol)] = max(self._locks.get(str(symbol), 0.0), now + self.cooldown_sec)
        self._prune(now)

    def set_global_drawdown(self, drawdown_pct: float, *, now: float | None = None):
        now = float(now if now is not None else time.time())
        if float(drawdown_pct) >= self.max_drawdown_pct > 0:
            self._global_lock = max(self._global_lock, now + self.cooldown_sec)

    def _prune(self, now):
        cutoff = now - self.window_sec
        while self._losses and self._losses[0][0] < cutoff:
            self._losses.popleft()
        for sym, until in list(self._locks.items()):
            if until <= now:
                self._locks.pop(sym, None)

    def check(self, symbol: str, *, now: float | None = None) -> ProtectionDecision:
        now = float(now if now is not None else time.time())
        self._prune(now)
        if now < self._global_lock:
            return ProtectionDecision(False, "GLOBAL_PROTECTION_LOCK", self._global_lock, "GLOBAL")
        until = self._locks.get(str(symbol), 0.0)
        if now < until:
            return ProtectionDecision(False, f"PAIR_COOLDOWN:{symbol}", until, "SYMBOL")
        recent_losses = len(self._losses)
        if recent_losses >= self.stoploss_limit:
            self._global_lock = now + self.cooldown_sec
            return ProtectionDecision(False, "STOPLOSS_GUARD", self._global_lock, "GLOBAL")
        return ProtectionDecision(True, "OK", 0.0, "NONE")
