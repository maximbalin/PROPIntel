import asyncio
import httpx
import logging
from backend.config import get_settings

logger = logging.getLogger(__name__)

CENSUS_GEOCODER_URL = "https://geocoding.geo.census.gov/geocoder/geographies/coordinates"
CENSUS_ACS_URL = "https://api.census.gov/data/2022/acs/acs5"

# US national benchmarks — ACS 2022 5-year estimates
NATIONAL = {
    "median_income":       74_755,
    "vacancy_rate_pct":    9.2,
    "unemployment_rate_pct": 4.0,
    "poverty_rate_pct":    11.5,
    "owner_occupancy_pct": 64.8,
    "gini_index":          0.489,
}

# All variables in one ACS call
ACS_VARS = ",".join([
    "B19013_001E",   # median household income
    "B01003_001E",   # total population
    "B17001_002E",   # population below poverty line
    "B25001_001E",   # total housing units
    "B25002_003E",   # vacant housing units
    "B25003_002E",   # owner-occupied units
    "B25003_003E",   # renter-occupied units
    "B25035_001E",   # median year structure built
    "B23025_003E",   # civilian labor force
    "B23025_005E",   # unemployed
    "B15003_022E",   # bachelor's degree holders (25+)
    "B15003_001E",   # total population 25+ (denominator for education)
    "B19083_001E",   # Gini index of income inequality
    "B25077_001E",   # median home value (owner-occupied)
])


def _safe_int(val, default: int = 0) -> int:
    try:
        v = int(val)
        return v if v >= 0 else default
    except (TypeError, ValueError):
        return default


def _safe_float(val, default: float = 0.0) -> float:
    try:
        v = float(val)
        return v if v >= 0 else default
    except (TypeError, ValueError):
        return default


def _rate(numerator: int, denominator: int, decimals: int = 1) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator * 100, decimals)


def _vs_national(value: float | None, benchmark: float, label: str) -> str:
    """Human-readable comparison to national benchmark."""
    if value is None:
        return f"{label}: data unavailable"
    ratio = value / benchmark if benchmark else 0
    if ratio >= 1.5:
        return f"{label} {value:.1f}% — {ratio:.1f}× national avg ({benchmark}%)"
    if ratio >= 1.1:
        return f"{label} {value:.1f}% — above national avg ({benchmark}%)"
    if ratio >= 0.9:
        return f"{label} {value:.1f}% — near national avg ({benchmark}%)"
    return f"{label} {value:.1f}% — below national avg ({benchmark}%)"


def _compute_stability_score(
    median_income: int,
    vacancy_rate: float | None,
    unemployment_rate: float | None,
    poverty_rate: float | None,
    owner_occupancy_rate: float | None,
    gini: float | None,
) -> int:
    """
    Stability score 0–100 (higher = more stable neighborhood).
    Each component scored 0–100 then weighted.
    """
    def income_score(inc: int) -> int:
        # national avg ($74,755) → 50; 2× → 100; 0.5× → 25
        if inc <= 0:
            return 50
        ratio = inc / NATIONAL["median_income"]
        return max(0, min(100, int(ratio * 50)))

    def rate_score(rate: float | None, national: float) -> int:
        # Lower rate = better. national avg → 50; 0% → 100; 2× avg → 0
        if rate is None:
            return 50
        ratio = rate / national if national else 1
        return max(0, min(100, int((2 - ratio) * 50)))

    def gini_score(g: float | None) -> int:
        if g is None:
            return 50
        # Gini 0=perfect equality, 1=max inequality. US avg ~0.49.
        return max(0, min(100, int((1 - g) * 100)))

    components = {
        "income":       (income_score(median_income),                                      0.30),
        "unemployment": (rate_score(unemployment_rate, NATIONAL["unemployment_rate_pct"]), 0.25),
        "poverty":      (rate_score(poverty_rate,      NATIONAL["poverty_rate_pct"]),      0.20),
        "vacancy":      (rate_score(vacancy_rate,      NATIONAL["vacancy_rate_pct"]),      0.15),
        "gini":         (gini_score(gini),                                                 0.10),
    }

    score = sum(s * w for s, w in components.values())
    return max(0, min(100, round(score)))


async def _get_fips(client: httpx.AsyncClient, lat: float, lon: float) -> tuple[str, str, str] | None:
    """Return (state, county, tract) FIPS codes for a lat/lon."""
    params = {
        "x": lon,
        "y": lat,
        "benchmark": "Public_AR_Current",
        "vintage": "Current_Current",
        "layers": "Census Tracts",
        "format": "json",
    }
    resp = await client.get(CENSUS_GEOCODER_URL, params=params)
    resp.raise_for_status()
    data = resp.json()
    tracts = data.get("result", {}).get("geographies", {}).get("Census Tracts", [])
    if not tracts:
        return None
    t = tracts[0]
    return t.get("STATE"), t.get("COUNTY"), t.get("TRACT")


