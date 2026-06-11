-- ═══════════════════════════════════════════════════════════════════════════
-- Applyin — Fresh Install Schema
-- Run in Supabase SQL Editor. Wipes and recreates everything.
-- ═══════════════════════════════════════════════════════════════════════════

-- ── Step 1: Drop everything ─────────────────────────────────────────────────
drop trigger  if exists on_auth_user_created on auth.users;
drop function if exists public.handle_new_user();
drop view     if exists user_credit_balances;
drop table    if exists usage_events    cascade;
drop table    if exists analysis_cache  cascade;
drop table    if exists credit_orders   cascade;
drop table    if exists credit_ledger   cascade;
drop table    if exists user_profiles   cascade;

-- ── Step 2: Tables ──────────────────────────────────────────────────────────
create table user_profiles (
  id          uuid primary key,
  email       text,
  full_name   text,
  created_at  timestamptz default now(),
  updated_at  timestamptz default now()
);

create table credit_ledger (
  id          bigserial primary key,
  user_id     uuid not null,
  delta       integer not null,
  reason      text not null,
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

create table analysis_cache (
  cache_key   text not null,
  user_id     uuid not null,
  result      jsonb not null,
  expires_at  timestamptz not null,
  created_at  timestamptz default now(),
  primary key (cache_key, user_id)
);
create index idx_cache_expires on analysis_cache(expires_at);

create table usage_events (
  id            bigserial primary key,
  user_id       uuid not null,
  job_title     text,
  company       text,
  match_score   integer,
  fit_level     text,
  had_resume    boolean default false,
  credits_used  integer default 1,
  created_at    timestamptz default now()
);
create index idx_usage_events_user on usage_events(user_id);

-- ── Step 3: Row Level Security ──────────────────────────────────────────────
alter table user_profiles  enable row level security;
alter table credit_ledger  enable row level security;
alter table credit_orders  enable row level security;
alter table analysis_cache enable row level security;
alter table usage_events   enable row level security;

-- Users read their own rows
create policy "users_own_profile" on user_profiles  for all    using (auth.uid() = id);
create policy "users_own_credits" on credit_ledger  for select using (auth.uid() = user_id);
create policy "users_own_orders"  on credit_orders  for select using (auth.uid() = user_id);
create policy "users_own_cache"   on analysis_cache for select using (auth.uid() = user_id);
create policy "users_own_usage"   on usage_events   for select using (auth.uid() = user_id);

-- Backend (service_role) bypasses RLS for all operations
create policy "service_all_profiles" on user_profiles  for all to service_role using (true) with check (true);
create policy "service_all_credits"  on credit_ledger  for all to service_role using (true) with check (true);
create policy "service_all_orders"   on credit_orders  for all to service_role using (true) with check (true);
create policy "service_all_cache"    on analysis_cache for all to service_role using (true) with check (true);
create policy "service_all_usage"    on usage_events   for all to service_role using (true) with check (true);

-- ── Step 4: Credit balance view ─────────────────────────────────────────────
create view user_credit_balances
with (security_invoker = true) as
select user_id, sum(delta) as balance
from credit_ledger
group by user_id;

-- ── Step 5: Auto-create profile on every new signup ─────────────────────────
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
-- Done. New signups auto-get a profile (trigger) + 3 credits (backend auth.py).
-- AFTER running this, run schema_migration.sql to add the metrics columns.
-- Verify: select * from user_credit_balances;
-- ═══════════════════════════════════════════════════════════════════════════
