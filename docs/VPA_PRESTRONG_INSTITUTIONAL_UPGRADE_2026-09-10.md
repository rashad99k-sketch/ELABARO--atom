# BARON VPA + Pre-STRONG Institutional Upgrade — 2026-09-10

## Objective

Add professional Volume-Price Analysis (VPA) as evidence around the existing causal Order Block, and start institutional analysis before the Watchlist reaches the public STRONG tier.

## Watchlist timing contract

- Public vocabulary remains `WEAK / MEDIUM / STRONG`.
- Raw STRONG threshold remains `8.0`.
- `PRE_STRONG_SCORE=6.0` is the default early-preparation threshold.
- A 6–8 candidate is still publicly MEDIUM, but is flagged `pre_strong` and receives priority institutional analysis.
- Institutional Zone Analysis can therefore begin before the STRONG badge appears.
- This is preparation only; it does not bypass Queue, confirmation, risk, allocator, or execution gates.

## VPA

`core/vpa.py` is a pure evidence engine. It evaluates:

- effort/result (volume versus candle result/efficiency)
- displacement volume
- retest volume contraction/expansion
- absorption proxy
- directional confirmation
- adverse high-effort attack into the position-side zone

VPA never places orders and never closes a position by itself.

## Order Block integration

The existing causal OB engine remains authoritative for zone construction. VPA is layered onto it:

- A+ requires directional VPA confirmation in addition to existing causal/freshness/displacement/sweep/PD requirements.
- A requires VPA confirmation.
- Broken OBs cannot be rescued by VPA.
- VPA evidence is persisted into Watchlist, Queue candidate evidence, and position state.

## Position management

The live manager records VPA evidence against the stored entry zone. VPA alone cannot close a healthy trend. A VPA adverse attack may corroborate a close only when independent reversal/momentum evidence is already strong.

This preserves the distinction between:

- healthy pullback / trend continuation → HOLD / trail
- distribution / exhaustion → profit protection
- confirmed thesis failure + adverse evidence → strict close

## Tests

The added `tests/test_vpa_prestrong.py` covers directional VPA confirmation, adverse zone attack, and pre-STRONG institutional registry activation.
