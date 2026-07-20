from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db import models  # noqa: F401  (registers tables on Base.metadata)
from app.db.session import Base


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def test_settings():
    return Settings(
        database_url="sqlite://",
        default_daily_cap_usd=1.00,
        default_monthly_cap_usd=5.00,
        cost_anomaly_calls_per_minute=3,
    )
