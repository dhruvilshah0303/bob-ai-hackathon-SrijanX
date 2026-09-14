# AI Ambulance-to-Hospital Coordinator

An explainable emergency-dispatch dashboard that recommends the hospital where a patient can reach appropriate treatment fastest, not simply the nearest hospital.

## Team

| Field | Value |
|---|---|
| Team name | SrijanX |
| Track | AI |
| Team lead | Shaunak - 25dcs098@charusat.edu.in |
| Members	| Abhi, Moon, Dhruvil |

## Problem

Dispatchers must balance clinical capability, available ICU or ED capacity, specialist coverage, and traffic under time pressure. A nearest-hospital decision can send a patient to a facility that cannot provide the required treatment or has no usable capacity.

## Solution

The coordinator filters hospitals against the incident severity and current capacity, calculates traffic-aware arrival and treatment times, and ranks the viable options with a human-readable explanation. The dashboard lets a dispatcher assess, accept, reroute, and monitor multiple ambulances while hospital capacity remains a shared resource.

## Key Features

- Explainable constraint filtering and weighted hospital ranking
- Live Leaflet map with concurrent ambulance trips
- ICU and ED reservation with release on reroute or cancellation
- Hospital pre-alerts and persistent audit analytics
- Live ETA integration with deterministic simulated fallback
- Optional production access token, CORS, rate limiting, logging, and Sentry settings

## Tech Stack

Python, FastAPI, Uvicorn, SQLAlchemy, JavaScript, Leaflet, WebSockets, SQLite/PostgreSQL, Docker, GitHub Actions, and OpenStreetMap.

The backend is configured for Render via `render.yaml`; the static frontend can be deployed to Vercel.

## Repository Structure

```text
src/
  backend/       FastAPI API, recommendation engine, data, and tests
  frontend/      Static dashboard and browser client
  Dockerfile     Single-container deployment for Render or similar hosts
docs/             Problem, solution, architecture, and setup documentation
demo/             Demo links and screenshots
presentation/     Hackathon presentation assets
submission.yaml   Structured submission metadata
```

## Run Locally

See [docs/setup-guide.md](docs/setup-guide.md) for tested commands. The shortest path is:

```bash
cd src/backend
python -m pip install -r requirements.txt
uvicorn app:app --reload --port 8000
```

Open `http://localhost:8000`. The backend serves the frontend in the bundled local configuration.

## Demo

- Live demo: [demo/live-demo-url.txt](demo/live-demo-url.txt)
- Video: [demo/demo-video-link.txt](demo/demo-video-link.txt)
- Screenshots: [demo/screenshots/](demo/screenshots/)

## Known Limitations

The hospital records, ambulance locations, and capacity changes are simulated. Authentication is a shared token rather than role-based access control. A production deployment should connect to hospital information systems and use a shared durable store for live trip state.

## What We Are Most Proud Of

The project makes the recommendation reasoning inspectable: dispatchers can see why a hospital was disqualified, how capacity reservations affect other ambulances, and why a reroute is suggested.
