"""Verified close lifecycle (BingX 101205 hardening).

Every close order in LIVE mode must be preceded by an Exchange Position Verify
for the exact hedge leg being closed. Scenarios covered here:

  A) position exists          -> close order -> fill verify -> position verify -> closed
  B) position vanished        -> State Sync ALREADY_CLOSED_ON_EXCHANGE only (no order loop)
  C) BingX 101205 raised      -> re-verify exchange -> gone -> ALREADY_CLOSED (no spam)
     and 101205 raised but the leg still exists -> CLOSE_FAILED, never marked closed
  D) duplicate/concurrent same-leg close        -> no competing orders
  E) Hedge Mode LONG/SHORT    -> only the requested leg is closed, sibling untouched
  F) manually adopted position -> identical verify-first lifecycle (incl. wrong-leg guard)
"""

import threading
import unittest
from unittest import mock

import core.engine as engine

# Snapshot the real close functions BEFORE any upstream module stubs them.
_real_close_partial = engine.close_partial
_real_close_position_full = engine.close_position_full

_ORIG_EX = engine.ex
_ORIG_PAPER = engine.PAPER_MODE
_ORIG_STATE = dict(engine.STATE)
_ORIG_TRADE_STATE = dict(engine.TRADE_STATE)
_ORIG_CLOSING = getattr(engine, "_closing_in_progress", None)
_ORIG_RECON = getattr(engine, "_reconciliation_pending", None)


def _leg(side, contracts, symbol="BTC/USDT:USDT"):
    return {"symbol": symbol, "side": side, "contracts": contracts,
            "entryPrice": 100.0, "markPrice": 100.0}


class _Venue:
    """Scripted BingX harness: account-wide fetch_positions + recorded orders."""

    def __init__(self, script):
        self.script = list(script)
        self.created = []
        self.create_error = None
        self.amount_to_precision = lambda s, q: q
        self.markets = {"BTC/USDT:USDT": {}, "BTC/USDT": {}}

    def fetch_positions(self, *a, **k):
        if self.script:
            return self.script.pop(0)
        return []

    def create_order(self, sym, order_type, side, amount, params=None):
        err = self.create_error
        if err is not None:
            raise err(side, amount, params) if callable(err) else err
        params = params or {}
        rec = {"sym": sym, "type": order_type, "side": side, "amount": amount,
               "params": params, "id": "mock-order-%d" % len(self.created),
               "status": "closed", "average": 100.0}
        self.created.append(rec)
        return {"id": rec["id"], "status": "closed", "average": 100.0}


class _BlockingVenue(_Venue):
    """Venue whose first fetch_positions call blocks until released."""

    def __init__(self, script):
        super().__init__(script)
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


