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
    lat: Optional[float] = None  # skip geocoding when provided
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


class AnalyzeResponse(BaseModel):
    assessment_id: str
    address: str
    lat: float
    lon: float
    mode: str
    scores: ScoreSet
    risks: List[RiskItem]
    narrative: str
    mode_advice: str
    data_sources: List[str]
    overall_confidence: int
