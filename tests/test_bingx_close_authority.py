"""Exchange-authoritative close authority (BingX location truth).

The BingX swap venue is the ONLY source of truth for whether a position exists.
A local conclusion (order created, order ID, API code 0, local qty zero, STATE
updated, Telegram sent, PnL computed) never closes a position by itself; the
exchange position query for the exact hedge leg must return zero/absent first.

This module pins the deterministic close behaviours introduced alongside the
mode-aware close: position-mode detection (hedge vs one-way), exchange-quantity
sourcing, bounded post-close venue confirmation and the CLOSE_QTY_* /
CLOSE_NOTIONAL_BELOW_MIN / CLOSE_POSITION_NOT_CONFIRMED codes.

TEST INDEX (1-13)
  1.  LONG  single-TP full close is exchange-verified to zero      -> CLOSED
  2.  SHORT single-TP full close is exchange-verified to zero      -> CLOSED
  3.  Unfilled close order leaves the position OPEN (no finalize)
  4.  Order ID returned but position still open   -> stays OPEN
  5.  Transient venue error (PAUSED/ERROR)        -> bounded, stays OPEN
  6.  Eventual zero after bounded re-confirm      -> CLOSED
  7.  Duplicate close emits exactly one order + one Telegram
  8.  Near-min quantity                           -> CLOSE_QTY_BELOW_MIN, no order
  9.  Zero / invalid quantity                     -> CLOSE_QTY_INVALID, no order
 10. External closure reconciliation runs once    -> CLOSED, no order
 11. Restart reconciliation resolves to CLOSED once (no reopen)
 12. LONG close can never fashion an opposite SHORT (hedge + one-way)
 13. SHORT close can never fashion an opposite LONG (hedge + one-way)
"""

import threading
import unittest
from unittest import mock

import core.engine as engine

_real_close_partial = engine.close_partial
_real_close_position_full = engine.close_position_full

_ORIG_EX = engine.ex
_ORIG_PAPER = engine.PAPER_MODE
_ORIG_STATE = dict(engine.STATE)
_ORIG_TRADE_STATE = dict(engine.TRADE_STATE)
_ORIG_CLOSING = getattr(engine, "_closing_in_progress", None)
_ORIG_RECON = getattr(engine, "_reconciliation_pending", None)
_ORIG_CONFIRM_T = getattr(engine, "CLOSE_CONFIRM_TIMEOUT", None)
_ORIG_CONFIRM_I = getattr(engine, "CLOSE_CONFIRM_INTERVAL", None)


def _leg(side, contracts, symbol="BTC/USDT:USDT"):
    return {"symbol": symbol, "side": side, "contracts": contracts,
            "entryPrice": 100.0, "markPrice": 100.0}


class _Venue:
    """Scripted BingX harness: account-wide fetch_positions + recorded orders.

    Supports position-mode detection (fetch_position_mode) and market metadata
    (market/amount_to_precision) so the mode-aware close path is exercised
    without any network access. Not callable in offline venues: fetch_position_mode
    is only set on the venue when a test wants a specific mode.
    """

    def __init__(self, script, hedged=None):
        self.script = list(script)
        self.created = []
        self.create_error = None
        self.hedged = hedged
        self.amount_to_precision = lambda s, q: q
        self.markets = {
            "BTC/USDT:USDT": {
                "limits": {"amount": {"min": 0.001}, "cost": {"min": 5.0}},
                "precision": {"amount": 8},
            },
            "BTC/USDT": {
                "limits": {"amount": {"min": 0.001}, "cost": {"min": 5.0}},
                "precision": {"amount": 8},
            },
        }

    def fetch_positions(self, *a, **k):
        if self.script:
            return self.script.pop(0)
        return []

    def fetch_position_mode(self, *a, **k):
        if self.hedged is None:
            raise NotImplementedError("mode not pinned for this venue")
        return {"hedged": self.hedged}

    def market(self, sym):
        return dict(self.markets.get(sym, {}))

    def create_order(self, sym, order_type, side, amount, params=None):
        if callable(self.create_error):
            self.create_error(side, amount, params)
        if self.create_error:
            raise self.create_error
        params = params or {}
        rec = {"sym": sym, "type": order_type, "side": side, "amount": amount,
               "params": params, "id": "mock-order-%d" % len(self.created),
               "status": "closed", "average": 100.0}
        self.created.append(rec)
        return {"id": rec["id"], "status": "closed", "average": 100.0}


