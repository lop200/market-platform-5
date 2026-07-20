"""Auth + rate-limit dependencies (SRS 8, 18). Rate limiting itself lands with the web UI in M2."""
from __future__ import annotations

from fastapi import Header, HTTPException, status

from app.config import get_settings


def require_api_key(x_api_key: str = Header(...)) -> None:
    settings = get_settings()
    if x_api_key != settings.api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid API key")
