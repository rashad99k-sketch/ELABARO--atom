"""Single-TP (100% full close) profit-plan regression suite.

Production default: SINGLE_TP_ENABLED=true -> TP1 is the ONLY take-profit and
closes the FULL remaining position through the authoritative verified
close_position_full pipeline exactly like the legacy TP2 path. TP2 and the
runner/partial machinery are DERIVED OFF from that one flag (there is no
second independent toggle to drift out of sync).

The offline suite pins legacy 50/50 mode in tests/conftest.py; this file opts
back into SINGLE-TP mode by setting the module flags at call time (that is the
same mechanism apply_profit_engine / close_partial read on every invocation).

SINGLE-TP-1  production default is SINGLE_TP_ENABLED=true with derived
             TP2/RUNNER/PARTIAL flags OFF and close fraction 1.0 (fresh
             interpreter, clean env)
SINGLE-TP-2  TP1 touch -> close_position_full(100%) with close_reason
             SINGLE_TP, tp1/tp2_hit both True, runner+trail disabled
SINGLE-TP-3  TP2 cannot fire after TP1 under single-TP (tp2 set disabled)
SINGLE-TP-4  close_partial is refuse-closed (no 50% scale-out possible)
SINGLE-TP-5  _close_partial_min_ok rejects zero / below-min close qty with a
             CLOSE_FAILED event before any order is placed
SINGLE-TP-6  LiveTradeManager caller treats the single-TP full close as a
             terminal CLOSED lifecycle (no trailing / runner continuation)
"""
import copy
import os
import subprocess
import sys
import textwrap
import time

import pandas as pd
import pytest

import core.engine as E

_BASE_STATE = copy.deepcopy(E.STATE)
_BASE_TRADE_STATE = copy.deepcopy(E.TRADE_STATE)
_BASE_PAPER = copy.deepcopy(E.paper)
_BASE_DASH = copy.deepcopy(E.DASHBOARD_STATE)


@pytest.fixture(autouse=True)
def _reset():
    E.STATE.clear(); E.STATE.update(copy.deepcopy(_BASE_STATE))
    E.TRADE_STATE.clear(); E.TRADE_STATE.update(copy.deepcopy(_BASE_TRADE_STATE))
    E.paper.clear(); E.paper.update(copy.deepcopy(_BASE_PAPER))
    E.DASHBOARD_STATE.clear(); E.DASHBOARD_STATE.update(copy.deepcopy(_BASE_DASH))
    E.STATE["open"] = False
    E.STATE["close_reason"] = None
    yield
    E.STATE.clear(); E.STATE.update(copy.deepcopy(_BASE_STATE))
    E.TRADE_STATE.clear(); E.TRADE_STATE.update(copy.deepcopy(_BASE_TRADE_STATE))
    E.paper.clear(); E.paper.update(copy.deepcopy(_BASE_PAPER))
    E.DASHBOARD_STATE.clear(); E.DASHBOARD_STATE.update(copy.deepcopy(_BASE_DASH))


@pytest.fixture()
def single_tp(monkeypatch):
    """Pin the module flags to the SINGLE-TP production mode at call time and
    restore them afterwards (monkeypatch auto-undo)."""
    monkeypatch.setattr(E, "SINGLE_TP_ENABLED", True)
    monkeypatch.setattr(E, "SINGLE_TP_CLOSE_FRACTION", 1.0)
    monkeypatch.setattr(E, "TP2_ENABLED", False)
    monkeypatch.setattr(E, "RUNNER_ENABLED", False)
    monkeypatch.setattr(E, "PARTIAL_CLOSE_ENABLED", False)
    E._reset_close_ledger()
    yield


@pytest.fixture()
def capture(monkeypatch):
    calls = {"partial": [], "full": [], "events": []}

    def _fake_partial(ratio, stage="PARTIAL"):
        calls["partial"].append((ratio, stage))
        return True

    def _fake_full(close_price=None, stage="FULL"):
        calls["full"].append((close_price, stage))
        return True

    def _fake_event(*args, **kwargs):
        calls["events"].append((args, kwargs))

    monkeypatch.setattr(E, "close_partial", _fake_partial)
    monkeypatch.setattr(E, "close_position_full", _fake_full)
    monkeypatch.setattr(E, "_trade_event", _fake_event)
    return calls


