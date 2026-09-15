# AI Ambulance-to-Hospital Coordinator — Demo Prototype

This is a runnable prototype of the "killer feature" described in the PRD/TRD:
instead of routing an ambulance to the *nearest* hospital, it recommends the
hospital that gets the patient to **treatment fastest**, based on live ICU/ED
capacity, specialist availability, and traffic-aware ETA — with the full
reasoning shown, not just a hospital name.

It corresponds to **Phase 0 (Demo)** in the PRD's rollout plan and follows the
build order in TRD Section 14.

## What's included

* A working **AI Recommendation Engine** (`backend/recommendation_engine.py`)
  implementing the constraint-filter + weighted-scoring model from TRD
  Section 5, with full explainability, and **120 passing pytest tests**
  covering its edge cases (no viable hospital, ties, stale data, disqualified-
  nearest-hospital), the API's error paths and reservation logic, road
  routing, AI Triage Assist's watsonx.ai/keyword-fallback classification, and
  the security-hardening layer described below (see `backend/tests/`).
* **Multiple concurrent ambulances.** Hospital capacity is a single shared
  resource every ambulance competes for — the system tracks any number of
  simultaneous trips, not just one. Click **"Launch a second ambulance"** in
  the app to spin up an autopiloted second trip that assesses, selects, and
  drives itself, so you can show two ambulances contending for the same beds
  without needing a second browser or person.
* 5 mock hospitals with editable ICU/ward/ED capacity and specialist rosters
  (`backend/data/hospitals.json`).
* A **traffic-aware ETA service** (`backend/eta_service.py`) that uses a real
  provider (Google Maps Distance Matrix or Mapbox Directions) and
  **automatically falls back** to a simulated ETA if no key is configured or
  the live call fails. The simulated traffic factor is now seeded per trip so
  rankings stay stable through a trip instead of flickering between
  recomputes.
* **AI Triage Assist** (`backend/triage_service.py`, `POST
  /api/trips/{id}/triage`): given a dispatcher's free-text note (e.g.
  "55yo male, crushing chest pain radiating to left arm"), classifies it
  into a suggested condition code using **IBM watsonx.ai**, calling a
  Granite instruct model's text-generation API — a suggestion only, it
  would only ever prefill the condition dropdown, never set the condition
  itself (same "decision support, not authority" principle as the rest of
  the app). Same real-provider-with-graceful-fallback shape as the
  ETA/routing services: no `WATSONX_API_KEY`/`WATSONX_PROJECT_ID`
  configured, or a live call fails, and it transparently falls back to a
  labeled keyword classifier instead of breaking the demo or guessing
  silently — see `/api/health`'s `triage_provider` for which one actually
  answered. Fully implemented and tested (`backend/tests/test_triage_service.py`,
  plus the API-level tests in `test_api.py`) and reachable via the endpoint
  above; the dispatcher-note input control is not currently wired up in the
  console UI.
