"""일봉 종가 수집.

120일 이동평균을 구하려면 최소 120영업일치 종가가 필요하다. 무료 시세 소스는
차단·rate limit·포맷 변경이 잦아서 하나만 믿을 수 없으므로 여러 소스를 순서대로
시도하고, 먼저 성공한 소스의 데이터를 쓴다.

    1) Yahoo chart API (httpx, 429 재시도 + query1/query2 호스트 교대)
    2) Yahoo chart API (urllib, httpx/프록시 환경 이슈 우회)
    3) Nasdaq historical API
    4) Stooq CSV (브라우저 검증 PoW 챌린지 대응 포함)

파싱 함수(`parse_*`)는 순수 함수로 분리해 응답 샘플만으로 테스트할 수 있게 했다.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import logging
import re
import time
import urllib.parse
import urllib.request
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timedelta
from typing import Any

import httpx

import config
import timeutil
from values import DailyClose

logger = logging.getLogger(__name__)

PriceSource = Callable[[str], Awaitable[list[DailyClose]]]

#: Stooq PoW 챌린지가 비정상적으로 어려울 때 무한 루프를 막는 상한.
_MAX_POW_ATTEMPTS = 5_000_000


class PriceFetchError(RuntimeError):
    """모든 시세 소스가 실패했을 때."""


def new_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=config.HTTP_TIMEOUT,
        trust_env=True,
        follow_redirects=True,
        headers=config.DEFAULT_HTTP_HEADERS,
    )


# --- 순수 파서 ---------------------------------------------------------------


def normalize_closes(
    rows: Sequence[DailyClose],
    *,
    min_rows: int = config.SMA_WINDOW,
) -> list[DailyClose]:
    """날짜 기준 중복 제거 후 오름차순 정렬하고 최소 개수를 검증한다."""
    deduped: dict[str, DailyClose] = {}
    for row in rows:
        if row.close is None or float(row.close) <= 0:
            continue
        deduped[str(row.date)] = DailyClose(date=str(row.date), close=float(row.close), source=row.source)

    normalized = [deduped[key] for key in sorted(deduped)]
    if len(normalized) < min_rows:
        raise ValueError(f"정규화 후 종가가 {min_rows}개보다 적습니다. (현재 {len(normalized)}개)")
    return normalized


def parse_yahoo_chart(payload: dict[str, Any]) -> list[DailyClose]:
    result = (((payload.get("chart") or {}).get("result")) or [None])[0]
    if not isinstance(result, dict):
        raise ValueError("Yahoo 응답에 result가 없습니다.")

    timestamps = result.get("timestamp") or []
    quote = (((result.get("indicators") or {}).get("quote")) or [None])[0]
    if not timestamps or not isinstance(quote, dict):
        raise ValueError("Yahoo 응답에 시계열 데이터가 없습니다.")

    rows: list[DailyClose] = []
    for timestamp, close in zip(timestamps, quote.get("close") or []):
        if close in (None, "", "null"):
            continue
        rows.append(
            DailyClose(date=timeutil.epoch_to_market_date(timestamp), close=float(close), source="yahoo")
        )
    return rows


def parse_nasdaq_historical(payload: dict[str, Any]) -> list[DailyClose]:
    rows = (((payload.get("data") or {}).get("tradesTable")) or {}).get("rows") or []
    if not isinstance(rows, list) or not rows:
        raise ValueError("Nasdaq 응답에 historical rows가 없습니다.")

    parsed: list[DailyClose] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_date = str(row.get("date") or "").strip()
        raw_close = str(row.get("close") or "").strip().replace("$", "").replace(",", "")
        if not raw_date or not raw_close:
            continue
        try:
            market_date = datetime.strptime(raw_date, "%m/%d/%Y").strftime("%Y-%m-%d")
            close = float(raw_close)
        except ValueError:
            continue
        parsed.append(DailyClose(date=market_date, close=close, source="nasdaq"))
    return parsed


def parse_stooq_csv(text: str) -> list[DailyClose]:
    if "Date,Open,High,Low,Close,Volume" not in text:
        raise ValueError("Stooq CSV 헤더가 없습니다.")

    parsed: list[DailyClose] = []
    for row in csv.DictReader(io.StringIO(text)):
        market_date = (row.get("Date") or "").strip()
        close = row.get("Close")
        if not market_date or close in (None, "", "null"):
            continue
        try:
            parsed.append(DailyClose(date=market_date, close=float(close), source="stooq"))
        except ValueError:
            continue
    return parsed


def solve_pow_challenge(challenge: str, difficulty: int) -> int:
    """`sha256(challenge + nonce)`가 0을 difficulty개 앞에 갖는 nonce를 찾는다."""
    prefix = "0" * difficulty
    for nonce in range(_MAX_POW_ATTEMPTS):
        digest = hashlib.sha256(f"{challenge}{nonce}".encode("utf-8")).hexdigest()
        if digest.startswith(prefix):
            return nonce
    raise ValueError(f"PoW 챌린지를 {_MAX_POW_ATTEMPTS}회 안에 풀지 못했습니다. (difficulty={difficulty})")


# --- 개별 시세 소스 ----------------------------------------------------------

_YAHOO_HOSTS = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")
_YAHOO_PARAMS = {
    "interval": "1d",
    "range": "2y",
    "includePrePost": "false",
    "events": "div,splits",
}


async def from_yahoo_httpx(symbol: str) -> list[DailyClose]:
    errors: list[str] = []
    async with new_http_client() as client:
        for host in _YAHOO_HOSTS:
            url = f"https://{host}/v8/finance/chart/{symbol}"
            for attempt in range(3):
                try:
                    resp = await client.get(url, params=_YAHOO_PARAMS)
                    resp.raise_for_status()
                    return parse_yahoo_chart(resp.json())
                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code
                    if status == 429 and attempt < 2:
                        wait_seconds = attempt + 1
                        logger.warning("%s Yahoo 429 재시도 %s/3, %s초 대기", symbol, attempt + 1, wait_seconds)
                        await asyncio.sleep(wait_seconds)
                        continue
                    errors.append(f"{host} HTTP {status}")
                    break
                except Exception as exc:
                    errors.append(f"{host} {exc}")
                    break
    raise PriceFetchError(f"Yahoo(httpx) 실패 / {' | '.join(errors)}")


def _read_url(request: urllib.request.Request) -> bytes:
    with urllib.request.urlopen(request, timeout=config.HTTP_TIMEOUT) as resp:
        return resp.read()


async def from_yahoo_urllib(symbol: str) -> list[DailyClose]:
    query = urllib.parse.urlencode(_YAHOO_PARAMS)
    errors: list[str] = []
    for host in _YAHOO_HOSTS:
        url = f"https://{host}/v8/finance/chart/{symbol}?{query}"
        request = urllib.request.Request(url, headers=config.DEFAULT_HTTP_HEADERS)
        try:
            body = await asyncio.to_thread(_read_url, request)
            return parse_yahoo_chart(json.loads(body.decode("utf-8")))
        except Exception as exc:
            errors.append(f"{host} {exc}")
    raise PriceFetchError(f"Yahoo(urllib) 실패 / {' | '.join(errors)}")


async def from_nasdaq(symbol: str) -> list[DailyClose]:
    today = timeutil.market_today()
    query = urllib.parse.urlencode(
        {
            "assetclass": "etf",
            "fromdate": (today - timedelta(days=365 * 3)).isoformat(),
            "todate": today.isoformat(),
            "limit": "1000",
        }
    )
    request = urllib.request.Request(
        f"https://api.nasdaq.com/api/quote/{symbol}/historical?{query}",
        headers={
            **config.DEFAULT_HTTP_HEADERS,
            "Origin": "https://www.nasdaq.com",
            "Referer": f"https://www.nasdaq.com/market-activity/etf/{symbol.lower()}/historical",
        },
    )
    body = await asyncio.to_thread(_read_url, request)
    return parse_nasdaq_historical(json.loads(body.decode("utf-8")))


async def _pass_stooq_verification(client: httpx.AsyncClient, html: str) -> None:
    match = re.search(r'const c="([^"]+)",d=(\d+)', html)
    if not match:
        raise PriceFetchError("Stooq 검증 페이지를 해석하지 못했습니다.")

    challenge, difficulty = match.group(1), int(match.group(2))
    nonce = await asyncio.to_thread(solve_pow_challenge, challenge, difficulty)
    resp = await client.post(
        "https://stooq.com/__verify",
        data={"c": challenge, "n": str(nonce)},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    resp.raise_for_status()


async def from_stooq(symbol: str) -> list[DailyClose]:
    url = f"https://stooq.com/q/d/l/?s={config.stooq_symbol(symbol)}&i=d"
    async with new_http_client() as client:
        resp = await client.get(url)
        resp.raise_for_status()
        text = resp.text.strip()

        lowered = text.lower()
        if lowered.startswith("<!doctype html") or "verify your browser" in lowered:
            await _pass_stooq_verification(client, text)
            resp = await client.get(url)
            resp.raise_for_status()
            text = resp.text.strip()

    return parse_stooq_csv(text)


DEFAULT_SOURCES: tuple[PriceSource, ...] = (
    from_yahoo_httpx,
    from_yahoo_urllib,
    from_nasdaq,
    from_stooq,
)


# --- 폴백 체인 ---------------------------------------------------------------


class PriceService:
    """시세 소스를 순서대로 시도하고 결과를 짧게 캐시한다.

    여러 사용자가 같은 종목을 추적하면 한 사이클에 한 번만 조회한다. 일봉은 장
    마감 후에만 바뀌므로 TTL 캐시로 충분하고, 무료 소스의 rate limit도 함께 줄인다.
    """

    def __init__(
        self,
        sources: Sequence[PriceSource] = DEFAULT_SOURCES,
        *,
        min_rows: int = config.SMA_WINDOW,
        ttl_seconds: float = config.PRICE_CACHE_TTL_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sources = tuple(sources)
        self._min_rows = min_rows
        self._ttl = ttl_seconds
        self._monotonic = monotonic
        self._cache: dict[str, tuple[float, list[DailyClose]]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def clear(self) -> None:
        self._cache.clear()

    def _cached(self, key: str) -> list[DailyClose] | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        stored_at, closes = entry
        if self._monotonic() - stored_at > self._ttl:
            del self._cache[key]
            return None
        return closes

    async def daily_closes(self, symbol: str, *, use_cache: bool = True) -> list[DailyClose]:
        key = symbol.upper()
        if use_cache:
            cached = self._cached(key)
            if cached is not None:
                return cached

        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            if use_cache:
                cached = self._cached(key)
                if cached is not None:
                    return cached

            closes = await self._fetch(key)
            self._cache[key] = (self._monotonic(), closes)
            return closes

    async def _fetch(self, symbol: str) -> list[DailyClose]:
        errors: list[str] = []
        for source in self._sources:
            name = getattr(source, "__name__", repr(source))
            try:
                rows = await source(symbol)
                closes = normalize_closes(rows, min_rows=self._min_rows)
            except Exception as exc:
                logger.warning("%s 시세 소스 실패(%s): %s", symbol, name, exc)
                errors.append(f"{name}: {exc}")
                continue

            logger.info("%s 시세 확보: %s (%s건, 기준일 %s)", symbol, name, len(closes), closes[-1].date)
            return closes

        raise PriceFetchError(f"{symbol} 일봉 조회 실패 / {' | '.join(errors)}")
