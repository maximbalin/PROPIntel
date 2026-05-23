"""
Firecrawl-powered property listing scraper.

Uses the Firecrawl REST API (https://api.firecrawl.dev/v1/scrape) to extract
structured property data from Realtor.com, Zillow, and Redfin. Firecrawl handles
JavaScript rendering, anti-bot countermeasures, and AI-based JSON extraction.

Requires FIRECRAWL_API_KEY in environment. Falls back gracefully when not set.
"""
import logging
import re
from urllib.parse import quote_plus

import httpx

logger = logging.getLogger(__name__)

FIRECRAWL_SCRAPE_URL = "https://api.firecrawl.dev/v1/scrape"

# JSON schema passed to Firecrawl's AI extraction engine
PROPERTY_SCHEMA = {
    "type": "object",
    "properties": {
        "price": {
            "type": "number",
            "description": "Current listing price in USD (or last sold price if not for sale)",
        },
        "beds": {"type": "integer", "description": "Number of bedrooms"},
        "baths": {
            "type": "number",
            "description": "Total bathrooms including half baths (e.g. 2.5)",
        },
        "sqft": {
            "type": "integer",
            "description": "Interior living area in square feet",
        },
        "year_built": {
            "type": "integer",
            "description": "Year the home was originally built",
        },
        "property_type": {
            "type": "string",
            "description": "e.g. Single Family, Condo, Townhouse, Multi-family",
        },
        "lot_size_sqft": {
            "type": "integer",
            "description": "Lot size in square feet (0 for condos with no private lot)",
        },
        "hoa_fee_monthly": {
            "type": "number",
            "description": "Monthly HOA/condo fee in USD if applicable, else null",
        },
        "tax_annual": {
            "type": "number",
            "description": "Annual property tax amount in USD",
        },
        "status": {
            "type": "string",
            "description": "Listing status: For Sale, Pending, Sold, Off Market",
        },
        "days_on_market": {
            "type": "integer",
            "description": "Number of days the property has been listed",
        },
        "garage_spaces": {
            "type": "integer",
            "description": "Number of garage/parking spaces",
        },
        "heating_cooling": {
            "type": "string",
            "description": "Heating and cooling system type (e.g. Gas forced air, Central AC)",
        },
        "description": {
            "type": "string",
            "description": "Full property description from the listing (max 500 chars)",
        },
        "photos": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Direct URLs to listing photos (up to 4)",
        },
    },
    "required": ["price", "beds", "baths", "sqft"],
}


def _parse_address(address: str) -> dict:
    """Extract city, state, zip, and street from a US address string."""
    parts = [p.strip() for p in address.split(",")]
    street = parts[0] if parts else address
    city = parts[1].strip() if len(parts) > 1 else ""
    state_zip = parts[2].strip() if len(parts) > 2 else ""
    state = re.match(r"([A-Z]{2})", state_zip)
    state = state.group(1) if state else ""
    return {"street": street, "city": city, "state": state}


