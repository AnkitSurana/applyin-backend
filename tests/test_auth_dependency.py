"""get_current_user: JWT validation dependency (now a sync def for threadpool)."""
import pytest
from fastapi import HTTPException
import app.dependencies as dep


class _User:
    id = "user-123"
    email = "a@b.com"


def _fake_db(get_user_impl):
    db = type("DB", (), {})()
    db.auth = type("Auth", (), {"get_user": staticmethod(get_user_impl)})()
    return db


def test_valid_token_returns_user(monkeypatch):
    resp = type("R", (), {"user": _User()})()
    monkeypatch.setattr(dep, "get_supabase", lambda: _fake_db(lambda token: resp))
    out = dep.get_current_user("Bearer valid.jwt.token")
    assert out.id == "user-123"


def test_missing_bearer_prefix_rejected():
    with pytest.raises(HTTPException) as e:
        dep.get_current_user("Token abc")
    assert e.value.status_code == 401


def test_null_user_rejected(monkeypatch):
    resp = type("R", (), {"user": None})()
    monkeypatch.setattr(dep, "get_supabase", lambda: _fake_db(lambda token: resp))
    with pytest.raises(HTTPException) as e:
        dep.get_current_user("Bearer bad")
    assert e.value.status_code == 401


def test_validation_error_becomes_401(monkeypatch):
    def boom(token):
        raise RuntimeError("supabase unreachable")
    monkeypatch.setattr(dep, "get_supabase", lambda: _fake_db(boom))
    with pytest.raises(HTTPException) as e:
        dep.get_current_user("Bearer x")
    assert e.value.status_code == 401


def test_is_sync_def_for_threadpool():
    # The perf fix: dependency must be a plain function (FastAPI runs it in a
    # threadpool), NOT a coroutine, so the blocking Supabase call doesn't block
    # the event loop.
    import inspect
    assert not inspect.iscoroutinefunction(dep.get_current_user)
