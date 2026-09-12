"""Deterministic BARON source-tree verifier used by the Windows release gate."""
from __future__ import annotations
import ast
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = ("main.py", "core/engine.py", "core/runtime.py", "portfolio/manager.py",
            "scanner/deep_scanner.py", "execution/execution_service.py", "requirements.txt", ".env.example")

def main() -> int:
    missing = [p for p in REQUIRED if not (ROOT / p).is_file()]
    if missing:
        print("PROJECT VERIFY: FAIL - missing required files:", ", ".join(missing))
        return 1
    bad_env = ROOT / ".env"
    if bad_env.exists():
        # The release archive must never contain real/local credentials.
        print("PROJECT VERIFY: FAIL - local .env must not be part of the release tree")
        return 1
    py_files = sorted(ROOT.rglob("*.py"))
    for path in py_files:
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except Exception as exc:
            print(f"PROJECT VERIFY: FAIL - {path.relative_to(ROOT)}: {exc}")
            return 1
    # The verifier itself is intentionally dependency-free; import smoke is
    # covered by the dedicated test suite after the environment is installed.
    print(f"PROJECT VERIFY: PASS - {len(py_files)} Python files parse successfully")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
