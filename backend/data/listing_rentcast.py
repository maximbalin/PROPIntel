"""
Rentcast property data API.
Free tier: 50 lookups/month. Sign up at https://app.rentcast.io/app/api-keys

Set RENTCAST_API_KEY in .env to enable.
"""
import logging

import httpx

logger = logging.getLogger(__name__)

_BASE = "https://api.rentcast.io/v1"


async def get_listing_rentcast(address: str) -> dict | None:
    """
    Fetch property data from Rentcast API.
    Calls /properties (details) and /listings/sale (current price) in parallel.
    Returns None when API key is not configured.
    """
    from backend.config import get_settings
    import asyncio

    api_key = getattr(get_settings(), "rentcast_api_key", "") or ""
    if not api_key:
        return None

    headers = {"X-Api-Key": api_key, "Accept": "application/json"}
    params = {"address": address}

    async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
        prop_r, sale_r = await asyncio.gather(
            client.get(f"{_BASE}/properties", params=params),
            client.get(f"{_BASE}/listings/sale", params={**params, "status": "Active", "limit": 1}),
            return_exceptions=True,
        )

    prop_data = _parse_json(prop_r, "properties")
    sale_data = _parse_json(sale_r, "listings/sale")

    if not prop_data:
        return None

    return _extract(prop_data, sale_data)


def _parse_json(resp, label: str) -> dict | list | None:
    if isinstance(resp, Exception):
        logger.debug(f"Rentcast {label} failed: {resp}")
        return None
    if resp.status_code == 429:
        logger.warning("Rentcast rate limit hit (50/month free tier)")
        return None
    if resp.status_code != 200:
        logger.debug(f"Rentcast {label} HTTP {resp.status_code}")
        return None
    try:
        return resp.json()
    except Exception:
        return None


def _extract(prop: dict, sale_resp) -> dict | None:
    beds = prop.get("bedrooms")
    baths = prop.get("bathrooms")
    sqft = prop.get("squareFootage")
    year = prop.get("yearBuilt")

    # Current listing price from active sale listings
    price = None
    listing_url = None
    status = None
    dom = None
    if isinstance(sale_resp, list) and sale_resp:
        listing = sale_resp[0]
        price = listing.get("price") or listing.get("listPrice")
        listing_url = listing.get("listingUrl") or listing.get("url")
        status = listing.get("status")
        dom = listing.get("daysOnMarket")
    elif isinstance(sale_resp, dict):
        listings = sale_resp.get("listings") or []
        if listings:
            price = listings[0].get("price") or listings[0].get("listPrice")
            listing_url = listings[0].get("listingUrl") or listings[0].get("url")
            status = listings[0].get("status")
            dom = listings[0].get("daysOnMarket")

    # Fall back to last sale price if no active listing
    if not price:
        price = prop.get("lastSalePrice")
        status = "Off Market" if price else None

    if not any([price, beds, sqft]):
        return None

    result: dict = {
        "price": price,
        "beds": beds,
        "baths": baths,
        "sqft": sqft,
        "year_built": year,
        "listing_url": listing_url,
        "status": status,
        "days_on_market": dom,
        "photos": [],
        "source": "Rentcast",
    }

    # Property type
    pt = prop.get("propertyType")
    if pt:
        result["property_type"] = str(pt).replace("_", " ").title()

    # Lot size
    lot = prop.get("lotSize")
    if lot:
        result["lot_size_sqft"] = int(lot)

    # HOA
    hoa = prop.get("hoa") or {}
    if isinstance(hoa, dict):
        fee = hoa.get("fee") or hoa.get("monthlyFee")
        if fee is not None:
            result["hoa_fee_monthly"] = float(fee)

    # Annual tax — use most recent year
    taxes = prop.get("propertyTaxes") or {}
    if taxes:
        latest_year = max(taxes.keys(), default=None)
        if latest_year:
            result["tax_annual"] = float(taxes[latest_year].get("total", 0) or 0)

    # Garage / parking
    features = prop.get("features") or {}
    parking = features.get("parkingSpaces") or features.get("garageSpaces")
    if parking is not None:
        result["garage_spaces"] = int(parking)

    # Heating / cooling
    heating = features.get("heatingType") or features.get("heating")
    cooling = features.get("coolingType") or features.get("cooling")
    hvac_parts = [p for p in [heating, cooling] if p]
    if hvac_parts:
        result["heating_cooling"] = ", ".join(hvac_parts)

    return result
