import asyncio
import httpx
import logging
import math

logger = logging.getLogger(__name__)

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
HEADERS = {"User-Agent": "PropIntel/1.0 (propintel@example.com)"}

BAND_NEAR  = 300
BAND_FAR   = 1000
BAND_ROADS = 2000   # extended radius for traffic/noise from major roads
BAND_AMEN  = 1500   # amenity search radius


# Noise contribution weight per road class (0-100 scale per unit)
ROAD_NOISE_WEIGHT = {
    "motorway":   100,
    "trunk":       85,
    "primary":     65,   # US Routes, MA state routes — the main fix
    "secondary":   35,
    "tertiary":    15,
}


def _haversine_m(lat1, lon1, lat2, lon2) -> float:
    R = 6_371_000
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


def _build_hazard_query(radius_m: int, lat: float, lon: float) -> str:
    return f"""
[out:json][timeout:30];
(
  way["power"="line"](around:{radius_m},{lat},{lon});
  node["power"="substation"](around:{radius_m},{lat},{lon});
  way["power"="substation"](around:{radius_m},{lat},{lon});
  way["railway"~"^(rail|subway|light_rail|tram)$"](around:{radius_m},{lat},{lon});
  way["highway"~"^(motorway|trunk|primary|secondary|tertiary)$"](around:{radius_m},{lat},{lon});
  way["landuse"="industrial"](around:{radius_m},{lat},{lon});
  way["landuse"="landfill"](around:{radius_m},{lat},{lon});
  node["amenity"="waste_disposal"](around:{radius_m},{lat},{lon});
  node["aeroway"~"^(aerodrome|runway)$"](around:{radius_m},{lat},{lon});
  way["aeroway"~"^(aerodrome|runway)$"](around:{radius_m},{lat},{lon});
  node["amenity"="fuel"](around:{radius_m},{lat},{lon});
);
out center tags;
"""


def _build_road_query(radius_m: int, lat: float, lon: float) -> str:
    """Extended radius query for major roads only — captures roads that generate
    significant noise/traffic beyond the standard 1km hazard band."""
    return f"""
[out:json][timeout:30];
(
  way["highway"~"^(motorway|trunk|primary)$"](around:{radius_m},{lat},{lon});
);
out center tags;
"""


def _build_amenity_query(radius_m: int, lat: float, lon: float) -> str:
    return f"""
[out:json][timeout:30];
(
  node["amenity"~"^(school|university|college)$"](around:{radius_m},{lat},{lon});
  way["amenity"~"^(school|university|college)$"](around:{radius_m},{lat},{lon});
  node["leisure"~"^(park|nature_reserve|playground)$"](around:{radius_m},{lat},{lon});
  way["leisure"~"^(park|nature_reserve)$"](around:{radius_m},{lat},{lon});
  node["shop"~"^(supermarket|grocery|convenience)$"](around:{radius_m},{lat},{lon});
  way["shop"~"^(supermarket|grocery)$"](around:{radius_m},{lat},{lon});
  node["public_transport"="stop_position"](around:{radius_m},{lat},{lon});
  node["highway"="bus_stop"](around:{radius_m},{lat},{lon});
  node["railway"~"^(station|halt|tram_stop|subway_entrance)$"](around:{radius_m},{lat},{lon});
  node["amenity"~"^(hospital|clinic|doctors)$"](around:{radius_m},{lat},{lon});
  way["amenity"~"^(hospital|clinic)$"](around:{radius_m},{lat},{lon});
  node["amenity"~"^(restaurant|cafe|bar)$"](around:{radius_m},{lat},{lon});
);
out center tags;
"""


def _road_class(tags: dict) -> str | None:
    hw = tags.get("highway", "")
    if hw in ROAD_NOISE_WEIGHT:
        return hw
    return None


def _tag_hazard_category(tags: dict) -> str | None:
    power = tags.get("power", "")
    if power == "line":       return "power_line"
    if power == "substation": return "substation"

    railway = tags.get("railway", "")
    if railway in ("rail", "subway", "light_rail", "tram"):
        return "railway"

    hw = tags.get("highway", "")
    if hw in ROAD_NOISE_WEIGHT:
        return "highway"

    landuse = tags.get("landuse", "")
    if landuse == "industrial": return "industrial"
    if landuse == "landfill":   return "landfill"

    amenity = tags.get("amenity", "")
    if amenity == "waste_disposal": return "landfill"
    if amenity == "fuel":           return "fuel_station"

    aeroway = tags.get("aeroway", "")
    if aeroway in ("aerodrome", "runway"): return "airport"

    return None


