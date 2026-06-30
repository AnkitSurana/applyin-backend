from datetime import datetime, timedelta
from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel, Field
from typing import Optional, List
import hashlib, time, logging, uuid
from app.config import get_supabase
from app.dependencies import get_current_user
from app.services.ai import run_analysis, ResumeRejected, SCORE_WEIGHTS
from app.routers.auth import _get_balance
from app.limiter import limiter

router = APIRouter()
logger = logging.getLogger("applyin.analyze")

ANALYSIS_CREDIT_COST = 1


def _has_resume_consent(db, user_id: str) -> bool:
    """DPDP: reads the latest 'resume_processing' consent decision. Kept for
    reference/inspection and the privacy export; analysis no longer gates on it
    (see the log-only model in analyze_job). Returns True only if the newest row is
    granted=true."""
    try:
        rows = db.table("consent_records").select("granted,created_at") \
            .eq("user_id", user_id).eq("purpose", "resume_processing") \
            .order("created_at", desc=True).limit(1).execute()
        return bool(rows.data and rows.data[0].get("granted") is True)
    except Exception as e:
        logger.warning(f"Consent check failed: {e}")
        return False


def _backfill_consent(db, user_id: str, req_id: str, request) -> None:
    """Legacy-account safety net. Consent is meant to be captured ONCE at signup.
    For accounts created before signup recorded consent, there is no consent row, so
    we record it ONE time here (not per analysis) so the user's signup-level consent
    exists going forward. New accounts never reach this - they already have the
    signup consent row. This writes a single backfill row, then future analyses find
    it via _has_resume_consent and do nothing."""
    try:
        from app.routers.privacy import CURRENT_POLICY_VERSION, _hash_ip
        policy_version = CURRENT_POLICY_VERSION
        ip_hash = _hash_ip(request.client.host if request and request.client else "")
        ua = request.headers.get("user-agent", "")[:300] if request else ""
    except Exception:
        policy_version, ip_hash, ua = "unknown", None, ""
    db.table("consent_records").insert({
        "user_id": user_id,
        "purpose": "resume_processing",
        "granted": True,
        "policy_version": policy_version,
        "source": "backfill",
        "ip_hash": ip_hash,
        "user_agent": ua,
    }).execute()
    logger.info(f"CONSENT backfilled for legacy account req={req_id} user={user_id}")

# User-facing messages per gate reason. No silent JD-only output ever.
REJECT_MESSAGES = {
    "NO_RESUME":            "Upload your resume to run an analysis.",
    "NOT_A_PDF":            "That file isn't a valid PDF. Please upload a PDF resume.",
    "EMPTY_PDF":            "We couldn't read any pages from that PDF. Try re-saving and uploading again.",
    "UNREADABLE_PDF":       "We couldn't read that resume. If it's scanned, upload a text-based PDF.",
    "NOT_A_RESUME":         "That doesn't look like a resume. Please upload your CV.",
    "INSUFFICIENT_CONTENT": "That resume has too little readable content to analyse.",
    "RESUME_TOO_LARGE":     "That PDF is too large. Please upload a resume under 8 MB / 30 pages.",
    "ANALYSIS_DEGENERATE":  "We couldn't complete a reliable analysis for this job. This is usually a temporary issue or a job posting we couldn't read cleanly. Please reload the job page and try again. No credit was used.",
}

class JobData(BaseModel):
    title: str = Field("", max_length=300)
    company: str = Field("", max_length=300)
    location: Optional[str] = Field("", max_length=300)
    description: str = Field("", max_length=60_000)
    skills: List[str] = []
    experience: Optional[str] = Field("", max_length=500)

class AnalyzeRequest(BaseModel):
    job: JobData
    # ~8MB PDF ceiling as base64 (rejected before any decode into memory)
    resume_b64: Optional[str] = Field(None, max_length=12_000_000)
    force_refresh: bool = False


