from datetime import datetime, timedelta
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional, List
import hashlib, time, logging, uuid
from app.config import get_supabase, settings
from app.dependencies import get_current_user
from app.services.ai import run_analysis, ResumeRejected, SCORE_WEIGHTS
from app.routers.auth import _get_balance

router = APIRouter()
logger = logging.getLogger("applyin.analyze")

ANALYSIS_CREDIT_COST = 1

# User-facing messages per gate reason. No silent JD-only output ever.
REJECT_MESSAGES = {
    "NO_RESUME":            "Upload your resume to run an analysis.",
    "NOT_A_PDF":            "That file isn't a valid PDF. Please upload a PDF resume.",
    "EMPTY_PDF":            "We couldn't read any pages from that PDF. Try re-saving and uploading again.",
    "UNREADABLE_PDF":       "We couldn't read that resume. If it's scanned, upload a text-based PDF.",
    "NOT_A_RESUME":         "That doesn't look like a resume. Please upload your CV.",
    "INSUFFICIENT_CONTENT": "That resume has too little readable content to analyse.",
    "ANALYSIS_DEGENERATE":  "We couldn't read your resume reliably. Please re-upload and try again.",
}

class JobData(BaseModel):
    title: str
    company: str
    location: Optional[str] = ""
    description: str
    skills: List[str] = []
    experience: Optional[str] = ""

class AnalyzeRequest(BaseModel):
    job: JobData
    resume_b64: Optional[str] = None
    force_refresh: bool = False


def job_cache_key(job: JobData, resume_b64: Optional[str]) -> str:
    """Hash FULL description + FULL resume so different jobs/resumes never collide."""
    resume_fp = hashlib.sha256(resume_b64.encode()).hexdigest() if resume_b64 else "NO_RESUME"
    raw = f"{job.title}|{job.company}|{job.description}|{resume_fp}"
    return hashlib.sha256(raw.encode()).hexdigest()[:40]


def _strip_pii(result: dict) -> dict:
    """Remove the candidate name before anything is cached or persisted.
    The name is display-only and must never leave the response body."""
    r = dict(result)
    rm = dict(r.get("resume_meta") or {})
    rm.pop("name", None)            # drop name from the stored/cached copy
    r["resume_meta"] = rm
    return r


def _audit_log(req_id, user_id, job: JobData, result, meter_dict, *, cached,
               latency_ms, credits_remaining, resume_path, gate):
    """
    Compliance-oriented audit line: metadata only. NO resume text, NO candidate
    name. Identity is the user_id (their own account), not the parsed person.
    Retention is bounded by the metrics-table purge (see schema_migration.sql).
    NOTE: this is a sensible developer default, not certified legal compliance.
    """
    bd = (result or {}).get("score_breakdown", {})
    subs = " ".join(f"{d[:4]}={int(bd.get(d, 0))}" for d in SCORE_WEIGHTS)
    u = meter_dict or {}
    g = gate or {}
    logger.info(
        "AUDIT"
        f" req={req_id}"
        f" user={user_id}"
        f" job='{job.title}'@'{job.company}'"
        f" resume_path={resume_path}"
        f" pages={g.get('pages_parsed', 0)}"
        f" words={g.get('word_count', 0)}"
        f" cached={cached}"
        f" score={(result or {}).get('match_score')}"
        f" fit={(result or {}).get('fit_level')}"
        f" [{subs}]"
        f" latency={latency_ms}ms"
        f" tok_in={u.get('total_input_tokens', 0)}"
        f" tok_out={u.get('total_output_tokens', 0)}"
        f" cost=${u.get('total_cost_usd', 0):.5f}"
        f" credits_left={credits_remaining}"
    )
    for c in u.get("calls", []):
        logger.info(f"  └─ req={req_id} {c['label']}[{c['model']}] "
                    f"in={c['input_tokens']} out={c['output_tokens']} ${c['cost_usd']:.5f}")


def _store_metrics(db, req_id, user_id, job: JobData, result, meter_dict, *,
                   cached, latency_ms, resume_path, gate):
    """Business metrics → usage_events. No PII content; counts + cost only.
    Used for cost/token/behaviour analysis to inform pricing."""
    u = meter_dict or {}
    g = gate or {}
    calls = u.get("calls", [])
    def _c(label):
        for x in calls:
            if x["label"] == label:
                return x
        return {}
    try:
        db.table("usage_events").insert({
            "user_id": user_id,
            "request_id": req_id,
            "job_title": job.title,
            "company": job.company,
            "match_score": (result or {}).get("match_score"),
            "fit_level": (result or {}).get("fit_level"),
            "had_resume": True,
            "resume_path": resume_path,
            "pages_parsed": g.get("pages_parsed", 0),
            "word_count": g.get("word_count", 0),
            "cached": cached,
            "credits_used": 0 if cached else ANALYSIS_CREDIT_COST,
            "input_tokens": u.get("total_input_tokens", 0),
            "output_tokens": u.get("total_output_tokens", 0),
            "total_cost_usd": u.get("total_cost_usd", 0),
            "latency_ms": latency_ms,
            "cost_breakdown": {
                "analysis":  _c("analysis").get("cost_usd", 0),
                "research":  _c("research").get("cost_usd", 0),
                "interview": _c("interview").get("cost_usd", 0),
                "resume_gate": _c("resume_gate").get("cost_usd", 0),
            },
        }).execute()
    except Exception as e:
        logger.warning(f"Metrics insert failed (non-fatal): {e}")


