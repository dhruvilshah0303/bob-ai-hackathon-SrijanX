"""
AI Hospital Recommendation Engine
==================================
Implements TRD Section 5: constraint-filtered, weighted-scoring model with
mandatory, decomposable reasoning for every candidate hospital (not just the
winner). This is deliberately transparent rather than a black-box model —
see TRD 5.1.

Pipeline per request:
  1. Candidate generation   -> all hospitals (demo radius = "all", small city)
  2. Hard constraint filter -> disqualify hospitals missing a REQUIRED capability
  3. Estimated total treatment delay = ETA (travel) + estimated in-hospital wait
  4. Weighted score for viable candidates
  5. Rank + attach human-readable reasons to every candidate (viable or not)
"""
from dataclasses import dataclass, field
from typing import Optional
from utils import is_stale


@dataclass
class Candidate:
    hospital_id: str
    name: str
    lat: float
    lng: float
    distance_km: float
    eta_min: float
    eta_source: str  # "live_api" | "simulated"
    icu_available: bool
    specialist_match: bool
    matched_specialist: Optional[str]
    ed_load_state: str  # "normal" | "busy" | "overloaded"
    est_hospital_wait_min: float
    est_total_treatment_delay_min: float
    viable: bool
    score: float
    reasons: list = field(default_factory=list)
    disqualifiers: list = field(default_factory=list)
    data_stale: bool = False


def _ed_load_state(occupied: int, total: int) -> str:
    if total <= 0:
        return "overloaded"
    ratio = occupied / total
    if ratio < 0.6:
        return "normal"
    if ratio < 0.85:
        return "busy"
    return "overloaded"


def _check_hard_constraints(hospital: dict, required: list[str]) -> tuple[bool, list[str]]:
    disqualifiers = []
    if hospital.get("accepting_status") == "no":
        disqualifiers.append("Emergency department not accepting patients")
    if "ed_accepting" in required and hospital.get("accepting_status") not in ("yes", "limited"):
        disqualifiers.append("ED not accepting")
    if "icu" in required and hospital.get("icu_beds_free", 0) <= 0:
        disqualifiers.append("ICU unavailable")
    viable = len(disqualifiers) == 0
    return viable, disqualifiers


def _estimate_hospital_wait(hospital: dict, ed_load: str, specialist_available: bool,
                             specialist_required: bool) -> tuple[float, list[str]]:
    reasons = []
    base = hospital.get("avg_historical_wait_min", 10)
    load_penalty = {"normal": 0, "busy": 8, "overloaded": 20}[ed_load]
    if ed_load == "overloaded":
        reasons.append("emergency department overloaded")
    elif ed_load == "busy":
        reasons.append("emergency department busy")
    else:
        reasons.append("emergency department load normal")

    specialist_penalty = 0
    if specialist_required:
        if specialist_available:
            reasons.append("required specialist on duty")
        else:
            specialist_penalty = 15
            reasons.append("required specialist not on duty (must be called in)")

    total_wait = base + load_penalty + specialist_penalty
    return total_wait, reasons


def _normalize_inverse(value: float, all_values: list[float]) -> float:
    """Higher score for lower delay. Normalized to [0,1] across candidates."""
    if not all_values:
        return 0.0
    inv = [1.0 / v if v > 0 else 0.0 for v in all_values]
    max_inv = max(inv) if inv else 1.0
    this_inv = 1.0 / value if value > 0 else 0.0
    return this_inv / max_inv if max_inv > 0 else 0.0


