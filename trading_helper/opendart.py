"""OpenDART raw collector. Stdlib only. Never prints or stores the API key.

Scope: fetch disclosure lists, original documents and corp codes; preserve raw bytes
with a manifest (sha256, fetched_at, masked request). No interpretation here beyond
coarse report-name classification; normalization lives in opendart_normalize.py.

Endpoint parameter semantics are documented in docs/DATA_SOURCES.md with official URLs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone, timedelta
import hashlib
import io
import json
import os
import re
import time
from pathlib import Path
from typing import Callable
import urllib.error
import urllib.parse
import urllib.request
import zipfile

API_BASE = "https://opendart.fss.or.kr/api"
ENV_KEY = "OPENDART_API_KEY"
KST = timezone(timedelta(hours=9))
SCHEMA_VERSION = "1"
USER_AGENT = "trading_helper/0.0.1 (research; stdlib urllib)"

# OpenDART status codes (official guide). "013" means no matching data, not an error.
STATUS_OK = "000"
STATUS_NO_DATA = "013"


class OpenDartError(Exception):
    pass


def get_api_key(env: dict | None = None) -> str:
    env = os.environ if env is None else env
    key = env.get(ENV_KEY, "").strip()
    if not key:
        raise OpenDartError(f"{ENV_KEY} is not set. Export it in your shell; it is never printed or stored.")
    return key


def mask(text: str, key: str) -> str:
    return text.replace(key, "***") if key else text


def now_iso() -> str:
    return datetime.now(tz=KST).isoformat(timespec="seconds")


def yyyymmdd(d: date) -> str:
    return d.strftime("%Y%m%d")


# --------------------------------------------------------------------------- storage

@dataclass
class RawStore:
    """Writes raw payloads under root/<kind>/ and appends to root/manifest.jsonl."""
    root: Path

    def __post_init__(self):
        self.root = Path(self.root)
        self.root.mkdir(parents=True, exist_ok=True)

    @property
    def manifest_path(self) -> Path:
        return self.root / "manifest.jsonl"

    def save(self, kind: str, name: str, payload: bytes, meta: dict) -> Path:
        folder = self.root / kind
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_bytes(payload)
        record = {"schema_version": SCHEMA_VERSION, "kind": kind, "path": str(path.relative_to(self.root)),
                  "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
                  "fetched_at": now_iso(), **meta}
        with self.manifest_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return path

    def records(self) -> list[dict]:
        if not self.manifest_path.exists():
            return []
        with self.manifest_path.open("r", encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]


# --------------------------------------------------------------------------- http

Fetcher = Callable[[str, dict], tuple[int, bytes]]


def urllib_fetcher(url: str, params: dict, *, timeout: float = 30.0) -> tuple[int, bytes]:
    full = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(full, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


@dataclass
class Client:
    key: str
    store: RawStore
    fetcher: Fetcher = urllib_fetcher
    min_interval_s: float = 0.25
    retries: int = 3
    sleep: Callable[[float], None] = time.sleep
    _last_call: float = field(default=0.0, repr=False)

    def _request(self, endpoint: str, params: dict) -> tuple[int, bytes, dict]:
        url = f"{API_BASE}/{endpoint}"
        full_params = {"crtfc_key": self.key, **params}
        masked = {k: ("***" if k == "crtfc_key" else v) for k, v in full_params.items()}
        last_exc: Exception | None = None
        for attempt in range(1, self.retries + 1):
            wait = self.min_interval_s - (time.monotonic() - self._last_call)
            if wait > 0:
                self.sleep(wait)
            try:
                status, body = self.fetcher(url, full_params)
                self._last_call = time.monotonic()
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_exc = OpenDartError(mask(f"network error on {endpoint}: {exc}", self.key))
                self.sleep(min(2 ** attempt, 10))
                continue
            if status >= 500:
                last_exc = OpenDartError(f"HTTP {status} from {endpoint}")
                self.sleep(min(2 ** attempt, 10))
                continue
            return status, body, {"endpoint": endpoint, "params": masked, "http_status": status}
        raise last_exc or OpenDartError(f"request failed: {endpoint}")

    def get_json(self, endpoint: str, params: dict, *, save_as: str | None = None) -> dict:
        status, body, meta = self._request(endpoint, params)
        if save_as:
            self.store.save(endpoint.split(".")[0], save_as, body, meta)
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OpenDartError(mask(f"{endpoint}: non-JSON response (HTTP {status}): {exc}", self.key))
        if data.get("status") not in (STATUS_OK, STATUS_NO_DATA):
            raise OpenDartError(f"{endpoint}: status {data.get('status')} {data.get('message')}")
        return data

    def get_bytes(self, endpoint: str, params: dict, *, save_as: str) -> bytes:
        status, body, meta = self._request(endpoint, params)
        if status != 200:
            raise OpenDartError(mask(f"{endpoint}: HTTP {status}: {body[:200]!r}", self.key))
        # Error responses come back as JSON/XML instead of a zip.
        if not body.startswith(b"PK"):
            raise OpenDartError(mask(f"{endpoint}: expected zip, got {body[:200]!r}", self.key))
        self.store.save(endpoint.split(".")[0], save_as, body, meta)
        return body


# --------------------------------------------------------------------------- endpoints

# Without corp_code the official guide caps a query window at 3 months; stay under it.
MAX_WINDOW_DAYS = 89


def date_windows(bgn_de: date, end_de: date, max_days: int = MAX_WINDOW_DAYS) -> list[tuple[date, date]]:
    if end_de < bgn_de:
        raise ValueError("end_de before bgn_de")
    out, start = [], bgn_de
    while start <= end_de:
        stop = min(start + timedelta(days=max_days - 1), end_de)
        out.append((start, stop))
        start = stop + timedelta(days=1)
    return out


def search_filings(client: Client, bgn_de: date, end_de: date, *, pblntf_ty: str | None = None,
                   pblntf_detail_ty: str | None = None, corp_cls: str | None = None,
                   corp_code: str | None = None, last_reprt_at: str = "N",
                   page_count: int = 100, max_pages: int = 1000, use_cache: bool = True) -> list[dict]:
    """공시검색 list.json, all pages. last_reprt_at='N' keeps originals AND amendments
    as separate rows, which point-in-time reconstruction requires.

    Windows whose pages are all already in the raw store are read from disk (resumable),
    so an interrupted multi-year run never re-spends quota on finished windows."""
    if not corp_code and (end_de - bgn_de).days + 1 > MAX_WINDOW_DAYS:
        rows: list[dict] = []
        for a, b in date_windows(bgn_de, end_de):
            rows.extend(search_filings(client, a, b, pblntf_ty=pblntf_ty, pblntf_detail_ty=pblntf_detail_ty,
                                       corp_cls=corp_cls, corp_code=corp_code, last_reprt_at=last_reprt_at,
                                       page_count=page_count, max_pages=max_pages, use_cache=use_cache))
        return rows
    if end_de < bgn_de:
        raise ValueError("end_de before bgn_de")
    base = {"bgn_de": yyyymmdd(bgn_de), "end_de": yyyymmdd(end_de), "last_reprt_at": last_reprt_at,
            "page_count": page_count, "sort": "date", "sort_mth": "asc"}
    for k, v in (("pblntf_ty", pblntf_ty), ("pblntf_detail_ty", pblntf_detail_ty),
                 ("corp_cls", corp_cls), ("corp_code", corp_code)):
        if v:
            base[k] = v
    rows: list[dict] = []
    page = 1
    tag = f"{base['bgn_de']}_{base['end_de']}_{pblntf_ty or 'all'}_{pblntf_detail_ty or 'all'}_{corp_cls or 'all'}"
    if use_cache:
        cached = cached_list_pages(client.store, tag)
        if cached is not None:
            return cached
    while page <= max_pages:
        data = client.get_json("list.json", {**base, "page_no": page}, save_as=f"list_{tag}_p{page:04d}.json")
        if data.get("status") == STATUS_NO_DATA:
            break
        rows.extend(data.get("list", []))
        total_page = int(data.get("total_page", 1) or 1)
        if page >= total_page:
            break
        page += 1
    return rows


def cached_list_pages(store: RawStore, tag: str) -> list[dict] | None:
    """Rows of a fully downloaded window from disk, or None if any page is missing."""
    folder = store.root / "list"
    first = folder / f"list_{tag}_p0001.json"
    if not first.exists():
        return None
    try:
        data = json.loads(first.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if data.get("status") == STATUS_NO_DATA:
        return []
    if data.get("status") != STATUS_OK:
        return None
    total_page = int(data.get("total_page", 1) or 1)
    rows: list[dict] = list(data.get("list", []))
    for page in range(2, total_page + 1):
        path = folder / f"list_{tag}_p{page:04d}.json"
        if not path.exists():
            return None
        try:
            rows.extend(json.loads(path.read_text(encoding="utf-8")).get("list", []))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
    return rows


# 단일판매ㆍ공급계약 has no pblntf_detail_ty of its own: it is a KRX 수시공시 (I / I001),
# so we pull I001 and match the report name. Separator is U+318D "ㆍ".
SUPPLY_CONTRACT_PBLNTF_TY = "I"
SUPPLY_CONTRACT_DETAIL_TY = "I001"


def search_supply_contract_filings(client: Client, bgn_de: date, end_de: date, *,
                                   corp_cls: str | None = None) -> list[dict]:
    """All 단일판매ㆍ공급계약 체결/해지 filings (originals and amendments) in the window."""
    rows = search_filings(client, bgn_de, end_de, pblntf_ty=SUPPLY_CONTRACT_PBLNTF_TY,
                          pblntf_detail_ty=SUPPLY_CONTRACT_DETAIL_TY, corp_cls=corp_cls, last_reprt_at="N")
    return [r for r in rows if classify_report_name(r.get("report_nm", "")).is_supply_contract]


def decode_xml(raw: bytes) -> str:
    """Encoding inside the document zip is not documented; honor the XML declaration, else try UTF-8/CP949."""
    head = raw[:200].decode("ascii", errors="ignore")
    m = re.search(r'encoding=["\']([\w.-]+)["\']', head)
    candidates = ([m.group(1)] if m else []) + ["utf-8", "cp949", "euc-kr"]
    for enc in candidates:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def fetch_document(client: Client, rcept_no: str) -> dict[str, bytes]:
    """공시서류원본파일 document.xml: zip of the filing's XML. Returns {member: bytes}."""
    if not re.fullmatch(r"\d{14}", rcept_no):
        raise ValueError(f"rcept_no must be 14 digits: {rcept_no!r}")
    body = client.get_bytes("document.xml", {"rcept_no": rcept_no}, save_as=f"{rcept_no}.zip")
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        return {name: zf.read(name) for name in zf.namelist()}


