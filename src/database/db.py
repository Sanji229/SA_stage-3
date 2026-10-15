"""All database access lives here. UI code must not talk to Supabase directly."""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Optional

import bcrypt

from src import settings as cfg
from src.database.config import ConfigError, get_supabase
from src.services import biometrics
from src.validation import ValidationError, clean_person_name, clean_reason, clean_subject_fields

log = logging.getLogger("smartattend.db")

MAX_PASSWORD_BYTES = 72  # bcrypt silently truncates beyond this
MIN_PASSWORD_LEN = 8
USERNAME_RE = re.compile(r"^[a-z0-9_.-]{3,32}$")


class DatabaseError(RuntimeError):
    """A database operation failed. The message is safe to show to users."""


class InvalidInput(DatabaseError):
    """Input failed server-side validation (message is safe to show)."""


class RateLimited(DatabaseError):
    """Too many attempts; try again later."""


def _valid(fn: Callable, *args, **kwargs):
    """Run a validator and convert ValidationError into InvalidInput (what the UI already catches)."""
    try:
        return fn(*args, **kwargs)
    except ValidationError as exc:
        raise InvalidInput(str(exc)) from None


class DuplicateError(DatabaseError):
    """A uniqueness constraint was violated."""


def _is_unique_violation(exc: Exception) -> bool:
    return getattr(exc, "code", None) == "23505" or "23505" in str(exc) or "duplicate key" in str(exc).lower()


def _run(builder, action: str) -> list[dict[str, Any]]:
    try:
        return builder.execute().data or []
    except ConfigError:
        raise
    except Exception as exc:  # noqa: BLE001 - convert to a user-safe error
        if _is_unique_violation(exc):
            raise DuplicateError(f"Could not {action}: that value already exists.") from exc
        log.exception("Database error while trying to %s", action)
        raise DatabaseError(f"Could not {action}. Please try again.") from exc


def _t(name: str):
    try:
        return get_supabase().table(name)
    except ConfigError:
        raise
    except Exception as exc:  # noqa: BLE001 - never leak client internals to the UI
        log.exception("Could not open table %s", name)
        raise DatabaseError("The database is currently unavailable. Please try again.") from exc


# --------------------------------------------------------------------- passwords
def hash_pass(pwd: str) -> str:
    return bcrypt.hashpw(pwd.encode(), bcrypt.gensalt()).decode()


def check_pass(pwd: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pwd.encode(), hashed.encode())
    except ValueError:
        return False


def normalize_username(username: str) -> str:
    return (username or "").strip().lower()


def validate_registration(username: str, name: str, password: str, confirm: str) -> Optional[str]:
    """Return an error message, or None when the input is valid."""
    if not username or not name.strip() or not password:
        return "All fields are required."
    if not USERNAME_RE.match(normalize_username(username)):
        return "Username must be 3-32 characters: letters, numbers, dot, dash or underscore."
    if len(password) < MIN_PASSWORD_LEN:
        return f"Password must be at least {MIN_PASSWORD_LEN} characters."
    if len(password.encode()) > MAX_PASSWORD_BYTES:
        return "Password is too long (max 72 bytes)."
    if password != confirm:
        return "Passwords do not match."
    return None


# ---------------------------------------------------------------------- teachers
def check_teacher_exists(username: str) -> bool:
    rows = _run(_t("teachers").select("username").eq("username", normalize_username(username)), "check the username")
    return len(rows) > 0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


# ------------------------------------------------------------------ abuse protection
def check_throttle(kind: str, key: str, max_events: int, window_minutes: int, count_success: bool = False, now: datetime | None = None) -> None:
    """Raise RateLimited if `max_events` matching attempts happened inside the window.

    Stored in the database (auth_attempts) so it holds across Streamlit sessions and server
    restarts. Fails OPEN (with a warning) if the table is missing so a missing migration cannot lock
    everyone out; scripts/check_supabase_security.py reports that condition.
    """
    since = _iso((now or _now()) - timedelta(minutes=window_minutes))
    try:
        rows = _run(
            _t("auth_attempts").select("attempted_at").eq("kind", kind).eq("key", key).eq("success", count_success).gt("attempted_at", since),
            "check rate limits",
        )
    except DatabaseError:
        log.warning("auth_attempts table unavailable; rate limiting disabled (apply migration 004)")
        return
    if len(rows) >= max_events:
        raise RateLimited(f"Too many attempts. Please try again in about {window_minutes} minutes.")


