-- Fitly Database Schema
-- Run this in your Supabase SQL editor

-- ── User profiles (extends Supabase auth.users) ──────────────────────────────
create table if not exists user_profiles (
  id          uuid primary key references auth.users(id) on delete cascade,
  email       text,
  full_name   text,
  created_at  timestamptz default now(),
  updated_at  timestamptz default now()
);

-- ── Credit ledger (append-only, sum = balance) ───────────────────────────────
-- Positive delta = credits added, negative = credits spent
create table if not exists credit_ledger (
  id          bigserial primary key,
  user_id     uuid references user_profiles(id) on delete cascade,
  delta       integer not null,            -- e.g. +20, -1
  reason      text not null,               -- 'signup_bonus', 'purchase', 'analysis', 'refund'
  meta        jsonb default '{}',
  created_at  timestamptz default now()
);

-- Fast balance queries
create index if not exists idx_credit_ledger_user on credit_ledger(user_id);

-- ── Credit orders (Razorpay) ─────────────────────────────────────────────────
create table if not exists credit_orders (
  id                    bigserial primary key,
  user_id               uuid references user_profiles(id),
  razorpay_order_id     text unique,
  razorpay_payment_id   text,
  package_id            text,
  credits               integer,
  amount                integer,          -- in paise (INR) or cents (USD)
  currency              text default 'INR',
  status                text default 'pending', -- pending | paid | failed
  created_at            timestamptz default now(),
  updated_at            timestamptz default now()
);

-- ── Analysis cache ───────────────────────────────────────────────────────────
create table if not exists analysis_cache (
  cache_key   text not null,
  user_id     uuid references user_profiles(id),
  result      jsonb not null,
  expires_at  timestamptz not null,
  created_at  timestamptz default now(),
  primary key (cache_key, user_id)
);

-- Auto-delete expired cache rows
create index if not exists idx_cache_expires on analysis_cache(expires_at);

-- ── Usage events (analytics) ─────────────────────────────────────────────────
create table if not exists usage_events (
  id            bigserial primary key,
  user_id       uuid references user_profiles(id),
  job_title     text,
  company       text,
  match_score   integer,
  fit_level     text,
  had_resume    boolean default false,
  credits_used  integer default 1,
  created_at    timestamptz default now()
);

-- ── Row Level Security ───────────────────────────────────────────────────────
alter table user_profiles    enable row level security;
alter table credit_ledger    enable row level security;
alter table credit_orders    enable row level security;
alter table analysis_cache   enable row level security;
alter table usage_events     enable row level security;

-- Users can only read their own data
-- The backend uses the service key which bypasses RLS

create policy "users_own_profile"    on user_profiles  for all using (auth.uid() = id);
create policy "users_own_credits"    on credit_ledger  for select using (auth.uid() = user_id);
create policy "users_own_orders"     on credit_orders  for select using (auth.uid() = user_id);
create policy "users_own_cache"      on analysis_cache for select using (auth.uid() = user_id);
create policy "users_own_usage"      on usage_events   for select using (auth.uid() = user_id);

-- ── Useful views ─────────────────────────────────────────────────────────────

-- Current credit balance per user
create or replace view user_credit_balances as
select user_id, sum(delta) as balance
from credit_ledger
group by user_id;

-- ── Auto-create user profile on signup (trigger) ─────────────────────────────
-- This ensures user_profiles row exists before any foreign key inserts
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

-- Drop if exists then recreate
drop trigger if exists on_auth_user_created on auth.users;

create trigger on_auth_user_created
  after insert on auth.users
  for each row execute procedure public.handle_new_user();
