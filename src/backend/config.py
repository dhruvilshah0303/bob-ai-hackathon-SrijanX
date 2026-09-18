"""
Centralized runtime configuration, read once from environment variables
(via env_loader, so .env/local.env work the same as real platform env vars).

Every setting here has a safe, zero-friction default for local development -
nothing in this file requires configuration to run `uvicorn app:app` on your
laptop. Each one becomes meaningful the moment you deploy somewhere real;
see the README's Deployment section for what to set and why.
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

# Real user accounts (see auth_service.py/auth_routes.py) - every /api/*
# endpoint except /api/auth/register and /api/auth/login requires a valid
# JWT; there is no shared-secret fallback.
#
# No safe default for JWT_SECRET: unlike every other setting in this file,
# a missing/guessable secret lets anyone forge a valid session token for any
# user, including ADMIN - there's no "insecure but demo-able" middle ground
# the way there is for e.g. open CORS. A random one is generated at process
# startup if unset, so local dev still works with zero setup, but that means
# every restart invalidates every existing session - startup_warnings()
# below nags loudly if this happens with ENVIRONMENT=production, where it
# would silently log everyone out on every deploy.
_JWT_SECRET_ENV = os.environ.get("JWT_SECRET", "").strip()
if _JWT_SECRET_ENV:
    JWT_SECRET = _JWT_SECRET_ENV
    JWT_SECRET_IS_GENERATED = False
else:
    import secrets as _secrets
    JWT_SECRET = _secrets.token_urlsafe(48)
    JWT_SECRET_IS_GENERATED = True

JWT_ALGORITHM = "HS256"
JWT_ACCESS_EXPIRE_MINUTES = int(os.environ.get("JWT_EXPIRE_MINUTES", "60"))
JWT_REFRESH_EXPIRE_DAYS = int(os.environ.get("JWT_REFRESH_EXPIRE_DAYS", "7"))

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
    if IS_PRODUCTION and DATABASE_URL is None:
        logger.warning(
            "ENVIRONMENT=production but DATABASE_URL is not set - audit/trip history is a local SQLite "
            "file, which most hosting platforms erase on every redeploy. Set DATABASE_URL to a managed "
            "Postgres instance if that history should actually survive."
        )
    if IS_PRODUCTION and JWT_SECRET_IS_GENERATED:
        logger.warning(
            "ENVIRONMENT=production but JWT_SECRET is not set - a random secret was generated for this "
            "process only, so every existing login session is invalidated on every restart/redeploy, and "
            "horizontal scaling (multiple instances) would reject each other's tokens. Set JWT_SECRET to "
            "a long random string that stays fixed across deploys."
        )