def recommend(
    hospitals: list[dict],
    eta_lookup: dict,  # hospital_id -> {"eta_min": float, "distance_km": float, "source": str}
    condition_rule: dict,
    weights: dict,
    stale_threshold_min: float,
) -> dict:
    """
    Returns {
      "candidates": [Candidate as dict, ...] sorted best-first,
      "recommended_hospital_id": str or None,
    }
    """
    required = condition_rule.get("required", [])
    preferred_specialist = condition_rule.get("preferred_specialist")

    raw_candidates = []
    for h in hospitals:
        eta_info = eta_lookup.get(h["id"], {"eta_min": None, "distance_km": None, "source": "simulated"})
        eta_min = eta_info["eta_min"]
        distance_km = eta_info["distance_km"]

        viable, disqualifiers = _check_hard_constraints(h, required)
        ed_load = _ed_load_state(h.get("ed_bays_occupied", 0), h.get("ed_bays_total", 1))

        specialist_available = False
        matched_specialist = None
        if preferred_specialist:
            for s in h.get("specialists", []):
                if s["type"] == preferred_specialist and s.get("on_duty"):
                    specialist_available = True
                    matched_specialist = preferred_specialist
                    break

        hospital_wait, wait_reasons = _estimate_hospital_wait(
            h, ed_load, specialist_available, bool(preferred_specialist)
        )

        est_total_delay = (eta_min if eta_min is not None else 999) + hospital_wait

        stale = is_stale(h.get("last_capacity_update_at", ""), stale_threshold_min)

        reasons = []
        if viable:
            reasons.append(
                f"ICU {'available' if h.get('icu_beds_free', 0) > 0 else 'unavailable'}"
                if "icu" in required else "ICU not required for this condition"
            )
            if preferred_specialist:
                reasons.append(
                    f"{preferred_specialist.replace('_', ' ').title()} "
                    f"{'available' if specialist_available else 'not on duty'}"
                )
            reasons.extend(wait_reasons)
        else:
            reasons = list(disqualifiers)

        if stale:
            reasons.append(f"⚠ capacity data may be outdated (last update > {stale_threshold_min:.0f} min ago)")

        raw_candidates.append(dict(
            hospital_id=h["id"],
            name=h["name"],
            lat=h["lat"],
            lng=h["lng"],
            distance_km=round(distance_km, 2) if distance_km is not None else None,
            eta_min=round(eta_min, 1) if eta_min is not None else None,
            eta_source=eta_info["source"],
            icu_available=h.get("icu_beds_free", 0) > 0,
            specialist_match=specialist_available,
            matched_specialist=matched_specialist,
            ed_load_state=ed_load,
            est_hospital_wait_min=round(hospital_wait, 1),
            est_total_treatment_delay_min=round(est_total_delay, 1),
            viable=viable,
            reasons=reasons,
            disqualifiers=disqualifiers,
            data_stale=stale,
            score=0.0,
        ))

    # Score viable candidates against each other
    viable_delays = [c["est_total_treatment_delay_min"] for c in raw_candidates if c["viable"]]
    max_icu_free = max([h.get("icu_beds_free", 0) for h in hospitals], default=1) or 1

    for c in raw_candidates:
        if not c["viable"]:
            c["score"] = 0.0
            continue
        h = next(h for h in hospitals if h["id"] == c["hospital_id"])
        delay_score = _normalize_inverse(c["est_total_treatment_delay_min"], viable_delays)
        specialist_bonus = 1.0 if c["specialist_match"] else 0.0
        capacity_headroom = min(h.get("icu_beds_free", 0) / max_icu_free, 1.0)
        overload_penalty = 1.0 if c["ed_load_state"] == "overloaded" else (0.4 if c["ed_load_state"] == "busy" else 0.0)

        score = (
            weights["w1_delay"] * delay_score
            + weights["w2_specialist"] * specialist_bonus
            + weights["w3_capacity_headroom"] * capacity_headroom
            - weights["w4_ed_overload_penalty"] * overload_penalty
        )
        c["score"] = round(max(score, 0.0), 4)

    # Sort: viable first (by score desc), then non-viable (by distance asc) for transparency
    viable_sorted = sorted([c for c in raw_candidates if c["viable"]], key=lambda c: -c["score"])
    non_viable_sorted = sorted(
        [c for c in raw_candidates if not c["viable"]],
        key=lambda c: (c["distance_km"] if c["distance_km"] is not None else 9999)
    )
    ordered = viable_sorted + non_viable_sorted

    recommended_id = viable_sorted[0]["hospital_id"] if viable_sorted else None

    return {
        "candidates": ordered,
        "recommended_hospital_id": recommended_id,
        "required_capabilities": required,
        "preferred_specialist": preferred_specialist,
    }
