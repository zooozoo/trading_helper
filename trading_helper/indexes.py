"""공공데이터포털 금융위원회_지수시세정보 (V2) collector: KOSPI/KOSDAQ price indices as benchmarks.

Verified 2026-10-06: endpoint GetMarketIndexInfoService_V2/getStockMarketIndex_V2; `idxNm` exact match
with beginBasDt/endBasDt returns the whole range in one page (numOfRows=10000). Coverage from
2020-01-02. These are PRICE indices (dividends excluded): a strategy must beat them by roughly the
market dividend yield as well. No total-return KOSPI/KOSDAQ index is offered.
"""

from __future__ import annotations

import csv
import json
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path
from typing import Callable

from .datagokr import DataGoKrError, USER_AGENT, mask
from .opendart import RawStore

BASE_URL = "https://apis.data.go.kr/1160100/GetMarketIndexInfoService_V2/getStockMarketIndex_V2"
SOURCE_TAG = "data.go.kr:15094807:getStockMarketIndex_V2"
DEFAULT_INDEXES = ("코스피", "코스닥", "코스피 200", "코스닥 150", "코스피 소형주", "코스닥 소형주")
INDEX_COLUMNS = ["index", "date", "open", "high", "low", "close", "turnover_krw", "market_cap_krw", "source", "fetched_at"]

Fetcher = Callable[[str, dict], tuple[int, bytes]]


def urllib_fetcher(url: str, params: dict, *, timeout: float = 120.0) -> tuple[int, bytes]:
    req = urllib.request.Request(url + "?" + urllib.parse.urlencode(params), headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def parse_response(body: bytes) -> tuple[list[dict], str | None]:
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return [], f"non-JSON response: {body[:160]!r}"
    if "OpenAPI_ServiceResponse" in data:
        hdr = data["OpenAPI_ServiceResponse"].get("cmmMsgHeader", {})
        return [], f"gateway error {hdr.get('returnReasonCode')}: {hdr.get('returnAuthMsg') or hdr.get('errMsg')}"
    try:
        body_ = data["response"]["body"]
    except (KeyError, TypeError):
        return [], f"unexpected shape: {body[:160]!r}"
    items = body_.get("items")
    rows = items.get("item", []) if isinstance(items, dict) else (items or [])
    total = int(body_.get("totalCount", len(rows)) or 0)
    if total > len(rows):
        return rows, f"page truncated: totalCount {total} > returned {len(rows)}"
    return rows, None


def collect(key: str, store: RawStore, names: tuple[str, ...], start: date, end: date, *,
            fetcher: Fetcher = urllib_fetcher) -> dict:
    """One request per index name for the whole range. Overwrites the raw file for that name."""
    summary = {"indexes": {}, "requests_made": 0}
    for name in names:
        params = {"serviceKey": key, "resultType": "json", "numOfRows": 10000, "pageNo": 1, "idxNm": name,
                  "beginBasDt": start.strftime("%Y%m%d"), "endBasDt": end.strftime("%Y%m%d")}
        status, body = fetcher(BASE_URL, params)
        summary["requests_made"] += 1
        rows, err = parse_response(body)
        if err:
            raise DataGoKrError(mask(f"{name}: {err}", key))
        exact = [r for r in rows if r.get("idxNm") == name]
        store.save("index", f"{_slug(name)}_{start:%Y%m%d}_{end:%Y%m%d}.json", body,
                   {"endpoint": "getStockMarketIndex_V2", "params": {**params, "serviceKey": "***"},
                    "http_status": status, "rows": len(exact)})
        summary["indexes"][name] = len(exact)
    return summary


def _slug(name: str) -> str:
    return name.replace(" ", "_").replace("/", "-")


def normalize(store: RawStore, out_path: Path) -> dict:
    fetched = {Path(r["path"]).name: r["fetched_at"] for r in store.records() if r["kind"] == "index"}
    folder = store.root / "index"
    seen: dict[tuple[str, str], dict] = {}
    for path in sorted(folder.glob("*.json")):
        rows, err = parse_response(path.read_bytes())
        for r in rows:
            try:
                d = r["basDt"]
                row = {"index": r["idxNm"], "date": f"{d[:4]}-{d[4:6]}-{d[6:8]}", "open": float(r["mkp"]),
                       "high": float(r["hipr"]), "low": float(r["lopr"]), "close": float(r["clpr"]),
                       "turnover_krw": r.get("trPrc", ""), "market_cap_krw": r.get("lstgMrktTotAmt", ""),
                       "source": SOURCE_TAG, "fetched_at": fetched.get(path.name, "")}
            except (KeyError, ValueError):
                continue
            if row["close"] <= 0:
                continue
            seen[(row["index"], row["date"])] = row  # later files win (re-collection overwrites)
    out = sorted(seen.values(), key=lambda x: (x["index"], x["date"]))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=INDEX_COLUMNS)
        w.writeheader()
        w.writerows(out)
    by_index = {}
    for r in out:
        b = by_index.setdefault(r["index"], {"rows": 0, "date_min": r["date"], "date_max": r["date"]})
        b["rows"] += 1
        b["date_max"] = max(b["date_max"], r["date"])
    return {"rows": len(out), "by_index": by_index}


def load_index_series(path: Path, name: str) -> dict[date, float]:
    out = {}
    with Path(path).open(encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            if r["index"] == name:
                out[date.fromisoformat(r["date"])] = float(r["close"])
    return out
