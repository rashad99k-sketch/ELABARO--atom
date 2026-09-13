"""BARON Zone/OB Quality Judge — additive entry-location filter tests (T1-T7).

The judge is a pure, self-contained filter between RORO's candidate producers
and execute_entry. It only grades zone / order-block / location quality and
returns ENTER_NOW / WAIT_RETEST / BLOCK. These tests use synthetic offline
DataFrames (no ccxt, no Flask, no network) and verify:

  T1  pristine BUY location  -> ENTER_NOW
  T2  mirrored SELL location -> ENTER_NOW  (BUY/SELL symmetry)
  T3  price extended/chasing -> BLOCK PRICE_EXTENDED
  T4  support/zone broken    -> BLOCK ZONE_BROKEN
  T5  S/R flip (BUY @ resistance with weak structure) -> BLOCK SR_FLIP
  T6  degenerate OB (broken body) -> BLOCK OB_QUALITY:BROKEN
  T7  volume exhaustion still WAIT_RETEST, never a hard gate
  *   insufficient/invalid data -> BLOCK (never ENTER_NOW)
  *   waiting for retest when valid zone but price not at it
  *   judge is side-effect free and dependency-light
"""
import json as _json
import contextlib as _contextlib
import os
import sys
import unittest
from unittest import mock

import numpy as np
import pandas as pd

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import baron_zone_judge as jz


def _frame(rows):
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"])


def _mirror(df):
    """Reflect prices around 100.0 so a BUY setup becomes its SELL equivalent."""
    return pd.DataFrame({
        "open": 200.0 - df["open"],
        "high": 200.0 - df["low"],
        "low": 200.0 - df["high"],
        "close": 200.0 - df["close"],
        "volume": df["volume"],
    })


def _buy_enter_series():
    rows = [(100.0, 100.15, 99.85, 100.0, 1000)] * 46
    rows += [
        (100.0, 100.05, 99.3, 99.4, 500),
        (99.4, 99.5, 99.15, 99.3, 500),
        (99.3, 99.4, 99.1, 99.2, 500),
        (99.2, 100.0, 99.15, 99.5, 500),
        (99.6, 99.65, 99.1, 99.2, 500),     # OB (red), low 99.1
        (99.2, 99.3, 98.9, 99.05, 500),     # sell-side sweep
        (99.2, 100.5, 99.2, 100.4, 2000),   # displacement
        (100.4, 100.45, 99.08, 99.9, 2000), # pullback into zone
        (99.08, 99.12, 98.95, 99.10, 2000), # retest rejection (fresh)
    ]
    return _frame(rows)


class TestJudgeContract(unittest.TestCase):

    def test_import_is_dependency_light(self):
        pre = set(sys.modules)
        import baron_zone_judge  # noqa: F401
        post = set(sys.modules)
        leaked = {"ccxt", "flask"} & (post - pre)
        self.assertEqual(leaked, set())

    def test_assess_is_side_effect_free(self):
        df = _buy_enter_series()
        snapshot = df.copy(deep=True)
        verdict = jz.assess("BTCUSDT", "BUY", df)
        pd.testing.assert_frame_equal(df, snapshot)
        self.assertIn(verdict.decision, {"ENTER_NOW", "WAIT_RETEST", "BLOCK"})

    def test_verdict_serializable(self):
        verdict = jz.assess("BTCUSDT", "BUY", _buy_enter_series())
        payload = verdict.to_dict()
        self.assertTrue(_json.dumps(payload))
        self.assertEqual(payload["symbol"], "BTCUSDT")
        self.assertIsInstance(payload["dimensions"], dict)
        self.assertEqual(len(payload["dimensions"]), 8)
        self.assertAlmostEqual(
            sum(payload["dimensions"].get(k, 0) * w for k, w in jz.ZONE_WEIGHTS.items()),
            payload["final_zone_score"], delta=0.5)

    def test_weights_sum_to_one(self):
        self.assertAlmostEqual(sum(jz.ZONE_WEIGHTS.values()), 1.0, places=6)


