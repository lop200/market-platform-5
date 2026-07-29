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
    if provider is None:
        result = rank_option_chain(stock_analysis, [], settings)
        result.status = "provider_unavailable"
        result.warnings_ar.append("تعذر مزود OPRA؛ اكتمل تحليل السهم دون خيارات")
        return result
    try:
        contracts = provider.get_option_chain(stock_analysis["symbol"])
    except Exception:
        result = rank_option_chain(stock_analysis, [], settings)
        result.status = "provider_failed"
        result.warnings_ar.append("فشل options API؛ اكتمل تحليل السهم دون تعطيل الصفحة")
        return result
    return rank_option_chain(stock_analysis, contracts, settings)

