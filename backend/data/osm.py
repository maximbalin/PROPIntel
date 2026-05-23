import asyncio
import httpx
import logging
import math

logger = logging.getLogger(__name__)

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
HEADERS = {"User-Agent": "PropIntel/1.0 (propintel@example.com)"}

# Single query radius — distance filtering done in Python from computed centers
QUERY_RADIUS = 2500

BAND_NEAR  = 300
BAND_FAR   = 1000
BAND_ROADS = 2000
BAND_AMEN  = 1500

ROAD_NOISE_WEIGHT = {
    "motorway": 100,
    "trunk":     85,
    "primary":   65,
    "secondary": 35,
    "tertiary":  15,
}


def _haversine_m(lat1, lon1, lat2, lon2) -> float:
    R = 6_371_000
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


def _build_roads_query(radius_m: int, lat: float, lon: float) -> str:
    """Fast, roads-only query — small payload, low timeout, high reliability."""
    return f"""
[out:json][timeout:25];
(
  way["highway"~"^(motorway|trunk|primary|secondary|tertiary)$"](around:{radius_m},{lat},{lon});
  way["railway"~"^(rail|subway|light_rail|tram)$"](around:{radius_m},{lat},{lon});
);
out geom tags qt;
"""


def _build_query(radius_m: int, lat: float, lon: float) -> str:
    """Comprehensive single Overpass query — all feature types, one request."""
    return f"""
[out:json][timeout:60];
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
out geom tags qt;
"""


async def _fetch_overpass(query: str) -> list[dict]:
    """Fetch with up to 2 retries and exponential backoff."""
    for attempt in range(3):
        if attempt:
            await asyncio.sleep(2 ** attempt)
        try:
            async with httpx.AsyncClient(timeout=70.0) as client:
                resp = await client.post(OVERPASS_URL, data={"data": query}, headers=HEADERS)
                resp.raise_for_status()
                data = resp.json()
                elements = data.get("elements", [])
                logger.info(f"OSM: {len(elements)} elements returned (attempt {attempt + 1})")
                return elements
        except Exception as e:
            logger.warning(f"Overpass attempt {attempt + 1}/3 failed: {e}")
    logger.error("OSM: all 3 Overpass attempts failed — returning empty")
    return []


def _road_class(tags: dict) -> str | None:
    hw = tags.get("highway", "")
    return hw if hw in ROAD_NOISE_WEIGHT else None


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

    if amenity in ("school", "university", "college"):    return "school"
    if leisure in ("park", "nature_reserve", "playground"): return "park"
    if shop in ("supermarket", "grocery", "convenience"): return "grocery"
    if pt == "stop_position" or hw == "bus_stop":         return "transit_stop"
    if railway in ("station", "halt", "tram_stop", "subway_entrance"): return "transit_stop"
    if amenity in ("hospital", "clinic", "doctors"):      return "healthcare"
    if amenity in ("restaurant", "cafe", "bar"):          return "restaurant"
    return None


def _element_min_dist(el: dict, qlat: float, qlon: float) -> float | None:
    """
    True straight-line distance to the NEAREST POINT of this element.
    For nodes: distance to the node itself.
    For ways:  minimum distance across all geometry nodes — critical for long
               roads (e.g. a highway bridge 5 ft away whose way-center is 2 km
               away would otherwise be reported at 2 km, not 5 ft).
    """
    if el.get("type") == "node":
        lat, lon = el.get("lat"), el.get("lon")
        if lat is not None and lon is not None:
            return _haversine_m(qlat, qlon, lat, lon)
        return None

    # Way with full geometry (out geom tags)
    geometry = el.get("geometry") or []
    if geometry:
        min_d = None
        for pt in geometry:
            lat, lon = pt.get("lat"), pt.get("lon")
            if lat is None or lon is None:
                continue
            d = _haversine_m(qlat, qlon, lat, lon)
            if min_d is None or d < min_d:
                min_d = d
        if min_d is not None:
            return min_d

    # Fallback: center (older cached responses may still have this)
    c = el.get("center") or {}
    lat, lon = c.get("lat"), c.get("lon")
    if lat is not None and lon is not None:
        return _haversine_m(qlat, qlon, lat, lon)
    return None


