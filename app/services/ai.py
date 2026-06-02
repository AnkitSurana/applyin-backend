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


# ─────────────────────────────────────────────────────────────────────────────
# STEP 2 — Main analysis prompt
# ─────────────────────────────────────────────────────────────────────────────
PROMPT_TEMPLATE = """\
You are Applyin's intelligent matching engine. Your job is to compare ONE specific job \
description against ONE specific resume and produce an honest, evidence-grounded report.

THE TWO SOURCES OF TRUTH:
  1. The JOB DESCRIPTION below — this defines what the role requires. Nothing else.
  2. The RESUME below — this defines what the candidate has. Nothing else.

You must NOT use outside assumptions about what "this kind of role usually needs."
You must NOT invent requirements that are not written in the JD.
You must NOT invent candidate experience that is not written in the resume.
Every single claim, gap, skill, and suggestion must trace back to a specific line in
either the JD or the resume. If you cannot point to where something came from, omit it.

The "Technical skills detected" line below is a NAIVE keyword scan of the JD text and may
include false positives or miss things — treat it as a hint only. The authoritative source
is the full Job Description text. Re-read it yourself and decide what the role actually requires.

═══ JOB ═══
Title: {title}
Company: {company}
Location: {location}
Experience required: {experience}
Technical skills detected: {skills}

Full Job Description:
{description}

═══ CANDIDATE RESUME ═══
{resume_section}

═══ COMPANY INTERVIEW RESEARCH ═══
{interview_research}

════════════════════════════════════════
SCORING METHODOLOGY — READ CAREFULLY
════════════════════════════════════════
The final match percentage shown to the user is NOT a single gut-feel number.
It is calculated in Python as a weighted sum of FIVE dimension scores you provide:

  Final % = (skills_match × 0.35)
           + (experience_match × 0.25)
           + (domain_match × 0.20)
           + (qualifications_match × 0.10)
           + (soft_skills_match × 0.10)

Score each dimension 0-100 using ONLY the evidence present in the JD and resume:

  skills_match (35 %):
    100 = every required/preferred technical skill explicitly demonstrated
     75 = most core skills present, 1–2 minor gaps
     50 = roughly half the required skills present
     25 = few skills match; significant technology gaps
      0 = no meaningful skill overlap

  experience_match (25 %):
    100 = years of experience meets or exceeds JD requirement AND seniority matches
     75 = within 1–2 years of requirement OR seniority one level off
     50 = roughly half the required experience OR significant seniority gap
     25 = very early career vs senior role (or vice versa)
      0 = no relevant experience at all / no resume

  domain_match (20 %):
    100 = same industry, same problem space, direct domain expertise
     75 = adjacent domain with clear transferable knowledge
     50 = some domain overlap but meaningful gaps
     25 = different industry, limited transferability
      0 = completely unrelated domain

  qualifications_match (10 %):
    100 = all mandatory qualifications/certs met or exceeded
     75 = most qualifications met; non-critical gaps
     50 = partially meets qualifications
     25 = missing key qualifications listed as required
      0 = no relevant qualifications / no resume

  soft_skills_match (10 %):
    100 = strong evidence of required soft skills (leadership, comms, collaboration, etc.)
     75 = good signals with minor gaps
     50 = some evidence but limited detail
     25 = little evidence of soft skills
      0 = no signals at all

NO RESUME RULES (if resume section says "NO RESUME"):
  skills_match: 0, experience_match: 0, domain_match: 0,
  qualifications_match: 0, soft_skills_match: 0

CONSISTENCY RULE: identical inputs must produce identical dimension scores.
════════════════════════════════════════

════════════════════════════════════════
INTERCONNECTION RULES — CRITICAL
════════════════════════════════════════
Every section must flow directly from the JD text and the resume. These rules are
ABSOLUTE and non-negotiable:

ANTI-HALLUCINATION RULE (most important):
  NEVER mention a skill, tool, technology, or requirement that does not appear
  word-for-word or by clear synonym in the JD text above.
  
  WRONG: JD requires Spark → you list "Azure" as missing (Azure is not in JD)
  WRONG: JD requires Python → you list "Kubernetes" as missing (not in JD)
  RIGHT: JD requires "AWS EMR" → if resume lacks it, you list "AWS EMR" as missing
  
  Before writing any gap_reason, missing_skill, or resume_suggestion, ask:
  "Does this specific technology/skill appear in the JD text I was given?"
  If NO → do not include it. Period.

1. gap_reasons — JD evidence required
   Format MUST be: "JD requires [exact phrase from JD] — resume has no evidence of this"
   Only include gaps for things EXPLICITLY stated in the JD.
   If the resume covers everything in the JD, gap_reasons can have fewer than 2 items.

2. missing_skills — JD-sourced only
   Each missing skill MUST be a technology/skill named in the JD.
   If the candidate has everything the JD asks for, missing_skills can be empty [].
   Never pad with generic skills that "would be useful" but aren't in the JD.

3. gap_reasons → resume_suggestions (one-to-one)
   Each gap_reason that a resume change can address MUST have a corresponding
   resume_suggestion with a concrete fix. Do not repeat gaps without adding guidance.

4. gap_reasons + missing_skills → improvement_plan
   Each plan item must address a specific gap from gap_reasons or missing_skills.
   No generic advice unless grounded in a JD requirement.

5. resume_suggestions vs improvement_plan — complementary, not duplicative
   resume_suggestions = change the resume document NOW to represent existing experience.
   improvement_plan = acquire skills OVER TIME to close real gaps.
   Never put the same item in both.

6. resume_strengths → fit_reasons (consistent)
   Strengths must also appear in fit_reasons or verdict. No contradictions.

7. Minimum arrays (only when resume present AND genuine evidence exists):
   resume_strengths: 3 minimum, fit_reasons: 3 minimum.
   gap_reasons, missing_skills, resume_suggestions, improvement_plan:
     ONLY include items backed by JD evidence — no padding to hit a minimum count.
════════════════════════════════════════

════════════════════════════════════════
INTERVIEW GUIDE — BUILT FROM REAL RESEARCH
════════════════════════════════════════
The COMPANY INTERVIEW RESEARCH section above contains real, searched data about
how {company} interviews. USE IT. Every field in interview_guide must be grounded
in that research. Do not fall back to generic advice when research is available.

If the research says "{company} uses LeetCode hard DSA questions", your technical
questions must reflect that difficulty. If it says "Leadership Principles dominate",
your behavioural section must cover the specific LPs mentioned. If the research
found specific questions candidates reported, include them.

If research returned nothing useful, fall back to inferring from the JD and role type
— but say so in company_style: "No specific interview data found for {company};
the following is based on the JD and typical patterns for this role type."

TECHNICAL QUESTIONS (generate 5):
  - Directly tied to skills in the JD, calibrated to the difficulty level found in research
  - Include the WHY (what the interviewer is really testing)
  - Include a step-by-step HOW TO ANSWER framework
  - For coding/DSA: include clarify → brute force → optimise pattern
  - For system design: requirements → capacity → high-level → deep dive

BEHAVIOURAL QUESTIONS (generate 4):
  - Map each to a specific competency the JD requires or that the research flagged
  - If this company uses named frameworks (Leadership Principles, Googleyness, etc.),
    label each question with the framework it tests
  - Provide a detailed STAR guide with SPECIFIC hints for this exact question

COMPANY-SPECIFIC QUESTIONS (generate 3):
  - Must be grounded in the research: questions this company is actually known to ask
  - If research found real reported questions, use them
  - Include how to research the company further before the interview

CODING ROUND STRATEGY:
  - Calibrated to THIS company's known coding round format from research
  - Exact steps, what to say when stuck, mistakes to avoid

PREPARATION CHECKLIST (4–6 items):
  - Topics specifically flagged by the research as likely to come up
  - Real resources candidates recommend for this company (from research)
  - Estimated time per topic
════════════════════════════════════════

════════════════════════════════════════
STEP 0 — EXTRACT JD REQUIREMENTS FIRST (internal checklist)
════════════════════════════════════════
Before scoring anything, mentally extract every concrete requirement from the JD:
hard skills, tools, years of experience, qualifications, certifications, domain.
This list is your ONLY allowed vocabulary for gaps and missing skills. You will
also output it in "jd_requirements" so the user can see the analysis is grounded.

Every item in gap_reasons and missing_skills MUST correspond to an item you put in
jd_requirements. If it is not in jd_requirements, it cannot be a gap. This is the
mechanism that prevents hallucination — no requirement, no gap.
════════════════════════════════════════

Respond ONLY with a valid JSON object — no markdown, no backticks:

{{
  "jd_requirements": [
    "<each concrete requirement copied/paraphrased from the JD — e.g. '5+ years data engineering', 'Apache Spark (PySpark)', 'SQL performance tuning', 'AWS EMR', 'mentoring junior engineers'>"
  ],
  "score_breakdown": {{
    "skills_match":         <0-100 integer>,
    "experience_match":     <0-100 integer>,
    "domain_match":         <0-100 integer>,
    "qualifications_match": <0-100 integer>,
    "soft_skills_match":    <0-100 integer>,
    "skills_evidence":         "<1 sentence: what skills were found vs required>",
    "experience_evidence":     "<1 sentence: candidate years/seniority vs JD requirement>",
    "domain_evidence":         "<1 sentence: domain/industry alignment>",
    "qualifications_evidence": "<1 sentence: credentials found vs required>",
    "soft_skills_evidence":    "<1 sentence: soft-skill signals found>"
  }},
  "fit_level": "<strong|medium|weak>",
  "verdict": "<2-3 sentences: specific honest summary citing role, company, evidence>",
  "resume_strengths": [
    "<specific strength from resume that is directly relevant to this role>"
  ],
  "fit_reasons": [
    "<JD requires X — resume demonstrates Y with evidence Z>"
  ],
  "gap_reasons": [
    "<JD requires [exact phrase from JD text] — resume has no evidence of this>"
    /* Only include if the skill/requirement is literally stated in the JD */
  ],
  "missing_skills": [
    {{
      "skill": "<name — MUST appear verbatim or by direct synonym in the JD above>",
      "importance": "<critical|important|nice-to-have>",
      "how_to_learn": "<specific course, resource, or project>"
    }}
  ],
  /* If the candidate's resume covers all JD skills, return missing_skills: [] */
  "improvement_plan": [
    {{
      "action": "<specific action that closes a named gap from gap_reasons or missing_skills>",
      "closes_gap": "<which gap_reason or missing_skill this addresses>",
      "impact": "<high|medium>",
      "timeframe": "<e.g. 2 weeks, 1 month>"
    }}
  ],
  "resume_suggestions": [
    {{
      "gap_addressed": "<which gap_reason this suggestion helps signal>",
      "issue": "<what is missing or weak in the current resume for THIS role>",
      "fix": "<exact wording change or section to add>",
      "example": "<rewritten bullet or section text>"
    }}
  ],
  "interview_guide": {{
    "company_style": "<2-3 sentences describing this company's interview style, grounded in the research above>",
    "research_source": "<brief note on where the interview intel came from: 'Glassdoor reports', 'Reddit/cscareerquestions', 'inferred from JD — no specific data found', etc.>",
    "technical": [
      {{
        "question": "<the actual question>",
        "why_asked": "<what the interviewer is testing>",
        "how_to_answer": "<step-by-step framework>",
        "example_answer_start": "<first 2 sentences of a strong answer>"
      }}
    ],
    "behavioural": [
      {{
        "question": "<the actual question>",
        "why_asked": "<which competency / leadership principle this maps to>",
        "star_guide": "<detailed S/T/A/R with specific hints for this question>"
      }}
    ],
    "company_specific": [
      {{
        "question": "<a question this company is known to ask, from research>",
        "context": "<why this company asks this and what they look for>",
        "how_to_answer": "<key points and what to avoid>"
      }}
    ],
    "coding_round_strategy": {{
      "overview": "<how coding interviews work at this company, from research>",
      "step_by_step": ["<step 1>", "<step 2>", "<step 3>", "<step 4>", "<step 5>"],
      "when_stuck": "<exactly what to say and do when you don't know the answer>",
      "mistakes_to_avoid": ["<common mistake>", "<another>", "<another>"]
    }},
    "preparation_checklist": [
      {{
        "topic": "<specific topic to revise>",
        "why": "<why this is likely to come up — tie to research findings>",
        "resource": "<specific link, book, or platform candidates recommend>",
        "time_needed": "<e.g. 3 hours, 1 week>"
      }}
    ]
  }},
  "apply_recommendation": {{
    "verdict": "<Apply Now|Apply With Prep|Improve First|Skip>",
    "reasoning": "<1-2 sentences>",
    "next_step": "<single most important action>"
  }}
}}

LIMITS:
- missing_skills: max 6
- improvement_plan: max 5, each must name the gap it closes
- resume_suggestions: max 5, each must name the gap it addresses
- interview_guide.technical: exactly 5 questions
- interview_guide.behavioural: exactly 4 questions
- interview_guide.company_specific: exactly 3 questions
- interview_guide.preparation_checklist: 4–6 items
"""



