"""Regression suite: a heavy-calc exception must never block profit management.

Death point (BTR/USDT:USDT type symptom): _apply_management()'s heavy-calc
block previously ran ~15 bare engine calls (DI/ADX, pullback, smart-money,
momentum, regime, brain.update, IFVG, continuation-pressure/eval, thesis
failure, confidence, rejections, hold-score, exit-warning). ANY single
exception escaped to manage_all()'s per-symbol catch (portfolio/manager.py:653)
and aborted the management tick BEFORE apply_profit_engine() ran at
engine.py:5575 -> TP1 progress 100%, Remaining 100%, Action WAIT_TP1,
Runner OFF, Why "No management action recorded".

The fix wraps the heavy block in three phases with full STATE fallbacks
(identical to the else-branch values), a per-tick management-cycle id, and
tuple-complete exception logging. These tests prove:

  T1  a phase-1 heavy exception no longer prevents apply_profit_engine()
  T2  TP1 triggers at exactly 100% progress
  T3  TP1 still triggers past 100% (mark above TP1, BTR case)
  T4  TP1 execution is verified through the exchange path (profit_execution)
  T5  TP2 fires after the TP1 partial
  T6  close_partial failure is surfaced as TP1_EXECUTION_FAILED (no silent HOLD)
  T7  adopted position with tp1_price=0 but valid synthetic_tp1 still books TP1
  T8  heavy exception logging carries symbol/trade_id/stage/exc_type/traceback/ts/cycle
  T9  last_heavy_calc_ts is advanced on exception (no retry hammering)
  T10 STATE fallback values are fully valid for apply_profit_engine()
"""
import copy
import time

import pandas as pd
import pytest

import core.engine as E

_REAL_APPLY_PROFIT_ENGINE = E.apply_profit_engine

_BASE_STATE = copy.deepcopy(E.STATE)
_BASE_TRADE_STATE = copy.deepcopy(E.TRADE_STATE)
_BASE_PAPER = copy.deepcopy(E.paper)
_BASE_DASH = copy.deepcopy(E.DASHBOARD_STATE)


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


def _open_state(**kw):
    st = {
        "open": True,
        "current_symbol": "BTR/USDT:USDT",
        "side": "BUY",
        "entry": 99.9,
        "qty": 100.0,
        "qty_initial": 100.0,
        "remaining_qty": 100.0,
        "tp1_price": 110.0,
        "tp2_price": 120.0,
        "synthetic_tp1": 110.0,
        "synthetic_tp2": 120.0,
        "dynamic_tp1": 110.0,
        "dynamic_tp2": 120.0,
        "tp1_hit": False,
        "tp2_hit": False,
        "tp1_closed_qty": 0.0,
        "margin": 10.0,
        "entry_atr": 0.5,
        "roe_pct": 0.0,
        "roe_valid": True,
        "trade_id": "TRD-HEAVY-1001",
        "position_setup_snapshot": None,
        "last_confirmed_sl": 0.0,
        "adx_live": 20.0,
        "di_plus_live": 20.0,
        "di_minus_live": 20.0,
        "smart_money": {},
        "momentum_flow": {},
        "trade_state": "RANGE_CHOP",
        "advisory_trend_health": 5.0,
        "advisory_struct_shift": None,
        "advisory_structure_aligned": False,
        "continuation_probability": 0.5,
        "trend_strength": 0.5,
        "reclaim_risk": 0.0,
        "counter_pressure": 0.0,
        "continuation_reasons": [],
        "hold_quality": "UNKNOWN",
        "tp1_hold_score": 10,
        "exit_warning": 0,
        "continuation_pressure": 50,
        "thesis_failure_score": 0,
    }
    st.update(kw)
    return st


