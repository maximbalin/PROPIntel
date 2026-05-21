import httpx
import logging
from backend.config import get_settings

logger = logging.getLogger(__name__)

CENSUS_GEOCODER_URL = "https://geocoding.geo.census.gov/geocoder/geographies/coordinates"
CENSUS_ACS_URL = "https://api.census.gov/data/2022/acs/acs5"


async def get_demographics(lat: float, lon: float) -> dict:
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            geo_params = {
                "x": lon,
                "y": lat,
                "benchmark": "Public_AR_Current",
                "vintage": "Current_Current",
                "layers": "Census Tracts",
                "format": "json",
            }
            geo_resp = await client.get(CENSUS_GEOCODER_URL, params=geo_params)
            geo_resp.raise_for_status()
            geo_data = geo_resp.json()

            tracts = (
                geo_data.get("result", {})
                .get("geographies", {})
                .get("Census Tracts", [])
            )
            if not tracts:
                return {"error": "No census tract found", "source": "US Census ACS 2022"}

            tract = tracts[0]
            state = tract.get("STATE")
            county = tract.get("COUNTY")
            tract_code = tract.get("TRACT")

            settings = get_settings()
            acs_params = {
                "get": "B19013_001E,B01003_001E,B25002_003E,B23025_005E",
                "for": f"tract:{tract_code}",
                "in": f"state:{state}+county:{county}",
            }
            if settings.census_api_key:
                acs_params["key"] = settings.census_api_key

            acs_resp = await client.get(CENSUS_ACS_URL, params=acs_params)
            acs_resp.raise_for_status()
            acs_data = acs_resp.json()

            if len(acs_data) < 2:
                return {"error": "No ACS data returned", "source": "US Census ACS 2022"}

            headers = acs_data[0]
            values = acs_data[1]
            row = dict(zip(headers, values))

            median_income = int(row.get("B19013_001E", -1) or -1)
            population = int(row.get("B01003_001E", 0) or 0)
            vacant_units = int(row.get("B25002_003E", 0) or 0)
            unemployed = int(row.get("B23025_005E", 0) or 0)

            return {
                "median_household_income": median_income,
                "population": population,
                "vacant_units": vacant_units,
                "unemployed": unemployed,
                "state": state,
                "county": county,
                "tract": tract_code,
                "source": "US Census ACS 2022",
            }
    except Exception as e:
        logger.warning(f"Census data fetch failed: {e}")
        return {"error": str(e), "source": "US Census ACS 2022"}
