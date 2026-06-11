# Applyin — Backend

FastAPI backend for the Applyin Chrome extension. Scores a resume against a
LinkedIn JD, gates on a readable resume before charging, and tracks token + cost
per analysis.

## Layout

```
applyin-backend/
├── app/
│   ├── main.py              # FastAPI app, CORS, routers
│   ├── config.py            # settings + Supabase clients
│   ├── dependencies.py      # JWT auth dependency
│   ├── routers/
│   │   ├── auth.py          # signup / login / me / refresh
│   │   ├── analyze.py       # POST /analyze/job  (gate → charge → analyse → log)
│   │   ├── credits.py       # packages, payment links, verify
│   │   └── webhook.py       # Razorpay webhook
│   └── services/
│       ├── ai.py            # run_analysis: gate + Responses API + grounding
│       ├── resume_gate.py   # PyMuPDF parse + gpt-4o-mini identity extraction
│       └── costing.py       # per-call token + USD accounting
├── schema.sql               # run FIRST (fresh install)
├── schema_migration.sql     # run SECOND (metrics columns + retention)
├── requirements.txt
├── render.yaml
├── runtime.txt
├── COMPLIANCE_NOTES.md      # what the code does / does NOT guarantee
└── .env.example
```

## Setup

1. **Supabase**: run `schema.sql`, then `schema_migration.sql`, in the SQL Editor.
2. **Env**: copy `.env.example` → set real values (Supabase, OpenAI, Razorpay).
3. **Local run**:
   ```
   pip install -r requirements.txt
   uvicorn app.main:app --reload
   ```
4. **Deploy**: push to a repo, connect to Render (uses `render.yaml`), set the
   env vars in the Render dashboard.

## How an analysis flows

```
POST /analyze/job
  → cache hit?                  → return free (logged, metered)
  → credits available?          → no: 402
  → run_analysis():
       resume gate (parse + extract + validate)
         invalid                → 422 + reason, NO credit, NO output
       valid → analysis call (Responses API, resume as input_file/image)
       all-zero scores backstop → 422, NO credit (no false output)
  → success → charge 1 credit
  → cache (name stripped) + audit log + usage_events metrics
  → return result incl. resume_meta (name display-only)
```

## Verify after deploy

1. One real analysis on a LinkedIn job with a resume uploaded.
2. Render logs → grep `AUDIT`. You should see scores, tokens, cost, pages/words.
3. Run two different jobs → sub-scores should differ (resume is being read).
4. Upload a junk/non-PDF → should get a 422 rejection, no credit charged.

## Notes

- `temperature` is set on the Responses analysis call. If your account 400s on
  it, remove that one line in `ai.py::_call_responses_json`.
- `ANALYSIS_MODEL` / `INTERVIEW_MODEL` / `GATE_MODEL` are swappable; `costing.py`
  prices whatever you set. gpt-4o is grandfathered legacy pricing — new accounts
  may need to switch to gpt-4.1.
- **Compliance**: read `COMPLIANCE_NOTES.md`. The code supports data-minimisation
  but is not a certification. Get legal review before claiming compliance.
```
