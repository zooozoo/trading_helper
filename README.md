# trading_helper

**이어받는 작업자는 `docs/HANDOFF.md`부터 읽으세요.** 첫 메시지는 `prompts/03_handoff_start.md`.

국내 주식 이벤트 전략의 검증용 초기 설정. Claude Code가 메인, Codex가 검토를 담당합니다.
실제 종목 추천·자동 주문·실데이터 성과 검증은 아직 구현하거나 수행하지 않았습니다.

## 설치

Python 3.11 이상. 초기 코드는 외부 의존성 없이 실행됩니다.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m unittest discover -s tests -v
python -m trading_helper demo
```

`demo`는 가상의 가격으로 매매 계획을 계산합니다. 실제 시세·수익률이 아닙니다.
코드 재현용으로 Python 버전과 Git commit을 실행 기록에 저장하세요.

## 첫 전략

`docs/STRATEGY_V0.md`: 국내 수주 공시 이후 거래량을 동반한 돌파를 검증합니다.
`config/strategy_v0.json`: 초기 가설 수치. 검증된 최적값이 아닙니다.
`trading_helper/rules.py`: 신호·매매 계획·일봉 청산·수량 계산 함수.
`data/templates/`: 실데이터 입력 형식의 헤더. 샘플 종목 데이터는 없습니다.

## 주간 실행 전략

`docs/STRATEGY_WEEKLY_V1.md`: 사용자가 실제로 실행할 수 있는 절차에 맞춘 변형.
일요일 추천 → 월요일 개장 전 지정가 매수 예약 → 평일 장 마감 후 종가 보고(ChatGPT, 보고만)
→ 기준선 이탈 시 다음 날 개장 전 시장가 매도 예약 → 주 마지막 거래일 시가 청산.
`config/strategy_weekly_v1.json`: 초기 가설 수치. v0와 성과를 섞지 않습니다.
`docs/LIVE_RECONCILIATION.md`: 체결 기록(`data/templates/trades.csv`)으로 계획 대 실제를 대조하는 규칙과
전략 수정 허용 조건. 실거래 손익으로 파라미터를 직접 바꾸지 않습니다.

`trading_helper/weekly_v1.py` + `market.py`: 주간 모드 포트폴리오 백테스트 엔진. 실행:

```bash
python -m trading_helper backtest-weekly --exp W1-A-02 --variant A --segment development --risk-filter none
```

`--risk-filter none`은 재무 위험 필터를 끈 가설 단계 실행이며 결과에 그렇게 표시됩니다.
실험 기록은 `docs/runs/`. 첫 실행(2026-10-06)에서 개발 구간 수익성 가설은 지지되지 않았습니다.

`trading_helper/factor_v1.py` + `fundamentals_pit.py`: 월간 팩터 포트폴리오 엔진(사전 등록 `config/strategy_factor_v1.json`).

```bash
python -m trading_helper backtest-factor --exp F1-01 --segment development
```

## 에이전트 사용

1. Claude Code에서 `CLAUDE.md`, `AGENTS.md`, 전략·데이터 명세를 읽습니다.
2. `prompts/01_claude_start.md`를 전달해 데이터 확보와 정규화부터 진행합니다.
3. 구현 후 Codex에 `prompts/02_codex_review.md`를 전달합니다.
4. Claude Code가 검토 사항을 반영하고 재검증합니다.
5. 미사용 과거 구간 평가와 향후 모의투자를 통과한 경우에만 소액 실전을 검토합니다.

공통 지침은 `AGENTS.md`, Claude 전용 진입 문서는 `CLAUDE.md`입니다.
두 도구가 동시에 같은 파일을 수정하지 않습니다. 기본은 순차 작업입니다.

## 데이터 수집

조사 결과와 사용 조건은 `docs/DATA_SOURCES.md`. OpenDART 키는 환경변수 `OPENDART_API_KEY`로만 읽습니다.

```bash
export OPENDART_API_KEY=...   # 출력·커밋 금지
python -m trading_helper dart-collect --start 2024-01-01 --end 2024-03-31 --documents
python -m trading_helper dart-normalize
python -m trading_helper validate --events data/normalized/opendart_events.csv
```

`dart-normalize`는 숫자가 깨끗하게 추출되고 보고 비율과 교차 검증된 공시만 `opendart_events.csv`에
넣고, 그마저도 `risk_approved=false`입니다. 나머지는 `opendart_review_queue.csv`에서 수동 확인합니다.
정정·해지는 `opendart_related.csv`에 원본 공시와 연결됩니다.

## 다음 작업 순서

- (완료) 공급자 조사, OpenDART 수집기·정규화, 입력 검증기
- OpenDART 키 발급 후 실제 수집 실행, 추출 규칙을 실제 원문으로 보정
- (완료) 시세 2020~2026 수집(`prices-collect`, `prices-normalize`). 2016~2019는 미사용
- 공식 휴장일 교차 확인, 액면분할 보정 규칙
- 실적·재무 위험 자료를 당시 공개 시점별로 정규화
- 거래정지·가격제한·기업행동을 처리하는 포트폴리오 백테스트 구현
- 고정한 시간순 개발/검증/최종 평가 구간에서 비용 포함 성과 계산
- 주간 관찰 보고 및 일별 신호·보유 포지션 관리 구현

## 저장 규칙

코드는 Git으로 관리합니다. API 키, 원본 유료 데이터, 실제 계좌 정보는 커밋하지 않습니다.
`.env.example`은 형식만 제공합니다. 초기 프로그램은 `.env`를 자동 로드하지 않습니다.
운영 전 `docs/VALIDATION.md`의 한계와 통과 조건을 확인하세요.

## 전달된 파일 반영

기존 로컬 저장소에 파일을 복사한 후 `git diff`와 `git status`로 변경을 확인합니다.
기존 README나 에이전트 지침이 있다면 덮어쓰기보다 병합합니다. 배포·푸시는 자동 수행하지 않습니다.
