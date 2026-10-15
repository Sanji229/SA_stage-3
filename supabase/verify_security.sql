-- READ-ONLY checks. Run in the Supabase SQL editor after the migrations. Expected result is in each comment.

-- 1. Tables in public WITHOUT Row Level Security. EXPECT: no rows.
select schemaname, tablename from pg_tables where schemaname = 'public' and not rowsecurity;

-- 2. Anything granted to the public API roles. EXPECT: no rows.
select grantee, table_name, privilege_type
from information_schema.role_table_grants
where table_schema = 'public' and grantee in ('anon', 'authenticated');

-- 3. RLS policies. EXPECT: no rows (deny-all). Any row here deserves a careful review.
select tablename, policyname, roles, cmd, qual from pg_policies where schemaname = 'public';

-- 4. Views bypass RLS unless security_invoker is set. EXPECT: no rows, or only reviewed views.
select viewname from pg_views where schemaname = 'public';

-- 5. SECURITY DEFINER functions run with owner rights. EXPECT: no rows.
select p.proname from pg_proc p join pg_namespace n on n.oid = p.pronamespace
where n.nspname = 'public' and p.prosecdef;

-- 6. At least one active administrator exists. EXPECT: >= 1 after setup.
select count(*) as active_admins from teachers where role = 'admin' and status = 'active';

-- 7. Open (unused, unexpired) invitations. EXPECT: only ones you issued.
select invite_id, role, note, created_at, expires_at from teacher_invitations where used_at is null and expires_at > now();