@router.post("/job")
async def analyze_job(req: AnalyzeRequest, user=Depends(get_current_user)):
    db = get_supabase()
    user_id = user.id
    req_id = uuid.uuid4().hex[:12]
    t0 = time.time()

    cache_key = job_cache_key(req.job, req.resume_b64)

    # 1. Cache first — free even at 0 credits
    if not req.force_refresh:
        try:
            cached = db.table("analysis_cache").select("result") \
                .eq("cache_key", cache_key).eq("user_id", user_id) \
                .gt("expires_at", datetime.utcnow().isoformat()) \
                .maybe_single().execute()
            if cached.data:
                balance = _get_balance(db, user_id)
                stored = cached.data["result"]   # name already stripped before caching
                res = {**stored, "cached": True, "credits_used": 0,
                       "credits_remaining": balance}
                lat = int((time.time() - t0) * 1000)
                _audit_log(req_id, user_id, req.job, res, res.get("usage"),
                           cached=True, latency_ms=lat, credits_remaining=balance,
                           resume_path="cache", gate=res.get("resume_meta"))
                _store_metrics(db, req_id, user_id, req.job, res, res.get("usage"),
                               cached=True, latency_ms=lat, resume_path="cache",
                               gate=res.get("resume_meta"))
                return res
        except Exception as e:
            logger.warning(f"Cache check failed (non-fatal): {e}")

    # 2. Credits available?
    balance = _get_balance(db, user_id)
    if balance < ANALYSIS_CREDIT_COST:
        logger.warning(f"INSUFFICIENT_CREDITS req={req_id} user={user_id} balance={balance}")
        raise HTTPException(status_code=402, detail="INSUFFICIENT_CREDITS")

    # 3. Run — but DO NOT charge yet. The gate inside run_analysis may reject,
    #    and a rejected resume must never cost a credit or produce output.
    try:
        result, meter, diag = await run_analysis(req.job.dict(), req.resume_b64)
    except ResumeRejected as rj:
        # No credit was deducted. No analysis output exists. Tell the user plainly.
        g = rj.gate or {}
        logger.info(f"RESUME_REJECTED req={req_id} user={user_id} reason={rj.reason} "
                    f"pages={g.get('pages_parsed',0)} words={g.get('word_count',0)}")
        raise HTTPException(status_code=422, detail={
            "code": rj.reason,
            "message": REJECT_MESSAGES.get(rj.reason, "We couldn't analyse that resume."),
            "pages_parsed": g.get("pages_parsed", 0),
            "word_count": g.get("word_count", 0),
        })
    except Exception as e:
        logger.error(f"Analysis FAILED req={req_id} user={user_id} job='{req.job.title}': {e}")
        raise HTTPException(status_code=500, detail=f"Analysis failed: {str(e)}")

    # 4. Analysis succeeded → NOW charge the credit.
    db.table("credit_ledger").insert({
        "user_id": user_id, "delta": -ANALYSIS_CREDIT_COST, "reason": "analysis",
        "meta": {"job_title": req.job.title, "company": req.job.company, "req": req_id}
    }).execute()
    new_balance = balance - ANALYSIS_CREDIT_COST

    # 5. Cache the result — PII (name) stripped first.
    cacheable = _strip_pii(result)
    try:
        db.table("analysis_cache").upsert({
            "cache_key": cache_key, "user_id": user_id, "result": cacheable,
            "expires_at": (datetime.utcnow() + timedelta(hours=24)).isoformat()
        }).execute()
    except Exception as e:
        logger.warning(f"Cache write failed (non-fatal): {e}")

    # 6. Audit log + business metrics (both PII-free)
    lat = int((time.time() - t0) * 1000)
    _audit_log(req_id, user_id, req.job, result, meter.as_dict(),
               cached=False, latency_ms=lat, credits_remaining=new_balance,
               resume_path=diag.get("resume_path"), gate=diag.get("gate"))
    _store_metrics(db, req_id, user_id, req.job, result, meter.as_dict(),
                   cached=False, latency_ms=lat, resume_path=diag.get("resume_path"),
                   gate=diag.get("gate"))

    # 7. Return to extension WITH the name (display-only; never stored/logged above).
    return {**result, "cached": False, "credits_used": ANALYSIS_CREDIT_COST,
            "credits_remaining": new_balance, "request_id": req_id}
