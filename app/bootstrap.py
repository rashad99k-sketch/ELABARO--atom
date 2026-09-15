"""Application bootstrap boundary.

Keeps process startup separate from the trading kernel, scanner, portfolio,
and dashboard modules.
"""
from __future__ import annotations
import os
import threading
import signal

# CWD-independent, empty-shadow-proof .env loading BEFORE any module that reads
# os.getenv (config_loader merges the repo .env for absent/empty keys only).
from core.config_loader import ensure_env_loaded, protection_config_status

ensure_env_loaded()

import core.engine as E
import scanner.scanner as S
import core.runtime as R
from dashboard import app as DASHBOARD_APP

# Compatibility wiring: the preserved kernel remains the execution authority.
for name in getattr(S, "SCANNER_EXPORTS", []):
    if hasattr(S, name):
        setattr(E, name, getattr(S, name))
for name in getattr(R, "RUNTIME_EXPORTS", []):
    if hasattr(R, name):
        setattr(E, name, getattr(R, name))
for name in (
    "get_usdt_perp_symbols", "scan_market_rf", "smart_scanner_v2",
    "monitor_watchlist", "rebuild_radar_watchlist", "refresh_radar_watchlist",
    "global_discovery_scan", "promote_to_queue", "process_queue_entry",
    "build_rf_dashboard", "run_scanner_v2", "sniper_engine_v2",
    "update_institutional_flow_scanner", "live_institutional_updater",
    "build_40_symbol_universe", "WatchlistRotation", "SNIPER_MODE",
    "CANDIDATE_SCAN_INTERVAL",
):
    if hasattr(S, name):
        setattr(E, name, getattr(S, name))
for name in getattr(R, "RUNTIME_EXPORTS", []):
    if hasattr(R, name):
        setattr(E, name, getattr(R, name))


def run() -> None:
    print("=" * 72)
    print("RF LIQUIDITY PRO — MULTI-MARKET WINDOWS BUILD")
    print(f"MODE: {'LIVE' if E.MODE_LIVE else 'PAPER'}")
    print(f"MAX OPEN POSITIONS: {R.PORTFOLIO.max_positions}")
    print("MARKETS: CRYPTO | STOCKS | INDICES | GOLD | OIL")
    print("NEWS: ENABLED" if os.getenv("NEWS_ENABLED", "true").lower() in {"1", "true", "yes", "on"} else "NEWS: DISABLED")
    _np_cfg = protection_config_status()
    if E.MODE_LIVE and E.REQUIRE_NATIVE_PROTECTION_LIVE:
        _np_enabled = _np_cfg["effective_enabled"]
        print(
            "NATIVE PROTECTION: "
            f"{'REQUIRED + CONF' if _np_enabled else 'REQUIRED + MISSING CONFIG'}"
            f" | ENABLE_NATIVE_PROTECTION={_np_cfg['config']['ENABLE_NATIVE_PROTECTION']}"
            f" | REQUIRE_NATIVE_PROTECTION_LIVE=ON"
            f" | .env loaded={_np_cfg['dotenv_loaded']}"
        )
        if not _np_enabled:
            print("[LIVE_SAFETY] LIVE entries are fail-closed BLOCKED until "
                  "ENABLE_NATIVE_PROTECTION=1 is effective in .env (and "
                  "NATIVE_PROTECTION_ORDER_TYPE/NATIVE_PROTECTION_PARAMS_JSON"
                  " set as needed).")
        else:
            try:
                E._hydrate_native_protection()
                _mgr = E._NATIVE_PROTECTION
                print(f"[NATIVE PROTECTION] manager hydrated: {'INITIALIZED' if _mgr is not None else 'MISSING'} "
                      f"enabled={getattr(_mgr, 'enabled', False) if _mgr is not None else False}")
            except Exception as _hydrate_exc:
                print(f"[NATIVE PROTECTION] manager hydrate FAILED (fail-closed): {_hydrate_exc}")
    else:
        print("NATIVE PROTECTION: not enforced (PAPER mode or "
              "REQUIRE_NATIVE_PROTECTION_LIVE=OFF)")
    print("=" * 72)

    # Background workers remain daemonized so a hard process exit cannot hang,
    # but they are registered with the shared shutdown barrier so normal Ctrl-C
    # / SIGTERM exits stop them before Python finalizes buffered I/O.
    keep_alive_thread = threading.Thread(target=R.keep_alive, daemon=True, name="keep_alive")
    supervisor_thread = threading.Thread(
        target=R.safe_main_loop, args=(DASHBOARD_APP,), daemon=True,
        name="portfolio_supervisor"
    )
    E.register_runtime_thread(keep_alive_thread)
    E.register_runtime_thread(supervisor_thread)
    keep_alive_thread.start()
    supervisor_thread.start()

    def _handle_shutdown(signum, _frame):
        print(f"[SHUTDOWN] Signal {signum} received; stopping background workers...", flush=True)
        E.request_shutdown()

    for _sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
        if _sig is not None:
            try:
                signal.signal(_sig, _handle_shutdown)
            except (ValueError, OSError):
                pass

    port = int(os.environ.get("PORT", "8000"))
    print(f"[START] Dashboard: http://127.0.0.1:{port}")
    try:
        DASHBOARD_APP.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
    finally:
        # Explicitly stop workers before interpreter teardown. This is the
        # critical guard against Python's _enter_buffered_busy fatal error.
        E.request_shutdown()
