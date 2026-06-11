# Applyin — Data Handling & Compliance Notes

**This is a developer's description of what the code does. It is NOT legal advice
and NOT a certification of compliance.** Whether Applyin meets DPDP (India),
GDPR (EU), or any other regime depends on facts outside the code — your privacy
policy, lawful basis, jurisdictions, and your data-processing agreements with
OpenAI, Supabase, Render, and Razorpay. Have a qualified person review this
against your obligations before making any compliance claim to users.

## What personal data flows through the system

| Data | Where it goes | Retained? |
|---|---|---|
| Resume PDF (base64) | Sent to OpenAI for analysis; held in memory during the request | **No** — not written to disk or DB by Applyin |
| Candidate name (extracted) | Returned to the extension for the on-screen "Resume read: …" strip | **No** — stripped before caching, never logged |
| Resume text | Parsed in memory for the gate; sent to OpenAI | **No** — never logged, never stored |
| Email | Supabase Auth (account identity) | Yes — it's the account |
| Analysis result | `analysis_cache` (24h TTL), name removed first | 24h |
| Metrics (tokens, cost, scores, pages, words, latency) | `usage_events` | Per retention policy (default 12 months) |

## Design choices that support data-minimisation

- **No analysis without a readable resume.** The gate parses the whole PDF
  (pages + words) and runs one extraction call to confirm a real resume before
  any credit is charged or the main analysis runs. Rejected resumes cost nothing
  and produce no output (HTTP 422 with a reason).
- **Name is display-only.** Extracted, shown to the user (their own data),
  `_strip_pii()` removes it before the result is cached, and it never appears in
  any log line or the metrics table.
- **Logs are metadata, not content.** The `AUDIT` log line carries request_id,
  user_id, role/company, scores, tokens, cost, latency — no resume text, no name.
- **No false output.** If the model returns degenerate all-zero scores despite a
  valid resume, the request is rejected and refunded rather than returning a
  fabricated result. The interview guide returns empty (UI hides it) rather than
  placeholder text on failure.
- **Bounded retention.** `usage_events` has a purge query (default 12 months) in
  `schema_migration.sql`. Schedule it and state the matching window in your
  privacy notice.

## What the code does NOT do (your responsibility)

- It does not constitute a privacy policy, consent flow, or lawful-basis record.
- It does not implement a user-facing data-subject-access or erasure UI. Because
  nothing personal beyond user_id-linked metrics is stored, erasure ≈ deleting
  that user's `usage_events` + `analysis_cache` + auth rows — but you must build
  and document the process.
- It does not cover your DPAs with sub-processors (OpenAI, Supabase, Render,
  Razorpay). Resumes are sent to OpenAI; your policy must disclose that and your
  DPA must permit it.
- It makes no claim about OpenAI's data retention of submitted resumes. Confirm
  your OpenAI data-processing terms (e.g. zero-retention / no-training settings)
  separately.

## Recommended next steps before launch

1. Legal review against DPDP 2023 (you process Indian users' resumes from
   Bengaluru — this likely applies).
2. Privacy policy stating: what you collect, that resumes are sent to OpenAI,
   retention windows, and erasure process.
3. Confirm OpenAI account data-handling settings (retention/training).
4. Schedule the `usage_events` purge.
5. Decide and document a retention window for Supabase Auth + cache.
