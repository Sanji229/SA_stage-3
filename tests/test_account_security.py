import re
from datetime import timedelta
from pathlib import Path

import pytest

from src import settings as cfg
from src.database import db

ROOT = Path(__file__).resolve().parent.parent
GOOD_PW = "correct-horse-9"


@pytest.fixture()
def world(fake_db):
    admin = db._insert_teacher("boss", GOOD_PW, "Boss Admin", role="admin")["teacher_id"]
    plain = db.create_teacher("plain", GOOD_PW, "Plain Teacher")[0]["teacher_id"]
    return fake_db, admin, plain


def register(token, username="newbie", **kw):
    return db.register_teacher_with_invite(username, "New Teacher", kw.get("pw", GOOD_PW), kw.get("pw", GOOD_PW), token)


# --------------------------------------------------------------------------- invitations
def test_only_an_active_admin_can_create_invitations(world):
    fake, admin, plain = world
    for who in (plain, 9999, None, True, "1", -1):
        with pytest.raises(db.PermissionDenied):
            db.create_invitation(who)
    assert db.create_invitation(admin, "teacher", 3, "x")
    db.set_teacher_status(admin, plain, "disabled")
    with pytest.raises(db.PermissionDenied):
        db.create_invitation(plain)


def test_token_is_never_stored_and_cannot_be_listed(world):
    fake, admin, _ = world
    token = db.create_invitation(admin, note="Dr Rao")
    assert all(token not in str(v) for row in fake.tables["teacher_invitations"] for v in row.values())
    listed = db.list_invitations(admin)
    assert listed[0]["status"] == "open" and "token_hash" not in listed[0] and token not in str(listed)
    with pytest.raises(db.PermissionDenied):
        db.list_invitations(world[2])


def test_invitation_is_single_use_and_creates_the_right_role(world):
    fake, admin, _ = world
    t = register(db.create_invitation(admin, "teacher"))
    assert t["role"] == "teacher" and "password" not in t
    token = db.create_invitation(admin, "teacher")
    register(token, "first")
    with pytest.raises(db.PermissionDenied, match="invalid, already used"):
        register(token, "second")                                              # reuse
    assert db.teacher_login("second", GOOD_PW) is None
    boss2 = register(db.create_invitation(admin, "admin"), "boss2")
    assert boss2["role"] == "admin"


def test_expired_revoked_wrong_and_short_tokens_are_rejected(world):
    fake, admin, _ = world
    expired = db.create_invitation(admin)
    fake.tables["teacher_invitations"][0]["expires_at"] = "2000-01-01T00:00:00+00:00"
    revoked = db.create_invitation(admin)
    inv = db.list_invitations(admin)
    assert db.revoke_invitation(admin, [i for i in inv if i["status"] == "open"][0]["invite_id"]) is True
    for bad in (expired, revoked, "x" * 40, "short", "", None):
        with pytest.raises(db.PermissionDenied):
            register(bad, "someone")
    assert not [t for t in fake.tables["teachers"] if t["username"] == "someone"]
    with pytest.raises(db.PermissionDenied):
        db.revoke_invitation(world[2], 1)


def test_a_username_clash_or_weak_password_does_not_burn_the_invitation(world):
    fake, admin, _ = world
    token = db.create_invitation(admin)
    with pytest.raises(db.DuplicateError):
        register(token, "PLAIN")                                                # case-insensitive clash
    with pytest.raises(db.InvalidInput):
        register(token, "ok_name", pw="short")
    assert fake.tables["teacher_invitations"][0]["used_at"] is None
    assert register(token, "ok_name")["username"] == "ok_name"


def test_invitation_is_released_if_account_creation_fails(world, monkeypatch):
    fake, admin, _ = world
    token = db.create_invitation(admin)
    monkeypatch.setattr(db, "_insert_teacher", lambda *a, **k: (_ for _ in ()).throw(db.DatabaseError("boom")))
    with pytest.raises(db.DatabaseError):
        register(token, "willfail")
    assert fake.tables["teacher_invitations"][0]["used_at"] is None             # can be retried


