"""
Unit tests for triage_service.py - the "AI Triage Assist" feature: free-text
dispatcher note -> suggested condition_code, via IBM watsonx.ai's Granite
model when configured, falling back to a transparent keyword classifier
otherwise (same graceful-degradation shape already tested for
routing_service.py in test_routing.py).

Run with:  cd backend && python3 -m pytest tests/ -v
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import triage_service  # noqa: E402

CONDITIONS = [
    {
        "code": "cardiac",
        "label": "Suspected Heart Attack / Cardiac Event",
        "severity": "Red",
        "keywords": ["chest pain", "heart attack", "crushing chest", "left arm pain"],
    },
    {
        "code": "stroke",
        "label": "Suspected Stroke",
        "severity": "Red",
        "keywords": ["stroke", "facial droop", "slurred speech", "one-sided weakness"],
    },
    {
        "code": "urgent_stable",
        "label": "Urgent but Stable",
        "severity": "Yellow",
        "keywords": ["abdominal pain", "high fever", "difficulty breathing"],
    },
    {
        "code": "non_critical",
        "label": "Non-Critical",
        "severity": "Green",
        "keywords": ["minor cut", "sprain", "twisted ankle"],
    },
]


# ---------------------------------------------------------------------------
# Keyword fallback classifier
# ---------------------------------------------------------------------------

def test_keyword_classify_matches_cardiac_keywords():
    result = triage_service._keyword_classify(
        "55 year old male, sudden crushing chest pain radiating to left arm, sweating",
        CONDITIONS,
    )
    assert result["condition_code"] == "cardiac"
    assert result["source"] == "keyword_fallback"
    assert 0.0 < result["confidence"] <= 0.9
    assert "chest pain" in result["reasoning"] or "crushing chest" in result["reasoning"]


def test_keyword_classify_matches_stroke_keywords():
    result = triage_service._keyword_classify(
        "Patient has facial droop and slurred speech since 20 minutes ago",
        CONDITIONS,
    )
    assert result["condition_code"] == "stroke"


def test_keyword_classify_matches_non_critical_keywords():
    result = triage_service._keyword_classify("Patient has a minor cut on the hand", CONDITIONS)
    assert result["condition_code"] == "non_critical"


def test_keyword_classify_case_insensitive():
    result = triage_service._keyword_classify("CHEST PAIN and HEART ATTACK symptoms", CONDITIONS)
    assert result["condition_code"] == "cardiac"


def test_keyword_classify_breaks_ties_toward_higher_severity():
    # Construct a note that matches exactly one keyword from both a Red and
    # a Green condition equally (1 match each) - the safety bias should
    # favor the higher-severity (Red) condition rather than picking
    # whichever happened to be scored first.
    conditions = [
        {"code": "low", "severity": "Green", "keywords": ["ambiguous term"]},
        {"code": "high", "severity": "Red", "keywords": ["ambiguous term"]},
    ]
    result = triage_service._keyword_classify("patient reports ambiguous term", conditions)
    assert result["condition_code"] == "high"


def test_keyword_classify_defaults_to_urgent_stable_when_nothing_matches():
    result = triage_service._keyword_classify(
        "Caller is speaking a language the dispatcher doesn't recognize", CONDITIONS
    )
    assert result["condition_code"] == "urgent_stable"
    assert result["confidence"] < 0.5
    assert result["source"] == "keyword_fallback"


def test_keyword_classify_handles_empty_note():
    result = triage_service._keyword_classify("", CONDITIONS)
    assert result["condition_code"] == "urgent_stable"


# ---------------------------------------------------------------------------
# watsonx.ai call - mocked, no real network access
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json_data = json_data or {}
        self.text = text

    def json(self):
        return self._json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _stub_iam_and_generation(monkeypatch, generated_text, gen_status=200):
    monkeypatch.setenv("WATSONX_API_KEY", "fake-api-key-for-test")
    monkeypatch.setenv("WATSONX_PROJECT_ID", "fake-project-id")
    triage_service._iam_token_cache["token"] = None
    triage_service._iam_token_cache["expires_at"] = 0.0
    triage_service._last_failure_reason = None

    def fake_post(url, **kwargs):
        if url == triage_service._IAM_TOKEN_URL:
            return _FakeResponse(200, {"access_token": "fake-token", "expiration": 9999999999})
        return _FakeResponse(gen_status, {"results": [{"generated_text": generated_text}]}, text=generated_text)

    monkeypatch.setattr(triage_service.requests, "post", fake_post)


def test_watsonx_classify_parses_well_formed_json_response(monkeypatch):
    _stub_iam_and_generation(
        monkeypatch,
        '{"condition_code": "stroke", "confidence": 0.87, "reasoning": "Facial droop and slurred speech reported."}',
    )
    result = triage_service.classify("face is drooping and speech is slurred", CONDITIONS)
    assert result["condition_code"] == "stroke"
    assert result["confidence"] == 0.87
    assert result["source"] == "watsonx"
    assert "droop" in result["reasoning"].lower()


def test_watsonx_classify_handles_extra_text_around_json(monkeypatch):
    # Instruct-tuned models sometimes wrap the JSON in a sentence or two
    # despite instructions - the parser should still find the object.
    _stub_iam_and_generation(
        monkeypatch,
        'Sure, here is my answer:\n{"condition_code": "cardiac", "confidence": 0.7, '
        '"reasoning": "Chest pain reported."}\nLet me know if you need anything else.',
    )
    result = triage_service.classify("chest pain", CONDITIONS)
    assert result["condition_code"] == "cardiac"
    assert result["source"] == "watsonx"


def test_classify_falls_back_to_keyword_when_no_credentials(monkeypatch):
    monkeypatch.delenv("WATSONX_API_KEY", raising=False)
    monkeypatch.delenv("WATSONX_PROJECT_ID", raising=False)
    result = triage_service.classify("minor cut on the finger", CONDITIONS)
    assert result["source"] == "keyword_fallback"
    assert result["condition_code"] == "non_critical"


def test_classify_falls_back_to_keyword_when_live_call_raises(monkeypatch):
    monkeypatch.setenv("WATSONX_API_KEY", "fake-api-key-for-test")
    monkeypatch.setenv("WATSONX_PROJECT_ID", "fake-project-id")
    triage_service._iam_token_cache["token"] = None
    triage_service._iam_token_cache["expires_at"] = 0.0

    def _boom(*args, **kwargs):
        raise ConnectionError("simulated network failure")

    monkeypatch.setattr(triage_service.requests, "post", _boom)
    result = triage_service.classify("chest pain and left arm pain", CONDITIONS)
    assert result["source"] == "keyword_fallback"
    assert result["condition_code"] == "cardiac"


def test_classify_falls_back_when_model_output_names_an_unknown_code(monkeypatch):
    _stub_iam_and_generation(
        monkeypatch,
        '{"condition_code": "not_a_real_code", "confidence": 0.9, "reasoning": "n/a"}',
    )
    result = triage_service.classify("minor cut", CONDITIONS)
    assert result["source"] == "keyword_fallback"
    assert result["condition_code"] == "non_critical"


def test_confidence_is_clamped_to_0_1_range(monkeypatch):
    _stub_iam_and_generation(
        monkeypatch,
        '{"condition_code": "cardiac", "confidence": 5.0, "reasoning": "very sure"}',
    )
    result = triage_service.classify("chest pain", CONDITIONS)
    assert result["confidence"] == 1.0


# ---------------------------------------------------------------------------
# provider_status() and secret redaction
# ---------------------------------------------------------------------------

def test_provider_status_reports_keyword_fallback_when_unconfigured(monkeypatch):
    monkeypatch.delenv("WATSONX_API_KEY", raising=False)
    monkeypatch.delenv("WATSONX_PROJECT_ID", raising=False)
    status = triage_service.provider_status()
    assert status["configured"] is False
    assert status["active_mode"] == "keyword_fallback"


def test_provider_status_reports_watsonx_when_configured(monkeypatch):
    monkeypatch.setenv("WATSONX_API_KEY", "fake-api-key-for-test")
    monkeypatch.setenv("WATSONX_PROJECT_ID", "fake-project-id")
    status = triage_service.provider_status()
    assert status["configured"] is True
    assert status["active_mode"] == "watsonx"


def test_provider_status_never_leaks_the_api_key_in_a_failure_reason(monkeypatch):
    # SECURITY REGRESSION TEST: mirrors
    # test_routing.test_provider_status_never_leaks_the_api_key_in_a_failure_reason.
    # last_failure_reason is surfaced through the *public* /api/health and
    # /api/scenario endpoints, so it must never contain the raw API key or
    # the exchanged bearer token.
    monkeypatch.setenv("WATSONX_API_KEY", "SECRET-KEY-MUST-NOT-LEAK-12345")
    monkeypatch.setenv("WATSONX_PROJECT_ID", "fake-project-id")
    triage_service._iam_token_cache["token"] = None
    triage_service._iam_token_cache["expires_at"] = 0.0

    def _boom(*args, **kwargs):
        raise ConnectionError(
            "https://iam.cloud.ibm.com/identity/token failed for "
            "apikey=SECRET-KEY-MUST-NOT-LEAK-12345"
        )

    monkeypatch.setattr(triage_service.requests, "post", _boom)
    triage_service.classify("chest pain", CONDITIONS)
    status = triage_service.provider_status()
    assert status["last_failure_reason"] is not None
    assert "SECRET-KEY-MUST-NOT-LEAK-12345" not in status["last_failure_reason"]
    assert "REDACTED" in status["last_failure_reason"]


def test_redact_strips_apikey_and_bearer_tokens():
    assert "supersecret123" not in triage_service._redact("apikey=supersecret123 failed")
    assert "supersecret123" not in triage_service._redact('"apikey": "supersecret123"')
    assert "eyverysecrettoken" not in triage_service._redact("Authorization: Bearer eyverysecrettoken")
    # Non-secret text is left alone.
    assert triage_service._redact("watsonx.ai text-generation returned HTTP 401") == (
        "watsonx.ai text-generation returned HTTP 401"
    )


def test_parse_model_output_rejects_non_json_text():
    assert triage_service._parse_model_output("I'm not sure what to say here.", CONDITIONS) is None


def test_build_prompt_includes_all_condition_codes():
    prompt = triage_service._build_prompt("chest pain", CONDITIONS)
    for c in CONDITIONS:
        assert c["code"] in prompt


def test_strip_for_prompt_flattens_newlines_and_quotes():
    cleaned = triage_service._strip_for_prompt('line one\nline two "quoted"')
    assert "\n" not in cleaned
    assert '"' not in cleaned


# ---------------------------------------------------------------------------
# AI hospital handover note - generate_handover_note()
# ---------------------------------------------------------------------------

CARDIAC = {
    "code": "cardiac",
    "label": "Suspected Heart Attack / Cardiac Event",
    "severity": "Red",
    "preferred_specialist": "cardiologist",
}
NON_CRITICAL_NO_SPECIALIST = {
    "code": "non_critical",
    "label": "Non-Critical",
    "severity": "Green",
    "preferred_specialist": None,
}


def test_template_handover_note_includes_note_specialist_and_eta():
    result = triage_service._template_handover_note(
        "55yo male, crushing chest pain radiating to left arm", CARDIAC, 9.4, "MPUH Hospital"
    )
    assert result["source"] == "template_fallback"
    assert result["model_id"] is None
    assert "Red priority" in result["summary"]
    assert "crushing chest pain" in result["summary"]
    assert "cardiologist" in result["summary"]
    assert "MPUH Hospital" in result["summary"]
    assert "9 min" in result["summary"]


def test_template_handover_note_handles_missing_note_and_specialist():
    result = triage_service._template_handover_note("", NON_CRITICAL_NO_SPECIALIST, 5.0, "Sparsh Hospital")
    assert "Dispatcher note" not in result["summary"]
    assert "Requesting" not in result["summary"]
    assert "Sparsh Hospital" in result["summary"]


def test_generate_handover_note_falls_back_to_template_when_no_credentials(monkeypatch):
    monkeypatch.delenv("WATSONX_API_KEY", raising=False)
    monkeypatch.delenv("WATSONX_PROJECT_ID", raising=False)
    result = triage_service.generate_handover_note("chest pain", CARDIAC, 9.0, "MPUH Hospital")
    assert result["source"] == "template_fallback"


def test_generate_handover_note_uses_watsonx_when_configured(monkeypatch):
    _stub_iam_and_generation(monkeypatch, "  55yo male, crushing chest pain radiating to left arm, ETA 9 min.  ")
    result = triage_service.generate_handover_note(
        "55yo male, crushing chest pain radiating to left arm", CARDIAC, 9.0, "MPUH Hospital"
    )
    assert result["source"] == "watsonx"
    assert result["model_id"] == triage_service._model_id()
    # whitespace-trimmed and collapsed, per _watsonx_handover_note
    assert result["summary"] == "55yo male, crushing chest pain radiating to left arm, ETA 9 min."


def test_generate_handover_note_falls_back_when_live_call_raises(monkeypatch):
    monkeypatch.setenv("WATSONX_API_KEY", "fake-api-key-for-test")
    monkeypatch.setenv("WATSONX_PROJECT_ID", "fake-project-id")
    triage_service._iam_token_cache["token"] = None
    triage_service._iam_token_cache["expires_at"] = 0.0

    def _boom(*args, **kwargs):
        raise ConnectionError("simulated network failure")

    monkeypatch.setattr(triage_service.requests, "post", _boom)
    result = triage_service.generate_handover_note("chest pain", CARDIAC, 9.0, "MPUH Hospital")
    assert result["source"] == "template_fallback"


def test_generate_handover_note_falls_back_on_empty_model_output(monkeypatch):
    _stub_iam_and_generation(monkeypatch, "   ")
    result = triage_service.generate_handover_note("chest pain", CARDIAC, 9.0, "MPUH Hospital")
    assert result["source"] == "template_fallback"


def test_build_handover_prompt_includes_structured_fields_and_note():
    prompt = triage_service._build_handover_prompt(
        "chest pain, sweating", CARDIAC, 9.4, "MPUH Hospital"
    )
    assert "MPUH Hospital" in prompt
    assert "Suspected Heart Attack / Cardiac Event" in prompt
    assert "cardiologist" in prompt
    assert "chest pain, sweating" in prompt
    assert "9 minutes" in prompt


def test_call_watsonx_generate_raises_without_credentials(monkeypatch):
    monkeypatch.delenv("WATSONX_API_KEY", raising=False)
    monkeypatch.delenv("WATSONX_PROJECT_ID", raising=False)
    try:
        triage_service._call_watsonx_generate("hello")
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass
