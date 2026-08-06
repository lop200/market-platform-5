from __future__ import annotations

from dataclasses import dataclass, replace

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
    match_pct: int = 0
    classification_ar: str = "غير متحقق"
    setup_class_ar: str = "غير مصنف"
    checks: tuple[dict, ...] = ()


def _select_strategy(
    ind: dict, price: float, regime: MarketRegime, *, verified_news: bool = False
) -> StrategyChoice:
    if regime in {MarketRegime.HIGH_RISK, MarketRegime.UNSUITABLE}:
        return StrategyChoice("no_trade", "لا صفقة", "No Trade", 0, "حالة السوق لا تسمح بدخول منضبط", "انتظر تحسن السوق", "لا يوجد دخول", 5)
    rv = ind.get("relative_volume") or 0
    vwap = ind.get("vwap") or price
    resistance = ind.get("resistance")
    rsi = ind.get("rsi") or 50
    gap = ind.get("gap_pct") or 0
    if gap >= 4 and rv >= 1.2 and (ind.get("momentum") or 0) > 0 and verified_news:
        return StrategyChoice(
            "gap_and_go", "الفجوة والانطلاق", "Gap and Go", 80,
            "فجوة صاعدة مع حجم وزخم وخبر رسمي حديث",
            "تماسك بعد الافتتاح ثم كسر قمة النطاق بحجم أعلى من الطبيعي",
            "فقدان قاع نطاق الافتتاح", requires_news=True,
        )
    opening_high = ind.get("opening_range_high")
    if opening_high is not None and price >= opening_high * 0.998 and rv >= 1.2:
        return StrategyChoice("opening_range_breakout", "اختراق نطاق الافتتاح", "Opening Range Breakout", 82, "السعر يكسر نطاق الافتتاح بحجم أعلى من الطبيعي", f"إغلاق مؤكد فوق نطاق الافتتاح {opening_high:.2f}", f"العودة داخل النطاق أسفل {opening_high:.2f}")
    if resistance is not None and price >= resistance * 0.99 and rv >= 1.35:
        return StrategyChoice("volume_breakout", "اختراق مقاومة مع حجم", "Volume Breakout", 86, "السعر يختبر مقاومة بحجم نسبي قوي", f"إغلاق شمعة 5 دقائق فوق {resistance:.2f} مع حجم نسبي أعلى من 1.8", f"العودة أسفل المقاومة {resistance:.2f}")
    if price >= vwap * 0.998 and (ind.get("momentum") or 0) > 0 and rv >= 0.65:
        return StrategyChoice("vwap_reclaim", "استعادة متوسط السعر المرجح بالحجم", "VWAP Reclaim", 78, "السهم استعاد VWAP مع زخم إيجابي", f"ثبات شمعة 5 دقائق فوق VWAP عند {vwap:.2f}", f"إغلاق أسفل VWAP {vwap:.2f}")
    if rsi < 38 and price <= (ind.get("support") or price) * 1.025:
        if regime == MarketRegime.BEARISH:
            return StrategyChoice("no_trade", "لا صفقة", "No Trade", 0, "الانعكاس المبالغ فيه معطل في السوق الهابط", "انتظر استعادة مستوى", "لا يوجد دخول", 5)
        return StrategyChoice("oversold_reversal", "انعكاس مبالغ فيه", "Oversold Reversal", 68, "تشبع بيعي قرب دعم، ويحتاج تأكيدًا إضافيًا", f"استعادة {price:.2f} بحجم متزايد وتباعد إيجابي", "كسر الدعم")
    ema9, ema20, ema50 = ind.get("ema9") or 0, ind.get("ema20") or 0, ind.get("ema50") or 0
    support = ind.get("support")
    if (
        ema9 < ema20 < ema50
        and (ind.get("macd") or 0) < (ind.get("macd_signal") or 0)
        and support is not None
        and price <= support * 1.005
        and rv >= 0.9
    ):
        return StrategyChoice(
            "support_breakdown",
            "كسر دعم مع اتجاه هابط",
            "Support Breakdown",
            82,
            "السهم يكسر الدعم مع ترتيب متوسطات هابط وزخم سلبي",
            f"إغلاق شمعة 5 دقائق أسفل {support:.2f} مع استمرار الحجم",
            f"العودة والثبات أعلى الدعم {support:.2f}",
        )
    if ema9 > ema20 > ema50 and (ind.get("macd") or 0) > (ind.get("macd_signal") or 0):
        if price <= ema9 * 1.01 and price >= ema20 * 0.995:
            return StrategyChoice(
                "pullback_continuation", "استمرار بعد تراجع منظم", "Pullback Continuation",
                79, "تراجع منظم داخل ترتيب متوسطات صاعد",
                f"شمعة تأكيد فوق EMA9 قرب {ema9:.2f} مع عودة الحجم",
                f"إغلاق أسفل EMA20 عند {ema20:.2f}",
            )
        return StrategyChoice("trend_continuation", "استمرار الاتجاه", "Trend Continuation", 75, "ترتيب المتوسطات والزخم متوافقان", f"ارتداد من EMA20 قرب {ema20:.2f} ثم شمعة تأكيد", f"إغلاق أسفل EMA50 عند {ema50:.2f}")
    if price <= (ind.get("support") or price) * 1.025 and rv >= 0.7:
        support = ind.get("support") or price
        return StrategyChoice("support_bounce", "ارتداد من دعم", "Support Bounce", 70, "السعر قريب من دعم جلسة مثبت", f"شمعة رفض صاعدة فوق {support:.2f} مع زيادة الحجم", f"كسر الدعم {support:.2f}")
    return StrategyChoice("no_trade", "لا صفقة", "No Trade", 0, "الإشارات متضاربة أو لا يوجد مستوى دخول واضح", "انتظر تأكيدًا جديدًا", "لا يوجد دخول", 5)


