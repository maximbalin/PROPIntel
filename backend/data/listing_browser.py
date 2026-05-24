"""
Browser-impersonation scrapers for Zillow and Redfin.

Uses curl_cffi to send Chrome's exact TLS fingerprint + HTTP/2 settings,
plus a multi-step session warmup that mimics a real user landing on the
homepage and navigating to a property page.

Falls back gracefully to None if curl_cffi is not installed.
"""
import asyncio
import json
import logging
import random
import re

logger = logging.getLogger(__name__)

# ── Chrome 124 headers (exact order Chrome sends them) ───────
_NAV_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "DNT": "1",
    "Cache-Control": "max-age=0",
}

_XHR_HEADERS = {
    "User-Agent": _NAV_HEADERS["User-Agent"],
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "sec-ch-ua": _NAV_HEADERS["sec-ch-ua"],
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "DNT": "1",
}


def _jitter(lo: float = 1.2, hi: float = 3.5) -> float:
    """Random human-like delay in seconds."""
    return random.uniform(lo, hi)


# ═══════════════════════════════════════════════════════
#  Zillow — map search API (GetSearchPageState)
# ═══════════════════════════════════════════════════════

async def fetch_zillow_browser(address: str) -> dict | None:
    """
    Uses curl_cffi Chrome impersonation + session warmup to call
    Zillow's map-search JSON API (GetSearchPageState.htm).

    This endpoint is what powers Zillow's map view and returns full
    listing JSON. It does NOT go through Datadome (no JS challenge).
    We resolve lat/lon + zpid from autocomplete, then query a tight
    bounding box to get the matching property's data.
    """
    try:
        from curl_cffi.requests import AsyncSession
    except ImportError:
        logger.debug("curl_cffi not installed — skipping browser Zillow scraper")
        return None

    try:
        async with AsyncSession(impersonate="chrome124") as s:
            # ── Step 1: Homepage warmup ───────────────────────────────────────
            logger.debug("Zillow: warming up homepage session")
            await s.get("https://www.zillow.com/", headers=_NAV_HEADERS)
            await asyncio.sleep(_jitter(1.5, 3.0))

            # ── Step 2: Autocomplete → zpid + lat/lon ─────────────────────────
            ac_headers = {
                **_XHR_HEADERS,
                "Referer": "https://www.zillow.com/",
                "Sec-Fetch-Site": "same-site",
                "Origin": "https://www.zillow.com",
            }
            zpid = lat = lon = detail_url = None
            for ac_url in [
                "https://www.zillowstatic.com/autocomplete/v3/suggestions",
                "https://www.zillow.com/autocomplete/v3/suggestions",
            ]:
                await asyncio.sleep(_jitter(0.3, 0.9))
                try:
                    ac = await s.get(
                        ac_url,
                        params={"q": address, "clientId": "homepage-render"},
                        headers=ac_headers,
                    )
                    if ac.status_code == 200:
                        for r in ac.json().get("results", []):
                            meta = r.get("metaData", {})
                            if meta.get("zpid"):
                                zpid       = meta["zpid"]
                                lat        = meta.get("lat")
                                lon        = meta.get("lon") or meta.get("lng")
                                detail_url = meta.get("detailUrl")
                                break
                    if zpid:
                        break
                except Exception as e:
                    logger.debug(f"Zillow autocomplete error: {e}")

            if not zpid:
                logger.debug("Zillow: no zpid from autocomplete")
                return None

            logger.debug(f"Zillow: zpid={zpid} lat={lat} lon={lon}")

            # ── Step 3: Try map-search JSON API (no Datadome) ─────────────────
            if lat and lon:
                result = await _zillow_map_search(s, zpid, lat, lon)
                if result:
                    return result

            # ── Step 4: Fallback — try property detail page with session cookies ─
            slug = re.sub(r"[,\s]+", "-", address.strip()).strip("-")
            prop_url = (
                f"https://www.zillow.com{detail_url}" if detail_url and detail_url.startswith("/")
                else f"https://www.zillow.com/homedetails/{slug}/{zpid}_zpid/"
            )
            await asyncio.sleep(_jitter(2.0, 4.0))
            page = await s.get(
                prop_url,
                headers={**_NAV_HEADERS, "Referer": "https://www.zillow.com/",
                          "Sec-Fetch-Site": "same-origin"},
                allow_redirects=True,
            )
            if page.status_code == 200 and "__NEXT_DATA__" in page.text:
                m = re.search(
                    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                    page.text, re.DOTALL,
                )
                if m:
                    page_data = json.loads(m.group(1))
                    gdp = page_data.get("props", {}).get("pageProps", {}).get("gdpClientCache")
                    if gdp:
                        for value in gdp.values():
                            if isinstance(value, dict) and "property" in value:
                                from backend.data.listing import _extract_zillow
                                result = _extract_zillow(value["property"], str(page.url))
                                if result:
                                    result["source"] = "Zillow"
                                return result

            logger.debug(f"Zillow fallback page: HTTP {page.status_code}, blocked="
                         f"{any(w in page.text.lower() for w in ['datadome','captcha','robot'])}")

    except Exception as e:
        logger.debug(f"Zillow browser scraper failed: {e}")

    return None


