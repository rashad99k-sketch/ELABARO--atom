# EL-BARON PROFIT MANAGEMENT FORENSIC AUDIT

**Date:** 2026-09-16
**Branch/HEAD before fix:** `4c4439a` (BARON: fix TradFi classification and live candidate lifecycle)
**Engine:** `core/engine.py` (17,273 lines at HEAD)
**Scenario under audit:** BTR/USDT:USDT dashboard showed `TP1 progress = 100%`, `Action = WAIT_TP1`, `Remaining = 100%`, `Runner = OFF`, `Why: No management action recorded`.

---

## ROOT CAUSE

`LiveTradeManager._apply_management()` ran a heavy-calc block containing **~15 bare (unguarded) engine calls** — `get_di_components`, `trend_engine.analyze_pullback`, `trend_engine.get_trend_health`, `detect_structure_shift`, `SmartMoneyEngine.analyze_smart_money`, `MomentumFlowEngine.analyze_momentum_flow`, `regime_classifier.classify`, `brain.update`, `ifvg_warning_payload`, `continuation_pressure_engine.calculate_pressure`, `_continuation_engine.evaluate`, `thesis_failure_engine.evaluate_failure`, `confidence_engine.update_live_confidence`, `RejectionIntelligence`, `_compute_tp1_hold_score`, `_compute_institutional_exit_warning`. **If any single one throws, the whole management tick aborts before the canonical TP authority.**

## DEATH POINT

Any exception inside the heavy block escaped the block, was caught by **`PortfolioManager`'s per-symbol wrapper** (`portfolio/manager.py:653`, logs `[PORTFOLIO] manage {symbol}: {exc}` then continues to the next symbol), and the current tick **returned before `apply_profit_engine()` ever ran**. The trigger condition `touched1/valid_tp1` (engine.py:6368/6418) was never evaluated and `close_partial(stage="TP1")` (engine.py:3961) was never invoked → `tp1_hit` stayed `False`, `remaining_qty` stayed 100%, `position_action` stayed `None`. The dashboard rendered exactly the observed symptom via its fallbacks (dashboard/app.py:685-686): `Action = management_action or ("RUNNER" if tp1_hit else "WAIT_TP1")`, `Why = "No management action recorded"`.

## EXACT FILE / FUNCTION / CONDITION

| Item | Location |
|---|---|
| File | `core/engine.py` |
| Function | `LiveTradeManager._apply_management(self, symbol, now)` |
| Heavy-calc gate (pre-fix) | `if now - self.last_heavy_calc_ts >= 5:` — engine.py:5245 |
| Unguarded heavy-calc body | engine.py:5246-5413 (**the death block**) |
| Canonical TP authority (correctly placed AFTER the block, but unreachable) | `tp_action = apply_profit_engine(symbol, mark_price, df_live, len(df_live)-1, STATE)` — engine.py:5575 |
| Ownership catch that swallowed the tick | `portfolio/manager.py` `manage_all` per-symbol `try/except` — manager.py:653 |
| Trigger condition never reached | `apply_profit_engine` → `valid_tp1 = tp1 > entry and touched1` — engine.py:6361/6418 |
| Partial execution never reached | `close_partial(0.5, stage="TP1")` — engine.py:6443 (paper) / 4019-4038 (live) |
| Failure path that must never be silent | `_trade_event("TP1_EXECUTION_FAILED", ...)` — engine.py:6445/4105 |
| Visual progress (display-only, correct) | `tp1_progress_pct` — engine.py:7169; `_progress()` — manager.py:833 |
| Display fallbacks driving the symptom | `management_action` — engine.py:7176; dashboard Action/Why — app.py:685-686 |

## CONDITION (exact failing sequence)

1. A live position is in `LIFE` (engine.py:5209) and `now - last_heavy_calc_ts >= 5` (engine.py:5245).
2. Any one of the bare calls at engine.py:5246-5413 raises (e.g., `SmartMoneyEngine.analyze_smart_money(df_live)` or `brain.update(...)`).
3. Exception propagates: `_apply_management` → `manage_live_trade` (engine.py:5150) → `manage_all` (manager.py:626) where **manager.py:653**, `except Exception`, catches and swallows it.
4. `apply_profit_engine` (engine.py:5575) **never executes** for that tick — and would never execute on any tick where the heavy block throws.
5. `tp1_hit` remains `False`, `remaining_qty` remains 100%, no `profit_execution`, no `tp1_closed_qty` → dashboard: `TP1 progress 100%`, `Action WAIT_TP1`, `Remaining 100%`, `Runner OFF`, `Why: No management action recorded`.

## LATENCY

The death is **indefinite / non-self-healing**: every tick takes the heavy branch every 5 s and aborts in the same place. `last_heavy_calc_ts` was never advanced (it sits at the end of the block), so the retry fires every 5 s — a permanent stall, not a transient miss. Any profit window between the 100%-progress touch and SL/ratchet execution is lost; the position bleeds down while `Action=WAIT_TP1`.

## WHY (design flaw)

The heavy-calc block mixed three responsibilities into one unguarded region: (a) market analytics (advisory), (b) thesis-failure evaluation (which legitimately *decides* an exit), and (c) the **canonical profit-booking path** that must run every tick. A failure in advisory analytics was allowed to kill the execution-critical tail. The individual sub-calls for RSI/MACD/volume/VWAP/zones/VPA inside the block (engine.py:5305-5349) were already individually guarded — proving the team pattern was per-feature guards — but the surrounding engines were not.

## FIX (applied, surgical — Profit Management only)

