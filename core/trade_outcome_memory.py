"""Durable BARON trade-outcome memory.

Stores one immutable outcome record per trade_id.  It is statistical memory,
not an execution authority.  The file is append-only JSONL and duplicate close
callbacks are idempotent by trade_id.
"""
from __future__ import annotations
from pathlib import Path
from typing import Any, Dict
import json
import os
import threading
import time


class TradeOutcomeMemory:
    def __init__(self, path: str | os.PathLike[str] | None = None):
        self.path = Path(path or os.getenv("TRADE_OUTCOME_PATH", "runtime/trade_outcomes.jsonl"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._ids = set()
        self._load_ids()

    def _load_ids(self):
        if not self.path.exists():
            return
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                rec = json.loads(line)
                tid = rec.get("trade_id")
                if tid:
                    self._ids.add(str(tid))
        except Exception:
            # A corrupt history must never stop the trading engine.
            self._ids = set()

    def record(self, outcome: Dict[str, Any]) -> bool:
        tid = str(outcome.get("trade_id") or "").strip()
        if not tid:
            return False
        with self._lock:
            if tid in self._ids:
                return False
            rec = dict(outcome)
            rec.setdefault("recorded_at", time.time())
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True, default=str) + "\n")
            self._ids.add(tid)
            return True

    def contains(self, trade_id: str) -> bool:
        with self._lock:
            return str(trade_id or "") in self._ids


GLOBAL_TRADE_OUTCOME_MEMORY = TradeOutcomeMemory()
