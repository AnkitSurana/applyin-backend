import httpx
import json
from app.config import settings

# ─────────────────────────────────────────────────────────────────────────────
# SCORING DIMENSIONS
# The final match_score is a WEIGHTED SUM of five sub-scores.
# The AI must fill each sub-score; we compute the total here in Python,
# so the result is fully deterministic and auditable.
#
# Dimension               Weight   What it measures
# ─────────────────────── ──────   ──────────────────────────────────────────
# skills_match              35 %   Technical/hard skills overlap (JD vs resume)
# experience_match          25 %   Years & seniority alignment
# domain_match              20 %   Industry / domain knowledge fit
# qualifications_match      10 %   Degrees, certs, mandatory credentials
# soft_skills_match         10 %   Leadership, comms, culture signals
# ─────────────────────── ──────
# TOTAL                    100 %
# ─────────────────────────────────────────────────────────────────────────────

SCORE_WEIGHTS = {
    "skills_match":         0.35,
    "experience_match":     0.25,
    "domain_match":         0.20,
    "qualifications_match": 0.10,
    "soft_skills_match":    0.10,
}

def compute_weighted_score(dimensions: dict) -> int:
    """
    Compute the final match_score as a weighted sum of the five dimension scores.
    Each dimension score must be 0-100; missing dimensions default to 0.
    Returns an integer 0-100.
    """
    total = sum(
        dimensions.get(dim, 0) * weight
        for dim, weight in SCORE_WEIGHTS.items()
    )
    return min(100, max(0, round(total)))


# ─────────────────────────────────────────────────────────────────────────────
# STEP 1 — Company interview research prompt
# Uses OpenAI's web_search_preview tool to look up real interview intelligence.
# Returns a plain-text research summary that is injected into the main prompt.
# ─────────────────────────────────────────────────────────────────────────────
RESEARCH_PROMPT = """\
You are a job search researcher. Look up real, current information about how \
{company} interviews candidates for {title} roles.

Search for:
1. "{company} {title} interview process" — what rounds, what format
2. "{company} interview questions software engineer" (or relevant role) — actual questions reported
3. "{company} interview tips Glassdoor" or Blind or Reddit — what candidates say

Then write a concise research brief (200–300 words) covering:
- Number of interview rounds and their format (phone screen, coding, system design, etc.)
- What technical topics are tested (DSA difficulty level, system design expectations, domain knowledge)
- Behavioural/culture-fit style (Leadership Principles, Googleyness, values-based, etc.)
- Any specific tips or patterns reported by actual candidates
- Preparation resources candidates recommend

If you cannot find reliable information about this specific company, say so clearly and \
describe what a typical interview looks like for a {title} role at a company of this type \
(startup / enterprise / consulting / etc.) based on the JD context.

Be factual. Do not invent. Cite sources briefly (e.g. "per Glassdoor", "per Reddit/cscareerquestions").
"""


async def research_company_interview(client: httpx.AsyncClient, company: str, title: str) -> str:
    """
    Call GPT-4o with web search enabled to get real interview intelligence
    for the given company and role. Returns a plain-text research brief.
    Falls back to an empty string on any failure — the main prompt handles
    the no-research case gracefully.
    """
    prompt = RESEARCH_PROMPT.format(company=company, title=title)

    try:
        resp = await client.post(
            "https://api.openai.com/v1/responses",
            headers={
                "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "gpt-4o",
                "tools": [{"type": "web_search_preview"}],
                "input": prompt,
            },
            timeout=45,
        )

        if not resp.is_success:
            return ""

        data = resp.json()

        # Extract all text blocks from the response output
        text_parts = []
        for block in data.get("output", []):
            if block.get("type") == "message":
                for part in block.get("content", []):
                    if part.get("type") == "output_text":
                        text_parts.append(part.get("text", ""))

        return "\n".join(text_parts).strip()

    except Exception:
        return ""