class TestT1EnterNow(unittest.TestCase):

    def test_pristine_buy_enter_now(self):
        verdict = jz.assess("BTCUSDT", "BUY", _buy_enter_series())
        self.assertEqual(verdict.decision, "ENTER_NOW", verdict.to_dict())
        self.assertGreaterEqual(verdict.final_zone_score, 72)
        self.assertEqual(verdict.side, "BUY")

    def test_sell_enter_now_symmetric(self):
        verdict = jz.assess("BTCUSDT", "SELL", _mirror(_buy_enter_series()))
        self.assertEqual(verdict.decision, "ENTER_NOW", verdict.to_dict())
        self.assertGreaterEqual(verdict.final_zone_score, 72)
        self.assertEqual(verdict.side, "SELL")


class TestT3PriceExtended(unittest.TestCase):

    def test_chasing_price_is_blocked(self):
        verdict = jz.assess("BTCUSDT", "BUY", _buy_enter_series(), price=103.5)
        self.assertEqual(verdict.decision, "BLOCK")
        self.assertEqual(verdict.main_blocker, "PRICE_EXTENDED")


class TestT4ZoneBroken(unittest.TestCase):

    def test_support_break_is_blocked(self):
        verdict = jz.assess("BTCUSDT", "BUY", _buy_enter_series(), price=96.0)
        self.assertEqual(verdict.decision, "BLOCK")
        self.assertEqual(verdict.main_blocker, "ZONE_BROKEN")


class TestT5SrFlip(unittest.TestCase):

    def _build(self):
        rows = [(100.0, 101.1, 99.9, 100.0, 1000)] * 31
        rows += [
            (100.0, 101.1, 99.9, 101.0, 1000),
            (101.0, 101.1, 100.1, 100.2, 1000),
            (100.2, 101.1, 100.2, 101.0, 1000),
            (101.0, 101.1, 100.3, 100.4, 1000),
            (100.4, 100.5, 100.1, 100.3, 1000),
            (100.3, 100.4, 100.0, 100.2, 1000),
            (100.2, 101.1, 100.1, 101.05, 1000),
            (101.05, 101.15, 100.9, 101.0, 1200),
            (101.0, 101.2, 100.95, 101.1, 1500),  # price right at resistance
        ]
        return _frame(rows)

    def test_buy_at_resistance_with_weak_structure_is_blocked(self):
        verdict = jz.assess("BTCUSDT", "BUY", self._build())
        self.assertEqual(verdict.decision, "BLOCK")
        self.assertEqual(verdict.main_blocker, "SR_FLIP")


class TestT6ObBroken(unittest.TestCase):

    def _build(self):
        rows = [(100.0, 100.15, 99.85, 100.0, 1000)] * 36
        rows += [
            (100.0, 100.1, 99.4, 99.5, 800),
            (99.5, 99.6, 99.2, 99.4, 800),
            (99.4, 99.5, 99.0, 99.1, 800),       # OB (red), low 99.0
            (99.1, 100.7, 99.1, 100.6, 2000),    # displacement
            (100.6, 100.7, 98.0, 98.6, 2500),    # body broken below OB
            (98.6, 99.4, 98.5, 99.2, 1200),
            (99.2, 99.5, 98.9, 99.0, 1000),
            (99.0, 99.4, 98.95, 99.15, 1000),
            (99.15, 99.4, 99.0, 99.1, 1000),     # thin retest, no rejection
        ]
        return _frame(rows)

    def test_broken_ob_body_is_blocked(self):
        verdict = jz.assess("BTCUSDT", "BUY", self._build())
        self.assertEqual(verdict.decision, "BLOCK")
        self.assertEqual(verdict.main_blocker, "OB_QUALITY:BROKEN")


class TestT7VolumeEvidenceOnly(unittest.TestCase):

    def test_exhaustion_waits_never_blocks(self):
        df = _buy_enter_series()
        df.loc[df.index[-3], "volume"] = 200
        df.loc[df.index[-2], "volume"] = 150
        df.loc[df.index[-1], "volume"] = 120
        verdict = jz.assess("BTCUSDT", "BUY", df)
        self.assertEqual(verdict.decision, "WAIT_RETEST")
        self.assertEqual(verdict.volume_state, "exhaustion")
        self.assertNotEqual(verdict.decision, "BLOCK")


