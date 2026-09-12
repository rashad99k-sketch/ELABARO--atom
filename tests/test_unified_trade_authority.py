import unittest
from pathlib import Path

import pandas as pd

import core.engine as E

ROOT = Path(__file__).resolve().parents[1]


def _df(n=40):
    x = [100 + i * 0.1 for i in range(n)]
    return pd.DataFrame({
        "open": [v - 0.02 for v in x],
        "high": [v + 0.05 for v in x],
        "low": [v - 0.05 for v in x],
        "close": x,
        "volume": [1000.0] * n,
    })


class UnifiedTradeAuthorityTest(unittest.TestCase):
    def test_ppe_never_activates_runner_before_tp1(self):
        state = {
            "symbol": "BTC/USDT:USDT", "sl": 95.0, "trail_stop": 0.0,
            "max_price": 100.0, "min_price": 100.0,
            "smart_money": {"distribution_risk": 0, "institutional_bias": "BUY"},
            "momentum_flow": {"continuation_strength": 90, "momentum_health": 90,
                               "trend_expansion": True, "climax_risk": 0},
            "tp1_hit": False, "tp1_done": False, "runner_mode": False,
            "trail_active": False, "smart_trail_mult": 1.5,
        }
        old_atr, old_adx = E.compute_atr, E.compute_adx
        try:
            E.compute_atr = lambda df: pd.Series([1.0] * len(df), index=df.index)
            E.compute_adx = lambda df: pd.Series([35.0] * len(df), index=df.index)
            E.apply_50_50_profit_engine(_df(), len(_df()) - 1, 104.0, 1.0,
                                        "BUY", 100.0, state, 4.0,
                                        trade_state="TREND_RIDE", continuation_probability=0.9)
            self.assertFalse(state["runner_mode"])
        finally:
            E.compute_atr, E.compute_adx = old_atr, old_adx

    def test_live_loop_has_no_legacy_execution_bypass(self):
        source = (ROOT / "core" / "engine.py").read_text(encoding="utf-8")
        main_pos = source.index("def main_loop_sniper")
        main_tail = source[main_pos:]
        # The compatibility helpers may exist, but the live loop must not call
        # either of the legacy order paths.
        self.assertNotIn("if council_exit(df, price):", main_tail)
        self.assertNotIn("scaling_logic(sym, df, None)", main_tail)

    def test_canonical_tp_adapter_has_no_secondary_exit_logic(self):
        source = (ROOT / "core" / "engine.py").read_text(encoding="utf-8")
        start = source.index("def apply_profit_engine(")
        end = source.index("def detect_liquidity_context", start)
        block = source[start:end]
        self.assertIn('return "HOLD"', block)
        self.assertNotIn("Trailing stop hit", block)
        self.assertNotIn("Exhaustion exit", block)
        self.assertNotIn("close_position_full()\n", block)


if __name__ == "__main__":
    unittest.main()
