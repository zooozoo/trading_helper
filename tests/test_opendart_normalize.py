import csv
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from trading_helper import opendart as od
from trading_helper import opendart_normalize as nz
from trading_helper.validate import validate_events

KOSDAQ_DOC = """<?xml version="1.0" encoding="utf-8"?>
<DOCUMENT><BODY><TABLE>
<TR><TD>1. 판매ㆍ공급계약 내용</TD><TD></TD><TD>장비 공급</TD></TR>
<TR><TD>2. 계약내역</TD><TD>조건부 계약여부</TD><TD>미해당</TD></TR>
<TR><TD></TD><TD>확정 계약금액</TD><TD>12,000,000,000</TD></TR>
<TR><TD></TD><TD>조건부 계약금액</TD><TD>-</TD></TR>
<TR><TD></TD><TD>계약금액 총액(원)</TD><TD>12,000,000,000</TD></TR>
<TR><TD></TD><TD>최근 매출액(원)</TD><TD>100,000,000,000</TD></TR>
<TR><TD></TD><TD>매출액 대비(%)</TD><TD>12.0</TD></TR>
<TR><TD>3. 계약상대방</TD><TD></TD><TD>ABC Corp</TD></TR>
<TR><TD>5. 계약기간</TD><TD>시작일</TD><TD>2024-01-05</TD></TR>
<TR><TD></TD><TD>종료일</TD><TD>2024-12-31</TD></TR>
<TR><TD>12. 공시유보 여부</TD><TD></TD><TD>아니오</TD></TR>
<TR><TD>13. 계약(수주)일자</TD><TD></TD><TD>2024-01-04</TD></TR>
</TABLE></BODY></DOCUMENT>"""

KOSPI_DOC_USD = """<DOCUMENT><TABLE>
<TR><TD>2. 계약내역</TD><TD>계약금액(원)</TD><TD>USD 10,000,000</TD></TR>
<TR><TD></TD><TD>최근매출액(원)</TD><TD>50,000,000,000</TD></TR>
<TR><TD></TD><TD>매출액대비(%)</TD><TD>26.4</TD></TR>
<TR><TD>3. 계약상대</TD><TD></TD><TD>XYZ</TD></TR>
</TABLE></DOCUMENT>"""

MISMATCH_DOC = """<DOCUMENT><TABLE>
<TR><TD>계약금액(원)</TD><TD>1,000,000,000</TD></TR>
<TR><TD>최근매출액(원)</TD><TD>10,000,000,000</TD></TR>
<TR><TD>매출액대비(%)</TD><TD>25.0</TD></TR>
</TABLE></DOCUMENT>"""


DEFERRED_DOC = """<DOCUMENT><TABLE>
<TR><TD>2. 계약내역</TD><TD>조건부 계약여부</TD><TD>미해당</TD></TR>
<TR><TD></TD><TD>확정 계약금액</TD><TD>-</TD></TR>
<TR><TD></TD><TD>계약금액 총액(원)</TD><TD>-</TD></TR>
<TR><TD></TD><TD>최근 매출액(원)</TD><TD>37,266,321,862</TD></TR>
<TR><TD></TD><TD>매출액 대비(%)</TD><TD>-</TD></TR>
<TR><TD>3. 계약상대방</TD><TD></TD><TD>케이티스튜디오지니</TD></TR>
<TR><TD></TD><TD>- 최근 매출액(원)</TD><TD>156,921,580,240</TD></TR>
<TR><TD>9. 공시유보 관련내용</TD><TD>유보기한</TD><TD>2027-09-04</TD></TR>
<TR><TD></TD><TD>유보사유</TD><TD>경영상 비밀 유지</TD></TR>
<TR><TD>10. 기타</TD><TD></TD><TD>- 상기 '최근 매출액'은 2025년도말 연결재무제표 기준입니다.</TD></TR>
</TABLE></DOCUMENT>"""