def _tag_amenity_category(tags: dict) -> str | None:
    amenity = tags.get("amenity", "")
    leisure = tags.get("leisure", "")
    shop    = tags.get("shop", "")
    pt      = tags.get("public_transport", "")
    railway = tags.get("railway", "")
    hw      = tags.get("highway", "")

    if amenity in ("school", "university", "college"):  return "school"
    if leisure in ("park", "nature_reserve"):            return "park"
    if leisure == "playground":                          return "park"
    if shop in ("supermarket", "grocery", "convenience"):return "grocery"
    if pt == "stop_position" or hw == "bus_stop":        return "transit_stop"
    if railway in ("station", "halt", "tram_stop", "subway_entrance"): return "transit_stop"
    if amenity in ("hospital", "clinic", "doctors"):     return "healthcare"
    if amenity in ("restaurant", "cafe", "bar"):         return "restaurant"
    return None


def _element_center(el: dict) -> tuple[float, float] | None:
    if el.get("type") == "node":
        return el.get("lat"), el.get("lon")
    center = el.get("center", {})
    if center:
        return center.get("lat"), center.get("lon")
    return None


def _parse_hazard_elements(elements: list[dict], qlat: float, qlon: float) -> dict:
    cats: dict[str, dict] = {}
    for el in elements:
        tags = el.get("tags") or {}
        cat  = _tag_hazard_category(tags)
        if not cat:
            continue
        coords = _element_center(el)
        dist = _haversine_m(qlat, qlon, coords[0], coords[1]) if (coords and coords[0]) else None
        if cat not in cats:
            cats[cat] = {"count": 0, "nearest_m": None}
        cats[cat]["count"] += 1
        if dist is not None:
            prev = cats[cat]["nearest_m"]
            cats[cat]["nearest_m"] = dist if prev is None else min(prev, dist)
    for cat in cats:
        if cats[cat]["nearest_m"] is not None:
            cats[cat]["nearest_m"] = round(cats[cat]["nearest_m"])
    return cats


def _parse_road_elements(elements: list[dict], qlat: float, qlon: float) -> dict:
    """
    Returns per-road-class breakdown:
    { "primary": {"count": N, "nearest_m": M, "names": ["Route 9", ...]}, ... }
    """
    roads: dict[str, dict] = {}
    for el in elements:
        tags   = el.get("tags") or {}
        cls    = _road_class(tags)
        if not cls:
            continue
        coords = _element_center(el)
        dist   = _haversine_m(qlat, qlon, coords[0], coords[1]) if (coords and coords[0]) else None
        name   = tags.get("name") or tags.get("ref") or ""

        if cls not in roads:
            roads[cls] = {"count": 0, "nearest_m": None, "names": []}
        roads[cls]["count"] += 1
        if dist is not None:
            prev = roads[cls]["nearest_m"]
            roads[cls]["nearest_m"] = dist if prev is None else min(prev, dist)
        if name and name not in roads[cls]["names"] and len(roads[cls]["names"]) < 5:
            roads[cls]["names"].append(name)

    for cls in roads:
        if roads[cls]["nearest_m"] is not None:
            roads[cls]["nearest_m"] = round(roads[cls]["nearest_m"])
    return roads


def _parse_amenity_elements(elements: list[dict], qlat: float, qlon: float) -> dict:
    cats: dict[str, dict] = {}
    for el in elements:
        tags = el.get("tags") or {}
        cat  = _tag_amenity_category(tags)
        if not cat:
            continue
        coords = _element_center(el)
        dist   = _haversine_m(qlat, qlon, coords[0], coords[1]) if (coords and coords[0]) else None
        if cat not in cats:
            cats[cat] = {"count": 0, "nearest_m": None}
        cats[cat]["count"] += 1
        if dist is not None:
            prev = cats[cat]["nearest_m"]
            cats[cat]["nearest_m"] = dist if prev is None else min(prev, dist)
    for cat in cats:
        if cats[cat]["nearest_m"] is not None:
            cats[cat]["nearest_m"] = round(cats[cat]["nearest_m"])
    return cats


