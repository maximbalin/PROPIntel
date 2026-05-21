# PropIntel — AI Property Intelligence

PropIntel is an open-source, AI-powered property risk intelligence tool that analyzes any US address using **only free public data sources** — no paid APIs required. It uses a multi-agent LangGraph pipeline backed by Claude to deliver environmental, infrastructure, and neighborhood risk assessments with a causal narrative and mode-specific advice.

---

## Features

- **Multi-agent pipeline**: Three specialist AI agents (Environmental, Infrastructure, Neighborhood) run in parallel, synthesized by a Chief Analyst agent
- **Free data only**: FEMA NFHL flood zones, EPA ECHO facility data, OpenStreetMap infrastructure, US Census ACS demographics, USGS elevation
- **Two analysis modes**: Buyer (hidden risks + negotiation leverage) and Investor (ROI risk + liquidity)
- **Scored risk profile**: Five scored dimensions — Livability, Environmental Exposure, Infrastructure Risk, Neighborhood Stability, Hidden Risk
- **Evidence-grounded**: Every risk claim cites its source data; confidence scores reflect data completeness
- **Caching**: Redis caches raw data for 48 hours and assessments for 24 hours
- **Persistence**: PostgreSQL + PostGIS for spatial querying and report retrieval

---

## Quickstart

### Prerequisites

