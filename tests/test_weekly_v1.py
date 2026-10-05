import unittest
from array import array
from datetime import date, timedelta

from trading_helper import weekly_v1 as wv
from trading_helper.market import MISSING, Market, Security

POLICY = wv.WeeklyPolicy("test", 0.05, 20, 1.5, 14, 2.0, 2.0, 0.03, 3, 1_000_000_000, 0.005, 0.2, 3)
COSTS = wv.CostTable(0.00015, 0.00015, {2024: 0.0018}, 0.001)


def sessions_from(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def make_market(symbol_bars: dict[str, list[tuple | None]], sessions: list[date], splits=None) -> Market:
    bars = {}
    for sym, rows in symbol_bars.items():
        arrs = {k: array("q", [MISSING]) * len(sessions) for k in ("o", "h", "l", "c", "v")}
        for i, r in enumerate(rows):
            if r is None:
                continue
            arrs["o"][i], arrs["h"][i], arrs["l"][i], arrs["c"][i], arrs["v"][i] = r
        bars[sym] = arrs
    secs = {s: Security(s, s, "KOSPI", True, sessions[0], sessions[-1]) for s in symbol_bars}
    return Market(sessions, secs, bars, splits or {})


def flat(n, px=10000, vol=200_000):
    return [(px, px + 100, px - 100, px, vol)] * n


class WeeklyV1Tests(unittest.TestCase):
    def setUp(self):
        # 2024-01-01 is a Monday; 30 sessions = 6 full weeks. Breakout on Friday 2024-02-02 (idx 24).
        self.sessions = sessions_from(date(2024, 1, 1), 30)
        rows = flat(24) + [(10100, 10500, 10000, 10400, 400_000)] + flat(5, px=10400)
        self.market = make_market({"000010": rows}, self.sessions)
        self.f_idx = 24
        self.assertEqual(self.sessions[self.f_idx], date(2024, 2, 2))
        self.event = wv.EventRow("E1", "000010", date(2024, 2, 1), "new_contract", 10e9, 100e9, date(2024, 2, 1), True)
        self.seg = (date(2024, 1, 1), date(2024, 12, 31))

    def test_signal_requires_reaction_on_last_session_of_week(self):
        sigs, funnel = wv.generate_signals(self.market, [self.event], POLICY, segment=self.seg, risk_filter="approved_only")
        self.assertEqual(len(sigs), 1, funnel)
        s = sigs[0]
        self.assertEqual((s.f_idx, s.m_idx, s.l_idx), (24, 25, 29))
        self.assertAlmostEqual(s.atr, (13 * 200 + 500) / 14)
        self.assertAlmostEqual(s.cap, 10400 * 1.03)
        self.assertAlmostEqual(s.stop, 10400 - 2 * s.atr)
        # Receipt on Tuesday -> reaction Wednesday, not week end -> rejected
        mid = wv.EventRow("E2", "000010", date(2024, 1, 30), "new_contract", 10e9, 100e9, date(2024, 1, 30), True)
        sigs, funnel = wv.generate_signals(self.market, [mid], POLICY, segment=self.seg, risk_filter="approved_only")
        self.assertEqual(sigs, [])
        self.assertEqual(funnel["reject:reaction_not_last_session_of_week"], 1)

    def test_variant_b_judges_at_week_end_and_pre_coverage_events_rejected(self):
        policy_b = wv.WeeklyPolicy("test", 0.05, 20, 1.5, 14, 2.0, 2.0, 0.03, 3, 1_000_000_000, 0.005, 0.2, 3,
                                   signal_variant="B")
        mid = wv.EventRow("E2", "000010", date(2024, 1, 30), "new_contract", 10e9, 100e9, date(2024, 1, 30), True)
        sigs, funnel = wv.generate_signals(self.market, [mid], policy_b, segment=self.seg, risk_filter="approved_only")
        self.assertEqual(len(sigs), 1, funnel)
        self.assertEqual((sigs[0].f_idx, sigs[0].m_idx), (24, 25))
        old = wv.EventRow("E0", "000010", date(2019, 6, 3), "new_contract", 10e9, 100e9, date(2019, 6, 3), True)
        _, funnel = wv.generate_signals(self.market, [old], policy_b, segment=self.seg, risk_filter="approved_only")
        self.assertEqual(funnel["reject:before_price_coverage"], 1)
        self.assertEqual(wv.WeeklyPolicy.load("config/strategy_weekly_v1.json").signal_variant, "A")

    def test_risk_filter_and_other_rejections(self):
        unapproved = wv.EventRow("E3", "000010", date(2024, 2, 1), "new_contract", 10e9, 100e9, date(2024, 2, 1), False)
        _, funnel = wv.generate_signals(self.market, [unapproved], POLICY, segment=self.seg, risk_filter="approved_only")
        self.assertEqual(funnel["reject:risk_not_approved"], 1)
        sigs, _ = wv.generate_signals(self.market, [unapproved], POLICY, segment=self.seg, risk_filter="none")
        self.assertEqual(len(sigs), 1)
        small = wv.EventRow("E4", "000010", date(2024, 2, 1), "new_contract", 1e9, 100e9, date(2024, 2, 1), True)
        _, funnel = wv.generate_signals(self.market, [small], POLICY, segment=self.seg, risk_filter="none")
        self.assertEqual(funnel["reject:ratio_below_min"], 1)
        late_rev = wv.EventRow("E5", "000010", date(2024, 2, 1), "new_contract", 10e9, 100e9, date(2024, 2, 5), True)
        _, funnel = wv.generate_signals(self.market, [late_rev], POLICY, segment=self.seg, risk_filter="none")
        self.assertEqual(funnel["reject:revenue_after_receipt"], 1)
        _, funnel = wv.generate_signals(self.market, [self.event], POLICY, segment=(date(2025, 1, 1), date(2025, 12, 31)), risk_filter="none")
        self.assertEqual(funnel["reject:outside_segment"], 1)
        split_market = make_market({"000010": self.market.bars["000010"] and [
            (self.market.bars["000010"]["o"][i], self.market.bars["000010"]["h"][i], self.market.bars["000010"]["l"][i],
             self.market.bars["000010"]["c"][i], self.market.bars["000010"]["v"][i]) for i in range(30)]},
            self.sessions, splits={"000010": [(date(2024, 2, 6), 5.0)]})
        _, funnel = wv.generate_signals(split_market, [self.event], POLICY, segment=self.seg, risk_filter="none")
        self.assertEqual(funnel["reject:split_candidate_in_window"], 1)

    def test_halt_in_history_rejects(self):
        rows = flat(24) + [(10100, 10500, 10000, 10400, 400_000)] + flat(5, px=10400)
        rows[10] = None
        m = make_market({"000010": rows}, self.sessions)
        _, funnel = wv.generate_signals(m, [self.event], POLICY, segment=self.seg, risk_filter="none")
        self.assertEqual(funnel["reject:insufficient_or_halted_history"], 1)

    def run_one(self, week_rows):
        rows = flat(24) + [(10100, 10500, 10000, 10400, 400_000)] + week_rows
        m = make_market({"000010": rows}, self.sessions)
        sigs, funnel = wv.generate_signals(m, [self.event], POLICY, segment=self.seg, risk_filter="none")
        res = wv.run_portfolio(m, sigs, POLICY, COSTS, equity0=10_000_000, funnel=funnel)
        return m, sigs, res

    def test_gap_cancel(self):
        m, sigs, res = self.run_one([(10800, 11000, 10700, 10900, 300_000)] + flat(4, px=10900))
        self.assertEqual(res.trades, [])
        self.assertEqual(res.funnel["portfolio:gap_cancel"], 1)

    def test_time_exit_at_last_session_open(self):
        m, sigs, res = self.run_one(flat(5, px=10400))
        self.assertEqual(len(res.trades), 1)
        t = res.trades[0]
        self.assertEqual((t.entry_date, t.exit_date, t.reason), (self.sessions[25], self.sessions[29], "time_exit"))
        self.assertAlmostEqual(t.entry_price, 10400 * 1.001)
        self.assertAlmostEqual(t.exit_price, 10400 * 0.999)
        self.assertEqual(t.sessions_held, 5)
        self.assertLessEqual(t.entry_debit, 10_000_000 * 0.2)
        # planned loss at cap <= risk budget
        risk_ps = t.cap * 1.001 * 1.00015 - t.stop * 0.999 * (1 - 0.00015 - 0.0018)
        self.assertLessEqual(risk_ps * t.quantity, 10_000_000 * 0.005 + 1e-6)
        self.assertEqual(res.equity_curve[-1][1], 10_000_000 + t.pnl)

    def test_stop_close_triggers_next_open_fill(self):
        s = sigs = None
        stop_close = 9000
        week = [(10400, 10500, 10300, 10400, 300_000),       # Mon (entry)
                (10300, 10400, 8900, stop_close, 300_000),    # Tue close below stop
                (8500, 8600, 8400, 8550, 300_000),            # Wed open (exit here, with gap)
                (8550, 8600, 8500, 8550, 300_000), (8550, 8600, 8500, 8550, 300_000)]
        m, sigs, res = self.run_one(week)
        t = res.trades[0]
        self.assertEqual((t.reason, t.exit_date), ("stop", self.sessions[27]))
        self.assertAlmostEqual(t.exit_price, 8500 * 0.999)
        self.assertLess(t.pnl, 0)
        # gap below stop is NOT filled at the stop price
        self.assertLess(t.exit_price, t.stop)

    def test_target_and_halt_delay(self):
        week = [(10400, 10500, 10300, 10400, 300_000),
                (10400, 12000, 10300, 11900, 300_000),  # Tue close above target
                None,                                   # Wed halted -> exit delayed
                (11800, 11900, 11700, 11800, 300_000), (11800, 11900, 11700, 11800, 300_000)]
        m, sigs, res = self.run_one(week)
        t = res.trades[0]
        self.assertEqual((t.reason, t.exit_date), ("target", self.sessions[28]))
        self.assertIn("exit_delayed_by_halt:1", t.flags)
        self.assertGreater(t.pnl, 0)

    def test_portfolio_limits_and_summary(self):
        rows = flat(24) + [(10100, 10500, 10000, 10400, 400_000)] + flat(5, px=10400)
        syms = {f"0000{i}0": rows for i in range(1, 6)}
        m = make_market(syms, self.sessions)
        events = [wv.EventRow(f"E{s}", s, date(2024, 2, 1), "new_contract", 10e9, 100e9, date(2024, 2, 1), True) for s in syms]
        sigs, funnel = wv.generate_signals(m, events, POLICY, segment=self.seg, risk_filter="none")
        self.assertEqual(len(sigs), 5)
        res = wv.run_portfolio(m, sigs, POLICY, COSTS, equity0=10_000_000, funnel=funnel)
        self.assertEqual(len(res.trades), 3)
        self.assertEqual(res.funnel["portfolio:max_positions"], 2)
        self.assertEqual([t.symbol for t in res.trades], ["000010", "000020", "000030"])  # symbol order
        summary = wv.summarize(res)
        self.assertEqual(summary["trades"], 3)
        self.assertIn("max_drawdown", summary)
        self.assertEqual(summary["funnel"]["signals"], 5)


if __name__ == "__main__":
    unittest.main()
