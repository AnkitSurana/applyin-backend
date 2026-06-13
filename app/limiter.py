from slowapi import Limiter
from slowapi.util import get_remote_address

# Single shared limiter. Keyed by client IP. Routers import this; main.py wires
# it into the app and registers the 429 handler.
limiter = Limiter(key_func=get_remote_address)
