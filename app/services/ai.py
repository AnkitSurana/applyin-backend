import httpx
import json
import logging
import base64 as _b64
from app.config import settings
from app.services.costing import UsageMeter, extract_usage
from app.services.resume_gate import run_resume_gate

logger = logging.getLogger("applyin.ai")


class ResumeRejected(Exception):
    """Raised when the resume gate fails. Carries the machine reason + the
    parsed evidence (pages/words) so the router can message the user and log
    metrics WITHOUT any analysis call being made or any credit charged."""
    def __init__(self, reason: str, gate: dict):
        super().__init__(reason)
        self.reason = reason
        self.gate = gate

# ─────────────────────────────────────────────────────────────────────────────
# MODELS — change here to swap. GPT-4o is grandfathered legacy pricing; new
# accounts may need gpt-4.1. The cost table in costing.py prices whatever you set.
# ─────────────────────────────────────────────────────────────────────────────
ANALYSIS_MODEL  = "gpt-4o"
RESEARCH_MODEL  = "gpt-4o"
INTERVIEW_MODEL = "gpt-4o-mini"   # cheaper + faster; guide quality is fine here

# ─────────────────────────────────────────────────────────────────────────────
# SCORING DIMENSIONS — final match_score is a WEIGHTED SUM of five sub-scores,
# computed in Python (deterministic, auditable). The model only fills sub-scores.
# ─────────────────────────────────────────────────────────────────────────────
SCORE_WEIGHTS = {
    "skills_match":         0.35,
    "experience_match":     0.25,
    "domain_match":         0.20,
    "qualifications_match": 0.10,
    "soft_skills_match":    0.10,
}

def compute_weighted_score(dimensions: dict) -> int:
    total = sum(dimensions.get(dim, 0) * weight for dim, weight in SCORE_WEIGHTS.items())
    return min(100, max(0, round(total)))


# ═════════════════════════════════════════════════════════════════════════════
# OpenAI helpers
# ═════════════════════════════════════════════════════════════════════════════

def _strip_to_json(raw: str) -> dict:
    """Parse a model string that should be JSON, tolerating ``` fences."""
    raw = (raw or "").strip()
    cleaned = raw.replace("```json", "").replace("```", "").strip()
    for attempt in (cleaned, raw):
        try:
            return json.loads(attempt)
        except json.JSONDecodeError:
            pass
    s, e = cleaned.find("{"), cleaned.rfind("}")
    if s != -1 and e > s:
        try:
            return json.loads(cleaned[s:e + 1])
        except json.JSONDecodeError:
            pass
    raise Exception("AI returned malformed JSON.")


async def _call_chat_json(client, content, max_tokens, label, meter: UsageMeter,
                          model=INTERVIEW_MODEL, temperature=0.3):
    """Chat-completions JSON-mode call. Records usage into the meter."""
    resp = await client.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}",
                 "Content-Type": "application/json"},
        json={
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": content}],
        },
    )
    if resp.status_code == 401:
        raise Exception("OpenAI authentication failed")
    if resp.status_code == 429:
        raise Exception("RATE_LIMITED")
    if not resp.is_success:
        raise Exception(f"AI service error {resp.status_code} ({label}): {resp.text[:300]}")

    data = resp.json()
    in_tok, out_tok = extract_usage(data)
    meter.record(label, model, in_tok, out_tok)

    choice = (data.get("choices") or [{}])[0]
    if choice.get("finish_reason") == "length":
        raise Exception(f"AI response truncated ({label}): exceeded {max_tokens} tokens.")
    raw = (choice.get("message") or {}).get("content", "") or ""
    return _strip_to_json(raw)


