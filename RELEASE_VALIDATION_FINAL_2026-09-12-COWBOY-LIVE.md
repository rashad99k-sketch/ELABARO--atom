# BARON — Final Cowboy Surgical Live Candidate Validation

Date: 2026-09-12

## Scope

Surgical entry-only upgrade derived from the supplied Cowboy playbook:

- Liquidity sweep -> directional MSS/BOS -> causal zone/OB/FVG -> local retest/mitigation or local rejection/displacement -> anti-chase.
- Zone proximity is enforced with `COWBOY_MAX_ZONE_DISTANCE_ATR` (default 1.25 ATR).
- BUY/SELL symmetry is preserved.
- BARON remains the authority for scanner, portfolio, risk, queue, execution, protection, reconciliation, and position management.
- RF and volume remain supporting evidence, not independent execution authorities.

## Surgical repair

A Windows-determinism issue was found in `tests/test_runtime_warning_repairs.py`: the test read `core/engine.py` using the platform-default encoding. On Windows this caused a `UnicodeDecodeError` under cp1252. The test now explicitly reads UTF-8. No trading/runtime behavior was changed by this repair.

The previously observed dynamic six-position failure was re-run from a clean Linux process and passed. Its contract remains exactly 5 technical + 1 NEWS = 6 total positions.

## Validation

- `verify_project.py`: PASS
- `python -m compileall -q .`: PASS
- Cowboy surgical tests: PASS (7/7)
- Dynamic six-position lifecycle: PASS (2/2)
- Profit engine phase 3: PASS (31/31)
- TP1/TP2 canonical management: PASS
- Unified trade authority: PASS
- Orderbook side identification: PASS
- Release blocker repairs: PASS
- Runtime warning repairs: PASS (3/3, including Windows UTF-8 static contract)
- Combined critical regression set: PASS (86/86)
- Remaining regression test files were validated individually in staged batches; no non-pass result remained in the completed manifest.
- Paper runtime smoke: PASS
  - universe=5
  - watchlist=5
  - promoted=5
  - queue=5
  - ready=0

## Live-readiness boundary

This package is a code-level LIVE CANDIDATE. Local tests cannot prove real BingX authentication, symbol availability, account mode, leverage permissions, network behavior, or real fills. First live activation should therefore be controlled and observed through the existing protection/reconciliation path.
