"""Shared loader for data/severity_rules.json - the condition ->
required-capability/preferred-specialist table both emergency_routes.py
(triage) and trip_routes.py (recommendation + reservation) key off of, so
they can never silently disagree about what a given condition_code
requires."""
import json
from functools import lru_cache
from pathlib import Path

_DATA_DIR = Path(__file__).parent / "data"


@lru_cache(maxsize=1)
def load() -> dict:
    with open(_DATA_DIR / "severity_rules.json") as f:
        return json.load(f)


def condition_by_code() -> dict:
    return {c["code"]: c for c in load()["conditions"]}
