# BARON — Master Architecture & Research Integration Review

## Purpose

This document records the engineering conclusions from the cumulative BARON
forensic review and the external project corpus supplied for research.
External projects are capability references, not replacement trading engines.

## Authority model

1. Discovery/Scanner — finds candidates and formations.
2. Evidence Bus — normalizes source, timestamp, timeframe, freshness, quality,
   and no-lookahead metadata.
3. Institutional/Structural layers — liquidity, sweep, displacement, MSS/BOS,
   order blocks, FVG, retest, rejection, flow, smart money and maturity.
4. Thesis Challenger — bull/bear thesis and invalidation as advisory research.
5. Unified Trade Brain — the single decision authority.
6. Existing risk gates and PortfolioManager — portfolio/capital authority.
7. Existing execution layer — the only order-entry authority.
8. Trade Management — the only position-management authority.

No LLM, specialist agent, news feed, Kronos forecast, smart-money signal or
order-book metric may directly place a live order.

## External research matrix

### Vibe-Trading
- Adopt: data provenance, freshness/quality states, reconciliation semantics,
  shadow account, research evidence records.
- Adapt: L2 metrics to futures/perpetuals.
- Reject: autonomous multi-agent execution.

### TradingAgents
- Adopt: specialist evidence roles, structured thesis, bull/bear challenge,
  checkpoint/recovery, persistent decision logs and provider contracts.
- Reject: LLM as execution authority.

### Kronos
- Adopt: optional K-line forecast/representation evidence and volatility
  forecasting for early-move confirmation.
- Constraint: closed candles only; forecast never creates an entry.

### CryptoMeter
- Adopt: capital-flow acceleration, cross-exchange CVD, buy/sell pressure,
  OI changes, liquidity lens, whale/order-book/liquidation context.
- Treat estimated liquidation heatmaps as estimates, not confirmed orders.

### GMGN
- Adopt: smart-money cluster quality, wallet history/consistency,
  first-buyers, developer reputation, holder concentration and exit/distribution
  intelligence.
- Reject: copy trading.

### MRKT
- Adopt: Live News -> affected asset -> market reaction -> causal/contextual/
  background classification.
- News remains catalyst/risk evidence, never standalone BUY/SELL.

### Forex Factory / MyFXBook
- Adopt: economic event risk, actual/forecast/previous, impact classification,
  release proximity, sentiment and reaction analytics.
- Sentiment is contextual/contrarian evidence only.

### OSIRIS
- Adopt: layered intelligence visualization and market war-room concept,
  last-known-good with explicit stale state.
- Reject unrelated OSINT/recon domains.

### UpsideOnly / Invo-Involio
- Adopt: signal/position/realized-trade separation, setup edge, verified
  outcomes, MFE/MAE and lifecycle UX.
- Reject social/copy trading.

### Hermes
- Adopt: bounded strategy memory and Strategy Evolution Lab:
  outcome -> fingerprint -> hypothesis -> backtest/paper -> validation.
- Reject autonomous live strategy mutation.

### CREGOX
- Adopt: market-data circuit breaker, exchange-agnostic normalization and
  fee/slippage/funding-aware execution-cost thinking.
- Reject exchange-business features as core BARON capabilities.

### OpenInsider
- Adopt for stocks only: insider/CEO/CFO/director cluster accumulation.
- Explicitly distinct from crypto smart money and institutional investor flow.

### DotZonix
- Adopt: professional signal-card decomposition, MTF context, entry zone,
  SL/TP ladder, R:R and evidence checklist.
- Reject indicator-count scoring.

### Grok/Trading Desk pattern
- Adopt: specialist research roles, approval gates, persistent context,
  testnet/paper-first and client-order reconciliation.
- Reject 350-agent parallel execution.

### PumpEx
- Unverified from supplied evidence; no capability copied.

## Implemented BARON additions

- Durable trade lifecycle JSONL journal with stable Trade ID.
- Verified TP1/TP2 execution semantics.
- API UNKNOWN vs confirmed absence semantics.
- Symbol-aware close/lifecycle handling.
- Startup exchange-position recovery path.
- Optional exchange-native protection adapter with ACK verification.
- Guaranteed breakeven ratchet at configurable ROE independent of TP1 score.
- Early formation detector: compression, volatility compression, liquidity
  building, absorption, failed breaks, zone proximity, directional pressure.
- Move maturity: PRE_EXPANSION, EARLY_EXPANSION, MID_EXPANSION,
  LATE_EXPANSION, EXHAUSTION.
- Radar geometry consistency: nearest fresh zone determines directional
  watchlist hypothesis when a near-zone candidate exists.
- Setup fingerprint / historical edge registry with conservative minimum-sample
  behavior.
- News reaction telemetry.
- Professional read-only dashboard routes: `/trades`, `/early-moves`,
  `/intelligence` plus dashboard lifecycle/early-move panels.
- Explicit `.env.example` for the new safety controls.

## Deliberate non-changes

The preserved RF engine, scanner/portfolio architecture, leverage/sizing,
portfolio caps, execution authority, Telegram integration and existing risk
boundaries remain in place. New research layers enrich evidence and telemetry;
they do not create a parallel trading engine.
