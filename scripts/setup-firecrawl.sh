#!/usr/bin/env bash
# setup-firecrawl.sh — clone Firecrawl source and prepare local self-hosting.
#
# Run from the propintel/ project root:
#   bash scripts/setup-firecrawl.sh
#
# After this script completes, start all services with:
#   docker compose -f docker-compose.yml -f docker-compose.firecrawl.yml up --build -d

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
FIRECRAWL_DIR="$PROJECT_DIR/firecrawl"
ENV_FILE="$PROJECT_DIR/.env"

echo "=== PropIntel — Firecrawl Local Setup ==="
echo ""

# ── 1. Clone Firecrawl source ──────────────────────────────────
if [ -d "$FIRECRAWL_DIR/.git" ]; then
  echo "[1/5] Firecrawl already cloned at ./firecrawl — pulling latest..."
  git -C "$FIRECRAWL_DIR" pull --ff-only || true
else
  echo "[1/5] Cloning Firecrawl (shallow clone, ~30 seconds)..."
  git clone --depth=1 https://github.com/mendableai/firecrawl.git "$FIRECRAWL_DIR"
  echo "      Cloned to ./firecrawl"
fi
echo ""

# ── 2. Extract corporate SSL proxy CA certificate ──────────────
# WSL and corporate networks use SSL inspection proxies that present
# self-signed certs, breaking HTTPS inside Docker containers.
# We extract the root CA cert from the proxy and inject it into the
# Docker build context so ALL tools (curl, rustup, npm, pnpm) trust it.
echo "[2/5] Checking for SSL proxy CA certificate..."

CA_CERT_FILE="$PROJECT_DIR/proxy-ca.crt"
NEED_CA=false

# Quick probe: does the proxy present a non-standard cert for a well-known host?
if openssl s_client -connect sh.rustup.rs:443 -showcerts 2>/dev/null </dev/null \
     | openssl verify 2>&1 | grep -q "self.signed\|unable to get"; then
  NEED_CA=true
fi

if [ "$NEED_CA" = true ]; then
  echo "      SSL proxy detected — extracting root CA certificate..."
  openssl s_client -connect sh.rustup.rs:443 -showcerts 2>/dev/null </dev/null \
    | awk '/-----BEGIN CERTIFICATE-----/{c=""} {c=c"\n"$0} /-----END CERTIFICATE-----/{last=c} END{print last}' \
    | sed '/^$/d' \
    > "$CA_CERT_FILE"

  if [ ! -s "$CA_CERT_FILE" ]; then
    # Fallback: use system CA bundle (may already contain the proxy cert if IT configured WSL)
    echo "      openssl extraction empty — using system CA bundle as fallback"
    cp /etc/ssl/certs/ca-certificates.crt "$CA_CERT_FILE"
  fi
  echo "      Saved proxy CA to ./proxy-ca.crt"
else
  echo "      No SSL proxy detected — skipping CA extraction"
fi
echo ""

# ── 3. Patch Dockerfiles ───────────────────────────────────────
echo "[3/5] Patching Firecrawl Dockerfiles..."

python3 - "$FIRECRAWL_DIR" "$CA_CERT_FILE" << 'PYEOF'
import sys, re, os, shutil

firecrawl_dir = sys.argv[1]
ca_cert_file  = sys.argv[2]
has_ca = os.path.isfile(ca_cert_file) and os.path.getsize(ca_cert_file) > 0

# ── API Dockerfile ──────────────────────────────────────────────
api_df = os.path.join(firecrawl_dir, "apps", "api", "Dockerfile")
if os.path.isfile(api_df):
  content = open(api_df).read()
  changed = False

  # Remove any previous broken patches we may have applied
  content = re.sub(
      r'\n*# (Trust corporate|WSL/corporate)[^\n]*\n(COPY proxy-ca\.crt[^\n]*\n|ENV [^\n]*\n|RUN (?:update-ca-certificates|cp /usr/bin/curl|echo)[^\n]*\n|\s+&&[^\n]*\n)*',
      '\n', content)

  if has_ca:
    # Copy cert into API build context
    shutil.copy(ca_cert_file, os.path.join(firecrawl_dir, "apps", "api", "proxy-ca.crt"))

    ca_block = (
        "\n# Trust corporate SSL proxy CA (WSL/corporate network)\n"
        "COPY proxy-ca.crt /usr/local/share/ca-certificates/proxy-ca.crt\n"
        "RUN update-ca-certificates\n"
        "ENV RUSTUP_DIST_SERVER=https://static.rust-lang.org\n"
        "ENV CARGO_HTTP_CAINFO=/etc/ssl/certs/ca-certificates.crt\n"
        "ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt\n"
        "ENV NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt\n"
    )

    if "proxy-ca.crt" not in content:
      # Insert after first FROM line so it applies to the first build stage
      content = re.sub(r'(^FROM [^\n]+\n)', r'\1' + ca_block, content, count=1, flags=re.MULTILINE)
      changed = True
      print("      API: injected proxy CA certificate trust")
    else:
      print("      API Dockerfile already has CA injection — skipping")
  else:
    if "proxy-ca.crt" not in content:
      print("      API: no SSL proxy — no CA patch needed")

  if changed:
    open(api_df, "w").write(content)
else:
  print("      Warning: API Dockerfile not found — skipping")