def _grounded_in_jd(text: str, jd_text: str, jd_requirements: list) -> bool:
    """
    Returns True if the given skill/gap text plausibly traces back to the JD.
    A match means a meaningful token from `text` appears in the JD body or in
    one of the extracted jd_requirements. Prevents the model inventing skills
    (e.g. 'Azure') that never appear in the job description.
    """
    if not text:
        return False
    import re
    hay = (jd_text + " " + " ".join(jd_requirements)).lower()
    # Tokens length >= 2 (so short real skills like "go", "r", "ml" can be checked),
    # minus filler words.
    tokens = [t for t in re.findall(r"[a-zA-Z0-9+#.]{2,}", text.lower())
              if t not in _STOPWORDS]
    if not tokens:
        # Pure filler/no checkable token — for a *skill/gap* claim this is suspicious,
        # so reject rather than keep (prevents 1-char noise slipping through).
        return False
    # Every token must appear in the JD as a WHOLE WORD (word-boundary match).
    # This blocks "go" matching inside "governance", "r" inside "report",
    # "java" inside "javascript", etc. We require ALL content tokens to match so a
    # multi-word skill ("power bi") only passes if both words are present.
    for tok in tokens:
        esc = re.escape(tok)
        if not re.search(r"(?<![a-z0-9])" + esc + r"(?![a-z0-9])", hay):
            return False
    return True

_STOPWORDS = {
    "the","and","for","with","this","that","jd","requires","resume","has","have",
    "evidence","experience","role","candidate","not","any","your","you","from",
    "using","such","skills","skill","strong","years","year","preferred","required",
    "include","including","ability","work","working","based","data","engineer",
}

# ─────────────────────────────────────────────────────────────────────────────
# CALL 1 — Core analysis: scoring, fit, gaps, resume suggestions
# (NO interview guide — that is a separate call to keep output small & reliable)
# ─────────────────────────────────────────────────────────────────────────────
ANALYSIS_PROMPT = """\
You are Applyin's matching engine. Compare ONE job description against ONE resume and
produce an honest, evidence-grounded JSON report.

THE TWO SOURCES OF TRUTH:
  1. The JOB DESCRIPTION below defines what the role requires. Nothing else.
  2. The RESUME below defines what the candidate has. Nothing else.

Do NOT use outside assumptions about what "this kind of role usually needs".
Do NOT invent requirements not written in the JD.
Do NOT invent candidate experience not written in the resume.
Every claim, gap, and skill must trace to a specific line in the JD or resume.
If you cannot point to where it came from, omit it.

The "Technical skills detected" and "Experience required" lines are naive automated
scans — HINTS ONLY and often wrong. The authoritative source is the full JD text below.
Re-read it and decide the real requirements yourself.

IGNORE any experience figure that is not stated as a hiring requirement. For example,
"For more than 50 years the company has..." is company history, NOT a candidate
requirement. Only treat a number as required experience if the JD explicitly asks the
CANDIDATE to have N years (e.g. "5+ years of experience in data engineering"). If the
JD states experience only qualitatively (e.g. "significant experience"), set the
experience requirement accordingly and never invent a number.

═══ JOB ═══
Title: {title}
Company: {company}
Location: {location}
Experience required: {experience}
Technical skills detected (hint only): {skills}

Full Job Description:
{description}

═══ CANDIDATE RESUME ═══
{resume_section}

════════════════════════════════════════
STEP 0 — EXTRACT JD REQUIREMENTS FIRST
════════════════════════════════════════
Before scoring, extract every concrete requirement from the JD (hard skills, tools,
years of experience, qualifications, domain). This list is your ONLY allowed
vocabulary for gaps and missing skills, and you output it as "jd_requirements".
If a skill is not in jd_requirements, it cannot be a gap. No requirement, no gap.

════════════════════════════════════════
SCORING — weighted sum computed in Python from your 5 sub-scores
════════════════════════════════════════
  Final % = skills_match×0.35 + experience_match×0.25 + domain_match×0.20
          + qualifications_match×0.10 + soft_skills_match×0.10

Score each 0-100 using ONLY JD + resume evidence:
  skills_match: 100=every required skill shown · 75=most, minor gaps · 50=~half ·
                25=few · 0=none
  experience_match: 100=meets/exceeds years AND seniority · 75=within 1-2 yrs ·
                50=~half · 25=large gap · 0=none/no resume
  domain_match: 100=same industry/problem space · 75=adjacent transferable ·
                50=some overlap · 25=different · 0=unrelated
  qualifications_match: 100=all mandatory met · 75=most · 50=partial · 25=missing key · 0=none
  soft_skills_match: 100=strong evidence · 75=good · 50=some · 25=little · 0=none

If resume says "NO RESUME": all five = 0.
Identical inputs must produce identical scores.

════════════════════════════════════════
INTERCONNECTION — every section maps to the one above it
════════════════════════════════════════
ANTI-HALLUCINATION (most important): never mention a skill/tool that is not in the
JD text word-for-word or by clear synonym. Before writing any gap or missing skill,
ask "is this in the JD I was given?" If no → omit it.

1. gap_reasons: "JD requires [exact JD phrase] — resume has no evidence of this".
   Only for things explicitly in the JD. Fewer than 2 is fine if resume covers all.
2. missing_skills: each MUST be named in the JD. Empty [] if candidate has everything.
   Never pad with generically-useful skills not in the JD.
3. Each gap → a resume_suggestion with a concrete fix (gap_addressed names the gap).
4. Each improvement_plan item closes a specific gap (closes_gap names it). No generic advice.
5. resume_suggestions = change the resume NOW; improvement_plan = acquire skills OVER TIME.
   Never the same item in both.
6. resume_strengths must also appear in fit_reasons or verdict. No contradictions.
7. resume_strengths ≥3 and fit_reasons ≥3 when a resume is present.
   gaps/missing/suggestions/plan: only JD-grounded items, no padding.

Respond ONLY with valid JSON (no markdown):

{{
  "jd_requirements": ["<each concrete requirement copied/paraphrased from the JD>"],
  "score_breakdown": {{
    "skills_match": <0-100>, "experience_match": <0-100>, "domain_match": <0-100>,
    "qualifications_match": <0-100>, "soft_skills_match": <0-100>,
    "skills_evidence": "<1 sentence>", "experience_evidence": "<1 sentence>",
    "domain_evidence": "<1 sentence>", "qualifications_evidence": "<1 sentence>",
    "soft_skills_evidence": "<1 sentence>"
  }},
  "verdict": "<2-3 sentences: honest summary citing role, company, evidence>",
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
    {{"gap_addressed": "<which gap>", "issue": "<weak/missing for THIS role>",
      "fix": "<exact wording change>", "example": "<rewritten bullet>"}}
  ],
  "apply_recommendation": {{
    "verdict": "<Apply Now|Apply With Prep|Improve First|Skip>",
    "reasoning": "<1-2 sentences>", "next_step": "<single most important action>"
  }}
}}

LIMITS: missing_skills ≤6, improvement_plan ≤5, resume_suggestions ≤5.
"""

