import httpx
import logging

logger = logging.getLogger(__name__)

USGS_EPQS_URL = "https://epqs.nationalmap.gov/v1/json"


async def get_elevation(lat: float, lon: float) -> dict:
    try:
        params = {
            "x": lon,
            "y": lat,
            "units": "Meters",
            "includeDate": "false",
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(USGS_EPQS_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
            elevation = data.get("value")
            if elevation is None:
                elevation = data.get("elevation")
            return {
                "elevation_meters": float(elevation) if elevation is not None else None,
                "source": "USGS National Elevation Dataset",
            }
    except Exception as e:
        logger.warning(f"USGS elevation fetch failed: {e}")
        return {"error": str(e), "source": "USGS National Elevation Dataset"}