def _bump_cat(d: dict, cat: str, dist: float) -> None:
    if cat not in d:
        d[cat] = {"count": 0, "nearest_m": None}
    d[cat]["count"] += 1
    prev = d[cat]["nearest_m"]
    d[cat]["nearest_m"] = dist if prev is None else min(prev, dist)


def _parse_all(elements: list[dict], qlat: float, qlon: float):
    """Parse all elements into hazard bands, road dict, and amenity dict."""
    near:  dict = {}
    far:   dict = {}
    roads: dict = {}
    amen:  dict = {}

    for el in elements:
        tags = el.get("tags") or {}
        dist = _element_min_dist(el, qlat, qlon)
        if dist is None:
            continue

        haz = _tag_hazard_category(tags)
        if haz:
            if dist <= BAND_NEAR:
                _bump_cat(near, haz, dist)
            if dist <= BAND_FAR:
                _bump_cat(far, haz, dist)

        cls = _road_class(tags)
        if cls and dist <= BAND_ROADS:
            name = tags.get("name") or tags.get("ref") or ""
            if cls not in roads:
                roads[cls] = {"count": 0, "nearest_m": None, "names": []}
            roads[cls]["count"] += 1
            prev = roads[cls]["nearest_m"]
            roads[cls]["nearest_m"] = dist if prev is None else min(prev, dist)
            if name and name not in roads[cls]["names"] and len(roads[cls]["names"]) < 5:
                roads[cls]["names"].append(name)

        ac = _tag_amenity_category(tags)
        if ac and dist <= BAND_AMEN:
            _bump_cat(amen, ac, dist)

    # Round nearest_m values
    for d in (near, far, roads, amen):
        for k in d:
            if d[k].get("nearest_m") is not None:
                d[k]["nearest_m"] = round(d[k]["nearest_m"])

    return near, far, roads, amen


def _compute_scores(near: dict, far: dict, roads: dict) -> dict:
    def decay(nearest_m, max_m: int) -> float:
        if nearest_m is None:
            return 0.0
        return max(0.0, 1.0 - nearest_m / max_m)

    road_noise = 0.0
    for cls, weight in ROAD_NOISE_WEIGHT.items():
        nm = (roads.get(cls) or {}).get("nearest_m")
        if nm is None:
            nm = (far.get("highway") or {}).get("nearest_m")
        max_m = 2000 if cls in ("motorway", "trunk", "primary") else 1000
        road_noise += weight * decay(nm, max_m)

    rail_nm = (far.get("railway") or near.get("railway") or {}).get("nearest_m")
    noise_score = min(100, round(road_noise * 0.70 + 40 * decay(rail_nm, 1000) * 0.30))

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
        # Run a fast roads-only query first, then the full features query.
        # Roads are parsed from BOTH; amenities/hazards from the full query only.
        # If the full query fails, road risks are still guaranteed.
        road_elements, all_elements = await asyncio.gather(
            _fetch_overpass(_build_roads_query(QUERY_RADIUS, lat, lon)),
            _fetch_overpass(_build_query(QUERY_RADIUS, lat, lon)),
            return_exceptions=True,
        )

        if isinstance(road_elements, Exception):
            logger.warning(f"OSM roads-only query failed: {road_elements}")
            road_elements = []
        if isinstance(all_elements, Exception):
            logger.warning(f"OSM full query failed: {all_elements}")
            all_elements = []

        # Merge, dedup by (type, id) — full query elements take priority
        seen: set = set()
        merged: list[dict] = []
        for el in (all_elements or []) + (road_elements or []):
            key = (el.get("type"), el.get("id"))
            if key not in seen:
                seen.add(key)
                merged.append(el)

        logger.info(
            f"OSM merged: {len(road_elements)} road + {len(all_elements)} full "
            f"→ {len(merged)} unique elements"
        )

        near, far, roads, amen = _parse_all(merged, lat, lon)
        scores = _compute_scores(near, far, roads)
        return {
            "within_300m":  near,
            "within_1000m": far,
            "major_roads":  roads,
            "amenities":    amen,
            "noise_score":  scores["noise_score"],
            "hazard_score": scores["hazard_score"],
            "source": "OpenStreetMap Overpass API",
        }
    except Exception as e:
        logger.warning(f"OSM data fetch failed: {e}")
        return {"error": str(e), "source": "OpenStreetMap Overpass API"}