def _compute_scores(near: dict, far: dict, roads: dict) -> dict:
    """
    noise_score  (0-100, higher = worse): railway + road types, distance-weighted
    hazard_score (0-100, higher = worse): power/industrial/landfill etc.
    """
    def decay(nearest_m, max_m: int) -> float:
        if nearest_m is None:
            return 0.0
        return max(0.0, 1.0 - nearest_m / max_m)

    # Road noise — weight by road class, decay over 2000m for primary+, 1000m for secondary
    road_noise = 0.0
    for cls, weight in ROAD_NOISE_WEIGHT.items():
        r = roads.get(cls, {})
        nm = r.get("nearest_m")
        if nm is None:
            # fall back to far band (1km) for minor roads
            hw_far = far.get("highway", {})
            nm = hw_far.get("nearest_m")
        max_m = 2000 if cls in ("motorway", "trunk", "primary") else 1000
        road_noise += weight * decay(nm, max_m)

    # Railway noise
    rail_nm = (far.get("railway") or {}).get("nearest_m") or \
              (near.get("railway") or {}).get("nearest_m")
    rail_noise = 40 * decay(rail_nm, 1000)

    noise_score = min(100, round(road_noise * 0.70 + rail_noise * 0.30))

    # Hazard score (unchanged logic, using far band)
    def cw(cat):
        return decay((far.get(cat) or {}).get("nearest_m"), BAND_FAR)

    hazard_score = round(min(100,
        cw("power_line")   * 25 +
        cw("substation")   * 20 +
        cw("industrial")   * 20 +
        cw("landfill")     * 20 +
        cw("airport")      * 10 +
        cw("fuel_station") *  5
    ))

    return {"noise_score": noise_score, "hazard_score": hazard_score}


async def get_infrastructure(lat: float, lon: float) -> dict:
    try:
        async with httpx.AsyncClient(timeout=40.0) as client:
            near_task  = client.post(OVERPASS_URL, data={"data": _build_hazard_query(BAND_NEAR,  lat, lon)}, headers=HEADERS)
            far_task   = client.post(OVERPASS_URL, data={"data": _build_hazard_query(BAND_FAR,   lat, lon)}, headers=HEADERS)
            roads_task = client.post(OVERPASS_URL, data={"data": _build_road_query(BAND_ROADS,   lat, lon)}, headers=HEADERS)
            amen_task  = client.post(OVERPASS_URL, data={"data": _build_amenity_query(BAND_AMEN, lat, lon)}, headers=HEADERS)

            results = await asyncio.gather(near_task, far_task, roads_task, amen_task, return_exceptions=True)

        near_resp, far_resp, roads_resp, amen_resp = results

        def _elements(r, label):
            if isinstance(r, Exception):
                logger.warning(f"OSM {label} failed: {r}")
                return []
            r.raise_for_status()
            return r.json().get("elements", [])

        near_els  = _elements(near_resp,  "near")
        far_els   = _elements(far_resp,   "far")
        roads_els = _elements(roads_resp, "roads")
        amen_els  = _elements(amen_resp,  "amenities")

        near   = _parse_hazard_elements(near_els,  lat, lon)
        far    = _parse_hazard_elements(far_els,   lat, lon)
        roads  = _parse_road_elements(roads_els,   lat, lon)
        amen   = _parse_amenity_elements(amen_els, lat, lon)
        scores = _compute_scores(near, far, roads)

        return {
            "within_300m":    near,
            "within_1000m":   far,
            "major_roads":    roads,   # per-class breakdown with names + distances
            "amenities":      amen,    # schools, parks, transit, grocery, etc.
            "noise_score":    scores["noise_score"],
            "hazard_score":   scores["hazard_score"],
            "source": "OpenStreetMap Overpass API",
        }

    except Exception as e:
        logger.warning(f"OSM data fetch failed: {e}")
        return {"error": str(e), "source": "OpenStreetMap Overpass API"}
