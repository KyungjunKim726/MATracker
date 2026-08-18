"""120일 이동평균선 신호 계산.

네트워크와 DB에 의존하지 않는 순수 로직만 둔다.

판정 규칙
    종가 < 120일선                  -> BUY  (1회치 분할매수)
    종가 >= 120일선, 이격도 >= 기준  -> SELL (1회치 분할매도)
    그 외                            -> HOLD (관망)

이격도(%) = (종가 / 120일선 - 1) * 100
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime
from statistics import fmean
from typing import Protocol, Sequence

import config
from values import DailyClose

BUY = "BUY"
SELL = "SELL"
HOLD = "HOLD"

ACTION_LABELS = {
    BUY: "분할매수",
    SELL: "분할매도",
    HOLD: "관망",
}


class StrategyInput(Protocol):
    """신호 계산에 필요한 전략값. `SymbolState`가 그대로 만족한다."""

    round_unit: float
    shares: int
    sell_splits: int


@dataclass(frozen=True, slots=True)
class Signal:
    symbol: str
    market_date: str
    price_source: str
    close: float
    sma: float
    deviation_pct: float
    sell_threshold_pct: float
    action: str
    round_unit: float
    estimated_buy_shares: int
    shares: int
    sell_splits: int
    suggested_sell_shares: int
    window: int = config.SMA_WINDOW

    @property
    def action_label(self) -> str:
        return ACTION_LABELS.get(self.action, self.action)

    @property
    def is_above_sma(self) -> bool:
        return self.close >= self.sma

    @property
    def fingerprint(self) -> str:
        """같은 신호를 두 번 보내지 않기 위한 지문."""
        raw = "|".join(
            [
                self.symbol,
                self.market_date,
                self.action,
                f"{self.close:.4f}",
                f"{self.sma:.4f}",
                f"{self.deviation_pct:.4f}",
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def moving_average(closes: Sequence[DailyClose], window: int = config.SMA_WINDOW) -> float:
    """마지막 `window` 개 종가의 단순이동평균."""
    if len(closes) < window:
        raise ValueError(f"{window}일 이동평균을 계산하려면 최소 {window}개 종가가 필요합니다. (현재 {len(closes)}개)")
    return fmean(item.close for item in closes[-window:])


def calculate_signal(
    symbol: str,
    closes: Sequence[DailyClose],
    strategy: StrategyInput,
    *,
    window: int = config.SMA_WINDOW,
) -> Signal:
    """최신 종가와 이동평균을 비교해 매매 판정을 만든다."""
    sma = moving_average(closes, window)
    latest = closes[-1]
    close = latest.close
    if close <= 0:
        raise ValueError(f"{symbol} 최신 종가가 유효하지 않습니다: {close}")

    deviation_pct = ((close / sma) - 1.0) * 100.0
    threshold = config.sell_threshold_pct(symbol)

    if close < sma:
        action = BUY
    elif deviation_pct >= threshold:
        action = SELL
    else:
        action = HOLD

    round_unit = float(strategy.round_unit or 0.0)
    estimated_buy_shares = int(round_unit // close) if round_unit > 0 else 0

    shares = int(strategy.shares or 0)
    sell_splits = max(1, int(strategy.sell_splits or config.DEFAULT_SELL_SPLITS))
    suggested_sell_shares = max(1, shares // sell_splits) if shares > 0 else 0

    return Signal(
        symbol=symbol.upper(),
        market_date=latest.date,
        price_source=latest.source,
        close=close,
        sma=sma,
        deviation_pct=deviation_pct,
        sell_threshold_pct=threshold,
        action=action,
        round_unit=round_unit,
        estimated_buy_shares=estimated_buy_shares,
        shares=shares,
        sell_splits=sell_splits,
        suggested_sell_shares=suggested_sell_shares,
        window=window,
    )


def is_duplicate_signal(
    signal: Signal,
    *,
    last_signal_hash: str = "",
    last_signal_date: str = "",
    last_signal_action: str = "",
) -> bool:
    """이미 보낸 신호와 같은지 판단한다.

    지문이 같으면 당연히 중복이고, 같은 기준일에 같은 판정이면 가격만 미세하게
    달라진 경우이므로 역시 보내지 않는다.
    """
    if last_signal_hash and last_signal_hash == signal.fingerprint:
        return True
    return bool(
        last_signal_date
        and last_signal_date == signal.market_date
        and last_signal_action == signal.action
    )


def is_stale_market_date(
    market_date: str,
    *,
    today: date,
    max_age_days: int = config.STALE_MARKET_DAYS,
) -> bool:
    """시세 기준일이 너무 오래됐는지. 파싱 실패도 신뢰할 수 없으므로 stale 처리."""
    try:
        market_day = datetime.strptime(market_date, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return True
    return (today - market_day).days > max_age_days
