import asyncio
import httpx
import logging
import math

logger = logging.getLogger(__name__)

EPA_ECHO_URL = "https://echo.epa.gov/echo-rest/facility_search.json"

# Distance bands queried in parallel
RADIUS_BANDS = [0.5, 1.5, 3.0]

# Hazard tier definitions — fields that flag the facility type
TIER_1_FLAGS = ("FacDerivedSWINOn", "FacDerivedRCRAOn", "FacDerivedTRIOn")   # Superfund / hazardous waste / toxic releases
TIER_2_FLAGS = ("FacDerivedCAAAOn", "FacDerivedCWAOn")                        # Air / water violations
# Tier 3 = anything with active violations not in tier 1 or 2


def _haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Straight-line distance in miles between two lat/lon points."""
    R = 3958.8  # Earth radius in miles
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def _classify_tier(facility: dict) -> int:
    """Return hazard tier 1 (worst) / 2 / 3 (least) for a facility."""
    if any(facility.get(f) == "Y" for f in TIER_1_FLAGS):
        return 1
    if any(facility.get(f) == "Y" for f in TIER_2_FLAGS):
        return 2
    return 3


def _proximity_risk_score(distance_miles: float, tier: int) -> float:
    """
    Score 0–100 combining tier weight and distance decay.
    Closer + higher tier = higher score.
    Tier weights: 1→100, 2→60, 3→30
    Distance decay: halves every 0.5 miles.
    """
    tier_weight = {1: 100, 2: 60, 3: 30}[tier]
    decay = math.exp(-1.386 * distance_miles)   # ln(2)/0.5 ≈ 1.386
    return round(tier_weight * decay, 2)


def _parse_facility(raw: dict, query_lat: float, query_lon: float) -> dict:
    """Normalize a raw ECHO facility record into a clean dict."""
    try:
        fac_lat = float(raw.get("FacLat") or 0)
        fac_lon = float(raw.get("FacLong") or 0)
        distance = _haversine_miles(query_lat, query_lon, fac_lat, fac_lon)
    except (TypeError, ValueError):
        distance = float(raw.get("FacDistance") or 99)

    tier = _classify_tier(raw)
    score = _proximity_risk_score(distance, tier)

    try:
        penalties = float(raw.get("FacTotalPenalties") or 0)
    except (TypeError, ValueError):
        penalties = 0.0

    return {
        "name":             raw.get("FacName"),
        "distance_miles":   round(distance, 3),
        "tier":             tier,
        "risk_score":       score,
        "active":           raw.get("FacActiveFlag") == "Y",
        "superfund":        raw.get("FacDerivedSWINOn") == "Y",
        "hazardous_waste":  raw.get("FacDerivedRCRAOn") == "Y",
        "toxic_releases":   raw.get("FacDerivedTRIOn") == "Y",
        "air_violations":   raw.get("FacDerivedCAAAOn") == "Y",
        "water_violations": raw.get("FacDerivedCWAOn") == "Y",
        "violation_count":  int(raw.get("FacViolationCount") or 0),
        "penalty_count":    int(raw.get("FacPenaltyCount") or 0),
        "total_penalties_usd": penalties,
        "sic_codes":        raw.get("FacSICCodes", ""),
    }


async def _fetch_band(client: httpx.AsyncClient, lat: float, lon: float, radius: float) -> list[dict]:
    """Fetch raw facility list for one radius band."""
    params = {
        "p_c1lat":   lat,
        "p_c1lon":   lon,
        "p_c1event": "Y",
        "p_radius":  radius,
        "output":    "JSON",
    }
    resp = await client.get(EPA_ECHO_URL, params=params)
    resp.raise_for_status()
    data = resp.json()
    return data.get("Results", {}).get("Facilities", [])


async def get_epa_facilities(lat: float, lon: float) -> dict:
    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            band_results = await asyncio.gather(
                *[_fetch_band(client, lat, lon, r) for r in RADIUS_BANDS],
                return_exceptions=True,
            )

        # Merge results across bands, deduplicate by facility name + coords
        seen: set[str] = set()
        all_facilities: list[dict] = []

        for band_raw in band_results:
            if isinstance(band_raw, Exception):
                logger.warning(f"EPA band fetch failed: {band_raw}")
                continue
            for raw in band_raw:
                key = f"{raw.get('FacName')}|{raw.get('FacLat')}|{raw.get('FacLong')}"
                if key in seen:
                    continue
                seen.add(key)
                all_facilities.append(_parse_facility(raw, lat, lon))

        # Sort by risk score descending
        all_facilities.sort(key=lambda f: f["risk_score"], reverse=True)

        # Band counts (for scoring context)
        within_half  = [f for f in all_facilities if f["distance_miles"] <= 0.5]
        within_1_5   = [f for f in all_facilities if f["distance_miles"] <= 1.5]
        tier1_total  = [f for f in all_facilities if f["tier"] == 1]
        tier2_total  = [f for f in all_facilities if f["tier"] == 2]

        # Aggregate risk score: sum of top-5 proximity scores, capped at 100
        aggregate_risk = min(100, round(sum(f["risk_score"] for f in all_facilities[:5])))

        return {
            "facility_count_total":     len(all_facilities),
            "facility_count_half_mile": len(within_half),
            "facility_count_1_5_mile":  len(within_1_5),
            "tier1_count":              len(tier1_total),
            "tier2_count":              len(tier2_total),
            "aggregate_risk_score":     aggregate_risk,
            "top_facilities":           all_facilities[:5],
            "radii_queried_miles":      RADIUS_BANDS,
            "source":                   "EPA ECHO",
        }

    except Exception as e:
        logger.warning(f"EPA data fetch failed: {e}")
        return {"error": str(e), "source": "EPA ECHO"}
