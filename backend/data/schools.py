"""
Nearby school data via Urban Institute Education Data API (wraps NCES CCD).
No API key required. Returns nearest elementary, middle, and high school.
"""
import logging
import math
import httpx

logger = logging.getLogger(__name__)

URBAN_API = "https://educationdata.urban.org/api/v1/schools/ccd/directory/"

_LEVEL = {1: "Elementary", 2: "Middle", 3: "High", 4: "K-12", 5: "PreK",
          "1": "Elementary", "2": "Middle", "3": "High"}
_TYPE  = {1: "Public", 2: "Public Charter", 3: "Magnet",
          "1": "Public", "2": "Public Charter", "3": "Magnet"}


def _miles(lat1, lon1, lat2, lon2) -> float:
    R = 3958.8
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


async def get_nearby_schools(lat: float, lon: float, radius_miles: float = 2.0) -> dict:
    """Return nearest schools within radius_miles from NCES/Urban Institute API."""
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(
                URBAN_API,
                params={
                    "lat":           lat,
                    "lon":           lon,
                    "distance":      radius_miles,
                    "unit":          "mile",
                    "school_status": 1,
                },
                headers={"User-Agent": "PropIntel/1.0 (propintel@example.com)"},
            )
            if resp.status_code != 200:
                logger.debug(f"Urban Institute schools: HTTP {resp.status_code}")
                return {}
            data = resp.json()
    except Exception as e:
        logger.warning(f"Schools fetch failed: {e}")
        return {}

    raw = data.get("results") or []
    schools = []
    for s in raw[:30]:
        slat = s.get("latitude")
        slon = s.get("longitude")
        if not slat or not slon:
            continue
        dist = _miles(lat, lon, float(slat), float(slon))
        level_code = s.get("school_level")
        schools.append({
            "name":            s.get("school_name", "Unknown"),
            "level":           _LEVEL.get(level_code, str(level_code) if level_code else "Unknown"),
            "type":            _TYPE.get(s.get("school_type"), "Public"),
            "city":            s.get("city_location", ""),
            "state":           s.get("state_location", ""),
            "distance_miles":  round(dist, 2),
            "nces_id":         s.get("ncessch"),
            "level_code":      level_code,
        })
    schools.sort(key=lambda x: x["distance_miles"])

    nearest: dict = {}
    for s in schools:
        lc = s.get("level_code")
        if lc in (1, "1") and "elementary" not in nearest:
            nearest["elementary"] = s
        elif lc in (2, "2") and "middle" not in nearest:
            nearest["middle"] = s
        elif lc in (3, "3") and "high" not in nearest:
            nearest["high"] = s
        if len(nearest) == 3:
            break

    return {
        "total_within_radius": len(schools),
        "radius_miles":        radius_miles,
        "nearest":             nearest,
        "all":                 schools[:12],
        "source":              "NCES CCD via Urban Institute Education Data API",
    }
