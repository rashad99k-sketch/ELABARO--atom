"""LIVE SAFETY / native-protection root-cause forensic — regression suite.

Covers the full policy chain (LIVE safety forensic RC):

  A. Missing native protection blocks LIVE entry (fail-closed).
  B. Disabled native protection blocks LIVE entry when required.
  C. Manager initialization FAILURE blocks LIVE entry and surfaces a
     sanitized ERROR diagnostic (never silently "MISSING").
  D. Successful initialization permits the gate to proceed.
  E. Protection placement must return a valid exchange order id.
  F. Protection verification must succeed before LIVE is considered safe.
  G. Paper mode works without any exchange-native order requirement.
  H. Technical trades remain independent from News      -> test_news_technical_separation.py
  I. News slot remains exactly one                      -> test_news_technical_separation.py / runtime
  J. Portfolio = 2 Crypto + 2 Index/Stock + 1 Oil/Gold + 1 News = 6  -> allocator tests (guarded here too)
  K. Config loading: an EMPTY inherited env var must NOT eclipse the repo
     .env value (the production root cause); .env must be found independent
     of the current working directory.

The gate code under test is core.engine._native_protection_gate_ok /
_hydrate_native_protection / _ensure_native_protection. Everything is offline:
ccxt/flask are the conftest stubs, and no exchange method can place a real
order (FakeExchange.create_order raises TEST_BOUNDARY).
"""
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("PAPER_MODE", "True")
os.environ.setdefault("BINGX_KEY", "")
os.environ.setdefault("BINGX_SECRET", "")
os.environ.setdefault("NEWS_ENABLED", "True")

import core.engine as E  # noqa: E402
import core.config_loader as cl  # noqa: E402
import portfolio.native_protection as _portfolio_np  # noqa: E402


class _FakeNativeProtection:
    def __init__(self, exchange=None, logger=None):
        self.exchange = exchange
        self.enabled = os.getenv("ENABLE_NATIVE_PROTECTION", "0").strip().lower() in {"1", "true", "yes", "on"}


class _ExplodingNativeProtection:
    def __init__(self, exchange=None, logger=None):
        raise RuntimeError("SIMULATED_INIT_FAILURE")


