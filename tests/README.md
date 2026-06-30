# Tests

Unit + smoke tests for the backend. External services (Supabase, OpenAI, Razorpay,
PyMuPDF) are mocked - no test ever hits a real network or database.

## Run

```bash
cd applyin-backend
pip install -r requirements.txt -r requirements-dev.txt
pytest
```

`pytest` alone runs everything (config is in `pytest.ini`).

## What's covered

| File | Area |
|------|------|
| `test_costing.py` | Token/cost accounting + per-call `duration_ms` timing |
| `test_ai_helpers.py` | Weighted scoring, JSON parsing, JD grounding, casing |
| `test_auth_logic.py` | Email canonicalization, disposable blocking, password policy |
| `test_analyze_logic.py` | JD readability gate, fingerprint cache key, PII stripping |
| `test_privacy_logic.py` | IP hashing uses a dedicated salt (no payment-secret reuse) |
| `test_cors.py` | Extension lockdown flag + LinkedIn/site origin rules |
| `test_auth_dependency.py` | JWT validation dependency (sync def for threadpool) |
| `test_payments.py` | Payment idempotency: atomic claim prevents double-credit |
| `test_resume_gate.py` | Resume gate decisions (no-resume, oversize, image-only, valid, etc.) |
| `test_app_smoke.py` | HTTP: /health, CORS headers, gated /docs, webhook signature |

## Notes

- `conftest.py` sets dummy env vars and stubs heavy libs (`supabase`/`razorpay`/`fitz`)
  only if they aren't installed, so the suite runs even without them. In a full
  environment the real libs load, but external **calls** are still mocked.
- Tests cover the security/perf changes: payment double-credit guard, CORS lockdown,
  IP-salt fix, password hardening, non-blocking auth, and per-call timing.