async def _firecrawl_scrape(
    url: str, prompt: str, api_key: str, timeout: float = 45.0
) -> dict | None:
    """POST to Firecrawl /v1/scrape with JSON extraction schema. Returns extracted data or None."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                FIRECRAWL_SCRAPE_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "url": url,
                    "formats": ["json"],
                    "jsonOptions": {
                        "schema": PROPERTY_SCHEMA,
                        "prompt": prompt,
                    },
                    "waitFor": 3000,
                    "onlyMainContent": True,
                },
            )
            resp.raise_for_status()
            body = resp.json()
            if not body.get("success"):
                logger.warning(f"Firecrawl non-success for {url}: {body.get('error')}")
                return None
            extracted = (body.get("data") or {}).get("json") or {}
            if not extracted.get("price") and not extracted.get("beds"):
                logger.debug(f"Firecrawl returned empty extraction for {url}")
                return None
            return extracted
    except httpx.HTTPStatusError as e:
        logger.warning(f"Firecrawl HTTP {e.response.status_code} for {url}")
        return None
    except Exception as e:
        logger.warning(f"Firecrawl request failed for {url}: {e}")
        return None


async def _try_realtor(address: str, api_key: str) -> dict | None:
    """
    Find the Realtor.com listing page via their public suggest API,
    then extract structured data with Firecrawl.
    """
    parsed = _parse_address(address)
    city, state, street = parsed["city"], parsed["state"], parsed["street"]
    if not city or not state:
        return None

    # Step 1: Realtor.com address autocomplete (no auth required)
    listing_url = None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://www.realtor.com/api/v1/hulk_main_srp/call",
                params={
                    "client_id": "rdc-x",
                    "schema": "vesta",
                    "q": address,
                    "type": "address",
                    "limit": 3,
                },
                headers={
                    "Origin": "https://www.realtor.com",
                    "Referer": "https://www.realtor.com/",
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    "Accept": "application/json",
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                results = (
                    (data.get("data") or {})
                    .get("home_search", {})
                    .get("results", []) or []
                )
                if results:
                    slug = (results[0].get("property_id") or "")
                    permalink = results[0].get("permalink") or ""
                    if permalink:
                        listing_url = f"https://www.realtor.com/realestateandhomes-detail/{permalink}"
    except Exception as e:
        logger.debug(f"Realtor.com autocomplete failed: {e}")

    # Fallback: use search URL (Firecrawl extracts first listing from results page)
    if not listing_url:
        city_state = f"{city.replace(' ', '-')}_{state}"
        keywords = quote_plus(street)
        listing_url = (
            f"https://www.realtor.com/realestateandhomes-search"
            f"/{city_state}?keywords={keywords}"
        )

    prompt = (
        f"Extract the property listing details for '{address}' from this Realtor.com page. "
        "Find the price, bedrooms, bathrooms, square footage, year built, lot size, "
        "property type, HOA fees, annual taxes, days on market, garage spaces, "
        "heating/cooling, listing status, and property description."
    )
    extracted = await _firecrawl_scrape(listing_url, prompt, api_key)
    if extracted:
        extracted["listing_url"] = listing_url
        extracted["source"] = "Realtor.com"
    return extracted


async def _try_zillow(address: str, api_key: str) -> dict | None:
    """Find the Zillow listing URL via autocomplete, then extract with Firecrawl."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://www.zillowstatic.com/autocomplete/v3/suggestions",
                params={"q": address, "abKey": "", "clientId": "homepage-render"},
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
                    )
                },
            )
            resp.raise_for_status()
            suggestions = resp.json().get("results", [])
            if not suggestions:
                return None

            home = next(
                (s for s in suggestions if s.get("resultType") == "Property"),
                suggestions[0],
            )
            detail_url = (
                home.get("metaData", {}).get("detailUrl") or home.get("url") or ""
            )
            if not detail_url:
                return None

            listing_url = (
                f"https://www.zillow.com{detail_url}"
                if detail_url.startswith("/")
                else detail_url
            )
    except Exception as e:
        logger.debug(f"Zillow autocomplete failed: {e}")
        return None

    prompt = (
        f"Extract the property listing details for '{address}' from this Zillow page. "
        "Find the price, bedrooms, bathrooms, square footage, year built, lot size, "
        "property type, monthly HOA fee, annual property tax, days on market, "
        "garage spaces, heating/cooling type, listing status, and the property description."
    )
    extracted = await _firecrawl_scrape(listing_url, prompt, api_key)
    if extracted:
        extracted["listing_url"] = listing_url
        extracted["source"] = "Zillow"
    return extracted


async def _try_redfin(address: str, api_key: str) -> dict | None:
    """Find the Redfin listing URL via autocomplete, then extract with Firecrawl."""
    parsed = _parse_address(address)
    city, state = parsed["city"], parsed["state"]

    listing_url = None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://www.redfin.com/stingray/do/query-location-autocomplete",
                params={"al": 1, "location": address, "start": 0, "count": 5, "v": 2},
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
                    ),
                    "Accept": "application/json, text/plain, */*",
                    "Referer": "https://www.redfin.com/",
                },
            )
            raw = resp.text.lstrip("{}&&").strip()
            import json
            data = json.loads(raw)
            for section in data.get("payload", {}).get("sections", []):
                for row in section.get("rows", []):
                    if str(row.get("type")) == "1":
                        url = row.get("url", "")
                        listing_url = (
                            f"https://www.redfin.com{url}"
                            if url.startswith("/")
                            else url
                        )
                        break
                if listing_url:
                    break
    except Exception as e:
        logger.debug(f"Redfin autocomplete failed: {e}")

    if not listing_url and city and state:
        city_slug = city.lower().replace(" ", "-")
        listing_url = f"https://www.redfin.com/{state}/{city_slug}/filter/property-type=house"

    if not listing_url:
        return None

    prompt = (
        f"Extract the property listing details for '{address}' from this Redfin page. "
        "Find the price, bedrooms, bathrooms, square footage, year built, lot size, "
        "property type, HOA fee per month, annual property tax, days on market, "
        "garage spaces, heating/cooling, listing status, and property description."
    )
    extracted = await _firecrawl_scrape(listing_url, prompt, api_key)
    if extracted:
        extracted["listing_url"] = listing_url
        extracted["source"] = "Redfin"
    return extracted


async def get_listing_firecrawl(address: str) -> dict | None:
    """
    Try Realtor.com → Zillow → Redfin using Firecrawl for AI-based extraction.
    Returns None if FIRECRAWL_API_KEY is not configured.
    Returns a dict with property data or {"error": ...} on full failure.
    """
    from backend.config import get_settings

    api_key = getattr(get_settings(), "firecrawl_api_key", "") or ""
    if not api_key:
        return None  # caller falls back to direct scraping

    for attempt in [_try_realtor, _try_zillow, _try_redfin]:
        try:
            result = await attempt(address, api_key)
            if result and not result.get("error"):
                return result
        except Exception as e:
            logger.warning(f"Firecrawl attempt {attempt.__name__} raised: {e}")

    return {"error": "No listing data found via Firecrawl across Realtor.com, Zillow, Redfin"}
