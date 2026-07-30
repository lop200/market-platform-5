from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class AdvisorQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=2, max_length=500)
    symbol: str | None = Field(default=None, max_length=10)


class AdvisorExplanation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answer_ar: str
    conclusion_ar: str
    referenced_contract_symbols: list[str] = Field(
        default_factory=list, max_length=3
    )
    risk_warnings_ar: list[str] = Field(default_factory=list, max_length=4)

