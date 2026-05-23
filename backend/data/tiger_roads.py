"""
US Census Bureau TIGER/Web REST API fallback for road detection.

Single API call returns road geometry within a radius. No API key,
no rate limit, very high reliability (government infrastructure).
Returns the same major_roads dict format as OSM.
"""
import logging
import math
import re

import httpx

logger = logging.getLogger(__name__)

# Primary Roads layer: MTFCC S1100 (interstates, US highways, limited-access)
# Secondary Roads are in a separate layer (MTFCC S1200: state/county routes)
TIGER_LAYERS = [
    "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/Transportation/MapServer/2/query",
    "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/Transportation/MapServer/6/query",
]


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6_371_000
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


def _min_dist_to_path(lat: float, lon: float, geometry: dict) -> float | None:
    """Minimum straight-line distance from point to any vertex in the polyline."""
    min_d = None
    for path in geometry.get("paths", []):
        for pt in path:
            d = _haversine_m(lat, lon, pt[1], pt[0])   # Esri: [lon, lat]
            if min_d is None or d < min_d:
                min_d = d
    return min_d


def _classify(fullname: str, mtfcc: str) -> str | None:
    """Map TIGER road attributes to OSM highway class."""
    name = (fullname or "").upper()
    if mtfcc == "S1100":
        # Primary road — distinguish motorway vs trunk by name
        if re.search(r"\bI[-\s]?\d", name) or "INTERSTATE" in name:
            return "motorway"
        if re.search(r"\bUS[-\s]?\d", name) or "US ROUTE" in name or "US HWY" in name:
            return "trunk"
        # Turnpikes, toll roads — treat as motorway
        if any(k in name for k in ("TURNPIKE", "TPKE", "EXPRESSWAY", "FREEWAY", "PKWY")):
            return "motorway"
        return "trunk"  # Any other limited-access road
    if mtfcc == "S1200":
        return "primary"   # State / county route
    return None


async def get_nearby_roads_tiger(lat: float, lon: float) -> dict:
    """
    Query Census TIGER for major roads within 2 km of the property.
    Returns dict matching OSM major_roads format.
    """
    roads: dict = {}

    for url in TIGER_LAYERS:
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url, params={
                    "geometry":     f"{lon:.6f},{lat:.6f}",
                    "geometryType": "esriGeometryPoint",
                    "inSR":         "4326",
                    "spatialRel":   "esriSpatialRelIntersects",
                    "distance":     "2000",
                    "units":        "esriSRUnit_Meter",
                    "outFields":    "FULLNAME,MTFCC",
                    "returnGeometry": "true",
                    "outSR":        "4326",
                    "f":            "json",
                })
                resp.raise_for_status()
                features = resp.json().get("features", [])

            logger.info(f"TIGER layer {url.split('/')[-2]}: {len(features)} features")

            for feat in features:
                attrs    = feat.get("attributes", {})
                geom     = feat.get("geometry",   {})
                fullname = attrs.get("FULLNAME", "") or ""
                mtfcc    = attrs.get("MTFCC",    "") or ""

                cls  = _classify(fullname, mtfcc)
                if cls is None:
                    continue
                dist = _min_dist_to_path(lat, lon, geom)
                if dist is None:
                    continue
                dist = round(dist)

                if cls not in roads:
                    roads[cls] = {"count": 0, "nearest_m": None, "names": []}
                roads[cls]["count"] += 1
                prev = roads[cls]["nearest_m"]
                roads[cls]["nearest_m"] = dist if prev is None else min(prev, dist)
                if fullname and fullname not in roads[cls]["names"] and len(roads[cls]["names"]) < 5:
                    roads[cls]["names"].append(fullname)

        except Exception as e:
            logger.warning(f"TIGER layer {url} failed: {e}")

    if roads:
        logger.info(f"TIGER Roads: {list(roads.keys())} near ({lat:.4f},{lon:.4f})")
    else:
        logger.warning(f"TIGER Roads: no major roads found near ({lat:.4f},{lon:.4f})")

    return roads
