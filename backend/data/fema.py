import httpx
import logging

logger = logging.getLogger(__name__)

FEMA_NFHL_URL = "https://hazards.fema.gov/gis/nfhl/rest/services/public/NFHL/MapServer/28/query"


async def get_flood_data(lat: float, lon: float) -> dict:
    try:
        params = {
            "geometry": f"{lon},{lat}",
            "geometryType": "esriGeometryPoint",
            "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "FLD_ZONE,SFHA_TF,STUDY_TYP",
            "returnGeometry": "false",
            "f": "json",
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(FEMA_NFHL_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
            features = data.get("features", [])
            if not features:
                return {"flood_zone": "X", "sfha": False, "study_type": "unknown", "source": "OpenFEMA NFHL"}
            attrs = features[0].get("attributes", {})
            return {
                "flood_zone": attrs.get("FLD_ZONE", "X"),
                "sfha": attrs.get("SFHA_TF", "F") == "T",
                "study_type": attrs.get("STUDY_TYP", "unknown"),
                "all_zones": [f.get("attributes", {}) for f in features],
                "source": "OpenFEMA NFHL",
            }
    except Exception as e:
        logger.warning(f"FEMA data fetch failed: {e}")
        return {"error": str(e), "source": "OpenFEMA NFHL"}
