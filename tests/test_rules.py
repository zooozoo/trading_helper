from dataclasses import replace
from datetime import date, timedelta
import unittest

from trading_helper.rules import Bar, Costs, Event, Policy, exit_on_bar, plan_entry, signal_at_close


class RulesTests(unittest.TestCase):
    def setUp(self):
        self.p = Policy()
        self.costs = Costs(0.001, 0.001, 0.002, 0.001)  # arbitrary test fractions
        start = date(2024, 1, 1)  # synthetic calendar
        self.bars = [Bar(start + timedelta(days=i), 10000, 10100, 9900, 10000, 200000)
                     for i in range(20)]
        self.bars.append(Bar(start + timedelta(days=20), 10100, 10500, 10000, 10400, 400000))
        self.e = Event("test", "DEMO", self.bars[-2].date, "new_contract", 10, 100,
                       start, True, start, "synthetic://test")
        self.s = signal_at_close(self.e, self.bars, self.p)
        self.entry = Bar(start + timedelta(days=21), 10400, 10600, 10200, 10500, 300000)

    def plan(self, **kwargs):
        values = dict(expected_session=self.entry.date, equity=10_000_000,
                      available_cash=10_000_000, open_positions=0, policy=self.p, costs=self.costs)
        values.update(kwargs)
        return plan_entry(self.s, self.entry, **values)

    def test_signal_after_receipt_only(self):
        self.assertIsNotNone(self.s)
        self.assertIsNone(signal_at_close(replace(self.e, receipt_date=self.bars[-1].date), self.bars, self.p))
        self.assertIsNone(signal_at_close(self.e, self.bars + [self.entry], self.p))

    def test_future_revenue_and_risk_rejected(self):
        for field in ("revenue_available_date", "risk_available_date"):
            self.assertIsNone(signal_at_close(replace(self.e, **{field: self.entry.date}), self.bars, self.p))

    def test_no_unapproved_or_amended_event(self):
        self.assertIsNone(signal_at_close(replace(self.e, risk_approved=False), self.bars, self.p))
        self.assertIsNone(signal_at_close(replace(self.e, event_type="amendment"), self.bars, self.p))
        with self.assertRaises(ValueError):
            replace(self.e, risk_approved="false")

    def test_previous_window_excludes_reaction(self):
        # Reaction volume would dilute its own ratio if mistakenly included.
        policy = replace(self.p, volume_multiple=2)
        self.assertIsNotNone(signal_at_close(self.e, self.bars, policy))
        self.assertAlmostEqual(self.s.atr, (13 * 200 + 500) / 14)

    def test_low_liquidity_or_zero_revenue_rejected(self):
        self.assertIsNone(signal_at_close(self.e, self.bars, replace(self.p, min_avg_turnover_krw=3e9)))
        self.assertIsNone(signal_at_close(replace(self.e, annual_revenue=0), self.bars, self.p))

    def test_cost_inclusive_sizing_and_cash(self):
        plan = self.plan(available_cash=50000)
        self.assertLessEqual(plan.entry_debit, 50000)
        self.assertLessEqual(plan.planned_loss, 10_000_000 * self.p.risk_fraction)
        self.assertLessEqual(plan.entry_debit, 10_000_000 * self.p.max_position_fraction)
        self.assertEqual(plan.quantity, int(plan.quantity))
        self.assertIsNone(self.plan(available_cash=1))
        self.assertIsNone(self.plan(open_positions=3))

    def test_entry_ignores_future_intrabar_values(self):
        before = self.plan()
        self.entry = replace(self.entry, high=20000, low=1, close=15000)
        self.assertEqual(before, self.plan())

    def test_gap_entry_cancel_and_official_session(self):
        self.entry = replace(self.entry, open=10800, high=11000)
        self.assertIsNone(self.plan())
        with self.assertRaises(ValueError):
            self.plan(expected_session=self.entry.date + timedelta(days=1))

    def test_stop_first_when_intrabar_ambiguous(self):
        plan = self.plan()
        b = Bar(self.entry.date, self.entry.open, plan.target + 10, plan.stop - 10, self.entry.open, 200000)
        result = exit_on_bar(plan, b, held_sessions=1, policy=self.p, costs=self.costs)
        self.assertEqual(result.reason, "stop")
        self.assertAlmostEqual(result.net_pnl, -plan.planned_loss)

    def test_gap_stop_exceeds_planned_loss(self):
        plan = self.plan()
        b = Bar(self.entry.date + timedelta(days=1), 9000, 9200, 8900, 9100, 200000)
        result = exit_on_bar(plan, b, held_sessions=2, policy=self.p, costs=self.costs)
        self.assertEqual(result.reason, "gap_stop")
        self.assertLess(result.net_pnl, -plan.planned_loss)

    def test_open_target_precedes_later_low(self):
        plan = self.plan()
        b = Bar(self.entry.date + timedelta(days=1), plan.target + 10,
                plan.target + 20, plan.stop - 10, plan.target, 200000)
        result = exit_on_bar(plan, b, held_sessions=2, policy=self.p, costs=self.costs)
        self.assertEqual(result.reason, "gap_target_conservative")

    def test_time_exit_and_no_exit(self):
        plan = self.plan()
        b = replace(self.entry, date=self.entry.date + timedelta(days=9))
        self.assertIsNone(exit_on_bar(plan, b, held_sessions=9, policy=self.p, costs=self.costs))
        self.assertEqual(exit_on_bar(plan, b, held_sessions=10, policy=self.p, costs=self.costs).reason, "time_exit")

    def test_invalid_prices_costs_order_and_holding_counter(self):
        with self.assertRaises(ValueError):
            replace(self.entry, close=float("nan"))
        with self.assertRaises(ValueError):
            Costs(0, 0.6, 0.5, 0)
        with self.assertRaises(ValueError):
            signal_at_close(self.e, list(reversed(self.bars)), self.p)
        with self.assertRaises(ValueError):
            exit_on_bar(self.plan(), self.entry, held_sessions=2, policy=self.p, costs=self.costs)

    def test_config_matches_default_policy(self):
        self.assertEqual(Policy.load("config/strategy_v0.json"), self.p)


if __name__ == "__main__":
    unittest.main()
