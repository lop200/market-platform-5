from __future__ import annotations

from dataclasses import dataclass

from app.opportunities.schemas import MarketRegime


@dataclass(frozen=True)
class StrategyChoice:
    strategy_id: str
    name_ar: str
    name_en: str
    score: int
    reason: str
    trigger: str
    invalidation: str
    valid_minutes: int = 10
    requires_news: bool = False


def select_strategy(
    ind: dict, price: float, regime: MarketRegime, *, verified_news: bool = False
) -> StrategyChoice:
    if regime in {MarketRegime.HIGH_RISK, MarketRegime.UNSUITABLE, MarketRegime.LOW_LIQUIDITY}:
        return StrategyChoice("no_trade", "لا صفقة", "No Trade", 0, "حالة السوق لا تسمح بدخول منضبط", "انتظر تحسن السوق", "لا يوجد دخول", 5)
    rv = ind.get("relative_volume") or 0
    vwap = ind.get("vwap") or price
    resistance = ind.get("resistance")
    rsi = ind.get("rsi") or 50
    gap = ind.get("gap_pct") or 0
    if gap >= 4 and rv >= 1.5 and (ind.get("momentum") or 0) > 0 and verified_news:
        return StrategyChoice(
            "gap_and_go", "الفجوة والانطلاق", "Gap and Go", 80,
            "فجوة صاعدة مع حجم وزخم وخبر رسمي حديث",
            "تماسك بعد الافتتاح ثم كسر قمة النطاق بحجم أعلى من الطبيعي",
            "فقدان قاع نطاق الافتتاح", requires_news=True,
        )
    opening_high = ind.get("opening_range_high")
    if opening_high is not None and price >= opening_high and rv >= 1.5:
        return StrategyChoice("opening_range_breakout", "اختراق نطاق الافتتاح", "Opening Range Breakout", 82, "السعر يكسر نطاق الافتتاح بحجم أعلى من الطبيعي", f"إغلاق مؤكد فوق نطاق الافتتاح {opening_high:.2f}", f"العودة داخل النطاق أسفل {opening_high:.2f}")
    if resistance is not None and price >= resistance * 0.995 and rv >= 1.8:
        return StrategyChoice("volume_breakout", "اختراق مقاومة مع حجم", "Volume Breakout", 86, "السعر يختبر مقاومة بحجم نسبي قوي", f"إغلاق شمعة 5 دقائق فوق {resistance:.2f} مع حجم نسبي أعلى من 1.8", f"العودة أسفل المقاومة {resistance:.2f}")
    if price >= vwap and (ind.get("momentum") or 0) > 0:
        return StrategyChoice("vwap_reclaim", "استعادة متوسط السعر المرجح بالحجم", "VWAP Reclaim", 78, "السهم استعاد VWAP مع زخم إيجابي", f"ثبات شمعة 5 دقائق فوق VWAP عند {vwap:.2f}", f"إغلاق أسفل VWAP {vwap:.2f}")
    if rsi < 35 and price <= (ind.get("support") or price) * 1.02:
        if regime == MarketRegime.BEARISH:
            return StrategyChoice("no_trade", "لا صفقة", "No Trade", 0, "الانعكاس المبالغ فيه معطل في السوق الهابط", "انتظر استعادة مستوى", "لا يوجد دخول", 5)
        return StrategyChoice("oversold_reversal", "انعكاس مبالغ فيه", "Oversold Reversal", 68, "تشبع بيعي قرب دعم، ويحتاج تأكيدًا إضافيًا", f"استعادة {price:.2f} بحجم متزايد وتباعد إيجابي", "كسر الدعم")
    ema9, ema20, ema50 = ind.get("ema9") or 0, ind.get("ema20") or 0, ind.get("ema50") or 0
    if ema9 > ema20 > ema50 and (ind.get("macd") or 0) > (ind.get("macd_signal") or 0):
        if price <= ema9 * 1.01 and price >= ema20 * 0.995:
            return StrategyChoice(
                "pullback_continuation", "استمرار بعد تراجع منظم", "Pullback Continuation",
                79, "تراجع منظم داخل ترتيب متوسطات صاعد",
                f"شمعة تأكيد فوق EMA9 قرب {ema9:.2f} مع عودة الحجم",
                f"إغلاق أسفل EMA20 عند {ema20:.2f}",
            )
        return StrategyChoice("trend_continuation", "استمرار الاتجاه", "Trend Continuation", 75, "ترتيب المتوسطات والزخم متوافقان", f"ارتداد من EMA20 قرب {ema20:.2f} ثم شمعة تأكيد", f"إغلاق أسفل EMA50 عند {ema50:.2f}")
    if price <= (ind.get("support") or price) * 1.02:
        support = ind.get("support") or price
        return StrategyChoice("support_bounce", "ارتداد من دعم", "Support Bounce", 70, "السعر قريب من دعم جلسة مثبت", f"شمعة رفض صاعدة فوق {support:.2f} مع زيادة الحجم", f"كسر الدعم {support:.2f}")
    return StrategyChoice("no_trade", "لا صفقة", "No Trade", 0, "الإشارات متضاربة أو لا يوجد مستوى دخول واضح", "انتظر تأكيدًا جديدًا", "لا يوجد دخول", 5)


STRATEGY_REGISTRY = {
    item[0]: item[1]
    for item in [
        ("volume_breakout", "اختراق مقاومة مع حجم"),
        ("vwap_reclaim", "استعادة VWAP"),
        ("support_bounce", "ارتداد من دعم"),
        ("trend_continuation", "استمرار الاتجاه"),
        ("pullback_continuation", "استمرار بعد تراجع منظم"),
        ("gap_and_go", "الفجوة والانطلاق"),
        ("opening_range_breakout", "اختراق نطاق الافتتاح"),
        ("oversold_reversal", "انعكاس مبالغ فيه"),
        ("no_trade", "لا صفقة"),
    ]
}
