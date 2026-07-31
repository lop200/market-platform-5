from __future__ import annotations

import re
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Protocol

import httpx
import pandas as pd

from app.config import Settings
from app.spx.schemas import SPXContract, SPXProviderCapabilities, SPXQuote

OCC = re.compile(r"^(?P<root>SPXW?|XSP)(?P<expiry>\d{6})(?P<side>[CP])(?P<strike>\d{8})$")


class SPXMarketDataProvider(Protocol):
    provider_name: str

    def capabilities(self) -> SPXProviderCapabilities: ...
    def get_quote(self) -> SPXQuote: ...
    def get_history(self) -> pd.DataFrame: ...


class SPXOptionsProvider(Protocol):
    provider_name: str
    feed: str

    def get_chain(
        self, *, min_dte: int, max_dte: int, underlying_price: float
    ) -> list[SPXContract]: ...

    def get_synthetic_chain(
        self, *, min_dte: int, max_dte: int
    ) -> list[SPXContract]: ...


def _stamp(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def _capability_message(underlying: bool, options: bool, index_status: int) -> str:
    """Explain a missing index feed instead of blaming "the current provider"."""
    if underlying and options:
        return "بيانات SPX وعقوده متاحة من Alpaca."
    if not underlying:
        if index_status in (401, 403):
            return AlpacaSPXProvider.INDEX_SUBSCRIPTION_HINT
        return f"تعذر جلب قيمة مؤشر SPX من Alpaca (رمز {index_status})."
    return "سلسلة عقود SPX غير متاحة من المزود الحالي"


class AlpacaSPXProvider:
    """Read-only Alpaca adapter. It never submits trading requests."""

    provider_name = "alpaca"

    def __init__(self, settings: Settings):
        if not settings.alpaca_api_key or not settings.alpaca_api_secret:
            raise ValueError("Alpaca credentials are required")
        self.settings = settings
        self.feed = settings.alpaca_options_feed.lower()
        self.data_url = settings.alpaca_data_base_url.rstrip("/")
        self.paper_url = "https://paper-api.alpaca.markets"
        self.headers = {
            "APCA-API-KEY-ID": settings.alpaca_api_key,
            "APCA-API-SECRET-KEY": settings.alpaca_api_secret,
        }

    def _get(self, url: str, params: dict) -> httpx.Response:
        with httpx.Client(timeout=self.settings.external_timeout_seconds) as client:
            return client.get(url, params=params, headers=self.headers)

    @staticmethod
    def _settlement_type(row: dict, root_symbol: str) -> str | None:
        """Infer only the explicitly supported cash-settled SPXW family."""
        style = str(row.get("style") or "").lower()
        deliverables = row.get("deliverables") or []
        cash_settled = bool(deliverables) and all(
            str(item.get("type") or "").lower() == "cash"
            for item in deliverables
            if isinstance(item, dict)
        )
        if root_symbol == "SPXW" and style == "european" and cash_settled:
            return "PM_CASH"
        return None

    def _snapshots_for(self, symbols: list[str]) -> dict:
        snapshots: dict = {}
        for start in range(0, len(symbols), 100):
            response = self._get(
                f"{self.data_url}/v1beta1/options/snapshots",
                {
                    "symbols": ",".join(symbols[start : start + 100]),
                    "feed": self.feed,
                    "limit": 100,
                },
            )
            response.raise_for_status()
            snapshots.update(response.json().get("snapshots") or {})
        return snapshots

    def _map_contracts(self, rows: list[dict], snapshots: dict) -> list[SPXContract]:
        metadata = {row["symbol"]: row for row in rows if row.get("symbol")}
        result: list[SPXContract] = []
        for symbol, item in snapshots.items():
            match = OCC.fullmatch(symbol)
            if not match:
                continue
            row = metadata.get(symbol) or {}
            quote = item.get("latestQuote") or {}
            trade = item.get("latestTrade") or {}
            daily = item.get("dailyBar") or {}
            greeks = item.get("greeks") or {}
            root_symbol = match.group("root")
            expiry_date = (
                date.fromisoformat(str(row["expiration_date"]))
                if row.get("expiration_date")
                else datetime.strptime(match.group("expiry"), "%y%m%d").date()
            )
            result.append(
                SPXContract(
                    symbol=symbol,
                    option_type="call" if match.group("side") == "C" else "put",
                    strike=float(
                        row.get("strike_price")
                        or int(match.group("strike")) / 1000
                    ),
                    expiration=datetime.combine(
                        expiry_date, datetime.min.time(), tzinfo=timezone.utc
                    ),
                    bid=quote.get("bp"),
                    ask=quote.get("ap"),
                    last=trade.get("p"),
                    volume=daily.get("v"),
                    open_interest=row.get("open_interest"),
                    delta=greeks.get("delta"),
                    gamma=greeks.get("gamma"),
                    theta=greeks.get("theta"),
                    vega=greeks.get("vega"),
                    iv=item.get("impliedVolatility"),
                    quote_timestamp=_stamp(quote.get("t")),
                    trade_timestamp=_stamp(trade.get("t")),
                    feed=self.feed,
                    root_symbol=root_symbol,
                    settlement_type=self._settlement_type(row, root_symbol),
                    exercise_style=str(row.get("style") or "").lower() or None,
                )
            )
        return result

    # Index values are a separate Alpaca subscription from the stock/options
    # plans, so a key that streams SIP and reads OPRA can still be refused here.
    # Saying which is which saves the reader from debugging a working key.
    INDEX_SUBSCRIPTION_HINT = (
        "قيمة مؤشر SPX مرفوضة من Alpaca (403): بيانات المؤشرات اشتراك منفصل عن خطة "
        "الأسهم والخيارات. عقود SPXW تعمل، وبديل التداول الجاهز هو خيارات SPY وQQQ "
        "وIWM فهي مشمولة باشتراكك الحالي."
    )

    def capabilities(self) -> SPXProviderCapabilities:
        checked = datetime.now(timezone.utc)
        index = self._get(
            f"{self.data_url}/v1beta1/indices/latest/values",
            {"index_symbols": "SPX"},
        )
        underlying = index.status_code == 200 and bool((index.json().get("values") or {}))
        if not self.settings.options_enabled:
            return SPXProviderCapabilities(
                provider=self.provider_name,
                checked_at=checked,
                underlying_available=underlying,
                underlying_status=str(index.status_code),
                options_status="disabled",
                message_ar=(
                    _capability_message(underlying, False, index.status_code)
                    if not underlying
                    else "خيارات SPX غير مفعلة؛ اضبط OPTIONS_ENABLED=true"
                ),
            )
        chain = self._get(
            f"{self.data_url}/v1beta1/options/snapshots/SPX",
            {"feed": self.feed, "limit": 20},
        )
        weekly_chain = self._get(
            f"{self.data_url}/v1beta1/options/snapshots/SPXW",
            {
                "feed": self.feed,
                "expiration_date_gte": checked.date().isoformat(),
                "expiration_date_lte": (checked.date() + timedelta(days=7)).isoformat(),
                "limit": 20,
            },
        )
        snapshots = chain.json().get("snapshots") or {} if chain.status_code == 200 else {}
        weekly_snapshots = (
            weekly_chain.json().get("snapshots") or {}
            if weekly_chain.status_code == 200 else {}
        )
        snapshots.update(weekly_snapshots)
        contracts = list(snapshots.items())
        metadata = self._get(
            f"{self.paper_url}/v2/options/contracts",
            {
                "underlying_symbols": "SPX",
                "root_symbol": "SPXW",
                "status": "active",
                "expiration_date_gte": checked.date().isoformat(),
                "expiration_date_lte": (checked.date() + timedelta(days=7)).isoformat(),
                "limit": 100,
            },
        )
        metadata_rows = (
            metadata.json().get("option_contracts") or []
            if metadata.status_code == 200 else []
        )
        one_dte_metadata = self._get(
            f"{self.paper_url}/v2/options/contracts",
            {
                "underlying_symbols": "SPX",
                "root_symbol": "SPXW",
                "status": "active",
                "expiration_date": (checked.date() + timedelta(days=1)).isoformat(),
                "limit": 1,
            },
        )
        one_dte_rows = (
            one_dte_metadata.json().get("option_contracts") or []
            if one_dte_metadata.status_code == 200 else []
        )
        parsed_dtes: list[int] = []
        greeks = False
        for symbol, item in contracts:
            match = OCC.fullmatch(symbol)
            if match:
                expiry = datetime.strptime(match.group("expiry"), "%y%m%d").date()
                parsed_dtes.append((expiry - checked.date()).days)
            greeks = greeks or bool(item.get("greeks"))
        for item in [*metadata_rows, *one_dte_rows]:
            try:
                expiry = date.fromisoformat(str(item["expiration_date"]))
                parsed_dtes.append((expiry - checked.date()).days)
            except (KeyError, TypeError, ValueError):
                continue
        options_available = bool(contracts or metadata_rows)
        return SPXProviderCapabilities(
            provider=self.provider_name,
            checked_at=checked,
            underlying_available=underlying,
            option_chain_available=options_available,
            opra_available=options_available and self.feed == "opra",
            greeks_available=greeks,
            expirations_available=bool(parsed_dtes),
            zero_dte_available=0 in parsed_dtes,
            one_dte_available=1 in parsed_dtes,
            weekly_expirations_available=bool(metadata_rows or weekly_snapshots),
            underlying_status=str(index.status_code),
            options_status=(
                f"spx:{chain.status_code},spxw:{weekly_chain.status_code},"
                f"contracts:{metadata.status_code}"
            ),
            message_ar=_capability_message(
                underlying, options_available, index.status_code
            ),
        )

    def get_quote(self) -> SPXQuote:
        response = self._get(
            f"{self.data_url}/v1beta1/indices/latest/values",
            {"index_symbols": "SPX"},
        )
        response.raise_for_status()
        values = response.json().get("values") or {}
        item = values.get("SPX") or values.get("I:SPX")
        if not item:
            raise ValueError("SPX index value unavailable")
        stamp = _stamp(item.get("timestamp") or item.get("t")) or datetime.now(timezone.utc)
        price = item.get("value", item.get("p"))
        return SPXQuote(
            price=float(price),
            last_trade=float(price),
            quote_timestamp=stamp,
            trade_timestamp=stamp,
            source="alpaca_index",
            is_realtime=True,
        )

    def get_history(self) -> pd.DataFrame:
        end = datetime.now(timezone.utc)
        response = self._get(
            f"{self.data_url}/v1beta1/indices/values",
            {
                "index_symbols": "SPX",
                "start": (end - timedelta(days=10)).isoformat(),
                "end": end.isoformat(),
                "limit": 10000,
            },
        )
        response.raise_for_status()
        values = response.json().get("values") or []
        if isinstance(values, dict):
            values = values.get("SPX") or values.get("I:SPX") or []
        rows = [
            {
                "datetime": item.get("timestamp") or item.get("t"),
                "open": item.get("value", item.get("p")),
                "high": item.get("value", item.get("p")),
                "low": item.get("value", item.get("p")),
                "close": item.get("value", item.get("p")),
                "volume": 0,
            }
            for item in values
        ]
        frame = pd.DataFrame(rows)
        if frame.empty:
            raise ValueError("SPX history unavailable")
        frame["datetime"] = pd.to_datetime(frame["datetime"], utc=True)
        return frame.set_index("datetime")

    def get_chain(
        self, *, min_dte: int, max_dte: int, underlying_price: float
    ) -> list[SPXContract]:
        if not self.settings.options_enabled:
            return []
        today = datetime.now(timezone.utc).date()
        rows: list[dict] = []
        for root_symbol in ("SPXW", "SPX"):
            metadata = self._get(
                f"{self.paper_url}/v2/options/contracts",
                {
                    "underlying_symbols": "SPX",
                    "root_symbol": root_symbol,
                    "status": "active",
                    "expiration_date_gte": (today + timedelta(days=min_dte)).isoformat(),
                    "expiration_date_lte": (today + timedelta(days=max_dte)).isoformat(),
                    "strike_price_gte": round(underlying_price * 0.97, 2),
                    "strike_price_lte": round(underlying_price * 1.03, 2),
                    "limit": 1000,
                },
            )
            metadata.raise_for_status()
            rows.extend(metadata.json().get("option_contracts") or [])
        symbols = [str(row["symbol"]) for row in rows if row.get("symbol")]
        if not symbols:
            return []
        return self._map_contracts(rows, self._snapshots_for(symbols))

    def get_synthetic_chain(
        self, *, min_dte: int, max_dte: int
    ) -> list[SPXContract]:
        """Fetch one verified PM-settled SPXW expiration for parity matching."""
        if (
            not self.settings.options_enabled
            or not self.settings.spx_synthetic_enabled
            or self.feed != "opra"
        ):
            return []
        today = datetime.now(timezone.utc).date()
        response = self._get(
            f"{self.paper_url}/v2/options/contracts",
            {
                "underlying_symbols": "SPX",
                "root_symbol": "SPXW",
                "status": "active",
                "expiration_date_gte": (
                    today + timedelta(days=min_dte)
                ).isoformat(),
                "expiration_date_lte": (
                    today + timedelta(days=max_dte)
                ).isoformat(),
                "limit": 1000,
            },
        )
        response.raise_for_status()
        rows = response.json().get("option_contracts") or []
        eligible = [
            row
            for row in rows
            if row.get("symbol")
            and self._settlement_type(row, "SPXW") == "PM_CASH"
        ]
        pair_counts: Counter[str] = Counter()
        seen: dict[tuple[str, str], set[str]] = {}
        for row in eligible:
            match = OCC.fullmatch(str(row["symbol"]))
            expiration = str(row.get("expiration_date") or "")
            if not match or not expiration:
                continue
            seen.setdefault(
                (expiration, match.group("strike")), set()
            ).add(match.group("side"))
        for (expiration, _strike), sides in seen.items():
            if sides == {"C", "P"}:
                pair_counts[expiration] += 1
        if not pair_counts:
            return []
        expiration = min(
            pair_counts,
            key=lambda value: (
                date.fromisoformat(value),
                -pair_counts[value],
            ),
        )
        selected_rows = [
            row
            for row in eligible
            if str(row.get("expiration_date")) == expiration
        ]
        symbols = [str(row["symbol"]) for row in selected_rows]
        return self._map_contracts(
            selected_rows,
            self._snapshots_for(symbols),
        )
