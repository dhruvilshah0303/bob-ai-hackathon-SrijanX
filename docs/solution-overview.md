# Solution Overview

## What We Built

SrijanX is a real, multi-user emergency coordination platform backed by FastAPI + PostgreSQL. It turns a dispatcher's description of a patient into an explainable destination recommendation based on fastest expected *treatment* - not distance - and coordinates the resulting hospital acceptance, ambulance dispatch, and live GPS-tracked trip through to completion.

## How It Works

1. A dispatcher creates an emergency with the patient's symptoms (free text) and vitals.
2. AI-assisted triage classifies the case into a condition and maps it to required capabilities (ICU, ED capacity, a specialist) - decision support, never a diagnosis.
3. Hospitals that fail a hard clinical or capacity constraint are excluded from consideration entirely.
4. The remaining hospitals are scored on real traffic-aware ETA, hospital wait, specialist match, and capacity headroom, and ranked by total time-to-treatment.
5. The dispatcher requests a ranked hospital; that hospital's own staff accept (transactionally reserving an ICU bed/ED bay) or decline.
6. Once accepted, the dispatcher dispatches an available ambulance; the assigned crew streams real GPS from their phone/browser.
7. If hospital capacity or traffic changes mid-route, the dispatcher can approve a reroute - the new hospital's capacity is reserved before the old one is released, so the patient is never left without a held bed.
8. Every step is audited and pushed to affected users in real time.

## What Makes It Different

The system optimizes time-to-treatment instead of distance and exposes the full comparison behind the decision - every disqualified hospital shows why. Capacity is modeled as a genuinely shared, contended resource: two emergencies racing for a hospital's last ICU bed are resolved by an atomic database operation, not a hope that it won't happen. Nothing about the coordination workflow itself is simulated - the same real API a dispatcher uses is what a hospital and ambulance crew use too.

## IBM Technologies Used

AI-assisted triage and hospital handover-note generation (`src/backend/triage_service.py`) call **IBM watsonx.ai**'s Granite instruct model when `WATSONX_API_KEY`/`WATSONX_PROJECT_ID` are configured, via the watsonx.ai text-generation REST API. Judges/reviewers without those credentials still get a fully working system - both features fall back automatically to a labeled, deterministic non-AI path (a keyword classifier and a template), and every triage/handover result states which mode actually answered it.
