"""Minimal in-memory stand-in for the supabase-py client, for tests ONLY.

It implements just what src/database/db.py uses: select (with embedded
relations, count and !inner), insert, delete, eq, execute, unique constraints.
It is NOT a substitute for testing against a real Supabase project.
"""
from __future__ import annotations

import copy
import itertools
import re
from datetime import datetime, timedelta, timezone

UNIQUE = {
    "teacher_invitations": [("token_hash",)],
    "app_settings": [("key",)],
    "assessments": [("subject_id", "name")],
    "marks": [("assessment_id", "student_id")],
    "teachers": [("username",)],
    "subjects": [("subject_code",)],
    "subject_students": [("student_id", "subject_id")],
    "attendance_logs": [("subject_id", "student_id", "timestamp")],
}
PK = {"teacher_invitations": "invite_id", "auth_attempts": "attempt_id", "assessments": "assessment_id", "marks": "mark_id", "attendance_audit": "audit_id", "teachers": "teacher_id", "students": "student_id", "subjects": "subject_id", "attendance_logs": "log_id"}
# table -> relation name -> (target table, local key, remote key, many?)
REL = {
    "marks": {"assessments": ("assessments", "assessment_id", "assessment_id", False), "students": ("students", "student_id", "student_id", False)},
    "assessments": {"subjects": ("subjects", "subject_id", "subject_id", False), "marks": ("marks", "assessment_id", "assessment_id", True)},
    "subject_students": {"students": ("students", "student_id", "student_id", False), "subjects": ("subjects", "subject_id", "subject_id", False)},
    "attendance_logs": {"students": ("students", "student_id", "student_id", False), "subjects": ("subjects", "subject_id", "subject_id", False)},
    "subjects": {"subject_students": ("subject_students", "subject_id", "subject_id", True), "attendance_logs": ("attendance_logs", "subject_id", "subject_id", True)},
}


DB_DEFAULTS = {"teacher_invitations": {"used_at": None, "used_by": None, "role": "teacher"}, "teachers": {"role": "teacher", "status": "active"}, "students": {"biometric_updated_at": "2026-01-01T00:00:00+00:00"}}


class APIError(Exception):
    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.code = code


class Result:
    def __init__(self, data):
        self.data = data


class FakeSupabase:
    def __init__(self):
        self.tables = {n: [] for n in ["teachers", "students", "subjects", "subject_students", "attendance_logs",
                                       "assessments", "marks", "attendance_audit", "marks_audit",
                                       "teacher_invitations", "auth_attempts", "app_settings"]}
        self.missing_columns: dict[str, set] = {}  # table -> columns that "do not exist yet" (simulates an un-migrated DB)
        self._ids = {n: itertools.count(1) for n in self.tables}

    def table(self, name):
        return Query(self, name)

    def rpc(self, name, params):
        return RPCQuery(self, name, params)


