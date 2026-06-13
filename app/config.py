import os
from supabase import create_client, Client
from functools import lru_cache

class Settings:
    SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
    SUPABASE_SERVICE_KEY: str = os.getenv("SUPABASE_SERVICE_KEY", "")
    SUPABASE_JWT_SECRET: str = os.getenv("SUPABASE_JWT_SECRET", "")
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    RAZORPAY_KEY_ID: str = os.getenv("RAZORPAY_KEY_ID", "")
    RAZORPAY_KEY_SECRET: str = os.getenv("RAZORPAY_KEY_SECRET", "")
    RAZORPAY_WEBHOOK_SECRET: str = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")
    APP_URL: str = os.getenv("APP_URL", "https://applyin-backend.onrender.com").rstrip("/")
    BACKEND_URL: str = os.getenv("BACKEND_URL", "https://applyin-backend.onrender.com")

    # Consent enforcement. OFF by default so analysis works before the consent UI
    # exists. Turn ON (set ENFORCE_CONSENT=true) only AFTER the signup/first-run
    # consent flow is live, or every analysis will 403. DPDP requires this ON in
    # production once consent can actually be collected.
    ENFORCE_CONSENT: bool = os.getenv("ENFORCE_CONSENT", "false").lower() == "true"

    # Credit packages (credits, price in paise for INR, price in cents for USD)
    CREDIT_PACKAGES = [
        {"id": "starter",    "credits": 20,  "inr": 29900,  "usd": 399,   "label": "Starter",    "popular": False},
        {"id": "pro",        "credits": 60,  "inr": 79900,  "usd": 999,   "label": "Pro",        "popular": True},
        {"id": "power",      "credits": 150, "inr": 179900, "usd": 2199,  "label": "Power",      "popular": False},
    ]

    FREE_CREDITS_ON_SIGNUP = 3

settings = Settings()

def get_supabase() -> Client:
    """General client. NOTE: if you call .auth.sign_in/sign_up on this,
    it adopts the user's session and subsequent DB calls run as that user.
    For DB writes that must bypass RLS, use get_admin() instead."""
    return create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY)

_admin_client: Client | None = None

def get_admin() -> Client:
    """Dedicated service-role client for DB writes. Never call .auth.* on this —
    it must stay authenticated as service_role to bypass RLS.
    Reused singleton: created once, no .auth.* ever called, so it's safe to share."""
    global _admin_client
    if _admin_client is None:
        _admin_client = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY)
    return _admin_client
