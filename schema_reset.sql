-- ═══════════════════════════════════════════════════════════════════════════
-- Applyin — COMPLETE DATABASE RESET (single file)
-- ---------------------------------------------------------------------------
-- Run this ONCE in the Supabase SQL Editor. It DROPS everything and recreates
-- the full schema: profiles, credits, orders, analysis cache, usage/cost logs,
-- consent, DSR, breach log, retention config, the balance view, RLS, and the
-- new-user trigger. Replaces schema.sql + schema_migration.sql +
-- schema_compliance.sql + schema_consent.sql (no ordering to get wrong).
--
-- WARNING: this DELETES all existing app data in these tables.
-- ═══════════════════════════════════════════════════════════════════════════

-- ── 1. Drop everything (clean slate) ────────────────────────────────────────
drop trigger   if exists on_auth_user_created on auth.users;
drop function  if exists public.handle_new_user();
drop view      if exists user_credit_balances;
drop table     if exists usage_events    cascade;
drop table     if exists analysis_cache  cascade;
drop table     if exists credit_orders   cascade;
drop table     if exists credit_ledger   cascade;
drop table     if exists consent_records cascade;
drop table     if exists dsr_requests    cascade;
drop table     if exists breach_log      cascade;
drop table     if exists retention_policy cascade;
drop table     if exists user_profiles   cascade;

-- ── 2. Core tables ──────────────────────────────────────────────────────────
create table user_profiles (
  id          uuid primary key,
  email       text,
  full_name   text,
  created_at  timestamptz default now(),
  updated_at  timestamptz default now()
);

-- Credits as an append-only ledger; balance is the sum of deltas (see view).
create table credit_ledger (
  id          bigserial primary key,
  user_id     uuid not null,
  delta       integer not null,        -- + grant, - spend
  reason      text not null,           -- 'signup_bonus' | 'analysis' | 'purchase' ...
  meta        jsonb default '{}',
  created_at  timestamptz default now()
);
create index idx_credit_ledger_user on credit_ledger(user_id);

create table credit_orders (
  id                    bigserial primary key,
  user_id               uuid not null,
  razorpay_order_id     text unique,
  razorpay_payment_id   text,
  package_id            text,
  credits               integer,
  amount                integer,
  currency              text default 'INR',
  status                text default 'pending',
  meta                  jsonb default '{}',
  created_at            timestamptz default now(),
  updated_at            timestamptz default now()
);
create index idx_credit_orders_user on credit_orders(user_id);

-- Per-device result cache (TTL via expires_at). Resume text is NOT stored here.
create table analysis_cache (
  cache_key   text not null,
  user_id     uuid not null,
  result      jsonb not null,
  expires_at  timestamptz not null,
  created_at  timestamptz default now(),
  primary key (cache_key, user_id)
);
create index idx_cache_expires on analysis_cache(expires_at);

-- ── 3. Usage + COST + LOG events (full analytics in one table) ──────────────
-- One row per analysis attempt. Stores the business metrics, token usage, cost,
-- latency, and a freeform cost_breakdown so you can audit spend per user/model.
create table usage_events (
  id             bigserial primary key,
  user_id        uuid not null,
  -- what was analysed
  job_title      text,
  company        text,
  match_score    integer,
  fit_level      text,
  had_resume     boolean default false,
  credits_used   integer default 1,
  -- request log / parsing
  request_id     text,
  resume_path    text,           -- 'input_file' | 'page_images' | 'none' (no resume text)
  pages_parsed   integer default 0,
  word_count     integer default 0,
  cached         boolean default false,
  -- cost + tokens (the spend log you asked for)
  input_tokens   integer default 0,
  output_tokens  integer default 0,
  total_cost_usd numeric(10,6) default 0,
  latency_ms     integer default 0,
  model          text,
  cost_breakdown jsonb default '{}',
  -- outcome
  status         text default 'ok', -- 'ok' | 'rejected' | 'error'
  error_code     text,              -- e.g. 'ANALYSIS_DEGENERATE'
  created_at     timestamptz default now()
);
create index idx_usage_events_user    on usage_events(user_id);
create index idx_usage_events_created on usage_events(created_at);

-- ── 4. Consent records (append-only history) ────────────────────────────────
create table consent_records (
  id             uuid primary key default gen_random_uuid(),
  user_id        uuid not null,
  purpose        text not null,                 -- 'resume_processing' | 'analytics' | 'marketing_email'
  granted        boolean not null,              -- true = given, false = withdrawn
  policy_version text not null,
  source         text not null default 'web',   -- 'web' | 'extension'
  ip_hash        text,                          -- salted hash, never raw IP
  user_agent     text,
  created_at     timestamptz not null default now()
);
create index idx_consent_user    on consent_records(user_id);
create index idx_consent_purpose on consent_records(user_id, purpose, created_at desc);

