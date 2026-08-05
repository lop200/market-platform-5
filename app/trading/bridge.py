from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any

from app.trading.schemas import SahmBridgePayload


FORBIDDEN_AUTH_KEYS = {
    "password", "passcode", "otp", "one_time_password", "cookie", "cookies",
    "session", "session_id", "access_token", "refresh_token",
}


class BrokerAdapter(ABC):
    """Boundary for browser bridges. Adapters normalize data but never execute orders."""

    name: str

    @abstractmethod
    def normalize_snapshot(self, raw: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


class SahmAdapter(BrokerAdapter):
    name = "sahm"

    def normalize_snapshot(self, raw: dict[str, Any]) -> dict[str, Any]:
        lowered = {str(key).lower() for key in raw}
        if lowered & FORBIDDEN_AUTH_KEYS:
            raise ValueError("authentication material is not accepted by Marsad Bridge")
        payload = SahmBridgePayload.model_validate(raw)
        captured = payload.captured_at
        if captured.tzinfo is None:
            captured = captured.replace(tzinfo=timezone.utc)
        return {
            "adapter": self.name,
            "account": {
                "cash": payload.cash,
                "buying_power": payload.buying_power,
                "daily_pnl": payload.daily_pnl,
            },
            "positions": [item.model_dump(mode="json") for item in payload.positions],
            "orders": payload.orders,
            "quotes": [item.model_dump(mode="json") for item in payload.quotes],
            "captured_at": captured.astimezone(timezone.utc),
        }
