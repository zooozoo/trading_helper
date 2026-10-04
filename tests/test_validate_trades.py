import tempfile
import unittest
from pathlib import Path

from trading_helper.validate import TRADE_COLUMNS, validate_all, validate_trades

BUY = "T1,20240102000001,000010,buy,2024-01-08,09:00,10300,100,150,0,limit_open,entry,B1,,2024-01-08T20:00:00+09:00"
SELL = "T2,20240102000001,000010,sell,2024-01-10,09:00,10900,100,160,196,market_open,target,B2,,2024-01-10T20:00:00+09:00"


def write(dirpath, rows):
    p = Path(dirpath) / "t.csv"
    p.write_text("\n".join([",".join(TRADE_COLUMNS)] + rows) + "\n", encoding="utf-8")
    return p


def row(base, **kw):
    d = dict(zip(TRADE_COLUMNS, base.split(",")))
    d.update(kw)
    return ",".join(d[c] for c in TRADE_COLUMNS)


class TradesValidateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_template_and_round_trip(self):
        self.assertTrue(validate_trades("data/templates/trades.csv").ok)
        r = validate_trades(write(self.tmp.name, [BUY, SELL]))
        self.assertTrue(r.ok, r.issues)
        self.assertEqual(r.stats["open_positions"], {})
        self.assertFalse(r.warnings)

    def test_open_position_warns_and_oversell_errors(self):
        r = validate_trades(write(self.tmp.name, [BUY]))
        self.assertTrue(r.ok)
        self.assertEqual(r.stats["open_positions"], {"000010": 100})
        self.assertTrue(any(i.level == "warning" and i.row is None for i in r.issues))
        r = validate_trades(write(self.tmp.name, [BUY, row(SELL, quantity="150")]))
        self.assertIn((2, "quantity"), [(i.row, i.column) for i in r.errors])

    def test_procedure_and_value_rules(self):
        rows = [
            row(BUY, order_type="other"),                       # 1 deviation warning
            row(SELL, trade_id="T3", tax="0"),                  # 2 zero sell tax warning
            row(BUY, trade_id="T4", trade_date="2024-01-11", order_reason="stop"),  # 3 buy must be entry
            row(SELL, trade_id="T5", trade_date="2024-01-12", side="SELL"),        # 4 side literal
            row(SELL, trade_id="T6", trade_date="2024-01-12", fee="-1"),           # 5 fee negative
            row(SELL, trade_id="T7", trade_date="2024-01-12", recorded_at="2024-01-01T00:00:00+09:00"),  # 6
            row(BUY, trade_id="T8", trade_date="2024-01-05", signal_id=""),        # 7 chronological + signal
            row(BUY, trade_id="T1", trade_date="2024-01-13"),                      # 8 duplicate id
            row(BUY, trade_id="T9", trade_date="2024-01-13", fill_time="9am"),     # 9 fill_time format
        ]
        r = validate_trades(write(self.tmp.name, rows))
        errs = [(i.row, i.column) for i in r.errors]
        warns = [(i.row, i.column) for i in r.warnings]
        self.assertIn((1, "order_type"), warns)
        self.assertIn((2, "tax"), warns)
        self.assertIn((3, "order_reason"), errs)
        self.assertIn((4, "side"), errs)
        self.assertIn((5, "fee"), errs)
        self.assertIn((6, "recorded_at"), errs)
        self.assertIn((7, "trade_date"), errs)
        self.assertIn((7, "signal_id"), errs)
        self.assertIn((8, "trade_id"), errs)
        self.assertIn((9, "fill_time"), errs)

    def test_manual_rows_counted(self):
        r = validate_trades(write(self.tmp.name, [row(BUY, signal_id="MANUAL-1", order_reason="entry")]))
        self.assertEqual(r.stats["manual_rows"], 1)

    def test_validate_all_includes_trades(self):
        result = validate_all(None, None, write(self.tmp.name, [BUY, SELL]))
        self.assertTrue(result["ok"])
        self.assertEqual(result["files"][0]["kind"], "trades")


if __name__ == "__main__":
    unittest.main()
