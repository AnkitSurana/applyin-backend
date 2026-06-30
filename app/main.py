from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from app.routers import auth, analyze, credits, webhook, privacy
from app.limiter import limiter
import logging, time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("applyin")

app = FastAPI(
    title="Applyin API",
    version="1.0.0",
    # Disable auto-generated docs in production - the schema reveals all endpoints
    # and makes targeted abuse trivial. Access via /docs?key=YOUR_DIAGNOSTICS_KEY
    # only when DIAGNOSTICS_KEY is set, otherwise disabled entirely.
    docs_url=None,
    redoc_url=None,
)


@app.on_event("startup")
def _validate_on_startup():
    from app.config import validate_settings, settings
    validate_settings()
    # One-line config summary (booleans/counts only, never secret values) so the
    # active security posture is visible at a glance in the logs on every boot.
    logger.info(
        "Applyin API ready | consent_enforced=%s extension_lockdown=%s "
        "diagnostics_enabled=%s ip_salt_set=%s allowed_origins=%d",
        settings.ENFORCE_CONSENT, _LOCK_EXTENSIONS, bool(_DIAGNOSTICS_KEY),
        bool(settings.IP_HASH_SALT), len(ALLOWED_ORIGINS),
    )
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ── CORS - explicit allowlist, credentials only for trusted origins ──────────
# Set ALLOWED_ORIGINS env to a comma-separated list including your published
# extension id, e.g. "chrome-extension://abcd...,https://www.linkedin.com".
# Unknown origins get no ACAO header (browser blocks the response). We never
# reflect an arbitrary origin while also sending Allow-Credentials.
import os as _os
_DEFAULT_ORIGINS = "https://www.linkedin.com"
ALLOWED_ORIGINS = {
    o.strip() for o in _os.getenv("ALLOWED_ORIGINS", _DEFAULT_ORIGINS).split(",") if o.strip()
}

# Diagnostics endpoint secret. Set DIAGNOSTICS_KEY env var in production.
# If unset, the endpoint is disabled (returns 404) to avoid leaking build info.
_DIAGNOSTICS_KEY = _os.getenv("DIAGNOSTICS_KEY", "")

# Lock browser-extension origins to the published IDs listed in ALLOWED_ORIGINS.
# Default OFF so a local unpacked extension (whose id changes) keeps working in dev.
# In PRODUCTION set BLOCK_UNKNOWN_EXTENSIONS=true AND add your published
# chrome-extension://<id> to ALLOWED_ORIGINS, so only your extension is allowed.
# (Value-based parse: only an explicit true/1/yes locks down; any other value or
# unset stays permissive, so this can never lock you out by accident.)
_LOCK_EXTENSIONS = _os.getenv("BLOCK_UNKNOWN_EXTENSIONS", "false").lower() in ("1", "true", "yes")
if not _LOCK_EXTENSIONS:
    logger.warning("CORS: all browser extensions allowed. Set BLOCK_UNKNOWN_EXTENSIONS=true "
                   "+ list your extension id in ALLOWED_ORIGINS to lock down in production.")

def _is_allowed(origin: str) -> bool:
    if not origin:
        return False
    if origin in ALLOWED_ORIGINS:
        return True
    # Browser extension origins (chrome-extension:// / moz-extension://) are our
    # own first-party client and cannot be spoofed by an arbitrary website.
    if origin.startswith(("chrome-extension://", "moz-extension://")):
        if _LOCK_EXTENSIONS:
            return origin in ALLOWED_ORIGINS  # only explicitly listed IDs
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


# ── Request logging - one concise line per request with status + duration ─────
# Added last, so it is the OUTERMOST middleware and times the whole request.
# /health is skipped because the extension polls it constantly (cold-start wake).
class RequestLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in ("/health", "/favicon.ico"):
            return await call_next(request)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            dur = int((time.perf_counter() - start) * 1000)
            logger.exception("%s %s -> 500 (%dms)", request.method, request.url.path, dur)
            raise
        dur = int((time.perf_counter() - start) * 1000)
        level = logging.WARNING if response.status_code >= 500 else logging.INFO
        logger.log(level, "%s %s -> %s (%dms)",
                   request.method, request.url.path, response.status_code, dur)
        return response

app.add_middleware(RequestLogMiddleware)

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


@app.get("/docs", include_in_schema=False)
def docs_redirect(key: str = ""):
    """Swagger UI, gated by DIAGNOSTICS_KEY. Disabled if key is unset."""
    if not _DIAGNOSTICS_KEY or key != _DIAGNOSTICS_KEY:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=404, content={"detail": "Not found"})
    from fastapi.openapi.docs import get_swagger_ui_html
    return get_swagger_ui_html(openapi_url="/openapi.json", title="Applyin API docs")

@app.get("/health/diagnostics")
def diagnostics(key: str = ""):
    """Pinpoints why analysis might fail. Requires DIAGNOSTICS_KEY query param.
    Set the DIAGNOSTICS_KEY env var on Render; leave unset to disable entirely."""
    if not _DIAGNOSTICS_KEY or key != _DIAGNOSTICS_KEY:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=404, content={"detail": "Not found"})
    from app.config import settings
    out = {"checks": {}}

    # Build marker: confirms the CURRENT analysis code is deployed WITHOUT revealing
    # prompt internals. Checks for distinctive markers of the recent fixes so you can
    # tell from one call whether the live backend has them.
    try:
        from app.services.ai import ANALYSIS_PROMPT, run_analysis
        from app.routers.analyze import _jam_ratio, _normalize_for_fingerprint
        import inspect as _inspect
        _ai_src = _inspect.getsource(run_analysis.__wrapped__ if hasattr(run_analysis, "__wrapped__") else run_analysis)
        try:
            from app.services import ai as _aimod
            _ai_src = _inspect.getsource(_aimod)
        except Exception:
            pass
        checks = {
            "core": all(s in ANALYSIS_PROMPT for s in
                        ("STEP 0.5", "requirement_checks")),
            "strengths_fix": ("HARD RULE on strengths" in ANALYSIS_PROMPT
                              and "YEARS OF EXPERIENCE ARE NOT A STRENGTH" in ANALYSIS_PROMPT),
            "poorfit_scores_low": "reserved for ONE case only" in ANALYSIS_PROMPT,
            "jd_readability_gate": callable(_jam_ratio),
            "fingerprint_normalize": callable(_normalize_for_fingerprint),
            "requirement_grounding": "claimed evidence not found in resume" in _ai_src,
        }
        try:
            import inspect as _insp2
            from app.routers import auth as _authmod
            checks["signup_consent"] = "CONSENT recorded at signup" in _insp2.getsource(_authmod)
        except Exception:
            checks["signup_consent"] = False
        out["build"] = {"analysis_current": all(checks.values()), "features": checks}
    except Exception as e:
        out["build"] = {"analysis_current": False, "error": str(e)[:80]}

    # PyMuPDF (resume parsing)
    try:
        import fitz
        out["checks"]["pymupdf"] = {"ok": True, "version": getattr(fitz, "VersionBind", "unknown")}
    except Exception as e:
        out["checks"]["pymupdf"] = {"ok": False, "error": str(e)}

    # Env presence (booleans only - never the values)
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
