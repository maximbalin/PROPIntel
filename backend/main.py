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
    ScoreEvidence, Recommendation, PriceContext, NearbyRisk, HiddenCost,
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

    # OSM named major roads (uses per-class breakdown with road names)
    osm = raw_data.get("osm", {})
    near = osm.get("within_300m", {})
    far  = osm.get("within_1000m", {})
    major_roads = osm.get("major_roads", {}) or {}

    road_class_meta = {
        "motorway": ("Interstate",     lambda d: "high"   if d < 500  else ("medium" if d < 1500 else "low")),
        "trunk":    ("US Highway",     lambda d: "high"   if d < 400  else ("medium" if d < 1200 else "low")),
        "primary":  ("Primary Road",   lambda d: "high"   if d < 300  else ("medium" if d < 800  else "low")),
        "secondary":("Secondary Road", lambda d: "medium" if d < 200  else "low"),
    }
    for cls, (label, sev_fn) in road_class_meta.items():
        rd = major_roads.get(cls, {})
        if rd.get("count", 0) == 0:
            continue
        nm = rd.get("nearest_m")
        if nm is None:
            continue
        dist_m = float(nm)
        names = rd.get("names", [])
        name_str = names[0] if names else label
        risks.append(NearbyRisk(
            name=f"{label}: {name_str}",
            category="traffic",
            distance_label=f"{int(dist_m)}m",
            severity=sev_fn(dist_m),
            distance_m=dist_m,
        ))

    # OSM hazard categories (non-road)
    cat_names = {
        "railway":      "Railway",
        "power_line":   "Power Line",
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


def _build_score_evidence(raw_data: dict, llm_evidence: dict | None) -> dict:
    """
    Build per-score evidence bullets directly from fetcher raw_data.
    LLM evidence is used only when it provides MORE bullets than we can derive.
    This ensures evidence is always present regardless of LLM output.
    """
    ev: dict[str, list[str]] = {k: [] for k in
        ["livability", "environmental_exposure", "infrastructure_risk",
         "neighborhood_stability", "hidden_risk"]}

    fema  = raw_data.get("fema",   {}) or {}
    epa   = raw_data.get("epa",    {}) or {}
    osm   = raw_data.get("osm",    {}) or {}
    usgs  = raw_data.get("usgs",   {}) or {}
    census= raw_data.get("census", {}) or {}

    # ── Environmental exposure ────────────────────────────
    zone = fema.get("flood_zone", "")
    risk = fema.get("risk_level", "unknown")
    if fema.get("flood_zone_confirmed"):
        if fema.get("sfha"):
            ev["environmental_exposure"].append(
                f"FEMA Zone {zone} — SFHA, mandatory flood insurance (source: OpenFEMA NFHL)")
        else:
            ev["environmental_exposure"].append(
                f"FEMA Zone {zone} — {risk} flood risk (source: OpenFEMA NFHL)")
    else:
        ev["environmental_exposure"].append("No FEMA flood zone data for this location")

    bfe_margin = usgs.get("bfe_margin_feet")
    if bfe_margin is not None:
        if bfe_margin < 0:
            ev["environmental_exposure"].append(
                f"Property is {abs(bfe_margin):.1f}ft BELOW base flood elevation (source: USGS/FEMA)")
        elif bfe_margin < 3:
            ev["environmental_exposure"].append(
                f"Only {bfe_margin:.1f}ft above base flood elevation — minimal margin (source: USGS/FEMA)")
        else:
            ev["environmental_exposure"].append(
                f"{bfe_margin:.1f}ft above base flood elevation (source: USGS/FEMA)")

    t1 = epa.get("tier1_count", 0)
    total_fac = epa.get("facility_count_total", 0)
    if total_fac == 0:
        ev["environmental_exposure"].append(
            "No EPA-regulated facilities within 3 miles (source: EPA ECHO)")
    elif t1 > 0:
        ev["environmental_exposure"].append(
            f"{t1} Tier-1 EPA hazardous site(s) within 3 miles (source: EPA ECHO)")
    else:
        ev["environmental_exposure"].append(
            f"{total_fac} EPA facilities nearby, no Tier-1 hazards (source: EPA ECHO)")

    # ── Infrastructure risk ────────────────────────────────
    near        = osm.get("within_300m",  {}) or {}
    far         = osm.get("within_1000m", {}) or {}
    major_roads = osm.get("major_roads",  {}) or {}
    amenities   = osm.get("amenities",    {}) or {}
    traffic     = raw_data.get("traffic", {}) or {}
    aadt_data   = traffic.get("aadt_estimates",    {}) or {}
    crash       = traffic.get("crash_summary",     {}) or {}
    timelines   = traffic.get("timeline_patterns", []) or []

    # Major roads with AADT + noise estimates
    road_class_label = {"motorway": "Interstate/motorway", "trunk": "US highway",
                        "primary": "Primary road (US/state route)", "secondary": "Secondary road"}
    for cls in ["motorway", "trunk", "primary", "secondary"]:
        rd = major_roads.get(cls, {})
        if rd.get("count", 0) > 0:
            nm    = rd.get("nearest_m")
            names = ", ".join(rd.get("names", [])[:2]) or cls
            dist_txt = f"{int(nm)}m" if nm else "within 2km"
            aadt_info = aadt_data.get(cls, {})
            aadt_val  = aadt_info.get("estimated_aadt")
            noise_db  = aadt_info.get("noise_db_at_property")
            extra = ""
            if aadt_val:
                extra += f", est. {aadt_val:,} vehicles/day"
            if noise_db:
                extra += f", ~{noise_db} dB at property"
            ev["infrastructure_risk"].append(
                f"{road_class_label.get(cls, cls)}: {names} at {dist_txt}{extra} (source: OpenStreetMap)")

    # Crash history
    if "error" not in crash and crash.get("total_crashes") is not None:
        total   = crash["total_crashes"]
        fatals  = crash.get("fatal_crashes", 0)
        if total > 0:
            fatal_txt = f" incl. {fatals} fatal" if fatals > 0 else ""
            ev["infrastructure_risk"].append(
                f"{total} crashes within 0.5mi, 2019–2023{fatal_txt} (source: NHTSA FARS)")
        else:
            ev["infrastructure_risk"].append(
                "0 recorded crashes within 0.5mi, 2019–2023 (source: NHTSA FARS)")

    # Timeline pattern (first one, most relevant)
    if timelines:
        ev["infrastructure_risk"].append(timelines[0])

    for cat, label in [("railway", "Railway"), ("power_line", "Power line"),
                       ("industrial", "Industrial zone"), ("landfill", "Landfill")]:
        d = near.get(cat) or far.get(cat)
        if d and d.get("count", 0) > 0:
            nm = d.get("nearest_m")
            ev["infrastructure_risk"].append(
                f"{label} {'at ' + str(int(nm)) + 'm' if nm else 'within 1km'} (source: OpenStreetMap)")

    if not major_roads and not any((near.get(c) or far.get(c) or {}).get("count") for c in ["railway","highway"]):
        ev["infrastructure_risk"].append("No major roads or railway within 1km (source: OpenStreetMap)")

    noise  = osm.get("noise_score",  0)
    hazard = osm.get("hazard_score", 0)
    ev["infrastructure_risk"].append(
        f"Noise score {noise}/100, hazard score {hazard}/100 (source: OpenStreetMap)")

    # Amenities → livability
    for cat, label in [("school","School"), ("park","Park"), ("transit_stop","Transit stop"),
                       ("grocery","Grocery store"), ("healthcare","Healthcare")]:
        a = amenities.get(cat, {})
        if a.get("count", 0) > 0:
            nm = a.get("nearest_m")
            ev["livability"].append(
                f"{label} {'at ' + str(int(nm)) + 'm' if nm else 'nearby'} (source: OpenStreetMap)")

    # ── Neighborhood stability ─────────────────────────────
    income = census.get("median_household_income")
    if income and income > 0:
        diff = int((income / 74_755 - 1) * 100)
        sign = "+" if diff >= 0 else ""
        ev["neighborhood_stability"].append(
            f"Median household income ${income:,} ({sign}{diff}% vs national $74,755) (source: US Census ACS 2022)")

    unemp = census.get("unemployment_rate_pct")
    if unemp is not None:
        ev["neighborhood_stability"].append(
            f"Unemployment {unemp:.1f}% vs national avg 4.0% (source: US Census ACS 2022)")

    vacancy = census.get("vacancy_rate_pct")
    if vacancy is not None:
        ev["neighborhood_stability"].append(
            f"Vacancy rate {vacancy:.1f}% vs national avg 9.2% (source: US Census ACS 2022)")

    owner = census.get("owner_occupancy_pct")
    if owner is not None:
        ev["neighborhood_stability"].append(
            f"Owner occupancy {owner:.1f}% vs national 64.8% (source: US Census ACS 2022)")

    # ── Livability ─────────────────────────────────────────
    elev_ft = usgs.get("elevation_feet")
    terrain = usgs.get("terrain_type", "")
    elev_score = usgs.get("elevation_score", 50)
    if elev_ft:
        ev["livability"].append(
            f"Elevation {int(elev_ft)}ft, terrain: {terrain} (source: USGS EPQS)")
    ev["livability"].append(
        f"Elevation score {elev_score}/100 (source: USGS 9-point grid analysis)")

    if income and income >= 74_755:
        ev["livability"].append("Above-average income neighborhood (source: US Census)")
    if total_fac == 0:
        ev["livability"].append("Clean environment — no EPA facilities nearby (source: EPA ECHO)")
    noise_ok = noise < 25
    if noise_ok:
        ev["livability"].append(f"Low noise environment, score {noise}/100 (source: OpenStreetMap)")

    # ── Hidden risk ────────────────────────────────────────
    if fema.get("sfha"):
        ev["hidden_risk"].append(
            "SFHA flood zone — mandatory flood insurance adds $700-3,200/yr (source: FEMA)")
    else:
        ev["hidden_risk"].append("No SFHA — flood insurance not mandatory (source: FEMA NFHL)")

    if t1 > 0:
        ev["hidden_risk"].append(
            f"{t1} Tier-1 hazardous site(s) in area — environmental liability risk (source: EPA ECHO)")
    else:
        ev["hidden_risk"].append("No Tier-1 EPA hazardous sites within 3 miles (source: EPA ECHO)")

    age = census.get("housing_age_years")
    if age and age > 50:
        ev["hidden_risk"].append(
            f"Housing stock median age {age} yrs — potential deferred maintenance (source: US Census)")

    # Merge: if LLM provided evidence for a score, prefer it (it's more contextual)
    if llm_evidence and isinstance(llm_evidence, dict):
        for k in ev:
            llm_bullets = llm_evidence.get(k) or []
            if len(llm_bullets) >= 2:
                ev[k] = llm_bullets  # LLM wins if it gave at least 2 bullets

    # Cap at 4 bullets per score
    for k in ev:
        ev[k] = ev[k][:4]

    return ev


def _build_deterministic_risk_items(raw_data: dict) -> list[RiskItem]:
    """
    Generate guaranteed RiskItem entries for major roads, FEMA, and EPA hazards
    directly from raw_data — no LLM required. These fill gaps when agents fail or
    when the LLM overlooks an obvious risk (e.g. Route 9 near Natick).
    """
    items: list[RiskItem] = []
    osm     = raw_data.get("osm",     {}) or {}
    fema    = raw_data.get("fema",    {}) or {}
    epa     = raw_data.get("epa",     {}) or {}
    traffic = raw_data.get("traffic", {}) or {}
    major_roads = osm.get("major_roads", {}) or {}
    aadt_data   = traffic.get("aadt_estimates", {}) or {}
    crash       = traffic.get("crash_summary",  {}) or {}
    timelines   = traffic.get("timeline_patterns", []) or []

    # ── Major road risks ──────────────────────────────────────
    road_meta = {
        "motorway": ("Interstate / Motorway",
                     lambda d: "critical" if d < 200 else "high" if d < 800 else "medium"),
        "trunk":    ("US Highway",
                     lambda d: "high" if d < 400 else "medium" if d < 1200 else "low"),
        "primary":  ("Primary Road (US/State Route)",
                     lambda d: "high" if d < 300 else "medium" if d < 800 else "low"),
        "secondary":("Secondary Road",
                     lambda d: "medium" if d < 200 else "low"),
    }
    for cls, (label, sev_fn) in road_meta.items():
        rd = major_roads.get(cls, {})
        if rd.get("count", 0) == 0:
            continue
        nm = rd.get("nearest_m")
        if nm is None:
            continue
        dist_m = float(nm)
        names  = rd.get("names", [])
        name_str = ", ".join(names[:2]) if names else cls
        sev = sev_fn(dist_m)

        evidence = [
            f"{label} '{name_str}' at {int(dist_m)}m (source: OpenStreetMap)"
        ]
        t = aadt_data.get(cls, {})
        if t.get("estimated_aadt"):
            aadt_val  = t["estimated_aadt"]
            peak      = t.get("peak_hour_vehicles", round(aadt_val * 0.10))
            noise_db  = t.get("noise_db_at_property")
            evidence.append(
                f"Est. {aadt_val:,} vehicles/day; peak-hour ~{peak:,} vehicles "
                f"(source: road class AADT estimate)")
            if noise_db:
                evidence.append(
                    f"Estimated noise at property: ~{noise_db} dB "
                    f"(source: distance-attenuated calculation)")
        if "error" not in crash and crash.get("total_crashes") is not None:
            total  = crash["total_crashes"]
            fatals = crash.get("fatal_crashes", 0)
            if total > 0:
                fatal_txt = f", {fatals} fatal" if fatals else ""
                evidence.append(
                    f"{total} recorded crashes within 0.5mi, 2019–2023{fatal_txt} "
                    f"(source: NHTSA FARS)")
        if timelines:
            evidence.append(timelines[0])

        items.append(RiskItem(
            category=f"traffic_{cls}_road",
            severity=sev,
            description=(
                f"{label} '{name_str}' is {int(dist_m)}m from property — "
                f"ongoing traffic noise, air-quality impact, and pedestrian safety risk"
            ),
            evidence=evidence,
            confidence=88,
            timeline="ongoing",
        ))

    # ── FEMA flood risk ───────────────────────────────────────
    if fema.get("sfha"):
        zone = fema.get("flood_zone", "")
        bfe  = fema.get("bfe_feet")
        bfe_txt = f"; BFE {bfe} ft NAVD88" if bfe else ""
        items.append(RiskItem(
            category="flood_risk_sfha",
            severity="high",
            description=(
                f"Property is in FEMA Special Flood Hazard Area (Zone {zone})"
                f"{bfe_txt} — mandatory flood insurance required"
            ),
            evidence=[
                f"FEMA NFHL Zone {zone}: 1%-annual-chance flood (source: OpenFEMA NFHL)",
                "SFHA designation = mandatory flood insurance on federally-backed mortgages",
            ],
            confidence=92,
            timeline="ongoing",
        ))

    # ── EPA Tier-1 risk ───────────────────────────────────────
    t1_facilities = [
        f for f in (epa.get("top_facilities") or [])
        if f.get("tier") == 1
    ]
    for fac in t1_facilities[:2]:
        dist_mi = fac.get("distance_miles", 0) or 0
        sev = "critical" if dist_mi < 0.5 else "high"
        items.append(RiskItem(
            category="epa_tier1_hazard",
            severity=sev,
            description=(
                f"Tier-1 EPA hazardous site '{fac.get('name', 'facility')}' "
                f"at {dist_mi:.1f} mi — Superfund/toxic release risk"
            ),
            evidence=[
                f"EPA ECHO: '{fac.get('name')}' at {dist_mi:.1f} mi "
                f"(Superfund={fac.get('superfund')}, "
                f"violations={fac.get('violation_count', 0)}) "
                f"(source: EPA ECHO)",
            ],
            confidence=90,
            timeline="ongoing",
        ))

    return items


def _build_hidden_costs(raw_data: dict, llm_costs: list) -> list[HiddenCost]:
    """Deterministic hidden costs from raw data, supplemented by LLM."""
    costs: list[HiddenCost] = []
    names_seen: set[str] = set()

    def _add(c: HiddenCost):
        key = c.name.lower()
        if key not in names_seen:
            names_seen.add(key)
            costs.append(c)

    fema   = raw_data.get("fema",   {}) or {}
    epa    = raw_data.get("epa",    {}) or {}
    osm    = raw_data.get("osm",    {}) or {}
    usgs   = raw_data.get("usgs",   {}) or {}
    census = raw_data.get("census", {}) or {}

    # ── FEMA ──────────────────────────────────────────────
    if fema.get("sfha"):
        _add(HiddenCost(name="Mandatory Flood Insurance (NFIP)", category="insurance",
            annual_low=700, annual_high=3200, likelihood="confirmed",
            basis=f"SFHA Zone {fema.get('flood_zone','')} — required on all federally-backed mortgages (source: FEMA)"))
    elif fema.get("risk_level") in ("moderate", "high"):
        _add(HiddenCost(name="Flood Insurance (Voluntary)", category="insurance",
            annual_low=300, annual_high=900, likelihood="likely",
            basis=f"Flood Zone {fema.get('flood_zone','')} ({fema.get('risk_level','')}) — lenders often require it even outside SFHA (source: FEMA)"))

    # Terrain bowl + low elevation → sump pump
    if usgs.get("terrain_type") == "bowl" or usgs.get("property_is_low_point"):
        _add(HiddenCost(name="Sump Pump Maintenance & Backup Power", category="maintenance",
            annual_low=200, annual_high=600, likelihood="likely",
            basis="Bowl terrain / low point — sump pump needed to prevent basement flooding (source: USGS elevation analysis)"))

    # ── EPA ────────────────────────────────────────────────
    t1_close = any(
        f.get("tier") == 1 and (f.get("distance_miles") or 99) <= 1.0
        for f in (epa.get("top_facilities") or []))
    if t1_close:
        _add(HiddenCost(name="Annual Water Quality Testing", category="service",
            annual_low=150, annual_high=400, likelihood="confirmed",
            basis="Tier-1 EPA hazardous facility within 1 mile — independent testing strongly advised (source: EPA ECHO)"))

    any_fac_close = (epa.get("facility_count_half_mile") or 0) > 0
    if any_fac_close and not t1_close:
        _add(HiddenCost(name="Air Quality / HVAC Filter Upgrades", category="maintenance",
            annual_low=100, annual_high=300, likelihood="possible",
            basis="EPA-regulated facility within 0.5 miles — enhanced air filtration recommended (source: EPA ECHO)"))

    # ── OSM ────────────────────────────────────────────────
    near = osm.get("within_300m", {}) or {}
    far  = osm.get("within_1000m", {}) or {}

    landfill = near.get("landfill") or far.get("landfill")
    if landfill and (landfill.get("count") or 0) > 0:
        _add(HiddenCost(name="Air & Well Water Testing (Landfill)", category="service",
            annual_low=200, annual_high=500, likelihood="likely",
            basis=f"Landfill within {int(landfill.get('nearest_m',1000))}m — methane and leachate monitoring advised (source: OpenStreetMap)"))

    # No municipal waste infrastructure found → private waste removal
    waste_infra = (near.get("fuel_station") or {}).get("count", 0)  # weak proxy
    pop = census.get("population", 99999)
    if pop < 5000:  # rural / small town tract
        _add(HiddenCost(name="Private Trash / Waste Removal", category="service",
            annual_low=300, annual_high=700, likelihood="likely",
            basis=f"Census tract population {pop:,} — rural/small town areas often lack municipal waste service (source: US Census ACS 2022)"))
        _add(HiddenCost(name="Private Well Pump Maintenance & Testing", category="utility",
            annual_low=200, annual_high=500, likelihood="likely",
            basis="Low-density area — municipal water connection may not be available (source: US Census ACS 2022)"))
        _add(HiddenCost(name="Septic System Pumping (amortized)", category="utility",
            annual_low=75, annual_high=150, likelihood="likely",
            basis="Rural tract — likely on septic system; pump every 3-5 years + annual inspection (source: US Census ACS 2022)"))

    # ── Census ─────────────────────────────────────────────
    age = census.get("housing_age_years")
    if age and age > 50:
        _add(HiddenCost(name="Older Housing Maintenance Reserve", category="maintenance",
            annual_low=3000, annual_high=8000, likelihood="possible",
            basis=f"Median housing age {age} yrs — expect higher costs for plumbing, electrical, roof (source: US Census ACS 2022)"))

    # ── LLM additions ──────────────────────────────────────
    for raw in (llm_costs or []):
        if isinstance(raw, dict):
            try:
                _add(HiddenCost(
                    name=raw.get("name", "Unknown"),
                    category=raw.get("category", "service"),
                    annual_low=raw.get("annual_low"),
                    annual_high=raw.get("annual_high"),
                    likelihood=raw.get("likelihood", "possible"),
                    basis=raw.get("basis", ""),
                ))
            except Exception:
                pass

    lik_order = {"confirmed": 0, "likely": 1, "possible": 2}
    return sorted(costs, key=lambda c: lik_order.get(c.likelihood, 9))


@app.post("/api/analyze", response_model=AnalyzeResponse)
async def analyze(req: AnalyzeRequest):
    settings = get_settings()

    cache_key = f"assessment:v8:{hashlib.md5(req.address.encode()).hexdigest()}:{req.mode}"
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
        "hidden_costs": None,
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

    # Score evidence: deterministic from raw data, LLM enhances if it provided ≥2 bullets
    ev_dict = _build_score_evidence(raw_data, final_state.get("score_evidence"))
    score_evidence = ScoreEvidence(**{k: ev_dict.get(k, []) for k in ScoreEvidence.model_fields})

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

    # Hidden costs: LLM + deterministic from raw data
    hidden_costs = _build_hidden_costs(
        raw_data, final_state.get("hidden_costs") or []
    )

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

    # Merge deterministic risks — add any road/hazard class not already covered by LLM
    det_risks = _build_deterministic_risk_items(raw_data)
    llm_categories = {r.category for r in risks}
    _traffic_keywords = {"traffic", "highway", "road", "noise", "motorway", "trunk", "primary"}
    _flood_keywords   = {"flood"}
    _epa_keywords     = {"epa", "tier", "hazard", "superfund", "pollution"}

    def _covered(det_cat: str, llm_cats: set[str]) -> bool:
        words = set(det_cat.lower().split("_"))
        for llm_cat in llm_cats:
            llm_words = set(llm_cat.lower().split("_"))
            if words & llm_words & (_traffic_keywords | _flood_keywords | _epa_keywords):
                return True
        return False

    for dr in det_risks:
        if not _covered(dr.category, llm_categories):
            risks.append(dr)

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
        hidden_costs=hidden_costs,
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
