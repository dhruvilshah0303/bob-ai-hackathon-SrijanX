"""
Unit tests for recommendation_engine.py - the actual "AI" in this project.
This is the highest-value place to have tests: it's the part being pitched
as the differentiator, and it's exactly the kind of logic a judge (or a
future contributor) will poke at with edge cases.

Run with:  cd backend && python3 -m pytest tests/ -v
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from recommendation_engine import recommend  # noqa: E402

WEIGHTS = {
    "w1_delay": 0.5,
    "w2_specialist": 0.25,
    "w3_capacity_headroom": 0.1,
    "w4_ed_overload_penalty": 0.15,
}
STALE_THRESHOLD_MIN = 10

CARDIAC_RULE = {
    "code": "cardiac",
    "required": ["icu", "ed_accepting"],
    "preferred_specialist": "cardiologist",
}
NON_CRITICAL_RULE = {
    "code": "non_critical",
    "required": ["ed_accepting"],
    "preferred_specialist": None,
}


def fresh_ts():
    return datetime.now().astimezone().isoformat()


def stale_ts(minutes_ago):
    return (datetime.now().astimezone() - timedelta(minutes=minutes_ago)).isoformat()


def make_hospital(
    id_, name, icu_free, icu_total=10, ed_occupied=3, ed_total=10,
    accepting="yes", cardiologist_on_duty=False, last_update=None,
):
    return {
        "id": id_,
        "name": name,
        "lat": 0.0,
        "lng": 0.0,
        "ed_bays_total": ed_total,
        "ed_bays_occupied": ed_occupied,
        "icu_beds_total": icu_total,
        "icu_beds_free": icu_free,
        "ward_beds_total": 30,
        "ward_beds_free": 10,
        "accepting_status": accepting,
        "specialists": [{"type": "cardiologist", "on_duty": cardiologist_on_duty}],
        "avg_historical_wait_min": 10,
        "last_capacity_update_at": last_update or fresh_ts(),
    }


def eta(minutes, distance_km=5.0, source="live_api"):
    return {"eta_min": minutes, "distance_km": distance_km, "source": source}


def test_nearest_hospital_with_no_icu_is_disqualified_not_recommended():
    """The core product claim: proximity alone must not win when the
    nearest hospital can't actually treat the patient."""
    hospitals = [
        make_hospital("near", "Nearest Hospital", icu_free=0),   # closest but no ICU
        make_hospital("far", "Farther Hospital", icu_free=3, cardiologist_on_duty=True),
    ]
    eta_lookup = {"near": eta(3), "far": eta(15)}

    result = recommend(hospitals, eta_lookup, CARDIAC_RULE, WEIGHTS, STALE_THRESHOLD_MIN)

    near = next(c for c in result["candidates"] if c["hospital_id"] == "near")
    far = next(c for c in result["candidates"] if c["hospital_id"] == "far")

    assert near["viable"] is False
    assert "ICU unavailable" in near["disqualifiers"]
    assert far["viable"] is True
    assert result["recommended_hospital_id"] == "far"


def test_no_viable_hospital_returns_none_not_a_crash():
    hospitals = [
        make_hospital("h1", "Hospital 1", icu_free=0),
        make_hospital("h2", "Hospital 2", icu_free=0),
    ]
    eta_lookup = {"h1": eta(5), "h2": eta(8)}

    result = recommend(hospitals, eta_lookup, CARDIAC_RULE, WEIGHTS, STALE_THRESHOLD_MIN)

    assert result["recommended_hospital_id"] is None
    assert all(not c["viable"] for c in result["candidates"])
    # Still transparent about why, for every candidate - this is the product's
    # explainability requirement (TRD 5.3), not just an implementation detail.
    assert all(c["disqualifiers"] for c in result["candidates"])


def test_condition_with_no_icu_requirement_allows_zero_icu_hospitals():
    """A non-critical case shouldn't be blocked by a missing ICU bed - only
    conditions that actually require one should apply that constraint."""
    hospitals = [make_hospital("h1", "Hospital 1", icu_free=0, accepting="yes")]
    eta_lookup = {"h1": eta(5)}

    result = recommend(hospitals, eta_lookup, NON_CRITICAL_RULE, WEIGHTS, STALE_THRESHOLD_MIN)

    assert result["recommended_hospital_id"] == "h1"
    assert result["candidates"][0]["viable"] is True


def test_specialist_match_outweighs_pure_distance_when_delay_is_close():
    hospitals = [
        make_hospital("no_spec", "No Specialist Hospital", icu_free=3, cardiologist_on_duty=False),
        make_hospital("has_spec", "Has Specialist Hospital", icu_free=3, cardiologist_on_duty=True),
    ]
    # Similar ETA so the specialist bonus, not distance, decides the winner.
    eta_lookup = {"no_spec": eta(10), "has_spec": eta(11)}

    result = recommend(hospitals, eta_lookup, CARDIAC_RULE, WEIGHTS, STALE_THRESHOLD_MIN)

    assert result["recommended_hospital_id"] == "has_spec"


