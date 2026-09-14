"""A-GRADE/roro unlock + READY-execution grace + queue lifetime widening.

Three production funnel fixes (user-approved, 2026-09-14):
  1. DeepScanner now computes ``roro_signal`` from causal win-side evidence
     instead of hardcoding False, so ``a_grade_ready`` (the A-GRADE fast path
     into the queue) is reachable again.
  2. execute_entry accepts ONLY bounded ADX/liquidity drift for a candidate the
     queue granted READY within the grace window; absent validation / stale
     grants / non-READY fallback candidates stay hard-blocked.
  3. Queue candidate lifetime and the promotion extension window are
     env-tunable (QUEUE_LIFETIME_SEC, QUEUE_MAX_EXTENSION_ATR).

These tests run on the REAL engine module fixtures (mirroring
test_institutional_bridge_fix / test_pipeline_accounting style).
"""

import os
import time
import unittest
from unittest import mock

import core.engine as E


def _radar_entry(roro, symbol="AGRD/USDT:USDT"):
    return {
        "symbol": symbol,
        "asset_class": "CRYPTO",
        "side": "BUY",
        "price": 100.0,
        "score": 7.5,
        "watch_score": 7.5,
        "strength": "MEDIUM",
        "deep_analyzed": True,
        "pre_expansion_state": "PRE_EXPANSION_LONG",
        "pre_expansion_evidence": ["BOS", "DISPLACEMENT", "OB_RETEST", "RO_RO_INSTITUTIONAL_FLOW"],
        "pre_expansion": {
            "phase": "EARLY_EXPANSION",
            "zone_quality": 7.0,
            "indicator_alignment": "BULLISH",
        },
        "analysis": {
            "ob_grade": "A+",
            "roro_signal": roro,
            "struct_score": 80.0,
            "liq_score": 75.0,
            "trap_risk": 40.0,
        },
        "institutional": {"score": 70.0},
        "intent_score": 70.0,
        "institutional_zone_active": False,
    }


class AGradeRoroUnlockTest(unittest.TestCase):
    """Fix 1: a_grade_ready must be reachable when the (now evidence-derived)
    roro_signal is True, and stay off when False."""

    def test_roro_unlocks_a_grade_ready(self):
        entry = _radar_entry(roro=True)
        radar = E.InstitutionalRadar()
        ok = radar._update_a_grade_status(entry)
        self.assertTrue(ok)
        self.assertTrue(entry["institutional_prepared"])
        self.assertTrue(entry["a_grade_ready"])
        self.assertEqual(entry["institutional_stage"], "A-GRADE_READY")

    def test_roro_false_keeps_a_grade_dead_path(self):
        entry = _radar_entry(roro=False)
        radar = E.InstitutionalRadar()
        ok = radar._update_a_grade_status(entry)
        self.assertFalse(ok)
        self.assertTrue(entry["institutional_prepared"])
        self.assertFalse(entry["a_grade_ready"])
        self.assertNotEqual(entry["institutional_stage"], "A-GRADE_READY")


class ReadyExecutionGraceTest(unittest.TestCase):
    """Fix 2: the grace accepts ONLY a live, freshly-validated READY grant."""

    def test_no_context_hard_blocks(self):
        ok, sec = E._ready_execution_grace({})
        self.assertFalse(ok)
        self.assertGreater(sec, 0)

    def test_not_validated_hard_blocks(self):
        ok, _ = E._ready_execution_grace(
            {"execution_context": {"is_ready_validated": False, "ready_ts": time.time(),
                                   "ready_adx": 40.0}})
        self.assertFalse(ok)

    def test_stale_ready_grant_hard_blocks(self):
        ok, _ = E._ready_execution_grace(
            {"execution_context": {"is_ready_validated": True,
                                   "ready_ts": time.time() - 200, "ready_adx": 40.0}})
        self.assertFalse(ok)

    def test_fresh_ready_grant_accepts(self):
        ok, _ = E._ready_execution_grace(
            {"execution_context": {"is_ready_validated": True,
                                   "ready_ts": time.time(), "ready_adx": 40.0}})
        self.assertTrue(ok)

    def test_grace_window_env_tunable(self):
        with mock.patch.dict(os.environ, {"EXECUTION_READY_GRACE_SEC": "30"}):
            ok, sec = E._ready_execution_grace(
                {"execution_context": {"is_ready_validated": True,
                                       "ready_ts": time.time(), "ready_adx": 40.0}})
            self.assertTrue(ok)
            self.assertEqual(sec, 30.0)


class QueueLifetimeTest(unittest.TestCase):
    """Fix 3: QUEUE_LIFETIME_SEC must replace the hardcoded 1h eviction."""

    @staticmethod
    def _candidate(symbol, age_sec):
        cand = E.ExecutionCandidate(
            symbol=symbol, side="BUY", price=100.0, entry_price=100.0,
            stop_loss=99.0, take_profit_1=101.0, take_profit_2=102.0,
            atr=0.5, df=None, ob={}, priority_score=10.0,
        )
        cand.added_at = time.time() - age_sec
        return cand

    def test_default_7200_keeps_younger_candidate(self):
        q = E.ExecutionQueue()
        q.add_candidate(self._candidate("KEEP1/USDT:USDT", 4000))
        q.cleanup()
        self.assertIn("KEEP1/USDT:USDT", q._candidates)
        self.assertEqual(q.gate_stats["expired"], 0)

    def test_default_7200_evicts_older_candidate(self):
        q = E.ExecutionQueue()
        q.add_candidate(self._candidate("DROP1/USDT:USDT", 8000))
        q.cleanup()
        self.assertNotIn("DROP1/USDT:USDT", q._candidates)
        self.assertGreaterEqual(q.gate_stats["expired"], 1)

    def test_short_lifetime_env_respected(self):
        with mock.patch.dict(os.environ, {"QUEUE_LIFETIME_SEC": "3600"}):
            q = E.ExecutionQueue()
            q.add_candidate(self._candidate("DROP2/USDT:USDT", 4000))
            q.cleanup()
            self.assertNotIn("DROP2/USDT:USDT", q._candidates)
            self.assertGreaterEqual(q.gate_stats["expired"], 1)


if __name__ == "__main__":
    unittest.main()