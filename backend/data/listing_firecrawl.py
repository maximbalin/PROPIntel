"""
Firecrawl-powered property listing scraper.

Two modes:
  Local (self-hosted):  set FIRECRAWL_API_URL=http://localhost:3002
                        No API key required. Run: scripts/setup-firecrawl.sh
  Cloud:                set FIRECRAWL_API_KEY=fc-...
                        Rate-limited by plan (500 pages/mo free).

Local takes priority. Falls back gracefully when neither is configured.
"""
import logging
import re
from urllib.parse import quote_plus

import httpx

logger = logging.getLogger(__name__)

FIRECRAWL_CLOUD_BASE = "https://api.firecrawl.dev"

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


def _get_endpoint(settings) -> tuple[str, str]:
    """
    Returns (scrape_endpoint_url, api_key).
    Local self-hosted takes priority; cloud API key is fallback.
    Returns ("", "") when neither is configured.
    """
    local_url = (getattr(settings, "firecrawl_api_url", "") or "").rstrip("/")
    api_key   = (getattr(settings, "firecrawl_api_key", "") or "")
    if local_url:
        return f"{local_url}/v1/scrape", ""   # no auth needed for self-hosted
    if api_key:
        return f"{FIRECRAWL_CLOUD_BASE}/v1/scrape", api_key
    return "", ""


async def _firecrawl_scrape(
    url: str, prompt: str, endpoint: str, api_key: str, timeout: float = 45.0
) -> dict | None:
    """POST to Firecrawl /v1/scrape with AI extraction. Tries newer extract format, falls back to json."""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    # Try newer extract format first (Firecrawl v0.5+), then fall back to jsonOptions
    payloads = [
        {
            "url": url,
            "formats": ["extract"],
            "extract": {
                "schema": PROPERTY_SCHEMA,
                "prompt": prompt,
            },
            "waitFor": 3000,
            "onlyMainContent": True,
        },
        {
            "url": url,
            "formats": ["json"],
            "jsonOptions": {
                "schema": PROPERTY_SCHEMA,
                "prompt": prompt,
            },
            "waitFor": 3000,
            "onlyMainContent": True,
        },
    ]

    async with httpx.AsyncClient(timeout=timeout) as client:
        for payload in payloads:
            try:
                resp = await client.post(endpoint, headers=headers, json=payload)
                resp.raise_for_status()
                body = resp.json()
                if not body.get("success"):
                    logger.debug(f"Firecrawl non-success for {url}: {body.get('error')}")
                    continue
                # extract format stores result under data.extract; json format under data.json
                data_obj = body.get("data") or {}
                extracted = data_obj.get("extract") or data_obj.get("json") or {}
                if extracted.get("price") or extracted.get("beds") or extracted.get("sqft"):
                    return extracted
                logger.debug(f"Firecrawl empty extraction ({payload['formats'][0]}) for {url}")
            except httpx.HTTPStatusError as e:
                logger.debug(f"Firecrawl HTTP {e.response.status_code} ({payload['formats'][0]}) for {url}")
                if e.response.status_code not in (400, 422):
                    break  # non-format error, no point retrying with other format
            except Exception as e:
                logger.warning(f"Firecrawl request failed for {url}: {e}")
                break

    return None


async def _try_homes_com(address: str, endpoint: str, api_key: str) -> dict | None:
    """
    Scrape Homes.com (CoStar-owned) — less bot-protected than Zillow/Redfin.
    Construct URL directly from address; no auth autocomplete needed.
    """
    import re as _re
    parsed = _parse_address(address)
    street, city, state = parsed["street"], parsed["city"], parsed["state"]
    if not city or not state:
        return None

    # Homes.com URL format: /property/20-pine-st-natick-ma-01760/
    slug = _re.sub(r"[,\s]+", "-", address.lower().strip()).strip("-")
    listing_url = f"https://www.homes.com/property/{slug}/"

    prompt = (
        f"Extract the property listing details for '{address}' from this Homes.com page. "
        "Find the price, bedrooms, bathrooms, square footage, year built, lot size, "
        "property type, monthly HOA fee, annual property tax, days on market, "
        "garage spaces, heating/cooling type, listing status, and the property description."
    )
    extracted = await _firecrawl_scrape(listing_url, prompt, endpoint, api_key)
    if extracted:
        extracted["listing_url"] = listing_url
        extracted["source"] = "Homes.com"
    return extracted