async def _call_responses_json(client, content_blocks, max_tokens, label, meter: UsageMeter,
                               model=ANALYSIS_MODEL, temperature=0.3,
                               used_web_search=False):
    """
    Responses-API call. `content_blocks` is the list passed as input[0].content.
    Records usage. Returns parsed JSON from the assembled output_text.
    """
    resp = await client.post(
        "https://api.openai.com/v1/responses",
        headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}",
                 "Content-Type": "application/json"},
        json={
            "model": model,
            "max_output_tokens": max_tokens,
            "temperature": temperature,
            "text": {"format": {"type": "json_object"}},
            "input": [{"role": "user", "content": content_blocks}],
        },
    )
    if resp.status_code == 401:
        raise Exception("OpenAI authentication failed")
    if resp.status_code == 429:
        raise Exception("RATE_LIMITED")
    if not resp.is_success:
        # Surface the body so the caller can decide on a fallback (e.g. bad file).
        raise Exception(f"AI service error {resp.status_code} ({label}): {resp.text[:400]}")

    data = resp.json()
    in_tok, out_tok = extract_usage(data)
    meter.record(label, model, in_tok, out_tok, used_web_search=used_web_search)

    # Assemble text from output blocks (same shape your research call parses).
    text_parts = []
    for block in data.get("output", []):
        if block.get("type") == "message":
            for part in block.get("content", []):
                if part.get("type") in ("output_text", "text"):
                    text_parts.append(part.get("text", ""))
    raw = "\n".join(text_parts).strip()
    if not raw and data.get("output_text"):
        raw = data["output_text"]
    return _strip_to_json(raw)


def _render_pdf_to_image_blocks(resume_b64: str) -> list:
    """
    Fallback path: rasterise each PDF page to PNG and return Responses image
    blocks. Used only if the input_file path 400s. Requires pymupdf (fitz).
    Returns [] if rendering is unavailable so the caller degrades gracefully.
    """
    try:
        import fitz  # PyMuPDF
    except Exception:
        logger.warning("PyMuPDF not installed — image fallback unavailable")
        return []
    try:
        pdf_bytes = _b64.b64decode(resume_b64)
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        blocks = []
        for page in doc:
            pix = page.get_pixmap(dpi=150)
            png_b64 = _b64.b64encode(pix.tobytes("png")).decode()
            blocks.append({
                "type": "input_image",
                "image_url": f"data:image/png;base64,{png_b64}",
            })
        doc.close()
        return blocks
    except Exception as e:
        logger.error(f"PDF image render failed: {e}")
        return []


# ═════════════════════════════════════════════════════════════════════════════
# Grounding filter — loosened to PRIMARY-token match
# ═════════════════════════════════════════════════════════════════════════════
_STOPWORDS = {
    "the","and","for","with","this","that","jd","requires","resume","has","have",
    "evidence","experience","role","candidate","not","any","your","you","from",
    "using","such","skills","skill","strong","years","year","preferred","required",
    "include","including","ability","work","working","based","data","engineer",
    "orchestration","management","development","design","knowledge","tools","tool",
}

def _grounded_in_jd(text: str, jd_text: str, jd_requirements: list) -> bool:
    """
    True if the PRIMARY token of `text` appears as a whole word in the JD.
    Previously required EVERY token to match, which silently deleted real gaps
    like "Kubernetes orchestration" when the JD only said "Kubernetes". Now we
    take the most specific content token (longest non-stopword) and require only
    that to be present.
    """
    if not text:
        return False
    import re
    hay = (jd_text + " " + " ".join(jd_requirements)).lower()
    tokens = [t for t in re.findall(r"[a-zA-Z0-9+#.]{2,}", text.lower())
              if t not in _STOPWORDS]
    if not tokens:
        return False
    # Primary = longest token (most specific). Fall back to any token matching.
    tokens.sort(key=len, reverse=True)
    for tok in tokens:
        esc = re.escape(tok)
        if re.search(r"(?<![a-z0-9])" + esc + r"(?![a-z0-9])", hay):
            return True
    return False


