"""시간대 헬퍼.

APScheduler 3.x가 pytz 시간대만 받으므로 zoneinfo 대신 pytz를 사용한다.
"""

from __future__ import annotations

from datetime import date, datetime

import pytz

import config

KST = pytz.timezone(config.KST_NAME)
MARKET_TZ = pytz.timezone(config.MARKET_TZ_NAME)


def now_kst() -> datetime:
    return datetime.now(KST)


def now_market() -> datetime:
    return datetime.now(MARKET_TZ)


def market_today() -> date:
    """미국장 기준 오늘 날짜."""
    return now_market().date()


def format_kst(moment: datetime | None = None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    return (moment or now_kst()).astimezone(KST).strftime(fmt)


def epoch_to_market_date(timestamp: int | float) -> str:
    """유닉스 타임스탬프를 미국장 기준 `YYYY-MM-DD`로."""
    return datetime.fromtimestamp(int(timestamp), tz=MARKET_TZ).strftime("%Y-%m-%d")
