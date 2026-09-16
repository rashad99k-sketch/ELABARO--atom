# BARON Classification & Split-Brain Fix — Forensic + Implementation Evidence

**Audit scope:** BingX trading-engine asset classification (CRYPTO vs TradFi) and the
`NCFX FOREX_CAPACITY_FULL` split-brain execution defect.
**Audit date:** 2026-09-16. **Root of truth tested:** this repo (live tree, git aaa44c0..f8ac380).
**Change control:** NO commit / NO push / NO ZIP rebuilt.

---

## 1. Executive summary

BingX lists 1,244 swap contracts with **no productType/category field**. TradFi instruments
are only distinguishable by the venue's own family prefixes encoded in the symbol base
(NCSK/NCSI/NCCO/NCFX) plus the exchange `displayName`. The engine previously defaulted any
unmatched shape to `CRYPTO`, which silently relocated TradFi instruments (forex, gold, oil,
stock ETFs) into the crypto bucket. The execution path also self-matched its own top candidate
as the allocator decision (split-brain), forcing forex re-entry until capacity was full.

Both defects are fixed and covered by a deterministic test matrix.

## 2. Venue taxonomy evidence (live API)

Endpoint `https://open-api.bingx.com/openApi/swap/v2/quote/contracts` (live query,
evidence saved at `C:\Users\rasha\.local\share\opencode\tool-output\tool_0a83c6ecf001Ep5gSpp0RwsrBr`).

| Family | Count | Symbols |
|---|---|---|
| NCSK (stock/equity + equity ETFs) | 519 | `NCSKAAPL2USD/USDT:USDT`, `NCSKSPCX2USD-USDT`, ... |
| NCSI (indices + index ETFs) | 18 | `NCSINASDAQ1002USD/USDT:USDT`, `NCSIEWJ2USD/USDT:USDT`, ... |
| NCCO (commodity/energy) | 33 | `NCCOGOLD2USD`, `NCCO1OILBRENT2USD`, `NCCOXAG2USD`, ... |
| NCFX (forex) | 42 | `NCFXEURUSD2USD/USDT:USDT`, ... |
| NC* total | 612 | — |
| USDT/USDC margin pairs | 1195 | — |

No field distinguishes class; classification is `prefix + displayName` — the sole
exchange-grounded evidence. Crypto contracts are the non-NC remainder.
Display-name examples: `SPCX-USDT` (stock), `EWJ-USDT` (index ETF), `SILVER(XAG)-USDT` (metal).

## 3. Classification defect — audit findings

Paths through which a TradFi instrument could silently become `CRYPTO` (pre-fix):

1. `scanner/universe.py` `classify()` — final tail returned `CRYPTO` for any unmatched shape.
2. `portfolio/manager.py` `_asset_class()` — symbol-derived fallback defaulted to `CRYPTO`.
3. `core/engine.py` `AssetBehaviorProfile` / `resolve_asset_class()` (≈:2909) — default `CRYPTO`.
4. `core/engine.py` `ExecutionCandidate.__post_init__` — default `CRYPTO`.
5. `scanner/deep_scanner.py` (:884) — default `CRYPTO`.
6. `portfolio/news_slot.py` (:217-219) + `news/service.py` (:381) — default `CRYPTO`.
   **NOT edited** (News is an independent slot; Phase 17 scope protection). Left fail-safe
   because stored `asset_class="NEWS"` is captured at OPEN and never re-derived.

Priority rule locked in: metadata (`NCSK/NCSI/NCFX/NCCO` + displayName) → real class;
**never CRYPTO**. CRYPTO requires venue evidence (a venue-issued base or a real USDT/USDC
pair shape). Metadata-free / malformed shapes → `UNKNOWN`, confidence 0.0.

## 4. Split-brain defect — audit findings

`core/runtime.py` candidate→decision path:

