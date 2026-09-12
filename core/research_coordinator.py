"""BARON Evidence Coordinator.

A deterministic adaptation of the useful TradingAgents ideas: independent
bull/bear challenge, structured evidence, explicit contradictions and a single
review packet.  This module is advisory only.  It cannot place, resize, or
close a trade and it never replaces BARON's deterministic entry/risk/execution
authorities.
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Any, Dict, List
import time


@dataclass
class ResearchDecisionPacket:
    symbol: str
    side: str
    setup: str
    phase: str
    structure: str
    liquidity: str
    order_block: str
    zone: str
    volume: str
    l2: str
    trend: str
    bull_case: List[str]
    bear_case: List[str]
    contradictions: List[str]
    evidence_count: int
    entry_readiness: str
    created_at: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class EvidenceCoordinator:
    """Build a compact, explainable review packet from already-available data."""

    @staticmethod
    def _directional(side: str, value: str) -> bool:
        s = str(side).upper()
        v = str(value or "").upper()
        return (s == "BUY" and any(x in v for x in ("BUY", "BULL", "LONG", "DEMAND", "SUPPORT"))) or (s == "SELL" and any(x in v for x in ("SELL", "BEAR", "SHORT", "SUPPLY", "RESISTANCE")))

    def review(self, *, symbol: str, side: str, context: Dict[str, Any] | None = None,
               market: Dict[str, Any] | None = None) -> ResearchDecisionPacket:
        context = context if isinstance(context, dict) else {}
        market = market if isinstance(market, dict) else {}
        side = str(side or "").upper()
        setup = str(context.get("setup") or context.get("classification") or "INSTITUTIONAL_SETUP")
        phase = str(context.get("phase") or context.get("institutional_stage") or context.get("move_maturity") or "UNKNOWN")
        structure = str(context.get("structure") or context.get("structure_shift") or market.get("structure") or "UNKNOWN")
        liquidity = str(context.get("liquidity") or context.get("liquidity_event") or market.get("liquidity") or "UNKNOWN")
        ob_grade = str(context.get("ob_grade") or context.get("order_block") or "NONE")
        zone_raw = context.get("zone_info") or context.get("zone")
        zone = str(zone_raw.get("type") if isinstance(zone_raw, dict) else zone_raw or "UNKNOWN")
        vpa = context.get("vpa") if isinstance(context.get("vpa"), dict) else {}
        volume = str(context.get("volume") or context.get("vol_state") or vpa.get("state") or "UNKNOWN")
        l2 = str(context.get("l2") or context.get("orderbook") or "UNKNOWN")
        trend = str(context.get("trend") or context.get("trend_bias") or market.get("trend") or "UNKNOWN")

        bull: List[str] = []
        bear: List[str] = []
        contradictions: List[str] = []

        if self._directional(side, structure):
            bull.append("structure_aligned")
        elif structure not in ("UNKNOWN", "NONE"):
            bear.append("structure_not_aligned")
            contradictions.append("structure_vs_side")

        if self._directional(side, liquidity):
            bull.append("liquidity_event_aligned")
        elif liquidity not in ("UNKNOWN", "NONE"):
            bear.append("liquidity_not_aligned")
            contradictions.append("liquidity_vs_side")

        if ob_grade in {"A+", "A", "B"}:
            bull.append(f"ob_{ob_grade}")
        elif ob_grade in {"NONE", "INVALID", "C"}:
            bear.append("weak_or_missing_ob")
            contradictions.append("ob_quality")

        if zone not in ("UNKNOWN", "NONE", "NEUTRAL"):
            bull.append("directional_zone")
        elif zone == "NEUTRAL":
            contradictions.append("neutral_zone")

        if self._directional(side, trend):
            bull.append("trend_aligned")
        elif trend not in ("UNKNOWN", "NONE"):
            bear.append("trend_counter")
            contradictions.append("trend_vs_side")

        vol_upper = volume.upper()
        if any(x in vol_upper for x in ("EXPAND", "STRONG", "ACCUM", "DISTRIBUTION")):
            bull.append("active_volume")
        elif volume not in ("UNKNOWN", "NONE", "NORMAL"):
            contradictions.append("volume_quality")

        l2_upper = l2.upper()
        if any(x in l2_upper for x in ("SUPPORT", "APPROVE", "SUPPORTIVE")):
            bull.append("l2_supportive")
        elif any(x in l2_upper for x in ("OPPOSING", "DETERIORATION", "REJECT")):
            bear.append("l2_against")
            contradictions.append("l2_against_side")

        evidence_count = len(bull)
        # The coordinator does not veto a setup on its own. It labels the
        # evidence state for the real BARON entry authority to consume.
        if evidence_count >= 4 and len(contradictions) <= 1:
            readiness = "READY_REVIEW"
        elif evidence_count >= 3:
            readiness = "CAUTION_REVIEW"
        else:
            readiness = "INSUFFICIENT_EVIDENCE"

        return ResearchDecisionPacket(
            symbol=str(symbol), side=side, setup=setup, phase=phase,
            structure=structure, liquidity=liquidity, order_block=ob_grade,
            zone=zone, volume=volume, l2=l2, trend=trend,
            bull_case=bull, bear_case=bear,
            contradictions=sorted(set(contradictions)),
            evidence_count=evidence_count,
            entry_readiness=readiness, created_at=time.time(),
        )


GLOBAL_EVIDENCE_COORDINATOR = EvidenceCoordinator()
