"""공공데이터포털 금융위원회_주식시세정보 (V2) collector and normalizer. Stdlib only.

Facts verified by live probes on 2026-10-05 (see docs/DATA_SOURCES.md):
- Endpoint GetStockSecuritiesInfoService_V2/getStockPriceInfo_V2; one request per trading day
  returns every listed stock that day (~2,500-2,800 rows, numOfRows=10000 is honored).
- Coverage starts 2020-01-02. Holidays return 0 rows. Delisted stocks appear on the days they
  were listed (daily snapshots). ETFs are absent; preferred stocks and SPACs are present.
- Prices are RAW (unadjusted). A halted stock still has a row with mkp/hipr/lopr/trqu = 0 and
  clpr = last close. Listed share count (lstgStCnt) changes reveal splits/issues.

Raw JSON per day is preserved with a manifest; normalization writes DATA_CONTRACT prices.csv
plus securities, calendar, halts and share-count-change files. Nothing is adjusted here.
"""

from __future__ import annotations

import csv
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Callable

from .opendart import RawStore, now_iso
from .validate import PRICE_COLUMNS

ENV_KEY = "DATA_GO_KR_API_KEY"
BASE_URL = "https://apis.data.go.kr/1160100/GetStockSecuritiesInfoService_V2/getStockPriceInfo_V2"
SOURCE_TAG = "data.go.kr:15094808:getStockPriceInfo_V2"
COVERAGE_START = date(2020, 1, 2)
USER_AGENT = "trading_helper/0.0.1 (research; stdlib urllib)"
PAGE_SIZE = 10000  # whole market fits in one page

SECURITY_COLUMNS = ["symbol", "isin", "name", "market", "first_seen", "last_seen", "sessions",
                    "is_common_stock", "exclusion_reason"]
CALENDAR_COLUMNS = ["date", "rows", "status"]
HALT_COLUMNS = ["symbol", "date", "close", "source", "fetched_at"]
SHARE_CHANGE_COLUMNS = ["symbol", "date", "prev_date", "shares_before", "shares_after", "ratio",
                        "close_before", "close_after", "note"]


class DataGoKrError(Exception):
    pass


def get_api_key(env: dict | None = None) -> str:
    env = os.environ if env is None else env
    key = env.get(ENV_KEY, "").strip()
    if not key:
        raise DataGoKrError(f"{ENV_KEY} is not set. Export it in your shell; it is never printed or stored.")
    return key


def mask(text: str, key: str) -> str:
    return text.replace(key, "***") if key else text


Fetcher = Callable[[str, dict], tuple[int, bytes]]


