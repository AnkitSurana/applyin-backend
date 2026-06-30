import hashlib
from slowapi import Limiter
from slowapi.util import get_remote_address


def _user_or_ip(request):
    """Rate-limit per authenticated user when possible, else per IP. Behind a
    proxy/VPN, IP-only lets one user flood from many exit nodes; keying on the
    bearer token (hashed, never stored) gives a stable per-user limit."""
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return "u:" + hashlib.sha256(auth.encode()).hexdigest()[:16]
    return get_remote_address(request)


# Single shared limiter. Keyed per-user (token hash) with IP fallback. Routers
# import this; main.py wires it into the app and registers the 429 handler.
limiter = Limiter(key_func=_user_or_ip)
