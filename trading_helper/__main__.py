import argparse
from dataclasses import asdict
from datetime import date, timedelta
import json
from pathlib import Path

from .rules import Bar, Costs, Event, Policy, plan_entry, signal_at_close


def main():
    parser = argparse.ArgumentParser(description="Unvalidated research starter")
    parser.add_argument("command", choices=["demo"])
    parser.add_argument("--config", default="config/strategy_v0.json")
    args = parser.parse_args()
    policy = Policy.load(Path(args.config))
    # Fictional continuous session dates; deliberately NOT a KR trading calendar.
    start = date(2024, 1, 1)
    bars = [Bar(start + timedelta(days=i), 10000, 10100, 9900, 10000, 200000)
            for i in range(20)]
    reaction = Bar(start + timedelta(days=20), 10100, 10500, 10000, 10400, 400000)
    bars.append(reaction)
    event = Event("SYNTHETIC-001", "DEMO", bars[-2].date, "new_contract",
                  10_000_000_000, 100_000_000_000, bars[0].date,
                  True, bars[0].date, "synthetic://not-a-real-filing")
    signal = signal_at_close(event, bars, policy)
    assert signal is not None
    next_bar = Bar(reaction.date + timedelta(days=1), 10400, 10600, 10200, 10500, 300000)
    # Zero costs ONLY for synthetic arithmetic demonstration; NOT live tax/fee assumptions.
    plan = plan_entry(signal, next_bar, expected_session=next_bar.date,
                      equity=10_000_000, available_cash=10_000_000, open_positions=0,
                      policy=policy, costs=Costs(0, 0, 0, 0))
    print(json.dumps({"data_kind": "SYNTHETIC_NOT_INVESTMENT_ADVICE",
                      "profitability_validated": False, "costs": "zero_demo_only",
                      "signal": asdict(signal), "plan": asdict(plan) if plan else None},
                     ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
