"""LIVE_SAFETY protection gate regression.

The live gate (REQUIRE_NATIVE_PROTECTION_LIVE, default 1) is fail-closed:
it must block every live entry when exchange-native protection is NOT
configured (ENABLE_NATIVE_PROTECTION unset / "0") and when the adapter is
missing. However a correctly-configured FIRST live entry (ENABLE=1) must not
self-block merely because the manager is not hydrated yet: the gate hydrates
lazily WITHOUT placing any order (the real SL placement stays post-fill), and
it never instantiates the manager when protection is disabled.
"""
import os
import unittest

os.environ.setdefault("PAPER_MODE", "True")
os.environ.setdefault("BINGX_KEY", "")
os.environ.setdefault("BINGX_SECRET", "")
os.environ.setdefault("NEWS_ENABLED", "True")

import core.engine as E  # noqa: E402


class _FakeNativeProtection:
    def __init__(self, exchange=None, logger=None):
        self.exchange = exchange
        self.enabled = os.getenv("ENABLE_NATIVE_PROTECTION", "0").strip().lower() in {"1", "true", "yes", "on"}


class LiveProtectionGateTest(unittest.TestCase):

    def setUp(self):
        self._env_saved = {
            "ENABLE_NATIVE_PROTECTION": os.environ.get("ENABLE_NATIVE_PROTECTION"),
            "REQUIRE_NATIVE_PROTECTION_LIVE": os.environ.get("REQUIRE_NATIVE_PROTECTION_LIVE"),
        }
        for k in self._env_saved:
            os.environ.pop(k, None)
        self._saved = {
            "MODE_LIVE": E.MODE_LIVE,
            "REQUIRE_NATIVE_PROTECTION_LIVE": E.REQUIRE_NATIVE_PROTECTION_LIVE,
            "NPM": E.NativeProtectionManager,
            "NP": E._NATIVE_PROTECTION,
            "WARNED": E._protection_required_warned,
            "BLOCKERS": list(E.MEMORY.get("execution_blockers", [])),
            "LAST_BLOCKER": E.STATE.get("last_exec_blocker"),
        }
        E.MODE_LIVE = True
        E.REQUIRE_NATIVE_PROTECTION_LIVE = True
        E.NativeProtectionManager = _FakeNativeProtection
        E._NATIVE_PROTECTION = None
        E._protection_required_warned = False

    def tearDown(self):
        for k, v in self._env_saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        E.MODE_LIVE = self._saved["MODE_LIVE"]
        E.REQUIRE_NATIVE_PROTECTION_LIVE = self._saved["REQUIRE_NATIVE_PROTECTION_LIVE"]
        E.NativeProtectionManager = self._saved["NPM"]
        E._NATIVE_PROTECTION = self._saved["NP"]
        E._protection_required_warned = self._saved["WARNED"]
        blockers = E.MEMORY.setdefault("execution_blockers", [])
        del blockers[:]
        blockers.extend(self._saved["BLOCKERS"])
        if self._saved["LAST_BLOCKER"] is None:
            E.STATE.pop("last_exec_blocker", None)
        else:
            E.STATE["last_exec_blocker"] = self._saved["LAST_BLOCKER"]

    def _blockers(self):
        return E.MEMORY.get("execution_blockers", [])

    def test_requirement_off_skips_gate_without_hydration(self):
        E.MODE_LIVE = False
        self.assertTrue(E._native_protection_gate_ok("LAB/USDT:USDT", "BUY", 70.0, 25.0))
        self.assertIsNone(E._NATIVE_PROTECTION)

    def test_requirement_flag_false_skips_gate(self):
        E.REQUIRE_NATIVE_PROTECTION_LIVE = False
        self.assertTrue(E._native_protection_gate_ok("LAB/USDT:USDT", "BUY", 70.0, 25.0))
        self.assertIsNone(E._NATIVE_PROTECTION)

    def test_disabled_protection_blocks_and_records(self):
        self.assertFalse(E._native_protection_gate_ok("LAB/USDT:USDT", "BUY", 70.0, 25.0))
        self.assertIsNone(E._NATIVE_PROTECTION)
        last = self._blockers()[-1]
        self.assertEqual(last["blocker"], "PROTECTION_REQUIRED")
        self.assertEqual(last["symbol"], "LAB/USDT:USDT")
        self.assertIn("ENABLE_NATIVE_PROTECTION", E.STATE["last_exec_blocker"]["reason"])

    def test_explicit_zero_env_never_hydrates_manager(self):
        os.environ["ENABLE_NATIVE_PROTECTION"] = "0"
        self.assertFalse(E._native_protection_gate_ok("LAB/USDT:USDT", "BUY", 70.0, 25.0))
        self.assertIsNone(E._NATIVE_PROTECTION)

    def test_enabled_first_entry_hydrates_manager_and_passes(self):
        os.environ["ENABLE_NATIVE_PROTECTION"] = "1"
        self.assertTrue(E._native_protection_gate_ok("LAB/USDT:USDT", "BUY", 70.0, 25.0))
        self.assertIsNotNone(E._NATIVE_PROTECTION)
        self.assertTrue(E._NATIVE_PROTECTION.enabled)
        self.assertEqual(self._blockers(), [])

    def test_missing_adapter_fails_closed_even_when_enabled(self):
        os.environ["ENABLE_NATIVE_PROTECTION"] = "1"
        E.NativeProtectionManager = None
        self.assertFalse(E._native_protection_gate_ok("LAB/USDT:USDT", "BUY", 70.0, 25.0))
        self.assertIsNone(E._NATIVE_PROTECTION)
        self.assertEqual(self._blockers()[-1]["blocker"], "PROTECTION_REQUIRED")

    def test_warning_is_logged_only_once(self):
        logs = []
        orig = E.log_execution
        E.log_execution = lambda msg, *a, **k: logs.append(msg)
        try:
            os.environ["ENABLE_NATIVE_PROTECTION"] = "0"
            for _ in range(3):
                E._native_protection_gate_ok("LAB/USDT:USDT", "BUY", 70.0, 25.0)
            warnings = [m for m in logs if "REQUIRE_NATIVE_PROTECTION_LIVE=0" in m]
            self.assertEqual(len(warnings), 1)
        finally:
            E.log_execution = orig


if __name__ == "__main__":
    unittest.main()