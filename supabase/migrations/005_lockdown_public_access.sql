-- Migration 005: make sure the PUBLIC (anon / authenticated) API roles can reach nothing.
-- SmartAttend talks to Supabase only from the server with the secret key (which bypasses RLS), and
-- performs authorisation in src/database/db.py. It never uses the public key.
--
-- Review first: do NOT run this if OTHER applications in the same Supabase project rely on the
-- anon/authenticated roles. Idempotent; changes privileges only, never data.

do $$
declare t text;
begin
  foreach t in array array[
    'teachers','students','subjects','subject_students','attendance_logs','attendance_audit',
    'assessments','marks','marks_audit','teacher_invitations','auth_attempts','app_settings'
  ] loop
    if to_regclass('public.' || t) is not null then
      execute format('alter table public.%I enable row level security', t);
      execute format('revoke all on table public.%I from anon, authenticated', t);
    end if;
  end loop;
end $$;

revoke all on all sequences in schema public from anon, authenticated;
revoke execute on all functions in schema public from anon, authenticated;
alter default privileges in schema public revoke all on tables    from anon, authenticated;
alter default privileges in schema public revoke all on sequences from anon, authenticated;