def record_attempt(kind: str, key: str, success: bool, now: datetime | None = None) -> None:
    try:
        _run(_t("auth_attempts").insert({"kind": kind, "key": key, "success": bool(success), "attempted_at": _iso(now or _now())}), "record an attempt")
        if success and kind in ("teacher_login",):  # a good login clears earlier failures for this key
            _run(_t("auth_attempts").delete().eq("kind", kind).eq("key", key).eq("success", False), "reset attempts")
    except DatabaseError:
        log.warning("could not record auth attempt (apply migration 004)")


# --------------------------------------------------------------------- authorisation
_DUMMY_HASH: str | None = None


def _dummy_check(password: str) -> None:
    """Spend the same time as a real bcrypt check so unknown usernames are not distinguishable."""
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_pass(secrets.token_urlsafe(12))
    check_pass(password, _DUMMY_HASH)


def _teacher_row(teacher_id: int) -> dict | None:
    """Authoritative role/status, re-read from the database on every sensitive call."""
    try:
        rows = _run(_t("teachers").select("teacher_id, role, status").eq("teacher_id", teacher_id), "check permissions")
    except DatabaseError as exc:
        if exc.__cause__ is not None and _is_missing_column(exc.__cause__):  # un-migrated database
            rows = _run(_t("teachers").select("teacher_id").eq("teacher_id", teacher_id), "check permissions")
            return {"teacher_id": teacher_id, "role": "teacher", "status": "active"} if rows else None
        raise
    return rows[0] if rows else None


def require_active_teacher(teacher_id) -> dict:
    row = _teacher_row(teacher_id) if isinstance(teacher_id, int) and not isinstance(teacher_id, bool) else None
    if not row or row.get("status", "active") != "active":
        raise PermissionDenied("Your account is not active. Please contact an administrator.")
    return row


def require_admin(teacher_id) -> dict:
    row = require_active_teacher(teacher_id)
    if row.get("role") != "admin":
        raise PermissionDenied("Administrator access is required.")
    return row


# ------------------------------------------------------------------------ accounts
def _insert_teacher(username: str, password: str, name: str, role: str = "teacher") -> dict:
    data = {"username": normalize_username(username), "password": hash_pass(password), "name": name.strip()}
    if role != "teacher":
        data["role"] = role
    try:
        return _run(_t("teachers").insert(data), "create the account")[0]
    except DuplicateError as exc:
        raise DuplicateError("That username is already taken.") from exc


def create_teacher(username: str, password: str, name: str):
    """OPERATOR/TEST USE ONLY: creates a teacher without an invitation.

    The web UI must never call this (tests/test_account_security.py enforces it). Public visitors
    get teacher access only through register_teacher_with_invite().
    """
    return [_insert_teacher(username, password, name)]


def teacher_login(username: str, password: str) -> Optional[dict]:
    key = normalize_username(username)
    check_throttle("teacher_login", key, cfg.LOGIN_MAX_FAILURES, cfg.LOGIN_WINDOW_MINUTES)
    rows = _run(_t("teachers").select("*").eq("username", key), "sign in")
    ok = False
    if rows:
        ok = check_pass(password, rows[0]["password"]) and rows[0].get("status", "active") == "active"
    else:
        _dummy_check(password)
    record_attempt("teacher_login", key, ok)
    if not ok:
        return None
    teacher = dict(rows[0])
    teacher.pop("password", None)  # never keep the hash in session state
    teacher.setdefault("role", "teacher")
    return teacher


def _token_hash(token: str) -> str:
    return hashlib.sha256((token or "").strip().encode()).hexdigest()


def create_invitation(admin_id: int, role: str = "teacher", days: int = 3, note: str = "") -> str:
    """Admin only. Returns the one-time token (shown once; only its hash is stored)."""
    require_admin(admin_id)
    if role not in ("teacher", "admin"):
        raise InvalidInput("Invalid role.")
    if not (1 <= int(days) <= cfg.INVITE_MAX_DAYS):
        raise InvalidInput(f"Invitations must expire within 1-{cfg.INVITE_MAX_DAYS} days.")
    token = secrets.token_urlsafe(24)
    _run(_t("teacher_invitations").insert({
        "token_hash": _token_hash(token), "role": role, "note": " ".join(str(note or "").split())[:100],
        "created_by": admin_id, "expires_at": _iso(_now() + timedelta(days=int(days))),
    }), "create the invitation")
    return token


