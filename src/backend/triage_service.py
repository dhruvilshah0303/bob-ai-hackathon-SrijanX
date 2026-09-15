"""
AI Triage Assist - two IBM watsonx.ai (Granite) capabilities that both
follow the same graceful-degradation shape as eta_service.py /
routing_service.py: try a real model call first, and if no credentials are
configured or the live call fails for any reason, fall back to a
transparent, non-AI path instead of breaking the demo or guessing
silently.

1. classify(note, conditions) - classifies a dispatcher's free-text note
   into a suggested condition code (see data/severity_rules.json for the
   code list), so a dispatcher can type what they're seeing/hearing
   instead of hunting through a dropdown under time pressure. Falls back
   to a labeled keyword classifier (see _keyword_classify). Returns:

       {"condition_code": str, "confidence": float 0-1, "reasoning": str,
        "source": "watsonx" | "keyword_fallback", "model_id": str | None}

   This is a suggestion only - it prefills the dispatcher's condition
   select, it never sets a trip's condition_code directly (app.py still
   requires the existing POST .../case call to actually commit to a
   condition and generate a hospital recommendation).

2. generate_handover_note(note, condition, eta_min, hospital_name) - drafts
   a short natural-language clinical handover sentence that goes out with
   the hospital pre-alert (app.py's POST .../select), instead of the
   receiving ED only ever seeing a bare condition code and an ETA. Falls
   back to a deterministic template built from the same structured fields
   (see _template_handover_note). Returns:

       {"summary": str, "source": "watsonx" | "template_fallback",
        "model_id": str | None}

Same "decision support, not clinical/dispatch authority" principle the
rest of this prototype follows: neither capability's output is ever
treated as ground truth, only as a prefilled draft a human already sees.

IBM watsonx.ai setup (see backend/.env.example): set WATSONX_API_KEY (an
IBM Cloud API key) and WATSONX_PROJECT_ID (a watsonx.ai project id). This
module exchanges the API key for a short-lived IAM bearer token (cached
until shortly before it expires) and calls the watsonx.ai text-generation
REST API directly - no SDK dependency, consistent with how routing_service
and eta_service call their providers with plain `requests` calls.
"""
import json
import logging
import os
import re
import time

import requests

from env_loader import load_env

load_env()  # idempotent - same defensive import as eta_service.py/routing_service.py

logger = logging.getLogger(__name__)

_IAM_TOKEN_URL = "https://iam.cloud.ibm.com/identity/token"
_API_VERSION = "2024-05-31"

# Surfaced via provider_status(), same purpose as routing_service's
# _last_failure_reason: tells "no credentials configured at all" apart from
# "credentials ARE set but the live call is failing" - these look identical
# from the outside (both silently fall back to the keyword classifier) but
# need completely different fixes.
_last_failure_reason: str | None = None

_iam_token_cache = {"token": None, "expires_at": 0.0}


def _api_key() -> str:
    return os.environ.get("WATSONX_API_KEY", "").strip()


def _project_id() -> str:
    return os.environ.get("WATSONX_PROJECT_ID", "").strip()


def _base_url() -> str:
    return os.environ.get("WATSONX_URL", "https://us-south.ml.cloud.ibm.com").strip().rstrip("/")


def _model_id() -> str:
    # ibm/granite-3-8b-instruct is a current watsonx.ai foundation-model-
    # catalog Granite instruct model at time of writing; the catalog
    # changes, so this is deliberately overridable rather than hardcoded
    # everywhere - set WATSONX_MODEL_ID if your project uses a different one.
    return os.environ.get("WATSONX_MODEL_ID", "ibm/granite-3-8b-instruct").strip()


# SECURITY: same reasoning as routing_service._redact - failure reasons are
# surfaced through the public /api/health and /api/scenario endpoints, so
# nothing that could contain the API key, the exchanged bearer token, or the
# apikey= form field is ever allowed through unredacted.
_SECRET_RE = re.compile(r"(?i)\b(apikey|api_key|access_token|bearer)\b([\"'=:\s]+)[A-Za-z0-9\-_.]{8,}")


def _redact(text: str) -> str:
    return _SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}***REDACTED***", text)


