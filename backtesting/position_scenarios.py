from __future__ import annotations
from dataclasses import dataclass, field
from typing import Callable, Iterable, Any

@dataclass
class ScenarioResult:
    name: str
    prices: list[float]
    actions: list[str] = field(default_factory=list)
    final_price: float = 0.0
    realized_roe: float = 0.0
    max_roe: float = 0.0
    max_drawdown_roe: float = 0.0
    closed: bool = False
    close_index: int | None = None
    close_reason: str = ""

class ScenarioPositionBacktester:
    """Lightweight event replay harness for the existing management authority.

    The harness deliberately accepts a decision callback instead of importing
    BARON strategy rules. That lets us validate profit-lock/trailing/strict-close
    behavior without changing live entry logic or inventing backtest assumptions.
    """
    def __init__(self, leverage: float = 10.0):
        self.leverage = float(leverage)

    def run(self, name: str, roe_path: Iterable[float], decide: Callable[[dict], Any], side: str = "BUY") -> ScenarioResult:
        roes = [float(x) for x in roe_path]
        result = ScenarioResult(name=name, prices=roes)
        peak = float("-inf")
        trough = float("inf")
        for idx, roe in enumerate(roes):
            peak = max(peak, roe)
            trough = min(trough, roe)
            result.max_roe = max(result.max_roe, roe)
            result.max_drawdown_roe = max(result.max_drawdown_roe, peak - roe)
            state = {"roe_pct": roe, "peak_roe_pct": peak, "drawdown_roe_pct": peak - roe,
                     "index": idx, "side": side, "leverage": self.leverage}
            action = decide(state)
            if isinstance(action, dict):
                label = str(action.get("action", "HOLD"))
            else:
                label = str(action or "HOLD")
            result.actions.append(label)
            if label in {"STRICT_CLOSE", "SCALP_EXIT", "PROFIT_LOCK", "CLOSE", "EXIT"}:
                result.closed = True
                result.close_index = idx
                result.close_reason = label
                result.realized_roe = roe
                break
        if not result.closed and roes:
            result.final_price = roes[-1]
            result.realized_roe = roes[-1]
        elif result.closed:
            result.final_price = roes[result.close_index]
        return result
