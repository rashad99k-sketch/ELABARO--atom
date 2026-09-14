"""BARON ZONE/OB judge gate integrated into core.engine.execute_entry.

Mirrors the FAIL-CLOSED contract proven for roro.execute_entry
(tests/test_baron_zone_judge.py::TestFailClosedBarrier): when
BARON_ZONE_JUDGE != "0" the judge runs inside execute_entry and any verdict
other than ENTER_NOW — or any import/evaluation error — blocks the entry and
must never continue into position sizing / order execution. The gate itself is
pinned OFF for the deterministic offline suite via conftest; these tests opt
back in explicitly.
"""
import contextlib
import importlib
import os
import sys
import types
import unittest
import unittest.mock as mock

import numpy as np
import pandas as pd


class _FakeFlask:
    def __init__(self, *args, **kwargs):
        pass
    def route(self, *args, **kwargs):
        return lambda fn: fn
    def add_url_rule(self, *args, **kwargs):
        return None


def _load_engine():
    saved = {k: sys.modules.get(k) for k in ("ccxt", "flask", "core.engine")}
    old_paper = os.environ.pop("PAPER_MODE", None)
    fake_ccxt = types.ModuleType("ccxt")

    class FakeBingX:
        def __init__(self, *args, **kwargs):
            self.markets = {}
        def market(self, symbol):
            return {"limits": {"amount": {"min": 0}}, "precision": {"amount": 1}}

    fake_ccxt.bingx = FakeBingX
    fake_flask = types.ModuleType("flask")
    fake_flask.Flask = _FakeFlask
    fake_flask.jsonify = lambda *a, **k: None
    fake_flask.request = types.SimpleNamespace()
    sys.modules["ccxt"] = fake_ccxt
    sys.modules["flask"] = fake_flask
    sys.modules.pop("core.engine", None)
    engine = importlib.import_module("core.engine")
    return engine, saved, old_paper


def _bearing_df():
    """Valid OHLCV frame (>=10 bars, canonical columns) for gate plumbing."""
    n = 60
    o = np.full(n, 100.0); c = np.full(n, 100.0)
    h = np.full(n, 100.8); l = np.full(n, 99.3)
    v = np.full(n, 1000.0)
    i = 42
    o[i], c[i], h[i], l[i] = 100.6, 99.7, 100.8, 99.2
    o[i+1], c[i+1], h[i+1], l[i+1] = 99.7, 101.0, 100.9, 99.5
    o[i+2], c[i+2], h[i+2], l[i+2] = 101.2, 101.6, 101.7, 101.0
    o[i+3], c[i+3], h[i+3], l[i+3] = 101.6, 102.0, 102.1, 101.4
    v[i+1] = 2600.0
    for j in range(i+4, n-4):
        o[j] = c[j] = 102.0
        h[j], l[j] = 102.4, 101.5
    o[n-4], c[n-4], h[n-4], l[n-4] = 101.8, 100.6, 101.9, 100.5
    o[n-3], c[n-3], h[n-3], l[n-3] = 100.4, 99.8, 100.5, 99.4
    o[n-2], c[n-2], h[n-2], l[n-2] = 99.4, 99.6, 99.7, 99.15
    o[n-1], c[n-1], h[n-1], l[n-1] = 99.6, 99.85, 100.1, 99.5
    return pd.DataFrame({"timestamp": np.arange(n), "open": o, "high": h,
                         "low": l, "close": c, "volume": v})


def _verdict(decision):
    class _V:
        pass
    v = _V()
    v.decision = decision
    v.main_blocker = None if decision == "ENTER_NOW" else decision
    v.pending_reason = "" if decision == "ENTER_NOW" else "reason"
    v.final_zone_score = 80.0
    return v


def _jz():
    import baron_zone_judge
    return baron_zone_judge


class BaronGateEngineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine, cls.saved, cls.old_paper = _load_engine()
        cls._jz_was_present = "baron_zone_judge" in sys.modules
        # Make the sizing branch reachable (live sizing path) so the sentry is
        # only recorded when the flow passed every pre-sizing gate.
        cls.engine.PAPER_MODE = False

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop("core.engine", None)
        if not cls._jz_was_present:
            sys.modules.pop("baron_zone_judge", None)
        for name, module in cls.saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        if cls.old_paper is not None:
            os.environ["PAPER_MODE"] = cls.old_paper

    def setUp(self):
        E = self.engine
        E.STATE.clear()
        E.paper.update({"position": {}, "balance": 10000.0, "committed_margin": 0.0})
        E.TRADE_STATE.clear()
        E.MEMORY.setdefault("execution_blockers", [])
        self.sizing_mock = mock.MagicMock(return_value=10000.0)
        self._stack = contextlib.ExitStack()
        self._stack.__enter__()
        # Net-neutral pipeline so ONLY the BARON gate is the deciding filter
        # between us and position sizing.
        self._stack.enter_context(mock.patch.dict(os.environ, {"BARON_ZONE_JUDGE": "1"}))
        self._stack.enter_context(mock.patch.object(E, "get_ohlcv_safe", return_value=_bearing_df()))
        self._stack.enter_context(mock.patch.object(E, "get_free_balance_safe", self.sizing_mock))
        self._stack.enter_context(mock.patch.object(E, "log_execution", lambda *a, **k: None))
        self._stack.enter_context(mock.patch.object(E, "compute_adx", return_value=pd.Series([30.0])))
        self._stack.enter_context(mock.patch.object(E, "detect_liquidity_context", return_value="sell_side_taken"))
        self._stack.enter_context(mock.patch.object(E, "get_sweep_authenticity", return_value=("real", 5)))

    def tearDown(self):
        self._stack.__exit__(None, None, None)

    def _exec(self, **kw):
        E = self.engine
        kwargs = dict(
            side="BUY", symbol="BTCUSDT", price=99.12, sl=98.2, tp1=100.5, tp2=101.5,
            score=80.0, reason="test", atr_val=1.0, trade_type="SPOT",
            entry_type="RETEST", classification="SNIPER")
        kwargs.update(kw)
        return E.execute_entry(**kwargs)

    def _last_blocker(self):
        E = self.engine
        ledger = E.MEMORY.get("execution_blockers", [])
        return ledger[-1]["blocker"] if ledger else None

    def test_import_failure_fail_closed(self):
        with mock.patch.dict(sys.modules, {"baron_zone_judge": None}):
            result = self._exec()
        self.assertFalse(result)
        self.assertEqual(self._last_blocker(), "BARON_ERROR")
        self.sizing_mock.assert_not_called()

    def test_execution_error_fail_closed(self):
        with mock.patch.object(_jz(), "assess", side_effect=RuntimeError("internal evaluation error")):
            result = self._exec()
        self.assertFalse(result)
        self.assertEqual(self._last_blocker(), "BARON_ERROR")
        self.sizing_mock.assert_not_called()

    def test_internal_verdict_error_fail_closed(self):
        with mock.patch.object(_jz(), "assess", side_effect=ValueError("indeterminate verdict")):
            result = self._exec()
        self.assertFalse(result)
        self.assertEqual(self._last_blocker(), "BARON_ERROR")
        self.sizing_mock.assert_not_called()

    def test_block_verdict_blocks_entry(self):
        with mock.patch.object(_jz(), "assess", return_value=_verdict("BLOCK")):
            result = self._exec()
        self.assertFalse(result)
        self.assertEqual(self._last_blocker(), "BARON_REJECT")
        self.sizing_mock.assert_not_called()

    def test_wait_verdict_blocks_entry(self):
        with mock.patch.object(_jz(), "assess", return_value=_verdict("WAIT_RETEST")):
            result = self._exec()
        self.assertFalse(result)
        self.assertEqual(self._last_blocker(), "BARON_REJECT")
        self.sizing_mock.assert_not_called()

    def test_enter_now_passes_gate_to_sizing(self):
        with mock.patch.object(_jz(), "assess", return_value=_verdict("ENTER_NOW")):
            self._exec()
        # Reaching position sizing (get_free_balance_safe) proves the BARON
        # gate let the flow continue past it.
        self.sizing_mock.assert_called_once()

    def test_judge_explicitly_disabled_falls_through(self):
        with mock.patch.dict(os.environ, {"BARON_ZONE_JUDGE": "0"}):
            self._exec()
        self.sizing_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()