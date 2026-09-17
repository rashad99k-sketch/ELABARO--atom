"""Telegram close-notification idempotency suite (duplicate TRADE CLOSED fix).

Root cause observed in the live DIARY: the SAME physical position close is
re-sent as "TRADE CLOSED" with a DIFFERENT REC-{symbol}-{side}-{millis} id each
time. A physical position that disappears from local context can be re-adopted
from the venue with a brand-new trade_id; each re-adoption that reaches
finalize emits a fresh close notification, and the old in-memory send_once
dedup (close_{trade_id or symbol}, 10s) is defeated by the new id.

Fix = durable close-IDENTITY ledger keyed on the PHYSICAL position attributes
(symbol|side|entry|qty_initial), never the local REC id; it survives restart,
is checked inside tg_close before ANY send, and _adopt_position refuses to
re-adopt an identity whose close was already announced. sync_all_states only
finalizes an external close on authoritative NOT_FOUND (never on ERROR/PAUSED).

TG-1  same physical identity + different REC ids -> exactly ONE announce
TG-2  genuinely different physical identity -> a NEW announce is allowed
TG-3  dedup survives cold in-memory (only the durable ledger is left)
TG-4  identity key is venue-attribute based, rounding-stable, no trade_id
TG-5  _adopt_position skips an already-announced-closed physical position
TG-6  validate_position_state: NOT_FOUND==closed; ERROR/PAUSED preserve state
TG-7  sync_all_states finalizes ONLY on NOT_FOUND and tags EXTERNAL_CLOSE
TG-8  ledger survives a restart (verified against the persisted file)
"""
import copy
import json
import os
import time

import pytest

import core.engine as E
from portfolio.manager import PortfolioManager


@pytest.fixture(autouse=True)
def _reset():
    E._reset_close_ledger()
    E._last_tg_msg.clear()
    _saved_paper = E.PAPER_MODE
    _saved_state = copy.deepcopy(E.STATE)
    yield
    E._reset_close_ledger()
    E._last_tg_msg.clear()
    E.PAPER_MODE = _saved_paper
    E.STATE.clear(); E.STATE.update(_saved_state)


def _manual(symbol, side="BUY", contracts=1.0, entry=100.0):
    return {"symbol": symbol, "side": side, "contracts": float(contracts),
            "entryPrice": entry, "markPrice": entry,
            "stopLossPrice": 0.0, "takeProfitPrice": 0.0, "takeProfit2Price": 0.0}


# ------------------------------------------------ TG-1
def test_tg1_same_physical_identity_different_rec_ids_single_announce(monkeypatch):
    sent = []
    monkeypatch.setattr(E, "_tg_send", lambda text: sent.append(text))
    E.STATE["qty_initial"] = 1.0

    E.tg_close("BTC/USDT:USDT", -1.5, 12, "BUY", pnl_usdt=-5.0,
               reason="SINGLE_TP", entry=100.0, trade_id="REC-BTC/USDT_1-1")
    # Root-cause regression: the SAME position re-adopted with a NEW id must NOT
    # produce a second close telegram.
    E.tg_close("BTC/USDT:USDT", -1.5, 12, "BUY", pnl_usdt=-5.0,
               reason="SINGLE_TP", entry=100.0, trade_id="REC-BTC/USDT_1-2")
    E.tg_close("BTC/USDT:USDT", -1.5, 12, "BUY", pnl_usdt=-5.0,
               reason="SINGLE_TP", entry=100.0, trade_id="REC-BTC/USDT_1-3")
    assert len(sent) == 1


# ------------------------------------------------ TG-2
def test_tg2_genuinely_new_identity_is_announced(monkeypatch):
    sent = []
    monkeypatch.setattr(E, "_tg_send", lambda text: sent.append(text))
    E.STATE["qty_initial"] = 1.0

    E.tg_close("BTC/USDT:USDT", 5.0, 10, "BUY", pnl_usdt=10.0, entry=100.0, trade_id="REC-A")
    E.tg_close("ETH/USDT:USDT", 3.0, 8, "BUY", pnl_usdt=6.0, entry=3000.0, trade_id="REC-B")
    assert len(sent) == 2                               # different physical identity
    assert "BTC/USDT:USDT" in sent[0]


