"""Auth: email normalization, disposable blocking, and password hardening."""
import pytest
from pydantic import ValidationError
from app.routers.auth import _normalize_email, _is_disposable, SignupRequest


# ── Email canonicalization (alias-farming prevention) ────────────────────────
def test_gmail_strips_dots_and_plus():
    assert _normalize_email("u.s.e.r+tag@gmail.com") == "user@gmail.com"


def test_googlemail_treated_as_gmail():
    assert _normalize_email("u.s.er@googlemail.com") == "user@googlemail.com"


def test_outlook_keeps_dots_strips_plus():
    assert _normalize_email("first.last+promo@outlook.com") == "first.last@outlook.com"


def test_plain_address_unchanged():
    assert _normalize_email("me@company.com") == "me@company.com"


# ── Disposable domains ───────────────────────────────────────────────────────
def test_disposable_detected():
    assert _is_disposable("x@mailinator.com")
    assert _is_disposable("y@10minutemail.com")


def test_real_domain_not_disposable():
    assert not _is_disposable("x@gmail.com")


# ── Password policy (the hardening we added) ─────────────────────────────────
def test_strong_password_accepted():
    r = SignupRequest(email="a@b.com", password="Tr0ub4dour&3")
    assert r.password == "Tr0ub4dour&3"


def test_short_password_rejected():
    with pytest.raises(ValidationError):
        SignupRequest(email="a@b.com", password="short")


def test_common_password_rejected():
    with pytest.raises(ValidationError):
        SignupRequest(email="a@b.com", password="password123")


def test_common_password_rejected_case_insensitive():
    with pytest.raises(ValidationError):
        SignupRequest(email="a@b.com", password="PassWord123")


def test_all_same_char_rejected():
    with pytest.raises(ValidationError):
        SignupRequest(email="a@b.com", password="aaaaaaaa")


def test_disposable_email_rejected_by_model():
    with pytest.raises(ValidationError):
        SignupRequest(email="bot@mailinator.com", password="Tr0ub4dour&3")