async def _get_acs(client: httpx.AsyncClient, state: str, county: str, tract: str) -> dict:
    """Fetch all ACS variables for a census tract."""
    settings = get_settings()
    params = {
        "get":  ACS_VARS,
        "for":  f"tract:{tract}",
        "in":   f"state:{state}+county:{county}",
    }
    if settings.census_api_key:
        params["key"] = settings.census_api_key
    resp = await client.get(CENSUS_ACS_URL, params=params)
    resp.raise_for_status()
    data = resp.json()
    if len(data) < 2:
        raise ValueError("No ACS rows returned")
    return dict(zip(data[0], data[1]))


async def get_demographics(lat: float, lon: float) -> dict:
    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            fips = await _get_fips(client, lat, lon)
            if not fips:
                return {"error": "No census tract found", "source": "US Census ACS 2022"}

            state, county, tract = fips
            row = await _get_acs(client, state, county, tract)

        # ── Raw counts ────────────────────────────────────────────────
        median_home_value = _safe_int(row.get("B25077_001E"), -1)
        median_income    = _safe_int(row.get("B19013_001E"), -1)
        population       = _safe_int(row.get("B01003_001E"))
        poverty_pop      = _safe_int(row.get("B17001_002E"))
        total_units      = _safe_int(row.get("B25001_001E"))
        vacant_units     = _safe_int(row.get("B25002_003E"))
        owner_units      = _safe_int(row.get("B25003_002E"))
        renter_units     = _safe_int(row.get("B25003_003E"))
        median_yr_built  = _safe_int(row.get("B25035_001E"))
        labor_force      = _safe_int(row.get("B23025_003E"))
        unemployed       = _safe_int(row.get("B23025_005E"))
        bachelors        = _safe_int(row.get("B15003_022E"))
        pop_25_plus      = _safe_int(row.get("B15003_001E"))
        gini_raw         = _safe_float(row.get("B19083_001E"), -1)
        gini             = gini_raw if 0 < gini_raw <= 1 else None

        # ── Derived rates ─────────────────────────────────────────────
        vacancy_rate       = _rate(vacant_units, total_units)
        unemployment_rate  = _rate(unemployed, labor_force)
        poverty_rate       = _rate(poverty_pop, population)
        owner_occupancy    = _rate(owner_units, owner_units + renter_units)
        bachelors_rate     = _rate(bachelors, pop_25_plus)
        housing_age_years  = (2022 - median_yr_built) if median_yr_built > 1800 else None

        # ── Benchmark comparisons ─────────────────────────────────────
        benchmarks = {
            "income_vs_national":       f"${median_income:,} median HH income vs national ${NATIONAL['median_income']:,}",
            "vacancy_vs_national":      _vs_national(vacancy_rate,      NATIONAL["vacancy_rate_pct"],       "Vacancy rate"),
            "unemployment_vs_national": _vs_national(unemployment_rate, NATIONAL["unemployment_rate_pct"],  "Unemployment rate"),
            "poverty_vs_national":      _vs_national(poverty_rate,      NATIONAL["poverty_rate_pct"],       "Poverty rate"),
            "owner_occupancy_vs_national": _vs_national(owner_occupancy, NATIONAL["owner_occupancy_pct"],   "Owner occupancy"),
        }

        # ── Stability score ───────────────────────────────────────────
        stability_score = _compute_stability_score(
            median_income, vacancy_rate, unemployment_rate,
            poverty_rate, owner_occupancy, gini,
        )

        return {
            # Identifiers
            "state": state, "county": county, "tract": tract,

            # Raw counts
            "population":        population,
            "total_units":       total_units,
            "vacant_units":      vacant_units,
            "owner_units":       owner_units,
            "renter_units":      renter_units,
            "unemployed":        unemployed,
            "labor_force":       labor_force,

            # Key indicators
            "median_home_value":        median_home_value if median_home_value > 0 else None,
            "median_household_income":  median_income,
            "median_year_built":        median_yr_built if median_yr_built > 1800 else None,
            "housing_age_years":        housing_age_years,
            "gini_index":               gini,

            # Derived rates
            "vacancy_rate_pct":       vacancy_rate,
            "unemployment_rate_pct":  unemployment_rate,
            "poverty_rate_pct":       poverty_rate,
            "owner_occupancy_pct":    owner_occupancy,
            "bachelors_rate_pct":     bachelors_rate,

            # Benchmark comparisons (ready for agent to quote)
            "benchmarks":         benchmarks,
            "national_benchmarks": NATIONAL,

            # Pre-computed score
            "stability_score":    stability_score,

            "source": "US Census ACS 2022",
        }

    except Exception as e:
        logger.warning(f"Census data fetch failed: {e}")
        return {"error": str(e), "source": "US Census ACS 2022"}
