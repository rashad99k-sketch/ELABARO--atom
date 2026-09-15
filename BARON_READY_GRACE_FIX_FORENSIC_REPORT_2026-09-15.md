# BARON READY-GRACE FIX — FINAL FORENSIC REPORT (2026-09-15)

Release: `BARON_READY_GRACE_FIX_RELEASE_2026-09-15.zip`
SHA-256: `1F822D33271D2A3741B6CE583F5301CF21EEFD85A1FC3C562F6EFE9201311B85`

---

## 1. SYMPTOM (Production Zero-Trade Funnel)
Dashboard showed `READY > 0` candidates and `OPEN_REQUESTED` lifecycle events but
`EXECUTED = 0` for weeks. No liquidity, protection, sizing, or balance errors
appeared — every candidate silently died after `OPEN_REQUESTED`.

## 2. EVIDENCE OF THE FAILURE
For a queue-READY BTC BUY exploit/opportunity frame,
`core/engine.py execute_entry()`:
```
[BARON_JUDGE] BTC/USDT:USDT BUY -> WAIT_RETEST | score=58.7
[BARON_JUDGE] BTC/USDT:USDT BUY WAIT_RETEST admitted by READY-execution grace (90s) | score=58.7
[LIFECYCLE] NEW state: OPEN_PENDING_CONFIRMATION
[LIFECYCLE] ... open requested for BTC/USDT:USDT BUY
[EXECUTION] BTC/USDT:USDT BUY executed (paper) at 60000.0000
[PORTFOLIO] Opened BTC/USDT:USDT BUY | slot 1/6
[QUEUE] READY -> EXECUTED BTC/USDT:USDT BUY priority=71.4
```
Before the fix the same frame ended in `BARON_REJECT decision=WAIT_RETEST
score=58.7` immediately after `OPEN_REQUESTED`.

## 3. REPRODUCTION METHOD
Unit-level (offline): provider boundary stubbed (OHLCV/ticker/orderbook),
`BARON_ZONE_JUDGE=1` (the production default; conftest pins `=0` for legacy
fixtures). Driving `core.runtime._execute_ready_queue_candidate()`.
- With `BARON_ZONE_JUDGE=1` the READY candidate was rejected (EXECUTED=0).
- With `BARON_ZONE_JUDGE=0` the same code path returned `executed` and
  portfolio count = 1.
Both behaviours reproduced deterministically in 10+ runs.

## 4. ROOT CAUSE CONFIRMED
Two competing scoring authorities with materially different weights and bars:
- Queue READY floor = class-aware (CRYPTO 68 / INDEX 69 / GOLD-OIL 70), computed
  by `AssetBehaviorProfile.entry_config().ready_score`
  (`core/engine.py` 16121 / 2780-2787); candidate priority = 88 for the frame.
- BARON ZONE/OB judge `baron_zone_judge.py` scored the SAME frame 58.7 and
  required `final_zone_score >= 72 + struct_ok + rejection/displacement/sweep`
  for `ENTER_NOW`, else fail-closed.
The judge returned `WAIT_RETEST`, which the gate treated as a hard block. The
judge's own contract (`baron_zone_judge.py`) defines WAIT_RETEST as **"zone is
VALID, retest/confirmation pending"** — i.e. the queue's READY authority had
already answered that question (in-entry-window + trigger + 2 confirmations +
not-extended, re-verified each `QUEUE_RE_EVAL_INTERVAL`). Applying a second,
different threshold on top is exactly the hidden-duplicate-threshold defect.

## 5. FIX DESIGN (surgical; preserves every existing authority)
`WAIT_RETEST` is **advisory only** for a fresh queue-READY grant, using the
existing `_ready_execution_grace` contract (`core/engine.py` 8563-8593) that the
ADX/liquidity re-checks already use. Everything else stays fail-closed:
- `BLOCK` verdicts (zone broken, price extended, OB consumed/invalidated, S/R
  flip, unusable data) -> `BARON_REJECT` for EVERY candidate, READY or not.