@pytest.fixture(autouse=True)
def _reset():
    E.STATE.clear(); E.STATE.update(copy.deepcopy(_BASE_STATE))
    E.TRADE_STATE.clear(); E.TRADE_STATE.update(copy.deepcopy(_BASE_TRADE_STATE))
    E.paper.clear(); E.paper.update(copy.deepcopy(_BASE_PAPER))
    E.DASHBOARD_STATE.clear(); E.DASHBOARD_STATE.update(copy.deepcopy(_BASE_DASH))
    E.MEMORY["close_log"] = []
    E.STATE["open"] = False
    yield
    E.STATE.clear(); E.STATE.update(copy.deepcopy(_BASE_STATE))
    E.TRADE_STATE.clear(); E.TRADE_STATE.update(copy.deepcopy(_BASE_TRADE_STATE))
    E.paper.clear(); E.paper.update(copy.deepcopy(_BASE_PAPER))
    E.DASHBOARD_STATE.clear(); E.DASHBOARD_STATE.update(copy.deepcopy(_BASE_DASH))


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


def _make_manager(monkeypatch, mark=100.5, boom=True):
    """Full-fidelity manager with external inputs stubbed and heavy DI engine
    (optionally) exploding. Everything else runs the REAL engine path exactly
    like the live loop, so the death point is exercised end to end."""
    manager = E.LiveTradeManager(E._event_bus, E._exchange_sync, E._recovery_guard)
    manager.symbol = "BTR/USDT:USDT"
    manager.trade_id = E.STATE.get("trade_id")
    manager.lifecycle_state = E.TradeLifecycleState.LIVE
    manager.last_heavy_calc_ts = 0
    calls = {"di": 0}

    def _di(df, **kw):
        calls["di"] += 1
        if boom:
            raise RuntimeError("DI engine total failure")
        return 26.0, 18.0, 22.0, 0.1

    monkeypatch.setattr(E, "get_di_components", _di)
    monkeypatch.setattr(E, "get_ohlcv_safe", lambda symbol, n: _rising_df())
    monkeypatch.setattr(E, "get_live_hybrid_df", lambda symbol, base, live_price: base)
    monkeypatch.setattr(E, "_fresh_execution_mark", lambda symbol: mark)
    monkeypatch.setattr(E, "_refresh_live_levels", lambda *a, **k: None)
    monkeypatch.setattr(E, "publish_position_state", lambda *a, **k: None)
    monkeypatch.setattr(E, "_record_partial_leg", lambda *a, **k: None)
    monkeypatch.setattr(E, "_trade_event", lambda *a, **k: None)
    monkeypatch.setattr(E, "log_execution", lambda *a, **k: None)
    return manager, calls


# ------------------------------------------------ T1
def test_t1_heavy_exception_does_not_block_apply_profit_engine(monkeypatch, _reset):
    E.STATE.update(_open_state())
    E.STATE["open"] = True
    E.paper["position"] = {"side": "BUY", "entry": 99.9, "qty": 100.0, "remaining_qty": 100.0}
    manager, calls = _make_manager(monkeypatch, mark=100.5)

    apply_calls = []

    def _recorder(symbol, price, df, idx, state):
        apply_calls.append((symbol, state))
        return "HOLD"

    monkeypatch.setattr(E, "apply_profit_engine", _recorder)

    manager._apply_management("BTR/USDT:USDT", time.time())

    assert calls["di"] >= 1                     # heavy DI exploded inside the tick
    assert len(apply_calls) == 1                # ...but profit engine was still reached
    symbol, state = apply_calls[0]
    assert symbol == "BTR/USDT:USDT"
    assert state["open"] is True
    assert state["tp1_hit"] is False
    assert state["qty_initial"] == 100.0
    assert state["remaining_qty"] == 100.0       # untouched: death point no longer stalls tick


# ------------------------------------------------ T2
def test_t2_tp1_triggers_at_exactly_100_percent_progress(capture):
    st = _open_state()                              # entry 99.9 -> tp1 110.0
    mark = st["tp1_price"]                          # exactly 100% progress
    action = E.apply_profit_engine("BTR/USDT:USDT", mark, _df(99.9), 0, st)
    assert action == "TP1"
    assert capture["partial"] == [(0.5, "TP1")]
    assert st["tp1_hit"] is True
    assert st["tp1_done"] is True
    assert st["remaining_qty"] == 100.0
    assert st["sl"] == st["entry"]


