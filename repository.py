"""DB 조회/갱신 계층.

세션은 `db.session_scope()`로 열고 닫으며, 세션 팩토리가 `expire_on_commit=False`
이므로 반환된 ORM 객체는 세션이 닫힌 뒤에도 읽을 수 있는 스냅샷으로 다룬다.
쓰기는 반드시 `run()`에 넘긴 함수 안에서 수행한다.

동기 SQLAlchemy를 쓰기 때문에 async 코드에서는 `run()`이 `asyncio.to_thread`로
감싸 이벤트 루프를 막지 않는다.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timezone
from typing import Any, TypeVar

from sqlalchemy import select
from sqlalchemy.orm import Session

import config
from db import session_scope
from models import SymbolState, User
from signals import Signal

logger = logging.getLogger(__name__)

T = TypeVar("T")


async def run(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """세션을 열어 `fn(session, *args)`를 실행하고 커밋한다(별도 스레드)."""

    def _call() -> T:
        with session_scope() as session:
            return fn(session, *args, **kwargs)

    return await asyncio.to_thread(_call)


# --- 사용자 ------------------------------------------------------------------


def list_users(session: Session) -> list[User]:
    return list(session.scalars(select(User).order_by(User.user_id)))


def list_notifiable_users(session: Session) -> list[User]:
    """텔레그램 토큰과 챗ID가 모두 있는 사용자만."""
    stmt = (
        select(User)
        .where(User.telegram_token.is_not(None), User.telegram_token != "")
        .where(User.telegram_chat_id.is_not(None), User.telegram_chat_id != "")
        .order_by(User.user_id)
    )
    return list(session.scalars(stmt))


def get_user(session: Session, user_id: int) -> User | None:
    return session.get(User, user_id)


def find_user_by_chat(session: Session, telegram_token: str, chat_id: str) -> User | None:
    """롱폴링으로 받은 (토큰, 챗ID) 조합에 해당하는 사용자."""
    stmt = select(User).where(
        User.telegram_token == telegram_token,
        User.telegram_chat_id == str(chat_id),
    )
    return session.scalars(stmt).first()


# --- 종목 상태 ---------------------------------------------------------------


def get_state(session: Session, user_id: int, symbol: str) -> SymbolState | None:
    stmt = select(SymbolState).where(
        SymbolState.user_id == user_id,
        SymbolState.symbol == symbol.upper(),
    )
    return session.scalars(stmt).first()


def ensure_state(session: Session, user_id: int, symbol: str) -> SymbolState:
    """없으면 기본값으로 만들어 반환한다."""
    state = get_state(session, user_id, symbol)
    if state is not None:
        return state

    state = SymbolState(
        user_id=user_id,
        symbol=symbol.upper(),
        enabled=True,
        round_unit=config.DEFAULT_ROUND_UNIT,
        total_budget=config.DEFAULT_TOTAL_BUDGET,
        sell_splits=config.DEFAULT_SELL_SPLITS,
    )
    session.add(state)
    session.flush()
    logger.info("종목 상태 생성: user=%s symbol=%s", user_id, state.symbol)
    return state


def ensure_default_states(
    session: Session,
    user_id: int,
    symbols: Iterable[str] = config.DEFAULT_TRACKED_SYMBOLS,
) -> list[SymbolState]:
    return [ensure_state(session, user_id, symbol) for symbol in symbols]


def symbol_sort_key(symbol: str) -> tuple[int, str]:
    """기본 추적 종목 순서를 먼저 지키고, 그 밖의 종목은 알파벳순.

    종목명을 생략한 명령(`/config 150 18000 120`)이 목록의 첫 종목을 쓰기 때문에
    정렬 순서가 곧 기본 종목이 된다. 알파벳순이면 QLD가 기본이 되어버린다.
    """
    try:
        return (config.DEFAULT_TRACKED_SYMBOLS.index(symbol), symbol)
    except ValueError:
        return (len(config.DEFAULT_TRACKED_SYMBOLS), symbol)


def list_states(session: Session, user_id: int, *, enabled_only: bool = False) -> list[SymbolState]:
    stmt = select(SymbolState).where(SymbolState.user_id == user_id)
    if enabled_only:
        stmt = stmt.where(SymbolState.enabled.is_(True))
    return sorted(session.scalars(stmt), key=lambda state: symbol_sort_key(state.symbol))


def tracked_symbols(session: Session, user_id: int) -> list[str]:
    return [state.symbol for state in list_states(session, user_id, enabled_only=True)]


def tracked_or_seed(session: Session, user_id: int) -> list[str]:
    """추적 종목을 돌려준다. 상태가 하나도 없는 신규 사용자면 기본 종목을 심는다.

    사용자가 모든 종목을 /untrack 한 경우는 의도한 상태이므로 다시 심지 않는다.
    """
    states = list_states(session, user_id)
    if not states:
        states = ensure_default_states(session, user_id)
    return [state.symbol for state in states if state.enabled]


def all_tracked_symbols(session: Session) -> list[str]:
    """모든 사용자가 추적 중인 종목의 합집합. 시세 선반영(prefetch)용."""
    stmt = select(SymbolState.symbol).where(SymbolState.enabled.is_(True)).distinct()
    return sorted(session.scalars(stmt), key=symbol_sort_key)


# --- 갱신 --------------------------------------------------------------------


def set_holdings(
    session: Session,
    user_id: int,
    symbol: str,
    *,
    avg_price: float,
    shares: int,
    current_round: int,
) -> SymbolState:
    state = ensure_state(session, user_id, symbol)
    state.avg_price = avg_price
    state.shares = shares
    state.current_round = current_round
    state.total_invested = round(avg_price * shares, 2)
    state.enabled = True
    return state


def set_strategy(
    session: Session,
    user_id: int,
    symbol: str,
    *,
    round_unit: float,
    total_budget: float,
    sell_splits: int,
) -> SymbolState:
    state = ensure_state(session, user_id, symbol)
    state.round_unit = round_unit
    state.total_budget = total_budget
    state.sell_splits = max(1, sell_splits)
    return state


def set_enabled(session: Session, user_id: int, symbol: str, enabled: bool) -> SymbolState:
    state = ensure_state(session, user_id, symbol)
    state.enabled = enabled
    return state


def record_sent_signal(session: Session, user_id: int, signal: Signal) -> SymbolState:
    """신호 발송에 성공한 뒤 중복 차단용 기록을 남긴다."""
    state = ensure_state(session, user_id, signal.symbol)
    state.last_signal_date = signal.market_date
    state.last_signal_hash = signal.fingerprint
    state.last_signal_action = signal.action
    state.last_price_source = signal.price_source
    state.last_notified_at = datetime.now(timezone.utc)
    return state


def snapshot_states(states: Sequence[SymbolState]) -> list[dict[str, Any]]:
    """로깅/디버그용 평면 딕셔너리 변환."""
    return [
        {
            "symbol": state.symbol,
            "enabled": state.enabled,
            "round_unit": state.round_unit,
            "sell_splits": state.sell_splits,
            "shares": state.shares,
            "avg_price": state.avg_price,
            "last_signal_date": state.last_signal_date,
            "last_signal_action": state.last_signal_action,
            "last_price_source": state.last_price_source,
        }
        for state in states
    ]