- `WAIT_RETEST` + grace (`execution_context.is_ready_validated` and `ready_ts`
  within `EXECUTION_READY_GRACE_SEC=90`) -> advisory `BARON_ADVISORY` record.
- `WAIT_RETEST` without grace (non-READY / fallback / stale) -> `BARON_REJECT`.
- Judge exception -> `BARON_ERROR` fail-closed.
The legacy second authority (`process_queue_entry`, no execution_context) stays
inert because it can never satisfy the grace — no code change needed there.

WHY THE GRACE IS NOT A GENERAL BYPASS (reviewer focus): the 90s window is NOT
"wait 90 seconds then open anything". It applies ONLY when ALL of these hold at
the same evaluation tick: the candidate reached the executor through the queue's
READY authority (`get_best_candidate()` returned a READY candidate with
`ready_blocker=NONE` and `priority_score >=` the class-aware floor), AND
`execution_context.is_ready_validated=True` AND `ready_ts` is within
`EXECUTION_READY_GRACE_SEC=90`. It downgrades ONLY the judge's soft
`WAIT_RETEST` verdict; hard `BLOCK` and judge errors are untouched and every
other gate (kill switch, capacity, class/direction caps, allocator, portfolio,
risk, Entry Quality, ADX-in-class band, ticker validation, sizing) still runs.
The exact same `_ready_execution_grace` helper already gate-keeps the ADX and
liquidity re-checks below the judge — the fix only applies the existing contract
at the judge gate. The judge still executes, and its verdict/score stays visible
in the gate feed as `BARON_ADVISORY` (auditable, never hidden).

## 6. FILES CHANGED
- `core/engine.py` (+83): BARON gate rework (8583-8723); `_ready_execution_grace`
  helper (8563-8593); `OPEN_REQUESTED` now sets `exec_open_requested` flag +
  `last_open_outcome` (8623-8624); `_record_exec_blocker` (8493-8535) emits a
  terminal `OPEN_REJECTED` lifecycle event (flag-guarded single emission);
  paper (9153-9156) and live (9240-9243) fills emit `OPEN_CONFIRMED`.
- `core/runtime.py` (+54): `PORTFOLIO_REASON_USER` mapping (348-356); allocator
  reject now records `last_reject_reason` + `last_reject_reason_user` and gate
  detail `reason (reason_user)` (470-486); `no_slots` branch record (380-384);
  swallowed-exception path now surfaces `EXECUTION_ERROR` in gate feed +
  lifecycle blocker (521-537); NEWS slot user labels (576-587) and
  `execution_context` READY-validation injection (611-624).
- `portfolio/allocator.py` (+8): per-direction cap now env-configurable
  (`MAX_BUY_POSITIONS` / `MAX_SELL_POSITIONS`), default unchanged `BUY 4 /
  SELL 4`, mirroring the existing `MAX_TECHNICAL_POSITIONS` pattern.

## 7. NEW TESTS
- `tests/test_baron_gate_ready_grace.py` (5 tests, BARON_ZONE_JUDGE=1):
  READY grant executes (fails on pre-fix code); non-READY fallback still
  hard-blocked WAIT_RETEST + OPEN_REQUESTED->OPEN_REJECTED pairing; objective
  BLOCK (15-row INSUFFICIENT_DATA frame) never bypassed even for READY;
  OPEN_REQUESTED resolves to OPEN_CONFIRMED (success) or OPEN_REJECTED
  (failure); no duplicate execution (queue + manager guards).
