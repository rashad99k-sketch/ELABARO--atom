import os
import time
import unittest

from portfolio.risk import PortfolioRiskGuard


class FakeRiskEngine:
    def __init__(self):
        self.PERF = {"trades": 0, "last_trade": None}
        self.balance = 1000.0
        self.paper = {"balance": 1000.0, "committed_margin": 0.0}

    def get_balance_safe(self):
        return self.balance


class PortfolioRiskGuardTest(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        os.environ["POSITION_MARGIN_PCT"] = "0.10"
        os.environ["PORTFOLIO_MARGIN_CAP_PCT"] = "0.60"
        os.environ["MAX_DAILY_LOSS_PCT"] = "5"
        os.environ["MAX_CONSECUTIVE_LOSSES"] = "3"
        os.environ["COOLDOWN_MINUTES_LOSS"] = "1"
        os.environ["COOLDOWN_MINUTES_DRAWDOWN"] = "2"

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    def test_margin_cap_blocks_seventh_position(self):
        e = FakeRiskEngine()
        guard = PortfolioRiskGuard(e)
        self.assertTrue(guard.can_open(current_positions=5))
        self.assertFalse(guard.can_open(current_positions=6))

    def test_daily_drawdown_blocks_new_entries(self):
        e = FakeRiskEngine()
        guard = PortfolioRiskGuard(e)
        self.assertTrue(guard.can_open(current_positions=0))
        e.balance = 940.0
        status = guard.status(current_positions=0)
        self.assertFalse(status.allowed)
        self.assertEqual(status.reason, "DAILY_DRAWDOWN_LIMIT")

    def test_three_losses_arm_longer_cooldown(self):
        e = FakeRiskEngine()
        guard = PortfolioRiskGuard(e)
        guard.status(current_positions=0)
        for n in range(1, 4):
            e.PERF["trades"] = n
            e.PERF["last_trade"] = {"result": "LOSS", "pnl_pct": -1.0}
            guard.sync_closed_trades()
        status = guard.status(current_positions=0)
        self.assertFalse(status.allowed)
        self.assertEqual(status.reason, "GLOBAL_LOSS_COOLDOWN")
        self.assertEqual(status.consecutive_losses, 3)
        self.assertGreater(status.cooldown_until, time.time())

    def test_committed_margin_not_treated_as_drawdown(self):
        # Opening positions moves free balance into committed margin. This is
        # NOT a loss, so it must not trip the DAILY_DRAWDOWN_LIMIT. Equity is
        # defined as free balance + committed margin.
        e = FakeRiskEngine()
        guard = PortfolioRiskGuard(e)
        self.assertTrue(guard.can_open(current_positions=0))
        # Simulate 6 positions, each committing 10% of free balance
        for _ in range(6):
            margin = e.paper["balance"] * 0.10
            e.paper["balance"] -= margin
            e.paper["committed_margin"] += margin
            e.balance = e.paper["balance"]  # get_balance_safe reflects free balance
        # All 6 fits within the 60% portfolio margin cap
        self.assertTrue(guard.can_open(current_positions=5))
        self.assertFalse(guard.can_open(current_positions=6))
        # Even though free balance dropped heavily, it is not a drawdown:
        status = guard.status(current_positions=5)
        self.assertTrue(status.allowed, "committed margin must not count as loss")
        self.assertEqual(status.reason, "OK")


class PaperEquityIgnoresStaleBalanceCacheTest(unittest.TestCase):
    """Regression: risk._equity() must read the paper ledger directly, never the
    cached get_balance_safe() value. A stale 10s balance cache combined with a
    moving free balance produced wild drawdown readings (42% on a flat book),
    spuriously blocking can_open after every position was closed."""

    class PaperEngine:
        PAPER_MODE = True
        PERF = {"trades": 0, "last_trade": None}

        def __init__(self):
            self.paper = {"balance": 10000.0, "committed_margin": 0.0}
            self._cached = 5763.0  # adversarial stale cache (old low reading)

        def get_balance_safe(self):
            return self._cached

    def setUp(self):
        self._env = dict(os.environ)
        os.environ["POSITION_MARGIN_PCT"] = "0.10"
        os.environ["PORTFOLIO_MARGIN_CAP_PCT"] = "0.60"
        os.environ["MAX_DAILY_LOSS_PCT"] = "20"
        os.environ["MAX_CONSECUTIVE_LOSSES"] = "3"

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    def _after_full_cycle(self, balance, committed):
        e = self.PaperEngine()
        e.paper = {"balance": balance, "committed_margin": committed}
        guard = PortfolioRiskGuard(e)
        guard.status(current_positions=0)
        return guard, e

    def test_stale_cached_balance_never_distorts_paper_equity(self):
        # Real paper books after opening six positions then closing them all:
        # free + committed reconcile to ~start + realized PnL (9658.35). The
        # stale cache claim of 5763 must be ignored entirely in PAPER_MODE.
        guard, e = self._after_full_cycle(9658.35, 0.0)
        self.assertEqual(guard._equity(), 9658.35)
        status = guard.status(current_positions=0)
        self.assertTrue(status.allowed)
        self.assertEqual(status.reason, "OK")

    def test_committed_cash_still_counts_in_paper_equity(self):
        guard, e = self._after_full_cycle(9200.0, 700.0)
        self.assertEqual(guard._equity(), 9900.0)

    def test_live_path_keeps_cached_balance_behavior(self):
        # Without PAPER_MODE the guard still reads get_balance_safe() so live
        # exchange calls stay cached (rate-safety unchanged) -- plus committed.
        class LiveEngine(PaperEquityIgnoresStaleBalanceCacheTest.PaperEngine):
            PAPER_MODE = False

        e = LiveEngine()
        e.paper["committed_margin"] = 0.0
        guard = PortfolioRiskGuard(e)
        guard.status(current_positions=0)
        self.assertEqual(guard._equity(), e._cached)


if __name__ == "__main__":
    unittest.main()
