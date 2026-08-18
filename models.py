"""SQLAlchemy 모델.

`user` 테이블은 기존 스키마를 그대로 매핑한다. `symbol_state`는 이 서비스가
사용자별 종목 전략과 마지막 발송 신호를 기록하기 위해 추가한 테이블이며,
`init_db()`가 없을 때만 생성한다.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import declarative_base, relationship

import config

Base = declarative_base()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "user"

    user_id = Column(Integer, primary_key=True, autoincrement=True)
    user_name = Column(String(50))
    app_key = Column(String(255), nullable=False)
    app_secret = Column(String(255), nullable=False)
    cano = Column(String(20), nullable=False)
    acnt_prdt_cd = Column(String(5), nullable=False)
    telegram_token = Column(String(255))
    telegram_chat_id = Column(String(50))
    created_at = Column(DateTime, default=_utcnow)

    states = relationship(
        "SymbolState",
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    @property
    def display_name(self) -> str:
        return self.user_name or f"user#{self.user_id}"

    @property
    def can_notify(self) -> bool:
        """텔레그램 발송에 필요한 값이 모두 채워져 있는지."""
        return bool(self.telegram_token and self.telegram_chat_id)

    def __repr__(self) -> str:  # pragma: no cover - 디버깅 편의용
        return f"<User {self.user_id} {self.user_name!r}>"


class SymbolState(Base):
    """사용자 × 종목 단위의 전략 설정과 마지막 신호 기록."""

    __tablename__ = "symbol_state"
    __table_args__ = (UniqueConstraint("user_id", "symbol", name="uq_symbol_state_user_symbol"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("user.user_id", ondelete="CASCADE"), nullable=False)
    symbol = Column(String(20), nullable=False)

    # 전략 설정
    enabled = Column(Boolean, nullable=False, default=True)
    round_unit = Column(Float, nullable=False, default=config.DEFAULT_ROUND_UNIT)
    total_budget = Column(Float, nullable=False, default=config.DEFAULT_TOTAL_BUDGET)
    sell_splits = Column(Integer, nullable=False, default=config.DEFAULT_SELL_SPLITS)

    #: 분할매도 기준 이격도(%). NULL이면 config.SELL_THRESHOLD_PCT 기본값을 따른다.
    #: 사용자가 /track 으로 직접 지정한 경우에만 값이 들어간다.
    sell_threshold_pct = Column(Float, nullable=True)

    # 보유 현황 (사용자가 /update 로 직접 입력)
    avg_price = Column(Float, nullable=False, default=0.0)
    shares = Column(Integer, nullable=False, default=0)
    current_round = Column(Integer, nullable=False, default=1)
    total_invested = Column(Float, nullable=False, default=0.0)

    # 중복 발송 차단용 마지막 신호 기록
    last_signal_date = Column(String(10), nullable=False, default="")
    last_signal_hash = Column(String(64), nullable=False, default="")
    last_signal_action = Column(String(10), nullable=False, default="")
    last_price_source = Column(String(20), nullable=False, default="")
    last_notified_at = Column(DateTime)

    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    user = relationship("User", back_populates="states")

    def __repr__(self) -> str:  # pragma: no cover - 디버깅 편의용
        return f"<SymbolState user={self.user_id} {self.symbol} enabled={self.enabled}>"
