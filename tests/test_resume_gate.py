"""Resume gate decision logic (parse + identity mocked; no real PDF/network)."""
import app.services.resume_gate as rg
from app.services.costing import UsageMeter


def _stats(**over):
    base = {"pages_parsed": 0, "word_count": 0, "text": "", "is_pdf": False,
            "render_pages": 0, "oversize": False}
    base.update(over)
    return base


async def test_no_resume_when_none():
    g = await rg.run_resume_gate(None, None, UsageMeter())
    assert g["valid"] is False and g["reason"] == "NO_RESUME"


async def test_no_resume_when_too_short():
    g = await rg.run_resume_gate(None, "abc", UsageMeter())
    assert g["reason"] == "NO_RESUME"


async def test_oversize_rejected(monkeypatch):
    monkeypatch.setattr(rg, "parse_resume_stats", lambda b64: _stats(oversize=True))
    g = await rg.run_resume_gate(None, "x" * 200, UsageMeter())
    assert g["reason"] == "RESUME_TOO_LARGE"


async def test_not_a_pdf(monkeypatch):
    monkeypatch.setattr(rg, "parse_resume_stats", lambda b64: _stats(is_pdf=False))
    g = await rg.run_resume_gate(None, "x" * 200, UsageMeter())
    assert g["reason"] == "NOT_A_PDF"


async def test_empty_pdf(monkeypatch):
    monkeypatch.setattr(rg, "parse_resume_stats",
                        lambda b64: _stats(is_pdf=True, pages_parsed=0))
    g = await rg.run_resume_gate(None, "x" * 200, UsageMeter())
    assert g["reason"] == "EMPTY_PDF"


async def test_image_only_pdf_is_valid(monkeypatch):
    monkeypatch.setattr(rg, "parse_resume_stats",
                        lambda b64: _stats(is_pdf=True, pages_parsed=2,
                                           word_count=5, render_pages=2))
    g = await rg.run_resume_gate(None, "x" * 200, UsageMeter())
    assert g["valid"] is True and g["reason"] == "IMAGE_ONLY_PDF"


async def test_valid_resume(monkeypatch):
    monkeypatch.setattr(rg, "parse_resume_stats",
                        lambda b64: _stats(is_pdf=True, pages_parsed=2, word_count=400,
                                           text="John Doe Senior Engineer Python AWS",
                                           render_pages=2))

    async def fake_identity(client, text, meter):
        return {"name": "John Doe", "current_title": "Engineer",
                "looks_like_resume": True, "email_found": True}

    monkeypatch.setattr(rg, "extract_resume_identity", fake_identity)
    g = await rg.run_resume_gate(None, "x" * 200, UsageMeter())
    assert g["valid"] is True and g["reason"] == "OK"
    assert g["name"] == "John Doe"


async def test_not_a_resume(monkeypatch):
    monkeypatch.setattr(rg, "parse_resume_stats",
                        lambda b64: _stats(is_pdf=True, pages_parsed=1, word_count=400,
                                           text="lorem ipsum filler", render_pages=1))

    async def fake_identity(client, text, meter):
        return {"looks_like_resume": False}

    monkeypatch.setattr(rg, "extract_resume_identity", fake_identity)
    g = await rg.run_resume_gate(None, "x" * 200, UsageMeter())
    assert g["valid"] is False and g["reason"] == "NOT_A_RESUME"
