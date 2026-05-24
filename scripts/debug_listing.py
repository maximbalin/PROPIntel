#!/usr/bin/env python3
"""
Diagnostic script — run from ~/PROPIntel:
  python3 scripts/debug_listing.py "20 Pine St, Natick, MA 01760"
"""
import asyncio
import json
import re
import sys

import httpx

ADDRESS = sys.argv[1] if len(sys.argv) > 1 else "20 Pine St, Natick, MA 01760"

_ZILLOW_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
}

_REDFIN_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.redfin.com/",
    "Origin": "https://www.redfin.com",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "DNT": "1",
}


async def test_zillow():
    print("\n" + "="*60)
    print("ZILLOW — direct URL fetch")
    print("="*60)
    slug = re.sub(r"[,\s]+", "-", ADDRESS.strip()).strip("-")
    url = f"https://www.zillow.com/homes/{slug}_rb/"
    print(f"URL: {url}")
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=_ZILLOW_HEADERS) as c:
        try:
            r = await c.get(url)
            print(f"HTTP {r.status_code}  final URL: {r.url}")
            has_data = "__NEXT_DATA__" in r.text
            has_captcha = any(w in r.text.lower() for w in ["captcha", "robot", "recaptcha", "challenge"])
            print(f"Has __NEXT_DATA__: {has_data}")
            print(f"Looks like bot-block page: {has_captcha}")
            print(f"Response length: {len(r.text)} chars")
            if has_data:
                m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', r.text, re.DOTALL)
                if m:
                    d = json.loads(m.group(1))
                    gdp = d.get("props", {}).get("pageProps", {}).get("gdpClientCache")
                    print(f"gdpClientCache present: {bool(gdp)}")
                    if gdp:
                        keys = list(gdp.keys())[:3]
                        print(f"gdpClientCache keys (first 3): {keys}")
                        for k, v in gdp.items():
                            if isinstance(v, dict) and "property" in v:
                                prop = v["property"]
                                print(f"property.price = {prop.get('price')}")
                                print(f"property.bedrooms = {prop.get('bedrooms')}")
                                print(f"property.livingArea = {prop.get('livingArea')}")
                                break
        except Exception as e:
            print(f"ERROR: {e}")

    print("\n--- Zillow autocomplete fallback ---")
    async with httpx.AsyncClient(timeout=10, headers=_ZILLOW_HEADERS) as c:
        for url, params in [
            ("https://www.zillowstatic.com/autocomplete/v3/suggestions", {"q": ADDRESS, "clientId": "homepage-render"}),
            ("https://www.zillow.com/autocomplete/v3/suggestions", {"q": ADDRESS, "clientId": "homepage-render"}),
        ]:
            try:
                r = await c.get(url, params=params)
                print(f"AC {url.split('/')[-2]}... → HTTP {r.status_code}")
                if r.status_code == 200:
                    results = r.json().get("results", [])
                    print(f"  results count: {len(results)}")
                    if results:
                        print(f"  first result: {json.dumps(results[0], indent=2)[:300]}")
                else:
                    print(f"  body: {r.text[:200]}")
            except Exception as e:
                print(f"  ERROR: {e}")


async def test_redfin():
    print("\n" + "="*60)
    print("REDFIN — autocomplete endpoints")
    print("="*60)
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=_REDFIN_HEADERS) as c:
        for label, url, params in [
            ("legacySearchV2", "https://www.redfin.com/stingray/api/gis/legacySearchV2",
             {"al": 1, "market": "usmarket", "q": ADDRESS, "num_entries": 5, "start": 0, "v": 2}),
            ("query-location-autocomplete", "https://www.redfin.com/stingray/do/query-location-autocomplete",
             {"al": 1, "location": ADDRESS, "start": 0, "count": 5, "v": 2}),
        ]:
            try:
                r = await c.get(url, params=params)
                print(f"\n{label}: HTTP {r.status_code}")
                if r.status_code == 200:
                    raw = r.text.lstrip("{}&&").strip()
                    data = json.loads(raw)
                    print(f"  payload keys: {list((data.get('payload') or {}).keys())[:8]}")
                    sections = (data.get("payload") or {}).get("sections", [])
                    print(f"  sections: {len(sections)}")
                    for section in sections:
                        for row in section.get("rows", []):
                            if str(row.get("type")) == "1":
                                print(f"  FOUND property row: type={row.get('type')} id={row.get('id')} url={row.get('url')}")
                else:
                    print(f"  body[:200]: {r.text[:200]}")
            except Exception as e:
                print(f"  ERROR: {type(e).__name__}: {e}")