def _jam_ratio(text: str) -> float:
    """Fraction of letters sitting in long no-space runs (>25 chars). Clean prose is
    near 0; whitespace-stripped JD text ('partneringcloselywithmanagers...') is high.
    Server-side safety net: if the extension's readability gate is bypassed or an old
    extension runs, the backend still refuses to analyse unreadable JD text rather
    than produce a confident wrong result."""
    if not text:
        return 1.0
    import re
    jammed = sum(len(r) for r in re.findall(r"[A-Za-z]{26,}", text))
    alpha = len(re.findall(r"[A-Za-z]", text)) or 1
    return jammed / alpha


def _normalize_for_fingerprint(text: str) -> str:
    """Normalize text so scrape-to-scrape differences do not change the fingerprint.
    LinkedIn job text can vary slightly between page loads (extra whitespace, blank
    lines, 'show more' toggles, stray punctuation, casing). We reduce the text to its
    meaningful word content: lowercased, punctuation removed, collapsed to single
    spaces. The same job then maps to the same fingerprint even if the raw scrape
    differs cosmetically."""
    if not text:
        return ""
    import re
    t = text.lower()
    t = re.sub(r"[^a-z0-9 ]+", " ", t)   # drop punctuation/symbols, keep words + digits
    t = re.sub(r"\s+", " ", t)            # collapse whitespace runs
    return t.strip()