No Scanner/Universe/Watchlist/RF/Entry/Institutional/Auction/SmartMoney/MomentumFlow/ContinuationProbability/TradeStateMachine/6-slot/News/Dashboard/Telegram/Exchange/Position-sizing/Leverage code was modified. Only `_apply_management`'s heavy block was phase-wrapped (engine.py:5249-5473):

1. **Management-cycle counter** — `_mgmt_cycle = getattr(self, "_management_cycle_id", 0) + 1`, persisted to `self._management_cycle_id`, plus `_tid = STATE.get("trade_id") or "UNKNOWN"` for every diagnostic.
2. **Phase 1 (wrapped)** — core analytics (DI/ADX, pullback, smart-money, momentum, regime, `brain.update`, phase-3 signals, IFVG, continuation pressure/eval). `except Exception as _phase1_err:` logs full context and installs the **exact else-branch fallback locals** (engine.py:5376-5401): `adx_live=20`, `di_*_live=20`, `smart_money={}`, `momentum_flow={}`, `trade_state="RANGE_CHOP"`, `advisory_*` defaults, `ContinuationEvaluation` from STATE, `tp1_hold_score=10`, `exit_warning=0`, `continuation_pressure=50`, `adx_slope=0`, `di_spread_change=0`, `thesis_failure_score=STATE`.
3. **Phase 2 (evaluation wrapped, action NOT wrapped)** — only `thesis_failure_engine.evaluate_failure(...)` is guarded; on failure it degrades to `failed=False, failure_reasons=[], failure_score=STATE`. The **exit action path (`if failed: ... close_position_full(); return`) stays unguarded** — the critical safety path must still raise loudly if it breaks.
4. **Phase 3 (wrapped)** — confidence update, institutional modifiers, rejection detection, hold-score, exit-warning; fallback keeps `tp1_hold_score`/`exit_warning` from STATE.
5. **`self.last_heavy_calc_ts = now` runs unconditionally** after the phases (engine.py:5473) → a failed block samples at the normal 5 s cadence; it never hammers the broken engine and never permanently wedges the 5 s gate.
6. **No broad try/except** hides the defect: every handler logs a structured `[HEAVY_CALC]` diagnostic with `cycle`, `symbol`, `trade_id`, `stage` (PHASE1_CORE_ANALYTICS / PHASE2_THESIS_FAILURE / PHASE3_CONFIDENCE_GUARDS), `exc_type`, `exc`, `ts`, and a full `traceback.format_exc()` at `WARN`.
7. **`apply_profit_engine()` is now reachable on every tick** regardless of analytics health; TP1 stays event-based (touch of the canonical stored/synthetic/dynamic target) — **never score-based**.

Diff: `core/engine.py` +203/-143. New test module: `tests/test_heavy_calc_profit_resilience.py`.

## REGRESSION + TP1 TESTS

**New suite (`tests/test_heavy_calc_profit_resilience.py`): 12/12 PASS**

| # | Test | Proves |
|---|---|---|
| T1 | `test_t1_heavy_exception_does_not_block_apply_profit_engine` | Heavy DI explosion inside the tick no longer prevents `apply_profit_engine()` |
| T2 | `test_t2_tp1_triggers_at_exactly_100_percent_progress` | TP1 fires at exactly 100% progress (mark == TP1) |
| T3 | `test_t3_tp1_triggers_past_100_percent_mark_above_tp1` | BTR case: mark ABOVE TP1 still books TP1 |
| T3b | `test_t3b_wick_touch_past_100_percent_fires_when_mark_below` | Wick-only touch past 100% fires TP1 |
| T4 | `test_t4_tp1_execution_verified_via_exchange_path` | Real `close_partial` PAPER path: `profit_execution.stage=TP1`, `verified=True`, 100→50 qty, tp1_closed_qty=50, SL→BE |
| T5 | `test_t5_tp2_fires_after_tp1_partial` | TP2 full-close after TP1 partial (sequence exactly one partial then one full) |
| T6 | `test_t6_partial_failure_surfaces_tp1_execution_failed` | `close_partial` failure → `TP1_EXECUTION_FAILED` + fail-closed `HOLD` (never silent) |
| T7 | `test_t7_adopted_position_tp1_price_zero_heals_via_synthetic` | Adopted position with `tp1_price=0` heals via `synthetic_tp1` |
| T7b | `test_t7b_no_target_no_spurious_close` | Zero targets → `HOLD`, no spurious close |
| T8 | `test_t8_heavy_exception_log_has_all_required_fields` | Log carries cycle/symbol/trade_id/stage/exc_type/exc/ts/full traceback at WARN; phase-2 self-heals |
| T9 | `test_t9_last_heavy_calc_ts_advanced_on_exception_no_hammering` | Timestamp advances on failure → no 5 s hammering; profit engine runs both ticks |
| T10 | `test_t10_state_fallback_values_are_valid_for_apply_profit_engine` | Pure STATE fallback values are sufficient for TP1 booking (analytics not required) |

**Full suite: 798 passed, 1 skipped, 0 failed (~5:38).** No regression across engine, portfolio, atom, position management, profit phase-3, TP1/TP2 canonical/alignment, protection firewall, runtime, dashboard, news, and scanner suites.

## VERDICT

The death point is enclosed. Advisory analytics failures are contained, diagnosable (`[HEAVY_CALC]` tuple-complete logs with management-cycle id), and degrade to the same STATE fallbacks the engine already uses between heavy ticks. The critical thesis-exit and close-verification paths remain unguarded so real execution failures still propagate. `apply_profit_engine()` is unconditionally reachable, TP1/TP2 architecture (50%/50%) is unchanged, and profit-taking is event-based only.