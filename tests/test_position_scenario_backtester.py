import unittest
from backtesting.position_scenarios import ScenarioPositionBacktester

class ScenarioTest(unittest.TestCase):
    def test_profit_lock_reversal_is_measurable(self):
        bt=ScenarioPositionBacktester()
        def decide(s):
            if s['peak_roe_pct'] >= 20 and s['drawdown_roe_pct'] >= 8: return 'PROFIT_LOCK'
            return 'HOLD'
        r=bt.run('peak_then_reversal',[0,8,17,29,24,19,11,6,2,-3],decide)
        self.assertTrue(r.closed)
        self.assertEqual(r.close_index, 5)
        self.assertGreaterEqual(r.max_roe,29)
        self.assertGreaterEqual(r.max_drawdown_roe,8)

    def test_no_false_close_on_healthy_trend(self):
        bt=ScenarioPositionBacktester()
        r=bt.run('trend',[0,5,12,20,24,28,31],lambda s:'HOLD')
        self.assertFalse(r.closed)
        self.assertEqual(r.realized_roe,31)

if __name__ == '__main__': unittest.main()