async def _zillow_map_search(s, zpid: int, lat: float, lon: float) -> dict | None:
    """
    Query Zillow's map-search JSON API with a tight bounding box around the
    property. This endpoint returns full listing data as JSON and is NOT
    protected by Datadome (it's the same API Zillow's map view calls).
    """
    import urllib.parse

    # Tight bounding box: ~500m around the property
    delta_lat = 0.004
    delta_lon = 0.006
    search_state = {
        "pagination": {},
        "mapBounds": {
            "west":  lon - delta_lon,
            "east":  lon + delta_lon,
            "south": lat - delta_lat,
            "north": lat + delta_lat,
        },
        "filterState": {
            "sortSelection": {"value": "globalrelevanceex"},
            "isAllHomes":    {"value": True},
        },
        "isMapVisible":  True,
        "isListVisible": True,
    }
    wants = {"cat1": ["listResults", "mapResults"], "cat2": ["total"]}

    map_headers = {
        **_XHR_HEADERS,
        "Accept":         "application/json",
        "Referer":        "https://www.zillow.com/homes/for_sale/",
        "Sec-Fetch-Site": "same-origin",
    }

    for request_id in [2, 3, 4]:
        await asyncio.sleep(_jitter(0.8, 2.0))
        try:
            resp = await s.get(
                "https://www.zillow.com/search/GetSearchPageState.htm",
                params={
                    "searchQueryState": json.dumps(search_state, separators=(",", ":")),
                    "wants":            json.dumps(wants, separators=(",", ":")),
                    "requestId":        request_id,
                },
                headers=map_headers,
            )
            logger.debug(f"Zillow map API: HTTP {resp.status_code} (requestId={request_id})")
            if resp.status_code != 200:
                continue

            data = resp.json()
            list_results = (
                data.get("cat1", {})
                    .get("searchResults", {})
                    .get("listResults", [])
            )
            if not list_results:
                # Try mapResults too
                list_results = (
                    data.get("cat1", {})
                        .get("searchResults", {})
                        .get("mapResults", [])
                )

            logger.debug(f"Zillow map API: {len(list_results)} results in bounding box")

            # Find our property by zpid
            target = None
            for prop in list_results:
                if str(prop.get("zpid", "")) == str(zpid):
                    target = prop
                    break

            # Fall back to first result if only one in tight box
            if not target and len(list_results) == 1:
                target = list_results[0]

            if not target:
                logger.debug(f"Zillow map API: zpid {zpid} not in {len(list_results)} results")
                continue

            return _parse_map_result(target)

        except Exception as e:
            logger.debug(f"Zillow map API error (requestId={request_id}): {e}")

    return None