# ------------------------------------------------ T3
def test_t3_tp1_triggers_past_100_percent_mark_above_tp1(capture):
    st = _open_state()
    action = E.apply_profit_engine("BTR/USDT:USDT", 115.0, _df(99.9), 0, st)
    assert action == "TP1"
    assert capture["partial"] == [(0.5, "TP1")]
    assert st["tp1_hit"] is True


def test_t3b_wick_touch_past_100_percent_fires_when_mark_below(capture):
    st = _open_state()
    df = _df(99.9, high=110.5)                       # last candle wick touched TP1
    action = E.apply_profit_engine("BTR/USDT:USDT", 99.95, df, 0, st)
    assert action == "TP1"
    assert capture["partial"] == [(0.5, "TP1")]


# ------------------------------------------------ T4
def test_t4_tp1_execution_verified_via_exchange_path(monkeypatch, _reset):
    E.STATE.update(_open_state())
    E.STATE["open"] = True
    E.paper["position"] = {"side": "BUY", "entry": 99.9, "qty": 100.0, "remaining_qty": 100.0}
    monkeypatch.setattr(E, "_record_partial_leg", lambda *a, **k: None)
    monkeypatch.setattr(E, "_trade_event", lambda *a, **k: None)
    monkeypatch.setattr(E, "log_execution", lambda *a, **k: None)

    action = E.apply_profit_engine("BTR/USDT:USDT", 110.0, _df(99.9), 0, E.STATE)

    assert action == "TP1"
    assert E.STATE["tp1_hit"] is True
    assert E.STATE["remaining_qty"] == 50.0          # exactly 50% of original booked
    assert E.STATE["tp1_closed_qty"] == 50.0
    pe = E.STATE["profit_execution"]
    assert pe["stage"] == "TP1"
    assert pe["mode"] == "PAPER"
    assert pe["verified"] is True                    # strict close verification present
    assert pe["filled_qty"] == 50.0
    assert E.STATE["synthetic_sl"] == E.STATE["entry"]  # protection ratcheted to BE


# ------------------------------------------------ T5
def test_t5_tp2_fires_after_tp1_partial(capture):
    st = _open_state()
    a1 = E.apply_profit_engine("BTR/USDT:USDT", 110.5, _df(99.9), 0, st)
    assert a1 == "TP1"
    assert capture["partial"] == [(0.5, "TP1")]
    assert st["tp1_hit"] is True

    st["tp1_hit"] = True
    a2 = E.apply_profit_engine("BTR/USDT:USDT", 130.0, _df(99.9), 0, st)
    assert a2 == "TP2"
    assert capture["full"] == [(130.0, "TP2")]       # remaining 50% closed full
    assert st["tp2_hit"] is True
    assert st["close_reason"] == "TP2"


# ------------------------------------------------ T6
def test_t6_partial_failure_surfaces_tp1_execution_failed(monkeypatch):
    events = []
    monkeypatch.setattr(E, "close_partial", lambda ratio, stage="PARTIAL": False)
    monkeypatch.setattr(E, "_trade_event", lambda *a, **k: events.append((a, k)))
    st = _open_state()
    action = E.apply_profit_engine("BTR/USDT:USDT", 110.5, _df(99.9), 0, st)
    assert action == "HOLD"                          # fail-closed, no false TP1 mark
    assert st["tp1_hit"] is False
    assert st["remaining_qty"] == 100.0
    assert any("TP1_EXECUTION_FAILED" in a for a, k in events)


# ------------------------------------------------ T7
def test_t7_adopted_position_tp1_price_zero_heals_via_synthetic(capture):
    st = _open_state(tp1_price=0.0, dynamic_tp1=0.0)
    action = E.apply_profit_engine("BTR/USDT:USDT", 110.5, _df(99.9), 0, st)
    assert action == "TP1"
    assert capture["partial"] == [(0.5, "TP1")]
    assert st["tp1_hit"] is True


