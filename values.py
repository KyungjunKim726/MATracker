"""모듈 간 공유하는 값 객체."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DailyClose:
    """일봉 종가 한 건.

    date: 미국장 기준 `YYYY-MM-DD`
    source: 어떤 시세 소스에서 가져왔는지(yahoo / nasdaq / stooq ...)
    """

    date: str
    close: float
    source: str = ""
