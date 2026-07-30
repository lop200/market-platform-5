from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class NewsEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    source_type: str
    source_name: str
    source_url: str | None = None
    published_at: datetime
    received_at: datetime
    age_seconds: int = 0
    headline: str
    summary: str = ""
    raw_text: str = ""
    symbols: list[str] = Field(default_factory=list)
    sectors: list[str] = Field(default_factory=list)
    market_scope: str = "company"
    event_type: str = "other"
    sentiment: str = "neutral"
    impact_score: int = 0
    reliability_score: int = 0
    urgency_score: int = 0
    is_official: bool = False
    is_duplicate: bool = False
    language: str = "en"
    verified_at: datetime | None = None
    confirming_sources: list[dict] = Field(default_factory=list)
    conflict_warning: bool = False
    risk_flags: list[str] = Field(default_factory=list)
    supports_technical_scenario: bool | None = None
    contradicts_technical_scenario: bool | None = None
    prevent_entry: bool = False
    raise_risk: bool = False
    invalidates_previous_analysis: bool = False
    relation_reason_ar: str = ""
    status_message_ar: str = ""


class NewsUsage(BaseModel):
    news_api_calls: int = 0
    x_reads: int = 0
    sec_requests: int = 0
    cache_hits: int = 0
    openai_calls: int = 0
    estimated_cost: float = 0.0


class NewsSnapshot(BaseModel):
    items: list[dict] = Field(default_factory=list)
    updated_at: datetime | None = None
    source_status: dict[str, str] = Field(default_factory=dict)
    usage: dict = Field(default_factory=dict)
    is_stale: bool = False
    last_error: str | None = None