class TestWaitRetest(unittest.TestCase):

    def _build(self):
        rows = [(100.0, 100.15, 99.85, 100.0, 1000)] * 49
        rows += [
            (100.0, 100.1, 99.2, 99.5, 1000),
            (99.5, 99.6, 99.1, 99.2, 800),       # OB (red), low 99.1
            (99.2, 99.3, 98.9, 99.05, 800),      # sweep
            (99.2, 100.5, 99.2, 100.4, 2000),    # displacement
            (100.4, 100.45, 99.9, 100.1, 2000),
            (99.9, 100.0, 99.6, 99.7, 1000),     # price off the zone
        ]
        return _frame(rows)

    def test_valid_zone_but_price_not_at_zone(self):
        verdict = jz.assess("BTCUSDT", "BUY", self._build())
        self.assertEqual(verdict.decision, "WAIT_RETEST")
        self.assertEqual(verdict.pending_reason, "Do not chase. Wait for pullback / retest.")
        self.assertTrue(verdict.chase)
        self.assertTrue(verdict.zone["tapped"] in (True, False) or verdict.zone is not None)


class TestObZoneBand(unittest.TestCase):

    def test_ob_exposed_as_price_band(self):
        verdict = jz.assess("BTCUSDT", "BUY", _buy_enter_series())
        band = verdict.zone
        self.assertIsNotNone(band["high"])
        self.assertIsNotNone(band["low"])
        self.assertIsNotNone(band["mid"])
        self.assertGreater(band["width_atr"], 0)
        self.assertEqual(band["high"] >= band["mid"] >= band["low"], True)
        self.assertEqual(verdict.diagnostics["ob_zone"]["type"], "DEMAND")

    def test_price_inside_ob_band(self):
        verdict = jz.assess("BTCUSDT", "BUY", _buy_enter_series())
        self.assertTrue(verdict.zone["inside"])
        self.assertEqual(verdict.diagnostics["price_location"]["inside_zone"], True)

    def test_price_one_profile_above_zone_is_wait_not_block(self):
        df = _buy_enter_series()
        atr = jz._compute_atr(df)
        zone = 99.1
        v = jz.assess("BTCUSDT", "BUY", df, price=zone + 1.4 * atr)
        self.assertEqual(v.decision, "WAIT_RETEST")   # 1.4 ATR: extended-chase is NOT a block
        self.assertTrue(v.chase)
        self.assertTrue(v.diagnostics["price_location"]["chase"])


