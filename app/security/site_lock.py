"""Site-wide access lock for deployment (owner request 2026-07-20, Phase 5).

Every visitor must enter an access code on /lock — which also carries the MANDATORY
disclaimer-consent checkbox (SRS 19) — before seeing ANY content. The lock activates
only when ACCESS_CODE_MAIN is set in the environment, so local dev and the test suite
run unlocked by default.

Two kinds of codes grant entry:
- the MAIN code: env-only (ACCESS_CODE_MAIN), never in the DB, also unlocks /lock/admin;
- per-person codes: rows in the access_codes table (SHA-256 hashes), added/revoked from
  /lock/admin so one person's access can be cut without touching anyone else's.

The cookie is HMAC-signed with API_KEY as the signing secret (already required to be a
long random string in production). Its subject is "main" or the DB code's label, and the
middleware re-checks DB codes' active flag on every request — revocation is immediate,
not just at next login.
"""
from __future__ import annotations

import hashlib
import hmac
import uuid as _uuid
from datetime import datetime, timezone

from fastapi import Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import get_settings
from app.db.models import AccessCode
from app.db.session import SessionLocal

COOKIE_NAME = "site_access"
MAIN_SUBJECT = "main"

# Paths reachable WITHOUT a code: the lock screen itself and the deploy health check.
# /api/v1/* paths are also exempt from the cookie when they carry a valid X-API-Key —
# they authenticate through app/api/deps.py already.
#
# The install files are open too. Android reads the manifest, the icons and the
# worker before the user can type anything, so gating them means the app cannot
# be installed while locked. They carry no market data and no account state.
OPEN_PATH_PREFIXES = (
    "/lock",
    "/api/v1/health",
    "/favicon.ico",
    "/manifest.webmanifest",
    "/sw.js",
    "/static/",
)


def hash_code(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _constant_time_text_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _sign(subject: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), f"site-lock:{subject}".encode("utf-8"), hashlib.sha256).hexdigest()


def make_cookie_value(subject: str, secret: str) -> str:
    return f"{subject}|{_sign(subject, secret)}"


def parse_cookie_subject(value: str | None, secret: str) -> str | None:
    """Returns the verified subject ("main" or a DB code label) or None."""
    if not value or "|" not in value:
        return None
    subject, signature = value.rsplit("|", 1)
    if not hmac.compare_digest(signature, _sign(subject, secret)):
        return None
    return subject


def verify_credentials_and_get_subject(db, username: str, password: str, settings) -> str | None:
    """Checks submitted credentials against the owner account, then active DB accounts.

    Returns the cookie subject: "main" for the env code, or the DB row's UUID (as str)
    for a per-person code. The UUID — not the human label — is the subject so the cookie
    stays latin-1 encodable (labels may be Arabic) and never leaks who the code belongs
    to."""
    username_matches = _constant_time_text_equal(username, settings.site_username)
    password_matches = bool(settings.access_code_main) and _constant_time_text_equal(
        password, settings.access_code_main or ""
    )
    if username_matches and password_matches:
        return MAIN_SUBJECT
    row = db.execute(
        select(AccessCode).where(
            AccessCode.label == username,
            AccessCode.code_hash == hash_code(password),
            AccessCode.active.is_(True),
        )
    ).scalar_one_or_none()
    return str(row.id) if row else None


def subject_is_valid(db, subject: str, settings) -> bool:
    """Re-check on every request so revoking a DB code cuts access immediately."""
    if subject == MAIN_SUBJECT:
        return bool(settings.access_code_main)
    try:
        code_id = _uuid.UUID(subject)
    except ValueError:
        return False
    row = db.get(AccessCode, code_id)
    return row is not None and row.active


def revoke_access_code(db, code_id) -> bool:
    row = db.get(AccessCode, code_id)
    if row is None or not row.active:
        return False
    row.active = False
    row.revoked_at = datetime.now(timezone.utc)
    db.commit()
    return True


class SiteLockMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        settings = get_settings()
        if not settings.access_code_main:  # lock disabled (local dev / tests)
            return await call_next(request)

        path = request.url.path
        if any(path == p or path.startswith(p.rstrip("/") + "/") or path.startswith(p) for p in OPEN_PATH_PREFIXES):
            return await call_next(request)

        # Programmatic API access authenticates via X-API-Key (checked again in deps.py;
        # here we only need to know the caller HAS the key to bypass the browser lock).
        if path.startswith("/api/v1/") and hmac.compare_digest(
            request.headers.get("X-API-Key", ""), settings.api_key
        ):
            return await call_next(request)

        subject = parse_cookie_subject(request.cookies.get(COOKIE_NAME), settings.api_key)
        if subject is not None:
            db = SessionLocal()
            try:
                if subject_is_valid(db, subject, settings):
                    return await call_next(request)
            finally:
                db.close()

        if path.startswith("/api/") or path.startswith("/ui/"):
            from fastapi.responses import JSONResponse

            return JSONResponse(
                {"detail": "site locked — access code required"},
                status_code=401,
                headers={"Cache-Control": "no-store"},
            )
        return RedirectResponse(
            url="/lock", status_code=303, headers={"Cache-Control": "no-store"}
        )
