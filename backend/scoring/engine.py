def compute_scores(env_report: dict, infra_report: dict, neighborhood_report: dict) -> dict:
    env_sub = env_report.get("sub_scores", {})
    infra_sub = infra_report.get("sub_scores", {})
    nbhd_sub = neighborhood_report.get("sub_scores", {})

    environmental_exposure = int(
        env_sub.get("flood_score", 50) * 0.5 +
        env_sub.get("pollution_score", 50) * 0.3 +
        (100 - env_sub.get("elevation_score", 50)) * 0.2
    )

    infrastructure_risk = int(
        infra_sub.get("power_line_score", 50) * 0.4 +
        infra_sub.get("highway_noise_score", 50) * 0.35 +
        infra_sub.get("rail_score", 50) * 0.25
    )

    neighborhood_stability = int(
        nbhd_sub.get("income_score", 50) * 0.4 +
        nbhd_sub.get("employment_score", 50) * 0.35 +
        (100 - nbhd_sub.get("vacancy_score", 50)) * 0.25
    )

    livability = int(
        (100 - environmental_exposure) * 0.3 +
        (100 - infrastructure_risk) * 0.2 +
        neighborhood_stability * 0.3 +
        env_sub.get("elevation_score", 50) * 0.2
    )

    hidden_risk = int(
        (environmental_exposure + infrastructure_risk) / 2 * 0.7 +
        (100 - neighborhood_stability) * 0.3
    )

    return {
        "livability": max(0, min(100, livability)),
        "environmental_exposure": max(0, min(100, environmental_exposure)),
        "infrastructure_risk": max(0, min(100, infrastructure_risk)),
        "neighborhood_stability": max(0, min(100, neighborhood_stability)),
        "hidden_risk": max(0, min(100, hidden_risk)),
    }