class TestSrFlip(unittest.TestCase):

    def _flip_buy_support_invalid(self):
        rows = [(100.0, 100.15, 99.85, 100.0, 1000)] * 21
        rows += [
            (100.0, 100.05, 99.3, 99.4, 1000),
            (99.4, 99.5, 98.7, 99.0, 1000),
            (99.0, 99.1, 98.5, 98.6, 1000),
            (98.6, 99.0, 98.5, 98.8, 1000),
            (98.8, 99.05, 98.6, 98.9, 1000),
            (98.9, 98.95, 97.4, 97.6, 2000),   # support 98.5 broken by close
            (97.6, 97.8, 97.15, 97.5, 1500),
            (97.5, 97.7, 97.1, 97.3, 1500),
            (97.3, 98.1, 97.25, 98.0, 1500),
            (98.0, 98.15, 97.8, 98.1, 1200),   # retest from below, no reclaim
        ]
        return _frame(rows)

    def test_buy_broken_support_is_blocked_support_invalidated(self):
        verdict = jz.assess("BTCUSDT", "BUY", self._flip_buy_support_invalid())
        self.assertEqual(verdict.decision, "BLOCK")
        self.assertEqual(verdict.main_blocker, "SUPPORT_INVALIDATED")

    def _flip_buy_resistance_to_support(self):
        rows = [(100.0, 100.35, 99.65, 100.0, 1000)] * 21
        rows += [
            (100.0, 100.35, 99.65, 100.3, 1000),
            (100.3, 100.4, 100.2, 100.35, 1000),
            (100.35, 100.4, 99.9, 100.0, 1000),
            (100.0, 100.35, 99.95, 100.1, 1000),
            (100.1, 101.7, 100.05, 101.6, 2000),   # break above resistance 100.35
            (101.6, 101.7, 101.1, 101.5, 1200),
            (101.5, 101.6, 101.0, 101.2, 1200),
            (101.2, 101.3, 100.0, 100.9, 1500),    # retest into 100.35
            (100.9, 101.3, 100.8, 101.2, 1500),    # held -> support
        ]
        return _frame(rows)

    def test_buy_resistance_to_support_is_positive_evidence(self):
        verdict = jz.assess("BTCUSDT", "BUY", self._flip_buy_resistance_to_support())
        self.assertEqual(verdict.sr_flip_state, "RESISTANCE_TO_SUPPORT")
        self.assertEqual(verdict.decision, "WAIT_RETEST")
        self.assertGreaterEqual(verdict.dimensions["structure_alignment"], 85)

    def test_sell_broken_resistance_is_blocked_resistance_invalidated(self):
        verdict = jz.assess("BTCUSDT", "SELL", _mirror(self._flip_buy_support_invalid()))
        self.assertEqual(verdict.decision, "BLOCK")
        self.assertEqual(verdict.main_blocker, "RESISTANCE_INVALIDATED")

    def test_sell_support_to_resistance_is_positive_evidence(self):
        verdict = jz.assess("BTCUSDT", "SELL", _mirror(self._flip_buy_resistance_to_support()))
        self.assertEqual(verdict.sr_flip_state, "SUPPORT_TO_RESISTANCE")
        self.assertEqual(verdict.decision, "WAIT_RETEST")


class TestObValidityStates(unittest.TestCase):

    def _fresh_ob(self):
        rows = [(100.0, 100.15, 99.85, 100.0, 1000)] * 41
        rows += [
            (99.6, 99.65, 99.1, 99.15, 1000),   # OB (red), band [99.1, 99.65]
            (99.15, 99.9, 99.7, 99.2, 800),     # filler, stays out of band
            (99.2, 100.9, 99.8, 100.8, 2000),   # displacement
            (99.05, 99.14, 98.7, 99.12, 2000),  # fresh retest at OB low
        ]
        return _frame(rows)

    def test_fresh_ob_label(self):
        verdict = jz.assess("BTCUSDT", "BUY", self._fresh_ob())
        self.assertEqual(verdict.ob_state, "FRESH")
        self.assertEqual(verdict.ob_quality, "FRESH")
        self.assertGreaterEqual(verdict.dimensions["order_block_quality"], 85)

    def _consumed_ob(self):
        rows = [(100.0, 100.15, 99.85, 100.0, 1000)] * 39
        rows += [
            (99.8, 99.9, 99.3, 99.4, 1000),      # OB (red), band [99.3, 99.9]
            (99.4, 100.0, 99.3, 99.5, 1000),     # tap 1
            (99.5, 101.2, 99.6, 101.3, 2000),    # displacement
            (101.0, 101.05, 99.5, 99.9, 1500),   # tap 2, bleeding down
            (99.9, 100.0, 99.2, 99.5, 1500),     # tap 3
            (99.3, 99.42, 99.25, 99.4, 900),    # compact exhaustion candle, flat bleed
        ]
        return _frame(rows)

    def test_consumed_ob_blocked(self):
        verdict = jz.assess("BTCUSDT", "BUY", self._consumed_ob())
        self.assertEqual(verdict.ob_state, "CONSUMED")
        self.assertEqual(verdict.decision, "BLOCK")
        self.assertEqual(verdict.main_blocker, "OB_QUALITY:CONSUMED")

    def _invalidated_ob(self):
        rows = [(100.0, 100.15, 99.85, 100.0, 1000)] * 32
        rows += [
            (99.6, 99.65, 99.0, 99.1, 1000),     # OB (red), band [99.0, 99.65]
            (99.1, 99.5, 98.95, 99.3, 800),
            (99.3, 100.9, 99.3, 100.8, 2000),    # displacement up
            (100.8, 100.9, 100.0, 100.2, 1200),
            (100.2, 100.3, 99.2, 99.4, 1200),
            (99.4, 99.5, 98.6, 98.8, 1500),      # closes through OB low
            (98.8, 98.9, 98.3, 98.5, 1500),
            (98.5, 98.6, 98.0, 98.2, 1200),      # bearish shift, context reversed
        ]
        return _frame(rows)

    def test_invalidated_ob_reports_invalidated(self):
        verdict = jz.assess("BTCUSDT", "BUY", self._invalidated_ob())
        self.assertEqual(verdict.diagnostics["ob_validity"], "INVALIDATED")
        self.assertEqual(verdict.decision, "BLOCK")