class NativeProtectionLiveSafetyTest(unittest.TestCase):

    def setUp(self):
        self._env_saved = {
            "ENABLE_NATIVE_PROTECTION": os.environ.get("ENABLE_NATIVE_PROTECTION"),
            "REQUIRE_NATIVE_PROTECTION_LIVE": os.environ.get("REQUIRE_NATIVE_PROTECTION_LIVE"),
            "NATIVE_PROTECTION_ORDER_TYPE": os.environ.get("NATIVE_PROTECTION_ORDER_TYPE"),
            "NATIVE_PROTECTION_VERIFY": os.environ.get("NATIVE_PROTECTION_VERIFY"),
        }
        for k in self._env_saved:
            os.environ.pop(k, None)
        self._saved = {
            "MODE_LIVE": E.MODE_LIVE,
            "REQUIRE_NATIVE_PROTECTION_LIVE": E.REQUIRE_NATIVE_PROTECTION_LIVE,
            "NPM": E.NativeProtectionManager,
            "NP": E._NATIVE_PROTECTION,
            "NPE": E._NATIVE_PROTECTION_ERROR,
            "WARNED": E._protection_required_warned,
            "BLOCKERS": list(E.MEMORY.get("execution_blockers", [])),
            "LAST_BLOCKER": E.STATE.get("last_exec_blocker"),
            "PROT_STATUS": E.STATE.get("protection_status"),
        }
        E.MODE_LIVE = True
        E.REQUIRE_NATIVE_PROTECTION_LIVE = True
        E.NativeProtectionManager = _FakeNativeProtection
        E._NATIVE_PROTECTION = None
        E._NATIVE_PROTECTION_ERROR = None
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
        E._NATIVE_PROTECTION_ERROR = self._saved["NPE"]
        E._protection_required_warned = self._saved["WARNED"]
        if self._saved["PROT_STATUS"] is None:
            E.STATE.pop("protection_status", None)
        else:
            E.STATE["protection_status"] = self._saved["PROT_STATUS"]
        blockers = E.MEMORY.setdefault("execution_blockers", [])
        del blockers[:]
        blockers.extend(self._saved["BLOCKERS"])
        if self._saved["LAST_BLOCKER"] is None:
            E.STATE.pop("last_exec_blocker", None)
        else:
            E.STATE["last_exec_blocker"] = self._saved["LAST_BLOCKER"]

    def _last_blocker(self):
        return E.MEMORY.get("execution_blockers", [])[-1]

    # ---- A. Missing native protection blocks LIVE entry ----
    def test_a_missing_native_protection_blocks_live(self):
        self.assertFalse(E._native_protection_gate_ok("COTI/USDT:USDT", "BUY", 70.0, 25.0))
        self.assertIsNone(E._NATIVE_PROTECTION)
        self.assertEqual(self._last_blocker()["blocker"], "PROTECTION_REQUIRED")
        diag = E.DASHBOARD_STATE.get("native_protection", {})
        self.assertEqual(diag.get("status"), "MISSING")
        self.assertEqual(diag.get("live_entry"), "BLOCKED")
        self.assertEqual(diag.get("manager"), "MISSING")

    # ---- B. Disabled native protection blocks LIVE entry when required ----
    def test_b_disabled_native_protection_blocks_when_required(self):
        os.environ["ENABLE_NATIVE_PROTECTION"] = "0"
        self.assertFalse(E._native_protection_gate_ok("ETH/USDT:USDT", "BUY", 72.0, 26.0))
        self.assertIsNone(E._NATIVE_PROTECTION)
        self.assertEqual(self._last_blocker()["blocker"], "PROTECTION_REQUIRED")
        self.assertEqual(
            E.DASHBOARD_STATE.get("native_protection", {}).get("status"), "DISABLED")

    # ---- C. Manager init failure blocks LIVE entry + ERROR diagnostic ----
    def test_c_init_failure_blocks_with_error_diagnostic(self):
        E.NativeProtectionManager = _ExplodingNativeProtection
        os.environ["ENABLE_NATIVE_PROTECTION"] = "1"
        self.assertFalse(E._native_protection_gate_ok("COTI/USDT:USDT", "BUY", 70.0, 25.0))
        self.assertIsNone(E._NATIVE_PROTECTION)
        self.assertIn("INIT_FAILED", (E._NATIVE_PROTECTION_ERROR or ""))
        diag = E.DASHBOARD_STATE.get("native_protection", {})
        self.assertEqual(diag.get("status"), "ERROR")
        self.assertEqual(diag.get("live_entry"), "BLOCKED")
        # Root cause preserved in the sanitized diagnostic, never the raw stack.
        self.assertIn("SIMULATED_INIT_FAILURE", diag.get("reason", ""))

    def test_sanitize_reason_redacts_secret_values(self):
        out = E._sanitize_reason("exchange error secret=abc123def456789 maybe more")
        self.assertIn("<REDACTED>", out)
        self.assertNotIn("abc123def456789", out)
        out2 = E._sanitize_reason("apiKey: 0123456789ABCDEF0123456789ABCDEF0123")
        self.assertIn("<REDACTED>", out2)
        out3 = E._sanitize_reason("ORDER_STATUS_REJECTED")
        self.assertEqual(out3, "ORDER_STATUS_REJECTED")

    # ---- D. Successful initialization proceeds ----
    def test_d_enabled_initializes_and_gate_proceeds(self):
        os.environ["ENABLE_NATIVE_PROTECTION"] = "1"
        self.assertTrue(E._native_protection_gate_ok("ETH/USDT:USDT", "BUY", 72.0, 26.0))
        self.assertIsNotNone(E._NATIVE_PROTECTION)
        self.assertTrue(E._NATIVE_PROTECTION.enabled)
        diag = E.DASHBOARD_STATE.get("native_protection", {})
        self.assertEqual(diag.get("status"), "ENABLED")
        self.assertEqual(diag.get("live_entry"), "PROCEED")

    def test_d_verified_after_protected_sl(self):
        os.environ["ENABLE_NATIVE_PROTECTION"] = "1"
        self.assertTrue(E._native_protection_gate_ok("ETH/USDT:USDT", "BUY", 72.0, 26.0))
        E._set_protection_status("PROTECTED", order_id="SL-123")
        self.assertEqual(
            E.DASHBOARD_STATE.get("native_protection", {}).get("status"), "VERIFIED")

    # ---- G. Paper mode needs no exchange-native protection ----
    def test_g_paper_mode_no_native_requirement(self):
        E.MODE_LIVE = False
        os.environ["ENABLE_NATIVE_PROTECTION"] = "1"
        self.assertTrue(E._native_protection_gate_ok("LAB/USDT:USDT", "BUY", 70.0, 25.0))
        self.assertNotIn("PROTECTION_REQUIRED",
                         [b["blocker"] for b in E.MEMORY.get("execution_blockers", [])])

    def test_g_paper_synthetic_no_order_submitted(self):
        # Offline: force paper path through PAPER_MODE so no exchange order is
        # attempted with the real gate manager absent.
        old_paper = E.PAPER_MODE
        try:
            E.PAPER_MODE = True
            payload = E._ensure_native_protection("LAB/USDT:USDT")
            self.assertEqual(str(payload.get("status", "")).upper(), "PAPER_SYNTHETIC")
        finally:
            E.PAPER_MODE = old_paper

    # ---- E + F. Manager-level placement / verification contracts ----
    def test_e_place_requires_exchange_order_id(self):
        from portfolio.native_protection import NativeProtectionManager

        class NoIdExchange:
            def create_order(self, *a, **k):
                return {"status": "open"}

        mgr = NativeProtectionManager(NoIdExchange(), lambda *a, **k: None)
        mgr.enabled = True
        result = mgr.place("COTI/USDT:USDT", "BUY", 0.1, 1.0, "LONG")
        self.assertEqual(result["status"], "UNPROTECTED")
        self.assertEqual(result["reason"], "NO_ORDER_ID")

    def test_e_protected_includes_order_id(self):
        from unittest.mock import Mock
        from portfolio.native_protection import NativeProtectionManager
        ex = Mock()
        ex.create_order.return_value = {"id": "sl-abc", "status": "open"}
        ex.fetch_order.return_value = {"id": "sl-abc", "status": "open"}
        mgr = NativeProtectionManager(ex, lambda *a, **k: None)
        mgr.enabled = True
        result = mgr.place("COTI/USDT:USDT", "BUY", 0.1, 50000.0, "LONG")
        self.assertEqual(result["status"], "PROTECTED")
        self.assertTrue(result.get("sl_order_id"))

    def test_f_verification_failure_fails_closed(self):
        from unittest.mock import Mock
        from portfolio.native_protection import NativeProtectionManager
        ex = Mock()
        ex.create_order.return_value = {"id": "sl-bad", "status": "open"}
        ex.fetch_order.return_value = {"id": "sl-bad", "status": "rejected"}
        mgr = NativeProtectionManager(ex, lambda *a, **k: None)
        mgr.enabled = True
        result = mgr.place("COTI/USDT:USDT", "BUY", 0.1, 50000.0, "LONG")
        self.assertEqual(result["status"], "UNPROTECTED")
        self.assertEqual(result["reason"], "ORDER_STATUS_REJECTED")
        self.assertEqual(mgr.status("COTI/USDT:USDT"), "UNPROTECTED")

    def test_f_no_verifier_fails_closed(self):
        from portfolio.native_protection import NativeProtectionManager

        class NoVerify:
            def create_order(self, *a, **k):
                return {"id": "sl-x", "status": "open"}

        mgr = NativeProtectionManager(NoVerify(), lambda *a, **k: None)
        mgr.enabled = True
        result = mgr.place("COTI/USDT:USDT", "BUY", 0.1, 50000.0, "LONG")
        self.assertEqual(result["status"], "UNPROTECTED")
        self.assertEqual(result["reason"], "NO_ORDER_VERIFIER")

    # ---- J. Portfolio capacity guard (5 technical + 1 news = 6) ----
    def test_j_portfolio_caps_combined_bucket(self):
        from portfolio.manager import PortfolioManager
        from portfolio.allocator import GlobalAssetAllocator
        pm = PortfolioManager(6, None)
        a = GlobalAssetAllocator(pm, None)
        cands = [
            {"symbol": "BTC/USDT:USDT", "asset_class": "CRYPTO", "side": "BUY", "priority_score": 90},
            {"symbol": "ETH/USDT:USDT", "asset_class": "CRYPTO", "side": "SELL", "priority_score": 89},
            {"symbol": "NVDA", "asset_class": "STOCK", "side": "BUY", "priority_score": 88},
            {"symbol": "US500/USDT:USDT", "asset_class": "INDEX", "side": "SELL", "priority_score": 87},
            {"symbol": "XAUUSD", "asset_class": "GOLD", "side": "BUY", "priority_score": 86},
            {"symbol": "OILWTI/USDT:USDT", "asset_class": "OIL", "side": "SELL", "priority_score": 85},
            {"symbol": "USTECH/USDT:USDT", "asset_class": "INDEX", "side": "BUY", "priority_score": 84},
        ]
        r = a.allocate(cands, limit=6)
        allowed = [d for d in r.decisions if d.allowed]
        by = {d.symbol: d for d in r.decisions}
        # 2 CRYPTO (BUY/SELL) + STOCK #1 + INDEX #1 (combined seat 2/2) + GOLD
        # #1 (commodity seat 1/1) = 5 technical. Sides mixed so the symmetric
        # 4/4 side-cap control (unchanged) does not interfere with the bucket
        # assertions.
        self.assertEqual(len(allowed), 5)
        self.assertFalse(by["OILWTI/USDT:USDT"].allowed)
        self.assertEqual(by["OILWTI/USDT:USDT"].reason, "COMMODITY_CAP")
        self.assertFalse(by["USTECH/USDT:USDT"].allowed)
        self.assertEqual(by["USTECH/USDT:USDT"].reason, "INDEX_STOCK_CAP")


