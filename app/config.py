import os
import logging
from supabase import create_client, Client

logger = logging.getLogger("applyin.config")

class Settings:
    SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
    SUPABASE_SERVICE_KEY: str = os.getenv("SUPABASE_SERVICE_KEY", "")
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    RAZORPAY_KEY_ID: str = os.getenv("RAZORPAY_KEY_ID", "")
    RAZORPAY_KEY_SECRET: str = os.getenv("RAZORPAY_KEY_SECRET", "")
    RAZORPAY_WEBHOOK_SECRET: str = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")
    APP_URL: str = os.getenv("APP_URL", "https://applyin-backend.onrender.com").rstrip("/")
    BACKEND_URL: str = os.getenv("BACKEND_URL", "https://applyin-backend.onrender.com")

    # Consent enforcement. ON by default (DPDP compliance). The signup/first-run
    # consent flow is live, so this stays enforced. Only set ENFORCE_CONSENT=false
    # for a deliberate, temporary debugging session, never in production.
    ENFORCE_CONSENT: bool = os.getenv("ENFORCE_CONSENT", "true").lower() == "true"

    # Dedicated salt for IP hashing (privacy). If unset, a constant fallback is
    # used (fine for dev). Set IP_HASH_SALT explicitly in production so hashes stay
    # stable and private. Never reuse another secret (e.g. the Razorpay key) here.
    IP_HASH_SALT: str = os.getenv("IP_HASH_SALT", "")

    # Credit packages (credits, price in paise for INR, price in cents for USD)
    CREDIT_PACKAGES = [
        {"id": "starter",    "credits": 20,  "inr": 29900,  "usd": 399,   "label": "Starter",    "popular": False},
        {"id": "pro",        "credits": 60,  "inr": 79900,  "usd": 999,   "label": "Pro",        "popular": True},
        {"id": "power",      "credits": 150, "inr": 179900, "usd": 2199,  "label": "Power",      "popular": False},
    ]

    FREE_CREDITS_ON_SIGNUP = 3

settings = Settings()


def validate_settings():
    """Fail fast on a misconfigured deploy instead of booting and erroring on the
    first real request. Called at app startup."""
    required = {
        "SUPABASE_URL": settings.SUPABASE_URL,
        "SUPABASE_SERVICE_KEY": settings.SUPABASE_SERVICE_KEY,
        "OPENAI_API_KEY": settings.OPENAI_API_KEY,
        "RAZORPAY_KEY_SECRET": settings.RAZORPAY_KEY_SECRET,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        raise RuntimeError(f"Required environment variables not set: {', '.join(missing)}")
    if not settings.ENFORCE_CONSENT:
        import warnings
        warnings.warn("ENFORCE_CONSENT is disabled, DPDP consent is NOT being enforced.", stacklevel=2)
    if not settings.IP_HASH_SALT:
        import warnings
        warnings.warn("IP_HASH_SALT not set; using a constant fallback. Set it in "
                      "production for private, stable IP hashes.", stacklevel=2)

_supabase_client: Client | None = None

def get_supabase() -> Client:
    """Singleton general client. Reused across requests to avoid creating a new
    Supabase connection on every call (auth validation, balance checks, etc.).
    NOTE: if you call .auth.sign_in/sign_up on this it adopts the user's session;
    for DB writes that must bypass RLS use get_admin() instead.
    For auth sign-in/sign-up calls use get_auth_client() instead."""
    global _supabase_client
    if _supabase_client is None:
        _supabase_client = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY)
    return _supabase_client


def get_auth_client() -> Client:
    """Fresh client for auth sign-in/sign-up/refresh calls ONLY.
    Must NOT be reused: calling .auth.sign_in* on a client adopts the user's
    session and subsequent DB calls on that client run as that user (bypassing
    service-role). A fresh client per auth call prevents session leakage.

    The auth (GoTrue) client defaults to a 5s httpx read timeout, but a signup that
    sends a confirmation email via SMTP can take longer than that, causing a false
    'read operation timed out' even though Supabase succeeds. We give the auth client
    a generous read timeout so it waits for the real (slower) response instead of
    giving up early."""
    client = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY)
    try:
        import httpx
        # Replace the GoTrue http client with one that waits up to 30s for a response
        # (connect stays short). Guarded so an internal API change can't break auth.
        client.auth._http_client = httpx.Client(
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
            http2=True,
        )
    except Exception as e:
        logger.warning(f"Could not extend auth client timeout (using default): {e}")
    return client

_admin_client: Client | None = None

def get_admin() -> Client:
    """Dedicated service-role client for DB writes. Never call .auth.* on this -
    it must stay authenticated as service_role to bypass RLS.
    Reused singleton: created once, no .auth.* ever called, so it's safe to share."""
    global _admin_client
    if _admin_client is None:
        _admin_client = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY)
    return _admin_client
