"""BARON PAPER BINGX CLOSE-AUTHORITY RUNTIME VALIDATION.

Drives the REAL `close_position_full` verified-close transaction in LIVE mode
(turn live safe_api_call + order creation against a SCRIPTED exchange) to prove
the exchange is the authoritative source of close truth:

  1. Position mode is detected from the venue (fetch_position_mode), not guessed:
     hedge   -> {"positionSide": "LONG"/"SHORT"} with NO reduceOnly
     one-way -> {"positionSide": "BOTH", "reduceOnly": true}
     unknown -> fail closed, no order, CLOSE_POSITION_MODE_UNKNOWN
  2. Close quantity is the EXCHANGE qty (100% of what the venue holds), never a
     stale local figure.
  3. After the close order, the venue must report the leg at zero before any
     finalize; a bounded re-confirm that ends PRESENT/UNKNOWN never closes.
  4. Unfilled order / order-id-but-leg-still-open / transient PAUSED/ERROR keep
     the position OPEN; only a positive exchange-zero confirm closes it.
  5. No opposite position can ever be fashioned: LONG->SELL only, SHORT->BUY only,
     and reduceOnly=true in one-way mode.

No live network access, no real keys, no commit, no push. LIVE EXECUTION NOT
PERFORMED -- this runs against an in-process scripted BingX venue.
"""
import os, sys, types, time, json, copy

os.environ["PAPER_MODE"] = "False"
os.environ["BINGX_KEY"] = ""
os.environ["BINGX_SECRET"] = ""
os.environ["NEWS_ENABLED"] = "True"
os.environ["NEWS_SLOT_ENABLED"] = "True"
os.environ["BARON_ZONE_JUDGE"] = "1"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

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


def _leg(side, contracts, symbol="BTR/USDT:USDT"):
    return {"symbol": symbol, "side": side, "contracts": contracts,
            "entryPrice": 1.0, "markPrice": 1.6339}


class _Venue:
    """In-process scripted BingX swap venue (standard ccxt bingx contract)."""

    def __init__(self, script, hedged=None):
        self.script = list(script)
        self.created = []
        self.hedged = hedged
        self.amount_to_precision = lambda s, q: q
        self.markets = {
            "BTR/USDT:USDT": {
                "limits": {"amount": {"min": 0.001}, "cost": {"min": 0.05}},
                "precision": {"amount": 8},
            },
            "BTR/USDT": {
                "limits": {"amount": {"min": 0.001}, "cost": {"min": 0.05}},
                "precision": {"amount": 8},
            },
        }

    def fetch_positions(self, *a, **k):
        if self.script:
            return self.script.pop(0)
        return []

    def fetch_position_mode(self, *a, **k):
        return {"hedged": self.hedged}

    def market(self, sym):
        return dict(self.markets.get(sym, {}))

    def create_order(self, sym, order_type, side, amount, params=None):
        params = params or {}
        rec = {"sym": sym, "type": order_type, "side": side, "amount": amount,
               "params": params, "id": "mock-order-%d" % len(self.created),
               "status": "closed", "average": 1.6339}
        self.created.append(rec)
        return {"id": rec["id"], "status": "closed", "average": 1.6339}


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
    E.CLOSE_CONFIRM_TIMEOUT = 0.2
    E.CLOSE_CONFIRM_INTERVAL = 0.02
    return E


def _seed_position(E, side="BUY", mark=1.6339, qty=0.1, trade_id="BTR-CLOSE-AUTH-001"):
    S = E.STATE
    S.clear()
    S["open"] = True
    S["current_symbol"] = "BTR/USDT:USDT"
    S["side"] = side
    S["entry"] = 1.0
    S["mark_price"] = mark
    S["qty"] = qty
    S["qty_initial"] = qty
    S["remaining_qty"] = qty
    S["entry_time"] = time.time() - 3600
    S["trade_id"] = trade_id
    S["close_reason"] = "TP1"
    E.TRADE_STATE.clear()
    E.TRADE_STATE["qty"] = qty
    E.TRADE_STATE["in_position"] = True
    E.DASHBOARD_STATE.clear()
    E.DASHBOARD_STATE["logs"] = []
    E.DASHBOARD_STATE["errors"] = []
    E.DASHBOARD_STATE["trade_lifecycle"] = []
    E.DASHBOARD_STATE["live_trade_mode"] = True
    E.MEMORY["close_log"] = []
    E.paper = None