def fetch_corp_codes(client: Client) -> list[dict]:
    """고유번호 corpCode.xml: zip containing CORPCODE.xml. Returns list of dicts."""
    body = client.get_bytes("corpCode.xml", {}, save_as=f"corpCode_{date.today():%Y%m%d}.zip")
    return parse_corp_codes(body)


def parse_corp_codes(zip_bytes: bytes) -> list[dict]:
    import xml.etree.ElementTree as ET
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        name = next(n for n in zf.namelist() if n.lower().endswith(".xml"))
        root = ET.fromstring(zf.read(name))
    out = []
    for el in root.iter("list"):
        out.append({child.tag: (child.text or "").strip() for child in el})
    return out


# --------------------------------------------------------------------------- classification

# Official prefix list (공시검색 guide) plus plain [정정] as a tolerant fallback.
AMEND_PREFIX = re.compile(r"^\s*\[(기재정정|첨부정정|첨부추가|변경등록|연장결정|발행조건확정|정정명령부과|정정제출요구|정정)\]")
# Must contain 단일판매 (with or without the separator). Plain "공급계약" alone also matches
# 유동성공급계약 (liquidity-provider) and free-text 투자판단관련주요경영사항 titles, which are not sales contracts.
SUPPLY_CONTRACT = re.compile(r"단일판매\s*[ㆍ·･\.]?\s*공급계약")
HALT_NOTICE = re.compile(r"매매거래정지")
CANCEL_WORDS = re.compile(r"해지|취소|철회|해제")
# rm (비고) codes: 유 KOSPI, 코 KOSDAQ, 넥 KONEX, 채 bond, 공 KFTC, 연 consolidated,
# 정 "amended later" (look-ahead!), 철 "withdrawn later" (look-ahead!).
RM_LOOKAHEAD = {"정": "amended_later", "철": "withdrawn_later"}
RM_MARKET = {"유": "Y", "코": "K", "넥": "N"}


