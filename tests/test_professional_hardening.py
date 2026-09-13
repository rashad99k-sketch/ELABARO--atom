import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from core.early_discovery import analyze_formation, classify_move_maturity
from core.evidence_bus import EvidenceBus
from core.setup_edge import SetupEdgeEngine
from core.trade_lifecycle import TradeLifecycleJournal
from news.reaction import NewsReactionEngine
import core.engine as E


def _df(n=100, base=100.0):
    t=np.arange(n)
    x=base + 0.15*np.sin(t/4)
    return pd.DataFrame({"timestamp":t,"open":x-0.03,"high":x+0.05,"low":x-0.05,"close":x,"volume":1000.0})

class ProfessionalHardeningTest(unittest.TestCase):
    def test_lifecycle_journal_stable_trade_id_and_jsonl(self):
        with tempfile.TemporaryDirectory() as d:
            j=TradeLifecycleJournal(Path(d)/"trade.jsonl")
            tid=j.new_trade_id("BTC/USDT:USDT")
            j.emit(tid,"BTC/USDT:USDT","OPENED",entry=100)
            j.emit(tid,"BTC/USDT:USDT","TP1_VERIFIED",realized_pnl_usdt=2.0)
            rows=j.tail(10)
            self.assertEqual(len(rows),2); self.assertEqual(rows[0]["trade_id"],tid); self.assertEqual(rows[1]["event"],"TP1_VERIFIED")

    def test_evidence_bus_preserves_provenance_and_unknown(self):
        b=EvidenceBus(); b.publish("BTC","CVD",value=None,source="OKX",timeframe="15m",status="UNKNOWN",quality="UNKNOWN")
        snap=b.snapshot("BTC")["CVD"]
        self.assertEqual(snap["source"],"OKX"); self.assertEqual(snap["status"],"UNKNOWN"); self.assertIsNone(snap["value"])

    def test_formation_is_closed_candle_only(self):
        df=_df()
        a=analyze_formation(df)
        self.assertTrue(a["no_lookahead"])
        df2=df.copy(); df2.iloc[-1, df2.columns.get_loc("close")]=9999
        b=analyze_formation(df2)
        # The last forming candle is excluded from formation measurements; huge
        # changes to it must not turn a neutral base into a fabricated signal.
        self.assertEqual(a["score"], b["score"])

    def test_maturity_has_expansion_states(self):
        df=_df(100)
        self.assertIn(classify_move_maturity(df), {"PRE_EXPANSION","EARLY_EXPANSION","MID_EXPANSION","LATE_EXPANSION","EXHAUSTION"})

    def test_news_reaction_and_causality(self):
        n=NewsReactionEngine(); r=n.reaction(100,103,"BUY")
        self.assertEqual(r["direction"],"UP"); self.assertEqual(r["causality"],"CONFIRMED")

    def test_setup_edge_requires_minimum_history(self):
        with tempfile.TemporaryDirectory() as d:
            e=SetupEdgeEngine(Path(d)/"edge.jsonl",min_samples=3)
            state={"side":"BUY","trade_type":"TREND","market_phase":"COMPRESSION","move_maturity":"PRE_EXPANSION","zone_behaviour":"DEMAND","trade_style":"SCALP","institutional_stage":"FORMATION","early_formation":{"verdict":"ACCUMULATION"}}
            self.assertFalse(e.score(state)["available"])
            for i in range(3): e.record({**state,"trade_id":str(i)},1.0,1.0)
            out=e.score(state); self.assertTrue(out["available"]); self.assertEqual(out["samples"],3)

    def test_profit_engine_does_not_mark_tp1_at_zero_roe(self):
        saved={k:E.STATE.get(k) for k in ("open","side","entry","remaining_qty","tp1_hit","tp1_done","trail_activated")}
        try:
            E.PAPER_MODE=True
            E.STATE.update({"open":True,"side":"BUY","entry":100.0,"remaining_qty":10.0,"qty":10.0,"qty_initial":10.0,"tp1_hit":False,"tp1_done":False,"trail_activated":False,"margin":100.0})
            result=E.apply_profit_engine("TEST",100.0,_df(120),119,E.STATE)
            self.assertEqual(result,"HOLD"); self.assertFalse(E.STATE["tp1_hit"]); self.assertFalse(E.STATE["tp1_done"])
        finally:
            E.STATE.update(saved)

if __name__ == "__main__": unittest.main()
