import tempfile
import unittest
from pathlib import Path
from portfolio.trade_registry import TradeRegistry, TradeRecord

class RegistryTest(unittest.TestCase):
    def test_roundtrip_and_versioning(self):
        with tempfile.TemporaryDirectory() as d:
            reg=TradeRegistry(Path(d)/'state.json')
            rec=TradeRecord('T1','BTC/USDT:USDT','BUY',client_order_id=TradeRegistry.client_order_id('T1'))
            reg.upsert(rec)
            v=reg.get('T1').version
            reg.update('T1',status='OPEN',tp1_done=True,peak_roe=30)
            got=reg.get('T1')
            self.assertGreater(got.version,v)
            self.assertTrue(got.tp1_done)
            reg2=TradeRegistry(Path(d)/'state.json')
            self.assertEqual(reg2.get('T1').peak_roe,30)
            self.assertEqual(len(reg2.active()),1)

    def test_client_id_is_stable_and_bingx_safe(self):
        a=TradeRegistry.client_order_id('TRD-123','OPEN','MAIN')
        b=TradeRegistry.client_order_id('TRD-123','OPEN','MAIN')
        self.assertEqual(a,b); self.assertLessEqual(len(a),40); self.assertTrue(a.isalnum())

if __name__ == '__main__': unittest.main()
