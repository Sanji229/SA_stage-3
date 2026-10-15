-- Preserve attendance records and display identity when a student is unenrolled or deleted.
alter table attendance_logs add column if not exists student_name text;
alter table attendance_logs add column if not exists student_status text not null default 'active'
  check (student_status in ('active','unenrolled','deleted'));

update attendance_logs l
set student_name = s.name
from students s
where l.student_id = s.student_id and l.student_name is null;
update attendance_logs
set student_name = case when student_id is null then 'Deleted student' else 'Student #' || student_id::text end
where student_name is null;
alter table attendance_logs alter column student_name set not null;

-- Retain the enrollment join key as nullable; student_name is the durable history label.
alter table attendance_logs alter column student_id drop not null;
alter table attendance_logs drop constraint if exists attendance_logs_student_id_fkey;
alter table attendance_logs add constraint attendance_logs_student_id_fkey
  foreign key (student_id) references students(student_id) on delete set null;

create or replace function preserve_deleted_student_attendance() returns trigger
language plpgsql as $$
begin
  update attendance_logs
  set student_name = coalesce(student_name, old.name),
      student_status = 'deleted',
      student_id = null
  where student_id = old.student_id;
  return old;
end;
$$;

drop trigger if exists preserve_deleted_student_attendance_trg on students;
create trigger preserve_deleted_student_attendance_trg
before delete on students
for each row execute function preserve_deleted_student_attendance();
