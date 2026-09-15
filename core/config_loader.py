"""Fail-safe configuration loading for the BARON runtime.

Why this module exists (LIVE safety forensic RC):

python-dotenv's default ``load_dotenv()`` has two production failure modes that
match the observed symptom ``manager=MISSING / enabled=False /
ENABLE_NATIVE_PROTECTION=''`` while ``.env.example`` declares
``ENABLE_NATIVE_PROTECTION=1``:

1. CWD dependence: ``load_dotenv()`` searches the *current working directory*
   for ``.env``. Windows service / batch / scheduled-task startups inherit a
   different CWD than the project root, so ``.env`` is silently not found.

2. Empty-value shadowing: ``load_dotenv()`` (default ``override=False``) will
   never assign a key that ALREADY EXISTS in ``os.environ`` -- even when the
   existing value is an EMPTY string inherited from the machine/user
   environment or a wrapper script. An empty ``ENABLE_NATIVE_PROTECTION``
   therefore blocks the real ``.env`` value, and the fail-closed LIVE gate
   correctly refuses every entry.

This loader is idempotent and CWD-independent: it resolves ``.env`` relative to
the repository root, and merges each file value ONLY when the current process
value is ABSENT or EMPTY. Real non-empty shell overrides keep precedence, so an
explicit ``ENABLE_NATIVE_PROTECTION=1`` in ``.env`` is never silently eclipsed
by a blank variable. It never prints or returns secret values.
"""
from __future__ import annotations

import os
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DOTENV_PATH = _PROJECT_ROOT / ".env"

# Idempotence flags; set exactly once at first call.
_ENV_LOADED = False
_DOTENV_SOURCE = None  # path of the .env actually merged, or None

# Keys whose values must never be echoed (presence only).
_MASKED_SUBSTRINGS = ("KEY", "SECRET", "TOKEN", "PASSWORD", "PASSPHRASE", "CID", "ID")

_TRUTHY = {"1", "true", "yes", "on"}


def _is_blank(value) -> bool:
    return value is None or str(value).strip() == ""


def _is_truthy(value) -> bool:
    return str(value or "").strip().strip('"').strip("'").lower() in _TRUTHY


def find_dotenv_path():
    """Absolute path of the repository .env if present, else None."""
    return str(_DOTENV_PATH) if _DOTENV_PATH.is_file() else None


def dotenv_loaded() -> bool:
    return bool(_ENV_LOADED and _DOTENV_SOURCE)


def ensure_env_loaded() -> bool:
    """Merge the repository .env into os.environ (absent/empty only).

    Idempotent; safe to call from app/bootstrap.py before the engine import
    AND from core/engine.py's configuration section (direct tool imports).
    Returns True if a .env file was found.
    """
    global _ENV_LOADED, _DOTENV_SOURCE
    if _ENV_LOADED:
        return bool(_DOTENV_SOURCE)
    _ENV_LOADED = True
    if not _DOTENV_PATH.is_file():
        return False
    try:
        from dotenv import dotenv_values
        values = dotenv_values(str(_DOTENV_PATH))
    except Exception:
        return False
    merged = 0
    for key, value in values.items():
        if key is None:
            continue
        current = os.environ.get(key)
        if current is None or _is_blank(current):
            # value may be None for "KEY" bare; empty string for "KEY=".
            os.environ[key] = "" if value is None else str(value)
            merged += 1
    _DOTENV_SOURCE = str(_DOTENV_PATH)
    return True


def env_state(key: str) -> str:
    """Sanitized presence report: 'SET' | 'EMPTY' | 'MISSING'."""
    value = os.environ.get(key)
    if value is None:
        return "MISSING"
    if _is_blank(value):
        return "EMPTY"
    return "SET"


def protection_config_status() -> dict:
    """Sanitized diagnostic block for the native-protection config policy.

    Reports PRESENCE (SET / EMPTY / MISSING) and booleans only -- never values.
    """
    keys = (
        "ENABLE_NATIVE_PROTECTION",
        "REQUIRE_NATIVE_PROTECTION_LIVE",
        "NATIVE_PROTECTION_ORDER_TYPE",
        "NATIVE_PROTECTION_VERIFY",
        "NATIVE_PROTECTION_PARAMS_JSON",
    )
    return {
        "dotenv_loaded": dotenv_loaded(),
        "dotenv_path": _DOTENV_SOURCE or find_dotenv_path(),
        "effective_enabled": _is_truthy(os.getenv("ENABLE_NATIVE_PROTECTION", "0")),
        "require_live": _is_truthy(os.getenv("REQUIRE_NATIVE_PROTECTION_LIVE", "1")),
        "config": {key: env_state(key) for key in keys},
    }