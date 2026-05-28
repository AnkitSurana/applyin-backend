from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from app.routers import auth, analyze, credits, webhook
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("fitly")

limiter = Limiter(key_func=get_remote_address)

app = FastAPI(title="Fitly API", version="1.0.0", docs_url="/docs", redoc_url=None)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ── CORS — allow ALL chrome-extension origins + LinkedIn ─────────────────────
# We can't predict the extension ID so we allow all chrome-extension:// origins
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"chrome-extension://.*|https://www\.linkedin\.com|https://.*\.onrender\.com",
    allow_origins=["*"],          # fallback for non-regex clients
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    allow_credentials=True,
    max_age=3600,
)

# ── Fix double slashes (e.g. //credits/payment-callback) ─────────────────────
@app.middleware("http")
async def fix_double_slash(request: Request, call_next):
    if "//" in request.url.path:
        fixed = request.url.path.replace("//", "/")
        new_url = str(request.url).replace(request.url.path, fixed, 1)
        return RedirectResponse(url=new_url, status_code=301)
    return await call_next(request)

app.include_router(auth.router,     prefix="/auth",     tags=["auth"])
app.include_router(analyze.router,  prefix="/analyze",  tags=["analyze"])
app.include_router(credits.router,  prefix="/credits",  tags=["credits"])
app.include_router(webhook.router,  prefix="/webhook",  tags=["webhook"])

# Root-level payment callback fallback
from app.routers.credits import payment_callback
app.add_api_route("/payment-callback", payment_callback, methods=["GET"], tags=["credits"])

@app.get("/health")
def health():
    return {"status": "ok", "service": "Fitly API"}

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled error on {request.url}: {exc}", exc_info=True)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})
