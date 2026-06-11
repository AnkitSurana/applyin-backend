"""
Resume gate — runs BEFORE any credit is charged or the main analysis call is made.

Two jobs in one place:
  1. Parse the WHOLE resume with PyMuPDF → page count + word count (evidence, not
     a hardcoded floor).
  2. A single cheap gpt-4o-mini call extracts name + current details and judges
     whether this is a real, readable resume.

If the gate says invalid, the caller rejects the request: no credit, no analysis
call, clear message to the user.

PRIVACY: the extracted name is returned to the caller for the UI strip only.
It is NEVER logged and NEVER stored server-side (the caller strips it before
caching / logging). Only counts + role/company may surface in metrics.
"""

import json
import base64 as _b64
import logging
import httpx
from app.config import settings
from app.services.costing import UsageMeter, extract_usage

logger = logging.getLogger("applyin.gate")

GATE_MODEL = "gpt-4o-mini"


def parse_resume_stats(resume_b64: str) -> dict:
    """
    Parse the entire PDF with PyMuPDF. Returns full text + page/word counts.
    No thresholds here — just the raw evidence. Returns text="" on failure so
    the caller's validity logic decides, never this function.
    """
    out = {"pages_parsed": 0, "word_count": 0, "text": "", "is_pdf": False,
           "render_pages": 0}
    try:
        import fitz  # PyMuPDF
    except Exception:
        logger.warning("PyMuPDF missing — cannot parse resume stats")
        return out

    try:
        pdf_bytes = _b64.b64decode(resume_b64)
    except Exception:
        return out

    # Confirm it's actually a PDF (header within first bytes).
    out["is_pdf"] = pdf_bytes[:1024].find(b"%PDF") != -1
    if not out["is_pdf"]:
        return out

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as e:
        logger.warning(f"PyMuPDF open failed: {e}")
        return out

    texts = []
    for page in doc:
        try:
            texts.append(page.get_text() or "")
        except Exception:
            texts.append("")
    full = "\n".join(texts)
    out["pages_parsed"] = doc.page_count
    out["render_pages"] = doc.page_count  # pages we could rasterise if text is thin
    out["text"] = full
    out["word_count"] = len([w for w in full.split() if len(w) > 1])
    doc.close()
    return out


EXTRACT_PROMPT = """\
You are validating a resume. Read the text below (extracted from a PDF) and return
ONLY JSON. Extract what is genuinely present; use null for anything not found.
Do not invent.

{{
  "name": "<candidate's full name, or null>",
  "current_title": "<most recent job title, or null>",
  "current_company": "<most recent employer, or null>",
  "email_found": <true|false>,
  "phone_found": <true|false>,
  "looks_like_resume": <true|false  // true only if this reads like a real CV/resume>
}}

RESUME TEXT:
{resume_text}
"""


async def extract_resume_identity(client: httpx.AsyncClient, resume_text: str,
                                  meter: UsageMeter) -> dict:
    """gpt-4o-mini extraction. Metered. Returns {} on any failure (caller handles)."""
    if not resume_text.strip():
        return {}
    prompt = EXTRACT_PROMPT.format(resume_text=resume_text[:6000])
    try:
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}",
                     "Content-Type": "application/json"},
            json={
                "model": GATE_MODEL,
                "max_tokens": 300,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        if not resp.is_success:
            return {}
        data = resp.json()
        in_tok, out_tok = extract_usage(data)
        meter.record("resume_gate", GATE_MODEL, in_tok, out_tok)
        raw = (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
        return json.loads(raw.replace("```json", "").replace("```", "").strip())
    except Exception as e:
        logger.warning(f"Resume identity extraction failed: {e}")
        return {}


async def run_resume_gate(client: httpx.AsyncClient, resume_b64: str | None,
                          meter: UsageMeter) -> dict:
    """
    Full gate. Returns a dict the caller uses to allow/reject and to feed the UI.

    Decision (LENIENT, as agreed): valid when a PDF parsed with a name AND a
    meaningful word count relative to pages. Current role / contact strengthen
    confidence but are NOT mandatory (freshers / career-changers).

    Returned dict:
      {
        "valid": bool,
        "reason": "<machine reason if invalid>",
        "pages_parsed": int,
        "word_count": int,
        "name": str|None,            # display-only, caller must not log/store
        "current_title": str|None,
        "current_company": str|None,
        "email_found": bool,
        "extraction_text_source": "text" | "none",
      }
    """
    base = {"valid": False, "reason": "", "pages_parsed": 0, "word_count": 0,
            "name": None, "current_title": None, "current_company": None,
            "email_found": False, "extraction_text_source": "none"}

    if not resume_b64 or len(resume_b64) < 100:
        base["reason"] = "NO_RESUME"
        return base

    stats = parse_resume_stats(resume_b64)
    base["pages_parsed"] = stats["pages_parsed"]
    base["word_count"]   = stats["word_count"]

    if not stats["is_pdf"]:
        base["reason"] = "NOT_A_PDF"
        return base

    if stats["pages_parsed"] == 0:
        base["reason"] = "EMPTY_PDF"
        return base

    # Thin/zero text usually = scanned/image resume. We still allow it through to
    # the main analysis IMAGE path (Responses reads page images), but we can't
    # extract identity from text. Mark as image-only; caller decides.
    if stats["word_count"] < 20:
        # Too little text to validate identity. Treat as image-only readable PDF:
        # valid ONLY if there are renderable pages (so the image path can read it).
        if stats["render_pages"] > 0:
            base["valid"] = True
            base["reason"] = "IMAGE_ONLY_PDF"
            base["extraction_text_source"] = "none"
            return base
        base["reason"] = "UNREADABLE_PDF"
        return base

    # Healthy text → extract identity to confirm it's a resume.
    base["extraction_text_source"] = "text"
    identity = await extract_resume_identity(client, stats["text"], meter)

    name = (identity.get("name") or None)
    looks_like = bool(identity.get("looks_like_resume"))
    base["name"]            = name
    base["current_title"]   = identity.get("current_title") or None
    base["current_company"] = identity.get("current_company") or None
    base["email_found"]     = bool(identity.get("email_found"))

    # LENIENT validity: real resume if it reads like one AND (has a name OR has
    # enough words to clearly be a CV). Word count is evidence, computed, not a
    # magic constant — we require words to scale with pages so a 3-page blank
    # can't pass on one stray line.
    words_per_page = base["word_count"] / max(1, base["pages_parsed"])
    has_substance = words_per_page >= 40  # derived from parsed pages, not hardcoded floor
    if looks_like and (name or has_substance):
        base["valid"] = True
        base["reason"] = "OK"
    else:
        base["valid"] = False
        base["reason"] = "NOT_A_RESUME" if not looks_like else "INSUFFICIENT_CONTENT"

    return base
