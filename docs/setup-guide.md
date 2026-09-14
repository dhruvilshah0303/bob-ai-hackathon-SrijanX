# Setup Guide

## Prerequisites

- Python 3.11 or newer
- Git
- A modern browser
- Docker Desktop (optional, for the single-container run)

## Environment Variables

The backend reads `src/backend/.env` or `src/backend/local.env`. Start from `src/backend/.env.example`.

| Variable | Description | Required |
|---|---|---|
| `GOOGLE_MAPS_API_KEY` | Optional Google traffic ETA provider | No |
| `MAPBOX_API_KEY` | Optional Mapbox traffic ETA provider | No |
| `DEMO_TIME_SCALE` | Speed multiplier for simulated ambulance movement | No |
| `APP_ACCESS_TOKEN` | Shared API and WebSocket access token | Recommended in production |
| `ALLOWED_ORIGINS` | Comma-separated browser origins | Recommended in production |
| `DATABASE_URL` | SQLite default or PostgreSQL connection URL | No |
| `RATE_LIMIT_ENABLED` | Enable API rate limiting | No |
| `RATE_LIMIT_PER_MINUTE` | Per-IP API request limit | No |
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
uvicorn app:app --reload --port 8000
```

Open `http://localhost:8000`. The API serves the static frontend and WebSocket from the same origin.

For a separate frontend deployment, set `window.COORDINATOR_BACKEND_URL` in `src/frontend/config.js` to the Render backend URL and deploy `src/frontend` as a static Vercel project. The Render Blueprint already restricts browser CORS to the published Vercel origin.

For the hosted backend, use the included `render.yaml` Blueprint. It installs
`src/backend/requirements.txt`, starts Uvicorn on Render's `$PORT`, and exposes
`/api/health` for service health checks. After Render creates the service, copy
its public URL into `src/frontend/config.js` before deploying the frontend.

## Running Tests

From the repository root:

```bash
python -m pytest src/backend/tests -v
```

## Docker

```bash
cd src
docker build -t ambulance-coordinator .
docker run --rm -p 8000:8000 ambulance-coordinator
```

## Quick Demo

1. Open the dashboard and choose a condition.
2. Select **Assess & Get AI Recommendation**.
3. Inspect the rejected hospitals and recommendation explanation.
4. Accept the destination and watch capacity and the ambulance marker update.
5. Launch a second ambulance to demonstrate shared capacity.
6. Open Hospital View and Analytics to inspect pre-alerts and audit events.

## Troubleshooting

| Issue | Solution |
|---|---|
| `ModuleNotFoundError` | Activate the virtual environment and rerun `python -m pip install -r src/backend/requirements-dev.txt`. |
| Browser cannot connect | Confirm Uvicorn is running from `src/backend` on port 8000. |
| Live ETA is simulated | Set `GOOGLE_MAPS_API_KEY` or `MAPBOX_API_KEY`; simulation is the expected no-key fallback. |
| Cross-origin browser errors | Set backend `ALLOWED_ORIGINS` to the exact Vercel origin and set `frontend/config.js` to the Render origin. |
| PostgreSQL connection error | Leave `DATABASE_URL` empty for local SQLite, or verify the managed database URL and driver. |