class RPCQuery:
    """Small stand-in for the transactional correction and rate-limit functions."""

    def __init__(self, db, name, params):
        self.db, self.name, self.params = db, name, params

    def execute(self):
        if self.name == "delete_student_profile_anonymized":
            sid = self.params["p_student_id"]
            student = next((r for r in self.db.tables["students"] if r.get("student_id") == sid), None)
            if student is None:
                return Result(False)
            for table in ("attendance_logs", "attendance_audit", "marks", "marks_audit"):
                for row in self.db.tables[table]:
                    if row.get("student_id") == sid:
                        row["student_name"] = "Former student"
                        row["student_id"] = None
                        if table == "attendance_logs":
                            row["student_status"] = "deleted"
            self.db.tables["subject_students"] = [r for r in self.db.tables["subject_students"] if r.get("student_id") != sid]
            self.db.tables["students"].remove(student)
            return Result(True)
        if self.name == "reserve_student_registration_attempt":
            p = self.params
            since = datetime.now(timezone.utc) - timedelta(minutes=p["p_window_minutes"])
            rows = [r for r in self.db.tables["auth_attempts"]
                    if r.get("kind") == "student_register" and r.get("key") == "global"
                    and datetime.fromisoformat(r["attempted_at"]) > since]
            if len(rows) >= p["p_max_events"]:
                return Result(False)
            row = {"kind": "student_register", "key": "global", "success": False,
                   "attempted_at": datetime.now(timezone.utc).isoformat(),
                   "attempt_id": next(self.db._ids["auth_attempts"])}
            self.db.tables["auth_attempts"].append(row)
            return Result(True)
        if self.name != "correct_attendance_transaction":
            raise APIError(f"function {self.name} does not exist", code="PGRST202")
        p = self.params
        teacher = next((r for r in self.db.tables["teachers"] if r.get("teacher_id") == p["p_teacher_id"]), None)
        subject = next((r for r in self.db.tables["subjects"] if r.get("subject_id") == p["p_subject_id"]), None)
        row = next((r for r in self.db.tables["attendance_logs"]
                    if r.get("subject_id") == p["p_subject_id"]
                    and r.get("timestamp") == p["p_session_timestamp"]
                    and r.get("student_id") == p["p_student_id"]), None)
        if not teacher or teacher.get("status", "active") != "active" or not subject or subject.get("teacher_id") != p["p_teacher_id"] or not row:
            raise APIError("Attendance record not found or permission denied", code="42501")
        if row.get("is_present") == p["p_new_present"]:
            return Result(False)
        audit = {
            "subject_id": p["p_subject_id"], "student_id": p["p_student_id"],
            "student_name": row.get("student_name", ""), "session_timestamp": p["p_session_timestamp"],
            "old_present": row.get("is_present"), "new_present": p["p_new_present"],
            "reason": p["p_reason"], "changed_by": p["p_teacher_id"],
        }
        audit["audit_id"] = next(self.db._ids["attendance_audit"])
        self.db.tables["attendance_audit"].append(audit)
        row["is_present"] = p["p_new_present"]
        row["source"] = "corrected"
        return Result(True)


