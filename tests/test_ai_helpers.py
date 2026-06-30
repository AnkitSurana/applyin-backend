"""Deterministic AI helper logic: scoring, JSON parsing, JD grounding, casing."""
import pytest
from app.services.ai import (
    compute_weighted_score, _strip_to_json, _grounded_in_jd, _sentence_case,
    SCORE_WEIGHTS,
)


def test_weighted_score_all_max():
    assert compute_weighted_score({k: 100 for k in SCORE_WEIGHTS}) == 100


def test_weighted_score_empty_is_zero():
    assert compute_weighted_score({}) == 0


def test_weighted_score_clamped_to_100():
    assert compute_weighted_score({k: 200 for k in SCORE_WEIGHTS}) == 100


def test_weighted_score_known_mix():
    # skills .35, experience .25, domain .20, qualifications .10, soft .10
    bd = {"skills_match": 80, "experience_match": 40, "domain_match": 60,
          "qualifications_match": 100, "soft_skills_match": 50}
    # 28 + 10 + 12 + 10 + 5 = 65
    assert compute_weighted_score(bd) == 65


def test_strip_to_json_plain():
    assert _strip_to_json('{"a": 1}') == {"a": 1}


def test_strip_to_json_fenced():
    assert _strip_to_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_strip_to_json_embedded():
    assert _strip_to_json('here you go {"a": 1} thanks') == {"a": 1}


def test_strip_to_json_malformed_raises():
    with pytest.raises(Exception):
        _strip_to_json("not json at all")


def test_grounded_primary_token_present():
    # "orchestration" is a stopword, so primary token is "kubernetes" -> in JD
    assert _grounded_in_jd("Kubernetes orchestration", "We run Kubernetes daily", [])


def test_grounded_absent_is_false():
    assert not _grounded_in_jd("Photoshop", "We run Kubernetes daily", [])


def test_sentence_case_lowercases_first():
    assert _sentence_case("hello world") == "Hello world"


def test_sentence_case_preserves_acronyms():
    assert _sentence_case("SQL is great") == "SQL is great"