# ─────────────────────────────────────────────────────────────────────────────
# CALL 2 — Interview guide (separate call; text-only, no PDF, small + reliable)
# ─────────────────────────────────────────────────────────────────────────────
INTERVIEW_PROMPT = """\
You are Applyin's interview coach. Produce a crack-the-interview guide for this exact
role and company, as JSON only.

Role: {title} at {company}
Key requirements from the JD: {requirements}

═══ COMPANY INTERVIEW RESEARCH ═══
{interview_research}

Use the research above. If it names a coding style (e.g. LeetCode-hard DSA) or a
framework (e.g. Leadership Principles), reflect it. If research found nothing useful,
infer from the role type and say so in company_style.

Every technical question must tie to a skill in the JD requirements. Be specific and
practical — the candidate should be able to walk in using only this guide.

Respond ONLY with valid JSON (no markdown):

{{
  "company_style": "<2-3 sentences on this company's interview style, grounded in research>",
  "research_source": "<where intel came from: 'Glassdoor', 'Reddit', or 'inferred from JD — no data found'>",
  "technical": [
    {{"question": "<question tied to a JD skill>", "why_asked": "<what it tests>",
      "how_to_answer": "<step-by-step framework>", "example_answer_start": "<first 2 sentences>"}}
  ],
  "behavioural": [
    {{"question": "<question>", "why_asked": "<competency/principle>",
      "star_guide": "<S/T/A/R with specific hints for this question>"}}
  ],
  "company_specific": [
    {{"question": "<question this company is known to ask>", "context": "<why they ask>",
      "how_to_answer": "<key points and what to avoid>"}}
  ],
  "coding_round_strategy": {{
    "overview": "<how coding rounds work here>",
    "step_by_step": ["<step 1>","<step 2>","<step 3>","<step 4>","<step 5>"],
    "when_stuck": "<exactly what to say/do>",
    "mistakes_to_avoid": ["<mistake>","<mistake>","<mistake>"]
  }},
  "preparation_checklist": [
    {{"topic": "<topic to revise>", "why": "<why it'll come up>",
      "resource": "<specific resource>", "time_needed": "<e.g. 3 hours>"}}
  ]
}}

LIMITS: technical exactly 5, behavioural exactly 4, company_specific exactly 3,
preparation_checklist 4-6 items.
"""