* **AI hospital handover note** (`triage_service.generate_handover_note`):
  the same watsonx.ai Granite model drafts a short, natural-language
  clinical handover sentence (not just a bare condition code) that goes out
  with the hospital pre-alert — e.g. *"55yo male, crushing substernal chest
  pain radiating to left arm, ETA 9 min, requesting cardiologist
  availability."* Falls back to a deterministic template built from the
  same structured fields (condition, severity, specialist, ETA, and the
  dispatcher's own note if one was given) when watsonx.ai isn't configured
  or a live call fails — the receiving hospital always sees a real sentence,
  never a raw JSON blob or a missing field. Shown on both ends: the
  Hospital View pre-alert card, and the dispatcher's own Trip Status.
* **Priority-sorted incoming queue** (Hospital View): pre-alerts are ranked
  by clinical severity first, then soonest-arriving within the same
  severity tier — not by who alerted first — with a live countdown synced
  to the same accelerated demo-time movement clock the map animation uses,
  and a "⏱ Next in queue" badge on the top case whenever more than one
  ambulance is inbound.
* A **live map demo** (Leaflet/OpenStreetMap): every active ambulance
  animates from its incident scene to its selected hospital in accelerated
  demo-time, **following the actual road route** (`backend/routing_service.py`,
  drawn as a polyline on the map) rather than a straight diagonal line
  across the map — it uses the same real-provider-with-graceful-fallback
  pattern as the ETA service, so it automatically degrades to a straight
  line if no live routing API key is configured or reachable.
* **Dynamic rerouting** with real bed reservation: accepting a recommendation
  now actually reserves that hospital's capacity (an ICU bed, an ED bay), and
  releases it if the trip reroutes elsewhere or is cancelled — so two
  ambulances can't both be told the same last bed is free.
* **Hospital pre-alert**, shown as a list in the "Hospital View" tab so it
  correctly displays alerts from *every* active ambulance, not just one.
* An **analytics tab** with the KPIs named in the PRD, backed by a database
  (`backend/db.py`, via SQLAlchemy) so the audit trail survives a server
  restart — it's only cleared by an explicit "Reset demo" click. Defaults to
  a local SQLite file with zero setup; point `DATABASE_URL` at Postgres for
  a real deployment and nothing else changes.
* A `/api/health` endpoint for basic liveness checking (never requires the
  access code below, so hosting platforms can health-check it).
* Toast notifications and disabled-while-loading buttons in the frontend, so
  a failed or slow request is visible instead of silently doing nothing.
* **A guided, mobile-friendly UI**: a step tracker (Assess → Choose hospital
  → En route → Arrived) always shows where the current trip actually is;
  the condition dropdown forces an intentional choice before "Assess" is
  clickable instead of silently defaulting; the AI's per-hospital reasoning
  is tucked behind a "why?" toggle so the recommendation table reads cleanly
  at a glance but the full explanation is one tap away; the demo-only
  capacity-simulation controls are collapsed by default so they don't
  compete with the real dispatch flow; and every table scrolls horizontally
  on a phone instead of breaking the page layout.
* **Production-readiness layer**, all off by default so local dev stays
  frictionless, all on with one env var each for a real deployment: a
  shared-secret access gate (`APP_ACCESS_TOKEN`), configurable CORS
  (`ALLOWED_ORIGINS`), per-IP rate limiting (`RATE_LIMIT_PER_MINUTE`),
  structured logging (`LOG_LEVEL`), and optional Sentry error tracking
  (`SENTRY_DSN`). See `.env.example` for the full list. A `Dockerfile` and
  `Procfile` are included for container/PaaS deployment.
* **Security hardening** (see "Security hardening" below for the full
  list): all stored-XSS vectors fixed (frontend escaping + backend
  length/control-character validation), the rate limiter no longer trusts
  `X-Forwarded-For` unless explicitly configured to, `/api/health` is
  exempt from rate limiting, Postgres connections use `pool_pre_ping`, the
  WebSocket reconnect logic handles a rejected/rotated access token
  cleanly, dependencies are version-pinned, and the Docker image runs as a
  non-root user with a `HEALTHCHECK`.
* **A command-center visual design**: a dark, high-contrast palette built
  for readability under stress (not decoration), a persistent connection
  status indicator (● Live / Reconnecting / Offline) and live clock in the
  top bar, a "hero" card that puts the AI's top recommendation front and
  center above the full comparison table, visual capacity bars on the
  hospital view, and an explicit "Decision support, not clinical
  authority" disclaimer near the point of decision.
* **Fixes from a harsh-judge design review** (see "Design review fixes"
  below for the full list): the recommendation table no longer clips its
  Hospital name/Accept button on a real desktop (not just mobile), the
  Analytics audit log reads as plain English instead of raw JSON, the
  recommendation/assessment cards now hide and show correctly at every
  trip stage, multi-ambulance competition for the same hospital is now
  visually called out (not just inferable from two rows happening to
  match), the map has a legend and a proper "map unavailable" placeholder
  instead of a silent empty rectangle, the Hospital View leads with a
  live KPI strip, and the last few stray inline styles/icon-sizing
  inconsistencies are gone.
* **Explainable-AI score breakdown**: the hero recommendation card has a
  collapsible "How was this calculated?" panel showing the actual
  weight × value for each of the four scored factors (time-to-treatment,
  specialist match, capacity headroom, ED-overload penalty) — the real
  numbers the score was built from, not a display-only approximation —
  plus a "why not the runner-up" comparison against the second-ranked
  viable hospital.
* **Redesigned demo scenarios**: the old checkbox-based "simulate a
  change" panel is now 4 named, explained scenario cards (ICU becomes
  unavailable, ED overloaded, cardiologist on/off duty), each writing one
  real capacity change through the same endpoint a hospital's own
  dashboard would use.
