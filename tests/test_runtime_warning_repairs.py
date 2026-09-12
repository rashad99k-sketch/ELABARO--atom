import time
import unittest


class RuntimeWarningRepairTest(unittest.TestCase):
    def test_scanner_ticker_activity_is_optional(self):
        from scanner.deep_scanner import DeepScanner
        class Exchange:
            def fetch_tickers(self):
                raise AttributeError("bulk ticker unsupported")
        s = DeepScanner(max_symbols=1, exchange=Exchange())
        self.assertEqual(s._ticker_activity(), {})
        self.assertEqual(s.status["ticker_activity"], "DEGRADED")

    def test_scanner_ticker_activity_missing_method_is_supported(self):
        from scanner.deep_scanner import DeepScanner
        class Exchange:
            pass
        s = DeepScanner(max_symbols=1, exchange=Exchange())
        self.assertEqual(s._ticker_activity(), {})
        self.assertEqual(s.status["ticker_activity"], "UNSUPPORTED")

    def test_analysis_timestamp_is_initialized_before_analysis(self):
        # Static contract: the timestamp initialization must occur before the
        # first OHLCV/evidence call in _update_symbol.
        from pathlib import Path
        src = Path(__file__).resolve().parents[1] / "core" / "engine.py"
        text = src.read_text(encoding="utf-8")
        start = text.index("    def _update_symbol(self, symbol, entry):")
        block = text[start:start + 1800]
        self.assertIn('if not entry.get("institutional_analysis_time"):', block)
        self.assertLess(block.index('entry["institutional_analysis_time"] = time.time()'), block.index('df = get_ohlcv_safe(symbol, 100)'))

if __name__ == "__main__":
    unittest.main()
