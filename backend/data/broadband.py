"""
FCC Broadband Map API — fixed broadband availability at the property location.
No API key required. Returns provider count, max speeds, and technology types.
"""
import logging
import httpx

logger = logging.getLogger(__name__)

FCC_API = "https://broadbandmap.fcc.gov/api/public/map/listAvailability"

_TECH = {
    "10": "DSL", "11": "ADSL2", "12": "ADSL2+", "13": "VDSL", "14": "VDSL2",
    "40": "Cable (DOCSIS 3.0)", "41": "Cable (DOCSIS 3.1)", "42": "Cable",
    "50": "Fiber",
    "60": "Satellite", "61": "LEO Satellite",
    "70": "Fixed Wireless", "71": "Licensed Fixed Wireless", "72": "Unlicensed Wireless",
    "300": "Satellite (legacy)", "0": "Other Copper",
}

# FCC defines broadband as ≥25 Mbps down / ≥3 Mbps up (old standard)
# Updated 2024 standard: ≥100 Mbps down / ≥20 Mbps up
BROADBAND_THRESHOLD_DL = 25
GIGABIT_THRESHOLD_DL   = 940


async def get_broadband(lat: float, lon: float) -> dict:
    """Check FCC Broadband Map for fixed broadband availability."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                FCC_API,
                params={
                    "latitude":  lat,
                    "longitude": lon,
                    "unit":      "location",
                    "category":  "Fixed Broadband",
                },
                headers={"User-Agent": "PropIntel/1.0 (propintel@example.com)"},
            )
            if resp.status_code != 200:
                logger.debug(f"FCC Broadband API: HTTP {resp.status_code} — {resp.text[:200]}")
                return {}
            return _parse(resp.json())
    except Exception as e:
        logger.warning(f"FCC Broadband fetch failed: {e}")
        return {}


def _parse(data: dict) -> dict:
    providers = data.get("results") or data.get("availability") or []
    if not providers:
        return {"available": False, "provider_count": 0, "max_download_mbps": 0,
                "technologies": [], "source": "FCC Broadband Map"}

    max_dl = max_ul = 0
    techs: set = set()
    names: list = []

    for p in providers:
        dl   = p.get("max_download_speed") or p.get("download_speed") or 0
        ul   = p.get("max_upload_speed")   or p.get("upload_speed")   or 0
        tech = str(p.get("technology_code") or p.get("tech_code") or "")
        name = p.get("provider_name") or p.get("brand_name") or ""

        max_dl = max(max_dl, dl)
        max_ul = max(max_ul, ul)
        if tech:
            techs.add(_TECH.get(tech, f"Tech-{tech}"))
        if name and name not in names:
            names.append(name)

    return {
        "available":          max_dl >= BROADBAND_THRESHOLD_DL,
        "provider_count":     len(providers),
        "provider_names":     names[:5],
        "max_download_mbps":  max_dl,
        "max_upload_mbps":    max_ul,
        "technologies":       sorted(techs),
        "has_gigabit":        max_dl >= GIGABIT_THRESHOLD_DL,
        "source":             "FCC Broadband Map",
    }
