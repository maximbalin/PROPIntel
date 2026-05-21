import asyncio
import json
import logging
import time
import httpx
from backend.cache import get_redis
from backend.data.fema import get_flood_data
from backend.data.epa import get_epa_facilities
from backend.data.osm import get_infrastructure
from backend.data.census import get_demographics
from backend.data.usgs import get_elevation

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

    async def fetch_all(self, lat: float, lon: float) -> dict:
        cache_key = f"raw:{lat:.4f}:{lon:.4f}"
        redis = await get_redis()
        cached = await redis.get(cache_key)
        if cached:
            logger.info(f"Cache hit for {cache_key}")
            return json.loads(cached)

        results = await asyncio.gather(
            get_flood_data(lat, lon),
            get_epa_facilities(lat, lon),
            get_infrastructure(lat, lon),
            get_demographics(lat, lon),
            get_elevation(lat, lon),
            return_exceptions=True,
        )

        def safe(r):
            return r if not isinstance(r, Exception) else {"error": str(r)}

        data = {
            "fema": safe(results[0]),
            "epa": safe(results[1]),
            "osm": safe(results[2]),
            "census": safe(results[3]),
            "usgs": safe(results[4]),
        }

        await redis.setex(cache_key, 48 * 3600, json.dumps(data))
        return data