def _close_with_fill(E, filled_qty=0.1, filled=True):
    """Run close_position_full with a deterministic fill-verify so the close
    transaction reaches the post-fill venue confirm step without network."""
    _orig = E.verify_order_filled
    E.verify_order_filled = lambda *a, **k: (filled, filled_qty)
    try:
        return E.close_position_full(stage="TP1")
    finally:
        E.verify_order_filled = _orig


def main():
    print("=" * 72)
    print("PAPER BINGX CLOSE-AUTHORITY RUNTIME VALIDATION (scripted venue)")
    print("=" * 72)

    E = _load_engine()
    _reset_cache(E)
    saved_verify = E.verify_order_filled
    saved_finalize = E.finalize_trade_with_reality

    EVIDENCE["runtime"] = {
        "mode": "PAPER/scripted venue (ccxt bingx contract reproduced in-process)",
        "live_execution": "NOT PERFORMED - no network, no real keys",
        "ccxt_venue_methods": ["create_order", "fetch_positions",
                               "fetch_position_mode", "market",
                               "amount_to_precision"],
    }

    # ---------------------------------------------------------------- HEDGE 1
    print("\n[SCENARIO H1] LONG single-TP close, HEDGE mode, exchange qty source", flush=True)
    _seed_position(E, side="BUY", qty=0.1)
    E.STATE["remaining_qty"] = 5.0  # stale local figure; venue holds 0.1
    venue = _Venue([[_leg("long", 0.1)], [], []], hedged=True)
    E.ex = venue
    _reset_cache(E)
    ok = _close_with_fill(E, filled_qty=0.1)
    h1_rec = venue.created[0] if venue.created else {}
    h1 = {
        "ok": bool(ok),
        "orders": len(venue.created),
        "side": h1_rec.get("side"),
        "positionSide": h1_rec.get("params", {}).get("positionSide"),
        "reduceOnly": h1_rec.get("params", {}).get("reduceOnly"),
        "amount": h1_rec.get("amount"),
        "state_open": bool(E.STATE.get("open")),
    }
    EVIDENCE["scenario_h1"] = h1
    check(h1["ok"] is True, "LONG TP1 close succeeded")
    check(h1["orders"] == 1, f"exactly 1 close order ({h1['orders']})")
    check(h1["side"] == "sell", f"LONG closed with SELL ({h1['side']}) - never fashions SHORT")
    check(h1["positionSide"] == "LONG", f"hedge mode positionSide=LONG ({h1['positionSide']})")
    check(h1["reduceOnly"] is None, "hedge mode omits reduceOnly per BingX docs")
    h1_amount = h1["amount"] or 0.0
    check(abs(h1_amount - 0.1) < 1e-12,
          f"close amount = exchange qty 0.1, not stale local 5.0 ({h1_amount})")
    check(h1["state_open"] is False, "STATE closed only after venue confirmed zero")

    # ---------------------------------------------------------------- HEDGE 2
    print("\n[SCENARIO H2] SHORT single-TP close, HEDGE mode", flush=True)
    _seed_position(E, side="SELL", qty=0.1)
    venue = _Venue([[_leg("short", 0.1)], [], []], hedged=True)
    E.ex = venue
    _reset_cache(E)
    ok = _close_with_fill(E, filled_qty=0.1)
    h2_rec = venue.created[0] if venue.created else {}
    h2 = {
        "ok": bool(ok), "orders": len(venue.created),
        "side": h2_rec.get("side"),
        "positionSide": h2_rec.get("params", {}).get("positionSide"),
        "reduceOnly": h2_rec.get("params", {}).get("reduceOnly"),
    }
    EVIDENCE["scenario_h2"] = h2
    check(h2["ok"] is True, "SHORT TP1 close succeeded")
    check(h2["side"] == "buy", f"SHORT closed with BUY ({h2['side']}) - never fashions LONG")
    check(h2["positionSide"] == "SHORT", f"hedge mode positionSide=SHORT ({h2['positionSide']})")
    check(h2["reduceOnly"] is None, "hedge mode omits reduceOnly")

    # ---------------------------------------------------------------- ONEWAY 1
    print("\n[SCENARIO O1] LONG single-TP close, ONE-WAY mode (BOTH + reduceOnly)", flush=True)
    _seed_position(E, side="BUY", qty=0.1)
    venue = _Venue([[_leg("long", 0.1)], [], []], hedged=False)
    E.ex = venue
    _reset_cache(E)
    ok = _close_with_fill(E, filled_qty=0.1)
    o1_rec = venue.created[0] if venue.created else {}
    o1 = {"ok": bool(ok), "orders": len(venue.created),
          "side": o1_rec.get("side"),
          "positionSide": o1_rec.get("params", {}).get("positionSide"),
          "reduceOnly": o1_rec.get("params", {}).get("reduceOnly")}
    EVIDENCE["scenario_o1"] = o1
    check(o1["ok"] is True, "one-way LONG TP1 close succeeded")
    check(o1["side"] == "sell", "one-way LONG still closes with SELL")
    check(o1["positionSide"] == "BOTH", f"one-way mode positionSide=BOTH ({o1['positionSide']})")
    check(o1["reduceOnly"] is True, "one-way mode sends reduceOnly=true (guards the opposite)")

    # ---------------------------------------------------------------- ONEWAY 2
    print("\n[SCENARIO O2] SHORT single-TP close, ONE-WAY mode", flush=True)
    _seed_position(E, side="SELL", qty=0.1)
    venue = _Venue([[_leg("short", 0.1)], [], []], hedged=False)
    E.ex = venue
    _reset_cache(E)
    ok = _close_with_fill(E, filled_qty=0.1)
    o2_rec = venue.created[0] if venue.created else {}
    o2 = {"ok": bool(ok), "orders": len(venue.created),
          "side": o2_rec.get("side"),
          "positionSide": o2_rec.get("params", {}).get("positionSide"),
          "reduceOnly": o2_rec.get("params", {}).get("reduceOnly")}
    EVIDENCE["scenario_o2"] = o2
    check(o2["ok"] is True, "one-way SHORT TP1 close succeeded")
    check(o2["side"] == "buy", "one-way SHORT still closes with BUY")
    check(o2["positionSide"] == "BOTH", f"one-way SHORT positionSide=BOTH ({o2['positionSide']})")
    check(o2["reduceOnly"] is True, "one-way SHORT reduceOnly=true")

    # ----------------------------------------------------------------- UNKNOWN
    print("\n[SCENARIO U] venue position mode UNKNOWN -> fail closed, no guess", flush=True)
    _seed_position(E, side="BUY", qty=0.1)
    venue = _Venue([[_leg("long", 0.1)]], hedged=None)
    E.ex = venue
    _reset_cache(E)
    with _abort_mode(E, "raise"):
        ok = E.close_position_full(stage="TP1")
    u_rec = venue.created
    u = {"ok": bool(ok), "orders": len(u_rec),
         "state_open": bool(E.STATE.get("open"))}
    EVIDENCE["scenario_u"] = u
    check(u["ok"] is False, "unknown mode close fails closed (returns False)")
    check(u["orders"] == 0, f"no close order when mode unknown ({u['orders']})")
    check(u["state_open"] is True, "position stays open when the mode is unknown")

    # ---------------------------------------------------- ORDER ID BUT PRESENT
    print("\n[SCENARIO P] order ID returned but venue leg still open -> stays OPEN", flush=True)
    _seed_position(E, side="BUY", qty=0.1)
    venue = _Venue([], hedged=True)
    E.ex = venue
    _reset_cache(E)
    venue.fetch_positions = lambda *a, **k: [_leg("long", 0.1)]  # never goes absent
    ok = _close_with_fill(E, filled_qty=0.1)  # fill succeeds but position never vanishes
    p = {"ok": bool(ok), "orders": len(venue.created),
         "state_open": bool(E.STATE.get("open"))}
    EVIDENCE["scenario_p"] = p
    check(p["ok"] is False, "close fails when confirm never reaches zero")
    check(p["state_open"] is True, "position remains OPEN despite order ID existing")

    # ---------------------------------------------------------------- UNFILLED
    print("\n[SCENARIO F] close order never fills -> position stays OPEN", flush=True)
    _seed_position(E, side="BUY", qty=0.1)
    venue = _Venue([[_leg("long", 0.1)], [_leg("long", 0.1)],
                    [_leg("long", 0.1)], [_leg("long", 0.1)]], hedged=True)
    E.ex = venue
    _reset_cache(E)
    E.verify_order_filled = lambda *a, **k: (False, 0.0)
    saved_time_sleep = E.time.sleep
    E.time.sleep = lambda *a: None
    try:
        ok = E.close_position_full(stage="TP1")
    finally:
        E.time.sleep = saved_time_sleep
        E.verify_order_filled = saved_verify
    f = {"ok": bool(ok), "orders": len(venue.created),
         "state_open": bool(E.STATE.get("open"))}
    EVIDENCE["scenario_f"] = f
    check(f["ok"] is False, "unfilled close returns False")
    check(f["state_open"] is True, "unfilled close NEVER closes local state")

    # ---------------------------------------------------------------- PAUSED
    print("\n[SCENARIO T] transient venue PAUSED -> bounded, stays OPEN", flush=True)
    _seed_position(E, side="BUY", qty=0.1)
    venue = _Venue([[_leg("long", 0.1)]], hedged=True)
    E.ex = venue
    _reset_cache(E)
    _orig_fps = E.fetch_position_status
    E.fetch_position_status = lambda *a, **k: (None, "PAUSED")
    try:
        ok = E.close_position_full(stage="TP1")
    finally:
        E.fetch_position_status = _orig_fps
    t = {"ok": bool(ok), "orders": len(venue.created),
         "state_open": bool(E.STATE.get("open"))}
    EVIDENCE["scenario_t"] = t
    check(t["ok"] is False, "PAUSED venue refuses close")
    check(t["state_open"] is True, "transient error does not mark position closed")

    # ------------------------------------------------------------ EVENTUAL ZERO
    print("\n[SCENARIO Z] eventual zero after bounded re-confirm -> CLOSED", flush=True)
    _seed_position(E, side="BUY", qty=0.1)
    calls = {"n": 0}
    venue = _Venue([], hedged=True)
    E.ex = venue
    _reset_cache(E)

    def eventual(*a, **k):
        calls["n"] += 1
        return [_leg("long", 0.1)] if calls["n"] == 1 else []
    venue.fetch_positions = eventual
    ok = _close_with_fill(E, filled_qty=0.1)
    z = {"ok": bool(ok), "pos_queries": calls["n"],
         "orders": len(venue.created), "state_open": bool(E.STATE.get("open"))}
    EVIDENCE["scenario_z"] = z
    check(z["ok"] is True, "eventual-zero confirm closes the position")
    check(z["pos_queries"] >= 2, f"bounded re-confirm re-queried the venue ({z['pos_queries']} queries)")
    check(z["state_open"] is False, "STATE closed only on exchange zero")

    # ------------------------------------------------- EXTERNAL RECONCILIATION
    print("\n[SCENARIO R] external closure reconciled once, zero orders", flush=True)
    _seed_position(E, side="BUY", qty=0.1)
    venue = _Venue([[], []], hedged=True)
    E.ex = venue
    _reset_cache(E)
    ok = E.close_position_full(stage="TP1")
    r = {"ok": bool(ok), "orders": len(venue.created),
         "state_open": bool(E.STATE.get("open"))}
    EVIDENCE["scenario_r"] = r
    check(r["ok"] is True, "external closure reconciles as ALREADY_CLOSED")
    check(r["orders"] == 0, f"zero close orders for an externally-closed position ({r['orders']})")
    check(r["state_open"] is False, "state closed to match exchange truth")

    # ------------------------------------------------------------------- REPORT
    print("\n" + "=" * 72)
    print(f"EVIDENCE RESULT - {PASS_COUNTER['n']} passed, {len(FAILURES)} failed")
    print("=" * 72)
    if FAILURES:
        print("FAILED CHECKS:")
        for f_ in FAILURES:
            print(f"  - {f_}")
        verdict = "PAPER CLOSE-AUTHORITY VALIDATION FAILED"
    else:
        verdict = "PAPER CLOSE-AUTHORITY VALIDATED (scripted venue)"
    EVIDENCE["verdict"] = verdict
    EVIDENCE["checks"] = {"passed": PASS_COUNTER["n"], "failed": len(FAILURES),
                          "failures": FAILURES}

    out_path = os.path.join(os.environ.get("TEMP", ROOT),
                            "opencode", "paper_bingx_close_authority_validation_evidence.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(EVIDENCE, fh, indent=2, default=str)
    print(f"\nEvidence JSON: {out_path}")
    print(f"VERDICT: {verdict}")
    if FAILURES:
        sys.exit(1)


def _reset_cache(E):
    try:
        E._reset_position_mode_cache()
    except Exception:
        pass


def _abort_mode(E, how):
    """Monkeypatch context: make _detect_position_mode return 'unknown'."""
    from unittest import mock
    return mock.patch.object(E, "_detect_position_mode", return_value="unknown")


if __name__ == "__main__":
    main()