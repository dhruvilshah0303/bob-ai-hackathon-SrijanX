# AI Ambulance-to-Hospital Coordinator

Instead of routing an ambulance to the *nearest* hospital, this recommends
the hospital that gets the patient to **treatment fastest**, based on live
ICU/ED capacity, specialist availability, and traffic-aware ETA — with the
full reasoning shown, not just a hospital name.

This is a working application, not a scripted demo: every button in the UI
performs a real backend action, and all trip/hospital/user data persists in
a database and survives a restart. Real per-user login/RBAC exists but is
optional — by default the app opens straight into the console with no login
screen (see "Real per-user auth" below); flip `REQUIRE_LOGIN=true` if you
want it gated behind real accounts.

## What it does

* **AI recommendation engine** (`backend/recommendation_engine.py`) —
  constraint-filter + weighted-scoring model with full explainability (a
  "How was this calculated?" breakdown of each scored factor, and a
  "why not the runner-up" comparison).
* **Real per-user auth with roles, optional** (`backend/auth.py`) —
  PBKDF2-hashed passwords, session tokens (`POST /api/auth/login` /
  `/api/auth/logout` / `/api/auth/me`), three roles: `admin` (full access),
  `dispatcher` (create/triage/assess/select-hospital/manage trips), and
  `hospital` (scoped to exactly one hospital — can only update that
  hospital's own capacity and respond to that hospital's own pre-alerts).
  Whether logging in is actually *required* is controlled by `REQUIRE_LOGIN`
  (default `false`): with it off, a request with no credentials is treated
  as a full-access admin identity and the frontend never shows a login
  screen — useful for a single-operator/local setup. Set `REQUIRE_LOGIN=true`
  to gate access behind real accounts (e.g. a multi-hospital deployment
  where a hospital's own staff should only see/manage their own hospital). A
  default admin account is auto-created on first startup either way, from
  `ADMIN_USERNAME`/`ADMIN_PASSWORD`; the admin creates further accounts via
  `POST /api/auth/users`.
* **Persistent hospital + trip data** (`backend/db.py`, SQLAlchemy) —
  hospitals and trips are written to the database continuously, not just at
  trip end. Hospitals seed once from `backend/data/hospitals.json` when the
  database is empty; after that, capacity edits made through the app are
  what's authoritative. Active (non-terminal) trips reload from the
  database on startup and their movement/tracking resumes automatically —
  refreshing the page, or restarting the server, does not lose live state.
* **Traffic-aware ETA** (`backend/eta_service.py`) — Google Maps Distance
  Matrix or Mapbox Directions if a key is configured, otherwise a
  clearly-labeled simulated ETA (never presented as if it were live).
* **Road routing** (`backend/routing_service.py`) — real road-route polyline
  from Google/Mapbox Directions if configured, otherwise a straight-line
  fallback, same honest-about-its-source pattern as ETA.
* **AI Triage Assist** (`backend/triage_service.py`,
  `POST /api/trips/{id}/triage`, wired into the dispatcher note field in the
  UI) — classifies a dispatcher's free-text note (e.g. "55yo male, crushing
  chest pain radiating to left arm") into a suggested condition using IBM
  watsonx.ai's Granite model, or a transparent keyword classifier if
  watsonx.ai isn't configured. Always a suggestion the dispatcher must
  confirm, never an auto-set condition.
* **AI hospital handover note** — the same watsonx.ai model (or a
  deterministic template fallback) drafts the natural-language clinical
  handover sentence sent with the hospital pre-alert, including patient
  age/sex when provided.
* **Real ambulance position** — the browser reports live device GPS
  (`navigator.geolocation.watchPosition`) to `POST /api/trips/{id}/position`
  when available; the backend only falls back to route+time interpolation
  when no GPS report has arrived within the last 20 seconds
  (`DEVICE_GPS_FRESHNESS_SEC` in `app.py`).
  Either source is labeled in the trip data (`position_source`) — nothing is
  presented as live GPS when it isn't.
* **Dynamic rerouting with real bed reservation** — accepting a
  recommendation reserves that hospital's capacity (ICU bed / ED bay);
  a rejection or a capacity change on the current destination triggers a
  reroute, permanently excluding any hospital that rejected that trip's
  pre-alert from future recommendations for it.
* **Hospital pre-alert accept/reject** — a real per-hospital decision
  (`POST /api/trips/{id}/prealert/respond`), scoped so only that hospital's
  own staff (or an admin) can respond.
* **Multi-ambulance / multi-trip** — hospital capacity is a single shared
  resource every active trip competes for; the UI lists every trip you're
  responsible for and lets you focus any of them.
* **Analytics** — KPIs, a recommended-vs-selected hospital leaderboard, and
  a full audit trail, all backed by the database.
* **Production-readiness layer**, on by default where it matters and
  configurable via env var: CORS (`ALLOWED_ORIGINS`), per-IP rate limiting
  (`RATE_LIMIT_PER_MINUTE`), structured logging (`LOG_LEVEL`), optional
  Sentry error tracking (`SENTRY_DSN`). See `.env.example` for the full
  list. A `Dockerfile` and `Procfile` are included for container/PaaS
  deployment.
* **Security hardening** — stored-XSS defense-in-depth (frontend escaping +
  backend length/control-character validation), a rate limiter that doesn't
  trust `X-Forwarded-For` unless explicitly configured to, `/api/health`
  exempt from rate limiting and from auth, Postgres connections using
  `pool_pre_ping`, dependencies version-pinned, and a non-root Docker image
  with a `HEALTHCHECK`.

## Quick start

```bash
cd backend
pip install -r requirements.txt
cp .env.example .env   # or local.env — both are read; fill in what you have
uvicorn app:app --reload --port 8000
```

Open **http://localhost:8000** — that's the coordinator console directly (no
separate landing page), and it opens straight in — no login screen by
default. If you set `REQUIRE_LOGIN=true` in your `.env`, you'll hit a login
gate instead: sign in with the admin account from `ADMIN_USERNAME`/
`ADMIN_PASSWORD` (defaults to `admin` / `change-me-now` if unset — **change
this before setting REQUIRE_LOGIN=true anywhere real**, see below). Either
way, an admin can create dispatcher and hospital accounts via
`POST /api/auth/users`.