async def test_realtor():
    print("\n" + "="*60)
    print("REALTOR.COM — API autocomplete")
    print("="*60)
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(
                "https://www.realtor.com/api/v1/hulk_main_srp/call",
                params={"client_id": "rdc-x", "schema": "vesta", "q": ADDRESS, "type": "address", "limit": 3},
                headers={
                    "Origin": "https://www.realtor.com",
                    "Referer": "https://www.realtor.com/",
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                    "Accept": "application/json",
                },
            )
            print(f"HTTP {r.status_code}")
            if r.status_code == 200:
                data = r.json()
                results = (data.get("data") or {}).get("home_search", {}).get("results", []) or []
                print(f"Results: {len(results)}")
                if results:
                    r0 = results[0]
                    print(f"  permalink: {r0.get('permalink')}")
                    print(f"  property_id: {r0.get('property_id')}")
                    loc = r0.get("location", {})
                    addr = loc.get("address", {})
                    print(f"  address: {addr.get('line')}, {addr.get('city')}, {addr.get('state_code')} {addr.get('postal_code')}")
            else:
                print(f"body[:300]: {r.text[:300]}")
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")


async def test_firecrawl():
    print("\n" + "="*60)
    print("FIRECRAWL — health + test scrape")
    print("="*60)
    async with httpx.AsyncClient(timeout=30) as c:
        for health_url in ["http://localhost:3002/health", "http://localhost:3002/v0/health"]:
            try:
                r = await c.get(health_url)
                print(f"Health {health_url}: HTTP {r.status_code}  body: {r.text[:100]}")
                break
            except Exception as e:
                print(f"Health {health_url}: {type(e).__name__}: {e}")

        # Quick scrape test with a simple public page
        try:
            r = await c.post(
                "http://localhost:3002/v1/scrape",
                headers={"Content-Type": "application/json"},
                json={"url": "https://httpbin.org/get", "formats": ["markdown"]},
            )
            print(f"Scrape httpbin: HTTP {r.status_code}")
            body = r.json()
            print(f"  success: {body.get('success')}  keys: {list((body.get('data') or {}).keys())}")
        except Exception as e:
            print(f"Scrape test: {type(e).__name__}: {e}")


