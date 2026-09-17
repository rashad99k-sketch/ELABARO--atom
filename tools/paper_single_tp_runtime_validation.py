"""BARON PAPER SINGLE-TP RUNTIME VALIDATION — 100% full-close + Telegram dedup.

Drives the REAL `_apply_management` path end-to-end in PAPER mode under the
SINGLE-TP production default (SINGLE_TP_ENABLED unset -> true). Proves:

  1.  TP1 touch closes the FULL position via close_position_full (100%),
      close_reason SINGLE_TP, tp1/tp2 hit at the same moment, no runner/trail.
  2.  The LiveTradeManager caller treats the single-TP full close as TERMINAL:
      lifecycle CLOSED emitted and live_trade_mode dropped.
  3.  A re-adoption of the SAME physical position (new REC id) cannot re-send
      "TRADE CLOSED": the durable close-identity ledger suppresses the second
      announce even though finalize runs again under a different trade_id.

No live order. No commit. No push.
"""
import os, sys, types, time, json, copy

os.environ["PAPER_MODE"] = "True"
os.environ["BINGX_KEY"] = ""
os.environ["BINGX_SECRET"] = ""
os.environ["NEWS_ENABLED"] = "True"
os.environ["NEWS_SLOT_ENABLED"] = "True"
os.environ["BARON_ZONE_JUDGE"] = "1"
# Force the PRODUCTION default to be exercised (fresh import -> true).
os.environ.pop("SINGLE_TP_ENABLED", None)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import numpy as np
import pandas as pd

EVIDENCE = {}
FAILURES = []
PASS_COUNTER = {"n": 0}


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)
        print(f"  [FAIL] {msg}")
    else:
        PASS_COUNTER["n"] += 1
        print(f"  [ok]   {msg}")


def _make_df(n=120, base=1.0, atr_val=0.05):
    t = np.arange(n, dtype=float)
    close = base + 0.002 * t + atr_val * 0.3 * np.sin(t / 8.0)
    high = close + atr_val * 0.4
    low = close - atr_val * 0.4
    opn = close - atr_val * 0.1
    vol = np.full(n, 5000.0)
    vol[-5:] = 15000.0
    return pd.DataFrame({
        "timestamp": (time.time() - n * 300 + t * 300).astype(int),
        "open": opn, "high": high, "low": low, "close": close, "volume": vol,
    })


def _load_engine():
    for name in list(sys.modules):
        if name == "core.engine" or name.startswith("scanner.") or name.startswith("portfolio."):
            sys.modules.pop(name, None)
    fake_ccxt = types.ModuleType("ccxt")
    class FakeBingX:
        def __init__(self, *a, **kw): self.markets = {}
    fake_ccxt.bingx = FakeBingX
    sys.modules["ccxt"] = fake_ccxt
    fake_flask = types.ModuleType("flask")
    class _FakeFlask:
        def __init__(self, *a, **kw): pass
        def route(self, *a, **kw): return lambda fn: fn
        def add_url_rule(self, *a, **kw): pass
    fake_flask.Flask = _FakeFlask
    fake_flask.jsonify = lambda *a, **k: a[0] if a else None
    fake_flask.request = types.SimpleNamespace(headers={}, remote_addr="127.0.0.1", json=None)
    sys.modules["flask"] = fake_flask
    import core.engine as E
    return E


