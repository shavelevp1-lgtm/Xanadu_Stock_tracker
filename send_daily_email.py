"""Fetch XNDU quotes, SEC insider/filing watch, and email a daily summary."""

import os
from datetime import datetime
from zoneinfo import ZoneInfo

from email_util import send_email
from monitor_ceo_filings import (
    format_monitor_sections,
    get_last_email_toronto_date,
    run_monitor,
    set_last_email_toronto_date,
)
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


def should_send_today() -> tuple[bool, str]:
    """Gate scheduled runs: weekdays only, once per Toronto calendar day."""
    if os.environ.get("FORCE_SEND", "").lower() in ("1", "true", "yes"):
        return True, "forced send (test)"

    event = os.environ.get("GITHUB_EVENT_NAME", "")
    if event != "schedule":
        return True, "manual or local run"

    now = datetime.now(TORONTO)
    if now.weekday() >= 5:
        return False, "weekends disabled"

    # Only send during morning hours so a delayed GitHub run doesn't email at 3 PM.
    morning_start = int(os.environ.get("SEND_HOUR_START", "7"))
    morning_end = int(os.environ.get("SEND_HOUR_END", "11"))
    if not (morning_start <= now.hour <= morning_end):
        return False, (
            f"outside morning window (Toronto {now:%H:%M}, "
            f"window {morning_start:02d}:00–{morning_end:02d}:59)"
        )

    today = now.strftime("%Y-%m-%d")
    if get_last_email_toronto_date() == today:
        return False, f"already sent today ({today})"

    return True, f"scheduled send (Toronto {now:%Y-%m-%d %H:%M})"


def main() -> None:
    ok, reason = should_send_today()
    if not ok:
        print(f"Email skipped: {reason}")
        return

    print(f"Sending email: {reason}")

    nasdaq, tsx = get_xanadu_quotes()
    try:
        monitor_report = run_monitor(update_state=True)
    except Exception as exc:
        from monitor_ceo_filings import MonitorReport

        print(f"Monitor warning (email will still send): {exc}")
        monitor_report = MonitorReport(
            skipped=True,
            skip_reason=f"Monitor error: {exc}",
        )
    body = build_email_body(nasdaq, tsx, monitor_report)
    subject = build_email_subject(nasdaq)
    send_email(subject, body)
    set_last_email_toronto_date(datetime.now(TORONTO).strftime("%Y-%m-%d"))
    print(f"Email sent. Subject: {subject}")


if __name__ == "__main__":
    main()
