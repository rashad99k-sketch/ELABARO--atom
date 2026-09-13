# BARON Release Notes — 2026-09-10

## Research integration

This release integrates selected architecture patterns from Freqtrade and OpenBB without importing their trading engines.

### Added
- Persistent materialized trade registry.
- Closed-trade ledger for exact multi-close risk accounting.
- Opt-in Freqtrade-inspired protection firewall.
- Deterministic position-management scenario replay harness.
- OpenBB-inspired provider registry and normalized evidence fabric.
- Existing BARON EvidenceBus bridge.
- Read-only `/data-fabric` dashboard endpoint.
- New regression coverage for persistence, provider failures, protection behavior and peak/reversal scenarios.

### Preserved
- BARON remains the execution authority.
- BingX Hedge Mode uses `positionSide` without invalid `reduceOnly` combinations.
- Existing RF/institutional scanner/queue and position-management logic are not replaced.
- New persistence I/O is kept outside the per-symbol management decision path.

## Test gate

All 64 test modules passed in the final validation pass, including the long runtime repair suite (86 tests).


## Final forensic repair pass — 2026-09-10

- Fixed `ExecutionQueue._check_entry_conditions()` to pass the real symbol into `get_smart_zones()`, preventing cross-symbol smart-zone cache contamination.
- Enforced the NEWS portfolio slot as a hard singleton (`NEWS: 1`) even when `MAX_POSITIONS_PER_ASSET_CLASS` is raised for technical classes.
- Hardened restart recovery: when a durable TradeRegistry record exists, recovery restores its SL/TP1/TP2, TP1/runner state, peak ROE/price, profit-lock ROE and trade_id instead of recomputing live protection from current ATR. If durable protection is incomplete, the system refuses to silently invent new ATR-derived levels.
- Added regression test `tests/test_watchlist_symbol_zone_cache.py`.
- Targeted final regression after these repairs: 66 passed.
