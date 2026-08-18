# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

120일 이동평균선 기준 매매 신호를 텔레그램으로 보내는 다중 사용자 서비스입니다. MySQL
`user` 테이블에서 사용자별 봇 토큰·챗ID·KIS 키를 읽고, 종목 전략은 `symbol_state`에
저장합니다. 폴더 없이 루트에 평면 배치되어 있고 테스트만 `tests/`에 있습니다.

## 명령어

```bash
python main.py initdb              # symbol_state 생성 (user 테이블은 절대 건드리지 않음)
python main.py users               # 사용자와 추적 종목 확인
python main.py signal TQQQ QLD     # 신호만 계산 — 텔레그램·DB 쓰기 없음
python main.py signal TQQQ --user-id 1   # 해당 사용자 전략값 사용 (symbol_state 행 생성됨)
python main.py broadcast           # 일일 발송을 즉시 1회 실행
python main.py run                 # 서비스 (스케줄러 + 롱폴링)

python -m pytest
python -m pytest tests/test_signals.py::TestAction   # 클래스 단위
python -m pytest -k threshold                        # 이름으로 골라서
```

개발 의존성은 `pip install -r requirements-dev.txt`. 린트·포매터는 없습니다. Yahoo는
대부분의 가정용 IP에 429를 반환하므로, 실제 `signal` 실행 시 Yahoo 두 번 실패 후 Nasdaq
성공 로그가 찍히는 것이 **정상 동작**입니다(폴백 체인이 작동한 것).

## 여러 파일에 걸친 설계

**설정은 모듈 상수다.** `config.py`가 DB URL(기존 MySQL 계정이 기본값, 동명 환경변수로
덮어쓰기 가능), `SMA_WINDOW = 120`, 종목별 매도 기준, cron 시각을 갖는다. import 시점에
읽으므로 변경하면 재시작이 필요하다.

**모듈명 주의.** `values.py`는 원래 `types.py`였다. 루트 평면 배치에서는 표준 라이브러리
`types`를 섀도잉해 dataclasses 등이 깨지므로 이름을 바꿨다. 루트에 새 모듈을 만들 때
표준 모듈명(`types`, `queue`, `select`, `logging` …)을 피할 것. 같은 이유로 텔레그램
클라이언트는 `telegram_api.py`다.

**테이블 두 개, 하나는 기존 것.** `models.py`가 `user`(기존 스키마 —
`app_key`/`app_secret`/`cano`/`acnt_prdt_cd`는 KIS용,
`telegram_token`/`telegram_chat_id`는 알림용)를 매핑하고 `symbol_state`(사용자 × 종목
전략 + 마지막 발송 기록)를 정의한다. `db.init_db()`는 `create_all`을
`symbol_state.__table__`로 한정하므로 `user` 테이블은 변경되지 않는다.

**종목 정렬 순서가 동작에 영향을 준다.** 명령에서 종목을 생략할 수 있고
(`/config 150 18000 120`) 그때 *첫* 추적 종목을 쓰기 때문에,
`repository.symbol_sort_key`가 `config.DEFAULT_TRACKED_SYMBOLS` 순서(TQQQ, QLD, SOXL)를
먼저 지키고 그 밖의 종목만 알파벳순으로 둔다. 단순 알파벳 정렬이면 QLD가 기본 종목이
되어버린다. 종목 목록을 돌려주는 코드는 `list_states` / `tracked_symbols`를 거치고, 절대
`ORDER BY symbol`을 직접 쓰지 말 것.

**async 안의 동기 SQLAlchemy.** `repository.py`의 함수는 모두 첫 인자로 `session`을 받는
동기 함수다. async 호출부는 `await repository.run(fn, *args)`를 쓰며, 이것이
`asyncio.to_thread` 안에서 `db.session_scope()`를 열고 커밋한다. async 코드에서
리포지토리 함수를 직접 호출하지 말고, 쓰기는 반드시 `run()`에 넘긴 함수 안에서 할 것.
세션 팩토리가 `expire_on_commit=False`이므로 반환된 ORM 객체는 세션이 닫힌 뒤에도 읽을 수
있는 스냅샷이다.

**신규 사용자는 지연 초기화된다.** `repository.tracked_or_seed`는 상태 행이 *하나도* 없는
사용자에게만 기본 종목을 심는다. `/untrack`으로 전부 끈 사용자는 빈 상태로 남는다 —
누락이 아니라 의도된 동작이다.

**시세: 폴백 체인 + 순수 파서.** `prices.py`는 Yahoo/httpx → Yahoo/urllib → Nasdaq →
Stooq(브라우저 검증용 SHA-256 PoW 솔버 포함) 순으로 시도한다. 각 소스는
`list[DailyClose]`를 반환하고 120건 이상이어야 하며, `normalize_closes`가 날짜 중복
제거·정렬 후 개수를 다시 검증한다. 파싱은 `parse_yahoo_chart` /
`parse_nasdaq_historical` / `parse_stooq_csv`로 분리해 네트워크 없이 응답 샘플로 테스트할
수 있다. `PriceService`는 종목 키 TTL 캐시를 들고 있어 N명이 TQQQ를 추적해도 조회는 한
번이며, 테스트에서는 생성자로 가짜 소스를 주입한다.

**신호 계산은 순수 로직이다.** `signals.py`는 I/O를 import하지 않는다.
`calculate_signal`은 `StrategyInput` 프로토콜(`round_unit`, `shares`, `sell_splits`)을
만족하는 아무 객체나 받는다 — `SymbolState`도, 테스트 스텁도 만족한다.
`Signal.fingerprint`(종목·기준일·판정·종가·이동평균·이격도의 SHA-256)와
`is_duplicate_signal` / `is_stale_market_date`가 재발송을 막는 장치다.

**스케줄 경로가 온디맨드 경로보다 엄격하다.** `app.MATrackerService.broadcast_signals`는
시세 캐시를 비우고, 사용자·종목별로 노후 시세와 중복 신호를 건너뛰며, **`send`가 True를
반환한 뒤에만** DB에 기록한다 — 텔레그램 실패 시 다음 실행에서 재시도된다. `/signal`에는
이런 가드가 의도적으로 없다.

**롱폴링은 사용자별이 아니라 토큰별이다.** `sync_pollers`가
`USER_REFRESH_SECONDS`마다 알림 대상 사용자를 다시 읽어 서로 다른 `telegram_token`마다
폴링 태스크 하나를 유지한다(같은 토큰을 두 곳에서 폴링하면 텔레그램이 409를 준다).
죽은 태스크도 다음 주기에 되살린다. 인증은
`repository.find_user_by_chat(token, chat_id)` 하나이며, 미등록 챗은 경고 로그만 남기고
무시한다.

## 규칙

사용자에게 보이는 문장과 로그는 한국어로 쓴다. 메시지는 `parse_mode=HTML`로 나가므로
동적으로 끼워넣는 값(예외 문구, 시세 소스, 종목명)은 반드시 `messages.esc()`를 거쳐야
하고, 비밀값은 `messages.mask()`를 쓴다. 명령 핸들러는 `ctx.reply`로만 응답한다 — 이
간접 계층이 있어서 `tests/test_commands.py`가 네트워크 없이 전송 메시지를 검증할 수 있다.

`tests/`는 패키지가 아니다(`__init__.py` 없음). `pyproject.toml`의
`pythonpath = [".", "tests"]`가 루트 평면 모듈과 `conftest`를 함께 임포트할 수 있게 한다.

`config.py`에 DB 비밀번호가 평문 기본값으로 들어 있다. 커밋 전에 `DB_PASSWORD` 환경변수로
옮기는 것이 낫다.
