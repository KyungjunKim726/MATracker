# MATracker

120일 이동평균선(120일선)을 기준으로 분할매수/분할매도 신호를 텔레그램으로 알려주는 서비스.

MySQL `user` 테이블을 읽어 **여러 사용자**를 각자의 봇 토큰·챗ID로 지원하며, 사용자별
종목 전략과 마지막 발송 신호는 `symbol_state` 테이블에 저장합니다.

## 판정 규칙

```
이격도(%) = (종가 / 120일선 - 1) × 100

종가 < 120일선                        → BUY  분할매수 (1회 매수금액 ÷ 종가)
종가 ≥ 120일선, 이격도 ≥ 종목별 기준   → SELL 분할매도 (보유수량 ÷ 매도분할수)
그 외                                  → HOLD 관망
```

매도 기준 이격도는 두 단계로 결정됩니다.

1. 사용자가 `/track 종목 이격도`로 지정한 값 (`symbol_state.sell_threshold_pct`)
2. 지정하지 않았으면 `config.py`의 `SELL_THRESHOLD_PCT` 기본값
   (TQQQ 30% / QLD 20% / SOXL 30%, 미등록 종목은 30%)

같은 종목이라도 사용자마다 다른 기준을 쓸 수 있습니다. 예를 들어 `/track SOXL 25`는
그 사용자의 SOXL만 25%로 바꾸고, `/track SOXL 기본`은 지정을 지워 다시 기본값을
따르게 합니다.

## 설치

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt   # Windows
# .venv/bin/pip install -r requirements-dev.txt               # Linux
```

DB 접속 정보와 KIS API URL은 `config.py`에 있고, 같은 이름의 환경변수로 덮어쓸 수
있습니다 (`DB_URL`, `DB_HOST`, `DB_USER`, `DB_PASSWORD`, `DB_NAME`, `KIS_URL_BASE` …).

## 실행

```bash
python main.py initdb            # symbol_state 테이블 생성 (user 테이블은 건드리지 않음)
python main.py users             # 등록된 사용자와 추적 종목 확인
python main.py signal TQQQ QLD   # 텔레그램 없이 신호만 계산해 출력
python main.py signal TQQQ --user-id 1   # 특정 사용자의 전략값으로 계산
python main.py broadcast         # 일일 신호 발송을 즉시 1회 실행
python main.py run               # 서비스 실행 (스케줄러 + 롱폴링)
```

테스트:

```bash
python -m pytest                                     # 전체
python -m pytest tests/test_signals.py::TestAction   # 클래스 단위
python -m pytest -k threshold                        # 이름으로 골라서
```

Yahoo가 대부분의 가정용 IP에 429를 돌려주므로, 실제 실행 시 Yahoo 두 번 실패 후
Nasdaq으로 넘어가는 로그가 정상입니다.

## 텔레그램 명령어

| 명령 | 설명 |
| --- | --- |
| `/signal [종목]` | 단일 종목 신호 (종목 생략 시 첫 추적 종목) |
| `/signals` | 추적 종목 전체 요약 |
| `/config [종목] [1회매수금액] [총예수금] [매도분할수]` | 인자 없으면 현재 설정 조회 |
| `/update [종목] [평단가] [보유주수] [회차]` | 보유 현황 입력 |
| `/track 종목 [매도기준이격도]` | 추적 시작. 이격도를 함께 주면 그 종목의 매도 기준을 사용자 값으로 저장 |
| `/untrack 종목` | 추적 중단 (설정값은 유지) |
| `/debug` | 설정·마지막 신호 진단 |

발신자 검증은 (봇 토큰, 챗ID) 조합이 `user` 테이블에 있는지로만 판단합니다. 등록되지
않은 챗에서 온 명령은 무시하고 경고 로그만 남깁니다.

## 파일 구성

폴더 없이 루트에 평면 배치되어 있습니다. 테스트만 `tests/`에 있습니다.

| 파일 | 역할 |
| --- | --- |
| `main.py` | 진입점 |
| `cli.py` | 서브커맨드 파싱 (`run` / `initdb` / `users` / `broadcast` / `signal`) |
| `config.py` | DB URL, 이동평균 기간, 종목별 매도 기준, 스케줄 시각 |
| `models.py` | `user`(기존 스키마) / `symbol_state`(이 서비스가 추가) |
| `db.py` | 엔진·세션 관리, `init_db()` |
| `repository.py` | 사용자·종목 상태 조회/갱신. async에서는 `run()`이 스레드로 감싸 실행 |
| `prices.py` | 일봉 수집 폴백 체인 + 순수 파서 + TTL 캐시 |
| `signals.py` | 120일선 판정, 신호 지문, 중복/노후 시세 판정 (순수 로직) |
| `messages.py` | 한국어 HTML 메시지 조립 |
| `telegram_api.py` | 텔레그램 Bot API 최소 클라이언트 (토큰별 오프셋 관리) |
| `commands.py` | 명령 파싱과 핸들러 |
| `app.py` | 스케줄러 + 토큰별 롱폴링 |
| `timeutil.py` | KST / 미국 동부 시간대 헬퍼 |
| `values.py` | `DailyClose` 값 객체 |

### 시세 수집

무료 소스는 차단·rate limit이 잦으므로 순서대로 시도해 먼저 성공한 것을 씁니다.

1. Yahoo chart API (httpx, 429 재시도 + query1/query2 교대)
2. Yahoo chart API (urllib — httpx/프록시 환경 이슈 우회)
3. Nasdaq historical API
4. Stooq CSV (브라우저 검증 PoW 챌린지 대응 포함)

각 소스는 최소 120영업일치를 반환해야 하고, `normalize_closes()`가 날짜 중복 제거·정렬
후 개수를 한 번 더 검증합니다. 같은 종목을 여러 사용자가 추적해도 TTL 캐시로 한 번만
조회합니다.

### 자동 발송

평일 17:00 KST(`config.SIGNAL_CRON_*`)에 사용자별 추적 종목을 계산해 보냅니다.

- 대상은 `telegram_token`과 `telegram_chat_id`가 모두 채워진 사용자입니다. **`user`
  테이블에 행만 추가하면 됩니다** — 종목 상태가 없는 신규 사용자에게는 발송 시점에 기본
  종목(TQQQ/QLD/SOXL)을 자동으로 등록하므로, 봇에 `/start`를 보내거나 서비스를 재시작할
  필요가 없습니다. 단, 모든 종목을 `/untrack` 한 사용자는 그 상태를 존중해 다시 심지
  않습니다.
- 시세 기준일이 5일보다 오래되면 건너뜁니다 (`is_stale_market_date`).
- 이미 보낸 신호는 지문(종목·기준일·판정·종가·120일선·이격도의 SHA-256)으로 걸러냅니다.
- **발송 성공 후에만** DB에 기록하므로, 텔레그램 실패 시 다음 실행에서 다시 시도합니다.

## 배포 (Linux + systemd)

리눅스 서버에서 `python main.py run`을 상시 실행시키는 방법입니다. 아래는 앱을
`/opt/MATracker`에, MySQL을 같은 서버에 둔 경우를 기준으로 합니다.

### 1) 전용 계정과 디렉터리

루트로 돌리지 않습니다. 로그인 셸이 없는 시스템 계정을 만듭니다.

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin matracker
sudo mkdir -p /opt/MATracker
sudo chown -R matracker:matracker /opt/MATracker
```

