# PropIntel — Development Plan & Notes

## Self-Hosted Firecrawl on WSL (Corporate Network / SSL Proxy)

### Problem

Building Firecrawl Docker images on WSL Ubuntu behind a corporate SSL inspection
proxy fails with:

```
error: could not download file from 'https://static.rust-lang.org/dist/...'
error during download: [60] SSL peer certificate or SSH remote key was not OK
(SSL certificate OpenSSL verify result: self-signed certificate in certificate chain (19))
```

This also manifests as a Node.js/Playwright error during Chromium download:
```
Error: SELF_SIGNED_CERT_IN_CHAIN
```

**Root cause:** Corporate networks intercept HTTPS with a proxy that presents its own
self-signed (or internally-signed) root CA certificate. Docker containers start with
only the standard Mozilla CA bundle, so the proxy's cert is untrusted.

Importantly, `rustup` uses a compiled-in `rustls` TLS stack with its own CA roots —
it does NOT read `/root/.curlrc`, does NOT respect `RUSTUP_USE_CURL=1` reliably, and
does NOT pick up certs from the OS cert store unless you explicitly set
`CARGO_HTTP_CAINFO`/`SSL_CERT_FILE` to the updated bundle.

### What Did NOT Work

| Approach | Why it failed |
|---|---|
| `curl -k` flag on the `sh.rustup.rs` download | Outer curl downloads `rustup-init`, but rustup's internal HTTP client still uses its own TLS |
| `echo 'insecure' > /root/.curlrc` | rustup calls curl with `-q` which suppresses `.curlrc` |
| `ENV RUSTUP_USE_CURL=1` + `.curlrc` | Deprecated in recent rustup; even when active, rustup's toolchain manifest downloads use internal client |
| Replace `/usr/bin/curl` with a wrapper that always passes `--insecure` | curl binary IS replaced but rustup's internal HTTP stack never calls it for toolchain downloads |

### Solution: Inject the Proxy CA Certificate

The definitive fix is to add the proxy's root CA certificate to the container's
OS trust store **before any HTTPS operation**. This makes every tool trust it:
curl (OpenSSL), rustup (rustls via `SSL_CERT_FILE`), npm, pnpm, and any others.

**Implementation in `scripts/setup-firecrawl.sh` (Step 2):**

1. **Detect proxy** — probe `sh.rustup.rs:443`; if OpenSSL reports `self-signed`
   or `unable to get`, a proxy is present.

2. **Extract root CA** — `openssl s_client -showcerts` returns the full chain;
   `awk` extracts only the last cert (the root CA).  Falls back to
   `/etc/ssl/certs/ca-certificates.crt` (system bundle, already includes the
   proxy cert if IT configured WSL properly) if extraction returns empty.

3. **Copy cert into build contexts** — `proxy-ca.crt` is copied into
   `firecrawl/apps/api/` and `firecrawl/apps/playwright-service-ts/`
   (Docker `COPY` only reaches files inside the build context).

4. **Patch each Dockerfile** — inserted immediately after the first `FROM` line:
   ```dockerfile
   RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
       && rm -rf /var/lib/apt/lists/*
   COPY proxy-ca.crt /usr/local/share/ca-certificates/proxy-ca.crt
   RUN update-ca-certificates
   ENV CARGO_HTTP_CAINFO=/etc/ssl/certs/ca-certificates.crt
   ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
   ENV NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt
   ENV NODE_TLS_REJECT_UNAUTHORIZED=0
   ```
   > `ca-certificates` must be installed first — slim base images (`node:18-slim`,
   > `debian:slim`) do not include `update-ca-certificates` by default.

5. **Insertion point matters** — always insert after the `FROM` line, never in
   the middle of a multi-line `RUN apt-get install ... \` block.  Splitting a
   continuation line causes Docker to treat the continuation as an instruction
   (e.g. `curl \` → "unknown instruction: curl").

### Commands to Rebuild After a Failed / Broken Build

```bash
# 1. Reset Dockerfiles to unpatched state
git -C ~/PROPIntel/firecrawl checkout apps/api/Dockerfile
git -C ~/PROPIntel/firecrawl checkout apps/playwright-service-ts/Dockerfile

# 2. Pull latest PropIntel (gets updated setup script)
cd ~/PROPIntel && git pull

# 3. Re-run setup — detects proxy, extracts CA, patches correctly
bash ~/PROPIntel/scripts/setup-firecrawl.sh

# 4. Build (no cache to pick up Dockerfile changes)
sudo docker compose -f ~/PROPIntel/docker-compose.yml \
  -f ~/PROPIntel/docker-compose.firecrawl.yml \
  build --no-cache

# 5. Start services
sudo docker compose -f ~/PROPIntel/docker-compose.yml \
  -f ~/PROPIntel/docker-compose.firecrawl.yml \
  up -d

# 6. Verify Firecrawl is up
curl http://localhost:3002/v1/health
```

### Notes for Future Maintainers

- `proxy-ca.crt` and `firecrawl/` are both in `.gitignore` — they are local
  machine artifacts, not committed to the repo.
- If the corporate CA cert rotates, delete `~/PROPIntel/proxy-ca.crt` and
  re-run `setup-firecrawl.sh` to re-extract it.
- If you are on a network without an SSL proxy, `setup-firecrawl.sh` detects
  this automatically and skips all CA patching.
- The multi-stage Firecrawl API Dockerfile has several `FROM` stages; the patch
  is applied only to the **first** stage (`count=1`).  If a later stage also
  fails SSL, the same cert block needs to be inserted after that stage's `FROM`
  line too.

---

## Completed Features

### 1. Firecrawl-Powered Listing Enrichment
- `backend/data/listing_firecrawl.py` — scrapes Realtor.com → Zillow → Redfin
  using Firecrawl AI JSON extraction
- Priority: `FIRECRAWL_API_URL` (local, no limit) → `FIRECRAWL_API_KEY` (cloud)
  → direct HTTP scrapers (fallback)
- New `ListingData` fields: `property_type`, `lot_size_sqft`, `hoa_fee_monthly`,
  `tax_annual`, `status`, `days_on_market`, `garage_spaces`, `heating_cooling`,
  `description`

### 2. Hidden Risk — Named Facility Details
- Score evidence now shows facility name, distance, hazard types, violation
  count, and penalties instead of generic counts
- FEMA flood zone shows exact zone code + BFE in feet (NAVD88)
- LLM cannot override `hidden_risk` evidence (uses authoritative data sources)
- Assessment cache key: `assessment:v12`
