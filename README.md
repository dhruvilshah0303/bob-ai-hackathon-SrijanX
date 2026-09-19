# SrijanX — AI Emergency Coordination Platform

A real, multi-user emergency coordination system: dispatchers create emergencies and get an AI-assisted, explainable recommendation for which hospital gets a patient to *actual treatment* fastest — not just the nearest one — and hospitals, ambulance crews, and dispatchers coordinate the rest of the case (acceptance, dispatch, live GPS, rerouting, transfer) through role-scoped accounts backed by a real database.

## Team

| Field | Value |
|---|---|
| Team name | SrijanX |
| Track | AI |
| Team lead | Shaunak - 25dcs098@charusat.edu.in |
| Members	| Abhi, Moon, Dhruvil |

## Problem

Dispatchers must balance clinical capability, available ICU/ED capacity, specialist coverage, and traffic under time pressure. A nearest-hospital decision can send a patient to a facility that cannot provide the required treatment or has no usable capacity — and that decision, and everything that happens after it, needs to survive a server restart, work for many concurrent users with different roles, and never let two ambulances get told the same last ICU bed is free.

## Solution

The AI triage service classifies an incoming case, the recommendation engine filters hospitals against that case's hard requirements (ICU, ED capacity, specialist) and ranks the viable ones by *total time-to-treatment* (travel + hospital wait) with a full, inspectable explanation — then a dispatcher approves a hospital, that hospital accepts or declines (reserving capacity transactionally on accept), an ambulance is dispatched with a real GPS feed, and the whole case — including a mid-trip reroute if capacity or traffic changes — is tracked through to completion with an audit trail.

## Implemented

- **Real user accounts** — bcrypt-hashed passwords, JWT access/refresh tokens, four roles (`ADMIN`, `DISPATCHER`, `HOSPITAL_ADMIN`, `AMBULANCE_OPERATOR`) enforced server-side on every endpoint. No shared-secret auth anywhere.
- **Real hospital accounts** — onboarding/verification (ADMIN), resources and specialist rosters editable only by that hospital's own admin (or ADMIN).
- **AI-assisted triage** (decision support, not diagnosis) — IBM watsonx.ai Granite with a labeled keyword-classifier fallback when no API key is configured; every result states its own source (`watsonx` vs `keyword_fallback`).
- **Explainable hospital recommendation** — hard constraint filtering + weighted scoring on real hospital data, real traffic-aware ETA (Google/Mapbox, with a clearly-labeled simulated fallback), ranked by time-to-treatment.
- **Real hospital acceptance workflow** — a dispatcher requests a hospital; that hospital accepts (which transactionally reserves its ICU bed/ED bay — two emergencies competing for the last bed can never both succeed) or declines with a reason.
- **Real trip lifecycle** — dispatch → start → arrive → complete, plus a dispatcher-approved dynamic reroute (new hospital's capacity is reserved before the old one is released) and cancellation, all audited.
- **Real ambulance GPS** — `navigator.geolocation.watchPosition` in the browser, restricted to the ambulance's own assigned operator (not even an ADMIN can post GPS on an operator's behalf); throttled server-side so a live location stream doesn't flood the database.
- **Real-time updates** — a websocket pushes every state change (hospital request/accept/decline, trip lifecycle, capacity changes, notifications) to connected clients; the database, not the socket, remains the source of truth.
- **Per-user notifications**, a full **audit trail** (every consequential action), and **real analytics** (hospital acceptance rate, reroute count, average dispatch-to-arrival time) computed live from the database.
- **Admin user/hospital management** for onboarding real accounts outside the development seed data.
- Three real web portals — dispatcher, hospital, ambulance — plus a login/register page, all calling the same tested API.

## Prototype / test-only

- **Development seed data** (`src/backend/scripts/seed_development.py`) — clearly-named "Test Hospital A/B/C" and one test account per role, all sharing one documented password. Refuses to run against a production database. This is the only fixture data anywhere in the system; there is no hard-coded operational data in the frontend.
- **Simulated ETA/routing** when no `GOOGLE_MAPS_API_KEY`/`MAPBOX_API_KEY` is configured — clearly labeled as `simulated` in every API response that carries one, never presented as real traffic data.
- **Keyword-fallback triage** when no `WATSONX_API_KEY`/`WATSONX_PROJECT_ID` is configured — a deterministic, transparent classifier, not a model call, and labeled as such.

