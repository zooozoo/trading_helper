import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from trading_helper import factor_v1 as fv
from trading_helper import fundamentals_pit as fp
from trading_helper.market import Market, Security
from trading_helper.weekly_v1 import CostTable
from tests.test_weekly_v1 import make_market, sessions_from

COSTS = CostTable(0.00015, 0.00015, {2024: 0.0018, 2025: 0.0015}, 0.001)


class FakeFund:
    def __init__(self, table):
        self.table = table  # symbol -> Snapshot-like

    def snapshot(self, symbol, as_of, *, max_age_days=400):
        return self.table.get(symbol)


def snap(equity, ni, oi, rev, quarters=4):
    return fp.Snapshot(date(2024, 1, 1), date(2024, 1, 1), rev, oi, ni, equity, None, "CFS", quarters)


class FundamentalsPitTests(unittest.TestCase):
    def test_ttm_and_q4_derivation_and_point_in_time(self):
        rows = ["symbol,corp_code,bsns_year,reprt_code,period,fs_div,sj_div,account_nm,thstrm_amount,frmtrm_amount,thstrm_dt,rcept_no,filing_date,fetched_at"]
        def add(year, code, acct, amt, filing, sj="IS"):
            rows.append(f"000010,C1,{year},{code},,CFS,{sj},{acct},{amt},,,R,{filing},x")
        for code, amt, filing in (("11013", 10, "2023-05-15"), ("11012", 20, "2023-08-14"), ("11014", 30, "2023-11-14"), ("11011", 100, "2024-03-12")):
            add(2023, code, "당기순이익(손실)", amt, filing)
            add(2023, code, "매출액", amt * 10, filing)
            add(2023, code, "영업이익", amt * 2, filing)
        add(2024, "11013", "당기순이익(손실)", 15, "2024-05-15"); add(2024, "11013", "매출액", 150, "2024-05-15"); add(2024, "11013", "영업이익", 30, "2024-05-15")
        add(2023, "11011", "자본총계", 1000, "2024-03-12", sj="BS")
        add(2024, "11013", "자본총계", 1100, "2024-05-15", sj="BS")
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "f.csv"
            p.write_text("\n".join(rows) + "\n", encoding="utf-8")
            f = fp.Fundamentals(p)
            # before FY filing: Q1..Q3 2023 only -> fewer than 4 quarters -> no TTM
            s = f.snapshot("000010", date(2024, 3, 11))
            self.assertIsNone(s)  # no BS filed yet
            s = f.snapshot("000010", date(2024, 3, 12))
            self.assertEqual(s.equity, 1000)
            self.assertEqual(s.quarters, 4)
            self.assertEqual(s.ttm_net_income, 100)        # Q4 derived = 100-60 = 40; 10+20+30+40
            self.assertEqual(s.ttm_revenue, 1000)
            s = f.snapshot("000010", date(2024, 6, 1))
            self.assertEqual(s.equity, 1100)
            self.assertEqual(s.ttm_net_income, 105)        # 20+30+40+15
            self.assertIsNone(f.snapshot("000010", date(2026, 1, 1)))  # too old


