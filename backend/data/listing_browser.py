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
#  Zillow
# ═══════════════════════════════════════════════════════

async def fetch_zillow_browser(address: str) -> dict | None:
    try:
        from curl_cffi.requests import AsyncSession
    except ImportError:
        logger.debug("curl_cffi not installed — skipping browser Zillow scraper")
        return None

    slug = re.sub(r"[,\s]+", "-", address.strip()).strip("-")

    try:
        async with AsyncSession(impersonate="chrome124") as s:
            # ── Step 1: Land on homepage (get cookies: zguid, zgsession, etc.) ──
            logger.debug("Zillow browser: warming up homepage")
            await s.get("https://www.zillow.com/", headers=_NAV_HEADERS)
            await asyncio.sleep(_jitter(2.0, 4.5))

            # ── Step 2: Autocomplete to resolve zpid ──────────────────────────
            ac_headers = {
                **_XHR_HEADERS,
                "Referer": "https://www.zillow.com/",
                "Sec-Fetch-Site": "same-site",
                "Origin": "https://www.zillow.com",
            }
            zpid = detail_url = None
            for ac_url in [
                "https://www.zillowstatic.com/autocomplete/v3/suggestions",
                "https://www.zillow.com/autocomplete/v3/suggestions",
            ]:
                try:
                    await asyncio.sleep(_jitter(0.4, 1.2))
                    ac = await s.get(
                        ac_url,
                        params={"q": address, "clientId": "homepage-render"},
                        headers=ac_headers,
                    )
                    if ac.status_code == 200:
                        for r in ac.json().get("results", []):
                            meta = r.get("metaData", {})
                            zpid = meta.get("zpid")
                            detail_url = meta.get("detailUrl")
                            if zpid:
                                break
                    if zpid:
                        break
                except Exception as e:
                    logger.debug(f"Zillow autocomplete attempt failed: {e}")

            if not zpid:
                logger.debug("Zillow browser: could not resolve zpid")
                return None

            # ── Step 3: Simulate search results browse (makes session look real) ──
            await asyncio.sleep(_jitter(1.5, 3.0))
            try:
                search_ref = f"https://www.zillow.com/homes/for_sale/{slug}/"
                await s.get(
                    search_ref,
                    headers={**_NAV_HEADERS, "Sec-Fetch-Site": "same-origin"},
                    allow_redirects=True,
                )
            except Exception:
                pass

            # ── Step 4: Fetch property detail page ────────────────────────────
            if detail_url:
                prop_url = (f"https://www.zillow.com{detail_url}"
                            if detail_url.startswith("/") else detail_url)
            else:
                prop_url = f"https://www.zillow.com/homedetails/{slug}/{zpid}_zpid/"

            await asyncio.sleep(_jitter(1.8, 4.0))
            logger.debug(f"Zillow browser: fetching {prop_url}")
            page = await s.get(
                prop_url,
                headers={
                    **_NAV_HEADERS,
                    "Referer": f"https://www.zillow.com/homes/for_sale/{slug}/",
                    "Sec-Fetch-Site": "same-origin",
                },
                allow_redirects=True,
            )

            if page.status_code != 200:
                logger.debug(f"Zillow browser: HTTP {page.status_code}")
                return None

            text = page.text
            if "__NEXT_DATA__" not in text:
                is_blocked = any(w in text.lower() for w in
                                 ["captcha", "robot", "datadome", "challenge", "recaptcha"])
                logger.debug(f"Zillow browser: no __NEXT_DATA__ (blocked={is_blocked})")
                return None

            m = re.search(
                r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                text, re.DOTALL,
            )
            if not m:
                return None

            page_data = json.loads(m.group(1))
            gdp = (page_data.get("props", {})
                   .get("pageProps", {})
                   .get("gdpClientCache"))
            if not gdp:
                return None

            for value in gdp.values():
                if isinstance(value, dict) and "property" in value:
                    from backend.data.listing import _extract_zillow
                    result = _extract_zillow(value["property"], str(page.url))
                    if result:
                        result["source"] = "Zillow"
                    return result

    except Exception as e:
        logger.debug(f"Zillow browser scraper failed: {e}")

    return None


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
