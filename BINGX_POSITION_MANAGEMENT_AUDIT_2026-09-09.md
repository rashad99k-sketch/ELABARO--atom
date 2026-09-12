# BARON Position Management / BingX Execution Audit — 2026-09-09

## Scope
Deep review of the uploaded BARON build with focus on position management, profit capture, strict close, restart/timeout safety, and BingX Hedge Mode execution compatibility.

## BingX contract verification
The current BingX API reference states:
- Hedge mode uses `positionSide=LONG` / `SHORT`; `BOTH` is for one-way mode.
- `reduceOnly` is **not accepted when both long and short positions are enabled**.
- `clientOrderId` is 1–40 characters and is unique per order.
- Trigger orders use `stopPrice`; `workingType` can use `MARK_PRICE` or `CONTRACT_PRICE`.
- `closePosition=true` is a separate trigger-order mechanism for closing the full position and is not combined with quantity.

BARON production close/partial-close/native-protection paths were aligned to the Hedge Mode contract: `positionSide` remains explicit and `reduceOnly` is omitted.

## Production fixes in this release
1. Removed `reduceOnly` from Hedge Mode close/partial-close/emergency/native-protection paths.
2. Added deterministic, trade-intent-based BingX-safe client order IDs for opens (`brn` + SHA-256 compact digest), so retries/restarts preserve the same idempotency key for the same trade intent.
3. Timeout reconciliation now checks the exact `clientOrderId` first, then reconciles the exchange position. A pending/unknown order is never blindly retried.
4. If `create_order` itself raises after transmission may have occurred, the same client ID is reconciled before any retry.
5. Existing strict close verification remains: order acknowledgement -> fill verification -> exchange position re-read -> only then local close state.
6. Native SL placement remains fail-closed and now uses Hedge Mode-compatible parameters.

## Validation
- BingX Hedge Contract tests: 2/2 PASS
- Client Order ID tests: 4/4 PASS
- Position Side tests: 17/17 PASS
- Position Side Lifecycle: 12/12 PASS
- Portfolio / authority / lifecycle suite: 61/61 PASS
- Open Timeout Recovery: 10/10 PASS
- Python compileall: PASS
- Production Python source scan: no `reduceOnly` references remain in `core/`, `portfolio/`, or `execution/`.

## Important limitation
No real-money BingX order was submitted from this environment. The exchange contract was validated against current BingX API documentation and the production payloads were validated through deterministic test venues. Live deployment should remain disabled until a controlled paper/demo run reproduces the complete lifecycle.