def list_invitations(admin_id: int) -> list[dict]:
    require_admin(admin_id)
    rows = _run(_t("teacher_invitations").select("invite_id, role, note, created_at, expires_at, used_at"), "load invitations")
    now = _iso(_now())
    for r in rows:  # the token hash is never returned to the UI
        r["status"] = "used" if r.get("used_at") else ("expired" if str(r["expires_at"]) <= now else "open")
    return rows


def revoke_invitation(admin_id: int, invite_id: int) -> bool:
    require_admin(admin_id)
    rows = _run(_t("teacher_invitations").update({"expires_at": _iso(_now() - timedelta(seconds=1))}).eq("invite_id", invite_id).is_("used_at", "null"), "revoke the invitation")
    return bool(rows)


def register_teacher_with_invite(username: str, name: str, password: str, confirm: str, token: str) -> dict:
    """The ONLY public path to a teacher account: a valid, unexpired, single-use invitation."""
    err = validate_registration(username, name, password, confirm)
    if err:
        raise InvalidInput(err)
    name = _valid(clean_person_name, name, "Full name")
    check_throttle("teacher_register", "global", 10, 60)
    h = _token_hash(token)
    now = _now()
    bad = PermissionDenied("This invitation is invalid, already used, or has expired.")
    if len((token or "").strip()) < 20:
        record_attempt("teacher_register", "global", False)
        raise bad
    invite = _run(_t("teacher_invitations").select("invite_id, role").eq("token_hash", h), "check the invitation")
    if not invite:
        record_attempt("teacher_register", "global", False)
        raise bad
    if check_teacher_exists(username):  # do not burn the invitation on a username clash
        raise DuplicateError("That username is already taken.")
    # Atomic claim: only one concurrent request can flip used_at from NULL.
    claimed = _run(
        _t("teacher_invitations").update({"used_at": _iso(now)}).eq("token_hash", h).is_("used_at", "null").gt("expires_at", _iso(now)),
        "use the invitation",
    )
    if not claimed:
        record_attempt("teacher_register", "global", False)
        raise bad
    try:
        teacher = _insert_teacher(username, password, name, role=claimed[0].get("role", "teacher"))
    except Exception:
        _run(_t("teacher_invitations").update({"used_at": None}).eq("token_hash", h), "release the invitation")
        raise
    try:
        _run(_t("teacher_invitations").update({"used_by": teacher["teacher_id"]}).eq("token_hash", h), "link the invitation")
    except DatabaseError:
        log.warning("could not link invitation to the new account")
    record_attempt("teacher_register", "global", True)
    teacher.pop("password", None)
    teacher.setdefault("role", "teacher")
    return teacher


def count_admins() -> int:
    try:
        return len(_run(_t("teachers").select("teacher_id").eq("role", "admin"), "check administrators"))
    except DatabaseError as exc:
        if exc.__cause__ is not None and _is_missing_column(exc.__cause__):
            return 0
        raise


def admin_setup_available() -> bool:
    """True only while no administrator exists AND a setup token is configured."""
    return bool(cfg.ADMIN_SETUP_TOKEN) and count_admins() == 0


def bootstrap_admin(username: str, name: str, password: str, confirm: str, setup_token: str) -> dict:
    """Create the very first administrator. Works once: guarded by a secret and a unique DB claim."""
    err = validate_registration(username, name, password, confirm)
    if err:
        raise InvalidInput(err)
    name = _valid(clean_person_name, name, "Full name")
    expected = cfg.ADMIN_SETUP_TOKEN
    if not expected:
        raise PermissionDenied("Initial administrator setup is not enabled on this server.")
    check_throttle("admin_setup", "global", 5, 60)
    if not hmac.compare_digest(str(setup_token or "").encode(), str(expected).encode()):
        record_attempt("admin_setup", "global", False)
        raise PermissionDenied("Invalid setup token.")
    if count_admins() > 0:
        raise PermissionDenied("Setup is already complete: an administrator exists.")
    try:  # unique key => exactly one concurrent request wins
        _run(_t("app_settings").insert({"key": "admin_bootstrap", "value": _iso(_now())}), "start setup")
    except DuplicateError:
        raise PermissionDenied("Setup is already complete.") from None
    try:
        teacher = _insert_teacher(username, password, name, role="admin")
    except Exception:
        _run(_t("app_settings").delete().eq("key", "admin_bootstrap"), "release setup")
        raise
    teacher.pop("password", None)
    return teacher