# ------------------------------------------------ TG-3
def test_tg3_dedup_survives_cold_in_memory(monkeypatch):
    """After a hard forget of the in-memory send cooldown AND the in-memory
    ledger, the persisted ledger alone must still suppress the duplicate."""
    sent = []
    monkeypatch.setattr(E, "_tg_send", lambda text: sent.append(text))
    E.STATE["qty_initial"] = 1.0

    E.tg_close("BTC/USDT:USDT", -2.0, 5, "SELL", pnl_usdt=-8.0, entry=90.0, trade_id="REC-X1")
    assert len(sent) == 1

    # Simulate a fresh process with ZERO in-memory state (only the durable
    # ledger file remains).
    E._last_tg_msg.clear()
    E._close_identity_ledger = {}
    E._ledger_loaded = False

    E.tg_close("BTC/USDT:USDT", -2.0, 5, "SELL", pnl_usdt=-8.0, entry=90.0, trade_id="REC-X2")
    assert len(sent) == 1                               # still suppressed


# ------------------------------------------------ TG-4
def test_tg4_identity_key_is_venue_attribute_based():
    k1 = E._close_identity_key("BTC/USDT:USDT", "BUY", 100.0, 1.0)
    assert k1 == E._close_identity_key("BTC/USDT:USDT", "BUY", 100.0, 1.0)
    # Rounding-stable across float noise on a re-read entry.
    assert k1 == E._close_identity_key("BTC/USDT:USDT", "BUY", 99.9999999999, 1.0)
    # A different venue attribute is a DIFFERENT physical identity.
    assert k1 != E._close_identity_key("BTC/USDT:USDT", "BUY", 101.0, 1.0)
    assert k1 != E._close_identity_key("BTC/USDT:USDT", "BUY", 100.0, 2.0)
    assert k1 != E._close_identity_key("ETH/USDT:USDT", "BUY", 100.0, 1.0)
    assert k1 != E._close_identity_key("BTC/USDT:USDT", "SELL", 100.0, 1.0)
    # The key must never encode the local REC id (the thing that changed and
    # produced the duplicate notifications).
    assert "REC-" not in k1


# ------------------------------------------------ TG-5
def test_tg5_adopt_position_skips_already_announced_identity(monkeypatch):
    pm = PortfolioManager(6, E)
    activated = []
    monkeypatch.setattr(pm, "activate",
                        lambda key: activated.append(key) or True)

    # Identity already announced-closed -> adoption refused BEFORE any context.
    announced_key = E._close_identity_key("BTC/USDT:USDT", "BUY", 100.0, 1.0)
    monkeypatch.setattr(E, "_is_close_announced",
                        lambda key: key == announced_key)
    started = len(pm.contexts)
    assert pm._adopt_position(_manual("BTC/USDT:USDT", "BUY", 1.0, 100.0)) is False
    assert len(pm.contexts) == started                    # nothing adopted
    assert activated == []                                # guard short-circuits

    # Unrelated identity NOT announced -> the guard is transparent and the
    # normal adoption path is reached.
    monkeypatch.setattr(E, "_is_close_announced", lambda key: False)
    assert pm._adopt_position(_manual("ETH/USDT:USDT", "BUY", 1.0, 3000.0)) is True
    assert activated  # activation ran -> proven the skip came from the GUARD