def test_invitation_bounds(world):
    _, admin, _ = world
    for days in (0, -1, cfg.INVITE_MAX_DAYS + 1):
        with pytest.raises(db.InvalidInput):
            db.create_invitation(admin, "teacher", days)
    with pytest.raises(db.InvalidInput):
        db.create_invitation(admin, "superuser", 3)


# ------------------------------------------------------------------------ first administrator
def test_first_admin_setup_is_guarded_and_works_once(fake_db, monkeypatch):
    args = ("root", "Root Admin", GOOD_PW, GOOD_PW)
    monkeypatch.setattr(cfg, "ADMIN_SETUP_TOKEN", None)
    assert db.admin_setup_available() is False
    with pytest.raises(db.PermissionDenied, match="not enabled"):
        db.bootstrap_admin(*args, "anything")
    monkeypatch.setattr(cfg, "ADMIN_SETUP_TOKEN", "setup-secret-123")
    assert db.admin_setup_available() is True
    with pytest.raises(db.PermissionDenied, match="Invalid setup token"):
        db.bootstrap_admin(*args, "wrong-guess")
    assert db.count_admins() == 0
    admin = db.bootstrap_admin(*args, "setup-secret-123")
    assert admin["role"] == "admin" and "password" not in admin
    assert db.admin_setup_available() is False
    with pytest.raises(db.PermissionDenied):                                    # token still valid, but setup is closed
        db.bootstrap_admin("root2", "Second Admin", GOOD_PW, GOOD_PW, "setup-secret-123")
    assert db.count_admins() == 1


def test_setup_claim_is_released_if_creation_fails(fake_db, monkeypatch):
    monkeypatch.setattr(cfg, "ADMIN_SETUP_TOKEN", "setup-secret-123")
    monkeypatch.setattr(db, "_insert_teacher", lambda *a, **k: (_ for _ in ()).throw(db.DatabaseError("boom")))
    with pytest.raises(db.DatabaseError):
        db.bootstrap_admin("root", "Root Admin", GOOD_PW, GOOD_PW, "setup-secret-123")
    assert fake_db.tables["app_settings"] == []                                 # retry possible


def test_setup_claim_blocks_a_concurrent_second_setup(fake_db, monkeypatch):
    monkeypatch.setattr(cfg, "ADMIN_SETUP_TOKEN", "setup-secret-123")
    fake_db.table("app_settings").insert({"key": "admin_bootstrap", "value": "x"}).execute()   # the other request won
    with pytest.raises(db.PermissionDenied, match="already complete"):
        db.bootstrap_admin("root", "Root Admin", GOOD_PW, GOOD_PW, "setup-secret-123")


# ------------------------------------------------------------------------- admin management
def test_admin_cannot_lock_everyone_out_and_non_admins_cannot_manage(world):
    fake, admin, plain = world
    with pytest.raises(db.PermissionDenied):
        db.set_teacher_status(admin, admin, "disabled")                        # not yourself
    with pytest.raises(db.PermissionDenied):
        db.set_teacher_status(plain, admin, "disabled")                        # not as a plain teacher
    with pytest.raises(db.PermissionDenied):
        db.list_teachers(plain)
    boss2 = db._insert_teacher("boss2", GOOD_PW, "Boss Two", role="admin")["teacher_id"]
    db.set_teacher_status(admin, boss2, "disabled")
    with pytest.raises(db.PermissionDenied):                                    # boss2 disabled -> cannot act
        db.list_teachers(boss2)
    db.set_teacher_status(admin, boss2, "active")
    db.set_teacher_status(boss2, admin, "disabled")
    with pytest.raises(db.PermissionDenied, match="last active administrator"):
        db.set_teacher_status(boss2, admin, "disabled")                        # would leave zero admins (admin already disabled -> boss2 is last)
    assert all("password" not in t for t in db.list_teachers(boss2))


