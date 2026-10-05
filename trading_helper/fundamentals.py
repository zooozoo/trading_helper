"""OpenDART key-account fundamentals (fnlttMultiAcnt, up to 100 companies per call). Stdlib only.

Point-in-time rule: a report's figures become usable on its filing date, taken from the rcept_no
date prefix (YYYYMMDD; cross-checked against list data when available). The API serves the
currently effective figures, so later corrections may replace originals: rcept_no is recorded
so such rows can be audited, and this is listed as a limitation in docs/DATA_SOURCES.md.
"""

from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path

from .opendart import Client, OpenDartError, RawStore, STATUS_NO_DATA, fetch_corp_codes, parse_corp_codes

REPRT_CODES = {"11013": "Q1", "11012": "H1", "11014": "Q3", "11011": "FY"}
ACCOUNTS = ("매출액", "영업이익", "당기순이익(손실)", "자산총계", "부채총계", "자본총계", "자본금", "이익잉여금",
            "유동자산", "유동부채")
FUND_COLUMNS = ["symbol", "corp_code", "bsns_year", "reprt_code", "period", "fs_div", "sj_div", "account_nm",
                "thstrm_amount", "frmtrm_amount", "thstrm_dt", "rcept_no", "filing_date", "fetched_at"]
BATCH = 100


def corp_map(client: Client, store: RawStore, symbols: set[str]) -> dict[str, str]:
    """symbol -> corp_code for listed symbols, from a fresh or cached corpCode.xml."""
    cached = sorted((store.root / "corpCode").glob("corpCode_*.zip")) if (store.root / "corpCode").exists() else []
    rows = parse_corp_codes(cached[-1].read_bytes()) if cached else fetch_corp_codes(client)
    out = {}
    for r in rows:
        sc = (r.get("stock_code") or "").strip()
        if sc and sc in symbols:
            out[sc] = r["corp_code"]
    return out


def collect(client: Client, store: RawStore, corp_codes: list[str], years: list[int], *, max_requests: int) -> dict:
    """One request per (batch of 100 corp codes, year, report). Resumable: existing files skipped."""
    done = skipped = 0
    stopped = None
    batches = [corp_codes[i:i + BATCH] for i in range(0, len(corp_codes), BATCH)]
    for year in years:
        for reprt in REPRT_CODES:
            for bi, batch in enumerate(batches):
                name = f"fnltt_{year}_{reprt}_b{bi:03d}.json"
                if (store.root / "fnlttMultiAcnt" / name).exists():
                    skipped += 1
                    continue
                if done >= max_requests:
                    stopped = f"--max-requests {max_requests} reached; rerun to resume"
                    break
                client.get_json("fnlttMultiAcnt.json", {"corp_code": ",".join(batch), "bsns_year": str(year),
                                                        "reprt_code": reprt}, save_as=name)
                done += 1
            if stopped:
                break
        if stopped:
            break
    return {"requests_made": done, "files_skipped_existing": skipped, "batches": len(batches), "years": years,
            **({"stopped": stopped} if stopped else {})}


def _amount(text: str | None) -> str:
    if text is None:
        return ""
    t = str(text).replace(",", "").strip()
    if t in ("", "-"):
        return ""
    try:
        return str(int(float(t)))
    except ValueError:
        return ""


def normalize(store: RawStore, out_path: Path, *, filing_dates: dict[str, str] | None = None) -> dict:
    fetched = {Path(r["path"]).name: r["fetched_at"] for r in store.records() if r["kind"] == "fnlttMultiAcnt"}
    folder = store.root / "fnlttMultiAcnt"
    rows_out, counts = [], {"files": 0, "rows": 0, "skipped_no_data": 0, "skipped_account": 0, "no_stock_code": 0}
    seen = set()
    for path in sorted(folder.glob("fnltt_*.json")):
        counts["files"] += 1
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("status") == STATUS_NO_DATA:
            counts["skipped_no_data"] += 1
            continue
        for r in data.get("list", []):
            if r.get("account_nm") not in ACCOUNTS:
                counts["skipped_account"] += 1
                continue
            sym = (r.get("stock_code") or "").strip()
            if not sym:
                counts["no_stock_code"] += 1
                continue
            rc = r.get("rcept_no", "")
            key = (sym, r["bsns_year"], r["reprt_code"], r["fs_div"], r["sj_div"], r["account_nm"])
            if key in seen:
                continue
            seen.add(key)
            filing = (filing_dates or {}).get(rc) or (f"{rc[:4]}-{rc[4:6]}-{rc[6:8]}" if len(rc) == 14 else "")
            rows_out.append({"symbol": sym, "corp_code": r.get("corp_code", ""), "bsns_year": r["bsns_year"],
                             "reprt_code": r["reprt_code"], "period": REPRT_CODES.get(r["reprt_code"], ""),
                             "fs_div": r["fs_div"], "sj_div": r["sj_div"], "account_nm": r["account_nm"],
                             "thstrm_amount": _amount(r.get("thstrm_amount")), "frmtrm_amount": _amount(r.get("frmtrm_amount")),
                             "thstrm_dt": r.get("thstrm_dt", ""), "rcept_no": rc, "filing_date": filing,
                             "fetched_at": fetched.get(path.name, "")})
            counts["rows"] += 1
    rows_out.sort(key=lambda x: (x["symbol"], x["bsns_year"], x["reprt_code"], x["fs_div"], x["sj_div"], x["account_nm"]))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FUND_COLUMNS)
        w.writeheader()
        w.writerows(rows_out)
    counts["symbols"] = len({r["symbol"] for r in rows_out})
    return counts
