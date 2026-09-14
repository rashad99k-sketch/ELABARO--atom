"""Dashboard Trades/Wins/Losses/WinRate cards come from the live PERF tally.

Regression: the cards were sourced from DASHBOARD_STATE["stats"], which is only
written by update_stats() -> update_pnl_and_learning(), a path with NO callers
in the current engine — so the cards stayed frozen at 0 even while real trades
completed. The /data payload now sources the cards from PERF, which
finalize_trade_with_reality() increments per completed trade.
"""
import os
import unittest

os.environ.setdefault("PAPER_MODE", "True")
os.environ.setdefault("BINGX_KEY", "")
os.environ.setdefault("BINGX_SECRET", "")
os.environ.setdefault("NEWS_ENABLED", "True")

import core.engine as E  # noqa: E402


class DashboardStatsAliveTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.dashboard = __import__("dashboard.app", fromlist=["app"])
        if not hasattr(cls.dashboard.app, "test_client"):
            raise unittest.SkipTest("dashboard app not importable in this harness")

    def setUp(self):
        self._perf = dict(E.PERF)
        self._dash_stats = dict(E.DASHBOARD_STATE.get("stats", {}))
        self._account = dict(E.DASHBOARD_STATE.get("account", {}))
        self._last_refresh = E.DASHBOARD_STATE.get("last_live_refresh")
        self._dash_cache = E.CACHE.pop("dashboard", None)
        # Frost the legacy structure differently from PERF to prove the cards
        # follow PERF, not the frozen DASHBOARD_STATE tally.
        E.DASHBOARD_STATE["stats"].update(
            {"trades": 7, "wins": 9, "losses": 2, "win_rate": 99.9})
        E.PERF.update({"trades": 5, "wins": 3, "losses": 2})

    def tearDown(self):
        E.PERF.clear(); E.PERF.update(self._perf)
        E.DASHBOARD_STATE["stats"].clear()
        E.DASHBOARD_STATE["stats"].update(self._dash_stats)
        E.DASHBOARD_STATE["account"].clear()
        E.DASHBOARD_STATE["account"].update(self._account)
        if self._last_refresh is None:
            E.DASHBOARD_STATE.pop("last_live_refresh", None)
        else:
            E.DASHBOARD_STATE["last_live_refresh"] = self._last_refresh
        if self._dash_cache is not None:
            E.CACHE["dashboard"] = self._dash_cache

    def test_stats_cards_reflect_perf_not_frozen_dashboard_state(self):
        with self.dashboard.app.test_client() as client:
            body = client.get("/data").get_json()
        self.assertNotIn("error", body)
        stats = body["stats"]
        self.assertEqual(stats["trades"], 5)
        self.assertEqual(stats["wins"], 3)
        self.assertEqual(stats["losses"], 2)
        self.assertAlmostEqual(stats["win_rate"], 60.0, places=4)
        legacy = self._dash_stats or {}
        self.assertNotEqual(stats["trades"], legacy.get("trades", 0))
        self.assertNotEqual(stats["win_rate"], 99.9)

    def test_zero_trades_yields_zero_winrate_not_divide_by_zero(self):
        E.PERF.update({"trades": 0, "wins": 0, "losses": 0})
        with self.dashboard.app.test_client() as client:
            body = client.get("/data").get_json()
        stats = body["stats"]
        self.assertEqual(stats["trades"], 0)
        self.assertEqual(stats["win_rate"], 0.0)


if __name__ == "__main__":
    unittest.main()