import asyncio
import json
import logging
import time
import httpx
from backend.cache import get_redis
from backend.data.fema import get_flood_data
from backend.data.epa import get_epa_facilities
from backend.data.osm import get_infrastructure, _compute_scores
from backend.data.census import get_demographics
from backend.data.usgs import get_elevation
from backend.data.traffic import get_crash_data, enrich_traffic_data

logger = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
CENSUS_GEOCODER_URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
HEADERS = {"User-Agent": "PropIntel/1.0 (propintel@example.com)"}

_last_nominatim_call = 0.0


class FreeDataFetcher:
    async def geocode(self, address: str) -> tuple[float, float]:
        # Try Census geocoder first (no rate limit, works from cloud IPs)
        try:
            result = await self._geocode_census(address)
            if result:
                return result
        except Exception as e:
            logger.warning(f"Census geocoder failed: {e}")

        # Fall back to Nominatim
        try:
            result = await self._geocode_nominatim(address)
            if result:
                return result
        except Exception as e:
            logger.warning(f"Nominatim geocoder failed: {e}")

        raise ValueError(f"Address not found: {address}")

    async def _geocode_census(self, address: str) -> tuple[float, float] | None:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                CENSUS_GEOCODER_URL,
                params={"address": address, "benchmark": "Public_AR_Current", "format": "json"},
                headers=HEADERS,
            )
            resp.raise_for_status()
            data = resp.json()
            matches = data.get("result", {}).get("addressMatches", [])
            if not matches:
                return None
            coords = matches[0]["coordinates"]
            return float(coords["y"]), float(coords["x"])

    async def _geocode_nominatim(self, address: str) -> tuple[float, float] | None:
        global _last_nominatim_call
        elapsed = time.time() - _last_nominatim_call
        if elapsed < 1.0:
            await asyncio.sleep(1.0 - elapsed)
        _last_nominatim_call = time.time()

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                NOMINATIM_URL,
                params={"q": address, "format": "json", "limit": 1},
                headers=HEADERS,
            )
            resp.raise_for_status()
            results = resp.json()
            if not results:
                return None
            return float(results[0]["lat"]), float(results[0]["lon"])

    async def fetch_all(self, lat: float, lon: float, force_refresh: bool = False) -> dict:
        cache_key = f"raw:v7:{lat:.4f}:{lon:.4f}"
        redis = await get_redis()

        if not force_refresh:
            cached = await redis.get(cache_key)
            if cached:
                logger.info(f"Cache hit for {cache_key}")
                return json.loads(cached)

        # Fetch FEMA first so BFE can be passed to USGS elevation
        fema_result = await get_flood_data(lat, lon)
        bfe_feet = fema_result.get("bfe_feet") if not isinstance(fema_result, Exception) else None

        remaining = await asyncio.gather(
            get_epa_facilities(lat, lon),
            get_infrastructure(lat, lon),
            get_demographics(lat, lon),
            get_elevation(lat, lon, bfe_feet=bfe_feet),
            get_crash_data(lat, lon),
            return_exceptions=True,
        )

        def safe(r):
            return r if not isinstance(r, Exception) else {"error": str(r)}

        osm_result = safe(remaining[1])

        # ── Road-data fallback chain ───────────────────────────
        # If Overpass returned no road data, try:
        #   1. US Census TIGER REST API (fast, reliable, no key)
        #   2. OSM Nominatim reverse-geocode (slow, last resort)
        major_roads = osm_result.get("major_roads") or {}
        has_any_road = any(major_roads.get(cls, {}).get("count", 0) > 0
                           for cls in ("motorway", "trunk", "primary", "secondary"))

        if not has_any_road:
            fallback_roads = {}
            fallback_source = ""

            # Try TIGER first — single fast request, government uptime
            try:
                from backend.data.tiger_roads import get_nearby_roads_tiger
                fallback_roads = await get_nearby_roads_tiger(lat, lon)
                if fallback_roads:
                    fallback_source = "US Census TIGER/Web REST API"
            except Exception as e:
                logger.warning(f"TIGER fallback failed: {e}")

            # If TIGER also empty, try Nominatim reverse-geocode
            if not fallback_roads:
                try:
                    from backend.data.osm_nominatim import get_nearby_roads_nominatim
                    fallback_roads = await get_nearby_roads_nominatim(lat, lon)
                    if fallback_roads:
                        fallback_source = "OpenStreetMap Nominatim (last-resort fallback)"
                except Exception as e:
                    logger.warning(f"Nominatim fallback failed: {e}")

            if fallback_roads:
                merged_roads = {**major_roads, **fallback_roads}
                near   = osm_result.get("within_300m",  {}) or {}
                far    = osm_result.get("within_1000m", {}) or {}
                scores = _compute_scores(near, far, merged_roads)
                osm_result = {
                    **osm_result,
                    "major_roads":  merged_roads,
                    "noise_score":  scores["noise_score"],
                    "hazard_score": scores["hazard_score"],
                    "road_source":  fallback_source,
                }
                logger.info(f"Road fallback ({fallback_source}): {list(fallback_roads.keys())}")

        crash_result = safe(remaining[4])
        traffic_data = enrich_traffic_data(
            crash_result,
            osm_result.get("major_roads", {}),
            osm_result.get("amenities", {}),
        )

        data = {
            "fema":    safe(fema_result),
            "epa":     safe(remaining[0]),
            "osm":     osm_result,
            "census":  safe(remaining[2]),
            "usgs":    safe(remaining[3]),
            "traffic": traffic_data,
        }

        await redis.setex(cache_key, 48 * 3600, json.dumps(data))
        return data
