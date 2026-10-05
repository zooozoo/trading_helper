"""kr_factor_monthly_v1: monthly long-only factor portfolio engine (config/strategy_factor_v1.json).

Decision at the last session F of a month using data filed/known by F; orders fill at the next
session M open. Equal weight, full rebalance. Raw prices with listed-share-count adjustments.
Benchmarks: KOSPI/KOSDAQ price indices, equal-weight universe, random top-N draws. Quintile tables
per factor. Hypothesis stage; no parameter search beyond the pre-registered config.
"""

from __future__ import annotations

import csv
import json
import math
import random
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from statistics import mean, pstdev

from .market import MISSING, Market
from .weekly_v1 import CostTable

LOW_IS_GOOD = {"lowvol", "size"}


# --------------------------------------------------------------------------- config

@dataclass(frozen=True)
class FactorPolicy:
    strategy_id: str
    markets: tuple[str, ...]
    min_turnover: float
    min_history: int
    max_fund_age_days: int
    exclude_negative_book: bool
    portfolios: dict  # name -> {"factors": [...], "top_n": int}
    random_draws: int
    quintiles: int
    equity0: float
    raw: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> "FactorPolicy":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        u = raw["universe"]
        return cls(raw["strategy_id"], tuple(u["markets"]), float(u["min_avg_turnover_krw_20d"]),
                   int(u["min_price_history_sessions"]), int(u["max_fundamental_age_days"]),
                   bool(u["exclude_negative_book_equity"]), raw["portfolios"], int(raw["random_draws"]),
                   int(raw["quintiles"]), float(raw["equity0"]), raw)


# --------------------------------------------------------------------------- corporate actions

class CorporateActions:
    """Share-count change candidates mapped to an effective date (price-gap day if found)."""

    def __init__(self, market: Market, *, search_back: int = 45, tol: float = 0.15):
        self.by_symbol: dict[str, list[tuple[int, float, str]]] = defaultdict(list)  # (session idx, ratio, method)
        for sym, items in market.split_candidates.items():
            b = market.bars.get(sym)
            if b is None:
                continue
            c = b["c"]
            for d, ratio in items:
                i = market.index.get(d)
                if i is None:
                    i = market.next_session_after(d)
                    if i is None:
                        continue
                target = 1.0 / ratio
                found = None
                for j in range(i, max(i - search_back, 1) - 1, -1):
                    if c[j] == MISSING or c[j - 1] == MISSING or c[j - 1] <= 0:
                        continue
                    jump = c[j] / c[j - 1]
                    if abs(jump / target - 1) <= tol:
                        found = j
                        break
                self.by_symbol[sym].append((found if found is not None else i, ratio,
                                            "price_gap" if found is not None else "share_change_date"))
        for sym in self.by_symbol:
            self.by_symbol[sym].sort()

    def ratio_on(self, symbol: str, i: int) -> float:
        r = 1.0
        for idx, ratio, _ in self.by_symbol.get(symbol, ()):
            if idx == i:
                r *= ratio
        return r


# --------------------------------------------------------------------------- features

def load_caps_for(raw_dir: Path, d: date) -> dict[str, float]:
    path = Path(raw_dir) / "daily" / f"{d:%Y%m%d}.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    items = data["response"]["body"].get("items")
    rows = items.get("item", []) if isinstance(items, dict) else (items or [])
    out = {}
    for r in rows:
        try:
            out[r["srtnCd"]] = float(r["mrktTotAmt"])
        except (KeyError, ValueError):
            pass
    return out


