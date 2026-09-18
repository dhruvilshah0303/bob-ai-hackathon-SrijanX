"""
Tests for config.py: environment-variable parsing, and the startup_warnings
function that nags about specific, silently-dangerous misconfigurations.

config.py is deliberately a plain module of constants read ONCE at import
time (see its own docstring) - correct for how the real app runs, but it
means the parsing logic can't be exercised by monkeypatching os.environ and
re-reading an already-imported module's attributes (they're already fixed).
Rather than importlib.reload() - which would mutate the *same* config
module object every other already-imported test module in this session
holds a reference to, risking leaking altered flags (e.g. IS_PRODUCTION)
into unrelated tests depending on run order - the parsing tests below spawn
a short-lived subprocess with a controlled environment for each scenario.
Slower per-test, but fully isolated, and honestly exercises exactly what
happens when `uvicorn app:app` imports this module for real.

The startup_warnings tests, by contrast, exercise the *current* in-process
module directly via pytest's `monkeypatch` fixture, which is always safe -
it automatically reverts every setattr at the end of each test.
"""
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

import config as config_module  # noqa: E402

BACKEND_DIR = Path(__file__).parent.parent


def _read_config(env_overrides: dict) -> dict:
    """Run a fresh interpreter that imports config.py with the given
    environment, and dump the resulting values as JSON."""
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(BACKEND_DIR)}
    env.update(env_overrides)
    code = (
        "import config, json; "
        "print(json.dumps({"
        "'RATE_LIMIT_ENABLED': config.RATE_LIMIT_ENABLED, "
        "'RATE_LIMIT_PER_MINUTE': config.RATE_LIMIT_PER_MINUTE, "
        "'TRUST_PROXY_HEADERS': config.TRUST_PROXY_HEADERS, "
        "'ALLOWED_ORIGINS': config.ALLOWED_ORIGINS, "
        "'IS_PRODUCTION': config.IS_PRODUCTION, "
        "'APP_ACCESS_TOKEN': config.APP_ACCESS_TOKEN, "
        "}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(BACKEND_DIR), env=env, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, f"config import failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_defaults_are_safe_for_local_dev():
    cfg = _read_config({})
    assert cfg["RATE_LIMIT_ENABLED"] is True
    assert cfg["TRUST_PROXY_HEADERS"] is False  # SECURITY: must default closed
    assert cfg["ALLOWED_ORIGINS"] == ["*"]
    assert cfg["IS_PRODUCTION"] is False
    assert cfg["APP_ACCESS_TOKEN"] is None


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes"])
def test_trust_proxy_headers_truthy_values(value):
    cfg = _read_config({"TRUST_PROXY_HEADERS": value})
    assert cfg["TRUST_PROXY_HEADERS"] is True


@pytest.mark.parametrize("value", ["false", "FALSE", "0", "no", "", "garbage"])
def test_trust_proxy_headers_defaults_closed_for_anything_else(value):
    # SECURITY: a typo in this env var (e.g. "1rue") must never silently
    # open the gap - only recognized truthy strings should enable it.
    cfg = _read_config({"TRUST_PROXY_HEADERS": value})
    assert cfg["TRUST_PROXY_HEADERS"] is False


@pytest.mark.parametrize("value,expected", [("false", False), ("0", False), ("no", False), ("FALSE", False)])
def test_rate_limit_can_be_disabled(value, expected):
    cfg = _read_config({"RATE_LIMIT_ENABLED": value})
    assert cfg["RATE_LIMIT_ENABLED"] is expected


def test_rate_limit_per_minute_is_parsed_as_int():
    cfg = _read_config({"RATE_LIMIT_PER_MINUTE": "42"})
    assert cfg["RATE_LIMIT_PER_MINUTE"] == 42


def test_allowed_origins_comma_separated():
    cfg = _read_config({"ALLOWED_ORIGINS": "https://a.example.com, https://b.example.com"})
    assert cfg["ALLOWED_ORIGINS"] == ["https://a.example.com", "https://b.example.com"]


def test_allowed_origins_wildcard_by_default():
    cfg = _read_config({"ALLOWED_ORIGINS": "*"})
    assert cfg["ALLOWED_ORIGINS"] == ["*"]


def test_app_access_token_blank_means_disabled():
    cfg = _read_config({"APP_ACCESS_TOKEN": "   "})
    assert cfg["APP_ACCESS_TOKEN"] is None


def test_app_access_token_set_is_preserved():
    cfg = _read_config({"APP_ACCESS_TOKEN": "s3cret-value"})
    assert cfg["APP_ACCESS_TOKEN"] == "s3cret-value"


def test_environment_production_flag():
    cfg = _read_config({"ENVIRONMENT": "production"})
    assert cfg["IS_PRODUCTION"] is True


def test_environment_defaults_to_development():
    cfg = _read_config({})
    assert cfg["IS_PRODUCTION"] is False


# ------------------------------------------------------- startup_warnings -


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def _warnings_for(monkeypatch, **overrides):
    for key, value in overrides.items():
        monkeypatch.setattr(config_module, key, value)
    logger = logging.getLogger("test.config.startup_warnings")
    handler = _ListHandler()
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    try:
        config_module.startup_warnings(logger)
    finally:
        logger.removeHandler(handler)
    return handler.messages


def test_no_warnings_in_development_with_defaults(monkeypatch):
    messages = _warnings_for(
        monkeypatch, IS_PRODUCTION=False, ALLOWED_ORIGINS=["*"], APP_ACCESS_TOKEN=None, DATABASE_URL=None,
    )
    assert messages == []


def test_production_with_open_cors_warns(monkeypatch):
    messages = _warnings_for(
        monkeypatch, IS_PRODUCTION=True, ALLOWED_ORIGINS=["*"], APP_ACCESS_TOKEN="secret", DATABASE_URL="postgresql://x",
        JWT_SECRET_IS_GENERATED=False,
    )
    assert any("ALLOWED_ORIGINS" in m for m in messages)


def test_production_with_no_access_token_warns(monkeypatch):
    messages = _warnings_for(
        monkeypatch, IS_PRODUCTION=True, ALLOWED_ORIGINS=["https://real.example.com"],
        APP_ACCESS_TOKEN=None, DATABASE_URL="postgresql://x", JWT_SECRET_IS_GENERATED=False,
    )
    assert any("APP_ACCESS_TOKEN" in m for m in messages)


def test_production_with_sqlite_warns(monkeypatch):
    messages = _warnings_for(
        monkeypatch, IS_PRODUCTION=True, ALLOWED_ORIGINS=["https://real.example.com"],
        APP_ACCESS_TOKEN="secret", DATABASE_URL=None, JWT_SECRET_IS_GENERATED=False,
    )
    assert any("DATABASE_URL" in m for m in messages)


def test_production_with_generated_jwt_secret_warns(monkeypatch):
    messages = _warnings_for(
        monkeypatch, IS_PRODUCTION=True, ALLOWED_ORIGINS=["https://real.example.com"],
        APP_ACCESS_TOKEN="secret", DATABASE_URL="postgresql://x", JWT_SECRET_IS_GENERATED=True,
    )
    assert any("JWT_SECRET" in m for m in messages)


def test_production_fully_configured_warns_nothing(monkeypatch):
    messages = _warnings_for(
        monkeypatch, IS_PRODUCTION=True, ALLOWED_ORIGINS=["https://real.example.com"],
        APP_ACCESS_TOKEN="secret", DATABASE_URL="postgresql://x", JWT_SECRET_IS_GENERATED=False,
    )
    assert messages == []
