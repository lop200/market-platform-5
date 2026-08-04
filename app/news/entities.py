"""Deterministic company-news entity matching.

Provider category endpoints are treated as candidate generators, not proof that
every returned story belongs to the requested ticker.
"""
from __future__ import annotations

import re

from app.static_data.us_symbols import US_SYMBOLS


_COMPANY_NAMES = {symbol: name for symbol, name in US_SYMBOLS}
_ALIASES: dict[str, tuple[str, ...]] = {
    "NVDA": ("nvidia", "nvidia corporation", "jensen huang"),
    "PLTR": ("palantir", "palantir technologies", "alex karp", "alexander karp"),
}
_CORPORATE_SUFFIXES = {
    "class", "corp", "corporation", "company", "holdings", "inc", "incorporated",
    "limited", "ltd", "plc", "technologies", "group",
}


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _name_aliases(symbol: str) -> set[str]:
    aliases = {_normalize(value) for value in _ALIASES.get(symbol, ())}
    company = _normalize(_COMPANY_NAMES.get(symbol, ""))
    if company:
        aliases.add(company)
        meaningful = " ".join(
            token for token in company.split()
            if token not in _CORPORATE_SUFFIXES
        )
        if meaningful:
            aliases.add(meaningful)
    return {alias for alias in aliases if len(alias) >= 3}


def company_story_matches(
    symbol: str,
    headline: str,
    summary: str = "",
    *,
    related: str | list[str] | None = None,
) -> tuple[bool, str]:
    """Require direct ticker/entity evidence before assigning a company story."""
    requested = symbol.upper().strip()
    headline_text = _normalize(headline)
    related_symbols = {
        value.upper().strip()
        for value in (
            re.split(r"[,;\s]+", related) if isinstance(related, str) else related or []
        )
        if value
    }
    if re.search(rf"(?<![a-z0-9])\$?{re.escape(requested.lower())}(?![a-z0-9])", headline_text):
        return True, f"headline contains ticker {requested}"
    for alias in sorted(_name_aliases(requested), key=len, reverse=True):
        if re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", headline_text):
            return True, f"headline contains company entity {alias}"
    metadata_note = (
        "provider metadata alone is insufficient"
        if requested in related_symbols
        else "provider metadata does not confirm the ticker"
    )
    return False, f"no headline ticker/company-entity evidence for {requested}; {metadata_note}"


def filter_company_events(events, symbol: str):
    """Revalidate live and cached events so legacy bad cache cannot leak back."""
    accepted = []
    for event in events:
        matches, reason = company_story_matches(symbol, event.headline, event.summary)
        if matches:
            event.symbols = [symbol.upper()]
            event.entity_match_reason = reason
            accepted.append(event)
    return accepted
