"""Authorisation matrix (negative tests) and static checks on the SQL that protects the database."""
import base64
import json
import re
from pathlib import Path

import pytest

from src.database import db
from src.database.config import ConfigError, build_client, classify_key
from tests.helpers import fake_face

ROOT = Path(__file__).resolve().parent.parent
SQL_FILES = [ROOT / "supabase" / "schema.sql", *sorted((ROOT / "supabase" / "migrations").glob("*.sql"))]
ALL_SQL = "\n".join(f.read_text() for f in SQL_FILES)


# ----------------------------------------------------------------- which key is in use?
def jwt(role):
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{enc({'alg': 'HS256'})}.{enc({'role': role})}.sig"


@pytest.mark.parametrize("key,kind", [
    ("sb_secret_abc", "secret"), ("sb_publishable_abc", "public"), (jwt("service_role"), "secret"), (jwt("anon"), "public"),
    (jwt("authenticated"), "unknown"), ("garbage", "unknown"), ("", "unknown"), (None, "unknown"), ("a.b.c", "unknown"),
])
def test_key_classification(key, kind):
    assert classify_key(key) == kind


def test_app_refuses_to_start_with_a_public_key():
    for k in ("sb_publishable_abc", jwt("anon")):
        with pytest.raises(ConfigError, match="public"):
            build_client("https://x.supabase.co", k)


# ---------------------------------------------------------------- authorisation matrix
@pytest.fixture()
def world(fake_db):
    db.create_teacher("owner", "password-1", "Owner Teacher"); db.create_teacher("other", "password-2", "Other Teacher")
    o, x = (db.teacher_login(u, p)["teacher_id"] for u, p in (("owner", "password-1"), ("other", "password-2")))
    sub = db.create_subject("S1", "Subject", "A", o)[0]["subject_id"]
    stu = [db.create_student(n, fake_face(i))[0]["student_id"] for i, n in enumerate(["Al Roy", "Bo Lee"])]
    for s in stu:
        db.enroll_student_to_subject(s, sub)
    a = db.create_assessment(o, sub, "Mid-Semester 1", 20)["assessment_id"]
    ts = "2026-03-01T09:00:00+00:00"
    db.save_attendance_session([{"student_id": s, "subject_id": sub, "timestamp": ts, "is_present": True} for s in stu], teacher_id=o)
    db.import_marks(o, a, [{"student_id": stu[0], "obtained": 15}])
    return dict(o=o, x=x, sub=sub, stu=stu, a=a, ts=ts)


def sensitive_ops(w):
    """Every teacher-facing function that touches a subject/assessment, called as the WRONG teacher."""
    x, sub, a, s, ts = w["x"], w["sub"], w["a"], w["stu"], w["ts"]
    return {
        "enrolled students (incl. voice data)": lambda: db.get_enrolled_students(x, sub),
        "enrolled face rows (biometrics)": lambda: db.get_enrolled_face_rows(x, sub),
        "save attendance": lambda: db.save_attendance_session([{"student_id": s[0], "subject_id": sub, "timestamp": "2027-01-01T00:00:00+00:00", "is_present": True}], teacher_id=x),
        "session logs": lambda: db.get_session_logs(x, sub, ts),
        "correct attendance": lambda: db.correct_attendance(x, sub, ts, s[0], False, "forged correction"),
        "audit trail": lambda: db.get_audit_trail(x, sub),
        "create assessment": lambda: db.create_assessment(x, sub, "Injected", 10),
        "list assessments": lambda: db.list_assessments(x, sub),
        "publish marks": lambda: db.set_published(x, a, True),
        "read marks": lambda: db.get_marks(x, a),
        "import marks": lambda: db.import_marks(x, a, [{"student_id": s[0], "obtained": 1}]),
        "marks audit": lambda: db.get_marks_audit(x, a),
    }


def test_every_sensitive_operation_denies_a_teacher_who_does_not_own_the_subject(world):
    ops = sensitive_ops(world)
    assert len(ops) >= 12
    for name, op in ops.items():
        with pytest.raises(db.PermissionDenied):
            op()
    # nothing was changed by any denied attempt
    assert db.get_marks(world["o"], world["a"])[0]["obtained"] == 15
    assert not db.list_assessments(world["o"], world["sub"])[0]["published"]
    assert len(db.get_session_logs(world["o"], world["sub"], world["ts"])) == 2
    assert db.get_audit_trail(world["o"], world["sub"]) == []


def test_non_teacher_identities_are_denied_everywhere(world):
    for bogus in (None, 0, -1, 10**9, "1", 1.0, True, [1]):
        w = dict(world, x=bogus)
        for name, op in sensitive_ops(w).items():
            with pytest.raises((db.PermissionDenied, TypeError, db.DatabaseError)):
                op()


