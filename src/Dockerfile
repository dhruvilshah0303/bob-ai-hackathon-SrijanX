# AI Ambulance-to-Hospital Coordinator - single-container image.
#
# The FastAPI backend serves both the JSON/WebSocket API AND the static
# frontend (see app.py's StaticFiles mount), so one image + one process is
# enough to deploy the whole demo. Works as-is on Render/Railway/Fly/Heroku-
# style "build a Dockerfile, run it, expose $PORT" platforms.
FROM python:3.11-slim

WORKDIR /app

# Install dependencies first so Docker can cache this layer when only app
# code changes.
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend/ ./backend/
COPY frontend/ ./frontend/

WORKDIR /app/backend

# Most platforms inject $PORT at runtime; 8000 is the local-dev fallback.
ENV PORT=8000
EXPOSE 8000

# Shell form so $PORT is expanded at container start, not at build time.
CMD uvicorn app:app --host 0.0.0.0 --port ${PORT}
