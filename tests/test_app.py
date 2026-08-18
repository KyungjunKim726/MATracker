"""자동 발송(broadcast_signals) 경로 테스트.

텔레그램 클라이언트는 `client_for`를 갈아끼워 대체하고, 시세는 가짜 소스를 주입한다.
"""

from __future__ import annotations

import pytest

import repository
from app import MATrackerService
from prices import PriceService

from conftest import closes_ending_today
from test_repository import add_user


class FakeClient:
    """TelegramClient 대역. 보낸 메시지를 모아두고 성공/실패를 흉내낸다."""

    def __init__(self, *, ok: bool = True) -> None:
        self.sent: list[tuple[str, str]] = []
        self.ok = ok

    async def send(self, chat_id: str, text: str) -> bool:
        self.sent.append((str(chat_id), text))
        return self.ok

    @property
    def symbols_sent(self) -> list[str]:
        """보낸 메시지에서 종목명만 추출 (메시지 첫 줄에 들어 있다)."""
        found = []
        for _, text in self.sent:
            for symbol in ("TQQQ", "QLD", "SOXL"):
                if symbol in text.splitlines()[0]:
                    found.append(symbol)
        return found


def make_service(*, ok: bool = True, last: float = 80.0) -> tuple[MATrackerService, FakeClient]:
    async def source(symbol: str):
        # 마지막 종가가 이동평균 아래 -> BUY, 기준일은 오늘이라 stale 판정을 피한다
        return closes_ending_today([100.0] * 119 + [last])

    service = MATrackerService(prices=PriceService([source]))
    client = FakeClient(ok=ok)
    service.client_for = lambda token: client  # type: ignore[assignment]
    return service, client


class TestNewUser:
    async def test_user_with_no_symbols_gets_defaults_seeded(self, sqlite_db):
        """DB에 사용자만 추가하면 /start 없이도 다음 발송부터 받는다."""
        user_id = add_user(user_name="신규", telegram_chat_id="7001")
        assert await repository.run(repository.list_states, user_id) == []

        service, client = make_service()
        await service.broadcast_signals()

        assert client.symbols_sent == ["TQQQ", "QLD", "SOXL"]
        assert [state.symbol for state in await repository.run(repository.list_states, user_id)] == [
            "TQQQ",
            "QLD",
            "SOXL",
        ]

    async def test_message_goes_to_that_users_chat(self, sqlite_db):
        add_user(user_name="신규", telegram_chat_id="7001")
        service, client = make_service()
        await service.broadcast_signals()

        assert {chat_id for chat_id, _ in client.sent} == {"7001"}
        assert "분할매수" in client.sent[0][1]

    async def test_user_without_telegram_is_skipped(self, sqlite_db):
        add_user(user_name="설정없음", telegram_token="", telegram_chat_id="")
        service, client = make_service()
        await service.broadcast_signals()

        assert client.sent == []


class TestSeedingBoundaries:
    async def test_untracked_everything_stays_empty(self, sqlite_db):
        """전부 /untrack 한 사용자는 의도한 상태이므로 다시 심지 않는다."""
        user_id = add_user(telegram_chat_id="7002")
        await repository.run(repository.ensure_default_states, user_id)
        for symbol in ("TQQQ", "QLD", "SOXL"):
            await repository.run(repository.set_enabled, user_id, symbol, False)

        service, client = make_service()
        await service.broadcast_signals()

        assert client.sent == []

    async def test_only_enabled_symbols_are_sent(self, sqlite_db):
        user_id = add_user(telegram_chat_id="7003")
        await repository.run(repository.ensure_default_states, user_id)
        await repository.run(repository.set_enabled, user_id, "QLD", False)

        service, client = make_service()
        await service.broadcast_signals()

        assert client.symbols_sent == ["TQQQ", "SOXL"]


class TestDuplicateAndFailure:
    async def test_second_run_sends_nothing(self, sqlite_db):
        add_user(telegram_chat_id="7004")

        service, client = make_service()
        await service.broadcast_signals()
        assert len(client.sent) == 3

        service2, client2 = make_service()
        await service2.broadcast_signals()
        assert client2.sent == []  # 같은 기준일·판정이라 중복으로 걸러진다

    async def test_failed_send_is_not_recorded(self, sqlite_db):
        """발송이 실패하면 기록을 남기지 않아 다음 실행에서 재시도된다."""
        user_id = add_user(telegram_chat_id="7005")

        service, client = make_service(ok=False)
        await service.broadcast_signals()
        assert len(client.sent) == 3

        state = await repository.run(repository.get_state, user_id, "TQQQ")
        assert state.last_signal_hash == ""
        assert state.last_notified_at is None

        service2, client2 = make_service(ok=True)
        await service2.broadcast_signals()
        assert len(client2.sent) == 3

        state = await repository.run(repository.get_state, user_id, "TQQQ")
        assert state.last_signal_hash != ""

    async def test_stale_quote_is_not_sent(self, sqlite_db):
        from conftest import flat_closes

        add_user(telegram_chat_id="7006")

        async def old_source(symbol: str):
            return flat_closes(100.0)  # 2026-01 고정 날짜 -> 오래된 시세

        service = MATrackerService(prices=PriceService([old_source]))
        client = FakeClient()
        service.client_for = lambda token: client  # type: ignore[assignment]

        await service.broadcast_signals()
        assert client.sent == []


class TestMultipleUsers:
    async def test_each_user_gets_their_own_message(self, sqlite_db):
        add_user(user_name="A", telegram_token="111:AAA", telegram_chat_id="8001")
        add_user(user_name="B", telegram_token="222:BBB", telegram_chat_id="8002")

        service, client = make_service()
        await service.broadcast_signals()

        assert {chat_id for chat_id, _ in client.sent} == {"8001", "8002"}
        assert len(client.sent) == 6  # 2명 × 3종목

    async def test_user_threshold_changes_their_message_only(self, sqlite_db):
        """같은 시세라도 사용자별 매도 기준에 따라 판정이 달라진다."""
        first = add_user(user_name="A", telegram_token="111:AAA", telegram_chat_id="8001")
        second = add_user(user_name="B", telegram_token="222:BBB", telegram_chat_id="8002")
        for user_id in (first, second):
            await repository.run(repository.ensure_default_states, user_id)
            for symbol in ("QLD", "SOXL"):
                await repository.run(repository.set_enabled, user_id, symbol, False)
        await repository.run(repository.track_symbol, second, "TQQQ", sell_threshold_pct=25.0)

        # 이격도 약 29.7%: 기본 30%면 관망, 25%로 낮춘 사용자는 분할매도
        service, client = make_service(last=130.0)
        await service.broadcast_signals()

        by_chat = {chat_id: text for chat_id, text in client.sent}
        assert "관망" in by_chat["8001"]
        assert "분할매도" in by_chat["8002"]


class TestNoUsers:
    async def test_empty_user_table_is_handled(self, sqlite_db):
        service, client = make_service()
        await service.broadcast_signals()
        assert client.sent == []


@pytest.fixture(autouse=True)
def _no_startup_notice(monkeypatch):
    """이 파일의 테스트는 broadcast 만 검증한다. 시작 알림은 끈다."""
    monkeypatch.setattr("config.SEND_STARTUP_NOTICE", False, raising=False)
