-- NOTE: For a fresh setup, run schema_reset.sql instead (one file, all tables).
-- This file is kept for reference. SUPERSEDED by schema_reset.sql.

-- ═══════════════════════════════════════════════════════════════════════════
-- Applyin — metrics migration
-- Adds business-metric columns to usage_events (cost/token/behaviour analysis)
-- and a retention policy. Run in Supabase SQL Editor.
--
-- PII policy: this table stores NO resume text and NO candidate name. job_title
-- and company are the role being analysed, not the user. Identity is user_id
-- (the account holder). This supports data-minimisation; confirm against your
-- DPDP/GDPR obligations with a qualified reviewer — this is not legal advice.
-- ═══════════════════════════════════════════════════════════════════════════

-- ── New columns (idempotent) ────────────────────────────────────────────────
alter table usage_events add column if not exists request_id     text;
alter table usage_events add column if not exists resume_path     text;
alter table usage_events add column if not exists pages_parsed    integer default 0;
alter table usage_events add column if not exists word_count      integer default 0;
alter table usage_events add column if not exists cached          boolean default false;
alter table usage_events add column if not exists input_tokens    integer default 0;
alter table usage_events add column if not exists output_tokens   integer default 0;
alter table usage_events add column if not exists total_cost_usd  numeric(10,6) default 0;
alter table usage_events add column if not exists latency_ms      integer default 0;
alter table usage_events add column if not exists cost_breakdown  jsonb default '{}';

create index if not exists idx_usage_events_created on usage_events(created_at);
create index if not exists idx_usage_events_request on usage_events(request_id);

-- ── Retention: purge rows older than 12 months ──────────────────────────────
-- Schedule this (Supabase cron / pg_cron) to run daily. Adjust the interval to
-- match your stated retention policy in your privacy notice.
--
--   select cron.schedule('purge-usage-events', '0 3 * * *',
--     $$ delete from usage_events where created_at < now() - interval '12 months' $$);
--
-- Manual run:
-- delete from usage_events where created_at < now() - interval '12 months';

-- ═══════════════════════════════════════════════════════════════════════════
-- Useful analytics queries for pricing decisions
-- ═══════════════════════════════════════════════════════════════════════════
-- Avg cost + tokens per (non-cached) analysis:
--   select round(avg(total_cost_usd),5) avg_cost,
--          round(avg(input_tokens)) avg_in, round(avg(output_tokens)) avg_out,
--          round(avg(latency_ms)) avg_latency_ms
--   from usage_events where cached = false;
--
-- Cache hit rate (cached results are free → margin):
--   select round(100.0 * sum((cached)::int)/count(*), 1) as cache_hit_pct
--   from usage_events;
--
-- Cost per user (who is expensive):
--   select user_id, count(*) analyses, round(sum(total_cost_usd),4) spend_usd
--   from usage_events where cached=false group by user_id order by spend_usd desc;
--
-- Analyses before first purchase (behaviour → pricing):
--   select u.user_id, count(*) free_analyses_used
--   from usage_events u
--   where u.created_at < (
--     select min(created_at) from credit_ledger l
--     where l.user_id = u.user_id and l.reason = 'purchase')
--   group by u.user_id;
-- ═══════════════════════════════════════════════════════════════════════════
