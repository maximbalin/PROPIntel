import httpx
import logging

logger = logging.getLogger(__name__)

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
HEADERS = {"User-Agent": "PropIntel/1.0"}


async def get_infrastructure(lat: float, lon: float, radius_m: int = 1000) -> dict:
    try:
        query = f"""
[out:json][timeout:25];
(
  way["power"="line"](around:{radius_m},{lat},{lon});
  way["railway"~"rail|subway|light_rail"](around:{radius_m},{lat},{lon});
  way["highway"~"motorway|trunk"](around:{radius_m},{lat},{lon});
);
out tags;
"""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(OVERPASS_URL, data={"data": query}, headers=HEADERS)
            resp.raise_for_status()
            data = resp.json()
            elements = data.get("elements", [])
            power_lines = sum(1 for el in elements if el.get("tags", {}).get("power") == "line")
            railway = sum(1 for el in elements if "railway" in el.get("tags", {}))
            highway = sum(1 for el in elements if el.get("tags", {}).get("highway") in ("motorway", "trunk"))
            return {
                "power_lines_count": power_lines,
                "railway_count": railway,
                "highway_count": highway,
                "total_features": len(elements),
                "radius_m": radius_m,
                "source": "OpenStreetMap Overpass API",
            }
    except Exception as e:
        logger.warning(f"OSM data fetch failed: {e}")
        return {"error": str(e), "source": "OpenStreetMap Overpass API"}
