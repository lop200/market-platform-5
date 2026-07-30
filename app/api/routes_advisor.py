from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.advisor.schemas import AdvisorQuestion
from app.advisor.service import advisor_context, ask_advisor
from app.config import get_settings
from app.db.session import get_db

router = APIRouter(prefix="/api/v1/advisor", tags=["financial-advisor"])


@router.get("/context")
def get_advisor_context(
    symbol: str | None = Query(default=None, max_length=10),
    db: Session = Depends(get_db),
) -> dict:
    return advisor_context(db, get_settings(), symbol)


@router.post("/ask")
def post_advisor_question(
    payload: AdvisorQuestion,
    db: Session = Depends(get_db),
) -> dict:
    return ask_advisor(
        db,
        get_settings(),
        question=payload.question,
        symbol=payload.symbol,
    )