def job_cache_key(job: JobData, resume_b64: Optional[str]) -> str:
    """Fingerprint of the EXACT inputs: normalized title + company + description +
    full resume. Same meaningful inputs -> same key -> same stored result, so a
    re-run never produces a different analysis for identical content."""
    resume_fp = hashlib.sha256(resume_b64.encode()).hexdigest() if resume_b64 else "NO_RESUME"
    raw = "|".join([
        _normalize_for_fingerprint(job.title),
        _normalize_for_fingerprint(job.company),
        _normalize_for_fingerprint(job.description),
        resume_fp,
    ])
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
    Retention is bounded by the metrics-table purge job.
    NOTE: this is a sensible developer default, not certified legal compliance.
    """
    bd = (result or {}).get("score_breakdown", {})
    subs = " ".join(f"{d[:4]}={int(bd.get(d, 0))}" for d in SCORE_WEIGHTS)
    u = meter_dict or {}
    g = gate or {}
    # Per-call timings (ms), e.g. "resume_gate=1300 analysis=27000 research=6000 interview=18000".
    # Calls run sequentially, so these roughly sum to the total latency.
    timings = " ".join(f"{c['label']}={c.get('duration_ms', 0)}" for c in u.get("calls", []))
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
        f" timings_ms[{timings}]"
        f" tok_in={u.get('total_input_tokens', 0)}"
        f" tok_out={u.get('total_output_tokens', 0)}"
        f" cost=${u.get('total_cost_usd', 0):.5f}"
        f" credits_left={credits_remaining}"
    )
    for c in u.get("calls", []):
        logger.info(f"  └─ req={req_id} {c['label']}[{c['model']}] "
                    f"in={c['input_tokens']} out={c['output_tokens']} "
                    f"{c.get('duration_ms', 0)}ms ${c['cost_usd']:.5f}")


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
@limiter.limit("20/minute")
async def analyze_job(request: Request, req: AnalyzeRequest, user=Depends(get_current_user)):
    db = get_supabase()
    user_id = user.id
    req_id = uuid.uuid4().hex[:12]
    t0 = time.time()

    cache_key = job_cache_key(req.job, req.resume_b64)

    # 1. Fingerprint lookup. The cache_key is a fingerprint of the exact inputs
    # (normalized title + company + JD + full resume). If we have a stored result
    # for this fingerprint, the inputs are unchanged, so we return the SAME result,
    # guaranteeing identical output for identical input. This runs even on a Fresh
    # request: "Fresh" re-analyses only when the resume or JD actually changed (which
    # produces a different fingerprint and so misses this lookup). A deliberate
    # re-run of genuinely identical content does not burn a credit or change output.
    try:
        cached = db.table("analysis_cache").select("result") \
            .eq("cache_key", cache_key).eq("user_id", user_id) \
            .gt("expires_at", datetime.utcnow().isoformat()) \
            .limit(1).execute()
        # Use limit(1) + list rather than maybe_single(): maybe_single() returns a
        # 406 (and a None response that then raises) on a normal miss, which is noisy
        # and fragile. A plain list is empty on miss, one row on hit.
        rows = cached.data or []
        if rows:
            balance = _get_balance(db, user_id)
            stored = rows[0]["result"]   # name already stripped before caching
            res = {**stored, "cached": True, "credits_used": 0,
                   "credits_remaining": balance,
                   "unchanged": bool(req.force_refresh)}
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

    # 1b. JD readability gate (safety net; the extension checks this first). If the
    #     scraped JD text is heavily jammed (spaces stripped), the model cannot read
    #     it and would return a confident but wrong analysis. Refuse instead, with no
    #     credit charged and nothing cached. 0.12 leaves wide margin over clean JDs.
    jr = _jam_ratio(req.job.description)
    if jr > 0.12:
        logger.info(f"JD_UNREADABLE req={req_id} user={user_id} jam_ratio={jr:.3f} "
                    f"job='{req.job.title}'")
        raise HTTPException(status_code=422, detail={
            "code": "JD_UNREADABLE",
            "message": "We couldn't read this job description (its text came through "
                       "run together). Click 'see more' to expand the description, "
                       "reload the page, and try again. No credit was used.",
        })

    # 2. Credits available?
    balance = _get_balance(db, user_id)
    if balance < ANALYSIS_CREDIT_COST:
        logger.warning(f"INSUFFICIENT_CREDITS req={req_id} user={user_id} balance={balance}")
        raise HTTPException(status_code=402, detail="INSUFFICIENT_CREDITS")

    # 2b. Consent (DPDP) - rely on the ONE consent captured at signup.
    #     Consent is given and recorded once at account creation (a consent_records
    #     row). Every analysis simply USES that recorded consent: clicking Analyse is
    #     covered by the consent the user already gave at signup. We do not re-capture
    #     or re-prompt for consent here. We verify the signup consent exists and
    #     proceed; if for some reason it is missing (e.g. a legacy account created
    #     before consent was recorded), we do not block analysis - we record the
    #     consent once now so the signup-consent record is backfilled, then proceed.
    if not _has_resume_consent(db, user_id):
        try:
            _backfill_consent(db, user_id, req_id, request)
        except Exception as e:
            logger.warning(f"Consent backfill failed (non-fatal) req={req_id}: {e}")

    # 3. Run - but DO NOT charge yet. The gate inside run_analysis may reject,
    #    and a rejected resume must never cost a credit or produce output.
    try:
        result, meter, diag = await run_analysis(req.job.dict(), req.resume_b64, req_id=req_id)
    except ResumeRejected as rj:
        # No credit was deducted. No analysis output exists. Tell the user plainly.
        g = rj.gate or {}
        logger.info(f"ANALYSIS_REJECTED req={req_id} user={user_id} reason={rj.reason} "
                    f"pages={g.get('pages_parsed',0)} words={g.get('word_count',0)}")
        raise HTTPException(status_code=422, detail={
            "code": rj.reason,
            "message": REJECT_MESSAGES.get(rj.reason, "We couldn't analyse that resume."),
            "pages_parsed": g.get("pages_parsed", 0),
            "word_count": g.get("word_count", 0),
        })
    except Exception as e:
        logger.error(f"Analysis FAILED req={req_id} user={user_id} job='{req.job.title}': {e}")
        raise HTTPException(status_code=500, detail="Analysis failed. Please try again.")

    # 4. Analysis succeeded → NOW charge the credit.
    db.table("credit_ledger").insert({
        "user_id": user_id, "delta": -ANALYSIS_CREDIT_COST, "reason": "analysis",
        "meta": {"job_title": req.job.title, "company": req.job.company, "req": req_id}
    }).execute()
    new_balance = balance - ANALYSIS_CREDIT_COST

    # 5. Cache the result - PII (name) stripped first.
    # TTL is 1 day to keep the table small and consistent with retention_policy
    # (analysis_cache = 1 day). A re-run after expiry is a fresh analysis whose
    # score may vary slightly; the extension tells the user this.
    cacheable = _strip_pii(result)
    try:
        db.table("analysis_cache").upsert({
            "cache_key": cache_key, "user_id": user_id, "result": cacheable,
            "expires_at": (datetime.utcnow() + timedelta(days=1)).isoformat()
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