Leave `GOOGLE_MAPS_API_KEY`/`MAPBOX_API_KEY` and `WATSONX_API_KEY`/
`WATSONX_PROJECT_ID` blank and the app runs on its simulated-ETA and
keyword-classifier fallbacks — clearly labeled as such in the UI (the ETA
badge in the top bar, and `/api/health`'s `eta_provider`/`triage_provider`
fields) rather than presented as live.

### Running the tests

```bash
cd backend
pip install -r requirements-dev.txt
python3 -m pytest tests/ -v
```

No network or real server needed (FastAPI's `TestClient` runs the app
in-process, and the watsonx.ai call is mocked). Covers the recommendation
engine's edge cases, the full API surface (auth, RBAC, incident/patient
validation, prealert accept/reject, position reporting, dynamic reroute,
persistence-across-restart), road routing, AI Triage Assist's
watsonx.ai/keyword-fallback classification, and the security-hardening
layer (auth, rate limiting, config parsing, XSS defense-in-depth).

A separate, optional file (`tests/test_frontend_smoke.py`) drives a real
Chromium browser via Playwright to check things a Python-only test can't:
the page loads with no console errors, login gates the app, the full
create-incident → AI Triage Assist → assess → recommend → select → hospital
capacity → analytics flow works end to end, a phone-width viewport never
overflows horizontally, and a stored-XSS payload renders as inert text. It's
skipped automatically (not failed) if Playwright/Chromium isn't installed:

```bash
pip install playwright   # already in requirements-dev.txt
playwright install chromium
python3 -m pytest tests/test_frontend_smoke.py -v
```

## Deploying it for real

The app is one process (FastAPI serves the API, WebSocket, and the static
frontend), so any container-friendly host works the same way:

```bash
docker build -t coordinator .
docker run -p 8000:8000 --env-file backend/.env coordinator
```

Or, for a platform that runs a `Procfile` directly (Heroku-style) instead of
building the `Dockerfile`, that's included too.

Before pointing this at real traffic, set at minimum:

* `REQUIRE_LOGIN=true` — unless you genuinely want the API wide open to
  anyone with the URL, turn real login/RBAC on for a real deployment.
  `ENVIRONMENT=production` makes the app log a startup warning if this is
  still unset/false.
* `ADMIN_PASSWORD` — replace the default before the app is reachable by
  anyone else. `ENVIRONMENT=production` makes the app log a startup warning
  if this is still `change-me-now`.
* `ALLOWED_ORIGINS` — your actual frontend origin(s), instead of `*`.
* `DATABASE_URL` — a Postgres URL, so hospital/trip/user/session data
  survives a redeploy (most container hosts wipe local disk, including
  SQLite files, on every deploy).

Full details on every env var are in `.env.example`.

## Environment variables

See `.env.example` (and `backend/.env.example`, identical) for the complete,
commented list. Summary:

| Variable | Purpose |
| --- | --- |
| `GOOGLE_MAPS_API_KEY` / `MAPBOX_API_KEY` | Live traffic-aware ETA + road routing. Optional — falls back to simulated ETA / straight-line routing. |
| `REQUIRE_LOGIN` | `false` (default) = open access, no login screen, every request is a full-access admin identity. `true` = the frontend shows a login gate and every `/api/*` route (except health/login) requires a real session. |
| `WATSONX_API_KEY` / `WATSONX_PROJECT_ID` / `WATSONX_URL` / `WATSONX_MODEL_ID` | AI Triage Assist + AI handover notes. Optional — falls back to a keyword classifier / template. |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | Seeds the default admin account on first startup. |
| `SESSION_TTL_HOURS` | How long a login session stays valid (default 12). |
| `APP_ACCESS_TOKEN` | Legacy shared-secret fallback for scripts/health checks only — not the primary auth mechanism. Leave blank unless you need it. |
| `DATABASE_URL` | Postgres connection string. Blank = local SQLite file. |
| `ENVIRONMENT` | `development` (default) or `production` — controls startup warnings. |
| `ALLOWED_ORIGINS` | Comma-separated CORS origins. Defaults to `*`. |
| `RATE_LIMIT_ENABLED` / `RATE_LIMIT_PER_MINUTE` | Per-IP request cap on `/api/*`. |
| `TRUST_PROXY_HEADERS` | Only `true` if you control a reverse proxy in front of this app. |
| `SENTRY_DSN` | Optional error tracking. |
| `LOG_LEVEL` | Python logging level. |

