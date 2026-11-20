from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints
from typing_extensions import Annotated

NonEmptyText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=2, max_length=160)
]


class UserCreate(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=100)]
    email: EmailStr
    password: Annotated[str, StringConstraints(min_length=12, max_length=128)]


class UserLogin(BaseModel):
    email: EmailStr
    password: Annotated[str, StringConstraints(min_length=1, max_length=128)]


class UserPublic(BaseModel):
    id: str
    name: str
    email: EmailStr
    role: Literal["farmer", "admin"]


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserPublic


class FarmerProfileInput(BaseModel):
    location: NonEmptyText
    preferred_crops: list[str] = Field(default_factory=list, max_length=20)
    land_area: float | None = Field(default=None, gt=0, le=1_000_000)
    soil_type: str | None = Field(default=None, max_length=80)
    irrigation_method: str | None = Field(default=None, max_length=80)


class FarmerProfile(FarmerProfileInput):
    user_id: str
    updated_at: datetime


class AdvisoryRequest(BaseModel):
    crop: NonEmptyText
    location: NonEmptyText
    crop_stage: Literal[
        "Sowing", "Seedling", "Vegetative", "Flowering", "Fruiting", "Maturity", "Harvest"
    ]
    land_area: float = Field(gt=0, le=1_000_000)
    soil_type: str | None = Field(default=None, max_length=80)
    irrigation_method: str | None = Field(default=None, max_length=80)


class SourceReference(BaseModel):
    title: str
    source: str
    url: str | None = None
    excerpt: str | None = None
    page: int | None = None
    retrieved_at: datetime | None = None


class WeatherFinding(BaseModel):
    available: bool
    status: Literal["available", "unavailable", "failed"]
    temperature_c: float | None = None
    humidity_percent: float | None = None
    rainfall_next_48h_mm: float | None = None
    precipitation_probability_percent: float | None = None
    forecast: str | None = None
    agricultural_impact: str | None = None
    source: str | None = None
    retrieved_at: datetime | None = None
    reason_unavailable: str | None = None


class MarketFinding(BaseModel):
    available: bool
    status: Literal["available", "unavailable", "failed"]
    crop: str
    market: str | None = None
    current_price: float | None = None
    unit: str | None = None
    trend: Literal["increasing", "decreasing", "stable", "insufficient_data", "unknown"] = "unknown"
    recommendation: Literal["sell", "hold", "monitor", "unavailable"] = "unavailable"
    reason: str
    source: str | None = None
    retrieved_at: datetime | None = None
    history_points: list[dict[str, Any]] = Field(default_factory=list)


class SchemeMatch(BaseModel):
    name: str
    eligibility: str
    benefits: str
    application_steps: list[str] = Field(default_factory=list)
    sources: list[SourceReference] = Field(default_factory=list)
    needs_verification: bool = True


class SchemeFinding(BaseModel):
    available: bool
    status: Literal["available", "unavailable", "failed"]
    matches: list[SchemeMatch] = Field(default_factory=list)
    retrieved_sources: list[SourceReference] = Field(default_factory=list)
    reason_unavailable: str | None = None


class CropFinding(BaseModel):
    field_action: str
    actions: list[str] = Field(default_factory=list)
    reasoning: str
    weather_considered: bool
    sources: list[SourceReference] = Field(default_factory=list)


class RiskFinding(BaseModel):
    risk_level: Literal["LOW", "MODERATE", "HIGH", "CRITICAL", "UNASSESSED"]
    risk_type: str | None = None
    description: str
    preventive_actions: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    sources: list[SourceReference] = Field(default_factory=list)


class FinalAdvisory(BaseModel):
    farmer_summary: str
    field_action: str
    weather_summary: str
    market_recommendation: str
    schemes: list[SchemeMatch] = Field(default_factory=list)
    risk_alerts: list[RiskFinding] = Field(default_factory=list)
    reasoning: str
    sources: list[SourceReference] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class AgentExecutionPublic(BaseModel):
    agent_name: str
    execution_status: Literal["pending", "running", "completed", "failed"]
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_ms: int | None = None
    result_summary: str | None = None
    error: str | None = None


class AdvisorySessionPublic(BaseModel):
    session_id: str
    crop: str
    location: str
    crop_stage: str
    created_at: datetime
    overall_status: Literal["pending", "running", "completed", "partial", "failed"]
    risk_level: str | None = None
    final_advisory: FinalAdvisory | None = None


class BlackboardPublic(BaseModel):
    model_config = ConfigDict(extra="allow")
    session_id: str
    farmer_input: dict[str, Any]
    findings: dict[str, Any]
    execution_status: dict[str, Any]
    final_advisory: dict[str, Any] | None = None