class FactorEngineTests(unittest.TestCase):
    def setUp(self):
        # 300 sessions from 2023-07-03 so that a 2024 rebalance has >= 130 history
        self.sessions = sessions_from(date(2023, 7, 3), 300)
        n = 300
        def series(start, step, vol=0):
            return [(start + i * step, start + i * step + 10, start + i * step - 10, start + i * step, 100_000 + i) for i in range(n)]
        bars = {"000010": series(10000, 10), "000020": series(10000, 0), "000030": series(10000, -5),
                "000040": series(5000, 2), "000050": series(20000, 1)}
        self.market = make_market(bars, self.sessions)
        for s in list(self.market.securities):
            self.market.securities[s] = Security(s, s, "KOSPI", True, self.sessions[0], self.sessions[-1])
        self.caps = {s: 1e11 for s in bars}
        self.fund = FakeFund({"000010": snap(5e10, 1e10, 2e10, 1e11), "000020": snap(2e10, 5e8, 1e9, 1e10),
                              "000030": snap(8e10, -1e9, 1e9, 5e10), "000040": snap(3e10, 3e9, 4e9, 2e10),
                              "000050": snap(1e10, 2e9, 3e9, 3e10)})
        self.policy = fv.FactorPolicy("t", ("KOSPI",), 0.0, 130, 400, True,
                                      {"V": {"factors": ["bp"], "top_n": 2}}, 20, 5, 10_000_000)
        self.actions = fv.CorporateActions(self.market)

    def test_features_ranks_selection(self):
        rebs = fv.month_rebalances(self.market, (date(2024, 1, 1), date(2024, 12, 31)))
        self.assertTrue(rebs)
        f_idx, m_idx = rebs[0]
        self.assertEqual(self.market.sessions[m_idx].month, 1)
        feats = fv.compute_features(self.market, self.fund, self.caps, f_idx, self.policy)
        self.assertEqual(set(feats), {"000010", "000020", "000030", "000040", "000050"})
        self.assertAlmostEqual(feats["000010"]["bp"], 0.5)
        self.assertIsNotNone(feats["000010"]["mom_6_1"])
        ranks = fv.percentile_ranks(feats, "bp")
        self.assertEqual(ranks["000030"], 1.0)  # highest B/P
        lowvol = fv.percentile_ranks(feats, "lowvol")
        self.assertEqual(lowvol["000020"], 1.0)  # zero vol is best
        scores = fv.composite_scores(feats, ["bp", "ep"])
        top = fv.select_top(scores, 2)
        self.assertEqual(len(top), 2)

    def test_simulation_costs_and_curve(self):
        rebs = fv.month_rebalances(self.market, (date(2024, 1, 1), date(2024, 3, 31)))
        sel = [(f, m, ["000010", "000020"]) for f, m in rebs]
        res = fv.simulate(self.market, sel, COSTS, equity0=10_000_000, actions=self.actions, end_idx=rebs[-1][1] + 20)
        self.assertEqual(len(res.turnover), len(rebs))
        self.assertAlmostEqual(res.turnover[0], 1.0, delta=0.01)   # fully invested at first rebalance
        self.assertLess(res.turnover[1], 0.05)                       # same names -> tiny rebalance trades
        first_day_value = res.equity_curve[0][1]
        self.assertLess(first_day_value, 10_000_000)                 # costs + slippage paid
        self.assertGreater(first_day_value, 9_950_000)
        stats = fv.curve_stats(res.equity_curve, 10_000_000)
        self.assertIn("max_drawdown", stats)
        self.assertEqual(stats["by_year"].keys(), {2024})

    def test_corporate_action_adjustment_and_delisting(self):
        n = 300
        rows = [(1000, 1010, 990, 1000, 1000)] * 150 + [(500, 505, 495, 500, 1000)] * 100 + [None] * 50  # 2:1 split then delisted
        m = make_market({"000010": rows}, self.sessions, splits={"000010": [(self.sessions[160], 2.0)]})
        m.securities["000010"] = Security("000010", "x", "KOSPI", True, self.sessions[0], self.sessions[249])
        actions = fv.CorporateActions(m)
        idx, ratio, method = actions.by_symbol["000010"][0]
        self.assertEqual((idx, ratio, method), (150, 2.0, "price_gap"))
        self.assertAlmostEqual(fv.period_return(m, "000010", 140, 170, actions), 0.0)
        pol = fv.FactorPolicy("t", ("KOSPI",), 0.0, 130, 400, True, {"M": {"factors": ["mom_6_1"], "top_n": 1}}, 5, 5, 1e6)
        feats = fv.compute_features(m, FakeFund({"000010": snap(1e9, 1e8, 1e8, 1e9)}), {"000010": 1e9}, 240, pol, actions)
        self.assertAlmostEqual(feats["000010"]["mom_6_1"], 0.0, places=6)   # split-adjusted: flat, not -50%
        self.assertLess(feats["000010"]["lowvol"], 0.01)                      # split day excluded from vol
        sel = [(140, 141, ["000010"])]
        res = fv.simulate(m, sel, COSTS, equity0=1_000_000, actions=actions, end_idx=260)
        self.assertEqual(res.flags["corporate_action_adjustments"], 1)
        self.assertEqual(res.flags["delisting_liquidations"], 1)
        # value roughly preserved across the split (not halved), then liquidated at 500 after last_seen
        mid = dict(res.equity_curve)[self.sessions[200]]
        self.assertGreater(mid, 950_000)
        self.assertGreater(res.equity_curve[-1][1], 940_000)

    def test_quintiles_and_random_draws(self):
        rebs = fv.month_rebalances(self.market, (date(2024, 1, 1), date(2024, 6, 30)))
        feats_by_f = {f: fv.compute_features(self.market, self.fund, self.caps, f, self.policy) for f, _ in rebs}
        q = fv.quintile_table(self.market, rebs, feats_by_f, "bp", self.actions, 1)
        self.assertEqual(q["months"], len(rebs) - 1)
        elig = {f: sorted(feats_by_f[f]) for f, _ in rebs}
        chosen = {f: ["000010", "000040"] for f, _ in rebs}
        r = fv.random_draw_test(self.market, rebs, elig, chosen, self.actions, draws=20, top_n=2)
        self.assertEqual(r["months"], len(rebs) - 1)
        self.assertIsNotNone(r["chosen_percentile_vs_random"])


if __name__ == "__main__":
    unittest.main()