# ═════════════════════════════════════════════════════════════════════════════
# Prompts (unchanged content; trimmed comments)
# ═════════════════════════════════════════════════════════════════════════════
RESEARCH_PROMPT = """\
You are a job search researcher. Look up real, current information about how \
{company} interviews candidates for {title} roles.

Search for:
1. "{company} {title} interview process" — what rounds, what format
2. "{company} interview questions" — actual questions reported
3. "{company} interview tips Glassdoor / Blind / Reddit" — what candidates say

Then write a concise research brief (200–300 words) covering rounds & format,
technical topics tested, behavioural/culture style, candidate-reported patterns,
and recommended prep resources. If you cannot find reliable info for this specific
company, say so clearly and describe a typical interview for a {title} role at a
company of this type based on the JD context. Be factual. Do not invent. Cite
sources briefly (e.g. "per Glassdoor", "per Reddit").
"""

ANALYSIS_PROMPT = """\
You are Applyin's matching engine. Compare ONE job description against ONE resume and
produce an honest, evidence-grounded JSON report.

THE TWO SOURCES OF TRUTH:
  1. The JOB DESCRIPTION below defines what the role requires. Nothing else.
  2. The RESUME (attached) defines what the candidate has. Nothing else.

Do NOT use outside assumptions about what "this kind of role usually needs".
Do NOT invent requirements not written in the JD.
Do NOT invent candidate experience not written in the resume.
Every claim, gap, and skill must trace to a specific line in the JD or resume.
If you cannot point to where it came from, omit it.

The "Technical skills detected" and "Experience required" lines are naive automated
scans — HINTS ONLY and often wrong. The authoritative source is the full JD text below.
Re-read it and decide the real requirements yourself.

IGNORE any experience figure that is not stated as a hiring requirement. Only treat a
number as required experience if the JD explicitly asks the CANDIDATE to have N years.

═══ JOB ═══
Title: {title}
Company: {company}
Location: {location}
Experience required: {experience}
Technical skills detected (hint only): {skills}

Full Job Description:
{description}

════════════════════════════════════════
STEP 0 — EXTRACT JD REQUIREMENTS FIRST
════════════════════════════════════════
Before scoring, extract every concrete requirement from the JD (hard skills, tools,
years of experience, qualifications, domain). This list is your ONLY allowed
vocabulary for gaps and missing skills, output as "jd_requirements".

════════════════════════════════════════
SCORING — weighted sum computed in Python from your 5 sub-scores
════════════════════════════════════════
  Final % = skills_match×0.35 + experience_match×0.25 + domain_match×0.20
          + qualifications_match×0.10 + soft_skills_match×0.10

Score each 0-100 using ONLY JD + resume evidence:
  skills_match: 100=every required skill shown · 75=most · 50=~half · 25=few · 0=none
  experience_match: 100=meets/exceeds · 75=within 1-2 yrs · 50=~half · 25=large gap · 0=none
  domain_match: 100=same space · 75=adjacent · 50=some overlap · 25=different · 0=unrelated
  qualifications_match: 100=all met · 75=most · 50=partial · 25=missing key · 0=none
  soft_skills_match: 100=strong evidence · 75=good · 50=some · 25=little · 0=none

If no resume is attached: all five = 0.

════════════════════════════════════════
INTERCONNECTION — every section maps to the one above it
════════════════════════════════════════
ANTI-HALLUCINATION: never mention a skill/tool not in the JD text word-for-word or by
clear synonym. Before writing any gap, ask "is this in the JD I was given?" If no → omit.

1. gap_reasons: "JD requires [exact JD phrase] — resume has no evidence of this".
2. missing_skills: each MUST be named in the JD. Empty [] if candidate has everything.
3. Each gap → a resume_suggestion with a concrete fix (gap_addressed names the gap).
4. Each improvement_plan item closes a specific gap (closes_gap names it).
5. resume_suggestions = change the resume NOW; improvement_plan = acquire skills OVER TIME.
6. resume_strengths must also appear in fit_reasons or verdict.
7. resume_strengths ≥3 and fit_reasons ≥3 when a resume is present.

Respond ONLY with valid JSON (no markdown):

{{
  "jd_requirements": ["<each concrete requirement from the JD>"],
  "score_breakdown": {{
    "skills_match": <0-100>, "experience_match": <0-100>, "domain_match": <0-100>,
    "qualifications_match": <0-100>, "soft_skills_match": <0-100>,
    "skills_evidence": "<1 sentence>", "experience_evidence": "<1 sentence>",
    "domain_evidence": "<1 sentence>", "qualifications_evidence": "<1 sentence>",
    "soft_skills_evidence": "<1 sentence>"
  }},
  "verdict": "<2-3 sentences citing role, company, evidence>",
  "resume_strengths": ["<specific strength relevant to this role>"],
  "fit_reasons": ["<JD requires X — resume demonstrates Y>"],
  "gap_reasons": ["<JD requires [exact JD phrase] — resume has no evidence>"],
  "missing_skills": [
    {{"skill": "<must appear in JD>", "importance": "<critical|important|nice-to-have>",
      "how_to_learn": "<specific course/resource/project>"}}
  ],
  "improvement_plan": [
    {{"action": "<closes a named gap>", "closes_gap": "<which gap>",
      "impact": "<high|medium>", "timeframe": "<e.g. 2 weeks>"}}
  ],
  "resume_suggestions": [
    {{"gap_addressed": "<which gap>", "issue": "<weak/missing for this role>",
      "fix": "<exact wording change>", "example": "<rewritten bullet>"}}
  ],
  "apply_recommendation": {{
    "verdict": "<Apply Now|Apply With Prep|Improve First|Skip>",
    "reasoning": "<1-2 sentences>", "next_step": "<single most important action>"
  }}
}}

LIMITS: missing_skills ≤6, improvement_plan ≤5, resume_suggestions ≤5.
"""

