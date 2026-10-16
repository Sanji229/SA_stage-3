-- Migration 009: record biometric consent and retain attendance/marks in anonymized form.
-- Apply after migrations 002 through 008.

alter table students add column if not exists biometric_consent_at timestamptz;
alter table students add column if not exists biometric_consent_version text;

alter table marks add column if not exists student_name text;
update marks m set student_name = s.name from students s
where m.student_id = s.student_id and m.student_name is null;
update marks set student_name = 'Student #' || student_id::text where student_name is null;
alter table marks alter column student_name set not null;
alter table marks alter column student_id drop not null;
alter table marks drop constraint if exists marks_student_id_fkey;
alter table marks add constraint marks_student_id_fkey
  foreign key (student_id) references students(student_id) on delete set null;

alter table marks_audit add column if not exists student_name text;
update marks_audit a set student_name = s.name from students s
where a.student_id = s.student_id and a.student_name is null;
update marks_audit set student_name = 'Student #' || student_id::text where student_name is null;
alter table marks_audit alter column student_name set not null;
alter table marks_audit alter column student_id drop not null;

create or replace function marks_enforce_rules() returns trigger
language plpgsql set search_path = public, pg_temp as $$
declare mx numeric; subj bigint; v_name text;
begin
  -- The student deletion trigger nulls the FK while keeping this row as anonymous history.
  if new.student_id is null then
    new.updated_at := now();
    return new;
  end if;
  select max_marks, subject_id into mx, subj from assessments where assessment_id = new.assessment_id;
  if new.obtained > mx then
    raise exception 'marks (%) exceed the maximum (%) for this assessment', new.obtained, mx;
  end if;
  if not exists (select 1 from subject_students where subject_id = subj and student_id = new.student_id) then
    raise exception 'student % is not enrolled in the subject of this assessment', new.student_id;
  end if;
  select name into v_name from students where student_id = new.student_id;
  new.student_name := coalesce(v_name, new.student_name);
  new.updated_at := now();
  return new;
end $$;

create or replace function marks_write_audit() returns trigger
language plpgsql set search_path = public, pg_temp as $$
begin
  if tg_op = 'DELETE' then
    insert into marks_audit(assessment_id, student_id, student_name, action, old_obtained, changed_by)
    values (old.assessment_id, old.student_id, old.student_name, 'delete', old.obtained, old.updated_by);
  elsif tg_op = 'INSERT' then
    insert into marks_audit(assessment_id, student_id, student_name, action, new_obtained, changed_by)
    values (new.assessment_id, new.student_id, new.student_name, 'insert', new.obtained, new.updated_by);
  elsif new.obtained is distinct from old.obtained then
    insert into marks_audit(assessment_id, student_id, student_name, action, old_obtained, new_obtained, changed_by)
    values (new.assessment_id, new.student_id, new.student_name, 'update', old.obtained, new.obtained, new.updated_by);
  end if;
  return null;
end $$;

-- Keep all attendance and mark history while removing direct links to the deleted profile.
create or replace function preserve_deleted_student_attendance() returns trigger
language plpgsql set search_path = public, pg_temp as $$
begin
  update attendance_logs
  set student_name = 'Former student', student_status = 'deleted', student_id = null
  where student_id = old.student_id;
  update attendance_audit
  set student_name = 'Former student', student_id = null
  where student_id = old.student_id;
  update marks
  set student_name = 'Former student', student_id = null
  where student_id = old.student_id;
  update marks_audit
  set student_name = 'Former student', student_id = null
  where student_id = old.student_id;
  return old;
end $$;

drop trigger if exists preserve_deleted_student_attendance_trg on students;
create trigger preserve_deleted_student_attendance_trg
before delete on students for each row execute function preserve_deleted_student_attendance();

-- Route application deletion through a named RPC so older deployments cannot silently cascade
-- marks before the anonymization migration is installed.
create or replace function delete_student_profile_anonymized(p_student_id bigint) returns boolean
language plpgsql security definer set search_path = public, pg_temp as $$
begin
  delete from students where student_id = p_student_id;
  return found;
end $$;
revoke all on function delete_student_profile_anonymized(bigint) from public, anon, authenticated;
grant execute on function delete_student_profile_anonymized(bigint) to service_role;