def _df(close, high=None, low=None, n=1):
    close = float(close)
    high = float(close) if high is None else float(high)
    low = float(close) if low is None else float(low)
    return pd.DataFrame({
        "open": [float(close)] * n,
        "high": [high] * n,
        "low": [low] * n,
        "close": [float(close)] * n,
        "volume": [1000.0] * n,
    })


def _rising_df(n=60):
    return pd.DataFrame({
        "open": [100.0 + i * 0.05 for i in range(n)],
        "high": [100.0 + i * 0.05 + 0.2 for i in range(n)],
        "low": [100.0 + i * 0.05 - 0.2 for i in range(n)],
        "close": [100.0 + i * 0.05 for i in range(n)],
        "volume": [1000.0] * n,
    })


def _open_state(symbol="BTR/USDT:USDT", side="BUY", entry=99.9, qty=100.0, **kw):
    st = {
        "open": True,
        "current_symbol": symbol,
        "side": side,
        "entry": entry,
        "qty": qty,
        "qty_initial": qty,
        "remaining_qty": qty,
        "tp1_price": 110.0 if side == "BUY" else 90.0,
        "tp2_price": 120.0 if side == "BUY" else 80.0,
        "synthetic_tp1": 110.0 if side == "BUY" else 90.0,
        "synthetic_tp2": 120.0 if side == "BUY" else 80.0,
        "dynamic_tp1": 110.0 if side == "BUY" else 90.0,
        "dynamic_tp2": 120.0 if side == "BUY" else 80.0,
        "tp1_hit": False,
        "tp2_hit": False,
        "tp1_closed_qty": 0.0,
        "margin": 10.0,
        "trade_id": "TRD-SINGLE-1001",
        "runner_mode": False,
        "trail_activated": False,
        "position_setup_snapshot": None,
        "roe_pct": 0.0,
        "roe_valid": True,
    }
    st.update(kw)
    return st


# ------------------------------------------------ SINGLE-TP-1
def test_single_tp_1_default_100_percent_config_clean_env():
    """In a fresh interpreter with the env unset the production default is
    SINGLE_TP_ENABLED=true and TP2/RUNNER/PARTIAL are derived OFF with a 1.0
    close fraction."""
    stub = textwrap.dedent("""
        import os, sys, types
        os.environ.pop("SINGLE_TP_ENABLED", None)
        ccxt = types.ModuleType("ccxt")
        class _B:
            def __init__(self, *a, **kw): self.markets = {"BTC/USDT:USDT": {}}
        ccxt.bingx = _B
        _F = types.ModuleType("flask")
        class _Flask:
            def route(self, *a, **k): return lambda fn: fn
            def add_url_rule(self, *a, **k): return None
            def __init__(self, *a, **k): pass
        _F.Flask = _Flask; _F.jsonify = lambda *a, **k: None
        _F.request = types.SimpleNamespace()
        sys.modules["ccxt"] = ccxt; sys.modules["flask"] = _F
        sys.modules.pop("core.engine", None)
        import core.engine as E
        assert E.SINGLE_TP_ENABLED is True, E.SINGLE_TP_ENABLED
        assert E.SINGLE_TP_CLOSE_FRACTION == 1.0, E.SINGLE_TP_CLOSE_FRACTION
        assert E.TP2_ENABLED is False, E.TP2_ENABLED
        assert E.RUNNER_ENABLED is False, E.RUNNER_ENABLED
        assert E.PARTIAL_CLOSE_ENABLED is False, E.PARTIAL_CLOSE_ENABLED
        print("DEFAULT_OK")
    """)
    proc = subprocess.run([sys.executable, "-c", stub], capture_output=True,
                          text=True, timeout=120, cwd=os.path.dirname(os.path.dirname(__file__)))
    assert proc.returncode == 0, proc.stderr
    assert "DEFAULT_OK" in proc.stdout


