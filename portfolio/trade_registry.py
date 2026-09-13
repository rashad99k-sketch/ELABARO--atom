"""Persistent materialized trade state, inspired by Freqtrade's Trade model.

This is a state ledger, not an execution engine. It exists to survive restart
and preserve management state (TP1, runner, peak ROE, protection) exactly.
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
from pathlib import Path
import json, os, threading, time, hashlib

@dataclass
class TradeRecord:
    trade_id: str
    symbol: str
    side: str
    client_order_id: str = ""
    status: str = "INTENT"
    qty: float = 0.0
    entry: float = 0.0
    sl: float = 0.0
    tp1: float = 0.0
    tp2: float = 0.0
    tp1_done: bool = False
    runner: bool = False
    peak_roe: float = 0.0
    peak_price: float = 0.0
    profit_lock_roe: float = 0.0
    realized_pnl: float = 0.0
    opened_at: float = 0.0
    closed_at: float = 0.0
    version: int = 1
    recovery_source: str = ""
    metadata: dict | None = None

class TradeRegistry:
    def __init__(self, path=None):
        self.path = Path(path or os.getenv("TRADE_STATE_PATH", "runtime/trade_state.json"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._records = self._load()

    def _load(self):
        if not self.path.exists(): return {}
        try:
            raw=json.loads(self.path.read_text(encoding="utf-8"))
            return {k: TradeRecord(**v) for k,v in raw.items() if isinstance(v,dict)}
        except Exception:
            return {}

    def _persist(self):
        tmp=self.path.with_suffix(self.path.suffix+".tmp")
        payload={k:asdict(v) for k,v in self._records.items()}
        tmp.write_text(json.dumps(payload,ensure_ascii=False,sort_keys=True,default=str),encoding="utf-8")
        os.replace(tmp,self.path)

    @staticmethod
    def client_order_id(trade_id: str, action: str="OPEN", stage: str="MAIN") -> str:
        digest=hashlib.sha256(f"{trade_id}:{action}:{stage}".encode()).hexdigest()[:28]
        return f"brn{digest}"[:40]

    def upsert(self, record: TradeRecord) -> TradeRecord:
        with self._lock:
            old=self._records.get(record.trade_id)
            if old is not None: record.version=max(int(old.version)+1,int(record.version))
            self._records[record.trade_id]=record
            self._persist(); return record

    def update(self, trade_id: str, **changes) -> TradeRecord | None:
        with self._lock:
            rec=self._records.get(trade_id)
            if rec is None:return None
            for k,v in changes.items():
                if hasattr(rec,k): setattr(rec,k,v)
            rec.version += 1
            self._persist(); return rec

    def get(self, trade_id): return self._records.get(str(trade_id))
    def active(self): return [r for r in self._records.values() if r.status in {"INTENT","SUBMITTING","UNKNOWN","OPEN","PARTIAL"}]
    def closed(self): return [r for r in self._records.values() if r.status == "CLOSED"]
    def snapshot(self): return {k:asdict(v) for k,v in self._records.items()}