INTERVIEW_PROMPT = """\
You are Applyin's interview coach. Produce a crack-the-interview guide for this exact
role and company, as JSON only.

Role: {title} at {company}
Key requirements from the JD: {requirements}

═══ COMPANY INTERVIEW RESEARCH ═══
{interview_research}

Use the research above. If research found nothing useful, infer from the role type and
say so in company_style. Every technical question must tie to a skill in the JD requirements.

Respond ONLY with valid JSON (no markdown):

{{
  "company_style": "<2-3 sentences, grounded in research>",
  "research_source": "<'Glassdoor', 'Reddit', or 'inferred from JD — no data found'>",
  "technical": [
    {{"question": "<tied to a JD skill>", "why_asked": "<what it tests>",
      "how_to_answer": "<step-by-step framework>", "example_answer_start": "<first 2 sentences>"}}
  ],
  "behavioural": [
    {{"question": "<question>", "why_asked": "<competency/principle>",
      "star_guide": "<S/T/A/R hints for this question>"}}
  ],
  "company_specific": [
    {{"question": "<question this company is known to ask>", "context": "<why>",
      "how_to_answer": "<key points and what to avoid>"}}
  ],
  "coding_round_strategy": {{
    "overview": "<how coding rounds work here>",
    "step_by_step": ["<step 1>","<step 2>","<step 3>","<step 4>","<step 5>"],
    "when_stuck": "<exactly what to say/do>",
    "mistakes_to_avoid": ["<mistake>","<mistake>","<mistake>"]
  }},
  "preparation_checklist": [
    {{"topic": "<topic>", "why": "<why it'll come up>",
      "resource": "<specific resource>", "time_needed": "<e.g. 3 hours>"}}
  ]
}}

LIMITS: technical exactly 5, behavioural exactly 4, company_specific exactly 3,
preparation_checklist 4-6 items.
"""


async def research_company_interview(client, company, title, meter: UsageMeter) -> str:
    prompt = RESEARCH_PROMPT.format(company=company, title=title)
    try:
        resp = await client.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}",
                     "Content-Type": "application/json"},
            json={"model": RESEARCH_MODEL,
                  "tools": [{"type": "web_search_preview"}],
                  "input": prompt},
            timeout=45,
        )
        if not resp.is_success:
            return ""
        data = resp.json()
        in_tok, out_tok = extract_usage(data)
        meter.record("research", RESEARCH_MODEL, in_tok, out_tok, used_web_search=True)
        text_parts = []
        for block in data.get("output", []):
            if block.get("type") == "message":
                for part in block.get("content", []):
                    if part.get("type") in ("output_text", "text"):
                        text_parts.append(part.get("text", ""))
        return "\n".join(text_parts).strip()
    except Exception:
        return ""


