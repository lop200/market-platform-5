"""Independent price reconciliation for actionable stock opportunities."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from time import monotonic

import httpx

from app.config import Settings
from app.providers.base import Quote

_cache: dict[str, tuple[float, float, datetime]] = {}
_cache_lock = Lock()


@dataclass(frozen=True)
class PriceVerification:
    accepted: bool
    status: str
    provider: str | None = None
    price: float | None = None
    as_of: datetime | None = None
    age_seconds: int | None = None
    divergence_pct: float | None = None
    reason_ar: str = ""

    @property
    def data_status(self) -> str:
        """Return a UI status that does not mislabel verification outages.

        ``data_conflict`` is reserved for two fresh observations whose prices
        genuinely disagree.  A stale or unavailable independent observation
        still blocks entry when verification is required, but it is a data
        availability problem rather than evidence of conflicting prices.
        """
        if self.accepted:
            return "validation_warning" if self.status == "validation_warning" else "verified"
        return {
            "data_conflict": "data_conflict",
            "stale": "external_stale",
            "unavailable": "external_unavailable",
        }.get(self.status, "external_unverified")


def _fresh_alpaca_sip(primary: Quote, settings: Settings) -> bool:
    ages = (primary.bid_age_seconds, primary.ask_age_seconds)
    return (
        primary.provider.lower() == "alpaca"
        and (primary.feed or "").lower() == "sip"
        and primary.bid is not None
        and primary.ask is not None
        and primary.bid > 0
        and primary.ask >= primary.bid
        and all(age is not None and age <= settings.max_quote_age_seconds for age in ages)
    )


def _validation_failure(
    primary: Quote,
    settings: Settings,
    *,
    status: str,
    provider: str | None,
    reason_ar: str,
    price: float | None = None,
    as_of: datetime | None = None,
    age_seconds: int | None = None,
) -> PriceVerification:
    if _fresh_alpaca_sip(primary, settings):
        return PriceVerification(
            True, "validation_warning", provider, price, as_of, age_seconds,
            reason_ar=(
                f"{reason_ar}؛ تم تجاهله لأن Alpaca SIP مباشر وحديث، "
                "وFinnhub مصدر تحقق مساعد وليس veto."
            ),
        )
    return PriceVerification(
        not settings.price_verification_required,
        status,
        provider,
        price,
        as_of,
        age_seconds,
        reason_ar=reason_ar,
    )


def _age_seconds(value: datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - value).total_seconds()))


def verify_external_price(
    symbol: str,
    primary: Quote,
    settings: Settings,
    *,
    reference_price: float | None = None,
    reference_as_of: datetime | None = None,
    reference_provider: str = "finnhub",
) -> PriceVerification:
    """Compare the executable midpoint with an independent last trade.

    Tests and offline callers can inject the independent observation. In live
    use Finnhub is fetched only for the small deep-analysis shortlist.
    """
    if not settings.price_verification_enabled:
        return PriceVerification(True, "disabled", reason_ar="التحقق الخارجي معطل بالإعداد")

    if reference_price is None:
        if not settings.finnhub_api_key:
            return _validation_failure(
                primary, settings, status="unavailable", provider=reference_provider,
                reason_ar="تعذر التحقق الخارجي المستقل من السعر",
            )
        with _cache_lock:
            cached = _cache.get(symbol)
        if cached and monotonic() - cached[0] <= settings.price_verification_cache_seconds:
            reference_price, reference_as_of = cached[1], cached[2]
        try:
            if reference_price is not None and reference_as_of is not None:
                raise LookupError("cached")
            response = httpx.get(
                "https://finnhub.io/api/v1/quote",
                params={"symbol": symbol, "token": settings.finnhub_api_key},
                timeout=settings.external_timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
            reference_price = float(payload.get("c") or 0)
            stamp = int(payload.get("t") or 0)
            reference_as_of = datetime.fromtimestamp(stamp, tz=timezone.utc) if stamp else None
            if reference_price > 0 and reference_as_of is not None:
                with _cache_lock:
                    _cache[symbol] = (monotonic(), reference_price, reference_as_of)
        except LookupError:
            pass
        except Exception:
            return _validation_failure(
                primary, settings, status="unavailable", provider=reference_provider,
                reason_ar="تعذر التحقق الخارجي المستقل من السعر",
            )

    if not reference_price or reference_price <= 0 or reference_as_of is None:
        return _validation_failure(
            primary, settings, status="unavailable", provider=reference_provider,
            reason_ar="سعر التحقق الخارجي أو وقته غير صالح",
        )

    age = _age_seconds(reference_as_of)
    if age > settings.price_verification_max_age_seconds:
        return _validation_failure(
            primary, settings, status="stale", provider=reference_provider,
            price=reference_price, as_of=reference_as_of, age_seconds=age,
            reason_ar="سعر التحقق الخارجي قديم",
        )

    primary_price = primary.mid or primary.price
    divergence = abs(primary_price - reference_price) / reference_price * 100
    accepted = divergence <= settings.price_verification_max_divergence_pct
    return PriceVerification(
        accepted,
        "verified" if accepted else "data_conflict",
        reference_provider,
        reference_price,
        reference_as_of,
        age,
        round(divergence, 4),
        "تم التحقق من السعر بمصدر مستقل" if accepted else "Data Conflict — اختلاف السعر بين المصدرين يتجاوز الحد",
    )