def list_teachers(admin_id: int) -> list[dict]:
    require_admin(admin_id)
    rows = _run(_t("teachers").select("teacher_id, username, name, role, status"), "load accounts")
    for r in rows:
        r.setdefault("role", "teacher"); r.setdefault("status", "active")
    return sorted(rows, key=lambda r: r["username"])


def set_teacher_status(admin_id: int, teacher_id: int, status: str) -> None:
    require_admin(admin_id)
    if status not in ("active", "disabled"):
        raise InvalidInput("Invalid status.")
    if teacher_id == admin_id:
        raise PermissionDenied("You cannot change your own status.")
    target = _teacher_row(teacher_id)
    if not target:
        raise InvalidInput("That account does not exist.")
    if status == "disabled" and target.get("role") == "admin":
        active_admins = [t for t in list_teachers(admin_id) if t["role"] == "admin" and t["status"] == "active"]
        if len(active_admins) <= 1:
            raise PermissionDenied("You cannot disable the last active administrator.")
    _run(_t("teachers").update({"status": status}).eq("teacher_id", teacher_id), "update the account")


# ---------------------------------------------------------------------- students
def get_face_gallery() -> list[dict]:
    """Minimal rows needed for face matching (no voice data, no extra columns)."""
    rows = _run(_t("students").select("student_id, face_embedding"), "load enrolled faces")
    return [r for r in rows if r.get("face_embedding")]


_GALLERY_HAS_TS: bool | None = None


def get_gallery_version() -> str:
    """Cheap fingerprint of the enrolled faces (ids + biometric_updated_at).

    Changes whenever a student is added/removed or a face is re-enrolled (a DB trigger from migration
    004 bumps biometric_updated_at). Used as the cache key so every process/instance sees new
    enrolments immediately, with no retraining and no fixed-TTL staleness. Without the column the
    version covers adds/removals only and the cache TTL bounds staleness.
    """
    global _GALLERY_HAS_TS
    rows = None
    if _GALLERY_HAS_TS is not False:
        try:
            rows = _run(_t("students").select("student_id, biometric_updated_at"), "check enrolment version")
            _GALLERY_HAS_TS = True
        except DatabaseError as exc:
            if exc.__cause__ is None or not _is_missing_column(exc.__cause__):
                raise
            _GALLERY_HAS_TS = False
    if rows is None:
        rows = _run(_t("students").select("student_id"), "check enrolment version")
    payload = "|".join(f"{r['student_id']}:{r.get('biometric_updated_at') or ''}" for r in sorted(rows, key=lambda r: r["student_id"]))
    return hashlib.sha1(payload.encode()).hexdigest()[:12]


def get_student(student_id: int) -> Optional[dict]:
    rows = _run(_t("students").select("student_id, name").eq("student_id", student_id), "load the student")
    return rows[0] if rows else None


def update_student_voice_embedding(student_id: int, voice_embedding) -> None:
    """Save a student's voice profile after validating its shape and values."""
    try:
        voice = biometrics.clean_embedding(voice_embedding, biometrics.VOICE_DIM, "Voice")
    except ValueError as exc:
        raise InvalidInput(f"Voice enrolment failed: {exc}") from None
    _run(
        _t("students").update({"voice_embedding": voice}).eq("student_id", int(student_id)),
        "save your voice profile",
    )


