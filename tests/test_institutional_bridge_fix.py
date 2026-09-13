"""Fast Institutional Bridge regression tests (2026-09-13 production incident).

Production audit found the bridge inside DeepScanner._analyze_symbol bound
self._institutional_radar to E.queue (the ExecutionQueue: it owns the three
OB/liquidity/structure evaluator methods) and then called the registrar methods
``_update_a_grade_status`` / ``_sync_institutional_zone_registry`` on that same
object. Those methods live ONLY on InstitutionalRadar, so Python raised
AttributeError inside the bridge, the exception was swallowed as a WARN line,
``MEMORY["institutional_zone_analysis"]`` never filled, and promote_to_queue()
early-returned on the empty registry -> Promoted/Queue/Ready/Executed all 0.

These tests run on the REAL engine + REAL DeepScanner modules and prove the
fixed bridge routes registration to a dedicated real InstitutionalRadar and that
a symbol such as REZ actually crosses Watchlist -> Institutional Zone.
"""

import unittest

import core.engine as E
from scanner.deep_scanner import DeepScanner


def _fresh_entry(symbol="REZ/USDT:USDT"):
    return {
        "symbol": symbol,
        "asset_class": "CRYPTO",
        "side": "BUY",
        "price": 100.0,
        "score": 7.5,
        "watch_score": 7.5,
        "strength": "MEDIUM",
        "state": "DISPLACEMENT",
        "deep_analyzed": True,
        "pre_expansion_state": "PRE_EXPANSION_LONG",
        "pre_expansion_evidence": ["BOS", "DISPLACEMENT", "OB_RETEST"],
        "pre_expansion": {
            "phase": "EARLY_EXPANSION",
            "zone_quality": 7.0,
            "indicator_alignment": "BULLISH",
        },
        "analysis": {
            "ob_grade": "A+",
            "roro_signal": True,
            "struct_score": 80.0,
            "liq_score": 75.0,
            "trap_risk": 40.0,
        },
        "institutional": {"score": 70.0},
        "intent_score": 70.0,
        "institutional_zone_active": False,
    }


def _bare_scanner():
    # Avoid constructing StrategyEngine/NewsService: the bridge under test only
    # needs the state that production DeepScanner carries.
    scanner = object.__new__(DeepScanner)
    scanner.stats = {"errors": 0}
    scanner._institutional_radar = E.queue  # exact production evaluator binding
    return scanner


class InstitutionalBridgeFixTest(unittest.TestCase):
    KEYS = (
        "institutional_zone_analysis",
        "institutional_zone_count",
        "institutional_zone_updated_at",
        "watchlist",
    )

    def setUp(self):
        self._saved = {k: E.MEMORY.get(k) for k in self.KEYS}
        E.MEMORY["institutional_zone_analysis"] = {}

    def tearDown(self):
        for k in self.KEYS:
            if self._saved[k] is None:
                E.MEMORY.pop(k, None)
            else:
                E.MEMORY[k] = self._saved[k]

    def test_execution_queue_evaluator_is_routed_to_real_institutional_registrar(self):
        # Precondition: the ExecutionQueue (E.queue) has the OB evaluator methods
        # but NOT the registrar methods - the exact AttributeError the OLD bridge
        # triggered, leaving the registry empty.
        self.assertFalse(
            hasattr(E.queue, "_update_a_grade_status"),
            "precondition broken: ExecutionQueue grew registrar methods",
        )
        self.assertFalse(
            hasattr(E.queue, "_sync_institutional_zone_registry"),
            "precondition broken: ExecutionQueue grew registrar methods",
        )
        scanner = _bare_scanner()
        entry = _fresh_entry("REZ/USDT:USDT")
        E.MEMORY["watchlist"] = {entry["symbol"]: entry}

        ok = scanner._institutional_bridge(
            "REZ/USDT:USDT", entry, entry["score"],
            {"BOS", "DISPLACEMENT", "OB_RETEST"}, "MEDIUM",
        )
        self.assertTrue(ok)
        self.assertIn("REZ/USDT:USDT", E.MEMORY["institutional_zone_analysis"])
        self.assertTrue(entry.get("institutional_zone_active"))
        # The registrar that actually ran is a real InstitutionalRadar, never the
        # OS/OB evaluator object.
        registrar = scanner._institutional_registrar
        self.assertIsNotNone(registrar)
        self.assertIsNot(registrar, E.queue)
        self.assertIsInstance(registrar, E.InstitutionalRadar)
        self.assertTrue(hasattr(registrar, "_update_a_grade_status"))
        self.assertTrue(hasattr(registrar, "_sync_institutional_zone_registry"))

    def test_rez_transition_watchlist_to_institutional_zone(self):
        # Full production registrar logic on the real engine module: a deep
        # analyzed REZ entry must actually cross Watchlist -> Institutional Zone.
        entry = _fresh_entry("REZ/USDT:USDT")
        E.MEMORY["watchlist"] = {entry["symbol"]: entry}
        radar = E.InstitutionalRadar()
        radar._update_a_grade_status(entry)
        radar._sync_institutional_zone_registry("REZ/USDT:USDT", entry)
        reg = E.MEMORY["institutional_zone_analysis"]
        self.assertIn("REZ/USDT:USDT", reg)
        item = reg["REZ/USDT:USDT"]
        self.assertEqual(item["state"], "INSTITUTIONAL_WATCH")
        self.assertEqual(item["side"], "BUY")
        self.assertGreaterEqual(item["precursor_count"], 1)
        self.assertTrue(entry.get("institutional_zone_active"))
        # _update_a_grade_status() really ran on the real registrar: the rich
        # evidence qualifies preparation. A-GRADE_READY here is only a label;
        # execution still requires the queue's causal-zone/trigger/Atom/risk gates.
        self.assertEqual(entry.get("institutional_stage"), "A-GRADE_READY")
        self.assertTrue(entry.get("a_grade_ready"))

    def test_bridge_keeps_symbol_out_when_gate_not_met(self):
        # Weak score + no pre-expansion hypothesis => no registration, resolved
        # as "not yet a candidate" (no AttributeError, registry stays empty).
        scanner = _bare_scanner()
        entry = _fresh_entry("REZ/USDT:USDT")
        entry["score"] = 4.0
        entry["watch_score"] = 4.0
        entry["strength"] = "WEAK"
        E.MEMORY["watchlist"] = {entry["symbol"]: entry}

        ok = scanner._institutional_bridge(
            "REZ/USDT:USDT", entry, 4.0, {"BOS", "DISPLACEMENT"}, "WEAK",
        )
        self.assertFalse(ok)
        self.assertNotIn("REZ/USDT:USDT", E.MEMORY["institutional_zone_analysis"])
        self.assertFalse(entry.get("institutional_zone_active", False))


if __name__ == "__main__":
    unittest.main()