class Query:
    def __init__(self, db, name):
        self.db, self.name = db, name
        self.op, self.cols, self.payload, self.filters = "select", "*", None, []

    def select(self, cols="*"):
        self.op, self.cols = "select", cols
        return self

    def insert(self, payload):
        self.op, self.payload = "insert", payload
        return self

    def delete(self):
        self.op = "delete"
        return self

    def update(self, payload):
        self.op, self.payload = "update", payload
        return self

    def upsert(self, payload, on_conflict=None):
        self.op, self.payload, self.on_conflict = "upsert", payload, on_conflict
        return self

    def is_(self, col, val):
        self.filters.append((col, ("__is__", val)))
        return self

    def gt(self, col, val):
        self.filters.append((col, ("__gt__", val)))
        return self

    def in_(self, col, vals):
        self.filters.append((col, ("__in__", list(vals))))
        return self

    def eq(self, col, val):
        self.filters.append((col, val))
        return self

    # ------------------------------------------------------------------
    def execute(self):
        rows = self.db.tables[self.name]
        if self.op in ("insert", "upsert", "update"):
            bad = self.db.missing_columns.get(self.name, set())
            items = self.payload if isinstance(self.payload, list) else [self.payload]
            for it in items:
                for c in it:
                    if c in bad:
                        raise APIError(f"Could not find the '{c}' column of '{self.name}' in the schema cache", code="PGRST204")
        if self.op == "select":
            bad = self.db.missing_columns.get(self.name, set())
            for c in (x.strip() for x in self.cols.split(",") if "(" not in x):
                if c in bad:
                    raise APIError(f"column {self.name}.{c} does not exist", code="42703")
        if self.op == "update":
            hit = [r for r in rows if self._ok(r)]
            for r in hit:
                r.update(self.payload)
            return Result(copy.deepcopy(hit))
        if self.op == "upsert":
            keys = [k.strip() for k in (self.on_conflict or "").split(",") if k.strip()]
            batch = self.payload if isinstance(self.payload, list) else [self.payload]
            out = []
            for item in batch:  # all-or-nothing is not needed here: validated before the call
                existing = next((r for r in rows if keys and all(r.get(k) == item.get(k) for k in keys)), None)
                if existing:
                    existing.update(item); out.append(existing)
                else:
                    row = dict(item)
                    if self.name in PK:
                        row[PK[self.name]] = next(self.db._ids.setdefault(self.name, itertools.count(1)))
                    rows.append(row); out.append(row)
            return Result(copy.deepcopy(out))
        if self.op == "insert":
            return Result(self._insert(rows))
        if self.op == "delete":
            keep, gone = [], []
            for r in rows:
                (gone if self._ok(r) else keep).append(r)
            self.db.tables[self.name][:] = keep
            return Result(copy.deepcopy(gone))
        return Result(self._select(rows))

    def _ok(self, r):
        for c, v in self.filters:
            if isinstance(v, tuple) and v and v[0] == "__in__":
                if r.get(c) not in v[1]:
                    return False
            elif isinstance(v, tuple) and v and v[0] == "__is__":
                if (r.get(c) is None) != (str(v[1]).lower() == "null"):
                    return False
            elif isinstance(v, tuple) and v and v[0] == "__gt__":
                if r.get(c) is None or not str(r.get(c)) > str(v[1]):
                    return False
            elif r.get(c) != v:
                return False
        return True

    def _insert(self, rows):
        batch = self.payload if isinstance(self.payload, list) else [self.payload]
        new = []
        for item in batch:
            row = dict(item)
            self.db._ids.setdefault(self.name, itertools.count(1))
            for cols in UNIQUE.get(self.name, []):
                if any(all(r.get(c) == row.get(c) for c in cols) for r in rows + new):
                    raise APIError('duplicate key value violates unique constraint', code="23505")
            if self.name in PK:
                row[PK[self.name]] = next(self.db._ids[self.name])
            for col, default in DB_DEFAULTS.get(self.name, {}).items():  # what the migrated schema fills in
                if col not in self.db.missing_columns.get(self.name, set()):
                    row.setdefault(col, default)
            new.append(row)
        rows.extend(new)
        return copy.deepcopy(new)

    def _select(self, rows):
        local = [(c, v) for c, v in self.filters if "." not in c]
        remote = [(c, v) for c, v in self.filters if "." in c]
        out = []
        for r in rows:
            probe = Query(self.db, self.name); probe.filters = local
            if not probe._ok(r):
                continue
            row = self._embed(r)
            if remote:
                if not all((row.get(c.split(".")[0]) or {}).get(c.split(".")[1]) == v for c, v in remote):
                    continue
            out.append(self._project(row))
        return copy.deepcopy(out)

    def _embed(self, r):
        row = dict(r)
        for rel, (target, lk, rk, many) in REL.get(self.name, {}).items():
            if rel not in self.cols:
                continue
            matches = [t for t in self.db.tables[target] if t.get(rk) == r.get(lk)]
            m = re.search(re.escape(rel) + r"(?:!inner)?\(([^()]*(?:\([^()]*\))?[^()]*)\)", self.cols)
            if m and not many:  # like PostgREST: an embed returns only the columns that were asked for
                wanted = [c.strip() for c in m.group(1).split(",") if c.strip() and "(" not in c]
                if wanted and "*" not in wanted:
                    matches = [{c: t.get(c) for c in wanted} for t in matches]
            if many:
                row[rel] = [{"count": len(matches)}] if f"{rel}(count)" in self.cols else matches
            else:
                row[rel] = matches[0] if matches else None
        return row

    def _project(self, row):
        # Only enforce column lists for plain (non-embedded) selects.
        if "(" in self.cols or self.cols.strip() == "*":
            return row
        wanted = [c.strip() for c in self.cols.split(",")]
        return {c: row.get(c) for c in wanted}
