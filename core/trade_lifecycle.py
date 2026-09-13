"""Durable trade lifecycle journal used by management and dashboard."""
from __future__ import annotations
import json, os, threading, time, uuid
from pathlib import Path

class TradeLifecycleJournal:
    def __init__(self, path=None):
        self.path=Path(path or os.getenv("TRADE_LIFECYCLE_LOG","runtime/trade_lifecycle.jsonl"))
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self._lock=threading.RLock()
    @staticmethod
    def new_trade_id(symbol):
        return f"TRD-{str(symbol).replace('/','_').replace(':','_')}-{int(time.time()*1000)}-{uuid.uuid4().hex[:6].upper()}"
    def emit(self, trade_id, symbol, event, **data):
        rec={"ts":time.time(),"trade_id":trade_id,"symbol":symbol,"event":str(event).upper(),**data}
        with self._lock:
            with self.path.open("a",encoding="utf-8") as f:
                f.write(json.dumps(rec,ensure_ascii=False,sort_keys=True,default=str)+"\n")
        return rec
    def tail(self,limit=100):
        if not self.path.exists():return []
        with self._lock:
            lines=self.path.read_text(encoding="utf-8").splitlines()[-max(1,int(limit)):]
        out=[]
        for line in lines:
            try: out.append(json.loads(line))
            except Exception: continue
        return out