def create_student(new_name: str, face_embedding=None, voice_embedding=None):
    """Create a student profile. All checks run here, whatever the caller (UI or script) did.

    * name is validated; a valid 128-d face embedding is REQUIRED (no half-registered profiles)
    * a voice embedding is optional but, if given, must be a valid 256-d vector
    * a face that is already registered is rejected (even when two requests race)
    """
    name = _valid(clean_person_name, new_name, "Name")
    try:
        face = biometrics.clean_embedding(face_embedding, biometrics.FACE_DIM, "Face")
        voice = biometrics.clean_embedding(voice_embedding, biometrics.VOICE_DIM, "Voice") if voice_embedding is not None else None
    except ValueError as exc:
        raise InvalidInput(f"Enrolment failed: {exc}") from None
    dup_msg = ("This face is already registered. If it is you, sign in with your face; "
               "otherwise ask your teacher to check the enrolment.")
    if biometrics.find_face_conflicts(face, get_face_gallery(), cfg.FACE_MATCH_THRESHOLD):
        raise DuplicateError(dup_msg)
    rows = _run(_t("students").insert({"name": name, "face_embedding": face, "voice_embedding": voice}), "create the student profile")
    new_id = rows[0]["student_id"]
    # Race guard: if another registration of the same face landed first (lower id), keep the older one.
    older = [r for r in get_face_gallery() if r["student_id"] < new_id]
    if biometrics.find_face_conflicts(face, older, cfg.FACE_MATCH_THRESHOLD):
        _run(_t("students").delete().eq("student_id", new_id), "undo a duplicate registration")
        raise DuplicateError(dup_msg)
    return rows


def register_student(name: str, face_embedding, voice_embedding=None):
    """Public registration entry point: create_student + a global rate limit on new profiles."""
    check_throttle("student_register", "global", cfg.STUDENT_REGISTER_PER_HOUR, 60, count_success=True)
    rows = create_student(name, face_embedding, voice_embedding)
    record_attempt("student_register", "global", True)
    return rows


# ---------------------------------------------------------------------- subjects
def normalize_code(code: str) -> str:
    return re.sub(r"\s+", "", (code or "")).upper()


def create_subject(subject_code: str, name: str, section: str, teacher_id: int):
    require_active_teacher(teacher_id)
    code, name, section = _valid(clean_subject_fields, subject_code, name, section)
    data = {"subject_code": code, "name": name, "section": section, "teacher_id": teacher_id}
    try:
        return _run(_t("subjects").insert(data), "create the subject")
    except DuplicateError as exc:
        raise DuplicateError("That subject code is already in use. Choose a different one.") from exc


def update_subject(teacher_id: int, subject_id: int, subject_code: str, name: str, section: str) -> dict:
    """Update a subject owned by the active teacher."""
    require_active_teacher(teacher_id)
    assert_teacher_owns_subject(teacher_id, subject_id)
    code, name, section = _valid(clean_subject_fields, subject_code, name, section)
    try:
        rows = _run(
            _t("subjects").update({"subject_code": code, "name": name, "section": section})
            .eq("subject_id", subject_id).eq("teacher_id", teacher_id),
            "update the subject",
        )
    except DuplicateError as exc:
        raise DuplicateError("That subject code is already in use. Choose a different one.") from exc
    if not rows:
        raise DatabaseError("Subject not found or you do not have permission to update it.")
    return rows[0]


def delete_subject(teacher_id: int, subject_id: int) -> None:
    """Delete an owned subject. Related enrollments, attendance and assessments cascade."""
    require_active_teacher(teacher_id)
    assert_teacher_owns_subject(teacher_id, subject_id)
    rows = _run(
        _t("subjects").delete().eq("subject_id", subject_id).eq("teacher_id", teacher_id),
        "delete the subject",
    )
    if not rows:
        raise DatabaseError("Subject not found or you do not have permission to delete it.")


def get_subject_by_code(code: str) -> Optional[dict]:
    try:
        _valid(clean_subject_fields, code, "xx", "x")  # format check only
    except InvalidInput:
        return None
    rows = _run(
        _t("subjects").select("subject_id, name, subject_code, section").eq("subject_code", normalize_code(code)),
        "look up the subject",
    )
    return rows[0] if rows else None


def get_teacher_subjects(teacher_id: int) -> list[dict]:
    require_active_teacher(teacher_id)
    subjects = _run(
        _t("subjects")
        .select("*, subject_students(count), attendance_logs(timestamp)")
        .eq("teacher_id", teacher_id),
        "load your subjects",
    )
    for sub in subjects:
        enrolled = sub.pop("subject_students", None) or []
        sub["total_students"] = enrolled[0].get("count", 0) if enrolled else 0
        logs = sub.pop("attendance_logs", None) or []
        sub["total_classes"] = len({log_["timestamp"] for log_ in logs})
    return subjects


# -------------------------------------------------------------------- enrollment
def is_enrolled(student_id: int, subject_id: int) -> bool:
    rows = _run(
        _t("subject_students").select("student_id").eq("student_id", student_id).eq("subject_id", subject_id),
        "check enrollment",
    )
    return bool(rows)


