import hashlib
import json
import logging
import math
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from backend.cache import get_redis, close_redis
from backend.config import get_settings
from backend.data.fetcher import FreeDataFetcher
from backend.database import close_pool, get_pool, run_migrations
from backend.agents.graph import get_graph
from backend.models import (
    AnalyzeRequest, AnalyzeResponse, ScoreSet, ScoreBreakdown, RiskItem,
    ScoreEvidence, Recommendation, PriceContext, NearbyRisk,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent
TEMPLATES_DIR = BASE_DIR / "frontend" / "templates"
STATIC_DIR = BASE_DIR / "frontend" / "static"

app = FastAPI(title="PropIntel", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:8000", "http://127.0.0.1:8000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@app.on_event("startup")
async def startup():
    try:
        await run_migrations()
    except Exception as e:
        logger.warning(f"Migration warning: {e}")


@app.on_event("shutdown")
async def shutdown():
    await close_pool()
    await close_redis()


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")


def _bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Compass bearing in degrees (0=N, 90=E) from point 1 to point 2."""
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    x = math.sin(dlon) * math.cos(math.radians(lat2))
    y = math.cos(math.radians(lat1)) * math.sin(math.radians(lat2)) - \
        math.sin(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def _build_nearby_risks(raw_data: dict, prop_lat: float, prop_lon: float) -> list[NearbyRisk]:
    risks: list[NearbyRisk] = []

    # FEMA flood zone at property
    fema = raw_data.get("fema", {})
    if fema.get("sfha"):
        risks.append(NearbyRisk(
            name=f"SFHA Flood Zone {fema.get('flood_zone', '')}",
            category="flood",
            distance_label="at property",
            severity="high",
            distance_m=0,
        ))
    elif fema.get("risk_level") in ("critical", "high"):
        risks.append(NearbyRisk(
            name=f"Flood Zone {fema.get('flood_zone', '')} ({fema.get('risk_level', '')})",
            category="flood",
            distance_label="at property",
            severity=fema.get("risk_level", "medium"),
            distance_m=0,
        ))

    # EPA facilities with exact bearing
    epa = raw_data.get("epa", {})
    for fac in (epa.get("top_facilities") or [])[:6]:
        dist_mi = fac.get("distance_miles", 0) or 0
        dist_m = dist_mi * 1609.34
        fac_lat = fac.get("lat") or fac.get("FacLat")
        fac_lon = fac.get("lon") or fac.get("FacLong")
        bearing = _bearing(prop_lat, prop_lon, float(fac_lat), float(fac_lon)) \
            if fac_lat and fac_lon else None
        tier = fac.get("tier", 3)
        if dist_mi < 0.5 and tier == 1:
            sev = "critical"
        elif tier == 1:
            sev = "high"
        elif tier == 2 and dist_mi < 1.0:
            sev = "high"
        elif tier == 2:
            sev = "medium"
        else:
            sev = "low"
        if dist_mi < 0.1:
            dist_label = f"{int(dist_m)}m"
        else:
            dist_label = f"{dist_mi:.1f} mi"
        risks.append(NearbyRisk(
            name=fac.get("name", "EPA Facility"),
            category="pollution",
            distance_label=dist_label,
            severity=sev,
            distance_m=dist_m,
            bearing_deg=bearing,
        ))

    # OSM infrastructure by distance
    osm = raw_data.get("osm", {})
    near = osm.get("within_300m", {})
    far  = osm.get("within_1000m", {})
    cat_names = {
        "railway":      "Railway",
        "power_line":   "Power Line",
        "highway":      "Highway",
        "industrial":   "Industrial Zone",
        "landfill":     "Landfill",
        "substation":   "Electrical Substation",
        "fuel_station": "Fuel Station",
        "airport":      "Airport",
    }
    for cat, label in cat_names.items():
        data = near.get(cat) or far.get(cat)
        if not data or data.get("count", 0) == 0:
            continue
        nearest = data.get("nearest_m")
        if nearest is not None:
            dist_m = float(nearest)
            dist_label = f"{int(dist_m)}m"
        else:
            dist_m = 750.0
            dist_label = "< 1km"
        if cat in ("railway", "power_line") and dist_m < 150:
            sev = "high"
        elif cat == "landfill":
            sev = "high" if dist_m < 500 else "medium"
        elif dist_m < 200:
            sev = "high"
        elif dist_m < 500:
            sev = "medium"
        else:
            sev = "low"
        risks.append(NearbyRisk(
            name=label,
            category="infrastructure",
            distance_label=dist_label,
            severity=sev,
            distance_m=dist_m,
        ))

    sev_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    return sorted(risks, key=lambda r: (sev_order.get(r.severity, 9), r.distance_m or 9999))


@app.post("/api/analyze", response_model=AnalyzeResponse)
async def analyze(req: AnalyzeRequest):
    settings = get_settings()

    cache_key = f"assessment:{hashlib.md5(req.address.encode()).hexdigest()}:{req.mode}"
    redis = await get_redis()
    cached = await redis.get(cache_key)
    if cached:
        return AnalyzeResponse(**json.loads(cached))

    fetcher = FreeDataFetcher()
    if req.lat is not None and req.lon is not None:
        lat, lon = req.lat, req.lon
    else:
        try:
            lat, lon = await fetcher.geocode(req.address)
        except ValueError:
            raise HTTPException(status_code=422, detail="Address not found")

    raw_data = await fetcher.fetch_all(lat, lon)

    graph = get_graph()
    initial_state = {
        "address": req.address,
        "lat": lat,
        "lon": lon,
        "mode": req.mode.value,
        "risk_tolerance": req.risk_tolerance,
        "raw_data": raw_data,
        "env_report": None,
        "infra_report": None,
        "neighborhood_report": None,
        "scores": None,
        "risks": None,
        "narrative": None,
        "mode_advice": None,
        "overall_confidence": None,
        "score_evidence": None,
        "recommendation": None,
        "price_impact": None,
    }

    final_state = await graph.ainvoke(initial_state)

    scores_dict = final_state.get("scores", {})
    score_set = ScoreSet(
        livability=scores_dict.get("livability", 50),
        environmental_exposure=scores_dict.get("environmental_exposure", 50),
        infrastructure_risk=scores_dict.get("infrastructure_risk", 50),
        neighborhood_stability=scores_dict.get("neighborhood_stability", 50),
        hidden_risk=scores_dict.get("hidden_risk", 50),
    )
    debug = scores_dict.get("_debug", {})
    score_breakdown = ScoreBreakdown(**{k: debug.get(k, 50) for k in ScoreBreakdown.model_fields}) if debug else None

    # Score evidence from LLM
    raw_evidence = final_state.get("score_evidence") or {}
    score_evidence = ScoreEvidence(**{
        k: raw_evidence.get(k, []) for k in ScoreEvidence.model_fields
    }) if raw_evidence else None

    # Decision recommendation from LLM
    raw_rec = final_state.get("recommendation") or {}
    recommendation = Recommendation(
        verdict=raw_rec.get("verdict", "CAUTION"),
        score=int(raw_rec.get("score", 50)),
        summary=raw_rec.get("summary", ""),
        key_factors=raw_rec.get("key_factors", []),
    ) if raw_rec else None

    # Price context — LLM impact + Census area median value
    raw_pi = final_state.get("price_impact") or {}
    census = raw_data.get("census", {})
    area_median = census.get("median_home_value") if isinstance(census, dict) else None
    price_context = PriceContext(
        area_median_value=area_median,
        estimated_impact_pct=int(raw_pi.get("estimated_impact_pct", 0)),
        impact_drivers=raw_pi.get("impact_drivers", []),
    ) if (raw_pi or area_median) else None

    # Nearby risks derived from raw fetcher data
    nearby_risks = _build_nearby_risks(raw_data, lat, lon)

    raw_risks = final_state.get("risks", []) or []
    risks = []
    for r in raw_risks:
        if isinstance(r, dict):
            risks.append(RiskItem(
                category=r.get("category", "unknown"),
                severity=r.get("severity", "low"),
                description=r.get("description", ""),
                evidence=r.get("evidence", []),
                confidence=int(r.get("confidence", 50)),
                timeline=r.get("timeline"),
            ))

    data_sources = list({
        src
        for report in [
            final_state.get("env_report", {}),
            final_state.get("infra_report", {}),
            final_state.get("neighborhood_report", {}),
        ]
        if isinstance(report, dict)
        for src in report.get("sources_used", [])
    })

    assessment_id = str(uuid.uuid4())

    response = AnalyzeResponse(
        assessment_id=assessment_id,
        address=req.address,
        lat=lat,
        lon=lon,
        mode=req.mode.value,
        scores=score_set,
        score_breakdown=score_breakdown,
        score_evidence=score_evidence,
        recommendation=recommendation,
        price_context=price_context,
        nearby_risks=nearby_risks,
        risks=risks,
        narrative=final_state.get("narrative", ""),
        mode_advice=final_state.get("mode_advice", ""),
        data_sources=data_sources,
        overall_confidence=final_state.get("overall_confidence", 50),
    )

    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            prop_id = await conn.fetchval(
                """INSERT INTO properties (address, lat, lon, geom, raw_data)
                   VALUES ($1, $2, $3, ST_SetSRID(ST_MakePoint($3, $2), 4326), $4::jsonb)
                   ON CONFLICT DO NOTHING
                   RETURNING id""",
                req.address, lat, lon, json.dumps(raw_data),
            )
            if not prop_id:
                prop_id = await conn.fetchval(
                    "SELECT id FROM properties WHERE address = $1", req.address
                )
            await conn.execute(
                """INSERT INTO assessments (id, property_id, mode, scores, risks, narrative, mode_advice, confidence)
                   VALUES ($1, $2, $3, $4::jsonb, $5::jsonb, $6, $7, $8)""",
                uuid.UUID(assessment_id),
                prop_id,
                req.mode.value,
                json.dumps(scores_dict),
                json.dumps([r.model_dump() for r in risks]),
                response.narrative,
                response.mode_advice,
                response.overall_confidence / 100.0,
            )
    except Exception as e:
        logger.warning(f"DB store failed: {e}")

    await redis.setex(cache_key, 24 * 3600, response.model_dump_json())
    return response


@app.get("/api/report/{assessment_id}", response_model=AnalyzeResponse)
async def get_report(assessment_id: str):
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT a.id, p.address, p.lat, p.lon, a.mode,
                          a.scores, a.risks, a.narrative, a.mode_advice, a.confidence
                   FROM assessments a JOIN properties p ON a.property_id = p.id
                   WHERE a.id = $1""",
                uuid.UUID(assessment_id),
            )
        if not row:
            raise HTTPException(status_code=404, detail="Report not found")
        scores_dict = json.loads(row["scores"]) if row["scores"] else {}
        risks_raw = json.loads(row["risks"]) if row["risks"] else []
        return AnalyzeResponse(
            assessment_id=str(row["id"]),
            address=row["address"],
            lat=row["lat"],
            lon=row["lon"],
            mode=row["mode"],
            scores=ScoreSet(**scores_dict),
            risks=[RiskItem(**r) for r in risks_raw],
            narrative=row["narrative"] or "",
            mode_advice=row["mode_advice"] or "",
            data_sources=[],
            overall_confidence=int((row["confidence"] or 0.5) * 100),
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
