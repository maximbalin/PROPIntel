#!/usr/bin/env bash
# Patches Firecrawl's playwright-service-ts to use playwright-extra stealth mode.
# This makes Chromium look like a real browser to Datadome/Cloudflare:
#   - patches navigator.webdriver
#   - patches canvas fingerprint
#   - patches chrome runtime, plugins, permissions, etc.
#
# Run from ~/PROPIntel after running setup-firecrawl.sh:
#   bash scripts/patch-playwright-stealth.sh
# Then rebuild: docker compose -f docker-compose.yml -f docker-compose.firecrawl.yml up --build -d firecrawl-playwright

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROPINTEL_DIR="$(dirname "$SCRIPT_DIR")"
SERVICE_DIR="$PROPINTEL_DIR/firecrawl/apps/playwright-service-ts"

echo "==> Checking playwright-service-ts directory..."
if [ ! -d "$SERVICE_DIR" ]; then
    echo "ERROR: $SERVICE_DIR not found."
    echo "Run scripts/setup-firecrawl.sh first to clone Firecrawl source."
    exit 1
fi
echo "    Found: $SERVICE_DIR"

PKG="$SERVICE_DIR/package.json"
if [ ! -f "$PKG" ]; then
    echo "ERROR: package.json not found at $PKG"
    exit 1
fi

echo ""
echo "==> Backing up original files..."
cp "$PKG" "$PKG.bak" && echo "    Backed up package.json"

# ── Find the main TypeScript entrypoint ──────────────────────────────────────
SRC=""
for candidate in \
    "$SERVICE_DIR/src/index.ts" \
    "$SERVICE_DIR/src/app.ts" \
    "$SERVICE_DIR/src/server.ts" \
    "$SERVICE_DIR/index.ts"
do
    if [ -f "$candidate" ]; then
        SRC="$candidate"
        break
    fi
done

if [ -z "$SRC" ]; then
    # Fall back: find the file that imports playwright
    SRC=$(grep -rl "from 'playwright'" "$SERVICE_DIR/src" 2>/dev/null | head -1)
fi

if [ -z "$SRC" ]; then
    echo "ERROR: Could not find TypeScript source file in $SERVICE_DIR"
    echo "Files found:"
    find "$SERVICE_DIR/src" -name "*.ts" 2>/dev/null | head -10
    exit 1
fi
echo "    Found source: $SRC"
cp "$SRC" "$SRC.bak" && echo "    Backed up $(basename $SRC)"

# ── Add stealth plugin dependencies to package.json ──────────────────────────
echo ""
echo "==> Adding playwright-extra stealth dependencies to package.json..."

python3 - <<'PYEOF'
import json, sys

pkg_path = sys.argv[1]
with open(pkg_path) as f:
    pkg = json.load(f)

deps = pkg.setdefault("dependencies", {})
patched = []

for name, version in [
    ("playwright-extra",               "^0.0.1"),
    ("puppeteer-extra-plugin-stealth", "^2.11.2"),
]:
    if name not in deps:
        deps[name] = version
        patched.append(name)

with open(pkg_path, "w") as f:
    json.dump(pkg, f, indent=2)

if patched:
    print(f"    Added: {', '.join(patched)}")
else:
    print("    Already present — no changes needed")
PYEOF "$PKG"

# ── Patch the TypeScript source ───────────────────────────────────────────────
echo ""
echo "==> Patching TypeScript source for stealth mode..."

SRC_CONTENT=$(cat "$SRC")

# Check if already patched
if echo "$SRC_CONTENT" | grep -q "playwright-extra"; then
    echo "    Already patched — skipping"
else
    # Strategy 1: Replace 'import { chromium }' with stealth-wrapped version
    if echo "$SRC_CONTENT" | grep -q "from 'playwright'"; then
        python3 - <<'PYEOF'
import sys, re

src_path = sys.argv[1]
with open(src_path) as f:
    content = f.read()

stealth_header = """\
// --- Stealth mode patch (added by patch-playwright-stealth.sh) ---
import { chromium as _chromiumBase } from 'playwright-extra';
// @ts-ignore
import StealthPlugin from 'puppeteer-extra-plugin-stealth';
_chromiumBase.use(StealthPlugin());
// --- End stealth patch ---
"""

# Remove the original playwright chromium import and replace with stealth
# Handle patterns like:
#   import { chromium } from 'playwright';
#   import { chromium, Page } from 'playwright';
#   const { chromium } = require('playwright');

# Pattern: import { chromium ... } from 'playwright'
content = re.sub(
    r"import\s*\{([^}]*\bchromium\b[^}]*)\}\s*from\s*'playwright';?",
    lambda m: (
        stealth_header +
        # keep non-chromium imports if any
        (f"import {{{m.group(1).replace('chromium', '').strip().strip(',')}}}" +
         " from 'playwright';\n" if m.group(1).replace('chromium', '').strip().strip(',') else "")
    ),
    content,
    count=1,
)

# Replace uses of chromium. with _chromiumBase. so the rest of the code works
content = re.sub(r'\bchromium\.', '_chromiumBase.', content)

with open(src_path, "w") as f:
    f.write(content)

print("    Patched: replaced chromium import with stealth-wrapped version")
PYEOF "$SRC"

    else
        echo "    WARNING: Could not find 'from playwright' import in $SRC"
        echo "    Manual patch needed — see README section at end of this script"
    fi
fi

# ── Verify Dockerfile supports the new deps ───────────────────────────────────
DOCKERFILE="$SERVICE_DIR/Dockerfile"
echo ""
echo "==> Checking Dockerfile..."
if [ -f "$DOCKERFILE" ]; then
    if grep -q "npm install\|npm ci\|yarn install" "$DOCKERFILE"; then
        echo "    OK — Dockerfile installs npm deps (stealth packages will be included)"
    else
        echo "    WARNING: Dockerfile may not run npm install — check manually"
    fi
else
    echo "    No Dockerfile found at expected location — skipping"
fi

# ── Print summary ─────────────────────────────────────────────────────────────
echo ""
echo "============================================================"
echo " Stealth patch complete"
echo "============================================================"
echo ""
echo " What was changed:"
echo "   - package.json: added playwright-extra + puppeteer-extra-plugin-stealth"
echo "   - $(basename $SRC): chromium replaced with stealth-wrapped version"
echo ""
echo " Next steps:"
echo "   Rebuild and restart the playwright service:"
echo ""
echo "   docker compose -f docker-compose.yml -f docker-compose.firecrawl.yml \\"
echo "     up --build -d firecrawl-playwright"
echo ""
echo "   Then test:"
echo "   python3 scripts/debug_listing.py '20 Pine St, Natick, MA 01760'"
echo ""
echo " To undo:"
echo "   cp $PKG.bak $PKG"
echo "   cp $SRC.bak $SRC"
echo "   docker compose ... up --build -d firecrawl-playwright"
