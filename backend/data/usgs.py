import asyncio
import httpx
import logging
import math

logger = logging.getLogger(__name__)

USGS_EPQS_URL = "https://epqs.nationalmap.gov/v1/json"

# ~150m offset in degrees (lat: 150/111320 ≈ 0.001348; lon adjusted by cos(lat))
GRID_OFFSET_M = 150.0

# Compass directions for surrounding grid
DIRECTIONS = {
    "N":  ( 1,  0),
    "NE": ( 1,  1),
    "E":  ( 0,  1),
    "SE": (-1,  1),
    "S":  (-1,  0),
    "SW": (-1, -1),
    "W":  ( 0, -1),
    "NW": ( 1, -1),
}


def _offset_coords(lat: float, lon: float, dlat: int, dlon: int, offset_m: float) -> tuple[float, float]:
    """Apply a directional offset in meters, return new lat/lon."""
    lat_deg = offset_m / 111_320
    lon_deg = offset_m / (111_320 * math.cos(math.radians(lat)))
    return round(lat + dlat * lat_deg, 6), round(lon + dlon * lon_deg, 6)


async def _fetch_elevation(client: httpx.AsyncClient, lat: float, lon: float) -> float | None:
    """Fetch elevation in meters for a single point. Returns None on failure."""
    try:
        resp = await client.get(
            USGS_EPQS_URL,
            params={"x": lon, "y": lat, "units": "Meters", "includeDate": "false"},
        )
        resp.raise_for_status()
        data = resp.json()
        raw = data.get("value") if data.get("value") is not None else data.get("elevation")
        if raw is None:
            return None
        val = float(raw)
        # USGS returns -1000000 for ocean / no-data
        return val if val > -999_000 else None
    except Exception as e:
        logger.debug(f"Elevation fetch failed at ({lat},{lon}): {e}")
        return None


def _classify_terrain(center: float, neighbors: dict[str, float | None]) -> tuple[str, float]:
    """
    Compare property elevation to its 8 neighbours.
    Returns (terrain_type, slope_meters).

    terrain_type:
      bowl   — property is lower than most neighbours (water collects here)
      flat   — minimal variation across the grid
      slope  — directional gradient, property is mid-range
      ridge  — property is higher than most neighbours (water drains away)
    """
    valid = [v for v in neighbors.values() if v is not None]
    if not valid:
        return "unknown", 0.0

    all_values = valid + [center]
    slope = round(max(all_values) - min(all_values), 2)
    lower_count  = sum(1 for v in valid if v < center - 0.5)
    higher_count = sum(1 for v in valid if v > center + 0.5)
    total = len(valid)

    if slope < 1.5:
        terrain = "flat"
    elif higher_count >= total * 0.6:
        terrain = "bowl"
    elif lower_count >= total * 0.6:
        terrain = "ridge"
    else:
        terrain = "slope"

    return terrain, slope


def _elevation_score(
    elevation_m: float,
    terrain: str,
    slope_m: float,
    is_low_point: bool,
    bfe_feet: float | None,
) -> int:
    """
    Elevation score 0–100 (higher = safer / better drained).

    Components:
    - Absolute elevation (rough proxy for coastal/riverine flood risk)
    - Terrain type penalty/bonus
    - Above-BFE margin (when FEMA BFE available — most reliable signal)
    """
    # Base score from absolute elevation (sigmoid-ish, levels off after 30m)
    # 0m → 20,  5m → 35,  15m → 55,  30m → 70,  60m+ → ~85
    base = min(85, int(20 + 65 * (1 - math.exp(-elevation_m / 25))))

    # Terrain adjustment
    terrain_adj = {
        "ridge":   +10,
        "slope":   +2,
        "flat":    0,
        "bowl":    -15,
        "unknown": 0,
    }.get(terrain, 0)

    # Low-point penalty (property is lowest in its immediate grid)
    low_point_penalty = -10 if is_low_point else 0

    # BFE override — most precise signal when available
    # elevation_m converted to feet for comparison
    if bfe_feet is not None:
        elevation_ft = elevation_m * 3.28084
        margin_ft = elevation_ft - bfe_feet
        # Strong penalty if below or near BFE; bonus if well above
        if margin_ft < -2:
            bfe_adj = -30
        elif margin_ft < 0:
            bfe_adj = -15
        elif margin_ft < 3:
            bfe_adj = 0
        elif margin_ft < 10:
            bfe_adj = +10
        else:
            bfe_adj = +15
    else:
        bfe_adj = 0

    score = base + terrain_adj + low_point_penalty + bfe_adj
    return max(0, min(100, score))


async def get_elevation(lat: float, lon: float, bfe_feet: float | None = None) -> dict:
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            # Build all 9 tasks: center + 8 surrounding points
            grid_coords = {
                direction: _offset_coords(lat, lon, dlat, dlon, GRID_OFFSET_M)
                for direction, (dlat, dlon) in DIRECTIONS.items()
            }

            tasks = {"center": _fetch_elevation(client, lat, lon)}
            tasks.update({d: _fetch_elevation(client, *coords) for d, coords in grid_coords.items()})

            results = await asyncio.gather(*tasks.values(), return_exceptions=True)
            named = {
                k: (v if not isinstance(v, Exception) else None)
                for k, v in zip(tasks.keys(), results)
            }

        center_elev = named.pop("center")
        neighbors   = named  # remaining 8 directions

        if center_elev is None:
            return {"error": "Elevation data unavailable", "source": "USGS National Elevation Dataset"}

        terrain, slope_m = _classify_terrain(center_elev, neighbors)

        valid_neighbors = [v for v in neighbors.values() if v is not None]
        is_low_point = bool(valid_neighbors and all(center_elev <= v for v in valid_neighbors))
        is_high_point = bool(valid_neighbors and all(center_elev >= v for v in valid_neighbors))

        score = _elevation_score(center_elev, terrain, slope_m, is_low_point, bfe_feet)

        return {
            "elevation_meters":      round(center_elev, 2),
            "elevation_feet":        round(center_elev * 3.28084, 1),
            "terrain_type":          terrain,
            "slope_meters":          slope_m,
            "property_is_low_point": is_low_point,
            "property_is_high_point": is_high_point,
            "elevation_score":       score,
            "bfe_feet":              bfe_feet,
            "bfe_margin_feet":       round(center_elev * 3.28084 - bfe_feet, 1) if bfe_feet else None,
            "grid_samples":          {k: round(v, 2) for k, v in neighbors.items() if v is not None},
            "source":                "USGS National Elevation Dataset",
        }

    except Exception as e:
        logger.warning(f"USGS elevation fetch failed: {e}")
        return {"error": str(e), "source": "USGS National Elevation Dataset"}
