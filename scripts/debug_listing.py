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


async def main():
    print(f"Diagnosing listing data for: {ADDRESS!r}")
    await asyncio.gather(
        test_zillow(),
        test_redfin(),
        test_realtor(),
        test_firecrawl(),
    )
    print("\nDone.")


asyncio.run(main())
