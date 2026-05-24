from pydantic import BaseModel
from typing import Optional, List
from enum import Enum


class Mode(str, Enum):
    buyer = "buyer"
    investor = "investor"


class AnalyzeRequest(BaseModel):
    address: str
    mode: Mode = Mode.buyer
    risk_tolerance: str = "medium"
    lat: Optional[float] = None
    lon: Optional[float] = None


class RiskItem(BaseModel):
    category: str
    severity: str
    description: str
    evidence: List[str]
    confidence: int
    timeline: Optional[str] = None


class ScoreSet(BaseModel):
    livability: int
    environmental_exposure: int
    infrastructure_risk: int
    neighborhood_stability: int
    hidden_risk: int


class ScoreBreakdown(BaseModel):
    env_raw:         int
    env_agent:       int
    infra_raw:       int
    infra_agent:     int
    nbhd_raw:        int
    nbhd_agent:      int
    elevation_score: int


class ScoreEvidence(BaseModel):
    livability:             List[str] = []
    environmental_exposure: List[str] = []
    infrastructure_risk:    List[str] = []
    neighborhood_stability: List[str] = []
    hidden_risk:            List[str] = []


class Recommendation(BaseModel):
    verdict:     str            # BUY / NEGOTIATE / CAUTION / AVOID
    score:       int
    summary:     str
    key_factors: List[str] = []


class PriceContext(BaseModel):
    area_median_value:    Optional[int] = None
    estimated_impact_pct: int = 0
    impact_drivers:       List[str] = []


class NearbyRisk(BaseModel):
    name:           str
    category:       str
    distance_label: str
    severity:       str
    distance_m:     Optional[float] = None
    bearing_deg:    Optional[float] = None


class ListingData(BaseModel):
    price:             Optional[int]   = None
    beds:              Optional[int]   = None
    baths:             Optional[float] = None
    sqft:              Optional[int]   = None
    year_built:        Optional[int]   = None
    property_type:     Optional[str]   = None
    lot_size_sqft:     Optional[int]   = None
    hoa_fee_monthly:   Optional[float] = None
    tax_annual:        Optional[float] = None
    status:            Optional[str]   = None
    days_on_market:    Optional[int]   = None
    garage_spaces:     Optional[int]   = None
    heating_cooling:   Optional[str]   = None
    description:       Optional[str]   = None
    listing_url:       Optional[str]   = None
    external_links:    Optional[dict]  = None
    links_only:        bool            = False
    photos:            List[str]       = []
    source:            Optional[str]   = None
    error:             Optional[str]   = None
    # Assessor-sourced fields (public records, shown when live listing unavailable)
    assessed_total:    Optional[int]   = None
    assessed_building: Optional[int]   = None
    assessed_land:     Optional[int]   = None
    assessment_year:   Optional[int]   = None
    last_sale_price:   Optional[int]   = None
    last_sale_date:    Optional[str]   = None
    style:             Optional[str]   = None
    heat_type:         Optional[str]   = None
    fuel_type:         Optional[str]   = None
    total_rooms:       Optional[int]   = None
    stories:           Optional[int]   = None
    owner:             Optional[str]   = None
    assessor_source:   Optional[str]   = None


class HiddenCost(BaseModel):
    name:             str
    category:         str            # insurance / utility / service / maintenance / tax
    annual_low:       Optional[int] = None
    annual_high:      Optional[int] = None
    likelihood:       str            # confirmed / likely / possible
    basis:            str            # one-sentence explanation


class AnalyzeResponse(BaseModel):
    assessment_id:      str
    address:            str
    lat:                float
    lon:                float
    mode:               str
    scores:             ScoreSet
    score_breakdown:    Optional[ScoreBreakdown] = None
    score_evidence:     Optional[ScoreEvidence]  = None
    recommendation:     Optional[Recommendation] = None
    price_context:      Optional[PriceContext]   = None
    nearby_risks:       List[NearbyRisk]         = []
    hidden_costs:       List[HiddenCost]         = []
    listing_data:       Optional["ListingData"]  = None
    schools:            Optional[dict]           = None
    broadband:          Optional[dict]           = None
    risks:              List[RiskItem]
    narrative:          str
    mode_advice:        str
    data_sources:       List[str]
    overall_confidence: int
