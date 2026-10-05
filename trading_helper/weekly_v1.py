"""weekly_execution_v1 backtest engine (docs/STRATEGY_WEEKLY_V1.md). Deterministic, stdlib only.

Model recap (variant A):
  F = last session of the week in which the first post-receipt session falls (reaction day must be F)
  signal at F close: breakout over prior `lookback` highs, volume >= multiple x avg, liquidity, ATR
  entry: M open (first session of next week) if open <= F close x (1 + max_entry_gap), else no trade
  thresholds anchored to F close; quantity sized at the cap price
  exits: each session close vs stop/target -> next session open; final exit at L open (last session)
  prices are raw; events whose window contains a split candidate or a halt are excluded
All numbers here are hypothesis-stage results, not validated performance.
"""

from __future__ import annotations

import csv
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field, asdict
from datetime import date
from pathlib import Path
from statistics import mean

from .market import Market

RISK_FILTER_MODES = ("approved_only", "none")


@dataclass(frozen=True)
class WeeklyPolicy:
    strategy_id: str
    min_contract_revenue_ratio: float
    lookback: int
    volume_multiple: float
    atr_period: int
    atr_multiple: float
    reward_multiple: float
    max_entry_gap: float
    min_week_sessions: int
    min_avg_turnover_krw: float
    risk_fraction: float
    max_position_fraction: float
    max_positions: int
    require_breakout: bool = True
    require_volume: bool = True
    signal_variant: str = "A"  # A: reaction day must be week end; B: breakout judged at week end regardless

    def __post_init__(self):
        if self.lookback < 1 or not 1 <= self.atr_period <= self.lookback:
            raise ValueError("invalid lookback/ATR period")
        for f in ("volume_multiple", "atr_multiple", "reward_multiple"):
            if not math.isfinite(getattr(self, f)) or getattr(self, f) <= 0:
                raise ValueError(f"invalid {f}")
        for f in ("risk_fraction", "max_position_fraction"):
            if not 0 < getattr(self, f) <= 1:
                raise ValueError(f"invalid {f}")
        if self.min_week_sessions < 2 or self.max_positions < 1 or self.max_entry_gap < 0:
            raise ValueError("invalid constraints")
        if self.signal_variant not in ("A", "B"):
            raise ValueError("signal_variant must be A or B")

    @classmethod
    def load(cls, path: str | Path, **overrides) -> "WeeklyPolicy":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        keys = [k for k in cls.__dataclass_fields__ if k != "signal_variant"]
        variant = str(raw.get("signal_variant", "A"))[:1]
        return cls(**{k: raw[k] for k in keys if k in raw}, **{"signal_variant": variant, **overrides})


@dataclass(frozen=True)
class CostTable:
    buy_fee: float
    sell_fee: float
    sell_tax_by_year: dict
    slippage: float
    status: str = ""

    @classmethod
    def load(cls, path: str | Path) -> "CostTable":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(raw["buy_fee"], raw["sell_fee"], {int(k): v for k, v in raw["sell_tax_by_year"].items()},
                   raw["slippage"], raw.get("status", ""))

    def sell_tax(self, year: int) -> float:
        if year not in self.sell_tax_by_year:
            raise ValueError(f"no sell tax for {year}; extend the cost table")
        return self.sell_tax_by_year[year]


@dataclass(frozen=True)
class EventRow:
    event_id: str
    symbol: str
    receipt_date: date
    event_type: str
    contract_amount: float
    annual_revenue: float
    revenue_available_date: date
    risk_approved: bool


