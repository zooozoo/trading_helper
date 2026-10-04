"""CSV validators for docs/DATA_CONTRACT.md. Stdlib only.

Validation never fills gaps: missing or malformed values are reported, not defaulted.
Errors make a file unusable; warnings mark rows that are valid but not signal-eligible
or that need manual review.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
import hashlib
import json
import math
from pathlib import Path

PRICE_COLUMNS = ["symbol", "date", "open", "high", "low", "close", "volume", "source", "fetched_at"]
EVENT_COLUMNS = ["event_id", "symbol", "receipt_date", "event_type", "contract_amount",
                 "annual_revenue", "revenue_available_date", "risk_approved",
                 "risk_available_date", "source_url", "fetched_at"]
# Only new_contract enters signals. Others are kept for linkage and later cancellation handling.
EVENT_TYPES = {"new_contract", "amendment", "cancellation", "withdrawal", "other"}
SCHEMA_VERSION = "1"


@dataclass
class Issue:
    level: str  # "error" | "warning"
    row: int | None  # 1-based data row number (header excluded); None = file level
    column: str | None
    message: str


@dataclass
class FileReport:
    path: str
    kind: str
    sha256: str | None = None
    rows: int = 0
    issues: list[Issue] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def error(self, row, column, message):
        self.issues.append(Issue("error", row, column, message))

    def warn(self, row, column, message):
        self.issues.append(Issue("warning", row, column, message))

    def to_dict(self) -> dict:
        return {"path": self.path, "kind": self.kind, "sha256": self.sha256, "rows": self.rows,
                "ok": self.ok, "error_count": len(self.errors),
                "warning_count": len(self.warnings), "stats": self.stats,
                "issues": [asdict(i) for i in self.issues]}


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_date(text: str) -> date | None:
    """Strict ISO YYYY-MM-DD only."""
    try:
        return date.fromisoformat(text) if len(text) == 10 else None
    except ValueError:
        return None


def parse_datetime(text: str) -> datetime | None:
    """ISO 8601 datetime. Returns None if not parseable. A bare date is accepted
    as midnight (naive) so callers can still compare dates; see fetched_at checks."""
    if not text:
        return None
    try:
        if len(text) == 10:
            return datetime.combine(date.fromisoformat(text), datetime.min.time())
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_number(text: str) -> float | None:
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _read_rows(path: Path, expected: list[str], report: FileReport) -> list[dict] | None:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f)
            try:
                header = next(reader)
            except StopIteration:
                report.error(None, None, "empty file: header row missing")
                return None
            if header != expected:
                report.error(None, None, f"header mismatch: expected {expected}, got {header}")
                return None
            rows = []
            for n, values in enumerate(reader, start=1):
                if not values or all(v.strip() == "" for v in values):
                    report.warn(n, None, "blank row ignored")
                    continue
                if len(values) != len(expected):
                    report.error(n, None, f"expected {len(expected)} fields, got {len(values)}")
                    continue
                rows.append({k: v.strip() for k, v in zip(expected, values)})
            return rows
    except FileNotFoundError:
        report.error(None, None, "file not found")
    except UnicodeDecodeError as exc:
        report.error(None, None, f"not valid UTF-8: {exc}")
    return None


def _check_symbol(report: FileReport, n: int, symbol: str) -> None:
    if not symbol:
        report.error(n, "symbol", "empty symbol")
    elif not (len(symbol) == 6 and symbol.isalnum()):
        report.warn(n, "symbol", f"symbol {symbol!r} is not a 6-character KRX code; verify")


def _check_fetched_at(report: FileReport, n: int, text: str, not_before: date | None,
                      same_day_warning: str | None) -> None:
    if not text:
        report.error(n, "fetched_at", "fetched_at required")
        return
    fetched = parse_datetime(text)
    if fetched is None:
        report.error(n, "fetched_at", f"fetched_at not ISO 8601: {text!r}")
        return
    if len(text) == 10:
        report.warn(n, "fetched_at", "fetched_at has no time component; record full timestamp")
    if not_before is not None:
        if fetched.date() < not_before:
            report.error(n, "fetched_at", f"fetched_at {fetched.date()} precedes {not_before}")
        elif fetched.date() == not_before and same_day_warning:
            report.warn(n, "fetched_at", same_day_warning)


def validate_prices(path: str | Path) -> FileReport:
    path = Path(path)
    report = FileReport(str(path), "prices")
    if path.exists():
        report.sha256 = sha256_of(path)
    rows = _read_rows(path, PRICE_COLUMNS, report)
    if rows is None:
        return report
    report.rows = len(rows)
    seen: dict[tuple[str, date], int] = {}
    per_symbol: dict[str, list[date]] = {}
    sources: set[str] = set()
    for n, r in enumerate(rows, start=1):
        _check_symbol(report, n, r["symbol"])
        d = parse_date(r["date"])
        if d is None:
            report.error(n, "date", f"date not ISO YYYY-MM-DD: {r['date']!r}")
        prices = {}
        for col in ("open", "high", "low", "close"):
            v = parse_number(r[col])
            if v is None or v <= 0:
                report.error(n, col, f"{col} must be a positive finite number: {r[col]!r}")
            else:
                prices[col] = v
        if len(prices) == 4:
            lo, hi = prices["low"], prices["high"]
            body_lo, body_hi = min(prices["open"], prices["close"]), max(prices["open"], prices["close"])
            if not lo <= body_lo <= body_hi <= hi:
                report.error(n, None, "OHLC bounds violated: need low<=open/close<=high")
        vol = r["volume"]
        if not vol.isdigit():
            report.error(n, "volume", f"volume must be a nonnegative integer: {vol!r}")
        elif int(vol) == 0:
            report.warn(n, "volume", "zero volume; check for halt or missing data")
        if not r["source"]:
            report.error(n, "source", "source required")
        else:
            sources.add(r["source"])
        _check_fetched_at(report, n, r["fetched_at"], d,
                          "fetched_at is the same day as the session; confirm the session had closed")
        if d is not None and r["symbol"]:
            key = (r["symbol"], d)
            if key in seen:
                report.error(n, None, f"duplicate symbol/date {key[0]} {key[1]} (first at row {seen[key]})")
            else:
                seen[key] = n
            per_symbol.setdefault(r["symbol"], []).append(d)
    for symbol, dates in per_symbol.items():
        if dates != sorted(dates):
            report.warn(None, "date", f"{symbol}: rows are not in ascending date order")
    report.stats = {"symbols": len(per_symbol), "sources": sorted(sources),
                    "date_min": min((d for ds in per_symbol.values() for d in ds), default=None),
                    "date_max": max((d for ds in per_symbol.values() for d in ds), default=None)}
    return report


def validate_events(path: str | Path, price_symbols: set[str] | None = None) -> FileReport:
    path = Path(path)
    report = FileReport(str(path), "events")
    if path.exists():
        report.sha256 = sha256_of(path)
    rows = _read_rows(path, EVENT_COLUMNS, report)
    if rows is None:
        return report
    report.rows = len(rows)
    ids: dict[str, int] = {}
    eligible = 0
    types: dict[str, int] = {}
    for n, r in enumerate(rows, start=1):
        signal_ok = True
        if not r["event_id"]:
            report.error(n, "event_id", "event_id required")
        elif r["event_id"] in ids:
            report.error(n, "event_id", f"duplicate event_id {r['event_id']} (first at row {ids[r['event_id']]})")
        else:
            ids[r["event_id"]] = n
        _check_symbol(report, n, r["symbol"])
        if price_symbols is not None and r["symbol"] and r["symbol"] not in price_symbols:
            report.warn(n, "symbol", "symbol has no rows in prices.csv")
        receipt = parse_date(r["receipt_date"])
        if receipt is None:
            report.error(n, "receipt_date", f"receipt_date not ISO: {r['receipt_date']!r}")
        et = r["event_type"]
        if et not in EVENT_TYPES:
            report.error(n, "event_type", f"unknown event_type {et!r}; allowed {sorted(EVENT_TYPES)}")
        else:
            types[et] = types.get(et, 0) + 1
            if et != "new_contract":
                signal_ok = False
        amount = parse_number(r["contract_amount"])
        if amount is None or amount <= 0:
            report.error(n, "contract_amount", f"contract_amount must be positive KRW: {r['contract_amount']!r}")
        # Revenue may be legitimately unknown: both fields empty => not eligible, not an error.
        if r["annual_revenue"] == "" and r["revenue_available_date"] == "":
            report.warn(n, "annual_revenue", "annual_revenue unknown; event is not signal-eligible")
            signal_ok = False
        else:
            revenue = parse_number(r["annual_revenue"])
            if revenue is None or revenue <= 0:
                report.error(n, "annual_revenue", f"annual_revenue must be positive KRW or empty: {r['annual_revenue']!r}")
            rad = parse_date(r["revenue_available_date"])
            if rad is None:
                report.error(n, "revenue_available_date", "required when annual_revenue is given; ISO date")
            elif receipt is not None and rad > receipt:
                report.warn(n, "revenue_available_date", "published after receipt_date; not usable for this event")
                signal_ok = False
        ra = r["risk_approved"]
        if ra not in ("true", "false"):
            report.error(n, "risk_approved", f"risk_approved must be literal true/false: {ra!r}")
        else:
            rd = r["risk_available_date"]
            if ra == "true":
                parsed = parse_date(rd)
                if parsed is None:
                    report.error(n, "risk_available_date", "required ISO date when risk_approved=true (latest source publication date)")
                elif receipt is not None and parsed > receipt:
                    report.warn(n, "risk_available_date", "risk evidence published after receipt_date; not usable")
                    signal_ok = False
            else:
                signal_ok = False
                if rd and parse_date(rd) is None:
                    report.error(n, "risk_available_date", f"not ISO date: {rd!r}")
        url = r["source_url"]
        if not url:
            report.error(n, "source_url", "source_url required")
        elif not (url.startswith("http://") or url.startswith("https://")):
            report.error(n, "source_url", f"source_url must be http(s): {url!r}")
        _check_fetched_at(report, n, r["fetched_at"], receipt, None)
        if signal_ok and not any(i.row == n and i.level == "error" for i in report.issues):
            eligible += 1
    report.stats = {"event_types": types, "signal_eligible_rows": eligible}
    return report


def validate_all(prices: str | Path | None, events: str | Path | None) -> dict:
    reports = []
    symbols = None
    if prices is not None:
        pr = validate_prices(prices)
        reports.append(pr)
        if pr.ok:
            symbols = _symbols_in(Path(prices))
    if events is not None:
        reports.append(validate_events(events, symbols))
    return {"schema_version": SCHEMA_VERSION,
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "ok": all(r.ok for r in reports),
            "files": [r.to_dict() for r in reports]}


def _symbols_in(path: Path) -> set[str]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        return {row["symbol"].strip() for row in reader if row.get("symbol")}


def format_text(result: dict) -> str:
    lines = [f"validation {'OK' if result['ok'] else 'FAILED'}  schema v{result['schema_version']}"]
    for f in result["files"]:
        lines.append(f"- {f['kind']}: {f['path']}  rows={f['rows']}  errors={f['error_count']}  "
                     f"warnings={f['warning_count']}  sha256={f['sha256']}")
        if f["stats"]:
            lines.append(f"  stats: {json.dumps(f['stats'], ensure_ascii=False, default=str)}")
        for i in f["issues"][:200]:
            where = "file" if i["row"] is None else f"row {i['row']}"
            col = f" [{i['column']}]" if i["column"] else ""
            lines.append(f"  {i['level'].upper():7} {where}{col}: {i['message']}")
        if len(f["issues"]) > 200:
            lines.append(f"  ... {len(f['issues']) - 200} more issues")
    return "\n".join(lines)