def urllib_fetcher(url: str, params: dict, *, timeout: float = 90.0) -> tuple[int, bytes]:
    req = urllib.request.Request(url + "?" + urllib.parse.urlencode(params), headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


@dataclass
class Client:
    key: str
    store: RawStore
    fetcher: Fetcher = urllib_fetcher
    min_interval_s: float = 0.2
    retries: int = 3
    sleep: Callable[[float], None] = time.sleep
    _last_call: float = field(default=0.0, repr=False)

    def fetch_day(self, day: date) -> list[dict]:
        """Fetch and persist one day's whole-market snapshot. Returns rows (possibly empty)."""
        params = {"serviceKey": self.key, "resultType": "json", "numOfRows": PAGE_SIZE, "pageNo": 1,
                  "basDt": day.strftime("%Y%m%d")}
        masked = {k: ("***" if k == "serviceKey" else v) for k, v in params.items()}
        last_exc: Exception | None = None
        for attempt in range(1, self.retries + 1):
            wait = self.min_interval_s - (time.monotonic() - self._last_call)
            if wait > 0:
                self.sleep(wait)
            try:
                status, body = self.fetcher(BASE_URL, params)
                self._last_call = time.monotonic()
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_exc = DataGoKrError(mask(f"network error {day}: {exc}", self.key))
                self.sleep(min(2 ** attempt, 10))
                continue
            if status >= 500:
                last_exc = DataGoKrError(f"HTTP {status} on {day}")
                self.sleep(min(2 ** attempt, 10))
                continue
            rows, err = parse_response(body)
            if err:
                raise DataGoKrError(mask(f"{day}: {err}", self.key))
            self.store.save("daily", f"{day:%Y%m%d}.json", body,
                            {"endpoint": "getStockPriceInfo_V2", "params": masked, "http_status": status,
                             "rows": len(rows)})
            return rows
        raise last_exc or DataGoKrError(f"fetch failed {day}")


def parse_response(body: bytes) -> tuple[list[dict], str | None]:
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return [], f"non-JSON response: {body[:160]!r}"
    if "OpenAPI_ServiceResponse" in data:  # gateway-level error (key, quota, unknown service)
        hdr = data["OpenAPI_ServiceResponse"].get("cmmMsgHeader", {})
        return [], f"gateway error {hdr.get('returnReasonCode')}: {hdr.get('returnAuthMsg') or hdr.get('errMsg')}"
    try:
        header = data["response"]["header"]
        body_ = data["response"]["body"]
    except (KeyError, TypeError):
        return [], f"unexpected shape: {body[:160]!r}"
    if header.get("resultCode") != "00":
        return [], f"resultCode {header.get('resultCode')}: {header.get('resultMsg')}"
    items = body_.get("items")
    rows = items.get("item", []) if isinstance(items, dict) else (items or [])
    total = int(body_.get("totalCount", len(rows)) or 0)
    if total > len(rows):
        return rows, f"page truncated: totalCount {total} > returned {len(rows)}"
    return rows, None


def weekdays(start: date, end: date) -> list[date]:
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def collect(client: Client, start: date, end: date, *, max_requests: int) -> dict:
    """Fetch every weekday in [start, end] not already in the store. Resumable."""
    if start < COVERAGE_START:
        start = COVERAGE_START
    done = skipped = 0
    empty_days = []
    stopped = None
    for day in weekdays(start, end):
        if (client.store.root / "daily" / f"{day:%Y%m%d}.json").exists():
            skipped += 1
            continue
        if done >= max_requests:
            stopped = f"--max-requests {max_requests} reached; rerun to resume"
            break
        rows = client.fetch_day(day)
        done += 1
        if not rows:
            empty_days.append(day.isoformat())
    return {"start": start.isoformat(), "end": end.isoformat(), "requests_made": done,
            "days_skipped_existing": skipped, "empty_weekdays_this_run": empty_days,
            **({"stopped": stopped} if stopped else {})}


# --------------------------------------------------------------------------- normalization

PREFERRED_NAME = re.compile(r"(우|우B|우C|우\(신형\)|\d우)$")
SPAC_NAME = re.compile(r"스팩|SPAC", re.I)


def classify_security(isin: str, name: str) -> tuple[bool, str]:
    """(is_common_stock, exclusion_reason). ISIN char 8 is 0 for common shares; 1+ for preferred."""
    if SPAC_NAME.search(name or ""):
        return False, "spac"
    if len(isin) == 12 and isin[8] != "0":
        return False, "preferred_isin"
    if PREFERRED_NAME.search(name or ""):
        return False, "preferred_name"
    return True, ""


def load_days(store: RawStore) -> dict[date, tuple[list[dict], str]]:
    """{day: (rows, fetched_at)} from saved daily files, using the manifest for fetched_at."""
    fetched = {}
    for rec in store.records():
        if rec["kind"] == "daily":
            fetched[Path(rec["path"]).stem] = rec["fetched_at"]
    out = {}
    for path in sorted((store.root / "daily").glob("*.json")):
        rows, err = parse_response(path.read_bytes())
        if err and not rows:
            continue
        d = date(int(path.stem[:4]), int(path.stem[4:6]), int(path.stem[6:8]))
        out[d] = (rows, fetched.get(path.stem, now_iso()))
    return out


def normalize(store: RawStore, out_dir: Path) -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    days = load_days(store)
    prices, halts, calendar, changes = [], [], [], []
    securities: dict[str, dict] = {}
    last_seen: dict[str, tuple[date, str, str]] = {}  # symbol -> (date, shares, close)
    counts = {"days": len(days), "trading_days": 0, "no_row_weekdays": 0, "price_rows": 0,
              "halt_rows": 0, "share_count_changes": 0, "invalid_rows": 0}
    for day in sorted(days):
        rows, fetched_at = days[day]
        iso = day.isoformat()
        if not rows:
            calendar.append({"date": iso, "rows": 0, "status": "no_rows_holiday_or_missing"})
            counts["no_row_weekdays"] += 1
            continue
        calendar.append({"date": iso, "rows": len(rows), "status": "trading_day"})
        counts["trading_days"] += 1
        for r in rows:
            sym, isin, name, market = r["srtnCd"], r.get("isinCd", ""), r.get("itmsNm", ""), r.get("mrktCtg", "")
            sec = securities.get(sym)
            if sec is None:
                common, why = classify_security(isin, name)
                sec = securities[sym] = {"symbol": sym, "isin": isin, "name": name, "market": market,
                                         "first_seen": iso, "last_seen": iso, "sessions": 0,
                                         "is_common_stock": "true" if common else "false", "exclusion_reason": why}
            sec["last_seen"], sec["name"], sec["market"] = iso, name, market
            sec["sessions"] += 1
            try:
                o, h, l, c, v = int(r["mkp"]), int(r["hipr"]), int(r["lopr"]), int(r["clpr"]), int(r["trqu"])
                shares = r.get("lstgStCnt", "")
            except (KeyError, ValueError):
                counts["invalid_rows"] += 1
                continue
            prev = last_seen.get(sym)
            if prev and shares and prev[1] and shares != prev[1] and int(prev[1]) > 0:
                ratio = int(shares) / int(prev[1])
                changes.append({"symbol": sym, "date": iso, "prev_date": prev[0].isoformat(),
                                "shares_before": prev[1], "shares_after": shares, "ratio": f"{ratio:.6f}",
                                "close_before": prev[2], "close_after": c,
                                "note": "split_or_merge_candidate" if ratio >= 1.5 or ratio <= 0.67 else "share_count_change"})
                counts["share_count_changes"] += 1
            last_seen[sym] = (day, shares, str(c))
            if o == 0 and h == 0 and l == 0:
                halts.append({"symbol": sym, "date": iso, "close": c, "source": SOURCE_TAG, "fetched_at": fetched_at})
                counts["halt_rows"] += 1
                continue
            if not (0 < l <= min(o, c) <= max(o, c) <= h) or v < 0:
                counts["invalid_rows"] += 1
                continue
            prices.append({"symbol": sym, "date": iso, "open": o, "high": h, "low": l, "close": c,
                           "volume": v, "source": SOURCE_TAG, "fetched_at": fetched_at})
            counts["price_rows"] += 1
    _write_csv(out_dir / "prices.csv", PRICE_COLUMNS, prices)
    _write_csv(out_dir / "securities.csv", SECURITY_COLUMNS, sorted(securities.values(), key=lambda s: s["symbol"]))
    _write_csv(out_dir / "calendar.csv", CALENDAR_COLUMNS, calendar)
    _write_csv(out_dir / "halts.csv", HALT_COLUMNS, halts)
    _write_csv(out_dir / "share_count_changes.csv", SHARE_CHANGE_COLUMNS, changes)
    counts["securities"] = len(securities)
    counts["common_stocks"] = sum(1 for s in securities.values() if s["is_common_stock"] == "true")
    summary = {"counts": counts, "source": SOURCE_TAG, "prices_are_raw_unadjusted": True,
               "date_min": min(days).isoformat() if days else None, "date_max": max(days).isoformat() if days else None,
               "note": "no_rows weekdays must be cross-checked against the official KRX holiday list before use as a calendar"}
    (out_dir / "prices_normalize_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def _write_csv(path: Path, columns: list[str], rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in columns})
