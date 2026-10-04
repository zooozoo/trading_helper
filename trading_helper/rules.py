"""Deterministic research primitives, not a complete backtest or order system."""

from dataclasses import dataclass, asdict
from datetime import date
import json
import math
from pathlib import Path
from statistics import mean


@dataclass(frozen=True)
class Policy:
    strategy_id: str = "kr_contract_breakout_v0"
    status: str = "UNVALIDATED_HYPOTHESIS"
    market: str = "KR"
    direction: str = "long_only_cash"
    min_contract_revenue_ratio: float = 0.05
    lookback: int = 20
    volume_multiple: float = 1.5
    atr_period: int = 14
    atr_multiple: float = 2.0
    reward_multiple: float = 2.0
    max_entry_gap: float = 0.03
    hold_sessions: int = 10
    min_avg_turnover_krw: float = 1_000_000_000
    risk_fraction: float = 0.005
    max_position_fraction: float = 0.2
    max_positions: int = 3
    same_bar_policy: str = "stop_first"
    weekly_report_is_not_order_execution: bool = True

    def __post_init__(self):
        if self.lookback < 1 or not 1 <= self.atr_period <= self.lookback:
            raise ValueError("invalid lookback/ATR period")
        for field in ("volume_multiple", "atr_multiple", "reward_multiple"):
            value = getattr(self, field)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"invalid {field}")
        for field in ("risk_fraction", "max_position_fraction"):
            value = getattr(self, field)
            if not math.isfinite(value) or not 0 < value <= 1:
                raise ValueError(f"invalid {field}")
        for field in ("min_contract_revenue_ratio", "min_avg_turnover_krw", "max_entry_gap"):
            value = getattr(self, field)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"invalid {field}")
        if self.hold_sessions < 1 or self.max_positions < 1:
            raise ValueError("invalid portfolio/holding constraints")
        if self.same_bar_policy != "stop_first":
            raise ValueError("only stop_first supported")
        if self.market != "KR" or self.direction != "long_only_cash":
            raise ValueError("only KR long_only_cash supported")

    @classmethod
    def load(cls, path: str | Path):
        return cls(**json.loads(Path(path).read_text(encoding="utf-8")))


@dataclass(frozen=True)
class Bar:
    date: date
    open: float
    high: float
    low: float
    close: float
    volume: int

    def __post_init__(self):
        values = (self.open, self.high, self.low, self.close)
        if not all(math.isfinite(v) and v > 0 for v in values):
            raise ValueError("OHLC must be finite and positive")
        if not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high:
            raise ValueError("invalid OHLC bounds")
        if not isinstance(self.volume, int) or self.volume < 0:
            raise ValueError("volume must be a nonnegative integer")


@dataclass(frozen=True)
class Event:
    event_id: str
    symbol: str
    receipt_date: date
    event_type: str
    contract_amount: float
    annual_revenue: float
    revenue_available_date: date
    risk_approved: bool
    risk_available_date: date
    source_url: str

    def __post_init__(self):
        if not self.event_id or not self.symbol or not self.source_url:
            raise ValueError("missing event identity/source")
        if not isinstance(self.risk_approved, bool):
            raise ValueError("risk_approved must be a bool, not a string")
        if not all(math.isfinite(v) for v in (self.contract_amount, self.annual_revenue)):
            raise ValueError("nonfinite event amounts")


@dataclass(frozen=True)
class Signal:
    event_id: str
    symbol: str
    date: date
    close: float
    atr: float


def signal_at_close(event: Event, bars: list[Bar], policy: Policy) -> Signal | None:
    """bars contain ONE symbol through completed signal session, never future bars.

    Caller must verify a continuous official exchange calendar and tradability.
    The first session strictly AFTER receipt_date is the only reaction session.
    """
    if any(a.date >= b.date for a, b in zip(bars, bars[1:])):
        raise ValueError("bars must be unique and ordered")
    if len(bars) < policy.lookback + 1:
        return None
    current = bars[-1]
    if not bars[-2].date <= event.receipt_date < current.date:
        return None
    if (event.event_type != "new_contract" or not event.risk_approved
            or event.revenue_available_date > event.receipt_date
            or event.risk_available_date > event.receipt_date
            or event.annual_revenue <= 0 or event.contract_amount <= 0):
        return None
    if event.contract_amount / event.annual_revenue < policy.min_contract_revenue_ratio:
        return None
    previous = bars[-policy.lookback - 1:-1]
    avg_volume = mean(b.volume for b in previous)
    if avg_volume <= 0 or current.volume < avg_volume * policy.volume_multiple:
        return None
    if mean(b.close * b.volume for b in previous) < policy.min_avg_turnover_krw:
        return None
    if current.close <= max(b.high for b in previous):
        return None
    ranges = []
    for i in range(len(bars) - policy.atr_period, len(bars)):
        b, prev = bars[i], bars[i - 1]
        ranges.append(max(b.high - b.low, abs(b.high - prev.close), abs(b.low - prev.close)))
    atr = mean(ranges)
    if atr <= 0:
        return None
    return Signal(event.event_id, event.symbol, current.date, current.close, atr)


