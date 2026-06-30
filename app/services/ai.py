import httpx
import json
import logging
import base64 as _b64
from app.config import settings
from app.services.costing import UsageMeter, extract_usage
from app.services.resume_gate import run_resume_gate
from app.model_config import (model_for, is_reasoning_model, output_budget,
                              effort_or_default, token_budget)

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
# MODELS - one per backend STAGE/role (configured in models.json; edit there to
# swap). Role names match the AUDIT log `timings_ms[...]` labels. What each fills
# in the sidebar:
#   analysis  -> the scoring call. Powers the score header + 4 of the 5 accordions:
#                Fit analysis, Skills gap, Improvement plan, Resume improvements.
#   research  -> web-searches the company's interview style; feeds `interview`
#                (no accordion of its own).
#   interview -> builds the Interview prep accordion (uses `research`).
#   resume_gate (in resume_gate.py) -> validates the resume before charging;
#                powers the "Resume read: ..." strip, not an accordion.
# GPT-5 / o-series are detected automatically and called with reasoning-safe params.
# ─────────────────────────────────────────────────────────────────────────────
ANALYSIS_MODEL  = model_for("analysis")
RESEARCH_MODEL  = model_for("research")
INTERVIEW_MODEL = model_for("interview")

# ─────────────────────────────────────────────────────────────────────────────
# SCORING DIMENSIONS - final match_score is a WEIGHTED SUM of five sub-scores,
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


def _sentence_case(s):
    """Normalize a user-facing string to sentence case for consistent presentation:
    capitalize the first alphabetic character, leave the rest intact (so acronyms
    like SQL, AWS and proper nouns like Snowflake are preserved). Trims whitespace.
    Fixes the model occasionally returning lowercase-leading or inconsistent casing."""
    if not isinstance(s, str):
        return s
    t = s.strip()
    if not t:
        return t
    for i, ch in enumerate(t):
        if ch.isalpha():
            return t[:i] + ch.upper() + t[i + 1:]
    return t


def _normalize_casing(result: dict) -> dict:
    """Apply consistent sentence-case to the user-facing text fields so the UI
    does not show a mix of lowercase and proper-case statements."""
    # simple string fields
    for k in ("headline_hook", "verdict"):
        if isinstance(result.get(k), str):
            result[k] = _sentence_case(result[k])
    # list-of-strings fields
    for k in ("resume_strengths", "fit_reasons", "gap_reasons", "missing_skills",
              "critical_keywords_missing", "recruiter_red_flags"):
        if isinstance(result.get(k), list):
            # keywords/skills are tokens (keep as-is); the rest are sentences
            if k in ("critical_keywords_missing", "missing_skills"):
                continue
            result[k] = [_sentence_case(x) if isinstance(x, str) else x for x in result[k]]
    # list-of-dicts with text sub-fields
    for c in result.get("requirement_checks", []) or []:
        if isinstance(c, dict):
            for f in ("requirement", "required", "found", "reasoning"):
                if isinstance(c.get(f), str):
                    c[f] = _sentence_case(c[f])
    for p in result.get("improvement_plan", []) or []:
        if isinstance(p, dict):
            for f in ("action", "closes_gap", "detail"):
                if isinstance(p.get(f), str):
                    p[f] = _sentence_case(p[f])
    for s in result.get("resume_suggestions", []) or []:
        if isinstance(s, dict):
            for f in ("before", "after", "metric_prompt", "skill"):
                if isinstance(s.get(f), str):
                    s[f] = _sentence_case(s[f])
    rec = result.get("apply_recommendation")
    if isinstance(rec, dict):
        for f in ("reasoning", "next_step"):
            if isinstance(rec.get(f), str):
                rec[f] = _sentence_case(rec[f])
    return result


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
                          model=INTERVIEW_MODEL, temperature=0.3, seed=None):
    """Chat-completions JSON-mode call. Records usage into the meter.
    Retries once on 429 after a short backoff."""
    import asyncio as _asyncio
    import time as _time
    _t0 = _time.perf_counter()
    _body = {
        "model": model,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "user", "content": content}],
    }
    if is_reasoning_model(model):
        # GPT-5 / reasoning models: max_completion_tokens (not max_tokens), no
        # temperature/seed, a bounded reasoning_effort, and token headroom so the
        # JSON answer isn't truncated by the model's thinking.
        _body["max_completion_tokens"] = output_budget(model, max_tokens)
        _body["reasoning_effort"] = effort_or_default()
    else:
        _body["max_tokens"] = max_tokens
        _body["temperature"] = temperature
        if seed is not None:
            _body["seed"] = seed

    for attempt in range(2):
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}",
                     "Content-Type": "application/json"},
            json=_body,
        )
        if resp.status_code == 401:
            raise Exception("OpenAI authentication failed")
        if resp.status_code == 429:
            if attempt == 0:
                retry_after = int(resp.headers.get("retry-after", "5"))
                wait = min(retry_after, 10)  # cap at 10s so we don't stall too long
                logger.warning(f"OpenAI 429 on {label}, retrying in {wait}s")
                await _asyncio.sleep(wait)
                continue
            raise Exception("RATE_LIMITED")
        if not resp.is_success:
            raise Exception(f"AI service error {resp.status_code} ({label}): {resp.text[:300]}")

        data = resp.json()
        in_tok, out_tok = extract_usage(data)
        meter.record(label, model, in_tok, out_tok,
                     duration_ms=int((_time.perf_counter() - _t0) * 1000))

        choice = (data.get("choices") or [{}])[0]
        if choice.get("finish_reason") == "length":
            raise Exception(f"AI response truncated ({label}): exceeded {max_tokens} tokens.")
        raw = (choice.get("message") or {}).get("content", "") or ""
        return _strip_to_json(raw)


