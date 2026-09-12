# BARON Professional Live Candidate — Release Audit 2026-09-12

## Status
**READY FOR PAPER / STAGED LIVE VALIDATION. LIVE trading is not enabled by this release.**

This release repairs the four blockers identified in the 2026-09-12 audit without replacing the existing scanner, entry, execution, TP/SL, dashboard, Telegram, or exchange synchronization architecture.

## Repairs

### 1. L2 Institutional Heatmap / Persistence
Added `core/orderbook_heatmap.py` and wired it into fresh order-book snapshots.

Evidence now includes:
- snapshot history
- persistent bid/ask walls
- wall lifetime/persistence
- transient-wall / spoof-risk proxy
- absorption
- liquidity-vacuum evidence
- heatmap score
- explicit fresh/stale quality

This is evidence only. It never places or closes orders.

### 2. Unified Trade Management Brain
Added `UnifiedTradeManagementBrain` in `core.engine`.

Existing management engines remain evidence providers. Close decisions are now passed through the unified authority before management closes. TP2 also requires unified authorization before the verified close. The brain has no exchange access.

Healthy continuation can veto a weak provider's close proposal and downgrade it to protection; hard failure can authorize close.

### 3. Scanner Contract
Canonical defaults are now:
- global discovery: **20 minutes / 1200s**
- deep watchlist: **40**
- radar target: **40**

Existing batching/rotation behavior remains intact.

### 4. Expanded Asset-Class Execution
The hard portfolio contract remains:
- **5 Technical positions maximum**
- **1 NEWS position maximum**
- **6 positions total maximum**

STOCK / ENERGY / METALS / COMMODITIES now receive executable class capacity when a technical slot is available. Existing CRYPTO/INDEX/GOLD/OIL caps remain intact to avoid removing the prior concentration controls.

## Validation

- `verify_project.py`: PASS
- `compileall`: PASS
- staged test coverage: **512 passed, 1 skipped** across all 70 test files
- `test_runtime_repairs.py`: **86/86 PASS**
- `test_profit_engine_phase3.py`: **31/31 PASS**
- `test_orderbook_side_identification.py`: **32/32 PASS**
- `test_release_blocker_repairs.py`: **6/6 PASS**
- `test_slot_execution.py`: **10/10 PASS**
- `test_portfolio_full_cycle.py`: **5/5 PASS**
- `test_portfolio_dynamic_6way.py`: **2/2 PASS**
- `tools/paper_runtime_smoke.py`: **PASS**

The full 70-file suite was validated through staged isolated runs because a single uninterrupted pytest process exceeds the execution wall-clock budget; no completed staged file had a failing exit code.

## Live gate
Do not enable LIVE credentials until the following staged checks are completed against the actual BingX environment:
1. demo/VST connectivity
2. one controlled open + native protection verification
3. partial TP1 fill verification
4. TP2/full-close verification
5. exchange restart/reconciliation test
6. L2 live snapshot/persistence sanity check
7. 24h paper runtime

CCXT documents BingX perpetuals, order-book access, trigger/stop parameters, reduce-only closes, and BingX VST sandbox support. See the current CCXT BingX reference before the first live credential test.
