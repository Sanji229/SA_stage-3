# SmartAttend

AI-assisted classroom attendance built with Streamlit and Supabase.
Teachers take attendance from classroom **photos** (face recognition) or a **voice** recording;
students sign in with their face, join subjects by code/link/QR and see their own attendance.

## Features
- Landing page, teacher sign-up/login (bcrypt), student FaceID sign-in and registration
- Subjects with unique join codes, join link + QR (when `APP_BASE_URL` is set)
- Face attendance from multiple photos, restricted to students enrolled in the subject
- Optional voice attendance (Resemblyzer speaker embeddings)
- Review-before-save, duplicate-safe saving, overview dashboard, records, CSV export

## Quick start (local)
```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt            # voice deps (PyTorch) are large; see below
cp .env.example .env                        # then fill in SUPABASE_URL / SUPABASE_KEY
set -a && source .env && set +a             # or use .streamlit/secrets.toml instead
streamlit run app.py
```
Without credentials the app shows a setup screen instead of crashing.

### Database
Run `supabase/schema.sql` in the Supabase SQL editor. **It was inferred from the code** (the repo had no schema):
compare it with your existing tables first. It is idempotent and non-destructive. It enables RLS with no public
policies; the app uses a **server-side key** and enforces authorisation in `src/database/db.py`.

### Configuration
| Variable | Purpose |
|---|---|
| `SUPABASE_URL`, `SUPABASE_KEY` | Required. Server-side only; never commit or expose. |
| `APP_BASE_URL` | Public app URL for join links/QR (optional). |
| `FACE_MATCH_THRESHOLD` | Max face distance for a match (default 0.45; finite range 0–1; lower = stricter). |
| `VOICE_MATCH_THRESHOLD` | Min cosine similarity for a voice match (default 0.55; finite range 0–1). |
| `FACE_MATCH_MARGIN`, `VOICE_MATCH_MARGIN` | Ambiguity margins (default 0.05; finite range 0–2). |
| `FACE_NUM_JITTERS`, `FACE_MIN_SIZE_PX` | Face processing controls (defaults 0 and 40; ranges 0–100 and 1–2000). |
| `SESSION_IDLE_TIMEOUT_MINUTES` | Idle sign-out timeout (default 30 minutes; range 1–1440). |

Read from environment variables first, then `.streamlit/secrets.toml`.

### Voice features
`resemblyzer` installs PyTorch (large). If it is missing, the app still runs and voice features show a clear message.

## Tests
```bash
pip install -r requirements-dev.txt
python -m pytest tests -q
```
- `tests/test_services.py`, `tests/test_db.py`: business logic and DB layer (against an **in-memory fake**, `tests/fake_supabase.py`).
- `tests/test_app_flows.py`: headless UI flows via `streamlit.testing` (fake DB).
- `tests/test_face_real.py`: runs the **real dlib models** on public sample photos (downloaded at test time, skipped offline).
- `streamlit run tests/dev_fake_backend.py`: run the real app on a seeded in-memory DB (demo teacher `demo` / `demo-password`). Dev only.

## Deployment
Any host that runs `streamlit run app.py --server.port $PORT --server.address 0.0.0.0` works (Streamlit Community Cloud, a container, a VM).
Set the variables above as the host's secrets. Health check: `GET /_stcore/health` returns `ok`.
Use HTTPS (camera and microphone access require it in browsers). Not verified on a live host.

## Security notes
- Never commit `.env` or `.streamlit/secrets.toml` (both git-ignored). **Rotate any key that was ever exposed**, including the Supabase key that triggered GitHub push protection.
- Photos and audio are processed in memory; only numeric embeddings and attendance rows are stored. Embeddings are biometric data: obtain consent and check local regulations.
- Student sign-in is face-only with **no liveness check**: a printed photo may pass. Login attempts are not rate-limited across sessions.
- Sessions live in Streamlit session state: a browser refresh signs the user out.


---
## Recognition integrity, marks and migrations (stage 2)

**Run migrations in order in the Supabase SQL editor** (additive, non-destructive; compare with your DB first):
`supabase/schema.sql` -> migrations `002` through `009` in order. Migration 007 preserves student names and attendance
history when a student is unenrolled or deleted; 008 makes attendance corrections and their audit entries atomic and
retains correction audit rows after student deletion; 009 records biometric consent and keeps attendance, marks, and their audit history under the generic label “Former student” after profile deletion. The app still saves attendance on a database without 002 (legacy columns),
but audited corrections, marks, and durable student history need their migrations.
Student registration also requires migration 008 for its atomic attempt reservation and 009 for recorded consent / profile deletion; without 008, registration fails closed.

**How a face is decided:** detect -> embed -> match. Each face is `matched`, `ambiguous`, `duplicate` or `unknown`; only
`matched` is auto-present. Everything else is listed for the teacher in an editable review table. Detection and recognition
do not prove physical presence. Faces smaller than `FACE_MIN_SIZE_PX` are sent to teacher review and cannot be marked
automatically. A signed-in student can replace their face template from the dashboard; duplicate templates are rejected.
Uploaded images are validated then resized to a 2000-pixel maximum side before keeping them in session state. Audio
component payloads are size-limited before decoding, and recordings are limited to 10 minutes.

New settings: `FACE_MATCH_MARGIN`, `FACE_NUM_JITTERS`, `FACE_MIN_SIZE_PX`, `VOICE_MATCH_MARGIN` (see `src/settings.py`).

**Known limitations:** no liveness/anti-spoofing; thresholds and margins are not validated on real data (see
`evaluation/README.md`); voice model not exercised in CI; marks DB triggers untested; per-user RLS requires Supabase Auth.
Full per-item status: `docs/STATUS.md`.

**Data in `class_data/` is synthetic** and must not be used as an accuracy benchmark or imported into production.


---
## Hardening update: accounts, validation, Supabase

- **Teacher accounts are invitation-only.** There is no public teacher sign-up. See `docs/SECURITY.md` for the first-administrator procedure.
- **Run migrations `002` through `009` in order**; review migration `005` carefully because it changes public API grants. Then run `supabase/verify_security.sql`. The app refuses to start with a public Supabase key.
- **No classifier to retrain.** Face matching is a distance search over stored embeddings, cached per *gallery version*; a new enrolment is visible to every instance immediately. `FACE_MATCH_THRESHOLD`, `FACE_MATCH_MARGIN` are unchanged.
- New optional settings: `ADMIN_SETUP_TOKEN`, `LOGIN_MAX_FAILURES`, `LOGIN_WINDOW_MINUTES`, `STUDENT_REGISTER_PER_HOUR`.
- Breaking changes for scripts/tests: `db.get_enrolled_students(teacher_id, subject_id)` and `db.save_attendance_session(logs, teacher_id)` now require the acting teacher.
