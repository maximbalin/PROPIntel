"""
Nominatim reverse-geocode fallback for road detection.

When all Overpass mirrors fail, samples points around the property and
reverse-geocodes each via Nominatim to identify nearby major roads.
Sequential requests with 1.1 s gap to respect the OSM usage policy
(max 1 req/s per IP).
"""
import asyncio
import logging
import math

import httpx

logger = logging.getLogger(__name__)

NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
HEADERS = {"User-Agent": "PropIntel/1.0 (propintel@example.com)"}

# 8 compass bearings × 2 ring distances = 16 sample points
_BEARINGS  = list(range(0, 360, 45))   # N NE E SE S SW W NW
_DISTANCES = [250, 700]                 # metres

_OSM_ROAD_CLASSES = {"motorway", "trunk", "primary", "secondary", "tertiary"}


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


async def get_nearby_roads_nominatim(lat: float, lon: float) -> dict:
    """
    Reverse-geocode a grid of sample points via Nominatim to find nearby roads.
    Returns a dict matching the OSM major_roads format:
        { road_class: {"count": N, "nearest_m": M, "names": [...]} }
    """
    sample_points = [_offset(lat, lon, d, b) for d in _DISTANCES for b in _BEARINGS]
    roads: dict = {}
    seen_names: set = set()

    async with httpx.AsyncClient(timeout=10.0, headers=HEADERS) as client:
        for pt_lat, pt_lon in sample_points:
            await asyncio.sleep(1.1)   # OSM usage policy: max 1 req/s
            try:
                resp = await client.get(
                    NOMINATIM_REVERSE_URL,
                    params={
                        "lat": f"{pt_lat:.6f}",
                        "lon": f"{pt_lon:.6f}",
                        "format": "jsonv2",
                        "zoom": 17,          # road-level resolution
                        "addressdetails": 1,
                    },
                )
                resp.raise_for_status()
                data = resp.json()

                # Nominatim returns category="highway" and type="motorway" etc.
                if data.get("category") != "highway":
                    continue
                cls = data.get("type", "")
                if cls not in _OSM_ROAD_CLASSES:
                    continue

                name = (
                    data.get("name")
                    or data.get("address", {}).get("road")
                    or cls
                )
                if name in seen_names:
                    continue
                seen_names.add(name)

                dist = round(_haversine_m(lat, lon, pt_lat, pt_lon))
                if cls not in roads:
                    roads[cls] = {"count": 0, "nearest_m": None, "names": []}
                roads[cls]["count"] += 1
                prev = roads[cls]["nearest_m"]
                roads[cls]["nearest_m"] = dist if prev is None else min(prev, dist)
                if len(roads[cls]["names"]) < 5:
                    roads[cls]["names"].append(name)

            except Exception as e:
                logger.debug(f"Nominatim probe ({pt_lat:.5f},{pt_lon:.5f}) failed: {e}")

    if roads:
        logger.info(f"Nominatim Roads: detected {list(roads.keys())} near ({lat:.4f},{lon:.4f})")
    else:
        logger.warning(f"Nominatim Roads: no major roads found near ({lat:.4f},{lon:.4f})")

    return roads
