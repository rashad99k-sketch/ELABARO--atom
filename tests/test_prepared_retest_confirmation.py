import importlib
import os
import sys
import types
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd


class _FakeFlask:
    def __init__(self, *a, **k): pass
    def route(self, *a, **k): return lambda fn: fn
    def add_url_rule(self, *a, **k): return None


def _load():
    saved = {k: sys.modules.get(k) for k in ('ccxt', 'flask', 'core.engine')}
    old = os.environ.pop('PAPER_MODE', None)
    ccxt = types.ModuleType('ccxt')
    class FakeBingX:
        def __init__(self, *a, **k): self.markets = {}
    ccxt.bingx = FakeBingX
    flask = types.ModuleType('flask')
    flask.Flask = _FakeFlask
    flask.jsonify = lambda *a, **k: None
    flask.request = types.SimpleNamespace()
    sys.modules['ccxt'] = ccxt
    sys.modules['flask'] = flask
    sys.modules.pop('core.engine', None)
    return importlib.import_module('core.engine'), saved, old


def _frame():
    n = 40
    close = np.full(n, 100.0)
    open_ = close.copy()
    high = close + 0.5
    low = close - 0.5
    volume = np.full(n, 1000.0)
    # Last closed candle touches the causal BUY zone [99, 101] and rejects.
    open_[-1], close[-1], high[-1], low[-1] = 99.4, 100.3, 100.5, 98.9
    volume[-1] = 1800.0
    return pd.DataFrame({
        'timestamp': np.arange(n), 'open': open_, 'high': high,
        'low': low, 'close': close, 'volume': volume,
    })


class PreparedRetestConfirmationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.E, cls.saved, cls.old = _load()

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop('core.engine', None)
        for k, v in cls.saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
        if cls.old is not None:
            os.environ['PAPER_MODE'] = cls.old

    def test_prepared_retest_is_narrow_and_requires_zone_response(self):
        E = self.E
        q = E.ExecutionQueue()
        df = _frame()
        with patch.object(E.RejectionIntelligence, 'is_bullish_rejection', return_value=(True, ['rejection'])):
            self.assertTrue(q._prepared_retest_confirmed(df, 'BUY', 1.0, 99.0, 101.0))
        # No zone touch => no confirmation even if rejection exists elsewhere.
        df2 = df.copy()
        df2.loc[:, 'low'] = 105.0
        df2.loc[:, 'high'] = 106.0
        with patch.object(E.RejectionIntelligence, 'is_bullish_rejection', return_value=(True, ['rejection'])):
            self.assertFalse(q._prepared_retest_confirmed(df2, 'BUY', 1.0, 99.0, 101.0))

    def test_prepared_candidate_needs_only_one_new_confirmation_event(self):
        E = self.E
        q = E.ExecutionQueue()
        cand = E.ExecutionCandidate(
            symbol='PREP/USDT:USDT', side='BUY', price=100.0, entry_price=100.0,
            stop_loss=98.0, take_profit_1=102.0, take_profit_2=104.0,
            atr=1.0, df=_frame(), ob={},
        )
        cand.institutional_prepared = True
        cand.precursor_count = 3
        cand.zone_low, cand.zone_high = 99.0, 101.0
        cand.zone_state = 'ENTRY_WINDOW'
        cand.latest_adx = 30.0
        cand.latest_adx_bounds = [16.0, 55.0]
        cand.zone_metrics = E.ZoneMetrics(
            order_block_quality=90, zone_strength=90, liquidity_quality=80,
            institutional_confidence=85, structure_alignment=85,
            entry_timing=90, trend_alignment=90, risk_score=90,
            trigger_state='RETEST_CONFIRMED',
        )
        cand.evidence = {
            'sweep_quality': 'strong', 'structure_valid': True,
            'structure_score': 5, 'rejection_or_displacement': True,
            'vpa_confirmed': True,
        }
        q._update_confirmation(cand, 'RETEST_CONFIRMED', ('MSS_CONFIRMED', 'RETEST_CONFIRMED'), 40, 100.3, 1.0)
        self.assertEqual(cand.confirmation_count, 1)
        self.assertEqual(cand.confirmed_trigger, 'RETEST_CONFIRMED')
        q._update_state(cand, 100.3)
        self.assertEqual(cand.state, E.ExecutionState.READY)
        self.assertEqual(cand.gate_status['min_confirmations'], 1)
        self.assertEqual(cand.confirmation_state, 'CONFIRMED_1')

    def test_normal_candidate_still_requires_two_events(self):
        E = self.E
        q = E.ExecutionQueue()
        cand = E.ExecutionCandidate(
            symbol='NORMAL/USDT:USDT', side='BUY', price=100.0, entry_price=100.0,
            stop_loss=98.0, take_profit_1=102.0, take_profit_2=104.0,
            atr=1.0, df=_frame(), ob={},
        )
        cand.zone_low, cand.zone_high = 99.0, 101.0
        cand.zone_state = 'ENTRY_WINDOW'
        cand.latest_adx = 30.0
        cand.latest_adx_bounds = [16.0, 55.0]
        cand.zone_metrics = E.ZoneMetrics(
            order_block_quality=90, zone_strength=90, liquidity_quality=80,
            institutional_confidence=85, structure_alignment=85,
            entry_timing=90, trend_alignment=90, risk_score=90,
            trigger_state='MSS_CONFIRMED',
        )
        cand.evidence = {'sweep_quality': 'strong', 'structure_valid': True,
                         'structure_score': 5, 'rejection_or_displacement': True}
        q._update_confirmation(cand, 'MSS_CONFIRMED', ('MSS_CONFIRMED',), 40, 100.3, 1.0)
        q._update_state(cand, 100.3)
        self.assertNotEqual(cand.state, E.ExecutionState.READY)
        self.assertEqual(cand.gate_status['min_confirmations'], 2)
        self.assertEqual(cand.ready_blocker, 'CONFIRMATION')


if __name__ == '__main__':
    unittest.main()
