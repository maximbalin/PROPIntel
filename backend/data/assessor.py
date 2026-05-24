"""
Massachusetts property assessor data via MassGIS L3 Parcels ArcGIS Feature Service.

Returns property characteristics (beds, baths, sqft, year built, assessed value,
last sale, lot size, heat type) from the statewide parcel layer. No API key required.
Massachusetts properties only — returns {} for out-of-state addresses.
"""
import logging
import httpx

logger = logging.getLogger(__name__)

MASSGIS_URL = (
    "https://arcgisserver.digital.mass.gov/arcgisserver/rest/services/"
    "AGOL/MassGIS_L3_Parcels/FeatureServer/0/query"
)

_USE_CODES = {
    "101": "Single Family", "102": "Condominium", "103": "Mobile Home",
    "104": "Two Family",    "105": "Three Family", "111": "Multi-Family (4-8 units)",
    "112": "Multi-Family (9+ units)", "130": "Developable Land",
    "300": "Commercial",    "400": "Industrial",
}


async def get_assessor_data(lat: float, lon: float) -> dict:
    """
    Query MassGIS L3 Parcels by coordinate. Returns assessor dict or {}.
    Bounding box pre-filter: Massachusetts is roughly 41.2–42.9 N, 69.9–73.5 W.
    """
    if not (41.2 <= lat <= 42.9 and 69.9 <= abs(lon) <= 73.5):
        return {}

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            # Search within 40 m so a road-centroid geocode still hits the adjacent parcel
            resp = await client.get(
                MASSGIS_URL,
                params={
                    "geometry":      f"{lon},{lat}",
                    "geometryType":  "esriGeometryPoint",
                    "inSR":          "4326",
                    "spatialRel":    "esriSpatialRelIntersects",
                    "distance":      40,
                    "units":         "esriSRUnit_Meter",
                    "outFields":     "*",
                    "returnGeometry":"false",
                    "f":             "json",
                },
                headers={"User-Agent": "PropIntel/1.0 (propintel@example.com)"},
            )
            resp.raise_for_status()
            features = resp.json().get("features", [])
            if not features:
                logger.debug("MassGIS: no parcel within 40 m of this location")
                return {}
            return _parse(features[0].get("attributes", {}))
    except Exception as e:
        logger.warning(f"MassGIS assessor fetch failed: {e}")
        return {}


def _int(val) -> int | None:
    try:
        v = int(float(val))
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def _str(val) -> str | None:
    s = str(val).strip() if val is not None else ""
    return s or None


def _parse(a: dict) -> dict:
    result: dict = {"source": "MassGIS L3 Parcels (MA Assessor)"}

    beds   = _int(a.get("NUM_BEDRMS"))
    baths  = _int(a.get("NUM_BATHROOMS"))
    sqft   = _int(a.get("BLDG_AREA"))
    year   = _int(a.get("YR_BUILT"))
    rooms  = _int(a.get("NUM_ROOMS"))
    stories = _int(a.get("STORIES"))

    if beds:    result["beds"]        = beds
    if baths:   result["baths"]       = float(baths)
    if sqft:    result["sqft"]        = sqft
    if year:    result["year_built"]  = year
    if rooms:   result["total_rooms"] = rooms
    if stories: result["stories"]     = stories

    lot_acres = None
    try:
        v = float(a.get("LOT_SIZE") or 0)
        if v > 0:
            lot_acres = v
    except (TypeError, ValueError):
        pass
    if lot_acres:
        result["lot_size_sqft"] = int(lot_acres * 43560)

    bldg_val  = _int(a.get("BLDG_VAL"))
    land_val  = _int(a.get("LAND_VAL"))
    total_val = _int(a.get("TOTAL_VAL"))
    fiscal_yr = _int(a.get("FISCAL_YR"))

    if bldg_val:  result["assessed_building"] = bldg_val
    if land_val:  result["assessed_land"]     = land_val
    if total_val: result["assessed_total"]    = total_val
    if fiscal_yr: result["assessment_year"]   = fiscal_yr

    ls_price = _int(a.get("LS_PRICE"))
    ls_date  = _str(a.get("LS_DATE"))
    if ls_price: result["last_sale_price"] = ls_price
    if ls_date:  result["last_sale_date"]  = ls_date

    use_code = _str(a.get("USE_CODE")) or ""
    if use_code in _USE_CODES:
        result["property_type"] = _USE_CODES[use_code]
    elif use_code:
        result["use_code"] = use_code

    for field, key in [
        ("STYLE",    "style"),
        ("HEAT_TYPE","heat_type"),
        ("FUEL_TYPE","fuel_type"),
        ("EXTERIOR", "exterior"),
        ("OWNER1",   "owner"),
        ("CITY",     "city"),
    ]:
        v = _str(a.get(field))
        if v:
            result[key] = v

    return result
