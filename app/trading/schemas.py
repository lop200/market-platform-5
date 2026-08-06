from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class PaperOrderRequest(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=80)
    side: Literal["buy", "sell"]
    instrument_type: Literal["stock", "option"]
    symbol: str = Field(min_length=1, max_length=64)
    quantity: int = Field(gt=0, le=100_000)
    limit_price: float = Field(gt=0)
    take_profit: float | None = Field(default=None, gt=0)
    stop_loss: float | None = Field(default=None, gt=0)
    trailing_stop_pct: float | None = Field(default=None, gt=0, le=50)
    profit_protection_trigger_pct: float | None = Field(default=None, gt=0, le=100)
    simulated_fill_quantity: int | None = Field(default=None, gt=0)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        symbol = value.upper().strip()
        if not symbol or not all(ch.isalnum() or ch in ".-_" for ch in symbol):
            raise ValueError("invalid symbol")
        return symbol


class BridgePosition(BaseModel):
    instrument_type: Literal["stock", "option"]
    symbol: str
    underlying_symbol: str
    quantity: int = Field(ge=0)
    average_price: float = Field(ge=0)
    current_price: float = Field(ge=0)


class BridgeQuote(BaseModel):
    instrument_type: Literal["stock", "option"]
    symbol: str
    bid: float = Field(gt=0)
    ask: float = Field(gt=0)
    as_of: datetime
    source: str = "Sahm Bridge"


class SahmBridgePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cash: float = Field(ge=0)
    buying_power: float = Field(ge=0)
    daily_pnl: float = 0
    positions: list[BridgePosition] = Field(default_factory=list, max_length=500)
    orders: list[dict] = Field(default_factory=list, max_length=500)
    quotes: list[BridgeQuote] = Field(default_factory=list, max_length=500)
    captured_at: datetime


class TradeIntentEdit(BaseModel):
    """Fields the user may change after deterministic analysis and before preview."""

    quantity: int = Field(gt=0, le=100_000)
    limit_price: float = Field(gt=0)
    take_profit: float = Field(gt=0)
    stop_loss: float = Field(gt=0)
    time_in_force: Literal["day", "gtc"] = "day"


class IntentSizingRequest(BaseModel):
    """Numeric portfolio inputs only; no broker credentials or account identifiers."""

    buying_power: float = Field(gt=0, le=100_000_000)
    risk_pct: float = Field(default=1.0, gt=0, le=5.0)
    max_trade_value: float | None = Field(default=None, gt=0, le=100_000_000)


class ExtensionExecutionEvent(BaseModel):
    status: Literal[
        "awaiting_user_confirmation", "submitted", "open", "partially_filled",
        "filled", "rejected", "cancelled", "error", "disconnected",
    ]
    message: str = Field(min_length=1, max_length=300)
    filled_quantity: int = Field(default=0, ge=0)
    observed_at: datetime
