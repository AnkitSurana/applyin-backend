# Applyin - Backend

FastAPI backend for the Applyin Chrome extension. It scores a resume against a LinkedIn job description and returns a fit report: a match score, skill gaps, resume rewrites, and an interview guide.

## Stack

- FastAPI (Python 3.11)
- Supabase (auth + Postgres)
- OpenAI (analysis)
- Razorpay (payments)

## Run locally

```bash
cp .env.example .env          # fill in your own keys
pip install -r requirements.txt
uvicorn app.main:app --reload
```

All required environment variables are listed in `.env.example` and `render.yaml`.

## Deploy

Hosted on Render via `render.yaml`. Set the same environment variables in the Render dashboard.

## Health

`GET /health` returns `{"status": "ok"}`.

---

Architecture, the database schema, the go-live checklist, and compliance notes are maintained privately, outside this repository.
