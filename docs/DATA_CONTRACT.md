# 입력 데이터 계약

모든 날짜: ISO `YYYY-MM-DD`, 거래소 시간대 Asia/Seoul.
실제 시각을 확보하면 timezone-aware `published_at`을 별도로 추가합니다.
이벤트 접수일, 정보 이용 가능 시점, 수집 시점, 거래일은 서로 다른 개념입니다.
CSV 헤더 템플릿은 `data/templates/`에 있습니다.
검증기: `python -m trading_helper validate --prices <csv> --events <csv> [--json]`
(`trading_helper/validate.py`). 오류가 하나라도 있으면 종료 코드 1. 경고는 유효하지만
신호에 쓸 수 없거나 수동 확인이 필요한 행입니다. 검증기는 빈 값을 채우지 않습니다.

## prices.csv

`symbol,date,open,high,low,close,volume,source,fetched_at`

- symbol은 문자열: `005930`의 앞자리 0을 유지.
- 동일 symbol/date 중복 금지. OHLC 양수, low<=open/close<=high, volume>=0.
- 거래일별 원시 가격과 기업행동 원본을 함께 보관. 분할/병합/배당/권리락 처리 필요.
- 수정주가로 신호를 만들더라도 실제 체결 가격·수량·비용은 일관되게 변환.
- `date`는 장 마감이 완료된 세션만 포함. 현재 장중 일봉은 신호에 사용하지 않음.
- 공식 거래일과 거래정지 자료를 따로 관리. 데이터 누락을 휴장으로 처리하지 않음.

## events.csv

`event_id,symbol,receipt_date,event_type,contract_amount,annual_revenue,revenue_available_date,risk_approved,risk_available_date,source_url,fetched_at`

- event_type 허용값: `new_contract`, `amendment`, `cancellation`, `withdrawal`, `other`.
  초기 신호에는 `new_contract`만 포함. 나머지는 연결·취소 처리용으로 보관.
- annual_revenue와 revenue_available_date는 둘 다 비우면 "미확인"으로 취급(경고, 신호 제외).
  하나만 비우면 오류.
- 금액·매출은 원(KRW), 같은 연결/별도 범위. 비율 직접 입력보다 원본 두 값 저장.
- revenue_available_date는 매출 대상 회계기간 말이 아닌 **자료 공개일**.
- risk_available_date는 검토에 사용한 가장 늦은 원문 공개일. 검토를 작성한 오늘 날짜가 아님.
- risk_approved: `true`/`false`. 원본 근거 없이는 true 금지.
- 사건별 원본 공시와 관련 정정·취소를 연결. 취소는 그 공개 시점부터 적용.
- source_url과 fetched_at 필수. 외부 데이터를 LLM 추측으로 대체하지 않음.

## trades.csv

실거래·모의투자 체결 기록. 열 정의, 허용값, 대조 규칙은 `docs/LIVE_RECONCILIATION.md`.
검증: `python -m trading_helper validate --trades <csv>`. 수수료·세금은 증권사 내역의 실제 금액만.

## 원본·정규화 저장

`data/raw/`에 원본, `data/normalized/`에 정규화 결과(둘 다 Git 제외).
파일 SHA-256, 수집 일시, 원본 출처, 스키마 버전을 별도 manifest에 저장.
과거 시점 자료 복원이 불가능하면 해당 실험을 신뢰할 수 없는 것으로 표시.

## 공급자 확인

- OpenDART 공시검색 공식 가이드:
  https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS001&apiId=2019001
- 이 API의 rcept_dt는 날짜입니다. last_reprt_at=Y로 최종 정정본만 가져와 과거에 적용하면 안 됩니다.
- 재무 자료, 시세, 거래일·기업행동·상장폐지·거래정지 자료는 별도 확인 필요.
- 무료 웹 데이터는 품질/상장폐지/약관/접근 지속성을 확인한 뒤 사용합니다.