def _get_iam_token(api_key: str) -> str | None:
    """Exchanges the IBM Cloud API key for a short-lived IAM bearer token,
    cached until 60s before it expires (IAM tokens are normally valid ~1h;
    re-fetching one per triage request would add a slow round-trip to every
    single suggestion for no reason)."""
    now = time.time()
    if _iam_token_cache["token"] and now < _iam_token_cache["expires_at"] - 60:
        return _iam_token_cache["token"]

    resp = requests.post(
        _IAM_TOKEN_URL,
        data={"grant_type": "urn:ibm:params:oauth:grant-type:apikey", "apikey": api_key},
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    token = data.get("access_token")
    if not token:
        return None
    _iam_token_cache["token"] = token
    _iam_token_cache["expires_at"] = float(data.get("expiration") or (now + 3300))
    return token


def _strip_for_prompt(note: str) -> str:
    # Newlines/quotes in a free-text note could otherwise break the prompt
    # framing or look like an attempt to inject new instructions - flatten
    # to a single line and drop characters that could terminate the quoted
    # string early.
    cleaned = " ".join((note or "").split())
    return cleaned.replace('"', "'")[:500]


def _build_prompt(note: str, conditions: list[dict]) -> str:
    options = "\n".join(f'- {c["code"]}: {c["label"]} (severity {c["severity"]})' for c in conditions)
    return (
        "You are a clinical triage assistant helping an emergency dispatcher classify "
        "an incoming call. Read the dispatcher's free-text note and choose exactly one "
        "condition code from this list that best matches the patient described:\n"
        f"{options}\n\n"
        f'Dispatcher note: "{_strip_for_prompt(note)}"\n\n'
        "Respond with ONLY a single-line JSON object and nothing else, in exactly this "
        'shape: {"condition_code": "<one of the codes above, verbatim>", '
        '"confidence": <number 0.0-1.0>, "reasoning": "<one short sentence citing '
        'symptoms from the note>"}\n'
        "JSON:"
    )


_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_model_output(text: str, conditions: list[dict]) -> dict | None:
    valid_codes = {c["code"] for c in conditions}
    match = _JSON_OBJ_RE.search(text or "")
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None

    code = obj.get("condition_code")
    if code not in valid_codes:
        return None

    try:
        confidence = float(obj.get("confidence", 0.6))
    except (TypeError, ValueError):
        confidence = 0.6
    confidence = max(0.0, min(1.0, confidence))

    reasoning = str(obj.get("reasoning") or "").strip()[:280] or "Granite did not provide a rationale."
    return {"condition_code": code, "confidence": round(confidence, 2), "reasoning": reasoning}


def _call_watsonx_generate(prompt: str, max_new_tokens: int = 120) -> str:
    """Low-level watsonx.ai call shared by every AI Triage Assist capability
    (condition classification, hospital handover notes, and any future one)
    - exchanges the API key for an IAM token and calls the text-generation
    REST API, returning the raw generated text. Raises on any failure
    (missing credentials, HTTP error, malformed response); every caller
    catches that, sets _last_failure_reason (redacted), and falls back to
    its own non-AI path - this function itself never falls back to
    anything, that's the caller's job."""
    api_key = _api_key()
    project_id = _project_id()
    if not api_key or not project_id:
        raise RuntimeError("watsonx.ai not configured (WATSONX_API_KEY/WATSONX_PROJECT_ID unset)")

    token = _get_iam_token(api_key)
    if not token:
        raise RuntimeError("IBM IAM token exchange returned no access_token")

    url = f"{_base_url()}/ml/v1/text/generation?version={_API_VERSION}"
    body = {
        "model_id": _model_id(),
        "input": prompt,
        "parameters": {
            "decoding_method": "greedy",
            "max_new_tokens": max_new_tokens,
            "repetition_penalty": 1.05,
        },
        "project_id": project_id,
    }
    resp = requests.post(
        url,
        json=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        timeout=15,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"watsonx.ai text-generation returned HTTP {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    return data["results"][0]["generated_text"]


def _watsonx_classify(note: str, conditions: list[dict]) -> dict | None:
    global _last_failure_reason
    if not _api_key() or not _project_id():
        return None
    try:
        generated = _call_watsonx_generate(_build_prompt(note, conditions))
        parsed = _parse_model_output(generated, conditions)
        if parsed is None:
            reason = f"watsonx.ai response could not be parsed into a known condition code: {generated[:200]!r}"
            logger.warning(reason)
            _last_failure_reason = reason
            return None
        _last_failure_reason = None
        return {**parsed, "source": "watsonx", "model_id": _model_id()}
    except Exception as e:
        reason = _redact(f"watsonx.ai call raised: {e}")
        logger.warning(reason)
        _last_failure_reason = reason
        return None


def _build_handover_prompt(note: str, condition: dict, eta_min: float, hospital_name: str) -> str:
    label = condition.get("label", "Unspecified condition")
    severity = condition.get("severity", "")
    specialist = condition.get("preferred_specialist")
    specialist_line = f" The condition typically requires a {specialist.replace(chr(95), chr(32))}." if specialist else ""
    note_line = f' Dispatcher note: "{_strip_for_prompt(note)}"' if note else " No additional dispatcher note was provided."
    return (
        "You are drafting a one-sentence clinical handover for an emergency department "
        f"charge nurse at {hospital_name}, ahead of an ambulance's arrival. "
        f"Assessed condition: {label} (severity {severity}).{specialist_line}{note_line} "
        f"Estimated time of arrival: {round(eta_min)} minutes.\n\n"
        "Write ONLY the handover sentence itself - no preamble, no JSON, no labels - as a single "
        "sentence a busy ED nurse could read in two seconds and immediately know what to prepare for. "
        "Do not invent vitals or details not given above.\n\nHandover:"
    )


def _watsonx_handover_note(note: str, condition: dict, eta_min: float, hospital_name: str) -> dict | None:
    global _last_failure_reason
    if not _api_key() or not _project_id():
        return None
    try:
        generated = _call_watsonx_generate(
            _build_handover_prompt(note, condition, eta_min, hospital_name), max_new_tokens=90
        )
        summary = " ".join(generated.split()).strip().strip('"')[:400]
        if not summary:
            reason = "watsonx.ai handover-note call returned empty text"
            logger.warning(reason)
            _last_failure_reason = reason
            return None
        _last_failure_reason = None
        return {"summary": summary, "source": "watsonx", "model_id": _model_id()}
    except Exception as e:
        reason = _redact(f"watsonx.ai call raised: {e}")
        logger.warning(reason)
        _last_failure_reason = reason
        return None


def _template_handover_note(note: str, condition: dict, eta_min: float, hospital_name: str) -> dict:
    """Deterministic fallback: same 'always usable, fully transparent'
    principle as _keyword_classify - a plain-language sentence built
    straight from structured fields, no model call involved."""
    label = condition.get("label", "Unspecified condition")
    severity = condition.get("severity")
    specialist = condition.get("preferred_specialist")
    parts = [f"{(severity + ' priority') if severity else 'Priority'} — {label}."]
    if note:
        parts.append(f'Dispatcher note: "{" ".join(note.split())}".')
    if specialist:
        parts.append(f"Requesting {specialist.replace(chr(95), chr(32))} availability.")
    parts.append(f"ETA to {hospital_name}: {round(eta_min)} min.")
    return {"summary": " ".join(parts)[:400], "source": "template_fallback", "model_id": None}


def generate_handover_note(note: str, condition: dict, eta_min: float, hospital_name: str) -> dict:
    """Returns {"summary", "source": "watsonx"|"template_fallback", "model_id"}
    - a short natural-language clinical handover sentence sent to the
    receiving hospital alongside the structured pre-alert fields (condition,
    ETA). Tries watsonx.ai first, falls back to a deterministic template
    built from the same structured data on missing credentials or any
    live-call failure - the pre-alert always carries a usable summary."""
    result = _watsonx_handover_note(note, condition, eta_min, hospital_name)
    if result is None:
        result = _template_handover_note(note, condition, eta_min, hospital_name)
    return result


_SEVERITY_RANK = {"Red": 3, "Yellow": 2, "Green": 1}


def _keyword_classify(note: str, conditions: list[dict]) -> dict:
    """Deterministic, fully transparent fallback: score each condition by how
    many of its labeled keywords appear in the note, take the best match,
    and break ties toward the higher-severity condition (a safety bias - an
    ambiguous note should default toward over-triage, not under-triage).
    Always returns a usable suggestion, never raises."""
    text = (note or "").lower()
    scored = []
    for c in conditions:
        matches = [kw for kw in c.get("keywords", []) if kw.lower() in text]
        if matches:
            scored.append((len(matches), _SEVERITY_RANK.get(c.get("severity"), 0), c, matches))

    if not scored:
        fallback = next((c for c in conditions if c["code"] == "urgent_stable"), conditions[0])
        return {
            "condition_code": fallback["code"],
            "confidence": 0.25,
            "reasoning": (
                "No matching keywords found in the note; defaulting to an urgent-but-"
                "unconfirmed assessment pending dispatcher judgment."
            ),
            "source": "keyword_fallback",
            "model_id": None,
        }

    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    count, _rank, best, matches = scored[0]
    confidence = round(min(0.9, 0.45 + 0.15 * count), 2)
    return {
        "condition_code": best["code"],
        "confidence": confidence,
        "reasoning": f"Matched keyword(s): {', '.join(matches)}.",
        "source": "keyword_fallback",
        "model_id": None,
    }


def classify(note: str, conditions: list[dict]) -> dict:
    """Returns {"condition_code", "confidence", "reasoning", "source",
    "model_id"} - always a usable suggestion. Tries watsonx.ai first (when
    WATSONX_API_KEY/WATSONX_PROJECT_ID are set) and falls back to the
    keyword classifier on missing credentials or any live-call failure."""
    result = _watsonx_classify(note, conditions)
    if result is None:
        result = _keyword_classify(note, conditions)
    return result


def provider_status() -> dict:
    """Mirrors eta_service.provider_status()/routing_service.provider_status()
    - lets the frontend (and whoever's watching the demo) show which engine
    actually answered the last request and, when watsonx.ai is configured but
    failing, why."""
    configured = bool(_api_key() and _project_id())
    return {
        "configured": configured,
        "active_mode": "watsonx" if configured else "keyword_fallback",
        "model_id": _model_id(),
        "last_failure_reason": _last_failure_reason,
    }
