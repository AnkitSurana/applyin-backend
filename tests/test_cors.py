"""CORS origin policy: extension lockdown flag + LinkedIn/site rules.

Tests the `_is_allowed` decision directly by patching the module-level config,
so we don't depend on import-time env."""
import app.main as main


def test_default_permissive_allows_any_extension(monkeypatch):
    monkeypatch.setattr(main, "_LOCK_EXTENSIONS", False)
    monkeypatch.setattr(main, "ALLOWED_ORIGINS", {"https://www.linkedin.com"})
    assert main._is_allowed("chrome-extension://anyrandomid123") is True


def test_lockdown_blocks_unlisted_extension(monkeypatch):
    monkeypatch.setattr(main, "_LOCK_EXTENSIONS", True)
    monkeypatch.setattr(main, "ALLOWED_ORIGINS",
                        {"chrome-extension://realid", "https://www.linkedin.com"})
    assert main._is_allowed("chrome-extension://stranger") is False


def test_lockdown_allows_listed_extension(monkeypatch):
    monkeypatch.setattr(main, "_LOCK_EXTENSIONS", True)
    monkeypatch.setattr(main, "ALLOWED_ORIGINS", {"chrome-extension://realid"})
    assert main._is_allowed("chrome-extension://realid") is True


def test_linkedin_always_allowed(monkeypatch):
    monkeypatch.setattr(main, "_LOCK_EXTENSIONS", True)
    monkeypatch.setattr(main, "ALLOWED_ORIGINS", {"https://www.linkedin.com"})
    assert main._is_allowed("https://www.linkedin.com") is True


def test_random_website_blocked(monkeypatch):
    monkeypatch.setattr(main, "ALLOWED_ORIGINS", {"https://www.linkedin.com"})
    assert main._is_allowed("https://evil.example.com") is False


def test_empty_origin_blocked():
    assert main._is_allowed("") is False
