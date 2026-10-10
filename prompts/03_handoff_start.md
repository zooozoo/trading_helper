이 저장소(trading_helper)는 국내 주식 전략 연구 프로젝트이고, 너는 이전 작업자(Claude Code)로부터 이어받는 담당자야.
저장소 밖에는 기억이 없으니 아래 순서대로 문서를 읽고 상태를 확인한 뒤, 작업을 시작하기 전에 멈추고 나에게 보고해.

1. `AGENTS.md`(공통 규칙)와 `docs/HANDOFF.md`(인수인계)를 끝까지 읽어. HANDOFF의 §2 사용자 제약, §6 결정, §9 규칙은 바꾸지 마.
2. `docs/VALIDATION.md`, `docs/DATA_SOURCES.md`, `docs/runs/` 아래 기록 6개를 읽고 무엇이 기각됐고 무엇이 살아남았는지 파악해.
3. 환경 점검: `python -m unittest discover -s tests -v`가 69개 통과하는지, `data/normalized/`에 prices.csv·fundamentals.csv·indexes.csv·opendart_events.csv가 있는지,
   `~/.zshrc`에 OPENDART_API_KEY와 DATA_GO_KR_API_KEY가 설정돼 있는지(값은 절대 출력하지 말고 길이만) 확인해.
   네 셸이 .zshrc를 읽지 않으면 `zsh -ic '명령'`으로 실행해.
4. 재현 확인: `python -m trading_helper backtest-factor --exp F1-01-rerun --segment development`를 실행해
   `docs/runs/2026-10-06_F1_development.md`의 저변동성 포트폴리오 수치(비용 후 총수익 +26.2%, 최대낙폭 −22.0%)와 같은지 확인해.
5. 보고: 환경 상태, 재현 결과, HANDOFF §8의 다음 할 일 중 무엇부터 할지 제안. **검증 구간과 최종 구간은 내가 명시적으로 허락하기 전까지 실행하지 마.**

작업할 때의 원칙:
- 모든 실험은 새 번호를 붙여 개발 구간에서만 돌리고 `docs/runs/`에 결과·설정·데이터 해시·한계를 기록해. 실패도 기록해.
- 설정을 바꾸려면 config 파일의 새 버전을 만들어. 기존 사전 등록본은 수정하지 마.
- 시장 대리변수는 동일가중 평균, 무작위·플라시보 대조군 필수, 원주가는 주식수 보정 후 사용.
- 종목 추천이나 매매 지시를 하지 마. 수익성은 검증 구간 통과 전까지 가설이야.
- API 키를 출력·저장·커밋하지 마. 커밋은 해도 되고 푸시는 내가 요청할 때만.
- 데이터 수집은 하루 한도(OpenDART 20,000건, 공공데이터포털 10,000건) 안에서 `--max-requests`로 제한하고, 끊기면 같은 명령으로 이어받아.
