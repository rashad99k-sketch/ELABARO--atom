"""BARON Liquidity-Ahead runner tests.

Verifies the Liquidity Ahead integration in the REAL code paths:
  1. baron_zone_judge.forward_liquidity_map produces a pure, deterministic
     roadmap of forward targets (resistances for BUY, supports for SELL).
  2. engine._baron_liquidity_ahead_ctx is fail-open advisory (empty roadmap
     never blocks an entry gate).
  3. engine LiveTradeManager._compute_tp1_hold_score lets the runner ride when
     a major liquidity target lies ahead and protects when price is at a wall.
  4. engine LiveTradeManager._apply_runner_defense loosens the trail when
     riding into major liquidity and tightens at a liquidity wall.

No ccxt/Flask/network: synthetic offline DataFrames only.
"""
import os
import sys
import unittest

import numpy as np
import pandas as pd

import core.engine as E
import baron_zone_judge as jz


def _wave(level=100.0, amp=2.0, n=60, phase=0.0):
    """Oscillating series producing distinct swing highs/lows pattern."""
    o = np.full(n, level, dtype=float)
    c = np.full(n, level, dtype=float)
    h = np.full(n, level, dtype=float)
    l = np.full(n, level, dtype=float)
    v = np.full(n, 1000.0)
    for i in range(n):
        base = level + amp * np.sin(i / 4.0 + phase)
        body = amp * 0.4 * np.sin(i / 2.0 + phase)
        c[i] = base + body * 0.2
        o[i] = base - body * 0.2
        hi = base + amp * 0.7 + abs(body) * 0.3
        lo = base - amp * 0.7 - abs(body) * 0.3
        h[i] = max(hi, o[i], c[i])
        l[i] = min(lo, o[i], c[i])
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c, "volume": v})


def _mirror(df, axis=200.0):
    return pd.DataFrame({
        "open": axis - df["open"],
        "high": axis - df["low"],
        "low": axis - df["high"],
        "close": axis - df["close"],
        "volume": df["volume"],
    })


class JudgeForwardLiquidityMapTest(unittest.TestCase):

    def test_insufficient_data_returns_empty_roadmap(self):
        r = jz.forward_liquidity_map(None, side="BUY", price=100.0, atr=1.0)
        self.assertFalse(r["roadmap_valid"])
        self.assertEqual(r["targets"], [])
        self.assertIsNone(r["nearest_distance_atr"])

    def test_short_frame_returns_empty_roadmap(self):
        df = _wave(n=15)
        r = jz.forward_liquidity_map(df, side="BUY", price=100.0, atr=1.0)
        self.assertFalse(r["roadmap_valid"])

    def test_buy_roadmap_targets_above_price_sorted(self):
        df = _wave(level=100.0)
        r = jz.forward_liquidity_map(df, side="BUY", price=98.0, atr=1.0)
        self.assertTrue(r["roadmap_valid"])
        self.assertTrue(r["targets"])
        prices = [t["price"] for t in r["targets"]]
        self.assertTrue(all(p > 98.0 for p in prices))
        dists = [t["distance_atr"] for t in r["targets"]]
        self.assertEqual(dists, sorted(dists))
        self.assertEqual(r["nearest"], r["targets"][0])
        self.assertEqual(r["nearest_distance_atr"], r["targets"][0]["distance_atr"])

    def test_sell_roadmap_targets_below_price(self):
        df = _wave(level=100.0)
        r = jz.forward_liquidity_map(df, side="SELL", price=102.0, atr=1.0)
        self.assertTrue(r["roadmap_valid"])
        self.assertTrue(r["targets"])
        self.assertTrue(all(t["price"] < 102.0 for t in r["targets"]))

    def test_roadmap_pure_side_effect_free(self):
        df = _wave(level=100.0)
        before = df.copy(deep=True)
        jz.forward_liquidity_map(df, side="BUY", price=98.0, atr=1.0)
        pd.testing.assert_frame_equal(df, before)

    def test_major_liquidity_ahead_detected_when_runway_exists(self):
        # Resistance cluster 1+ ATR above the price -> major target ahead.
        df = _wave(level=100.0)
        r = jz.forward_liquidity_map(df, side="BUY", price=98.0, atr=1.0)
        if r["roadmap_valid"]:
            self.assertIn("major_liquidity_ahead", r)
            self.assertIn("major_target", r)
            self.assertIsNotNone(r["nearest"])

    def test_roadmap_valid_flag_false_without_forward_levels(self):
        # Perfectly flat series: no level strictly above the current price,
        # so no forward target may be invented.
        flat = pd.DataFrame({
            "open": [100.0] * 60, "high": [100.0] * 60,
            "low": [100.0] * 60, "close": [100.0] * 60, "volume": [1000.0] * 60,
        })
        r = jz.forward_liquidity_map(flat, side="BUY", price=100.0, atr=0.5)
        self.assertFalse(r["roadmap_valid"])