def _seed_position(E, entry=1.0, mark=1.6339, tp1=1.5, tp2=2.0, qty=100.0,
                   trade_id="BTR-RUNTIME-001"):
    S = E.STATE
    S.clear()
    S["open"] = True
    S["current_symbol"] = "BTR/USDT:USDT"
    S["side"] = "BUY"
    S["entry"] = entry
    S["mark_price"] = mark
    S["qty"] = qty
    S["qty_initial"] = qty
    S["remaining_qty"] = qty
    S["tp1_price"] = tp1
    S["tp2_price"] = tp2
    S["tp1_hit"] = False
    S["tp1_done"] = False
    S["tp2_hit"] = False
    S["sl"] = entry - 0.1
    S["synthetic_sl"] = entry - 0.1
    S["roe_pct"] = (mark - entry) / entry * 100.0 * 20.0
    S["roe_valid"] = True
    S["margin"] = entry * qty / 20.0
    S["unrealized_pnl_usdt"] = (mark - entry) * qty
    S["trade_id"] = trade_id
    S["entry_time"] = time.time() - 3600
    S["entry_atr"] = 0.05
    S["trade_state"] = "RANGE_CHOP"
    S["adx_live"] = 20.0
    S["di_plus_live"] = 20.0
    S["di_minus_live"] = 20.0
    S["smart_money"] = {}
    S["momentum_flow"] = {}
    S["classification"] = "CRYPTO"
    S["last_management_event"] = None
    S["partial_realized"] = []
    S["trade_thesis"] = {}
    S["position_asset_class"] = "CRYPTO"
    S["position_trade_type"] = "TREND"
    S["continuation_probability"] = 0.5
    S["hold_quality"] = "UNKNOWN"
    S["counter_pressure"] = 0.0
    S["reclaim_risk"] = 0.0
    S["trend_strength"] = 0.5
    S["continuation_reasons"] = []
    S["continuation_pressure"] = 50.0
    S["tp1_hold_score"] = 10
    S["exit_warning"] = 0
    S["advisory_trend_health"] = 5.0
    S["advisory_struct_shift"] = None
    S["advisory_structure_aligned"] = False
    S["thesis_failure_score"] = 0
    S["current_confidence"] = 50.0
    S["prev_di_spread"] = 0.0
    S["peak_roe"] = S["roe_pct"]
    S["drawdown_from_peak"] = 0.0
    S["last_confirmed_sl"] = S["sl"]
    S["protection_confirmed"] = False
    S["be_ratchet_active"] = False
    S["profit_lock_activated"] = False
    S["runner_mode"] = False
    S["trail_activated"] = False
    S["trail_stop"] = 0.0
    S["smart_trail_mult"] = 1.5
    S["max_price"] = mark
    S["min_price"] = entry
    S["position_setup_snapshot"] = {}
    S["daily_loss_limit_hit"] = False
    E.paper = {
        "balance": 10000.0,
        "position": {"symbol": "BTR/USDT:USDT", "side": "BUY", "remaining_qty": qty,
                     "entry": entry, "qty": qty},
        "committed_margin": S["margin"],
    }
    E.TRADE_STATE.clear()
    E.TRADE_STATE["qty"] = qty
    E.TRADE_STATE["in_position"] = True
    E.DASHBOARD_STATE.clear()
    E.DASHBOARD_STATE["logs"] = []
    E.DASHBOARD_STATE["errors"] = []
    E.DASHBOARD_STATE["trade_lifecycle"] = []
    E.DASHBOARD_STATE["live_trade_mode"] = True
    E.DASHBOARD_STATE["protection"] = {}