def load_events(path: str | Path) -> list[EventRow]:
    out = []
    with Path(path).open(encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            if not r["annual_revenue"]:
                continue
            out.append(EventRow(r["event_id"], r["symbol"], date.fromisoformat(r["receipt_date"]), r["event_type"],
                                float(r["contract_amount"]), float(r["annual_revenue"]),
                                date.fromisoformat(r["revenue_available_date"]), r["risk_approved"] == "true"))
    return out


@dataclass
class Signal:
    event_id: str
    symbol: str
    f_idx: int
    m_idx: int
    l_idx: int
    f_close: int
    atr: float
    cap: float
    stop: float
    target: float
    ratio: float


@dataclass
class Trade:
    event_id: str
    symbol: str
    f_date: date
    entry_date: date
    exit_date: date
    entry_price: float
    exit_price: float
    quantity: int
    entry_debit: float
    proceeds: float
    pnl: float
    reason: str
    stop: float
    target: float
    cap: float
    sessions_held: int
    flags: str = ""

    @property
    def ret(self) -> float:
        return self.pnl / self.entry_debit


# --------------------------------------------------------------------------- signals

def _week_of(market: Market, idx: int) -> tuple[int, int]:
    return market.week_bounds(idx)


def generate_signals(market: Market, events: list[EventRow], policy: WeeklyPolicy, *,
                     segment: tuple[date, date], risk_filter: str) -> tuple[list[Signal], Counter]:
    """Variant A. Returns candidate signals and a funnel of rejection reasons (first failing check)."""
    if risk_filter not in RISK_FILTER_MODES:
        raise ValueError(f"risk_filter must be one of {RISK_FILTER_MODES}")
    funnel: Counter = Counter()
    signals: list[Signal] = []
    n = len(market.sessions)
    for ev in events:
        funnel["events_total"] += 1
        if ev.event_type != "new_contract":
            funnel["reject:not_new_contract"] += 1
            continue
        sec = market.securities.get(ev.symbol)
        if sec is None or not sec.is_common:
            funnel["reject:symbol_not_in_universe"] += 1
            continue
        if ev.annual_revenue <= 0 or ev.contract_amount / ev.annual_revenue < policy.min_contract_revenue_ratio:
            funnel["reject:ratio_below_min"] += 1
            continue
        if ev.revenue_available_date > ev.receipt_date:
            funnel["reject:revenue_after_receipt"] += 1
            continue
        if risk_filter == "approved_only" and not ev.risk_approved:
            funnel["reject:risk_not_approved"] += 1
            continue
        if ev.receipt_date < market.sessions[0]:
            funnel["reject:before_price_coverage"] += 1
            continue
        r = market.next_session_after(ev.receipt_date)
        if r is None or r + 1 >= n:
            funnel["reject:no_session_after_receipt"] += 1
            continue
        wa, wb = _week_of(market, r)
        if policy.signal_variant == "A" and r != wb:
            funnel["reject:reaction_not_last_session_of_week"] += 1
            continue
        f = wb  # A: f == r; B: judge at the week's last session even if the reaction day was earlier
        if f + 1 >= n:
            funnel["reject:no_session_after_week_end"] += 1
            continue
        m = f + 1
        ma, l = _week_of(market, m)
        if ma != m:
            raise AssertionError("session after week end must start a new week")
        if l - m + 1 < policy.min_week_sessions:
            funnel["reject:week_too_short"] += 1
            continue
        m_date = market.sessions[m]
        if not segment[0] <= m_date <= segment[1]:
            funnel["reject:outside_segment"] += 1
            continue
        start = f - policy.lookback
        if start < 0 or not market.has_continuous_bars(ev.symbol, start, f):
            funnel["reject:insufficient_or_halted_history"] += 1
            continue
        if market.has_split_candidate(ev.symbol, start, l):
            funnel["reject:split_candidate_in_window"] += 1
            continue
        b = market.bars[ev.symbol]
        prev = range(start, f)
        avg_vol = mean(b["v"][i] for i in prev)
        turnover = mean(b["c"][i] * b["v"][i] for i in prev)
        if turnover < policy.min_avg_turnover_krw:
            funnel["reject:liquidity_below_min"] += 1
            continue
        f_close, f_vol = b["c"][f], b["v"][f]
        if policy.require_volume and (avg_vol <= 0 or f_vol < avg_vol * policy.volume_multiple):
            funnel["reject:volume_below_multiple"] += 1
            continue
        if policy.require_breakout and f_close <= max(b["h"][i] for i in prev):
            funnel["reject:no_breakout"] += 1
            continue
        ranges = []
        for i in range(f - policy.atr_period + 1, f + 1):
            pc = b["c"][i - 1]
            ranges.append(max(b["h"][i] - b["l"][i], abs(b["h"][i] - pc), abs(b["l"][i] - pc)))
        atr = mean(ranges)
        stop = f_close - policy.atr_multiple * atr
        if atr <= 0 or stop <= 0:
            funnel["reject:atr_or_stop_invalid"] += 1
            continue
        target = f_close + policy.reward_multiple * (f_close - stop)
        cap = f_close * (1 + policy.max_entry_gap)
        funnel["signals"] += 1
        signals.append(Signal(ev.event_id, ev.symbol, f, m, l, f_close, atr, cap, stop, target,
                              ev.contract_amount / ev.annual_revenue))
    signals.sort(key=lambda s: (s.m_idx, s.symbol, s.event_id))
    return signals, funnel


# --------------------------------------------------------------------------- simulation

def _next_valid_open(market: Market, symbol: str, start: int, limit: int = 60) -> int | None:
    for i in range(start, min(start + limit, len(market.sessions))):
        if market.bar(symbol, i) is not None:
            return i
    return None


def size_quantity(sig: Signal, equity: float, cash: float, policy: WeeklyPolicy, costs: CostTable,
                  exit_year: int) -> int:
    cap_fill = sig.cap * (1 + costs.slippage)
    debit_ps = cap_fill * (1 + costs.buy_fee)
    stop_net = sig.stop * (1 - costs.slippage) * (1 - costs.sell_fee - costs.sell_tax(exit_year))
    risk_ps = debit_ps - stop_net
    if risk_ps <= 0 or debit_ps <= 0:
        return 0
    return math.floor(min(equity * policy.risk_fraction / risk_ps,
                          equity * policy.max_position_fraction / debit_ps,
                          cash / debit_ps))


def simulate_trade(market: Market, sig: Signal, quantity: int, costs: CostTable) -> Trade | None:
    """Deterministic path of one position from M open. Returns None on gap-cancel or no entry bar."""
    entry_bar = market.bar(sig.symbol, sig.m_idx)
    if entry_bar is None:
        return None
    if entry_bar[0] > sig.cap:
        return None
    fill = entry_bar[0] * (1 + costs.slippage)
    debit = fill * (1 + costs.buy_fee) * quantity
    flags = []
    reason, exec_idx = None, None
    for t in range(sig.m_idx, sig.l_idx):
        bar = market.bar(sig.symbol, t)
        if bar is None:
            flags.append(f"halt_during_hold@{market.sessions[t]}")
            continue
        close = bar[3]
        if close <= sig.stop:
            reason, exec_idx = "stop", t + 1
            break
        if close >= sig.target:
            reason, exec_idx = "target", t + 1
            break
    if reason is None:
        reason, exec_idx = "time_exit", sig.l_idx
    actual = _next_valid_open(market, sig.symbol, exec_idx)
    if actual is None:
        # No tradable session within the search window (delisting/long halt): assume total loss.
        last = market.sessions[min(exec_idx, len(market.sessions) - 1)]
        return Trade(sig.event_id, sig.symbol, market.sessions[sig.f_idx], market.sessions[sig.m_idx], last,
                     fill, 0.0, quantity, debit, 0.0, -debit, "unresolved_total_loss", sig.stop, sig.target, sig.cap,
                     last and (exec_idx - sig.m_idx + 1), ";".join(flags + ["no_exit_bar_assumed_total_loss"]))
    if actual != exec_idx:
        flags.append(f"exit_delayed_by_halt:{actual - exec_idx}")
    if actual > sig.l_idx:
        flags.append("held_past_week_end")
    price = market.bar(sig.symbol, actual)[0] * (1 - costs.slippage)
    year = market.sessions[actual].year
    proceeds = price * quantity * (1 - costs.sell_fee - costs.sell_tax(year))
    return Trade(sig.event_id, sig.symbol, market.sessions[sig.f_idx], market.sessions[sig.m_idx],
                 market.sessions[actual], fill, price, quantity, debit, proceeds, proceeds - debit, reason,
                 sig.stop, sig.target, sig.cap, actual - sig.m_idx + 1, ";".join(flags))


@dataclass
class Result:
    trades: list[Trade]
    funnel: Counter
    equity_curve: list[tuple[date, float]]
    equity0: float
    labels: dict = field(default_factory=dict)


def run_portfolio(market: Market, signals: list[Signal], policy: WeeklyPolicy, costs: CostTable,
                  *, equity0: float, funnel: Counter | None = None) -> Result:
    funnel = funnel if funnel is not None else Counter()
    cash = equity0
    open_trades: list[Trade] = []  # exits applied when their exit_date session is reached
    trades: list[Trade] = []
    by_week: dict[int, list[Signal]] = defaultdict(list)
    for s in signals:
        by_week[s.m_idx].append(s)
    equity_curve: list[tuple[date, float]] = []
    pending_exit: list[Trade] = []
    sess = market.sessions
    first_m = min(by_week) if by_week else len(sess)
    last_needed = 0
    for i in range(first_m, len(sess)):
        today = sess[i]
        # 1) settle exits executed at today's open
        still = []
        for t in pending_exit:
            if t.exit_date <= today:
                cash += t.proceeds
            else:
                still.append(t)
        pending_exit = still
        # 2) new entries at today's open (conservative: positions exiting today still occupy slot/cash)
        for sig in by_week.get(i, []):
            open_syms = {t.symbol for t in pending_exit}
            if len(pending_exit) >= policy.max_positions:
                funnel["portfolio:max_positions"] += 1
                continue
            if sig.symbol in open_syms:
                funnel["portfolio:duplicate_symbol"] += 1
                continue
            entry_bar = market.bar(sig.symbol, sig.m_idx)
            if entry_bar is None:
                funnel["portfolio:no_entry_bar"] += 1
                continue
            if entry_bar[0] > sig.cap:
                funnel["portfolio:gap_cancel"] += 1
                continue
            mtm = cash + sum(_mark(market, t, i - 1) for t in pending_exit)
            qty = size_quantity(sig, mtm, cash, policy, costs, sess[sig.l_idx].year)
            if qty < 1:
                funnel["portfolio:quantity_zero"] += 1
                continue
            trade = simulate_trade(market, sig, qty, costs)
            if trade is None:
                funnel["portfolio:gap_cancel"] += 1
                continue
            cash -= trade.entry_debit
            pending_exit.append(trade)
            trades.append(trade)
            funnel["trades"] += 1
            last_needed = max(last_needed, market.index.get(trade.exit_date, i))
        # 3) daily equity mark at close
        equity_curve.append((today, cash + sum(_mark(market, t, i) for t in pending_exit)))
        if i > max(by_week) if by_week else True:
            if not pending_exit and i >= last_needed:
                break
    return Result(trades, funnel, equity_curve, equity0)


def _mark(market: Market, t: Trade, i: int) -> float:
    """Mark an open trade at session i close (falls back to entry cost if no bar)."""
    if i < 0:
        return t.entry_debit
    if market.sessions[i] >= t.exit_date:
        return t.proceeds if t.reason != "unresolved_total_loss" else 0.0
    bar = market.bar(t.symbol, i)
    if bar is None:
        # halted: last known close
        for j in range(i, max(i - 60, -1), -1):
            b = market.bar(t.symbol, j)
            if b is not None:
                return b[3] * t.quantity
        return t.entry_debit
    return bar[3] * t.quantity


# --------------------------------------------------------------------------- statistics

def summarize(result: Result) -> dict:
    tr = result.trades
    eq = result.equity_curve
    out: dict = {"trades": len(tr)}
    if tr:
        wins = [t for t in tr if t.pnl > 0]
        losses = [t for t in tr if t.pnl <= 0]
        gross_win = sum(t.pnl for t in wins)
        gross_loss = -sum(t.pnl for t in losses)
        out.update({
            "win_rate": len(wins) / len(tr),
            "avg_return_per_trade": mean(t.ret for t in tr),
            "median_return_per_trade": sorted(t.ret for t in tr)[len(tr) // 2],
            "avg_win_return": mean(t.ret for t in wins) if wins else None,
            "avg_loss_return": mean(t.ret for t in losses) if losses else None,
            "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else None,
            "total_pnl": sum(t.pnl for t in tr),
            "avg_sessions_held": mean(t.sessions_held for t in tr),
            "exit_reasons": dict(Counter(t.reason for t in tr)),
            "flagged_trades": sum(1 for t in tr if t.flags),
            "by_year": _by_year(tr),
        })
    if eq:
        peak, mdd = -math.inf, 0.0
        for _, v in eq:
            peak = max(peak, v)
            mdd = min(mdd, v / peak - 1)
        out.update({"equity_start": result.equity0, "equity_end": eq[-1][1],
                    "total_return": eq[-1][1] / result.equity0 - 1, "max_drawdown": mdd,
                    "curve_start": eq[0][0].isoformat(), "curve_end": eq[-1][0].isoformat(),
                    "sessions_in_curve": len(eq)})
        if tr:
            held = set()
            for t in tr:
                held.update(d for d, _ in eq if t.entry_date <= d < t.exit_date)
            out["exposure_fraction"] = len(held) / len(eq)
    out["funnel"] = dict(sorted(result.funnel.items()))
    out["labels"] = result.labels
    return out


def _by_year(trades: list[Trade]) -> dict:
    groups: dict[int, list[Trade]] = defaultdict(list)
    for t in trades:
        groups[t.entry_date.year].append(t)
    return {y: {"trades": len(g), "win_rate": sum(1 for t in g if t.pnl > 0) / len(g),
                "avg_return": mean(t.ret for t in g), "total_pnl": sum(t.pnl for t in g)}
            for y, g in sorted(groups.items())}


def write_outputs(result: Result, summary: dict, out_dir: str | Path) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    with (out / "trades.csv").open("w", encoding="utf-8", newline="") as fh:
        cols = list(Trade.__dataclass_fields__.keys()) + ["return"]
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for t in result.trades:
            w.writerow({**asdict(t), "return": f"{t.ret:.6f}"})
    with (out / "equity.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "equity"])
        for d, v in result.equity_curve:
            w.writerow([d.isoformat(), f"{v:.2f}"])
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
