from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional, List
import hashlib, json
from app.config import get_supabase, settings
from app.dependencies import get_current_user
from app.services.ai import run_analysis
from app.routers.auth import _get_balance

router = APIRouter()

ANALYSIS_CREDIT_COST = 1

class JobData(BaseModel):
    title: str
    company: str
    location: Optional[str] = ""
    description: str
    skills: List[str] = []
    experience: Optional[str] = ""

class AnalyzeRequest(BaseModel):
    job: JobData
    resume_b64: Optional[str] = None   # base64 PDF
    force_refresh: bool = False

def job_cache_key(job: JobData, resume_b64: Optional[str]) -> str:
    resume_fp = resume_b64[:60] if resume_b64 else "NO_RESUME"
    raw = f"{job.title}{job.company}{job.description[:200]}{resume_fp}"
    return hashlib.sha256(raw.encode()).hexdigest()[:40]

@router.post("/job")
async def analyze_job(req: AnalyzeRequest, user=Depends(get_current_user)):
    db = get_supabase()
    user_id = user.id

    # 1. Check credits
    balance = _get_balance(db, user_id)
    if balance < ANALYSIS_CREDIT_COST:
        raise HTTPException(status_code=402, detail="INSUFFICIENT_CREDITS")

    # 2. Check cache (don't charge if cached)
    cache_key = job_cache_key(req.job, req.resume_b64)
    if not req.force_refresh:
        cached = db.table("analysis_cache") \
            .select("result") \
            .eq("cache_key", cache_key) \
            .eq("user_id", user_id) \
            .gt("expires_at", "now()") \
            .maybe_single().execute()
        if cached.data:
            return {**cached.data["result"], "cached": True, "credits_used": 0, "credits_remaining": balance}

    # 3. Deduct credit (optimistic — refund on failure)
    db.table("credit_ledger").insert({
        "user_id": user_id,
        "delta": -ANALYSIS_CREDIT_COST,
        "reason": "analysis",
        "meta": {"job_title": req.job.title, "company": req.job.company}
    }).execute()

    try:
        # 4. Run analysis (OpenAI call, key never leaves server)
        result = await run_analysis(req.job.dict(), req.resume_b64)
    except Exception as e:
        # Refund on error
        db.table("credit_ledger").insert({
            "user_id": user_id,
            "delta": ANALYSIS_CREDIT_COST,
            "reason": "analysis_refund",
            "meta": {"error": str(e)}
        }).execute()
        raise HTTPException(status_code=500, detail=f"Analysis failed: {str(e)}")

    # 5. Cache result (24h)
    db.table("analysis_cache").upsert({
        "cache_key": cache_key,
        "user_id": user_id,
        "result": result,
        "expires_at": "now() + interval '24 hours'"
    }).execute()

    # 6. Log analytics
    db.table("usage_events").insert({
        "user_id": user_id,
        "job_title": req.job.title,
        "company": req.job.company,
        "match_score": result.get("match_score"),
        "fit_level": result.get("fit_level"),
        "had_resume": bool(req.resume_b64),
        "credits_used": ANALYSIS_CREDIT_COST,
    }).execute()

    new_balance = balance - ANALYSIS_CREDIT_COST
    return {**result, "cached": False, "credits_used": ANALYSIS_CREDIT_COST, "credits_remaining": new_balance}
