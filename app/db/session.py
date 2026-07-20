"""SQLAlchemy engine/session setup. SQLite locally, Postgres in production (SRS AD-5)."""
from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


def _make_engine():
    settings = get_settings()
    url = settings.database_url
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, connect_args=connect_args, future=True)


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def init_db() -> None:
    """Ensure the schema exists and the cost_limits default row is seeded — run at every
    boot (app/main.py lifespan). Idempotent: create_all only creates MISSING tables, so it
    is safe alongside Alembic — on the recommended Postgres path `alembic upgrade head`
    runs first in the start command and this becomes a no-op for the schema, while still
    performing the default-row seeding that migrations don't do.

    This is the safety net that lets a fresh deploy serve requests even when the migration
    step was skipped — the cause of the first Render deploy's `no such table: cost_limits`
    error (2026-07-20). Seeds the daily/monthly caps from settings (SRS 16.2)."""
    from app.config import get_settings
    from app.db import models  # noqa: F401 — registers every table on Base.metadata
    from app.db import repository

    Base.metadata.create_all(bind=engine)

    settings = get_settings()
    db = SessionLocal()
    try:
        repository.get_or_create_cost_limits(
            db, settings.default_daily_cap_usd, settings.default_monthly_cap_usd
        )
    finally:
        db.close()


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