- `tests/test_six_slot_runtime_baron.py` (4 tests, BARON_ZONE_JUDGE=1, real
  runtime executor + news slot): six positions simultaneously
  (2 CRYPTO + 2 INDEX + 1 GOLD + 1 NEWS) with no hard `BARON_REJECT` in the
  gate feed; 6th technical -> `TECHNICAL_CAPACITY_FULL`; 2nd NEWS attempt ->
  `NEWS_SLOT_FULL` (news never consumes a technical quota); 7th position ->
  `TOTAL_PORTFOLIO_CAPACITY_FULL`.
- `tests/test_news_technical_separation.py` (5 tests, reviewer-focus suite):
  1) technical book opens fully with `NEWS_SLOT_ENABLED=false` (no technical
  dependency on news); 2) a news-only watchlist produces ZERO queue READY and
  ZERO executions (no back edge NEWS -> technical entry); 3) a NEWS trade is
  classified NEWS in the book and contributes ZERO to technical/class quotas;
  4) no reaction telemetry -> `news_waiting_reaction`, never opens (no blind
  headlines); 5) wrong-direction reaction -> never opens.

## 8. OTHER DEFECTS SURFACED & FIXED ALONG THE WAY
- **NEWS slot was also silently killed by the judge**: `execute_news_slot` never
  injected `execution_context`, so news candidates were judged non-READY and
  hard-blocked under BARON_ZONE_JUDGE=1. Fixed (runtime.py 611-624), tested.
- **Per-direction cap not configurable**: hardcoded `SIDE_CAPS={BUY:4,SELL:4}`
  silently capped a pure one-direction six-slot day at 4. Now env-configurable,
  default unchanged (allocator.py).
- **Paper-mode kill switch measured "loss" against FREE balance**: committed
  margin looked like a 34% daily loss in multi-position paper tests. Harness
  now feeds equity (`balance + committed_margin`) like a real exchange.
- **OPEN_REQUESTED had no terminal lifecycle outcome**: every rejection ended
  with a phantom "pending open". Now binomial OPEN_REJECTED/OPEN_CONFIRMED.
- **Swallowed executor exception**: `_execute_ready_queue_candidate` now emits
  `EXECUTION_ERROR` + lifecycle `primary_blocker=EXECUTION_ERROR`.

## 9. SAFETY PRESERVATION
- Strategy (RF Engine, Entry Logic, Institutional, Smart Money, Momentum Flow,
  Trade State Machine) untouched.
- Every REAL gate remains: kill switch, slots, class caps, direction caps,
  allocator, PortfolioManager.can_open, risk guard, Entry Quality, sizing,
  exchange validation, judges. No bypass, no forced fill, no fake success.
- BLOCK/error judge verdicts remain absolute fail-closed.
- Legacy `process_queue_entry` second authority verified inert (no
  execution_context -> no grace).

## 10. DASHBOARD / OBSERVABILITY
- New gate events: `BARON_ADVISORY`, `OPEN_REJECTED`, `OPEN_CONFIRMED`,
  `EXECUTION_ERROR`, `NEWS_SLOT_EXECUTED`, allocator detail
  `TECHNICAL_CAP (TECHNICAL_CAPACITY_FULL)`, etc.
- New exec_pipe fields: `last_reject_reason`, `last_reject_reason_user`,
  `last_open_outcome`. Dashboard now explains WHY each candidate was refused.

## 11. PRODUCTION AUTHORITY VERIFIED
`app/bootstrap.py` starts `R.safe_main_loop` -> runtime `portfolio_loop` ->
`_execute_ready_queue_candidate` / `execute_news_slot` as the SINGLE execution
authority. Engine `main_loop_sniper` legacy loop is not started by bootstrap;
`process_queue_entry` unaffected (inert under the fix by design).

## 12. CAPACITY MODEL VERIFIED (no model change)
Enforced today: 2 CRYPTO + 2 INDEX + 1 OIL/GOLD + 1 independent NEWS
(= 6 total, max 5 technical). Internal reason strings pinned by legacy tests
(`TECHNICAL_CAP`, `STOCK_CAP`, `NEWS_CAP`) are untouched; user labels additive.

