import json
import tempfile
import unittest
from pathlib import Path

from trading_helper import fundamentals as fd
from trading_helper.opendart import RawStore


class FundamentalsTests(unittest.TestCase):
    def test_amount_and_normalize(self):
        self.assertEqual(fd._amount("1,234"), "1234")
        self.assertEqual(fd._amount("-"), "")
        self.assertEqual(fd._amount("-5,000"), "-5000")
        with tempfile.TemporaryDirectory() as tmp:
            store = RawStore(Path(tmp))
            rows = [
                {"stock_code": "005930", "corp_code": "00126380", "bsns_year": "2023", "reprt_code": "11013", "fs_div": "CFS",
                 "sj_div": "IS", "account_nm": "매출액", "thstrm_amount": "63,745,371,000,000", "frmtrm_amount": "77,781,000,000",
                 "thstrm_dt": "2023.01.01 ~ 2023.03.31", "rcept_no": "20230515002335"},
                {"stock_code": "005930", "corp_code": "00126380", "bsns_year": "2023", "reprt_code": "11013", "fs_div": "CFS",
                 "sj_div": "IS", "account_nm": "매출액", "thstrm_amount": "63,745,371,000,000", "frmtrm_amount": "",
                 "thstrm_dt": "2023.01.01 ~ 2023.03.31", "rcept_no": "20230515002335"},  # duplicate
                {"stock_code": "005930", "corp_code": "00126380", "bsns_year": "2023", "reprt_code": "11013", "fs_div": "CFS",
                 "sj_div": "IS", "account_nm": "총포괄손익", "thstrm_amount": "1", "rcept_no": "20230515002335"},  # not kept
                {"stock_code": " ", "corp_code": "00000001", "bsns_year": "2023", "reprt_code": "11013", "fs_div": "CFS",
                 "sj_div": "BS", "account_nm": "자산총계", "thstrm_amount": "1", "rcept_no": "20230515000001"},  # unlisted
            ]
            store.save("fnlttMultiAcnt", "fnltt_2023_11013_b000.json", json.dumps({"status": "000", "list": rows}).encode(), {})
            store.save("fnlttMultiAcnt", "fnltt_2023_11012_b000.json", json.dumps({"status": "013"}).encode(), {})
            out = Path(tmp) / "fundamentals.csv"
            counts = fd.normalize(store, out)
            self.assertEqual((counts["rows"], counts["skipped_no_data"], counts["skipped_account"], counts["no_stock_code"]), (1, 1, 1, 1))
            text = out.read_text(encoding="utf-8").splitlines()
            self.assertEqual(text[0].split(",")[:3], ["symbol", "corp_code", "bsns_year"])
            self.assertIn("2023-05-15", text[1])
            self.assertIn("63745371000000", text[1])
            self.assertIn(",Q1,", text[1])


if __name__ == "__main__":
    unittest.main()
