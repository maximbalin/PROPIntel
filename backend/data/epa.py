import httpx
import logging

logger = logging.getLogger(__name__)

EPA_ECHO_URL = "https://echo.epa.gov/echo-rest/facility_search.json"


async def get_epa_facilities(lat: float, lon: float, radius_miles: float = 3.0) -> dict:
    try:
        params = {
            "p_c1lat": lat,
            "p_c1lon": lon,
            "p_c1event": "Y",
            "p_radius": radius_miles,
            "p_act": "Y",
            "output": "JSON",
        }
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(EPA_ECHO_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
            facilities = data.get("Results", {}).get("Facilities", [])
            return {
                "facility_count": len(facilities),
                "facilities": facilities[:10],
                "radius_miles": radius_miles,
                "source": "EPA ECHO",
            }
    except Exception as e:
        logger.warning(f"EPA data fetch failed: {e}")
        return {"error": str(e), "source": "EPA ECHO"}