# ------------------------------------------------ TG-6
def test_tg6_paused_or_error_never_finalizes(monkeypatch):
    E.PAPER_MODE = False
    local = {"open": True, "sym": "BTC/USDT:USDT"}

    # PAUSED venue: local state preserved, NOT a close.
    monkeypatch.setattr(E, "fetch_position_status", lambda symbol, position_side=None: (None, "PAUSED"))
    assert E.validate_position_state(local, "BTC/USDT:USDT") is local
    # ERROR venue: local state preserved, NOT a close.
    monkeypatch.setattr(E, "fetch_position_status", lambda symbol, position_side=None: (None, "ERROR"))
    assert E.validate_position_state(local, "BTC/USDT:USDT") is local
    # Authoritative NOT_FOUND: genuinely closed.
    monkeypatch.setattr(E, "fetch_position_status", lambda symbol, position_side=None: (None, "NOT_FOUND"))
    assert E.validate_position_state(local, "BTC/USDT:USDT") is None


# ------------------------------------------------ TG-7
def test_tg7_sync_all_states_finalizes_only_on_not_found(monkeypatch):
    E.PAPER_MODE = False
    E.STATE.update({"open": True, "current_symbol": "BTC/USDT:USDT",
                    "side": "BUY", "entry": 100.0, "qty_initial": 1.0,
                    "qty": 1.0, "trade_id": "REC-SYNC-1"})
    E.TRADE_STATE.update({"in_position": True, "symbol": "BTC/USDT:USDT"})
    monkeypatch.setattr(E, "get_realized_pnl_for_symbol", lambda *a, **k: (0.0, 0.0))
    monkeypatch.setattr(E, "_set_wallet_pnl_memory", lambda *a, **k: None)

    # NOT_FOUND -> finalize once, tagged EXTERNAL_CLOSE, memory marked CLOSED.
    finalized = []
    monkeypatch.setattr(E, "fetch_position_status", lambda symbol, position_side=None: (None, "NOT_FOUND"))
    monkeypatch.setattr(
        E, "finalize_trade_with_reality",
        lambda symbol: finalized.append(E.STATE.get("close_reason")) or (0.0, 0.0))
    E.sync_all_states()
    assert len(finalized) == 1
    assert finalized[0] == "EXTERNAL_CLOSE"
    assert E.MEMORY["position_status"] == "CLOSED"

    # ERROR -> finalize must NOT fire; state preserved.
    finalized.clear()
    monkeypatch.setattr(E, "fetch_position_status", lambda symbol, position_side=None: (None, "ERROR"))
    E.sync_all_states()
    assert finalized == []
    assert E.MEMORY["position_status"] == "OPEN"


# ------------------------------------------------ TG-8
def test_tg8_ledger_survives_restart(monkeypatch, tmp_path):
    """The durable ledger is written to disk: a fully cold engine (empty
    in-memory ledger, empty send cooldown) reloads it and STILL suppresses the
    duplicate -- exactly what a process restart does."""
    ledger_file = tmp_path / "close_identity_ledger.json"
    monkeypatch.setattr(E, "_CLOSE_LEDGER_PATH", str(ledger_file))
    sent = []
    monkeypatch.setattr(E, "_tg_send", lambda text: sent.append(text))
    E.STATE["qty_initial"] = 2.5

    E.tg_close("XAUUSD", 1.2, 9, "BUY", pnl_usdt=30.0, entry=3300.0, trade_id="REC-G1")
    assert len(sent) == 1
    assert ledger_file.exists()                           # persisted to disk

    # A key could change the qty rounding of the SAME position across a
    # restart?  No: qty_initial is stable; verify the persisted record contains
    # the exact identity for the announced position.
    key = E._close_identity_key("XAUUSD", "BUY", 3300.0, 2.5)
    with open(ledger_file, "r", encoding="utf-8") as fh:
        persisted = json.load(fh)
    assert key in persisted                              # durable evidence on disk

    # Cold restart: no in-memory state, in-memory cooldown empty.
    E._last_tg_msg.clear()
    E._close_identity_ledger = {}
    E._ledger_loaded = False

    E.tg_close("XAUUSD", 1.2, 9, "BUY", pnl_usdt=30.0, entry=3300.0, trade_id="REC-G2")
    assert len(sent) == 1                                # suppressed after restart