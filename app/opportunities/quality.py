from __future__ import annotations

from app.config import Settings
from app.opportunities.schemas import QualityDecision
from app.providers.base import Quote


def evaluate_quote(quote: Quote, settings: Settings) -> QualityDecision:
    reasons: list[str] = []
    warnings: list[str] = []
    if not settings.stock_min_price <= quote.price <= settings.stock_max_price:
        reasons.append("السعر خارج النطاق المسموح من 2 إلى 5 دولارات")
    if quote.bid is None or quote.ask is None or quote.bid <= 0 or quote.ask <= 0:
        reasons.append("بيانات العرض أو الطلب غير مكتملة")
    elif quote.ask < quote.bid:
        reasons.append("بيانات العرض والطلب غير صالحة")
    elif quote.spread_pct is None or quote.spread_pct > settings.max_spread_pct:
        reasons.append("السبريد أعلى من الحد المسموح")
    if quote.age_seconds > settings.max_quote_age_seconds:
        reasons.append("السعر قديم ولا يصلح لقراءة فنية مباشرة")
    if quote.feed and quote.feed.lower() == "iex":
        warnings.append("البيانات من IEX وليست تغطية SIP الكاملة")
    if quote.is_delayed:
        warnings.append("بيانات المزود متأخرة")
    return QualityDecision(accepted=not reasons, reasons=reasons, warnings=warnings)
