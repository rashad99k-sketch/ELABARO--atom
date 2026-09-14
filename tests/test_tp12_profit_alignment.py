"""Surgical TP1/TP2 profit-alignment tests (professional review fixes).

Covers the four code-verified failure classes that let a position reach
100% TP1 progress without booking profit:

C1  sampled-mark-only trigger -> a touch between 2-10s reconcile samples was
    permanently missed (no wick detection).
C2  bare get() on *_price killed TP1/TP2 when *_price was 0 while
    synthetic_*/dynamic_* stayed valid (dashboard showed target, engine dead).
C3  remaining_qty <= 0 silently hard-blocked a genuine TP1 touch.
C4  close_partial verification failure was silent (retry only on next touch).

Plus regression guards: geometry, no double TP1, mark-based trigger intact.
"""
import pandas as pd
import pytest

import core.engine as E


def _df(close, high=None, low=None):
    high = float(close) if high is None else float(high)
    low = float(close) if low is None else float(low)
    return pd.DataFrame({
        "open": [float(close)],
        "high": [high],
        "low": [low],
        "close": [float(close)],
        "volume": [1.0],
    })


def _buyp_state(**kw):
    state = {
        "open": True, "side": "BUY", "entry": 0.25,
        "tp1_price": 0.2560, "tp2_price": 0.2590,
        "synthetic_tp1": 0.2560, "synthetic_tp2": 0.2590,
        "dynamic_tp1": 0.2560, "dynamic_tp2": 0.2590,
        "remaining_qty": 100.0, "qty_initial": 100.0, "qty": 100.0,
        "tp1_hit": False, "tp2_hit": False,
    }
    state.update(kw)
    return state


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


def test_mark_trigger_still_works(monkeypatch, capture):
    st = _buyp_state()
    action = E.apply_profit_engine("TEST/USDT:USDT", 0.2565, _df(0.25, high=0.25), 0, st)
    assert action == "TP1"
    assert capture["partial"] == [(0.5, "TP1")]
    assert st["tp1_hit"] is True


def test_tp1_zero_price_heals_via_synthetic(capture):
    st = _buyp_state(tp1_price=0.0, synthetic_tp1=0.2560, dynamic_tp1=0.2560)
    action = E.apply_profit_engine("TEST/USDT:USDT", 0.2565, _df(0.25, high=0.25), 0, st)
    assert action == "TP1"
    assert capture["partial"] == [(0.5, "TP1")]
    assert st["tp1_hit"] is True


def test_tp2_zero_price_heals_via_synthetic(capture):
    st = _buyp_state(tp1_hit=True, tp2_price=0.0, synthetic_tp2=0.2590,
                     dynamic_tp2=0.2590)
    action = E.apply_profit_engine("TEST/USDT:USDT", 0.2595, _df(0.25, high=0.25), 0, st)
    assert action == "TP2"
    assert capture["full"] == [(0.2595, "TP2")]
    assert st["tp2_hit"] is True


def test_sell_tp1_then_tp2_geometry(capture):
    st = {"open": True, "side": "SELL", "entry": 0.25,
          "tp1_price": 0.2440, "tp2_price": 0.2410,
          "synthetic_tp1": 0.2440, "synthetic_tp2": 0.2410,
          "dynamic_tp1": 0.2440, "dynamic_tp2": 0.2410,
          "remaining_qty": 100.0, "qty_initial": 100.0,
          "tp1_hit": False, "tp2_hit": False}
    a1 = E.apply_profit_engine("TEST/USDT:USDT", 0.2435, _df(0.25, low=0.25), 0, st)
    assert a1 == "TP1"
    st["tp1_hit"] = True
    a2 = E.apply_profit_engine("TEST/USDT:USDT", 0.2405, _df(0.25, low=0.25), 0, st)
    assert a2 == "TP2"
    assert capture["full"] == [(0.2405, "TP2")]


def test_wick_touch_fires_when_mark_below(capture):
    st = _buyp_state()
    df = _df(0.2540, high=0.2565)
    action = E.apply_profit_engine("TEST/USDT:USDT", 0.2540, df, 0, st)
    assert action == "TP1"
    assert capture["partial"] == [(0.5, "TP1")]


def test_no_false_positive_when_wick_below(capture):
    st = _buyp_state()
    df = _df(0.2538, high=0.2555)
    action = E.apply_profit_engine("TEST/USDT:USDT", 0.2538, df, 0, st)
    assert action == "HOLD"
    assert capture["partial"] == []


