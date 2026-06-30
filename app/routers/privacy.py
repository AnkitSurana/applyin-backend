"""
Privacy router - consent management + data-subject-rights (DSR).
DPDP foundations. Scaffold: verify against your real data before production.

Endpoints (all under /privacy):
  POST /privacy/consent            record or withdraw consent for a purpose
  GET  /privacy/consent            current consent state per purpose
  POST /privacy/dsr                raise an access/correction/erasure/portability request
  GET  /privacy/dsr                list my requests + status
  GET  /privacy/export             machine-readable export of my data (portability/access)
  POST /privacy/erase              request erasure (soft-deletes + queues hard delete)
"""
from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel
from typing import Optional
import hashlib, logging
from app.config import get_supabase, settings
from app.dependencies import get_current_user
from app.limiter import limiter

logger = logging.getLogger("applyin.privacy")
router = APIRouter()

# Purposes we ask consent for. Keep in sync with the consent notices on the site.
VALID_PURPOSES = {"resume_processing", "analytics", "marketing_email"}
CURRENT_POLICY_VERSION = "2026-06-21"


def _hash_ip(ip: str) -> str:
    if not ip:
        return ""
    # Dedicated salt only. Never reuse another secret (e.g. the Razorpay key) here:
    # one secret = one purpose. Falls back to a constant if IP_HASH_SALT is unset
    # (fine for dev; set IP_HASH_SALT in production for private, stable hashes).
    salt = (settings.IP_HASH_SALT or "applyin-salt").encode()
    return hashlib.sha256(salt + ip.encode()).hexdigest()[:32]


class ConsentInput(BaseModel):
    purpose: str
    granted: bool
    source: str = "web"


@router.post("/consent")
@limiter.limit("30/minute")
async def record_consent(request: Request, body: ConsentInput, user=Depends(get_current_user)):
    if body.purpose not in VALID_PURPOSES:
        raise HTTPException(400, "Unknown consent purpose")
    db = get_supabase()
    db.table("consent_records").insert({
        "user_id": user.id,
        "purpose": body.purpose,
        "granted": body.granted,
        "policy_version": CURRENT_POLICY_VERSION,
        "source": body.source if body.source in ("web", "extension") else "web",
        "ip_hash": _hash_ip(request.client.host if request.client else ""),
        "user_agent": request.headers.get("user-agent", "")[:300],
    }).execute()
    logger.info(f"CONSENT user={user.id} purpose={body.purpose} granted={body.granted}")
    return {"ok": True, "purpose": body.purpose, "granted": body.granted}


@router.get("/consent")
async def get_consent(user=Depends(get_current_user)):
    """Latest decision per purpose."""
    db = get_supabase()
    rows = db.table("consent_records").select("purpose,granted,created_at") \
        .eq("user_id", user.id).order("created_at", desc=True).execute()
    latest = {}
    for r in (rows.data or []):
        latest.setdefault(r["purpose"], r["granted"])  # first seen = newest
    # default: not granted unless explicitly granted
    return {"policy_version": CURRENT_POLICY_VERSION,
            "consents": {p: latest.get(p, False) for p in VALID_PURPOSES}}


class DSRInput(BaseModel):
    kind: str
    detail: Optional[str] = None


@router.post("/dsr")
@limiter.limit("10/minute")
async def raise_dsr(request: Request, body: DSRInput, user=Depends(get_current_user)):
    if body.kind not in {"access", "correction", "erasure", "portability"}:
        raise HTTPException(400, "Unknown request type")
    db = get_supabase()
    res = db.table("dsr_requests").insert({
        "user_id": user.id, "kind": body.kind, "detail": (body.detail or "")[:2000],
    }).execute()
    rid = (res.data or [{}])[0].get("id")
    logger.info(f"DSR raised user={user.id} kind={body.kind} id={rid}")
    return {"ok": True, "request_id": rid, "kind": body.kind, "status": "received"}


@router.get("/dsr")
async def list_dsr(user=Depends(get_current_user)):
    db = get_supabase()
    rows = db.table("dsr_requests").select("id,kind,status,created_at,due_at,completed_at") \
        .eq("user_id", user.id).order("created_at", desc=True).execute()
    return {"requests": rows.data or []}


@router.get("/export")
@limiter.limit("5/minute")
async def export_my_data(request: Request, user=Depends(get_current_user)):
    """Access + portability: everything we hold about this user, as JSON.
    Resume content is NOT stored, so it does not appear here - only metadata."""
    db = get_supabase()
    def grab(table, cols="*"):
        try:
            return db.table(table).select(cols).eq("user_id", user.id).execute().data or []
        except Exception as e:
            logger.warning(f"export grab {table} failed: {e}")
            return []
    profile = []
    try:
        profile = db.table("user_profiles").select("*").eq("id", user.id).execute().data or []
    except Exception:
        pass
    bundle = {
        "exported_at": __import__("datetime").datetime.utcnow().isoformat() + "Z",
        "user_id": user.id,
        "profile": profile,
        "credits_ledger": grab("credit_ledger"),
        "orders": grab("credit_orders"),
        "usage_events": grab("usage_events"),
        "consents": grab("consent_records"),
        "dsr_requests": grab("dsr_requests"),
        "note": "Resume files and candidate names are not stored by Applyin, so "
                "they are not part of this export.",
    }
    return bundle


@router.post("/erase")
@limiter.limit("3/minute")
async def request_erasure(request: Request, user=Depends(get_current_user)):
    """Queue an erasure. We log a DSR row and immediately purge what we safely can
    (cache + metrics). Account deletion via Supabase auth is handled by an operator
    step (documented), because it also affects billing records you may be legally
    required to retain. This is intentionally conservative."""
    db = get_supabase()
    db.table("dsr_requests").insert({
        "user_id": user.id, "kind": "erasure",
        "detail": "User-initiated erasure via /privacy/erase",
    }).execute()
    purged = {}
    for table in ("analysis_cache", "usage_events"):
        try:
            db.table(table).delete().eq("user_id", user.id).execute()
            purged[table] = "purged"
        except Exception as e:
            purged[table] = f"error: {e}"
            logger.warning(f"erase purge {table} failed: {e}")
    logger.info(f"ERASURE requested user={user.id} purged={purged}")
    return {"ok": True,
            "message": "Erasure requested. Cache and analytics for your account "
                       "were removed. Billing records may be retained as required "
                       "by law; our team will complete account deletion.",
            "purged": purged}
