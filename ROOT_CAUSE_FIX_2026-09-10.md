# BARON — Institutional Preparedness / READY Delay Root-Cause Fix

## Confirmed root cause
The institutional scanner correctly computed `institutional_prepared` and `precursor_count`, but queue admission did not preserve those fields on `ExecutionCandidate`. The queue therefore lost the institutional-maturity verdict.

A second execution-path problem was also confirmed: the institutional fast gate could act as an alternate READY authority and skip the canonical deep READY path. This could make READY behavior inconsistent with the normal trigger/confirmation/zone/score/ATOM/ADX/evidence gates.

## Surgical changes
1. `promote_to_queue()` now transfers:
   - `institutional_prepared`
   - `precursor_count`
   - institutional hypothesis/phase/zone state
2. PREPARED candidates can earn a single confirmation from `RETEST_CONFIRMED` when the live price retests the same causal zone and shows rejection or displacement.
3. `RETEST_CONFIRMED` is accepted as a trigger only for PREPARED candidates.
4. PREPARED candidates require 1 confirmed event instead of 2 because their institutional preparation already contains the required precursor cluster. Normal candidates remain at 2 confirmations.
5. All other READY gates remain mandatory: score, trigger, zone window, evidence, ADX, ATOM and risk/execution controls.
6. The institutional fast gate is now an accelerator/prefetch hint only; it cannot mint READY or bypass canonical deep evaluation.
7. Existing monkeypatch-compatible `_detect_trigger_state()` signature was preserved.

## Validation
- 573 tests collected.
- Every test file passed when executed independently/isolated.
- One `test_portfolio_isolation.py` failure occurred only during an intentionally parallel multi-process run because multiple test processes raced on the shared `runtime/trade_state.json` path. The same test file was rerun alone and passed 3/3.
- Targeted root-fix regression: 32 passed, 1 skipped.
- Combined root-fix/evidence/prepared/pipeline regression: 38 passed, 1 skipped.
- Final targeted regression: 47 passed.
- `verify_project.py`: PASS.
- `compileall`: PASS.

## Safety note
This fix does not turn PREPARED into unconditional entry. PREPARED only changes the confirmation requirement; every other canonical READY gate remains authoritative.