# ------------------------------------------------ SINGLE-TP-2
def test_single_tp_2_tp1_touch_closes_full_position(single_tp, capture):
    st = _open_state()
    action = E.apply_profit_engine("BTR/USDT:USDT", 110.0, _df(99.9), 0, st)
    assert action == "TP1"
    # 100% full close through the authoritative pipeline, NOT a 50% partial.
    assert capture["full"] == [(110.0, "TP1")]
    assert capture["partial"] == []
    assert st["close_reason"] == "SINGLE_TP"
    assert st["tp1_hit"] is True
    assert st["tp1_done"] is True
    assert st["tp2_hit"] is True           # single TP implies TP2 done at the same moment
    assert st["remaining_qty"] == 100.0    # venue-side full close owns the quantity
    assert st["runner_mode"] is False
    assert st["trail_activated"] is False


def test_single_tp_2b_sell_side_tp1_touch_full_close(single_tp, capture):
    st = _open_state(side="SELL")
    action = E.apply_profit_engine("BTR/USDT:USDT", 90.0, _df(100.0), 0, st)
    assert action == "TP1"
    assert capture["full"] == [(90.0, "TP1")]
    assert capture["partial"] == []
    assert st["close_reason"] == "SINGLE_TP"
    assert st["tp1_hit"] is True


def test_single_tp_2c_full_close_failure_is_hold_not_partial(single_tp, monkeypatch):
    events = []
    monkeypatch.setattr(E, "close_position_full", lambda close_price=None, stage="FULL": False)
    monkeypatch.setattr(E, "_trade_event", lambda *a, **k: events.append((a, k)))
    st = _open_state()
    action = E.apply_profit_engine("BTR/USDT:USDT", 110.5, _df(99.9), 0, st)
    assert action == "HOLD"                                   # fail-closed, no reduced close
    assert any("SINGLE_TP_FULL_CLOSE_NOT_VERIFIED" in a or "SINGLE_TP_FULL_CLOSE_NOT_VERIFIED" in str(k)
               for a, k in events)
    assert st["tp1_hit"] is False


# ------------------------------------------------ SINGLE-TP-3
def test_single_tp_3_tp2_cannot_fire_after_tp1(single_tp, capture):
    st = _open_state()
    st["tp1_hit"] = True
    st["tp2_hit"] = False                                     # TP2 never marked in single mode
    action = E.apply_profit_engine("BTR/USDT:USDT", 135.0, _df(99.9), 0, st)
    assert action == "HOLD"                                   # TP2 disabled: no second close
    assert capture["full"] == []
    assert capture["partial"] == []
    assert st["tp2_hit"] is False


def test_single_tp_3b_legacy_tp2_still_fires_when_opt_back_out(monkeypatch, capture):
    # Proves the derivation is single-flag: opting OUT of single-TP restores
    # the legacy TP2 path untouched.
    monkeypatch.setattr(E, "SINGLE_TP_ENABLED", False)
    monkeypatch.setattr(E, "TP2_ENABLED", True)
    monkeypatch.setattr(E, "RUNNER_ENABLED", True)
    monkeypatch.setattr(E, "PARTIAL_CLOSE_ENABLED", True)
    st = _open_state()
    st["tp1_hit"] = True
    st["tp1_done"] = True
    action = E.apply_profit_engine("BTR/USDT:USDT", 130.0, _df(99.9), 0, st)
    assert action == "TP2"
    assert capture["full"] == [(130.0, "TP2")]


# ------------------------------------------------ SINGLE-TP-4
def test_single_tp_4_close_partial_is_refuse_closed(single_tp):
    st = _open_state()
    st["remaining_qty"] = 100.0
    ok = E.close_partial(0.5, stage="TP1")
    assert ok is False
    assert st["remaining_qty"] == 100.0     # no 50% scale-out ever possible