async def _try_realtor(address: str, endpoint: str, api_key: str) -> dict | None:
    """
    Scrape Realtor.com listing page via Firecrawl.
    Uses autocomplete to find the direct permalink; falls back to search URL.
    """
    parsed = _parse_address(address)
    city, state, street = parsed["city"], parsed["state"], parsed["street"]
    if not city or not state:
        return None

    listing_url = None
    _ua = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    # Try Realtor.com's autocomplete API (several known paths)
    for ac_path, ac_params in [
        (
            "https://www.realtor.com/api/v1/hulk_main_srp/call_main_srp",
            {"client_id": "rdc-x", "schema": "vesta", "q": address, "type": "address", "limit": 3},
        ),
        (
            "https://parser.realestatecourt.com/suggestions/v1",
            {"input": address, "types": "address"},
        ),
    ]:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    ac_path, params=ac_params,
                    headers={"User-Agent": _ua, "Accept": "application/json",
                             "Origin": "https://www.realtor.com",
                             "Referer": "https://www.realtor.com/"},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    results = (
                        (data.get("data") or {}).get("home_search", {}).get("results", [])
                        or data.get("suggestions", [])
                        or []
                    )
                    if results:
                        permalink = (results[0].get("permalink") or
                                     results[0].get("description", {}).get("value", ""))
                        if permalink and "/" in permalink:
                            listing_url = permalink if permalink.startswith("http") else (
                                f"https://www.realtor.com/realestateandhomes-detail/{permalink}"
                            )
                            break
        except Exception as e:
            logger.debug(f"Realtor.com autocomplete attempt failed: {e}")

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
    extracted = await _firecrawl_scrape(listing_url, prompt, endpoint, api_key)
    if extracted:
        extracted["listing_url"] = listing_url
        extracted["source"] = "Realtor.com"
    return extracted


async def _try_zillow(address: str, endpoint: str, api_key: str) -> dict | None:
    """
    Resolve Zillow zpid via autocomplete → use precise homedetails URL → extract with Firecrawl.
    """
    import re as _re
    _ua = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
    )
    slug = _re.sub(r"[,\s]+", "-", address.strip()).strip("-")
    listing_url = f"https://www.zillow.com/homes/{slug}_rb/"  # default

    # Try autocomplete to get zpid → homedetails URL (more specific)
    try:
        async with httpx.AsyncClient(timeout=10.0, headers={"User-Agent": _ua}) as client:
            for ac_url, ac_params in [
                ("https://www.zillowstatic.com/autocomplete/v3/suggestions",
                 {"q": address, "clientId": "homepage-render"}),
                ("https://www.zillow.com/autocomplete/v3/suggestions",
                 {"q": address, "clientId": "homepage-render"}),
            ]:
                try:
                    resp = await client.get(ac_url, params=ac_params)
                    if resp.status_code == 200:
                        for result in resp.json().get("results", []):
                            meta = result.get("metaData", {})
                            zpid = meta.get("zpid")
                            detail_url = meta.get("detailUrl")
                            if zpid:
                                if detail_url:
                                    listing_url = (
                                        f"https://www.zillow.com{detail_url}"
                                        if detail_url.startswith("/") else detail_url
                                    )
                                else:
                                    listing_url = f"https://www.zillow.com/homedetails/{slug}/{zpid}_zpid/"
                                break
                    if listing_url != f"https://www.zillow.com/homes/{slug}_rb/":
                        break
                except Exception:
                    continue
    except Exception as e:
        logger.debug(f"Zillow zpid lookup failed: {e}")

    prompt = (
        f"Extract the property listing details for '{address}' from this Zillow page. "
        "Find the price, bedrooms, bathrooms, square footage, year built, lot size, "
        "property type, monthly HOA fee, annual property tax, days on market, "
        "garage spaces, heating/cooling type, listing status, and the property description."
    )
    extracted = await _firecrawl_scrape(listing_url, prompt, endpoint, api_key)
    if extracted:
        extracted["listing_url"] = listing_url
        extracted["source"] = "Zillow"
    return extracted


async def _try_redfin(address: str, endpoint: str, api_key: str) -> dict | None:
    """Scrape Redfin city search page with Firecrawl (autocomplete too bot-blocked)."""
    parsed = _parse_address(address)
    city, state = parsed["city"], parsed["state"]
    if not city or not state:
        return None

    city_slug = city.lower().replace(" ", "-")
    listing_url = f"https://www.redfin.com/{state}/{city_slug}/filter/property-type=house"

    prompt = (
        f"Extract the property listing details for '{address}' from this Redfin page. "
        "Find the price, bedrooms, bathrooms, square footage, year built, lot size, "
        "property type, HOA fee per month, annual property tax, days on market, "
        "garage spaces, heating/cooling, listing status, and property description."
    )
    extracted = await _firecrawl_scrape(listing_url, prompt, endpoint, api_key)
    if extracted:
        extracted["listing_url"] = listing_url
        extracted["source"] = "Redfin"
    return extracted


async def get_listing_firecrawl(address: str) -> dict | None:
    """
    Try Homes.com → Realtor.com → Zillow → Redfin using Firecrawl for AI-based extraction.

    Priority:
      1. FIRECRAWL_API_URL  — self-hosted (no limit, no API key)
      2. FIRECRAWL_API_KEY  — cloud (rate-limited)
      3. Neither set        — returns None, caller uses direct scrapers

    Returns a dict with property data or {"error": ...} on full failure.
    """
    from backend.config import get_settings

    settings = get_settings()
    endpoint, api_key = _get_endpoint(settings)
    if not endpoint:
        return None  # neither local nor cloud configured — use direct scrapers

    mode = "local" if not api_key else "cloud"
    logger.info(f"Firecrawl listing fetch ({mode}): {endpoint}")

    for attempt in [_try_homes_com, _try_realtor, _try_zillow, _try_redfin]:
        try:
            result = await attempt(address, endpoint, api_key)
            if result and not result.get("error"):
                return result
        except Exception as e:
            logger.warning(f"Firecrawl attempt {attempt.__name__} raised: {e}")

    return {"error": "No listing data found via Firecrawl"}
