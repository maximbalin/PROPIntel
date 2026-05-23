import json
import logging
import re
import asyncio
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
    Fetch property listing data from multiple sources in parallel.
    Returns the most complete result found, or {"error": ...} on full failure.
    """

    async def _safe(coro, name: str):
        try:
            r = await coro
            if r and not r.get("error"):
                r.setdefault("source", name)
                return r
        except Exception as e:
            logger.debug(f"{name} fetch failed: {e}")
        return None

    # ── Try Firecrawl + direct scrapers simultaneously ──────────
    firecrawl_coro = _safe(_firecrawl(address), "Realtor.com/Zillow/Redfin (Firecrawl)")
    redfin_coro    = _safe(_fetch_redfin(address), "Redfin")
    zillow_coro    = _safe(_fetch_zillow(address), "Zillow")

    results = await asyncio.gather(firecrawl_coro, redfin_coro, zillow_coro,
                                   return_exceptions=True)

    valid = [r for r in results if isinstance(r, dict) and r and not r.get("error")]
    if not valid:
        return {"error": "Property listing not found on Realtor.com, Zillow, or Redfin"}

    # Pick result with the most populated fields
    def _richness(r):
        skip = {"source", "listing_url", "photos", "error"}
        return sum(1 for k, v in r.items() if k not in skip and v is not None)

    return max(valid, key=_richness)


# ── Firecrawl (AI-powered extraction) ─────────────────────────

async def _firecrawl(address: str) -> dict | None:
    try:
        from backend.data.listing_firecrawl import get_listing_firecrawl
        return await get_listing_firecrawl(address)
    except Exception as e:
        logger.debug(f"Firecrawl import/call failed: {e}")
        return None


# ── Redfin direct scraper ──────────────────────────────────────

async def _fetch_redfin(address: str) -> dict | None:
    async with httpx.AsyncClient(timeout=20.0, headers=_REDFIN_HEADERS) as client:
        # Step 1: resolve address → property ID
        ac = await client.get(
            "https://www.redfin.com/stingray/do/query-location-autocomplete",
            params={"al": 1, "location": address, "start": 0, "count": 5, "v": 2},
        )
        ac.raise_for_status()
        data = json.loads(ac.text.lstrip("{}&&").strip())

        prop_id = url_path = None
        for section in data.get("payload", {}).get("sections", []):
            for row in section.get("rows", []):
                if str(row.get("type")) == "1":
                    prop_id  = (row.get("id") or {}).get("tableId")
                    url_path = row.get("url", "")
                    break
            if prop_id:
                break

        if not prop_id:
            return None

        # Step 2: fetch above + below the fold in parallel
        above_r, below_r = await asyncio.gather(
            client.get(
                "https://www.redfin.com/stingray/api/home/details/aboveTheFold",
                params={"propertyId": prop_id, "accessLevel": 1},
            ),
            client.get(
                "https://www.redfin.com/stingray/api/home/details/belowTheFold",
                params={"propertyId": prop_id, "accessLevel": 1},
            ),
            return_exceptions=True,
        )

        above = json.loads(above_r.text.lstrip("{}&&").strip()) if not isinstance(above_r, Exception) else {}
        below = json.loads(below_r.text.lstrip("{}&&").strip()) if not isinstance(below_r, Exception) else {}

        return _extract_redfin(above, below, url_path or "")


def _extract_redfin(above: dict, below: dict, url_path: str) -> dict | None:
    payload = above.get("payload") or {}
    main    = payload.get("mainHouseInfo") or {}

    # ── core fields ──────────────────────────────────────────
    price = (main.get("priceInfo") or {}).get("amount")

    beds = baths = sqft = year = None
    for fact in (main.get("keyFacts") or []):
        label = (fact.get("keyFact") or "").lower()
        val   = re.sub(r"[,\s]", "", fact.get("keyFactValue") or "")
        if "bed" in label:
            beds  = _int(val)
        elif "bath" in label:
            baths = _float(val)
        elif ("sq" in label and "ft" in label) or "sqft" in label:
            sqft  = _int(re.sub(r"\D", "", val))
        elif "year" in label or "built" in label:
            year  = _int(re.sub(r"\D", "", val))

    if not any([price, beds, sqft]):
        return None

    listing_url = (
        f"https://www.redfin.com{url_path}" if url_path.startswith("/") else url_path
    )

    result: dict = {
        "price": price,
        "beds": beds,
        "baths": baths,
        "sqft": sqft,
        "year_built": year,
        "listing_url": listing_url,
        "photos": [],
    }

    # ── extended fields ──────────────────────────────────────
    # Property type
    pt = main.get("propertyType") or main.get("propertyTypeName")
    if pt:
        result["property_type"] = str(pt).replace("_", " ").title()

    # Listing status
    status_obj = main.get("listingDisplayStatus") or main.get("status") or {}
    if isinstance(status_obj, dict):
        s = status_obj.get("displayValue") or status_obj.get("value")
        if s:
            result["status"] = str(s)

    # Days on market
    dom = main.get("daysOnMarket")
    if dom is not None:
        result["days_on_market"] = _int(str(dom))

    # HOA — try several paths
    for hoa_path in [main.get("hoa"), main.get("hoaFee"), payload.get("hoaFee")]:
        if isinstance(hoa_path, dict):
            hoa_amt = hoa_path.get("amount") or hoa_path.get("fee") or hoa_path.get("monthlyFee")
            if hoa_amt is not None:
                result["hoa_fee_monthly"] = _float(str(hoa_amt))
                break
        elif isinstance(hoa_path, (int, float)):
            result["hoa_fee_monthly"] = float(hoa_path)
            break

    # Annual tax
    for tax_path in [main.get("taxInfo"), main.get("annualTax"), payload.get("taxInfo")]:
        if isinstance(tax_path, dict):
            t = tax_path.get("taxPaid") or tax_path.get("amount") or tax_path.get("annualTax")
            if t is not None:
                result["tax_annual"] = _float(str(t))
                break
        elif isinstance(tax_path, (int, float)):
            result["tax_annual"] = float(tax_path)
            break

    # Lot size
    for lot_path in [main.get("lotSize"), payload.get("lotSize")]:
        if isinstance(lot_path, dict):
            lv   = lot_path.get("value") or lot_path.get("amount")
            unit = (lot_path.get("unit") or lot_path.get("unitType") or "").lower()
            if lv:
                lv_f = _float(str(re.sub(r"[,\s]", "", str(lv))))
                if lv_f:
                    if "acre" in unit:
                        result["lot_size_sqft"] = int(lv_f * 43560)
                    else:
                        result["lot_size_sqft"] = int(lv_f)
                    break

    # Description / remarks
    remarks = main.get("publicRemarksInfo") or payload.get("publicRemarksInfo") or {}
    if isinstance(remarks, dict):
        desc = remarks.get("remarks") or remarks.get("text") or remarks.get("agentDescription")
        if desc:
            result["description"] = str(desc)[:800]

    # Garage / parking
    parking = main.get("parkingInfo") or payload.get("parkingInfo") or {}
    if isinstance(parking, dict):
        spaces = parking.get("spaces") or parking.get("parkingSpaces")
        if spaces is not None:
            result["garage_spaces"] = _int(str(spaces))

    # Heating / cooling
    for section in (below.get("payload") or {}).get("amenitiesInfo", {}).get("superGroups", []):
        for group in section.get("amenityGroups", []):
            title = (group.get("groupTitle") or "").lower()
            if "heat" in title or "cool" in title or "hvac" in title:
                items = [e.get("amenityName", "") for e in group.get("amenityEntries", [])]
                if items:
                    result["heating_cooling"] = ", ".join(items[:3])
                    break

    # Photos from below-the-fold
    media = (below.get("payload") or {}).get("mediaBrowserInfo") or {}
    for photo in (media.get("photos") or [])[:4]:
        url = photo.get("url") or photo.get("photoUrl") or photo.get("photoSmallUrl")
        if url:
            result["photos"].append(url)

    return result


# ── Zillow direct scraper ──────────────────────────────────────

async def _fetch_zillow(address: str) -> dict | None:
    async with httpx.AsyncClient(
        timeout=20.0, follow_redirects=True, headers=_ZILLOW_HEADERS
    ) as client:
        ac = await client.get(
            "https://www.zillowstatic.com/autocomplete/v3/suggestions",
            params={"q": address, "abKey": "", "clientId": "homepage-render"},
        )
        ac.raise_for_status()
        suggestions = ac.json().get("results", [])
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
            page.text, re.DOTALL,
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


def _extract_zillow(prop: dict, listing_url: str) -> dict | None:
    price = prop.get("price") or prop.get("zestimate")
    beds  = prop.get("bedrooms")
    baths = prop.get("bathrooms")
    sqft  = prop.get("livingArea")
    year  = prop.get("yearBuilt")

    if not any([price, beds, sqft]):
        return None

    result: dict = {
        "price": price,
        "beds": beds,
        "baths": baths,
        "sqft": sqft,
        "year_built": year,
        "listing_url": listing_url,
        "photos": [],
    }

    # Extended fields
    pt = prop.get("propertyTypeDimension") or prop.get("homeType")
    if pt:
        result["property_type"] = str(pt).replace("_", " ").title()

    status = prop.get("homeStatus") or prop.get("listingStatus")
    if status:
        result["status"] = str(status).replace("_", " ").title()

    hoa = prop.get("monthlyHoaFee")
    if hoa is not None:
        result["hoa_fee_monthly"] = _float(str(hoa))

    tax = prop.get("taxAnnualAmount") or prop.get("propertyTaxRate")
    if tax is not None:
        result["tax_annual"] = _float(str(tax))

    lot_val  = prop.get("lotAreaValue")
    lot_unit = (prop.get("lotAreaUnits") or prop.get("lotAreaUnit") or "").lower()
    if lot_val is not None:
        lv = _float(str(lot_val))
        if lv:
            result["lot_size_sqft"] = int(lv * 43560) if "acre" in lot_unit else int(lv)

    dom = prop.get("daysOnZillow") or prop.get("daysOnMarket")
    if dom is not None:
        result["days_on_market"] = _int(str(dom))

    garage = prop.get("garageParkingCapacity") or prop.get("garageParkingSpaces")
    if garage is not None:
        result["garage_spaces"] = _int(str(garage))

    desc = prop.get("description")
    if desc:
        result["description"] = str(desc)[:800]

    # Heating / cooling from atAGlanceFacts
    for fact in (prop.get("atAGlanceFacts") or []):
        label = (fact.get("factLabel") or "").lower()
        if any(kw in label for kw in ("heat", "cool", "hvac", "forced")):
            result["heating_cooling"] = fact.get("factValue", "")
            break

    # Photos
    for img in (prop.get("images") or {}).get("responsivePhotos", [])[:4]:
        url = (
            ((img.get("mixedSources") or {}).get("jpeg") or [{}])[0].get("url")
            or img.get("url")
        )
        if url:
            result["photos"].append(url)

    return result


# ── helpers ────────────────────────────────────────────────────

def _int(s: str) -> int | None:
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return None


def _float(s: str) -> float | None:
    try:
        return float(s)
    except (ValueError, TypeError):
        return None
