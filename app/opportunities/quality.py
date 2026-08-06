from __future__ import annotations

from app.config import Settings
from app.opportunities.schemas import QualityDecision
from app.providers.base import Quote
from app.stocks.rules import allowed_stock_spread_pct


def evaluate_quote(quote: Quote, settings: Settings) -> QualityDecision:
    reasons: list[str] = []
    warnings: list[str] = []
    if quote.bid is None or quote.ask is None or quote.bid <= 0 or quote.ask <= 0:
        reasons.append("بيانات العرض أو الطلب غير مكتملة")
    elif quote.ask < quote.bid:
        reasons.append("بيانات العرض والطلب غير صالحة")
    elif quote.spread_pct is None or quote.spread_pct > allowed_stock_spread_pct(quote.price, settings):
        reasons.append("السبريد أعلى من الحد المسموح")
    component_ages = [quote.bid_age_seconds, quote.ask_age_seconds]
    if any(age is None for age in component_ages) or max(age or 0 for age in component_ages) > settings.max_quote_age_seconds:
        reasons.append("Bid/Ask قديمان أو بلا طابع زمني ولا يصلحان للدخول")
    if quote.trade_age_seconds is None or quote.trade_age_seconds > settings.max_quote_age_seconds:
        reasons.append("وقت آخر صفقة قديم أو غير متوفر")
    references = [
        value for value in (quote.mid, quote.last_trade, quote.bar_close)
        if value is not None and value > 0
    ]
    if len(references) >= 2:
        divergence = (max(references) - min(references)) / min(references) * 100
        if divergence > settings.max_price_source_divergence_pct:
            reasons.append("Data Conflict — اختلاف داخلي غير منطقي بين Mid وآخر صفقة والشمعة")
    if quote.feed and quote.feed.lower() == "iex":
        warnings.append("البيانات من IEX وليست تغطية SIP الكاملة")
    if quote.is_delayed:
        warnings.append("بيانات المزود متأخرة")
    return QualityDecision(accepted=not reasons, reasons=reasons, warnings=warnings)
