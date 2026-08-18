from __future__ import annotations

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine

import db
from models import Base
from values import DailyClose


@event.listens_for(Engine, "connect")
def _enable_sqlite_fk(dbapi_connection, connection_record):
    """SQLite에서도 ForeignKey/CASCADE가 동작하게 한다."""
    if dbapi_connection.__class__.__module__.startswith("sqlite3"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


@pytest.fixture()
def sqlite_db(tmp_path):
    """테스트용 SQLite DB로 엔진을 교체한다(MySQL 없이 리포지토리 검증)."""
    db.configure(f"sqlite+pysqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(bind=db.get_engine())
    try:
        yield db
    finally:
        db.dispose()


def make_closes(prices: list[float], *, start_day: int = 1, source: str = "test") -> list[DailyClose]:
    """`2026-01-01`부터 하루씩 증가하는 종가 시계열을 만든다."""
    rows: list[DailyClose] = []
    for offset, price in enumerate(prices):
        day = start_day + offset
        month, day_of_month = divmod(day - 1, 28)
        rows.append(
            DailyClose(
                date=f"2026-{month + 1:02d}-{day_of_month + 1:02d}",
                close=price,
                source=source,
            )
        )
    return rows


def flat_closes(price: float, count: int = 120, **kwargs) -> list[DailyClose]:
    return make_closes([price] * count, **kwargs)
