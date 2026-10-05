"""Point-in-time fundamentals from data/normalized/fundamentals.csv.

Quarterly income-statement rows in the key-accounts API are 3-month figures (verified): Q1=11013,
Q2=11012, Q3=11014; Q4 is derived as FY(11011) minus Q1..Q3 of the same fiscal year, and becomes
available on the FY filing date. Balance-sheet rows are point-in-time. A figure is usable only on
or after its filing_date. Consolidated (CFS) is preferred; separate (OFS) is the fallback.
"""

from __future__ import annotations

import csv
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

QUARTER_CODES = {"11013": 1, "11012": 2, "11014": 3, "11011": 4}
IS_ACCOUNTS = {"매출액": "revenue", "영업이익": "op_income", "당기순이익(손실)": "net_income"}
BS_ACCOUNTS = {"자본총계": "equity", "자산총계": "assets", "부채총계": "liabilities"}


@dataclass(frozen=True)
class Snapshot:
    as_of: date
    filing_date: date          # latest filing used
    ttm_revenue: float | None
    ttm_op_income: float | None
    ttm_net_income: float | None
    equity: float | None
    assets: float | None
    fs_div: str                # CFS or OFS used for the TTM figures
    quarters: int              # number of quarters in the TTM sum (4 expected)


class Fundamentals:
    def __init__(self, path: str | Path):
        # bs[symbol][fs_div] -> sorted list of (filing_date, {field: value})
        self.bs: dict[str, dict[str, list[tuple[date, dict]]]] = defaultdict(lambda: defaultdict(list))
        # q[symbol][fs_div][(year, quarter)] -> (filing_date, {field: value})  (quarter rows as filed)
        self.q: dict[str, dict[str, dict[tuple[int, int], tuple[date, dict]]]] = defaultdict(lambda: defaultdict(dict))
        self._load(Path(path))
        self._derive_q4()
        for sym in self.bs:
            for fs in self.bs[sym]:
                self.bs[sym][fs].sort(key=lambda t: t[0])

    def _load(self, path: Path) -> None:
        tmp_bs: dict[tuple[str, str, str], dict] = {}
        with path.open(encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                if not r["filing_date"] or not r["thstrm_amount"]:
                    continue
                fd = date.fromisoformat(r["filing_date"])
                sym, fs = r["symbol"], r["fs_div"]
                try:
                    value = float(r["thstrm_amount"])
                except ValueError:
                    continue
                if r["sj_div"] == "BS" and r["account_nm"] in BS_ACCOUNTS:
                    key = (sym, fs, r["rcept_no"], fd, r["bsns_year"], r["reprt_code"])
                    entry = tmp_bs.setdefault(key, {"_filing": fd})
                    entry[BS_ACCOUNTS[r["account_nm"]]] = value
                elif r["sj_div"] == "IS" and r["account_nm"] in IS_ACCOUNTS:
                    qn = QUARTER_CODES.get(r["reprt_code"])
                    if qn is None:
                        continue
                    yq = (int(r["bsns_year"]), qn)
                    cur = self.q[sym][fs].get(yq)
                    fields = dict(cur[1]) if cur else {}
                    fields[IS_ACCOUNTS[r["account_nm"]]] = value
                    self.q[sym][fs][yq] = (fd, fields)
        for (sym, fs, *_), entry in tmp_bs.items():
            fd = entry.pop("_filing")
            self.bs[sym][fs].append((fd, entry))

    def _derive_q4(self) -> None:
        """Q4 3-month = FY - (Q1+Q2+Q3) when all three quarters exist; else FY row is kept as annual only."""
        for sym in self.q:
            for fs in self.q[sym]:
                table = self.q[sym][fs]
                for (year, qn), (fd, fields) in list(table.items()):
                    if qn != 4:
                        continue
                    parts = [table.get((year, k)) for k in (1, 2, 3)]
                    if any(p is None for p in parts):
                        table[(year, 4)] = (fd, {**fields, "_annual_only": True})
                        continue
                    derived = {}
                    for f in ("revenue", "op_income", "net_income"):
                        if f in fields and all(f in p[1] for p in parts):
                            derived[f] = fields[f] - sum(p[1][f] for p in parts)
                    derived["_annual"] = fields
                    table[(year, 4)] = (fd, derived)

    def snapshot(self, symbol: str, as_of: date, *, max_age_days: int = 400) -> Snapshot | None:
        """Latest usable figures with filing_date <= as_of. None if nothing recent enough."""
        best = None
        for fs in ("CFS", "OFS"):
            rows = self.bs.get(symbol, {}).get(fs, [])
            if not rows:
                continue
            idx = bisect_right([r[0] for r in rows], as_of) - 1
            if idx < 0:
                continue
            fd, fields = rows[idx]
            if (as_of - fd).days > max_age_days:
                continue
            best = (fs, fd, fields)
            break
        if best is None:
            return None
        fs_bs, fd_bs, bsf = best
        ttm = None
        for fs in ("CFS", "OFS"):
            table = self.q.get(symbol, {}).get(fs, {})
            usable = [(yq, fd, f) for yq, (fd, f) in table.items() if fd <= as_of and (as_of - fd).days <= max_age_days + 365]
            if not usable:
                continue
            usable.sort(key=lambda t: t[0])
            # take the latest 4 consecutive quarters by (year, quarter) order
            last4 = usable[-4:]
            if len(last4) < 4:
                continue
            seq_ok = all(_next_yq(last4[i][0]) == last4[i + 1][0] for i in range(3))
            if not seq_ok:
                continue
            sums = {}
            for f in ("revenue", "op_income", "net_income"):
                vals = [row[2].get(f) for row in last4]
                sums[f] = sum(vals) if all(v is not None for v in vals) else None
            latest_fd = max(row[1] for row in last4)
            ttm = (fs, latest_fd, sums)
            break
        if ttm is None:
            return Snapshot(as_of, fd_bs, None, None, None, bsf.get("equity"), bsf.get("assets"), fs_bs, 0)
        fs_is, fd_is, sums = ttm
        return Snapshot(as_of, max(fd_bs, fd_is), sums["revenue"], sums["op_income"], sums["net_income"],
                        bsf.get("equity"), bsf.get("assets"), fs_is, 4)


def _next_yq(yq: tuple[int, int]) -> tuple[int, int]:
    y, q = yq
    return (y, q + 1) if q < 4 else (y + 1, 1)
