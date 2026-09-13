"""News event classification and post-release market-reaction tracker."""
from __future__ import annotations
import re, time

class NewsReactionEngine:
    HIGH=re.compile(r"cpi|ppi|fomc|fed|interest rate|payroll|nfp|employment|gdp|ecb|boe|boj|opec|war|hack|etf approval|rate decision",re.I)
    def classify(self,title,impact="MEDIUM"):
        title=str(title or "")
        high=bool(self.HIGH.search(title)) or str(impact).upper()=="HIGH"
        return {"impact":"HIGH" if high else str(impact).upper(),"event_type":"MACRO" if high else "MARKET","timestamp":time.time()}
    @staticmethod
    def reaction(pre_price, post_price, side=None):
        try:
            move=(float(post_price)-float(pre_price))/float(pre_price)*100.0
        except Exception:return {"move_pct":None,"reaction":"UNKNOWN"}
        direction="UP" if move>0 else "DOWN" if move<0 else "FLAT"
        causal="CONFIRMED" if side and ((str(side).upper()=="BUY" and move>0) or (str(side).upper()=="SELL" and move<0)) else "OBSERVED"
        return {"move_pct":round(move,4),"direction":direction,"causality":causal}
