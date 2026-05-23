import asyncio
import httpx
import logging
import math

logger = logging.getLogger(__name__)

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
HEADERS = {"User-Agent": "PropIntel/1.0 (propintel@example.com)"}

# Radius bands in meters
BAND_NEAR = 300
BAND_FAR  = 1000


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance in meters between two lat/lon points."""
    R = 6_371_000
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def _build_query(radius_m: int, lat: float, lon: float) -> str:
    return f"""
[out:json][timeout:30];
(
  way["power"="line"](around:{radius_m},{lat},{lon});
  node["power"="substation"](around:{radius_m},{lat},{lon});
  way["power"="substation"](around:{radius_m},{lat},{lon});
  way["railway"~"^(rail|subway|light_rail|tram)$"](around:{radius_m},{lat},{lon});
  way["highway"~"^(motorway|trunk)$"](around:{radius_m},{lat},{lon});
  way["landuse"="industrial"](around:{radius_m},{lat},{lon});
  way["landuse"="landfill"](around:{radius_m},{lat},{lon});
  node["amenity"="waste_disposal"](around:{radius_m},{lat},{lon});
  way["amenity"="waste_disposal"](around:{radius_m},{lat},{lon});
  node["aeroway"~"^(aerodrome|runway|taxiway)$"](around:{radius_m},{lat},{lon});
  way["aeroway"~"^(aerodrome|runway|taxiway)$"](around:{radius_m},{lat},{lon});
  node["amenity"="fuel"](around:{radius_m},{lat},{lon});
  way["waterway"~"^(drain|canal)$"](around:{radius_m},{lat},{lon});
);
out center;
"""


def _tag_category(tags: dict) -> str | None:
    """Map OSM tags to a risk category name."""
    power = tags.get("power", "")
    if power == "line":
        return "power_line"
    if power == "substation":
        return "substation"

    railway = tags.get("railway", "")
    if railway in ("rail", "subway", "light_rail", "tram"):
        return "railway"

    highway = tags.get("highway", "")
    if highway in ("motorway", "trunk"):
        return "highway"

    landuse = tags.get("landuse", "")
    if landuse == "industrial":
        return "industrial"
    if landuse == "landfill":
        return "landfill"

    amenity = tags.get("amenity", "")
    if amenity == "waste_disposal":
        return "landfill"
    if amenity == "fuel":
        return "fuel_station"

    aeroway = tags.get("aeroway", "")
    if aeroway in ("aerodrome", "runway", "taxiway"):
        return "airport"

    waterway = tags.get("waterway", "")
    if waterway in ("drain", "canal"):
        return "waterway"

    return None


def _element_center(el: dict) -> tuple[float, float] | None:
    """Extract lat/lon from an Overpass element with center."""
    if el.get("type") == "node":
        return el.get("lat"), el.get("lon")
    center = el.get("center", {})
    if center:
        return center.get("lat"), center.get("lon")
    return None


def _parse_elements(elements: list[dict], query_lat: float, query_lon: float) -> dict[str, dict]:
    """
    Build per-category summary: count + nearest distance in meters.
    Returns dict keyed by category name.
    """
    categories: dict[str, dict] = {}

    for el in elements:
        tags = el.get("tags") or {}
        cat = _tag_category(tags)
        if not cat:
            continue

        coords = _element_center(el)
        if coords and coords[0] and coords[1]:
            dist = _haversine_m(query_lat, query_lon, coords[0], coords[1])
        else:
            dist = None

        if cat not in categories:
            categories[cat] = {"count": 0, "nearest_m": None}

        categories[cat]["count"] += 1
        if dist is not None:
            prev = categories[cat]["nearest_m"]
            categories[cat]["nearest_m"] = dist if prev is None else min(prev, dist)

    # Round nearest distances
    for cat in categories:
        if categories[cat]["nearest_m"] is not None:
            categories[cat]["nearest_m"] = round(categories[cat]["nearest_m"])

    return categories


def _compute_scores(near: dict, far: dict) -> dict:
    """
    Compute noise_score and hazard_score (0-100, higher = worse).

    Noise sources:   highway, railway
    Hazard sources:  power_line, substation, industrial, landfill, airport, fuel_station
    """
    def distance_weight(nearest_m: float | None, max_m: int) -> float:
        """Linear decay: 1.0 at 0m, 0.0 at max_m, 0 if absent."""
        if nearest_m is None:
            return 0.0
        return max(0.0, 1.0 - nearest_m / max_m)

    def cat_weight(cat: str, band: dict) -> float:
        info = band.get(cat, {})
        return distance_weight(info.get("nearest_m"), BAND_FAR)

    # Noise score — weights sum to 100; distance_weight is 0.0–1.0
    highway_w  = cat_weight("highway", far)
    railway_w  = cat_weight("railway", far)
    noise_score = round(min(100, highway_w * 60 + railway_w * 40))

    # Hazard score — weights sum to 100
    power_w      = cat_weight("power_line",   far)
    substation_w = cat_weight("substation",   far)
    industrial_w = cat_weight("industrial",   far)
    landfill_w   = cat_weight("landfill",     far)
    airport_w    = cat_weight("airport",      far)
    fuel_w       = cat_weight("fuel_station", far)

    hazard_score = round(min(100,
        power_w      * 25 +
        substation_w * 20 +
        industrial_w * 20 +
        landfill_w   * 20 +
        airport_w    * 10 +
        fuel_w       *  5
    ))

    return {"noise_score": noise_score, "hazard_score": hazard_score}


async def get_infrastructure(lat: float, lon: float) -> dict:
    try:
        async with httpx.AsyncClient(timeout=35.0) as client:
            near_task = client.post(OVERPASS_URL, data={"data": _build_query(BAND_NEAR, lat, lon)}, headers=HEADERS)
            far_task  = client.post(OVERPASS_URL, data={"data": _build_query(BAND_FAR,  lat, lon)}, headers=HEADERS)

            near_resp, far_resp = await asyncio.gather(near_task, far_task, return_exceptions=True)

        near_elements = []
        far_elements  = []

        if isinstance(near_resp, Exception):
            logger.warning(f"OSM near band failed: {near_resp}")
        else:
            near_resp.raise_for_status()
            near_elements = near_resp.json().get("elements", [])

        if isinstance(far_resp, Exception):
            logger.warning(f"OSM far band failed: {far_resp}")
        else:
            far_resp.raise_for_status()
            far_elements = far_resp.json().get("elements", [])

        near = _parse_elements(near_elements, lat, lon)
        far  = _parse_elements(far_elements,  lat, lon)
        scores = _compute_scores(near, far)

        return {
            "within_300m":  near,
            "within_1000m": far,
            "noise_score":   scores["noise_score"],
            "hazard_score":  scores["hazard_score"],
            "source": "OpenStreetMap Overpass API",
        }

    except Exception as e:
        logger.warning(f"OSM data fetch failed: {e}")
        return {"error": str(e), "source": "OpenStreetMap Overpass API"}