## Project structure

```
prototype/
  src/
    backend/
      app.py                     FastAPI app: auth-gated routes, trips, websocket, movement loop, DB persistence
      config.py                  Centralized env-var config + startup warnings
      auth.py                    Real per-user auth (sessions + RBAC) + legacy shared-secret fallback
      ratelimit.py               Per-IP rate limiting middleware
      sentry_init.py             Optional error tracking (no-op unless SENTRY_DSN set)
      recommendation_engine.py   The AI scoring/reasoning engine
      eta_service.py             Real ETA API wrapper + seeded simulated fallback
      routing_service.py         Real road-route wrapper (Google/Mapbox Directions) + straight-line fallback
      triage_service.py          IBM watsonx.ai triage classification + handover notes, keyword/template fallback
      db.py                      SQLAlchemy persistence: hospitals, trips, users, sessions, audit log
      env_loader.py              Reads .env / local.env
      utils.py                   Distance + freshness helpers + road-route interpolation
      data/
        hospitals.json           Initial hospital capacity/specialist data (seeds the DB once, empty-DB only)
        severity_rules.json      Condition -> required capability mapping + scoring weights
      tests/
        conftest.py                     Shared fixtures (rate-limit isolation, bypass_auth for non-auth-focused tests)
        test_recommendation_engine.py   Unit tests for the scoring/disqualification logic
        test_api.py                     API-level tests: RBAC, incident/patient validation, prealert, reroute, persistence
        test_routing.py                 Unit tests for road-route interpolation + polyline decoding + fallback
        test_auth.py                    Real auth: password hashing, sessions, RBAC, legacy token fallback, WS auth
        test_ratelimit.py               Rate limiter tests, incl. the X-Forwarded-For spoof regression test
        test_config.py                  Env-var parsing + startup-warning tests
        test_security_escaping.py       Backend XSS defense-in-depth (length caps, control-char stripping)
        test_triage_service.py          AI Triage Assist: watsonx.ai classification + handover notes + keyword/template fallback
        test_frontend_smoke.py          Optional real-browser (Playwright) smoke + login gate + XSS + mobile-overflow tests
      requirements.txt
      requirements-dev.txt       Adds pytest/httpx/playwright for the test suite
      pytest.ini
      .env.example
    frontend/
      index.html                 The coordinator console (login gate, dispatch/hospital/analytics tabs)
      app.js
      style.css
      config.js
      effects.js
  Dockerfile                    Single-container deploy (backend + static frontend), non-root, HEALTHCHECK
  Procfile                      Heroku-style process declaration
  render.yaml                   Render.com deploy config
  .dockerignore
  .gitignore
  .github/
    workflows/
      ci.yml                    GitHub Actions: pytest on push/PR + a Docker build sanity check
```

## Known limitations

* **Open by default.** `REQUIRE_LOGIN=false` (the default) means anyone who
  can reach the URL has full admin access with no login — right for local
  use or a single-operator deployment behind your own network controls, not
  for anything reachable by strangers. Set `REQUIRE_LOGIN=true` (and a real
  `ADMIN_PASSWORD`) before deploying this anywhere public.
* **Ambulance position** is real device GPS when the browser reports it
  (within the last 20 seconds), and honest route+time interpolation
  otherwise — there's no dedicated vehicle GPS hardware integration, and the
  UI labels which source is active (`position_source`) rather than
  presenting interpolation as live tracking.
* **Single-instance movement/reroute loop** — trip movement and rerouting
  run as in-process background tasks guarded by one coarse lock
  (`STATE_LOCK` in `app.py`). This is fine for a single running instance;
  running multiple backend replicas behind a load balancer would need that
  moved to a shared scheduler/queue, since two instances would each try to
  drive the same trip.
* **The map** (Leaflet via the unpkg.com CDN) requires that CDN to be
  reachable; if it isn't, incident location falls back to manual
  latitude/longitude entry (`#incidentLatInput`/`#incidentLngInput`) rather
  than map-click placement — the rest of the app is unaffected.
* **Hospital data seeds from `data/hospitals.json` once**, only when the
  database is empty. Editing that file after first startup has no effect —
  hospital capacity is managed at runtime (through the app or
  `POST /api/hospitals/{id}/capacity`) from then on.
* All hospital names/data are fictional — not real facilities.