COUNTERPARTY_ONLY_DOC = """<DOCUMENT><TABLE>
<TR><TD>2. 계약내역</TD><TD>계약금액(원)</TD><TD>1,000,000,000</TD></TR>
<TR><TD>3. 계약상대방</TD><TD></TD><TD>XYZ</TD></TR>
<TR><TD></TD><TD>- 최근 매출액(원)</TD><TD>156,921,580,240</TD></TR>
</TABLE></DOCUMENT>"""


def zip_doc(rcept_no, text, encoding="utf-8"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{rcept_no}.xml", text.encode(encoding))
    return buf.getvalue()


class ExtractTests(unittest.TestCase):
    def test_kosdaq_form(self):
        ex = nz.extract_contract(KOSDAQ_DOC)
        self.assertEqual(ex.contract_amount, 12_000_000_000)
        self.assertEqual(ex.amount_source, "amount_total")
        self.assertEqual(ex.annual_revenue, 100_000_000_000)
        self.assertEqual(ex.ratio_reported, 12.0)
        self.assertTrue(ex.consistent)
        self.assertEqual(ex.counterparty, "ABC Corp")
        self.assertEqual(str(ex.contract_start), "2024-01-05")
        self.assertEqual(str(ex.contract_date), "2024-01-04")
        self.assertEqual(ex.reasons, [])

    def test_foreign_currency_is_not_guessed(self):
        ex = nz.extract_contract(KOSPI_DOC_USD)
        self.assertIsNone(ex.contract_amount)
        self.assertTrue(any(r.startswith("contract_amount_not_parsed") for r in ex.reasons))
        self.assertEqual(ex.counterparty, "XYZ")

    def test_ratio_mismatch_flagged(self):
        ex = nz.extract_contract(MISMATCH_DOC)
        self.assertEqual(ex.contract_amount, 1_000_000_000)
        self.assertFalse(ex.consistent)
        self.assertTrue(any(r.startswith("ratio_mismatch") for r in ex.reasons))

    def test_deferred_disclosure_and_counterparty_revenue(self):
        ex = nz.extract_contract(DEFERRED_DOC)
        self.assertIsNone(ex.contract_amount)
        self.assertEqual(ex.annual_revenue, 37_266_321_862)  # company's, not counterparty's
        self.assertTrue(ex.reasons[0].startswith("deferred_disclosure"), ex.reasons)
        self.assertEqual(ex.revenue_basis_hint, "연결")
        partial = DEFERRED_DOC.replace("<TD>-</TD></TR>\n<TR><TD></TD><TD>계약금액 총액(원)</TD><TD>-</TD>",
                                       "<TD>-</TD></TR>\n<TR><TD></TD><TD>계약금액 총액(원)</TD><TD>3,726,632,186</TD>")
        partial = partial.replace("<TD>매출액 대비(%)</TD><TD>-</TD>", "<TD>매출액 대비(%)</TD><TD>10.0</TD>")
        ex = nz.extract_contract(partial)
        self.assertEqual(ex.reasons, [])
        self.assertTrue(ex.flags and ex.flags[0].startswith("partial_deferral"), ex.flags)
        ex = nz.extract_contract(COUNTERPARTY_ONLY_DOC)
        self.assertIsNone(ex.annual_revenue)  # sub-item revenue must never be used
        self.assertIn("annual_revenue_not_parsed", ex.reasons)

    def test_units_and_dates(self):
        self.assertEqual(nz.parse_krw("1,500 백만원"), 1_500_000_000)
        self.assertIsNone(nz.parse_krw("-"))
        self.assertEqual(str(nz.parse_ymd("2024년 03월 05일")), "2024-03-05")
        self.assertIsNone(nz.parse_ymd("미정"))

    def test_cp949_document_decodes(self):
        raw = KOSDAQ_DOC.replace('encoding="utf-8"', 'encoding="euc-kr"').encode("cp949")
        self.assertIn("계약금액 총액", od.decode_xml(raw))


class NormalizeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = od.RawStore(self.root / "raw")
        rows = [
            {"corp_code": "C1", "corp_name": "알파", "stock_code": "000010", "report_nm": "단일판매ㆍ공급계약체결",
             "rcept_no": "20240102000001", "rcept_dt": "20240102", "rm": "코정"},
            {"corp_code": "C1", "corp_name": "알파", "stock_code": "000010", "report_nm": "[기재정정]단일판매ㆍ공급계약체결",
             "rcept_no": "20240110000002", "rcept_dt": "20240110", "rm": "코"},
            {"corp_code": "C2", "corp_name": "베타", "stock_code": "000020", "report_nm": "단일판매ㆍ공급계약체결",
             "rcept_no": "20240103000003", "rcept_dt": "20240103", "rm": "유"},
            {"corp_code": "C3", "corp_name": "감마", "stock_code": "000030", "report_nm": "단일판매ㆍ공급계약해지",
             "rcept_no": "20240104000004", "rcept_dt": "20240104", "rm": "유"},
            {"corp_code": "C4", "corp_name": "델타", "stock_code": "000040", "report_nm": "단일판매ㆍ공급계약체결",
             "rcept_no": "20240105000005", "rcept_dt": "20240105", "rm": "코"},
            {"corp_code": "C5", "corp_name": "기타", "stock_code": "000050", "report_nm": "주요사항보고서(유상증자결정)",
             "rcept_no": "20240105000006", "rcept_dt": "20240105", "rm": "코"},
        ]
        page = json.dumps({"status": "000", "list": rows}).encode()
        self.store.save("list", "list_x_p0001.json", page, {"endpoint": "list.json", "params": {}})
        self.store.save("document", "20240102000001.zip", zip_doc("20240102000001", KOSDAQ_DOC), {})
        self.store.save("document", "20240103000003.zip", zip_doc("20240103000003", KOSPI_DOC_USD), {})
        # 20240105000005: no document fetched yet

    def tearDown(self):
        self.tmp.cleanup()

    def read(self, name):
        with (self.root / "norm" / name).open(encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f))

    def test_outputs(self):
        summary = nz.normalize(self.store, self.root / "norm")
        self.assertEqual(summary["counts"], {"filings": 5, "events_auto": 1, "review": 2, "related": 2,
                                             "missing_document": 1})
        self.assertEqual(summary["breakdown"]["by_year_status"]["2024"],
                         {"auto_ok_pending_risk_review": 1, "needs_review": 1, "needs_document": 1})
        self.assertEqual(summary["breakdown"]["related_linked"], 1)
        self.assertIn("contract_amount_not_parsed", summary["breakdown"]["review_reasons"])
        events = self.read("opendart_events.csv")
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertEqual(e["event_id"], "20240102000001")
        self.assertEqual(e["symbol"], "000010")
        self.assertEqual(e["contract_amount"], "12000000000")
        self.assertEqual(e["revenue_available_date"], "2024-01-02")
        self.assertEqual(e["risk_approved"], "false")
        report = validate_events(self.root / "norm" / "opendart_events.csv")
        self.assertTrue(report.ok, report.issues)
        self.assertEqual(report.stats["signal_eligible_rows"], 0)  # risk not approved yet

        related = {r["rcept_no"]: r for r in self.read("opendart_related.csv")}
        self.assertEqual(related["20240110000002"]["kind"], "amendment")
        self.assertEqual(related["20240110000002"]["linked_original_rcept_no"], "20240102000001")
        self.assertEqual(related["20240104000004"]["kind"], "cancellation")
        self.assertEqual(related["20240104000004"]["link_method"], "unlinked_manual")

        review = {r["rcept_no"]: r for r in self.read("opendart_review_queue.csv")}
        self.assertEqual(review["20240102000001"]["status"], "auto_ok_pending_risk_review")
        self.assertEqual(review["20240102000001"]["lookahead_flags"], "amended_later")
        self.assertEqual(review["20240103000003"]["status"], "needs_review")
        self.assertIn("contract_amount_not_parsed", review["20240103000003"]["reasons"])
        self.assertEqual(review["20240105000005"]["status"], "needs_document")


if __name__ == "__main__":
    unittest.main()
