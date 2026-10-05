# 데이터 공급자 조사

조사일: 2026-10-04. 공식 페이지에서 확인한 사실만 적고, 확인하지 못한 항목은 "미확인"으로 둡니다.
유료 구독은 사용하지 않습니다. 사용 조건은 실제 수집 전에 다시 확인합니다.

## 1. 공시: OpenDART (채택)

공식 가이드: https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS001&apiId=2019001
약관: https://opendart.fss.or.kr/intro/terms.do

- 키 발급: 회원가입 후 [인증키 신청/관리]. 개인회원은 즉시 발급. 무료.
- 한도: 개인 키 **하루 20,000건**(전체 API 합산). 분당 1,000건 초과 시 제한 가능.
  오류 020 = 한도 초과, 010 = 미등록 키, 013 = 조회 결과 없음(오류 아님).
- 이용: 공공데이터로 상업적 이용 가능하되 재배포·재가공 책임은 이용자. 키 공유 금지.
- 공시목록·원문은 **전체 기간** 조회 가능. 재무정보 API는 2015 사업보고서부터.

### 공시검색 `list.json`

- `corp_code` 없이 조회하면 **기간이 3개월로 제한**됩니다. 수집기는 89일 단위로 나눠 호출합니다.
- `last_reprt_at=N`(기본)이어야 원본과 정정이 **각각의 접수번호**로 모두 나옵니다. 과거 시점
  복원에 필수. `Y`는 최종본만 주므로 사용하지 않습니다.
- `page_count` 최대 100. `total_page`로 페이지네이션.
- 단일판매ㆍ공급계약은 **전용 상세유형 코드가 없습니다.** 거래소 수시공시이므로
  `pblntf_ty=I`, `pblntf_detail_ty=I001`로 받은 뒤 `report_nm`으로 거릅니다.
  구분자는 U+318D "ㆍ"입니다. 관찰된 제목:
  `단일판매ㆍ공급계약체결`, `…체결(자율공시)`, `…체결(자회사의 주요경영사항)`,
  `[기재정정]단일판매ㆍ공급계약체결`, `단일판매ㆍ공급계약해지`, `[기재정정]…해지`.
- 정정 접두어(공식): `[기재정정]`, `[첨부정정]`, `[첨부추가]`, `[변경등록]`, `[연장결정]`,
  `[발행조건확정]`, `[정정명령부과]`, `[정정제출요구]`. 단순 `[정정]`은 공식 목록에 없음(허용은 함).
- 해지는 별도 제목의 공시이며 원본에 표시되지 않습니다. 수집기는 같은 회사의 직전 체결 공시에
  연결하되 `link_method`를 기록하고, 연결 실패는 수동 확인으로 남깁니다.
- `rm`(비고) 코드: 유=유가, 코=코스닥, 넥=코넥스, 채, 공, 연=연결, **정=나중에 정정됨**,
  **철=나중에 철회됨**. "정"과 "철"은 사후 정보이므로 신호 입력에 절대 쓰지 않습니다.
  정규화 결과에는 `lookahead_flags`로만 기록합니다. "동"은 미확인.
- `corp_cls`(Y/K/N/E)가 공시 당시 기준인지 현재 기준인지는 미확인. 시장 소속은 KRX 자료로 교차 확인.

### 원문 `document.xml`, 고유번호 `corpCode.xml`

- 원문은 접수번호당 1건, zip(binary). 오류 014 = 파일 없음. zip 내부 XML 인코딩은 미확인이라
  수집기는 XML 선언 → UTF-8 → CP949 순으로 시도합니다.
