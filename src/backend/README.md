# SrijanX Backend

FastAPI backend for the real, multi-user emergency coordination platform — see the [repository root README](../../README.md) for the full picture (problem/solution, what's implemented vs. dev-only, deployment). This file covers backend-specific setup and module layout.

## Quick start

```bash
cd src/backend
python -m pip install -r requirements.txt
alembic upgrade head                    # creates the schema (users, hospitals, emergencies, ...)
python scripts/seed_development.py      # optional: dev/test hospitals + one account per role
uvicorn app:app --reload --port 8000
```

Open `http://localhost:8000/login.html`. If you seeded, sign in as `dispatcher@example-dev.test` / `DevPass123!` (every seeded account is listed in `scripts/seed_development.py`, all sharing that password).

`GET /docs` (Swagger UI) and `GET /openapi.json` are available in every environment.

### Running the tests

```bash
pip install -r requirements-dev.txt
pytest tests/ -v
```

229 tests, no network or real server needed (`TestClient` runs the app in-process; the watsonx.ai call is mocked in `test_triage_service.py`). Includes a real concurrency test (`test_two_concurrent_reservations_for_the_last_icu_bed_only_one_succeeds` in `test_trip_routes.py`) that fires two threads at a hospital's last ICU bed and asserts exactly one reservation succeeds — the actual guarantee, not a hope.

Tests share one on-disk SQLite file across a run (no per-test transaction rollback) — each test uses uniquely-generated emails/vehicle numbers/hospital names to stay independent regardless of what ran before it. `conftest.py` bootstraps the ORM schema via `models.init_models()` (test-only; a real deployment always uses `alembic upgrade head`).

## Module layout

```
app.py                    Wires every router together + the /ws websocket + /api/health.
                           No business logic or application state of its own.
models.py                 SQLAlchemy ORM schema - the single source of truth. Migrated with
                           Alembic (alembic/), not created ad hoc.
db.py / db_session.py     Engine/URL resolution, and the per-request Session dependency.
config.py                 All env-var-driven settings, read once at import time.

auth_service.py           Password hashing, JWT issue/verify, get_current_user()/require_role()
auth_routes.py            POST /api/auth/register|login|logout|refresh, GET /api/auth/me

hospital_routes.py        Hospital accounts: create/verify (ADMIN), resources, specialists
hospital_service.py       Adapts DB hospitals into the dict shape recommendation_engine.py expects

emergency_routes.py       Patient intake + synchronous AI-assisted triage
severity_config.py        Shared data/severity_rules.json loader (condition -> requirements)
triage_service.py         IBM watsonx.ai classification/handover notes + keyword/template fallback

ambulance_routes.py       Ambulance accounts + live GPS (restricted to the assigned operator)

trip_routes.py            Recommendation, hospital request/accept/decline, trip dispatch/
                           lifecycle/reroute/cancel - the core coordination workflow
reservation_service.py    Transactional (atomic conditional-UPDATE) capacity reservations
recommendation_engine.py  Constraint filtering + weighted scoring (TRD Section 5) - unchanged
                           since the original prototype; only its data source moved to Postgres
eta_service.py            Real traffic-aware ETA (Google/Mapbox) + labeled simulated fallback
routing_service.py        Real road-route geometry + labeled straight-line fallback

notification_service.py / notification_routes.py   Per-user notifications
analytics_routes.py       Real metrics computed live from HospitalRequest/Trip/TripEvent rows
admin_routes.py           ADMIN-only user provisioning (roles that can't self-register) + listing

ratelimit.py               Per-IP rate limiting middleware
sentry_init.py              Optional error tracking (no-op unless SENTRY_DSN set)
utils.py                    haversine distance, staleness check, polyline interpolation
scripts/seed_development.py Dev/test fixtures - refuses to run with ENVIRONMENT=production
```

## Environment variables

See `.env.example` for the full, documented list. Nothing is required for local dev (SQLite, simulated ETA/routing, keyword-fallback triage, an auto-generated JWT secret all work with zero configuration) — every setting becomes meaningful once you deploy somewhere real.

## Deploying it for real

One process (FastAPI serves the API, websocket, and static frontend):

```bash
docker build -t srijanx .
docker run -p 8000:8000 --env-file backend/.env srijanx
```

Before pointing this at real traffic, set at minimum:

- `JWT_SECRET` — a long random string that stays fixed across restarts/redeploys (unset, a random one is generated per-process, which invalidates every session on every restart).
- `ALLOWED_ORIGINS` — your actual frontend origin(s), instead of `*`.
- `DATABASE_URL` — a Postgres URL (`render.yaml` provisions one automatically for a Render deployment).

`ENVIRONMENT=production` makes the app log a warning on startup for any of the above still left at its insecure local-dev default.