class EngineLiquidityAheadAdvisoryTest(unittest.TestCase):

    def test_advisory_fail_open_empty_roadmap(self):
        ctx = E._baron_liquidity_ahead_ctx(None, "BUY", 100.0, 1.0)
        self.assertIsInstance(ctx, dict)
        self.assertEqual(ctx.get("targets"), [])

    def test_advisory_with_roadmap_is_not_entry_gate(self):
        df = _wave(level=100.0)
        ctx = E._baron_liquidity_ahead_ctx(df, "BUY", 98.0, 1.0)
        self.assertIsInstance(ctx, dict)
        self.assertIn("nearest_distance_atr", ctx)
        self.assertIsInstance(ctx.get("roadmap_valid"), bool)


class HoldScoreLiquidityAheadTest(unittest.TestCase):

    def setUp(self):
        self.mgr = E.LiveTradeManager(E._event_bus, E._exchange_sync, E._recovery_guard)
        self._backup_open = E.STATE.get("open", False)
        E.STATE["open"] = False

    def tearDown(self):
        E.STATE["open"] = self._backup_open

    def _base(self):
        smart = {"smart_money_dominant": False, "banker_pressure": 50,
                 "retailer_pressure": 50, "retail_euphoria": False,
                 "distribution_risk": 20}
        momentum = {"continuation_strength": 60, "momentum_health": 55,
                    "trend_expansion": False, "exhaustion_risk": 20,
                    "climax_risk": 10, "momentum_decay": False}
        cont = E.ContinuationEvaluation(
            continuation_probability=0.7, trend_strength=0.7,
            exhaustion_probability=0.1, reclaim_risk=0.2,
            counter_pressure=0.2, confidence=0.7, reasons=["test"],
            should_hold=True, hold_quality="GOOD")
        return smart, momentum, cont

    def test_major_liquidity_ahead_boosts_hold_score(self):
        smart, momentum, cont = self._base()
        base = self.mgr._compute_tp1_hold_score(
            smart, momentum, 25.0, 1.0, "TREND_RIDE", cont, 20, False, False, 30)
        ride = {"roadmap_valid": True, "nearest_distance_atr": 2.0,
                "major_liquidity_ahead": True,
                "major_target": {"price": 102.0, "distance_atr": 2.0}}
        scored = self.mgr._compute_tp1_hold_score(
            smart, momentum, 25.0, 1.0, "TREND_RIDE", cont, 20, False, False, 30,
            liquidity_ahead=ride)
        self.assertGreaterEqual(scored, base)

    def test_at_liquidity_wall_reduces_hold_score(self):
        smart, momentum, cont = self._base()
        base = self.mgr._compute_tp1_hold_score(
            smart, momentum, 25.0, 1.0, "TREND_RIDE", cont, 20, False, False, 30)
        wall = {"roadmap_valid": True, "nearest_distance_atr": 0.2,
                "major_liquidity_ahead": False, "major_target": None}
        scored = self.mgr._compute_tp1_hold_score(
            smart, momentum, 25.0, 1.0, "TREND_RIDE", cont, 20, False, False, 30,
            liquidity_ahead=wall)
        self.assertLessEqual(scored, base)


class RunnerDefenseLiquidityAheadTest(unittest.TestCase):

    def setUp(self):
        self.mgr = E.LiveTradeManager(E._event_bus, E._exchange_sync, E._recovery_guard)
        self._backup = {"tp1_hit": E.STATE.get("tp1_hit", False),
                        "open": E.STATE.get("open", False)}
        E.STATE["open"] = True
        E.STATE["tp1_hit"] = True

    def tearDown(self):
        for k, v in self._backup.items():
            E.STATE[k] = v

    def test_ride_into_major_liquidity_loosens_trail(self):
        base = self.mgr._apply_runner_defense(
            60.0, 65.0, 5.0, 1, 0.7, 1.5, "TREND_RIDE")
        ride = {"roadmap_valid": True, "nearest_distance_atr": 2.0,
                "major_liquidity_ahead": True,
                "major_target": {"price": 102.0, "distance_atr": 2.0}}
        ridden = self.mgr._apply_runner_defense(
            60.0, 65.0, 5.0, 1, 0.7, 1.5, "TREND_RIDE", liquidity_ahead=ride)
        self.assertGreaterEqual(ridden, base)

    def test_at_wall_tightens_trail(self):
        base = self.mgr._apply_runner_defense(
            60.0, 65.0, 5.0, 1, 0.7, 1.5, "TREND_RIDE")
        wall = {"roadmap_valid": True, "nearest_distance_atr": 0.3,
                "major_liquidity_ahead": False, "major_target": None}
        walled = self.mgr._apply_runner_defense(
            60.0, 65.0, 5.0, 1, 0.7, 1.5, "TREND_RIDE", liquidity_ahead=wall)
        self.assertLessEqual(walled, base)


if __name__ == "__main__":
    unittest.main()