@dataclass(frozen=True)
class Costs:
    """Fractions, NOT percent/bps. No live tax defaults: caller must specify all."""
    buy_fee: float
    sell_fee: float
    sell_tax: float
    slippage: float

    def __post_init__(self):
        if not all(math.isfinite(v) and 0 <= v < 1 for v in asdict(self).values()):
            raise ValueError("invalid cost fraction")
        if self.sell_fee + self.sell_tax >= 1:
            raise ValueError("invalid combined sell costs")


@dataclass(frozen=True)
class Plan:
    event_id: str
    symbol: str
    entry_date: date
    entry_price: float
    stop: float
    target: float
    quantity: int
    entry_debit: float
    planned_loss: float


def plan_entry(signal: Signal, next_bar: Bar, *, expected_session: date,
               equity: float, available_cash: float, open_positions: int,
               policy: Policy, costs: Costs) -> Plan | None:
    """Uses next_bar.open only; high/low/close MUST NOT affect entry sizing.

    expected_session must come from an official exchange calendar, not bar gaps.
    Caller must reject overlapping positions in the same symbol.
    """
    if next_bar.date != expected_session or next_bar.date <= signal.date:
        raise ValueError("entry must be the next official session after signal")
    if not all(math.isfinite(v) and v >= 0 for v in (equity, available_cash)):
        raise ValueError("invalid capital")
    if equity == 0 or open_positions >= policy.max_positions:
        return None
    if open_positions < 0:
        raise ValueError("invalid position count")
    if next_bar.open / signal.close - 1 > policy.max_entry_gap + 1e-12:
        return None
    entry = next_bar.open * (1 + costs.slippage)
    stop = entry - policy.atr_multiple * signal.atr
    target = entry + policy.reward_multiple * (entry - stop)
    if stop <= 0:
        return None
    debit_per_share = entry * (1 + costs.buy_fee)
    stop_net = stop * (1 - costs.slippage) * (1 - costs.sell_fee - costs.sell_tax)
    risk_per_share = debit_per_share - stop_net
    quantity = math.floor(min(equity * policy.risk_fraction / risk_per_share,
                              equity * policy.max_position_fraction / debit_per_share,
                              available_cash / debit_per_share))
    if quantity < 1:
        return None
    return Plan(signal.event_id, signal.symbol, next_bar.date, entry, stop, target,
                quantity, debit_per_share * quantity, risk_per_share * quantity)


@dataclass(frozen=True)
class Exit:
    date: date
    price: float
    reason: str
    proceeds: float
    net_pnl: float


def exit_on_bar(plan: Plan, bar: Bar, *, held_sessions: int,
                policy: Policy, costs: Costs) -> Exit | None:
    """Simplified liquid daily-bar model; no halt/limit-lock/corporate actions.

    held_sessions includes entry session as 1. It must be counted by caller.
    An open gap is evaluated before intrabar stop/target ambiguity.
    """
    if bar.date < plan.entry_date or held_sessions < 1:
        raise ValueError("invalid exit time")
    if (bar.date == plan.entry_date) != (held_sessions == 1):
        raise ValueError("holding counter inconsistent with entry date")
    if bar.open <= plan.stop:
        raw, reason = bar.open, "gap_stop"
    elif bar.open >= plan.target:
        raw, reason = plan.target, "gap_target_conservative"
    elif bar.low <= plan.stop:
        raw, reason = plan.stop, "stop"
    elif bar.high >= plan.target:
        raw, reason = plan.target, "target"
    elif held_sessions >= policy.hold_sessions:
        raw, reason = bar.close, "time_exit"
    else:
        return None
    price = raw * (1 - costs.slippage)
    proceeds = price * plan.quantity * (1 - costs.sell_fee - costs.sell_tax)
    return Exit(bar.date, price, reason, proceeds, proceeds - plan.entry_debit)
