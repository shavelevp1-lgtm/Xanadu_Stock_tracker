"""Fetch XNDU quotes and email a daily summary."""

import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from xanadu_stock import COMPANY_NAME, TICKER, get_xanadu_quotes


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


def build_email_body(nasdaq, tsx) -> str:
    lines = [
        COMPANY_NAME,
        f"Ticker: {TICKER}",
        "",
        _format_line("NASDAQ", nasdaq.price, nasdaq.currency, nasdaq.change, nasdaq.change_percent),
        _format_line("TSX", tsx.price, tsx.currency, tsx.change, tsx.change_percent),
        "",
        "Data via Yahoo Finance. Percent change vs previous close.",
    ]
    return "\n".join(lines)


def send_email(subject: str, body: str) -> None:
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASSWORD"]
    to_addr = os.environ["EMAIL_TO"]
    from_addr = os.environ.get("EMAIL_FROM", user)

    msg = MIMEMultipart()
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    with smtplib.SMTP(host, port) as server:
        server.starttls()
        server.login(user, password)
        server.sendmail(from_addr, [to_addr], msg.as_string())


def main() -> None:
    nasdaq, tsx = get_xanadu_quotes()
    body = build_email_body(nasdaq, tsx)
    subject = os.environ.get("EMAIL_SUBJECT") or build_email_subject(nasdaq)
    send_email(subject, body)
    print(f"Email sent. Subject: {subject}")


if __name__ == "__main__":
    main()
