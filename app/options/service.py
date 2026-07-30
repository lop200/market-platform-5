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
    allowed_symbols = settings.configured_sniper_symbols
    symbol = str(stock_analysis.get("symbol") or "").upper()
    if settings.options_scalp_mode and allowed_symbols and symbol not in allowed_symbols:
        # Answer up front instead of spending an OPRA call to return nothing.
        result = rank_option_chain(stock_analysis, [], settings)
        result.status = "no_short_term_options"
        result.warnings_ar.append(
            f"سهم {symbol} ليس له عقود أوبشن تنتهي خلال أيام قليلة، "
            "بل عقود شهرية فقط، ولذلك لا يصلح للقنص السريع. "
            f"جرّب سهمًا مثل: {'، '.join(allowed_symbols[:6])}."
        )
        return result
    if provider is None:
        result = rank_option_chain(stock_analysis, [], settings)
        result.status = "provider_unavailable"
        result.market["options_status"] = "opra_unavailable"
        result.market["options_label_ar"] = "بيانات الأوبشن غير متاحة"
        result.warnings_ar.append("تعذّر مزوّد بيانات الأوبشن، واكتمل تحليل السهم بدون عقود")
        return result
    try:
        contracts = provider.get_option_chain(
            stock_analysis["symbol"],
            float((stock_analysis.get("quote") or {}).get("price") or 0),
        )
    except Exception:
        result = rank_option_chain(stock_analysis, [], settings)
        result.status = "provider_failed"
        result.warnings_ar.append("فشل جلب بيانات الأوبشن، واكتمل تحليل السهم بدون تعطيل الصفحة")
        return result
    return rank_option_chain(stock_analysis, contracts, settings)
