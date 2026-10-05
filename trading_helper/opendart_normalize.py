"""Normalize raw OpenDART 단일판매ㆍ공급계약 filings into DATA_CONTRACT rows.

Rules:
- Only filings whose figures parse cleanly and cross-check become events.csv rows, and even
  those carry risk_approved=false until a human reviews financial risk with sources.
- Everything else goes to a review queue with explicit reasons. Nothing is filled with 0.
- Amendments and cancellations are never new events; they are linked to the most recent prior
  supply-contract filing of the same company and written to a related-filings file.
- rm flags 정/철 are look-ahead information; they are recorded but must not feed signals.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field, asdict
from datetime import date
import html
import json
import re
import zipfile
from pathlib import Path

from .opendart import RawStore, classify_report_name, dart_viewer_url, decode_xml
from .validate import EVENT_COLUMNS

RELATED_COLUMNS = ["rcept_no", "symbol", "corp_name", "receipt_date", "kind", "report_nm",
                   "linked_original_rcept_no", "link_method", "amendment_tag", "lookahead_flags",
                   "source_url", "fetched_at"]
REVIEW_COLUMNS = ["rcept_no", "symbol", "corp_name", "receipt_date", "report_nm", "kind", "status",
                  "reasons", "contract_amount", "annual_revenue", "ratio_pct_reported",
                  "ratio_pct_computed", "counterparty", "contract_start", "contract_end",
                  "contract_date", "deferred_disclosure", "conditional", "subsidiary", "voluntary",
                  "lookahead_flags", "flags", "revenue_basis", "revenue_basis_hint", "source_url", "document_path", "fetched_at"]

# ----------------------------------------------------------------------------- document parsing

TAG = re.compile(r"<[^>]+>")
ROW = re.compile(r"<TR\b[^>]*>(.*?)</TR>", re.I | re.S)
CELL = re.compile(r"<(?:TD|TH|TE|TU)\b[^>]*>(.*?)</(?:TD|TH|TE|TU)>", re.I | re.S)
WS = re.compile(r"\s+")


def _clean(fragment: str) -> str:
    text = TAG.sub(" ", fragment)
    text = html.unescape(text).replace("\xa0", " ")
    return WS.sub(" ", text).strip()


def table_rows(xml_text: str) -> list[list[str]]:
    rows = []
    for m in ROW.finditer(xml_text):
        cells = [_clean(c) for c in CELL.findall(m.group(1))]
        if any(cells):
            rows.append(cells)
    return rows


def norm_label(text: str) -> str:
    t = re.sub(r"\((?:단위\s*:\s*)?(?:원|%|백만원|천원)\)", "", text)
    t = re.sub(r"[\s:：\-–]", "", t)
    t = re.sub(r"^\d+\.\s*", "", t)  # leading "2." numbering
    return t


# Priority-ordered label patterns per field (applied to normalized labels).
FIELD_PATTERNS: dict[str, list[re.Pattern]] = {
    "amount_total": [re.compile(r"^계약금액총액")],
    "amount_confirmed": [re.compile(r"^확정계약금액")],
    "amount_plain": [re.compile(r"^계약금액(?!총액)(?!\s)")],
    "amount_conditional": [re.compile(r"^조건부계약금액")],
    "revenue": [re.compile(r"^최근매출액"), re.compile(r"최근사업연도매출액")],
    "ratio": [re.compile(r"^매출액대비")],
    "counterparty": [re.compile(r"^계약상대방?$"), re.compile(r"^계약상대방?")],
    "start": [re.compile(r"^시작일"), re.compile(r"계약기간시작")],
    "end": [re.compile(r"^종료일"), re.compile(r"계약기간종료")],
    "contract_date": [re.compile(r"^계약\(수주\)일자"), re.compile(r"^계약일자"), re.compile(r"^계약일$")],
    "deferred": [re.compile(r"공시유보여부"), re.compile(r"^유보여부")],
    "deferred_until": [re.compile(r"^유보기한")],
    "deferred_reason": [re.compile(r"^유보사유")],
    "conditional": [re.compile(r"^조건부계약여부")],
    "amend_reason": [re.compile(r"^정정사유")],
}
UNIT_LABEL = re.compile(r"^(원|백만원|천원|%|\$|USD)$")
SUB_ITEM = re.compile(r"^\s*[-–]\s*")  # "- 최근 매출액(원)" under 계약상대방 is the COUNTERPARTY's revenue
PRIMARY_ONLY = {"revenue", "ratio", "amount_total", "amount_confirmed", "amount_plain", "amount_conditional"}
BASIS_HINT = re.compile(r"최근\s*매출액[^。\n]{0,40}?(연결|별도|개별)")


def extract_fields(rows: list[list[str]]) -> dict[str, str | None]:
    found: dict[str, str | None] = {k: None for k in FIELD_PATTERNS}
    for cells in rows:
        for i, cell in enumerate(cells):
            label = norm_label(cell)
            if not label:
                continue
            sub_item = bool(SUB_ITEM.match(cell))
            for fld, patterns in FIELD_PATTERNS.items():
                if found[fld] is not None or not any(p.search(label) for p in patterns):
                    continue
                if sub_item and fld in PRIMARY_ONLY:
                    continue
                value = next((c for c in cells[i + 1:] if c and not UNIT_LABEL.match(c)), None)
                if value is not None and norm_label(value) != label:
                    found[fld] = value
    return found


NUM = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def parse_krw(text: str | None) -> float | None:
    """Returns a KRW amount or None. Rejects non-KRW currencies explicitly instead of guessing."""
    if text is None:
        return None
    t = text.strip()
    if t in ("", "-", "–", "해당없음", "해당사항없음"):
        return None
    if re.search(r"USD|\$|EUR|€|JPY|¥|CNY|달러|유로|엔\b", t):
        return None
    m = NUM.search(t.replace(" ", ""))
    if not m:
        return None
    value = float(m.group(0).replace(",", ""))
    if "백만원" in t:
        value *= 1_000_000
    elif "천원" in t:
        value *= 1_000
    return value if value > 0 else None


def parse_pct(text: str | None) -> float | None:
    if text is None:
        return None
    m = NUM.search(text.replace(" ", "").replace(",", ""))
    return float(m.group(0)) if m else None


def parse_ymd(text: str | None) -> date | None:
    if not text:
        return None
    m = re.search(r"(\d{4})[.\-/년\s]*(\d{1,2})[.\-/월\s]*(\d{1,2})", text)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


@dataclass
class Extraction:
    contract_amount: float | None
    amount_source: str | None
    annual_revenue: float | None
    ratio_reported: float | None
    ratio_computed: float | None
    counterparty: str | None
    contract_start: date | None
    contract_end: date | None
    contract_date: date | None
    deferred: str | None
    conditional: str | None
    amend_reason: str | None
    revenue_basis_hint: str | None = None
    reasons: list[str] = field(default_factory=list)  # block automatic acceptance
    flags: list[str] = field(default_factory=list)    # informational; do not block

    @property
    def consistent(self) -> bool:
        if self.ratio_reported is None or self.ratio_computed is None:
            return False
        tol = max(0.5, 0.02 * abs(self.ratio_reported))
        return abs(self.ratio_reported - self.ratio_computed) <= tol


def extract_contract(xml_text: str) -> Extraction:
    f = extract_fields(table_rows(xml_text))
    amount, source = None, None
    for key in ("amount_total", "amount_confirmed", "amount_plain"):
        amount = parse_krw(f[key])
        if amount is not None:
            source = key
            break
    revenue = parse_krw(f["revenue"])
    ratio = parse_pct(f["ratio"])
    computed = amount / revenue * 100 if amount and revenue else None
    deferred = f["deferred"]
    if deferred is None and any(_present(f[k]) for k in ("deferred_until", "deferred_reason")):
        deferred = f"유보기한={f['deferred_until'] or ''} 유보사유={f['deferred_reason'] or ''}"
    basis = BASIS_HINT.search(WS.sub(" ", _clean(xml_text)))
    ex = Extraction(amount, source, revenue, ratio, computed, f["counterparty"],
                    parse_ymd(f["start"]), parse_ymd(f["end"]), parse_ymd(f["contract_date"]),
                    deferred, f["conditional"], f["amend_reason"], basis.group(1) if basis else None)
    if amount is None:
        ex.reasons.append("contract_amount_not_parsed" + (f": {f['amount_plain']!r}" if f["amount_plain"] else ""))
    if revenue is None:
        ex.reasons.append("annual_revenue_not_parsed" + (f": {f['revenue']!r}" if f["revenue"] else ""))
    if amount is not None and revenue is not None:
        if ratio is None:
            ex.reasons.append("ratio_not_reported")
        elif not ex.consistent:
            ex.reasons.append(f"ratio_mismatch reported={ratio} computed={computed:.2f}")
    if source == "amount_plain" and parse_krw(f["amount_conditional"]):
        ex.reasons.append("conditional_amount_present_check_total")
    if ex.deferred and re.search(r"예|유보|Y", ex.deferred) and not re.fullmatch(r".*(아니오|미해당|N)\s*$", ex.deferred):
        # Full deferral hides the amount: cannot be sized, needs review. Partial deferral (e.g. the
        # counterparty withheld) leaves the amount/revenue/ratio cross-check intact: flag only.
        if amount is None or revenue is None:
            ex.reasons.insert(0, f"deferred_disclosure={ex.deferred}")
        else:
            ex.flags.append(f"partial_deferral={ex.deferred}")
    return ex


def _present(text: str | None) -> bool:
    return bool(text) and text.strip() not in ("-", "–", "해당없음", "미해당", "해당사항없음")


# ----------------------------------------------------------------------------- normalization

def _manifest_index(store: RawStore) -> tuple[dict[str, dict], dict[str, dict]]:
    """Returns (document records by rcept_no, list page records by path)."""
    docs, lists = {}, {}
    for rec in store.records():
        if rec["kind"] == "document":
            docs[Path(rec["path"]).stem] = rec
        elif rec["kind"] == "list":
            lists[rec["path"]] = rec
    return docs, lists


def load_filings(store: RawStore) -> list[dict]:
    """All unique list rows from saved pages, with fetched_at of the page they came from."""
    _, lists = _manifest_index(store)
    seen: dict[str, dict] = {}
    for rel, rec in lists.items():
        path = store.root / rel
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        for row in data.get("list", []):
            row = dict(row, _list_fetched_at=rec["fetched_at"])
            seen.setdefault(row["rcept_no"], row)
    return sorted(seen.values(), key=lambda r: (r["rcept_dt"], r["rcept_no"]))


def read_document_text(store: RawStore, rcept_no: str) -> str | None:
    path = store.root / "document" / f"{rcept_no}.zip"
    if not path.exists():
        return None
    with zipfile.ZipFile(path) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".xml")]
        if not names:
            return None
        # Main document is normally named <rcept_no>.xml; otherwise take the largest XML.
        main = next((n for n in names if Path(n).stem == rcept_no), None)
        if main is None:
            main = max(names, key=lambda n: zf.getinfo(n).file_size)
        return decode_xml(zf.read(main))


def _iso(d: str) -> str:
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}"


def normalize(store: RawStore, out_dir: Path) -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    docs, _ = _manifest_index(store)
    filings = load_filings(store)
    events, related, review = [], [], []
    last_original: dict[str, str] = {}  # corp_code -> most recent new_contract rcept_no
    counts = {"filings": 0, "events_auto": 0, "review": 0, "related": 0, "missing_document": 0}
    for row in filings:
        row["report_nm"] = re.sub(r"\s+", " ", row.get("report_nm", "")).strip()
        cls = classify_report_name(row["report_nm"], row.get("rm", ""))
        if not cls.is_supply_contract:
            continue
        counts["filings"] += 1
        rcept_no, corp = row["rcept_no"], row.get("corp_code", "")
        symbol = (row.get("stock_code") or "").strip()
        receipt = _iso(row["rcept_dt"])
        url = dart_viewer_url(rcept_no)
        fetched_at = docs.get(rcept_no, {}).get("fetched_at") or row["_list_fetched_at"]
        flags = ",".join(cls.lookahead_flags)
        if cls.kind != "new_contract":
            original = last_original.get(corp)
            related.append({"rcept_no": rcept_no, "symbol": symbol, "corp_name": row.get("corp_name", ""),
                            "receipt_date": receipt, "kind": cls.kind, "report_nm": row.get("report_nm", ""),
                            "linked_original_rcept_no": original or "",
                            "link_method": "latest_prior_same_corp" if original else "unlinked_manual",
                            "amendment_tag": cls.amendment_tag or "", "lookahead_flags": flags,
                            "source_url": url, "fetched_at": fetched_at})
            counts["related"] += 1
            continue
        last_original[corp] = rcept_no
        text = read_document_text(store, rcept_no)
        base = {"rcept_no": rcept_no, "symbol": symbol, "corp_name": row.get("corp_name", ""),
                "receipt_date": receipt, "report_nm": row.get("report_nm", ""), "kind": cls.kind,
                "subsidiary": cls.subsidiary, "voluntary": cls.voluntary, "lookahead_flags": flags,
                "revenue_basis": "filing_self_reported_most_recent_annual", "source_url": url,
                "document_path": f"document/{rcept_no}.zip" if text is not None else "",
                "fetched_at": fetched_at}
        reasons = []
        if not symbol:
            reasons.append("no_stock_code_unlisted")
        if cls.subsidiary:
            reasons.append("subsidiary_filing_revenue_basis_review")
        if text is None:
            counts["missing_document"] += 1
            review.append({**base, "status": "needs_document", "reasons": ";".join(reasons + ["document_not_fetched"])})
            counts["review"] += 1
            continue
        ex = extract_contract(text)
        reasons += ex.reasons
        record = {**base, "contract_amount": _fmt(ex.contract_amount), "annual_revenue": _fmt(ex.annual_revenue),
                  "ratio_pct_reported": ex.ratio_reported, "ratio_pct_computed": _fmt(ex.ratio_computed, 2),
                  "counterparty": ex.counterparty or "", "contract_start": ex.contract_start or "",
                  "contract_end": ex.contract_end or "", "contract_date": ex.contract_date or "",
                  "deferred_disclosure": ex.deferred or "", "conditional": ex.conditional or "",
                  "revenue_basis_hint": ex.revenue_basis_hint or "", "flags": ";".join(ex.flags)}
        auto_ok = (not reasons and ex.contract_amount and ex.annual_revenue and ex.consistent)
        if auto_ok:
            record["status"] = "auto_ok_pending_risk_review"
            events.append({"event_id": rcept_no, "symbol": symbol, "receipt_date": receipt,
                           "event_type": "new_contract", "contract_amount": _fmt(ex.contract_amount),
                           "annual_revenue": _fmt(ex.annual_revenue), "revenue_available_date": receipt,
                           "risk_approved": "false", "risk_available_date": "", "source_url": url,
                           "fetched_at": fetched_at})
            counts["events_auto"] += 1
        else:
            record["status"] = "needs_review"
            counts["review"] += 1
        record["reasons"] = ";".join(reasons)
        review.append(record)
    _write_csv(out_dir / "opendart_events.csv", EVENT_COLUMNS, events)
    _write_csv(out_dir / "opendart_related.csv", RELATED_COLUMNS, related)
    _write_csv(out_dir / "opendart_review_queue.csv", REVIEW_COLUMNS, review)
    summary = {"counts": counts, "breakdown": breakdown(review, related),
               "outputs": [str(out_dir / n) for n in
                           ("opendart_events.csv", "opendart_related.csv", "opendart_review_queue.csv")],
               "note": "events rows have risk_approved=false; signals require manual risk review with sources"}
    (out_dir / "opendart_normalize_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def breakdown(review: list[dict], related: list[dict]) -> dict:
    """Reproducible stats for reports: by year/status, by reason, basis hints, related kinds."""
    by_year: dict[str, dict[str, int]] = {}
    reasons: dict[str, int] = {}
    basis: dict[str, int] = {}
    for r in review:
        y = r["receipt_date"][:4]
        by_year.setdefault(y, {})
        by_year[y][r["status"]] = by_year[y].get(r["status"], 0) + 1
        for item in (r.get("reasons") or "").split(";"):
            if item:
                key = item.split("=")[0].split(":")[0].split(" ")[0]
                reasons[key] = reasons.get(key, 0) + 1
        if r.get("document_path"):
            hint = r.get("revenue_basis_hint") or "none"
            basis[hint] = basis.get(hint, 0) + 1
    related_kinds: dict[str, int] = {}
    for r in related:
        related_kinds[r["kind"]] = related_kinds.get(r["kind"], 0) + 1
    linked = sum(1 for r in related if r["linked_original_rcept_no"])
    return {"by_year_status": dict(sorted(by_year.items())),
            "review_reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
            "revenue_basis_hint_with_document": basis,
            "related_kinds": related_kinds, "related_linked": linked, "related_unlinked": len(related) - linked}


def _fmt(value, digits: int = 0):
    if value is None:
        return ""
    return f"{value:.{digits}f}" if digits else f"{int(round(value))}"


def _write_csv(path: Path, columns: list[str], rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in columns})
