"""
Fetch Xanadu Quantum Technologies (XNDU) stock prices from NASDAQ and TSX.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

import yfinance as yf

COMPANY_NAME = "Xanadu Quantum Technologies Limited"
TICKER = "XNDU"
NASDAQ_SYMBOL = TICKER
TSX_SYMBOL = f"{TICKER}.TO"


@dataclass(frozen=True)
class Quote:
    exchange: str
    symbol: str
    currency: str
    price: float | None
    previous_close: float | None
    change: float | None
    change_percent: float | None
    market_time: datetime | None


def _to_float(value) -> float | None:
    if value is None or (isinstance(value, float) and value != value):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _market_time(info: dict) -> datetime | None:
    ts = info.get("regularMarketTime") or info.get("postMarketTime")
    if ts is None:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc)


def _quote_from_info(exchange: str, symbol: str, info: dict) -> Quote:
    price = _to_float(info.get("regularMarketPrice") or info.get("currentPrice"))
    previous_close = _to_float(info.get("regularMarketPreviousClose") or info.get("previousClose"))

    change = _to_float(info.get("regularMarketChange"))
    change_percent = _to_float(info.get("regularMarketChangePercent"))

    if change is None and price is not None and previous_close is not None:
        change = price - previous_close
    if change_percent is None and change is not None and previous_close:
        change_percent = (change / previous_close) * 100

    return Quote(
        exchange=exchange,
        symbol=symbol,
        currency=info.get("currency") or ("CAD" if exchange == "TSX" else "USD"),
        price=price,
        previous_close=previous_close,
        change=change,
        change_percent=change_percent,
        market_time=_market_time(info),
    )


def get_nasdaq_quote() -> Quote:
    """Latest XNDU quote from NASDAQ (via Yahoo Finance)."""
    ticker = yf.Ticker(NASDAQ_SYMBOL)
    return _quote_from_info("NASDAQ", NASDAQ_SYMBOL, ticker.info)


def get_tsx_quote() -> Quote:
    """Latest XNDU quote from TSX (via Yahoo Finance)."""
    ticker = yf.Ticker(TSX_SYMBOL)
    return _quote_from_info("TSX", TSX_SYMBOL, ticker.info)


def get_xanadu_quotes() -> tuple[Quote, Quote]:
    """Return (NASDAQ quote, TSX quote)."""
    return get_nasdaq_quote(), get_tsx_quote()


def _format_quote(quote: Quote) -> str:
    if quote.price is None:
        return f"{quote.exchange} ({quote.symbol}): price unavailable"

    lines = [
        f"{quote.exchange} ({quote.symbol}): {quote.price:.2f} {quote.currency}",
    ]
    if quote.change is not None and quote.change_percent is not None:
        sign = "+" if quote.change >= 0 else ""
        lines.append(
            f"  Change: {sign}{quote.change:.2f} ({sign}{quote.change_percent:.2f}%)"
        )
    if quote.previous_close is not None:
        lines.append(f"  Previous close: {quote.previous_close:.2f} {quote.currency}")
    if quote.market_time is not None:
        lines.append(f"  As of (UTC): {quote.market_time.isoformat()}")
    return "\n".join(lines)


def main() -> None:
    print(COMPANY_NAME)
    print(f"Ticker: {TICKER}\n")

    nasdaq, tsx = get_xanadu_quotes()
    print(_format_quote(nasdaq))
    print()
    print(_format_quote(tsx))


if __name__ == "__main__":
    main()
