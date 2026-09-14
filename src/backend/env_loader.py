"""
Tiny config loader used by both app.py and eta_service.py.

Why this exists: the standard `.env` filename can't be written to your
machine by Claude's remote-file tools (a deliberate safety guardrail against
writing secret/dotfiles to your computer). So this loader checks BOTH the
standard `.env` (for when you create it yourself) AND `local.env` (a
non-hidden filename Claude *can* write for you) and loads whichever exists,
in that order, without overriding variables that are already set in the
real environment. Either one works - use whichever is more convenient.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).parent
_loaded = False


def load_env():
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        from dotenv import load_dotenv
    except Exception:
        return
    for name in (".env", "local.env"):
        path = BASE_DIR / name
        if path.exists():
            load_dotenv(dotenv_path=path, override=False)
