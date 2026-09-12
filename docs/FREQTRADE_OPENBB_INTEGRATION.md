# BARON — Freqtrade + OpenBB Architecture Integration

## What was adopted

### From Freqtrade
- **Materialized Trade state:** `portfolio/trade_registry.py` persists trade identity, client-order ID, SL/TP, TP1/runner state, peak ROE, profit-lock state and realized PnL.
- **Protection firewall:** `portfolio/protection_firewall.py` adds deterministic stop-loss guard / drawdown / pair-lock primitives. It is a risk gate only and does not place or close orders.
- **Scenario position replay:** `backtesting/position_scenarios.py` provides a small deterministic harness for peak-profit → pullback → reversal tests against the existing BARON management authority.
- **Closed-trade ledger:** BARON now keeps immutable `PERF["closed_trades"]` entries so multiple closes between risk polls are not collapsed into one `last_trade` record.

### From OpenBB
- **Provider interface:** `data_fabric/providers.py` isolates data sources as independent providers.
- **Normalized evidence model:** `data_fabric/models.py` standardizes source, timestamp, timeframe, quality, status, confidence and no-lookahead metadata.
- **Data fabric:** `data_fabric/fabric.py` aggregates providers and can mirror them into the existing BARON `EvidenceBus`.
- **Read-only API:** `/data-fabric` exposes provider coverage and the normalized snapshot without creating a trade authority.
- Existing BARON EvidenceBus is bridged through `EvidenceBusProvider`, so the new layer enriches rather than replaces the established evidence pipeline.

## What was deliberately NOT copied

- No Freqtrade strategy code.
- No Freqtrade entry/exit engine replacement.
- No OpenBB router/application replacement.
- No LLM/agent execution authority.
- No new indicator-based entry rule.
- No change to RF, institutional queue, sizing, leverage, BingX hedge-mode execution contract, TP/SL formulas, or the Unified Trade Management authority.

## Safety model

`Provider -> Evidence -> Institutional/Management evidence -> existing BARON authority -> existing execution layer`

The data fabric cannot call execution. The protection firewall can only block an entry. The scenario backtester is offline and deterministic.

## Validation

- New integration tests: 9/9 passed.
- Existing BARON test modules: all 64 test files passed after the final fixes.
- `test_runtime_repairs.py`: 86/86 passed.
- BingX contract / position-side / timeout recovery suites passed.
- Portfolio dynamic six-way and radar rotation suites passed after persistence was moved out of the per-symbol management path.
