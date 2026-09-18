"""
Shared pytest fixtures for the whole test suite.

Session-wide isolation concern this file exists to solve: FastAPI's
TestClient always presents as the same client host ("testclient"), and
ratelimit.py's counters are a plain module-level dict keyed by client IP -
so every test file in a single pytest run shares ONE rate-limit bucket for
that host. Without a reset between tests, a test that intentionally sends
enough requests to trigger a 429 (see test_ratelimit.py) would "spend" that
budget for every other test file that happens to run afterward in the same
process, causing unrelated tests to fail with spurious 429s depending on
test order/count - exactly the kind of flaky, order-dependent failure a
good test suite should never allow. Clearing the counters before AND after
every single test keeps each test's view of the rate limiter fully
independent of what ran before or after it.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

import models  # noqa: E402
import ratelimit  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_rate_limit_counters():
    ratelimit._counters.clear()
    yield
    ratelimit._counters.clear()


@pytest.fixture(scope="session", autouse=True)
def _ensure_orm_tables():
    """Test-only schema bootstrap for models.py's ORM tables (users,
    hospitals, ...) - a real deployment always uses `alembic upgrade head`
    instead (see models.init_models()'s docstring)."""
    models.init_models()