def test_remaining_zero_healed_from_qty_initial(capture):
    st = _buyp_state(remaining_qty=0.0)
    action = E.apply_profit_engine("TEST/USDT:USDT", 0.2565, _df(0.25, high=0.25), 0, st)
    assert action == "TP1"
    assert st["remaining_qty"] == 100.0
    assert capture["partial"] == [(0.5, "TP1")]


def test_no_double_tp1(capture):
    st = _buyp_state(tp1_hit=True)
    action = E.apply_profit_engine("TEST/USDT:USDT", 0.2570, _df(0.25, high=0.2570), 0, st)
    assert action == "HOLD"
    assert capture["partial"] == []
    assert capture["full"] == []


def test_tp2_geometry_requires_above_entry(capture):
    st = _buyp_state(tp1_hit=True, tp2_price=0.2490, synthetic_tp2=0.2490,
                     dynamic_tp2=0.2490)
    action = E.apply_profit_engine("TEST/USDT:USDT", 0.2510, _df(0.25, high=0.2510), 0, st)
    assert action == "HOLD"
    assert capture["full"] == []


def test_partial_failure_emits_execution_failed(monkeypatch):
    events = []
    monkeypatch.setattr(E, "close_partial", lambda ratio, stage="PARTIAL": False)
    monkeypatch.setattr(E, "_trade_event", lambda *a, **k: events.append((a, k)))
    st = _buyp_state()
    action = E.apply_profit_engine("TEST/USDT:USDT", 0.2565, _df(0.25, high=0.25), 0, st)
    assert action == "HOLD"
    assert st["tp1_hit"] is False
    assert any("TP1_EXECUTION_FAILED" in a for a, k in events)


def test_price_zero_uses_wick_only(capture):
    st = _buyp_state()
    df = _df(0.2565, high=0.2565)
    action = E.apply_profit_engine("TEST/USDT:USDT", 0.0, df, 0, st)
    assert action == "TP1"
    assert capture["partial"] == [(0.5, "TP1")]


def test_mark_freshness_helper_prefers_fresh_ticker(monkeypatch):
    E.STATE["mark_price"] = 0.5000
    monkeypatch.setattr(E, "get_ticker_safe", lambda symbol: 0.9990)
    assert E._fresh_execution_mark("TUNNEL/USDT:USDT") == 0.9990


def test_mark_freshness_helper_falls_back_to_cached(monkeypatch):
    E.STATE["mark_price"] = 0.6000
    monkeypatch.setattr(E, "get_ticker_safe", lambda symbol: None)
    assert E._fresh_execution_mark("TUNNEL/USDT:USDT") == 0.6000


def test_mark_freshness_helper_returns_none_when_unknown(monkeypatch):
    E.STATE["mark_price"] = 0.0
    monkeypatch.setattr(E, "get_ticker_safe", lambda symbol: None)
    assert E._fresh_execution_mark("TUNNEL/USDT:USDT") is None


@pytest.fixture(autouse=True)
def _hybrid_wick_state():
    saved = (dict(E._live_high), dict(E._live_low),
             dict(E._last_candle_timestamp), dict(E._last_candle_base))
    E._live_high.clear()
    E._live_low.clear()
    E._last_candle_timestamp.clear()
    E._last_candle_base.clear()
    yield
    E._live_high.clear()
    E._live_low.clear()
    E._last_candle_timestamp.clear()
    E._last_candle_base.clear()
    E._live_high.update(saved[0])
    E._live_low.update(saved[1])
    E._last_candle_timestamp.update(saved[2])
    E._last_candle_base.update(saved[3])


def test_wick_accumulation_retained_on_unchanged_candle():
    base = _df(0.2500, high=0.2500, low=0.2498)
    E.get_live_hybrid_df("SYM", base, 0.2520)
    res = E.get_live_hybrid_df("SYM", _df(0.2500, high=0.2500, low=0.2498), 0.2540)
    assert float(res.iloc[-1]["high"]) == 0.2540


def test_wick_state_reset_when_candle_data_changed():
    E.get_live_hybrid_df("SYM", _df(0.2560, high=0.2580, low=0.2540), 0.2590)
    res = E.get_live_hybrid_df("SYM", _df(0.2510, high=0.2515, low=0.2505), 0.2510)
    assert float(res.iloc[-1]["high"]) == 0.2515


def test_stale_candle_high_cannot_fire_tp1(capture):
    st = _buyp_state()
    E.get_live_hybrid_df("SYM", _df(0.2560, high=0.2580, low=0.2540), 0.2590)
    df = E.get_live_hybrid_df("SYM", _df(0.2510, high=0.2515, low=0.2505), 0.2510)
    action = E.apply_profit_engine("SYM/USDT:USDT", 0.2510, df, 0, st)
    assert action == "HOLD"
    assert capture["partial"] == []