* A **landing/pitch page** at `frontend/index.html` (served at the app's
  root `/` by the existing static mount), built from the PRD/TRD and the
  app's own design tokens, linking to the actual dispatcher console below
  via its "Launch Console" / "Launch Live Simulator" buttons.
* **A second harsh-judge design-review pass** (see "Design review fixes,
  round 2" below): the hospital comparison is now a genuine, compact
  `<table>` instead of stacked cards even on desktop, so all 5 hospitals
  are scannable side by side again without losing the earlier fix's
  zero-overflow guarantee; a raw routing-diagnostic string that used to
  leak onto the live Trip Status card is now a calm summary with the real
  detail behind a collapsed disclosure; every button/input/select now
  renders in the app's own typeface instead of the browser's default UI
  font; the demo-scenarios target hospital defaults to the ambulance's
  actual destination; and a one-line value-prop statement is now visible
  on the very first screen, before any interaction.

## Quick start

```bash
cd backend
pip install -r requirements.txt
uvicorn app:app --reload --port 8000
```

Then open **http://localhost:8000** in a browser — that's the landing/pitch
page. Click **"Launch Console"** (or go straight to
**http://localhost:8000/live-simulator/**) to open the actual dispatcher
console, which is where the live demo (map, recommendation engine, hospital
view, analytics) runs. That's it — the backend also serves both frontends,
so there's nothing else to run or build.

A `local.env` file with a real Google Maps API key is already included in
`backend/`, so real traffic-aware ETA is on by default — check the badge in
the top bar; it should say "live traffic API". If you'd rather use your own
key or Mapbox instead, edit `local.env` directly, or create a standard
`.env` file (copy `.env.example`) — both are read, `.env` takes priority if
both exist. Leave both keys blank and the app falls back to simulated ETA —
the demo works either way. **Before pushing this repo anywhere public (e.g.
a GitHub repo for judging), double check `local.env` isn't committed — it's
in `.gitignore`, but verify with `git status`.**

AI Triage Assist works the same way: set `WATSONX_API_KEY` and
`WATSONX_PROJECT_ID` in `local.env`/`.env` (see `.env.example` for where to
get them) to have the "Suggest condition with AI" button call IBM
watsonx.ai's Granite model for real. Leave them blank and it automatically
uses the keyword classifier instead — the assess card tells you which one is
active (and `/api/health`'s `triage_provider.active_mode` reports it too),
so the demo works, and is honest about which mode it's in, either way.

### Running the tests

```bash
cd backend
pip install -r requirements-dev.txt
python3 -m pytest tests/ -v
```

120 tests, no network or real server needed (FastAPI's `TestClient` runs the
app in-process, and the watsonx.ai call is mocked - see
`tests/test_triage_service.py`) — covers the recommendation engine's edge
cases, the API's error handling / reservation logic, road routing, AI Triage
Assist's watsonx.ai/keyword-fallback classification, and the
security-hardening layer (auth, rate limiting, config parsing, XSS
defense-in-depth). The same command can be run locally before deployment.

A separate, optional file (`tests/test_frontend_smoke.py`) drives a real
Chromium browser via Playwright to check things a Python-only test can't:
the page actually loads with no console errors, the assess → recommend →
select → hospital view → analytics flow works end to end, a phone-width
viewport never overflows horizontally, and a stored-XSS payload really
renders as inert text rather than executing. It's skipped automatically
(not failed) if Playwright/Chromium isn't installed:

```bash
pip install playwright   # already in requirements-dev.txt
playwright install chromium
python3 -m pytest tests/test_frontend_smoke.py -v
```

## Deploying it for real

The app is one process (FastAPI serves the API, WebSocket, *and* the static
frontend), so any container-friendly host works the same way:

```bash
docker build -t coordinator .
docker run -p 8000:8000 --env-file backend/.env coordinator
```

Or, for a platform that runs a `Procfile` directly (Heroku-style) instead of
building the `Dockerfile`, that's included too (`web: cd backend && uvicorn
app:app --host 0.0.0.0 --port ${PORT:-8000}`).

Before pointing this at real traffic, set at minimum:

* `APP_ACCESS_TOKEN` — a long random string. Without this, the app (and
  everyone's audit log, hospital data, etc.) is open to anyone with the URL.
* `ALLOWED_ORIGINS` — your actual frontend origin(s), instead of `*`.
* `DATABASE_URL` — a Postgres URL, so trip/audit history survives a redeploy
  (most container hosts wipe local disk, including SQLite files, on every
  deploy).

`ENVIRONMENT=production` makes the app log a warning on startup for any of
the above still left at its insecure local-dev default, so misconfiguration
is loud instead of silent. Full details on every env var are in
`.env.example`.

## Demo script (suggested)

1. Open the landing page (`http://localhost:8000`) and click **"Launch
   Console"** to open the dispatcher console (`/live-simulator/`) in a new
   tab — that's where the rest of this script happens. You'll see the
   incident marker, 5 hospitals color-coded by status (green = accepting,
   orange = limited, red = not viable/no ICU), and your ambulance.
2. Pick **"Suspected Heart Attack / Cardiac Event"** and click **Assess &
   Get AI Recommendation**. Point at the hero card: the recommended hospital,
   its ETA, estimated treatment delay, and the reasoning behind the pick, all
   in one glance — then the full comparison table below it: the nearest
   hospital is very likely disqualified (no ICU bed), and the recommended
   one is picked for lowest *total time-to-treatment*, not distance — the
   same Hospital A/B/C story from the PRD, generated live.
3. Click **Accept**. Watch the pre-alert fire (check the **Hospital View**
   tab) and the ambulance start moving on the map. Point out the hospital's
   capacity actually decrementing on the map/hospital table — this is a real
   reservation, not just a status label.
4. Click **"🚑 + Launch a second ambulance"**. A second ambulance appears,
   assesses its own (random) patient, and picks its own hospital automatically
   — while your ambulance is still en route. Point at the fleet list showing
   both, and the shared hospital capacity both are drawing from. This is the
   "why does this need to handle more than one ambulance" answer, live.
5. While your ambulance is en route, go to **"Simulate a live change"** and
   knock out the ICU at the hospital it's currently headed to. Within about
   5 seconds a reroute banner appears — accept it and the ambulance changes
   course live, and its reserved bed at the old hospital is released.
6. Switch to **Analytics** to show the KPIs and the full audit trail — this
   is the "so what does the health system get out of this" close. Restart
   the server (`Ctrl+C`, run `uvicorn` again) and reopen the tab to show this
   history survived the restart.
7. Click **Reset demo** to wipe everything (including the audit history) and
   run again with a different condition (try "Suspected Stroke" — a
   different hospital wins because of neurologist availability).

## Security hardening

A security/robustness pass found several real gaps in the production-readiness
layer above; all of them are now fixed:

* **Stored XSS** — `override_reason` and `ambulance_label` are free text
  that gets broadcast live to every connected browser. They were being
  rendered via `.innerHTML`, `JSON.stringify()`-into-`innerHTML`, and
  Leaflet's `bindPopup()` — all three render raw HTML, so a malicious value
  in any of them would have executed for everyone watching that trip. Fixed
  with a single `escapeHtml()` helper (`frontend/app.js`) applied at every
  render site, plus backend `Field(max_length=...)` and control-character
  stripping (`backend/app.py`) as defense-in-depth. Verified against real
  `<script>`/`<img onerror>` payloads (see `tests/test_frontend_smoke.py`).
* **Rate limiter trusting `X-Forwarded-For` unconditionally** — spoofable
  by anyone, letting them dodge the limit or frame another IP. Fixed with
  `TRUST_PROXY_HEADERS` (default `false` — the direct connection's address
  is always used unless you explicitly opt in for a reverse proxy you
  control). **Also fixed a second, subtler instance of the same gap**:
  uvicorn has its *own*, separate X-Forwarded-For trust mechanism
  (independent of this app's setting) that defaults to trusting `127.0.0.1`
  — exactly the topology of nginx-on-the-same-host proxying to uvicorn. The
  `Dockerfile` now sets `FORWARDED_ALLOW_IPS=""` so that gap is closed by
  default too; see the comment there and in `.env.example` if you run
  `uvicorn` directly behind a same-host proxy.
* **No automated tests on the new modules** — `backend/tests/test_auth.py`,
  `test_ratelimit.py`, and `test_config.py` now cover auth, rate limiting
  (including a direct regression test for the X-Forwarded-For spoof), and
  environment-variable parsing.
* `/api/health` no longer shares a rate-limit bucket with real traffic
  (`ratelimit.py`).
* Postgres connections use `pool_pre_ping=True` (`db.py`) so a connection
  a cloud provider silently closed gets replaced instead of surfacing a
  confusing error on the next query.
* The WebSocket reconnect loop (`frontend/app.js`) now handles a rejected
  or rotated access token (close code `4401`) by clearing the stale token
  and re-showing the access gate, instead of looping forever retrying with
  a token that will never be accepted.
* Dependencies are pinned to specific versions (`requirements.txt`,
  `requirements-dev.txt`) instead of open-ended `>=` ranges.
* `Dockerfile` now runs as a non-root user and defines a `HEALTHCHECK`.
* **API key leak via routing failure diagnostics** — added while
  implementing better "why isn't road routing working" messaging (see
  Troubleshooting below): `requests`' own exception text embeds the full
  request URL, `key=...`/`access_token=...` included, and that text is
  surfaced through the *public* `/api/scenario` and `/api/health`
  endpoints so it could show your live Google/Mapbox key straight in the
  trip-status UI to anyone with the page open. Fixed with a `_redact()`
  helper (`routing_service.py`) that strips those query values before a
  failure reason is ever stored, logged, or returned — never shipped
  publicly; caught during this same round of work and covered by
  `test_provider_status_never_leaks_the_api_key_in_a_failure_reason`.

## Troubleshooting

* **"I updated the files but the app still shows the old bug"** — the
  browser tab is almost certainly serving a cached copy of `style.css`/
  `app.js` from before the update. `index.html` now loads both with a
  `?v=...` cache-busting query string specifically so this stops
  happening going forward, but a tab that's been open since *before* that
  change was in place can still be stuck on the old files. Do a hard
  refresh (Ctrl+Shift+R / Cmd+Shift+R), or close and reopen the tab. You
  do **not** need to restart `uvicorn` for a frontend file change to take
  effect — static files are read from disk on every request.
* **"The ambulance drives in a straight line instead of following roads,
  even though I have a Google Maps API key set"** — check the "Route:"
  line in the trip status card once a trip is en route. If it says a key
  IS configured but the live call isn't succeeding, the single most
  common cause is that **Google Maps Platform treats "Directions API" and
  "Distance Matrix API" as two separate products that must each be
  enabled on your project** — a key that's already working for the live
  ETA badge (Distance Matrix) is not automatically enabled for Directions
  too. Fix: in Google Cloud Console → APIs & Services → Library, search
  for "Directions API" and enable it for the same project your key
  belongs to (and confirm the key has no API restriction that excludes
  it). The trip-status message and your backend's console log both show
  the exact status Google returned (e.g. `REQUEST_DENIED`) once a live
  call has actually been attempted, which confirms this immediately.

## Design review fixes

A follow-up pass had a harsh hackathon-judge/principal-product-designer
review of the finished app (live inspection with real screenshots, DOM
measurements, and a manual demo walkthrough — not just a code read), which
scored it against 17 criteria and produced a prioritized top-10 list. Every
P0 (must-fix) and P1 (major improvement) item from that list is now
implemented and re-verified live (fresh Playwright screenshots, a full
94-test pytest run, a manual click-through of the whole demo script, and a
zero-horizontal-overflow check at all 8 responsive breakpoints):

* **P0 — Recommendation table clipped on real desktop, not just mobile.**
  `.side-col` is a fixed ~380px-wide flex item at *every* viewport ≥900px
  wide (the "mobile" stacked-card layout only kicked in below a 640px
  *viewport* media query) — so the 8-column table's real content width
  (~534px) silently overflowed its ~325px container and scrolled the
  Hospital name and Accept/Choose button out of view even at 1440px. Fixed
  *at the time* by making the stacked-card `.rec-table` layout
  unconditional instead of gated behind a viewport-width breakpoint that
  never matched the actual (container-width-constrained) failure case.
  Verified: table content width measured 325px against a 379px container
  (no overflow) at every tested breakpoint, both before and after a
  recommendation renders. **This traded away real side-by-side comparison
  for the fix, though — see "Design review fixes, round 2" below for the
  follow-up that gets both properties at once.**
* **P0 — Audit log was unreadable raw JSON.** Every row in the Analytics
  audit log showed `JSON.stringify(payload)`. Fixed with a category badge
  (`EVENT_LABELS`) plus a `describeAuditEvent()` formatter (`app.js`) that
  turns all 11 backend event types into a plain-English sentence (e.g.
  *"AMB-14: recommended Lakeside Trauma & Heart Institute for cardiac."*)
  using the same hospital/trip name lookups the rest of the UI uses.
* **P0 — Recommendation/assessment cards didn't hide/show correctly at
  every trip stage.** The assessment card stayed visible after a
  recommendation was generated, and the recommendation card didn't
  reliably hide once a trip was en route. Fixed by tying both cards'
  visibility directly to `trip.status` the same way the trip-status card
  already was. Verified by walking a real trip through all four states
  (`awaiting_assessment → recommendation_ready → en_route`) and checking
  each card's `hidden` attribute at each step.
* **P1 — Multi-ambulance competition wasn't visually called out.** Two
  ambulances heading for the same hospital's last beds is the whole point
  of the multi-ambulance architecture, but nothing on screen said so — you
  had to notice two rows shared a destination. Fixed with real (not
  fabricated) contention detection computed from the live trip broadcast:
  an amber "Competing with another ambulance for capacity" note on your
  own card plus a "⚠ competing for capacity" badge on every affected fleet
  card, whenever 2+ en-route trips share a destination hospital. Verified
  live with two independent browser sessions both landing on the same
  hospital.
* **P1 — Icon/glyph sizing inconsistency.** Ad-hoc inline emoji (★ ⚠ ↺ 🔔
  🚑 🗺️) rendered at whatever size each glyph happened to default to. Fixed
  with a shared `.icon` CSS class for consistent sizing/alignment; brand
  and section-header emoji (🚑 in the top bar, 🏥/📊/🔒 in section titles)
  were deliberately left as-is since those are meant to read as a bigger,
  different visual weight than an inline status glyph.
* **P1 — Map had no legend and failed silently.** The color-coded hospital
  markers (green/amber/red) had no key explaining what the colors meant,
  and if the Leaflet/tile CDN was unreachable the map area was just a
  blank dark rectangle with only an easy-to-miss toast explaining why.
  Fixed with an always-visible map legend (shown once the map loads) and a
  proper "Map unavailable" placeholder with an explanation, shown *inside*
  the map area itself when it fails. Verified against a real CDN failure
  (this sandbox blocks `unpkg.com`, the same restriction noted for
  Google's routing API — a genuine network condition, not a simulated
  one).
* **P1 — Hospital View was sparse.** It jumped straight from the page
  title to a list of pre-alerts with no at-a-glance summary. Fixed with a
  KPI strip (ambulances inbound, how many are a critical/Red-severity
  condition, how many arrive in under 15 minutes) computed from the same
  live trip data as the rest of the page — no fabricated numbers.
* A handful of remaining inline `style="..."` attributes (the override
  card's button row, the access-gate error text) were moved into proper
  CSS classes for consistency with the rest of the stylesheet.

## Design review fixes, round 2

A second harsh-judge/principal-product-designer pass (live Playwright
screenshots, computed-style/font audits, a real network capture of every
demo-scenario request, and a full desktop + mobile walkthrough) scored the
app against the same 17 criteria and produced 2 P0s + 4 P1s. All are now
implemented and re-verified live (99/99 tests, zero horizontal overflow at
8 breakpoints from 360px to 1440px, and a full demo-flow walkthrough with
no new console errors):

* **P0 — The round-1 fix above gave up real comparison entirely.** Every
  hospital row rendered as one stacked key/value card, even on a full
  1440px desktop — safe, but it lost the "scan all 5 hospitals at a
  glance" moment this table exists for. Fixed properly instead of
  re-widening the panel: the table is now a genuine `<table>`
  (`table-layout: fixed`, 4 narrow columns — Hospital / Delay / Status /
  Action — with ICU + specialist + ED-load collapsed into one compact but
  still-legible "Status" cell of short text+color badges, see
  `statusCellHtml()` in `app.js`) that fits the panel's real ~330–380px
  content width at every viewport from 360px up. Verified: zero horizontal
  overflow at 8 breakpoints (360/390/480/768/900/1024/1280/1440px) and the
  Accept/Choose button always on-screen, both before and after the table
  and the demo-scenarios panel render.
* **P0 — A raw diagnostic string leaked onto the live Trip Status card.**
  `routeSourceMessage()` used to inline the routing service's actual
  failure reason — a redacted-but-still-stack-trace-shaped string,
  connection-pool internals and all — directly into the operator-facing
  status line. Never insecure (the key redaction from the earlier routing
  fix was never the issue), but it reads as broken mid-demo. Split into a
  calm always-shown summary and a collapsed "Technical details"
  disclosure holding the real reason, only when one was recorded — same
  on-demand-detail pattern as "why?" and "How was this calculated?"
  elsewhere. Verified: the raw reason never appears in rendered text by
  default, and is still fully there, correctly redacted, once expanded.
* **P1 — Every form control rendered off-brand.** A computed-style audit
  found all 30 buttons/inputs/selects/options on the page reporting
  `Arial`, not the app's Inter/IBM Plex Sans — browsers never inherit
  typographic properties into form controls by default, and nothing
  overrode that. Fixed with one rule; re-verified 0/30 offenders.
* **P1 — Triggering the reroute demo took an extra manual step.** The
  demo-scenarios target-hospital dropdown didn't default to the
  ambulance's actual destination, so showing the marquee "watch it
  reroute live" moment meant picking it out by name first. Fixed with
  `syncSimHospitalDefault()`, which keeps it defaulted to the live
  destination without ever overriding a deliberate manual pick.
* **P1 — The working app's first screen didn't state the pitch.** Only a
  button label implied the core "not nearest, but fastest to actual
  treatment" claim. Added a one-sentence `.value-prop` strip as the first
  thing in the side panel, visible before any interaction.
* **P1 — The side panel got very long with both panels open.** Mostly
  resolved by the compact-table fix above (each row went from ~180px to a
  few table rows); the demo-scenarios panel now also auto-collapses once
  a scenario finishes applying, since its result is already visible via
  toast and (when relevant) the reroute banner.

Two polish items found during verification were fixed in the same pass:
toast notifications (fixed top-right) were confirmed overlapping the
Analytics/Hospital KPI cards in a live screenshot, so they moved to
bottom-right and were re-verified with zero overlap; and the hero
recommendation's label was confirmed wrapping to two lines at the panel's
width, so it was shortened and re-verified on one line.

## Code review fixes (post-launch hardening pass)

A full read-through of the PRD/TRD, delivery notes, and the entire codebase
(prompted by a "study everything and tell me what needs fixing" pass) turned
up one real functional bug and several project-hygiene gaps. All are now
fixed and verified:

* **P0 — the landing page silently created a real ambulance trip on every
  visit.** The landing/pitch page's `frontend/app.js` was a near-duplicate
  of the dispatcher console's own `app.js`, and its `init()` still called
  `startMyTrip()` (a `POST /api/trips`) and opened a live WebSocket on
  every single load of `frontend/index.html` — not just the actual
  dispatcher console. That meant anyone just reading the pitch page (a
  judge, a link shared casually) silently spawned a phantom ambulance,
  polluting the fleet list and the Analytics audit trail with trips nobody
  dispatched. Root cause: the rest of that `init()` targeted DOM elements
  (`#map`, `#conditionSelect`, `#hospTable`, etc.) that don't exist on the
  landing page, so it silently no-opped after creating the trip — the bug
  was invisible unless you were watching the audit log. Fixed by trimming
  the landing page's `app.js` from 838 lines to 293: removed the entire
  dead dispatcher block and replaced `init()` with only the three calls the
  landing page actually needs (`setupNavigation()`,
  `setupAnimatedCounters()`, `initTheme()`). Verified with `node --check`
  and a grep pass confirming `index.html` has zero references to any
  removed function (`initMap`, `startMyTrip`, `connectWs`, `renderAll`,
  and others).
* **Project had no CI or version control.** `ci.yml` (pytest on every
  push/PR, plus a Docker build sanity check) was sitting in an unrelated
  delivery-notes folder outside the repo, so GitHub Actions would never
  have picked it up — moved to its correct location,
  `.github/workflows/ci.yml`. The project also had no git repository at
  all; initialized one at `prototype/` (the app root) with a `.gitignore`
  covering `.venv/`, `__pycache__/`, `.pytest_cache/`, `.env`/`local.env`
  at any depth, and the local SQLite database files, and made an initial
  commit.
* **This README had drifted from the actual code in three places** —
  fixed: the Quick Start and Demo Script sections implied
  `http://localhost:8000` opens the dispatcher console directly, when it
  actually opens the landing page (the console is one click away, at
  `/live-simulator/`); the "What's included" list claimed the landing page
  lived at `frontend/site/index.html` (no such path exists — it's
  `frontend/index.html`, served at the app root); and the Project
  Structure tree was missing `frontend/live-simulator/` entirely and
  mislabeled the root `frontend/index.html` as the map/recommendation
  dispatcher dashboard, when that's actually the landing page.

## Notes on what's simplified for the demo (see TRD for the full design)

* Persistence is SQLite for the audit log / trip history only — live,
  in-transit trip state and hospital capacity are still in-memory for speed,
  so an *unplanned* crash mid-trip loses the in-flight ambulance (its history
  up to that point is safe). The TRD's real version needs a proper data
  store for live state too, plus real HIS integration for hospital capacity.
* A single coarse-grained lock (`STATE_LOCK` in `app.py`) guards all shared
  state. This is a deliberate simplification, not an oversight: at
  demo/pilot request volume it costs nothing and makes correctness easy to
  reason about. Splitting it into finer-grained per-hospital/per-trip locks
  is a reasonable next step once real concurrent load makes the coarse lock
  measurably slow — not before.
* The ambulance's "movement" is a scripted interpolation toward the chosen
  hospital at accelerated speed, not real GPS — see TRD Section 14.
* Authentication is a single shared secret (`APP_ACCESS_TOKEN`), not
  per-user login/RBAC — right for a demo/pilot behind one link, not for a
  real multi-hospital deployment with different roles per user. Both this
  and CORS default wide open (`allow_origins=["*"]`) for zero-friction local
  dev; both should be locked down (see "Deploying it for real" above)
  before this is reachable over any network you don't fully trust.
* All hospital names/data are fictional, generated for this demo — they are
  not real facilities.

## Project structure

```
prototype/
  backend/
    app.py                     FastAPI app: multi-trip routes, websocket, ambulance sim, locking
    config.py                  Centralized env-var config + startup warnings
    auth.py                    Shared-secret API/WebSocket access gate
    ratelimit.py                Per-IP rate limiting middleware
    sentry_init.py             Optional error tracking (no-op unless SENTRY_DSN set)
    recommendation_engine.py   The AI scoring/reasoning engine (TRD Section 5)
    eta_service.py             Real ETA API wrapper + seeded simulated fallback
    routing_service.py         Real road-route wrapper (Google/Mapbox Directions) + straight-line fallback
    triage_service.py          IBM watsonx.ai triage classification + handover notes, keyword/template fallback
    db.py                      SQLAlchemy persistence (SQLite locally, Postgres via DATABASE_URL)
    env_loader.py              Reads .env / local.env
    utils.py                   Distance + freshness helpers + road-route interpolation
    data/
      hospitals.json           Mock hospital capacity/specialist data
      severity_rules.json      Condition -> required capability mapping + scoring weights
      scenario.json            Demo incident location
    tests/
      conftest.py                     Shared fixtures (rate-limit counter isolation between tests)
      test_recommendation_engine.py   Unit tests for the scoring/disqualification logic
      test_api.py                     API-level tests (status codes, reservation, multi-trip)
      test_routing.py                 Unit tests for road-route interpolation + polyline decoding + fallback
      test_auth.py                    Access-gate middleware + WebSocket auth tests
      test_ratelimit.py               Rate limiter tests, incl. the X-Forwarded-For spoof regression test
      test_config.py                  Env-var parsing + startup-warning tests
      test_security_escaping.py       Backend XSS defense-in-depth (length caps, control-char stripping)
      test_triage_service.py          AI Triage Assist: watsonx.ai classification + handover notes + keyword/template fallback
      test_frontend_smoke.py          Optional real-browser (Playwright) smoke + XSS + mobile-overflow tests
    requirements.txt
    requirements-dev.txt       Adds pytest/httpx/playwright for the test suite
    pytest.ini
    .env.example
  frontend/
    index.html                 Landing/pitch page — links to the dispatcher console below
    app.js
    style.css
    live-simulator/
      index.html                Dispatcher console: map, recommendation engine, hospital view, analytics
      app.js
      style.css
  Dockerfile                   Single-container deploy (backend + static frontend), non-root, HEALTHCHECK
  Procfile                     Heroku-style process declaration
  .dockerignore
  .gitignore
  .github/
    workflows/
      ci.yml                    GitHub Actions: pytest on push/PR + a Docker build sanity check
```
