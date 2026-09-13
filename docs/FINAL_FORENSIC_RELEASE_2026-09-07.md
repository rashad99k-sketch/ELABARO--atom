# EL-BARON Final Forensic Release — 2026-09-07

## Release gate
- Source: latest supplied `EL-BARON-BOT-PRO-FINAL-2026-09-07-RELEASE(1).zip`
- Verification: `python verify_project.py` PASS
- Collection: 552 tests collected (549 original + 3 canonical TP tests)
- Targeted release gate: 67 passed
- `test_runtime_repairs.py`: 86 passed in 30.54s
- All test modules were exercised in isolated Python processes or individually; no failing assertion remained in the final targeted suites.
- ZIP integrity checked with Python `zipfile.testzip()` and system `unzip -t`.

## Trade-management invariants
- TP1 = 50% of original position quantity.
- TP2 = 100% of the remaining quantity (the other 50% under the normal 50/50 lifecycle).
- TP1 execution is explicitly staged as `TP1`; generic `close_partial()` remains generic `PARTIAL` so legacy/manual partial semantics do not accidentally become TP1.
- Repeated TP1 calls are idempotent: the engine closes only the quantity still required to reach 50% of `qty_initial`.
- LIVE partial close uses `reduceOnly` and the correct hedge `positionSide` derived from BUY/SELL.
- LIVE state is not declared closed until exchange position verification confirms flat.
- TP1 immediately re-arms native protection for the verified remaining quantity and protected stop.
- Canonical geometry: BUY `SL < ENTRY < TP1 < TP2`; SELL `TP2 < TP1 < ENTRY < SL`.
- TP1/TP2 are the only profit-taking stages; no TP3–TP6 execution path is used by the canonical adapter.
- Runner starts only after TP1 is verified.

## Accounting
- Partial legs are journaled separately and credited immediately.
- Final close books only the remaining quantity.
- Completed trade count increments once at lifecycle finalization, not on TP1.
- Realized and unrealized PnL remain distinct.
- Dashboard ROI is derived from unrealized PnL / initial margin for live position cards; realized PnL is exposed separately.

## News / intelligence
- News is evidence, not a direct execution authority.
- Cross-asset mapping and market-reaction logic remain advisory inputs to the unified management path.
- External/reference project ideas are used selectively: reconciliation/audit/SSE safety, event impact/reaction, flow/smart-money evidence, memory/post-trade analysis, and portfolio risk gates.

## Known environment boundary
- No real BingX account credentials were used.
- Real exchange order fills cannot be claimed from this offline Linux environment.
- Windows launcher and real exchange connectivity must still be smoke-tested on the user's Windows machine in PAPER/TESTNET before any LIVE capital is enabled.
