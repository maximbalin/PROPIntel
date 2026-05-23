import httpx
import logging
import math

logger = logging.getLogger(__name__)

FARS_URL = "https://crashviewer.nhtsa.dot.gov/CrashAPI/crashes/GetCrashesByLocation"
HEADERS = {"User-Agent": "PropIntel/1.0 (propintel@example.com)"}

# Estimated Annual Average Daily Traffic by road class (US national averages)
AADT_ESTIMATES = {
    "motorway": 120_000,
    "trunk":     45_000,
    "primary":   25_000,
    "secondary":  8_000,
    "tertiary":   2_000,
}

PEAK_HOUR_FACTOR = {
    "motorway": 0.12,
    "trunk":    0.11,
    "primary":  0.10,
    "secondary": 0.09,
    "tertiary":  0.08,
}

# Typical noise level (dBA Leq) at 15m from road centerline
NOISE_DB_AT_15M = {
    "motorway": 80,
    "trunk":    72,
    "primary":  68,
    "secondary": 62,
    "tertiary":  56,
}


def _noise_at_distance(road_class: str, distance_m: float) -> int | None:
    base_db = NOISE_DB_AT_15M.get(road_class)
    if base_db is None or distance_m <= 0:
        return None
    ref = max(15.0, distance_m)
    decay = 10 * math.log10(ref / 15.0) * 1.5   # ~4.5 dB per doubling
    return max(0, round(base_db - decay))


def _timeline_patterns(road_classes: list[str], near_school: bool, near_commercial: bool) -> list[str]:
    patterns = []
    has_motorway = "motorway" in road_classes
    has_primary  = "primary" in road_classes or "trunk" in road_classes

    if has_motorway:
        patterns.append("Heavy congestion Mon-Fri 7–9 AM and 4–7 PM (commuter peak)")
        patterns.append("Weekend travel surge Fri 3 PM – Sun 8 PM (holiday/leisure)")
    if has_primary:
        patterns.append("Continuous daytime traffic 6 AM–10 PM on primary road corridor")
        patterns.append("Commercial delivery vehicles peak Tue–Thu 9 AM–3 PM")
    if near_school:
        patterns.append("School-zone congestion Mon–Fri 7:30–8:30 AM and 2:30–3:30 PM (Sep–Jun)")
        patterns.append("Traffic drops 30–50% Jun–Aug during summer break")
    if near_commercial and not has_motorway:
        patterns.append("Weekend retail peak Sat 11 AM–6 PM (shopping traffic)")
    if "secondary" in road_classes and not has_primary:
        patterns.append("Moderate daytime traffic; light volume after 8 PM")
    if not patterns:
        patterns.append("Light residential traffic — no significant recurrent peak patterns identified")
    return patterns


async def get_crash_data(lat: float, lon: float) -> dict:
    """Fetch NHTSA FARS historical crash records within 0.5 mi, 2019–2023."""
    result: dict = {
        "total_crashes": 0,
        "fatal_crashes": 0,
        "injury_crashes": 0,
        "years": "2019-2023",
        "radius_miles": 0.5,
    }
    try:
        async with httpx.AsyncClient(timeout=20.0, headers=HEADERS) as client:
            resp = await client.get(FARS_URL, params={
                "fromCaseYear": 2019,
                "toCaseYear":   2023,
                "cevent":       1,
                "aoi":          1,
                "lat":          lat,
                "lng":          lon,
                "radius":       0.5,
                "caseType":     1,
                "maxRecords":   50,
                "format":       "json",
            })
            resp.raise_for_status()
            data = resp.json()
            records = data.get("Results", [])
            if records and isinstance(records[0], list):
                records = records[0]
            result["total_crashes"] = len(records)
            result["fatal_crashes"] = sum(
                1 for r in records
                if isinstance(r, dict) and int(r.get("FATALS", 0) or 0) > 0
            )
            result["injury_crashes"] = result["total_crashes"] - result["fatal_crashes"]
    except Exception as e:
        logger.warning(f"NHTSA FARS fetch failed: {e}")
        result["error"] = str(e)
    return result


def enrich_traffic_data(crash_data: dict, osm_roads: dict, osm_amenities: dict) -> dict:
    """Combine FARS crash data with AADT estimates and timeline patterns from OSM."""
    roads     = osm_roads or {}
    amenities = osm_amenities or {}

    aadt_by_class: dict = {}
    for cls, aadt in AADT_ESTIMATES.items():
        rd = roads.get(cls, {})
        if rd.get("count", 0) > 0:
            nearest_m = rd.get("nearest_m")
            aadt_by_class[cls] = {
                "estimated_aadt":        aadt,
                "nearest_m":             nearest_m,
                "road_names":            rd.get("names", [])[:3],
                "peak_hour_vehicles":    round(aadt * PEAK_HOUR_FACTOR.get(cls, 0.10)),
                "noise_db_at_property":  _noise_at_distance(cls, nearest_m) if nearest_m else None,
            }

    road_classes    = list(aadt_by_class.keys())
    near_school     = (amenities.get("school", {}).get("count", 0) > 0 and
                       (amenities.get("school", {}).get("nearest_m") or 9999) < 600)
    near_commercial = any(roads.get(cls, {}).get("count", 0) > 0 for cls in ("trunk", "primary"))
    timeline        = _timeline_patterns(road_classes, near_school, near_commercial)

    return {
        "aadt_estimates":    aadt_by_class,
        "timeline_patterns": timeline,
        "crash_summary":     crash_data,
        "source":            "NHTSA FARS (crashes), OpenStreetMap road class AADT estimates",
    }