def enroll_student_to_subject(student_id: int, subject_id: int) -> bool:
    """Idempotent. Returns True if newly enrolled, False if already enrolled."""
    if is_enrolled(student_id, subject_id):
        return False
    try:
        _run(_t("subject_students").insert({"student_id": student_id, "subject_id": subject_id}), "enroll")
    except DuplicateError:
        return False  # lost a race with another request; still enrolled
    return True


def unenroll_student_to_subject(student_id: int, subject_id: int):
    return _run(
        _t("subject_students").delete().eq("student_id", student_id).eq("subject_id", subject_id), "unenroll"
    )


def _enrolled_students(subject_id: int) -> list[dict]:
    """INTERNAL: no authorisation. Never expose directly to UI code."""
    rows = _run(
        _t("subject_students").select("student_id, students(student_id, name, voice_embedding)").eq("subject_id", subject_id),
        "load enrolled students",
    )
    return [r["students"] for r in rows if r.get("students")]


def get_enrolled_students(teacher_id: int, subject_id: int) -> list[dict]:
    """Class list (incl. voice embeddings used for matching), only for the teacher who owns the subject."""
    assert_teacher_owns_subject(teacher_id, subject_id)
    return _enrolled_students(subject_id)


def get_student_subjects(student_id: int):
    return _run(_t("subject_students").select("*, subjects(*)").eq("student_id", student_id), "load your subjects")


# -------------------------------------------------------------------- attendance
def get_student_attendance(student_id: int):
    return _run(_t("attendance_logs").select("*, subjects(*)").eq("student_id", student_id), "load your attendance")


class PermissionDenied(DatabaseError):
    """The signed-in user may not perform this operation."""


def assert_teacher_owns_subject(teacher_id: int, subject_id: int) -> dict:
    """Server-side authorisation: enforced here, not just by hiding buttons in the UI."""
    require_active_teacher(teacher_id)
    rows = _run(_t("subjects").select("subject_id, name, teacher_id").eq("subject_id", subject_id), "check permissions")
    if not rows or rows[0]["teacher_id"] != teacher_id:
        raise PermissionDenied("You do not have permission to manage this subject.")
    return rows[0]


def get_enrolled_face_rows(teacher_id: int, subject_id: int) -> list[dict]:
    """Enrolled students with stored face embedding (data-quality checks only); owner-only."""
    assert_teacher_owns_subject(teacher_id, subject_id)
    rows = _run(
        _t("subject_students").select("student_id, students(student_id, name, face_embedding)").eq("subject_id", subject_id),
        "load enrolled students",
    )
    return [r["students"] for r in rows if r.get("students")]


_EXTENDED_COLS = ("source", "match_score", "method", "marked_by")


def _is_missing_column(exc: Exception) -> bool:
    return getattr(exc, "code", None) in ("PGRST204", "42703") or "schema cache" in str(exc) or "does not exist" in str(exc)


def save_attendance_session(logs: Iterable[dict], teacher_id: int) -> int:
    """Persist one attendance session. Safe to call twice with the same logs.

    Server-side invariants (not dependent on the UI):
      * every row is for the same subject and timestamp (one session)
      * the active teacher owns the subject (teacher_id is required)
      * every student is currently enrolled in the subject
      * a student appears at most once; repeated saves are a no-op
    The DB unique constraint (subject_id, student_id, timestamp) is the final guard against races.
    Works on databases without migration 002: extended columns are dropped on a schema error.
    """
    logs = [dict(l) for l in logs]
    if not logs:
        return 0
    subject_id, ts = logs[0]["subject_id"], logs[0]["timestamp"]
    if any(l["subject_id"] != subject_id or l["timestamp"] != ts for l in logs):
        raise DatabaseError("Attendance rows must belong to a single session.")
    ids = [l["student_id"] for l in logs]
    if len(set(ids)) != len(ids):
        raise DatabaseError("A student appears more than once in this session.")
    assert_teacher_owns_subject(teacher_id, subject_id)      # mandatory: authorisation is never optional
    enrolled = {s["student_id"] for s in _enrolled_students(subject_id)}
    outsiders = set(ids) - enrolled
    if outsiders:
        raise DatabaseError("Some students are not enrolled in this subject; refresh the page and try again.")
    existing = _run(
        _t("attendance_logs").select("student_id").eq("subject_id", subject_id).eq("timestamp", ts),
        "check for an existing session",
    )
    if existing:
        return 0
    for l in logs:
        l.setdefault("marked_by", teacher_id)
    try:
        return len(_run(_t("attendance_logs").insert(logs), "save attendance"))
    except DuplicateError:
        return 0
    except DatabaseError as exc:
        if not isinstance(exc.__cause__, Exception) or not _is_missing_column(exc.__cause__):
            raise
        log.warning("attendance_logs lacks migration-002 columns; saving legacy columns only")
        legacy = [{k: v for k, v in l.items() if k not in _EXTENDED_COLS} for l in logs]
        try:
            return len(_run(_t("attendance_logs").insert(legacy), "save attendance"))
        except DuplicateError:
            return 0


