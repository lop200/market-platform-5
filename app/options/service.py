from __future__ import annotations

from app.config import Settings
from app.options.engine import rank_option_chain
from app.options.provider import OptionDataProvider
from app.options.schemas import OptionChainResult


def analyze_options_after_stock(
    stock_analysis: dict,
    settings: Settings,
    provider: OptionDataProvider | None,
) -> OptionChainResult:
    """Enforce stock-first gating before touching any options provider."""
    if not settings.options_enabled:
        return rank_option_chain(stock_analysis, [], settings)
    stock_valid = (
        stock_analysis.get("status") == "conditional_entry"
        and bool(stock_analysis.get("data_quality", {}).get("valid_for_plan"))
        and bool(stock_analysis.get("trade_plan"))
    )
    if not stock_valid:
        return rank_option_chain(stock_analysis, [], settings)
    universe = settings.configured_sniper_universe
    symbol = str(stock_analysis.get("symbol") or "").upper()
    if settings.options_scalp_mode and universe and symbol not in universe:
        # Answer up front instead of spending an OPRA call to return nothing.
        result = rank_option_chain(stock_analysis, [], settings)
        result.status = "outside_sniper_universe"
        result.warnings_ar.append(
            f"{symbol} خارج كون القنّاص: لا تتوفر له انتهاءات 0–2 DTE سائلة، "
            "فأغلب الأسهم خارج القائمة لها انتهاء شهري فقط. "
            f"جرّب مثلًا: {'، '.join(universe[:6])}."
        )
        return result
    if provider is None:
        result = rank_option_chain(stock_analysis, [], settings)
        result.status = "provider_unavailable"
        result.market["options_status"] = "opra_unavailable"
        result.market["options_label_ar"] = "بيانات OPRA غير متاحة"
        result.warnings_ar.append("تعذر مزود OPRA؛ اكتمل تحليل السهم دون خيارات")
        return result
    try:
        contracts = provider.get_option_chain(
            stock_analysis["symbol"],
            float((stock_analysis.get("quote") or {}).get("price") or 0),
        )
    except Exception:
        result = rank_option_chain(stock_analysis, [], settings)
        result.status = "provider_failed"
        result.warnings_ar.append("فشل options API؛ اكتمل تحليل السهم دون تعطيل الصفحة")
        return result
    return rank_option_chain(stock_analysis, contracts, settings)