def test_t7b_no_target_no_spurious_close(capture):
    st = _open_state(tp1_price=0.0, synthetic_tp1=0.0, dynamic_tp1=0.0)
    action = E.apply_profit_engine("BTR/USDT:USDT", 999.0, _df(99.9), 0, st)
    assert action == "HOLD"
    assert capture["partial"] == []
    assert st["tp1_hit"] is False


# ------------------------------------------------ T8
def test_t8_heavy_exception_log_has_all_required_fields(monkeypatch, _reset):
    E.STATE.update(_open_state())
    E.STATE["open"] = True
    E.paper["position"] = {"side": "BUY", "entry": 99.9, "qty": 100.0, "remaining_qty": 100.0}
    manager, calls = _make_manager(monkeypatch, mark=100.5)
    monkeypatch.setattr(E, "apply_profit_engine", lambda *a, **k: "HOLD")
    logs = []
    streams = {"logs": logs}
    monkeypatch.setattr(E, "log_execution", lambda msg, level="INFO", **k: streams["logs"].append((msg, level)))

    manager._apply_management("BTR/USDT:USDT", time.time())

    heavy = [m for m, lvl in logs if str(m).startswith("[HEAVY_CALC]")]
    assert heavy, "no [HEAVY_CALC] diagnostic emitted"
    msg = heavy[0]
    assert "cycle=" in msg
    assert "symbol=BTR/USDT:USDT" in msg
    assert "trade_id=TRD-HEAVY-1001" in msg
    assert "stage=PHASE1_CORE_ANALYTICS" in msg
    assert "exc_type=RuntimeError" in msg
    assert "exc=" in msg
    assert "th=" not in msg and "ts=" in msg              # timestamp present
    assert "Traceback (most recent call last)" in msg      # full traceback embedded
    assert any(lvl == "WARN" for m, lvl in logs if str(m).startswith("[HEAVY_CALC]"))
    # Phase 2 must self-heal WITHOUT raising: the phase-1 fallback now binds
    # thesis_dict + market_state so evaluate_failure() runs clean against the
    # STATE-derived fallback (degrading to failed=False) instead of emitting a
    # second PHASE2 exception that the phase-2 guard would have to absorb.
    assert not any("stage=PHASE2_THESIS_FAILURE" in m for m, lvl in logs
                   if str(m).startswith("[HEAVY_CALC]")), \
        "phase-2 must evaluate cleanly against the phase-1 fallback inputs"
    # Phase 3 (confidence/rejection) can still lose DI because
    # RejectionIntelligence re-invokes get_di_components; its guard must emit a
    # format-complete diagnostic.
    phase3 = [m for m, lvl in logs if str(m).startswith("[HEAVY_CALC]")
              and "stage=PHASE3_CONFIDENCE_GUARDS" in m]
    assert phase3, "no PHASE3 diagnostic emitted"
    p3 = phase3[0]
    assert "cycle=" in p3 and "symbol=BTR/USDT:USDT" in p3 and "trade_id=TRD-HEAVY-1001" in p3
    assert "stage=PHASE3_CONFIDENCE_GUARDS" in p3
    assert "exc_type=RuntimeError" in p3 and "exc=" in p3 and "ts=" in p3
    assert "Traceback (most recent call last)" in p3