class _Close101205Base(unittest.TestCase):
    """PAPER off, DASHBOARD off, real fetch_position_status, scripted venue."""

    def setUp(self):
        engine.PAPER_MODE = False
        engine._closing_in_progress = False
        engine._reconciliation_pending = False
        with engine._CLOSE_IN_FLIGHT_LOCK:
            engine._CLOSE_IN_FLIGHT.clear()
        engine.STATE.clear()
        engine.STATE.update({
            "open": True, "side": "BUY", "qty": 0.1, "remaining_qty": 0.1,
            "qty_initial": 0.1, "entry": 100.0, "mark_price": 100.0,
            "current_symbol": "BTC/USDT:USDT", "symbol": "BTC/USDT:USDT",
            "trade_id": "T-101205-TEST",
        })
        engine.TRADE_STATE.clear()
        engine.TRADE_STATE.update({"in_position": True, "qty": 0.1})
        engine.DASHBOARD_STATE["live_trade_mode"] = False

        self.venue = _Venue([])
        engine.ex = self.venue
        self.patches = [
            mock.patch.object(engine, "finalize_trade_with_reality", return_value=None),
            mock.patch.object(engine, "verify_order_filled", return_value=(True, 0.1)),
            mock.patch.object(engine, "_cancel_native_protection", return_value=True),
            mock.patch.object(engine, "_exchange_sync"),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

    def tearDown(self):
        engine.ex = _ORIG_EX
        engine.PAPER_MODE = _ORIG_PAPER
        engine.STATE.clear()
        engine.STATE.update(_ORIG_STATE)
        engine.TRADE_STATE.clear()
        engine.TRADE_STATE.update(_ORIG_TRADE_STATE)
        if _ORIG_CLOSING is not None:
            engine._closing_in_progress = _ORIG_CLOSING
        if _ORIG_RECON is not None:
            engine._reconciliation_pending = _ORIG_RECON
        with engine._CLOSE_IN_FLIGHT_LOCK:
            engine._CLOSE_IN_FLIGHT.clear()

    def _last_close_outcome(self):
        log = engine.MEMORY.get("close_log", [])
        return log[-1] if log else {}


class VerifiedCloseLifecycleTest(_Close101205Base):
    """A) position exists -> verify -> close order -> fill verify -> verify -> closed."""

    def test_close_order_only_after_exchange_verify(self):
        self.venue.script = [[_leg("long", 0.1)], []]
        self.assertTrue(engine.close_position_full())
        self.assertEqual(len(self.venue.created), 1)
        rec = self.venue.created[0]
        self.assertEqual(rec["side"], "sell")
        self.assertEqual(rec["params"]["positionSide"], "LONG")
        self.assertFalse(engine.STATE["open"])
        self.assertFalse(engine.TRADE_STATE["in_position"])
        self.assertEqual(self._last_close_outcome()["event"], "CLOSE_EXECUTED")
        self.assertEqual(self._last_close_outcome()["reason"], "CONFIRMED_ABSENT")

    def test_short_leg_lifecycle_symmetric(self):
        engine.STATE["side"] = "SELL"
        self.venue.script = [[_leg("short", 0.1)], []]
        self.assertTrue(engine.close_position_full())
        rec = self.venue.created[0]
        self.assertEqual(rec["side"], "buy")
        self.assertEqual(rec["params"]["positionSide"], "SHORT")
        self.assertFalse(engine.STATE["open"])

    def test_venue_qty_beats_stale_local_qty(self):
        # Local remaining_qty is stale and larger than the venue leg: the close
        # order must use the venue qty, never an over-sized blind attempt.
        engine.STATE["remaining_qty"] = 5.0
        self.venue.script = [[_leg("long", 0.1)], []]
        self.assertTrue(engine.close_position_full())
        rec = self.venue.created[0]
        self.assertEqual(rec["amount"], 0.1)


class AlreadyClosedOnExchangeTest(_Close101205Base):
    """B) leg vanished before close -> sync only, zero orders, zero retry loop."""

    def test_no_close_order_when_leg_gone(self):
        self.venue.script = [[], []]
        self.assertTrue(engine.close_position_full())
        self.assertEqual(len(self.venue.created), 0)
        self.assertFalse(engine.STATE["open"])
        self.assertFalse(engine.TRADE_STATE["in_position"])
        outcome = self._last_close_outcome()
        self.assertEqual(outcome["event"], "CLOSE_EXECUTED")
        self.assertEqual(outcome["reason"], engine.ALREADY_CLOSED_ON_EXCHANGE)

    def test_no_spam_no_retry_loop_on_repeat_calls(self):
        self.venue.script = [[], []]
        self.assertTrue(engine.close_position_full())
        first_orders = len(self.venue.created)
        # A second management cycle must not resurrect a close attempt.
        self.assertFalse(engine.close_position_full())
        self.assertFalse(engine.close_position_full())
        self.assertEqual(len(self.venue.created), first_orders)

    def test_sibling_leg_reports_opposite_exists_and_is_untouched(self):
        # LONG gone, SHORT sibling still open: notify opposite_exists, emit no
        # order, and never touch the sibling.
        self.venue.script = [[], [_leg("short", 0.2)]]
        self.assertTrue(engine.close_position_full())
        self.assertEqual(len(self.venue.created), 0)
        self.assertFalse(engine.STATE["open"])
        outcome = self._last_close_outcome()
        self.assertEqual(outcome["reason"], engine.ALREADY_CLOSED_ON_EXCHANGE)


class BingX101205HandlingTest(_Close101205Base):
    """C) 101205 is re-verified against the exchange; never blindly swallowed."""

    def _raise_101205(self, *a, **k):
        return Exception("BingX 101205: No position to close")

    def test_101205_then_gone_syncs_and_never_repeats(self):
        self.venue.create_error = self._raise_101205
        self.venue.script = [[_leg("long", 0.1)], []]
        with mock.patch.object(engine, "safe_api_call",
                               side_effect=lambda f, *a, **k: f(*a, **k)):
            self.assertTrue(engine.close_position_full())
        # No close order was ever emitted; the error led to a re-verify then sync.
        self.assertEqual(len(self.venue.created), 0)
        self.assertFalse(engine.STATE["open"])
        self.assertFalse(engine.TRADE_STATE["in_position"])
        self.assertEqual(engine.STATE["close_reason"], engine.ALREADY_CLOSED_ON_EXCHANGE)
        log = engine.MEMORY.get("close_log", [])
        self.assertTrue(any(r["reason"] == engine.ALREADY_CLOSED_ON_EXCHANGE for r in log))
        # Even if called again (a new management cycle), nothing fires.
        self.assertFalse(engine.close_position_full())
        self.assertEqual(len(self.venue.created), 0)

    def test_101205_but_leg_still_present_is_never_marked_closed(self):
        self.venue.create_error = self._raise_101205
        self.venue.script = [[_leg("long", 0.1)], [_leg("long", 0.1)]]
        with mock.patch.object(engine, "safe_api_call",
                               side_effect=lambda f, *a, **k: f(*a, **k)):
            self.assertFalse(engine.close_position_full())
        # State must REMAIN open: never closed on error text alone.
        self.assertEqual(len(self.venue.created), 0)
        self.assertTrue(engine.STATE["open"])
        self.assertTrue(engine.TRADE_STATE["in_position"])
        outcome = self._last_close_outcome()
        self.assertEqual(outcome["event"], "CLOSE_FAILED")
        self.assertEqual(outcome["reason"], "101205_RECHECK_POSITION_PRESENT")


class DuplicateCloseGuardTest(_Close101205Base):
    """D) duplicate/concurrent same-(symbol, positionSide) closes never compete."""

    def test_inflight_leg_skips_duplicate_close(self):
        # Another close for the exact same (symbol, LONG) is already in flight.
        with engine._CLOSE_IN_FLIGHT_LOCK:
            engine._CLOSE_IN_FLIGHT.add(("BTC/USDT:USDT", "LONG"))
        self.venue.script = [[_leg("long", 0.1)], []]
        self.assertFalse(engine.close_position_full())
        self.assertEqual(len(self.venue.created), 0)
        self.assertTrue(engine.STATE["open"])  # not touched by the skipped duplicate
        outcome = self._last_close_outcome()
        self.assertEqual(outcome["event"], "SKIP_DUPLICATE")
        self.assertEqual(outcome["reason"], "ALREADY_IN_FLIGHT")

    def test_concurrent_duplicate_emits_single_order(self):
        self.venue = _BlockingVenue([[_leg("long", 0.1)], []])
        engine.ex = self.venue
        results = []

        def worker():
            results.append(engine.close_position_full())

        t = threading.Thread(target=worker)
        t.start()
        self.assertTrue(self.venue.entered.wait(15),
                        "first close should block inside the pre-order verify")
        # A second same-leg close while the first is verifying: no competing order.
        self.assertFalse(engine.close_position_full())
        self.assertEqual(len(self.venue.created), 0)
        self.venue.release.set()
        t.join(timeout=15)
        self.assertEqual(results, [True])
        self.assertEqual(len(self.venue.created), 1)
        self.assertEqual(self.venue.created[0]["params"]["positionSide"], "LONG")
        self.assertFalse(engine.STATE["open"])


class HedgeModeLegTest(_Close101205Base):
    """E) Hedge Mode: only the requested leg is matched and closed."""

    def test_fetch_position_status_belts_leg_filter(self):
        both = [_leg("long", 0.1), _leg("short", 0.2)]
        self.venue.script = [both]
        pos, status = engine.fetch_position_status("BTC/USDT:USDT", position_side="LONG")
        self.assertEqual(status, "OK")
        self.assertEqual(engine._leg_of_position(pos), "LONG")
        self.venue.script = [both]
        pos, status = engine.fetch_position_status("BTC/USDT:USDT", position_side="SHORT")
        self.assertEqual(status, "OK")
        self.assertEqual(engine._leg_of_position(pos), "SHORT")
        self.venue.script = [both]
        pos, status = engine.fetch_position_status("BTC/USDT:USDT")  # legacy symbol-only
        self.assertEqual(status, "OK")
        self.assertEqual(engine._leg_of_position(pos), "LONG")

    def test_sibling_short_never_matched_for_long_close(self):
        # Both legs are on the venue; a LONG close must target the LONG leg and
        # leave the SHORT sibling completely untouched.
        self.venue.script = [[_leg("long", 0.1), _leg("short", 0.2)],
                             [_leg("short", 0.2)],
                             [_leg("short", 0.2)],
                             [_leg("short", 0.2)]]
        self.assertTrue(engine.close_position_full())
        self.assertEqual(len(self.venue.created), 1)
        rec = self.venue.created[0]
        self.assertEqual(rec["side"], "sell")
        self.assertEqual(rec["params"]["positionSide"], "LONG")
        # Post-close the venue still holds the SHORT sibling.
        pos, status = engine.fetch_position_status("BTC/USDT:USDT")
        self.assertEqual(status, "OK")
        self.assertEqual(engine._leg_of_position(pos), "SHORT")
        self.assertFalse(engine.STATE["open"])

    def test_long_sibling_never_matched_for_short_close(self):
        self.venue.script = [[_leg("short", 0.1), _leg("long", 0.2)],
                             [_leg("long", 0.2)],
                             [_leg("long", 0.2)],
                             [_leg("long", 0.2)]]
        engine.STATE["side"] = "SELL"
        self.assertTrue(engine.close_position_full())
        self.assertEqual(len(self.venue.created), 1)
        rec = self.venue.created[0]
        self.assertEqual(rec["side"], "buy")
        self.assertEqual(rec["params"]["positionSide"], "SHORT")
        pos, status = engine.fetch_position_status("BTC/USDT:USDT")
        self.assertEqual(status, "OK")
        self.assertEqual(engine._leg_of_position(pos), "LONG")


class ManualAdoptionCloseLifecycleTest(_Close101205Base):
    """F) manually adopted / restored positions follow the same lifecycle."""

    def test_adopted_long_closes_through_verification(self):
        # restore_from_exchange seeds raw "BUY"; adoption keeps local state.
        engine.STATE.update({"open": True, "side": "BUY", "qty": 0.1,
                             "remaining_qty": 0.1})
        self.venue.script = [[_leg("long", 0.1)], []]
        self.assertTrue(engine.close_position_full())
        self.assertEqual(len(self.venue.created), 1)
        rec = self.venue.created[0]
        self.assertEqual(rec["side"], "sell")
        self.assertEqual(rec["params"]["positionSide"], "LONG")
        self.assertFalse(engine.STATE["open"])

    def test_state_side_mismatch_never_closes_wrong_leg(self):
        # Local state says SELL/SHORT but the venue only holds LONG: the SHORT
        # leg does not exist, so no wrong-leg BUY order is emitted.
        engine.STATE.update({"side": "SELL", "qty": 0.1, "remaining_qty": 0.1})
        self.venue.script = [[_leg("long", 0.1)],
                             [_leg("long", 0.1)],
                             [_leg("long", 0.1)]]
        # A venue LONG means NO short leg to close: verified absent -> sync only.
        self.assertTrue(engine.close_position_full())
        self.assertEqual(len(self.venue.created), 0)
        self.assertFalse(engine.STATE["open"])
        self.assertEqual(self._last_close_outcome()["reason"],
                         engine.ALREADY_CLOSED_ON_EXCHANGE)


class CloseStatusUnknownGuardTest(_Close101205Base):
    """Exchange query failures must never become a false local close."""

    def test_paused_position_refuses_close_and_stays_open(self):
        with mock.patch.object(engine, "fetch_position_status",
                               return_value=(None, "PAUSED")):
            self.assertFalse(engine.close_position_full())
        self.assertTrue(engine.STATE["open"])
        self.assertTrue(engine.TRADE_STATE["in_position"])
        self.assertEqual(len(self.venue.created), 0)
        self.assertEqual(self._last_close_outcome()["event"], "CLOSE_STATUS_UNKNOWN")


if __name__ == "__main__":
    unittest.main()