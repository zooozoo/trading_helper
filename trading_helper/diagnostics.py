"""Parameter-free event study for 단일판매ㆍ공급계약 filings (development segment only).

Per event: pre-event run-up, announcement-day and reaction-day returns, post-reaction returns at
+1/+4/+10/+20 sessions (close-to-close) and the executable M-open -> L-open path, all market-adjusted
with an equal-weight cross-sectional median return proxy built from the same price file.
No parameters are fitted; buckets are fixed before looking at results. Hypothesis-stage only.
"""

from __future__ import annotations

import csv
import json
import math
from collections import defaultdict
from datetime import date
from pathlib import Path
from statistics import mean, median, pstdev

from .market import MISSING, Market
from .weekly_v1 import EventRow

HORIZONS = (1, 4, 10, 20)
PRE_WINDOW = 20
REACTION_BUCKETS = [(-math.inf, 0.0, "<0%"), (0.0, 0.03, "0-3%"), (0.03, 0.10, "3-10%"), (0.10, math.inf, ">10%")]
CAP_RATIO_BUCKETS = [(-math.inf, 0.02, "<2%"), (0.02, 0.05, "2-5%"), (0.05, 0.15, "5-15%"), (0.15, math.inf, ">15%")]
RUNUP_BUCKETS = [(-math.inf, -0.05, "<-5%"), (-0.05, 0.05, "-5..5%"), (0.05, 0.20, "5-20%"), (0.20, math.inf, ">20%")]


def market_proxy(market: Market, *, statistic: str = "mean") -> list[float]:
    """Daily equal-weight close-to-close return across common stocks with bars on both days.

    DIAG-01 used the median and was biased: the cross-sectional median daily return is about
    -0.24%/day, so every stock looked +5% "abnormal" over 20 sessions (placebo confirmed).
    The equal-weight mean is the default; a matched placebo control is reported alongside."""
    n = len(market.sessions)
    syms = [s for s, sec in market.securities.items() if sec.is_common and s in market.bars]
    out = [0.0] * n
    agg = mean if statistic == "mean" else median
    for i in range(1, n):
        rets = []
        for s in syms:
            c = market.bars[s]["c"]
            if c[i] != MISSING and c[i - 1] != MISSING and c[i - 1] > 0:
                rets.append(c[i] / c[i - 1] - 1)
        out[i] = agg(rets) if rets else 0.0
    return out


def placebo_rows(market: Market, rows: list[dict], proxy: list[float], *, min_turnover: float, seed: int = 7,
                 lookback: int = 20) -> list[dict]:
    """For each event row, one random liquid common stock on the same reaction date, same metrics.

    Controls for any residual proxy bias and calendar effects: compare event stats with these."""
    import random
    rng = random.Random(seed)
    n = len(market.sessions)
    syms = [s for s, sec in market.securities.items() if sec.is_common and s in market.bars]
    out = []
    for r in rows:
        ri = market.index[date.fromisoformat(r["reaction_date"])]
        for _ in range(50):
            s = rng.choice(syms)
            if s == r["symbol"]:
                continue
            b = market.bars[s]
            base = ri - 1
            if base - lookback < 0 or ri + max(HORIZONS) + 1 >= n or not market.has_continuous_bars(s, base - lookback, ri + 1):
                continue
            c, v, o, h = b["c"], b["v"], b["o"], b["h"]
            prev = range(base - lookback + 1, base + 1)
            if mean(c[i] * v[i] for i in prev) < min_turnover:
                continue
            row = {"event_id": r["event_id"], "symbol": s, "reaction_date": r["reaction_date"], "liquid": True,
                   "reaction_abn": (c[ri] / c[base] - 1) - _cum(proxy, base, ri),
                   "runup_20_abn": (c[base] / c[base - lookback] - 1) - _cum(proxy, base - lookback, base),
                   "breakout_at_r": c[ri] > max(h[i] for i in prev)}
            for hz in HORIZONS:
                row[f"post_{hz}_abn"] = (c[ri + hz] / c[ri] - 1) - _cum(proxy, ri, ri + hz) if c[ri + hz] != MISSING else None
            if o[ri + 1] != MISSING and ri + 5 < n and o[ri + 5] != MISSING:
                row["exec_open1_to_open5_abn"] = (o[ri + 5] / o[ri + 1] - 1) - _cum(proxy, ri, ri + 4)
            else:
                row["exec_open1_to_open5_abn"] = None
            row["bucket_reaction"] = bucket(row["reaction_abn"], REACTION_BUCKETS)
            out.append(row)
            break
    return out


def _cum(daily: list[float], a: int, b: int) -> float:
    """Cumulative proxy return from close a to close b (a < b)."""
    r = 1.0
    for i in range(a + 1, b + 1):
        r *= 1 + daily[i]
    return r - 1


