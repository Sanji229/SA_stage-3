-- Migration 008: atomic attendance corrections and student-registration rate reservations.
-- Apply after migrations 002, 004, and 007. No attendance values are changed by this migration.

-- Keep correction history when a student profile is deleted. The attendance row and audit row
-- retain a display name while their live student foreign keys become NULL.
alter table attendance_audit add column if not exists student_name text;
update attendance_audit a
set student_name = s.name
from students s
where a.student_id = s.student_id and a.student_name is null;
update attendance_audit
set student_name = 'Student #' || student_id::text
where student_name is null;
alter table attendance_audit alter column student_name set not null;
alter table attendance_audit alter column student_id drop not null;
alter table attendance_audit drop constraint if exists attendance_audit_student_id_fkey;
alter table attendance_audit add constraint attendance_audit_student_id_fkey
  foreign key (student_id) references students(student_id) on delete set null;

create or replace function preserve_deleted_student_attendance() returns trigger
language plpgsql
set search_path = public, pg_temp
as $$
begin
  update attendance_logs
  set student_name = coalesce(student_name, old.name),
      student_status = 'deleted',
      student_id = null
  where student_id = old.student_id;

  update attendance_audit
  set student_name = coalesce(student_name, old.name),
      student_id = null
  where student_id = old.student_id;
  return old;
end;
$$;

create or replace function correct_attendance_transaction(
  p_teacher_id bigint,
  p_subject_id bigint,
  p_session_timestamp timestamptz,
  p_student_id bigint,
  p_new_present boolean,
  p_reason text
) returns boolean
language plpgsql
set search_path = public, pg_temp
as $$
declare
  v_old_present boolean;
  v_student_name text;
begin
  if p_reason is null or length(btrim(p_reason)) < 3 or length(p_reason) > 500 then
    raise exception 'A correction reason of 3 to 500 characters is required.' using errcode = '22023';
  end if;

  select l.is_present, l.student_name
    into v_old_present, v_student_name
  from attendance_logs l
  join subjects s on s.subject_id = l.subject_id
  join teachers t on t.teacher_id = p_teacher_id and t.status = 'active'
  where l.subject_id = p_subject_id
    and l."timestamp" = p_session_timestamp
    and l.student_id = p_student_id
    and s.teacher_id = p_teacher_id
  for update of l;

  if not found then
    raise exception 'Attendance record not found or permission denied.' using errcode = '42501';
  end if;

  if v_old_present = p_new_present then
    return false;
  end if;

  update attendance_logs
  set is_present = p_new_present, source = 'corrected'
  where subject_id = p_subject_id
    and "timestamp" = p_session_timestamp
    and student_id = p_student_id;

  insert into attendance_audit (
    subject_id, student_id, student_name, session_timestamp,
    old_present, new_present, reason, changed_by
  ) values (
    p_subject_id, p_student_id, v_student_name, p_session_timestamp,
    v_old_present, p_new_present, btrim(p_reason), p_teacher_id
  );

  return true;
end;
$$;

create or replace function reserve_student_registration_attempt(
  p_max_events integer,
  p_window_minutes integer
) returns boolean
language plpgsql
set search_path = public, pg_temp
as $$
declare
  v_attempts bigint;
  v_since timestamptz;
begin
  if p_max_events < 1 or p_window_minutes < 1 then
    raise exception 'Invalid registration rate-limit settings.' using errcode = '22023';
  end if;

  -- Serialize reservations across app instances so simultaneous requests cannot all pass the cap.
  perform pg_advisory_xact_lock(hashtext('smartattend:student-registration'), 0);
  v_since := now() - make_interval(mins => p_window_minutes);
  delete from auth_attempts
  where kind = 'student_register' and key = 'global' and attempted_at <= v_since;

  select count(*) into v_attempts
  from auth_attempts
  where kind = 'student_register' and key = 'global' and attempted_at > v_since;

  if v_attempts >= p_max_events then
    return false;
  end if;

  insert into auth_attempts(kind, key, success, attempted_at)
  values ('student_register', 'global', false, now());
  return true;
end;
$$;

revoke all on function correct_attendance_transaction(bigint, bigint, timestamptz, bigint, boolean, text)
  from public, anon, authenticated;
grant execute on function correct_attendance_transaction(bigint, bigint, timestamptz, bigint, boolean, text)
  to service_role;
revoke all on function reserve_student_registration_attempt(integer, integer)
  from public, anon, authenticated;
grant execute on function reserve_student_registration_attempt(integer, integer)
  to service_role;