def test_unpublished_marks_stay_invisible_and_other_students_data_is_not_returned(world):
    a_id, b_id = world["stu"]
    assert db.get_student_marks(a_id) == [] and db.get_student_marks(b_id) == []   # draft
    db.set_published(world["o"], world["a"], True)
    assert [m["obtained"] for m in db.get_student_marks(a_id)] == [15]
    assert db.get_student_marks(b_id) == []                                        # has no mark; never sees Al's
    assert db.get_student_marks(999) == []
    for row in db.get_student_attendance(a_id):
        assert row["student_id"] == a_id
    for row in db.get_student_subjects(a_id):
        assert row["student_id"] == a_id and "face_embedding" not in str(row) and "voice_embedding" not in str(row)


def test_nothing_returned_to_the_ui_layer_contains_secrets_or_biometrics(world):
    blob = " ".join(str(x) for x in (
        db.teacher_login("owner", "password-1"), db.get_teacher_subjects(world["o"]), db.get_student(world["stu"][0]),
        db.get_attendance_for_teacher(world["o"]), db.get_marks(world["o"], world["a"]), db.get_student_marks(world["stu"][0]),
        db.get_student_subjects(world["stu"][0]), db.get_student_attendance(world["stu"][0]), db.get_session_logs(world["o"], world["sub"], world["ts"])))
    for forbidden in ("face_embedding", "voice_embedding", "$2b$", "password", "token_hash"):
        assert forbidden not in blob, forbidden


def test_students_cannot_be_enrolled_into_or_marked_for_subjects_they_do_not_belong_to(world):
    outsider = db.create_student("Outside Kid", fake_face(40))[0]["student_id"]
    with pytest.raises(db.DatabaseError, match="not enrolled"):
        db.save_attendance_session([{"student_id": outsider, "subject_id": world["sub"], "timestamp": "2027-02-02T00:00:00+00:00", "is_present": True}], teacher_id=world["o"])
    with pytest.raises(db.DatabaseError, match="not enrolled"):
        db.import_marks(world["o"], world["a"], [{"student_id": outsider, "obtained": 5}])


# --------------------------------------------------------------------- static SQL policy checks
def test_every_table_has_row_level_security_enabled():
    tables = set(re.findall(r"create table if not exists (\w+)", ALL_SQL))
    enabled = set(re.findall(r"alter table (\w+)\s+enable row level security", ALL_SQL))
    assert {"teachers", "students", "subjects", "attendance_logs", "marks", "teacher_invitations", "auth_attempts", "app_settings"} <= tables
    assert tables - enabled == set(), f"RLS not enabled for: {tables - enabled}"


def test_no_policy_opens_data_to_public_roles():
    assert not re.search(r"create\s+policy", ALL_SQL, re.I), "any policy needs a deliberate security review"
    assert not re.search(r"grant\s+.+\s+to\s+(anon|authenticated|public)", ALL_SQL, re.I)
    assert not re.search(r"using\s*\(\s*true\s*\)", ALL_SQL, re.I)


def test_lockdown_migration_revokes_public_access():
    sql = (ROOT / "supabase" / "migrations" / "005_lockdown_public_access.sql").read_text()
    assert "revoke all on table" in sql and "anon, authenticated" in sql and "enable row level security" in sql
    for t in ("teachers", "students", "marks", "teacher_invitations", "auth_attempts", "app_settings", "attendance_audit"):
        assert f"'{t}'" in sql


def test_migrations_are_additive_and_safe_to_rerun():
    for f in sorted((ROOT / "supabase" / "migrations").glob("*.sql")):
        body = re.sub(r"--.*", "", f.read_text())
        assert not re.search(r"\b(drop\s+table|drop\s+column|truncate|delete\s+from)\b", body, re.I), f.name
        for stmt in re.findall(r"create\s+table\s+(?!if not exists)", body, re.I):
            pytest.fail(f"{f.name}: create table without IF NOT EXISTS")
        for stmt in re.findall(r"add\s+column\s+(?!if not exists)", body, re.I):
            pytest.fail(f"{f.name}: add column without IF NOT EXISTS")


def test_critical_invariants_exist_in_the_database_layer():
    assert re.search(r"unique\s*\(subject_id, student_id, \"timestamp\"\)", ALL_SQL)       # no double attendance
    assert re.search(r"unique\s*\(assessment_id, student_id\)", ALL_SQL)                  # one mark per student
    assert "marks_enforce_rules" in ALL_SQL and "not enrolled in the subject" in ALL_SQL  # max marks + enrolment enforced by trigger
    assert "token_hash  text   not null unique" in ALL_SQL                                # invitations single row per token hash