def get_session_logs(teacher_id: int, subject_id: int, timestamp: str) -> list[dict]:
    assert_teacher_owns_subject(teacher_id, subject_id)
    return _run(
        _t("attendance_logs").select("*, students(name)").eq("subject_id", subject_id).eq("timestamp", timestamp),
        "load the session",
    )


def correct_attendance(teacher_id: int, subject_id: int, timestamp: str, student_id: int, new_present: bool, reason: str) -> bool:
    """Change one student's attendance in a saved session and record who/when/why.

    Returns False if nothing changed. Requires migration 002 (attendance_audit); we refuse to
    change attendance without an audit trail.
    """
    reason = _valid(clean_reason, reason)
    assert_teacher_owns_subject(teacher_id, subject_id)
    rows = _run(
        _t("attendance_logs").select("*").eq("subject_id", subject_id).eq("timestamp", timestamp).eq("student_id", student_id),
        "load the record",
    )
    if not rows:
        raise DatabaseError("That attendance record does not exist.")
    old = bool(rows[0]["is_present"])
    if old == bool(new_present):
        return False
    try:
        _run(
            _t("attendance_audit").insert({
                "subject_id": subject_id, "student_id": student_id, "session_timestamp": timestamp,
                "old_present": old, "new_present": bool(new_present), "reason": reason, "changed_by": teacher_id,
            }),
            "write the audit entry",
        )
    except DatabaseError as exc:
        raise DatabaseError("Corrections need the audit table. Run supabase/migrations/002_attendance_integrity.sql.") from exc
    patch = {"is_present": bool(new_present), "source": "corrected"}
    try:
        _run(_t("attendance_logs").update(patch).eq("subject_id", subject_id).eq("timestamp", timestamp).eq("student_id", student_id), "update attendance")
    except DatabaseError as exc:
        if exc.__cause__ is not None and _is_missing_column(exc.__cause__):
            _run(_t("attendance_logs").update({"is_present": bool(new_present)}).eq("subject_id", subject_id).eq("timestamp", timestamp).eq("student_id", student_id), "update attendance")
        else:
            raise
    return True


def get_audit_trail(teacher_id: int, subject_id: int) -> list[dict]:
    assert_teacher_owns_subject(teacher_id, subject_id)
    return _run(_t("attendance_audit").select("*, students(name)").eq("subject_id", subject_id), "load the audit trail")


def get_attendance_for_teacher(teacher_id: int) -> list[dict]:
    require_active_teacher(teacher_id)
    try:
        return _run(
            _t("attendance_logs")
            .select("*, subjects!inner(*), students(name)")
            .eq("subjects.teacher_id", teacher_id),
            "load attendance records",
        )
    except DatabaseError:
        # Fallback for databases without the attendance_logs -> students foreign key.
        return _run(
            _t("attendance_logs").select("*, subjects!inner(*)").eq("subjects.teacher_id", teacher_id),
            "load attendance records",
        )


# ------------------------------------------------------------------------- marks
def create_assessment(teacher_id: int, subject_id: int, name: str, max_marks: float, assessed_on=None) -> dict:
    assert_teacher_owns_subject(teacher_id, subject_id)
    name = (name or "").strip()
    if not name or len(name) > 60:
        raise DatabaseError("Assessment name is required (max 60 characters).")
    if not (0 < float(max_marks) <= 100000):
        raise DatabaseError("Maximum marks must be greater than zero.")
    row = {"subject_id": subject_id, "name": name, "max_marks": float(max_marks), "published": False, "created_by": teacher_id}
    if assessed_on:
        row["assessed_on"] = str(assessed_on)
    try:
        return _run(_t("assessments").insert(row), "create the assessment")[0]
    except DuplicateError as exc:
        raise DuplicateError("This subject already has an assessment with that name.") from exc


