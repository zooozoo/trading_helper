import tempfile
import unittest
from pathlib import Path

from trading_helper.validate import (EVENT_COLUMNS, PRICE_COLUMNS, validate_all,
                                     validate_events, validate_prices)

PRICE_OK = ["005930,2024-01-02,70000,71000,69500,70500,1000000,synthetic,2024-01-03T08:00:00+09:00"]
EVENT_OK = ["E1,005930,2024-01-02,new_contract,10000000000,100000000000,2023-03-20,true,2023-03-31,"
            "https://dart.fss.or.kr/x,2024-01-03T08:00:00+09:00"]


def write(dirpath, name, header, rows):
    p = Path(dirpath) / name
    p.write_text("\n".join([",".join(header)] + rows) + "\n", encoding="utf-8")
    return p


class ValidateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def errors(self, report):
        return [(i.row, i.column) for i in report.errors]

    def test_templates_are_valid_empty_files(self):
        self.assertTrue(validate_prices("data/templates/prices.csv").ok)
        self.assertTrue(validate_events("data/templates/events.csv").ok)

    def test_valid_rows(self):
        p = write(self.dir, "p.csv", PRICE_COLUMNS, PRICE_OK)
        e = write(self.dir, "e.csv", EVENT_COLUMNS, EVENT_OK)
        result = validate_all(p, e)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["files"][1]["stats"]["signal_eligible_rows"], 1)
        self.assertEqual(len(result["files"][0]["sha256"]), 64)

    def test_header_mismatch_and_missing_file(self):
        p = write(self.dir, "p.csv", ["symbol", "date"], [])
        self.assertFalse(validate_prices(p).ok)
        self.assertFalse(validate_prices(Path(self.dir) / "nope.csv").ok)

    def test_price_bounds_duplicates_and_fetch_time(self):
        rows = PRICE_OK + [
            "005930,2024-01-02,70000,71000,69500,70500,1000000,synthetic,2024-01-03T08:00:00+09:00",  # dup
            "005930,2024-01-04,70000,69000,69500,70500,1000000,synthetic,2024-01-05",  # high<open
            "005930,2024-01-05,70000,71000,69500,70500,-5,synthetic,2024-01-06T08:00:00+09:00",  # volume
            "005930,2024-01-08,70000,71000,69500,70500,100,synthetic,2024-01-07T08:00:00+09:00",  # fetched early
            "005930,2024-01-09,70000,71000,69500,70500,100,synthetic,2024-01-09T10:00:00+09:00",  # same day
        ]
        report = validate_prices(write(self.dir, "p.csv", PRICE_COLUMNS, rows))
        errs = self.errors(report)
        self.assertIn((2, None), errs)
        self.assertIn((3, None), errs)
        self.assertIn((4, "volume"), errs)
        self.assertIn((5, "fetched_at"), errs)
        self.assertNotIn((6, "fetched_at"), errs)
        self.assertTrue(any(i.row == 6 and i.level == "warning" for i in report.issues))
        self.assertTrue(any(i.row == 3 and i.column == "fetched_at" and i.level == "warning"
                            for i in report.issues))  # no time component

    def test_symbol_leading_zero_preserved(self):
        report = validate_prices(write(self.dir, "p.csv", PRICE_COLUMNS, PRICE_OK))
        self.assertEqual(report.stats["symbols"], 1)
        self.assertFalse(any(i.column == "symbol" for i in report.issues))

    def test_event_rules(self):
        base = EVENT_OK[0].split(",")

        def row(**kw):
            d = dict(zip(EVENT_COLUMNS, base))
            d.update(kw)
            return ",".join(d[c] for c in EVENT_COLUMNS)

        rows = [
            row(),
            row(event_id="E1"),  # dup id
            row(event_id="E3", risk_approved="TRUE"),  # literal only
            row(event_id="E4", risk_approved="true", risk_available_date=""),  # evidence date required
            row(event_id="E5", risk_approved="true", risk_available_date="2024-01-05"),  # after receipt -> warn
            row(event_id="E6", annual_revenue="", revenue_available_date=""),  # unknown -> warn, not eligible
            row(event_id="E7", annual_revenue="0"),
            row(event_id="E8", event_type="cancellation"),
            row(event_id="E9", event_type="bogus"),
            row(event_id="E10", source_url="dart.fss.or.kr/x"),
            row(event_id="E11", fetched_at="2023-12-31T00:00:00+09:00"),  # fetched before receipt
        ]
        report = validate_events(write(self.dir, "e.csv", EVENT_COLUMNS, rows), price_symbols={"000001"})
        errs = self.errors(report)
        self.assertIn((2, "event_id"), errs)
        self.assertIn((3, "risk_approved"), errs)
        self.assertIn((4, "risk_available_date"), errs)
        self.assertNotIn((5, "risk_available_date"), errs)
        self.assertNotIn((6, "annual_revenue"), errs)
        self.assertIn((7, "annual_revenue"), errs)
        self.assertNotIn((8, "event_type"), errs)
        self.assertIn((9, "event_type"), errs)
        self.assertIn((10, "source_url"), errs)
        self.assertIn((11, "fetched_at"), errs)
        self.assertEqual(report.stats["signal_eligible_rows"], 1)
        self.assertEqual(report.stats["event_types"]["cancellation"], 1)
        self.assertTrue(any(i.column == "symbol" and i.level == "warning" for i in report.issues))


if __name__ == "__main__":
    unittest.main()
