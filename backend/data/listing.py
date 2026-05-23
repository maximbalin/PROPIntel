import json
import logging
import re
from urllib.parse import quote_plus

import httpx

logger = logging.getLogger(__name__)

_ZILLOW_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
}

_REDFIN_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.redfin.com/",
}


async def get_listing_data(address: str, lat: float = None, lon: float = None) -> dict:
    """
    Try Zillow then Redfin for property listing data.
    Returns dict with price/beds/baths/sqft/year_built/listing_url/photos/source
    or {"error": ...} on full failure.
    """
    for fetcher, name in [(_fetch_zillow, "Zillow"), (_fetch_redfin, "Redfin")]:
        try:
            result = await fetcher(address)
            if result and not result.get("error"):
                result["source"] = name
                return result
        except Exception as e:
            logger.warning(f"{name} listing fetch failed: {e}")

    return {"error": "Property listing not found", "sources_tried": ["Zillow", "Redfin"]}


async def _fetch_zillow(address: str) -> dict | None:
    """Resolve address via Zillow autocomplete, then scrape __NEXT_DATA__ from the property page."""
    async with httpx.AsyncClient(
        timeout=20.0, follow_redirects=True, headers=_ZILLOW_HEADERS
    ) as client:
        autocomplete = await client.get(
            "https://www.zillowstatic.com/autocomplete/v3/suggestions",
            params={"q": address, "abKey": "", "clientId": "homepage-render"},
        )
        autocomplete.raise_for_status()
        suggestions = autocomplete.json().get("results", [])
        if not suggestions:
            return None

        home = next(
            (s for s in suggestions if s.get("resultType") == "Property"),
            suggestions[0],
        )
        detail_url = home.get("metaData", {}).get("detailUrl") or home.get("url") or ""
        if not detail_url:
            return None

        full_url = (
            f"https://www.zillow.com{detail_url}"
            if detail_url.startswith("/")
            else detail_url
        )
        page = await client.get(full_url)
        page.raise_for_status()

        m = re.search(
            r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
            page.text,
            re.DOTALL,
        )
        if not m:
            return None

        page_data = json.loads(m.group(1))
        gdp = (
            page_data.get("props", {})
            .get("pageProps", {})
            .get("gdpClientCache")
        )
        if not gdp:
            return None

        for value in gdp.values():
            if isinstance(value, dict) and "property" in value:
                return _extract_zillow(value["property"], full_url)

    return None


def _extract_zillow(prop: dict, listing_url: str) -> dict:
    price = prop.get("price") or prop.get("zestimate")
    beds  = prop.get("bedrooms")
    baths = prop.get("bathrooms")
    sqft  = prop.get("livingArea")
    year  = prop.get("yearBuilt")

    photos = []
    for img in (prop.get("images") or {}).get("responsivePhotos", [])[:4]:
        url = (
            ((img.get("mixedSources") or {}).get("jpeg") or [{}])[0].get("url")
            or img.get("url")
        )
        if url:
            photos.append(url)

    if not any([price, beds, sqft]):
        return {"error": "Insufficient data from Zillow"}

    return {
        "price": price,
        "beds": beds,
        "baths": baths,
        "sqft": sqft,
        "year_built": year,
        "listing_url": listing_url,
        "photos": photos,
    }


async def _fetch_redfin(address: str) -> dict | None:
    """Resolve address via Redfin autocomplete, then fetch property details."""
    async with httpx.AsyncClient(timeout=15.0, headers=_REDFIN_HEADERS) as client:
        autocomplete = await client.get(
            "https://www.redfin.com/stingray/do/query-location-autocomplete",
            params={"al": 1, "location": address, "start": 0, "count": 5, "v": 2},
        )
        autocomplete.raise_for_status()

        raw = autocomplete.text.lstrip("{}&&").strip()
        data = json.loads(raw)
        sections = data.get("payload", {}).get("sections", [])
        if not sections:
            return None

        for section in sections:
            for row in section.get("rows", []):
                if str(row.get("type")) == "1":
                    prop_id = (row.get("id") or {}).get("tableId")
                    url     = row.get("url", "")
                    if prop_id:
                        return await _redfin_details(client, prop_id, url)

    return None


async def _redfin_details(client: httpx.AsyncClient, property_id: int, url: str) -> dict | None:
    resp = await client.get(
        "https://www.redfin.com/stingray/api/home/details/aboveTheFold",
        params={"propertyId": property_id, "accessLevel": 1},
    )
    resp.raise_for_status()

    raw  = resp.text.lstrip("{}&&").strip()
    data = json.loads(raw)
    payload   = data.get("payload") or {}
    main_info = payload.get("mainHouseInfo") or {}
    price_info = main_info.get("priceInfo") or {}
    key_facts  = main_info.get("keyFacts") or []

    beds = baths = sqft = year = None
    for fact in key_facts:
        label = (fact.get("keyFact") or "").lower()
        val   = (fact.get("keyFactValue") or "").replace(",", "")
        if "bed" in label:
            try: beds = int(re.sub(r"\D", "", val))
            except: pass
        elif "bath" in label:
            try: baths = float(re.sub(r"[^\d.]", "", val))
            except: pass
        elif "sq" in label and "ft" in label:
            try: sqft = int(re.sub(r"\D", "", val))
            except: pass
        elif "year" in label or "built" in label:
            try: year = int(re.sub(r"\D", "", val))
            except: pass

    price = price_info.get("amount")
    if not any([price, beds, sqft]):
        return None

    listing_url = f"https://www.redfin.com{url}" if url.startswith("/") else url
    return {
        "price": price,
        "beds": beds,
        "baths": baths,
        "sqft": sqft,
        "year_built": year,
        "listing_url": listing_url,
        "photos": [],
    }
