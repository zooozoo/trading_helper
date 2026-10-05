import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from trading_helper import screen as sc
from trading_helper.opendart import RawStore


class ScreenTests(unittest.TestCase):
    def test_clean_title(self):
        self.assertEqual(sc.clean_title("[기재정정]투자판단관련주요경영사항      (특허권 취득)"),
                         ("투자판단관련주요경영사항", "특허권 취득", "기재정정"))
        self.assertEqual(sc.clean_title("주요사항보고서(무상증자결정)"), ("주요사항보고서(무상증자결정)", None, ""))
        self.assertEqual(sc.clean_title("단일판매ㆍ공급계약체결(자율공시)")[0], "단일판매ㆍ공급계약체결")

    def test_extract_events_types_amendments_duplicates(self):
        types = sc.load_type_config("config/event_types_screen_v1.json")
        with tempfile.TemporaryDirectory() as tmp:
            store = RawStore(Path(tmp))
            rows = [
                {"rcept_no": "20240102000001", "stock_code": "000010", "rcept_dt": "20240102", "report_nm": "주식소각결정"},
                {"rcept_no": "20240102000002", "stock_code": "000010", "rcept_dt": "20240102", "report_nm": "주식소각결정"},  # dup
                {"rcept_no": "20240103000003", "stock_code": "000010", "rcept_dt": "20240103", "report_nm": "[기재정정]주식소각결정"},
                {"rcept_no": "20240104000004", "stock_code": "000020", "rcept_dt": "20240104", "report_nm": "투자판단관련주요경영사항   (특허권 취득)"},
                {"rcept_no": "20240104000005", "stock_code": "000020", "rcept_dt": "20240104", "report_nm": "투자판단관련주요경영사항   (임상 3상 승인)"},
                {"rcept_no": "20240105000006", "stock_code": "000030", "rcept_dt": "20240105", "report_nm": "주요사항보고서(무상증자결정)"},
                {"rcept_no": "20240105000007", "stock_code": "", "rcept_dt": "20240105", "report_nm": "주요사항보고서(무상증자결정)"},  # unlisted
                {"rcept_no": "20190105000008", "stock_code": "000030", "rcept_dt": "20190105", "report_nm": "주요사항보고서(무상증자결정)"},  # old
            ]
            store.save("list", "list_x_p0001.json", json.dumps({"status": "000", "list": rows}).encode(), {})
            ev, stats = sc.extract_events(store, types, min_date=date(2020, 1, 1))
            self.assertEqual([e.event_id for e in ev["share_cancellation"]], ["20240102000001"])
            self.assertEqual(stats["duplicate_same_day"], 1)
            self.assertEqual(stats["amended_skipped"], 1)
            self.assertEqual(len(ev["key_matter_patent"]), 1)
            self.assertEqual(len(ev["key_matter_clinical_approval"]), 1)
            self.assertEqual([e.symbol for e in ev["bonus_issue"]], ["000030"])
            self.assertNotIn("rights_issue", ev)


if __name__ == "__main__":
    unittest.main()