def compute_features(market: Market, fund, caps: dict[str, float], f_idx: int, policy: FactorPolicy) -> dict[str, dict]:
    """Per-symbol factor values at session f_idx (None where not computable). Universe filters applied."""
    out = {}
    f_date = market.sessions[f_idx]
    for sym, sec in market.securities.items():
        if not sec.is_common or sec.market not in policy.markets or sym not in market.bars:
            continue
        b = market.bars[sym]
        c, v = b["c"], b["v"]
        start = f_idx - policy.min_history + 1
        if start < 0 or c[f_idx] == MISSING:
            continue
        if any(c[i] == MISSING for i in range(f_idx - 19, f_idx + 1)):
            continue
        turnover = mean(c[i] * v[i] for i in range(f_idx - 19, f_idx + 1))
        if turnover < policy.min_turnover:
            continue
        cap = caps.get(sym)
        if not cap or cap <= 0:
            continue
        snap = fund.snapshot(sym, f_date, max_age_days=policy.max_fund_age_days)
        if snap is None or snap.equity is None:
            continue
        if policy.exclude_negative_book and snap.equity <= 0:
            continue
        feats = {"cap": cap, "turnover": turnover, "bp": snap.equity / cap, "size": math.log(cap),
                 "ep": None, "roe": None, "opm": None, "lowvol": None, "mom_12_1": None, "mom_6_1": None}
        if snap.quarters == 4:
            if snap.ttm_net_income is not None:
                feats["ep"] = snap.ttm_net_income / cap
                feats["roe"] = snap.ttm_net_income / snap.equity if snap.equity > 0 else None
            if snap.ttm_op_income is not None and snap.ttm_revenue:
                feats["opm"] = snap.ttm_op_income / snap.ttm_revenue
        rets = []
        ok = True
        for i in range(f_idx - 119, f_idx + 1):
            if c[i] == MISSING or c[i - 1] == MISSING or c[i - 1] <= 0:
                ok = False
                break
            rets.append(c[i] / c[i - 1] - 1)
        if ok and len(rets) >= 100:
            feats["lowvol"] = pstdev(rets)
        if f_idx - 252 >= 0 and c[f_idx - 252] != MISSING and c[f_idx - 21] != MISSING:
            feats["mom_12_1"] = c[f_idx - 21] / c[f_idx - 252] - 1
        if f_idx - 126 >= 0 and c[f_idx - 126] != MISSING and c[f_idx - 21] != MISSING:
            feats["mom_6_1"] = c[f_idx - 21] / c[f_idx - 126] - 1
        out[sym] = feats
    return out


def percentile_ranks(features: dict[str, dict], factor: str) -> dict[str, float]:
    items = [(sym, f[factor]) for sym, f in features.items() if f.get(factor) is not None and math.isfinite(f[factor])]
    if not items:
        return {}
    reverse = factor in LOW_IS_GOOD
    items.sort(key=lambda t: (t[1], t[0]), reverse=reverse)  # worst first
    n = len(items)
    return {sym: (k + 1) / n for k, (sym, _) in enumerate(items)}


def composite_scores(features: dict[str, dict], factors: list[str]) -> dict[str, float]:
    ranks = {f: percentile_ranks(features, f) for f in factors}
    out = {}
    for sym in features:
        vals = [ranks[f].get(sym) for f in factors]
        if all(v is not None for v in vals):
            out[sym] = mean(vals)
    return out


def select_top(scores: dict[str, float], top_n: int) -> list[str]:
    return [s for s, _ in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:top_n]]


# --------------------------------------------------------------------------- schedule

def month_rebalances(market: Market, segment: tuple[date, date]) -> list[tuple[int, int]]:
    """(f_idx, m_idx) pairs: m = first session of a calendar month within segment, f = previous session."""
    out = []
    for i in range(1, len(market.sessions)):
        d, prev = market.sessions[i], market.sessions[i - 1]
        if (d.year, d.month) != (prev.year, prev.month) and segment[0] <= d <= segment[1]:
            out.append((i - 1, i))
    return out


# --------------------------------------------------------------------------- simulation

@dataclass
class SimResult:
    equity_curve: list[tuple[date, float]]
    holdings: list[dict]
    turnover: list[float]
    flags: dict
    equity0: float