async def test_browser():
    print("\n" + "="*60)
    print("BROWSER IMPERSONATION (curl_cffi chrome124)")
    print("="*60)
    try:
        from curl_cffi.requests import AsyncSession
        print("curl_cffi available")
    except ImportError:
        print("curl_cffi NOT installed — run: pip install curl-cffi")
        return

    import random

    def jitter(lo=1.2, hi=3.5):
        return random.uniform(lo, hi)

    _nav = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "DNT": "1",
    }

    print("\n--- Zillow with Chrome impersonation ---")
    async with AsyncSession(impersonate="chrome124") as s:
        print("1. Warming up homepage...")
        r = await s.get("https://www.zillow.com/", headers=_nav)
        print(f"   Homepage: HTTP {r.status_code}  cookies: {list(s.cookies.keys())[:6]}")
        await asyncio.sleep(jitter(2, 4))

        print("2. Autocomplete...")
        ac_h = {**_nav, "Accept": "application/json", "Referer": "https://www.zillow.com/",
                "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-site"}
        await asyncio.sleep(jitter(0.5, 1.5))
        ac = await s.get("https://www.zillowstatic.com/autocomplete/v3/suggestions",
                         params={"q": ADDRESS, "clientId": "homepage-render"}, headers=ac_h)
        print(f"   Autocomplete: HTTP {ac.status_code}")
        zpid = detail_url = None
        if ac.status_code == 200:
            for result in ac.json().get("results", []):
                meta = result.get("metaData", {})
                zpid = meta.get("zpid")
                detail_url = meta.get("detailUrl")
                if zpid:
                    print(f"   zpid={zpid}  detailUrl={detail_url}")
                    break

        if zpid:
            slug = re.sub(r"[,\s]+", "-", ADDRESS.strip()).strip("-")
            prop_url = (f"https://www.zillow.com{detail_url}" if detail_url and detail_url.startswith("/")
                        else f"https://www.zillow.com/homedetails/{slug}/{zpid}_zpid/")
            await asyncio.sleep(jitter(2, 4))
            print(f"3. Fetching property page: {prop_url}")
            page = await s.get(prop_url, headers={**_nav, "Referer": "https://www.zillow.com/",
                                                  "Sec-Fetch-Site": "same-origin"}, allow_redirects=True)
            print(f"   HTTP {page.status_code}  len={len(page.text)}")
            has_data = "__NEXT_DATA__" in page.text
            blocked = any(w in page.text.lower() for w in ["captcha", "robot", "recaptcha", "are you human"])
            print(f"   Has __NEXT_DATA__: {has_data}   Blocked: {blocked}")
            if has_data:
                print("   SUCCESS - property data available!")
        else:
            print("   Could not resolve zpid")

        if zpid and ac.status_code == 200 and "lat" in ac.text:
            print("\n--- Zillow GetSearchPageState map API ---")
            try:
                meta_full = {}
                for result in ac.json().get("results", []):
                    m2 = result.get("metaData", {})
                    if m2.get("zpid"):
                        meta_full = m2
                        break
                lat2 = meta_full.get("lat")
                lon2 = meta_full.get("lon") or meta_full.get("lng")
                print(f"   lat={lat2}  lon={lon2}")
                if lat2 and lon2:
                    search_state = {"pagination": {}, "mapBounds": {"west": lon2 - 0.006, "east": lon2 + 0.006, "south": lat2 - 0.004, "north": lat2 + 0.004}, "filterState": {"sortSelection": {"value": "globalrelevanceex"}, "isAllHomes": {"value": True}}, "isMapVisible": True, "isListVisible": True}
                    wants = {"cat1": ["listResults", "mapResults"], "cat2": ["total"]}
                    map_h = {**_nav, "Accept": "application/json", "Referer": "https://www.zillow.com/homes/for_sale/", "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-origin"}
                    await asyncio.sleep(jitter(1, 2))
                    mr = await s.get("https://www.zillow.com/search/GetSearchPageState.htm",
                        params={"searchQueryState": json.dumps(search_state, separators=(",",":")), "wants": json.dumps(wants, separators=(",",":")), "requestId": 2},
                        headers=map_h)
                    print(f"   Map API: HTTP {mr.status_code}  len={len(mr.text)}")
                    if mr.status_code == 200:
                        mdata = mr.json()
                        results2 = (mdata.get("cat1", {}).get("searchResults", {}).get("listResults", []) or
                                    mdata.get("cat1", {}).get("searchResults", {}).get("mapResults", []))
                        print(f"   Results in bbox: {len(results2)}")
                        for p in results2:
                            print(f"   zpid={p.get('zpid')} price={p.get('price') or p.get('unformattedPrice')} beds={p.get('beds')} sqft={p.get('area')}")
                    else:
                        print(f"   Body: {mr.text[:200]}")
            except Exception as e:
                print(f"   Map API error: {e}")

    print("\n--- Redfin with Chrome impersonation ---")
    async with AsyncSession(impersonate="chrome124") as s:
        print("1. Warming up homepage...")
        r = await s.get("https://www.redfin.com/", headers=_nav)
        print(f"   Homepage: HTTP {r.status_code}  cookies: {list(s.cookies.keys())[:6]}")
        await asyncio.sleep(jitter(2, 4))

        print("2. Autocomplete API...")
        ac_h = {**_nav, "Accept": "application/json", "Referer": "https://www.redfin.com/",
                "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-origin"}
        await asyncio.sleep(jitter(0.8, 2))
        ac = await s.get("https://www.redfin.com/stingray/do/query-location-autocomplete",
                         params={"al": 1, "location": ADDRESS, "start": 0, "count": 5, "v": 2},
                         headers=ac_h)
        print(f"   Autocomplete: HTTP {ac.status_code}  len={len(ac.text)}")
        if ac.status_code == 200:
            try:
                data = json.loads(ac.text.lstrip("{}&&").strip())
                sections = (data.get("payload") or {}).get("sections", [])
                for section in sections:
                    for row in section.get("rows", []):
                        if str(row.get("type")) == "1":
                            print(f"   FOUND: propertyId={row.get('id')}  url={row.get('url')}")
            except Exception as e:
                print(f"   Parse error: {e}")
                print(f"   Raw: {ac.text[:200]}")
        else:
            print(f"   Body: {ac.text[:200]}")


async def main():
    print(f"Diagnosing listing data for: {ADDRESS!r}")
    # Run browser test first (sequential — needs warmup delays)
    await test_browser()
    # Run rest in parallel
    await asyncio.gather(
        test_zillow(),
        test_redfin(),
        test_realtor(),
        test_firecrawl(),
    )
    print("\nDone.")


asyncio.run(main())
