# BARON Forensic Audit — Watchlist → Institutional → 6-Position Management

Date: 2026-09-09

## Findings

### WATCHLIST coverage
The previous production default used a rotating batch of 10 symbols every 20 seconds, which could leave a 60-symbol watchlist waiting roughly 120 seconds for a full deep-analysis pass.

### Fix
The deep scanner now defaults to:
- `WATCHLIST_DEEP_BATCH_SIZE = 60` (or the configured watchlist size)
- `WATCHLIST_DEEP_INTERVAL_SEC = 10`
- `WATCHLIST_DEEP_WORKERS = 8`
- `WATCHLIST_DEEP_MAX_AGE_SEC = 30`

The scheduler prioritizes STRONG first, then MEDIUM, then stale/never-analyzed rows. Analysis remains analysis-only; it does not place orders.

### Institutional OB hand-off
The deep scanner now evaluates both BUY and SELL causal Order Blocks on the same closed-candle frame before institutional hand-off. Each side records:
- direction: `BULLISH_DEMAND` / `BEARISH_SUPPLY`
- grade and score
- zone bounds
- freshness
- displacement in ATR
- volume ratio
- touches
- liquidity score/state
- structure score/type
- distance from current price
- active-near-price state

The winning side and opposing side are explicitly compared. A strong active opposing A/A+ OB is surfaced as `BLOCK_OPPOSING_OB` evidence. The existing downstream execution gates remain authoritative.

### Canonical analysis contract repair
The deep scanner previously could publish a MEDIUM/STRONG row without the nested `analysis` payload consumed by the downstream InstitutionalRadar A-GRADE derivation. That could cause the institutional layer to see `ob_grade=NONE`/zero structure/liquidity even though fresh deep evidence existed.

The fix now publishes the canonical `analysis` fields from the same institutional OB/liquidity/structure evaluation used by the downstream layer.

## Position management
The production PortfolioManager keeps one independent PositionContext/live manager per open symbol and enforces `MAX_POSITIONS=6` plus class caps. `manage_all()` activates each context, synchronizes exchange/paper state, runs that symbol's LiveTradeManager, and removes contexts only after the position is actually closed.

The tested six-position lifecycle covers:
- six simultaneous positions
- seventh-position rejection
- independent management
- real SL exits in PAPER
- slot rotation after closes
- margin/PnL reconciliation
- dashboard snapshot of all open positions
- final capacity release

## Validation
Targeted regression suite after the changes:

`80 passed in 11.20s`

Covered:
- deep scanner runtime
- institutional queue
- OB quality / causal confirmation
- six-position dynamic lifecycle
- portfolio full-cycle profit management
- unified trade authority
- position-side lifecycle
- position-management phase 1

No live BingX order was submitted during this audit.
