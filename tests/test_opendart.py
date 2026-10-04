import io
import json
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path

from trading_helper import opendart as od


def zip_bytes(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


class FakeFetcher:
    """Scripted responses keyed by endpoint; records masked-safe call log."""

    def __init__(self, responses):
        self.responses = responses  # endpoint -> list of (status, bytes) or callable(params)
        self.calls = []

    def __call__(self, url, params):
        endpoint = url.rsplit("/", 1)[1]
        self.calls.append((endpoint, dict(params)))
        r = self.responses[endpoint]
        if callable(r):
            return r(params)
        return r.pop(0)


def page(page_no, total_page, rows):
    return 200, json.dumps({"status": "000", "message": "정상", "page_no": page_no,
                            "total_page": total_page, "list": rows}).encode()


class OpenDartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = od.RawStore(Path(self.tmp.name))
        self.key = "SECRETKEY123"

    def tearDown(self):
        self.tmp.cleanup()

    def client(self, fetcher):
        return od.Client(self.key, self.store, fetcher=fetcher, min_interval_s=0, sleep=lambda s: None)

    def test_api_key_from_env_only(self):
        with self.assertRaises(od.OpenDartError):
            od.get_api_key({})
        self.assertEqual(od.get_api_key({od.ENV_KEY: " k "}), "k")

    def test_pagination_and_manifest_masks_key(self):
        rows1 = [{"rcept_no": "20240102000001", "report_nm": "단일판매ㆍ공급계약체결", "rcept_dt": "20240102"}]
        rows2 = [{"rcept_no": "20240103000002", "report_nm": "[정정]단일판매ㆍ공급계약체결", "rcept_dt": "20240103"}]
        f = FakeFetcher({"list.json": [page(1, 2, rows1), page(2, 2, rows2)]})
        rows = od.search_filings(self.client(f), date(2024, 1, 1), date(2024, 1, 31), pblntf_ty="I")
        self.assertEqual([r["rcept_no"] for r in rows], ["20240102000001", "20240103000002"])
        self.assertEqual(f.calls[0][1]["last_reprt_at"], "N")
        self.assertEqual(f.calls[0][1]["crtfc_key"], self.key)
        records = self.store.records()
        self.assertEqual(len(records), 2)
        manifest_text = self.store.manifest_path.read_text(encoding="utf-8")
        self.assertNotIn(self.key, manifest_text)
        self.assertEqual(records[0]["params"]["crtfc_key"], "***")
        self.assertEqual(len(records[0]["sha256"]), 64)
        saved = Path(self.tmp.name) / records[0]["path"]
        self.assertTrue(saved.exists())

    def test_completed_window_is_served_from_disk(self):
        rows1 = [{"rcept_no": "20240102000001", "report_nm": "x", "rcept_dt": "20240102"}]
        f = FakeFetcher({"list.json": [page(1, 2, rows1), page(2, 2, [])]})
        od.search_filings(self.client(f), date(2024, 1, 1), date(2024, 1, 31), pblntf_ty="I")
        self.assertEqual(len(f.calls), 2)
        f2 = FakeFetcher({"list.json": []})  # any request would raise IndexError
        rows = od.search_filings(self.client(f2), date(2024, 1, 1), date(2024, 1, 31), pblntf_ty="I")
        self.assertEqual([r["rcept_no"] for r in rows], ["20240102000001"])
        self.assertEqual(f2.calls, [])
        # A partially downloaded window is re-fetched from page 1.
        (self.store.root / "list" / "list_20240101_20240131_I_all_all_p0002.json").unlink()
        f3 = FakeFetcher({"list.json": [page(1, 2, rows1), page(2, 2, [])]})
        od.search_filings(self.client(f3), date(2024, 1, 1), date(2024, 1, 31), pblntf_ty="I")
        self.assertEqual(len(f3.calls), 2)

    def test_no_data_status_returns_empty(self):
        body = json.dumps({"status": "013", "message": "조회된 데이타가 없습니다."}).encode()
        f = FakeFetcher({"list.json": [(200, body)]})
        self.assertEqual(od.search_filings(self.client(f), date(2024, 1, 1), date(2024, 1, 2)), [])

    def test_error_status_raises_without_key_leak(self):
        body = json.dumps({"status": "010", "message": "등록되지 않은 키입니다."}).encode()
        f = FakeFetcher({"list.json": [(200, body)]})
        with self.assertRaises(od.OpenDartError) as ctx:
            od.search_filings(self.client(f), date(2024, 1, 1), date(2024, 1, 2))
        self.assertNotIn(self.key, str(ctx.exception))

    def test_retry_on_5xx_then_success(self):
        f = FakeFetcher({"list.json": [(503, b"busy"), page(1, 1, [])]})
        rows = od.search_filings(self.client(f), date(2024, 1, 1), date(2024, 1, 2))
        self.assertEqual(rows, [])
        self.assertEqual(len(f.calls), 2)

    def test_document_zip_and_rcept_no_validation(self):
        z = zip_bytes({"20240102000001.xml": "<DOCUMENT>본문</DOCUMENT>".encode("utf-8")})
        f = FakeFetcher({"document.xml": [(200, z)]})
        members = od.fetch_document(self.client(f), "20240102000001")
        self.assertIn("20240102000001.xml", members)
        with self.assertRaises(ValueError):
            od.fetch_document(self.client(f), "123")
        f2 = FakeFetcher({"document.xml": [(200, b'{"status":"013"}')]})
        with self.assertRaises(od.OpenDartError):
            od.fetch_document(self.client(f2), "20240102000001")

    def test_corp_codes_parse(self):
        xml = ("<result><list><corp_code>00126380</corp_code><corp_name>삼성전자</corp_name>"
               "<stock_code>005930</stock_code><modify_date>20240101</modify_date></list>"
               "<list><corp_code>00000001</corp_code><corp_name>비상장</corp_name>"
               "<stock_code> </stock_code><modify_date>20240101</modify_date></list></result>")
        rows = od.parse_corp_codes(zip_bytes({"CORPCODE.xml": xml.encode("utf-8")}))
        self.assertEqual(rows[0]["stock_code"], "005930")
        self.assertEqual(rows[1]["stock_code"], "")

    def test_classify_report_name(self):
        c = od.classify_report_name("단일판매ㆍ공급계약체결")
        self.assertEqual((c.is_supply_contract, c.kind), (True, "new_contract"))
        c = od.classify_report_name("[기재정정]단일판매ㆍ공급계약체결", rm="정")
        self.assertEqual((c.kind, c.amendment_tag), ("amendment", "기재정정"))
        self.assertIn("rm=정", c.note)
        c = od.classify_report_name("단일판매ㆍ공급계약해지")
        self.assertEqual(c.kind, "cancellation")
        c = od.classify_report_name("[정정]단일판매ㆍ공급계약해지")
        self.assertEqual(c.kind, "cancellation")
        c = od.classify_report_name("주요사항보고서(유상증자결정)")
        self.assertEqual((c.is_supply_contract, c.kind), (False, "other"))
        # Real-data false positives: liquidity-provider contracts, free-text titles, halt notices, padding.
        self.assertFalse(od.classify_report_name("유동성공급계약의체결").is_supply_contract)
        self.assertFalse(od.classify_report_name("투자판단관련주요경영사항      (라이선스 및 공급계약 체결)").is_supply_contract)
        c = od.classify_report_name("주권매매거래정지              (단일판매공급계약)")
        self.assertEqual((c.is_supply_contract, c.kind), (False, "halt_notice"))
        c = od.classify_report_name("단일판매ㆍ공급계약체결(자율공시)              ")
        self.assertEqual((c.kind, c.voluntary), ("new_contract", True))


if __name__ == "__main__":
    unittest.main()