- Docker and Docker Compose
- Python 3.11+
- An Anthropic API key (get one at [console.anthropic.com](https://console.anthropic.com))
- (Optional) A free Census API key

### 1. Clone and configure

```bash
git clone <repo-url>
cd propintel
cp .env.example .env
# Edit .env and add your ANTHROPIC_API_KEY
```

### 2. Start infrastructure

```bash
docker-compose up -d
```

This starts PostgreSQL (PostGIS) on port 5432 and Redis on port 6379.

### 3. Install Python dependencies

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 4. Run the server

```bash
cd ..   # Go to repo root (SkillForge/)
uvicorn propintel.backend.main:app --reload --port 8000
```

Or from within the `propintel/` directory:

```bash
PYTHONPATH=.. uvicorn backend.main:app --reload --port 8000
```

Open [http://localhost:8000](http://localhost:8000) in your browser.

---

## Getting a Free Census API Key

The Census ACS data works without a key but is rate-limited. For production use:

1. Visit [https://api.census.gov/data/key_signup.html](https://api.census.gov/data/key_signup.html)
2. Fill in your name and email — the key is emailed immediately, no approval required
3. Add it to your `.env` file: `CENSUS_API_KEY=your_key_here`

---

## API Usage

### Analyze a property

```bash
curl -X POST http://localhost:8000/api/analyze \
  -H "Content-Type: application/json" \
  -d '{
    "address": "1600 Pennsylvania Ave NW, Washington DC",
    "mode": "buyer",
    "risk_tolerance": "medium"
  }'
```

**Response fields:**

| Field | Description |
|-------|-------------|
| `assessment_id` | UUID for retrieving this report later |
| `scores` | Five-dimension risk score object (0-100) |
| `risks` | Array of identified risk factors with evidence |
| `narrative` | AI-generated causal risk paragraph |
| `mode_advice` | Buyer or investor specific advice |
| `overall_confidence` | Data completeness confidence (0-100%) |

### Retrieve a saved report

```bash
curl http://localhost:8000/api/report/<assessment_id>
```

### Health check

```bash
curl http://localhost:8000/health
```

---

## Score Guide

All scores are on a 0-100 scale:

| Score | Dimension | Interpretation |
|-------|-----------|----------------|
| **Livability** | Composite | Higher = more livable. 70+ is good. |
| **Environmental Exposure** | Risk | Lower = less exposed. 30 or below is good. |
| **Infrastructure Risk** | Risk | Lower = fewer infrastructure hazards. |
| **Neighborhood Stability** | Positive | Higher = more stable. 65+ is good. |
| **Hidden Risk** | Composite risk | Lower = fewer latent risks. Key for investors. |

**Color coding in the UI:**
- Green: favorable
- Amber: moderate concern
- Red: elevated risk

---

## Data Sources

| Source | What it provides | Update frequency |
|--------|-----------------|-----------------|
| [FEMA NFHL](https://hazards.fema.gov/gis/nfhl/) | Flood zone designations (Zone A, AE, X, etc.) | Updated per FIRM panel |
| [EPA ECHO](https://echo.epa.gov/) | Regulated facilities within 3 miles (Superfund, air, water, waste) | Near real-time |
| [OpenStreetMap Overpass API](https://overpass-api.de/) | Power lines, highways, railways within 1km | Community-maintained |
| [US Census ACS 5-Year](https://www.census.gov/programs-surveys/acs) | Median income, population, vacancy, unemployment by tract | Annual (2022 vintage) |
| [USGS National Elevation Dataset](https://www.usgs.gov/core-science-systems/national-geospatial-program/national-map) | Ground elevation in meters | Continuously updated |

All data fetches are cached in Redis (raw data: 48h, assessments: 24h) to respect rate limits and minimize latency.

---

## Architecture

```
frontend/           Dark-themed single-page app (HTML/CSS/JS)
backend/
  main.py           FastAPI app, /api/analyze, /api/report endpoints
  config.py         Pydantic settings from .env
  database.py       asyncpg connection pool + migrations
  cache.py          aioredis client
  data/
    fetcher.py      FreeDataFetcher: geocoding + parallel data fetch
    fema.py         FEMA NFHL flood zone query
    epa.py          EPA ECHO facility search
    osm.py          OpenStreetMap Overpass API query
    census.py       Census geocoder + ACS demographics
    usgs.py         USGS elevation point query service
  agents/
    graph.py        LangGraph StateGraph definition
    supervisor.py   Synthesizer node (Chief Analyst)
    environmental.py Environmental risk agent
    infrastructure.py Infrastructure risk agent
    neighborhood.py  Neighborhood stability agent
    prompts.py      All LLM prompt templates
  scoring/
    engine.py       Deterministic fallback score calculator
  models.py         Pydantic request/response models
migrations/
  001_init.sql      PostGIS schema setup
docker-compose.yml  PostgreSQL + Redis services
```

**Agent flow:**

```
Address Input
    │
    ▼
Geocode (Nominatim)
    │
    ▼
Parallel Data Fetch (FEMA + EPA + OSM + Census + USGS)
    │
    ▼
┌───────────────────────────────────────┐
│  Parallel AI Agents                   │
│  [Environmental] [Infrastructure]     │
│  [Neighborhood]                       │
└───────────────────────────────────────┘
    │
    ▼
Synthesizer (Chief Analyst)
    │
    ▼
Mode Adapter (Buyer / Investor framing)
    │
    ▼
Response + DB persist + Redis cache
```

---

## Configuration

All configuration is via environment variables (`.env` file):

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | Yes | — | Your Anthropic API key |
| `CENSUS_API_KEY` | No | — | Free Census API key (optional but recommended) |
| `DATABASE_URL` | No | `postgresql+asyncpg://postgres:postgres@localhost:5432/propintel` | PostgreSQL DSN |
| `REDIS_URL` | No | `redis://localhost:6379` | Redis URL |
| `LLM_MODEL` | No | `claude-sonnet-4-20250514` | Anthropic model ID |
| `LOG_LEVEL` | No | `INFO` | Python log level |

---

## Disclaimer

PropIntel is an informational tool only. It uses publicly available data that may be incomplete, outdated, or inaccurate for any specific property. Risk scores and AI-generated narratives are NOT a substitute for:

- A professional property inspection
- A licensed environmental assessment (Phase I/II ESA)
- Flood insurance determination from FEMA
- Legal or financial advice

Always verify risk factors independently before making real estate decisions.

---

## Roadmap

- [ ] Wildfire risk layer (USFS HIFLD data)
- [ ] Historical sale price trend overlay
- [ ] Property tax assessment history (county open data)
- [ ] Air quality index (EPA AQS API)
- [ ] School district quality scores (NCES data)
- [ ] PDF report export
- [ ] Shareable report links
- [ ] Batch analysis mode (CSV upload)
- [ ] Map view with risk heatmap

---

## License

MIT License — free to use, modify, and distribute with attribution.