# ------------------------------------------------ SINGLE-TP-5
def test_single_tp_5_min_qty_validation_wired_into_live_verified_close(single_tp, monkeypatch):
    events = []
    monkeypatch.setattr(E, "_trade_event", lambda *a, **k: events.append((a, k)))
    monkeypatch.setattr(E, "log_execution", lambda *a, **k: None)

    def _market(sym):
        return {"limits": {"amount": {"min": 0.1}},
                "precision": {"amount": 3}}

    monkeypatch.setattr(E, "ex", type("Ex", (), {"market": staticmethod(_market)})())

    # Zero quantity must be rejected before any order.
    assert E._close_partial_min_ok("BTR/USDT:USDT", "BTR/USDT:USDT", 0.0) is False
    assert any("INVALID_QUANTITY_ZERO" in a or "INVALID_QUANTITY_ZERO" in str(k) for a, k in events)
    # Below market minimum must be rejected.
    assert E._close_partial_min_ok("BTR/USDT:USDT", "BTR/USDT:USDT", 0.00001) is False
    assert any("BELOW_MIN_QUANTITY" in a or "BELOW_MIN_QUANTITY" in str(k) for a, k in events)
    # A valid quantity passes.
    assert E._close_partial_min_ok("BTR/USDT:USDT", "BTR/USDT:USDT", 0.5) is True
    assert E._close_partial_min_ok("BTR/USDT:USDT", "BTR/USDT:USDT", 0.1) is True


def _boom_di(df, **kw):
    raise RuntimeError("DI engine total failure")


# ------------------------------------------------ SINGLE-TP-6
def test_single_tp_6_manager_caller_terminates_on_single_tp_full_close(monkeypatch, single_tp, _reset):
    E.STATE.update(_open_state())
    E.STATE["open"] = True
    E.paper["position"] = {"side": "BUY", "entry": 99.9, "qty": 100.0, "remaining_qty": 100.0}
    E.DASHBOARD_STATE["live_trade_mode"] = True

    manager = E.LiveTradeManager(E._event_bus, E._exchange_sync, E._recovery_guard)
    manager.symbol = "BTR/USDT:USDT"
    manager.trade_id = E.STATE.get("trade_id")
    manager.lifecycle_state = E.TradeLifecycleState.LIVE
    manager.last_heavy_calc_ts = 0

    calls = {"full": [], "events": []}
    monkeypatch.setattr(E, "get_di_components", _boom_di)
    monkeypatch.setattr(E, "get_ohlcv_safe", lambda symbol, n: _rising_df())
    monkeypatch.setattr(E, "get_live_hybrid_df", lambda symbol, base, live_price: base)
    monkeypatch.setattr(E, "_fresh_execution_mark", lambda symbol: 110.5)
    monkeypatch.setattr(E, "_refresh_live_levels", lambda *a, **k: None)
    monkeypatch.setattr(E, "publish_position_state", lambda *a, **k: None)
    monkeypatch.setattr(E, "_record_partial_leg", lambda *a, **k: None)
    monkeypatch.setattr(E, "_trade_event", lambda *a, **k: None)
    monkeypatch.setattr(E, "log_execution", lambda *a, **k: None)
    monkeypatch.setattr(
        E, "close_position_full",
        lambda close_price=None, stage="FULL": (calls["full"].append((close_price, stage)), True)[1])
    monkeypatch.setattr(E._event_bus, "emit", lambda evt, state: calls["events"].append((evt, state)))

    manager._apply_management("BTR/USDT:USDT", time.time())

    # The single-TP full close is terminal exactly like legacy TP2: a CLOSED
    # lifecycle event and the position mode flag dropped.
    assert calls["full"] == [(110.5, "TP1")]
    assert ("lifecycle_change", E.TradeLifecycleState.CLOSED) in calls["events"]
    assert E.DASHBOARD_STATE["live_trade_mode"] is False
    assert E.STATE["close_reason"] == "SINGLE_TP"