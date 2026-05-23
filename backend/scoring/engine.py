"""
Scoring engine — computes 5 composite scores (0-100).

Two-layer design:
  Layer 1 (raw signals): pre-computed scores from data fetchers —
    deterministic, always available, not subject to LLM variability.
  Layer 2 (agent signals): LLM sub_scores from specialist agents —
    contextual, may be absent if agent failed.

Final score = weighted blend of both layers.
Higher is BETTER for: livability, neighborhood_stability.
Higher is WORSE for:  environmental_exposure, infrastructure_risk, hidden_risk.
"""


def _get(d: dict, *keys: str, default: int = 50) -> int:
    """Walk nested dicts, return int at final key or default."""
    for key in keys[:-1]:
        d = d.get(key) or {}
    val = d.get(keys[-1])
    try:
        return max(0, min(100, int(val))) if val is not None else default
    except (TypeError, ValueError):
        return default


def _blend(raw: int, agent: int, raw_weight: float = 0.4) -> int:
    """Blend raw-signal score and agent score. Raw anchors, agent refines."""
    return round(raw * raw_weight + agent * (1 - raw_weight))


# ── Environmental Exposure ─────────────────────────────────────────────────

def _env_raw(raw_data: dict) -> int:
    """
    Environmental exposure from raw fetcher data (higher = worse).
    Sources: FEMA flood zone, EPA aggregate risk, USGS elevation score.
    """
    fema = raw_data.get("fema", {})
    epa  = raw_data.get("epa",  {})
    usgs = raw_data.get("usgs", {})

    # Flood risk (0-100 worse) — derived from zone risk_level + SFHA + BFE margin
    risk_level = fema.get("risk_level", "unknown")
    flood_base = {
        "critical": 95, "high": 75, "moderate": 45, "low": 15, "unknown": 50
    }.get(risk_level, 50)

    # BFE margin adjustment — below flood level pushes score toward 100
    bfe_margin = usgs.get("bfe_margin_feet")
    if bfe_margin is not None:
        if bfe_margin < -2:
            flood_base = min(100, flood_base + 20)
        elif bfe_margin < 0:
            flood_base = min(100, flood_base + 10)
        elif bfe_margin > 10:
            flood_base = max(0, flood_base - 10)

    # SFHA mandatory insurance flag
    if fema.get("sfha"):
        flood_base = max(flood_base, 70)

    # EPA pollution (0-100, pre-computed aggregate_risk_score)
    pollution_raw = epa.get("aggregate_risk_score", 0)

    # Elevation (0-100 better → invert for exposure)
    elevation_raw = 100 - _get(usgs, "elevation_score", default=50)

    return round(flood_base * 0.5 + pollution_raw * 0.3 + elevation_raw * 0.2)


def _env_agent(env_report: dict) -> int:
    sub = env_report.get("sub_scores", {})
    flood      = _get(sub, "flood_score")
    pollution  = _get(sub, "pollution_score")
    elevation  = 100 - _get(sub, "elevation_score")  # invert
    return round(flood * 0.5 + pollution * 0.3 + elevation * 0.2)


# ── Infrastructure Risk ────────────────────────────────────────────────────

def _infra_raw(raw_data: dict) -> int:
    """
    Infrastructure risk from OSM pre-computed scores (higher = worse).
    """
    osm = raw_data.get("osm", {})
    noise_score  = osm.get("noise_score",  0)
    hazard_score = osm.get("hazard_score", 0)
    return round(noise_score * 0.55 + hazard_score * 0.45)


def _infra_agent(infra_report: dict) -> int:
    sub = infra_report.get("sub_scores", {})
    noise   = _get(sub, "noise_score")
    hazard  = _get(sub, "hazard_score")
    power   = _get(sub, "power_line_score")
    # power_line_score is a fallback if agent uses old key names
    return round(noise * 0.55 + hazard * 0.35 + power * 0.10)


# ── Neighborhood Stability ─────────────────────────────────────────────────

def _nbhd_raw(raw_data: dict) -> int:
    """
    Neighborhood stability from Census pre-computed stability_score (higher = better).
    """
    return raw_data.get("census", {}).get("stability_score", 50)


def _nbhd_agent(nbhd_report: dict) -> int:
    sub = nbhd_report.get("sub_scores", {})
    income     = _get(sub, "income_score")
    employment = _get(sub, "employment_score")
    vacancy    = 100 - _get(sub, "vacancy_score")  # invert (lower vacancy = better)
    return round(income * 0.40 + employment * 0.35 + vacancy * 0.25)


# ── Composite Scores ───────────────────────────────────────────────────────

def compute_scores(
    env_report:          dict,
    infra_report:        dict,
    neighborhood_report: dict,
    raw_data:            dict | None = None,
) -> dict:
    """
    Compute all 5 scores. raw_data is optional — when absent falls back
    to agent sub_scores only (legacy behaviour, used in tests/fallbacks).
    """
    rd = raw_data or {}

    # ── Component scores ──────────────────────────────────────────
    env_r    = _env_raw(rd)   if rd else _env_agent(env_report)
    env_a    = _env_agent(env_report)
    infra_r  = _infra_raw(rd) if rd else _infra_agent(infra_report)
    infra_a  = _infra_agent(infra_report)
    nbhd_r   = _nbhd_raw(rd)  if rd else _nbhd_agent(neighborhood_report)
    nbhd_a   = _nbhd_agent(neighborhood_report)

    environmental_exposure  = _blend(env_r,   env_a,   raw_weight=0.4)
    infrastructure_risk     = _blend(infra_r,  infra_a, raw_weight=0.45)
    neighborhood_stability  = _blend(nbhd_r,  nbhd_a,  raw_weight=0.45)

    # ── Livability (higher = better) ──────────────────────────────
    # Elevation score directly from USGS if available
    elevation_score = _get(rd.get("usgs", {}), "elevation_score", default=50) if rd else 50

    livability = round(
        (100 - environmental_exposure) * 0.30 +
        (100 - infrastructure_risk)    * 0.20 +
        neighborhood_stability         * 0.30 +
        elevation_score                * 0.20
    )

    # ── Hidden Risk (higher = worse) ──────────────────────────────
    # Weighted average of risk scores + inverse of stability
    # Extra weight if SFHA flag set or tier-1 EPA facility nearby
    hidden_risk = round(
        (environmental_exposure + infrastructure_risk) / 2 * 0.60 +
        (100 - neighborhood_stability)                     * 0.25 +
        (100 - elevation_score)                            * 0.15
    )

    # Hard boosts for critical signals that should always surface
    fema = rd.get("fema", {})
    epa  = rd.get("epa",  {})
    if fema.get("sfha"):
        hidden_risk = max(hidden_risk, 60)
    if epa.get("tier1_count", 0) > 0 and epa.get("facility_count_half_mile", 0) > 0:
        hidden_risk = max(hidden_risk, 55)

    def clamp(v: int) -> int:
        return max(0, min(100, v))

    return {
        "livability":               clamp(livability),
        "environmental_exposure":   clamp(environmental_exposure),
        "infrastructure_risk":      clamp(infrastructure_risk),
        "neighborhood_stability":   clamp(neighborhood_stability),
        "hidden_risk":              clamp(hidden_risk),
        # Expose component breakdowns for debugging / transparency
        "_debug": {
            "env_raw":   env_r,   "env_agent":   env_a,
            "infra_raw": infra_r, "infra_agent": infra_a,
            "nbhd_raw":  nbhd_r,  "nbhd_agent":  nbhd_a,
            "elevation_score": elevation_score,
        },
    }