### 2) 코드와 가상환경

```bash
# 코드 복사 (rsync / git clone / scp 중 편한 방법)
sudo -u matracker rsync -a --exclude '.venv' --exclude '__pycache__' ./ /opt/MATracker/

cd /opt/MATracker
sudo -u matracker python3 -m venv .venv
sudo -u matracker .venv/bin/pip install -r requirements.txt   # 서버에는 dev 의존성 불필요
```

Python 3.11 이상이 필요합니다(`python3 --version`으로 확인).

### 3) 비밀정보는 EnvironmentFile로 분리

`config.py`의 기본값(DB 비밀번호 등)을 그대로 쓰지 말고 환경변수로 덮어씁니다. 이 파일은
서비스 계정만 읽을 수 있게 권한을 조입니다.

```bash
sudo install -o root -g matracker -m 640 /dev/null /etc/matracker.env
sudo tee /etc/matracker.env >/dev/null <<'EOF'
DB_HOST=localhost
DB_PORT=3306
DB_NAME=ibm
DB_USER=jun
DB_PASSWORD=여기에_실제_비밀번호
LOG_LEVEL=INFO
ENABLE_SCHEDULED_SIGNAL=true
SEND_STARTUP_NOTICE=true
PYTHONDONTWRITEBYTECODE=1
EOF
```

인식되는 환경변수는 `DB_URL`(전체 URL을 직접 지정할 때), `DB_HOST` / `DB_PORT` /
`DB_NAME` / `DB_USER` / `DB_PASSWORD`, `DB_ECHO`, `KIS_URL_BASE`, `LOG_LEVEL`,
`ENABLE_SCHEDULED_SIGNAL`, `SEND_STARTUP_NOTICE`, 그리고 고정 IP 프록시가 필요할 때
`QUOTAGUARDSTATIC_URL` / `HTTP_PROXY` / `HTTPS_PROXY`입니다. **모두 import 시점에 한 번만
읽으므로 값을 바꾸면 서비스 재시작이 필요합니다.**

