# AI-Based Interview Security System — Production-ready V1 (still free)

You said: general public, unknown scale, zero cost. Here's the honest version of
what that means and how this build handles it.

## The tension, stated plainly
"General public + zero cost" works fine **until** real traffic shows up — free
tiers have hard ceilings (CPU, DB rows, bandwidth, sleep-on-idle). This build is
designed to degrade gracefully instead of crashing when it hits those ceilings,
and to make the eventual "pay for the one thing that's actually the bottleneck"
upgrade a five-minute change, not a rewrite.

## What changed from the college-project version

| Problem | Fix |
|---|---|
| `/admin` had **zero authentication** — anyone could delete users, approve fake candidates, view every photo | Flask-Login session auth on every `/admin*` route + a real login page |
| Photos/snapshots were served publicly at `/uploads/...` | Now requires login too |
| DeepFace calls block the Flask worker for 1-3s each — enough concurrent users and the server chokes | A semaphore caps concurrent face-matching at 3; extra requests queue briefly instead of taking the process down |
| Anyone could POST arbitrary files with a `.jpg` extension | Files are now verified as genuine images (Pillow) before ever reaching DeepFace |
| SQLite doesn't handle concurrent writes well under real traffic | Swapped to SQLAlchemy — same code works with SQLite locally, and with a **free Postgres** (Neon/Supabase) in production by just setting one environment variable |
| Face-verify endpoint had no abuse protection | Rate-limited: 15 verify attempts/min, 5 review-requests/min, per IP |
| Secrets/passwords risk of being hardcoded | Everything sensitive comes from environment variables (`.env.example` has the full list) |

## Required environment variables (production)

Copy `.env.example` to `.env` and fill in:

```bash
SECRET_KEY=<python -c "import secrets; print(secrets.token_hex(32))">
ADMIN_USERNAME=admin
ADMIN_PASSWORD_HASH=<python -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('your-password'))">
DATABASE_URL=   # leave blank for SQLite locally; set for Postgres in production
FLASK_DEBUG=false
```

The app will not let you log into `/admin` at all if `ADMIN_PASSWORD_HASH` isn't set —
this is intentional, so you can't accidentally deploy with the door unlocked.

## Free production stack (recommended)

| Piece | Free service | Why |
|---|---|---|
| Hosting | **Render.com** free web service, or **Google Cloud Run** free tier | Cloud Run doesn't have the "sleeps after 15 min idle" problem Render's free tier has — better for genuinely public traffic. Render is simpler to set up if occasional cold-start is acceptable. |
| Database | **Neon.tech** or **Supabase** free Postgres | Handles concurrent writes properly, unlike SQLite. Just paste the connection string into `DATABASE_URL`. |
| Photo storage | Local disk is fine to start; move to **Supabase Storage** or **Cloudinary** free tier once you outgrow single-instance disk | Free tiers here are generous (1GB+) — plenty for reference photos |
| Video/meeting | **meet.jit.si** (already wired in) | Free, no self-hosting, includes screen share/chat |

## Deploy steps (Render + Neon, both free)

1. **Database**: create a free project at neon.tech → copy the connection string.
2. **Push to GitHub**, then on Render: New → Web Service → connect repo.
3. Build command: `pip install -r requirements.txt`
4. Start command: `gunicorn app:app`
5. Add environment variables (from `.env.example`) in Render's dashboard, including `DATABASE_URL` from Neon.
6. Deploy. Render gives you a free HTTPS URL — required for browser camera access.

## What will actually break first if traffic grows (and the fix)

Realistically, in order:
1. **CPU on face verification** — the semaphore prevents a crash, but response times will slow under heavy concurrent load. Fix when you hit this: upgrade Render's instance type for ~$7/month, or move DeepFace inference to a separate worker queue (Celery + free Redis tier).
2. **Render free tier cold starts** — first request after idle takes 30-60s. Fix: Cloud Run (still free, no sleep) or a paid always-on instance.
3. **Neon free tier connection limits** — fine for moderate traffic; if exceeded, Neon's next tier is a few dollars/month, not a redesign.

None of these require touching your application code — that's the point of setting
up Postgres + rate limiting + the concurrency cap now, before you need them.

## Known simplifications still in V1
- Recording isn't implemented (would need Jibri or browser MediaRecorder — Phase 2)
- Rate limiter uses in-memory storage — fine for one instance; if you run multiple
  gunicorn workers, point Flask-Limiter at a free Redis tier (e.g. Upstash) so
  limits are shared across workers instead of per-worker
- Admin approval uses polling (3s) instead of WebSockets

## Local setup (unchanged)

```bash
cp .env.example .env    # fill in ADMIN_PASSWORD_HASH at minimum
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt --break-system-packages
python app.py
```

Visit `http://localhost:5000/admin/login`.

## Folder structure
```
interview-security/
├── app.py                  # routes, auth-protected admin, rate limits, concurrency cap
├── database.py              # SQLAlchemy — SQLite locally, Postgres in production
├── auth.py                   # admin login (Flask-Login)
├── .env.example
├── requirements.txt
├── templates/
│   ├── base.html
│   ├── admin_login.html      # new
│   ├── admin.html
│   ├── verify.html
│   ├── meeting.html
│   └── logs.html
├── static/css/style.css
└── uploads/                  # now login-protected
```