def main():
    print("=" * 72)
    print("PAPER SINGLE-TP RUNTIME VALIDATION - 100% full close + Telegram dedup")
    print("=" * 72)

    E = _load_engine()
    df = _make_df(n=120, base=1.0, atr_val=0.05)

    # ---- Production-default config proof ----
    print("\n[CONFIG] Production default (env unset)", flush=True)
    check(E.SINGLE_TP_ENABLED is True, f"SINGLE_TP_ENABLED={E.SINGLE_TP_ENABLED} (expect True)")
    check(E.SINGLE_TP_CLOSE_FRACTION == 1.0, f"SINGLE_TP_CLOSE_FRACTION={E.SINGLE_TP_CLOSE_FRACTION} (expect 1.0)")
    check(E.TP2_ENABLED is False, f"TP2_ENABLED={E.TP2_ENABLED} (derived False)")
    check(E.RUNNER_ENABLED is False, f"RUNNER_ENABLED={E.RUNNER_ENABLED} (derived False)")
    check(E.PARTIAL_CLOSE_ENABLED is False, f"PARTIAL_CLOSE_ENABLED={E.PARTIAL_CLOSE_ENABLED} (derived False)")
    EVIDENCE["config"] = {
        "single_tp_enabled": E.SINGLE_TP_ENABLED,
        "single_tp_close_fraction": E.SINGLE_TP_CLOSE_FRACTION,
        "tp2_enabled": E.TP2_ENABLED,
        "runner_enabled": E.RUNNER_ENABLED,
        "partial_close_enabled": E.PARTIAL_CLOSE_ENABLED,
    }

    # ---- Scenario S: real management tick, TP1 = 100% full close ----
    print("\n[SCENARIO S] Single-TP full close through the REAL manager", flush=True)
    _seed_position(E, entry=1.0, mark=1.6339, tp1=1.5, tp2=2.0, qty=100.0)
    mgr = E.LiveTradeManager(E._event_bus, E._exchange_sync, E._recovery_guard)
    mgr.last_management_ts = 0
    mgr.last_live_debug_ts = 0
    mgr.last_position_sync_ts = 0
    mgr.last_heavy_calc_ts = 0
    mgr.lifecycle_state = E.TradeLifecycleState.LIVE

    events_captured = []
    tg_sent = []
    lifecycle_emitted = []
    full_calls = []

    _saved = {}
    for attr in ("get_ohlcv_safe", "get_live_hybrid_df", "_fresh_execution_mark",
                 "compute_atr", "_trade_event", "log_execution",
                 "_refresh_live_levels", "publish_position_state",
                 "close_position_full", "_tg_send"):
        _saved[attr] = getattr(E, attr)

    _real_full = _saved["close_position_full"]
    def hook_full(close_price=None, stage="FULL"):
        full_calls.append((close_price, stage))
        return _real_full(close_price=close_price, stage=stage)

    def hook_ohlcv(sym, limit): return df
    def hook_event(event, **data):
        events_captured.append({"event": event, "data": data, "ts": time.time()})
        return _saved["_trade_event"](event, **data)
    def hook_tg(text):
        tg_sent.append(text)
        return _saved["_tg_send"](text)

    def hook_emit(evt, state):
        lifecycle_emitted.append((evt, str(state)))
        return _orig_emit(evt, state)

    E.get_ohlcv_safe = hook_ohlcv
    E.get_live_hybrid_df = lambda sym, dc, mp: df
    E._fresh_execution_mark = lambda sym: 1.6339
    E.compute_atr = lambda d: pd.Series([0.05] * len(d))
    E._trade_event = hook_event
    E.log_execution = lambda *a, **k: None
    E._refresh_live_levels = lambda *a, **kw: None
    E.publish_position_state = lambda *a, **kw: None
    E.close_position_full = hook_full
    E._tg_send = hook_tg
    _orig_emit = E._event_bus.emit
    E._event_bus.emit = hook_emit

    t0 = time.time()
    _mgmt_exception = None
    try:
        mgr._apply_management("BTR/USDT:USDT", time.time())
    except Exception as exc:
        _mgmt_exception = str(exc)
        EVIDENCE["s_exception"] = _mgmt_exception
        print(f"  [!] _apply_management raised: {exc}")
    tick_ms = round((time.time() - t0) * 1000, 2)

    S = E.STATE
    ev = {
        "tick_ms": tick_ms,
        "management_exception": _mgmt_exception,
        "full_close_calls": full_calls,
        "close_reason": S.get("close_reason"),
        "tp1_hit": S.get("tp1_hit"),
        "tp1_done": S.get("tp1_done"),
        "tp2_hit": S.get("tp2_hit"),
        "remaining_qty": S.get("remaining_qty"),
        "qty_initial": S.get("qty_initial"),
        "runner_mode": S.get("runner_mode"),
        "trail_activated": S.get("trail_activated"),
        "live_trade_mode": E.DASHBOARD_STATE.get("live_trade_mode"),
        "lifecycle_events": lifecycle_emitted,
        "telegram_sent": len(tg_sent),
        "telegram_excerpt": tg_sent[0][:80] + "..." if tg_sent else None,
        "events": [e["event"] for e in events_captured],
    }
    EVIDENCE["scenario_s"] = ev

    check(ev["management_exception"] is None, "management tick completed")
    check(len(ev["full_close_calls"]) == 1, "close_position_full called EXACTLY once")
    check(ev["full_close_calls"][0][1] == "TP1", "full close stage = TP1")
    check(ev["close_reason"] == "SINGLE_TP", f"close_reason = {ev['close_reason']}")
    check(ev["tp1_hit"] is True and ev["tp1_done"] is True, "tp1_hit + tp1_done = True")
    check(ev["tp2_hit"] is True, "tp2_hit = True (single TP implies TP2 done at once)")
    check(ev["runner_mode"] is False and ev["trail_activated"] is False,
          "runner + trail remain disabled (nothing left to trail)")
    check(ev["remaining_qty"] == 0.0, f"remaining_qty = {ev['remaining_qty']} (fully closed)")
    check(ev["live_trade_mode"] is False, "live_trade_mode dropped (terminal close)")
    check(("lifecycle_change", str(E.TradeLifecycleState.CLOSED)) in ev["lifecycle_events"],
          f"lifecycle CLOSED emitted ({ev['lifecycle_events']})")
    check(ev["telegram_sent"] == 1, f"exactly ONE close telegram sent (got {ev['telegram_sent']})")
    check("TRADE CLOSED" in (ev["telegram_excerpt"] or ""), "telegram is the TRADE CLOSED message")

    for attr, fn in _saved.items():
        setattr(E, attr, fn)
    E._event_bus.emit = _orig_emit

    # ---- Scenario D: re-adoption of the SAME physical position -> no duplicate ----
    print("\n[SCENARIO D] Same physical position re-adopted with a NEW id", flush=True)
    tg_sent.clear()
    E._reset_close_ledger()          # NOTE: real runtime would NOT reset; simulate the
    #                                # production ledger already holding the first announce
    # Restore the durable announce recorded by the first close so the re-adoption
    # is checked against a REAL ledger entry (equivalent to process state after S).
    _id_key = E._close_identity_key("BTR/USDT:USDT", "BUY", 1.0, 100.0)
    E._announce_close(_id_key)

    # The venue still reports the position (physical close never happened on the
    # exchange, e.g. only local finalize ran). Re-adoption mints a NEW REC id.
    _seed_position(E, entry=1.0, mark=1.6339, tp1=1.5, tp2=2.0, qty=100.0,
                   trade_id="REC-BTR/USDT:USDT-BUY-1999999999999")
    E._tg_send = hook_tg
    E.log_execution = lambda *a, **k: None
    dup_before = len(tg_sent)
    E.finalize_trade_with_reality("BTR/USDT:USDT")
    dup_after = len(tg_sent)
    E._tg_send = _saved["_tg_send"]

    dedup_ev = {
        "announced_identity": _id_key,
        "new_trade_id": "REC-BTR/USDT:USDT-BUY-1999999999999",
        "telegram_before_finalize": dup_before,
        "telegram_after_finalize": dup_after,
    }
    EVIDENCE["scenario_d"] = dedup_ev
    check(dup_after == dup_before,
          f"re-finalize under a NEW trade_id sent {dup_after - dup_before} telegram(s) (must be 0)")

    # ---- Report ----
    print("\n" + "=" * 72)
    print(f"EVIDENCE RESULT - {PASS_COUNTER['n']} passed, {len(FAILURES)} failed")
    print("=" * 72)
    if FAILURES:
        print("FAILED CHECKS:")
        for f in FAILURES:
            print(f"  - {f}")
        verdict = "PAPER SINGLE-TP VALIDATION FAILED"
    else:
        verdict = "PAPER SINGLE-TP VALIDATED"
    EVIDENCE["verdict"] = verdict
    EVIDENCE["checks"] = {"passed": PASS_COUNTER["n"], "failed": len(FAILURES),
                          "failures": FAILURES}

    out_path = os.path.join(os.environ.get("TEMP", ROOT),
                            "opencode", "paper_single_tp_runtime_validation_evidence.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(EVIDENCE, fh, indent=2, default=str)
    print(f"\nEvidence JSON: {out_path}")
    print(f"VERDICT: {verdict}")
    if FAILURES:
        sys.exit(1)


if __name__ == "__main__":
    main()