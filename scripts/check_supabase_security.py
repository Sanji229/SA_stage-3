"""Prove the PUBLIC key cannot read or write your data. Needs a real project; read-only except one probe row.

    SUPABASE_URL=... SUPABASE_ANON_KEY=<the publishable/anon key> python scripts/check_supabase_security.py

Never pass the secret key here. Exit code 1 means the public key can see or change data: fix before going live.
"""
from __future__ import annotations

import os
import sys

TABLES = ["teachers", "students", "subjects", "subject_students", "attendance_logs", "attendance_audit",
          "assessments", "marks", "marks_audit", "teacher_invitations", "auth_attempts", "app_settings"]


def main() -> int:
    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_ANON_KEY")
    if not url or not key:
        print("Set SUPABASE_URL and SUPABASE_ANON_KEY (the PUBLIC key).")
        return 2
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from src.database.config import classify_key

    if classify_key(key) == "secret":
        print("Refusing to run: that is a SECRET key. Use the public (anon/publishable) key.")
        return 2
    from supabase import create_client

    client = create_client(url, key)
    failures = 0
    for t in TABLES:
        try:
            rows = client.table(t).select("*").limit(1).execute().data
            if rows:
                print(f"FAIL  {t}: public key can READ rows")
                failures += 1
            else:
                print(f"ok    {t}: no rows visible (denied or empty; run verify_security.sql to tell which)")
        except Exception as exc:  # noqa: BLE001
            print(f"ok    {t}: read denied ({type(exc).__name__})")
    try:
        client.table("auth_attempts").insert({"kind": "probe", "key": "probe", "success": True}).execute()
        print("FAIL  auth_attempts: public key can WRITE")
        failures += 1
    except Exception as exc:  # noqa: BLE001
        print(f"ok    write denied ({type(exc).__name__})")
    print("RESULT:", "FAIL" if failures else "PASS")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