@dataclass(frozen=True)
class Classification:
    is_supply_contract: bool
    kind: str  # new_contract | amendment | cancellation | other
    amendment_tag: str | None
    note: str
    lookahead_flags: tuple[str, ...] = ()  # from rm; known only AFTER the event, never a signal input
    market_from_rm: str | None = None
    subsidiary: bool = False  # "(자회사의 주요경영사항)" variant
    voluntary: bool = False   # "(자율공시)" variant


def classify_report_name(report_nm: str, rm: str = "") -> Classification:
    """Coarse, name-based. Anything ambiguous is marked for manual review downstream.

    - '[정정]' family prefixes => amendment of an earlier filing (needs link to original).
    - names with 해지/취소/철회/해제 => cancellation-type; a cancellation is NOT a new event.
    - '단일판매ㆍ공급계약체결' without those => candidate new_contract.
    Remarks (rm) codes are kept as-is in the note; their meaning is documented in docs/DATA_SOURCES.md.
    """
    name = re.sub(r"\s+", " ", (report_nm or "")).strip()  # API pads titles with trailing spaces
    m = AMEND_PREFIX.match(name)
    tag = m.group(1) if m else None
    core = AMEND_PREFIX.sub("", name).strip()
    flags = tuple(v for k, v in RM_LOOKAHEAD.items() if k in (rm or ""))
    market = next((v for k, v in RM_MARKET.items() if k in (rm or "")), None)
    if HALT_NOTICE.search(core):
        # e.g. "주권매매거래정지 (단일판매공급계약)": KRX halts trading around a large contract filing.
        return Classification(False, "halt_notice", tag, "trading-halt notice, not a contract", flags, market)
    supply = bool(SUPPLY_CONTRACT.search(core))
    if not supply:
        return Classification(False, "other", tag, "not a supply-contract report", flags, market)
    subsidiary = "자회사" in core
    voluntary = "자율공시" in core
    if CANCEL_WORDS.search(core):
        kind, note = "cancellation", "cancellation/withdrawal wording in report name"
    elif tag:
        kind, note = "amendment", f"{tag}: amendment; link to original before use"
    else:
        kind, note = "new_contract", "candidate; contract figures need document extraction"
    if subsidiary:
        note += "; subsidiary contract (parent files); revenue basis needs review"
    if rm:
        note += f"; rm={rm}"
    return Classification(True, kind, tag, note, flags, market, subsidiary, voluntary)


def dart_viewer_url(rcept_no: str) -> str:
    return f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"
