import unittest
from datetime import date

from trading_helper import diagnostics as dx
from trading_helper.weekly_v1 import EventRow
from tests.test_weekly_v1 import flat, make_market, sessions_from


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.sessions = sessions_from(date(2024, 1, 1), 60)
        up = [(100 + i, 101 + i, 99 + i, 100 + i, 1000) for i in range(60)]   # +1/day
        flat_ = flat(60, px=1000, vol=1000)
        self.market = make_market({"000010": up, "000020": flat_}, self.sessions)

    def test_market_proxy_mean_vs_median(self):
        mean_p = dx.market_proxy(self.market, statistic="mean")
        med_p = dx.market_proxy(self.market, statistic="median")
        self.assertAlmostEqual(mean_p[1], (1 / 100 + 0.0) / 2)
        self.assertAlmostEqual(med_p[1], (1 / 100 + 0.0) / 2)  # two stocks: median == mean
        self.assertEqual(mean_p[0], 0.0)

    def test_event_table_and_placebo(self):
        proxy = dx.market_proxy(self.market)
        ev = EventRow("E1", "000010", self.sessions[25], "new_contract", 10e9, 100e9, self.sessions[25], True)
        caps = {("000010", self.sessions[24]): 50e9}
        rows, funnel = dx.event_table(self.market, [ev], segment=(self.sessions[0], self.sessions[-1]), proxy=proxy,
                                      caps=caps, min_turnover=0)
        self.assertEqual(funnel["rows"], 1)
        r = rows[0]
        self.assertTrue(r["receipt_is_session"])
        self.assertEqual(r["reaction_date"], self.sessions[26].isoformat())
        self.assertAlmostEqual(r["contract_to_mktcap"], 0.2)
        # stock rises 1/day vs proxy ~half of that -> positive abnormal returns
        self.assertGreater(r["post_4_abn"], 0)
        self.assertTrue(r["breakout_at_r"])
        self.assertEqual(r["bucket_cap_ratio"], ">15%")
        pl = dx.placebo_rows(self.market, rows, proxy, min_turnover=0)
        self.assertEqual(len(pl), 1)
        self.assertEqual(pl[0]["symbol"], "000020")  # the only other symbol
        self.assertLess(pl[0]["post_4_abn"], 0)        # flat stock underperforms the proxy
        summary = dx.summarize(rows, liquid_only=False)
        self.assertEqual(summary["n_events"], 1)
        self.assertIn("breakout_and_volume", summary)
        self.assertEqual(dx.summarize_placebo(pl)["n"], 1)

    def test_bucket_edges(self):
        self.assertEqual(dx.bucket(-0.01, dx.REACTION_BUCKETS), "<0%")
        self.assertEqual(dx.bucket(0.03, dx.REACTION_BUCKETS), "3-10%")
        self.assertEqual(dx.bucket(None, dx.REACTION_BUCKETS), "n/a")


if __name__ == "__main__":
    unittest.main()
