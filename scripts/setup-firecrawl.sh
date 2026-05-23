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
  echo "[1/4] Firecrawl already cloned at ./firecrawl — pulling latest..."
  git -C "$FIRECRAWL_DIR" pull --ff-only || true
else
  echo "[1/4] Cloning Firecrawl (shallow clone, ~30 seconds)..."
  git clone --depth=1 https://github.com/mendableai/firecrawl.git "$FIRECRAWL_DIR"
  echo "      Cloned to ./firecrawl"
fi
echo ""

# ── 2. Patch Playwright Dockerfile for SSL proxy environments ──
# WSL and corporate networks often have SSL inspection proxies that cause
# self-signed certificate errors when Playwright downloads Chromium.
# We insert NODE_TLS_REJECT_UNAUTHORIZED=0 before the install step.
echo "[2/4] Patching Playwright Dockerfile for SSL proxy compatibility..."
PLAYWRIGHT_DF="$FIRECRAWL_DIR/apps/playwright-service-ts/Dockerfile"
if [ -f "$PLAYWRIGHT_DF" ]; then
  if grep -q "NODE_TLS_REJECT_UNAUTHORIZED" "$PLAYWRIGHT_DF"; then
    echo "      Already patched — skipping"
  else
    # Insert ENV line before the playwright install line
    sed -i '/RUN npx playwright install/i ENV NODE_TLS_REJECT_UNAUTHORIZED=0' "$PLAYWRIGHT_DF"
    echo "      Patched: disabled TLS verification for Chromium download"
  fi
else
  echo "      Warning: Playwright Dockerfile not found at expected path — skipping patch"
fi
echo ""

# ── 3. Patch .env ──────────────────────────────────────────────
echo "[3/4] Configuring .env..."

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

# ── 4. Check prerequisites ─────────────────────────────────────
echo "[4/4] Checking prerequisites..."

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