async def _call_responses_json(client, content_blocks, max_tokens, label, meter: UsageMeter,
                               model=ANALYSIS_MODEL, temperature=0.3,
                               used_web_search=False, seed=None):
    """
    Responses-API call. Retries once on 429. Records usage. Returns parsed JSON.
    """
    import asyncio as _asyncio
    import time as _time
    _t0 = _time.perf_counter()
    payload = {
        "model": model,
        "max_output_tokens": max_tokens,
        "text": {"format": {"type": "json_object"}},
        "input": [{"role": "user", "content": content_blocks}],
    }
    if is_reasoning_model(model):
        # GPT-5 / reasoning models: no temperature/top_p; bounded reasoning effort;
        # token headroom so reasoning doesn't truncate the JSON answer.
        payload["max_output_tokens"] = output_budget(model, max_tokens)
        payload["reasoning"] = {"effort": effort_or_default()}
    else:
        payload["temperature"] = temperature
        payload["top_p"] = 1

    for attempt in range(2):
        resp = await client.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}",
                     "Content-Type": "application/json"},
            json=payload,
        )
        if resp.status_code == 401:
            raise Exception("OpenAI authentication failed")
        if resp.status_code == 429:
            if attempt == 0:
                retry_after = int(resp.headers.get("retry-after", "5"))
                wait = min(retry_after, 15)
                logger.warning(f"OpenAI 429 on {label} (Responses API), retrying in {wait}s")
                await _asyncio.sleep(wait)
                continue
            raise Exception("RATE_LIMITED")
        if not resp.is_success:
            raise Exception(f"AI service error {resp.status_code} ({label}): {resp.text[:400]}")

        data = resp.json()
        in_tok, out_tok = extract_usage(data)
        meter.record(label, model, in_tok, out_tok, used_web_search=used_web_search,
                     duration_ms=int((_time.perf_counter() - _t0) * 1000))

        status = data.get("status") or ""
        incomplete = data.get("incomplete_details") or {}
        if status == "incomplete" or incomplete.get("reason") == "max_output_tokens":
            logger.warning("Responses API output truncated (%s) for label=%s; JSON may be partial",
                           incomplete.get("reason") or status, label)

        text_parts = []
        for block in data.get("output", []):
            if block.get("type") == "message":
                for part in block.get("content", []):
                    if part.get("type") in ("output_text", "text"):
                        text_parts.append(part.get("text", ""))
        raw = "\n".join(text_parts).strip()
        if not raw and data.get("output_text"):
            raw = data["output_text"]
        parsed = _strip_to_json(raw)
        if not parsed or (isinstance(parsed, dict) and not parsed):
            status_diag = None
            try:
                status_diag = data.get("status") or (data.get("output", [{}])[0].get("status"))
            except Exception:
                pass
            logger.error(f"DIAG {label}: empty/[]-parse. status={status_diag} "
                         f"raw_len={len(raw)} raw_head={raw[:300]!r} "
                         f"data_keys={list(data.keys())[:12]}")
        return parsed


def _render_pdf_to_image_blocks(resume_b64: str) -> list:
    """
    Fallback path: rasterise each PDF page to PNG and return Responses image
    blocks. Used only if the input_file path 400s. Requires pymupdf (fitz).
    Returns [] if rendering is unavailable so the caller degrades gracefully.
    """
    try:
        import fitz  # PyMuPDF
    except Exception:
        logger.warning("PyMuPDF not installed - image fallback unavailable")
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
# Grounding filter - loosened to PRIMARY-token match
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
1. "{company} {title} interview process" - what rounds, what format
2. "{company} interview questions" - actual questions reported
3. "{company} interview tips Glassdoor / Blind / Reddit" - what candidates say

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
Do NOT copy any example text, sample phrasing, or placeholder figures from THESE
INSTRUCTIONS into your output. Examples here teach FORMAT only; the CONTENT must come
entirely from the actual JD and resume. If an example mentions a tool, number, or phrase
that is not in this JD or resume, it must NOT appear in your output.
Every claim, gap, and skill must trace to a specific line in the JD or resume.
If you cannot point to where it came from, omit it.

The "Skills detected" and "Experience required" lines are naive automated
scans - HINTS ONLY and often wrong. The authoritative source is the full JD text below.
Re-read it and decide the real requirements yourself.

IGNORE any experience figure that is not stated as a hiring requirement. Only treat a
number as required experience if the JD explicitly asks the CANDIDATE to have N years.

CRITICAL - IGNORE COMPANY MARKETING. Job postings often contain large blocks of
company-culture, "What We Offer", benefits, perks, mission, and brand-slogan text
that are NOT job requirements. Extract requirements ONLY from sections describing
the role itself: responsibilities, "what you'll do", "what we're looking for",
qualifications, required skills, and nice-to-haves. NEVER treat company values,
product names, brand taglines, benefits, or culture language as requirements. If a
phrase describes the company or its perks rather than what the candidate must do or
have, exclude it entirely.

═══ JOB ═══
Title: {title}
Company: {company}
Location: {location}
Experience required: {experience}
Skills detected in JD (hint only, may be empty): {skills}

Full Job Description:
{description}

════════════════════════════════════════
STEP 0 - EXTRACT JD REQUIREMENTS FIRST
════════════════════════════════════════
Before scoring, extract every concrete requirement from the JD (hard skills, tools,
years of experience, qualifications, domain). This list is your ONLY allowed
vocabulary for gaps and missing skills, output as "jd_requirements".