# ---- K. Config loading: empty-shadow + CWD independence (root cause unit) ----
class ConfigLoaderTest(unittest.TestCase):
    def test_loader_empty_env_var_does_not_eclipse_dotenv_value(self):
        import core.config_loader as cl
        tmp = Path(tempfile.mkdtemp(prefix="cfg_loader_")) / "faux.env"
        tmp.write_text(
            "ENABLE_NATIVE_PROTECTION=1\n"
            "REQUIRE_NATIVE_PROTECTION_LIVE=0\n"
            "NATIVE_PROTECTION_ORDER_TYPE=STOP_MARKET\n"
            "NATIVE_PROTECTION_VERIFY=1\n",
            encoding="utf-8",
        )
        saved = {
            "ENABLE_NATIVE_PROTECTION": os.environ.get("ENABLE_NATIVE_PROTECTION"),
            "REQUIRE_NATIVE_PROTECTION_LIVE": os.environ.get("REQUIRE_NATIVE_PROTECTION_LIVE"),
        }
        old_path, old_loaded, old_src = cl._DOTENV_PATH, cl._ENV_LOADED, cl._DOTENV_SOURCE
        try:
            cl._DOTENV_PATH = tmp
            cl._ENV_LOADED = False
            cl._DOTENV_SOURCE = None
            os.environ.pop("ENABLE_NATIVE_PROTECTION", None)
            os.environ.pop("REQUIRE_NATIVE_PROTECTION_LIVE", None)
            # The production symptom: key exists but with an EMPTY value.
            os.environ["ENABLE_NATIVE_PROTECTION"] = ""
            cl.ensure_env_loaded()
            # The real .env value must win -- empty shadow repaired, not "1".
            self.assertEqual(os.environ.get("ENABLE_NATIVE_PROTECTION"), "1")
            self.assertEqual(cl.env_state("ENABLE_NATIVE_PROTECTION"), "SET")
            self.assertTrue(cl.protection_config_status()["effective_enabled"])
            self.assertFalse(cl.protection_config_status()["require_live"])
            self.assertEqual(
                cl.protection_config_status()["config"]["NATIVE_PROTECTION_ORDER_TYPE"], "SET")
        finally:
            cl._DOTENV_PATH, cl._ENV_LOADED, cl._DOTENV_SOURCE = old_path, old_loaded, old_src
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_loader_nonempty_existing_override_preserved(self):
        import core.config_loader as cl
        tmp = Path(tempfile.mkdtemp(prefix="cfg_loader_")) / "faux.env"
        tmp.write_text("ENABLE_NATIVE_PROTECTION=1\n", encoding="utf-8")
        old_path, old_loaded, old_src = cl._DOTENV_PATH, cl._ENV_LOADED, cl._DOTENV_SOURCE
        saved = os.environ.get("ENABLE_NATIVE_PROTECTION")
        try:
            cl._DOTENV_PATH = tmp
            cl._ENV_LOADED = False
            cl._DOTENV_SOURCE = None
            os.environ["ENABLE_NATIVE_PROTECTION"] = "0"
            cl.ensure_env_loaded()
            # Explicit non-empty shell override keeps precedence.
            self.assertEqual(os.environ.get("ENABLE_NATIVE_PROTECTION"), "0")
        finally:
            cl._DOTENV_PATH, cl._ENV_LOADED, cl._DOTENV_SOURCE = old_path, old_loaded, old_src
            if saved is None:
                os.environ.pop("ENABLE_NATIVE_PROTECTION", None)
            else:
                os.environ["ENABLE_NATIVE_PROTECTION"] = saved