def _parse_map_result(prop: dict) -> dict | None:
    """Extract listing fields from a Zillow map/list result object."""
    price = prop.get("price") or prop.get("unformattedPrice")
    if isinstance(price, str):
        price = int(re.sub(r"[^\d]", "", price)) if re.sub(r"[^\d]", "", price) else None

    beds  = prop.get("beds")
    baths = prop.get("baths")
    sqft  = prop.get("area") or prop.get("livingArea")
    year  = prop.get("hdpData", {}).get("homeInfo", {}).get("yearBuilt") if prop.get("hdpData") else None

    # hdpData.homeInfo has richer data
    home_info = (prop.get("hdpData") or {}).get("homeInfo") or {}
    if not year:
        year = home_info.get("yearBuilt")
    if not sqft:
        sqft = home_info.get("livingArea")
    if not baths:
        baths = home_info.get("bathrooms")

    listing_url = prop.get("detailUrl") or ""
    if listing_url and not listing_url.startswith("http"):
        listing_url = f"https://www.zillow.com{listing_url}"

    status = prop.get("statusText") or prop.get("homeStatus") or home_info.get("homeStatus")
    if status:
        status = status.replace("_", " ").title()

    if not any([price, beds, sqft]):
        return None

    result: dict = {
        "price":       price,
        "beds":        int(beds)   if beds  is not None else None,
        "baths":       float(baths) if baths is not None else None,
        "sqft":        int(sqft)   if sqft  is not None else None,
        "year_built":  int(year)   if year  is not None else None,
        "listing_url": listing_url,
        "status":      status,
        "photos":      [],
        "source":      "Zillow",
    }

    # Grab a photo from the carousel
    for img_key in ("carouselPhotos", "photos"):
        photos = prop.get(img_key) or []
        for p in photos[:4]:
            url = p.get("url") or p.get("src") or (p if isinstance(p, str) else None)
            if url:
                result["photos"].append(url)
        if result["photos"]:
            break

    # lot size, HOA, tax from hdpData
    lot = home_info.get("lotAreaValue")
    lot_unit = (home_info.get("lotAreaUnits") or "").lower()
    if lot:
        result["lot_size_sqft"] = int(float(lot) * 43560) if "acre" in lot_unit else int(float(lot))

    hoa = home_info.get("monthlyHoaFee")
    if hoa is not None:
        result["hoa_fee_monthly"] = float(hoa)

    tax = home_info.get("taxAnnualAmount")
    if tax is not None:
        result["tax_annual"] = float(tax)

    dom = home_info.get("daysOnZillow") or prop.get("daysOnMarket")
    if dom is not None:
        result["days_on_market"] = int(dom)

    pt = home_info.get("homeType") or prop.get("propertyType")
    if pt:
        result["property_type"] = str(pt).replace("_", " ").title()

    return result


# ═══════════════════════════════════════════════════════
#  Redfin
# ═══════════════════════════════════════════════════════

async def fetch_redfin_browser(address: str) -> dict | None:
    try:
        from curl_cffi.requests import AsyncSession
    except ImportError:
        return None

    try:
        async with AsyncSession(impersonate="chrome124") as s:
            # ── Step 1: Homepage warmup ───────────────────────────────────────
            logger.debug("Redfin browser: warming up homepage")
            await s.get("https://www.redfin.com/", headers=_NAV_HEADERS)
            await asyncio.sleep(_jitter(2.0, 4.0))

            # ── Step 2: Try autocomplete API ──────────────────────────────────
            ac_headers = {
                **_XHR_HEADERS,
                "Referer": "https://www.redfin.com/",
                "Origin": "https://www.redfin.com",
                "Sec-Fetch-Site": "same-origin",
            }
            prop_id = url_path = None

            await asyncio.sleep(_jitter(0.8, 2.0))
            try:
                ac = await s.get(
                    "https://www.redfin.com/stingray/do/query-location-autocomplete",
                    params={"al": 1, "location": address, "start": 0, "count": 5, "v": 2},
                    headers=ac_headers,
                )
                if ac.status_code == 200:
                    data = json.loads(ac.text.lstrip("{}&&").strip())
                    for section in data.get("payload", {}).get("sections", []):
                        for row in section.get("rows", []):
                            if str(row.get("type")) == "1":
                                prop_id = (row.get("id") or {}).get("tableId")
                                url_path = row.get("url", "")
                                break
                        if prop_id:
                            break
                else:
                    logger.debug(f"Redfin browser: autocomplete HTTP {ac.status_code}")
            except Exception as e:
                logger.debug(f"Redfin browser: autocomplete error: {e}")

            if not prop_id:
                return None

            # ── Step 3: Browse to property page (establish Referer chain) ─────
            if url_path:
                prop_page_url = (f"https://www.redfin.com{url_path}"
                                 if url_path.startswith("/") else url_path)
                await asyncio.sleep(_jitter(1.5, 3.0))
                try:
                    await s.get(
                        prop_page_url,
                        headers={**_NAV_HEADERS, "Referer": "https://www.redfin.com/",
                                 "Sec-Fetch-Site": "same-origin"},
                        allow_redirects=True,
                    )
                    await asyncio.sleep(_jitter(1.0, 2.5))
                except Exception:
                    pass

            # ── Step 4: Fetch above + below fold in parallel ──────────────────
            api_headers = {
                **_XHR_HEADERS,
                "Referer": f"https://www.redfin.com{url_path or '/'}",
                "Origin": "https://www.redfin.com",
                "Sec-Fetch-Site": "same-origin",
            }
            above_r, below_r = await asyncio.gather(
                s.get(
                    "https://www.redfin.com/stingray/api/home/details/aboveTheFold",
                    params={"propertyId": prop_id, "accessLevel": 1},
                    headers=api_headers,
                ),
                s.get(
                    "https://www.redfin.com/stingray/api/home/details/belowTheFold",
                    params={"propertyId": prop_id, "accessLevel": 1},
                    headers=api_headers,
                ),
                return_exceptions=True,
            )

            def _parse(r):
                if isinstance(r, Exception) or r.status_code != 200:
                    return {}
                try:
                    return json.loads(r.text.lstrip("{}&&").strip())
                except Exception:
                    return {}

            above = _parse(above_r)
            below = _parse(below_r)

            if not above and not below:
                logger.debug("Redfin browser: empty above/below fold responses")
                return None

            from backend.data.listing import _extract_redfin
            result = _extract_redfin(above, below, url_path or "")
            if result:
                result["source"] = "Redfin"
            return result

    except Exception as e:
        logger.debug(f"Redfin browser scraper failed: {e}")

    return None


