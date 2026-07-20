"""POST /api/v1/analyze/option-image (Annex C-2, M2-B).

The cost-gate/vision/data-fetch flow lives in `core/orchestrator.run_option_image_analysis`
(shared with the web UI's option tab) — this route only handles the HTTP concerns.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app.api.deps import require_api_key
from app.core.orchestrator import (
    CostLimitExceededError,
    DataFetchError,
    VisionExtractionError,
    run_option_image_analysis,
)
from app.db.session import get_db

router = APIRouter(prefix="/api/v1/analyze", tags=["options"], dependencies=[Depends(require_api_key)])

MAX_IMAGE_BYTES = 8 * 1024 * 1024  # 8 MB
ALLOWED_MEDIA_TYPES = {"image/png", "image/jpeg", "image/webp"}


@router.post("/option-image")
def analyze_option_image(file: UploadFile = File(...), db: Session = Depends(get_db)) -> dict:
    if file.content_type not in ALLOWED_MEDIA_TYPES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"unsupported image type '{file.content_type}'"
        )

    image_bytes = file.file.read(MAX_IMAGE_BYTES + 1)
    if len(image_bytes) > MAX_IMAGE_BYTES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "image exceeds the 8MB limit")

    try:
        result = run_option_image_analysis(db, image_bytes, file.content_type)
    except VisionExtractionError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except DataFetchError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    except CostLimitExceededError as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, exc.reason) from exc

    return result.model_dump(mode="json")