# ------------------------------------------------ T8b
def test_t8b_phase2_guard_emits_format_complete_log_when_failure_engine_raises(
        monkeypatch, _reset):
    """If the thesis-failure engine itself throws (independent of DI), the
    phase-2 guard must still absorb it and emit the tuple-complete WARN
    diagnostic without stalling the management tick."""
    E.STATE.update(_open_state())
    E.STATE["open"] = True
    E.paper["position"] = {"side": "BUY", "entry": 99.9, "qty": 100.0,
                           "remaining_qty": 100.0}
    manager, _ = _make_manager(monkeypatch, mark=100.5, boom=False)  # DI OK
    monkeypatch.setattr(E, "apply_profit_engine", lambda *a, **k: "HOLD")

    def _boom(*a, **k):
        raise ValueError("thesis engine exploded")

    monkeypatch.setattr(manager.thesis_failure_engine, "evaluate_failure", _boom)
    logs = []
    monkeypatch.setattr(E, "log_execution",
                        lambda msg, level="INFO", **k: logs.append((msg, level)))

    manager._apply_management("BTR/USDT:USDT", time.time())

    p2 = [m for m, lvl in logs if str(m).startswith("[HEAVY_CALC]")
          and "stage=PHASE2_THESIS_FAILURE" in m]
    assert p2, "no PHASE2 diagnostic emitted"
    msg = p2[0]
    assert "cycle=" in msg and "symbol=BTR/USDT:USDT" in msg
    assert "trade_id=TRD-HEAVY-1001" in msg
    assert "stage=PHASE2_THESIS_FAILURE" in msg
    assert "exc_type=ValueError" in msg and "exc=" in msg and "ts=" in msg
    assert "Traceback (most recent call last)" in msg
    assert all(lvl == "WARN" for m, lvl in logs if str(m).startswith("[HEAVY_CALC]"))


# ------------------------------------------------ T9
def test_t9_last_heavy_calc_ts_advanced_on_exception_no_hammering(monkeypatch, _reset):
    E.STATE.update(_open_state())
    E.STATE["open"] = True
    E.paper["position"] = {"side": "BUY", "entry": 99.9, "qty": 100.0, "remaining_qty": 100.0}
    manager, calls = _make_manager(monkeypatch, mark=100.5)
    apply_count = [0]
    monkeypatch.setattr(E, "apply_profit_engine",
                        lambda *a, **k: (apply_count.__setitem__(0, apply_count[0] + 1), "HOLD")[1])
    now = time.time()

    manager._apply_management("BTR/USDT:USDT", now)
    assert calls["di"] >= 1
    assert manager.last_heavy_calc_ts == now             # timestamp advanced despite exception

    # Second tick inside the same window MUST take the else path: the heavy
    # block is gated on now - last_heavy_calc_ts >= 5, and the timestamp was
    # advanced on the failed tick, so the broken engine is NOT hammered again.
    manager._apply_management("BTR/USDT:USDT", now)
    assert manager.last_heavy_calc_ts == now             # still inside the 5s window
    assert apply_count[0] == 2                          # profit engine ran on both ticks


# ------------------------------------------------ T10
def test_t10_state_fallback_values_are_valid_for_apply_profit_engine(monkeypatch, _reset):
    E.STATE.update(_open_state())
    E.STATE["open"] = True
    E.paper["position"] = {"side": "BUY", "entry": 99.9, "qty": 100.0, "remaining_qty": 100.0}
    manager, calls = _make_manager(monkeypatch, mark=110.2, boom=True)
    # Last fallback shape: zero heavy analytics were ever computed, so all inputs
    # come from STATE defaults (the exact values the phase-1 except fallback and
    # the else-branch produce). Profit booking cannot depend on analytics.
    monkeypatch.setattr(E, "apply_profit_engine", lambda *a, **k: "HOLD")
    now = time.time()
    manager._apply_management("BTR/USDT:USDT", now)
    assert E.STATE["tp1_hit"] is False                   # apply masked; heavy boom tick completed unharmed

    manager.last_heavy_calc_ts = now                     # force else (STATE-only) path
    E.STATE["mark_price"] = 110.2
    monkeypatch.setattr(E, "_fresh_execution_mark", lambda s: 115.0)  # now past TP1
    # Real profit engine on the else path with pure fallback inputs must book TP1.
    monkeypatch.setattr(E, "apply_profit_engine", _REAL_APPLY_PROFIT_ENGINE)
    monkeypatch.setattr(E, "_record_partial_leg", lambda *a, **k: None)
    manager._apply_management("BTR/USDT:USDT", now)
    assert E.STATE["tp1_hit"] is True
    assert E.STATE["remaining_qty"] == 50.0
    assert E.STATE["profit_execution"]["verified"] is True
    assert E.STATE["profit_execution"]["stage"] == "TP1"