"""Analyze: JD readability gate, fingerprint cache key, and PII stripping."""
from app.routers.analyze import (
    _jam_ratio, _normalize_for_fingerprint, job_cache_key, _strip_pii, JobData,
)


# ── JD readability gate ──────────────────────────────────────────────────────
def test_jam_ratio_clean_text_low():
    assert _jam_ratio("This is a normal job description with plenty of spaces") < 0.12


def test_jam_ratio_jammed_text_high():
    assert _jam_ratio("partneringcloselywithmanagersacrosstheorganisationtodeliverresults") > 0.12


def test_jam_ratio_empty_is_one():
    assert _jam_ratio("") == 1.0


# ── Fingerprint normalization ────────────────────────────────────────────────
def test_normalize_lowercases_and_strips_punctuation():
    assert _normalize_for_fingerprint("Hello,   World!!") == "hello world"


# ── Cache key (consistency guarantee) ────────────────────────────────────────
def _job():
    return JobData(title="Engineer", company="Acme", description="Build things")


def test_cache_key_is_deterministic():
    assert job_cache_key(_job(), "resumeb64") == job_cache_key(_job(), "resumeb64")


def test_cache_key_changes_with_resume():
    assert job_cache_key(_job(), "resumeA") != job_cache_key(_job(), "resumeB")


def test_cache_key_stable_across_cosmetic_jd_changes():
    j1 = JobData(title="Engineer", company="Acme", description="Build  things!!")
    j2 = JobData(title="engineer", company="ACME", description="build things")
    assert job_cache_key(j1, "r") == job_cache_key(j2, "r")


def test_cache_key_no_resume_distinct():
    assert job_cache_key(_job(), None) != job_cache_key(_job(), "r")


# ── PII stripping (name never cached/logged) ─────────────────────────────────
def test_strip_pii_removes_name():
    res = {"resume_meta": {"name": "John Doe", "pages_parsed": 2}, "match_score": 70}
    out = _strip_pii(res)
    assert "name" not in out["resume_meta"]
    assert out["resume_meta"]["pages_parsed"] == 2
    assert out["match_score"] == 70


def test_strip_pii_does_not_mutate_original():
    res = {"resume_meta": {"name": "Jane", "pages_parsed": 1}}
    _strip_pii(res)
    assert res["resume_meta"]["name"] == "Jane"  # original untouched
