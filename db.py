"""DB 엔진과 세션 관리."""

from __future__ import annotations

import logging
from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

import config
from models import Base, SymbolState

logger = logging.getLogger(__name__)

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    """프로세스 단위로 공유하는 엔진."""
    global _engine
    if _engine is None:
        _engine = create_engine(
            config.DB_URL,
            echo=config.DB_ECHO,
            pool_pre_ping=True,  # MySQL 유휴 커넥션 끊김 대비
            pool_recycle=3600,
            future=True,
        )
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _session_factory


def configure(url: str, *, echo: bool = False) -> None:
    """엔진을 특정 URL로 교체한다. 테스트에서 SQLite로 바꿔 끼울 때 사용."""
    global _engine, _session_factory
    dispose()
    _engine = create_engine(url, echo=echo, future=True)
    _session_factory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)


def dispose() -> None:
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


@contextmanager
def session_scope() -> Generator[Session]:
    """커밋/롤백/클로즈를 보장하는 세션 컨텍스트."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db() -> None:
    """이 서비스가 추가한 테이블만 없을 때 생성한다.

    기존 `user` 테이블은 이미 존재하므로 건드리지 않는다(create_all은 누락된
    테이블만 만든다).
    """
    Base.metadata.create_all(bind=get_engine(), tables=[SymbolState.__table__])
    logger.info("DB 초기화 완료: %s", SymbolState.__tablename__)
