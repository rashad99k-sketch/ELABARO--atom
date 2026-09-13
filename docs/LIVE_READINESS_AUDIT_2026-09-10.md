# BARON Live-Readiness Audit — 2026-09-10

## Scope

Surgical production hardening of the supplied `BARON-ROOTFIX-PREPARED-RETEST-2026-09-10` build.

### Changes made

1. **Order Book data integrity**
   - Removed the position-open `cache-only` blind spot.
   - Added per-symbol/per-depth timestamps and bounded stale fallback.
   - Added explicit Order Book quality states: `ORDERBOOK_OK`, `ORDERBOOK_STALE`, `ORDERBOOK_TIMEOUT`, `ORDERBOOK_API_ERROR`, `ORDERBOOK_EMPTY`, `ORDERBOOK_UNSUPPORTED_SYMBOL`, `ORDERBOOK_RATE_LIMITED`, `ORDERBOOK_BLOCKED_BY_LOCAL_POLICY`.
   - Empty/unavailable Order Book is no longer converted into measured neutral imbalance.
   - Deep Scanner and Dashboard carry the structured Order Book quality payload.

2. **Portfolio capacity**
   - Total maximum remains **6**.
   - Added explicit aggregate **technical maximum = 5**.
   - News remains a hard singleton **maximum = 1**.
   - This produces the intended capacity contract: **5 Technical/Institutional + 1 News = 6**.
   - Allocator total-slot accounting now counts only actually selected/chosen candidates rather than every rejected decision.

3. **Portfolio lifecycle hygiene**
   - Rejected capacity/risk candidates are not materialized as active `INTENT` trade records.
   - News candidates preserve `asset_class=NEWS` through execution/position management.

4. **Position synchronization safety**
   - A missing/unknown exchange snapshot no longer automatically means the live position disappeared. Local state is preserved until exchange truth is authoritative.

5. **Realized PnL fallback safety**
   - Removed raw buy/sell cash-flow arithmetic as a futures realized-PnL fallback.
   - Explicit exchange PnL fields are used when available; otherwise the verified close leg / position ROE path remains authoritative.

6. **Live-entry protection gate**
   - Live entries require exchange-native protection when `REQUIRE_NATIVE_PROTECTION_LIVE=1`.
   - Native protection is now fail-closed: after `create_order`, the exact conditional order is re-read with `fetch_order` when verification is enabled; rejected/cancelled/expired/failed states are not accepted as protection.
   - Accounting fields needed for rollback are initialized before protection verification, so a protection failure can safely flatten the just-opened position without depending on post-entry accounting initialization.
   - Native protection updates use the live execution mode consistently; a failed replacement preserves the old stop because replacement is place-first/cancel-second.

## Validation

- `verify_project.py`: PASS
- `compileall`: PASS
- New live-hardening tests: **8 PASS**
- Native protection/hedge contract tests: **4 PASS**
- Final targeted live/management regression batch: **117 PASS** across protection, portfolio, profit engine, slot execution, position-side, reconciliation, timeout recovery, and authority suites.
- Portfolio dynamic 6-way: **2 PASS**
- News production: **9 PASS**
- Portfolio full cycle: **5 PASS**
- Position-side lifecycle: **12 PASS**
- Runtime repairs: **86 PASS**
- Profit-engine phase 3: **31 PASS**
- Slot execution: **10 PASS**
- Additional targeted suites were executed independently and passed.

The project contains **581 collected tests**. Individual test files were executed to isolate long-running suites; several long suites were also rerun after the final protection changes. The monolithic all-in-one invocation is intentionally not represented as a PASS because the sandbox process can hit an execution timeout.

## Live-readiness boundary

This artifact has **not** been connected to a funded BingX account in this audit environment. No real order was submitted. Exchange-native protection must still be validated on the exact deployed CCXT/BingX version and account mode before allowing funded execution.

Therefore this document is a production hardening report, not a guarantee of zero runtime failures or financial safety.
