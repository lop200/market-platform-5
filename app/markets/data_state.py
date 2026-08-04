from __future__ import annotations

from enum import StrEnum


class DataState(StrEnum):
    LIVE = "LIVE"
    VALIDATION_WARNING = "VALIDATION_WARNING"
    BLOCKED = "BLOCKED"
    STALE = "STALE"
    NO_DATA = "NO_DATA"


def resolve_data_state(
    *,
    primary_available: bool,
    primary_fresh: bool,
    blocked: bool = False,
    validator_status: str | None = None,
) -> DataState:
    if not primary_available:
        return DataState.NO_DATA
    if not primary_fresh:
        return DataState.STALE
    if blocked:
        return DataState.BLOCKED
    if validator_status in {
        "validation_warning", "stale", "unavailable",
        "external_stale", "external_unavailable",
    }:
        return DataState.VALIDATION_WARNING
    return DataState.LIVE