def _classification(score: int) -> str:
    if score >= 90:
        return "تحقق قوي جدًا"
    if score >= 80:
        return "تحقق قوي"
    if score >= 70:
        return "تحقق جيد"
    if score >= 60:
        return "تحقق مبدئي"
    return "تحقق ضعيف"


def _evidence(
    strategy_id: str, ind: dict, price: float, regime: MarketRegime,
    verified_news: bool,
) -> tuple[int, str, tuple[dict, ...]]:
    rv = float(ind.get("relative_volume") or 0)
    momentum = float(ind.get("momentum") or 0)
    vwap = float(ind.get("vwap") or price or 0)
    ema9 = float(ind.get("ema9") or 0)
    ema20 = float(ind.get("ema20") or 0)
    ema50 = float(ind.get("ema50") or 0)
    rsi = float(ind.get("rsi") or 50)
    support = float(ind.get("support") or price or 0)
    resistance = float(ind.get("resistance") or price or 0)
    opening_high = ind.get("opening_range_high")
    gap = float(ind.get("gap_pct") or 0)
    bullish_15m = bool(ind.get("trend_15m_bullish"))
    specs: dict[str, tuple[str, list[tuple[str, bool, int]]]] = {
        "gap_and_go": ("زخم خبري", [
            ("فجوة 4% أو أكثر", gap >= 4, 25),
            ("خبر رسمي حديث", verified_news, 25),
            ("حجم نسبي 1.2 أو أكثر", rv >= 1.2, 20),
            ("زخم موجب", momentum > 0, 15),
            ("السعر فوق VWAP", price >= vwap, 15),
        ]),
        "opening_range_breakout": ("اختراق وزخم", [
            ("اختبار نطاق الافتتاح", opening_high is not None and price >= float(opening_high) * .998, 30),
            ("حجم نسبي 1.2 أو أكثر", rv >= 1.2, 25),
            ("السعر فوق VWAP", price >= vwap, 20),
            ("زخم موجب", momentum > 0, 15),
            ("اتجاه 15 دقيقة صاعد", bullish_15m, 10),
        ]),
        "volume_breakout": ("اختراق وزخم", [
            ("قرب المقاومة", price >= resistance * .99, 30),
            ("حجم نسبي 1.35 أو أكثر", rv >= 1.35, 25),
            ("زخم موجب", momentum > 0, 20),
            ("EMA9 فوق EMA20", ema9 > ema20, 15),
            ("اتجاه 15 دقيقة صاعد", bullish_15m, 10),
        ]),
        "vwap_reclaim": ("اتجاه داخل الجلسة", [
            ("السعر استعاد VWAP", price >= vwap * .998, 30),
            ("زخم موجب", momentum > 0, 25),
            ("EMA9 فوق EMA20", ema9 >= ema20, 20),
            ("حجم نسبي 0.65 أو أكثر", rv >= .65, 15),
            ("RSI بين 45 و70", 45 <= rsi <= 70, 10),
        ]),
        "oversold_reversal": ("انعكاس متوسط", [
            ("RSI دون 38", rsi < 38, 30),
            ("السعر قريب من الدعم", price <= support * 1.025, 30),
            ("السوق ليس هابطًا", regime != MarketRegime.BEARISH, 20),
            ("حجم نسبي 0.65 أو أكثر", rv >= .65, 10),
            ("السعر دون VWAP", price <= vwap, 10),
        ]),
        "support_breakdown": ("كسر هابط", [
            ("ترتيب EMA هابط", ema9 < ema20 < ema50, 25),
            ("السعر يكسر الدعم", price <= support * 1.005, 25),
            ("MACD دون الإشارة", float(ind.get("macd") or 0) < float(ind.get("macd_signal") or 0), 20),
            ("حجم نسبي 0.9 أو أكثر", rv >= .9, 20),
            ("زخم سلبي", momentum < 0, 10),
        ]),
        "pullback_continuation": ("استمرار اتجاه", [
            ("ترتيب EMA صاعد", ema9 > ema20 > ema50, 30),
            ("السعر بين EMA9 وEMA20", price <= ema9 * 1.01 and price >= ema20 * .995, 25),
            ("MACD فوق الإشارة", float(ind.get("macd") or 0) > float(ind.get("macd_signal") or 0), 20),
            ("حجم نسبي 0.7 أو أكثر", rv >= .7, 15),
            ("اتجاه 15 دقيقة صاعد", bullish_15m, 10),
        ]),
        "trend_continuation": ("استمرار اتجاه", [
            ("ترتيب EMA صاعد", ema9 > ema20 > ema50, 35),
            ("MACD فوق الإشارة", float(ind.get("macd") or 0) > float(ind.get("macd_signal") or 0), 25),
            ("السعر فوق VWAP", price >= vwap, 15),
            ("حجم نسبي 0.7 أو أكثر", rv >= .7, 15),
            ("اتجاه 15 دقيقة صاعد", bullish_15m, 10),
        ]),
        "support_bounce": ("ارتداد من مستوى", [
            ("السعر قريب من الدعم", price <= support * 1.025, 35),
            ("حجم نسبي 0.7 أو أكثر", rv >= .7, 20),
            ("السعر فوق VWAP", price >= vwap, 15),
            ("زخم غير سلبي", momentum >= 0, 15),
            ("RSI دون 65", rsi < 65, 15),
        ]),
    }
    setup_class, checks = specs.get(strategy_id, ("غير مصنف", []))
    rows = tuple(
        {"label_ar": label, "passed": passed, "weight": weight}
        for label, passed, weight in checks
    )
    score = sum(row[2] for row in checks if row[1])
    return score, setup_class, rows


def select_strategy(
    ind: dict, price: float, regime: MarketRegime, *, verified_news: bool = False
) -> StrategyChoice:
    choice = _select_strategy(
        ind, price, regime, verified_news=verified_news
    )
    if choice.strategy_id == "no_trade":
        return choice
    score, setup_class, checks = _evidence(
        choice.strategy_id, ind, price, regime, verified_news
    )
    return replace(
        choice,
        match_pct=score,
        classification_ar=_classification(score),
        setup_class_ar=setup_class,
        checks=checks,
        valid_minutes=min(choice.valid_minutes, 5) if price <= 5 else choice.valid_minutes,
    )


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
        ("support_breakdown", "كسر دعم مع اتجاه هابط"),
        ("no_trade", "لا صفقة"),
    ]
}
