# BARON Final VPA + Pre-STRONG Release Validation — 2026-09-10

## Scope

This release adds VPA as an Order Block evidence layer, introduces the 6.0 pre-STRONG preparation threshold, prioritizes institutional analysis before the public STRONG tier, and wires VPA evidence into live position management without making VPA a blind exit authority.

## Public behavior

- STRONG remains the public Watchlist label.
- Raw STRONG threshold remains 8.0.
- 6.0–7.999 is publicly MEDIUM plus `pre_strong=true`.
- PRE_STRONG receives accelerated Institutional Radar scheduling (default 8s minimum interval subject to existing scheduling/data gates).
- Institutional Zone Analysis can activate before STRONG when real precursor evidence exists.
- Queue/confirmation/risk/allocator/execution authority remains unchanged.

## VPA behavior

- Causal OB construction remains the existing BARON engine.
- VPA evaluates effort/result, displacement volume, retest volume, absorption proxy and adverse high-effort zone attack.
- A/A+ institutional OB grading requires directional VPA confirmation.
- VPA cannot rescue a broken OB.
- Live management uses VPA as corroborating thesis evidence; healthy trends are not closed by VPA alone.

## Validation

`verify_project.py`: PASS.

`python -m compileall`: PASS.

Full project collection: **569 tests collected**.

Targeted post-change regression suite: **155 passed** covering VPA, DeepScanner, early institutional radar/pre-expansion, OB quality/configuration, position management, scenario backtesting, six-position portfolio lifecycle, BingX hedge lifecycle, timeout recovery, trade registry and unified trade authority.

Additional project test batches were run across the remaining existing test modules; the test modules were also run individually where the monolithic suite was affected by process-global/background-worker teardown behavior. `tests/test_fill_reconciliation.py` passed directly (8/8), and `tests/test_openbb_data_fabric.py` passed directly (2/2).

A single monolithic `pytest -q` invocation did not finish within the validation timeout because the repository contains long-running/process-global runtime tests. This is a runtime harness limitation, not reported as a PASS for the monolithic command. No production test was altered to force a pass.

## Safety conclusion

No new order path was introduced. VPA cannot submit orders. PRE_STRONG cannot submit orders. Position closes remain behind the existing strict execution path; VPA only corroborates independent failure evidence.
