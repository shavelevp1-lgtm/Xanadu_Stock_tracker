"""
SEC filing and volume monitor for Xanadu (XNDU).

Tracks Form 144, Form 4, Form 6-K, Schedule 13D, unusual volume, and
large sales by any reporting owner (not only the CEO).

Used by send_daily_email.py; can also run standalone.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import yfinance as yf

# --- Xanadu ---
CIK = "0002097163"
CIK_INT = int(CIK)
TICKER = "XNDU"
CEO_NAME_MARKERS = ("weedbrook", "christian weedbrook")
INSIDER_KEYWORDS = (
    "insider",
    "disposition",
    "disposed",
    "acquired",
    "10b5",
    "10b5-1",
    "rule 144",
    "beneficial ownership",
    "securities of the company",
    "subordinate voting",
    "multiple voting",
)
SALE_KEYWORDS = re.compile(
    r"\b(sold|sale|dispose|disposed|disposition|decreased|reduced|decrease)\b",
    re.I,
)

STATE_FILE = Path(os.environ.get("MONITOR_STATE_FILE", "monitor_state.json"))
SEC_SUBMISSIONS_URL = f"https://data.sec.gov/submissions/CIK{CIK}.json"
SEC_ARCHIVES = "https://www.sec.gov/Archives/edgar/data"

VOLUME_SPIKE_RATIO = float(os.environ.get("VOLUME_SPIKE_RATIO", "2.0"))
VOLUME_AVG_DAYS = int(os.environ.get("VOLUME_AVG_DAYS", "20"))
VOLUME_SCAN_DAYS = int(os.environ.get("VOLUME_SCAN_DAYS", "5"))
FILING_LOOKBACK_DAYS = int(os.environ.get("FILING_LOOKBACK_DAYS", "14"))
LARGE_SALE_MIN_SHARES = int(os.environ.get("LARGE_SALE_MIN_SHARES", "100000"))
REQUEST_DELAY_SEC = float(os.environ.get("SEC_REQUEST_DELAY_SEC", "0.25"))


@dataclass
class FilingAlert:
    category: str
    form: str
    filing_date: str
    accession: str
    description: str
    url: str
    ceo_related: bool = False


@dataclass
class LargeSaleAlert:
    owner: str
    shares: float | None
    form: str
    filing_date: str
    accession: str
    description: str
    url: str
    is_ceo: bool = False


@dataclass
class VolumeAlert:
    trade_date: str
    volume: int
    avg_volume: int
    ratio: float
    close: float | None
    near_filing_dates: list[str]


@dataclass
class MonitorReport:
    filing_alerts: list[FilingAlert] = field(default_factory=list)
    large_sale_alerts: list[LargeSaleAlert] = field(default_factory=list)
    volume_alerts: list[VolumeAlert] = field(default_factory=list)
    seed_mode: bool = False
    skipped: bool = False
    skip_reason: str = ""


def _sec_headers() -> dict[str, str]:
    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    if not ua:
        raise RuntimeError(
            "SEC_USER_AGENT is required. Example: SEC_USER_AGENT=\"XNDU-Monitor you@example.com\""
        )
    return {"User-Agent": ua}


def _http_get(url: str, timeout: int = 30) -> bytes:
    req = Request(url, headers=_sec_headers())
    time.sleep(REQUEST_DELAY_SEC)
    with urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return raw


def _accession_path(accession: str) -> str:
    return accession.replace("-", "")


def filing_url(accession: str, primary_document: str | None = None) -> str:
    path = _accession_path(accession)
    if primary_document:
        return f"{SEC_ARCHIVES}/{CIK_INT}/{path}/{primary_document}"
    return f"{SEC_ARCHIVES}/{CIK_INT}/{path}/{accession}-index.htm"


def _load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {"seen_accessions": [], "alerted_volume_dates": [], "initialized": False}
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def _save_state(state: dict[str, Any]) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def get_last_email_toronto_date() -> str | None:
    return _load_state().get("last_email_toronto_date")


def set_last_email_toronto_date(date_str: str) -> None:
    state = _load_state()
    state["last_email_toronto_date"] = date_str
    _save_state(state)


def _fetch_submissions() -> dict[str, Any]:
    return json.loads(_http_get(SEC_SUBMISSIONS_URL))


def _normalize_forms(form: str) -> str:
    return form.upper().strip()


def _is_form_144(form: str) -> bool:
    f = _normalize_forms(form)
    return f in ("144", "144/A") or f.startswith("144/")


def _is_form_4(form: str) -> bool:
    return _normalize_forms(form) in ("4", "4/A")


def _is_form_6k(form: str) -> bool:
    return _normalize_forms(form) == "6-K"


def _is_schedule_13d(form: str) -> bool:
    f = _normalize_forms(form)
    return "13D" in f and "13G" not in f


def _filing_category(form: str) -> str | None:
    if _is_form_144(form):
        return "form_144"
    if _is_form_4(form):
        return "form_4"
    if _is_form_6k(form):
        return "form_6k"
    if _is_schedule_13d(form):
        return "schedule_13d"
    return None


def _mentions_ceo(text: str) -> bool:
    lower = text.lower()
    return any(m in lower for m in CEO_NAME_MARKERS)


def _mentions_insider_activity(text: str) -> bool:
    lower = text.lower()
    return any(k in lower for k in INSIDER_KEYWORDS)


def _fetch_filing_text(accession: str, primary_document: str | None) -> str:
    chunks: list[str] = []
    path = _accession_path(accession)

    if primary_document:
        try:
            chunks.append(
                _http_get(f"{SEC_ARCHIVES}/{CIK_INT}/{path}/{primary_document}").decode(
                    "utf-8", errors="replace"
                )
            )
        except (HTTPError, URLError) as exc:
            chunks.append(f"[Could not fetch primary doc: {exc}]")

    try:
        chunks.append(
            _http_get(f"{SEC_ARCHIVES}/{CIK_INT}/{path}/{accession}.txt").decode(
                "utf-8", errors="replace"
            )
        )
    except (HTTPError, URLError):
        pass

    return "\n".join(chunks)


def _owner_from_filing(text: str) -> str:
    for pattern in (
        r"<rptOwnerName>([^<]+)</rptOwnerName>",
        r"reportingPersonName.*?>([^<]+)<",
        r"Name of Reporting Person[:\s]*([^\n<]+)",
        r"NAME OF REPORTING PERSON[:\s]*([^\n<]+)",
    ):
        m = re.search(pattern, text, re.I | re.S)
        if m:
            name = m.group(1).strip()
            if name and name.lower() not in ("see appendix", "n/a"):
                return name
    return "Unknown reporting owner"


def _parse_form4_sales(text: str) -> list[tuple[str, float, str]]:
    owner = _owner_from_filing(text)
    sales: list[tuple[str, float, str]] = []

    blocks = re.findall(
        r"<nonDerivativeTransaction>(.*?)</nonDerivativeTransaction>",
        text,
        re.I | re.S,
    )
    if not blocks:
        blocks = [text]

    for block in blocks:
        code_m = re.search(r"<transactionCode>([^<]+)</transactionCode>", block, re.I)
        disp_m = re.search(
            r"<transactionAcquiredDisposedCode>([^<]+)</transactionAcquiredDisposedCode>",
            block,
            re.I,
        )
        shares_m = re.search(r"<transactionShares>([^<]+)</transactionShares>", block, re.I)
        if not shares_m:
            continue
        try:
            shares = float(shares_m.group(1).replace(",", "").strip())
        except ValueError:
            continue
        code = (code_m.group(1).strip().upper() if code_m else "")
        disp = (disp_m.group(1).strip().upper() if disp_m else "")
        if disp == "D" or code in ("S", "F"):
            sales.append((owner, shares, code or disp or "sale"))

    return sales


def _parse_form144_units(text: str) -> float | None:
    patterns = (
        r"noOfUnitsSold[^0-9]*([\d,]+)",
        r"aggregateNoOfUnits[^0-9]*([\d,]+)",
        r"units?\s+to\s+be\s+sold[^0-9]*([\d,]+)",
        r"amount\s+of\s+securities[^0-9]*([\d,]+)",
    )
    best = 0.0
    for pat in patterns:
        for m in re.finditer(pat, text, re.I):
            try:
                val = float(m.group(1).replace(",", ""))
                best = max(best, val)
            except ValueError:
                pass
    return best if best > 0 else None


def _parse_large_share_mentions(text: str) -> list[tuple[str, float]]:
    """Heuristic extraction of large share counts near sale language."""
    found: list[tuple[str, float]] = []
    owner = _owner_from_filing(text)
    for m in re.finditer(
        r"([\d,]{6,})\s+(?:class\s+b\s+)?(?:subordinate\s+voting\s+)?shares",
        text,
        re.I,
    ):
        if not SALE_KEYWORDS.search(text[max(0, m.start() - 400) : m.end() + 200]):
            continue
        try:
            shares = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        if shares >= LARGE_SALE_MIN_SHARES:
            found.append((owner, shares))
    return found


def _detect_large_sales(
    category: str,
    form: str,
    text: str,
    filing_date: str,
    accession: str,
    url: str,
) -> list[LargeSaleAlert]:
    alerts: list[LargeSaleAlert] = []
    is_ceo = _mentions_ceo(text)

    if category == "form_4":
        for owner, shares, code in _parse_form4_sales(text):
            if shares >= LARGE_SALE_MIN_SHARES:
                alerts.append(
                    LargeSaleAlert(
                        owner=owner,
                        shares=shares,
                        form=form,
                        filing_date=filing_date,
                        accession=accession,
                        description=f"Form 4 sale/disposition: {shares:,.0f} shares (code {code})",
                        url=url,
                        is_ceo=_mentions_ceo(text) or _mentions_ceo(owner),
                    )
                )

    if category == "form_144":
        units = _parse_form144_units(text)
        if units and units >= LARGE_SALE_MIN_SHARES:
            owner = _owner_from_filing(text)
            alerts.append(
                LargeSaleAlert(
                    owner=owner,
                    shares=units,
                    form=form,
                    filing_date=filing_date,
                    accession=accession,
                    description=f"Form 144 proposed sale: {units:,.0f} shares",
                    url=url,
                    is_ceo=is_ceo or _mentions_ceo(owner),
                )
            )

    if category == "schedule_13d" and SALE_KEYWORDS.search(text):
        owner = _owner_from_filing(text)
        for owner2, shares in _parse_large_share_mentions(text):
            alerts.append(
                LargeSaleAlert(
                    owner=owner2 if owner2 != "Unknown reporting owner" else owner,
                    shares=shares,
                    form=form,
                    filing_date=filing_date,
                    accession=accession,
                    description=f"Schedule 13D: large stake change / sale language ({shares:,.0f} shares cited)",
                    url=url,
                    is_ceo=_mentions_ceo(owner2) or _mentions_ceo(owner),
                )
            )
        if not alerts and "13D/A" in form.upper():
            alerts.append(
                LargeSaleAlert(
                    owner=owner,
                    shares=None,
                    form=form,
                    filing_date=filing_date,
                    accession=accession,
                    description="Schedule 13D/A amendment with sale/decrease language — review filing",
                    url=url,
                    is_ceo=is_ceo,
                )
            )

    if category == "form_6k" and SALE_KEYWORDS.search(text):
        for owner, shares in _parse_large_share_mentions(text):
            alerts.append(
                LargeSaleAlert(
                    owner=owner,
                    shares=shares,
                    form=form,
                    filing_date=filing_date,
                    accession=accession,
                    description=f"6-K: large disposition language ({shares:,.0f} shares)",
                    url=url,
                    is_ceo=_mentions_ceo(owner) or is_ceo,
                )
            )

    return alerts


def _should_alert_6k(accession: str, primary_document: str | None) -> tuple[bool, str]:
    text = _fetch_filing_text(accession, primary_document)
    ceo = _mentions_ceo(text)
    insider = _mentions_insider_activity(text)
    large_sales = _detect_large_sales("form_6k", "6-K", text, "", accession, "")
    if large_sales:
        owners = ", ".join({a.owner for a in large_sales})
        return True, f"Large owner sale signal ({owners})"
    if ceo and insider:
        return True, "CEO name and insider-trading keywords found"
    if ceo:
        return True, "CEO name found in filing"
    if insider and re.search(r"\bceo\b", text, re.I):
        return True, "Insider keywords and CEO role reference found"
    return False, ""


def _parse_date(s: str) -> datetime | None:
    try:
        return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return None


def _iter_recent_filings(submissions: dict[str, Any]) -> list[dict[str, str]]:
    recent = submissions["filings"]["recent"]
    keys = list(recent.keys())
    n = len(recent["accessionNumber"])
    return [{k: recent[k][i] for k in keys} for i in range(n)]


def _scan_filings(
    rows: list[dict[str, str]],
    seen: set[str],
    *,
    seed_mode: bool,
) -> tuple[list[FilingAlert], list[LargeSaleAlert]]:
    cutoff = datetime.now(UTC) - timedelta(days=FILING_LOOKBACK_DAYS)
    filing_alerts: list[FilingAlert] = []
    large_sale_alerts: list[LargeSaleAlert] = []

    for row in rows:
        accession = row["accessionNumber"]
        if accession in seen:
            continue

        form = row.get("form", "")
        category = _filing_category(form)
        if not category:
            continue

        filing_date = row.get("filingDate", "")
        fdate = _parse_date(filing_date)
        if fdate and fdate < cutoff:
            seen.add(accession)
            continue

        if seed_mode:
            seen.add(accession)
            continue

        primary = row.get("primaryDocument")
        url = filing_url(accession, primary)
        text = _fetch_filing_text(accession, primary)
        ceo_related = _mentions_ceo(text)
        description = row.get("primaryDocDescription") or form

        large_sale_alerts.extend(
            _detect_large_sales(category, form, text, filing_date, accession, url)
        )

        include_filing = False

        if category == "form_144":
            include_filing = True
            description = "Form 144: notice of proposed sale (Rule 144 / 10b5-1 plan)"
        elif category == "form_4":
            include_filing = True
            sales = _parse_form4_sales(text)
            if sales:
                total = sum(s for _, s, _ in sales)
                owners = ", ".join({o for o, _, _ in sales})
                description = f"Form 4: {owners} — {total:,.0f} shares reported"
            else:
                description = "Form 4: insider ownership change (review filing)"
            if ceo_related:
                description += " [CEO Weedbrook]"
        elif category == "form_6k":
            alert, reason = _should_alert_6k(accession, primary)
            if not alert:
                if not any(a.accession == accession for a in large_sale_alerts):
                    seen.add(accession)
                    continue
            else:
                description = f"{description} — {reason}"
            include_filing = alert or bool(
                [a for a in large_sale_alerts if a.accession == accession]
            )
        elif category == "schedule_13d":
            include_filing = True
            description = (
                "Schedule 13D: beneficial ownership >5%"
                + (" [CEO mentioned]" if ceo_related else "")
            )

        if not include_filing and not any(a.accession == accession for a in large_sale_alerts):
            seen.add(accession)
            continue

        if include_filing:
            filing_alerts.append(
                FilingAlert(
                    category=category,
                    form=form,
                    filing_date=filing_date,
                    accession=accession,
                    description=description,
                    url=url,
                    ceo_related=ceo_related,
                )
            )
        seen.add(accession)

    return filing_alerts, large_sale_alerts


def _scan_volume(
    state: dict[str, Any],
    recent_filing_dates: list[str],
    *,
    seed_mode: bool,
) -> list[VolumeAlert]:
    hist = yf.Ticker(TICKER).history(period="3mo", auto_adjust=False)
    if hist.empty or "Volume" not in hist.columns:
        return []

    hist = hist.sort_index()
    alerted: set[str] = set(state.get("alerted_volume_dates", []))
    alerts: list[VolumeAlert] = []

    for i in range(-VOLUME_SCAN_DAYS, 0):
        if len(hist) + i < VOLUME_AVG_DAYS:
            continue
        row = hist.iloc[i]
        trade_date = row.name.strftime("%Y-%m-%d")
        volume = int(row["Volume"])
        window = hist.iloc[i - VOLUME_AVG_DAYS : i]
        if window.empty:
            continue
        avg_vol = int(window["Volume"].mean())
        if avg_vol <= 0:
            continue
        ratio = volume / avg_vol
        if ratio < VOLUME_SPIKE_RATIO or trade_date in alerted:
            continue

        near = [
            d
            for d in recent_filing_dates
            if d
            and _parse_date(trade_date)
            and _parse_date(d)
            and abs((_parse_date(trade_date) - _parse_date(d)).days) <= 3
        ]

        if seed_mode:
            alerted.add(trade_date)
            continue

        close = float(row["Close"]) if row["Close"] == row["Close"] else None
        alerts.append(
            VolumeAlert(
                trade_date=trade_date,
                volume=volume,
                avg_volume=avg_vol,
                ratio=ratio,
                close=close,
                near_filing_dates=near,
            )
        )
        alerted.add(trade_date)

    state["alerted_volume_dates"] = sorted(alerted)
    return alerts


def format_monitor_sections(report: MonitorReport) -> list[str]:
    """Plain-text sections for inclusion in the daily email."""
    if report.skipped:
        return [
            "",
            "=== INSIDER & FILING WATCH ===",
            f"(skipped: {report.skip_reason})",
        ]

    if report.seed_mode:
        return [
            "",
            "=== INSIDER & FILING WATCH ===",
            "(first run — existing filings recorded; alerts start tomorrow)",
        ]

    lines = [
        "",
        "=== INSIDER & FILING WATCH (new since last check) ===",
    ]

    if not report.filing_alerts and not report.large_sale_alerts and not report.volume_alerts:
        lines.append("(no new SEC filings, large owner sales, or volume spikes)")
        lines.append(
            f"Threshold: single-owner sales flagged at {LARGE_SALE_MIN_SHARES:,}+ shares."
        )
        return lines

    if report.filing_alerts:
        labels = {
            "form_144": "Form 144 — intent to sell",
            "form_4": "Form 4 — insider transactions",
            "form_6k": "Form 6-K — CEO / insider",
            "schedule_13d": "Schedule 13D — major holders",
        }
        lines.append("")
        lines.append("SEC filings:")
        by_cat: dict[str, list[FilingAlert]] = {}
        for a in report.filing_alerts:
            by_cat.setdefault(a.category, []).append(a)
        for cat in ("form_144", "form_4", "form_6k", "schedule_13d"):
            for a in by_cat.get(cat, []):
                lines.append(f"  • [{labels.get(cat, cat)}] {a.form} ({a.filing_date})")
                lines.append(f"    {a.description}")
                lines.append(f"    {a.url}")

    if report.large_sale_alerts:
        lines.append("")
        lines.append(
            f"Large owner sales ({LARGE_SALE_MIN_SHARES:,}+ shares or 13D sale language):"
        )
        seen_desc: set[str] = set()
        for a in report.large_sale_alerts:
            key = f"{a.accession}:{a.owner}:{a.shares}"
            if key in seen_desc:
                continue
            seen_desc.add(key)
            ceo_tag = " [CEO]" if a.is_ceo else ""
            sh = f"{a.shares:,.0f} shares" if a.shares else "see filing"
            lines.append(f"  • {a.owner}{ceo_tag}: {sh} — {a.form} ({a.filing_date})")
            lines.append(f"    {a.description}")
            lines.append(f"    {a.url}")

    if report.volume_alerts:
        lines.append("")
        lines.append("Unusual NASDAQ volume:")
        for v in report.volume_alerts:
            lines.append(
                f"  • {v.trade_date}: {v.volume:,} shares "
                f"({v.ratio:.1f}x vs {VOLUME_AVG_DAYS}-day avg {v.avg_volume:,})"
            )
            if v.close is not None:
                lines.append(f"    Close: ${v.close:.2f}")
            if v.near_filing_dates:
                lines.append(f"    Near filing date(s): {', '.join(v.near_filing_dates)}")

    lines.append("")
    lines.append(
        "Note: Canadian insider trades may appear on SEDI before SEC. "
        f"Large-sale threshold = {LARGE_SALE_MIN_SHARES:,} shares."
    )
    return lines


def run_monitor(*, update_state: bool = True) -> MonitorReport:
    try:
        state = _load_state()
    except Exception:
        state = {"seen_accessions": [], "alerted_volume_dates": [], "initialized": False}

    seen = set(state.get("seen_accessions", []))
    seed_mode = not state.get("initialized", False)

    try:
        submissions = _fetch_submissions()
    except RuntimeError as exc:
        return MonitorReport(skipped=True, skip_reason=str(exc))
    except (HTTPError, URLError, OSError) as exc:
        return MonitorReport(skipped=True, skip_reason=f"SEC fetch failed: {exc}")

    rows = _iter_recent_filings(submissions)
    filing_alerts, large_sale_alerts = _scan_filings(rows, seen, seed_mode=seed_mode)
    recent_dates = [r.get("filingDate", "") for r in rows[:30]]
    volume_alerts = _scan_volume(state, recent_dates, seed_mode=seed_mode)

    state["seen_accessions"] = sorted(seen)
    if seed_mode:
        state["initialized"] = True

    if update_state:
        _save_state(state)

    return MonitorReport(
        filing_alerts=filing_alerts,
        large_sale_alerts=large_sale_alerts,
        volume_alerts=volume_alerts,
        seed_mode=seed_mode,
    )


def main() -> None:
    from email_util import send_email

    report = run_monitor()
    if report.skipped:
        print(report.skip_reason)
        raise SystemExit(1)

    if report.seed_mode:
        print("Initialized monitor state. No standalone email on first run.")
        return

    if not (report.filing_alerts or report.large_sale_alerts or report.volume_alerts):
        print("No new alerts.")
        if os.environ.get("ALERT_ONLY_IF_CHANGES", "true").lower() in ("1", "true", "yes"):
            return

    body = "\n".join(format_monitor_sections(report))
    subject = "XNDU insider/filing alert"
    if report.large_sale_alerts:
        a = report.large_sale_alerts[0]
        sh = f"{a.shares:,.0f}" if a.shares else "sale"
        subject = f"XNDU alert: {a.owner} {sh} shares"
    send_email(subject, body)
    print(f"Sent: {subject}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Monitor failed: {exc}", file=sys.stderr)
        if os.environ.get("EMAIL_ON_ERROR", "").lower() in ("1", "true", "yes"):
            from email_util import send_email

            send_email("XNDU monitor ERROR", str(exc))
        raise SystemExit(1) from exc
