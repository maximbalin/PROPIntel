"""
Google Maps Geocoding API fallback for road detection.

Generates sample points in multiple directions/distances from the property
and reverse-geocodes each with result_type=route to identify nearby roads.
Calls are batched concurrently (Google allows 50 QPS).
"""
import asyncio
import logging
import math
import re

import httpx

logger = logging.getLogger(__name__)

GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"

# Bearings: N, NE, E, SE, S, SW, W, NW
_BEARINGS = list(range(0, 360, 45))
# Distances (meters): cast a tight and wide ring
_DISTANCES = [200, 600]


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6_371_000
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


def _offset(lat: float, lon: float, dist_m: float, bearing_deg: float) -> tuple[float, float]:
    R = 6_371_000
    b = math.radians(bearing_deg)
    dlat = math.degrees(dist_m * math.cos(b) / R)
    dlon = math.degrees(dist_m * math.sin(b) / (R * math.cos(math.radians(lat))))
    return lat + dlat, lon + dlon


def _classify(short_name: str, long_name: str) -> str | None:
    """Map a route's name to an OSM highway class or None for local roads."""
    s = short_name.upper().strip()
    l = long_name.upper().strip()

    # Interstate / motorway
    if re.match(r"^I-\d", s) or re.match(r"^I \d", s):
        return "motorway"
    if any(kw in l for kw in ("INTERSTATE", "MASS PIKE", "MASSACHUSETTS TURNPIKE",
                               "MASS TURNPIKE", "TURNPIKE")):
        return "motorway"

    # US Highway → trunk
    if re.match(r"^US-\d", s) or re.match(r"^US \d", s) or re.match(r"^US\d", s):
        return "trunk"

    # State highway → primary  (handles MA-9, NH-101, CT-15, etc.)
    if re.match(r"^[A-Z]{2}-\d", s):
        return "primary"
    # Spelled-out "Route NNN" or "State Route NNN"
    m = re.match(r"ROUTE\s+(\d+)", s) or re.match(r"ROUTE\s+(\d+)", l)
    if m:
        return "primary" if int(m.group(1)) <= 299 else "secondary"
    if "STATE ROUTE" in l or "STATE HWY" in l or "STATE HIGHWAY" in l:
        return "primary"

    return None  # local / residential — skip


async def get_nearby_roads_google(lat: float, lon: float, api_key: str) -> dict:
    """
    Detect nearby major roads via Google Maps Geocoding API.
    Returns a dict matching OSM major_roads format:
        { road_class: {"count": N, "nearest_m": M, "names": [...]} }
    """
    sample_points = [
        _offset(lat, lon, d, b) for d in _DISTANCES for b in _BEARINGS
    ]

    roads: dict = {}
    seen_names: set = set()
    lock = asyncio.Lock()

    async def _probe(pt_lat: float, pt_lon: float) -> None:
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(
                    GEOCODE_URL,
                    params={
                        "latlng": f"{pt_lat:.6f},{pt_lon:.6f}",
                        "result_type": "route",
                        "key": api_key,
                    },
                )
                resp.raise_for_status()
                results = resp.json().get("results", [])

            for result in results:
                for comp in result.get("address_components", []):
                    if "route" not in comp.get("types", []):
                        continue
                    short = comp.get("short_name", "")
                    long_ = comp.get("long_name", "")
                    cls = _classify(short, long_)
                    if cls is None:
                        continue

                    name = short or long_
                    dist = round(_haversine_m(lat, lon, pt_lat, pt_lon))

                    async with lock:
                        if name in seen_names:
                            continue
                        seen_names.add(name)
                        if cls not in roads:
                            roads[cls] = {"count": 0, "nearest_m": None, "names": []}
                        roads[cls]["count"] += 1
                        prev = roads[cls]["nearest_m"]
                        roads[cls]["nearest_m"] = dist if prev is None else min(prev, dist)
                        if len(roads[cls]["names"]) < 5:
                            roads[cls]["names"].append(name)

        except Exception as e:
            logger.debug(f"Google Roads probe ({pt_lat:.5f},{pt_lon:.5f}) failed: {e}")

    await asyncio.gather(*[_probe(p[0], p[1]) for p in sample_points])

    if roads:
        logger.info(f"Google Roads: detected {list(roads.keys())} near ({lat:.4f},{lon:.4f})")
    else:
        logger.warning(f"Google Roads: no major roads found near ({lat:.4f},{lon:.4f})")

    return roads
