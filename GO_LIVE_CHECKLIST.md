# Go-live checklist — Applyin backend

This repo is **deploy-and-verify**, not blindly production-ready. The code passed
60 logic checks locally, but the live path (real OpenAI calls, real Supabase
writes, real resume quality) has never run. Tick every box below and it becomes
production-ready, because you will have seen it work.

## 1. Database (order matters)
- [ ] Run `schema.sql` in Supabase SQL editor (creates base tables).
- [ ] THEN run `schema_migration.sql` (adds metric columns to `usage_events`).
      Running migration first fails — it ALTERs a table schema.sql creates.

## 2. Environment
- [ ] All vars from `.env.example` set on Render (OPENAI_API_KEY, SUPABASE_*,
      RAZORPAY_*, etc).
- [ ] **Set `ALLOWED_ORIGINS`** to include your published extension id, e.g.
      `chrome-extension://<your-id>,https://www.linkedin.com`. If you skip this,
      the extension's API calls are CORS-blocked in production. Do NOT set
      `ALLOW_ANY_EXTENSION` in production.
- [ ] In the OpenAI dashboard, confirm data-retention / no-training settings
      match what your privacy notice claims.

## 2a. Security hardening (applied — verify in staging)
- [ ] CORS now uses an allowlist (no more reflect-any-origin). Confirm the
      extension can call the API once `ALLOWED_ORIGINS` includes its id.
- [ ] Rate limits are live (login 10/min, signup 5/min, analyze 20/min, etc).
      Confirm a burst of bad logins returns HTTP 429.
- [ ] `/credits/verify-payment` is now read-only; credits are granted ONLY by
      the Razorpay webhook and the signed callback. Confirm a real purchase still
      credits the account (via webhook), and that calling verify-payment never
      adds credits.
- [ ] PDF uploads are capped at 8 MB / 30 pages → `RESUME_TOO_LARGE` (422).
      Confirm a huge PDF is rejected without burning a credit.
- [ ] Client error messages are now generic; full detail is in Render logs only.
- [ ] STILL YOUR ACTION: enable Supabase RLS policies on `analysis_cache`,
      `credit_ledger`, `credit_orders`, `usage_events`. The backend uses the
      service-role key and filters by `user_id` in code, but RLS is the real
      backstop and must be turned on in the Supabase dashboard.

## 3. Deploy + smoke test
- [ ] Deploy to Render. Confirm `/health` returns 200.
- [ ] Run `live_smoke_test.py` (from the tests zip) with your real key against the
      deployed URL. It runs one resume against a data-eng JD and a frontend JD and
      prints sub-scores side by side.
- [ ] **The two jobs MUST give DIFFERENT sub-scores.** Identical scores = resume
      not being read = the original bug is back. Do not go live if they match.

## 4. Known runtime risk to watch
- [ ] First real call: if OpenAI returns 400 on `temperature` for the Responses
      API with gpt-4o, drop the single `temperature` line in
      `ai.py::_call_responses_json`. (It is accepted as of last check, but verify.)
- [ ] Grep Render logs for `AUDIT` — confirm one line per analysis with pages,
      words, scores, tokens, cost.
- [ ] Watch for `[!ALL-ZERO]` / `ANALYSIS_DEGENERATE` — means a valid resume
      produced all-zero sub-scores; the backstop refused to emit fake output.

## 5. Extension
- [ ] Load `applyin-extension` unpacked in Chrome. Click through once on a real
      LinkedIn job: upload resume → see the "Resume read: …" strip → analyse →
      get a job-specific result.
- [ ] Confirm rejecting a junk PDF shows the reject toast and does NOT burn a credit.

## Known cosmetic note
- `src/background/pdf_parser.js` is dead (not referenced anywhere). Safe to leave
  or delete; it does nothing.

## Still your responsibility (not code)
- Legal review for DPDP/GDPR, privacy policy, OpenAI DPA, lawful basis.
- Schedule the `usage_events` purge (commented cron in `schema_migration.sql`).
