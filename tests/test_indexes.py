import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from trading_helper import indexes as ix
from trading_helper.opendart import RawStore


def body(rows):
    return json.dumps({"response": {"header": {"resultCode": "00"}, "body": {"totalCount": len(rows), "numOfRows": 10000,
                                                                                "pageNo": 1, "items": {"item": rows}}}}).encode()


class IndexTests(unittest.TestCase):
    def test_collect_and_normalize(self):
        rows = [{"basDt": "20240103", "idxNm": "코스피", "mkp": "2600.1", "hipr": "2610", "lopr": "2590", "clpr": "2605.5",
                 "trPrc": "1", "lstgMrktTotAmt": "2"},
                {"basDt": "20240102", "idxNm": "코스피", "mkp": "2645.47", "hipr": "2675.8", "lopr": "2641.88", "clpr": "2669.81",
                 "trPrc": "1", "lstgMrktTotAmt": "2"},
                {"basDt": "20240102", "idxNm": "코스피 200", "mkp": "1", "hipr": "1", "lopr": "1", "clpr": "1", "trPrc": "", "lstgMrktTotAmt": ""}]
        calls = []

        def fetcher(url, params):
            calls.append(params)
            return 200, body(rows)
        with tempfile.TemporaryDirectory() as tmp:
            store = RawStore(Path(tmp))
            s = ix.collect("SECRETKEY123", store, ("코스피",), date(2024, 1, 1), date(2024, 1, 31), fetcher=fetcher)
            self.assertEqual(s["indexes"], {"코스피": 2})
            self.assertEqual(calls[0]["idxNm"], "코스피")
            self.assertNotIn("SECRETKEY123", store.manifest_path.read_text(encoding="utf-8"))
            out = Path(tmp) / "indexes.csv"
            n = ix.normalize(store, out)
            self.assertEqual(n["by_index"]["코스피"], {"rows": 2, "date_min": "2024-01-02", "date_max": "2024-01-03"})
            series = ix.load_index_series(out, "코스피")
            self.assertEqual(series[date(2024, 1, 2)], 2669.81)

    def test_gateway_error(self):
        rows, err = ix.parse_response(b'{"OpenAPI_ServiceResponse":{"cmmMsgHeader":{"returnReasonCode":"30","returnAuthMsg":"x"}}}')
        self.assertEqual(rows, [])
        self.assertIn("30", err)


if __name__ == "__main__":
    unittest.main()
