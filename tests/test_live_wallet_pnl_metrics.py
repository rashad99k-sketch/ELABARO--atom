"""Live-wallet anchored TOTAL PnL metrics.

Regression proves that in LIVE mode the dashboard TOTAL PnL (Total PnL% /
Total PnL USDT) is derived from the REAL wallet balance (the shared 10s
"balance" cache) against a baseline (INITIAL_BALANCE env, else the first live
balance), and that sync_all_states() no longer clobbers the accumulated PERF
totals with a single symbol's 30s realized PnL. PAPER mode keeps the
paper-anchored PERF totals untouched. The wallet reads are strictly read-only
with respect to the shared balance cache so the risk gate's day-start equity
snapshot is never perturbed.
"""
import os
import unittest

os.environ.setdefault("PAPER_MODE", "True")
os.environ.setdefault("BINGX_KEY", "")
os.environ.setdefault("BINGX_SECRET", "")
os.environ.setdefault("NEWS_ENABLED", "True")
os.environ.setdefault("INITIAL_BALANCE", "")

import core.engine as E  # noqa: E402


class LiveWalletPnlMetricsTest(unittest.TestCase):

    def setUp(self):
        self._saved = {
            "PAPER_MODE": E.PAPER_MODE,
            "baseline": E._live_wallet_baseline,
            "get_realized_pnl_for_symbol": E.get_realized_pnl_for_symbol,
            "fetch_position": E.fetch_position,
            "validate_position_state": E.validate_position_state,
            "PERF": dict(E.PERF),
            "INITIAL_BALANCE": os.environ.get("INITIAL_BALANCE"),
            "STATE": dict(E.STATE),
            "TRADE_STATE": dict(E.TRADE_STATE),
            "MEMORY_KEYS": {k: E.MEMORY.get(k)
                            for k in ("total_pnl", "total_pnl_pct",
                                      "position_status", "current_position",
                                      "entry", "sl", "tp")},
            "balance_cache": dict(E.CACHE.get("balance", {}) or {}),
        }
        E._live_wallet_baseline = None
        E.PAPER_MODE = True
        E.get_realized_pnl_for_symbol = lambda *a, **k: (0.0, 0.0)
        E.fetch_position = lambda *a, **k: None
        E.validate_position_state = lambda local_pos, symbol: None
        E.PERF.update({"total_pnl_pct": 0.0, "total_pnl_usdt": 0.0,
                       "trades": 0, "wins": 0, "losses": 0, "last_trade": None})
        E.STATE.clear()
        E.TRADE_STATE.clear()
        E.CACHE.pop("balance", None)
        os.environ.pop("INITIAL_BALANCE", None)

    def tearDown(self):
        E._live_wallet_baseline = self._saved["baseline"]
        E.PAPER_MODE = self._saved["PAPER_MODE"]
        E.get_realized_pnl_for_symbol = self._saved["get_realized_pnl_for_symbol"]
        E.fetch_position = self._saved["fetch_position"]
        E.validate_position_state = self._saved["validate_position_state"]
        E.PERF.clear(); E.PERF.update(self._saved["PERF"])
        E.STATE.clear(); E.STATE.update(self._saved["STATE"])
        E.TRADE_STATE.clear(); E.TRADE_STATE.update(self._saved["TRADE_STATE"])
        for k, v in self._saved["MEMORY_KEYS"].items():
            if v is None:
                E.MEMORY.pop(k, None)
            else:
                E.MEMORY[k] = v
        if self._saved["balance_cache"]:
            E.CACHE["balance"] = dict(self._saved["balance_cache"])
        else:
            E.CACHE.pop("balance", None)
        prev = self._saved["INITIAL_BALANCE"]
        if prev is None:
            os.environ.pop("INITIAL_BALANCE", None)
        else:
            os.environ["INITIAL_BALANCE"] = prev

    def _set_perf(self, pct, usdt, trades=3, wins=2, losses=1):
        E.PERF.update({"total_pnl_pct": pct, "total_pnl_usdt": usdt,
                       "trades": trades, "wins": wins, "losses": losses})

    def test_paper_metrics_use_perf_only(self):
        E.PAPER_MODE = True
        self._set_perf(0.04, 5.0, trades=4, wins=3, losses=1)
        m = E.get_dashboard_metrics()
        self.assertEqual(m["total_pnl"], "+4.00%")
        self.assertAlmostEqual(m["total_pnl_usdt"], 5.0)
        self.assertEqual(m["trades"], 4)
        self.assertEqual(m["wins"], 3)
        self.assertEqual(m["losses"], 1)
        self.assertIsNone(E.live_wallet_pnl())
        self.assertIsNone(E._live_wallet_baseline)

    def test_live_metrics_from_wallet_with_env_baseline(self):
        E.PAPER_MODE = False
        os.environ["INITIAL_BALANCE"] = "10.0"
        E.cache_set("balance", 20.7)
        self._set_perf(0.9, 90.0)
        m = E.get_dashboard_metrics()
        self.assertEqual(m["total_pnl"], "+107.00%")
        self.assertAlmostEqual(m["total_pnl_usdt"], 10.7)
        self.assertEqual(m["trades"], 3)
        self.assertEqual(E._live_wallet_baseline, 10.0)
        bal, pnl_usdt, pnl_pct = E.live_wallet_pnl()
        self.assertAlmostEqual(bal, 20.7)
        self.assertAlmostEqual(pnl_usdt, 10.7)
        self.assertAlmostEqual(pnl_pct, 107.0)

    def test_live_autocapture_baseline_on_first_wallet(self):
        E.PAPER_MODE = False
        E.cache_set("balance", 100.0)
        self._set_perf(0.0, 0.0)
        m1 = E.get_dashboard_metrics()
        self.assertEqual(m1["total_pnl"], "+0.00%")
        self.assertAlmostEqual(m1["total_pnl_usdt"], 0.0)
        E.cache_set("balance", 110.0)
        m2 = E.get_dashboard_metrics()
        self.assertEqual(m2["total_pnl"], "+10.00%")
        self.assertAlmostEqual(m2["total_pnl_usdt"], 10.0)
        self.assertEqual(E._live_wallet_baseline, 100.0)

    def test_live_fallback_to_perf_when_wallet_unavailable(self):
        E.PAPER_MODE = False
        self._set_perf(0.03, 3.0)
        m = E.get_dashboard_metrics()
        self.assertEqual(m["total_pnl"], "+3.00%")
        self.assertAlmostEqual(m["total_pnl_usdt"], 3.0)
        self.assertIsNone(E._live_wallet_baseline)

    def test_sync_no_symbol_keeps_perf_totals_in_live(self):
        E.PAPER_MODE = False
        E.fetch_position = lambda *a, **k: None
        E.get_realized_pnl_for_symbol = lambda *a, **k: (7.5, 9.0)
        self._set_perf(0.4, 40.0)
        E.sync_all_states()
        self.assertAlmostEqual(E.MEMORY["total_pnl"], 7.5)
        self.assertAlmostEqual(E.MEMORY["total_pnl_pct"], 9.0)
        self.assertAlmostEqual(E.PERF["total_pnl_usdt"], 40.0)
        self.assertAlmostEqual(E.PERF["total_pnl_pct"], 0.4)

    def test_sync_no_symbol_memory_uses_wallet_basis(self):
        E.PAPER_MODE = False
        os.environ["INITIAL_BALANCE"] = "10.0"
        E.cache_set("balance", 20.0)
        E.fetch_position = lambda *a, **k: None
        E.get_realized_pnl_for_symbol = lambda *a, **k: (7.5, 9.0)
        self._set_perf(0.4, 40.0)
        E.sync_all_states()
        self.assertAlmostEqual(E.MEMORY["total_pnl"], 10.0)
        self.assertAlmostEqual(E.MEMORY["total_pnl_pct"], 100.0)
        self.assertAlmostEqual(E.PERF["total_pnl_usdt"], 40.0)
        self.assertAlmostEqual(E.PERF["total_pnl_pct"], 0.4)

    def test_sync_open_symbol_keeps_perf_totals_in_live(self):
        E.PAPER_MODE = False
        os.environ["INITIAL_BALANCE"] = "10.0"
        E.cache_set("balance", 15.0)
        E.STATE.update({"open": True, "current_symbol": "BTC/USDT:USDT",
                        "side": "BUY", "entry": 100.0, "trade_id": "TRD-W"})
        E.validate_position_state = lambda local_pos, symbol: {"symbol": symbol, "ok": True}
        E.get_realized_pnl_for_symbol = lambda *a, **k: (3.3, 4.0)
        self._set_perf(0.4, 40.0)
        E.sync_all_states()
        self.assertEqual(E.MEMORY["position_status"], "OPEN")
        self.assertAlmostEqual(E.MEMORY["total_pnl"], 5.0)
        self.assertAlmostEqual(E.MEMORY["total_pnl_pct"], 50.0)
        self.assertAlmostEqual(E.PERF["total_pnl_usdt"], 40.0)
        self.assertAlmostEqual(E.PERF["total_pnl_pct"], 0.4)


if __name__ == "__main__":
    unittest.main()