def load_market_caps(raw_dir: Path, dates: set[date]) -> dict[tuple[str, date], float]:
    """Market cap (KRW) per (symbol, date) from the raw daily JSON files."""
    caps = {}
    for d in dates:
        path = raw_dir / "daily" / f"{d:%Y%m%d}.json"
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        items = data["response"]["body"].get("items")
        rows = items.get("item", []) if isinstance(items, dict) else (items or [])
        for r in rows:
            try:
                caps[(r["srtnCd"], d)] = float(r["mrktTotAmt"])
            except (KeyError, ValueError):
                pass
    return caps


def bucket(value: float | None, edges) -> str:
    if value is None:
        return "n/a"
    for lo, hi, label in edges:
        if lo <= value < hi:
            return label
    return "n/a"


def event_table(market: Market, events: list[EventRow], *, segment: tuple[date, date], proxy: list[float],
                caps: dict[tuple[str, date], float], min_turnover: float, lookback: int = 20,
                volume_multiple: float = 1.5, require_revenue: bool = True) -> tuple[list[dict], dict]:
    rows, funnel = [], defaultdict(int)
    n = len(market.sessions)
    for ev in events:
        funnel["events"] += 1
        sec = market.securities.get(ev.symbol)
        if sec is None or not sec.is_common:
            funnel["skip:not_common_or_unknown"] += 1
            continue
        if require_revenue and ev.annual_revenue <= 0:
            funnel["skip:no_revenue"] += 1
            continue
        if ev.receipt_date < market.sessions[0]:
            funnel["skip:before_coverage"] += 1
            continue
        r = market.next_session_after(ev.receipt_date)
        if r is None:
            funnel["skip:no_reaction_session"] += 1
            continue
        if not segment[0] <= market.sessions[r] <= segment[1]:
            funnel["skip:outside_segment"] += 1
            continue
        d_idx = market.index.get(ev.receipt_date)  # None if receipt on a non-session day
        base = (d_idx if d_idx is not None else r) - 1  # last close before any reaction
        if base - lookback < 0 or r + max(HORIZONS) + 1 >= n:
            funnel["skip:insufficient_window"] += 1
            continue
        if not market.has_continuous_bars(ev.symbol, base - lookback, r + 1):
            funnel["skip:halt_or_gap_in_core_window"] += 1
            continue
        if market.has_split_candidate(ev.symbol, base - lookback, r + max(HORIZONS)):
            funnel["skip:split_candidate"] += 1
            continue
        b = market.bars[ev.symbol]
        c, h, v, o = b["c"], b["h"], b["v"], b["o"]
        prev = range(base - lookback + 1, base + 1)  # 20 sessions ending at base
        turnover = mean(c[i] * v[i] for i in prev)
        liquid = turnover >= min_turnover
        avg_vol = mean(v[i] for i in prev)
        row = {
            "event_id": ev.event_id, "symbol": ev.symbol, "market": sec.market, "receipt_date": ev.receipt_date.isoformat(),
            "receipt_is_session": d_idx is not None, "reaction_date": market.sessions[r].isoformat(),
            "contract_to_revenue": (ev.contract_amount / ev.annual_revenue) if ev.annual_revenue > 0 else None,
            "liquid": liquid,
            "avg_turnover_krw": turnover,
        }
        cap = caps.get((ev.symbol, market.sessions[base]))
        row["contract_to_mktcap"] = (ev.contract_amount / cap) if (cap and ev.contract_amount > 0) else None
        row["mktcap_krw"] = cap
        # pre-event run-up (abnormal) over the 20 sessions ending at base
        row["runup_20_abn"] = (c[base] / c[base - lookback] - 1) - _cum(proxy, base - lookback, base)
        row["pre_vol_ratio_5_20"] = (mean(v[i] for i in range(base - 4, base + 1)) / avg_vol) if avg_vol > 0 else None
        # announcement day (if a session) and reaction day returns, market-adjusted
        row["ann_day_abn"] = ((c[d_idx] / c[base] - 1) - proxy[d_idx]) if d_idx is not None else None
        row["reaction_abn"] = (c[r] / c[base] - 1) - _cum(proxy, base, r)  # covers D (if session) and R
        row["reaction_vol_mult"] = v[r] / avg_vol if avg_vol > 0 else None
        row["breakout_at_r"] = c[r] > max(h[i] for i in prev)
        row["volume_ok_at_r"] = avg_vol > 0 and v[r] >= volume_multiple * avg_vol
        row["mkt_20d_before"] = _cum(proxy, base - lookback, base)
        # post-reaction close-to-close abnormal returns
        ok = True
        for hz in HORIZONS:
            j = r + hz
            if c[j] == MISSING:
                row[f"post_{hz}_abn"] = None
                ok = False
            else:
                row[f"post_{hz}_abn"] = (c[j] / c[r] - 1) - _cum(proxy, r, j)
        # executable path: next open after R to the open 4 sessions later (strategy's hold), raw
        if o[r + 1] != MISSING and r + 5 < n and o[r + 5] != MISSING:
            row["exec_open1_to_open5_raw"] = o[r + 5] / o[r + 1] - 1
            row["exec_open1_to_open5_abn"] = row["exec_open1_to_open5_raw"] - _cum(proxy, r, r + 4)
        else:
            row["exec_open1_to_open5_raw"] = row["exec_open1_to_open5_abn"] = None
        row["gap_open1_vs_close_r"] = (o[r + 1] / c[r] - 1) if o[r + 1] != MISSING else None
        row["bucket_reaction"] = bucket(row["reaction_abn"], REACTION_BUCKETS)
        row["bucket_cap_ratio"] = bucket(row["contract_to_mktcap"], CAP_RATIO_BUCKETS)
        row["bucket_runup"] = bucket(row["runup_20_abn"], RUNUP_BUCKETS)
        row["bucket_regime"] = "mkt_up_20d" if row["mkt_20d_before"] > 0 else "mkt_down_20d"
        rr = row["contract_to_revenue"]
        row["bucket_rev_ratio"] = "n/a" if rr is None else (">=5%" if rr >= 0.05 else "<5%")
        row["breakout_and_volume"] = bool(row["breakout_at_r"] and row["volume_ok_at_r"])
        rows.append(row)
        funnel["rows"] += 1
        if not ok:
            funnel["rows_with_missing_horizon"] += 1
    return rows, dict(funnel)