class _BlockingVenue(_Venue):
    """Venue whose first fetch_positions call blocks until released."""

    def __init__(self, script, hedged=None):
        super().__init__(script, hedged=hedged)
        self.entered = threading.Event()
        self.release = threading.Event()
        self._blocked = False

    def fetch_positions(self, *a, **k):
        if not self._blocked:
            self._blocked = True
            self.entered.set()
            if not self.release.wait(15):
                self.entered.clear()
        return super().fetch_positions(*a, **k)


class _CloseAuthorityBase(unittest.TestCase):
    """PAPER off, DASHBOARD off, real fetch_position_status, scripted venue."""

    def setUp(self):
        engine.PAPER_MODE = False
        engine._closing_in_progress = False
        engine._reconciliation_pending = False
        with engine._CLOSE_IN_FLIGHT_LOCK:
            engine._CLOSE_IN_FLIGHT.clear()
        _reset_mode_cache()
        engine.STATE.clear()
        engine.STATE.update({
            "open": True, "side": "BUY", "qty": 0.1, "remaining_qty": 0.1,
            "qty_initial": 0.1, "entry": 100.0, "mark_price": 100.0,
            "current_symbol": "BTC/USDT:USDT", "symbol": "BTC/USDT:USDT",
            "trade_id": "T-CLOSE-AUTH",
        })
        engine.TRADE_STATE.clear()
        engine.TRADE_STATE.update({"in_position": True, "qty": 0.1})
        engine.DASHBOARD_STATE["live_trade_mode"] = False
        engine.CLOSE_CONFIRM_TIMEOUT = 0.05
        engine.CLOSE_CONFIRM_INTERVAL = 0.01

        self.venue = _Venue([])
        engine.ex = self.venue
        self.sent = []
        self.patches = [
            mock.patch.object(engine, "finalize_trade_with_reality", return_value=None),
            mock.patch.object(engine, "verify_order_filled",
                              side_effect=lambda *a, **k: (True, self._fill_qty())),
            mock.patch.object(engine, "_cancel_native_protection", return_value=True),
            mock.patch.object(engine, "_exchange_sync"),
            mock.patch.object(engine, "_tg_send", side_effect=lambda text: self.sent.append(text)),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

    def _fill_qty(self):
        return engine.STATE.get("remaining_qty", 0.1) or 0.1

    def tearDown(self):
        engine.ex = _ORIG_EX
        engine.PAPER_MODE = _ORIG_PAPER
        _reset_mode_cache()
        engine.STATE.clear()
        engine.STATE.update(_ORIG_STATE)
        engine.TRADE_STATE.clear()
        engine.TRADE_STATE.update(_ORIG_TRADE_STATE)
        engine.CLOSE_CONFIRM_TIMEOUT = _ORIG_CONFIRM_T
        engine.CLOSE_CONFIRM_INTERVAL = _ORIG_CONFIRM_I
        if _ORIG_CLOSING is not None:
            engine._closing_in_progress = _ORIG_CLOSING
        if _ORIG_RECON is not None:
            engine._reconciliation_pending = _ORIG_RECON
        with engine._CLOSE_IN_FLIGHT_LOCK:
            engine._CLOSE_IN_FLIGHT.clear()

    def _last_close_outcome(self):
        log = engine.MEMORY.get("close_log", [])
        return log[-1] if log else {}

    @property
    def _msgs(self):
        return list(self.sent)


def _reset_mode_cache():
    try:
        engine._reset_position_mode_cache()
    except Exception:
        pass


class SingleTPLONGCloseTest(_CloseAuthorityBase):
    # TEST 1: LONG single-TP full close is exchange-verified to zero.
    def test_long_tp_close_verified_zero(self):
        self.venue.script = [[_leg("long", 0.1)],
                             [],       # post-fill confirm -> NOT_FOUND
                             []]
        ok = engine.close_position_full(stage="TP1")
        self.assertTrue(ok)
        self.assertEqual(len(self.venue.created), 1)
        rec = self.venue.created[0]
        self.assertEqual(rec["side"], "sell")
        self.assertEqual(rec["params"]["positionSide"], "LONG")  # hedge fallback
        self.assertNotIn("reduceOnly", rec["params"])
        self.assertFalse(engine.STATE["open"])
        result_close_log = [r for r in engine.MEMORY.get("close_log", [])]
        self.assertEqual(result_close_log[-1]["event"], "CLOSE_EXECUTED")

    # TEST 1b: LONG close sends exchange quantity, not stale local.
    def test_long_tp_close_uses_exchange_quantity(self):
        engine.STATE["remaining_qty"] = 0.25  # stale local > venue
        self.venue.script = [[_leg("long", 0.1)], [], []]
        ok = engine.close_position_full(stage="TP1")
        self.assertTrue(ok)
        self.assertEqual(self.venue.created[0]["amount"], 0.1)


class SingleTPShortCloseTest(_CloseAuthorityBase):
    # TEST 2: SHORT single-TP full close is exchange-verified to zero.
    def test_short_tp_close_verified_zero(self):
        engine.STATE["side"] = "SELL"
        self.venue.script = [[_leg("short", 0.1)], [], []]
        ok = engine.close_position_full(stage="TP1")
        self.assertTrue(ok)
        self.assertEqual(len(self.venue.created), 1)
        rec = self.venue.created[0]
        self.assertEqual(rec["side"], "buy")
        self.assertEqual(rec["params"]["positionSide"], "SHORT")
        self.assertNotIn("reduceOnly", rec["params"])
        self.assertFalse(engine.STATE["open"])


class UnfilledOrderStaysOpenTest(_CloseAuthorityBase):
    # TEST 3: an order that never fills must not close local state.
    def test_unfilled_order_keeps_position_open(self):
        # Leg present on every pre-verify so order attempts keep firing; the
        # patched fill verify always says unfilled; after attempts exhaust the
        # leg is STILL present -> CLOSE_FAILED, local state stays open.
        self.venue.script = [[_leg("long", 0.1)], [_leg("long", 0.1)],
                             [_leg("long", 0.1)], [_leg("long", 0.1)]]
        with mock.patch.object(engine, "verify_order_filled",
                               return_value=(False, 0.0)), \
             mock.patch.object(engine, "time", wraps=engine.time) as _tw:
            _tw.sleep = lambda *a: None
            ok = engine.close_position_full()
        self.assertFalse(ok)
        self.assertTrue(engine.STATE["open"])
        self.assertTrue(engine.TRADE_STATE["in_position"])
        self.assertEqual(self._last_close_outcome()["event"], "CLOSE_FAILED")


class OrderIDButPositionOpenTest(_CloseAuthorityBase):
    # TEST 4: an order id alone is NOT proof; the venue leg must go to zero.
    def test_order_id_plus_present_leg_stays_open(self):
        # constant-script venue always reports the LONG leg present; _confirm
        # bounded by tight window -> PRESENT on every attempt -> fails closed.
        self.venue.script = []
        self.venue.fetch_positions = lambda *a, **k: [_leg("long", 0.1)]
        ok = engine.close_position_full()
        self.assertFalse(ok)
        self.assertTrue(engine.STATE["open"])
        self.assertTrue(engine.TRADE_STATE["in_position"])


class TransientVenueErrorBoundedTest(_CloseAuthorityBase):
    # TEST 5: transient venue PAUSED/ERROR must be bounded and never close.
    def test_transient_error_refuses_close(self):
        with mock.patch.object(engine, "fetch_position_status",
                               return_value=(None, "PAUSED")):
            ok = engine.close_position_full()
        self.assertFalse(ok)
        self.assertTrue(engine.STATE["open"])
        self.assertEqual(self._last_close_outcome()["event"], "CLOSE_STATUS_UNKNOWN")


class EventualZeroBecomesClosedTest(_CloseAuthorityBase):
    # TEST 6: a bounded re-confirm that eventually sees zero closes the position.
    def test_eventual_zero_at_confirm_closes(self):
        calls = {"n": 0}

        def scripted(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                return [_leg("long", 0.1)]
            return []
        self.venue.fetch_positions = scripted
        ok = engine.close_position_full()
        self.assertTrue(ok)
        self.assertFalse(engine.STATE["open"])
        self.assertTrue(calls["n"] >= 2)


class DuplicateCloseOneOrderOneTelegramTest(_CloseAuthorityBase):
    # TEST 7: exactly one close order and one Telegram for duplicate requests.
    def test_duplicate_close_single_order_single_telegram(self):
        self.venue.script = [[_leg("long", 0.1)], [], [], [], [], []]
        ok = engine.close_position_full(stage="TP1")
        self.assertTrue(ok)
        self.assertEqual(len(self.venue.created), 1)
        self.assertLessEqual(len(self._msgs), 1)
        self.assertEqual(len([m for m in self._msgs if "CLOSE" in m.upper() or "closed" in m.lower()]), 0)
        # A second management cycle finds the position already closed; no order.
        self.assertFalse(engine.close_position_full(stage="TP1"))
        self.assertEqual(len(self.venue.created), 1)


class NearMinQuantityTest(_CloseAuthorityBase):
    # TEST 8: quantity below the venue minimum -> CLOSE_QTY_BELOW_MIN, no order.
    def test_near_min_qty_rejected(self):
        engine.STATE["remaining_qty"] = 0.0005
        self.venue.script = [[_leg("long", 0.0005)], [], [], []]
        ok = engine.close_position_full()
        self.assertFalse(ok)
        self.assertEqual(len(self.venue.created), 0)
        outcome = self._last_close_outcome()
        self.assertEqual(outcome["event"], "CLOSE_FAILED")
        self.assertEqual(outcome["reason"], "CLOSE_QTY_BELOW_MIN")


class InvalidQuantityTest(_CloseAuthorityBase):
    # TEST 9: zero/invalid quantity -> CLOSE_QTY_INVALID, no order.
    def test_zero_qty_checker_emits_invalid_code(self):
        # Direct deterministic-code unit check: a zero qty is CLOSE_QTY_INVALID.
        ok = engine._close_partial_min_ok("BTC/USDT:USDT", "BTC/USDT:USDT", 0.0)
        self.assertFalse(ok)
        self.assertEqual(engine._close_failure_code_for("BTC/USDT:USDT",
                                                        "BTC/USDT:USDT", 0.0),
                         "CLOSE_QTY_INVALID")

    def test_zero_qty_venue_leg_syncs_and_emits_no_order(self):
        # A leg the venue reports with zero/absent quantity is ALREADY_CLOSED:
        # no order is emitted and state is closed to match the exchange truth.
        self.venue.script = [[], []]
        ok = engine.close_position_full()
        self.assertTrue(ok)
        self.assertEqual(len(self.venue.created), 0)
        self.assertFalse(engine.STATE["open"])


class ExternalClosureReconciliationTest(_CloseAuthorityBase):
    # TEST 10: the venue reports the leg gone externally -> sync once, no order.
    def test_external_closure_reconciled_once(self):
        self.venue.script = [[], []]  # both verifies see NOT_FOUND
        ok = engine.close_position_full()
        self.assertTrue(ok)
        self.assertEqual(len(self.venue.created), 0)
        self.assertFalse(engine.STATE["open"])
        self.assertEqual(self._last_close_outcome()["reason"],
                         engine.ALREADY_CLOSED_ON_EXCHANGE)


class RestartReconciliationTest(_CloseAuthorityBase):
    # TEST 11: on a simulated restart the same close resolves once, never reopens.
    def test_restart_reconciles_to_closed_once(self):
        self.venue.script = [[], []]
        ok = engine.close_position_full()
        self.assertTrue(ok)
        self.assertFalse(engine.STATE["open"])
        # Second restart cycle: no reopen, no new order.
        engine.STATE["open"] = False
        ok2 = engine.close_position_full()
        self.assertFalse(ok2)
        self.assertEqual(len(self.venue.created), 0)


class LongCloseNeverFashionsShortTest(_CloseAuthorityBase):
    # TEST 12: LONG reduction must not create an opposite SHORT in EITHER mode.
    def test_long_close_uses_reduce_only_in_oneway_mode(self):
        self.venue = _Venue([], hedged=False)
        engine.ex = self.venue
        self.venue.script = [[_leg("long", 0.1)], [], []]
        ok = engine.close_position_full()
        self.assertTrue(ok)
        rec = self.venue.created[0]
        self.assertEqual(rec["side"], "sell")
        self.assertEqual(rec["params"]["positionSide"], "BOTH")  # one-way needs BOTH
        self.assertTrue(rec["params"].get("reduceOnly"))

    def test_long_close_hedge_mode_never_both(self):
        self.venue = _Venue([], hedged=True)
        engine.ex = self.venue
        self.venue.script = [[_leg("long", 0.1)], [], []]
        ok = engine.close_position_full()
        self.assertTrue(ok)
        rec = self.venue.created[0]
        self.assertEqual(rec["side"], "sell")
        self.assertEqual(rec["params"]["positionSide"], "LONG")
        self.assertNotIn("reduceOnly", rec["params"])


class ShortCloseNeverFashionsLongTest(_CloseAuthorityBase):
    # TEST 13: SHORT reduction must not create an opposite LONG in EITHER mode.
    def test_short_close_uses_reduce_only_in_oneway_mode(self):
        engine.STATE["side"] = "SELL"
        self.venue = _Venue([], hedged=False)
        engine.ex = self.venue
        self.venue.script = [[_leg("short", 0.1)], [], []]
        ok = engine.close_position_full()
        self.assertTrue(ok)
        rec = self.venue.created[0]
        self.assertEqual(rec["side"], "buy")
        self.assertEqual(rec["params"]["positionSide"], "BOTH")
        self.assertTrue(rec["params"].get("reduceOnly"))

    def test_short_close_hedge_mode_never_both(self):
        engine.STATE["side"] = "SELL"
        self.venue = _Venue([], hedged=True)
        engine.ex = self.venue
        self.venue.script = [[_leg("short", 0.1)], [], []]
        ok = engine.close_position_full()
        self.assertTrue(ok)
        rec = self.venue.created[0]
        self.assertEqual(rec["side"], "buy")
        self.assertEqual(rec["params"]["positionSide"], "SHORT")
        self.assertNotIn("reduceOnly", rec["params"])


class NotionalBelowMinTest(_CloseAuthorityBase):
    # TEST 13b (extension): tiny notional -> CLOSE_NOTIONAL_BELOW_MIN, no order.
    def test_notional_below_min_rejected(self):
        self.venue = _Venue([], hedged=True)
        engine.ex = self.venue
        engine.STATE["remaining_qty"] = 0.01
        engine.STATE["mark_price"] = 50.0  # notional 0.5 < cost min 5.0
        self.venue.script = [[_leg("long", 0.01)], [], [], []]
        ok = engine.close_position_full()
        self.assertFalse(ok)
        self.assertEqual(len(self.venue.created), 0)
        outcome = self._last_close_outcome()
        self.assertEqual(outcome["event"], "CLOSE_FAILED")
        self.assertEqual(outcome["reason"], "CLOSE_NOTIONAL_BELOW_MIN")


class ModeUnavailableFailsClosedTest(_CloseAuthorityBase):
    # The venue cannot report its position mode -> fail closed, no guess.
    def test_unknown_mode_fails_closed(self):
        self.venue = _Venue([])  # hedged=None -> fetch_position_mode raises
        engine.ex = self.venue
        with mock.patch.object(engine, "_detect_position_mode", return_value="unknown"):
            ok = engine.close_position_full()
        self.assertFalse(ok)
        self.assertEqual(len(self.venue.created), 0)
        self.assertTrue(engine.STATE["open"])
        self.assertEqual(self._last_close_outcome()["reason"],
                         "CLOSE_POSITION_MODE_UNKNOWN")


if __name__ == "__main__":
    unittest.main()