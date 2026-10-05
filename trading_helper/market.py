"""Market data container for backtests: official sessions, per-symbol aligned OHLCV arrays.

Built from data/normalized/{calendar,securities,prices,halts,share_count_changes}.csv.
Prices are RAW (unadjusted). Sessions with no valid bar for a symbol are -1 (not listed,
halted, or no trade). A pickle cache under data/cache speeds up repeated runs.
"""

from __future__ import annotations

import csv
import hashlib
import pickle
from array import array
from dataclasses import dataclass
from datetime import date
from pathlib import Path

MISSING = -1


@dataclass(frozen=True)
class Security:
    symbol: str
    name: str
    market: str
    is_common: bool
    first_seen: date
    last_seen: date


class Market:
    def __init__(self, sessions: list[date], securities: dict[str, Security],
                 bars: dict[str, dict[str, array]], split_candidates: dict[str, list[tuple[date, float]]]):
        self.sessions = sessions
        self.index = {d: i for i, d in enumerate(sessions)}
        self.securities = securities
        self.bars = bars  # symbol -> {"o","h","l","c","v": array aligned to sessions}
        self.split_candidates = split_candidates
        self.iso_week = [d.isocalendar()[:2] for d in sessions]

    # ------------------------------------------------------------------ sessions
    def next_session_after(self, d: date) -> int | None:
        """Index of the first session strictly after calendar date d, or None."""
        lo, hi = 0, len(self.sessions)
        while lo < hi:
            mid = (lo + hi) // 2
            if self.sessions[mid] <= d:
                lo = mid + 1
            else:
                hi = mid
        return lo if lo < len(self.sessions) else None

    def week_bounds(self, i: int) -> tuple[int, int]:
        """(first_idx, last_idx) of the ISO week containing session i."""
        wk = self.iso_week[i]
        a = i
        while a > 0 and self.iso_week[a - 1] == wk:
            a -= 1
        b = i
        while b + 1 < len(self.sessions) and self.iso_week[b + 1] == wk:
            b += 1
        return a, b

    # ------------------------------------------------------------------ bars
    def bar(self, symbol: str, i: int) -> tuple[int, int, int, int, int] | None:
        b = self.bars.get(symbol)
        if b is None or i < 0 or i >= len(self.sessions) or b["c"][i] == MISSING:
            return None
        return b["o"][i], b["h"][i], b["l"][i], b["c"][i], b["v"][i]

    def has_continuous_bars(self, symbol: str, start: int, end: int) -> bool:
        """True if every session in [start, end] has a valid bar."""
        b = self.bars.get(symbol)
        if b is None or start < 0 or end >= len(self.sessions):
            return False
        c = b["c"]
        return all(c[i] != MISSING for i in range(start, end + 1))

    def has_split_candidate(self, symbol: str, start: int, end: int) -> bool:
        lo, hi = self.sessions[start], self.sessions[end]
        return any(lo <= d <= hi for d, _ in self.split_candidates.get(symbol, ()))


# ---------------------------------------------------------------------- loading

def _file_sig(paths: list[Path]) -> str:
    h = hashlib.sha256()
    for p in paths:
        st = p.stat()
        h.update(f"{p.name}:{st.st_size}:{int(st.st_mtime)}".encode())
    return h.hexdigest()[:16]


def load_market(normalized_dir: str | Path, *, markets: tuple[str, ...] = ("KOSPI", "KOSDAQ"),
                cache_dir: str | Path | None = "data/cache") -> Market:
    nd = Path(normalized_dir)
    files = [nd / n for n in ("calendar.csv", "securities.csv", "prices.csv", "share_count_changes.csv")]
    for f in files:
        if not f.exists():
            raise FileNotFoundError(f"missing {f}; run prices-normalize first")
    cache = None
    if cache_dir is not None:
        Path(cache_dir).mkdir(parents=True, exist_ok=True)
        cache = Path(cache_dir) / f"market_{_file_sig(files)}_{'_'.join(markets)}.pkl"
        if cache.exists():
            with cache.open("rb") as fh:
                return pickle.load(fh)
    sessions = []
    with files[0].open(encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            if r["status"] == "trading_day":
                sessions.append(date.fromisoformat(r["date"]))
    sessions.sort()
    index = {d: i for i, d in enumerate(sessions)}
    securities: dict[str, Security] = {}
    with files[1].open(encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            if r["market"] not in markets:
                continue
            securities[r["symbol"]] = Security(r["symbol"], r["name"], r["market"], r["is_common_stock"] == "true",
                                               date.fromisoformat(r["first_seen"]), date.fromisoformat(r["last_seen"]))
    n = len(sessions)
    bars: dict[str, dict[str, array]] = {}
    with files[2].open(encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        col = {name: i for i, name in enumerate(header)}
        si, di = col["symbol"], col["date"]
        oi, hi_, li, ci, vi = col["open"], col["high"], col["low"], col["close"], col["volume"]
        for row in reader:
            sym = row[si]
            if sym not in securities:
                continue
            i = index.get(date.fromisoformat(row[di]))
            if i is None:
                continue
            b = bars.get(sym)
            if b is None:
                b = bars[sym] = {k: array("q", [MISSING]) * n for k in ("o", "h", "l", "c", "v")}
            b["o"][i], b["h"][i], b["l"][i], b["c"][i], b["v"][i] = (
                int(float(row[oi])), int(float(row[hi_])), int(float(row[li])), int(float(row[ci])), int(row[vi]))
    splits: dict[str, list[tuple[date, float]]] = {}
    with files[3].open(encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            if r["note"] == "split_or_merge_candidate":
                splits.setdefault(r["symbol"], []).append((date.fromisoformat(r["date"]), float(r["ratio"])))
    market = Market(sessions, securities, bars, splits)
    if cache is not None:
        with cache.open("wb") as fh:
            pickle.dump(market, fh, protocol=pickle.HIGHEST_PROTOCOL)
    return market