# ═══════════════════════════════════════════════════════
#  Zillow — Playwright stealth (executes JS challenges)
# ═══════════════════════════════════════════════════════

async def fetch_zillow_playwright(address: str) -> dict | None:
    """
    Playwright + playwright-stealth: runs real Chromium with JS-detection patches.

    Unlike curl_cffi (which only spoofs TLS), Playwright actually *executes*
    Datadome's JavaScript challenge. The stealth plugin removes webdriver signals,
    fakes navigator.plugins, patches canvas fingerprinting, etc., so the challenge
    is solved and the property page renders normally.

    Falls back gracefully if playwright / playwright-stealth are not installed.
    Run once after install: playwright install chromium
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.debug("playwright not installed — skipping; run: pip install playwright && playwright install chromium")
        return None

    # playwright-stealth v1: stealth_async  v2: Stealth().apply_stealth_async
    stealth_fn = None
    try:
        from playwright_stealth import stealth_async as stealth_fn  # v1
    except (ImportError, AttributeError):
        try:
            from playwright_stealth import Stealth as _Stealth
            stealth_fn = _Stealth().apply_stealth_async  # v2
        except (ImportError, AttributeError):
            logger.debug("playwright-stealth not available — running without stealth patches")

    # Find Chromium: prefer playwright's bundled version, fall back to system
    import shutil
    _system_chromium = (
        shutil.which("chromium-browser")
        or shutil.which("chromium")
        or shutil.which("google-chrome")
        or shutil.which("google-chrome-stable")
    )

    try:
        async with async_playwright() as pw:
            launch_kwargs: dict = {
                "headless": True,
                "args": [
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                ],
            }
            if _system_chromium:
                launch_kwargs["executable_path"] = _system_chromium
                logger.debug(f"Playwright: using system Chromium at {_system_chromium}")
            browser = await pw.chromium.launch(**launch_kwargs)
            try:
                context = await browser.new_context(
                    ignore_https_errors=True,  # snap Chromium has its own cert store
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    viewport={"width": 1920, "height": 1080},
                    locale="en-US",
                    timezone_id="America/New_York",
                )
                page = await context.new_page()

                if stealth_fn:
                    await stealth_fn(page)
                    logger.debug("Playwright: stealth patches applied")

                # ── Step 1: Homepage warmup ───────────────────────────────
                logger.debug("Playwright: loading Zillow homepage")
                await page.goto("https://www.zillow.com/", wait_until="domcontentloaded")
                await asyncio.sleep(_jitter(2.0, 4.0))

                # ── Step 2: Autocomplete → zpid + lat/lon ─────────────────
                zpid = lat = lon = detail_url = None
                try:
                    ac = await page.request.get(
                        "https://www.zillowstatic.com/autocomplete/v3/suggestions",
                        params={"q": address, "clientId": "homepage-render"},
                    )
                    if ac.ok:
                        for r in (await ac.json()).get("results", []):
                            meta = r.get("metaData", {})
                            if meta.get("zpid"):
                                zpid       = meta["zpid"]
                                lat        = meta.get("lat")
                                lon        = meta.get("lon") or meta.get("lng")
                                detail_url = meta.get("detailUrl")
                                break
                except Exception as e:
                    logger.debug(f"Playwright autocomplete error: {e}")

                if not zpid:
                    logger.debug("Playwright: no zpid from autocomplete")
                    return None

                logger.debug(f"Playwright: zpid={zpid} lat={lat} lon={lon}")

                # ── Step 3: Try map search API first (no Datadome) ────────
                if lat and lon:
                    try:
                        search_state = {
                            "pagination": {},
                            "mapBounds": {
                                "west": lon - 0.006, "east": lon + 0.006,
                                "south": lat - 0.004, "north": lat + 0.004,
                            },
                            "filterState": {
                                "sortSelection": {"value": "globalrelevanceex"},
                                "isAllHomes": {"value": True},
                            },
                            "isMapVisible": True, "isListVisible": True,
                        }
                        wants = {"cat1": ["listResults", "mapResults"], "cat2": ["total"]}
                        await asyncio.sleep(_jitter(0.8, 1.5))
                        mr = await page.request.get(
                            "https://www.zillow.com/search/GetSearchPageState.htm",
                            params={
                                "searchQueryState": json.dumps(search_state, separators=(",", ":")),
                                "wants": json.dumps(wants, separators=(",", ":")),
                                "requestId": 2,
                            },
                        )
                        logger.debug(f"Playwright map API: HTTP {mr.status}")
                        if mr.ok:
                            data = await mr.json()
                            list_results = (
                                data.get("cat1", {}).get("searchResults", {}).get("listResults", [])
                                or data.get("cat1", {}).get("searchResults", {}).get("mapResults", [])
                            )
                            target = next(
                                (p for p in list_results if str(p.get("zpid", "")) == str(zpid)),
                                list_results[0] if len(list_results) == 1 else None,
                            )
                            if target:
                                result = _parse_map_result(target)
                                if result:
                                    return result
                    except Exception as e:
                        logger.debug(f"Playwright map API error: {e}")

                # ── Step 4: Navigate to property detail page ──────────────
                slug = re.sub(r"[,\s]+", "-", address.strip()).strip("-")
                prop_url = (
                    f"https://www.zillow.com{detail_url}" if detail_url and detail_url.startswith("/")
                    else f"https://www.zillow.com/homedetails/{slug}/{zpid}_zpid/"
                )
                await asyncio.sleep(_jitter(1.5, 3.0))
                logger.debug(f"Playwright: navigating to {prop_url}")
                await page.goto(prop_url, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(_jitter(3.0, 5.0))

                content = await page.content()
                if "__NEXT_DATA__" not in content:
                    is_blocked = any(w in content.lower() for w in
                                     ["captcha", "robot", "recaptcha", "are you human"])
                    logger.debug(f"Playwright: no __NEXT_DATA__ (hard_block={is_blocked}, len={len(content)})")
                    return None

                m = re.search(
                    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                    content, re.DOTALL,
                )
                if not m:
                    return None

                page_data = json.loads(m.group(1))
                gdp = page_data.get("props", {}).get("pageProps", {}).get("gdpClientCache")
                if not gdp:
                    return None

                for value in gdp.values():
                    if isinstance(value, dict) and "property" in value:
                        from backend.data.listing import _extract_zillow
                        result = _extract_zillow(value["property"], prop_url)
                        if result:
                            result["source"] = "Zillow"
                        return result

            finally:
                await browser.close()

    except Exception as e:
        logger.debug(f"Playwright Zillow scraper failed: {e}")

    return None
