"""Fetch XNDU quotes, SEC insider/filing watch, and email a daily summary."""

import os
import time
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
SEND_HOUR = int(os.environ.get("SEND_HOUR_TORONTO", "9"))
SEND_MINUTE = int(os.environ.get("SEND_MINUTE_TORONTO", "0"))
MAX_WAIT_SECONDS = int(os.environ.get("MAX_WAIT_SECONDS", str(7 * 3600)))


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
    """Basic gates: weekdays, once per day, manual/force bypass."""
    if os.environ.get("FORCE_SEND", "").lower() in ("1", "true", "yes"):
        return True, "forced send (test)"

    event = os.environ.get("GITHUB_EVENT_NAME", "")
    if event != "schedule":
        return True, "manual or local run"

    now = datetime.now(TORONTO)
    if now.weekday() >= 5:
        return False, "weekends disabled"

    today = now.strftime("%Y-%m-%d")
    if get_last_email_toronto_date() == today:
        return False, f"already sent today ({today})"

    return True, "scheduled run"


def wait_for_toronto_send_time() -> tuple[bool, str]:
    """
    Scheduled runs: sleep until 9 AM Toronto if the job starts early.
    If GitHub runs in the afternoon and nothing was sent yet, deliver anyway.
    """
    if os.environ.get("GITHUB_EVENT_NAME") != "schedule":
        return True, "not scheduled"
    if os.environ.get("FORCE_SEND", "").lower() in ("1", "true", "yes"):
        return True, "forced send"

    now = datetime.now(TORONTO)
    target = now.replace(hour=SEND_HOUR, minute=SEND_MINUTE, second=0, microsecond=0)

    if now < target:
        wait_secs = (target - now).total_seconds()
        if wait_secs > MAX_WAIT_SECONDS:
            return False, (
                f"job started too early to wait until {SEND_HOUR:02d}:{SEND_MINUTE:02d} "
                f"Toronto ({wait_secs / 3600:.1f}h away)"
            )
        print(
            f"Job started at {now:%H:%M} Toronto — "
            f"waiting {wait_secs / 60:.0f} min until {SEND_HOUR:02d}:{SEND_MINUTE:02d}..."
        )
        time.sleep(wait_secs)
        return True, f"sent at {SEND_HOUR:02d}:{SEND_MINUTE:02d} Toronto (after wait)"

    if now.hour < 12:
        return True, f"sent on time (Toronto {now:%H:%M})"

    return True, f"afternoon fallback (Toronto {now:%H:%M}) — GitHub ran late"


def main() -> None:
    ok, reason = should_send_today()
    if not ok:
        print(f"Email skipped: {reason}")
        return

    ok, timing = wait_for_toronto_send_time()
    if not ok:
        print(f"Email skipped: {timing}")
        return

    print(f"Sending email: {timing}")

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