# ── Playwright Dockerfile ───────────────────────────────────────
pw_df = os.path.join(firecrawl_dir, "apps", "playwright-service-ts", "Dockerfile")
if os.path.isfile(pw_df):
  content = open(pw_df).read()
  changed = False

  # Remove any previous broken patches
  content = re.sub(
      r'\n*# (Trust corporate|WSL/corporate)[^\n]*\n(COPY proxy-ca\.crt[^\n]*\n|ENV [^\n]*\n|RUN (?:update-ca-certificates|cp /usr/bin/curl|echo)[^\n]*\n|\s+&&[^\n]*\n)*',
      '\n', content)
  content = re.sub(r'\nENV NODE_TLS_REJECT_UNAUTHORIZED=0\n', '\n', content)

  if has_ca:
    shutil.copy(ca_cert_file, os.path.join(firecrawl_dir, "apps", "playwright-service-ts", "proxy-ca.crt"))

    ca_block = (
        "\n# Trust corporate SSL proxy CA (WSL/corporate network)\n"
        "COPY proxy-ca.crt /usr/local/share/ca-certificates/proxy-ca.crt\n"
        "RUN update-ca-certificates\n"
        "ENV NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt\n"
        "ENV NODE_TLS_REJECT_UNAUTHORIZED=0\n"
    )

    if "proxy-ca.crt" not in content:
      content = re.sub(r'(^FROM [^\n]+\n)', r'\1' + ca_block, content, count=1, flags=re.MULTILINE)
      changed = True
      print("      Playwright: injected proxy CA certificate trust")
    else:
      print("      Playwright Dockerfile already has CA injection — skipping")
  else:
    # No CA file, just disable TLS verification for Node (playwright install)
    if "NODE_TLS_REJECT_UNAUTHORIZED" not in content:
      content = re.sub(
          r'(RUN npx playwright install)',
          'ENV NODE_TLS_REJECT_UNAUTHORIZED=0\n\\1',
          content)
      changed = True
      print("      Playwright: disabled TLS verification for Chromium download")

  if changed:
    open(pw_df, "w").write(content)
else:
  print("      Warning: Playwright Dockerfile not found — skipping")
PYEOF
echo ""

# ── 4. Configure .env ──────────────────────────────────────────
echo "[4/5] Configuring .env..."

if [ ! -f "$ENV_FILE" ]; then
  cp "$PROJECT_DIR/.env.example" "$ENV_FILE"
  echo "      Created .env from .env.example"
fi

# Add or update FIRECRAWL_API_URL
if grep -q "^FIRECRAWL_API_URL=" "$ENV_FILE"; then
  sed -i 's|^FIRECRAWL_API_URL=.*|FIRECRAWL_API_URL=http://localhost:3002|' "$ENV_FILE"
  echo "      Updated FIRECRAWL_API_URL=http://localhost:3002"
else
  echo "" >> "$ENV_FILE"
  echo "# Self-hosted Firecrawl (set by setup-firecrawl.sh)" >> "$ENV_FILE"
  echo "FIRECRAWL_API_URL=http://localhost:3002" >> "$ENV_FILE"
  echo "      Added FIRECRAWL_API_URL=http://localhost:3002 to .env"
fi

# Comment out FIRECRAWL_API_KEY if present — local instance needs no key
if grep -q "^FIRECRAWL_API_KEY=fc-" "$ENV_FILE"; then
  sed -i 's|^FIRECRAWL_API_KEY=fc-|# FIRECRAWL_API_KEY=fc-|' "$ENV_FILE"
  echo "      Commented out FIRECRAWL_API_KEY (not needed for local)"
fi
echo ""

# ── 5. Check prerequisites ─────────────────────────────────────
echo "[5/5] Checking prerequisites..."

OK=true

if ! command -v docker &>/dev/null; then
  echo "      ✗ Docker not found — install from https://docs.docker.com/get-docker/"
  OK=false
else
  DOCKER_VER=$(docker --version | grep -oE '[0-9]+\.[0-9]+' | head -1)
  echo "      ✓ Docker $DOCKER_VER"
fi

if ! docker compose version &>/dev/null 2>&1; then
  echo "      ✗ 'docker compose' plugin not found — update Docker Desktop or install the plugin"
  OK=false
else
  echo "      ✓ docker compose"
fi

OPENAI_KEY=$(grep "^OPENAI_API_KEY=" "$ENV_FILE" 2>/dev/null | cut -d= -f2 || true)
if [ -z "$OPENAI_KEY" ] || [ "$OPENAI_KEY" = "sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" ]; then
  echo "      ✗ OPENAI_API_KEY not set — Firecrawl's JSON extraction requires it"
  echo "        Set it in .env: OPENAI_API_KEY=sk-..."
  OK=false
else
  echo "      ✓ OPENAI_API_KEY set"
fi

echo ""

if [ "$OK" = false ]; then
  echo "Fix the issues above, then re-run this script or proceed to the next step."
  exit 1
fi

# ── Done ──────────────────────────────────────────────────────
cat <<'EOF'
Setup complete. Next steps:

  1. Build and start all services (first run takes ~5 minutes to build):

       docker compose -f docker-compose.yml -f docker-compose.firecrawl.yml up --build -d

  2. Wait for Firecrawl to be ready (watch logs):

       docker compose -f docker-compose.yml -f docker-compose.firecrawl.yml logs -f firecrawl-api

  3. Verify Firecrawl is running:

       curl http://localhost:3002/v1/health

  4. Test a scrape:

       curl -s -X POST http://localhost:3002/v1/scrape \
         -H 'Content-Type: application/json' \
         -d '{"url":"https://www.realtor.com","formats":["markdown"]}' | head -c 200

  PropIntel will automatically use the local Firecrawl instance
  (FIRECRAWL_API_URL=http://localhost:3002 is now in your .env).

  Queue admin UI: http://localhost:3002/admin/propintel-local/queues
EOF