# ─────────────────────────────────────────────────────────────────────────────
# OpenAI JSON-mode helper — guarantees valid JSON, detects truncation
# ─────────────────────────────────────────────────────────────────────────────
async def _call_openai_json(client, content, max_tokens, label="call"):
    """
    POST to chat/completions with JSON mode on. Raises a descriptive error if the
    response was truncated (finish_reason='length') or could not be parsed.
    Returns the parsed dict.
    """
    resp = await client.post(
        "https://api.openai.com/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": "gpt-4o",
            "max_tokens": max_tokens,
            "temperature": 0,
            "seed": 42,
            "response_format": {"type": "json_object"},   # guarantees valid JSON
            "messages": [{"role": "user", "content": content}],
        },
    )

    if resp.status_code == 401:
        raise Exception("OpenAI authentication failed")
    if resp.status_code == 429:
        raise Exception("RATE_LIMITED")
    if not resp.is_success:
        # Surface the real OpenAI error body for debugging
        body = ""
        try:
            body = resp.text[:300]
        except Exception:
            pass
        raise Exception(f"AI service error {resp.status_code} ({label}): {body}")

    data = resp.json()
    choice = (data.get("choices") or [{}])[0]
    finish = choice.get("finish_reason")
    raw = (choice.get("message") or {}).get("content", "") or ""
    raw = raw.strip()

    # If the model hit the token ceiling, the JSON is incomplete — say so clearly.
    if finish == "length":
        raise Exception(
            f"AI response truncated ({label}): output exceeded {max_tokens} tokens. "
            f"Try a shorter resume/JD."
        )

    cleaned = raw.replace("```json", "").replace("```", "").strip()
    for attempt in (cleaned, raw):
        try:
            return json.loads(attempt)
        except json.JSONDecodeError:
            pass
    # Last resort: outermost {...}
    s, e = cleaned.find("{"), cleaned.rfind("}")
    if s != -1 and e > s:
        try:
            return json.loads(cleaned[s:e + 1])
        except json.JSONDecodeError:
            pass
    raise Exception(f"AI returned malformed response ({label}). Please try again.")


