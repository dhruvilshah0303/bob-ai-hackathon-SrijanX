"""
Centralized runtime configuration, read once from environment variables
(via env_loader, so .env/local.env work the same as real platform env vars).

Every setting here has a safe, zero-friction default for local development -
nothing in this file requires configuration to run `uvicorn app:app` on your
laptop. Each one becomes meaningful the moment you deploy somewhere real;
see PRODUCTION_READINESS_RECOMMENDATIONS.md and the README's Deployment
section for what to set and why.
"""
import logging
import os

from env_loader import load_env

load_env()

ENVIRONMENT = os.environ.get("ENVIRONMENT", "development").strip().lower()
IS_PRODUCTION = ENVIRONMENT == "production"

# CORS: comma-separated list of allowed origins, e.g.
#   ALLOWED_ORIGINS=https://your-app.example.com,https://staging.example.com
# Defaults to "*" (allow everything) because that's correct for local dev
# (frontend and backend share an origin anyway). A real deployment should
# always set this explicitly - we log a warning at startup if it's still "*"
# while ENVIRONMENT=production.
_raw_origins = os.environ.get("ALLOWED_ORIGINS", "*").strip()
ALLOWED_ORIGINS = ["*"] if _raw_origins == "*" else [o.strip() for o in _raw_origins.split(",") if o.strip()]

# Shared-secret access gate for the API. Unset (the default) means auth is
# OFF - fine for local dev, not fine for a public URL. Set this to a random
# string before deploying anywhere reachable by strangers; the frontend will
# then prompt for it once (see app.js) and remember it in localStorage.
APP_ACCESS_TOKEN = os.environ.get("APP_ACCESS_TOKEN", "").strip() or None

# Real persistence. Unset -> SQLite file next to the code (fine for local
# dev; most hosting platforms wipe local disk on redeploy, so this matters
# once you deploy - see db.py). Set to a Postgres URL
# (postgresql://user:pass@host:port/dbname) for real persistence.
# Some platforms hand out "postgres://" - db.py normalizes that for you.
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip() or None

# Basic in-memory rate limiting (per-process, per-IP) - see
# ratelimit.py. Deliberately simple: this app is architected to run as a
# single instance (see README's "state in memory" note), so an in-memory
# limiter is consistent with that, not a workaround for it.
RATE_LIMIT_PER_MINUTE = int(os.environ.get("RATE_LIMIT_PER_MINUTE", "120"))
RATE_LIMIT_ENABLED = os.environ.get("RATE_LIMIT_ENABLED", "true").strip().lower() not in ("0", "false", "no")

# SECURITY: the rate limiter identifies a client by IP address so it can cap
# requests per-IP. By default it uses the direct TCP connection's address
# ONLY. It will NOT read the "X-Forwarded-For" header unless you explicitly
# set this to true - that header is client-controllable, so trusting it
# blindly lets anyone bypass the rate limit by sending a fake header (or
# spoof another user's IP to get THEM rate-limited). Only enable this if you
# know your deployment sits behind a reverse proxy/load balancer that you
# control and that OVERWRITES this header itself (e.g. a platform like
# Render/Heroku/Railway/nginx configured to strip client-supplied values) -
# never enable it if the app is reachable directly by untrusted clients.
TRUST_PROXY_HEADERS = os.environ.get("TRUST_PROXY_HEADERS", "false").strip().lower() in ("1", "true", "yes")

# Optional error tracking - only initializes if a DSN is provided.
SENTRY_DSN = os.environ.get("SENTRY_DSN", "").strip() or None

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").strip().upper()


def configure_logging():
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


def startup_warnings(logger: logging.Logger):
    """Loud, explicit warnings for the specific misconfigurations that are
    silently dangerous rather than loudly broken - the ones a demo running
    on localhost would never surface."""
    if IS_PRODUCTION and ALLOWED_ORIGINS == ["*"]:
        logger.warning(
            "ENVIRONMENT=production but ALLOWED_ORIGINS is not set (defaulting to '*'). "
            "Any website can call this API from a browser. Set ALLOWED_ORIGINS to your real frontend origin."
        )
    if IS_PRODUCTION and APP_ACCESS_TOKEN is None:
        logger.warning(
            "ENVIRONMENT=production but APP_ACCESS_TOKEN is not set. The API has no access control - "
            "anyone with the URL can create/cancel trips and change hospital capacity. Set APP_ACCESS_TOKEN."
        )
    if IS_PRODUCTION and DATABASE_URL is None:
        logger.warning(
            "ENVIRONMENT=production but DATABASE_URL is not set - audit/trip history is a local SQLite "
            "file, which most hosting platforms erase on every redeploy. Set DATABASE_URL to a managed "
            "Postgres instance if that history should actually survive."
        )
