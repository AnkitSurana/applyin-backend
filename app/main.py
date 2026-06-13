from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from app.routers import auth, analyze, credits, webhook, privacy
from app.limiter import limiter
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("applyin")

app = FastAPI(title="Applyin API", version="1.0.0", docs_url="/docs", redoc_url=None)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ── CORS — explicit allowlist, credentials only for trusted origins ──────────
# Set ALLOWED_ORIGINS env to a comma-separated list including your published
# extension id, e.g. "chrome-extension://abcd...,https://www.linkedin.com".
# Unknown origins get no ACAO header (browser blocks the response). We never
# reflect an arbitrary origin while also sending Allow-Credentials.
import os as _os
_DEFAULT_ORIGINS = "https://www.linkedin.com"
ALLOWED_ORIGINS = {
    o.strip() for o in _os.getenv("ALLOWED_ORIGINS", _DEFAULT_ORIGINS).split(",") if o.strip()
}

def _is_allowed(origin: str) -> bool:
    if not origin:
        return False
    if origin in ALLOWED_ORIGINS:
        return True
    # Allow any chrome/edge extension origin only if explicitly opted in via "*ext*".
    if "ALLOW_ANY_EXTENSION" in _os.environ and origin.startswith(("chrome-extension://", "moz-extension://")):
        return True
    return False

class CORSMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        origin = request.headers.get("origin", "")
        allowed = _is_allowed(origin)

        if request.method == "OPTIONS":
            response = JSONResponse(content={}, status_code=200)
            if allowed:
                response.headers["Access-Control-Allow-Origin"]  = origin
                response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
                response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
                response.headers["Access-Control-Allow-Credentials"] = "true"
                response.headers["Access-Control-Max-Age"] = "3600"
                response.headers["Vary"] = "Origin"
            return response

        response = await call_next(request)
        if allowed:
            response.headers["Access-Control-Allow-Origin"]  = origin
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Vary"] = "Origin"
        return response

app.add_middleware(CORSMiddleware)

# ── Fix double slashes ────────────────────────────────────────────────────────
class DoubleSlashMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if "//" in request.url.path:
            fixed = request.url.path.replace("//", "/")
            new_url = str(request.url).replace(request.url.path, fixed, 1)
            return RedirectResponse(url=new_url, status_code=301)
        return await call_next(request)

app.add_middleware(DoubleSlashMiddleware)

app.include_router(auth.router,     prefix="/auth",     tags=["auth"])
app.include_router(analyze.router,  prefix="/analyze",  tags=["analyze"])
app.include_router(credits.router,  prefix="/credits",  tags=["credits"])
app.include_router(webhook.router,  prefix="/webhook",  tags=["webhook"])
app.include_router(privacy.router,  prefix="/privacy",  tags=["privacy"])

from app.routers.credits import payment_callback
app.add_api_route("/payment-callback", payment_callback, methods=["GET"], tags=["credits"])

@app.get("/health")
def health():
    return {"status": "ok", "service": "Applyin API"}

@app.get("/health/diagnostics")
def diagnostics():
    """Pinpoints why analysis might fail. Reports presence/health of each
    dependency WITHOUT exposing secret values. Safe to call; returns booleans."""
    from app.config import settings
    out = {"checks": {}}

    # PyMuPDF (resume parsing)
    try:
        import fitz
        out["checks"]["pymupdf"] = {"ok": True, "version": getattr(fitz, "VersionBind", "unknown")}
    except Exception as e:
        out["checks"]["pymupdf"] = {"ok": False, "error": str(e)}

    # Env presence (booleans only — never the values)
    out["checks"]["env"] = {
        "OPENAI_API_KEY": bool(settings.OPENAI_API_KEY),
        "SUPABASE_URL": bool(settings.SUPABASE_URL),
        "SUPABASE_SERVICE_KEY": bool(settings.SUPABASE_SERVICE_KEY),
        "RAZORPAY_KEY_ID": bool(settings.RAZORPAY_KEY_ID),
        "ALLOWED_ORIGINS_set": bool(__import__("os").getenv("ALLOWED_ORIGINS")),
    }
    out["checks"]["flags"] = {"ENFORCE_CONSENT": settings.ENFORCE_CONSENT}

    # Supabase reachability
    try:
        from app.config import get_supabase
        db = get_supabase()
        db.table("user_credit_balances").select("user_id").limit(1).execute()
        out["checks"]["supabase"] = {"ok": True}
    except Exception as e:
        out["checks"]["supabase"] = {"ok": False, "error": str(e)[:200]}

    # A tiny self-test parse so you can see the parser works in THIS environment
    try:
        import fitz, base64
        doc = fitz.open(); pg = doc.new_page()
        pg.insert_text((72, 72), "diagnostic resume text sample words here")
        b = base64.b64encode(doc.tobytes()).decode(); doc.close()
        from app.services.resume_gate import parse_resume_stats
        s = parse_resume_stats(b)
        out["checks"]["resume_parser"] = {"ok": s["word_count"] > 0,
                                          "pages": s["pages_parsed"], "words": s["word_count"]}
    except Exception as e:
        out["checks"]["resume_parser"] = {"ok": False, "error": str(e)[:200]}

    out["overall_ok"] = all(
        c.get("ok", True) for c in out["checks"].values() if isinstance(c, dict) and "ok" in c
    ) and out["checks"]["env"]["OPENAI_API_KEY"]
    return out

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled error on {request.url}: {exc}", exc_info=True)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})