# ------------------------------------------------------------------------------ rate limits
def test_login_lockout_applies_even_to_the_right_password_then_expires(world):
    fake, _, _ = world
    for _ in range(cfg.LOGIN_MAX_FAILURES):
        assert db.teacher_login("plain", "wrong-password-1") is None
    with pytest.raises(db.RateLimited):
        db.teacher_login("plain", GOOD_PW)                                      # locked, even with the correct password
    assert db.teacher_login("boss", GOOD_PW)["role"] == "admin"                 # other accounts unaffected
    for r in fake.tables["auth_attempts"]:
        r["attempted_at"] = "2000-01-01T00:00:00+00:00"                         # window passes
    assert db.teacher_login("plain", GOOD_PW)["username"] == "plain"


def test_unknown_usernames_are_throttled_too_and_look_identical(world):
    for _ in range(cfg.LOGIN_MAX_FAILURES):
        assert db.teacher_login("ghost", "whatever-123") is None
    with pytest.raises(db.RateLimited):
        db.teacher_login("ghost", "whatever-123")


def test_success_resets_failures(world):
    for _ in range(cfg.LOGIN_MAX_FAILURES - 1):
        db.teacher_login("plain", "wrong-password-1")
    assert db.teacher_login("plain", GOOD_PW)
    for _ in range(cfg.LOGIN_MAX_FAILURES - 1):
        db.teacher_login("plain", "wrong-password-1")
    assert db.teacher_login("plain", GOOD_PW)                                   # not locked: earlier failures were cleared


def test_invitation_guessing_is_rate_limited(world):
    for i in range(10):
        with pytest.raises(db.PermissionDenied):
            register("g" * 30 + str(i), f"user{i}")
    with pytest.raises(db.RateLimited):
        register("g" * 40, "user99")


def test_student_registration_has_a_global_rate_limit(fake_db, monkeypatch):
    from tests.helpers import fake_face
    monkeypatch.setattr(cfg, "STUDENT_REGISTER_PER_HOUR", 3)
    for i in range(3):
        db.register_student(f"Student {chr(65 + i)}x", fake_face(i))
    with pytest.raises(db.RateLimited):
        db.register_student("Another One", fake_face(50))


def test_rate_limiting_fails_open_but_logs_when_the_table_is_missing(fake_db, caplog):
    fake_db.missing_columns["auth_attempts"] = {"attempted_at"}
    db.check_throttle("teacher_login", "x", 1, 1)                               # must not raise or lock anyone out
    assert "rate limiting disabled" in caplog.text


# ---------------------------------------------------- disabled accounts and forged roles
def test_disabled_teacher_cannot_log_in_or_use_existing_ids(world):
    fake, admin, plain = world
    sub = db.create_subject("S1", "Subject", "A", plain)[0]["subject_id"]
    db.set_teacher_status(admin, plain, "disabled")
    assert db.teacher_login("plain", GOOD_PW) is None
    ops = [lambda: db.get_teacher_subjects(plain), lambda: db.create_subject("S2", "Other", "A", plain),
           lambda: db.get_enrolled_students(plain, sub), lambda: db.list_assessments(plain, sub),
           lambda: db.get_attendance_for_teacher(plain), lambda: db.create_assessment(plain, sub, "Mid", 10)]
    for op in ops:
        with pytest.raises(db.PermissionDenied):
            op()


def test_role_is_read_from_the_database_not_from_the_caller(world):
    _, admin, plain = world
    forged_session = {"teacher_id": plain, "role": "admin"}                     # what a tampered client would claim
    with pytest.raises(db.PermissionDenied):
        db.create_invitation(forged_session["teacher_id"])


def test_public_visitors_have_no_path_to_create_teachers():
    ui_files = list((ROOT / "src" / "screens").glob("*.py")) + list((ROOT / "src" / "components").glob("*.py")) + [ROOT / "app.py"]
    offenders = [f.name for f in ui_files if re.search(r"\b(create_teacher|_insert_teacher)\b", f.read_text())]
    assert offenders == []
