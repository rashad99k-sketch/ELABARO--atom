import unittest
from portfolio.risk import PortfolioRiskGuard

class E:
    PERF={
      'trades':3,
      'closed_trades':[
        {'symbol':'A','result':'LOSS','pnl_pct':-1},
        {'symbol':'B','result':'LOSS','pnl_pct':-2},
        {'symbol':'C','result':'WIN','pnl_pct':3},
      ]
    }
    def get_balance_safe(self): return 10000
    paper={'committed_margin':0}

class LedgerRiskTest(unittest.TestCase):
    def test_all_closed_items_processed_once(self):
        r=PortfolioRiskGuard(E())
        r.sync_closed_trades()
        self.assertEqual(r._last_seen_trade_count,3)
        self.assertEqual(r._consecutive_losses,0)  # final win resets streak
        r.sync_closed_trades()
        self.assertEqual(r._last_seen_trade_count,3)

if __name__ == '__main__': unittest.main()