# ═════════════════════════════════════════════════════════════════════════════
# Main entry — returns (result_dict, meter, diagnostics)
# ═════════════════════════════════════════════════════════════════════════════
async def run_analysis(job_data: dict, resume_b64: str | None):
    company = job_data.get("company", "")
    title   = job_data.get("title", "")
    jd_text = (job_data.get("description", "") or "")
    meter = UsageMeter()
    diag = {"has_resume": True, "resume_path": "none", "all_zero_scores": False,
            "gate": None}

    async with httpx.AsyncClient(timeout=150) as client:

        # ══ GATE — runs BEFORE any analysis call. No valid resume ⇒ no analysis ══
        gate = await run_resume_gate(client, resume_b64, meter)
        diag["gate"] = gate
        if not gate["valid"]:
            # No analysis call is made. Raise so the router rejects + refunds
            # (in practice the router checks the gate before charging — see below).
            raise ResumeRejected(gate["reason"], gate)

        analysis_prompt = ANALYSIS_PROMPT.format(
            title=title, company=company,
            location=job_data.get("location", "Not specified"),
            experience=job_data.get("experience", "Not specified"),
            skills=", ".join(job_data.get("skills", [])) or "See JD",
            description=jd_text[:8000],
        )

        # ── CALL 1: analysis via Responses API ────────────────────────────────
        # Path chosen from gate evidence: text resumes → input_file; image-only
        # PDFs (scanned) → straight to the image path. No JD-only fallback exists
        # any more: a resume that can't be read was already rejected by the gate.
        if gate["reason"] == "IMAGE_ONLY_PDF":
            img_blocks = _render_pdf_to_image_blocks(resume_b64)
            content = img_blocks + [{"type": "input_text", "text": analysis_prompt}]
            result = await _call_responses_json(client, content, 4000, "analysis", meter)
            diag["resume_path"] = "image_only"
        else:
            file_blocks = [{
                "type": "input_file",
                "filename": "resume.pdf",
                "file_data": f"data:application/pdf;base64,{resume_b64}",
            }]
            content = file_blocks + [{"type": "input_text", "text": analysis_prompt}]
            try:
                result = await _call_responses_json(client, content, 4000, "analysis", meter)
                diag["resume_path"] = "input_file"
            except Exception as e:
                logger.warning(f"input_file path failed ({e}); trying image fallback")
                img_blocks = _render_pdf_to_image_blocks(resume_b64)
                if img_blocks:
                    content = img_blocks + [{"type": "input_text", "text": analysis_prompt}]
                    result = await _call_responses_json(client, content, 4000, "analysis", meter)
                    diag["resume_path"] = "image_fallback"
                else:
                    # Gate already confirmed readability, so this is a hard error,
                    # not a JD-only degrade. Surface it; router refunds.
                    raise

        # ── CALL 2 + 3: research + interview guide (non-fatal) ────────────────
        interview_guide = {}
        try:
            research = await research_company_interview(client, company, title, meter)
            note = research or (f"No interview research found for {company or 'this company'}. "
                                f"Infer from the role type and say so in company_style.")
            reqs = ", ".join(result.get("jd_requirements", [])[:20]) or "See JD"
            interview_prompt = INTERVIEW_PROMPT.format(
                title=title, company=company, requirements=reqs, interview_research=note)
            interview_guide = await _call_chat_json(
                client, interview_prompt, 3500, "interview", meter, model=INTERVIEW_MODEL)
        except Exception as e:
            logger.warning(f"Interview guide failed (non-fatal): {e}")
            # No dummy filler — return an empty guide so the UI hides the section
            # rather than showing fabricated "temporarily unavailable" text.
            interview_guide = {
                "company_style": "", "research_source": "", "technical": [],
                "behavioural": [], "company_specific": [],
                "coding_round_strategy": {}, "preparation_checklist": [],
            }

    result["interview_guide"] = interview_guide

    # ── Deterministic score ───────────────────────────────────────────────────
    breakdown = result.get("score_breakdown", {})
    match_score = compute_weighted_score(breakdown)
    result["match_score"] = match_score
    result["score_breakdown"] = {
        dim: min(100, max(0, int(breakdown.get(dim, 0)))) for dim in SCORE_WEIGHTS
    } | {k: breakdown.get(k, "") for k in (
        "skills_evidence","experience_evidence","domain_evidence",
        "qualifications_evidence","soft_skills_evidence")}
    result["score_weights"] = SCORE_WEIGHTS
    result["fit_level"] = "strong" if match_score >= 75 else "medium" if match_score >= 45 else "weak"

    # Backstop: resume passed the gate but the model still returned all-zero
    # sub-scores ⇒ it didn't actually read it. Do NOT return a fake result.
    if all(int(breakdown.get(d, 0)) == 0 for d in SCORE_WEIGHTS):
        diag["all_zero_scores"] = True
        logger.error("ALL SUB-SCORES ZERO despite valid gate — rejecting (no false output)")
        raise ResumeRejected("ANALYSIS_DEGENERATE", diag["gate"])

    result.setdefault("jd_requirements", [])
    result.setdefault("resume_strengths", [])
    result.setdefault("fit_reasons", [])
    result.setdefault("gap_reasons", [])
    result["missing_skills"]     = result.get("missing_skills", [])[:6]
    result["improvement_plan"]   = result.get("improvement_plan", [])[:5]
    result["resume_suggestions"] = result.get("resume_suggestions", [])[:5]
    result.setdefault("apply_recommendation",
                      {"verdict": "Apply With Prep", "reasoning": "", "next_step": ""})

    # ── Anti-hallucination enforcement (now primary-token grounding) ──────────
    jd_reqs = result.get("jd_requirements", []) or []
    result["missing_skills"] = [
        ms for ms in result["missing_skills"]
        if _grounded_in_jd(ms.get("skill", "") if isinstance(ms, dict) else str(ms), jd_text, jd_reqs)
    ]
    def _sane_experience_gap(g: str) -> bool:
        import re
        m = re.search(r"(\d{1,3})\s*\+?\s*years?", g.lower())
        return not (m and int(m.group(1)) > 20)
    result["gap_reasons"] = [
        g for g in result["gap_reasons"]
        if _grounded_in_jd(g if isinstance(g, str) else g.get("text", ""), jd_text, jd_reqs)
        and _sane_experience_gap(g if isinstance(g, str) else g.get("text", ""))
    ]
    cleaned = []
    for s in result["resume_suggestions"]:
        if not isinstance(s, dict):
            cleaned.append(s); continue
        ga = s.get("gap_addressed", "")
        if not ga or _grounded_in_jd(ga, jd_text, jd_reqs):
            cleaned.append(s)
    result["resume_suggestions"] = cleaned
    result["improvement_plan"] = [
        p for p in result["improvement_plan"]
        if not isinstance(p, dict) or not p.get("closes_gap")
        or _grounded_in_jd(p.get("closes_gap", ""), jd_text, jd_reqs)
    ]

    # Attach usage so the router can log + return it.
    result["usage"] = meter.as_dict()

    # Resume meta for the UI confirmation strip. `name` is DISPLAY-ONLY: the
    # router forwards it to the extension but strips it before caching/logging.
    g = diag["gate"] or {}
    result["resume_meta"] = {
        "name": g.get("name"),                      # display-only, never stored/logged
        "current_title": g.get("current_title"),
        "current_company": g.get("current_company"),
        "pages_parsed": g.get("pages_parsed", 0),
        "word_count": g.get("word_count", 0),
        "email_found": g.get("email_found", False),
        "source": g.get("extraction_text_source", "none"),
    }
    return result, meter, diag
