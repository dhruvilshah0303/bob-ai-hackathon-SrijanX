# Setup Guide

## Prerequisites

- Python 3.11 or newer
- Git
- A modern browser (the ambulance portal's GPS needs `navigator.geolocation`, which requires HTTPS or `localhost`)
- Docker Desktop (optional, for the single-container run)

## Environment Variables

The backend reads `src/backend/.env` or `src/backend/local.env`. Start from `src/backend/.env.example`.

| Variable | Description | Required |
|---|---|---|
| `GOOGLE_MAPS_API_KEY` | Optional Google traffic ETA/routing provider | No |
| `MAPBOX_API_KEY` | Optional Mapbox traffic ETA/routing provider | No |
| `WATSONX_API_KEY` / `WATSONX_PROJECT_ID` | Optional IBM watsonx.ai for real AI triage/handover notes | No |
| `JWT_SECRET` | Signs session tokens | Required in production (unset = auto-generated per process, invalidating sessions on every restart) |
| `JWT_EXPIRE_MINUTES` / `JWT_REFRESH_EXPIRE_DAYS` | Token lifetimes | No |
| `ALLOWED_ORIGINS` | Comma-separated browser origins | Recommended in production |
| `DATABASE_URL` | SQLite default or PostgreSQL connection URL | Required in production (SQLite is not a multi-user production database) |
| `RATE_LIMIT_ENABLED` / `RATE_LIMIT_PER_MINUTE` | API rate limiting | No |
| `SENTRY_DSN` | Optional Sentry error tracking DSN | No |
| `LOG_LEVEL` | Application log level | No |

## Installation

```bash
git clone https://github.com/dhruvilshah0303/bob-ai-hackathon-SrijanX.git
cd bob-ai-hackathon-SrijanX
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r src/backend/requirements-dev.txt
```

On macOS or Linux, activate with `source .venv/bin/activate`.

## Running the Application

```bash
cd src/backend
alembic upgrade head                    # creates the schema
python scripts/seed_development.py      # optional: dev/test accounts + hospitals
uvicorn app:app --reload --port 8000
```

Open `http://localhost:8000` for the landing page, or `http://localhost:8000/login.html` to sign in directly. The API serves the static frontend and WebSocket from the same origin in this local setup.

For a separate frontend deployment, set `window.COORDINATOR_BACKEND_URL` in `src/frontend/config.js` to the Render backend URL and deploy `src/frontend` as a static Vercel project. The Render Blueprint already restricts browser CORS to the published Vercel origin.

For the hosted backend, use the included `render.yaml` Blueprint - it provisions a managed PostgreSQL database, generates `JWT_SECRET`, installs `src/backend/requirements.txt`, runs `alembic upgrade head`, starts Uvicorn on Render's `$PORT`, and exposes `/api/health` for service health checks. After Render creates the service, copy its public URL into `src/frontend/config.js` before deploying the frontend.

## Running Tests

From the repository root:

```bash
python -m pytest src/backend/tests -v
```

229 tests as of this writing.

## Docker

```bash
cd src
docker build -t srijanx .
docker run --rm -p 8000:8000 --env-file backend/.env srijanx
```

## First Login

1. Run `python scripts/seed_development.py` (refuses to run if `ENVIRONMENT=production`).
2. Sign in at `/login.html` as `dispatcher@example-dev.test` / `DevPass123!` (every seeded account/password is listed in the script's output).
3. Create an emergency with a real symptom description - triage runs immediately.
4. Pull a recommendation, request a hospital.
5. Sign in as that hospital's admin (e.g. `hospitaladmin.a@example-dev.test`) in another browser/profile to accept it.
6. Back as the dispatcher, dispatch one of the seeded ambulances.
7. Sign in as that ambulance's operator (`operator1@example-dev.test`) to start the trip and grant location permission for real GPS.

## Troubleshooting

| Issue | Solution |
|---|---|
| `ModuleNotFoundError` | Activate the virtual environment and rerun `python -m pip install -r src/backend/requirements-dev.txt`. |
| `no such table: users` (or similar) | Run `alembic upgrade head` - the schema is migrated, never auto-created on `uvicorn` startup. |
| Browser cannot connect | Confirm Uvicorn is running from `src/backend` on port 8000. |
| Live ETA/routing is simulated | Set `GOOGLE_MAPS_API_KEY` or `MAPBOX_API_KEY`; simulation is the expected, clearly-labeled no-key fallback. |
| Triage uses the keyword fallback | Set `WATSONX_API_KEY`/`WATSONX_PROJECT_ID`; the keyword classifier is the expected, clearly-labeled no-credentials fallback. |
| GPS doesn't work on the ambulance portal | `navigator.geolocation` requires a secure context - `localhost` is fine, a plain `http://` deployment is not. |
| Cross-origin browser errors | Set backend `ALLOWED_ORIGINS` to the exact frontend origin and set `frontend/config.js` to the backend origin. |
| PostgreSQL connection error | Leave `DATABASE_URL` empty for local SQLite, or verify the managed database URL and driver. |
