-- NOTE: For a fresh setup, run schema_reset.sql instead (one file, all tables).
-- This file is kept for reference. SUPERSEDED by schema_reset.sql.

-- ============================================================================
-- Applyin — Consent schema (standalone, idempotent)
-- ----------------------------------------------------------------------------
-- This is the table that stores user consent for resume processing, recorded
-- by the extension signup flow via POST /privacy/consent {purpose, granted,
-- source:'extension'}. Safe to run multiple times (IF NOT EXISTS guards).
-- It mirrors the consent_records definition in schema_compliance.sql; run
-- either file — they create the same table.
-- ============================================================================

create table if not exists consent_records (
    id             uuid primary key default gen_random_uuid(),
    user_id        uuid not null,
    purpose        text not null,                 -- 'resume_processing' | 'analytics' | 'marketing_email'
    granted        boolean not null,              -- true = consent given, false = withdrawn
    policy_version text not null,                 -- privacy notice version the user saw
    source         text not null default 'web',   -- 'web' | 'extension'
    ip_hash        text,                          -- salted hash of IP, never the raw IP
    user_agent     text,
    created_at     timestamptz not null default now()
);

-- Fast lookups: "latest consent for this user + purpose" (used by enforcement).
create index if not exists idx_consent_user    on consent_records(user_id);
create index if not exists idx_consent_purpose on consent_records(user_id, purpose, created_at desc);

-- Row Level Security: a user can read only their own consent rows; the backend
-- service role can do everything. ENABLE RLS in the Supabase dashboard too.
alter table consent_records enable row level security;

drop policy if exists "users_own_consent" on consent_records;
create policy "users_own_consent"
    on consent_records for select
    using (auth.uid() = user_id);

drop policy if exists "service_all_consent" on consent_records;
create policy "service_all_consent"
    on consent_records for all
    to service_role
    using (true) with check (true);

-- Verify what was recorded (run after a test signup):
--   select user_id, purpose, granted, source, created_at
--   from consent_records order by created_at desc limit 20;