def simulate(market: Market, selections: list[tuple[int, int, list[str]]], costs: CostTable, *, equity0: float,
             actions: CorporateActions, end_idx: int) -> SimResult:
    """selections: (f_idx, m_idx, symbols). Full rebalance to equal weight at each m open."""
    cash = equity0
    pos: dict[str, float] = {}
    last_close: dict[str, float] = {}
    curve, holdings, turnover = [], [], []
    flags = defaultdict(int)
    sel_at = {m: (f, syms) for f, m, syms in selections}
    if not selections:
        return SimResult(curve, holdings, turnover, dict(flags), equity0)
    start = selections[0][1]
    for i in range(start, end_idx + 1):
        today = market.sessions[i]
        year = today.year
        # corporate actions effective today (before open)
        for sym in list(pos):
            r = actions.ratio_on(sym, i)
            if r != 1.0:
                pos[sym] *= r
                flags["corporate_action_adjustments"] += 1
        # delisting: no bar today and past last_seen -> liquidate at last known close
        for sym in list(pos):
            if market.bar(sym, i) is None and today > market.securities[sym].last_seen:
                px = last_close.get(sym, 0.0) * (1 - costs.slippage)
                cash += px * pos[sym] * (1 - costs.sell_fee - costs.sell_tax(year))
                flags["delisting_liquidations"] += 1
                del pos[sym]
        if i in sel_at:
            f_idx, target_syms = sel_at[i]
            traded = 0.0
            # value at previous close
            equity_prev = cash + sum(pos[s] * last_close.get(s, 0.0) for s in pos)
            # sells: names leaving (need a bar today)
            for sym in list(pos):
                if sym in target_syms:
                    continue
                bar = market.bar(sym, i)
                if bar is None:
                    flags["sell_blocked_no_bar"] += 1
                    continue
                px = bar[0] * (1 - costs.slippage)
                proceeds = px * pos[sym] * (1 - costs.sell_fee - costs.sell_tax(year))
                cash += proceeds
                traded += px * pos[sym]
                del pos[sym]
            # buys / rebalances
            buyable = [s for s in target_syms if market.bar(s, i) is not None]
            flags["buy_skipped_no_bar"] += len(target_syms) - len(buyable)
            if buyable:
                stuck_value = sum(pos[s] * last_close.get(s, 0.0) for s in pos if s not in target_syms)
                investable = max(cash + sum(pos[s] * market.bar(s, i)[0] for s in pos if s in buyable) - 0.0, 0.0)
                target_value = investable / len(buyable)
                for sym in buyable:
                    px_open = market.bar(sym, i)[0]
                    have = pos.get(sym, 0.0)
                    want = math.floor(target_value / (px_open * (1 + costs.slippage) * (1 + costs.buy_fee)))
                    delta = want - have
                    if delta > 0:
                        px = px_open * (1 + costs.slippage)
                        cost = px * delta * (1 + costs.buy_fee)
                        if cost > cash:
                            delta = math.floor(cash / (px * (1 + costs.buy_fee)))
                            cost = px * delta * (1 + costs.buy_fee)
                        if delta > 0:
                            cash -= cost
                            pos[sym] = have + delta
                            traded += px * delta
                    elif delta < 0:
                        px = px_open * (1 - costs.slippage)
                        cash += px * (-delta) * (1 - costs.sell_fee - costs.sell_tax(year))
                        pos[sym] = have + delta
                        traded += px * (-delta)
                        if pos[sym] <= 0:
                            del pos[sym]
                _ = stuck_value
            turnover.append(traded / equity_prev if equity_prev > 0 else 0.0)
            for sym in pos:
                holdings.append({"rebalance_date": today.isoformat(), "symbol": sym, "shares": pos[sym]})
        # mark to market at close
        value = cash
        for sym, sh in pos.items():
            bar = market.bar(sym, i)
            if bar is not None:
                last_close[sym] = bar[3]
            value += sh * last_close.get(sym, 0.0)
        curve.append((today, value))
    return SimResult(curve, holdings, turnover, dict(flags), equity0)


# --------------------------------------------------------------------------- analytics

def period_return(market: Market, sym: str, i_from: int, i_to: int, actions: CorporateActions) -> float | None:
    """Close-to-close return with corporate-action share adjustments, None if either close missing."""
    b = market.bars.get(sym)
    if b is None or b["c"][i_from] == MISSING or b["c"][i_to] == MISSING:
        return None
    adj = 1.0
    for j in range(i_from + 1, i_to + 1):
        adj *= actions.ratio_on(sym, j)
    return (b["c"][i_to] * adj) / b["c"][i_from] - 1


def quintile_table(market: Market, rebalances: list[tuple[int, int]], feats_by_f: dict[int, dict], factor: str,
                   actions: CorporateActions, n_q: int) -> dict:
    """Average next-period (F -> next F) return by factor quintile, cost-free."""
    per_month = []
    for k in range(len(rebalances) - 1):
        f, _ = rebalances[k]
        f_next = rebalances[k + 1][0]
        ranks = percentile_ranks(feats_by_f[f], factor)
        if len(ranks) < n_q * 5:
            continue
        buckets = defaultdict(list)
        for sym, pr in ranks.items():
            r = period_return(market, sym, f, f_next, actions)
            if r is None:
                continue
            q = min(int(pr * n_q), n_q - 1) + 1  # 1 = worst, n_q = best
            buckets[q].append(r)
        if len(buckets) == n_q:
            per_month.append({q: mean(v) for q, v in buckets.items()})
    if not per_month:
        return {"months": 0}
    avg = {q: mean(m[q] for m in per_month) for q in range(1, n_q + 1)}
    spread = [m[n_q] - m[1] for m in per_month]
    return {"months": len(per_month), "avg_monthly_return_by_quintile": avg,
            "top_minus_bottom_mean": mean(spread), "top_minus_bottom_positive_share": sum(1 for s in spread if s > 0) / len(spread),
            "monotonic": all(avg[q] <= avg[q + 1] for q in range(1, n_q))}


