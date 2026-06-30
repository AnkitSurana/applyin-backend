"""
Shared test setup.

- Sets dummy env vars BEFORE any app import (config.py reads env at import time).
- Stubs the heavy external libs (supabase / razorpay / fitz) IF they are not
  installed, so the suite runs anywhere. Tests never hit a real network or DB
  regardless of whether the real libs are present - all external calls are mocked.
"""
import os
import sys
import types
from unittest.mock import MagicMock

# ── Test environment (must be set before importing app.config) ───────────────
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-service-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("RAZORPAY_KEY_ID", "rzp_test_x")
os.environ.setdefault("RAZORPAY_KEY_SECRET", "test-razorpay-secret")
os.environ.setdefault("RAZORPAY_WEBHOOK_SECRET", "test-webhook-secret")
os.environ.setdefault("IP_HASH_SALT", "test-ip-salt")


def _ensure_stub(name, build):
    """Install a stub module under `name` only if the real one isn't importable."""
    if name in sys.modules:
        return
    try:
        __import__(name)
    except Exception:
        sys.modules[name] = build()


def _build_supabase():
    m = types.ModuleType("supabase")

    class _Client:  # placeholder used only for type annotations
        pass

    m.Client = _Client
    m.create_client = lambda url, key: MagicMock(name="SupabaseClient")
    return m


def _build_razorpay():
    m = types.ModuleType("razorpay")
    errors = types.ModuleType("razorpay.errors")

    class BadRequestError(Exception):
        pass

    errors.BadRequestError = BadRequestError
    m.errors = errors
    m.Client = lambda **kw: MagicMock(name="RazorpayClient")
    sys.modules["razorpay.errors"] = errors
    return m


def _build_fitz():
    m = types.ModuleType("fitz")
    m.open = MagicMock(name="fitz.open")
    m.VersionBind = "test"
    return m


_ensure_stub("supabase", _build_supabase)
_ensure_stub("razorpay", _build_razorpay)
_ensure_stub("fitz", _build_fitz)