def _grounded_in_jd(text: str, jd_text: str, jd_requirements: list) -> bool:
    """
    Returns True if the given skill/gap text plausibly traces back to the JD.
    A match means a meaningful token from `text` appears in the JD body or in
    one of the extracted jd_requirements. Prevents the model inventing skills
    (e.g. 'Azure') that never appear in the job description.
    """
    if not text:
        return False
    hay = (jd_text + " " + " ".join(jd_requirements)).lower()
    # Pull alphanumeric tokens of length >= 3 (skip filler words)
    import re
    tokens = [t for t in re.findall(r"[a-zA-Z0-9+#.]{3,}", text.lower())
              if t not in _STOPWORDS]
    if not tokens:
        return True  # nothing checkable — keep it
    # Require that at least one "content" token from the claim appears in the JD
    return any(tok in hay for tok in tokens)

_STOPWORDS = {
    "the","and","for","with","this","that","jd","requires","resume","has","have",
    "evidence","experience","role","candidate","not","any","your","you","from",
    "using","such","skills","skill","strong","years","year","preferred","required",
    "include","including","ability","work","working","based","data","engineer",
}


async def run_analysis(job_data: dict, resume_b64: str | None) -> dict:
    has_resume = bool(resume_b64 and len(resume_b64) > 100)
    company = job_data.get("company", "")
    title   = job_data.get("title", "")

    async with httpx.AsyncClient(timeout=120) as client:

        # ── Step 1: Research the company's interview process ──────────────────
        interview_research = await research_company_interview(client, company, title)

        research_note = (
            interview_research
            if interview_research
            else f"No interview research available for {company or 'this company'}. "
                 f"Generate the interview guide based on the JD, role type, and industry best practices. "
                 f"Clearly state in company_style that this is inferred, not researched."
        )

        # ── Step 2: Full analysis ─────────────────────────────────────────────
        prompt_text = PROMPT_TEMPLATE.format(
            title=title,
            company=company,
            location=job_data.get("location", "Not specified"),
            experience=job_data.get("experience", "Not specified"),
            skills=", ".join(job_data.get("skills", [])) or "See JD",
            description=job_data.get("description", "")[:8000],
            resume_section=(
                "The resume is attached as a PDF. Read it carefully."
                if has_resume
                else "NO RESUME — set all five dimension scores to 0."
            ),
            interview_research=research_note,
        )

        if has_resume:
            messages_content = [
                {
                    "type": "file",
                    "file": {
                        "filename": "resume.pdf",
                        "file_data": f"data:application/pdf;base64,{resume_b64}"
                    }
                },
                {"type": "text", "text": prompt_text}
            ]
        else:
            messages_content = prompt_text

        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "gpt-4o",
                "max_tokens": 6000,
                "temperature": 0,
                "seed": 42,
                "messages": [{"role": "user", "content": messages_content}]
            }
        )

    if resp.status_code == 401:
        raise Exception("OpenAI authentication failed")
    if resp.status_code == 429:
        raise Exception("RATE_LIMITED")
    if not resp.is_success:
        raise Exception(f"AI service error {resp.status_code}")

    data = resp.json()
    raw = data["choices"][0]["message"]["content"].strip()
    cleaned = raw.replace("```json", "").replace("```", "").strip()

    # Try direct parse first, then fall back to extracting the outermost JSON object.
    # GPT sometimes wraps output in markdown or adds text before/after the JSON.
    result = None
    for attempt in (cleaned, raw):
        try:
            result = json.loads(attempt)
            break
        except json.JSONDecodeError:
            pass
    if result is None:
        # Extract the outermost {...} block
        start = cleaned.find('{')
        end   = cleaned.rfind('}')
        if start != -1 and end > start:
            try:
                result = json.loads(cleaned[start:end + 1])
            except json.JSONDecodeError:
                pass
    if result is None:
        raise Exception("AI returned malformed response. Please try again.")

    # ── Compute the final score deterministically in Python ───────────────────
    breakdown = result.get("score_breakdown", {})
    match_score = compute_weighted_score(breakdown)

    result["match_score"] = match_score
    result["score_breakdown"] = {
        dim: min(100, max(0, int(breakdown.get(dim, 0))))
        for dim in SCORE_WEIGHTS
    } | {
        k: breakdown.get(k, "")
        for k in ("skills_evidence", "experience_evidence", "domain_evidence",
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
    result.setdefault("interview_guide", {
        "company_style": "",
        "research_source": "",
        "technical": [],
        "behavioural": [],
        "company_specific": [],
        "coding_round_strategy": {},
        "preparation_checklist": []
    })
    result.setdefault("apply_recommendation", {
        "verdict": "Apply With Prep",
        "reasoning": "",
        "next_step": ""
    })

    # ── ANTI-HALLUCINATION ENFORCEMENT ────────────────────────────────────────
    # Drop any gap / missing skill / resume suggestion that does not trace back
    # to the JD text or the model's own extracted jd_requirements. Even if the
    # model ignores the prompt, these claims never reach the user.
    jd_text = (job_data.get("description", "") or "")
    jd_reqs = result.get("jd_requirements", []) or []

    result["missing_skills"] = [
        ms for ms in result["missing_skills"]
        if _grounded_in_jd(ms.get("skill", "") if isinstance(ms, dict) else str(ms),
                           jd_text, jd_reqs)
    ]
    result["gap_reasons"] = [
        g for g in result["gap_reasons"]
        if _grounded_in_jd(g if isinstance(g, str) else g.get("text", ""),
                           jd_text, jd_reqs)
    ]
    # resume_suggestions: keep only those whose gap_addressed is grounded, OR
    # that are pure resume-wording fixes (no gap claimed).
    cleaned_suggestions = []
    for s in result["resume_suggestions"]:
        if not isinstance(s, dict):
            cleaned_suggestions.append(s); continue
        ga = s.get("gap_addressed", "")
        if not ga or _grounded_in_jd(ga, jd_text, jd_reqs):
            cleaned_suggestions.append(s)
    result["resume_suggestions"] = cleaned_suggestions
    # improvement_plan: keep only items whose closes_gap is grounded
    result["improvement_plan"] = [
        p for p in result["improvement_plan"]
        if not isinstance(p, dict) or not p.get("closes_gap")
        or _grounded_in_jd(p.get("closes_gap", ""), jd_text, jd_reqs)
    ]

    return result