async def run_analysis(job_data: dict, resume_b64: str | None) -> dict:
    has_resume = bool(resume_b64 and len(resume_b64) > 100)
    company = job_data.get("company", "")
    title   = job_data.get("title", "")
    jd_text = (job_data.get("description", "") or "")

    async with httpx.AsyncClient(timeout=150) as client:

        # ── CALL 1: Core analysis (scoring, gaps, resume) ─────────────────────
        analysis_prompt = ANALYSIS_PROMPT.format(
            title=title,
            company=company,
            location=job_data.get("location", "Not specified"),
            experience=job_data.get("experience", "Not specified"),
            skills=", ".join(job_data.get("skills", [])) or "See JD",
            description=jd_text[:8000],
            resume_section=(
                "The resume is attached as a PDF. Read it carefully."
                if has_resume else
                "NO RESUME — set all five dimension scores to 0."
            ),
        )

        if has_resume:
            analysis_content = [
                {"type": "file", "file": {
                    "filename": "resume.pdf",
                    "file_data": f"data:application/pdf;base64,{resume_b64}"}},
                {"type": "text", "text": analysis_prompt},
            ]
        else:
            analysis_content = analysis_prompt

        result = await _call_openai_json(client, analysis_content, 4000, "analysis")

        # ── CALL 2: Interview guide (text-only, uses extracted requirements) ──
        # Run research + guide. Failures here are non-fatal — the core analysis
        # is what matters; the guide degrades gracefully to empty.
        interview_guide = {}
        try:
            interview_research = await research_company_interview(client, company, title)
            research_note = (
                interview_research if interview_research else
                f"No interview research found for {company or 'this company'}. "
                f"Infer from the role type and say so in company_style."
            )
            reqs = ", ".join(result.get("jd_requirements", [])[:20]) or "See JD"
            interview_prompt = INTERVIEW_PROMPT.format(
                title=title, company=company,
                requirements=reqs, interview_research=research_note,
            )
            interview_guide = await _call_openai_json(client, interview_prompt, 3500, "interview")
        except Exception as e:
            # Keep the analysis; expose a minimal guide noting the issue.
            interview_guide = {
                "company_style": "Interview guide is temporarily unavailable — "
                                 "the fit analysis above is complete.",
                "research_source": "", "technical": [], "behavioural": [],
                "company_specific": [], "coding_round_strategy": {},
                "preparation_checklist": [],
            }

    result["interview_guide"] = interview_guide

    # ── Deterministic score (computed in Python, never trusted from model) ────
    breakdown = result.get("score_breakdown", {})
    match_score = compute_weighted_score(breakdown)
    result["match_score"] = match_score
    result["score_breakdown"] = {
        dim: min(100, max(0, int(breakdown.get(dim, 0)))) for dim in SCORE_WEIGHTS
    } | {
        k: breakdown.get(k, "") for k in (
            "skills_evidence", "experience_evidence", "domain_evidence",
            "qualifications_evidence", "soft_skills_evidence")
    }
    result["score_weights"] = SCORE_WEIGHTS
    result["fit_level"] = (
        "strong" if match_score >= 75 else
        "medium" if match_score >= 45 else "weak"
    )

    result.setdefault("jd_requirements", [])
    result.setdefault("resume_strengths", [])
    result.setdefault("fit_reasons", [])
    result.setdefault("gap_reasons", [])
    result["missing_skills"]     = result.get("missing_skills", [])[:6]
    result["improvement_plan"]   = result.get("improvement_plan", [])[:5]
    result["resume_suggestions"] = result.get("resume_suggestions", [])[:5]
    result.setdefault("apply_recommendation", {
        "verdict": "Apply With Prep", "reasoning": "", "next_step": ""})

    # ── ANTI-HALLUCINATION ENFORCEMENT ────────────────────────────────────────
    jd_reqs = result.get("jd_requirements", []) or []
    result["missing_skills"] = [
        ms for ms in result["missing_skills"]
        if _grounded_in_jd(ms.get("skill", "") if isinstance(ms, dict) else str(ms), jd_text, jd_reqs)
    ]
    def _sane_experience_gap(g: str) -> bool:
        # Reject gaps citing an implausible experience requirement (e.g. "50+ years"),
        # which usually come from scraping company history ("for 50 years...").
        import re
        m = re.search(r"(\d{1,3})\s*\+?\s*years?", g.lower())
        if m and int(m.group(1)) > 20:
            return False
        return True

    result["gap_reasons"] = [
        g for g in result["gap_reasons"]
        if _grounded_in_jd(g if isinstance(g, str) else g.get("text", ""), jd_text, jd_reqs)
        and _sane_experience_gap(g if isinstance(g, str) else g.get("text", ""))
    ]
    cleaned_suggestions = []
    for s in result["resume_suggestions"]:
        if not isinstance(s, dict):
            cleaned_suggestions.append(s); continue
        ga = s.get("gap_addressed", "")
        if not ga or _grounded_in_jd(ga, jd_text, jd_reqs):
            cleaned_suggestions.append(s)
    result["resume_suggestions"] = cleaned_suggestions
    result["improvement_plan"] = [
        p for p in result["improvement_plan"]
        if not isinstance(p, dict) or not p.get("closes_gap")
        or _grounded_in_jd(p.get("closes_gap", ""), jd_text, jd_reqs)
    ]

    return result