## 13. REGRESSION RESULT
`python -m pytest tests -q`
**739 passed, 1 skipped, 0 failed** (0:06:05).
Baseline before this fix pass: 27 passed / 1 skipped (targeted).
Includes the separation suite, so any future change that makes a technical
entry depend on news, or lets news manufacture a technical entry, fails here.

## 14. CROSS-TEST ISOLATION ISSUES FOUND & FIXED
- Module-import `NEWS_ENABLED=False` leaked into later-imported modules during
  pytest collection (documented footgun, test_runtime_repairs.py:273). Removed.
- `os.environ.clear()` teardowns (test_portfolio_isolation et al.) wiped
  module-import env for later files; new runtime tests now re-assert the full
  required environment in setUp (order-independent).
- Kill-switch equity semantics (free vs equity) corrected in harnesses.

## 15. RELEASE PACKAGE
`BARON_READY_GRACE_FIX_RELEASE_2026-09-15.zip` (759,221 bytes, 200 files).
Excludes: `BARON/`, `SAR_ATOM/`, `archive/`, `__pycache__/`, `runtime/`,
`logs/`, `.git/`, `.env`, secrets, `.pyc`. Includes: source, tests, config
templates (`.env.example`), docs. Archive integrity verified (extract + file
count + leak scan + fix-presence spot checks). SHA-256:
`1F822D33271D2A3741B6CE583F5301CF21EEFD85A1FC3C562F6EFE9201311B85`.

## 16. COMPILATION CHECK
`python -m compileall -q core portfolio news scanner strategy dashboard app`
exit 0 — all modules byte-compile clean.

## 17. REMAINING ISSUES / NOTES
- Test suite is intentionally offline; ccxt/Flask are stubbed at the test
  boundary. Real-exchange live validation must be performed on the operational
  machine with the SAME script (`bootstrap.safe_main_loop`).
- `BINGX_FUTURES_API_RESEARCH_REPORT.md` describes the BingX contract
  API/position work done previously; not repeated here.
- No local `runtime/`/`logs/` artifacts exist in this sandbox; all execution
  evidence is from the deterministic offline code-path reproduction above.

## 18. HOW TO VERIFY LIVE
1. `python main.py` (or `python app/bootstrap.py`) with `PAPER_MODE=False` +
   real BingX key set.
2. Watch the gate feed (`MEMORY["gate_feed"]`) for `BARON_ADVISORY` on READY
   entries and terminal `OPEN_CONFIRMED` in the trade lifecycle.
3. If a judge `BLOCK` ever appears for a READY candidate, the position is
   refused by design (location broken) — that is the intended fail-closed guard.

## 19. ENV KNOBS (unchanged defaults)
- `BARON_ZONE_JUDGE` = "1" (production default, fail-closed; tests force "0").
- `EXECUTION_READY_GRACE_SEC` = 90.
- `MAX_TECHNICAL_POSITIONS` = 5, `MAX_BUY_POSITIONS`/`MAX_SELL_POSITIONS` = 4,
  now env-configurable; `NEWS_SLOT_ENABLED` = True in production `.env`.

## 20. VERDICT
ROOT CAUSE: queue READY grants were killed by the BARON ZONE/OB judge's
duplicate, stricter WAIT_RETEST threshold under the production default.
FIX: WAIT_RETEST is advisory for fresh READY-validated grants (reusing the
existing 90s ready-execution grace); objective BLOCKs and all other gates remain
fail-closed. Six-slot deterministic paper test through the real runtime
executor: **PASS** (2 CRYPTO + 2 INDEX + 1 GOLD + 1 NEWS). NEWS<->Technical
separation suite: **PASS** (technical opens with news OFF; news alone can never
manufacture a technical entry; news trade classified NEWS; reaction telemetry
mandatory). Full regression: **PASS (739/739)**. Release ZIP + SHA-256 delivered.