def test_overloaded_ed_lowers_score_relative_to_normal_load():
    hospitals = [
        make_hospital("overloaded", "Overloaded Hospital", icu_free=3, ed_occupied=9, ed_total=10),
        make_hospital("normal", "Normal Load Hospital", icu_free=3, ed_occupied=2, ed_total=10),
    ]
    eta_lookup = {"overloaded": eta(10), "normal": eta(10)}

    result = recommend(hospitals, eta_lookup, CARDIAC_RULE, WEIGHTS, STALE_THRESHOLD_MIN)

    overloaded = next(c for c in result["candidates"] if c["hospital_id"] == "overloaded")
    normal = next(c for c in result["candidates"] if c["hospital_id"] == "normal")
    assert overloaded["ed_load_state"] == "overloaded"
    assert normal["ed_load_state"] == "normal"
    assert normal["score"] > overloaded["score"]
    assert result["recommended_hospital_id"] == "normal"


def test_stale_capacity_data_is_flagged_not_silently_trusted():
    hospitals = [make_hospital("h1", "Hospital 1", icu_free=3, last_update=stale_ts(60))]
    eta_lookup = {"h1": eta(5)}

    result = recommend(hospitals, eta_lookup, CARDIAC_RULE, WEIGHTS, STALE_THRESHOLD_MIN)

    c = result["candidates"][0]
    assert c["data_stale"] is True
    assert any("outdated" in r for r in c["reasons"])
    # Staleness is a warning, not an automatic disqualification (TRD 5.5).
    assert c["viable"] is True


def test_not_accepting_hospital_is_disqualified_regardless_of_beds():
    hospitals = [make_hospital("h1", "Closed Hospital", icu_free=5, accepting="no")]
    eta_lookup = {"h1": eta(5)}

    result = recommend(hospitals, eta_lookup, CARDIAC_RULE, WEIGHTS, STALE_THRESHOLD_MIN)

    assert result["candidates"][0]["viable"] is False
    assert result["recommended_hospital_id"] is None


def test_recommendation_is_deterministic_for_identical_inputs():
    """Guards against accidental nondeterminism creeping back into the
    engine (e.g. relying on dict iteration order or unseeded randomness) -
    the same inputs must always produce the same ranking."""
    hospitals = [
        make_hospital("h1", "Hospital 1", icu_free=3, cardiologist_on_duty=True),
        make_hospital("h2", "Hospital 2", icu_free=2, cardiologist_on_duty=False),
    ]
    eta_lookup = {"h1": eta(9), "h2": eta(9)}

    result_a = recommend(hospitals, eta_lookup, CARDIAC_RULE, WEIGHTS, STALE_THRESHOLD_MIN)
    result_b = recommend(hospitals, eta_lookup, CARDIAC_RULE, WEIGHTS, STALE_THRESHOLD_MIN)

    ids_a = [c["hospital_id"] for c in result_a["candidates"]]
    ids_b = [c["hospital_id"] for c in result_b["candidates"]]
    assert ids_a == ids_b
    assert result_a["recommended_hospital_id"] == result_b["recommended_hospital_id"]


def test_score_breakdown_is_exposed_and_matches_the_final_score():
    """The UI's "how was this calculated" panel renders these per-factor
    numbers directly - they must be the real components the final score was
    built from (each weight * value), not a display-only approximation, and
    they must sum back to the actual score so the panel can never show
    reasoning that doesn't add up to the number next to it."""
    hospitals = [
        make_hospital("h1", "Hospital 1", icu_free=4, icu_total=10, ed_occupied=2, ed_total=10, cardiologist_on_duty=True),
    ]
    eta_lookup = {"h1": eta(10)}

    result = recommend(hospitals, eta_lookup, CARDIAC_RULE, WEIGHTS, STALE_THRESHOLD_MIN)
    cand = result["candidates"][0]

    assert cand["viable"] is True
    breakdown = cand["score_breakdown"]
    assert breakdown is not None
    for key in ("delay", "specialist", "capacity_headroom", "ed_overload_penalty"):
        assert key in breakdown
        assert breakdown[key]["weight"] == WEIGHTS[{"delay": "w1_delay", "specialist": "w2_specialist",
                                                       "capacity_headroom": "w3_capacity_headroom",
                                                       "ed_overload_penalty": "w4_ed_overload_penalty"}[key]]
        assert breakdown[key]["contribution"] == round(breakdown[key]["weight"] * breakdown[key]["value"] * (-1 if key == "ed_overload_penalty" else 1), 4)

    # Specialist is on duty and ED is quiet (2/10 = 20% occupied, "normal") -
    # so the specialist bonus is fully earned and the overload penalty is zero.
    assert breakdown["specialist"]["value"] == 1.0
    assert breakdown["ed_overload_penalty"]["value"] == 0.0

    total = sum(f["contribution"] for f in breakdown.values())
    assert abs(total - cand["score"]) < 1e-6


def test_score_breakdown_is_none_for_a_disqualified_hospital():
    # A non-viable candidate never entered the scoring competition, so there
    # is nothing honest to show in a "how was this calculated" panel for it -
    # None (not a zeroed-out breakdown that implies it WAS scored) is what
    # the frontend checks to decide whether to render that panel at all.
    hospitals = [make_hospital("h1", "No ICU Hospital", icu_free=0)]
    eta_lookup = {"h1": eta(5)}

    result = recommend(hospitals, eta_lookup, CARDIAC_RULE, WEIGHTS, STALE_THRESHOLD_MIN)
    cand = result["candidates"][0]

    assert cand["viable"] is False
    assert cand["score_breakdown"] is None