class TestVolumeRecipe(unittest.TestCase):

    def test_volume_confirmation_good_with_expansion(self):
        verdict = jz.assess("BTCUSDT", "BUY", _buy_enter_series())
        recipe = verdict.diagnostics["volume_recipe"]
        self.assertEqual(recipe["confirmation"], "GOOD")
        self.assertGreaterEqual(recipe["rejection_ratio"], 1.3)
        self.assertEqual(recipe["state"], "expansion")
        self.assertTrue(recipe["creation_ratio"] is None or recipe["creation_ratio"] > 0)

    def test_volume_never_hard_gate_on_its_own(self):
        df = _buy_enter_series()
        df.loc[df.index[-1], "volume"] = 90
        df.loc[df.index[-2], "volume"] = 110
        verdict = jz.assess("BTCUSDT", "BUY", df)
        self.assertNotEqual(verdict.decision, "BLOCK")


class TestReport(unittest.TestCase):

    def test_render_report_shape(self):
        verdict = jz.assess("BTCUSDT", "BUY", _buy_enter_series())
        text = verdict.render_report()
        self.assertIn("RORO SIGNAL", text)
        self.assertIn("ENTER_NOW", text)
        self.assertIn("DEMAND", text)
        self.assertIn("\U0001F7E2", text)  # green dot present


class TestInsufficientData(unittest.TestCase):

    def test_none_df_blocks(self):
        verdict = jz.assess("BTCUSDT", "BUY", None)
        self.assertEqual(verdict.decision, "BLOCK")
        self.assertEqual(verdict.main_blocker, "INSUFFICIENT_DATA")

    def test_empty_df_blocks(self):
        empty = _frame([])
        verdict = jz.assess("BTCUSDT", "BUY", empty)
        self.assertEqual(verdict.decision, "BLOCK")
        self.assertEqual(verdict.main_blocker, "INSUFFICIENT_DATA")

    def test_short_df_blocks(self):
        short = _buy_enter_series().iloc[:10].reset_index(drop=True)
        verdict = jz.assess("BTCUSDT", "BUY", short)
        self.assertEqual(verdict.decision, "BLOCK")
        self.assertEqual(verdict.main_blocker, "INSUFFICIENT_DATA")

    def test_missing_column_blocks(self):
        df = _buy_enter_series().drop(columns=["volume"])
        verdict = jz.assess("BTCUSDT", "BUY", df)
        self.assertEqual(verdict.decision, "BLOCK")
        self.assertTrue(verdict.main_blocker.startswith("INSUFFICIENT_DATA"))

    def test_invalid_price_blocks(self):
        verdict = jz.assess("BTCUSDT", "BUY", _buy_enter_series(), price=-1.0)
        self.assertEqual(verdict.decision, "BLOCK")
        self.assertEqual(verdict.main_blocker, "INVALID_CONTEXT:price")

    def test_zero_price_falls_back_to_last_close(self):
        verdict = jz.assess("BTCUSDT", "BUY", _buy_enter_series(), price=0.0)
        self.assertEqual(verdict.decision, "ENTER_NOW")

    def test_missing_data_never_enters(self):
        for df in (None, _frame([]), _buy_enter_series().iloc[:10]):
            verdict = jz.assess("BTCUSDT", "BUY", df)
            self.assertNotEqual(verdict.decision, "ENTER_NOW")


class TestFailClosedBarrier(unittest.TestCase):
    """RORO integration: the BARON gate inside roro.execute_entry is FAIL-CLOSED.

    If the judge cannot be imported, cannot execute, or raises an internal
    evaluation error, execute_entry MUST return False (entry blocked) and the
    flow MUST NOT continue past the BARON gate into order execution.
    """

    _DOWNLOAD_DIR = r"C:\Users\rasha\Downloads"

    @classmethod
    def setUpClass(cls):
        if cls._DOWNLOAD_DIR not in sys.path:
            sys.path.insert(0, cls._DOWNLOAD_DIR)
        cls.roro = sys.modules.get("roro")
        if cls.roro is None:
            import importlib
            cls.roro = importlib.import_module("roro")
        cls._env_backup = None

    def setUp(self):
        roro = self.roro
        self._state_open_backup = roro.STATE.get("open")
        self._in_position_backup = roro.TRADE_STATE.get("in_position")
        self._paper_mode_backup = roro.PAPER_MODE
        roro.STATE["open"] = False
        roro.TRADE_STATE["in_position"] = False
        roro.PAPER_MODE = False
        self._stack = _contextlib.ExitStack()
        self._stack.__enter__()
        self._stack.enter_context(mock.patch.dict(os.environ, {"BARON_ZONE_JUDGE": "1"}))
        self._stack.enter_context(mock.patch.object(roro, "log_execution", lambda *a, **k: None))
        self._stack.enter_context(
            mock.patch.object(roro, "get_ohlcv_safe", return_value=_buy_enter_series()))
        self._stack.enter_context(
            mock.patch.object(roro, "get_free_balance_safe",
                              side_effect=AssertionError("BARON gate bypassed: reached balance sizing")))

    def tearDown(self):
        roro = self.roro
        roro.STATE["open"] = self._state_open_backup
        roro.TRADE_STATE["in_position"] = self._in_position_backup
        roro.PAPER_MODE = self._paper_mode_backup
        self._stack.__exit__(None, None, None)

    def _exec(self, **kw):
        kwargs = dict(
            side="BUY", symbol="BTCUSDT", price=99.12, sl=98.2, tp1=100.5, tp2=101.5,
            score=80.0, reason="test", atr_val=1.0, trade_type="SPOT",
            entry_type="RETEST", classification="SNIPER")
        kwargs.update(kw)
        return self.roro.execute_entry(**kwargs)

    @staticmethod
    def _verdict(decision):
        class _V:
            pass
        v = _V()
        v.decision = decision
        v.main_blocker = None if decision == "ENTER_NOW" else decision
        v.pending_reason = "" if decision == "ENTER_NOW" else "reason"
        v.final_zone_score = 80.0
        return v

    def test_import_failure_fail_closed(self):
        with mock.patch.dict(sys.modules, {"baron_zone_judge": None}):
            result = self._exec()
        self.assertFalse(result)

    def test_execution_error_fail_closed(self):
        with mock.patch.object(
                jz, "assess",
                side_effect=RuntimeError("internal evaluation error")):
            result = self._exec()
        self.assertFalse(result)

    def test_internal_verdict_error_fail_closed(self):
        with mock.patch.object(
                jz, "assess",
                side_effect=ValueError("indeterminate verdict")):
            result = self._exec()
        self.assertFalse(result)

    def test_block_verdict_blocks_entry(self):
        with mock.patch.object(jz, "assess", return_value=self._verdict("BLOCK")):
            result = self._exec()
        self.assertFalse(result)

    def test_wait_verdict_blocks_entry(self):
        with mock.patch.object(jz, "assess", return_value=self._verdict("WAIT_RETEST")):
            result = self._exec()
        self.assertFalse(result)

    def test_enter_now_passes_gate(self):
        with mock.patch.object(jz, "assess", return_value=self._verdict("ENTER_NOW")):
            with self.assertRaises(AssertionError):
                self._exec()

    def test_judge_explicitly_disabled_falls_through(self):
        with mock.patch.dict(os.environ, {"BARON_ZONE_JUDGE": "0"}):
            with self.assertRaises(AssertionError):
                self._exec()


if __name__ == "__main__":
    unittest.main()