════════════════════════════════════════
STEP 0.5 - EVALUATE EACH REQUIREMENT LIKE A STRICT HUMAN REVIEWER
════════════════════════════════════════
Evaluate this resume against this job the way a demanding hiring manager or a teacher
grading a paper would: read everything, judge each requirement on its real merits, and
let the score be EARNED by the evidence. There are no default or baseline scores. A
fresher applying to a senior role genuinely scores near zero on experience; a candidate
who meets four of five years scores high. The number must reflect reality, not a formula.

CRITICAL: Only output requirements that are LITERALLY stated in this JD. Never invent a
number of years the JD does not state. If the JD says "strong experience in X" with no
number, the requirement is "experience in X", NOT "5 years of X". Do not import example
figures from these instructions into your output. Read THIS job's actual words.

For EVERY requirement, output a row in "requirement_checks":
{{"requirement": "<the requirement, copied/paraphrased from THIS JD only>",
  "required": "<what THIS JD actually asks for>",
  "found": "<what the resume actually provides, or 'not present'>",
  "status": "met|partial|not_met",
  "score": <0-100, how fully THIS requirement is satisfied, judged genuinely>,
  "reasoning": "<one sentence justifying that exact score from the evidence>"}}

When a requirement DOES state a number of years in a specific area, evaluate it explicitly:
1. List the candidate's roles and attribute a duration to each (from the resume dates).
2. Decide which roles genuinely count toward the specific area the requirement names. A
   role counts only if its actual work matches that area, not merely employment.
3. Sum ONLY the qualifying years and judge on that sum, never on total tenure.

Handle "or a related role" / "or similar" wording CAREFULLY. It does NOT mean "any job
counts"; it means closely-adjacent roles in the same domain may be partially counted,
weighted by how related each role genuinely is. Do not let "or related" become an escape
hatch that credits total tenure.

Illustration of the METHOD (do not copy these words or numbers into your output): if some
JD asked for "N years in <area> or related" and a resume showed fewer qualifying years
than N once unrelated roles are excluded, that is a partial or not-met requirement scored
on the real shortfall, with the "found" field showing the year attribution. Apply this
method only to requirements THIS JD actually states.

A candidate with zero relevant experience for a senior role scores near zero on
experience, earned by the evidence, never a floor value.

════════════════════════════════════════
SCORING - the sub-scores are your honest summary of STEP 0.5
════════════════════════════════════════
Each sub-score below is a genuine judgment that MUST be consistent with, and explained by,
the requirement_checks above. The "evidence" string for each is your transparent reason
for that exact number. Do not anchor to round numbers; use the precise value the evidence
supports (a 2-of-5-years case is not "50", it is whatever the genuine shortfall warrants).

  skills_match        - how well the resume covers the required skills/tools
  experience_match    - genuine match on the SPECIFIC experience asked (not total years)
  domain_match        - how close the candidate's field/industry is to the role's
  qualifications_match- degrees, certifications, licences the JD requires
  soft_skills_match   - communication/leadership/collaboration evidence for this role

The bands below are orientation for your judgment, NOT fixed buckets. Interpolate to the
exact number the evidence justifies:
  ~90-100 fully meets or exceeds · ~70-85 strong with minor gaps · ~45-65 partial, real
  shortfalls · ~20-40 largely unmet · ~0-15 essentially absent.

Each sub-score needs a matching "<dim>_evidence" sentence that makes the number defensible.
A sub-score of exactly 0 is reserved for ONE case only: no resume is attached. When a
resume IS present, never score a dimension 0. Even a wrong-field candidate has SOME
transferable signal (communication, stakeholder work, analytical ability), so a genuine
cross-domain mismatch scores LOW (around 5-25 on the unmet dimensions), not 0. Score the
poor fit honestly and low; do not collapse everything to zero. If no resume is attached:
all five = 0.

The final % is a weighted sum computed in Python from these five:
  Final % = skills_match×0.35 + experience_match×0.25 + domain_match×0.20
          + qualifications_match×0.10 + soft_skills_match×0.10

════════════════════════════════════════
INTERCONNECTION - every section maps to the one above it
════════════════════════════════════════
ANTI-HALLUCINATION: never mention a skill/tool not in the JD text word-for-word or by
clear synonym. Before writing any gap, ask "is this in the JD I was given?" If no → omit.

1. gap_reasons: "JD requires [exact JD phrase]. Resume has no evidence of this".
2. missing_skills: each MUST be named in the JD. Empty [] if candidate has everything.
3. Each gap → a resume_suggestion with a concrete fix (gap_addressed names the gap).
4. Each improvement_plan item closes a specific gap (closes_gap names it).
5. resume_suggestions = change the resume NOW; improvement_plan = acquire skills OVER TIME.
6. Any resume_strength you list must also appear in fit_reasons or verdict. (This
   does NOT mean you must produce strengths: if there are no role-relevant strengths,
   resume_strengths is empty and that is correct.)
7. HARD RULE on strengths. A resume_strength is valid ONLY if it directly matches a
   specific requirement, skill, or responsibility named in THIS JD, with resume
   evidence. Before listing any strength, check it against the JD's stated
   requirements; if it does not map to one, DO NOT list it. Skills from a different
   field than this role are NOT strengths here, no matter how senior or impressive:
   e.g. for a Business Process Management / process-design / HR / sales role, a data
   engineering / software / ML background is NOT a strength and must be omitted,
   never reframed as "transferable", "aligns with", or "demonstrates ability to learn".
   YEARS OF EXPERIENCE ARE NOT A STRENGTH BY THEMSELVES. "X years total" only counts
   if those years are in the SPECIFIC field the JD asks for. Never write a strength
   like "has 10 years, meeting the 5+ requirement" when the candidate's years are in
   a different field than the role: total tenure in an unrelated field does NOT meet
   an in-field years requirement, and claiming it does is a false strength. Apply the
   same qualifying-years logic used for scoring: only in-domain years count.
   If the candidate has no genuine role-relevant strengths, return an EMPTY
   resume_strengths list. An empty list is the correct, honest answer for a poor-fit
   resume; never invent or stretch a strength to avoid an empty list. A poor-fit
   resume shows few or zero strengths and many gaps.

