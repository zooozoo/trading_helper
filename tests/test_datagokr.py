import csv
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from trading_helper import datagokr as dg
from trading_helper.opendart import RawStore
from trading_helper.validate import validate_prices


def row(sym, name, isin, close, o=None, h=None, l=None, vol=1000, shares="1000000", market="KOSPI"):
    o = close if o is None else o
    h = max(o, close) + 10 if h is None else h
    l = min(o, close) - 10 if l is None else l
    return {"basDt": "x", "srtnCd": sym, "isinCd": isin, "itmsNm": name, "mrktCtg": market,
            "clpr": str(close), "mkp": str(o), "hipr": str(h), "lopr": str(l), "trqu": str(vol),
            "trPrc": "0", "lstgStCnt": shares, "mrktTotAmt": "0", "vs": "0", "fltRt": "0"}


def body(rows, total=None):
    return json.dumps({"response": {"header": {"resultCode": "00", "resultMsg": "NORMAL SERVICE."},
                                    "body": {"numOfRows": 10000, "pageNo": 1,
                                             "totalCount": len(rows) if total is None else total,
                                             "items": {"item": rows} if rows else ""}}}).encode()


class FakeFetcher:
    def __init__(self, by_day):
        self.by_day = by_day  # 'YYYYMMDD' -> (status, bytes)
        self.calls = []

    def __call__(self, url, params):
        self.calls.append(params["basDt"])
        return self.by_day[params["basDt"]]


class DataGoKrTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RawStore(Path(self.tmp.name) / "raw")
        self.key = "SECRET64"

    def tearDown(self):
        self.tmp.cleanup()

    def client(self, fetcher):
        return dg.Client(self.key, self.store, fetcher=fetcher, min_interval_s=0, sleep=lambda s: None)

    def test_parse_gateway_error_and_truncation(self):
        gw = b'{"OpenAPI_ServiceResponse":{"cmmMsgHeader":{"errMsg":"x","returnAuthMsg":"\xeb\x93\xb1\xeb\xa1\x9d\xeb\x90\x98\xec\xa7\x80 \xec\x95\x8a\xec\x9d\x80 \xec\x84\x9c\xeb\xb9\x84\xec\x8a\xa4\xed\x82\xa4","returnReasonCode":"30"}}}'
        rows, err = dg.parse_response(gw)
        self.assertEqual(rows, [])
        self.assertIn("30", err)
        rows, err = dg.parse_response(body([row("000010", "a", "KR7000010003", 100)], total=5))
        self.assertEqual(len(rows), 1)
        self.assertIn("truncated", err)

    def test_collect_is_weekday_only_resumable_and_masks_key(self):
        days = {"20240102": (200, body([row("000010", "알파", "KR7000010003", 100)])),
                "20240103": (200, body([])),  # holiday-like
                "20240104": (200, body([row("000010", "알파", "KR7000010003", 101)])),
                "20240105": (200, body([row("000010", "알파", "KR7000010003", 102)]))}
        f = FakeFetcher(days)
        # Tue 2024-01-02 .. Sun 2024-01-07: Sat/Sun must never be requested.
        summary = dg.collect(self.client(f), date(2024, 1, 2), date(2024, 1, 7), max_requests=10)
        self.assertEqual(f.calls, ["20240102", "20240103", "20240104", "20240105"])
        self.assertEqual(summary["requests_made"], 4)
        self.assertEqual(summary["empty_weekdays_this_run"], ["2024-01-03"])
        self.assertNotIn(self.key, self.store.manifest_path.read_text(encoding="utf-8"))
        again = dg.collect(self.client(FakeFetcher(days)), date(2024, 1, 2), date(2024, 1, 7), max_requests=10)
        self.assertEqual((again["requests_made"], again["days_skipped_existing"]), (0, 4))

    def test_max_requests_stops_cleanly(self):
        days = {d: (200, body([])) for d in ("20240102", "20240103", "20240104")}
        summary = dg.collect(self.client(FakeFetcher(days)), date(2024, 1, 2), date(2024, 1, 4), max_requests=2)
        self.assertEqual(summary["requests_made"], 2)
        self.assertIn("stopped", summary)

    def test_gateway_error_raises_without_key(self):
        gw = (403, b'{"OpenAPI_ServiceResponse":{"cmmMsgHeader":{"returnReasonCode":"30","returnAuthMsg":"bad"}}}')
        with self.assertRaises(dg.DataGoKrError) as ctx:
            dg.collect(self.client(FakeFetcher({"20240102": gw})), date(2024, 1, 2), date(2024, 1, 2), max_requests=5)
        self.assertNotIn(self.key, str(ctx.exception))

    def test_classify_security(self):
        self.assertEqual(dg.classify_security("KR7005930003", "삼성전자"), (True, ""))
        self.assertEqual(dg.classify_security("KR7005931001", "삼성전자우"), (False, "preferred_isin"))
        self.assertEqual(dg.classify_security("KR7000010003", "대신밸런스제16호스팩"), (False, "spac"))
        self.assertEqual(dg.classify_security("KR7000010003", "현대차2우B"), (False, "preferred_name"))

    def test_normalize_outputs(self):
        d1 = [row("000010", "알파", "KR7000010003", 1000, shares="100"),
              row("000020", "베타우", "KR7000021001", 500),
              row("000030", "감마", "KR7000030003", 300, o=0, h=0, l=0, vol=0)]  # halted
        d2 = [row("000010", "알파", "KR7000010003", 200, shares="500"),  # 5:1 split candidate
              row("000030", "감마", "KR7000030003", 310)]
        self.store.save("daily", "20240102.json", body(d1), {"rows": 3})
        self.store.save("daily", "20240103.json", body([]), {"rows": 0})
        self.store.save("daily", "20240104.json", body(d2), {"rows": 2})
        out = Path(self.tmp.name) / "norm"
        s = dg.normalize(self.store, out)
        c = s["counts"]
        self.assertEqual((c["trading_days"], c["no_row_weekdays"], c["price_rows"], c["halt_rows"]), (2, 1, 4, 1))
        self.assertEqual(c["share_count_changes"], 1)
        self.assertTrue(validate_prices(out / "prices.csv").ok)
        with open(out / "securities.csv", encoding="utf-8") as fh:
            secs = {r["symbol"]: r for r in csv.DictReader(fh)}
        self.assertEqual(secs["000020"]["is_common_stock"], "false")
        self.assertEqual(secs["000010"]["sessions"], "2")
        self.assertEqual(secs["000030"]["last_seen"], "2024-01-04")
        with open(out / "share_count_changes.csv", encoding="utf-8") as fh:
            change = list(csv.DictReader(fh))[0]
        self.assertEqual((change["symbol"], change["ratio"][:3], change["note"]), ("000010", "5.0", "split_or_merge_candidate"))
        with open(out / "halts.csv", encoding="utf-8") as fh:
            halt = list(csv.DictReader(fh))[0]
        self.assertEqual((halt["symbol"], halt["date"]), ("000030", "2024-01-02"))
        with open(out / "calendar.csv", encoding="utf-8") as fh:
            cal = [r["status"] for r in csv.DictReader(fh)]
        self.assertEqual(cal, ["trading_day", "no_rows_holiday_or_missing", "trading_day"])


if __name__ == "__main__":
    unittest.main()