## Future integrations

- Real hospital information system (HIS) integration for capacity data (today, a hospital's own staff enter it through the hospital portal — real accounts, real data, just not machine-fed from an EHR).
- Per-channel websockets (`/ws/dispatcher`, `/ws/hospital/{id}`, `/ws/ambulance/{id}`) instead of one broadcast channel.
- A grounded AI copilot for structured "why was X recommended" / "what changed for trip Y" queries.
- A live map (Leaflet) on the dispatcher portal showing ambulances/hospitals/routes.

## Tech Stack

Python, FastAPI, SQLAlchemy (ORM) + Alembic migrations, PostgreSQL (SQLite for local dev), JWT auth, WebSockets, vanilla JavaScript, Docker, GitHub Actions.

The backend is configured for Render via `render.yaml` (includes a managed Postgres database and auto-generated JWT secret); the static frontend can be deployed to Vercel.

## Repository Structure

```text
src/
  backend/       FastAPI app: auth, hospitals, emergencies, ambulances, trips, notifications,
                 analytics, admin; models.py (ORM) + alembic/ (migrations); tests/
  frontend/      login.html, dispatcher.html, hospital.html, ambulance.html + js/*.js;
                 index.html is the separate marketing/landing page
  Dockerfile     Single-container deployment for Render or similar hosts
docs/             Architecture, problem/solution, setup documentation
demo/             Demo links and screenshots
presentation/     Hackathon presentation assets
submission.yaml   Structured submission metadata
```

## Run Locally

```bash
cd src/backend
python -m pip install -r requirements.txt
alembic upgrade head                    # creates the schema
python scripts/seed_development.py      # optional: dev/test accounts + hospitals
uvicorn app:app --reload --port 8000
```

Open `http://localhost:8000` for the landing page, or go straight to `http://localhost:8000/login.html`. If you ran the seed script, sign in as `dispatcher@example-dev.test` / `DevPass123!` (see the script for every seeded account).

## Security

Bcrypt password hashing, JWT access (short-lived) + refresh tokens, server-enforced RBAC on every endpoint (never trusting a role claimed by the client), input validation on every write, per-IP rate limiting, CORS locked to configured origins in production, no secrets committed (`.env.example` documents every variable), append-only audit trail. See `.env.example` for `JWT_SECRET` / `DATABASE_URL` / provider keys.

## Testing

229 backend tests (`cd src/backend && pip install -r requirements-dev.txt && pytest`), including a real concurrency test that fires two threads at the last ICU bed and asserts exactly one reservation succeeds. No network or real server needed — FastAPI's `TestClient` runs the app in-process.

## Deployment

- **Backend (Render)**: `render.yaml` provisions a managed Postgres database, generates `JWT_SECRET`, runs `alembic upgrade head` on every deploy, and starts `uvicorn`.
- **Frontend (Vercel or similar)**: static files under `src/frontend/`; set `window.COORDINATOR_BACKEND_URL` in `config.js` to the deployed backend's URL.

## Known Limitations

- No dedicated live map, notification bell, or command-center dashboard UI yet — the three portals are functional but plain; every action is a real API call, none are simulated.
- The websocket is one shared broadcast channel rather than per-role channels; every connected client receives every event and filters client-side.
- No manual browser click-through was performed on the rebuilt frontend in this environment (no browser-automation tool available) — verified instead via `node --check` on every script, all pages/assets serving 200 from a live server, and every API call matching a backend endpoint covered by the passing test suite.
- Ventilator requirements aren't yet derived by triage (the resource exists on the hospital model and can be reserved manually; `severity_rules.json` doesn't currently mark any condition as requiring one).

## What We Are Most Proud Of

The project makes every decision inspectable end to end: a dispatcher can see why a hospital was disqualified, a reservation service that makes the "two ambulances, one last bed" race genuinely impossible (not just unlikely), and a real trip lifecycle where a mid-route capacity change leads to an actual, audited reroute rather than a demo animation.
