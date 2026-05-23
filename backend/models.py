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
    risks:              List[RiskItem]
    narrative:          str
    mode_advice:        str
    data_sources:       List[str]
    overall_confidence: int
