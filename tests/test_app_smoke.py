"""End-to-end HTTP smoke tests via TestClient: health, CORS headers, gated docs,
webhook signature rejection."""
from fastapi.testclient import TestClient
import app.main as main

client = TestClient(main.app)


def test_health_ok():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_cors_preflight_linkedin_allowed():
    r = client.options("/auth/login", headers={
        "Origin": "https://www.linkedin.com",
        "Access-Control-Request-Method": "POST",
    })
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "https://www.linkedin.com"


def test_cors_preflight_random_site_no_header():
    r = client.options("/auth/login", headers={
        "Origin": "https://evil.example.com",
        "Access-Control-Request-Method": "POST",
    })
    assert r.headers.get("access-control-allow-origin") is None


def test_docs_disabled_without_key():
    # /docs returns 404 unless DIAGNOSTICS_KEY is set (it is not, in tests).
    assert client.get("/docs").status_code == 404


def test_diagnostics_disabled_without_key():
    assert client.get("/health/diagnostics").status_code == 404


def test_webhook_rejects_bad_signature():
    r = client.post(
        "/webhook/razorpay",
        content=b'{"event":"payment_link.paid"}',
        headers={"x-razorpay-signature": "deadbeef"},
    )
    assert r.status_code == 400