def stats(values: list[float]) -> dict:
    vals = [x for x in values if x is not None and math.isfinite(x)]
    if not vals:
        return {"n": 0}
    m = mean(vals)
    sd = pstdev(vals) if len(vals) > 1 else 0.0
    t = (m / (sd / math.sqrt(len(vals)))) if sd > 0 and len(vals) > 1 else None
    return {"n": len(vals), "mean": m, "median": median(vals), "pos_rate": sum(1 for x in vals if x > 0) / len(vals),
            "t_stat_iid": t}


def summarize(rows: list[dict], *, liquid_only: bool = True) -> dict:
    sel = [r for r in rows if r["liquid"]] if liquid_only else rows
    out = {"n_events": len(sel), "liquid_only": liquid_only}
    metrics = ["runup_20_abn", "ann_day_abn", "reaction_abn", "gap_open1_vs_close_r"] + [f"post_{h}_abn" for h in HORIZONS] + \
              ["exec_open1_to_open5_raw", "exec_open1_to_open5_abn"]
    out["overall"] = {m: stats([r[m] for r in sel]) for m in metrics}
    out["receipt_is_session"] = {str(k): stats([r["ann_day_abn"] for r in sel if r["receipt_is_session"] == k])
                                 for k in (True, False)}
    for key in ("bucket_reaction", "bucket_cap_ratio", "bucket_runup", "bucket_regime", "breakout_at_r",
                "breakout_and_volume", "bucket_rev_ratio", "market"):
        groups = defaultdict(list)
        for r in sel:
            groups[str(r[key])].append(r)
        out[key] = {g: {m: stats([r[m] for r in rs]) for m in ("post_4_abn", "post_10_abn", "post_20_abn", "exec_open1_to_open5_abn")}
                    for g, rs in sorted(groups.items())}
    out["by_year"] = {}
    groups = defaultdict(list)
    for r in sel:
        groups[r["reaction_date"][:4]].append(r)
    for y, rs in sorted(groups.items()):
        out["by_year"][y] = {m: stats([r[m] for r in rs]) for m in ("reaction_abn", "post_4_abn", "post_20_abn")}
    return out


def summarize_placebo(rows: list[dict]) -> dict:
    out = {"n": len(rows), "overall": {m: stats([r[m] for r in rows]) for m in
                                       ("runup_20_abn", "reaction_abn", "post_1_abn", "post_4_abn", "post_10_abn",
                                        "post_20_abn", "exec_open1_to_open5_abn")}}
    for key in ("bucket_reaction", "breakout_at_r"):
        groups = defaultdict(list)
        for r in rows:
            groups[str(r[key])].append(r)
        out[key] = {g: {m: stats([r[m] for r in rs]) for m in ("post_4_abn", "post_20_abn", "exec_open1_to_open5_abn")}
                    for g, rs in sorted(groups.items())}
    return out


def write_table(rows: list[dict], path: Path) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if v is None else v) for k, v in r.items()})
