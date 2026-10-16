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
| Student | Public registration with a face photo and voice sample (rate-limited; duplicate faces rejected; explicit biometric consent recorded) |
| Teacher | **Only** via a single-use invitation created by an administrator (`register_teacher_with_invite`); codes stay valid until used or revoked |
| Administrator | First one: `ADMIN_SETUP_TOKEN` + one-time setup form (closes after use; unique DB claim makes it atomic). Later ones: admin invitation |

`teachers.role` / `teachers.status` are authoritative. Every teacher operation calls `require_active_teacher()` which re-reads them
from the database; the session's copy of the role only decides which tabs to *display*. Invitation tokens are 192-bit random,
shown once and stored only as SHA-256; codes are consumed atomically and stay valid until used or revoked.
Existing teachers keep working (migration 004 defaults: `teacher` / `active`).

## Abuse protection (`auth_attempts`, database-backed)
Teacher login: 5 failures / 15 min per username (also unknown usernames; the right password is refused while locked).
Invitation guessing: 10 failures / hour globally. Admin setup: 5 / hour. Student registration: 30 attempts / hour globally;
the attempt is reserved before face and voice inference, so failed submissions count too.
Limits are per key, **not per IP**: Streamlit does not expose a trustworthy client IP. If the table is missing the check
**fails open** with a log warning for the existing throttles; public student registration fails closed unless migration
008's atomic reservation function is installed. Profile deletion and consent metadata require migration 009.

## Write-path audit
| Table | Written by | Guard |
|---|---|---|
| teachers | `register_teacher_with_invite`, `bootstrap_admin`, `set_teacher_status` | invitation / setup token / admin role |
| teacher_invitations | `create_invitation`, `revoke_invitation`, registration | admin role; atomic single use |
| students | `register_student` → `create_student`; `delete_student_profile` | validation, required valid face, duplicate-face check + race guard, rate limit, recorded consent; deletion anonymizes attendance and marks |
| subjects, subject_students | `create_subject`, enrol/unenrol | active teacher / student session; validated |
| Student records, attendance, marks, voice profile | student dashboard database calls | student ID must match the face-authenticated server session |
| attendance_logs, attendance_audit | `save_attendance_session`, `correct_attendance` | active owner (mandatory `teacher_id`), enrolled students only; correction and audit are one transaction (migration 008); profile deletion retains anonymous history (009) |
| assessments, marks, marks_audit | `create_assessment`, `import_marks`, `set_published` | active owner, re-validated, one atomic upsert; DB triggers (max marks, enrolment, audit) |
| auth_attempts, app_settings | throttle / setup | server only |
| **students (operator scripts)** | `scripts/import_synthetic_students.py`, `scripts/enroll_synthetic_students.py` | **bypass `db.py` and its checks**: run locally with your key, never expose; do not point them at production |

## Residual risks (not fixed)
- A student's identity is a server-side session value set after a face match; there is no liveness check, so a printed photo may pass face sign-in.
- Student registration is public by design (rate-limited), so someone can create junk profiles up to the limit.
- Voice and face thresholds/margins are not validated on real data (`evaluation/README.md`).
- No per-IP limits; registration and login limits remain global/per username. Sessions now expire after configurable inactivity, but there is no separate account-change audit log for status changes or invitations.
- The SQL triggers (marks rules, audit, biometric version) and the lockdown migration have **not been executed** in any automated test here.

## Deployment checklist
1. Back up the database. 2. Run `schema.sql` (only if tables are missing), then migrations `002`, `003`, `004`, `005`, `006`, `007`, `008`, and `009` in order. Review migration `005` because it changes public API grants.
3. Run `supabase/verify_security.sql`; expect no rows from queries 1-5. 4. Set secrets: `SUPABASE_URL`, `SUPABASE_KEY` (secret key), `ADMIN_SETUP_TOKEN` (temporarily), `APP_BASE_URL`.
5. Open the app → Teacher login → *First-time setup*, create the administrator, then **delete `ADMIN_SETUP_TOKEN`** and redeploy.
6. Admin tab → create teacher invitations. 7. Run `python scripts/check_supabase_security.py` with the PUBLIC key; expect `RESULT: PASS`.
8. Rotate any key that was ever committed or shared.
