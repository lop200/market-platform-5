"""Unified, trusted news layer."""

from app.news.schemas import NewsEvent
from app.news.service import UnifiedNewsService

__all__ = ["NewsEvent", "UnifiedNewsService"]