### 4) 테이블 생성과 수동 확인

서비스로 올리기 전에 사람이 직접 한 번 확인하는 편이 문제를 빨리 찾습니다.

```bash
cd /opt/MATracker
set -a; . /etc/matracker.env; set +a

sudo -u matracker --preserve-env .venv/bin/python main.py initdb   # symbol_state 생성
sudo -u matracker --preserve-env .venv/bin/python main.py users    # 사용자·종목 확인
sudo -u matracker --preserve-env .venv/bin/python main.py signal TQQQ   # 시세 경로 확인
```

`users`에 `알림 OK`가 찍혀야 자동 발송 대상입니다. `텔레그램 설정 없음`이면 해당 사용자의
`telegram_token` / `telegram_chat_id`를 채워야 합니다.

### 5) 유닛 파일

`/etc/systemd/system/matracker.service`:

```ini
[Unit]
Description=MATracker 120일선 신호봇
Documentation=https://github.com/  # 사내 저장소 주소로 바꿔주세요
After=network-online.target mysql.service
Wants=network-online.target
# DB가 다른 서버에 있으면 mysql.service 는 지웁니다.

# 재시작 폭주 제한: 10분 안에 5번 넘게 죽으면 멈추고 failed 상태로 남긴다.
# 이 두 키는 [Service] 가 아니라 [Unit] 소속이다 ([Service] 에 두면
# "Unknown key name 'StartLimitIntervalSec' in section 'Service'" 경고와 함께 무시된다).
StartLimitIntervalSec=600
StartLimitBurst=5

[Service]
Type=simple
User=matracker
Group=matracker
WorkingDirectory=/opt/MATracker
EnvironmentFile=/etc/matracker.env
ExecStart=/opt/MATracker/.venv/bin/python main.py run

# 죽으면 항상 되살린다 (원본 봇의 healthcheck.sh 역할)
# 재시작 폭주 제한은 위 [Unit] 섹션의 StartLimit* 키가 담당한다.
Restart=always
RestartSec=10

# 로그는 journald 로 (파일 로테이션 신경 쓸 필요 없음)
StandardOutput=journal
StandardError=journal
SyslogIdentifier=matracker

# 최소 권한
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
# 주의: ProtectHome=true 는 /root 와 /home 을 가린다. 앱이나 venv 를 홈 디렉터리에 두면
# 인터프리터를 찾지 못해 203/EXEC 로 죽는다. 홈에 둘 수밖에 없으면 read-only 로 바꾼다.
ProtectHome=true
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictSUIDSGID=true
# 앱은 디스크에 아무것도 쓰지 않으므로 쓰기 경로가 필요 없다.
# 나중에 파일을 쓰게 되면 ReadWritePaths=/opt/MATracker/... 를 추가한다.

[Install]
WantedBy=multi-user.target
```

`WorkingDirectory`는 반드시 앱 디렉터리여야 합니다. 모듈이 폴더 없이 평면 배치라
`main.py`가 같은 디렉터리에서 `config`, `signals` 등을 임포트합니다.

