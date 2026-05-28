import httpx
import json
from app.config import settings

PROMPT_TEMPLATE = """You are Fitly's intelligent matching engine. Your task is to analyse how well a candidate fits a job, then produce a structured report.

Be precise, honest, and specific. Every finding must reference actual content from the JD and resume — no boilerplate.

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

Respond ONLY with a valid JSON object — no markdown, no backticks:

{{
  "match_score": <0-100 integer. SCORING RUBRIC: 90+=near-perfect skill+exp match, 75-89=strong match with minor gaps, 60-74=good match some gaps, 45-59=partial match notable gaps, <45=weak match. Be objective and consistent — identical inputs must produce identical scores>,
  "fit_level": "<strong|medium|weak>",
  "verdict": "<2–3 sentences: specific honest summary citing role, company, evidence>",
  "resume_strengths": ["<strong point IN the resume: specific skill/experience that stands out>", "<another>", "<another>"],
  "fit_reasons": ["<specific match: JD requires X, resume shows Y>", "<another>", "<another>"],
  "gap_reasons": ["<gap: JD requires X, resume has none>", "<another>"],
  "missing_skills": [
    {{ "skill": "<name>", "importance": "<critical|important|nice-to-have>", "how_to_learn": "<specific course>" }}
  ],
  "improvement_plan": [
    {{ "action": "<specific action>", "impact": "<high|medium>", "timeframe": "<timeframe>" }}
  ],
  "resume_suggestions": [
    {{ "issue": "<gap for THIS role>", "fix": "<exact change>", "example": "<rewritten bullet>" }}
  ],
  "interview_guide": {{
    "technical": [
      {{ "question": "<q>", "why_asked": "<what tested>", "how_to_answer": "<framework>", "example_answer_start": "<1–2 sentences>" }}
    ],
    "behavioural": [
      {{ "question": "<q>", "why_asked": "<competency>", "star_guide": "<S/T/A/R guidance>" }}
    ],
    "company_specific": [
      {{ "question": "<q>", "context": "<why asked>", "how_to_answer": "<key points>" }}
    ]
  }},
  "apply_recommendation": {{
    "verdict": "<Apply Now|Apply With Prep|Improve First|Skip>",
    "reasoning": "<1–2 sentences>",
    "next_step": "<single most important action>"
  }}
}}

SCORING RULES:
- No resume → 35–55
- Resume matches 80%+ of JD → 80–95
- Resume matches 50–79% → 55–79
- Resume matches 30–49% → 35–54
- Resume matches <30% → 15–34
- NEVER default to 50
- fit_level: strong=75+, medium=45–74, weak=<45
- missing_skills: max 5, only absent JD skills
- improvement_plan: max 4 items by impact
- resume_suggestions: max 4, job-specific
- interview_guide: 4 technical, 3 behavioural, 2 company_specific"""

async def run_analysis(job_data: dict, resume_b64: str | None) -> dict:
    has_resume = bool(resume_b64 and len(resume_b64) > 100)

    prompt_text = PROMPT_TEMPLATE.format(
        title=job_data.get("title", ""),
        company=job_data.get("company", ""),
        location=job_data.get("location", "Not specified"),
        experience=job_data.get("experience", "Not specified"),
        skills=", ".join(job_data.get("skills", [])) or "See JD",
        description=job_data.get("description", "")[:4000],
        resume_section="The resume is attached as a PDF. Read it carefully." if has_resume
                       else "NO RESUME — score 35–55, use generic suggestions.",
    )

    # Build message — attach PDF if available
    if has_resume:
        content = [
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
        content = prompt_text

    async with httpx.AsyncClient(timeout=90) as client:
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "gpt-4o",
                "max_tokens": 2800,
                "temperature": 0.1,  # Low for consistent scoring
                "messages": [{"role": "user", "content": content}]
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

    try:
        result = json.loads(cleaned)
    except json.JSONDecodeError:
        raise Exception("AI returned malformed response. Please try again.")

    # Normalise
    result["match_score"] = min(100, max(0, int(result.get("match_score", 40))))
    result["fit_level"] = (
        "strong" if result["match_score"] >= 75 else
        "medium" if result["match_score"] >= 45 else "weak"
    )
    result.setdefault("resume_strengths", [])
    result.setdefault("fit_reasons", [])
    result.setdefault("gap_reasons", [])
    result["missing_skills"] = result.get("missing_skills", [])[:5]
    result["improvement_plan"] = result.get("improvement_plan", [])[:4]
    result["resume_suggestions"] = result.get("resume_suggestions", [])[:4]
    result.setdefault("interview_guide", {"technical": [], "behavioural": [], "company_specific": []})
    result.setdefault("apply_recommendation", {"verdict": "Apply With Prep", "reasoning": "", "next_step": ""})

    return result