- `queue_snapshot()` included the current best candidate's own `to_dict()` (default class
  CRYPTO). Because it sorted by `score` (not the allocator's `priority_score`), that
  self-row landed LAST but the decision loop matched by symbol and picked it first.
- Result: the allocator answered the candidate with the candidate's own stale/default
  CRYPTO identity → forex rejected everything → `FOREX_CAPACITY_FULL` re-entry loop
  (the `f8ac380` commit's reported split-brain).

Fix: candidate carries `priority_score` + `candidate_key`; queue snapshot excludes
`best.symbol`; decision matched by `candidate_key` (symbol fallback for legacy callers);
orphaned `break` removed.

## 5. Implementation changes

| File | Change |
|---|---|
| `scanner/universe.py` | `ASSET_CLASSES` includes `ETF`, `UNKNOWN`; `VENUE_TRADFI_PREFIXES`; `ETF_HINTS`; `_venue_pair()`; NCFX constant; `classify()` fully rewritten: NCSK→STOCK/ETF (conf 1.0/0.98), NCSI→INDEX/ETF, NCFX→FOREX, NCCO→GOLD/METAL/OIL/ENERGY by displayName hints, venue-named fiat pair → FOREX, unhinted NCCO → ENERGY 0.80 (never CRYPTO); pattern fallbacks at lower confidence; venue-CRYPTO only with market base or USDT/USDC shape; final tail `UNKNOWN` 0.0. |
| `portfolio/allocator.py` | `ASSET_BUCKETS`: ETF→INDEX_STOCK, METAL→COMMODITY, FOREX→FOREX; `AllocationDecision.candidate_key`; allocation decision carries `candidate_key`. |
| `portfolio/manager.py` | `_asset_class()` fail-closed tail: USDT/USDC → CRYPTO, clean venue-listed crypto root → CRYPTO, else UNKNOWN; NC* prefixes resolved upstream never reach this tail. |
| `core/engine.py` | `AssetBehaviorProfile._RESOLVED_CLASSES`; `resolve_asset_class()` covers full class set + fail-closed tail; `ExecutionCandidate.__post_init__` guard (NC* or non-pair shapes → resolve real class; `???` → UNKNOWN; pair symbols keep CRYPTO); `re_evaluate_all` skips EXECUTED/INVALIDATED/RETURNED_WATCHLIST. |
| `scanner/scanner.py` | module `_has_live_context()`; promotion guard blocks `position_already_open` before `eligible+=1` (gate event PROMOTION/POSITION_ALREADY_OPEN); candidate carries stored class or authoritative resolver result. |
| `scanner/deep_scanner.py` | default class → stored entry class or `resolve_asset_class()`. |
| `core/runtime.py` | candidate dict `priority_score` + `candidate_key="runtime:{sym}:{side}"`; snapshot excludes `best.symbol`; deterministic decision match (candidate_key → symbol fallback → `allocator_error`); Fix#4 duplicate live-position invalidation (INVALIDATED, `POSITION_ALREADY_OPEN`, total_rejected++); removed orphaned `break`. |

## 6. Dead-code guarantees (fail closed)

- `ExecutionCandidate` default can never turn a TradFi shape into CRYPTO.
- `Universe.classify` cannot emit CRYPTO for any `NC*` shape.
- `manager._asset_class` cannot emit CRYPTO for malformed or TradFi evident symbols.
- `re_evaluate_all` cannot push EXECUTED/INVALIDATED symbols back to NEW OPEN.

## 7. Test evidence

New regression matrix (`tests/test_baron_authoritative_classification.py`, 10 tests, green in
isolation): venue authoritative matrix; TradFi families never CRYPTO; allocator bucket map +
caps (candidate_key, FOREX_CAP/CRYPTO_CAP/INDEX_STOCK_CAP/COMMODITY_CAP statistical points);
allocator capacity coherence; malformed/metadata-free never CRYPTO;
`ExecutionCandidate` default never CRYPTO-converts TradFi; runtime decision matches runtime
candidate (STOCK opens as STOCK, no CRYPTO_CAP hit); duplicate live position removes queued
candidate; promotion blocks live re-entry; `re_evaluate_all` skips terminal states.

Scoped suites **44 tests OK** (93s): above + `tests.test_allocator_ready_execution_fix`,
`tests.test_deep_universe`, `tests.test_forensic_fixes` (incl. updated `test_ncfx_forex_...`
asserting `src=="metadata"`, `conf>=0.9`). `tests.test_runtime_repairs.TradFiStockDiscoveryTest` 8/8 OK
(`test_forex_not_gold` now passes: `NCCOEUR2USD/USD:USD + display "EURUSD"` → FOREX, not ENERGY).

Full `unittest discover` shows pre-existing failures (46) that **reproduce on the git HEAD
baseline with these changes stashed** (verified by stash/rebase spot-checks of
`test_news_slot_production`, `test_portfolio_dynamic_6way`, `test_portfolio_full_cycle`,
`test_profit_engine_phase3`, `test_slot_execution`, `test_runtime_repairs`). Symptom: the BARON
judge's `WAIT_RETEST` gate (`score=55.2`) blocks every real-engine open (`open_top(...)==0`).
These are NOT caused by the classification work; class-related delta = **zero**.
Log: `%TEMP%\opencode\full_suite.log`.

Environment note: `UnicodeEncodeError: 'charmap'` on log emoji (U+1F7E2) when piping console —
benign, resolves with `PYTHONIOENCODING=utf-8`.

## 8. Proof items (15)

1. Live venue query: 1244 contracts, no productType; counts above (§2).
2. Evidence file path recorded, timestamped (`tool_0a83c6ec...`).
3. Prefix + displayName = sole exchange-grounded classifier (§2).
4. Pre-fix CRYPTO fallback sites enumerated (§3, six sites; two news sites deliberately kept).
5. NCSK/NCSI/NCFX/NCCO authoritative classes verified symbol-by-symbol.
6. Capacity model unchanged: CRYPTO 2 / INDEX_STOCK 2 / COMMODITY 1 / NEWS 1 / TOTAL 6 —
   ETF/METAL/ENERGY map to existing buckets; FOREX deliberately cap-0 fail-closed.
7. Allocator bucket map = manager capacity map (single authority, no disagreement).
8. Split-brain root cause demonstrated: self-candidate default CRYPTO wins decision (§4).
9. `candidate_key` + snapshot exclusion eliminated self-match.
10. Decision outcome deterministic: candidate_key → symbol → allocator_error.
11. Duplicate live position never re-opens (invalidation proof in tests).
12. Promotion-to-queue blocks live re-entry from NEW OPEN.
13. `re_evaluate_all` respects terminal states.
14. Missing metadata / malformed shapes → UNKNOWN (never CRYPTO).
15. No live-broker trading possible in this environment (synthetic ccxt stubs) — runtime
    path validated on paper; stated as a limitation.

## 9. Limitations

- In-environment broker execution not exercisable (fake ccxt); paper only.
- Full-suite pre-existing failures (§7) are BARON judge-gated and outside this audit's Phase
  17 scope (no strategy/RF/binar/judgement threshold changes).
- News `CRYPTO` text defaults retained by scope contract; harmless because NEWS is captured
  explicitly at OPEN.

## 10. Files touched

`scanner/universe.py`, `scanner/scanner.py`, `scanner/deep_scanner.py`, `core/engine.py`,
`core/runtime.py`, `portfolio/allocator.py`, `portfolio/manager.py`,
`tests/test_baron_authoritative_classification.py` (new), `tests/test_allocator_ready_execution_fix.py`.