def list_assessments(teacher_id: int, subject_id: int) -> list[dict]:
    assert_teacher_owns_subject(teacher_id, subject_id)
    return _run(_t("assessments").select("*").eq("subject_id", subject_id), "load assessments")


def _owned_assessment(teacher_id: int, assessment_id: int) -> dict:
    rows = _run(_t("assessments").select("*").eq("assessment_id", assessment_id), "load the assessment")
    if not rows:
        raise DatabaseError("That assessment does not exist.")
    assert_teacher_owns_subject(teacher_id, rows[0]["subject_id"])
    return rows[0]


def set_published(teacher_id: int, assessment_id: int, published: bool) -> None:
    _owned_assessment(teacher_id, assessment_id)
    patch = {"published": bool(published)}
    _run(_t("assessments").update(patch).eq("assessment_id", assessment_id), "change publication")


def get_marks(teacher_id: int, assessment_id: int) -> list[dict]:
    _owned_assessment(teacher_id, assessment_id)
    rows = _run(_t("marks").select("*, students(name)").eq("assessment_id", assessment_id), "load marks")
    return [{"student_id": r["student_id"], "name": (r.get("students") or {}).get("name", r["student_id"]), "obtained": r["obtained"]} for r in rows]


def import_marks(teacher_id: int, assessment_id: int, rows: list[dict]) -> int:
    """Save marks atomically: ONE upsert request = one transaction. If any row is rejected
    (the app re-validates here; DB triggers enforce max marks and enrolment too), nothing is saved."""
    from src.services.marks import validate_mark

    a = _owned_assessment(teacher_id, assessment_id)
    enrolled = {s["student_id"] for s in _enrolled_students(a["subject_id"])}
    payload, seen = [], set()
    for r in rows:
        sid = int(r["student_id"])
        value, why = validate_mark(r["obtained"], a["max_marks"])
        if sid not in enrolled:
            raise DatabaseError(f"Student {sid} is not enrolled in this subject.")
        if why:
            raise DatabaseError(f"Student {sid}: {why}.")
        if sid in seen:
            raise DatabaseError(f"Student {sid} appears twice.")
        seen.add(sid)
        payload.append({"assessment_id": assessment_id, "student_id": sid, "obtained": float(value), "updated_by": teacher_id})
    if not payload:
        return 0
    return len(_run(_t("marks").upsert(payload, on_conflict="assessment_id,student_id"), "save marks"))


def get_marks_audit(teacher_id: int, assessment_id: int) -> list[dict]:
    _owned_assessment(teacher_id, assessment_id)
    return _run(_t("marks_audit").select("*").eq("assessment_id", assessment_id), "load the audit trail")


def get_student_marks(student_id: int) -> list[dict]:
    """A student's own PUBLISHED marks only (the published filter is part of the query)."""
    rows = _run(
        _t("marks").select("*, assessments!inner(*)").eq("student_id", student_id).eq("assessments.published", True),
        "load your marks",
    )
    sub_ids = sorted({r["assessments"]["subject_id"] for r in rows})
    names = {}
    if sub_ids:
        subs = _run(_t("subjects").select("subject_id, name, subject_code").in_("subject_id", sub_ids), "load subjects")
        names = {s["subject_id"]: s for s in subs}
    out = []
    for r in rows:
        a = r["assessments"]
        s = names.get(a["subject_id"], {})
        out.append({"subject": s.get("name", "?"), "code": s.get("subject_code", "?"), "assessment": a["name"],
                    "assessed_on": a.get("assessed_on"), "obtained": r["obtained"], "max_marks": a["max_marks"]})
    return out


def get_student_pending_assessments(student_id: int) -> list[dict]:
    """Names of unpublished assessments in the student's subjects (never any marks)."""
    sub_ids = [r["subject_id"] for r in get_student_subjects(student_id)]
    if not sub_ids:
        return []
    rows = _run(_t("assessments").select("name, subject_id, published, assessed_on").in_("subject_id", sub_ids), "load assessments")
    return [{"assessment": r["name"], "subject_id": r["subject_id"]} for r in rows if not r["published"]]
