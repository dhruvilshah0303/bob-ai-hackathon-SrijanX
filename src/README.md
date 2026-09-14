# Source Code

The application is organized as a small web system:

- `backend/` contains the FastAPI application, recommendation engine, ETA service, persistence layer, JSON demo data, and pytest tests.
- `frontend/` contains the static browser dashboard, Leaflet map, and WebSocket client.
- `Dockerfile` builds the backend and frontend into one Railway-compatible container.
- `Procfile` provides a platform-style Uvicorn start command.

The frontend uses same-origin requests by default. For a separate Vercel frontend, set `window.COORDINATOR_BACKEND_URL` in `frontend/config.js` to the public Railway URL.
