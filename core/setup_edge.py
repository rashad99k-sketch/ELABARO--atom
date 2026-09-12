"""Setup fingerprint / edge registry.

No ML dependency and no execution authority.  It records structural features
and verified outcomes, then exposes a conservative historical edge only after a
minimum sample size is available.
"""
from __future__ import annotations
import json, os, time
from pathlib import Path

class SetupEdgeEngine:
    def __init__(self, path=None, min_samples=10):
        self.path=Path(path or os.getenv("SETUP_EDGE_LOG","runtime/setup_outcomes.jsonl"))
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.min_samples=max(3,int(min_samples))
    def fingerprint(self, state):
        return {
            "side": state.get("side"),
            "trade_type": state.get("trade_type"),
            "market_phase": state.get("market_phase"),
            "move_maturity": state.get("move_maturity"),
            "zone_behaviour": state.get("zone_behaviour"),
            "trade_style": state.get("trade_style"),
            "institutional_stage": state.get("institutional_stage"),
            "formation_verdict": (state.get("early_formation") or {}).get("verdict"),
        }
    def record(self, state, pnl_usdt, pnl_pct):
        rec={"ts":time.time(),"trade_id":state.get("trade_id"),"symbol":state.get("current_symbol"),"fingerprint":self.fingerprint(state),"pnl_usdt":float(pnl_usdt or 0),"pnl_pct":float(pnl_pct or 0)}
        with self.path.open("a",encoding="utf-8") as f:f.write(json.dumps(rec,sort_keys=True,default=str)+"\n")
    def score(self, state):
        fp=self.fingerprint(state); rows=[]
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines()[-2000:]:
                try: rows.append(json.loads(line))
                except Exception: pass
        matches=[r for r in rows if r.get("fingerprint")==fp]
        if len(matches)<self.min_samples:return {"available":False,"score":None,"samples":len(matches)}
        wins=sum(1 for r in matches if float(r.get("pnl_pct",0) or 0)>0)
        avg=sum(float(r.get("pnl_pct",0) or 0) for r in matches)/len(matches)
        return {"available":True,"score":round(max(0,min(100,wins/len(matches)*70+max(-5,min(5,avg))*6)),1),"samples":len(matches),"win_rate":round(wins/len(matches)*100,1),"avg_pnl_pct":round(avg,3)}
