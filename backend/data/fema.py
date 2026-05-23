import asyncio
import httpx
import logging

logger = logging.getLogger(__name__)

NFHL_BASE = "https://hazards.fema.gov/gis/nfhl/rest/services/public/NFHL/MapServer"

# High-risk zones that trigger SFHA mandatory insurance requirement
SFHA_ZONES = {"A", "AE", "AH", "AO", "AR", "A99", "V", "VE"}

# Zone risk classification for scoring
ZONE_RISK = {
    "VE": "critical",   # coastal + wave action
    "V":  "critical",
    "AE": "high",
    "A":  "high",
    "AH": "high",
    "AO": "high",
    "AR": "high",
    "A99":"high",
    "X":  "low",        # resolved per sfha flag below
    "D":  "unknown",
}


async def _query_layer(client: httpx.AsyncClient, layer_id: int, lat: float, lon: float, out_fields: str) -> list[dict]:
    """Query a single NFHL MapServer layer at a point, return list of attribute dicts."""
    url = f"{NFHL_BASE}/{layer_id}/query"
    params = {
        "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": out_fields,
        "returnGeometry": "false",
        "f": "json",
    }
    resp = await client.get(url, params=params)
    resp.raise_for_status()
    data = resp.json()
    return [f.get("attributes", {}) for f in data.get("features", [])]


async def get_flood_data(lat: float, lon: float) -> dict:
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            # Fetch all 3 layers in parallel
            zones_task   = _query_layer(client, 28, lat, lon, "FLD_ZONE,SFHA_TF,STUDY_TYP")
            bfe_task     = _query_layer(client, 16, lat, lon, "ELEV,LEN_UNIT")
            firm_task    = _query_layer(client, 3,  lat, lon, "EFF_DATE,PANEL,SUFFIX")

            zones, bfe_rows, firm_rows = await asyncio.gather(
                zones_task, bfe_task, firm_task, return_exceptions=True
            )

        # ── Flood Hazard Zone (layer 28) ──────────────────────────────
        zone_result = _parse_zones(zones if not isinstance(zones, Exception) else [])

        # ── Base Flood Elevation (layer 16) ───────────────────────────
        bfe_result = _parse_bfe(bfe_rows if not isinstance(bfe_rows, Exception) else [])
        if isinstance(bfe_rows, Exception):
            logger.warning(f"FEMA BFE layer failed: {bfe_rows}")

        # ── FIRM Panel (layer 3) ──────────────────────────────────────
        firm_result = _parse_firm(firm_rows if not isinstance(firm_rows, Exception) else [])
        if isinstance(firm_rows, Exception):
            logger.warning(f"FEMA FIRM layer failed: {firm_rows}")

        return {
            **zone_result,
            **bfe_result,
            **firm_result,
            "source": "OpenFEMA NFHL",
        }

    except Exception as e:
        logger.warning(f"FEMA data fetch failed: {e}")
        return {"error": str(e), "source": "OpenFEMA NFHL"}


def _parse_zones(rows: list[dict]) -> dict:
    if not rows:
        # No polygon found — truly outside mapped area, not confirmed safe
        return {
            "flood_zone": None,
            "flood_zone_confirmed": False,  # distinguishes "no data" from "confirmed X"
            "sfha": False,
            "study_type": None,
            "risk_level": "unknown",
            "all_zones": [],
        }

    primary = rows[0]
    raw_zone = (primary.get("FLD_ZONE") or "").strip().upper()
    sfha_flag = primary.get("SFHA_TF", "F") == "T"

    # Zone X has two meanings: shaded (moderate) vs unshaded (minimal)
    # SFHA_TF == "F" + zone X → unshaded (minimal); shaded X would appear
    # as a separate polygon — treat shaded X as moderate
    if raw_zone == "X":
        risk_level = "moderate" if sfha_flag else "low"
    else:
        risk_level = ZONE_RISK.get(raw_zone, "unknown")

    return {
        "flood_zone": raw_zone or None,
        "flood_zone_confirmed": True,
        "sfha": raw_zone.rstrip("0123456789") in SFHA_ZONES or sfha_flag,
        "study_type": primary.get("STUDY_TYP"),
        "risk_level": risk_level,
        "all_zones": rows,
    }


def _parse_bfe(rows: list[dict]) -> dict:
    if not rows:
        return {"bfe_feet": None, "bfe_available": False}

    row = rows[0]
    elev = row.get("ELEV")
    unit = (row.get("LEN_UNIT") or "FEET").upper()

    if elev is None:
        return {"bfe_feet": None, "bfe_available": False}

    try:
        elev_ft = float(elev)
        if unit == "METERS":
            elev_ft = round(elev_ft * 3.28084, 2)
        return {"bfe_feet": elev_ft, "bfe_available": True}
    except (TypeError, ValueError):
        return {"bfe_feet": None, "bfe_available": False}


def _parse_firm(rows: list[dict]) -> dict:
    if not rows:
        return {"firm_panel": None, "firm_effective_date": None, "firm_panel_age_years": None}

    row = rows[0]
    panel    = row.get("PANEL")
    suffix   = row.get("SUFFIX", "")
    eff_date = row.get("EFF_DATE")  # milliseconds epoch from ESRI

    age_years = None
    date_str  = None
    if eff_date:
        try:
            from datetime import datetime, timezone
            dt = datetime.fromtimestamp(int(eff_date) / 1000, tz=timezone.utc)
            date_str  = dt.strftime("%Y-%m-%d")
            age_years = (datetime.now(tz=timezone.utc) - dt).days // 365
        except Exception:
            pass

    return {
        "firm_panel": f"{panel}{suffix}" if panel else None,
        "firm_effective_date": date_str,
        "firm_panel_age_years": age_years,
    }