### 6) 등록과 시작

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now matracker
sudo systemctl status matracker
```

정상이라면 알림 대상 사용자에게 "120일선 신호봇 시작" 메시지가 갑니다. 시작 알림이
필요 없으면 `SEND_STARTUP_NOTICE=false`로 두세요.

### 7) 로그

```bash
sudo journalctl -u matracker -f              # 실시간
sudo journalctl -u matracker --since today   # 오늘 것만
sudo journalctl -u matracker -p warning      # 경고 이상만
```

자세히 보려면 `/etc/matracker.env`에서 `LOG_LEVEL=DEBUG`로 바꾸고 재시작합니다.

### 8) 코드 갱신 배포

```bash
sudo -u matracker rsync -a --exclude '.venv' --exclude '__pycache__' ./ /opt/MATracker/
sudo -u matracker /opt/MATracker/.venv/bin/pip install -r /opt/MATracker/requirements.txt
sudo systemctl restart matracker
sudo journalctl -u matracker -n 30
```

`models.py`에 컬럼을 추가했다면 재시작 전에 `main.py initdb`를 다시 돌립니다.
`create_all`은 없는 테이블만 만들고 기존 테이블은 건드리지 않으므로, 나중에 추가된
컬럼은 `db.py`의 `_ADDED_COLUMNS`에 `ALTER TABLE` 문을 등록해 두었습니다. `initdb`가
누락된 컬럼만 찾아 채우고 로그에 남깁니다. 새 컬럼을 만들 때도 같은 방식으로 등록하세요
(컬럼 타입 변경이나 삭제는 자동화하지 않습니다 — 직접 `ALTER TABLE`).

### 9) 기동 실패 진단

`systemctl status matracker`의 `status=N`이 원인을 거의 특정해 줍니다. 파이썬까지 도달하지
못한 실패(203 / 200 / 217)는 로그에 트레이스백이 없습니다.

| status | 의미 | 원인과 조치 |
| --- | --- | --- |
| `203/EXEC` | ExecStart 실행 불가 | ① **앱이나 venv가 `/root` 아래에 있는데 `ProtectHome=true`를 걸었다** — systemd가 `/root`를 가려서 인터프리터가 없는 것으로 보인다. `/opt`로 옮기거나 `ProtectHome=read-only`로 완화한다. ② 경로 오타. ③ venv의 `bin/python3` 심볼릭 링크가 깨졌다(시스템 파이썬 업그레이드 후 흔함) → venv 재생성 |
| `200/CHDIR` | WorkingDirectory 진입 불가 | 경로가 없거나 서비스 계정에 접근 권한이 없음 |
| `217/USER` | `User=` 계정 없음 | `useradd` 단계를 건너뛴 경우 |
| `1` | 파이썬이 예외로 종료 | 로그의 트레이스백 확인. 대개 DB 접속 실패(`OperationalError`)나 의존성 누락(`ModuleNotFoundError`) |

`ProtectHome` / `ProtectSystem` 같은 샌드박스 옵션은 **경로와 함께 결정해야 합니다.** 앱을
`/opt`에 두면 이 예시 그대로 쓸 수 있고, 홈 디렉터리(`/root`, `/home/사용자`)에 두려면
`ProtectHome`을 빼거나 `read-only`로 바꿔야 합니다.

의존성이 서버 venv에 들어 있는지 한 줄로 확인할 수 있습니다.

```bash
/opt/MATracker/.venv/bin/python -c "import httpx, apscheduler, pytz, sqlalchemy, pymysql; print('deps OK')"
```

재시작 폭주로 `failed`에 머물러 있으면 카운터를 지우고 다시 올립니다.

```bash
sudo systemctl reset-failed matracker
sudo systemctl restart matracker
```

### 10) 운영 시 주의사항

- **인스턴스는 하나만.** 같은 봇 토큰으로 두 프로세스가 롱폴링하면 텔레그램이 409를
  돌려주고 명령이 간헐적으로 씹힙니다. 서비스가 돌고 있는 동안 `main.py run`을 손으로
  또 띄우지 마세요. 진단은 `main.py signal` / `users`처럼 폴링하지 않는 명령으로 합니다.
- **서버 시간대는 상관없습니다.** 스케줄러가 `pytz` KST를 직접 지정하므로 서버 TZ가
  UTC여도 평일 17:00 KST에 발송됩니다. 다만 `journalctl` 시간 표시는 서버 TZ 기준입니다.
- **발송 시각을 바꾸려면** `config.py`의 `SIGNAL_CRON_HOUR` / `SIGNAL_CRON_MINUTE` /
  `SIGNAL_CRON_DAYS`를 수정하고 재시작합니다.
- **하루치 발송을 다시 보내려면** `symbol_state`의 `last_signal_hash`(또는
  `last_signal_date`)를 비우고 `main.py broadcast`를 실행합니다. 지문이 남아 있으면 중복
  으로 판단해 건너뜁니다.
- **Yahoo 429 경고는 정상입니다.** Nasdaq으로 넘어가 성공했다면 문제가 아닙니다. 네 소스가
  모두 실패할 때만 신호가 누락되며, 그때는 경고 로그에 각 소스의 실패 사유가 남습니다.
- **MySQL 연결이 끊기는 환경**이라면 엔진에 이미 `pool_pre_ping` / `pool_recycle=3600`이
  걸려 있어 유휴 커넥션 끊김은 자동 복구됩니다. 그래도 DB가 완전히 죽으면 명령 처리가
  실패하고 경고가 남습니다.

## 참고

`user` 테이블의 `app_key` / `app_secret` / `cano` / `acnt_prdt_cd`는 매핑만 해두었고 아직
사용하지 않습니다. 한국투자증권 잔고 조회(자산 리포트)를 붙일 때 쓰입니다.
