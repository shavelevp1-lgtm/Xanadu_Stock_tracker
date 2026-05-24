"""Fetch XNDU quotes, SEC insider/filing watch, and email a daily summary."""

from datetime import datetime
from zoneinfo import ZoneInfo

from email_util import send_email
from monitor_ceo_filings import format_monitor_sections, run_monitor
from xanadu_stock import COMPANY_NAME, TICKER, get_xanadu_quotes

TORONTO = ZoneInfo("America/Toronto")


def _format_line(exchange: str, price, currency, change, change_percent) -> str:
    if price is None:
        return f"{exchange}: price unavailable"

    sign = "+" if change is not None and change >= 0 else ""
    pct = f"{sign}{change_percent:.2f}%" if change_percent is not None else "n/a"
    chg = f"{sign}{change:.2f}" if change is not None else "n/a"
    return f"{exchange}: {price:.2f} {currency}  ({chg}, {pct})"


def build_email_subject(nasdaq) -> str:
    """Subject line highlighting NASDAQ percent change."""
    if nasdaq.change_percent is not None:
        sign = "+" if nasdaq.change_percent >= 0 else ""
        pct = f"{sign}{nasdaq.change_percent:.2f}%"
    else:
        pct = "n/a"

    if nasdaq.price is not None:
        return f"XNDU: NASDAQ {pct} ({nasdaq.price:.2f} {nasdaq.currency})"
    return f"XNDU: NASDAQ {pct}"


def build_email_body(nasdaq, tsx, monitor_report) -> str:
    lines = [
        COMPANY_NAME,
        f"Ticker: {TICKER}",
        "",
        "=== STOCK PRICES ===",
        _format_line("NASDAQ", nasdaq.price, nasdaq.currency, nasdaq.change, nasdaq.change_percent),
        _format_line("TSX", tsx.price, tsx.currency, tsx.change, tsx.change_percent),
        "",
        "Data via Yahoo Finance. Percent change vs previous close.",
    ]
    lines.extend(format_monitor_sections(monitor_report))
    lines.extend(
        [
            "",
            f"Sent {datetime.now(TORONTO).strftime('%Y-%m-%d %H:%M')} Toronto time.",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    nasdaq, tsx = get_xanadu_quotes()
    monitor_report = run_monitor(update_state=True)
    body = build_email_body(nasdaq, tsx, monitor_report)
    subject = build_email_subject(nasdaq)
    send_email(subject, body)
    print(f"Email sent. Subject: {subject}")


if __name__ == "__main__":
    main()
