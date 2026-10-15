-- Migration 004: roles, invitations, rate-limit log, enrolment version. ADDITIVE ONLY.
-- Existing teachers keep working: they get role='teacher', status='active'.
-- To make an existing teacher the first administrator WITHOUT the web setup form, run once as DB owner:
--   update teachers set role = 'admin' where username = 'your_username';

-- Authoritative role/status live here, never in the browser/session.
alter table teachers add column if not exists role   text not null default 'teacher' check (role in ('teacher','admin'));
alter table teachers add column if not exists status text not null default 'active'  check (status in ('active','disabled'));

-- Single-use teacher invitations. New codes have expires_at NULL; legacy rows may expire.
-- Only a SHA-256 hash of the token is stored.
create table if not exists teacher_invitations (
  invite_id   bigint generated always as identity primary key,
  token_hash  text   not null unique,
  role        text   not null default 'teacher' check (role in ('teacher','admin')),
  note        text,
  created_by  bigint references teachers(teacher_id) on delete set null,
  created_at  timestamptz not null default now(),
  expires_at  timestamptz,
  used_at     timestamptz,
  used_by     bigint references teachers(teacher_id) on delete set null
);

-- Attempt log for rate limiting (login, invitation use, student registration, admin setup).
create table if not exists auth_attempts (
  attempt_id    bigint generated always as identity primary key,
  kind          text        not null,
  key           text        not null,
  success       boolean     not null,
  attempted_at  timestamptz not null default now()
);
create index if not exists auth_attempts_lookup_idx on auth_attempts(kind, key, attempted_at);

-- One-row-per-key settings; the unique key makes the first-admin setup atomic (exactly one winner).
create table if not exists app_settings (
  key         text primary key,
  value       text,
  created_at  timestamptz not null default now()
);

-- Usernames are compared case-insensitively by the app; enforce it too (skipped if duplicates exist).
do $$
begin
  if not exists (select 1 from teachers group by lower(username) having count(*) > 1) then
    create unique index if not exists teachers_username_lower_uq on teachers (lower(username));
  else
    raise notice 'Case-insensitive duplicate usernames exist; resolve them, then create teachers_username_lower_uq manually.';
  end if;
end $$;

-- Gallery version: bumps whenever a student's face embedding changes, so every app instance reloads it.
alter table students add column if not exists biometric_updated_at timestamptz not null default now();
create or replace function students_touch_biometric() returns trigger language plpgsql as $$
begin
  if new.face_embedding is distinct from old.face_embedding then
    new.biometric_updated_at := now();
  end if;
  return new;
end $$;
drop trigger if exists students_biometric_touch on students;
create trigger students_biometric_touch before update on students for each row execute function students_touch_biometric();

alter table teacher_invitations enable row level security;   -- no policies: server key only
alter table auth_attempts       enable row level security;
alter table app_settings        enable row level security;
