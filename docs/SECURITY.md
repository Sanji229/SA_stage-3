# SmartAttend security model

## Which Supabase key does the app use?
`SUPABASE_KEY` must be the **server-side secret key** (`sb_secret_…` or the legacy `service_role` JWT). It bypasses Row Level
Security, so **all authorisation is enforced in `src/database/db.py`**. The app classifies the key at startup
(`classify_key`) and **refuses to start with a public (anon/publishable) key**, because that key would either be denied
everything (RLS on) or expose every table to anyone holding it (RLS off). The key lives only in server secrets
(`.streamlit/secrets.toml`, host secret store, or env) and is never sent to the browser, logged or committed.
SmartAttend does **not** use Supabase Auth, so per-user RLS policies (`auth.uid()`) cannot apply; RLS is enabled with **no
policies** on every table so the public key reaches nothing. Moving to Supabase Auth would be the only way to get
DB-enforced per-user policies: a larger change that needs your decision.

## Roles and account creation
| Who | How they get access |
|---|---|
| Student | Public registration with a face photo (rate-limited; duplicate faces rejected) |
| Teacher | **Only** via a single-use, expiring invitation created by an administrator (`register_teacher_with_invite`) |
| Administrator | First one: `ADMIN_SETUP_TOKEN` + one-time setup form (closes after use; unique DB claim makes it atomic). Later ones: admin invitation |

`teachers.role` / `teachers.status` are authoritative. Every teacher operation calls `require_active_teacher()` which re-reads them
from the database; the session's copy of the role only decides which tabs to *display*. Invitation tokens are 192-bit random,
shown once, stored only as SHA-256, consumed with an atomic `UPDATE … WHERE used_at IS NULL AND expires_at > now()`.
Existing teachers keep working (migration 004 defaults: `teacher` / `active`).

## Abuse protection (`auth_attempts`, database-backed)
Teacher login: 5 failures / 15 min per username (also unknown usernames; the right password is refused while locked).
Invitation guessing: 10 failures / hour globally. Admin setup: 5 / hour. Student registration: 30 / hour globally.
Limits are per key, **not per IP**: Streamlit does not expose a trustworthy client IP. If the table is missing the check
**fails open** with a log warning (so a missing migration cannot lock everyone out).

## Write-path audit
| Table | Written by | Guard |
|---|---|---|
| teachers | `register_teacher_with_invite`, `bootstrap_admin`, `set_teacher_status` | invitation / setup token / admin role |
| teacher_invitations | `create_invitation`, `revoke_invitation`, registration | admin role; atomic single use |
| students | `register_student` → `create_student` | validation, required valid face, duplicate-face check + race guard, rate limit |
| subjects, subject_students | `create_subject`, enrol/unenrol | active teacher / student session; validated |
| attendance_logs, attendance_audit | `save_attendance_session`, `correct_attendance` | active owner (mandatory `teacher_id`), enrolled students only, audit row |
| assessments, marks, marks_audit | `create_assessment`, `import_marks`, `set_published` | active owner, re-validated, one atomic upsert; DB triggers (max marks, enrolment, audit) |
| auth_attempts, app_settings | throttle / setup | server only |
| **students (operator scripts)** | `scripts/import_synthetic_students.py`, `scripts/enroll_synthetic_students.py` | **bypass `db.py` and its checks**: run locally with your key, never expose; do not point them at production |

## Residual risks (not fixed)
- A student's identity is a server-side session value set after a face match: there is no cryptographic session token, no expiry and no liveness check. A printed photo can pass face sign-in.
- Student registration is public by design (rate-limited), so someone can create junk profiles up to the limit.
- Voice and face thresholds/margins are not validated on real data (`evaluation/README.md`).
- No per-IP limits; no session timeout; no audit log for account changes (status, invitations) beyond the tables themselves.
- The SQL triggers (marks rules, audit, biometric version) and the lockdown migration have **not been executed** in any automated test here.

## Deployment checklist
1. Back up the database. 2. Run `schema.sql` (only if tables are missing), then migrations `002`, `003`, `004`, and, after reviewing it, `005`.
3. Run `supabase/verify_security.sql`; expect no rows from queries 1-5. 4. Set secrets: `SUPABASE_URL`, `SUPABASE_KEY` (secret key), `ADMIN_SETUP_TOKEN` (temporarily), `APP_BASE_URL`.
5. Open the app → Teacher login → *First-time setup*, create the administrator, then **delete `ADMIN_SETUP_TOKEN`** and redeploy.
6. Admin tab → create teacher invitations. 7. Run `python scripts/check_supabase_security.py` with the PUBLIC key; expect `RESULT: PASS`.
8. Rotate any key that was ever committed or shared.