- 계약금액·최근매출액·매출액대비는 **구조화 API가 없습니다.** DS005(주요사항보고서 주요정보)에
  없음을 확인했습니다(https://opendart.fss.or.kr/guide/main.do?apiGrpCd=DS005). 원문 표에서 추출합니다.
- 관찰된 표 항목. 유가 양식: 계약금액(원), 최근매출액(원), 매출액대비(%), 계약상대, 계약기간 시작일/종료일,
  공시유보 여부. 코스닥 양식: 조건부 계약여부, 확정 계약금액, 조건부 계약금액, 계약금액 총액(원),
  최근 매출액(원), 매출액 대비(%), 계약상대방, 계약(수주)일자. 시기·시장별로 표기가 달라
  추출기는 공백·단위를 제거한 라벨로 느슨하게 매칭하고, 금액/매출/비율 교차 검증이 안 맞으면
  자동 반영하지 않습니다. 외화 금액은 추측 환산하지 않고 수동 확인으로 보냅니다.
- 원문 반영 지연: 제출 후 약 15분(혼잡 시 익영업일).

### 재무제표 API (DS003)

- `fnlttSinglAcnt`(2019016), `fnlttSinglAcntAll`(2019020): `bsns_year`, `reprt_code`
  (11013 1분기, 11012 반기, 11014 3분기, 11011 사업보고서). 2015년 이후.
- 응답에 `rcept_no`가 있어 공개일을 역추적할 수 있습니다. 다만 정정 후 수치가 바뀔 수 있고
  원본만 요청하는 옵션은 없습니다. 엄격한 시점 복원이 필요하면 원본 접수번호의 XBRL/원문을 씁니다.
- v0/v1의 `annual_revenue`는 우선 공시 원문의 "최근 매출액"(공시 시점에 공개된 값)을 쓰고
  `revenue_available_date = 접수일`로 둡니다. 연결/별도 기준은 수동 확인 항목입니다.

## 2. 시세: 공공데이터포털 금융위원회_주식시세정보 (채택, 2026-10-05 실측)

API 페이지: https://www.data.go.kr/data/15094808/openapi.do
엔드포인트(실측): `https://apis.data.go.kr/1160100/GetStockSecuritiesInfoService_V2/getStockPriceInfo_V2`
(페이지의 구버전 주소 `.../service/GetStockSecuritiesInfoService/...`는 "등록되지 않은 서비스키"로 거부됨)

- 키: 회원가입 후 활용신청(자동 승인). 환경변수 `DATA_GO_KR_API_KEY`로만 읽음. 개발계정 하루 10,000건.
- 라이선스: KOGL 4유형(출처표시·**비상업**·변경금지). 개인 연구용으로만 사용. 재배포 금지.
- 호출 방식: `basDt`(일자) 하나로 그날 **전 종목** 조회. `numOfRows=10000`이면 한 페이지(약 2,500~2,800행).
  거래일 하나당 요청 1회. 2020~2026 전체 약 1,760회.
- 필드: basDt, srtnCd(6자리 종목코드), isinCd, itmsNm, mrktCtg(KOSPI/KOSDAQ/KONEX), clpr(종가), mkp(시가),
  hipr(고가), lopr(저가), trqu(거래량), trPrc(거래대금), lstgStCnt(상장주식수), mrktTotAmt(시가총액).
- **데이터 시작: 2020-01-02.** 2019년 이전은 0행. 따라서 2016~2019년 공시는 이 출처로 가격을 붙일 수 없음.
- **원주가(미수정)**: 카카오 2021-04-15 액면분할 전후 종가 558,000 → 120,500, 상장주식수 5배. 보정은 엔진에서
  상장주식수 변화와 공시로 처리해야 함.
- **상장폐지 종목 포함**: 일별 스냅샷이라 당시 상장 종목이 그대로 나옴(오스템임플란트 2022 있음, 2024 없음).
  생존 편향이 이 범위에서는 해소됨.
- ETF·ETN 없음. **우선주·스팩은 포함**되므로 ISIN 9번째 문자(0=보통주)와 종목명으로 제외.
- 휴장일은 0행. **거래정지 종목은 행이 있되 시가·고가·저가·거래량이 0**이고 종가는 직전 종가.
  정규화기는 이를 `halts.csv`로 분리하고 `prices.csv`에 넣지 않음.
- 평일인데 0행인 날은 휴장 또는 결측. 공식 휴장일 목록(open.krx)과 교차 확인 전에는 캘린더로 확정하지 않음.
- 기업행동(배당·증자 세부)은 없음. 2020-04 이후는 `주식권리일정정보` API(15059609)로 보완 가능.

수집기: `python -m trading_helper prices-collect --start 2020-01-02 --end <date>`,
정규화: `python -m trading_helper prices-normalize` → `data/normalized/prices.csv`, `securities.csv`,
`calendar.csv`, `halts.csv`, `share_count_changes.csv`.

## 3. 시세·거래일·상장폐지 후보 조사 (2026-10-04)

| 출처 | 일봉 | 상장폐지 종목 | 거래일/휴장 | 기업행동 | 사용 조건 위험 | 접근 |
|---|---|---|---|---|---|---|
| KRX 정보데이터시스템 | 1995~ 원주가 | 목록·시세 있음 | open.krx 휴장일 2016~ | 액면가·상장일 현재값 | **높음**: 약관 10조 자동화 수집 금지 | 수동 CSV 다운로드 |
| 공공데이터포털 금융위 주식시세 | 일봉, 시작일 미확인 | 미확인 | 없음 | 권리일정 API 2020-04~ | 낮음, **KOGL 4유형(비상업·변경금지)** | REST, 키, 10,000건/일 |
| 한국투자증권 KIS API | 호출당 100봉, 수정/원주가 선택 | 미확인(없을 가능성) | 휴장일 조회 있음 | 현재 마스터 플래그만 | 낮음(계좌 필요) | REST |
| 네이버 금융 | 2000~ 수정주가 | 없음 | 없음 | 없음 | **높음**: robots.txt 전체 차단 | 스크래핑 |
| Yahoo Finance | 커버리지 불안정 | 불안정 | 없음 | 일부 | 높음 | 스크래핑 |
| pykrx / FinanceDataReader | 위 출처 래핑 | FDR 상장폐지 목록 1961~ | FDR 휴장일 CSV 1975~2026(비공식) | 없음 | 원출처 조건 상속 | 라이브러리 |
| KIND | 없음 | 상장폐지 현황(엑셀) | 없음 | 공시 원문 | WAF, 자동화 불가 | 수동 |

출처 URL:
- KRX 약관 https://data.krx.co.kr/contents/MDC/INFO/informationController/MDCINFO003.cmd
- KRX 휴장일 https://open.krx.co.kr/contents/MKD/01/0110/01100305/MKD01100305.jsp
- 공공데이터포털 주식시세 https://www.data.go.kr/data/15094808/openapi.do ,
  상장종목정보 https://www.data.go.kr/data/15094775/openapi.do ,
  주식권리일정 https://www.data.go.kr/data/15059609/openapi.do
- KIS Developers https://apiportal.koreainvestment.com/apiservice-category ,
  https://github.com/koreainvestment/open-trading-api
- 네이버 robots https://finance.naver.com/robots.txt
- FinanceDataReader https://github.com/FinanceData/FinanceDataReader , pykrx https://github.com/sharebook-kr/pykrx
- KIND 상장폐지 https://kind.krx.co.kr/investwarn/delcompany.do?method=searchDelCompanyMain

### 평가

- **법적으로 깨끗한 자동 수집 경로는 공공데이터포털(비상업 개인 연구)과 KIS API(계좌 필요)뿐입니다.**
  KRX·네이버·Yahoo는 약관이나 robots가 자동 수집을 금지합니다. pykrx/FDR는 이를 우회하는 라이브러리라
  같은 위험을 상속합니다.
- 상장폐지 종목 시세는 KRX 정보데이터시스템에만 사실상 존재합니다. 수동 CSV 다운로드는 약관상 허용됩니다.
  표본이 수천 건이면 수동 다운로드가 현실적이지 않으므로, 생존 편향 보정 범위를 좁혀야 할 수 있습니다.
- 과거 **관리종목·거래정지 지정 이력**, **액면분할·무상증자 보정 계수 표**, **과거 시점의 증권 유형**(SPAC·우선주)은
  무료로 완전 복원이 어렵습니다. 2020-04 이후는 권리일정 API로 부분 보정 가능합니다.
- 2016년 이전 공식 휴장일은 open.krx에 없습니다. FDR의 비공식 CSV만 있습니다.

### 결정 필요 (사용자)

1. 주 시세 출처: 공공데이터포털 API(키 발급 필요, 시작일·상장폐지 포함 여부는 발급 후 실측)
   vs KIS API(한국투자증권 계좌 보유 여부) vs KRX 수동 CSV.
2. 상장폐지 종목 처리: KRX 수동 다운로드로 일부 복원 vs 생존 편향을 한계로 명시하고 기간을 좁힘.
3. 검증 기간 시작점: 거래일 캘린더가 공식적으로 있는 2016년 이후를 기본으로 제안합니다.

## 4. 저장소 반영 상태

- 구현: `trading_helper/opendart.py`(수집), `trading_helper/opendart_normalize.py`(정규화).
- 원본: `data/raw/opendart/{list,document}/` + `manifest.jsonl`(sha256, fetched_at, 마스킹된 요청).
- 산출: `data/normalized/opendart_events.csv`(자동 통과분, `risk_approved=false`),
  `opendart_related.csv`(정정·해지 연결), `opendart_review_queue.csv`(전체 상태와 사유).
- 2016-01-01 ~ 2026-10-04 수집·정규화 완료. 기록: `docs/runs/2026-10-05_opendart_collection.md`.
- 하루 한도 때문에 전체 기간 원문 수집은 며칠에 나눠야 할 수 있습니다. `--max-requests`로 제한하고
  이미 받은 원문은 건너뜁니다.