# ---- L. Exact-production regression: EMPTY env + .env merged through the REAL
# engine bootstrap path (loader -> engine hydrate -> gate). No fakes, no helper-
# in-isolation shortcut: this reproduces the production failure and proves the
# fix end-to-end through the actual configuration path used at startup.
class BootstrapConfigPathRegressionTest(unittest.TestCase):

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
            "NP": E._NATIVE_PROTECTION,
            "NPE": E._NATIVE_PROTECTION_ERROR,
            "WARNED": E._protection_required_warned,
            "BLOCKERS": list(E.MEMORY.get("execution_blockers", [])),
            "DOTENV_PATH": cl._DOTENV_PATH,
            "ENV_LOADED": cl._ENV_LOADED,
            "DOTENV_SOURCE": cl._DOTENV_SOURCE,
        }
        E.MODE_LIVE = True
        E.REQUIRE_NATIVE_PROTECTION_LIVE = True
        E._NATIVE_PROTECTION = None
        E._NATIVE_PROTECTION_ERROR = None
        E._protection_required_warned = False
        E.NativeProtectionManager = _portfolio_np.NativeProtectionManager

    def tearDown(self):
        for k, v in self._env_saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        E.MODE_LIVE = self._saved["MODE_LIVE"]
        E.REQUIRE_NATIVE_PROTECTION_LIVE = self._saved["REQUIRE_NATIVE_PROTECTION_LIVE"]
        E._NATIVE_PROTECTION = self._saved["NP"]
        E._NATIVE_PROTECTION_ERROR = self._saved["NPE"]
        E._protection_required_warned = self._saved["WARNED"]
        blockers = E.MEMORY.setdefault("execution_blockers", [])
        del blockers[:]
        blockers.extend(self._saved["BLOCKERS"])
        cl._DOTENV_PATH = self._saved["DOTENV_PATH"]
        cl._ENV_LOADED = self._saved["ENV_LOADED"]
        cl._DOTENV_SOURCE = self._saved["DOTENV_SOURCE"]

    @staticmethod
    def _write_faux_env(body):
        tmp = Path(tempfile.mkdtemp(prefix="bootstrap_cfg_")) / ".env"
        tmp.write_text(body, encoding="utf-8")
        return tmp

    def test_exact_production_failure_repro_repaired_empty_env_plus_dotenv_one(self):
        # Production symptom: EMPTY inherited var eclipses the real .env value.
        os.environ["ENABLE_NATIVE_PROTECTION"] = ""
        cl._DOTENV_PATH = self._write_faux_env(
            "ENABLE_NATIVE_PROTECTION=1\n"
            "REQUIRE_NATIVE_PROTECTION_LIVE=1\n")
        cl._ENV_LOADED = False
        cl._DOTENV_SOURCE = None
        # Real loader, real merge rule (empty shadow repaired).
        self.assertTrue(cl.ensure_env_loaded())
        # The resulting runtime configuration must be 1, state SET.
        self.assertEqual(os.environ.get("ENABLE_NATIVE_PROTECTION"), "1")
        self.assertEqual(cl.env_state("ENABLE_NATIVE_PROTECTION"), "SET")
        self.assertTrue(cl.protection_config_status()["effective_enabled"])
        # Real engine hydrate through the actual gate path -> real manager.
        self.assertTrue(E._native_protection_gate_ok("FIL/USDT:USDT", "BUY", 70.0, 25.0))
        self.assertIsNotNone(E._NATIVE_PROTECTION)
        self.assertTrue(E._NATIVE_PROTECTION.enabled)
        self.assertIsInstance(E._NATIVE_PROTECTION, _portfolio_np.NativeProtectionManager)
        self.assertEqual(
            E.DASHBOARD_STATE.get("native_protection", {}).get("status"), "ENABLED")

    def test_exact_production_failure_still_fails_closed_when_dotenv_is_zero(self):
        # Same empty-shadow repro, but the real .env value is 0 -> gate must
        # STILL block: the loader only repairs the shadow, never invents safety.
        os.environ["ENABLE_NATIVE_PROTECTION"] = ""
        cl._DOTENV_PATH = self._write_faux_env("ENABLE_NATIVE_PROTECTION=0\n")
        cl._ENV_LOADED = False
        cl._DOTENV_SOURCE = None
        self.assertTrue(cl.ensure_env_loaded())
        self.assertEqual(os.environ.get("ENABLE_NATIVE_PROTECTION"), "0")
        self.assertFalse(E._native_protection_gate_ok("ETH/USDT:USDT", "BUY", 72.0, 26.0))
        self.assertIsNone(E._NATIVE_PROTECTION)
        self.assertEqual(
            E.DASHBOARD_STATE.get("native_protection", {}).get("status"), "DISABLED")

    def test_nonempty_shell_override_still_wins_over_dotenv(self):
        # Explicit non-empty override must keep precedence (never clobbered).
        os.environ["ENABLE_NATIVE_PROTECTION"] = "0"
        cl._DOTENV_PATH = self._write_faux_env("ENABLE_NATIVE_PROTECTION=1\n")
        cl._ENV_LOADED = False
        cl._DOTENV_SOURCE = None
        self.assertTrue(cl.ensure_env_loaded())
        self.assertEqual(os.environ.get("ENABLE_NATIVE_PROTECTION"), "0")
        self.assertFalse(E._native_protection_gate_ok("ETH/USDT:USDT", "BUY", 72.0, 26.0))

    def test_missing_dotenv_invents_nothing_and_fails_closed(self):
        # .env absent: loader must NOT invent a value; gate stays fail-closed.
        cl._DOTENV_PATH = Path(tempfile.mkdtemp(prefix="no_dotenv_")) / ".env"
        cl._ENV_LOADED = False
        cl._DOTENV_SOURCE = None
        os.environ.pop("ENABLE_NATIVE_PROTECTION", None)
        self.assertFalse(cl.ensure_env_loaded())
        self.assertIsNone(os.environ.get("ENABLE_NATIVE_PROTECTION"))
        self.assertEqual(cl.env_state("ENABLE_NATIVE_PROTECTION"), "MISSING")
        self.assertFalse(E._native_protection_gate_ok("ETH/USDT:USDT", "BUY", 72.0, 26.0))
        self.assertIsNone(E._NATIVE_PROTECTION)

    def test_malformed_dotenv_value_never_enables_fail_closed(self):
        # Case E: an unparseable .env value must NOT enable protection and must
        # surface a diagnostic that explains the fail-closed verdict.
        os.environ.pop("ENABLE_NATIVE_PROTECTION", None)
        cl._DOTENV_PATH = self._write_faux_env("ENABLE_NATIVE_PROTECTION=definitely-not-1\n")
        cl._ENV_LOADED = False
        cl._DOTENV_SOURCE = None
        self.assertTrue(cl.ensure_env_loaded())
        self.assertEqual(cl.env_state("ENABLE_NATIVE_PROTECTION"), "SET")
        self.assertFalse(cl.protection_config_status()["effective_enabled"])
        self.assertFalse(E._native_protection_gate_ok("ETH/USDT:USDT", "BUY", 72.0, 26.0))
        self.assertIsNone(E._NATIVE_PROTECTION)
        diag = E.DASHBOARD_STATE.get("native_protection", {})
        self.assertEqual(diag.get("status"), "DISABLED")
        self.assertEqual(diag.get("live_entry"), "BLOCKED")
        self.assertIn("ENABLE_NATIVE_PROTECTION must be enabled",
                      E.MEMORY["execution_blockers"][-1]["reason"])

    def test_dotenv_value_is_idempotent_across_engine_and_bootstrap_calls(self):
        # Both bootstrap.ensure_env_loaded() and engine's config section call
        # the SAME loader; a second call must not clobber the merged value.
        os.environ["ENABLE_NATIVE_PROTECTION"] = ""
        cl._DOTENV_PATH = self._write_faux_env("ENABLE_NATIVE_PROTECTION=1\n")
        cl._ENV_LOADED = False
        cl._DOTENV_SOURCE = None
        self.assertTrue(cl.ensure_env_loaded())   # bootstrap-style startup call
        self.assertTrue(cl.ensure_env_loaded())   # engine config-section call
        self.assertEqual(os.environ.get("ENABLE_NATIVE_PROTECTION"), "1")
        self.assertTrue(E._native_protection_gate_ok("LAB/USDT:USDT", "BUY", 70.0, 25.0))


if __name__ == "__main__":
    unittest.main()