def random_draw_test(market: Market, rebalances: list[tuple[int, int]], eligible_by_f: dict[int, list[str]],
                     chosen_by_f: dict[int, list[str]], actions: CorporateActions, *, draws: int, top_n: int,
                     seed: int = 11) -> dict:
    """Cost-free F->next-F equal-weight returns: chosen top-N vs random N-name draws from the same eligible set."""
    rng = random.Random(seed)
    chosen_rets, draw_rets = [], []
    for k in range(len(rebalances) - 1):
        f = rebalances[k][0]
        f_next = rebalances[k + 1][0]
        elig = eligible_by_f.get(f, [])
        chosen = chosen_by_f.get(f, [])
        if len(elig) < top_n or not chosen:
            continue
        cr = [period_return(market, s, f, f_next, actions) for s in chosen]
        cr = [x for x in cr if x is not None]
        if not cr:
            continue
        chosen_rets.append(mean(cr))
        month_draws = []
        for _ in range(draws):
            sample = rng.sample(elig, top_n)
            rr = [period_return(market, s, f, f_next, actions) for s in sample]
            rr = [x for x in rr if x is not None]
            if rr:
                month_draws.append(mean(rr))
        draw_rets.append(month_draws)
    if not chosen_rets:
        return {"months": 0}
    chosen_cum = math.prod(1 + r for r in chosen_rets) - 1
    draw_cums = []
    for d in range(draws):
        path = [m[d] for m in draw_rets if len(m) > d]
        if len(path) == len(draw_rets):
            draw_cums.append(math.prod(1 + r for r in path) - 1)
    pct = sum(1 for x in draw_cums if x < chosen_cum) / len(draw_cums) if draw_cums else None
    return {"months": len(chosen_rets), "chosen_cum_return_cost_free": chosen_cum,
            "chosen_avg_monthly": mean(chosen_rets),
            "random_cum_return_median": sorted(draw_cums)[len(draw_cums) // 2] if draw_cums else None,
            "random_cum_return_p05": sorted(draw_cums)[int(0.05 * len(draw_cums))] if draw_cums else None,
            "random_cum_return_p95": sorted(draw_cums)[int(0.95 * len(draw_cums))] if draw_cums else None,
            "chosen_percentile_vs_random": pct}


def curve_stats(curve: list[tuple[date, float]], equity0: float) -> dict:
    if len(curve) < 2:
        return {}
    vals = [v for _, v in curve]
    rets = [vals[i] / vals[i - 1] - 1 for i in range(1, len(vals)) if vals[i - 1] > 0]
    years = (curve[-1][0] - curve[0][0]).days / 365.25
    total = vals[-1] / equity0 - 1
    peak, mdd = -math.inf, 0.0
    for v in vals:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    vol = pstdev(rets) * math.sqrt(248) if len(rets) > 1 else None
    cagr = (vals[-1] / equity0) ** (1 / years) - 1 if years > 0 else None
    by_year = {}
    for y in sorted({d.year for d, _ in curve}):
        pts = [v for d, v in curve if d.year == y]
        prev = [v for d, v in curve if d.year < y]
        base = prev[-1] if prev else equity0
        by_year[y] = pts[-1] / base - 1
    return {"total_return": total, "cagr": cagr, "ann_vol": vol, "sharpe_rf0": (cagr / vol) if (cagr is not None and vol) else None,
            "max_drawdown": mdd, "by_year": by_year, "start": curve[0][0].isoformat(), "end": curve[-1][0].isoformat()}


def index_stats(series: dict[date, float], start: date, end: date) -> dict:
    pts = sorted((d, v) for d, v in series.items() if start <= d <= end)
    if len(pts) < 2:
        return {}
    base = pts[0][1]
    curve = [(d, v / base) for d, v in pts]
    return curve_stats(curve, 1.0)


def write_outputs(out_dir: Path, summary: dict, sims: dict[str, SimResult], indexes: dict[str, dict[date, float]]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    names = list(sims)
    dates = sorted({d for s in sims.values() for d, _ in s.equity_curve})
    lookup = {n: dict(s.equity_curve) for n, s in sims.items()}
    with (out_dir / "equity.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["date"] + names + list(indexes))
        for d in dates:
            w.writerow([d.isoformat()] + [f"{lookup[n].get(d, ''):.2f}" if lookup[n].get(d) is not None else "" for n in names]
                       + [indexes[k].get(d, "") for k in indexes])
    with (out_dir / "holdings.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["portfolio", "rebalance_date", "symbol", "shares"])
        w.writeheader()
        for n, s in sims.items():
            for h in s.holdings:
                w.writerow({"portfolio": n, **h})
