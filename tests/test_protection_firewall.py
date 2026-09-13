import unittest
from portfolio.protection_firewall import ProtectionFirewall

class ProtectionTest(unittest.TestCase):
    def test_pair_cooldown_after_loss(self):
        f=ProtectionFirewall(stoploss_limit=99, cooldown_sec=100)
        f.record_close('BTC',-2,now=1000)
        d=f.check('BTC',now=1001)
        self.assertFalse(d.allowed); self.assertIn('PAIR_COOLDOWN',d.reason)
        self.assertTrue(f.check('ETH',now=1001).allowed)

    def test_stoploss_guard(self):
        f=ProtectionFirewall(stoploss_limit=3,cooldown_sec=100)
        for i in range(3): f.record_close(f'S{i}',-1,now=1000+i)
        d=f.check('NEW',now=1004)
        self.assertFalse(d.allowed); self.assertEqual(d.reason,'STOPLOSS_GUARD')

if __name__ == '__main__': unittest.main()
