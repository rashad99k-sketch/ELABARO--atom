import unittest
from unittest.mock import patch
import numpy as np
import pandas as pd
import core.engine as E


def _df(n=100, base=100.0):
    t=np.arange(n)
    x=base + 0.02*np.sin(t/4)
    return pd.DataFrame({
        'timestamp':t,'open':x-0.03,'high':x+0.05,
        'low':x-0.05,'close':x,'volume':1000.0
    })


class CowboySurgicalEntryTest(unittest.TestCase):
    def _run(self, side='BUY', zone_low=99.0, zone_high=101.0,
             sweep=True, structure=True, retest=True, reaction=True):
        df=_df()
        evidence={
            'liquidity_sweep': sweep,
            'structure_bos': structure,
            'structure_mss': False,
            'zone': {'score': 80, 'low': zone_low, 'high': zone_high, 'in_zone': True},
            'fvg': False,
            'displacement': reaction,
            'retest': 'RETEST_CONFIRMED' if retest else 'NONE',
            'distance_atr': 0.0,
        }
        ti={'valid': True, 'score': 80, 'timing': 'RETEST_CONFIRMED', 'distance_atr': 0.0, 'evidence': evidence}
        with patch.object(E, 'TRADE_INTELLIGENCE_AVAILABLE', True), \
             patch.object(E, 'analyze_setup', return_value=ti), \
             patch.object(E, 'classify_move_maturity', return_value='EARLY_EXPANSION'), \
             patch.object(E, 'compute_atr', return_value=pd.Series([1.0]*len(df))), \
             patch.object(E, 'compute_adx', return_value=pd.Series([25.0]*len(df))), \
             patch.object(E, 'classify_volume', return_value='normal'), \
             patch.object(E, 'candle_rejection', return_value=reaction), \
             patch.object(E.RFEngine, 'compute', return_value={'signal': side}), \
             patch.object(E, 'detect_displacement', return_value=reaction):
            return E.check_institutional_entry('TEST/USDT:USDT', side, df, {}, 1.0, 100.0)

    def test_buy_local_cowboy_setup_passes(self):
        ok, cls, reason = self._run('BUY')
        self.assertTrue(ok, reason); self.assertEqual(cls, 'INSTITUTIONAL_SNIPER')
        self.assertIn('LIQUIDITY_SWEEP', reason)

    def test_sell_local_cowboy_setup_passes(self):
        ok, cls, reason = self._run('SELL')
        self.assertTrue(ok, reason); self.assertEqual(cls, 'INSTITUTIONAL_SNIPER')

    def test_off_zone_rejects(self):
        ok, _, reason = self._run('BUY', zone_low=105.0, zone_high=106.0)
        self.assertFalse(ok); self.assertIn('zone too far', reason)


    def test_fallback_displacement_can_complete_sequence(self):
        # TradeIntelligence may omit its displacement flag even when the
        # deterministic engine detector confirms displacement. The Cowboy
        # gate must evaluate that fallback before rejecting the local zone.
        df=_df()
        evidence={
            'liquidity_sweep': True,
            'structure_bos': True,
            'structure_mss': False,
            'zone': {'score': 80, 'low': 99.0, 'high': 101.0, 'in_zone': True},
            'fvg': False,
            'displacement': False,
            'retest': 'NONE',
            'distance_atr': 0.0,
        }
        ti={'valid': True, 'score': 80, 'timing': 'WAIT_RETEST', 'distance_atr': 0.0, 'evidence': evidence}
        with patch.object(E, 'TRADE_INTELLIGENCE_AVAILABLE', True), \
             patch.object(E, 'analyze_setup', return_value=ti), \
             patch.object(E, 'classify_move_maturity', return_value='EARLY_EXPANSION'), \
             patch.object(E, 'compute_atr', return_value=pd.Series([1.0]*len(df))), \
             patch.object(E, 'compute_adx', return_value=pd.Series([25.0]*len(df))), \
             patch.object(E, 'classify_volume', return_value='normal'), \
             patch.object(E, 'candle_rejection', return_value=False), \
             patch.object(E.RFEngine, 'compute', return_value={'signal': 'BUY'}), \
             patch.object(E, 'detect_displacement', return_value=True):
            ok, cls, reason = E.check_institutional_entry('TEST/USDT:USDT', 'BUY', df, {}, 1.0, 100.0)
        self.assertTrue(ok, reason)
        self.assertEqual(cls, 'INSTITUTIONAL_SNIPER')
        self.assertIn('DISPLACEMENT', reason)

    def test_missing_retest_and_local_response_rejects(self):
        ok, _, reason = self._run('BUY', retest=False, reaction=False)
        self.assertFalse(ok); self.assertIn('Waiting for zone retest', reason)

    def test_missing_sweep_rejects(self):
        ok, _, reason = self._run('BUY', sweep=False)
        self.assertFalse(ok); self.assertIn('No recent directional liquidity sweep', reason)

    def test_missing_structure_rejects(self):
        ok, _, reason = self._run('SELL', structure=False)
        self.assertFalse(ok); self.assertIn('No MSS/BOS after liquidity event', reason)

if __name__ == '__main__':
    unittest.main()
