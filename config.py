"""서비스 전역 설정.

DB 접속 정보와 KIS API URL은 기존 프로젝트의 config.py 값을 그대로 사용하며,
필요하면 같은 이름의 환경변수로 덮어쓸 수 있다.
"""

from __future__ import annotations

import os
import urllib.parse

# --- 한국투자증권 API (실전투자: 9443 / 모의투자: 29443) ---------------------
URL_BASE = os.environ.get("KIS_URL_BASE", "https://openapi.koreainvestment.com:9443")

# --- MySQL 접속 --------------------------------------------------------------
db_user = os.environ.get("DB_USER", "jun")
db_password = os.environ.get("DB_PASSWORD", "RhRkfzhs1@")
db_host = os.environ.get("DB_HOST", "localhost")
db_port = os.environ.get("DB_PORT", "3306")
db_name = os.environ.get("DB_NAME", "ibm")

# 특수문자 포함 비밀번호를 안전하게 인코딩
encoded_password = urllib.parse.quote_plus(db_password)

DB_URL = os.environ.get(
    "DB_URL",
    f"mysql+pymysql://{db_user}:{encoded_password}@{db_host}:{db_port}/{db_name}?charset=utf8mb4",
)

DB_ECHO = os.environ.get("DB_ECHO", "false").strip().lower() in ("1", "true", "yes", "on")

# --- 시간대 -----------------------------------------------------------------
KST_NAME = "Asia/Seoul"
MARKET_TZ_NAME = "America/New_York"

# --- 신호 규칙 --------------------------------------------------------------
#: 이동평균선 기간. 이 서비스의 핵심 기준값이다.
SMA_WINDOW = 120

#: 종가가 120일선 위에 있을 때, 이격도가 이 값 이상이면 분할매도 신호.
DEFAULT_SELL_THRESHOLD_PCT = 30.0
SELL_THRESHOLD_PCT: dict[str, float] = {
    "TQQQ": 30.0,
    "QLD": 20.0,
    "SOXL": 30.0,
}

#: 신규 사용자에게 기본으로 등록해주는 추적 종목.
DEFAULT_TRACKED_SYMBOLS: tuple[str, ...] = ("TQQQ", "QLD", "SOXL")

#: 보유수량을 몇 등분해서 분할매도할지에 대한 기본값.
DEFAULT_SELL_SPLITS = 120

#: 1회 분할매수 금액(USD)과 총 예수금 기준 기본값.
DEFAULT_ROUND_UNIT = 100.0
DEFAULT_TOTAL_BUDGET = 20000.0

#: 자동 신호 발송 시각(KST). 미국장 마감 이후 시간대여야 한다.
SIGNAL_CRON_DAYS = "mon-fri"
SIGNAL_CRON_HOUR = 17
SIGNAL_CRON_MINUTE = 0

#: 시세 기준일이 이보다 오래되면 자동 알림을 건너뛴다.
STALE_MARKET_DAYS = 5

# --- 실행 -------------------------------------------------------------------
#: 일봉 캐시 유지 시간(초). 일봉은 장 마감 후에만 바뀌므로 짧게 캐시해도 충분하다.
PRICE_CACHE_TTL_SECONDS = 900.0

#: 사용자 목록(=폴링해야 할 텔레그램 토큰)을 다시 읽는 주기(초).
USER_REFRESH_SECONDS = 60.0

#: 시작 시 사용자에게 가동 알림을 보낼지.
SEND_STARTUP_NOTICE = os.environ.get("SEND_STARTUP_NOTICE", "true").strip().lower() in ("1", "true", "yes", "on")

#: 자동 신호 스케줄러 사용 여부.
ENABLE_SCHEDULED_SIGNAL = os.environ.get("ENABLE_SCHEDULED_SIGNAL", "true").strip().lower() in ("1", "true", "yes", "on")

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

# --- HTTP -------------------------------------------------------------------
HTTP_TIMEOUT = 20.0
TELEGRAM_POLL_TIMEOUT = 30
POLL_INTERVAL_SECONDS = 2.0

PROXY_URL = (
    os.environ.get("QUOTAGUARDSTATIC_URL")
    or os.environ.get("HTTP_PROXY")
    or os.environ.get("HTTPS_PROXY")
)

DEFAULT_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/137.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/csv,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9,ko;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


def sell_threshold_pct(symbol: str) -> float:
    """종목별 분할매도 이격도 기준(%)."""
    return SELL_THRESHOLD_PCT.get(symbol.upper(), DEFAULT_SELL_THRESHOLD_PCT)


def stooq_symbol(symbol: str) -> str:
    """Stooq CSV 심볼. 미국 상장 ETF는 `<티커>.us` 규칙을 따른다."""
    return f"{symbol.lower()}.us"
