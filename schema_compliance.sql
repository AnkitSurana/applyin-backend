-- NOTE: For a fresh setup, run schema_reset.sql instead (one file, all tables).
-- This file is kept for reference. SUPERSEDED by schema_reset.sql.

-- ============================================================================
-- Applyin — Compliance schema (DPDP Act 2023 foundations)
-- Run AFTER schema.sql and schema_migration.sql.
-- Adds: consent records, data-subject-rights (DSR) requests, breach log,
-- retention config. RLS policies included; ENABLE RLS in the Supabase dashboard.
-- ============================================================================

-- ── 1. Consent records ──────────────────────────────────────────────────────
-- One row per consent decision per purpose. We never overwrite; withdrawal is a
-- new row with granted=false, so the full history is auditable (DPDP needs proof
-- of when/what was consented and withdrawn).
create table if not exists consent_records (
    id            uuid primary key default gen_random_uuid(),
    user_id       uuid not null,
    purpose       text not null,            -- e.g. 'resume_processing', 'analytics'
    granted       boolean not null,         -- true = given, false = withdrawn
    policy_version text not null,           -- which privacy notice version they saw
    source        text not null default 'web', -- 'web' | 'extension'
    ip_hash       text,                     -- salted hash, NOT raw IP
    user_agent    text,
    created_at    timestamptz not null default now()
);
create index if not exists idx_consent_user    on consent_records(user_id);
create index if not exists idx_consent_purpose on consent_records(user_id, purpose, created_at desc);

-- ── 2. Data-subject-rights requests ─────────────────────────────────────────
-- Access / correction / erasure / portability requests and their lifecycle.
create table if not exists dsr_requests (
    id            uuid primary key default gen_random_uuid(),
    user_id       uuid not null,
    kind          text not null,            -- 'access'|'correction'|'erasure'|'portability'
    status        text not null default 'received', -- received|in_progress|completed|rejected
    detail        text,                     -- free text from the user (e.g. what to correct)
    result_ref    text,                     -- where the export/result is, if any
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now(),
    completed_at  timestamptz,
    -- DPDP expects responses within a reasonable, stated period. Track a due date.
    due_at        timestamptz not null default (now() + interval '30 days')
);
create index if not exists idx_dsr_user   on dsr_requests(user_id);
create index if not exists idx_dsr_status on dsr_requests(status, due_at);

-- ── 3. Breach log ───────────────────────────────────────────────────────────
-- Internal record of suspected/confirmed incidents + notification tracking.
create table if not exists breach_log (
    id              uuid primary key default gen_random_uuid(),
    detected_at     timestamptz not null default now(),
    severity        text not null,          -- 'low'|'medium'|'high'|'critical'
    summary         text not null,
    affected_users  integer,
    data_categories text,                   -- e.g. 'email, resume metadata'
    status          text not null default 'open', -- open|contained|notified|closed
    board_notified_at timestamptz,          -- Data Protection Board (DPDP)
    users_notified_at timestamptz,
    notes           text,
    created_at      timestamptz not null default now()
);

-- ── 4. Retention config (single source of truth for purge jobs) ─────────────
create table if not exists retention_policy (
    data_category text primary key,         -- 'analysis_cache'|'usage_events'|...
    retain_days   integer not null,
    rationale     text,
    updated_at    timestamptz not null default now()
);

insert into retention_policy (data_category, retain_days, rationale) values
    ('analysis_cache', 1,   'Short-lived performance cache; no need to keep resume-derived results.'),
    ('usage_events',   365, 'Cost/operational analytics; 12 months then purge.'),
    ('dsr_requests',   2555,'Keep proof of handling rights requests (~7y) for accountability.'),
    ('consent_records',2555,'Keep consent/withdrawal history (~7y) as proof of lawful basis.'),
    ('breach_log',     2555,'Incident records retained long-term for accountability.')
on conflict (data_category) do nothing;

-- ── 5. RLS ──────────────────────────────────────────────────────────────────
alter table consent_records enable row level security;
alter table dsr_requests    enable row level security;
alter table breach_log      enable row level security;
alter table retention_policy enable row level security;

-- users can see their own consent + DSR rows
create policy "users_own_consent" on consent_records for select using (auth.uid() = user_id);
create policy "users_own_dsr"     on dsr_requests    for select using (auth.uid() = user_id);

-- service role does everything (backend uses the service key)
create policy "service_all_consent"   on consent_records  for all to service_role using (true) with check (true);
create policy "service_all_dsr"        on dsr_requests     for all to service_role using (true) with check (true);
create policy "service_all_breach"     on breach_log       for all to service_role using (true) with check (true);
create policy "service_all_retention"  on retention_policy for all to service_role using (true) with check (true);
-- breach_log and retention_policy have NO public-user select policy = users cannot read them.

-- ── 6. Retention purge (run on a schedule, e.g. Supabase cron / pg_cron) ─────
-- Example purge statements. Wrap in a scheduled job; review before enabling.
-- delete from analysis_cache where created_at < now() - interval '1 day';
-- delete from usage_events   where created_at < now() - interval '365 days';