Respond ONLY with valid JSON (no markdown):

{{
  "jd_requirements": ["<each concrete requirement from the JD>"],
  "requirement_checks": [
    {{"requirement": "<a requirement stated in THIS JD>",
      "required": "<what THIS JD actually asks for>",
      "found": "<what the resume actually provides, or 'not present'>",
      "status": "<met|partial|not_met>",
      "score": <0-100, how fully this single requirement is satisfied>,
      "reasoning": "<one sentence justifying that exact score from the evidence>"}}
  ],
  "score_breakdown": {{
    "skills_match": <0-100>, "experience_match": <0-100>, "domain_match": <0-100>,
    "qualifications_match": <0-100>, "soft_skills_match": <0-100>,
    "skills_evidence": "<1 sentence>", "experience_evidence": "<1 sentence>",
    "domain_evidence": "<1 sentence>", "qualifications_evidence": "<1 sentence>",
    "soft_skills_evidence": "<1 sentence>"
  }},
  "ats_assessment": {{
    "ats_score": <0-100>,
    "keyword_match_pct": <0-100>,
    "title_alignment_pct": <0-100>,
    "must_have_coverage_pct": <0-100>,
    "formatting_parseability_pct": <0-100>,
    "reasoning": "<2-3 sentences: how a generic ATS would parse and score this resume against this JD, and why. This is an ESTIMATE of ATS-style behaviour, not any specific named platform.>"
  }},
  "critical_keywords_missing": ["<up to 12 exact keywords/phrases from the JD that the resume lacks and that an ATS would filter on, the ones the candidate is 'invisible' without>"],
  "recruiter_red_flags": ["<up to 3 things a recruiter would notice negatively about THIS resume for THIS role, only if genuinely present; omit if none apply, never invent one>"],
  "verdict": "<2-3 sentences citing role, company, evidence>",
  "headline_hook": "<ONE punchy sentence (max 22 words) grounded in THIS resume vs THIS job, creating curiosity or stakes. Never generic, never copied from these instructions.>",
  "resume_strengths": ["<a specific strength of THIS resume relevant to THIS role, in plain prose>"],
  "fit_reasons": ["<a concrete reason this candidate fits, naming the real JD requirement and the real resume evidence, in plain prose (not a template)>"],
  "gap_reasons": ["<a real gap: name the actual JD requirement and state what the resume lacks, in plain prose (not a template)>"],
  "missing_skills": [
    {{"skill": "<must appear in JD>", "importance": "<critical|important|nice-to-have>",
      "how_to_learn": "<specific course/resource/project>"}}
  ],
  "improvement_plan": [
    {{"action": "<closes a named gap>", "closes_gap": "<which gap>",
      "impact": "<high|medium>", "timeframe": "<e.g. 2 weeks>"}}
  ],
  "resume_suggestions": [
    {{"gap_addressed": "<which gap>",
      "before": "<the candidate's actual weak bullet, quoted from the resume>",
      "after": "<rewrite using Google XYZ: 'Accomplished [X] as measured by [Y], by doing [Z]'>",
      "missing_metric": <true if the original had no number and the 'after' needs one the candidate must supply; else false>,
      "metric_prompt": "<if missing_metric true: tell the candidate exactly what real number to insert, e.g. 'add the % latency reduction'; else empty>"}}
  ],
  "apply_recommendation": {{
    "verdict": "<Apply Now|Apply With Prep|Improve First|Skip - NOTE: this label is re-derived from the final score in code, so make your reasoning consistent with the actual fit; do not claim 'Apply Now' for a weak match>",
    "reasoning": "<1-2 sentences that explain the recommendation in line with the evidence>", "next_step": "<single most important action>"
  }}
}}

XYZ RESUME FIXES (resume_suggestions): rewrite the candidate's WEAKEST bullets using
Google's XYZ formula - "Accomplished [X] as measured by [Y], by doing [Z]". Aim for 3
to 5 suggestions when the resume has multiple improvable bullets; return fewer only if
the resume genuinely has very few weak points. Rules:
- "before" must be the candidate's REAL bullet, quoted from the resume. Never fabricate.
- NEVER invent numbers. If the real bullet has no metric, set missing_metric=true and
  use metric_prompt to ask the candidate for the real figure; leave a clear [ADD METRIC]
  placeholder in "after" rather than making one up.
- The rewrite should sound like a sharper version of the candidate's own wording, not
  generic AI phrasing.

ATS ASSESSMENT: produce ONE honest ATS-style score with transparent sub-metrics. This
estimates how a generic applicant-tracking system would parse and rank this resume for
this JD. Do NOT claim to be any specific named product (Workday, Taleo, etc.) - it is a
general ATS-style estimate.

COMPLETENESS AND CONSISTENCY: for EVERY list in the output (missing_skills,
improvement_plan, resume_suggestions, critical_keywords_missing, fit_reasons,
gap_reasons, requirement_checks, recruiter_red_flags), include EVERY item that genuinely
applies to this resume and JD. Do not pad with weak or invented items, and do not
arbitrarily stop early. Each list's length must be driven only by how many real items
exist, so the same resume and JD always yield the same items, the same count, and the
same order across runs. List items in order of importance (most important first). The
following are MAXIMUMS, not targets: missing_skills ≤12, improvement_plan ≤10,
resume_suggestions ≤10, critical_keywords_missing ≤12, recruiter_red_flags ≤3.
requirement_checks must cover EVERY distinct requirement stated in the JD (no cap).
"""

INTERVIEW_PROMPT = """\
You are Applyin's interview coach. Produce a crack-the-interview guide for this exact
role and company, as JSON only.

