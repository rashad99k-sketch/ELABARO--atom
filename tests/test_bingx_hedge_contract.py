import os
import unittest
from unittest.mock import Mock

from portfolio.native_protection import NativeProtectionManager


class BingXHedgeContractTest(unittest.TestCase):
    def test_native_sl_omits_reduce_only_in_hedge_mode(self):
        ex = Mock()
        ex.create_order.return_value = {"id": "sl-1", "status": "open"}
        ex.fetch_order.return_value = {"id": "sl-1", "status": "open"}
        mgr = NativeProtectionManager(ex, lambda *a, **k: None)
        mgr.enabled = True
        result = mgr.place("BTC/USDT:USDT", "BUY", 0.1, 60000.0, "LONG")
        self.assertEqual(result["status"], "PROTECTED")
        params = ex.create_order.call_args.args[5]
        self.assertEqual(params["positionSide"], "LONG")
        self.assertNotIn("reduceOnly", params)


    def test_native_sl_fails_closed_when_exchange_verification_rejects(self):
        ex = Mock()
        ex.create_order.return_value = {"id": "sl-2", "status": "open"}
        ex.fetch_order.return_value = {"id": "sl-2", "status": "rejected"}
        mgr = NativeProtectionManager(ex, lambda *a, **k: None)
        mgr.enabled = True
        result = mgr.place("BTC/USDT:USDT", "BUY", 0.1, 60000.0, "LONG")
        self.assertEqual(result["status"], "UNPROTECTED")
        self.assertEqual(result["reason"], "ORDER_STATUS_REJECTED")
        self.assertEqual(mgr.status("BTC/USDT:USDT"), "UNPROTECTED")

    def test_native_sl_fails_closed_without_order_verifier(self):
        class NoVerifierExchange:
            def create_order(self, *args, **kwargs):
                return {"id": "sl-3", "status": "open"}
        mgr = NativeProtectionManager(NoVerifierExchange(), lambda *a, **k: None)
        mgr.enabled = True
        result = mgr.place("BTC/USDT:USDT", "BUY", 0.1, 60000.0, "LONG")
        self.assertEqual(result["status"], "UNPROTECTED")
        self.assertEqual(result["reason"], "NO_ORDER_VERIFIER")

    def test_stable_trade_client_id_is_bingx_safe(self):
        import core.engine as engine
        cid1 = engine._stable_client_order_id("TRD-123", "OPEN", "MAIN")
        cid2 = engine._stable_client_order_id("TRD-123", "OPEN", "MAIN")
        self.assertEqual(cid1, cid2)
        self.assertLessEqual(len(cid1), 40)
        self.assertRegex(cid1, r"^[A-Za-z0-9]+$")


if __name__ == "__main__":
    unittest.main()
