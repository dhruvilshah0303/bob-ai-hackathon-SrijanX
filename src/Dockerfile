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

# SECURITY: don't run the app as root inside the container. A non-root user
# limits blast radius if the app process is ever compromised (can't write
# outside its own files, can't install/modify system packages, etc). The
# SQLite file and any local .env live under /app/backend, which this user
# owns, so local-dev-style SQLite persistence still works unmodified.
RUN useradd --create-home --uid 1000 appuser && chown -R appuser:appuser /app
USER appuser

WORKDIR /app/backend

# Most platforms inject $PORT at runtime; 8000 is the local-dev fallback.
ENV PORT=8000
EXPOSE 8000

# SECURITY: uvicorn has its OWN, separate notion of trusting X-Forwarded-For
# (uvicorn.middleware.proxy_headers.ProxyHeadersMiddleware), independent of
# this app's TRUST_PROXY_HEADERS setting in config.py/ratelimit.py - and its
# default (FORWARDED_ALLOW_IPS unset -> trusts 127.0.0.1) silently rewrites
# the request's client IP whenever the TCP connection reaching uvicorn comes
# from localhost, which is exactly what happens in the extremely common
# "nginx/Caddy on the same host, proxying to 127.0.0.1:8000" deployment
# shape - meaning that setup would trust a client-supplied X-Forwarded-For
# header even with TRUST_PROXY_HEADERS=false. Default this closed (trust no
# proxy at the uvicorn layer either); a deployment that puts a real, trusted
# reverse proxy in front on the same host should explicitly set this to that
# proxy's IP (or override at `docker run -e FORWARDED_ALLOW_IPS=...`).
ENV FORWARDED_ALLOW_IPS=""

# Lets `docker ps` / orchestrators (Compose, Kubernetes, most PaaS hosts)
# see the container is actually serving traffic, not just alive - hits the
# same unauthenticated, rate-limit-exempt endpoint a real load balancer
# would use. Uses Python's stdlib (urllib) rather than curl/wget so it works
# without adding OS packages to this slim image.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/api/health', timeout=3)" || exit 1

# Shell form so $PORT is expanded at container start, not at build time.
# Migrations run here (not just in render.yaml's buildCommand) so this
# image is self-sufficient on any plain `docker run` deployment, not only
# Render's - a fresh DATABASE_URL otherwise has no schema at all.
CMD alembic upgrade head && uvicorn app:app --host 0.0.0.0 --port ${PORT}
