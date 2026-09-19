# SrijanX — `src/`

This directory is the deployment build context for the platform (its own `Dockerfile`, `Procfile`, and `.env.example` treat `src/` as the app root — that's why they exist here as well as under `backend/`).

- For the project overview (problem, solution, what's implemented vs. dev-only, architecture, deployment) see the [repository root README](../README.md).
- For backend setup, module layout, and running the tests, see [backend/README.md](backend/README.md).
- The frontend (`frontend/`) is static: `login.html`, `dispatcher.html`, `hospital.html`, `ambulance.html`, plus the separate marketing page at `index.html`. It's served by the backend's static mount in local/single-container deployments, or hosted separately (e.g. Vercel) pointed at the backend via `frontend/config.js`.

## Quick start

```bash
cd backend
pip install -r requirements.txt
alembic upgrade head
python scripts/seed_development.py   # optional: dev/test accounts + hospitals
uvicorn app:app --reload --port 8000
```

Open `http://localhost:8000/login.html`.