Role: {title} at {company}
Key requirements from the JD: {requirements}

═══ COMPANY INTERVIEW RESEARCH ═══
{interview_research}

Use the research above. If research found nothing useful, infer from the role type and
say so in company_style.

THIS TOOL SERVES EVERY DOMAIN, not just technology. The role may be in hospitality, retail,
BPO/customer support, healthcare, finance, skilled trades, education, logistics, sales,
administration, or anything else. Adapt the questions to what THIS role actually involves:
- For a software role, role_specific questions are technical (systems, code, tools).
- For a hotel/hospitality role, they are about guest service, operations, handling
  difficult situations, standards.
- For a BPO/support role, they are about customer handling, process adherence, metrics,
  de-escalation.
- For a sales role, they are about pipeline, objection handling, targets.
Never force technical/coding content onto a non-technical role. Every role_specific
question MUST derive from a skill or responsibility that literally appears in THIS job
description. Different jobs must produce different questions.

The assessment_strategy describes whatever practical evaluation THIS role uses: a coding
round for engineers, a role-play or scenario for service/sales, a case study for analysts,
a practical task or shift trial for trades/hospitality. Describe what fits THIS role; if
the role has no such round, say so.

Respond ONLY with valid JSON (no markdown):

{{
  "company_style": "<2-3 sentences, grounded in research>",
  "research_source": "<'Glassdoor', 'Reddit', or 'inferred from JD - no data found'>",
  "role_specific": [
    {{"question": "<tied to a real responsibility/skill in THIS JD, technical only if the role is technical>", "why_asked": "<what it tests>",
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
  "assessment_strategy": {{
    "round_type": "<what kind of practical round this role uses, e.g. coding round, role-play, case study, practical task, or 'none typical'>",
    "overview": "<how that round works for THIS role>",
    "step_by_step": ["<step 1>","<step 2>","<step 3>","<step 4>","<step 5>"],
    "when_stuck": "<exactly what to say/do>",
    "mistakes_to_avoid": ["<mistake>","<mistake>","<mistake>"]
  }},
  "preparation_checklist": [
    {{"topic": "<topic>", "why": "<why it'll come up>",
      "resource": "<specific resource>", "time_needed": "<e.g. 3 hours>"}}
  ]
}}

LIMITS (guidance, driven by what the role genuinely warrants, not padding):
role_specific up to 5, behavioural up to 4, company_specific up to 3,
preparation_checklist 4-6 items. Produce as many as are genuinely useful for THIS
role; do not invent filler to hit a number.
"""


async def research_company_interview(client, company, title, meter: UsageMeter) -> str:
    prompt = RESEARCH_PROMPT.format(company=company, title=title)
    import time as _time
    _t0 = _time.perf_counter()
    _payload = {"model": RESEARCH_MODEL,
                "tools": [{"type": "web_search_preview"}],
                "input": prompt}
    if is_reasoning_model(RESEARCH_MODEL):
        # GPT-5 / reasoning research model: bound the thinking. Non-reasoning
        # models (gpt-4o) get the exact same payload as before - no change.
        _payload["reasoning"] = {"effort": effort_or_default()}
    try:
        resp = await client.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}",
                     "Content-Type": "application/json"},
            json=_payload,
            timeout=45,
        )
        if not resp.is_success:
            return ""
        data = resp.json()
        in_tok, out_tok = extract_usage(data)
        meter.record("research", RESEARCH_MODEL, in_tok, out_tok, used_web_search=True,
                     duration_ms=int((_time.perf_counter() - _t0) * 1000))
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
# Main entry - returns (result_dict, meter, diagnostics)
# ═════════════════════════════════════════════════════════════════════════════
async def run_analysis(job_data: dict, resume_b64: str | None, req_id: str = ""):
    import hashlib
    company = job_data.get("company", "")
    title   = job_data.get("title", "")
    jd_text = (job_data.get("description", "") or "")
    # Stable seed from the job + resume so the same input samples identically every
    # run (paired with temperature 0). Different resume or JD => different seed.
    _seed_src = (title + "|" + company + "|" + jd_text[:20000] + "|" + (resume_b64 or "")[:5000]).encode()
    analysis_seed = int(hashlib.sha256(_seed_src).hexdigest()[:8], 16)
    meter = UsageMeter()
    diag = {"has_resume": True, "resume_path": "none", "all_zero_scores": False,
            "gate": None}

    def _stage(name: str, detail: str = ""):
        # One-line execution trail: each of the 4 stages logs 'start' then 'ok'.
        # If an analysis fails, the LAST 'start' with no matching 'ok' is exactly
        # where it broke - so the failure point is obvious in the logs.
        logger.info("req=%s STAGE %-11s %s", req_id, name, detail)

    async with httpx.AsyncClient(timeout=180) as client:  # increased: 6000 output tokens + retry headroom

        # ══ STAGE 1/4: resume_gate - runs BEFORE any analysis call / charge. ══
        _stage("resume_gate", "start")
        gate = await run_resume_gate(client, resume_b64, meter)
        diag["gate"] = gate
        resume_text = gate.get("resume_text", "")  # used later for keyword verification
        if not gate["valid"]:
            # No analysis call is made. Raise so the router rejects + refunds
            # (in practice the router checks the gate before charging - see below).
            _stage("resume_gate", f"REJECT reason={gate['reason']}")
            raise ResumeRejected(gate["reason"], gate)
        _stage("resume_gate", f"ok words={gate.get('word_count', 0)} pages={gate.get('pages_parsed', 0)}")

        analysis_prompt = ANALYSIS_PROMPT.format(
            title=title, company=company,
            location=job_data.get("location", "Not specified"),
            experience=job_data.get("experience", "Not specified"),
            skills=", ".join(job_data.get("skills", [])) or "See JD",
            description=jd_text[:40000],  # increased from 20000 - long JDs were silently truncated
        )

        # ── CALL 1: analysis via Responses API ────────────────────────────────
        # Resume delivery strategy (in order of reliability):
        #
        # PRIMARY PATH - extracted text injection:
        #   PyMuPDF already parsed the resume during the gate check and the full
        #   text is in `resume_text`. Injecting it directly as input_text is more
        #   reliable than the input_file inline base64 path because:
        #     • The Responses API input_file field expects a pre-uploaded file_id,
        #       not inline base64. Inline base64 via file_data is undocumented and
        #       intermittently accepted - when it silently fails the model has no
        #       resume to read and produces generic, confident-looking wrong output.
        #     • Text injection is deterministic: we know exactly what the model sees.
        #     • Token cost is equivalent for a normal 1-3 page resume.
        #
        # FALLBACK - input_file for text resumes if text is suspiciously short,
        # then image rendering for scanned/image-only PDFs.
        #
        # This fixes the root cause of "analysis doesn't match my actual resume":
        # the model was guessing from the JD alone when input_file silently failed.

        # Base output token budget (from models.json). Complex JDs with many
        # requirements can exceed 4000 tokens; reasoning models get extra headroom
        # added on top (see output_budget / model_config).
        OUTPUT_TOKENS = token_budget("analysis")

        # ══ STAGE 2/4: analysis - the scoring call (fills score + 4 accordions). ══
        _stage("analysis", f"start model={ANALYSIS_MODEL} budget={OUTPUT_TOKENS}")
        if gate["reason"] == "IMAGE_ONLY_PDF":
            # Scanned PDF - no text available, must use image rendering.
            img_blocks = _render_pdf_to_image_blocks(resume_b64)
            content = img_blocks + [{"type": "input_text", "text": analysis_prompt}]
            result = await _call_responses_json(client, content, OUTPUT_TOKENS, "analysis", meter, temperature=0, seed=analysis_seed)
            diag["resume_path"] = "image_only"

        elif resume_text and len(resume_text.split()) >= 50:
            # PRIMARY PATH: inject extracted text directly - most reliable.
            # We have good text from PyMuPDF (gate confirmed ≥50 words), so we
            # don't need to re-send the binary PDF at all. The model reads the
            # resume text as a clearly labelled section of the prompt.
            text_with_resume = (
                "RESUME (extracted text - this is the candidate's actual resume):\n"
                "─────────────────────────────────────────────\n"
                + resume_text[:25000] +
                "\n─────────────────────────────────────────────\n\n"
                + analysis_prompt
            )
            content = [{"type": "input_text", "text": text_with_resume}]
            result = await _call_responses_json(client, content, OUTPUT_TOKENS, "analysis", meter, temperature=0, seed=analysis_seed)
            diag["resume_path"] = "extracted_text"

        else:
            # FALLBACK: text was thin or empty, try input_file inline base64.
            # This path is kept as a last resort for edge-case PDFs where PyMuPDF
            # extracted very little text but the gate still passed (e.g. a PDF with
            # embedded fonts that PyMuPDF partially parsed but the Responses API
            # vision model can read better).
            file_blocks = [{
                "type": "input_file",
                "filename": "resume.pdf",
                "file_data": f"data:application/pdf;base64,{resume_b64}",
            }]
            content = file_blocks + [{"type": "input_text", "text": analysis_prompt}]
            try:
                result = await _call_responses_json(client, content, OUTPUT_TOKENS, "analysis", meter, temperature=0, seed=analysis_seed)
                diag["resume_path"] = "input_file_fallback"
            except Exception as e:
                logger.warning(f"input_file fallback failed ({e}); trying image rendering")
                img_blocks = _render_pdf_to_image_blocks(resume_b64)
                if img_blocks:
                    content = img_blocks + [{"type": "input_text", "text": analysis_prompt}]
                    result = await _call_responses_json(client, content, OUTPUT_TOKENS, "analysis", meter, temperature=0, seed=analysis_seed)
                    diag["resume_path"] = "image_fallback"
                else:
                    raise
        _stage("analysis", f"ok path={diag['resume_path']}")

        # ══ STAGE 3 + 4: research (web search) -> interview guide. Sequential and
        # both NON-FATAL: a failure here still returns a complete (interview-less)
        # report, so these stages can't fail the whole analysis. ══
        import asyncio as _asyncio

        async def _do_research():
            try:
                return await research_company_interview(client, company, title, meter)
            except Exception as e:
                logger.warning(f"Research failed (non-fatal): {e}")
                return ""

        async def _do_interview(research_text: str):
            try:
                note = research_text or (
                    f"No interview research found for {company or 'this company'}. "
                    f"Infer from the role type and say so in company_style."
                )
                reqs = ", ".join(result.get("jd_requirements", [])[:20]) or "See JD"
                interview_prompt = INTERVIEW_PROMPT.format(
                    title=title, company=company, requirements=reqs,
                    interview_research=note)
                return await _call_chat_json(
                    client, interview_prompt, token_budget("interview"), "interview", meter,
                    model=INTERVIEW_MODEL, temperature=0, seed=analysis_seed)
            except Exception as e:
                logger.warning(f"Interview guide failed (non-fatal): {e}")
                return None

        # Run research first (capped so a slow web search can't stall the request),
        # then build the interview guide with whatever we got.
        _stage("research", f"start model={RESEARCH_MODEL}")
        try:
            research = await _asyncio.wait_for(_do_research(), timeout=40.0)
        except _asyncio.TimeoutError:
            logger.warning("Research timed out after 40s - continuing without it")
            research = ""
        _stage("research", f"ok chars={len(research)}")

        _stage("interview", f"start model={INTERVIEW_MODEL}")
        interview_guide = await _do_interview(research) or {
            "company_style": "", "research_source": "", "role_specific": [],
            "behavioural": [], "company_specific": [],
            "assessment_strategy": {}, "preparation_checklist": [],
        }
        _stage("interview", "ok" if interview_guide.get("role_specific") else "empty")

    result["interview_guide"] = interview_guide

    # ── Deterministic score ───────────────────────────────────────────────────
    breakdown = result.get("score_breakdown", {})
    # Keep an untouched copy of the model's raw sub-scores for diagnostics, so if the
    # all-zero backstop fires we can tell whether the model truly returned 0 or we
    # mis-read non-zero values.
    import copy as _copy
    result["score_breakdown_raw"] = _copy.deepcopy(breakdown)
    match_score = compute_weighted_score(breakdown)
    result["match_score"] = match_score
    result["score_breakdown"] = {
        dim: min(100, max(0, int(breakdown.get(dim, 0)))) for dim in SCORE_WEIGHTS
    } | {k: breakdown.get(k, "") for k in (
        "skills_evidence","experience_evidence","domain_evidence",
        "qualifications_evidence","soft_skills_evidence")}
    result["score_weights"] = SCORE_WEIGHTS
    # Transparent breakdown of how the final score was computed: each dimension's
    # sub-score, its weight, and its points contribution. The user can audit the math.
    result["score_math"] = [
        {"dimension": dim,
         "sub_score": int(result["score_breakdown"].get(dim, 0)),
         "weight_pct": int(SCORE_WEIGHTS[dim] * 100),
         "points": round(int(result["score_breakdown"].get(dim, 0)) * SCORE_WEIGHTS[dim], 1)}
        for dim in SCORE_WEIGHTS
    ]
    result["fit_level"] = "strong" if match_score >= 75 else "medium" if match_score >= 45 else "weak"
    _stage("scoring", f"ok score={match_score} fit={result['fit_level']}")

    # ── Derive the apply-recommendation LABEL from the genuine score ───────────
    # The score itself is earned by the model's evaluation (not a baseline). The
    # label below is just a transparent translation of that score into a verdict,
    # so the recommendation can never contradict the number the user sees. We keep
    # the model's reasoning/next_step text, only the verdict label is derived, and
    # we record exactly how it was derived in "derivation" for full transparency.
    if match_score >= 75:
        _verdict = "Apply Now"
    elif match_score >= 60:
        _verdict = "Apply With Prep"
    elif match_score >= 40:
        _verdict = "Improve First"
    else:
        _verdict = "Skip"
    _rec = result.get("apply_recommendation") or {}
    _rec["verdict"] = _verdict
    _rec.setdefault("reasoning", "")
    _rec.setdefault("next_step", "")
    _rec["derivation"] = (
        f"Verdict derived from overall score {match_score}/100: "
        f"75+ Apply Now, 60-74 Apply With Prep, 40-59 Improve First, under 40 Skip."
    )
    result["apply_recommendation"] = _rec

    # Backstop: resume passed the gate but the model still returned all-zero
    # sub-scores ⇒ it didn't actually read it. Do NOT return a fake result.
    if all(int(breakdown.get(d, 0)) == 0 for d in SCORE_WEIGHTS):
        diag["all_zero_scores"] = True
        # Diagnostic: log the RAW values exactly as the model returned them (before
        # any int() coercion). This distinguishes a real read-bug (model said "80"
        # but we stored 0 - e.g. wrong key path, string like "80%", nested object)
        # from a genuine model-zero (model actually returned 0). raw_breakdown is the
        # untouched score_breakdown object from the model's JSON.
        raw_bd = result.get("score_breakdown_raw", breakdown)
        logger.error("ALL SUB-SCORES ZERO despite valid gate - rejecting (no false output)")
        logger.error(f"DIAG all-zero RAW values per dim: "
                     f"{ {d: (raw_bd.get(d), type(raw_bd.get(d)).__name__) for d in SCORE_WEIGHTS} }")
        logger.error(f"DIAG all-zero: result_keys={list(result.keys())[:15]} "
                     f"breakdown_present={'score_breakdown' in result} "
                     f"breakdown_keys={list(breakdown.keys())[:10]} "
                     f"breakdown_sample={ {d: breakdown.get(d) for d in SCORE_WEIGHTS} }")
        raise ResumeRejected("ANALYSIS_DEGENERATE", diag["gate"])

    # Past the backstop: scores are valid. Drop the raw diagnostic copy so it does
    # not bloat the response or the cached payload.
    result.pop("score_breakdown_raw", None)

    result.setdefault("jd_requirements", [])
    result.setdefault("requirement_checks", [])
    result.setdefault("headline_hook", "")
    result.setdefault("resume_strengths", [])
    result.setdefault("fit_reasons", [])
    result.setdefault("gap_reasons", [])
    result["missing_skills"]     = result.get("missing_skills", [])[:12]
    result["improvement_plan"]   = result.get("improvement_plan", [])[:10]
    result["resume_suggestions"] = result.get("resume_suggestions", [])[:10]

    # Deterministic keyword verification: the model proposes keywords it thinks are
    # missing; we KEEP one only if it genuinely does not appear in the resume text.
    # This removes false positives (model says missing but it is actually present)
    # so the chips shown to the user are provable, not guessed.
    _resume_lc = (resume_text or "").lower()
    _proposed_kw = result.get("critical_keywords_missing", []) or []
    if _resume_lc:
        _verified = []
        for k in _proposed_kw:
            ks = str(k).strip()
            if ks and ks.lower() not in _resume_lc:
                _verified.append(ks)
        result["critical_keywords_missing"] = _verified[:12]
        result["keywords_verified"] = True
    else:
        # No resume text (image-only PDF): cannot verify, fall back to model list.
        result["critical_keywords_missing"] = _proposed_kw[:12]
        result["keywords_verified"] = False

    result["recruiter_red_flags"]       = result.get("recruiter_red_flags", [])[:3]

    # ── Verify "met" requirements against the REAL resume text ────────────────
    # The model can mark a requirement "met" with hollow OR fabricated evidence
    # (plausible-sounding "found" text not actually in the resume). We hold each
    # "met" to two deterministic tests against the actual resume text:
    #   1. The "found" field must be non-empty / not a "not present" placeholder.
    #   2. The "found" text must be GROUNDED in the resume: enough of its meaningful
    #      words must actually appear in the resume text. If the model invented the
    #      evidence, its words won't be in the resume, so it fails and we downgrade.
    # This turns "met" from a model claim into something provable. Deterministic;
    # only runs when we have resume text (text-based PDF).
    import re as _re
    _empty_found = {"", "not present", "none", "n/a", "na", "not found",
                    "not mentioned", "not specified", "not stated", "not evident",
                    "no evidence", "-", "."}
    _STOP = {"the","a","an","and","or","of","to","in","for","with","on","at","by",
             "is","are","as","that","this","candidate","experience","resume","has",
             "have","their","they","role","work","working","including","such","from"}
    _resume_words = set(_re.findall(r"[a-z0-9]+", (resume_text or "").lower()))
    _checks = result.get("requirement_checks", []) or []
    _downgraded = 0
    for c in _checks:
        if not isinstance(c, dict):
            continue
        if str(c.get("status", "")).lower() != "met":
            continue
        found_raw = str(c.get("found", "")).strip()
        found = found_raw.lower().rstrip(".")
        req_text = str(c.get("requirement", "")).lower() + " " + str(c.get("required", "")).lower()
        reason = ""
        # Years-of-experience requirements need SPECIAL handling. The JD asks for N
        # years IN A SPECIFIC FIELD. A data-engineering resume contains the words
        # "years"/"experience", so the grounding check alone would pass it. So for a
        # years requirement, also require that the JD's domain terms appear in the
        # evidence: if the requirement names a field (e.g. "business process",
        # "consulting", "analytics") and none of those domain words are in the
        # candidate's evidence, the years are in the wrong field -> not met.
        _is_years_req = bool(_re.search(r"\d+\s*\+?\s*year", req_text)
                             or "years of experience" in req_text)
        if _is_years_req:
            # domain terms = meaningful requirement words minus generic experience words
            _yrs_stop = _STOP | {"year","years","experience","minimum","least","plus",
                                 "relevant","related","field","similar","or","over"}
            domain_terms = [w for w in _re.findall(r"[a-z0-9]+", req_text)
                            if w not in _yrs_stop and len(w) > 2 and not w.isdigit()]
            if domain_terms:
                ev_lc = found
                # require the evidence itself to name at least one domain term
                ev_hit = sum(1 for w in domain_terms if w in ev_lc)
                if ev_hit == 0:
                    reason = "years are not in the field this role requires"

        if not reason and (found in _empty_found or len(found) < 3):
            reason = "no concrete resume evidence shown"
        elif not reason and _resume_words:
            # Grounding check: meaningful words of the claimed evidence that actually
            # appear in the resume. If too few do, the evidence is likely fabricated.
            ev_words = [w for w in _re.findall(r"[a-z0-9]+", found)
                        if w not in _STOP and len(w) > 2]
            if ev_words:
                present = sum(1 for w in ev_words if w in _resume_words)
                ratio = present / len(ev_words)
                if ratio < 0.5:
                    reason = "claimed evidence not found in resume"
        if reason:
            c["status"] = "partial"
            c["score"] = min(int(c.get("score", 50) or 50), 55)
            c["reasoning"] = (str(c.get("reasoning", "")).strip()
                              + f" (downgraded: {reason})").strip()
            _downgraded += 1
    if _downgraded:
        logger.info(f"requirement_checks: downgraded {_downgraded} weakly-evidenced 'met' to 'partial'")
    result["requirement_checks"] = _checks
    result["requirements_verified"] = bool(resume_text)

    # ATS fallback: if the model did not return an ats_assessment, do NOT fabricate
    # zeros (which would show a misleading 0% ATS that varies job to job). Mark it
    # unavailable and fall back to the genuine match_score, so the UI can show "not
    # available" rather than a fake 0.
    if not result.get("ats_assessment"):
        result["ats_assessment"] = {
            "ats_score": result.get("match_score", 0),
            "keyword_match_pct": None, "title_alignment_pct": None,
            "must_have_coverage_pct": None, "formatting_parseability_pct": None,
            "reasoning": "", "available": False,
        }
    # NOTE: apply_recommendation is always derived from the score above, so no
    # hardcoded verdict default is needed here (a default like "Apply With Prep"
    # would risk contradicting the genuine score if it ever fired).

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
    # Consistent sentence-case across user-facing text (fixes mixed casing in UI).
    result = _normalize_casing(result)
    return result, meter, diag
