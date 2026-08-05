"""Lock-screen + access-code admin routes (owner request 2026-07-20, Phase 5).

GET/POST /lock         — entry gate: access code + MANDATORY disclaimer consent checkbox.
GET  /lock/admin       — list per-person codes (main-code holders only).
POST /lock/admin/add   — create a person's code (random, shown exactly once).
POST /lock/admin/revoke— deactivate one person's code immediately.

These routes stay importable/mountable even when the lock is disabled (no
ACCESS_CODE_MAIN) — they just answer 404-equivalent guidance, so nothing here affects
local dev or the test suite unless the lock is on.
"""
from __future__ import annotations

import secrets as pysecrets

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import AccessCode
from app.db.session import get_db
from app.legal.disclaimers import DISCLAIMER_AR
from app.security.site_lock import (
    COOKIE_NAME,
    MAIN_SUBJECT,
    hash_code,
    make_cookie_value,
    parse_cookie_subject,
    revoke_access_code,
    verify_credentials_and_get_subject,
)

router = APIRouter(tags=["lock"])
templates = Jinja2Templates(directory="app/templates")


def _lock_enabled() -> bool:
    return bool(get_settings().access_code_main)


def _is_main(request: Request) -> bool:
    settings = get_settings()
    subject = parse_cookie_subject(request.cookies.get(COOKIE_NAME), settings.api_key)
    return subject == MAIN_SUBJECT and bool(settings.access_code_main)


@router.get("/lock", response_class=HTMLResponse)
def lock_page(request: Request) -> HTMLResponse:
    if not _lock_enabled():
        return RedirectResponse(url="/", status_code=303)  # type: ignore[return-value]
    return templates.TemplateResponse(
        request, "lock.html", {"error": None, "disclaimer": DISCLAIMER_AR}
    )


@router.post("/lock", response_class=HTMLResponse)
def lock_submit(
    request: Request,
    username: str = Form(...),
    code: str = Form(...),
    consent: str = Form(None),
    db: Session = Depends(get_db),
):
    settings = get_settings()
    if not _lock_enabled():
        return RedirectResponse(url="/", status_code=303)
    if not consent:
        return templates.TemplateResponse(
            request, "lock.html",
            {"error": "يجب الموافقة على إخلاء المسؤولية قبل الدخول.", "disclaimer": DISCLAIMER_AR},
            status_code=400,
        )
    subject = verify_credentials_and_get_subject(db, username.strip(), code, settings)
    if subject is None:
        return templates.TemplateResponse(
            request, "lock.html",
            {"error": "اسم المستخدم أو كلمة المرور غير صحيحة.", "disclaimer": DISCLAIMER_AR},
            status_code=401,
        )
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        COOKIE_NAME,
        make_cookie_value(subject, settings.api_key),
        max_age=settings.site_lock_cookie_days * 24 * 3600,
        httponly=True,
        samesite="lax",
        secure=settings.app_env == "production",
    )
    return response


@router.post("/logout")
def logout() -> RedirectResponse:
    response = RedirectResponse(url="/lock", status_code=303)
    response.delete_cookie(COOKIE_NAME)
    return response


@router.get("/lock/admin", response_class=HTMLResponse)
def lock_admin(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    if not _lock_enabled() or not _is_main(request):
        return RedirectResponse(url="/lock", status_code=303)  # type: ignore[return-value]
    codes = db.execute(select(AccessCode).order_by(AccessCode.created_at)).scalars().all()
    return templates.TemplateResponse(
        request, "lock_admin.html", {"codes": codes, "new_code": None, "new_label": None, "error": None}
    )


@router.post("/lock/admin/add", response_class=HTMLResponse)
def lock_admin_add(
    request: Request, label: str = Form(...), db: Session = Depends(get_db)
) -> HTMLResponse:
    if not _lock_enabled() or not _is_main(request):
        return RedirectResponse(url="/lock", status_code=303)  # type: ignore[return-value]
    label = label.strip()[:80]
    error = None
    new_code = None
    if not label:
        error = "اكتب اسماً/وصفاً لصاحب الرمز."
    elif db.execute(select(AccessCode).where(AccessCode.label == label)).scalar_one_or_none():
        error = "يوجد رمز بهذا الاسم من قبل — اختر اسماً مختلفاً أو ألغِ القديم."
    else:
        new_code = pysecrets.token_urlsafe(9)  # ~12 chars, URL-safe
        db.add(AccessCode(label=label, code_hash=hash_code(new_code), active=True))
        db.commit()
    codes = db.execute(select(AccessCode).order_by(AccessCode.created_at)).scalars().all()
    return templates.TemplateResponse(
        request, "lock_admin.html",
        {"codes": codes, "new_code": new_code, "new_label": label if new_code else None, "error": error},
    )


@router.post("/lock/admin/revoke", response_class=HTMLResponse)
def lock_admin_revoke(
    request: Request, code_id: str = Form(...), db: Session = Depends(get_db)
) -> HTMLResponse:
    if not _lock_enabled() or not _is_main(request):
        return RedirectResponse(url="/lock", status_code=303)  # type: ignore[return-value]
    import uuid as _uuid

    try:
        revoke_access_code(db, _uuid.UUID(code_id))
    except ValueError:
        pass
    return RedirectResponse(url="/lock/admin", status_code=303)  # type: ignore[return-value]