-- ── 5. Data-subject-rights requests ─────────────────────────────────────────
create table dsr_requests (
  id            uuid primary key default gen_random_uuid(),
  user_id       uuid not null,
  kind          text not null,        -- 'access' | 'correction' | 'erasure' | 'portability'
  status        text not null default 'received',
  detail        jsonb default '{}',
  created_at    timestamptz not null default now(),
  resolved_at   timestamptz
);
create index idx_dsr_user on dsr_requests(user_id);

-- ── 6. Breach log ───────────────────────────────────────────────────────────
create table breach_log (
  id            uuid primary key default gen_random_uuid(),
  summary       text not null,
  severity      text not null default 'low',
  affected      jsonb default '{}',
  detected_at   timestamptz not null default now(),
  reported_at   timestamptz,
  notes         text
);

-- ── 7. Retention policy (drives the purge job) ──────────────────────────────
create table retention_policy (
  data_category text primary key,
  retain_days   integer not null,
  note          text
);
insert into retention_policy (data_category, retain_days, note) values
  ('analysis_cache',  1,    'Per-device result cache; short TTL.'),
  ('usage_events',    365,  'Cost/usage metrics; ~12 months then delete.'),
  ('consent_records', 2555, 'Consent/withdrawal history (~7y) as proof of lawful basis.'),
  ('dsr_requests',    2555, 'Rights requests kept ~7y for accountability.')
on conflict (data_category) do update set retain_days = excluded.retain_days;

-- ── 8. Row Level Security ───────────────────────────────────────────────────
alter table user_profiles   enable row level security;
alter table credit_ledger   enable row level security;
alter table credit_orders   enable row level security;
alter table analysis_cache  enable row level security;
alter table usage_events    enable row level security;
alter table consent_records enable row level security;
alter table dsr_requests    enable row level security;
alter table breach_log      enable row level security;

-- Users read only their own rows
create policy "users_own_profile" on user_profiles  for all    using (auth.uid() = id);
create policy "users_own_credits" on credit_ledger  for select using (auth.uid() = user_id);
create policy "users_own_orders"  on credit_orders  for select using (auth.uid() = user_id);
create policy "users_own_cache"   on analysis_cache for select using (auth.uid() = user_id);
create policy "users_own_usage"   on usage_events   for select using (auth.uid() = user_id);
create policy "users_own_consent" on consent_records for select using (auth.uid() = user_id);
create policy "users_own_dsr"     on dsr_requests   for select using (auth.uid() = user_id);

-- Backend (service_role) does everything
create policy "service_all_profiles" on user_profiles  for all to service_role using (true) with check (true);
create policy "service_all_credits"  on credit_ledger  for all to service_role using (true) with check (true);
create policy "service_all_orders"   on credit_orders  for all to service_role using (true) with check (true);
create policy "service_all_cache"    on analysis_cache for all to service_role using (true) with check (true);
create policy "service_all_usage"    on usage_events   for all to service_role using (true) with check (true);
create policy "service_all_consent"  on consent_records for all to service_role using (true) with check (true);
create policy "service_all_dsr"      on dsr_requests   for all to service_role using (true) with check (true);
create policy "service_all_breach"   on breach_log     for all to service_role using (true) with check (true);

-- ── 9. Credit balance view ──────────────────────────────────────────────────
create view user_credit_balances
with (security_invoker = true) as
select user_id, sum(delta) as balance
from credit_ledger
group by user_id;

-- ── 10. Auto-create profile on new signup ───────────────────────────────────
create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer set search_path = public
as $$
begin
  insert into public.user_profiles (id, email)
  values (new.id, new.email)
  on conflict (id) do nothing;
  return new;
end;
$$;

create trigger on_auth_user_created
  after insert on auth.users
  for each row execute procedure public.handle_new_user();

-- ═══════════════════════════════════════════════════════════════════════════
-- Done. Verify:
--   select * from user_credit_balances;
--   select * from retention_policy;
--   select data_category, retain_days from retention_policy;
-- Cost report:
--   select user_id, count(*) analyses, round(sum(total_cost_usd),4) spend_usd
--   from usage_events group by user_id order by spend_usd desc;
-- Consent check (after a signup):
--   select user_id, purpose, granted, source, created_at
--   from consent_records order by created_at desc limit 20;
-- ═══════════════════════════════════════════════════════════════════════════
